"""Named GPU reservations over the network (contract gpu-lease/2): only the holder's requests use the GPU."""
import json
import threading
import time
from types import SimpleNamespace
from urllib import error, request

import pytest

from test_gateway_gpu_coordination import gateway, requires_linux_flock, serving
from tare_node.gpu_lease import SharedGpuLease

CHAT = {"model": "coding", "messages": [{"role": "user", "content": "Hi"}]}


def call(endpoint, path, body=None, *, method="POST", token=None, reservation=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if reservation:
        headers[gateway.RESERVATION_HEADER] = reservation
    req = request.Request(endpoint + path, data=json.dumps(body or {}).encode(), headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response), dict(response.headers)
    except error.HTTPError as exc:
        return exc.code, json.load(exc), dict(exc.headers)


def status(endpoint):
    with request.urlopen(endpoint + "/v1/fleet/status", timeout=5) as response:
        return json.load(response)["gpu_coordination"]


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "GPU_API_TOKEN", "node-token")
    monkeypatch.setattr(gateway, "RESERVATION", None)
    monkeypatch.setattr(gateway, "TTL_RANGE", (1, 600))
    with serving(lock_path=tmp_path / "gpu.lock", route_timeout=3.0) as rt:
        rt.state_dir = tmp_path
        yield rt, tmp_path / "gpu.lock"


@requires_linux_flock
def test_a_reservation_lets_only_its_holder_use_the_gpu_until_it_is_released(node):
    rt, lock = node
    assert call(rt.endpoint, "/v1/gpu/reservations", {"name": "bench", "max_seconds": 60})[0] == 401
    code, made, _ = call(rt.endpoint, "/v1/gpu/reservations", {"name": "bench", "holder": "acer", "max_seconds": 60,
                                                               "ttl_seconds": 30, "wait_seconds": 2}, token="node-token")
    assert code == 201 and made["fence"] == 1 and made["secret"]
    shown = status(rt.endpoint)
    assert shown["reservation"]["name"] == "bench" and shown["reservation"]["holder"] == "acer"
    assert 50 <= shown["eta_seconds"] <= 60 and made["secret"] not in json.dumps(shown)
    code, refused, headers = call(rt.endpoint, "/v1/chat/completions", CHAT)  # my other sessions, a client elsewhere
    assert code == 503 and refused["error"]["type"] == "gpu_reserved" and 50 <= int(headers["Retry-After"]) <= 60
    assert call(rt.endpoint, "/v1/chat/completions", CHAT, reservation=made["secret"])[0] == 200  # the bench
    with pytest.raises(TimeoutError):  # a training job waits for the reservation, it does not cut it
        with SharedGpuLease(lock, timeout=0.5).hold("image", "training"):
            pass
    again = call(rt.endpoint, "/v1/gpu/reservations", {"name": "other", "max_seconds": 60}, token="node-token")
    assert again[0] == 409 and again[1]["error"]["type"] == "gpu_reserved"
    path = "/v1/gpu/reservations/" + made["id"]
    assert call(rt.endpoint, path + "/renew", {"secret": made["secret"], "fence": 0}, token="node-token")[0] == 409
    assert call(rt.endpoint, path + "/renew", {"secret": made["secret"], "fence": 1}, token="node-token")[0] == 200
    assert call(rt.endpoint, path, {"secret": made["secret"], "fence": 1}, method="DELETE", token="node-token")[0] == 200
    assert "reservation" not in status(rt.endpoint)
    with SharedGpuLease(lock, timeout=2).hold("image", "training"):  # free again
        pass
    assert call(rt.endpoint, "/v1/chat/completions", CHAT)[0] == 200
    nxt = call(rt.endpoint, "/v1/gpu/reservations", {"name": "next", "max_seconds": 5, "wait_seconds": 2},
               token="node-token")
    assert nxt[0] == 201 and nxt[1]["fence"] == 2  # the number only grows
    assert (rt.state_dir / "gpu-fence").read_text() == "2"


@requires_linux_flock
def test_a_holder_that_stops_renewing_frees_the_gpu_at_its_ttl(node):
    rt, lock = node
    code, made, _ = call(rt.endpoint, "/v1/gpu/reservations", {"name": "bench", "max_seconds": 600, "ttl_seconds": 1,
                                                               "wait_seconds": 2}, token="node-token")
    assert code == 201
    time.sleep(1.3)  # no renewal
    assert call(rt.endpoint, "/v1/chat/completions", CHAT)[0] == 200
    with SharedGpuLease(lock, timeout=2).hold("image", "training"):
        pass
    gone = call(rt.endpoint, "/v1/gpu/reservations/" + made["id"] + "/renew",
                {"secret": made["secret"], "fence": made["fence"]}, token="node-token")
    assert gone[0] == 404


@requires_linux_flock
def test_a_reservation_waits_for_a_running_job_only_as_long_as_asked(node):
    rt, lock = node
    taken, release = threading.Event(), threading.Event()
    def training():
        with SharedGpuLease(lock, timeout=2).hold("image", "training"):
            taken.set()
            release.wait(5)
    worker = threading.Thread(target=training)
    worker.start()
    try:
        assert taken.wait(3)
        code, busy, headers = call(rt.endpoint, "/v1/gpu/reservations", {"name": "bench", "max_seconds": 60,
                                                                         "wait_seconds": 1}, token="node-token")
        assert code == 503 and busy["error"]["type"] == "gpu_busy" and "Retry-After" in headers
        assert "reservation" not in status(rt.endpoint)
    finally:
        release.set()
        worker.join(5)


def test_reservations_are_off_without_a_node_token(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "GPU_API_TOKEN", None)
    with serving(lock_path=tmp_path / "gpu.lock") as rt:
        code, body, _ = call(rt.endpoint, "/v1/gpu/reservations", {"name": "bench", "max_seconds": 60}, token="x")
    assert code == 404 and body["error"]["type"] == "reservations_disabled"


@requires_linux_flock
def test_a_request_running_when_the_reservation_ends_keeps_only_its_own_grace(tmp_path):
    lock = tmp_path / "gpu.lock"
    lease = SharedGpuLease(lock, timeout=2)
    lease.reserve("r1", 600, wait=1, reason="bench")
    inside, finish = threading.Event(), threading.Event()
    def holder_request():
        with lease.hold("text", "req", timeout=1, linger=0.2):
            inside.set()
            finish.wait(5)
    worker = threading.Thread(target=holder_request)
    worker.start()
    assert inside.wait(3)
    assert lease.end_reservation("r1", wait=0.1)  # the request holds the lease: it cannot close the grace now
    finish.set()
    worker.join(5)
    with SharedGpuLease(lock, timeout=2).hold("image", "training"):  # free after the request's own 0.2 s grace
        pass
    assert not lease.end_reservation("r1")  # already over


@requires_linux_flock
def test_stale_free_observation_cannot_replace_a_concurrent_reservation(tmp_path, monkeypatch):
    lock = tmp_path / "gpu.lock"
    lease = SharedGpuLease(lock, timeout=2)
    monkeypatch.setattr(gateway, "RUNTIME", SimpleNamespace(gpu_lease=lease, state_dir=tmp_path), raising=False)
    monkeypatch.setattr(gateway, "RESERVATION", None)
    monkeypatch.setattr(gateway, "FENCE", 0)
    assert not gateway.RESERVING.locked()
    assert not gateway.RESERVATION_LOCK.locked()

    observed_free, resume = threading.Event(), threading.Event()
    active_reservation = gateway.active_reservation
    results, errors = [], []

    def paused_observation():
        current = active_reservation()
        if threading.current_thread() is worker and not observed_free.is_set():
            assert current is None
            observed_free.set()
            assert resume.wait(5), "the stale admission was not resumed"
        return current

    def stale_admission():
        try:
            results.append(gateway.reserve_request(
                {"name": "stale", "holder": "A", "max_seconds": 60, "wait_seconds": 1}, "A"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(gateway, "active_reservation", paused_observation)
    worker = threading.Thread(target=stale_admission)
    worker.start()
    try:
        assert observed_free.wait(5), "A did not observe the free GPU"
        code, made, _ = gateway.reserve_request(
            {"name": "first", "holder": "B", "max_seconds": 60, "wait_seconds": 1}, "B")
        assert code == 201 and made["fence"] == 1
        first = active_reservation()
        assert first["holder"] == "B" and first["id"] == made["id"]
        owner = lease.status()["owner"]
        assert owner["request_id"] == made["id"]

        resume.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors, errors
        assert len(results) == 1
        code, refused, headers = results[0]
        assert code == 409 and refused["error"]["type"] == "gpu_reserved"
        assert "Retry-After" in headers
        assert active_reservation() is first
        assert lease.status()["owner"] == owner
        assert gateway.FENCE == 1
        assert (tmp_path / "gpu-fence").read_text() == "1"
        credentials = {"secret": made["secret"], "fence": made["fence"]}
        current, refusal = gateway.held_reservation(made["id"], credentials)
        assert current is first and refusal is None
        assert gateway.renew_request(made["id"], credentials)[0] == 200
        assert active_reservation() is first
    finally:
        resume.set()
        worker.join(5)
        current = active_reservation()
        if current is not None:
            gateway.end_reservation(current, "test cleanup")

    assert not worker.is_alive()
    assert gateway.RESERVATION is None
    assert not gateway.RESERVING.locked()
    assert not gateway.RESERVATION_LOCK.locked()
    assert not lease.status()["held"]
    with SharedGpuLease(lock, timeout=1).hold("image", "cleanup-check"):
        pass


@pytest.fixture
def fence_runtime(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    lock = tmp_path / "gpu.lock"
    lease = SharedGpuLease(lock, timeout=1)
    monkeypatch.setattr(gateway, "RUNTIME", SimpleNamespace(gpu_lease=lease, state_dir=state), raising=False)
    monkeypatch.setattr(gateway, "RESERVATION", None)
    monkeypatch.setattr(gateway, "FENCE", 0)
    try:
        yield state / "gpu-fence", lease, lock
    finally:
        current = gateway.active_reservation()
        if current is not None:
            gateway.end_reservation(current, "test cleanup")


def assert_fence_refusal(runtime, reason):
    path, lease, lock = runtime
    code, body, _ = gateway.reserve_request(
        {"name": "private-name", "holder": "private-holder", "max_seconds": 60, "wait_seconds": 1}, "local")
    assert code == 503 and body["error"]["type"] == reason
    public = json.dumps(body)
    assert str(path) not in public and "private-" not in public and "secret" not in public
    assert gateway.RESERVATION is None
    assert gateway.FENCE == 0
    assert not gateway.RESERVING.locked()
    assert not gateway.RESERVATION_LOCK.locked()
    assert not lease.status()["held"]
    with SharedGpuLease(lock, timeout=1).hold("image", "after-refusal"):
        pass


@requires_linux_flock
@pytest.mark.parametrize("failure", ["read", "write", "fsync", "replace"])
def test_fence_storage_failure_refuses_without_taking_gpu(fence_runtime, monkeypatch, failure):
    path, _, _ = fence_runtime
    path.write_text("41", encoding="ascii")

    def unavailable(*args, **kwargs):
        raise PermissionError("private storage diagnostic")

    with monkeypatch.context() as patch:
        if failure == "read":
            patch.setattr(type(path), "read_text", unavailable)
        elif failure == "write":
            patch.setattr(gateway.tempfile, "NamedTemporaryFile", unavailable)
        else:
            original = getattr(gateway.os, failure)
            failed = False

            def fail_counter_once(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    return unavailable(*args, **kwargs)
                # The independent lease used to verify cleanup must still work.
                return original(*args, **kwargs)

            patch.setattr(gateway.os, failure, fail_counter_once)
        assert_fence_refusal(fence_runtime, "fence_state_unavailable")
    assert path.read_bytes() == b"41"
    assert list(path.parent.iterdir()) == [path]


@requires_linux_flock
@pytest.mark.parametrize("content", [b"", b"not-a-counter", b"-1", b"1.5", b"\xff"])
def test_invalid_fence_refuses_without_taking_gpu(fence_runtime, content):
    path, _, _ = fence_runtime
    path.write_bytes(content)
    assert_fence_refusal(fence_runtime, "fence_state_invalid")
    assert path.read_bytes() == content


@requires_linux_flock
def test_read_only_state_directory_refuses_without_taking_gpu(fence_runtime):
    if gateway.os.geteuid() == 0:
        pytest.skip("permission denial requires an unprivileged user")
    path, _, _ = fence_runtime
    path.parent.chmod(0o500)
    try:
        assert_fence_refusal(fence_runtime, "fence_state_unavailable")
        assert not path.exists()
    finally:
        path.parent.chmod(0o700)


@requires_linux_flock
@pytest.mark.parametrize("stored", [None, 41])
def test_fence_is_durable_and_grows_across_runtime_restarts(fence_runtime, monkeypatch, stored):
    path, _, lock = fence_runtime
    if stored is not None:
        path.write_text(str(stored), encoding="ascii")
    previous = stored or 0
    for _ in range(2):
        # A fresh lease and in-memory counter simulate restarting with the same state.
        lease = SharedGpuLease(lock, timeout=1)
        monkeypatch.setattr(gateway, "RUNTIME", SimpleNamespace(gpu_lease=lease, state_dir=path.parent))
        monkeypatch.setattr(gateway, "FENCE", 0)
        code, made, _ = gateway.reserve_request({"name": "durable", "max_seconds": 60, "wait_seconds": 1}, "local")
        assert code == 201 and made["fence"] == previous + 1
        assert path.read_text(encoding="ascii") == str(made["fence"])
        assert lease.status()["held"]
        assert gateway.release_request(made["id"], {"secret": made["secret"], "fence": made["fence"]})[0] == 200
        assert gateway.RESERVATION is None and not lease.status()["held"]
        previous = made["fence"]

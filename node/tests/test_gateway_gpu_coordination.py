"""Tests for gateway GPU admission coordination and lifecycle boundaries."""
import contextlib
from contextlib import contextmanager
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from threading import Event, RLock, Thread
import time
from urllib import error, request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tare_node.gpu_lease import SharedGpuLease, _get_fcntl

FCNTL = _get_fcntl()
requires_linux_flock = pytest.mark.skipif(
    FCNTL is None, reason="fcntl flock requires Linux/POSIX environment"
)

spec = importlib.util.spec_from_file_location(
    "gateway_module_for_tests", ROOT / "tare_node/qualified_model_gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class MockBackend(gateway.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        runtime = gateway.RUNTIME
        runtime.calls.append((self.path, runtime.model_id))
        if self.path == "/props":
            return self.reply({
                "model_path": "/models/" + (runtime.model_id or "one"),
                "total_slots": len(runtime.windows),
                "model_alias": runtime.model_id or "one",
                "chat_template": runtime.template,
                "build_info": "fixture-build",
            })
        if self.path == "/slots":
            return self.reply([{"id": i, "n_ctx": v} for i, v in enumerate(runtime.windows)])
        if self.path == "/health":
            return self.reply({"status": "ok"})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        runtime = gateway.RUNTIME
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length))
        runtime.calls.append((self.path, runtime.model_id))
        if self.path == "/apply-template":
            return self.reply({"prompt": json.dumps(value["messages"]) + runtime.template})
        if self.path == "/tokenize":
            return self.reply({"tokens": list(range(len(value["content"].split())))})
        if self.path == "/v1/chat/completions":
            runtime.generations.append(value)
            if value.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(data):
                    self.wfile.write(b"data: " + json.dumps(data).encode() + b"\n\n")
                    self.wfile.flush()

                emit({
                    "model": runtime.model_id,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "OK"},
                        "finish_reason": None,
                    }],
                })
                runtime.stream_continue.wait(5)
                emit({
                    "model": runtime.model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                })
                emit({
                    "model": runtime.model_id,
                    "choices": [],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 1},
                })
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            return self.reply({
                "model": runtime.model_id,
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "OK"},
                }],
            })


class FixtureRuntime:
    def __init__(self, lock_path=None, route_timeout=5.0):
        self.config = {
            "fleet": {"default_model": "one"},
            "aliases": {"coding": "one"},
            "models": {
                name: {
                    "artifact": {"path": "/models/" + name, "sha256": name[0] * 64},
                    "runtime": {
                        "binary": "/bin/llama-server",
                        "args": ["--parallel", "2"],
                        "environment": {},
                    },
                }
                for name in ("one", "two")
            },
        }
        self.request_lock = RLock()
        self.gpu_lease = SharedGpuLease(lock_path, timeout=route_timeout)
        self.route_timeout = route_timeout
        self.model_id = None
        self.requested_name = None
        self.process = None
        self.last_switch_seconds = None
        self.last_error = None
        self.stop_timeout = 2.0
        self.template = "template-v1"
        self.windows = [4096, 4096]
        self.calls = []
        self.generations = []
        self.backend_host = "127.0.0.1"
        self.backend_port = 0
        self.stream_continue = Event()

    def backend_url(self, path):
        return f"http://127.0.0.1:{self.backend_port}{path}"

    def backend_health(self, timeout=2.0):
        return True

    def ensure_model(self, requested):
        name, _ = gateway.resolve_model(self.config, requested)
        self.model_id = name
        self.requested_name = requested
        if self.process is None:
            self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        return name, 0.0

    def stop_backend(self):
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            self.model_id = None
            self.requested_name = None
            return

        with contextlib.suppress(Exception):
            process.terminate()
        deadline = time.monotonic() + self.stop_timeout
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
            kill_deadline = time.monotonic() + 1.0
            while process.poll() is None and time.monotonic() < kill_deadline:
                time.sleep(0.05)
        if process.poll() is None:
            self.last_error = f"failed to stop backend pid={process.pid}"
            raise RuntimeError(f"backend process {process.pid} could not be stopped")
        self.process = None
        self.model_id = None
        self.requested_name = None

    def status(self):
        lease_status = self.gpu_lease.status()
        return {
            "status": "ok",
            "role": "qualified-model-gateway",
            "current_model": self.model_id,
            "requested_name": self.requested_name,
            "backend_healthy": True,
            "backend_pid": self.process.pid if self.process and self.process.poll() is None else None,
            "backend_port": self.backend_port,
            "last_switch_seconds": self.last_switch_seconds,
            "last_error": self.last_error,
            "max_resident_models": 1,
            "available_models": sorted(self.config["models"]),
            "gpu_coordination": lease_status,
        }


@contextmanager
def serving(lock_path=None, route_timeout=5.0):
    backend = gateway.ThreadingHTTPServer(("127.0.0.1", 0), MockBackend)
    runtime = FixtureRuntime(lock_path=lock_path, route_timeout=route_timeout)
    runtime.backend_port = backend.server_port
    old_runtime = getattr(gateway, "RUNTIME", None)
    gateway.RUNTIME = runtime

    public = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    threads = [Thread(target=s.serve_forever, daemon=True) for s in (backend, public)]
    for t in threads:
        t.start()
    runtime.endpoint = f"http://127.0.0.1:{public.server_port}"
    try:
        yield runtime
    finally:
        runtime.stream_continue.set()
        if runtime.process is not None:
            with contextlib.suppress(Exception):
                runtime.process.terminate()
                runtime.process.wait(timeout=1)
        for s in (public, backend):
            s.shutdown()
            s.server_close()
        for t in threads:
            t.join(timeout=2)
        gateway.RUNTIME = old_runtime


def post(endpoint, path, body):
    req = request.Request(
        endpoint + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except error.HTTPError as exc:
        return exc.code, json.load(exc)


def get(endpoint, path):
    req = request.Request(endpoint + path)
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_coordination_disabled_rejects_yield(tmp_path):
    with serving(lock_path=None) as rt:
        code, body = post(rt.endpoint, "/internal/gpu/yield", {"nonce": "abc"})
        assert code == 400
        assert body["error"]["type"] == "gpu_coordination_disabled"


def test_malformed_yield_requests_rejected(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    with serving(lock_path=lock_file) as rt:
        # Extra keys
        code, body = post(rt.endpoint, "/internal/gpu/yield", {"nonce": "abc", "extra": 1})
        assert code == 400
        assert body["error"]["type"] == "invalid_request"

        # Nonce not a string
        code, body = post(rt.endpoint, "/internal/gpu/yield", {"nonce": 123})
        assert code == 400

        # Empty nonce
        code, body = post(rt.endpoint, "/internal/gpu/yield", {"nonce": ""})
        assert code == 400


@requires_linux_flock
def test_unheld_or_wrong_nonce_yield_refused(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    with serving(lock_path=lock_file) as rt:
        # No lock held by anyone: yield must be refused
        code, body = post(rt.endpoint, "/internal/gpu/yield", {"nonce": "fake_nonce"})
        assert code == 403
        assert body["error"]["type"] == "invalid_lease_proof"


@requires_linux_flock
def test_valid_image_lease_stops_resident_text(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    with serving(lock_path=lock_file) as rt:
        # 1. Warm up resident text model
        rt.ensure_model("one")
        assert rt.model_id == "one"
        assert rt.process is not None
        child_pid = rt.process.pid

        # 2. External Comfy process acquires image lease
        comfy_lease = SharedGpuLease(lock_file)
        with comfy_lease.hold("image", "comfy-job-1") as receipt:
            nonce = receipt["nonce"]

            # 3. Comfy calls POST /internal/gpu/yield
            code, body = post(rt.endpoint, "/internal/gpu/yield", {"nonce": nonce})
            assert code == 200
            assert body == {"status": "released", "backend_pid": None}

            # 4. Verify text backend stopped and references cleared
            assert rt.model_id is None
            assert rt.process is None

            # Confirm child process actually exited
            try:
                subprocess.os.kill(child_pid, 0)
                exited = False
            except OSError:
                exited = True
            assert exited, f"child process {child_pid} did not exit"


@requires_linux_flock
def test_text_request_waits_behind_image_lease(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    with serving(lock_path=lock_file, route_timeout=5.0) as rt:
        comfy_lease = SharedGpuLease(lock_file)
        context = comfy_lease.hold("image", "comfy-job-blocking")
        receipt = context.__enter__()
        try:
            # Client sends text chat completions in a thread
            results = []

            def send_chat():
                code, resp = post(
                    rt.endpoint,
                    "/v1/chat/completions",
                    {"model": "coding", "messages": [{"role": "user", "content": "Hi"}]},
                )
                results.append((code, resp))

            t = Thread(target=send_chat)
            t.start()

            # Verify request is waiting and hasn't finished yet
            time.sleep(0.3)
            assert len(results) == 0

            # Release image lease
            context.__exit__(None, None, None)

            # Request unblocks and completes
            t.join(timeout=3.0)
            assert len(results) == 1
            code, resp = results[0]
            assert code == 200
            assert resp["choices"][0]["message"]["content"] == "OK"
            assert rt.model_id == "one"
        finally:
            with contextlib.suppress(Exception):
                context.__exit__(None, None, None)


@requires_linux_flock
def test_text_gives_way_to_a_lasting_image_job_with_gpu_busy(tmp_path, monkeypatch):
    lock_file = tmp_path / "gpu.lock"
    monkeypatch.setattr(gateway, "TEXT_YIELDS_TO_IMAGE_SECONDS", 0.3)
    with serving(lock_path=lock_file, route_timeout=30.0) as rt:
        with SharedGpuLease(lock_file).hold("image", "training", reason="cohort-26"):
            started = time.monotonic()
            code, resp = post(rt.endpoint, "/v1/chat/completions",
                              {"model": "coding", "messages": [{"role": "user", "content": "Hi"}]})
            assert code == 503 and resp["error"]["type"] == "gpu_busy"
            assert resp["error"]["retry_after_seconds"] == 60  # no estimate for this job: a minute
            assert time.monotonic() - started < 5  # not the 30 s route timeout
            assert SharedGpuLease(lock_file).status()["queue"] == []
        code, _ = post(rt.endpoint, "/v1/chat/completions",
                       {"model": "coding", "messages": [{"role": "user", "content": "Hi"}]})
        assert code == 200


@requires_linux_flock
def test_stream_retains_lease_until_done(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    with serving(lock_path=lock_file, route_timeout=5.0) as rt:
        chat_req = {
            "model": "coding",
            "messages": [{"role": "user", "content": "Stream me"}],
            "stream": True,
        }
        wire = json.dumps(chat_req).encode()
        http_req = request.Request(
            rt.endpoint + "/v1/chat/completions",
            data=wire,
            headers={"Content-Type": "application/json"},
        )

        with request.urlopen(http_req, timeout=5) as response:
            line1 = response.readline()
            assert b"data: " in line1

            # While stream is active in gateway, external image acquisition MUST fail / timeout
            comfy_lease = SharedGpuLease(lock_file)
            with pytest.raises(TimeoutError):
                with comfy_lease.hold("image", "try-during-stream", timeout=0.15):
                    pass

            # Resume stream and read to the end
            rt.stream_continue.set()
            rest = response.read()
            assert b"[DONE]" in rest

        # After stream finishes, image lease can be acquired immediately
        with comfy_lease.hold("image", "after-stream", timeout=1.0) as r:
            assert r["kind"] == "image"


def test_backend_stop_failure_keeps_reference_and_fails_closed(tmp_path):
    lock_file = tmp_path / "gpu.lock"

    class StuckProcess:
        pid = 98765

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

    stuck = StuckProcess()
    with serving(lock_path=lock_file) as rt:
        rt.process = stuck
        rt.model_id = "one"

        # stop_backend directly
        with pytest.raises(RuntimeError, match="could not be stopped"):
            rt.stop_backend()

        # Confirm references NOT cleared!
        assert rt.process is stuck
        assert rt.model_id == "one"
        assert "failed to stop backend" in rt.last_error


def test_status_reports_coordination_without_leaking_nonce(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    with serving(lock_path=lock_file) as rt:
        code, data = get(rt.endpoint, "/fleet/status")
        assert code == 200
        assert "gpu_coordination" in data
        assert data["gpu_coordination"]["enabled"] is True
        assert "nonce" not in json.dumps(data)


def test_backend_port_waits_until_bindable_again():
    import socket
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    assert gateway.port_is_bindable("127.0.0.1", port) is False
    with pytest.raises(RuntimeError, match="still unavailable"):
        gateway.wait_until_bindable("127.0.0.1", port, timeout=0.2)
    listener.close()
    gateway.wait_until_bindable("127.0.0.1", port, timeout=5)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux TCP TIME_WAIT bind semantics')
def test_reusable_backend_does_not_wait_for_server_side_time_wait():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(2)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.settimeout(2)
                accepted.sendall(b'x')
                accepted.shutdown(socket.SHUT_WR)  # the server actively closes
                assert client.recv(1) == b'x' and client.recv(1) == b''
                client.shutdown(socket.SHUT_WR)
                assert accepted.recv(1) == b''
    # Same closed listener, different real bind policies: no sleeps or mocked
    # readiness. The conservative llama-server path still sees TIME_WAIT.
    assert gateway.port_is_bindable('127.0.0.1', port) is False
    assert gateway.port_is_bindable('127.0.0.1', port, reuse_address=True) is True
    gateway.wait_until_bindable('127.0.0.1', port, timeout=0, reuse_address=True)


def test_reuse_probe_never_admits_a_live_listener():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        assert gateway.port_is_bindable('127.0.0.1', listener.getsockname()[1], reuse_address=True) is False


def test_idle_backend_is_unloaded_only_after_its_limit_and_never_mid_request():
    import time as _time
    stopped = []
    runtime = gateway.FleetRuntime.__new__(gateway.FleetRuntime)
    runtime.config = {"models": {"big": {"runtime": {"idle_unload_seconds": 900}}, "small": {"runtime": {}}}}
    runtime.request_lock = gateway.threading.RLock()
    runtime.process = object()
    runtime.model_id = "big"
    runtime.stop_backend = lambda: stopped.append(runtime.model_id)
    runtime.last_used = _time.monotonic() - 60
    assert runtime.unload_if_idle() is False and stopped == []
    runtime.last_used = _time.monotonic() - 901
    held = gateway.threading.Event(); release = gateway.threading.Event()
    def busy():
        with runtime.request_lock:
            held.set(); release.wait(5)
    t = gateway.threading.Thread(target=busy); t.start(); held.wait(5)
    assert runtime.unload_if_idle() is False and stopped == []     # a request holds the lock
    release.set(); t.join()
    assert runtime.unload_if_idle() is True and stopped == ["big"]
    runtime.model_id = "small"
    assert runtime.unload_if_idle() is False                      # no limit on this card


@requires_linux_flock
def test_an_agent_turn_keeps_its_model_when_an_image_job_arrives_between_calls(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    chat = {"model": "coding", "messages": [{"role": "user", "content": "Hi"}]}
    with serving(lock_path=lock_file, route_timeout=5.0) as rt:
        rt.text_idle_grace, rt.image_max_wait = 0.6, 300.0
        code, _ = post(rt.endpoint, "/v1/chat/completions", chat)
        assert code == 200
        pid = rt.process.pid
        yielded = []

        def image_job():
            with SharedGpuLease(lock_file, timeout=10).hold("image", "comfy-job", reason="test") as receipt:
                yielded.append(post(rt.endpoint, "/internal/gpu/yield", {"nonce": receipt["nonce"]}))

        job = Thread(target=image_job, daemon=True)
        job.start()
        queue = Path(str(lock_file) + ".queue")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
                queue.exists() and any(not n.name.startswith(".") for n in queue.iterdir())):
            time.sleep(0.02)
        time.sleep(0.1)  # the agent runs a tool between two calls of one turn
        code, resp = post(rt.endpoint, "/v1/chat/completions", chat)
        assert code == 200 and resp["choices"][0]["message"]["content"] == "OK"
        assert yielded == []
        assert rt.process is not None and rt.process.pid == pid  # the model was not stopped mid-turn
        job.join(timeout=5)
        assert yielded and yielded[0][0] == 200  # after the grace the image job takes the GPU
        assert rt.process is None


def test_a_refusal_says_how_long_text_waits_from_the_nodes_estimate():
    # Contract gpu-lease/1: Retry-After and retry_after_seconds, from the estimate as it is: a 12 h training run
    # says 12 h (43200 s), not 10 min; kept between 5 s and 24 h.
    class Lease:
        is_enabled = True
        def __init__(self, eta):
            self.eta = eta
        def status(self):
            return {"held": True, "owner": {"kind": "text"}, "queue": [
                {"kind": "image", "expected_seconds": self.eta}]}
    for eta, seconds in ((240, 240), (1, 5), (43200, 43200), (10**6, 86400), (None, 60)):
        body, headers = gateway.gpu_refusal(Lease(eta) if eta else None, "gpu_busy", "busy", "coding")
        assert body["error"]["retry_after_seconds"] == seconds and headers == {"Retry-After": str(seconds)}
        assert body["error"]["type"] == "gpu_busy" and body["error"]["model"] == "coding"

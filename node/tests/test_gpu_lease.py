"""Tests for cross-process SharedGpuLease coordination."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tare_node.gpu_lease import SharedGpuLease, _get_fcntl

FCNTL = _get_fcntl()
requires_linux_flock = pytest.mark.skipif(
    FCNTL is None, reason="fcntl flock requires Linux/POSIX environment"
)


def test_disabled_mode():
    lease = SharedGpuLease(None)
    assert lease.is_enabled is False
    assert lease.status() == {"enabled": False}
    assert lease.validate_image_lease("test_nonce") is False
    with lease.hold("text", "req-1") as receipt:
        assert receipt is None


@requires_linux_flock
def test_two_independent_processes_exclude_each_other(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    ready_file = tmp_path / "child_ready"
    exit_file = tmp_path / "child_exit"

    child_code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease

lease = SharedGpuLease({str(lock_path)!r})
with lease.hold("image", "child-proc") as receipt:
    Path({str(ready_file)!r}).write_text(receipt["nonce"])
    while not Path({str(exit_file)!r}).exists():
        time.sleep(0.05)
"""
    proc = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists(), "child process failed to acquire lease in time"
        nonce = ready_file.read_text().strip()

        parent_lease = SharedGpuLease(lock_path)
        status = parent_lease.status()
        assert status["held"] is True
        assert status["owner"]["kind"] == "image"
        assert status["owner"]["request_id"] == "child-proc"
        # Nonce must NEVER be leaked in status!
        assert "nonce" not in status
        assert "nonce" not in status.get("owner", {})

        # Parent attempts to acquire with short timeout: must fail
        with pytest.raises(TimeoutError):
            with parent_lease.hold("text", "parent-req", timeout=0.2):
                pass

        # Validate that child's image lease can be verified
        assert parent_lease.validate_image_lease(nonce) is True
        assert parent_lease.validate_image_lease("wrong_nonce") is False

        # Tell child to release
        exit_file.write_text("exit")
        proc.wait(timeout=5)

        # Parent should now be able to acquire immediately
        with parent_lease.hold("text", "parent-req", timeout=2.0) as receipt:
            assert receipt is not None
            assert receipt["kind"] == "text"
            assert receipt["request_id"] == "parent-req"
    finally:
        exit_file.write_text("exit")
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)


@requires_linux_flock
def test_timeout_raises_timeout_error(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    ready_file = tmp_path / "child_ready"
    exit_file = tmp_path / "child_exit"

    child_code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease

lease = SharedGpuLease({str(lock_path)!r})
with lease.hold("text", "child-timeout"):
    Path({str(ready_file)!r}).write_text("ready")
    while not Path({str(exit_file)!r}).exists():
        time.sleep(0.05)
"""
    proc = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists()

        lease = SharedGpuLease(lock_path)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with lease.hold("image", "timed-out", timeout=0.15):
                pass
        elapsed = time.monotonic() - started
        assert 0.1 <= elapsed < 1.0
    finally:
        exit_file.write_text("exit")
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)


@requires_linux_flock
def test_exception_releases_lock(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    lease = SharedGpuLease(lock_path)

    with pytest.raises(RuntimeError, match="deliberate_failure"):
        with lease.hold("text", "req-err"):
            raise RuntimeError("deliberate_failure")

    status = lease.status()
    assert status["held"] is False
    assert status["owner"] is None

    # Should be immediately re-acquirable
    with lease.hold("text", "req-ok", timeout=1.0) as receipt:
        assert receipt["request_id"] == "req-ok"


@requires_linux_flock
def test_process_exit_or_crash_releases_kernel_lock(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    ready_file = tmp_path / "crash_ready"

    # Child acquires and immediately aborts via os._exit(0)
    child_code = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease

lease = SharedGpuLease({str(lock_path)!r})
context = lease.hold("image", "crashed-job")
context.__enter__()
Path({str(ready_file)!r}).write_text("holding")
# Hard exit: bypass all python exit handlers / finally blocks
os._exit(0)
"""
    proc = subprocess.Popen([sys.executable, "-c", child_code])
    proc.wait(timeout=5)
    assert proc.returncode == 0
    assert ready_file.exists()

    # The lock file may still exist on disk with stale metadata,
    # but the kernel has automatically released the flock descriptor.
    parent_lease = SharedGpuLease(lock_path)
    status = parent_lease.status()
    assert status["held"] is False
    assert status["owner"] is None

    # Parent can acquire immediately without waiting
    with parent_lease.hold("text", "after-crash", timeout=1.0) as receipt:
        assert receipt["request_id"] == "after-crash"


@requires_linux_flock
def test_stale_metadata_not_active(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    # Write fake metadata into the file without holding flock
    stale_meta = {
        "kind": "image",
        "request_id": "stale-req",
        "pid": 999999,
        "nonce": "stale_nonce_12345",
        "acquired_at": time.time() - 100,
    }
    lock_path.write_text(json.dumps(stale_meta))

    lease = SharedGpuLease(lock_path)
    status = lease.status()
    # Flock was never acquired, so status MUST report held=False and owner=None
    assert status["held"] is False
    assert status["owner"] is None

    # validate_image_lease MUST return False because no external flock is held
    assert lease.validate_image_lease("stale_nonce_12345") is False


@requires_linux_flock
def test_symlink_refused(tmp_path):
    real_file = tmp_path / "real.lock"
    real_file.touch()
    symlink_file = tmp_path / "symlink.lock"
    symlink_file.symlink_to(real_file)

    lease = SharedGpuLease(symlink_file)
    with pytest.raises(ValueError, match="must not be a symlink"):
        with lease.hold("text", "req-sym"):
            pass

    with pytest.raises(ValueError, match="must not be a symlink"):
        lease.status()

    with pytest.raises(ValueError, match="must not be a symlink"):
        lease.validate_image_lease("nonce")


@requires_linux_flock
def test_cancelled_callback_before_and_during_wait(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    lease = SharedGpuLease(lock_path)

    # Cancelled before wait
    with pytest.raises(InterruptedError, match="cancelled before wait"):
        with lease.hold("text", "req-cancel-pre", cancelled=lambda: True):
            pass

    # Cancelled during wait
    ready_file = tmp_path / "child_ready"
    exit_file = tmp_path / "child_exit"

    child_code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease

lease = SharedGpuLease({str(lock_path)!r})
with lease.hold("image", "child-cancel"):
    Path({str(ready_file)!r}).write_text("ready")
    while not Path({str(exit_file)!r}).exists():
        time.sleep(0.05)
"""
    proc = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists()

        flag = {"cancelled": False}

        def check_cancelled():
            return flag["cancelled"]

        import threading

        def cancel_later():
            time.sleep(0.1)
            flag["cancelled"] = True

        t = threading.Thread(target=cancel_later)
        t.start()

        started = time.monotonic()
        with pytest.raises(InterruptedError):
            with lease.hold("text", "req-cancel-during", timeout=5.0, cancelled=check_cancelled):
                pass
        elapsed = time.monotonic() - started
        assert elapsed < 2.0  # Raised well before the 5.0s timeout
        t.join()
    finally:
        exit_file.write_text("exit")
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)


@requires_linux_flock
def test_validate_image_lease_rules(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    ready_file = tmp_path / "child_ready"
    exit_file = tmp_path / "child_exit"

    # Test text kind rejection
    child_text_code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease

lease = SharedGpuLease({str(lock_path)!r})
with lease.hold("text", "child-text") as receipt:
    Path({str(ready_file)!r}).write_text(receipt["nonce"])
    while not Path({str(exit_file)!r}).exists():
        time.sleep(0.05)
"""
    proc = subprocess.Popen([sys.executable, "-c", child_text_code])
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists()
        text_nonce = ready_file.read_text().strip()

        lease = SharedGpuLease(lock_path)
        # Even with the exact nonce, text kind cannot validate as an image lease
        assert lease.validate_image_lease(text_nonce) is False
    finally:
        exit_file.write_text("exit")
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_deadlines_must_be_finite_and_positive(tmp_path, timeout):
    with pytest.raises(ValueError, match='finite and positive'):
        SharedGpuLease(tmp_path/'gpu.lock', timeout=timeout)
    lease=SharedGpuLease(tmp_path/'gpu.lock')
    with pytest.raises(ValueError, match='finite and positive'):
        with lease.hold('text','request',timeout=timeout):
            pytest.fail('invalid wait admitted')


@requires_linux_flock
def test_request_identifier_is_bounded(tmp_path):
    lease=SharedGpuLease(tmp_path/'gpu.lock')
    with pytest.raises(ValueError, match='128'):
        with lease.hold('image','x'*129):
            pytest.fail('unbounded metadata admitted')


def _waiter(lock_path, name, order_file, exit_file, hold_seconds=0.0):
    code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease
with SharedGpuLease({str(lock_path)!r}, timeout=30).hold("text", {name!r}):
    with open({str(order_file)!r}, "a") as f:
        f.write({name!r} + "\\n")
    while {hold_seconds!r} == 0 and not Path({str(exit_file)!r}).exists():
        time.sleep(0.05)
    time.sleep({hold_seconds!r})
"""
    return subprocess.Popen([sys.executable, "-c", code])


def _queued(lock_path):
    queue = Path(str(lock_path) + ".queue")
    return [n for n in queue.iterdir() if not n.name.startswith(".")] if queue.exists() else []


@requires_linux_flock
def test_waiters_get_the_gpu_in_arrival_order(tmp_path):
    lock_path, order, release = tmp_path / "gpu.lock", tmp_path / "order", tmp_path / "release"
    procs = [_waiter(lock_path, "first", order, release)]
    try:
        deadline = time.monotonic() + 5
        while not order.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        for i, name in enumerate(["second", "third", "fourth"], start=1):
            procs.append(_waiter(lock_path, name, order, release, hold_seconds=0.2))
            while len(_queued(lock_path)) < i and time.monotonic() < deadline + 5:
                time.sleep(0.02)
        release.write_text("go")
        for proc in procs:
            proc.wait(timeout=20)
        assert order.read_text().split() == ["first", "second", "third", "fourth"]
        assert _queued(lock_path) == []
    finally:
        release.write_text("go")
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.kill()


@requires_linux_flock
def test_ticket_of_a_dead_waiter_does_not_block_the_queue(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    queue = Path(str(lock_path) + ".queue")
    queue.mkdir()
    (queue / f"{0:020d}-1-dead").write_text("")  # earlier ticket with no live lock holder
    with SharedGpuLease(lock_path).hold("text", "after-dead", timeout=2) as receipt:
        assert receipt["request_id"] == "after-dead"
    assert _queued(lock_path) == []


def _image_job(lock_path, got, hold_seconds=0.0):
    """Hold an image lease from another lease object in a thread; records when it got the GPU."""
    import threading

    def run():
        with SharedGpuLease(lock_path, timeout=30).hold("image", "image-job", reason="test"):
            got.append(time.monotonic())
            time.sleep(hold_seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not _queued(lock_path) and not got and time.monotonic() < deadline:
        time.sleep(0.02)
    return thread


@requires_linux_flock
def test_text_keeps_the_gpu_between_back_to_back_calls(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    text, got = SharedGpuLease(lock_path), []
    with text.hold("text", "call-1", linger=0.6):
        image = _image_job(lock_path, got)
    time.sleep(0.1)  # the agent runs a tool between two calls of one turn
    assert got == []
    status = text.status()
    assert status["held"] is True and status["owner"]["request_id"] == "call-1"
    assert isinstance(status["idle_since"], float)
    assert "nonce" not in json.dumps(status)
    with text.hold("text", "call-2", timeout=1, yield_to_image=0.3, linger=0.6) as receipt:
        assert receipt["request_id"] == "call-2" and got == []
    ended = time.monotonic()
    image.join(timeout=5)
    assert got and got[0] - ended >= 0.5  # the image job got the GPU only after the grace
    assert text.status()["held"] is False


@requires_linux_flock
def test_an_image_job_past_its_wait_limit_ends_the_grace_after_the_running_request(tmp_path, caplog):
    import logging
    from tare_node.gpu_lease import GpuBusy

    lock_path = tmp_path / "gpu.lock"
    text, got = SharedGpuLease(lock_path), []
    with caplog.at_level(logging.WARNING, logger="tare_node.gpu_lease"):
        with text.hold("text", "long-call", linger=30, image_max_wait=0.3):
            image = _image_job(lock_path, got, hold_seconds=1.0)
            time.sleep(0.5)  # past the limit while the request still runs
            assert got == []  # a running request is never cut
        deadline = time.monotonic() + 5
        while not got and time.monotonic() < deadline:
            time.sleep(0.02)
        assert got  # no 30 s grace
        assert "over the" in caplog.text
        with pytest.raises(GpuBusy):
            with text.hold("text", "next-call", timeout=5, yield_to_image=0.2,
                           linger=30, image_max_wait=0.3):
                pass
    image.join(timeout=5)


@requires_linux_flock
def test_process_exit_during_the_grace_releases_the_gpu(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    code = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease
lease = SharedGpuLease({str(lock_path)!r})
with lease.hold("text", "dying", linger=30):
    pass
assert lease.status()["held"] is True
os._exit(0)
"""
    proc = subprocess.run([sys.executable, "-c", code], timeout=10)
    assert proc.returncode == 0
    with SharedGpuLease(lock_path).hold("image", "after-exit", timeout=1.0) as receipt:
        assert receipt["kind"] == "image"


@requires_linux_flock
def test_without_grace_text_releases_at_once(tmp_path):
    lease = SharedGpuLease(tmp_path / "gpu.lock")
    with lease.hold("text", "plain", linger=0, image_max_wait=300):
        pass
    status = lease.status()
    assert status["held"] is False and "idle_since" not in status


@pytest.mark.parametrize("options", [{"linger": -1}, {"linger": float("nan")}, {"linger": float("inf")},
                                     {"image_max_wait": 0}, {"image_max_wait": float("nan")}])
def test_grace_and_image_limit_are_validated(tmp_path, options):
    with pytest.raises(ValueError):
        with SharedGpuLease(tmp_path / "gpu.lock").hold("text", "bad", **options):
            pytest.fail("invalid grace admitted")


@requires_linux_flock
def test_a_probe_reusing_the_grace_never_shortens_it(tmp_path):
    lock_path = tmp_path / "gpu.lock"
    lease, got = SharedGpuLease(lock_path), []
    with lease.hold("text", "turn-call", linger=1.0, image_max_wait=300):
        pass
    ended = time.monotonic()
    image = _image_job(lock_path, got)
    # The local judge probes the resident model with no grace of its own, during another turn's grace.
    with lease.hold("text", "judge-probe", timeout=0.15, yield_to_image=5, linger=0, image_max_wait=300):
        pass
    time.sleep(0.3)
    assert got == []  # the probe kept the turn's grace instead of releasing the GPU
    assert lease.status()["held"] is True
    image.join(timeout=5)
    assert got and got[0] - ended >= 0.9  # the image job waited the whole original grace
    # A probe that finds the GPU free starts no grace of its own.
    with lease.hold("text", "free-probe", timeout=0.15, linger=0, image_max_wait=300):
        pass
    status = lease.status()
    assert status["held"] is False and "idle_since" not in status

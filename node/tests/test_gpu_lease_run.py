"""Linux subprocess integration tests for tare_node/gpu_lease_run.py."""
from __future__ import annotations

import contextlib
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
from threading import Thread
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tare_node.gpu_lease import SharedGpuLease, _get_fcntl
from test_gateway_gpu_coordination import post, serving

FCNTL = _get_fcntl()
requires_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux") or FCNTL is None,
    reason="Linux environment with fcntl flock and prctl subreaper required",
)
pytestmark = requires_linux

CLI_PATH = ROOT / "tare_node/gpu_lease_run.py"


def wait_until(predicate, timeout=5.0, interval=0.02, msg="condition not met"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = predicate()
        if res:
            return res
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting ({timeout}s): {msg}")


def is_process_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


class WrapperRunner:
    def __init__(self, args, **popen_kwargs):
        if "--stop-grace" not in args:
            args = ["--stop-grace", "0.5", *args]
        cmd = [sys.executable, str(CLI_PATH), *args]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **popen_kwargs,
        )
        self.events: list[dict] = []
        self._event_cond = threading.Condition()
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []

        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stderr_thread.start()
        self._stdout_thread.start()

    def _drain_stderr(self):
        for line in iter(self.proc.stderr.readline, ""):
            line_str = line.strip()
            if line_str:
                self.stderr_lines.append(line_str)
                try:
                    data = json.loads(line_str)
                    if isinstance(data, dict) and "event" in data:
                        with self._event_cond:
                            self.events.append(data)
                            self._event_cond.notify_all()
                except Exception:
                    pass

    def _drain_stdout(self):
        for line in iter(self.proc.stdout.readline, ""):
            line_str = line.strip()
            if line_str:
                self.stdout_lines.append(line_str)

    def wait_for_event(self, event_name: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        with self._event_cond:
            while time.monotonic() < deadline:
                for ev in self.events:
                    if ev.get("event") == event_name:
                        return ev
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._event_cond.wait(timeout=min(remaining, 0.05))
        available = [e.get("event") for e in self.events]
        raise TimeoutError(f"Event {event_name!r} not seen within {timeout}s (saw: {available})")

    def wait(self, timeout: float = 10.0) -> int:
        code = self.proc.wait(timeout=timeout)
        self._stderr_thread.join(timeout=1.0)
        self._stdout_thread.join(timeout=1.0)
        return code

    def stop(self):
        self.proc.terminate()
        self.wait(timeout=10.0)


@contextmanager
def launch_wrapper(args, **popen_kwargs):
    runner = WrapperRunner(args, **popen_kwargs)
    try:
        yield runner
    finally:
        if runner.proc.poll() is None:
            runner.stop()
        runner.wait()
        runner.proc.stdout.close()
        runner.proc.stderr.close()


def test_preloaded_fixture_text_unloaded_before_command(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    marker_file = tmp_path / "child_marker.txt"

    with serving(lock_path=lock_file) as rt:
        rt.ensure_model("one")
        assert rt.model_id == "one"
        assert rt.process is not None
        child_pid = rt.process.pid

        child_code = """
import sys
from pathlib import Path
Path(sys.argv[1]).write_text('child_executed')
"""
        child_cmd = [sys.executable, "-c", child_code, str(marker_file)]

        with launch_wrapper([
            "--gateway", rt.endpoint,
            "--gpu-lock", str(lock_file),
            "--", *child_cmd
        ]) as wrapper:
            code = wrapper.wait(timeout=5.0)
            assert code == 0

        assert marker_file.read_text().strip() == "child_executed"
        assert rt.model_id is None
        assert rt.process is None
        assert is_process_dead(child_pid)

        event_names = [e["event"] for e in wrapper.events]
        assert "waiting" in event_names
        assert "acquired" in event_names
        assert "text_backend_released" in event_names
        assert "started" in event_names
        assert "processes_reaped" in event_names
        assert "lease_released" in event_names

        idx_released = event_names.index("text_backend_released")
        idx_started = event_names.index("started")
        assert idx_released < idx_started


def test_text_request_blocked_while_training_lease_then_works_after_release(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    started_file = tmp_path / "training_started.txt"
    finish_file = tmp_path / "training_finish.txt"

    child_code = """
import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text('ready')
finish = Path(sys.argv[2])
deadline = time.monotonic() + 10.0
while not finish.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    child_cmd = [sys.executable, "-c", child_code, str(started_file), str(finish_file)]

    with serving(lock_path=lock_file, route_timeout=5.0) as rt:
        with launch_wrapper([
            "--gateway", rt.endpoint,
            "--gpu-lock", str(lock_file),
            "--", *child_cmd
        ]) as wrapper:
            wait_until(started_file.exists, timeout=5.0, msg="training started")

            results = []
            t = Thread(target=lambda: results.append(
                post(rt.endpoint, "/v1/chat/completions", {
                    "model": "coding",
                    "messages": [{"role": "user", "content": "hello"}]
                })
            ))
            t.start()

            # Confirm text request is blocked while training holds the lease
            time.sleep(0.3)
            assert len(results) == 0

            # Signal training child to finish
            finish_file.write_text("done")

            code = wrapper.wait(timeout=5.0)
            assert code == 0

            t.join(timeout=3.0)
            assert len(results) == 1
            status_code, body = results[0]
            assert status_code == 200
            assert body["choices"][0]["message"]["content"] == "OK"
            assert rt.model_id == "one"


def test_second_wrapper_waits_and_times_out_never_concurrent(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    c1_started = tmp_path / "c1_started.txt"
    c1_finish = tmp_path / "c1_finish.txt"
    c2_started = tmp_path / "c2_started.txt"
    c3_started = tmp_path / "c3_started.txt"

    child1_code = """
import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text('ready1')
finish = Path(sys.argv[2])
deadline = time.monotonic() + 10.0
while not finish.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    child1_cmd = [sys.executable, "-c", child1_code, str(c1_started), str(c1_finish)]

    child_marker_code = """
import sys
from pathlib import Path
Path(sys.argv[1]).write_text('ran')
"""

    with serving(lock_path=lock_file) as rt:
        with launch_wrapper([
            "--gateway", rt.endpoint,
            "--gpu-lock", str(lock_file),
            "--", *child1_cmd
        ]) as wrapper1:
            wait_until(c1_started.exists, timeout=5.0, msg="wrapper1 started")

            # Wrapper 2 with short timeout (0.3s) times out and NEVER executes child
            with launch_wrapper([
                "--gateway", rt.endpoint,
                "--gpu-lock", str(lock_file),
                "--gpu-wait", "0.3",
                "--", sys.executable, "-c", child_marker_code, str(c2_started)
            ]) as wrapper2:
                w2_code = wrapper2.wait(timeout=3.0)
                assert w2_code == 124
                assert not c2_started.exists()
                assert wrapper2.wait_for_event("acquisition_timeout")

            # Wrapper 3 with sufficient wait (5.0s) waits without executing concurrently
            with launch_wrapper([
                "--gateway", rt.endpoint,
                "--gpu-lock", str(lock_file),
                "--gpu-wait", "5.0",
                "--", sys.executable, "-c", child_marker_code, str(c3_started)
            ]) as wrapper3:
                wrapper3.wait_for_event("waiting", timeout=3.0)
                assert not c3_started.exists()

                # Release wrapper 1
                c1_finish.write_text("done")
                assert wrapper1.wait(timeout=3.0) == 0

                # Wrapper 3 now acquires and executes
                wait_until(c3_started.exists, timeout=4.0, msg="wrapper3 executed after release")
                assert wrapper3.wait(timeout=3.0) == 0


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_sent_to_wrapper_kills_child_and_detached_grandchild_before_release(tmp_path, signum):
    lock_file = tmp_path / "gpu.lock"
    child_pid_file = tmp_path / "child_pid.txt"
    grandchild_pid_file = tmp_path / "grandchild_pid.txt"

    supervisor_script = """
import os, sys, time
from pathlib import Path

child_f = Path(sys.argv[1])
grandchild_f = Path(sys.argv[2])

pid = os.fork()
if pid == 0:
    os.setsid()
    grandchild_f.write_text(str(os.getpid()))
    while True:
        time.sleep(0.05)
else:
    child_f.write_text(str(os.getpid()))
    while True:
        time.sleep(0.05)
"""
    child_cmd = [sys.executable, "-c", supervisor_script, str(child_pid_file), str(grandchild_pid_file)]

    child_pid, grandchild_pid = None, None
    with serving(lock_path=lock_file) as rt:
        try:
            with launch_wrapper([
                "--gateway", rt.endpoint,
                "--gpu-lock", str(lock_file),
                "--stop-grace", "1.0",
                "--", *child_cmd
            ]) as wrapper:
                wait_until(lambda: child_pid_file.exists() and child_pid_file.stat().st_size, timeout=5.0,
                           msg="child pid written")
                wait_until(lambda: grandchild_pid_file.exists() and grandchild_pid_file.stat().st_size, timeout=5.0,
                           msg="grandchild pid written")

                child_pid = int(child_pid_file.read_text().strip())
                grandchild_pid = int(grandchild_pid_file.read_text().strip())

                assert not is_process_dead(child_pid)
                assert not is_process_dead(grandchild_pid)

                # Send signal to the wrapper process
                os.kill(wrapper.proc.pid, signum)

                code = wrapper.wait(timeout=5.0)
                assert code == 128 + signum

                # Both child and detached grandchild must be dead before lease was released
                assert is_process_dead(child_pid)
                assert is_process_dead(grandchild_pid)

                assert wrapper.wait_for_event("processes_reaped")
                assert wrapper.wait_for_event("lease_released")
        finally:
            for p in (child_pid, grandchild_pid):
                if p and not is_process_dead(p):
                    with contextlib.suppress(Exception):
                        os.kill(p, signal.SIGKILL)


def test_signal_while_waiting_does_not_start_child(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    marker_file = tmp_path / "child_started.txt"

    child_code = """
import sys
from pathlib import Path
Path(sys.argv[1]).write_text('bad_child_started')
"""
    child_cmd = [sys.executable, "-c", child_code, str(marker_file)]

    with serving(lock_path=lock_file) as rt:
        external_lease = SharedGpuLease(lock_file)
        with external_lease.hold("text", "external-holder"):
            with launch_wrapper([
                "--gateway", rt.endpoint,
                "--gpu-lock", str(lock_file),
                "--gpu-wait", "10.0",
                "--", *child_cmd
            ]) as wrapper:
                wrapper.wait_for_event("waiting", timeout=3.0)
                os.kill(wrapper.proc.pid, signal.SIGINT)
                code = wrapper.wait(timeout=3.0)
                assert code == 130  # 128 + SIGINT

        assert not marker_file.exists()
        assert wrapper.wait_for_event("cancelled_while_waiting")


def test_child_exit_7_propagated(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    child_cmd = [sys.executable, "-c", "import sys; sys.exit(7)"]

    with serving(lock_path=lock_file) as rt:
        with launch_wrapper([
            "--gateway", rt.endpoint,
            "--gpu-lock", str(lock_file),
            "--", *child_cmd
        ]) as wrapper:
            code = wrapper.wait(timeout=5.0)
            assert code == 7

        reaped = wrapper.wait_for_event("processes_reaped")
        assert reaped.get("child_exit_code") == 7
        released = wrapper.wait_for_event("lease_released")
        assert released.get("exit_code") == 7


def test_false_yield_or_unreachable_gateway_refuses_child(tmp_path):
    lock_file = tmp_path / "gpu.lock"

    # 1. Unreachable gateway refused
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    unused_port = s.getsockname()[1]
    s.close()

    marker1 = tmp_path / "marker_unreachable.txt"
    child_cmd1 = [sys.executable, "-c", "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('bad')", str(marker1)]

    with launch_wrapper([
        "--gateway", f"http://127.0.0.1:{unused_port}",
        "--gpu-lock", str(lock_file),
        "--", *child_cmd1
    ]) as wrapper1:
        code1 = wrapper1.wait(timeout=5.0)
        assert code1 == 125
        assert not marker1.exists()
        assert wrapper1.wait_for_event("failed")

    # 2. False yield from gateway refused
    class FalseYieldHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = json.dumps({"status": "busy", "backend_pid": 99999}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FalseYieldHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    mock_port = server.server_port

    marker2 = tmp_path / "marker_falseyield.txt"
    child_cmd2 = [sys.executable, "-c", "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('bad')", str(marker2)]

    try:
        with launch_wrapper([
            "--gateway", f"http://127.0.0.1:{mock_port}",
            "--gpu-lock", str(lock_file),
            "--", *child_cmd2
        ]) as wrapper2:
            code2 = wrapper2.wait(timeout=5.0)
            assert code2 == 125
            assert not marker2.exists()
            assert wrapper2.wait_for_event("failed")
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


def test_an_environment_flag_alone_is_not_lease_authority(monkeypatch):
    from tare_node import gpu_lease_run

    monkeypatch.delenv(gpu_lease_run.LEASE_CONTEXT, raising=False)
    assert gpu_lease_run.inherited_lease() is None
    monkeypatch.setenv(gpu_lease_run.LEASE_CONTEXT, '{}')
    with pytest.raises(ValueError, match='Inherited GPU lease is invalid'):
        gpu_lease_run.inherited_lease()


def test_cli_argument_rejections():
    invalid_invocations = [
        ["--gpu-wait", "0", "--", "echo", "1"],
        ["--gpu-wait", "nan", "--", "echo", "1"],
        ["--gpu-wait", "inf", "--", "echo", "1"],
        ["--gpu-wait", "-1", "--", "echo", "1"],
        ["--stop-grace", "0", "--", "echo", "1"],
        ["--stop-grace", "nan", "--", "echo", "1"],
        ["--stop-grace", "inf", "--", "echo", "1"],
        ["--stop-grace", "-1", "--", "echo", "1"],
        ["--gateway", "http://127.0.0.1:8080"],
        ["--gateway", "http://127.0.0.1:8080", "--"],
    ]
    for inv in invalid_invocations:
        res = subprocess.run(
            [sys.executable, str(CLI_PATH), *inv],
            capture_output=True,
            text=True, timeout=5,
        )
        assert res.returncode == 2, f"Expected 2 for {inv}, got {res.returncode}"


def test_escaped_grandchild_after_root_exit_gets_reaped(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    grandchild_pid_file = tmp_path / "grandchild_pid.txt"

    child_script = """
import os, sys, time
from pathlib import Path

grandchild_f = Path(sys.argv[1])
pid = os.fork()
if pid == 0:
    os.setsid()
    grandchild_f.write_text(str(os.getpid()))
    while True:
        time.sleep(0.05)
else:
    sys.exit(0)
"""
    child_cmd = [sys.executable, "-c", child_script, str(grandchild_pid_file)]

    grandchild_pid = None
    with serving(lock_path=lock_file) as rt:
        try:
            with launch_wrapper([
                "--gateway", rt.endpoint,
                "--gpu-lock", str(lock_file),
                "--stop-grace", "1.0",
                "--", *child_cmd
            ]) as wrapper:
                wait_until(lambda: grandchild_pid_file.exists() and grandchild_pid_file.stat().st_size, timeout=5.0,
                           msg="grandchild pid written")
                grandchild_pid = int(grandchild_pid_file.read_text().strip())
                code = wrapper.wait(timeout=5.0)
                assert code == 0

                assert is_process_dead(grandchild_pid)
                reaped = wrapper.wait_for_event("processes_reaped")
                assert reaped.get("child_exit_code") == 0
                assert wrapper.wait_for_event("lease_released")
        finally:
            if grandchild_pid and not is_process_dead(grandchild_pid):
                with contextlib.suppress(Exception):
                    os.kill(grandchild_pid, signal.SIGKILL)


def test_linger_ignored_term_escalates_to_sigkill(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    child_pid_file = tmp_path / "child_pid.txt"

    child_script = """
import os, sys, time, signal
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    time.sleep(0.05)
"""
    child_cmd = [sys.executable, "-c", child_script, str(child_pid_file)]

    child_pid = None
    with serving(lock_path=lock_file) as rt:
        try:
            with launch_wrapper([
                "--gateway", rt.endpoint,
                "--gpu-lock", str(lock_file),
                "--stop-grace", "0.3",
                "--", *child_cmd
            ]) as wrapper:
                wait_until(lambda: child_pid_file.exists() and child_pid_file.stat().st_size, timeout=5.0,
                           msg="child pid written")
                child_pid = int(child_pid_file.read_text().strip())
                assert not is_process_dead(child_pid)

                os.kill(wrapper.proc.pid, signal.SIGTERM)

                code = wrapper.wait(timeout=5.0)
                # Escalated to SIGKILL (child killed by SIGKILL, exit code 128 - (-9) = 137)
                assert code == 137

                assert is_process_dead(child_pid)
                cleanup_ev = wrapper.wait_for_event("cleanup_wait")
                assert "SIGKILL" in cleanup_ev.get("message", "")
                assert wrapper.wait_for_event("processes_reaped")
                assert wrapper.wait_for_event("lease_released")
        finally:
            if child_pid and not is_process_dead(child_pid):
                with contextlib.suppress(Exception):
                    os.kill(child_pid, signal.SIGKILL)


def test_executable_missing_returns_127(tmp_path):
    lock_file = tmp_path / "gpu.lock"

    with serving(lock_path=lock_file) as rt:
        with launch_wrapper([
            "--gateway", rt.endpoint,
            "--gpu-lock", str(lock_file),
            "--", "/nonexistent_command_xyz_12345"
        ]) as wrapper:
            code = wrapper.wait(timeout=5.0)
            assert code == 127
            assert wrapper.wait_for_event("failed")


def test_no_expiry_during_child_runtime(tmp_path):
    lock_file = tmp_path / "gpu.lock"
    done_file = tmp_path / "child_done.txt"

    child_script = """
import sys, time
from pathlib import Path
time.sleep(0.5)
Path(sys.argv[1]).write_text('finished')
"""
    child_cmd = [sys.executable, "-c", child_script, str(done_file)]

    with serving(lock_path=lock_file) as rt:
        with launch_wrapper([
            "--gateway", rt.endpoint,
            "--gpu-lock", str(lock_file),
            "--gpu-wait", "0.2",
            "--", *child_cmd
        ]) as wrapper:
            wrapper.wait_for_event("started")
            with pytest.raises(TimeoutError):
                with SharedGpuLease(lock_file, timeout=.3).hold("text", "still-training"):
                    pytest.fail("Training lost its lease after its acquisition deadline")
            code = wrapper.wait(timeout=5.0)
            assert code == 0
            assert done_file.read_text().strip() == "finished"


def test_elapsed_cleanup_grace_does_not_release_lease(tmp_path, monkeypatch):
    """A failed kill cannot admit text, even after the cleanup deadline."""
    lock_file, finish = tmp_path/'gpu.lock', tmp_path/'finish'
    driver = tmp_path/'failed_kill_driver.py'
    driver.write_text(
        'import importlib.util\n'
        f'spec=importlib.util.spec_from_file_location("runner", {str(CLI_PATH)!r})\n'
        'runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)\n'
        'runner.signal_tree=lambda *args: None\n'
        'raise SystemExit(runner.main())\n')
    monkeypatch.setitem(globals(), 'CLI_PATH', driver)
    child = ('import time,sys\nfrom pathlib import Path\n'
             'deadline=time.monotonic()+8\n'
             f'while not Path({str(finish)!r}).exists() and time.monotonic()<deadline: time.sleep(.02)\n'
             'sys.exit(7)\n')
    with serving(lock_path=lock_file) as rt:
        with launch_wrapper(['--gateway', rt.endpoint, '--gpu-lock', str(lock_file),
                '--stop-grace', '.1', '--', sys.executable, '-c', child]) as wrapper:
            try:
                wrapper.wait_for_event('started')
                wrapper.proc.send_signal(signal.SIGTERM)
                wrapper.wait_for_event('cleanup_wait')
                assert wrapper.proc.poll() is None
                assert not any(row['event'] == 'lease_released' for row in wrapper.events)
                with pytest.raises(TimeoutError):
                    with SharedGpuLease(lock_file, timeout=.2).hold('text', 'cleanup-probe'):
                        pytest.fail('Unconfirmed cleanup released the GPU')
            finally:
                finish.write_text('allow observed exit')
            assert wrapper.wait() == 7
            assert wrapper.wait_for_event('processes_reaped')['child_exit_code'] == 7
            assert wrapper.wait_for_event('lease_released')


def test_closed_diagnostics_do_not_skip_child_cleanup(tmp_path):
    lock_file, pidfile = tmp_path/'gpu.lock', tmp_path/'child-pid'
    driver = tmp_path/'closed_stderr.py'
    driver.write_text(
        'import importlib.util,sys\n'
        f'spec=importlib.util.spec_from_file_location("runner", {str(CLI_PATH)!r})\n'
        'runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)\n'
        'class Broken:\n'
        ' def write(self,*args): raise BrokenPipeError("reader disconnected")\n'
        ' def flush(self): pass\n'
        'sys.stderr=Broken()\n'
        'raise SystemExit(runner.main())\n')
    child = ('import os,signal,time\nfrom pathlib import Path\n'
             'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
             f'Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
             'time.sleep(10)\n')
    with serving(lock_path=lock_file) as rt:
        process = subprocess.Popen([sys.executable, str(driver), '--gateway', rt.endpoint,
            '--gpu-lock', str(lock_file), '--stop-grace', '.1', '--', sys.executable, '-c', child])
        try:
            wait_until(lambda: pidfile.exists() and pidfile.stat().st_size)
            pid = int(pidfile.read_text())
            assert SharedGpuLease(lock_file).status()['held']
            process.terminate()
            assert process.wait(timeout=5) == 137
            assert is_process_dead(pid)
            assert not SharedGpuLease(lock_file).status()['held']
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=12)

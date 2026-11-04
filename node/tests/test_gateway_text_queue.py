"""Text requests run one at a time, the smallest waiting first, and the queue shows who is calling."""
import importlib.util
from pathlib import Path
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gateway_text_queue", ROOT / "tare_node/qualified_model_gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def caller(name):
    return {"address": "127.0.0.1", "client": name, "model": "qwen38-gsq"}


def until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(.01)


def test_a_short_turn_goes_before_large_prompts_that_waited_longer():
    queue, order, release = gateway.TextQueue(), [], threading.Event()

    def run(name, size):
        with queue.turn(size, 10, caller(name)):
            order.append(name)
            if name == "batch-1":
                release.wait(5)

    first = threading.Thread(target=run, args=("batch-1", 480_000))
    first.start()
    until(lambda: queue.status()["running"] is not None)
    waiters = []
    for name, size in (("batch-2", 480_000), ("copilot", 6_000), ("tui", 40_000)):  # arrival order
        waiters.append(threading.Thread(target=run, args=(name, size)))
        waiters[-1].start()
        until(lambda n=len(waiters): len(queue.status()["waiting"]) == n)
    status = queue.status()
    assert status["running"]["client"] == "batch-1" and status["running"]["bytes"] == 480_000
    assert [w["client"] for w in status["waiting"]] == ["copilot", "tui", "batch-2"]
    release.set()
    for thread in (first, *waiters):
        thread.join(5)
    assert order == ["batch-1", "copilot", "tui", "batch-2"]  # the running request was never interrupted
    assert queue.status() == {"running": None, "waiting": []}


def test_a_request_that_waits_past_its_timeout_leaves_the_queue():
    queue, release = gateway.TextQueue(), threading.Event()

    def hold():
        with queue.turn(10, 10, caller("long")):
            release.wait(5)
    holder = threading.Thread(target=hold)
    holder.start()
    until(lambda: queue.status()["running"] is not None)
    with pytest.raises(TimeoutError):
        with queue.turn(5, .2, caller("impatient")):
            pass
    assert queue.status()["waiting"] == []
    release.set()
    holder.join(5)
    with queue.turn(5, 1, caller("next")) as waited:  # the queue still admits after a timeout
        assert waited < 1

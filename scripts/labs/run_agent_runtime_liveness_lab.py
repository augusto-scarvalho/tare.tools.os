#!/usr/bin/env python3
"""Cross-repository F1 liveness lab for the ADR-057 agent runtime boundary."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_contracts(agent_runtime_root: Path, kernel_root: Path) -> dict[str, Any]:
    runtime_src = agent_runtime_root / "src"
    for path in (runtime_src, kernel_root):
        if not path.exists():
            raise FileNotFoundError(f"required repository root is missing: {path}")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from compute_plane.aider_driver import AiderDriver
    from relay import agent_runtime_host, cluster_coordinator
    from tare_tools_agent_runtime import CancellationToken, OutcomeStatus, RunBudget

    return {
        "AiderDriver": AiderDriver,
        "CancellationToken": CancellationToken,
        "OutcomeStatus": OutcomeStatus,
        "RunBudget": RunBudget,
        "agent_runtime_host": agent_runtime_host,
        "cluster_coordinator": cluster_coordinator,
    }


def run_lab(agent_runtime_root: Path, kernel_root: Path) -> dict[str, Any]:
    modules = _load_contracts(agent_runtime_root, kernel_root)
    AiderDriver = modules["AiderDriver"]
    CancellationToken = modules["CancellationToken"]
    OutcomeStatus = modules["OutcomeStatus"]
    RunBudget = modules["RunBudget"]
    host = modules["agent_runtime_host"]
    coordinator = modules["cluster_coordinator"]

    class CommandDriver(AiderDriver):
        def __init__(self, source: str):
            super().__init__()
            self.source = source
            self.started = threading.Event()

        def build_command(self, **kwargs):
            return [sys.executable, "-c", self.source]

        def run_headless(self, *args, **kwargs):
            self.started.set()
            return super().run_headless(*args, **kwargs)

    def execute_thread(runtime, **kwargs):
        box: dict[str, Any] = {}

        def target():
            try:
                box["outcome"] = runtime.run(**kwargs)
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=target)
        thread.start()
        return thread, box

    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as temp:
        temp_root = Path(temp)

        success_db = coordinator.CoordinatorDB(temp_root / "success.db")
        success_driver = CommandDriver("print('baseline-ok')")
        success_runtime, success_gate = host.compose_aider_runtime(
            success_db, driver=success_driver, cwd=temp_root,
            workspace_is_ephemeral=True,
        )
        success_binding = success_gate.acquire("WORK-LAB-SUCCESS", "worker-a", ttl_s=30)
        success = success_runtime.run(
            binding=success_binding,
            instruction="baseline",
            budget=RunBudget(1, 0, 5),
        )
        assert success.status is OutcomeStatus.SUCCEEDED and success.claim_accepted
        results["baseline"] = "PASS"

        rebind_db = coordinator.CoordinatorDB(temp_root / "rebind.db")
        rebind_driver = CommandDriver("import time; time.sleep(30)")
        rebind_runtime, rebind_gate = host.compose_aider_runtime(
            rebind_db, driver=rebind_driver, cwd=temp_root,
            workspace_is_ephemeral=True,
        )
        old_binding = rebind_gate.acquire("WORK-LAB-REBIND", "worker-a", ttl_s=30)
        started = time.monotonic()
        thread, box = execute_thread(
            rebind_runtime,
            binding=old_binding,
            instruction="long turn",
            budget=RunBudget(1, 0, 10),
        )
        assert rebind_driver.started.wait(1)
        new_binding = rebind_gate.handoff(old_binding, "worker-b", ttl_s=30)
        thread.join(3)
        rebind_elapsed = time.monotonic() - started
        assert not thread.is_alive() and "error" not in box
        assert box["outcome"].status is OutcomeStatus.REJECTED
        assert new_binding.fence == old_binding.fence + 1
        assert rebind_elapsed < 2
        results["rebind_cancels_in_flight_seconds"] = round(rebind_elapsed, 4)

        cancel_db = coordinator.CoordinatorDB(temp_root / "cancel.db")
        cancel_driver = CommandDriver("import time; time.sleep(30)")
        cancel_runtime, cancel_gate = host.compose_aider_runtime(
            cancel_db, driver=cancel_driver, cwd=temp_root,
            workspace_is_ephemeral=True,
        )
        cancel_binding = cancel_gate.acquire("WORK-LAB-CANCEL", "worker-a", ttl_s=30)
        cancellation = CancellationToken()
        started = time.monotonic()
        thread, box = execute_thread(
            cancel_runtime,
            binding=cancel_binding,
            instruction="long turn",
            budget=RunBudget(1, 0, 10),
            cancellation=cancellation,
        )
        assert cancel_driver.started.wait(1)
        cancellation.cancel()
        thread.join(3)
        cancel_elapsed = time.monotonic() - started
        assert not thread.is_alive() and "error" not in box
        assert box["outcome"].status is OutcomeStatus.CANCELLED
        assert cancel_elapsed < 2
        results["explicit_cancel_seconds"] = round(cancel_elapsed, 4)

        timeout_db = coordinator.CoordinatorDB(temp_root / "timeout.db")
        timeout_driver = CommandDriver("import time; time.sleep(30)")
        timeout_runtime, timeout_gate = host.compose_aider_runtime(
            timeout_db, driver=timeout_driver, cwd=temp_root,
            workspace_is_ephemeral=True,
        )
        timeout_binding = timeout_gate.acquire("WORK-LAB-TIMEOUT", "worker-a", ttl_s=30)
        started = time.monotonic()
        timeout = timeout_runtime.run(
            binding=timeout_binding,
            instruction="long turn",
            budget=RunBudget(1, 0, 0.2),
        )
        timeout_elapsed = time.monotonic() - started
        assert timeout.status is OutcomeStatus.EXHAUSTED and timeout.claim_accepted
        assert timeout_elapsed < 2
        results["timeout_seconds"] = round(timeout_elapsed, 4)

        gc.collect()

    results["status"] = "PASS"
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent-runtime-root",
        type=Path,
        default=REPO_ROOT.parent / "tare.tools.agent-runtime",
    )
    parser.add_argument(
        "--kernel-root",
        type=Path,
        default=REPO_ROOT.parent / "tare.tools.kernel",
    )
    args = parser.parse_args()
    try:
        result = run_lab(args.agent_runtime_root.resolve(), args.kernel_root.resolve())
    except BaseException as exc:
        print(json.dumps({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

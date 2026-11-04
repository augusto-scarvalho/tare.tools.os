"""Fixture liveness and real-provider canary policy checks.

These checks are kept outside ``scripts/spec_test_gate.py`` so subprocess
liveness, transient fixture cleanup, and provider-canary policy do not bloat the
main validation CLI.  The module is dependency-free and returns result-shaped
records that ``spec_test_gate`` can print.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from harness_lib.processes import pid_alive, run_process_tree_bounded, signal_pid_group

TRANSIENT_MARKERS = (
    "# installer-created generic Gemini context",
    "<!-- harness protected-files transient fixture -->",
)
TRANSIENT_MARKET_FILES = (
    "GEMINI.md",
)


def _row(name: str, ok: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def cleanup_transient_market_fixture_files(root: Path) -> list[str]:
    """Remove interrupted protected-file fixture artifacts, not real docs.

    The protected-file fixture deliberately creates a market-recognized optional
    instruction file to prove snapshot-if-present drift is detected.  If the
    process is killed between write and ``finally`` cleanup, the file can poison
    the next validation run.  We remove only known fixture payloads carrying one
    of the marker strings, leaving adopter-owned instruction files untouched.
    """
    removed: list[str] = []
    for rel in TRANSIENT_MARKET_FILES:
        path = root / rel
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in TRANSIENT_MARKERS):
            path.unlink(missing_ok=True)
            removed.append(rel)
    return removed


def provider_canary_policy(root: Path) -> list[dict[str, str]]:
    project = read_json(root / ".harness" / "project.json", {}) or {}
    workflows = project.get("workflows", {}) if isinstance(project.get("workflows"), dict) else {}
    policy = workflows.get("providerCanaries", {}) if isinstance(workflows.get("providerCanaries"), dict) else {}
    out: list[dict[str, str]] = []
    opt_in_ok = policy.get("enabled") is False and policy.get("optInEnv") == "HARNESS_RUN_PROVIDER_CANARIES" and policy.get("networkDefault") == "disabled"
    out.append(_row("provider-canary:fail-closed", opt_in_ok, "real provider canaries are downstream opt-in" if opt_in_ok else str(policy)[:240]))
    modes = set(policy.get("modes", []) or [])
    required_modes = {"readonly-preflight", "write-sandbox", "auth-failure-classification", "rate-limit-classification"}
    out.append(_row("provider-canary:modes", required_modes.issubset(modes), ", ".join(sorted(modes)) or "missing"))
    executors = read_json(root / ".harness" / "routing" / "executors.json", {}) or {}
    rows = (executors.get("executors") or {}) if isinstance(executors, dict) else {}
    missing: list[str] = []
    for name in ["codex", "claude"]:
        runtime = rows.get(name, {}).get("runtimeLimits", {}) if isinstance(rows.get(name), dict) else {}
        for key in ["rateLimitPatterns", "authFailurePatterns", "quotaFailurePatterns"]:
            if not runtime.get(key):
                missing.append(f"{name}.{key}")
    out.append(_row("provider-canary:failure-classifiers", not missing, "auth/rate/quota classifiers present" if not missing else ", ".join(missing[:10])))
    return out


def process_timeout_cleanup(root: Path) -> list[dict[str, str]]:
    """Verify timed-out commands clean descendant processes.

    This fixture exercises the shared ``run_process_tree_bounded`` helper rather
    than open-coding a second cleanup path. The child script records the spawned
    descendant PID before sleeping, then the helper is forced to time out and
    tear down the process group.
    """
    if os.name == "nt":
        return [{"name": "process-timeout-cleanup:descendants", "status": "skip", "detail": "requires Unix /proc process inspection"}]
    with tempfile.TemporaryDirectory(prefix="harness-timeout-fixture-") as tmp:
        tmp_path = Path(tmp)
        pid_file = tmp_path / "child.pid"
        # Use a tiny shell fixture instead of a nested Python interpreter.  In
        # constrained CI sandboxes Python startup can be slower than the forced
        # timeout, making the descendant PID file racey even though process-tree
        # cleanup works. The shell writes the descendant PID before waiting.
        timed_out = False
        child_pid = 0
        try:
            try:
                run_process_tree_bounded(["bash", "-lc", "sleep 60 & echo $! > child.pid; wait"], timeout=3, cwd=tmp_path, env=os.environ.copy())
            except subprocess.TimeoutExpired:
                timed_out = True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if pid_file.exists():
                    try:
                        child_pid = int(pid_file.read_text(encoding="utf-8").strip() or "0")
                    except Exception:
                        child_pid = 0
                    break
                time.sleep(0.05)
            time.sleep(0.2)
            alive = bool(child_pid and pid_alive(child_pid))
            ok = timed_out and bool(child_pid) and not alive
            return [_row(
                "process-timeout-cleanup:descendants",
                ok,
                "timed-out process tree cleaned descendant process" if ok else f"timed_out={timed_out}; child_pid={child_pid}; alive={alive}",
            )]
        finally:
            if child_pid:
                signal_pid_group(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def fixture_liveness(root: Path) -> list[dict[str, str]]:
    """Validate fixture liveness controls without nesting detached subprocesses."""
    removed = cleanup_transient_market_fixture_files(root)
    source = (root / "scripts" / "spec_test_gate.py").read_text(encoding="utf-8", errors="ignore")
    fn_start = source.find("def check_protected_files_fixture")
    fn_end = source.find("def check_workflow_profile_planning_fixture", fn_start)
    protected_source = source[fn_start:fn_end] if fn_start >= 0 and fn_end > fn_start else ""
    per_market_subprocess = "for rel in market_paths:" in protected_source and "run_fixture_command" in protected_source.split("for rel in market_paths:", 1)[1].split("transient =", 1)[0]
    out = [
        _row(
            "fixture-liveness:protected-market-matrix-in-process",
            not per_market_subprocess,
            "market matrix avoids one subprocess per path" if not per_market_subprocess else "market path loop still shells out per path",
        ),
        _row(
            "fixture-liveness:transient-market-cleanup",
            True,
            "removed=" + (",".join(removed) if removed else "0"),
        ),
    ]
    out.extend(process_timeout_cleanup(root))
    return out

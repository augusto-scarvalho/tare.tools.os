"""`harness.py gate-staged`: launch `validate --staged` DETACHED (SPEC-137).

Failure mode 1 (SPEC-137): a foreground gate hard-killed by a timeout mid-run
strands an orphaned gate-hold. This verb detaches the gate AT THE SOURCE so it
can no longer be foreground-hard-killed: it fires `validate --staged` through the
mediated `processes.launch_detached` spawn (Windows: CREATE_NEW_PROCESS_GROUP +
hidden console; POSIX: start_new_session), tees stdout+stderr to a run log, and
returns IMMEDIATELY with the log path + PID. A detached WORKER wrapper
(`gate-staged --run`) runs the gate in-process and writes a completion marker
carrying the exit code, so the caller can poll for the terminal verdict without
parsing the log.

`--wait` (2026-07-30, owner demand): after launching, OBSERVE the marker until
the gate settles and exit with its exit code. Pure observer -- killing the
waiter never kills the detached gate, so the SPEC-137 protection is intact.
The sanctioned agent flow is `gate-staged --wait` under the shell tool's
run_in_background (deny_hitl_flags.py enforces it): the background-task
completion notification wakes the agent, instead of a foreground poll loop
holding the turn hostage for the gate's 5-6 min.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

# Standalone-run bootstrap (direct `python scripts/harness_lib/gate_staged.py`
# for the self-check): put scripts/ on the path so `harness_lib` resolves. No-op
# when imported as harness_lib.gate_staged (harness.py already wired the path).
if __package__ in (None, "") and str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib import processes
from harness_lib.common import ROOT, now


def _runs_dir(root: Path) -> Path:
    d = root / ".harness" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_worker(marker: str | None) -> int:
    """Detached WORKER: run `validate --staged` in-process (its own subprocess
    gates are already mediated + bounded), then write the completion marker with
    the exit code. stdout/stderr already point at the log (launch_detached wired
    them), so the gate output tees there with no extra console."""
    from harness_lib import validation_stamp

    ns = argparse.Namespace(staged=True, check=False, json=False)
    rc = 0
    try:
        validation_stamp.cmd_validate(ns)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException as exc:  # noqa: BLE001 -- record, don't crash the detached worker
        print(f"gate-staged worker error: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        rc = 1
    if marker:
        try:
            Path(marker).write_text(json.dumps(
                {"gate": "validate-staged", "exitCode": rc,
                 "status": "pass" if rc == 0 else "fail", "finishedAt": now()},
                indent=2), encoding="utf-8")
        except OSError:
            pass
    return rc


def _sweep_old(runs: Path, keep: int = 5) -> None:
    """Retention for gate-staged artifacts: keep the newest `keep` log/marker
    pairs, delete the rest. Post-D041 every batch runs a staged gate, so an
    unbounded pair-per-run pile is a real leak (the 2026-07-21 stray-marker
    release-hygiene fail). Fail-open: sweeping must never block a launch."""
    try:
        stems = sorted({p.name.rsplit(".", 1)[0] for p in runs.glob("gate-staged-*.*")})
        passing = []
        for stem in stems:
            try:
                marker = json.loads((runs / f"{stem}.marker").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (isinstance(marker, dict) and marker.get("status") == "pass"
                    and marker.get("exitCode") == 0):
                passing.append(stem)
        for stem in passing[:-keep] if len(passing) > keep else []:
            for p in runs.glob(stem + ".*"):
                p.unlink(missing_ok=True)
    except OSError:
        pass


def _filter_stale_staged(porcelain_lines: list[str]) -> list[str]:
    """Pure filter: porcelain XY rows where X staged something (MARC) and Y=M
    (worktree edited on top) — the staged copy is already superseded."""
    return [line[3:].strip() for line in porcelain_lines
            if len(line) > 3 and line[0] in "MARC" and line[1] == "M"]


def _staged_but_modified(root: Path) -> list[str]:
    """Paths whose INDEX copy diverged from the worktree — gating them validates
    a version the worktree already superseded (wasted-gate-run class, 2026-07-22)."""
    proc = processes.run_quiet(
        ["git", "-C", str(root), "status", "--porcelain"],
        timeout=30, capture_output=True, text=True)
    if proc.returncode != 0:
        return []  # git unavailable: never block the gate on the guard itself
    return _filter_stale_staged((proc.stdout or "").splitlines())


def _launch(root: Path) -> dict:
    ts = now().replace(":", "").replace("-", "")[:15]  # ISO -> compact, filename-safe
    runs = _runs_dir(root)
    _sweep_old(runs)
    log = runs / f"gate-staged-{ts}.log"
    marker = runs / f"gate-staged-{ts}.marker"
    worker = [sys.executable, str(root / "scripts" / "harness.py"),
              "gate-staged", "--run", "--marker", str(marker)]
    pid = processes.launch_detached(worker, log_path=log, cwd=str(root))
    return {"status": "launched", "pid": pid,
            "log": str(log.relative_to(root)).replace("\\", "/"),
            "marker": str(marker.relative_to(root)).replace("\\", "/")}


def _wait_marker(marker: Path, timeout_s: float = 2700, poll_s: float = 5.0) -> dict | None:
    """Observe (never own) the detached gate: poll for the completion marker,
    return its payload, None on timeout. A torn mid-write read re-polls instead
    of crashing. 45 min cap covers the 20+ min contention outliers."""
    deadline = time.monotonic() + timeout_s
    while True:
        if marker.exists():
            try:
                return json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass  # torn read: the worker is mid-write, re-poll
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


def cmd_gate_staged(args: argparse.Namespace) -> None:
    if getattr(args, "run", False):
        raise SystemExit(_run_worker(getattr(args, "marker", None)))
    # gate-while-dirty guard (loop-guards, 2026-07-22): a staged file with UNSTAGED
    # edits on top means the gate would validate a copy the worktree already
    # diverged from — the exact wasted-gate-run failure observed live. Abort with
    # the list; re-add (or stash) first. Detection only: nothing is auto-staged.
    dirty = _staged_but_modified(ROOT)
    if dirty:
        print("gate-staged: REFUSED — staged copy is stale vs worktree for: "
              + ", ".join(dirty)
              + ". Re-add (or stash) these before gating (gate-while-dirty guard).",
              file=sys.stderr)
        raise SystemExit(2)
    info = _launch(ROOT)
    print(json.dumps(info, ensure_ascii=False)
          if getattr(args, "json", False)
          else f"gate-staged: launched (pid {info['pid']}); "
               f"log {info['log']}, marker {info['marker']}", flush=True)
    if not getattr(args, "wait", False):
        return
    data = _wait_marker(ROOT / info["marker"])
    if data is None:
        print(f"gate-staged: wait cap hit, gate still running; log {info['log']}, "
              f"marker {info['marker']}", file=sys.stderr)
        raise SystemExit(3)
    rc = data.get("exitCode")
    rc = rc if isinstance(rc, int) else 1
    print(json.dumps({**data, "status": data.get("status", "settled")}, ensure_ascii=False)
          if getattr(args, "json", False)
          else f"gate-staged: settled {data.get('status')} (exitCode {rc}); log {info['log']}")
    raise SystemExit(rc)


def _self_check() -> int:
    import tempfile

    # _launch builds the detached WORKER command and returns log+marker+pid,
    # WITHOUT running the real gate (launch_detached stubbed).
    calls: list[list[str]] = []
    orig = processes.launch_detached
    processes.launch_detached = lambda cmd, **kw: (calls.append(list(cmd)), 4242)[1]  # type: ignore
    try:
        with tempfile.TemporaryDirectory() as tmp:
            info = _launch(Path(tmp))
    finally:
        processes.launch_detached = orig  # type: ignore
    assert info["pid"] == 4242, info
    assert info["log"].startswith(".harness/runs/gate-staged-") and info["log"].endswith(".log")
    assert info["marker"].endswith(".marker")

    # gate-while-dirty guard: MM/AM rows flag, clean/staged-only/untracked rows pass.
    rows = ["MM ui/src/App.tsx", "M  scripts/x.py", " M docs/y.md", "AM ui/a.ts", "?? new.txt"]
    assert _filter_stale_staged(rows) == ["ui/src/App.tsx", "ui/a.ts"], _filter_stale_staged(rows)
    assert calls and calls[0][1].endswith("harness.py")
    assert calls[0][2:5] == ["gate-staged", "--run", "--marker"], calls[0]

    # _sweep_old keeps the newest N log/marker pairs and never a partial pair.
    with tempfile.TemporaryDirectory() as tmp:
        runs = Path(tmp)
        for i in range(8):
            (runs / f"gate-staged-2026010{i}T000000.log").write_text("x", encoding="utf-8")
            (runs / f"gate-staged-2026010{i}T000000.marker").write_text(
                json.dumps({"status": "pass", "exitCode": 0}), encoding="utf-8")
        _sweep_old(runs, keep=5)
        left = sorted(p.name for p in runs.glob("gate-staged-*.*"))
        assert len(left) == 10, left                       # 5 pairs survive
        assert left[0].startswith("gate-staged-20260103"), left  # oldest 3 pairs gone

    # --wait observer: a settled marker returns its payload; an absent one times
    # out to None; a torn/garbled marker re-polls (never crashes) until the cap.
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp) / "m.marker"
        m.write_text(json.dumps({"exitCode": 1, "status": "fail"}), encoding="utf-8")
        assert _wait_marker(m, timeout_s=1, poll_s=0.01) == {"exitCode": 1, "status": "fail"}
        assert _wait_marker(Path(tmp) / "absent.marker", timeout_s=0.05, poll_s=0.01) is None
        torn = Path(tmp) / "torn.marker"
        torn.write_text('{"exitCode":', encoding="utf-8")
        assert _wait_marker(torn, timeout_s=0.05, poll_s=0.01) is None
        # ...and it really WAITS: a marker that lands mid-wait is returned, not
        # timed out (kills the flipped-deadline mutant that returns None early).
        import threading
        late = Path(tmp) / "late.marker"
        threading.Timer(0.15, lambda: late.write_text('{"exitCode": 0}', encoding="utf-8")).start()
        assert _wait_marker(late, timeout_s=5, poll_s=0.02) == {"exitCode": 0}

    # cmd_gate_staged --wait wiring: wait=False returns right after the launch;
    # wait=True exits with the settled marker's exit code (collaborators stubbed).
    g = globals()
    orig_fns = (g["_staged_but_modified"], g["_launch"], g["_wait_marker"])
    g["_staged_but_modified"] = lambda root: []
    g["_launch"] = lambda root: {"status": "launched", "pid": 1, "log": "l", "marker": "m"}
    g["_wait_marker"] = lambda marker, **kw: {"exitCode": 1, "status": "fail"}
    try:
        assert cmd_gate_staged(argparse.Namespace(run=False, json=False, wait=False)) is None
        try:
            cmd_gate_staged(argparse.Namespace(run=False, json=False, wait=True))
            raise AssertionError("wait=True must exit with the gate's exit code")
        except SystemExit as exc:
            assert exc.code == 1, exc.code
    finally:
        g["_staged_but_modified"], g["_launch"], g["_wait_marker"] = orig_fns

    # _run_worker writes a marker with the exit code (validate stubbed to a clean pass).
    from harness_lib import validation_stamp
    orig_v = validation_stamp.cmd_validate
    validation_stamp.cmd_validate = lambda ns: None  # type: ignore
    try:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "m.marker"
            rc = _run_worker(str(marker))
            data = json.loads(marker.read_text(encoding="utf-8"))
        assert rc == 0 and data["exitCode"] == 0 and data["status"] == "pass", data
        # a gate that blocks -> SystemExit(1) -> marker records fail
        validation_stamp.cmd_validate = lambda ns: (_ for _ in ()).throw(SystemExit(1))  # type: ignore
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "m.marker"
            rc = _run_worker(str(marker))
            data = json.loads(marker.read_text(encoding="utf-8"))
        assert rc == 1 and data["exitCode"] == 1 and data["status"] == "fail", data
    finally:
        validation_stamp.cmd_validate = orig_v  # type: ignore
    print("gate_staged self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())

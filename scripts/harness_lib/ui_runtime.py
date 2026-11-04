"""Read-only /api/runtime — the live resource/lock surface (OP-δ pre-req).

The REAL "resources & locks" of this harness (verify-before-build 2026-07-20:
`services` declares none in harness scope; the operative resources are):

  circuits      — .harness/runtime/executor-circuit-*.json (breaker state per
                  executor: healthy/open, failure counts, cooldowns, reasons)
  gateHolds     — .harness/runs/gate-hold/*/hold.json (the SPEC-137 baseline
                  holds, incl. *-recovered orphans — incident material)
  workflowLocks — .harness/workflows/active/WF-*/.run.lock ({pid, startedAt,
                  command, cwd})

Facts, not verdicts: locks/holds carry pidAlive + ageSeconds computed here; the
UI decides what reads as stale (measure-honesty — no fabricated staleness rule
duplicating workflow_config's bound logic). Every string value is redacted via
secret_scan.redact_text before egress (commands/reasons can carry tokens).
Per-collector never-crash; a missing dir is an honest empty list. Pure GET.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct self-check execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib.secret_scan import redact_text
from harness_lib.processes import pid_alive


def _redact(v: Any) -> Any:
    return redact_text(v) if isinstance(v, str) else v


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(str(iso))
        now = dt.datetime.now(t.tzinfo or dt.timezone.utc)
        return round((now - t).total_seconds(), 1)
    except (ValueError, TypeError):  # unparseable OR naive-vs-aware subtraction
        return None


def _circuits(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = root / ".harness" / "runtime"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("executor-circuit-*.json")):
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        out.append({k: _redact(v) for k, v in row.items()})
    return out


def _gate_holds(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = root / ".harness" / "runs" / "gate-hold"
    if not d.is_dir():
        return out
    for mf in sorted(d.glob("*/hold.json")):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict):
            continue
        pid = m.get("pid")
        entries = [str(e[0]) if isinstance(e, (list, tuple)) and e else str(e)
                   for e in (m.get("entries") or [])]
        out.append({
            "name": mf.parent.name,
            "recovered": mf.parent.name.endswith("-recovered"),
            "created": m.get("created"),
            "pid": pid,
            "pidAlive": pid_alive(int(pid)) if isinstance(pid, int) else None,
            "entries": [redact_text(e) for e in entries],
        })
    return out


def _workflow_locks(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = root / ".harness" / "workflows" / "active"
    if not d.is_dir():
        return out
    for lf in sorted(d.glob("WF-*/.run.lock")):
        try:
            m = json.loads(lf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            m = {}
        pid = m.get("pid") if isinstance(m, dict) else None
        started = m.get("startedAt") if isinstance(m, dict) else None
        out.append({
            "workflowId": lf.parent.name,
            "pid": pid,
            "pidAlive": pid_alive(int(pid)) if isinstance(pid, int) else None,
            "startedAt": started,
            "ageSeconds": _age_seconds(started),
            "command": _redact(m.get("command")) if isinstance(m, dict) else None,
        })
    return out


def runtime_snapshot(root: Path | str) -> dict[str, Any]:
    root = Path(root)
    circuits = _circuits(root)
    holds = _gate_holds(root)
    locks = _workflow_locks(root)
    return {
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "circuits": circuits,
        "gateHolds": holds,
        "workflowLocks": locks,
        "counts": {"circuits": len(circuits), "gateHolds": len(holds),
                   "workflowLocks": len(locks),
                   "openCircuits": sum(1 for c in circuits if str(c.get("state")) == "open")},
    }


def _self_check() -> int:
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rt = root / ".harness" / "runtime"
        gh = root / ".harness" / "runs" / "gate-hold" / "20990101T000000Z-1"
        wf = root / ".harness" / "workflows" / "active" / "WF-X"
        for d in (rt, gh, wf):
            d.mkdir(parents=True)
        (rt / "executor-circuit-t.json").write_text(json.dumps(
            {"executor": "t", "state": "open",
             "lastFailureReason": "auth sk-FAKEKEYdonotleak0123456789abcdef bad"}),  # mojibake-ok: planted fixture
            encoding="utf-8")
        (rt / "executor-state.json").write_text("{}", encoding="utf-8")  # non-circuit: ignored
        (gh / "hold.json").write_text(json.dumps(
            {"created": "20990101T000000Z", "pid": os.getpid(),
             "entries": [["C:/x/.harness/state", "e0"]]}), encoding="utf-8")
        (wf / ".run.lock").write_text(json.dumps(
            {"pid": 2_000_000_000, "startedAt": "2026-07-20T00:00:00+00:00",
             "command": "workflow run"}), encoding="utf-8")

        s = runtime_snapshot(root)
        assert s["counts"] == {"circuits": 1, "gateHolds": 1, "workflowLocks": 1,
                               "openCircuits": 1}, s["counts"]
        assert "sk-FAKEKEYdonotleak0123456789abcdef" not in json.dumps(s), "redaction must fire"
        assert s["gateHolds"][0]["pidAlive"] is True and s["gateHolds"][0]["recovered"] is False
        lock = s["workflowLocks"][0]
        assert lock["pidAlive"] is False and lock["ageSeconds"] and lock["ageSeconds"] > 0, lock
        assert runtime_snapshot(root / "nowhere")["counts"]["circuits"] == 0  # honest empties
    print("ui_runtime self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())

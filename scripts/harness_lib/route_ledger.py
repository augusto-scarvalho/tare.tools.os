#!/usr/bin/env python3
"""SPEC-144 durable route ledger -- the single-writer twin of the transient route events.

EXP-17 verdict was "insufficient-data" for a STRUCTURAL reason: route events are
transient (the SPEC-137 gate truncates events.jsonl) and triage/dispatch/outcome
rows share NO correlation id. This module is the N2/escalations survival pattern
applied to routing: a durable, append-only ledger under .harness/state keyed by ONE
idempotent id (demand_id), so triage <-> dispatched <-> done rows join per-demand and
survive the gate wipe. Events remain the transient VIEW; the ledger is the durable
measurement SUBSTRATE.

Class B (internal journal): born-at-runtime, absence-tolerant, mirrored whole to the
private state home via tools/state_home_sync.py (SYNC_PATHS includes .harness/state).

Pure stdlib, EN-only, ASCII-safe. record() NEVER raises -- routing must not break on a
ledger failure. Self-check: python scripts/harness_lib/route_ledger.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_REL = ".harness/state/route-ledger.jsonl"
PHASES = ("triage", "dispatched", "withheld", "done")

# HARNESS_RESULT tail parsing for backfill (cloned from the exp17 probe /
# result_contracts.extract_harness_result -- that module needs a bind(env)-injected
# `re`, so it is not importable standalone).
TAIL_BYTES = 32768
RESULT_STATUSES = ("done", "partial", "blocked", "failed")


def demand_id(demand: str) -> str:
    """route- + sha256(demand)[:8] -- EXACTLY the existing withheld idempotent id
    form (harness.py cmd_route esc_id, security_routing_escalation). ONE id, everywhere."""
    return "route-" + hashlib.sha256((demand or "").encode("utf-8")).hexdigest()[:8]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_predicted_p(scores: Any, chosen_profile: Any) -> float | None:
    """R4 pre-registered predictedP: score(chosen_profile) / sum(scores.values())
    when the sum is > 0, else None -- the predicted probability the ECE probe consumes.

    NEVER divides by zero and NEVER raises: an empty/all-zero score dict, a missing
    chosen profile, or any non-numeric value yields None. Deterministic and pure, so it
    is testable in isolation and reusable at every record point (and the future ECE probe)."""
    if not isinstance(scores, dict) or not scores:
        return None
    try:
        total = sum(int(v) for v in scores.values())
    except (TypeError, ValueError):
        return None
    if total <= 0:  # all-zero (or degenerate) scores -> no prediction, never a zero-divide
        return None
    chosen = scores.get(chosen_profile)
    if chosen is None:
        return None
    try:
        return int(chosen) / total
    except (TypeError, ValueError):
        return None


def record(root: Any, phase: str, payload: dict) -> None:
    """Append one ledger row {phase, at, **payload} to .harness/state/route-ledger.jsonl.

    NEVER raises: routing must not break on a ledger failure -- any error is swallowed
    with a stderr one-liner. `payload` carries `demandId` for triage/dispatched/withheld
    (the join key) and `entryId` for the loop's done emit.

    HARNESS_ROUTE_LEDGER_DISABLE=1 is the scenario kill switch: the ledger is a
    MEASUREMENT corpus (EXP-17) and synthetic test demands must never pollute it.
    Env-based so it crosses subprocess CLI drives (rt_route_dispatcher run())."""
    if os.environ.get("HARNESS_ROUTE_LEDGER_DISABLE") == "1":
        return
    # gate-hold backstop (defect-sink-lost-under-gate-hold): route-ledger.jsonl is
    # under the held .harness/state, so a write during a live hold lands on
    # materialized HEAD and is discarded on release. The `route` CLI verb refuses
    # at the front door; this covers the non-CLI callers (route_loop done-emit,
    # backfill). Fail-OPEN (a bad root reads as "not holding"), skip visibly, never
    # raise — routing must not break on the ledger.
    try:
        try:
            from .common import gate_holding
        except ImportError:  # run directly for the __main__ self-check
            from common import gate_holding  # type: ignore
        held = gate_holding(Path(root))
    except Exception:
        held = False
    if held:
        sys.stderr.write(f"gate-hold: route-ledger {phase} skipped (a gate holds "
                         ".harness; the row would be discarded on release)\n")
        return
    try:
        path = Path(root) / LEDGER_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"phase": phase, "at": _iso_now(), **(payload or {})}
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception as exc:  # ledger is telemetry, never load-bearing for routing
        sys.stderr.write(f"route_ledger.record failed ({phase}): {exc}\n")


def read_ledger(root: Any) -> list[dict]:
    """Parse the ledger; blanks/bad lines skipped, missing/bad file -> []."""
    path = Path(root) / LEDGER_REL
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except Exception:
        return []
    return out


def _extract_harness_result(text: str):
    import re
    m = re.search(r"##\s*HARNESS_RESULT\s*```json\s*(\{.*?\})\s*```", text, flags=re.S)
    if not m:
        m = re.search(r"```json\s*(\{[^`]*\"status\"[^`]*\})\s*```", text, flags=re.S)
    if not m:
        return None
    return json.loads(m.group(1))


def _log_status(path: Path) -> str:
    """Status from the log tail's HARNESS_RESULT, or 'absent' (dead/still-running).
    Never raises -- a malformed/absent block is a legitimate 'absent' observation."""
    try:
        tail = path.read_bytes()[-TAIL_BYTES:].decode("utf-8", errors="replace")
        result = _extract_harness_result(tail)
    except Exception:
        return "absent"
    if not isinstance(result, dict):
        return "absent"
    status = result.get("status")
    return status if status in RESULT_STATUSES else "absent"


def backfill_from_logs(root: Any) -> dict:
    """Idempotent one-time fold: surviving .harness/runs/route-dispatch-*.log
    HARNESS_RESULT tails -> `done` rows keyed by log filename in the payload. Skips
    logs already folded (a re-run adds nothing). Returns {logs, folded, skipped}.

    Backfilled rows carry no `demandId` (the demand text is not recoverable from the
    lossy log slug), so they are outcome-only survivors, not join-keyed rows."""
    root = Path(root)
    runs = root / ".harness" / "runs"
    logs = sorted(runs.glob("route-dispatch-*.log")) if runs.exists() else []
    seen = {r.get("log") for r in read_ledger(root)
            if r.get("phase") == "done" and r.get("backfill")}
    folded = 0
    for lp in logs:
        if lp.name in seen:
            continue
        record(root, "done", {"log": lp.name, "status": _log_status(lp), "backfill": True})
        folded += 1
    return {"logs": len(logs), "folded": folded, "skipped": len(logs) - folded}


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # demand_id is the exact withheld idempotent form.
        did = demand_id("add a docs note")
        assert did == "route-" + hashlib.sha256(b"add a docs note").hexdigest()[:8], did

        # predictedP (R4): score(chosen)/sum when sum>0, else None; never a zero-divide.
        assert normalized_predicted_p({"docs": 3, "plan": 1}, "docs") == 0.75
        assert normalized_predicted_p({"a": 0, "b": 0}, "a") is None  # sum=0 -> None
        assert normalized_predicted_p({}, "docs") is None  # empty scores -> None
        assert normalized_predicted_p({"docs": 2}, "plan") is None  # chosen absent -> None
        assert normalized_predicted_p(None, "docs") is None  # non-dict -> None
        p = normalized_predicted_p({"docs": 1, "plan": 1}, "docs")
        assert isinstance(p, float) and 0.0 <= p <= 1.0, p  # always in [0,1]

        # round-trip: three phases -> read back -> keyed join by demandId works.
        record(root, "triage", {"demandId": did, "route": "inline", "riskTier": "R0"})
        record(root, "dispatched", {"demandId": did, "pid": 111, "executor": "claude"})
        record(root, "done", {"demandId": did, "route": "inline", "outcome": "committed"})
        rows = read_ledger(root)
        assert len(rows) == 3, rows
        assert [r["phase"] for r in rows] == ["triage", "dispatched", "done"], rows
        assert all(r.get("demandId") == did for r in rows), rows
        assert all("at" in r for r in rows), rows
        joined = [r for r in rows if r.get("demandId") == did]
        assert {r["phase"] for r in joined} == {"triage", "dispatched", "done"}, joined

        # idempotent backfill: one fake dispatch log folded twice -> one row.
        runs = root / ".harness" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"status": "done", "summary": "seeded"}, indent=2)
        (runs / "route-dispatch-Z.log").write_text(
            f"prose\ntail\n\n## HARNESS_RESULT\n```json\n{body}\n```\n", encoding="utf-8")
        c1 = backfill_from_logs(root)
        c2 = backfill_from_logs(root)
        assert c1 == {"logs": 1, "folded": 1, "skipped": 0}, c1
        assert c2 == {"logs": 1, "folded": 0, "skipped": 1}, c2
        done_z = [r for r in read_ledger(root)
                  if r.get("log") == "route-dispatch-Z.log"]
        assert len(done_z) == 1 and done_z[0]["status"] == "done", done_z

        # missing corpus -> empty counts, no rows, no raise.
        empty_root = Path(td) / "no" / "such" / "root"
        assert read_ledger(empty_root) == [], "missing ledger -> []"
        assert backfill_from_logs(empty_root) == {"logs": 0, "folded": 0, "skipped": 0}

    # gate-hold backstop: a live hold makes record() skip (the row would be lost on
    # release); once the hold clears the write lands. Fresh root so it stays isolated.
    with tempfile.TemporaryDirectory() as td2:
        held = Path(td2)
        hold = held / ".harness" / "runs" / "gate-hold" / "h1"
        hold.mkdir(parents=True)
        (hold / "hold.json").write_text("{}", encoding="utf-8")
        record(held, "triage", {"demandId": "route-held01"})
        assert read_ledger(held) == [], "held route write must be skipped"
        import shutil
        shutil.rmtree(held / ".harness" / "runs" / "gate-hold")
        record(held, "triage", {"demandId": "route-held01"})
        assert len(read_ledger(held)) == 1, "post-hold route write lands"

    # never-raises: an unwritable/invalid root is swallowed (no exception escapes).
    record("\x00::invalid::path", "done", {"demandId": "route-deadbeef"})
    print("route_ledger self-check OK")


if __name__ == "__main__":
    _self_check()

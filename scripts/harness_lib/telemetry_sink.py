#!/usr/bin/env python3
"""Shared non-fatal JSONL appender for SPEC-173 Phase 0 meters (rule 4).

The append idiom of `tools/hooks/graphify_search_guard.py:log_telemetry` MINUS
its 2MB rotation: rule 4 says append-only, never a ring — a Phase 0 series that
silently drops its own tail cannot answer "what did the compiler change?".
Every producer of a Phase 0 sink writes through here, so `ts` has one format
across sinks, and a telemetry failure is ALWAYS swallowed: the injection sink's
producer is the SessionStart hook, where a raised exception bricks hydration.
Self-check: python scripts/harness_lib/telemetry_sink.py.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _held_state_root(path: Path) -> Path | None:
    """The repo root when `path` is a `.harness/state` sink AND a gate hold is live;
    else None. A write to a held state sink lands on the materialized-HEAD tree and is
    discarded on release (defect-sink-lost-under-gate-hold). Never raises — this guard
    must not break the swallow-everything contract."""
    try:
        # In-gate contract, the SAME exemption scenario_isolation.hold_write_guard
        # already carries: inside a gate scenario the per-scenario snapshot/restore of
        # `.harness/state` IS the isolation, so the row the scenario is about to assert
        # on must LAND even though the gate's own hold is up. Without this the hold
        # swallowed every injection-telemetry row for the whole duration of every gate
        # run (measured 2026-08-03: pbc-2/pe-6 red on the serial path).
        if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
            return None
        parts = path.resolve().parts
        for i in range(1, len(parts) - 2):
            if parts[i] == ".harness" and parts[i + 1] == "state":
                root = Path(*parts[:i])
                try:
                    from .common import gate_holding
                except ImportError:  # standalone self-check
                    from common import gate_holding  # type: ignore
                return root if gate_holding(root) else None
        return None
    except Exception:
        return None


def sink_held(path: Path) -> bool:
    """True when `path` is a `.harness/state` sink AND a gate hold is live — the one
    predicate the append_jsonl backstop and the direct-writer hooks (search/design
    telemetry) share, so "which sink is held" has a single definition. Never raises."""
    return _held_state_root(path) is not None


def append_jsonl(path: Path, row: dict) -> bool:
    """Append one JSONL row; never raise, never rotate (SPEC-173 rule 4).

    Returns whether the row landed. Automatic producers (hooks, review, mutate)
    keep ignoring it — swallowing is the contract that stops a telemetry failure
    from bricking hydration. A producer the OPERATOR invoked on purpose has the
    opposite duty: it must not print "recorded" over a silent drop, so it reads
    this and exits nonzero (seat-telemetry amendment 2026-07-31).

    gate-hold backstop (defect-sink-lost-under-gate-hold): a `.harness/state` sink
    written during a LIVE hold is discarded on release, so this REFUSES visibly (False
    + one stderr line) instead of losing it silently. The CLI verb guard
    (scenario_isolation.hold_write_guard) is the primary seam; this is the fail-safe
    for the non-CLI append_jsonl callers (injection telemetry, etc.).
    """
    if sink_held(path):
        print(f"gate-hold: telemetry write to {path.name} refused — a gate holds "
              ".harness (the write would be discarded on release; retry after the gate)",
              file=sys.stderr)
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sink = Path(tmp) / "nested" / "series.jsonl"
        append_jsonl(sink, {"kind": "review-warn", "check": "footprint"})
        append_jsonl(sink, {"kind": "mutate-survivor", "file": "mod.py"})
        rows = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2, rows
        assert all(r["ts"] for r in rows) and rows[0]["kind"] == "review-warn", rows
        # the WRITTEN branch reports True — an operator-invoked producer needs to be
        # able to tell "recorded" from "silently dropped"
        assert append_jsonl(sink, {"kind": "note"}) is True
        # rule 4: an unwritable path (a DIRECTORY where the sink belongs) is silent…
        blocked = Path(tmp) / "blocked.jsonl"
        blocked.mkdir()
        assert append_jsonl(blocked, {"kind": "review-warn"}) is False
        # …and so is a row json cannot serialize — both report False, neither raises
        assert append_jsonl(sink, {"kind": object()}) is False
        assert len(sink.read_text(encoding="utf-8").splitlines()) == 3

    # gate-hold backstop: a .harness/state sink is REFUSED (no write) while a hold is
    # live, and writes normally once the hold clears — a non-state temp sink is never
    # affected (asserted above, its path has no .harness/state segment).
    with tempfile.TemporaryDirectory() as troot:
        root = Path(troot)
        state_sink = root / ".harness" / "state" / "defect-telemetry.jsonl"
        hold = root / ".harness" / "runs" / "gate-hold" / "h1"
        hold.mkdir(parents=True)  # non-empty gate-hold dir => holding
        (hold / "hold.json").write_text("{}", encoding="utf-8")
        # The in-gate env is POPPED for the refusal arm: this self-check is itself run
        # inside a gate scenario (pbc_meters pbc-3) with HARNESS_SCENARIO_ISOLATED=1,
        # which is exactly the value the exemption keys on.
        saved = os.environ.pop("HARNESS_SCENARIO_ISOLATED", None)
        try:
            assert append_jsonl(state_sink, {"kind": "defect"}) is False, "held state sink must refuse"
            assert not state_sink.exists(), "refused write must not create the sink"
            # ...and the in-gate arm: the same held sink ACCEPTS the write, because the
            # scenario's own snapshot/restore is the isolation contract there.
            os.environ["HARNESS_SCENARIO_ISOLATED"] = "1"
            assert append_jsonl(state_sink, {"kind": "defect"}) is True, "in-gate write must land under a hold"
            assert state_sink.exists(), "in-gate write must create the sink"
        finally:
            if saved is None:
                os.environ.pop("HARNESS_SCENARIO_ISOLATED", None)
            else:
                os.environ["HARNESS_SCENARIO_ISOLATED"] = saved
        import shutil
        shutil.rmtree(root / ".harness" / "runs" / "gate-hold")  # hold released
        assert append_jsonl(state_sink, {"kind": "defect"}) is True, "post-hold write lands"
        assert state_sink.exists()
    print("telemetry_sink self-check: ok")


if __name__ == "__main__":
    _demo()

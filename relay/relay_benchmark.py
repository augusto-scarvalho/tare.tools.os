#!/usr/bin/env python3
"""TRAIN-06 Seam D.4 — deterministic local relay benchmark (no authority).

Builds throwaway fixtures (idle / single transition / replay / malformed /
N-landed histories) against a temp relay root and reports machine-readable
before/after metrics. Observational only: it never touches the live relay.

Usage: python relay/relay_benchmark.py [--landed 10000] [--out report.json]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RELAY_ROOT))
sys.path.insert(0, str(RELAY_ROOT / "relay"))
from relay import relay_mesh  # noqa: E402
import relay_io  # noqa: E402


def _fixture(root: Path, landed: int, active: int = 3) -> None:
    relay_mesh.rebind_root(root)
    relay_mesh.ensure_dirs()
    relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps({
        "nodes": [{"id": f"T-{i}", "completion": {"status": "NOT_DONE"}} for i in range(active)],
        "edges": []}), encoding="utf-8")
    for i in range(landed):
        d = relay_mesh.TRAINS_DIR / f"TRAIN-OLD-{i}"
        d.mkdir(parents=True, exist_ok=True)
        relay_io.write_json_atomic(d / "STATE.json", {
            "train_id": f"TRAIN-OLD-{i}", "task_ids": [f"OLD-{i}"], "status": "LANDED",
            "tombstone": True, "revision": 7, "landed_at": "2026-01-01T00:00:00+00:00"})
    for i in range(active):
        relay_mesh.init_train(f"TRAIN-ACT-{i}", f"Active {i}", [f"T-{i}"], "bench")


def run(landed: int = 10000) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="relay-bench-"))
    try:
        t0 = time.perf_counter()
        _fixture(tmp, landed)
        build_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        relay_mesh.update_board()
        first_render_s = time.perf_counter() - t0
        board_bytes = relay_mesh.BOARD_JSON.stat().st_size
        board = relay_io.read_json(relay_mesh.BOARD_JSON)

        mtime_before = relay_mesh.BOARD_JSON.stat().st_mtime_ns
        t0 = time.perf_counter()
        idle_writes = sum(bool(relay_mesh.update_board()) for _ in range(30))
        idle_s = time.perf_counter() - t0
        noop_preserved = relay_mesh.BOARD_JSON.stat().st_mtime_ns == mtime_before

        # single transition: exactly one board refresh + one role message
        inbox = relay_mesh.INBOX_DIR / "implementer"
        msgs_before = len(list(inbox.glob("*.json")))
        (relay_mesh.TRAINS_DIR / "TRAIN-ACT-0" / "PLAN_AUDIT_REPORT.md").write_text(
            "T-0\nPLAN_VERDICT: APPROVED", encoding="utf-8")
        relay_mesh.transition_train("TRAIN-ACT-0", "PLAN_APPROVED", "implementer",
                                    "bench", "plan-auditor", "bench")
        msgs_delta = len(list(inbox.glob("*.json"))) - msgs_before

        # replay dedup: same action key never re-runs
        key = "TRAIN-ACT-0::r2::plan-audit"
        relay_io.outcome_ack(key)
        replay_effects = sum(relay_io.outcome_actionable(key) for _ in range(10))

        return {
            "fixture": {"landed": landed, "active": 3, "buildSeconds": round(build_s, 3)},
            "board": {"bytes": board_bytes, "under4KiB": board_bytes <= 4096,
                      "activeListed": len(board.get("trains", [])),
                      "fullHistoryEmbedded": "history" in json.dumps(board),
                      "firstRenderSeconds": round(first_render_s, 3)},
            "idle": {"ticks": 30, "boardWrites": idle_writes,
                     "noopPreservesMtime": noop_preserved,
                     "seconds": round(idle_s, 3)},
            "transition": {"roleMessages": msgs_delta},
            "replay": {"duplicateEffects": replay_effects},
            "tokenAccounting": {"actualTokens": None,
                                "note": "runtime does not expose token counts; "
                                        "estimates are intentionally NOT reported as actuals"},
        }
    finally:
        relay_mesh.rebind_root(RELAY_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    landed = int(sys.argv[sys.argv.index("--landed") + 1]) if "--landed" in sys.argv else 10000
    report = run(landed)
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    text = json.dumps(report, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text)

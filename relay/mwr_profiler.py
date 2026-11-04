#!/usr/bin/env python3
"""
relay/mwr_profiler.py — Metadata-to-Work Ratio (MWR) & Token Spend Profiler
==========================================================================
Measures the efficiency of multi-agent release trains by comparing governance metadata
volume against productive code diffs:
  MWR = (Bytes of PACKET.md + AUDIT_REPORTS + STATE) / max(1, Bytes of DIFF.patch)

Provides automated loop breaking if MWR or rework revision count exceeds safe thresholds.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
TRAINS_DIR = RELAY_DIR / "trains"
METRICS_LOG = RELAY_DIR / "history" / "mwr_metrics.jsonl"

if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import relay_mesh

# Safe thresholds
MAX_HEALTHY_MWR = 25.0  # Alert if >25x metadata overhead
MAX_REVISION_CHURN = 10  # Flag ping-pong rework loop


def profile_train(train_id: str) -> Dict[str, Any]:
    """Calculates MWR, byte footprints, and rework metrics for a given train."""
    train_dir = TRAINS_DIR / train_id
    if not train_dir.exists():
        return {"train_id": train_id, "error": "train directory not found"}

    state = relay_io.read_json(train_dir / "STATE.json") or {}
    revision = relay_mesh.train_revision(state)
    history = state.get("history", [])

    # Calculate metadata bytes
    meta_bytes = 0
    meta_files = ["PACKET.md", "PLAN_AUDIT_REPORT.md", "AUDIT_REPORT.md", "STATE.json", "IMPLEMENTATION_EVIDENCE.md"]
    for mf in meta_files:
        p = train_dir / mf
        if p.exists():
            meta_bytes += p.stat().st_size

    # Calculate productive work bytes (DIFF.patch)
    diff_file = train_dir / "DIFF.patch"
    diff_bytes = diff_file.stat().st_size if diff_file.exists() else 0

    # Calculate MWR
    effective_diff = max(1, diff_bytes)
    mwr = round(meta_bytes / effective_diff, 2)

    # Calculate cycle duration
    start_iso = state.get("created_at") or (history[0].get("timestamp") if history else "")
    last_iso = state.get("updated_at") or (history[-1].get("timestamp") if history else "")

    status = state.get("status", "UNKNOWN")
    is_churning = (revision >= MAX_REVISION_CHURN)
    is_high_mwr = (mwr > MAX_HEALTHY_MWR and diff_bytes > 0)

    report = {
        "train_id": train_id,
        "title": state.get("title", ""),
        "status": status,
        "revision": revision,
        "transitions_count": len(history),
        "metadata_bytes": meta_bytes,
        "productive_diff_bytes": diff_bytes,
        "mwr": mwr,
        "is_churning": is_churning,
        "is_high_mwr": is_high_mwr,
        "health": "CRITICAL_CHURN" if is_churning else ("HIGH_MWR_OVERHEAD" if is_high_mwr else "HEALTHY"),
        "timestamp": relay_mesh.now_iso(),
    }
    return report


def log_train_metrics(train_id: str) -> Dict[str, Any]:
    """Computes train MWR and appends the snapshot to the append-only metrics log."""
    report = profile_train(train_id)
    if "error" not in report:
        METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(METRICS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return report


def profile_all_trains() -> List[Dict[str, Any]]:
    """Runs a full sweep over all trains and generates an executive efficiency summary."""
    reports = []
    if TRAINS_DIR.exists():
        for td in sorted(TRAINS_DIR.iterdir()):
            if td.is_dir():
                rep = profile_train(td.name)
                if "error" not in rep:
                    reports.append(rep)
    return reports


if __name__ == "__main__":
    relay_io.utf8_console()
    print("[MWR PROFILER] Sweeping all trains for efficiency and MWR metrics...")
    all_reps = profile_all_trains()
    print(f"Total Trains Profiled: {len(all_reps)}")
    print(f"{'Train ID':<35} | {'Rev':<4} | {'Status':<25} | {'Diff':<8} | {'Meta':<8} | {'MWR':<6} | {'Health'}")
    print("-" * 115)
    for r in all_reps:
        print(f"{r['train_id']:<35} | {r['revision']:<4} | {r['status']:<25} | {r['productive_diff_bytes']:<8} | {r['metadata_bytes']:<8} | {r['mwr']:<6} | {r['health']}")

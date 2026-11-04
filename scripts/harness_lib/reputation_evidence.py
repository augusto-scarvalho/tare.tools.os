#!/usr/bin/env python3
"""SPEC-180 Reputation Evidence V1 (Shadow / Observability Only).

Builds contextual ReputationSnapshots from historical execution outcomes.
Reputation is observational evidence only and carries NO routing authority.
"""
from __future__ import annotations

import datetime as dt
import math
import secrets
from pathlib import Path
from typing import Any


def calculate_confidence(sample_size: int, k: float = 5.0) -> float:
    """Calculate confidence score [0.0, 1.0] based on sample size.
    Low sample size yields low confidence / high uncertainty.
    """
    if sample_size <= 0:
        return 0.0
    return round(1.0 - math.exp(-sample_size / k), 4)


def apply_temporal_decay(age_days: float, half_life_days: float = 30.0) -> float:
    """Return decay weight [0.0, 1.0] given age in days."""
    if age_days <= 0:
        return 1.0
    return round(math.exp(-math.log(2) * age_days / half_life_days), 4)


def build_reputation_snapshot(
    records: list[dict[str, Any]],
    *,
    dimension_filters: dict[str, str] | None = None,
    half_life_days: float = 30.0,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Compile a contextual ReputationSnapshot from outcome records.
    
    EVIDENCE ONLY: cannot alter risk tiers, select models, or bypass policies.
    """
    filters = dimension_filters or {}
    now_dt = dt.datetime.now(dt.timezone.utc)
    s_id = snapshot_id or f"rep-{secrets.token_hex(6)}"

    matching = []
    for r in records:
        match = True
        for key, val in filters.items():
            if r.get(key) != val:
                match = False
                break
        if match:
            matching.append(r)

    sample_size = len(matching)
    successes = 0
    failures = 0
    partials = 0
    blocked = 0
    infra_failures = 0
    total_cost = 0.0
    total_latency = 0.0
    weighted_passes = 0.0
    total_weight = 0.0

    for r in matching:
        ts_str = r.get("timestamp")
        age_days = 0.0
        if ts_str:
            try:
                r_dt = dt.datetime.fromisoformat(ts_str)
                age_days = max(0.0, (now_dt - r_dt).total_seconds() / 86400.0)
            except Exception:
                pass
        w = apply_temporal_decay(age_days, half_life_days)
        total_weight += w

        success = r.get("execution_success", False)
        err_cls = r.get("error_class")
        
        if success:
            successes += 1
            weighted_passes += w
        elif err_cls in ("VendorFailover", "NetworkTimeout", "InfraFailure"):
            infra_failures += 1
            failures += 1
        elif err_cls in ("BlockedPartial", "PartialExecution"):
            partials += 1
        elif err_cls == "Blocked":
            blocked += 1
        else:
            failures += 1

        total_cost += float(r.get("total_cost_usd", 0.0))
        total_latency += float(r.get("duration_ms", 0))

    confidence = calculate_confidence(sample_size)
    pass_rate = (weighted_passes / total_weight) if total_weight > 0 else 0.0
    avg_cost = (total_cost / sample_size) if sample_size > 0 else 0.0
    avg_latency = (total_latency / sample_size) if sample_size > 0 else 0.0

    return {
        "snapshotId": s_id,
        "timestamp": now_dt.isoformat(timespec="seconds"),
        "dimensions": filters,
        "sampleSize": sample_size,
        "successes": successes,
        "failures": failures,
        "partials": partials,
        "blocked": blocked,
        "infraFailures": infra_failures,
        "weightedPassRate": round(pass_rate, 4),
        "avgCostUsd": round(avg_cost, 6),
        "avgLatencyMs": round(avg_latency, 2),
        "confidence": confidence,
        "decayHalfLifeDays": half_life_days,
        "shadowMode": True,
        "authorityGranted": False,
    }

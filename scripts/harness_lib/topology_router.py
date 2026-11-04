"""DW.4 deterministic topology router — observe-only verdict stamped at plan time.

Measurement-before-control: ``recommendedWorkers`` is ALWAYS 1 this phase.
Auto-fork is forbidden until the DW.3 metrics are trusted; the verdict only
records whether the router WOULD fork and why, so the recommendation can be
audited against real outcomes before it is ever allowed to control topology.

Stdlib only.
"""
from __future__ import annotations

from typing import Any

from harness_lib.common import HARNESS, read_json

# The exact open-circuit literal written by async_state.workflow_executor_record_failure
# and enforced by workflow_executor_circuit_assert_usable: state["state"] == "open"
# (and only while the breaker is enabled). Keep in lockstep with async_state.py.
_OPEN = "open"


def open_circuits() -> list[str]:
    """Executor names whose ``.harness/runtime/executor-circuit-*.json`` is open.

    Mirrors async_state.workflow_executor_circuit_assert_usable: an executor is
    open only when its file is a dict with a truthy ``enabled`` and
    ``state == "open"``. Missing, malformed, or non-dict files are not open.
    """
    names: list[str] = []
    for path in sorted((HARNESS / "runtime").glob("executor-circuit-*.json")):
        try:
            state = read_json(path, {}) or {}
        except Exception:  # odd/corrupt file -> not open
            continue
        if isinstance(state, dict) and state.get("enabled") and state.get("state") == _OPEN:
            names.append(str(state.get("executor") or path.stem[len("executor-circuit-"):]))
    return names


def verdict(limits: dict, split_strategy: Any, shard_count: int, open_circuits: list[str]) -> dict:
    """Deterministic would-fork verdict. Never recommends more than 1 worker (DW.4)."""
    mw = int((limits or {}).get("maxWorkers", 1))
    would_fork = shard_count > 1 and mw > 1 and not open_circuits
    reasons: list[str] = []
    if shard_count > 1:
        reasons.append(f"shards={shard_count} (>1: independent scopes)")
    else:
        reasons.append(f"shards={shard_count} (<=1: single scope, nothing to fork)")
    if mw > 1:
        reasons.append(f"maxWorkers={mw}")
    else:
        reasons.append(f"maxWorkers={mw} (<=1: fan-out capped)")
    if open_circuits:
        for name in open_circuits:
            reasons.append(f"circuit open: {name} — would not fork")
    else:
        reasons.append("no open circuits")
    res = {
        "recommendedWorkers": 1,  # ALWAYS 1 this phase — observe-only, see module docstring
        "wouldFork": would_fork,
        "reasons": reasons,
        "inputs": {
            "maxWorkers": mw,
            "splitStrategy": split_strategy,
            "shardCount": shard_count,
            "openCircuits": list(open_circuits),
        },
    }
    try:
        from harness_lib import unified_routing
        ctx = unified_routing.create_decision_context(
            task="topology_routing",
            task_type="topology_router",
            extra={"shardCount": shard_count, "openCircuits": list(open_circuits)},
        )
        dec = unified_routing.create_route_decision(
            component="topology_router",
            input_context=ctx,
            chosen_profile="single_worker" if not would_fork else "multi_worker_recommended",
            chosen_model="topology-observe-only",
            chosen_role="topology_router",
            confidence=1.0,
            reasoning_summary="; ".join(reasons),
        )
        unified_routing.record_shadow_decision(ctx, dec)
    except Exception:
        pass
    return res


#!/usr/bin/env python3
"""SPEC-179 Routing Evidence Pipeline V1.

Provides correlation, query, and coverage reporting across DecisionContext,
RouteDecision, Execution, and OutcomeRecord.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from harness_lib import event_ledger


def compute_digest(data: Any) -> str:
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def record_evidence_link(
    decision_id: str,
    outcome: dict[str, Any],
    *,
    run_id: str | None = None,
    root: Path | str | None = None,
) -> str:
    """Correlate a RouteDecision with its Execution OutcomeRecord."""
    r_path = Path(root) if root else Path.cwd()
    payload = {
        "decisionId": decision_id,
        "runId": run_id,
        "outcome": outcome,
        "outcomeDigest": compute_digest(outcome),
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    return event_ledger.append("routing_outcome_correlation", payload, root=r_path)


def read_shadow_events(root: Path | str | None = None) -> list[dict[str, Any]]:
    r_path = Path(root) if root else Path.cwd()
    events_path = r_path / ".harness" / "runs" / "events.jsonl"
    if not events_path.exists():
        return []
    out = []
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            if row.get("event") in ("shadow_routing_decision", "routing_outcome_correlation"):
                out.append(row)
        except Exception:
            pass
    return out


def get_decision_evidence(decision_id: str, root: Path | str | None = None) -> dict[str, Any] | None:
    events = read_shadow_events(root)
    dec_event = None
    out_event = None

    for ev in events:
        if ev.get("event") == "shadow_routing_decision":
            d_obj = ev.get("decision") or {}
            if d_obj.get("decision_id") == decision_id:
                dec_event = ev
        elif ev.get("event") == "routing_outcome_correlation":
            if ev.get("decisionId") == decision_id:
                out_event = ev

    if not dec_event and not out_event:
        return None

    dec_obj = (dec_event.get("decision") if dec_event else {}) or {}
    ctx_obj = (dec_event.get("context") if dec_event else {}) or {}
    out_obj = (out_event.get("outcome") if out_event else (dec_event.get("outcome") if dec_event else {})) or {}

    return {
        "decisionId": decision_id,
        "component": dec_obj.get("component"),
        "chosenProfile": dec_obj.get("chosen_profile"),
        "chosenModel": dec_obj.get("chosen_model"),
        "confidence": dec_obj.get("confidence"),
        "configVersion": "1.0",
        "featureSchemaVersion": "1.0",
        "contextDigest": compute_digest(ctx_obj),
        "decisionDigest": compute_digest(dec_obj),
        "outcomeDigest": compute_digest(out_obj),
        "executionSuccess": out_obj.get("execution_success"),
        "durationMs": out_obj.get("duration_ms"),
        "totalTokens": out_obj.get("total_tokens"),
        "totalCostUsd": out_obj.get("total_cost_usd"),
        "errorClass": out_obj.get("error_class"),
        "correlated": bool(dec_event and out_event),
    }


def coverage_report(root: Path | str | None = None) -> dict[str, Any]:
    events = read_shadow_events(root)
    decisions: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    by_path: dict[str, int] = {}

    for ev in events:
        if ev.get("event") == "shadow_routing_decision":
            d_obj = ev.get("decision") or {}
            d_id = d_obj.get("decision_id")
            if d_id:
                decisions[d_id] = ev
                comp = d_obj.get("component") or "unknown"
                by_path[comp] = by_path.get(comp, 0) + 1
        elif ev.get("event") == "routing_outcome_correlation":
            d_id = ev.get("decisionId")
            if d_id:
                outcomes[d_id] = ev

    joined_count = sum(1 for d_id in decisions if d_id in outcomes)
    orphan_decisions = [d_id for d_id in decisions if d_id not in outcomes]
    orphan_outcomes = [d_id for d_id in outcomes if d_id not in decisions]
    total_unique = len(set(decisions) | set(outcomes))
    orphan_rate = (len(orphan_decisions) / len(decisions)) if decisions else 0.0

    return {
        "routingPathsBreakdown": by_path,
        "decisionsCount": len(decisions),
        "outcomesJoined": joined_count,
        "orphanDecisions": len(orphan_decisions),
        "orphanOutcomes": len(orphan_outcomes),
        "orphanRate": round(orphan_rate, 4),
        "totalUniqueEvents": total_unique,
    }

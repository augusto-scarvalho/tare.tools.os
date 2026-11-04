#!/usr/bin/env python3
"""Unified Routing Decision Contract (Shadow / Observability-Only Mode).

SPEC-176: Provides unified, typed schemas (`DecisionContext`, `RouteDecision`,
and `OutcomeRecord`) to capture shadow telemetry across all 6 harness routing components.

Shadow mode is strictly observational and pass-through: recording failures are swallowed,
and live routing choices/fallback chains remain 100% unaffected.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from harness_lib import common, event_ledger


def generate_decision_id(component: str, task: str | None = None) -> str:
    seed = f"{component}:{task}:{dt.datetime.now().astimezone().isoformat()}:{uuid.uuid4().hex[:8]}"
    return "dec-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def create_decision_context(
    task: str = "",
    task_type: str = "general",
    requested_role: str | None = None,
    cost_sensitivity: str = "medium",
    latency_sensitivity: str = "medium",
    risk_tier: str = "medium",
    staged_files: list[str] | None = None,
    workflow_id: str | None = None,
    parent_decision_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "task": str(task),
        "task_type": str(task_type),
        "requested_role": requested_role,
        "cost_sensitivity": cost_sensitivity,
        "latency_sensitivity": latency_sensitivity,
        "risk_tier": risk_tier,
        "staged_files": list(staged_files or []),
        "workflow_id": workflow_id,
        "parent_decision_id": parent_decision_id,
        "extra": dict(extra or {}),
    }


def create_route_decision(
    component: str,
    input_context: dict[str, Any],
    chosen_profile: str,
    chosen_model: str,
    chosen_role: str,
    confidence: float = 1.0,
    reasoning_summary: str = "shadow decision",
    fallback_chain: list[str] | None = None,
    applied_policies: list[str] | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    d_id = decision_id or generate_decision_id(component, input_context.get("task"))
    return {
        "decision_id": d_id,
        "timestamp": common.now(),
        "component": str(component),
        "input_context": dict(input_context),
        "chosen_profile": str(chosen_profile),
        "chosen_model": str(chosen_model),
        "chosen_role": str(chosen_role),
        "confidence": float(confidence),
        "reasoning_summary": str(reasoning_summary),
        "fallback_chain": list(fallback_chain or []),
        "applied_policies": list(applied_policies or []),
        "shadow_mode": True,
    }


def create_outcome_record(
    decision_id: str,
    execution_success: bool = True,
    duration_ms: int = 0,
    total_tokens: int = 0,
    total_cost_usd: float = 0.0,
    error_class: str | None = None,
    quality_score: float | None = None,
    feedback_signal: str | None = None,
) -> dict[str, Any]:
    return {
        "decision_id": str(decision_id),
        "timestamp": common.now(),
        "execution_success": bool(execution_success),
        "duration_ms": int(duration_ms),
        "total_tokens": int(total_tokens),
        "total_cost_usd": float(total_cost_usd),
        "error_class": error_class,
        "quality_score": quality_score,
        "feedback_signal": feedback_signal,
    }


def record_shadow_decision(
    context: dict[str, Any],
    decision: dict[str, Any],
    outcome: dict[str, Any] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Emit shadow routing decision telemetry event.
    
    Observability only: failure to log is swallowed to guarantee zero runtime disruption.
    """
    payload = {
        "context": context,
        "decision": decision,
        "outcome": outcome,
        "shadow_mode": True,
    }
    try:
        event_ledger.append("shadow_routing_decision", payload, root=root)
    except Exception:
        pass
    return payload

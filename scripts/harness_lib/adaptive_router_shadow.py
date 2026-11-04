#!/usr/bin/env python3
"""SPEC-181 Adaptive Router Shadow Policy V1.

Ranks deterministically eligible routing candidates in shadow mode.
Non-disruptive and strictly observational.
"""
from __future__ import annotations

import secrets
from typing import Any

from harness_lib import unified_routing


def evaluate_shadow_recommendation(
    context: dict[str, Any],
    eligible_candidates: list[str],
    production_candidate: str,
    reputation_snapshot: dict[str, Any] | None = None,
    circuit_state: dict[str, bool] | None = None,
    confidence_threshold: float = 0.3,
) -> dict[str, Any]:
    """Produce a ShadowRouteRecommendation from deterministically eligible candidates.
    
    The deterministic floor runs FIRST. The adaptive selector only ranks candidates
    that passed the deterministic policy floor.
    """
    rec_id = f"srec-{secrets.token_hex(6)}"
    rep = reputation_snapshot or {}
    circuits = circuit_state or {}
    confidence = float(rep.get("confidence", 0.0))

    filtered_candidates: list[dict[str, str]] = []
    viable_candidates: list[str] = []

    for cand in eligible_candidates:
        if circuits.get(cand, False):
            filtered_candidates.append({"candidate": cand, "reason": "circuit_open"})
        else:
            viable_candidates.append(cand)

    if confidence < confidence_threshold:
        return {
            "recommendationId": rec_id,
            "candidateSet": eligible_candidates,
            "filteredCandidates": filtered_candidates,
            "scoreComponents": {},
            "selectedShadowCandidate": None,
            "activeProductionCandidate": production_candidate,
            "disagreement": False,
            "confidence": confidence,
            "abstentionReason": f"confidence_{confidence}_below_threshold_{confidence_threshold}",
            "shadowMode": True,
        }

    if not viable_candidates:
        return {
            "recommendationId": rec_id,
            "candidateSet": eligible_candidates,
            "filteredCandidates": filtered_candidates,
            "scoreComponents": {},
            "selectedShadowCandidate": None,
            "activeProductionCandidate": production_candidate,
            "disagreement": False,
            "confidence": confidence,
            "abstentionReason": "no_viable_candidates_after_circuit_filter",
            "shadowMode": True,
        }

    pass_rate = float(rep.get("weightedPassRate", 0.5))
    scores: dict[str, float] = {}
    for cand in viable_candidates:
        base_score = 0.5
        if cand == rep.get("dimensions", {}).get("model"):
            base_score = pass_rate
        scores[cand] = round(base_score, 4)

    best_candidate = max(viable_candidates, key=lambda c: (scores.get(c, 0.0), c))
    disagreement = best_candidate != production_candidate

    return {
        "recommendationId": rec_id,
        "candidateSet": eligible_candidates,
        "filteredCandidates": filtered_candidates,
        "scoreComponents": scores,
        "selectedShadowCandidate": best_candidate,
        "activeProductionCandidate": production_candidate,
        "disagreement": disagreement,
        "confidence": confidence,
        "abstentionReason": None,
        "shadowMode": True,
    }


def classify_counterfactual_evaluation(
    recommended_candidate: str | None,
    production_candidate: str,
    historical_outcome: dict[str, Any] | None = None,
) -> str:
    """Classify counterfactual evaluation method for a shadow recommendation."""
    if not recommended_candidate:
        return "unknown_counterfactual"
    if recommended_candidate == production_candidate:
        return "directly_observed"
    if historical_outcome and historical_outcome.get("has_replay_record"):
        return "paired_replay_evidence"
    if historical_outcome and "quality_score" in historical_outcome:
        return "proxy"
    return "unknown_counterfactual"

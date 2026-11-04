"""Deterministic token-economics helpers for workflow planning and audit.

These functions are intentionally dependency-free. The harness uses stable
character-divisor estimates for budgeting/regression instead of model-specific
tokenizers so the template works across Codex, Claude, local CLI executors, and
offline validation.
"""
from __future__ import annotations

import math
from typing import Any

# charsPerToken 3.1: chars/4 measured ~1.3x low on real path/punctuation-heavy
# packets (audit + TASK-002 E2: heuristic vs chars/4 = 1.31x, 3.046 chars/token);
# token limits below are rebased ~1.3x so effective char ceilings are unchanged.
DEFAULT_TOKEN_BUDGET: dict[str, Any] = {
    "estimationMode": "chars-divisor",
    "charsPerToken": 3.1,
    "warnAtRatio": 0.8,
    "maxWorkerPromptTokens": 3900,
    "maxTotalWorkerPromptTokens": 31000,
    "maxReducerPromptTokens": 2000,
    "maxReviewerPromptTokens": 2000,
    "maxWorkerOutputTokens": 1300,
    "maxTotalPlannedTokens": 42000,
    "reportPath": "reduce/token-audit.json",
}


def merged_token_budget(
    global_token_budget: dict[str, Any] | None = None,
    profile_token_budget: dict[str, Any] | None = None,
    *,
    workflow_type: str | None = None,
) -> dict[str, Any]:
    """Merge global and profile token-budget settings into runtime defaults."""
    cfg = dict(DEFAULT_TOKEN_BUDGET)
    if isinstance(global_token_budget, dict):
        cfg.update(global_token_budget)
    if isinstance(profile_token_budget, dict):
        cfg.update(profile_token_budget)
    if workflow_type == "fork-join" and "maxTotalWorkerPromptTokens" not in cfg:
        cfg["maxTotalWorkerPromptTokens"] = 18000  # rebased ~1.3x (charsPerToken 3.1)
    return cfg


def chars_per_token(cfg: dict[str, Any] | None = None) -> float:
    try:
        return max(1.0, float((cfg or {}).get("charsPerToken", DEFAULT_TOKEN_BUDGET["charsPerToken"])))
    except Exception:
        return float(DEFAULT_TOKEN_BUDGET["charsPerToken"])


def estimate_tokens_from_text(text: str | None, cfg: dict[str, Any] | None = None) -> int:
    return int(math.ceil(len(text or "") / chars_per_token(cfg)))


def estimate_tokens_from_chars(chars: int, cfg: dict[str, Any] | None = None) -> int:
    return int(math.ceil(max(0, int(chars)) / chars_per_token(cfg)))


def token_budget_status(value: int, limit: int | None, warn_ratio: float) -> str:
    if not limit or limit <= 0:
        return "pass"
    if value > limit:
        return "fail"
    if value >= int(limit * warn_ratio):
        return "warn"
    return "pass"


def token_budget_limit(cfg: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except Exception:
        return default


def rollup_by_type(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate workflow token-audit summaries by workflow type."""
    rollup: dict[str, dict[str, Any]] = {}
    for item in summaries:
        workflow_type = str(item.get("type") or "unknown")
        bucket = rollup.setdefault(workflow_type, {
            "workflowType": workflow_type,
            "workflowCount": 0,
            "profiles": [],
            "workers": 0,
            "totalWorkerPromptTokens": 0,
            "plannedWorkerOutputTokens": 0,
            "totalWorkerResultTokens": 0,
            "totalReducerPromptTokens": 0,
            "totalReducerResultTokens": 0,
            "totalPlannedTokens": 0,
            "totalObservedTokens": 0,
            "statusCounts": {},
        })
        bucket["workflowCount"] += 1
        profile = item.get("workflowProfile")
        if profile and profile not in bucket["profiles"]:
            bucket["profiles"].append(profile)
        bucket["workers"] += int(item.get("workers", 0) or 0)
        for key in [
            "totalWorkerPromptTokens",
            "plannedWorkerOutputTokens",
            "totalWorkerResultTokens",
            "totalReducerPromptTokens",
            "totalReducerResultTokens",
            "totalPlannedTokens",
            "totalObservedTokens",
        ]:
            bucket[key] += int(item.get(key, 0) or 0)
        status = str(item.get("status") or "unknown")
        bucket["statusCounts"][status] = int(bucket["statusCounts"].get(status, 0)) + 1
    for bucket in rollup.values():
        count = max(1, int(bucket.get("workflowCount", 1)))
        bucket["avgPlannedTokensPerWorkflow"] = int(round(int(bucket.get("totalPlannedTokens", 0)) / count))
        bucket["avgObservedTokensPerWorkflow"] = int(round(int(bucket.get("totalObservedTokens", 0)) / count))
        statuses = set(bucket.get("statusCounts", {}).keys())
        bucket["status"] = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    return dict(sorted(rollup.items()))

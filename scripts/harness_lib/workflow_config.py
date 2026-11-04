"""Workflow config / budget / lock / event domain for the Universal Agent Harness.

Split out of scripts/harness.py (MF.1 round 2, harness-lines-burndown): the
cohesive block that reads workflow configuration (limits, runtime limits, run
lock staleness, budgets, token budgets) and owns the run-lock lifecycle plus the
workflow event helper. harness.py imports this module as ``workflow_config_lib``
and re-exports every EXPORTED_FUNCTIONS name so existing callers -- internal and
external (``import harness; harness.workflow_limits(...)``) -- keep working.

The domain needs a few harness-router names (load_project, workflow_dir,
workflow_profile_config, append_event, WORKFLOWS_DIR); those arrive via bind(env)
at import time, mirroring async_runtime.bind / async_state.bind.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

from harness_lib import token_calibration, token_economics
from harness_lib.common import HarnessError, ROOT, now, parse_iso_datetime, read_json, write_json


def bind(env: dict[str, Any]) -> None:
    """Inject the harness-router callbacks this domain needs (mirror of async_state.bind)."""
    required = ("WORKFLOWS_DIR", "append_event", "load_project", "workflow_dir", "workflow_profile_config")
    missing = sorted(name for name in required if name not in env)
    if missing:
        raise RuntimeError("workflow_config bind missing dependencies: " + ", ".join(missing))
    globals().update({name: env[name] for name in required})


def workflow_config() -> dict[str, Any]:
    project = load_project()
    cfg = project.get("workflows", {}) if isinstance(project.get("workflows"), dict) else {}
    return cfg

def workflow_limits(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = workflow_config()
    limits = cfg.get("limits", {}) if isinstance(cfg.get("limits"), dict) else {}
    overrides = overrides or {}
    def as_int(key: str, default: int) -> int:
        try:
            return int(overrides.get(key, limits.get(key, default)))
        except Exception:
            return default
    return {
        "maxWorkers": max(1, as_int("maxWorkers", 8)),
        "maxRounds": max(1, as_int("maxRounds", 2)),
        "maxWorkerOutputChars": max(1000, as_int("maxWorkerOutputChars", 4000)),
        "concurrency": max(1, as_int("concurrency", 1)),
        "timeoutSeconds": max(30, as_int("timeoutSeconds", 1800)),
    }

def workflow_runtime_limits() -> dict[str, Any]:
    cfg = workflow_config().get("agentRuntimeLimits", {}) if isinstance(workflow_config().get("agentRuntimeLimits"), dict) else {}
    return cfg

def workflow_run_lock_stale_seconds() -> int:
    run_lock = workflow_config().get("runLock", {}) if isinstance(workflow_config().get("runLock"), dict) else {}
    try:
        return int(run_lock.get("staleAfterSeconds", 7200))
    except Exception:
        return 7200

def workflow_type_from_profile(profile_name: str | None) -> str | None:
    if not profile_name:
        return None
    cfg = workflow_profile_config(profile_name)
    t = cfg.get("type")
    return str(t) if t else None

def workflow_budget_config() -> dict[str, Any]:
    cfg = workflow_config()
    budgets = cfg.get("budgets", {}) if isinstance(cfg.get("budgets"), dict) else {}
    return budgets

def workflow_budget_value(section: str, key: str, default: int) -> int:
    section_cfg = workflow_budget_config().get(section, {})
    if not isinstance(section_cfg, dict):
        return default
    try:
        return int(section_cfg.get(key, default))
    except Exception:
        return default

def token_budget_enforced(profile_name: str | None = None) -> bool:
    """SPEC-102: budget refusal is opt-in via enforceTokenBudget (profile overrides global)."""
    profile_cfg = (load_workflow_profiles().get("profiles", {}) or {}).get(profile_name or "", {})
    profile_budget = profile_cfg.get("tokenBudget") if isinstance(profile_cfg, dict) else None
    if isinstance(profile_budget, dict) and "enforceTokenBudget" in profile_budget:
        return profile_budget.get("enforceTokenBudget") is True
    global_cfg = workflow_budget_config().get("tokenBudget", {})
    return isinstance(global_cfg, dict) and global_cfg.get("enforceTokenBudget") is True


def enforce_token_budget(wfid: str, workflow: dict[str, Any], *, override: bool = False) -> None:
    if not token_budget_enforced(workflow.get("workflowProfile")):
        return
    plan = read_json(workflow_dir(wfid) / "plan.json", {}) or {}
    audit = plan.get("tokenAudit", {}) or {}
    if audit.get("status") != "fail":
        return
    if override:
        append_event("workflow_budget_overridden", {
            "workflowId": wfid,
            "plannedTokens": audit.get("totalPlannedTokens"),
            "limit": audit.get("maxTotalPlannedTokens"),
        })
        return
    raise HarnessError(
        f"Workflow {wfid} exceeds its token budget: planned {audit.get('totalPlannedTokens')} > limit {audit.get('maxTotalPlannedTokens')}\n"
        "cause: enforceTokenBudget is enabled for this profile/project and the token audit status is 'fail'\n"
        f"fix: shrink the plan (fewer shards/workers), raise the profile tokenBudget, or rerun with --override-budget (records an override event)"
    )


def workflow_token_budget_config(profile_name: str | None = None, workflow_type: str | None = None,
                                 subject: str | None = None) -> dict[str, Any]:
    """Return token-budget defaults merged with per-workflow-profile overrides."""
    global_cfg = workflow_budget_config().get("tokenBudget", {})
    # The committed charsPerToken is the SEED; a locally-learned override (gitignored,
    # written by self-review) wins for sizing so learning never touches tracked config.
    # per-target-token-calibration: a TARGET workflow prefers its own subject's
    # learned value, cascading to the harness's, then the seed.
    override_cpt = None
    if subject and subject != "self":
        override_cpt = token_calibration.read_override(ROOT, subject)
    if override_cpt is None:
        override_cpt = token_calibration.read_override(ROOT)
    if override_cpt is not None and isinstance(global_cfg, dict):
        global_cfg = {**global_cfg, "charsPerToken": override_cpt}
    profile_budget: dict[str, Any] | None = None
    if profile_name:
        profile_cfg = (load_workflow_profiles().get("profiles", {}) or {}).get(profile_name, {})
        profile_budget = profile_cfg.get("tokenBudget", {}) if isinstance(profile_cfg, dict) else None
    return token_economics.merged_token_budget(
        global_cfg if isinstance(global_cfg, dict) else None,
        profile_budget if isinstance(profile_budget, dict) else None,
        workflow_type=workflow_type,
    )

def token_chars_per_token(profile_name: str | None = None, workflow_type: str | None = None) -> float:
    return token_economics.chars_per_token(workflow_token_budget_config(profile_name, workflow_type))

def estimate_tokens_from_text(text: str | None, *, profile_name: str | None = None, workflow_type: str | None = None) -> int:
    return token_economics.estimate_tokens_from_text(text, workflow_token_budget_config(profile_name, workflow_type))

def estimate_tokens_from_chars(chars: int, *, profile_name: str | None = None, workflow_type: str | None = None) -> int:
    return token_economics.estimate_tokens_from_chars(chars, workflow_token_budget_config(profile_name, workflow_type))

def workflow_lock_path(wfid: str) -> Path:
    return workflow_dir(wfid) / ".run.lock"

def lock_payload(command: str) -> dict[str, Any]:
    return {"pid": os.getpid(), "startedAt": now(), "command": command, "cwd": str(ROOT)}

def workflow_lock_active(path: Path) -> bool:
    """Return True only for active non-stale locks.

    A lock is stale when its recorded process no longer exists or when its age
    exceeds workflows.runLock.staleAfterSeconds. Age-based staleness matters
    across containers/hosts where PID checks can be misleading.
    """
    if not path.exists():
        return False
    try:
        data = read_json(path, {}) or {}
    except Exception:
        return True
    started = parse_iso_datetime(str(data.get("startedAt", "")))
    if started is not None:
        age = (dt.datetime.now(started.tzinfo or dt.timezone.utc) - started).total_seconds()
        if age > workflow_run_lock_stale_seconds():
            return False
    try:
        pid = int(data.get("pid", 0))
    except Exception:
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True

def acquire_workflow_lock(wfid: str, command: str, *, force: bool = False) -> None:
    path = workflow_lock_path(wfid)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and workflow_lock_active(path) and not force:
        raise HarnessError(
            f"Workflow {wfid} is locked by another run: {path.relative_to(ROOT)}\n"
            f"cause: a previous run is still active or crashed while holding the lock\n"
            f"fix: confirm nothing is running, then: python scripts/harness.py workflow unlock {wfid}"
        )
    if path.exists() and not workflow_lock_active(path):
        path.unlink()
    write_json(path, lock_payload(command))
    append_event("workflow_lock_acquired", {"workflowId": wfid, "command": command})

def release_workflow_lock(wfid: str) -> None:
    path = workflow_lock_path(wfid)
    if path.exists():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        append_event("workflow_lock_released", {"workflowId": wfid})

def workflow_event(wfid: str, event: str, payload: dict[str, Any] | None = None) -> None:
    payload = dict(payload or {})
    payload.setdefault("workflowId", wfid)
    append_event(event, payload)

def load_workflow_profiles() -> dict[str, Any]:
    path = WORKFLOWS_DIR / "workflow-profiles.json"
    data = read_json(path, {}) or {}
    return data if isinstance(data, dict) else {}


EXPORTED_FUNCTIONS = [
    "workflow_config",
    "workflow_limits",
    "workflow_runtime_limits",
    "workflow_run_lock_stale_seconds",
    "workflow_type_from_profile",
    "workflow_budget_config",
    "workflow_budget_value",
    "enforce_token_budget",
    "workflow_token_budget_config",
    "token_chars_per_token",
    "estimate_tokens_from_text",
    "estimate_tokens_from_chars",
    "workflow_lock_path",
    "workflow_lock_active",
    "acquire_workflow_lock",
    "release_workflow_lock",
    "workflow_event",
    "load_workflow_profiles",
]

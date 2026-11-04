"""Spawn resolution + the worker SEAT derived from it (SPEC-128 v4, backlog
session-seat-unwired). A spawned worker's HARNESS_SESSION_ROLE/MODEL/EFFORT must
describe the seat it ACTUALLY runs on — the same resolved spawn as its command,
never the parent's triplet and never the routing pin. Keeping the resolution in
ONE place is why `resolved_spawn` here is shared by the command builder and the
seat builder: they cannot drift onto different hops.

Lives in a lib (not harness.py) so the harness-py-line-budget ratchet
(wt_workflow_tree wt-3) stays green. `active_executor` and `executor_profile_spawn`
live in harness.py and are injected via bind(), the same pattern async_runtime and
workflow_reduce use.
"""
from __future__ import annotations

from typing import Any

try:
    from . import chat_engines
    from .common import HarnessError
except ImportError:  # direct run (self-check): put scripts/ on the path
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from harness_lib import chat_engines
    from harness_lib.common import HarnessError

# Injected from harness.py by bind() (executor resolution lives there).
active_executor = None
executor_config = None
executor_profile_spawn = None
resolve_worker_executor = None


def bind(env: dict[str, Any]) -> None:
    global active_executor, executor_config, executor_profile_spawn, resolve_worker_executor
    active_executor = env["active_executor"]
    executor_config = env["executor_config"]
    executor_profile_spawn = env["executor_profile_spawn"]
    resolve_worker_executor = env.get("resolve_worker_executor")


def validated_branch_spawn(entry: dict[str, Any], index: int) -> dict[str, Any] | None:
    """Validate/carry the optional P6 branch seat without growing harness.py."""
    if "spawn" not in entry:
        return None
    spawn = entry["spawn"]
    if not isinstance(spawn, dict):
        raise HarnessError(f"fork branch {index} spawn must be an object")
    extra = set(spawn) - {"executor", "model", "effort"}
    if extra:
        raise HarnessError(f"fork branch {index} spawn has unknown key(s): {', '.join(sorted(extra))}")
    if "executor" in spawn:
        try:
            executor_config(str(spawn["executor"]))
        except HarnessError as exc:
            raise HarnessError(f"fork branch {index} spawn.executor is invalid: {exc}") from exc
    return spawn


def fork_branch_row(row: tuple[Any, ...]) -> dict[str, Any]:
    branch = {"branchId": row[0], "workerRole": row[1], "title": row[2], "taskProfile": row[3],
              "paths": (row[4] if len(row) > 4 else []), "splitStrategy": "fork-join"}
    if len(row) > 5 and row[5] is not None:
        branch["spawn"] = row[5]
    if len(row) > 6 and isinstance(row[6], dict):
        branch.update(row[6])
    return branch


def branch_executor(worker: dict[str, Any]) -> str | None:
    spawn = worker.get("spawn")
    return active_executor(str(spawn["executor"])) if isinstance(spawn, dict) and spawn.get("executor") else None


def seed_executor_conflict(workflow: dict[str, Any], resolved_executor: str) -> list[str]:
    """Return multi-seed sources whose selected findings include this executor."""
    seed = workflow.get("seed") or {}
    if not isinstance(seed.get("workflowIds"), list) or not seed["workflowIds"]:
        return []
    conflicts = []
    for source in seed.get("sources") or []:
        raw = source.get("executors") if "executors" in source else source.get("executor")
        executors = raw if isinstance(raw, list) else [raw]
        if resolved_executor in executors:
            conflicts.append(str(source.get("workflowId")))
    return conflicts


def seed_isolation_conflicts(worker: dict[str, Any], resolved_executor: str,
                             workflow: dict[str, Any]) -> list[str]:
    """Return INV-1 conflicts unless plan-time filtering matches the runtime seat."""
    pin = branch_executor(worker)
    return [] if pin is not None and resolved_executor == pin else seed_executor_conflict(workflow, resolved_executor)


def worker_spawn_overrides(runnable: list[dict[str, Any]], run_override: dict[str, Any] | None) -> dict[str, dict[str, Any] | None]:
    """Merge each branch pin with the uniform run pin; the run pin wins."""
    return {w["workerId"]: ({**{k: v for k, v in (w.get("spawn") or {}).items()
                                if k in ("model", "effort") and v is not None},
                             **(run_override or {})} or None) for w in runnable}


def cap_worker_concurrency(wfid: str, requested: int, worker_executors: dict[str, str],
                           limit_resolver: Any, event_writer: Any) -> int:
    """Clamp a blocking mixed run by every distinct resolved executor limit."""
    limits = []
    for executor in dict.fromkeys(worker_executors.values()):
        limit = limit_resolver(executor)
        if limit is not None:
            limits.append(limit)
            if requested > limit:
                event_writer(wfid, "workflow_concurrency_capped", {"executor": executor, "requested": requested, "limit": limit})
    return min([requested, *limits])


def async_executor_caps(worker_executors: dict[str, str], limit_resolver: Any,
                        per_executor_cfg: dict[str, Any], default_cap: int) -> dict[str, int]:
    """Independent per-executor caps below the async global consumer pool."""
    default_cap = max(1, int(default_cap))
    caps = {}
    for executor in dict.fromkeys(worker_executors.values()):
        limit = limit_resolver(executor)
        configured = per_executor_cfg.get(executor)
        caps[executor] = max(1, min(default_cap,
                                    int(limit) if limit is not None else default_cap,
                                    int(configured) if configured is not None else default_cap))
    return caps


def resolved_spawn(profile_name: str, executor_name: str | None,
                   spawn_override: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """The active executor name and its resolved spawn mapping (with a SPEC-115
    failover override applied). Single source so the spawn COMMAND and the worker's
    session-seat ENV derive from the identical resolution and cannot drift."""
    executor_name = active_executor(executor_name)
    spawn = executor_profile_spawn(profile_name, executor_name)
    if not spawn:
        raise HarnessError(
            f"Profile {profile_name!r} has no spawn mapping for executor {executor_name!r}. "
            "Add profiles.<profile>.spawn.<executor> or executor.defaultSpawn, then run "
            f"`python scripts/harness.py executor validate {executor_name}`."
        )
    if spawn_override:  # SPEC-115 failover: chain entry pins model/effort for this hop
        spawn = {**spawn, **{k: v for k, v in spawn_override.items() if v is not None}}
    return executor_name, spawn


def session_env(profile_name: str, executor_name: str | None,
                spawn_override: dict[str, Any] | None = None) -> dict[str, str]:
    """The HARNESS_SESSION_* seat triplet for a spawned worker, from the SAME
    resolved spawn the command uses — never the routing pin, never the parent's.
    Role = the actual vendor selector: resolved `agent`, else `profile`, else the
    task profile. Model/effort come from the resolved spawn; `configured-by-executor`
    and empties clear to unknown inside session_role_env, so a seat is complete only
    when it is truthful (a fallback hop names the fallback card — the caller passes
    the hop's override)."""
    _executor_name, spawn = resolved_spawn(profile_name, executor_name, spawn_override)
    role = str(spawn.get("agent") or spawn.get("profile") or profile_name)
    model = str(spawn.get("model", ""))
    effort = str(spawn.get("effort", spawn.get("reasoning", "")))
    return chat_engines.session_role_env(role, model, effort)


def spawn_override_from_flags(model: str | None, effort: str | None) -> dict[str, str] | None:
    """The uniform operator spawn pin (DD-mechanization P1) built from `workflow
    run/start --model/--effort`: a dict of ONLY the provided keys, or None when
    neither is set. None is falsy at every consumer (resolved_spawn's `if
    spawn_override`, the guard's `if spawn_override and ...`), so an unset pin is
    byte-identical to the pre-pin spawn. Shared so `run` and `start` construct it
    identically (one None-drop, one seam)."""
    override = {k: v for k, v in (("model", model), ("effort", effort)) if v is not None}
    return override or None


def session_env_safe(profile_name: str | None, executor_name: str | None) -> dict[str, str]:
    """session_env that NEVER raises. A spawned lane's seat is telemetry-grade, but the
    dispatch launching it (the detached route-dispatch funnel) is load-bearing — a
    resolution glitch must yield no seat, never break the AFK run (session-seat-unwired
    follow-up: the route funnel rides the seat at build_worker_spawn_env, and this is the
    fail-open it rides through)."""
    try:
        return session_env(str(profile_name or ""), executor_name)
    except Exception:  # noqa: BLE001 — seat is best-effort; the dispatch must survive
        return {}


def _self_check() -> None:
    # Pure resolution + seat, with injected stubs (no harness import).
    bind({"active_executor": lambda e: e or "codex", "executor_config": lambda e: {"name": e},
          "executor_profile_spawn": lambda p, e: {"profile": p, "model": "m0", "effort": "high"}})
    assert resolved_spawn("scan", "codex", None)[1]["model"] == "m0"
    # a fallback override names the hop's card/effort, not the base
    fb = session_env("scan", "codex", {"model": "m9", "effort": "xhigh"})
    assert fb["HARNESS_SESSION_MODEL"] == "m9" and fb["HARNESS_SESSION_EFFORT"] == "xhigh", fb
    # role precedence agent > profile > profileName
    bind({"active_executor": lambda e: "claude", "executor_config": lambda e: {"name": e},
          "executor_profile_spawn": lambda p, e: {"agent": "implementer", "profile": p, "model": "c1"}})
    assert session_env("implementation", "claude")["HARNESS_SESSION_ROLE"] == "implementer"
    # session_env_safe never raises: a valid seat passes through, a missing spawn
    # mapping yields {} so the load-bearing dispatch survives a seat glitch.
    assert session_env_safe("implementation", "claude")["HARNESS_SESSION_ROLE"] == "implementer"
    bind({"active_executor": lambda e: "x", "executor_config": lambda e: {"name": e},
          "executor_profile_spawn": lambda p, e: None})
    assert session_env_safe("p", "x") == {}  # no spawn -> resolved_spawn raises -> {}
    # spawn_override_from_flags (DD-mechanization P1): only the provided keys; a partial
    # pin keeps the set key (the `or None` mutant would drop it); no flags -> None.
    assert spawn_override_from_flags("sonnet", "high") == {"model": "sonnet", "effort": "high"}
    assert spawn_override_from_flags("sonnet", None) == {"model": "sonnet"}  # partial pin kept
    assert spawn_override_from_flags(None, "high") == {"effort": "high"}
    assert spawn_override_from_flags(None, None) is None  # no flags -> byte-identical
    seeded = {"seed": {"workflowIds": ["WF-A", "WF-B"], "sources": [
        {"workflowId": "WF-A", "executors": ["claude"]},
        {"workflowId": "WF-B", "executors": ["codex"]}]}}
    assert seed_executor_conflict(seeded, "claude") == ["WF-A"]
    single = {"seed": {"workflowIds": ["WF-A"], "sources": [
        {"workflowId": "WF-A", "executors": ["claude", "codex"]}]}}
    assert seed_executor_conflict(single, "claude") == ["WF-A"]
    assert seed_executor_conflict(single, "nvidia-compat") == []
    assert seed_executor_conflict({"seed": {"workflowId": "WF-A"}}, "claude") == []
    bind({"active_executor": lambda e: e or "codex", "executor_config": lambda e: {"name": e},
          "executor_profile_spawn": lambda p, e: {"profile": p, "model": "m0", "effort": "high"}})
    pinned = {"spawn": {"executor": "claude"}}
    assert seed_isolation_conflicts(pinned, "claude", seeded) == []
    assert seed_isolation_conflicts(pinned, "codex", seeded) == ["WF-B"]
    limits = {"codex": 2, "claude": 1, "floor": 0}
    caps = async_executor_caps(
        {"W1": "codex", "W2": "claude", "W3": "missing", "W4": "floor"},
        lambda e: limits.get(e), {"codex": 3, "claude": 4, "floor": 0}, 5)
    assert caps == {"codex": 2, "claude": 1, "missing": 5, "floor": 1}, caps
    print("workflow_spawn self-check ok")


if __name__ == "__main__":
    _self_check()

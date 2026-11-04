"""Async workflow state & persistence for the Universal Agent Harness.

Split out of scripts/harness_lib/async_runtime.py (MF fragmentation): the pure,
asyncio-free state/persistence + await-policy + executor circuit-breaker domain.
async_runtime.py imports and re-exports every name defined here; the async
runtime itself (supervisor / await / cancel) stays there. Router callbacks
arrive via bind(env) at import time, mirroring async_runtime.bind.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import signal
from pathlib import Path
from typing import Any

from harness_lib import model_routing, processes
from harness_lib.common import HARNESS, HarnessError, now, parse_iso_datetime, read_json, replay_class, write_json

_BOUND_NAMES: set[str] = set()


def bind(env: dict[str, Any]) -> None:
    """Bind router callbacks used by the async state helpers (mirror of async_runtime.bind)."""
    allowed = {
        "AWAIT_MODES", "validate_worker_result", "worker_scope_paths",
        "workflow_config", "workflow_dir", "workflow_event", "workflow_limits",
        "workflow_profile_config", "workflow_runtime_limits", "workflow_state_put",
    }
    missing = sorted(name for name in allowed if name not in env)
    if missing:
        raise RuntimeError("async_state bind missing dependencies: " + ", ".join(missing))
    globals().update({name: env[name] for name in allowed})
    _BOUND_NAMES.update(allowed)


def workflow_async_config() -> dict[str, Any]:
    cfg = workflow_config().get("asyncScheduler", {}) if isinstance(workflow_config().get("asyncScheduler", {}), dict) else {}
    return cfg

def workflow_await_config() -> dict[str, Any]:
    cfg = workflow_config().get("awaitPolicy", {}) if isinstance(workflow_config().get("awaitPolicy", {}), dict) else {}
    return cfg

def workflow_async_dir(wfid: str) -> Path:
    return workflow_dir(wfid) / "async"

def workflow_async_tasks_dir(wfid: str) -> Path:
    return workflow_async_dir(wfid) / "tasks"

def workflow_async_group_path(wfid: str) -> Path:
    return workflow_async_dir(wfid) / "async-group.json"

def workflow_async_events_path(wfid: str) -> Path:
    return workflow_async_dir(wfid) / "events.jsonl"

def workflow_async_cancellation_path(wfid: str) -> Path:
    return workflow_async_dir(wfid) / "cancellation.json"

def workflow_async_supervisor_pid_path(wfid: str) -> Path:
    return workflow_async_dir(wfid) / "supervisor.pid"

def workflow_async_await_result_path(wfid: str) -> Path:
    return workflow_async_dir(wfid) / "await-result.json"

def workflow_async_task_path(wfid: str, async_task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", async_task_id)
    return workflow_async_tasks_dir(wfid) / f"{safe}.json"


# -- pending-queue ordering (queue-reorder, D039) -----------------------------
# There is NO cross-workflow async DEQUEUE today: each workflow schedules its own
# workers independently (workflow_async_runnable_workers), and the only observable
# cross-WF queue is the sorted active/ listing (ui_queue.queue_snapshot). So
# `workflow reorder` persists an explicit queueOrder OVER that listing, and
# ordered_pending_workflows is the accessor that HONORS it. These helpers are
# root-parameterized (not the bound workflow_dir) so the CLI drives the real root
# while tests drive a temp fixture set.
# ponytail: listing-order seam only — wire queueOrder into a real multi-WF
# scheduler if/when one lands (ui_queue.queue_snapshot is out of this slice's scope).
_QUEUE_WFID_RE = re.compile(r"^WF-[A-Za-z0-9][A-Za-z0-9_.-]*$")


def workflow_queue_order_path(root: Path | str) -> Path:
    return Path(root) / ".harness" / "workflows" / "queue-order.json"


def read_queue_order(root: Path | str) -> list[str]:
    data = read_json(workflow_queue_order_path(root), {}) or {}
    order = data.get("queueOrder") if isinstance(data, dict) else None
    return [str(x) for x in order] if isinstance(order, list) else []


def write_queue_order(root: Path | str, order: list[str]) -> None:
    write_json(workflow_queue_order_path(root),
               {"queueOrder": [str(x) for x in order], "updatedAt": now()})


def pending_workflow_ids(root: Path | str) -> list[str]:
    """Active workflow ids still PENDING (phase 'planned'), in the default sorted
    listing order — the queue `workflow reorder` reshapes. Running/settled workflows
    (any non-'planned' phase) are excluded."""
    active = Path(root) / ".harness" / "workflows" / "active"
    pending: list[str] = []
    if active.is_dir():
        for wf_file in sorted(active.glob("WF-*/workflow.json")):
            wf = read_json(wf_file, {}) or {}
            if str(wf.get("phase") or "planned") == "planned":
                pending.append(str(wf.get("workflowId") or wf_file.parent.name))
    return pending


def ordered_pending_workflows(root: Path | str) -> list[str]:
    """Pending workflow ids in QUEUE ORDER: the persisted queueOrder first (only ids
    still pending), then any remaining pending ids in the default sorted order — the
    stable fallback: ids absent from the list keep today's order after the listed ones."""
    pending = pending_workflow_ids(root)
    listed = [w for w in read_queue_order(root) if w in pending]
    return listed + [w for w in pending if w not in listed]


def workflow_reorder(root: Path | str, wfids: list[str]) -> dict[str, Any]:
    """Reorder the pending async queue: persist an explicit queueOrder over the active/
    listing. Every id must be WF-shaped (forecloses traversal / flag injection — this is
    the browser-input trust boundary), name a real active/ workflow, and still be PENDING
    ('planned'). A running/settled or unknown id is REFUSED (HarnessError -> exit 2)."""
    root = Path(root)
    active = root / ".harness" / "workflows" / "active"
    order: list[str] = []
    for raw in wfids:
        wfid = str(raw)
        if not _QUEUE_WFID_RE.match(wfid):
            raise HarnessError(f"invalid workflow id: {wfid!r} (must match WF-<id>)")
        wf_file = active / wfid / "workflow.json"
        if not wf_file.is_file():
            raise HarnessError(f"unknown workflow id: {wfid!r} (must exist under .harness/workflows/active/)")
        phase = str((read_json(wf_file, {}) or {}).get("phase") or "planned")
        if phase != "planned":
            raise HarnessError(
                f"workflow {wfid} is {phase!r}, not pending — reorder covers pending/queued workflows only")
        if wfid not in order:
            order.append(wfid)
    write_queue_order(root, order)
    return {"reordered": order, "queueOrder": ordered_pending_workflows(root)}

def tail_lines(path: Path | str | None, lines: int = 50, max_bytes: int = 262144) -> dict[str, Any]:
    """SPEC-118: last ``lines`` lines of a (live) log, reading only the tail.

    Pure/importable without ``bind()`` so the panel can reuse it. Seeks from the
    end, reads at most ``max_bytes``, decodes ``errors="replace"``, and drops the
    first (possibly partial) line when the read was clipped. Absence is a state:
    a missing log returns ``exists=False`` with empty lines (a queued worker),
    never an error.
    """
    p = Path(path) if path is not None else None
    if p is None or not p.exists():
        return {"exists": False, "size": 0, "lines": [], "clipped": False}
    size = p.stat().st_size
    read = min(size, max(0, int(max_bytes)))
    with p.open("rb") as fh:
        if size > read:
            fh.seek(size - read)
        data = fh.read()
    clipped = size > read
    text = data.decode("utf-8", errors="replace")
    parts = [p[:-1] if p.endswith("\r") else p for p in text.split("\n")]  # normalize CRLF (Windows workers)
    if clipped and parts:
        parts = parts[1:]  # the seek can land mid-line; discard the partial head
    if parts and parts[-1] == "":
        parts = parts[:-1]  # trailing newline is not an empty final line
    if lines and lines > 0:
        parts = parts[-lines:]
    return {"exists": True, "size": size, "lines": parts, "clipped": clipped}


def append_async_event(wfid: str, event: str, payload: dict[str, Any] | None = None) -> None:
    payload = dict(payload or {})
    payload.setdefault("workflowId", wfid)
    payload.setdefault("timestamp", now())
    payload["event"] = event
    payload["replayClass"] = replay_class(event)  # article Section 8.2, additive
    path = workflow_async_events_path(wfid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    workflow_event(wfid, event, payload)

def read_async_group(wfid: str) -> dict[str, Any]:
    return read_json(workflow_async_group_path(wfid), {}) or {}

def write_async_group(wfid: str, group: dict[str, Any]) -> None:
    group["updatedAt"] = now()
    write_json(workflow_async_group_path(wfid), group)
    workflow_state_put(wfid, "async-group", group)

def read_async_task(wfid: str, async_task_id: str) -> dict[str, Any]:
    return read_json(workflow_async_task_path(wfid, async_task_id), {}) or {}

def write_async_task(wfid: str, task: dict[str, Any]) -> None:
    task["updatedAt"] = now()
    async_task_id = str(task.get("asyncTaskId"))
    write_json(workflow_async_task_path(wfid, async_task_id), task)
    workflow_state_put(wfid, f"async-task-{async_task_id}", task)

def list_async_tasks(wfid: str) -> list[dict[str, Any]]:
    tasks_dir = workflow_async_tasks_dir(wfid)
    if not tasks_dir.exists():
        return []
    tasks: list[dict[str, Any]] = []
    for path in sorted(tasks_dir.glob("*.json")):
        data = read_json(path, {}) or {}
        if isinstance(data, dict):
            tasks.append(data)
    return tasks

def workflow_async_cancellation(wfid: str) -> dict[str, Any]:
    default = {"requested": False, "reason": None, "requestedAt": None, "graceSeconds": 10, "killAfterGrace": True}
    data = read_json(workflow_async_cancellation_path(wfid), {}) or {}
    if isinstance(data, dict):
        default.update(data)
    return default

def workflow_process_alive(pid: int | None) -> bool:
    return processes.pid_alive(pid)

def safe_signal_pid(pid: int | None, sig: int = signal.SIGTERM) -> bool:
    return processes.signal_pid_group(pid, sig)

def read_receipt(result_path: Path) -> tuple[dict[str, Any], str | None]:
    """Read a worker receipt tolerating the half-written artifact of a crash.

    Row recover-raises-on-partial-receipt (EXP-21 shipped, exp21-4 finding): a
    worker that dies mid-write leaves an EXISTING-but-unparseable receipt, and
    `read_json`'s default only covers the MISSING file -- so every settlement/
    recovery seam that read a receipt died on the most likely artifact of the
    very crash it was handling. Returns (doc, None) on success and ({}, reason)
    when the file exists but cannot be parsed; callers turn the reason into a
    validation error, never an exception. Deliberately receipt-seam-local:
    `read_json`'s contract (246 call sites) is untouched, so corruption in any
    OTHER state file still fails loud.
    """
    try:
        return (read_json(result_path, {}) or {}, None)
    except ValueError as exc:  # json.JSONDecodeError included: the crash's own artifact
        return ({}, f"unparseable worker result JSON (partial write?): {exc}")

def workflow_async_runnable_workers(base: Path, workflow: dict[str, Any], *, only_missing: bool = True) -> list[dict[str, Any]]:
    limits = workflow.get("limits", {}) or {}
    runnable: list[dict[str, Any]] = []
    for worker in workflow.get("workers", []):
        result_path = base / str(worker.get("resultPath", ""))
        if only_missing and result_path.exists():
            doc, parse_error = read_receipt(result_path)
            # a garbage receipt is an INVALID result: the worker stays runnable,
            # exactly the semantics a parseable-but-invalid receipt already had.
            if parse_error is None and not validate_worker_result(
                doc,
                expected_workflow_id=str(workflow.get("workflowId")),
                expected_worker_id=worker.get("workerId"),
                max_chars=int(limits.get("maxWorkerOutputChars", 4000)),
                expected_scope_paths=worker_scope_paths(base, worker),
                result_path=result_path,
            ):
                continue
        if worker.get("status") in {"done", "partial", "blocked", "skipped"} and only_missing:
            continue
        runnable.append(worker)
    return runnable

def workflow_effective_await_policy(workflow: dict[str, Any], *, mode: str | None = None, timeout: int | None = None, min_success: int | None = None) -> dict[str, Any]:
    profile_cfg = workflow_profile_config(workflow.get("workflowProfile")) if workflow.get("workflowProfile") else {}
    policy: dict[str, Any] = {
        "mode": "all-settled",
        "failFast": False,
        "cancelRestOnRace": False,
        "cancelRestOnFirstSuccess": False,
        "minSuccess": 1,
        "groupTimeoutSeconds": 1800,
        "workerTimeoutSeconds": int((workflow.get("limits", {}) or {}).get("timeoutSeconds", workflow_limits()["timeoutSeconds"])),
        "allowPartialReduce": True,
    }
    for source in [workflow_await_config(), profile_cfg.get("awaitPolicy", {}), workflow.get("awaitPolicy", {})]:
        if isinstance(source, dict):
            policy.update(source)
    if mode:
        policy["mode"] = mode
    if timeout is not None:
        policy["groupTimeoutSeconds"] = int(timeout)
    if min_success is not None:
        policy["minSuccess"] = int(min_success)
    if str(policy.get("mode")) not in AWAIT_MODES:
        raise HarnessError(f"invalid await mode {policy.get('mode')!r}; expected one of {sorted(AWAIT_MODES)}")
    policy["minSuccess"] = max(1, int(policy.get("minSuccess", 1)))
    policy["groupTimeoutSeconds"] = max(1, int(policy.get("groupTimeoutSeconds", 1800)))
    policy["workerTimeoutSeconds"] = max(1, int(policy.get("workerTimeoutSeconds", workflow_limits()["timeoutSeconds"])))
    return policy

def workflow_async_scheduler_policy(executor: str, concurrency: int) -> dict[str, Any]:
    cfg = workflow_async_config()
    policy = {
        "queueMaxSize": int(cfg.get("queueMaxSize", 32)) if str(cfg.get("queueMaxSize", "")).strip() else 32,
        "maxInFlight": int(cfg.get("maxInFlight", max(1, concurrency))) if str(cfg.get("maxInFlight", "")).strip() else max(1, concurrency),
        "perExecutorConcurrency": cfg.get("perExecutorConcurrency", {}) if isinstance(cfg.get("perExecutorConcurrency", {}), dict) else {},
        "rateLimitBackoffSeconds": int(cfg.get("rateLimitBackoffSeconds", workflow_runtime_limits().get("backoffSeconds", 30))),
        "circuitBreaker": cfg.get("circuitBreaker", {}) if isinstance(cfg.get("circuitBreaker", {}), dict) else {},
    }
    per_executor = policy["perExecutorConcurrency"].get(executor)
    if per_executor is not None:
        try:
            concurrency = min(concurrency, max(1, int(per_executor)))
        except Exception:
            pass
    policy["effectiveConcurrency"] = max(1, min(int(policy["maxInFlight"]), concurrency))
    policy["queueMaxSize"] = max(1, int(policy["queueMaxSize"]))
    return policy

def workflow_executor_circuit_path(executor: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", executor)
    return HARNESS / "runtime" / f"executor-circuit-{safe}.json"

def workflow_executor_circuit_state(executor: str) -> dict[str, Any]:
    cfg = workflow_async_scheduler_policy(executor, 1).get("circuitBreaker", {})
    enabled = bool(cfg.get("enabled", False))
    threshold = max(1, int(cfg.get("failureThreshold", 3))) if cfg else 3
    cooldown = max(1, int(cfg.get("cooldownSeconds", 300))) if cfg else 300
    state = read_json(workflow_executor_circuit_path(executor), {}) or {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("executor", executor)
    state.setdefault("enabled", enabled)
    state.setdefault("state", "healthy")
    state.setdefault("failureCount", 0)
    state.setdefault("failureThreshold", threshold)
    state.setdefault("cooldownSeconds", cooldown)
    opened_at = parse_iso_datetime(str(state.get("openedAt", "")))
    if enabled and state.get("state") == "open" and opened_at is not None:
        age = (dt.datetime.now(opened_at.tzinfo or dt.timezone.utc) - opened_at).total_seconds()
        if age >= cooldown:
            state["state"] = "cooldown"
            state["cooldownAt"] = now()
            write_json(workflow_executor_circuit_path(executor), state)
    return state

def workflow_executor_circuit_assert_usable(executor: str) -> None:
    state = workflow_executor_circuit_state(executor)
    if state.get("enabled") and state.get("state") == "open":
        raise HarnessError(f"Executor circuit breaker for {executor!r} is open; wait for cooldown or reset .harness/runtime/executor-circuit-{executor}.json")

def workflow_executor_record_success(executor: str) -> None:
    state = workflow_executor_circuit_state(executor)
    if not state.get("enabled"):
        return
    state.update({"state": "healthy", "failureCount": 0, "lastSuccessAt": now()})
    write_json(workflow_executor_circuit_path(executor), state)

def workflow_executor_record_failure(executor: str, reason: str) -> None:
    state = workflow_executor_circuit_state(executor)
    if not state.get("enabled"):
        return
    state["failureCount"] = int(state.get("failureCount", 0)) + 1
    state["lastFailureAt"] = now()
    state["lastFailureReason"] = reason
    if int(state["failureCount"]) >= int(state.get("failureThreshold", 3)):
        state["state"] = "open"
        state["openedAt"] = now()
    elif state.get("state") == "healthy":
        state["state"] = "degraded"
    write_json(workflow_executor_circuit_path(executor), state)


def workflow_next_failover(root: Path, role: str, executor: str | None, target: str | None,
                           failover_history: list[dict[str, Any]], is_usable: Any = None) -> dict[str, Any] | None:
    """SPEC-115 mid-workflow failover decision: the next untried fallback entry
    for a rate-limited worker, or None. Pure — the routing file is the only input.

    Resolve against the *origin* executor so the fallback chain is stable across
    hops; ``failover_history`` (already-tried ``{executor, card, effort}`` entries)
    is what advances the walk, bounding it to the chain length. Under the
    canonical profile fallbacks are ``[]``, so this returns None and the caller
    finalizes blocked byte-identically to pre-SPEC-115. ``is_usable(executor)``
    lets the caller skip an open-circuit executor to the following entry without
    this helper reading circuit state itself.
    """
    res = model_routing.resolve_role(root, role, executor=executor, target=target)
    tried = {(e.get("executor"), e.get("card"), e.get("effort"))
             for e in (failover_history or []) if isinstance(e, dict)}
    for entry in res.get("fallbacks") or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("executor"), entry.get("card"), entry.get("effort")) in tried:
            continue
        if is_usable is not None and not is_usable(entry.get("executor")):
            continue
        return entry
    return None

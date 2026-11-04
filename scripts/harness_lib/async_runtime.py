"""Async workflow runtime helpers for the Universal Agent Harness.

This module owns detached worker task persistence, await policies, supervisor
lifecycle, cancellation, async metrics, and executor circuit-breaker state.
The CLI router binds project-specific callbacks at import time so this module
can stay reusable while preserving the existing JSON/CLI contracts.
"""
from __future__ import annotations

from harness_lib import result_contracts
from harness_lib import workflow_spawn

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_lib import context_digest, epoch, processes, route_tuple, sandbox_spawn, spawn_guard, stream_json
from harness_lib.common import HarnessError, ROOT, now, parse_iso_datetime, read_json, read_text, truncate_text, write_json, write_text
from harness_lib.async_state import (  # MF split: pure state/persistence domain lives there
    workflow_async_config, workflow_await_config, workflow_async_dir, workflow_async_tasks_dir,
    workflow_async_group_path, workflow_async_events_path, workflow_async_cancellation_path, workflow_async_supervisor_pid_path,
    workflow_async_await_result_path, workflow_async_task_path, append_async_event, read_async_group,
    write_async_group, read_async_task, write_async_task, list_async_tasks,
    workflow_async_cancellation, workflow_process_alive, safe_signal_pid, workflow_async_runnable_workers, read_receipt,
    workflow_effective_await_policy, workflow_async_scheduler_policy, workflow_executor_circuit_path, workflow_executor_circuit_state,
    workflow_executor_circuit_assert_usable, workflow_executor_record_success, workflow_executor_record_failure,
    workflow_next_failover, tail_lines,
)

# Bound by scripts/harness.py after it has defined workflow callbacks and
# project constants. Keeping the dependency surface explicit prevents this
# module from importing the router and creating a circular import.
_BOUND_NAMES: set[str] = set()


def bind(env: dict[str, Any]) -> None:
    """Bind router callbacks used by the async runtime.

    Shared dependency-free helpers come straight from ``harness_lib.common``;
    only true router callbacks/state accessors are bound here.
    """
    allowed = {
        "ASYNC_TASK_TERMINAL_STATES", "AWAIT_MODES", "HARNESS_ROUTER_SCRIPT",
        "active_executor", "async_group_id", "build_worker_spawn_env", "ensure_executor_runnable", "executor_concurrency_limit", "extract_worker_result",
        "load_workflow", "rate_limit_detected", "worker_harness_vars", "worker_target_env",
        "release_workflow_lock", "run_one_worker", "set_worker_status", "validate_worker_result", "bound_soft_fields",
        "validate_write_workflow_ready", "worker_event", "worker_scope_paths", "workflow_budget_config",
        "workflow_config", "workflow_dir", "workflow_event", "workflow_limits", "workflow_lock_active",
        "workflow_lock_path", "workflow_profile_config", "workflow_runtime_limits", "workflow_spawn_command_for_prompt",
        "workflow_state_put", "workflow_state_put_artifact", "workflow_update",
    }
    missing = sorted(name for name in allowed if name not in env)
    if missing:
        raise RuntimeError("async_runtime bind missing dependencies: " + ", ".join(missing))
    globals().update({name: env[name] for name in allowed})
    _BOUND_NAMES.update(allowed)


EXPORTED_FUNCTIONS = [
    "workflow_async_config", "workflow_await_config", "workflow_async_dir", "workflow_async_tasks_dir",
    "workflow_async_group_path", "workflow_async_events_path", "workflow_async_cancellation_path",
    "workflow_async_supervisor_pid_path", "workflow_async_await_result_path", "workflow_async_task_path",
    "append_async_event", "read_async_group", "write_async_group", "read_async_task", "write_async_task",
    "list_async_tasks", "workflow_async_cancellation", "workflow_process_alive", "safe_signal_pid",
    "workflow_async_runnable_workers", "workflow_effective_await_policy", "workflow_async_scheduler_policy",
    "workflow_executor_circuit_path", "workflow_executor_circuit_state", "workflow_executor_circuit_assert_usable",
    "workflow_executor_record_success", "workflow_executor_record_failure", "workflow_async_metrics",
    "workflow_async_status_counts", "workflow_async_collect", "workflow_await_condition",
    "workflow_async_update_group_settlement", "workflow_async_recover", "workflow_async_run_one_worker",
    "workflow_async_supervisor_main", "workflow_async_supervisor", "workflow_start", "workflow_await",
    "workflow_cancel", "workflow_async_status", "workflow_doctor", "workflow_async_events", "tail_lines", "workflow_tail",
]


def _tail_log_path(base: Path, task_path: Any, default_rel: str) -> Path | None:
    """Resolve a worker log path (task JSON's stored rel path, else run-logs/),
    refusing anything that resolves outside ROOT. Read-only; never creates."""
    p = Path(str(task_path)) if task_path else (base / default_rel)
    if not p.is_absolute():
        p = ROOT / p
    try:
        p.resolve().relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    return p


def workflow_tail(wfid: str, worker_id: str | None = None, lines: int = 50) -> dict[str, Any]:
    """SPEC-118 read-only tail: last ``lines`` of stdout/stderr per worker.

    ``load_workflow`` validates the WF for free (raises ``HarnessError`` on an
    unknown one); an unknown worker id likewise raises. Never touches lock/state/
    events — a pure reader safe to call while the supervisor runs.
    """
    base, wf = load_workflow(wfid)
    workers = list(wf.get("workers", []) or [])
    if worker_id is not None:
        workers = [w for w in workers if str(w.get("workerId")) == str(worker_id)]
        if not workers:
            raise HarnessError(f"worker not found in {wfid}: {worker_id}")
    tasks = {str(t.get("workerId")): t for t in list_async_tasks(wfid)}
    out: list[dict[str, Any]] = []
    for w in workers:
        wid = str(w.get("workerId"))
        task = tasks.get(wid, {})
        so = _tail_log_path(base, task.get("stdoutPath"), f"run-logs/{wid}.stdout.log")
        se = _tail_log_path(base, task.get("stderrPath"), f"run-logs/{wid}.stderr.log")
        out.append({
            "workerId": wid,
            "status": str(w.get("status") or ""),
            "stdoutPath": str(so.relative_to(ROOT)) if so else None,
            "stderrPath": str(se.relative_to(ROOT)) if se else None,
            "stdout": tail_lines(so, lines),
            "stderr": tail_lines(se, lines),
        })
    return {"workflowId": wfid, "workers": out}

def workflow_async_metrics(wfid: str, group: dict[str, Any] | None = None, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    group = group or read_async_group(wfid)
    tasks = tasks or list_async_tasks(wfid)
    settled = [t for t in tasks if t.get("status") in ASYNC_TASK_TERMINAL_STATES]
    fulfilled = [t for t in tasks if t.get("status") == "fulfilled"]
    rejected = [t for t in tasks if t.get("status") == "rejected"]
    timed_out = [t for t in tasks if t.get("status") == "timeout"]
    cancelled = [t for t in tasks if t.get("status") == "cancelled"]
    starts = [parse_iso_datetime(str(t.get("startedAt", ""))) for t in settled]
    ends = [parse_iso_datetime(str(t.get("settledAt", ""))) for t in settled]
    starts = [x for x in starts if x]
    ends = [x for x in ends if x]
    total_duration = 0.0
    if starts and ends:
        total_duration = max(0.001, (max(ends) - min(starts)).total_seconds())
    durations: list[float] = []
    wait_times: list[float] = []
    for task in settled:
        st = parse_iso_datetime(str(task.get("startedAt", "")))
        en = parse_iso_datetime(str(task.get("settledAt", "")))
        cr = parse_iso_datetime(str(task.get("createdAt", "")))
        if st and en:
            durations.append(max(0.0, (en - st).total_seconds()))
        if cr and st:
            wait_times.append(max(0.0, (st - cr).total_seconds()))
    concurrency = max(1, int(group.get("concurrency", 1) or 1))
    return {
        "tasksTotal": len(tasks),
        "tasksSettled": len(settled),
        "fulfilled": len(fulfilled),
        "rejected": len(rejected),
        "timeout": len(timed_out),
        "cancelled": len(cancelled),
        "promiseResolutionRate": round(len(fulfilled) / len(tasks), 4) if tasks else 0,
        "partialCompletionRate": round(len(settled) / len(tasks), 4) if tasks else 0,
        "timeoutRate": round(len(timed_out) / len(tasks), 4) if tasks else 0,
        "cancelRate": round(len(cancelled) / len(tasks), 4) if tasks else 0,
        "throughput": round(len(settled) / total_duration, 4) if total_duration else 0,
        "responseTimeSeconds": round(total_duration, 3) if total_duration else 0,
        "averageWorkerDurationSeconds": round(sum(durations) / len(durations), 3) if durations else 0,
        "queueWaitTimeSeconds": round(sum(wait_times) / len(wait_times), 3) if wait_times else 0,
        "concurrencyEfficiency": round(min(1.0, sum(durations) / (concurrency * total_duration)), 4) if total_duration and durations else 0,
    }

def workflow_async_status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts

def workflow_async_collect(wfid: str, *, recover: bool = False) -> dict[str, Any]:
    if recover:
        workflow_async_recover(wfid)
    group = read_async_group(wfid)
    tasks = list_async_tasks(wfid)
    base, workflow = load_workflow(wfid)
    limits = workflow.get("limits", {}) or {}
    fulfilled: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    timed_out: list[dict[str, Any]] = []
    cancelled: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    invalid_results: list[dict[str, Any]] = []
    for task in tasks:
        status = str(task.get("status", "unknown"))
        item = {"asyncTaskId": task.get("asyncTaskId"), "workerId": task.get("workerId"), "status": status, "resultPath": task.get("resultPath"), "error": task.get("error")}
        result_path = base / str(task.get("resultPath", ""))
        if status == "fulfilled":
            if result_path.exists():
                data = read_json(result_path, {}) or {}
                errors = validate_worker_result(data, expected_workflow_id=wfid, expected_worker_id=task.get("workerId"), max_chars=int(limits.get("maxWorkerOutputChars", 4000)), result_path=result_path)
                if data.get("degraded") is True:
                    item["degraded"] = True
                if errors:
                    item["validationErrors"] = errors
                    invalid_results.append(item)
            fulfilled.append(item)
        elif status == "rejected":
            rejected.append(item)
        elif status == "timeout":
            timed_out.append(item)
        elif status == "cancelled":
            cancelled.append(item)
        elif status == "orphaned":
            orphaned.append(item)
        else:
            pending.append(item)
    metrics = workflow_async_metrics(wfid, group, tasks)
    # G3b: re-hash the wave's shared digest sources (SPEC-119). Warn-only — a drifted
    # source means the digest may have steered workers stale; never blocks the reduce.
    digest_drift = context_digest.check_digest_drift(ROOT, base)
    result = {
        "schemaVersion": "1.0",
        "workflowId": wfid,
        "asyncGroupId": group.get("asyncGroupId"),
        "mode": group.get("mode"),
        "status": group.get("status", "unknown"),
        "counts": workflow_async_status_counts(tasks),
        "fulfilled": fulfilled,
        "rejected": rejected,
        "timeout": timed_out,
        "cancelled": cancelled,
        "orphaned": orphaned,
        "pending": pending,
        "invalidResults": invalid_results,
        "workersDegraded": sum(1 for item in fulfilled if item.get("degraded") is True),
        "digestDrift": digest_drift,
        "canReduce": bool(fulfilled) and not pending,
        "allowPartialReduce": bool((group.get("settlementPolicy", {}) if isinstance(group.get("settlementPolicy", {}), dict) else {}).get("allowPartialReduce", True)),
        "metrics": metrics,
        "collectedAt": now(),
    }
    write_json(workflow_async_dir(wfid) / "collect.result.json", result)
    return result

def workflow_await_condition(tasks: list[dict[str, Any]], policy: dict[str, Any]) -> tuple[bool, str]:
    total = len(tasks)
    if total == 0:
        return True, "no tasks"
    terminal = [t for t in tasks if t.get("status") in ASYNC_TASK_TERMINAL_STATES]
    fulfilled = [t for t in tasks if t.get("status") == "fulfilled"]
    rejected_like = [t for t in tasks if t.get("status") in {"rejected", "timeout", "cancelled", "orphaned"}]
    pending_count = total - len(terminal)
    mode = str(policy.get("mode", "all-settled"))
    if mode == "all":
        if rejected_like:
            return True, "all fail-fast rejected because at least one task failed"
        return len(fulfilled) == total, "all fulfilled" if len(fulfilled) == total else "waiting for all tasks"
    if mode == "all-settled":
        return pending_count == 0, "all settled" if pending_count == 0 else "waiting for all tasks to settle"
    if mode == "race":
        return bool(terminal), "first task settled" if terminal else "waiting for first settled task"
    if mode == "first-success":
        if fulfilled:
            return True, "first successful task fulfilled"
        if pending_count == 0:
            return True, "all tasks settled without success"
        return False, "waiting for first success"
    if mode == "quorum":
        min_success = int(policy.get("minSuccess", 1))
        if len(fulfilled) >= min_success:
            return True, f"quorum reached: {len(fulfilled)}/{min_success}"
        if len(fulfilled) + pending_count < min_success:
            return True, f"quorum impossible: {len(fulfilled)}/{min_success}"
        return False, f"waiting for quorum: {len(fulfilled)}/{min_success}"
    return False, f"unknown mode {mode}"

def workflow_async_update_group_settlement(wfid: str) -> dict[str, Any]:
    group = read_async_group(wfid)
    tasks = list_async_tasks(wfid)
    policy = group.get("settlementPolicy", {}) if isinstance(group.get("settlementPolicy", {}), dict) else {}
    satisfied, reason = workflow_await_condition(tasks, policy)
    counts = workflow_async_status_counts(tasks)
    group["counts"] = counts
    group["metrics"] = workflow_async_metrics(wfid, group, tasks)
    if satisfied:
        statuses = {str(t.get("status")) for t in tasks}
        if statuses and statuses <= {"fulfilled"}:
            group["status"] = "settled"
        elif statuses and statuses <= {"cancelled"}:
            group["status"] = "cancelled"
        elif "timeout" in statuses and not any(t.get("status") == "fulfilled" for t in tasks):
            group["status"] = "timeout"
        elif any(t.get("status") == "fulfilled" for t in tasks):
            group["status"] = "partial"
        else:
            group["status"] = "failed"
        group["settledAt"] = group.get("settledAt") or now()
        group["settlementReason"] = reason
        append_async_event(wfid, "async_group_settled", {"asyncGroupId": group.get("asyncGroupId"), "status": group.get("status"), "reason": reason, "counts": counts})
    else:
        group["status"] = "running"
        group["settlementReason"] = reason
    write_async_group(wfid, group)
    return group

def workflow_async_recover(wfid: str) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    tasks = list_async_tasks(wfid)
    group_snapshot = read_async_group(wfid)
    supervisor_pid = group_snapshot.get("supervisorPid") or (
        int(read_text(workflow_async_supervisor_pid_path(wfid), "0").strip() or 0)
        if workflow_async_supervisor_pid_path(wfid).exists()
        else None
    )
    supervisor_alive = workflow_process_alive(int(supervisor_pid) if supervisor_pid else None)
    recovered: list[dict[str, Any]] = []
    limits = workflow.get("limits", {}) or {}
    for task in tasks:
        status = str(task.get("status", "unknown"))
        if status not in {"running", "scheduled", "pending"}:
            continue
        pid = task.get("pid")
        result_path = base / str(task.get("resultPath", ""))
        if result_path.exists():
            doc, parse_error = read_receipt(result_path)
            errors = [parse_error] if parse_error else validate_worker_result(doc, expected_workflow_id=wfid, expected_worker_id=task.get("workerId"), max_chars=int(limits.get("maxWorkerOutputChars", 4000)), result_path=result_path)
            if not errors:
                task.update({"status": "fulfilled", "promiseState": "fulfilled", "settledAt": now(), "recoveredAt": now(), "recoveryReason": "valid result file found"})
                write_async_task(wfid, task)
                recovered.append({"asyncTaskId": task.get("asyncTaskId"), "status": task.get("status")})
                continue
            if parse_error and not supervisor_alive and not workflow_process_alive(int(pid) if pid else None):
                # Row recover-raises-on-partial-receipt (EXP-21 shipped, exp21-4):
                # the crash's own artifact settles as REJECTED with the parse
                # failure as validationErrors -- never an exception, and only when
                # nobody (worker or supervisor) could still be writing the file.
                # A parseable-but-invalid receipt keeps its existing fall-through.
                task.update({"status": "rejected", "promiseState": "rejected", "settledAt": now(), "recoveredAt": now(),
                             "validationErrors": [parse_error], "error": parse_error,
                             "recoveryReason": "unparseable receipt (partial write) and no live process"})
                write_async_task(wfid, task)
                recovered.append({"asyncTaskId": task.get("asyncTaskId"), "status": task.get("status")})
                continue
        if status == "running" and not workflow_process_alive(int(pid) if pid else None):
            # If the detached supervisor is still alive, do not race its own
            # timeout/cancellation settlement path.  A worker can disappear
            # briefly between SIGTERM/SIGKILL and the supervisor writing the
            # terminal task state; marking it orphaned here loses the more
            # precise timeout/cancel reason and made async fixtures flaky.
            if supervisor_alive:
                continue
            task.update({"status": "orphaned", "promiseState": "rejected", "settledAt": now(), "recoveredAt": now(), "error": "registered process is no longer alive and no valid result was found"})
            write_async_task(wfid, task)
            recovered.append({"asyncTaskId": task.get("asyncTaskId"), "status": task.get("status")})
    group = workflow_async_update_group_settlement(wfid) if tasks else read_async_group(wfid)
    append_async_event(wfid, "async_recovery_completed", {"recovered": recovered, "status": group.get("status")})
    return {"workflowId": wfid, "recovered": recovered, "group": group}

def _record_executor_outcome(executor_name: str, task_status: str, worker_status: str, error: str | None) -> None:
    """CE.1 breaker seam: record the worker's REAL outcome, not just task_status.

    Genuine success = the task ``fulfilled`` AND the WORKER_RESULT's own status is not
    a failure ({"failed","error"}). Everything else records a breaker failure — rejected
    (validation errors, missing-result-at-rc0, rate-limit, nonzero rc) plus timeout and
    cancelled — so a persistently-failing executor actually trips instead of silently
    re-running failed waves at full price. The SPEC-115 blocked/rate_limit failover path
    records here exactly once, then re-spawns on the next card (no double-count).
    """
    if task_status == "fulfilled" and worker_status not in {"failed", "error"}:
        workflow_executor_record_success(executor_name)
    else:
        workflow_executor_record_failure(executor_name, error or "async worker failure")

async def workflow_async_run_one_worker(wfid: str, async_task_id: str, executor_name: str, timeout: int,
                                        state_lock: asyncio.Lock, throttles: dict[str, asyncio.Semaphore] | None = None) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    task = read_async_task(wfid, async_task_id)
    worker_id = str(task.get("workerId"))
    workers = {str(w.get("workerId")): w for w in workflow.get("workers", [])}
    worker = workers.get(worker_id)
    if not worker:
        task.update({"status": "rejected", "promiseState": "rejected", "settledAt": now(), "error": "worker not found"})
        write_async_task(wfid, task)
        return task
    role = str(worker.get("taskProfile", "scan"))
    cancel = workflow_async_cancellation(wfid)
    if cancel.get("requested"):
        task.update({"status": "cancelled", "promiseState": "rejected", "settledAt": now(), "error": cancel.get("reason") or "cancelled before start"})
        write_async_task(wfid, task)
        async with state_lock:
            base2, wf2 = load_workflow(wfid)
            set_worker_status(wf2, worker_id, "cancelled", force=True, run={"cancelled": True, "reason": task.get("error")})
            workflow_update(base2, wf2, force=True)
        append_async_event(wfid, "async_task_cancelled", {"asyncTaskId": async_task_id, "workerId": worker_id, "reason": task.get("error")})
        return task
    cmd = (task.get("command") if isinstance(task.get("command"), list)
           else workflow_spawn_command_for_prompt(worker["promptPath"], role, executor_name,
                                                  write_allowed=bool(worker.get("writeAllowed"))))
    # The worker's SEAT is persisted beside its command (prep + every failover), so
    # a fallback hop's env names the fallback card, not the origin. A pre-seat task
    # (no persisted sessionEnv) derives it from the current executor, no override.
    session_env = (task.get("sessionEnv") if isinstance(task.get("sessionEnv"), dict)
                   else workflow_spawn.session_env(role, executor_name))
    logs_dir = base / "run-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = Path(str(task.get("stdoutPath") or logs_dir / f"{worker_id}.stdout.log"))
    stderr_path = Path(str(task.get("stderrPath") or logs_dir / f"{worker_id}.stderr.log"))
    if not stdout_path.is_absolute():
        stdout_path = ROOT / stdout_path
    if not stderr_path.is_absolute():
        stderr_path = ROOT / stderr_path
    workspace = worker.get("workspacePath")
    cwd = (ROOT / str(workspace)).resolve() if workspace else ROOT
    # SPEC-119 v5 / E3-C6a: same least-privilege filter as the blocking spawn path.
    harness_vars = worker_harness_vars(wfid, base, worker, worker_id, cwd)
    env = build_worker_spawn_env(executor_name, {**harness_vars, **session_env}, extra=worker_target_env(base))
    # SPEC-148 rule 1: same sandbox chokepoint as the blocking path; the async
    # primitive stays here (baselined), the confinement decisions live there.
    # SPEC-151: thread the break-glass (workerSandbox=false) + the task's egress
    # declaration; a refusal (declared egress with no enforceable egress) settles
    # the task rejected legibly instead of leaving it un-settled (invariant 4/6).
    hop = _workflow_async_spawn_and_settle(
        wfid, async_task_id, executor_name, timeout, state_lock, base, workflow, task,
        worker, cmd, stdout_path, stderr_path, cwd, env, sandbox_spawn.sandbox_prepare)
    throttle = throttles.get(executor_name) if throttles else None
    if throttle is None:
        outcome = await hop
    else:
        async with throttle:
            outcome = await hop
    # OD-3: the current-hop semaphore is released before failover recursion, so a
    # worker never holds two executor semaphores and cannot form a hold-and-wait cycle.
    if isinstance(outcome, str):
        return await workflow_async_run_one_worker(wfid, async_task_id, outcome, timeout, state_lock, throttles)
    return outcome


async def _workflow_async_spawn_and_settle(
    wfid: str, async_task_id: str, executor_name: str, timeout: int, state_lock: asyncio.Lock,
    base: Path, workflow: dict[str, Any], task: dict[str, Any], worker: dict[str, Any],
    cmd: list[Any], stdout_path: Path, stderr_path: Path, cwd: Path, env: dict[str, str], sandbox_prepare: Any,
) -> dict[str, Any] | str:
    worker_id = str(task.get("workerId"))
    role = str(worker.get("taskProfile", "scan"))
    # SPEC-115 failover resolves against the ORIGIN executor so the fallback chain
    # stays stable while executor_name changes across hops; failover_history advances the walk.
    origin_executor = str(task.get("executor") or executor_name)
    failover_target = workflow.get("target")
    failover_history = list(task.get("failoverHistory") or [])
    try:
        sandbox_receipt = sandbox_prepare(
            vendor=executor_name, write_allowed=bool(worker.get("writeAllowed")),
            cwd=cwd, env=env, root=ROOT, declares_egress=bool(worker.get("declaresEgress")),
            sandbox_enabled=(workflow_config() or {}).get("workerSandbox", True) is not False,
            mode="async")  # SPEC-163 v2 M4: this seam spawns directly, so it declares its own reach
    except sandbox_spawn.SandboxRefused as exc:
        task.update({"status": "rejected", "promiseState": "rejected", "settledAt": now(), "error": f"sandbox refused: {exc}"})
        write_async_task(wfid, task)
        async with state_lock:
            base2, wf2 = load_workflow(wfid)
            set_worker_status(wf2, worker_id, "failed", force=True, run={"error": str(exc)})
            workflow_update(base2, wf2, force=True)
        append_async_event(wfid, "async_task_sandbox_refused", {"asyncTaskId": async_task_id, "workerId": worker_id, "reason": str(exc)})
        return task
    log_budget = workflow_budget_config().get("logBudget", {}) if isinstance(workflow_budget_config().get("logBudget", {}), dict) else {}
    max_stdout = int(log_budget.get("maxStdoutChars", 20000))
    max_stderr = int(log_budget.get("maxStderrChars", 20000))
    started = now()
    # process_group_kwargs() also adds CREATE_NO_WINDOW on nt so this worker, spawned
    # from the console-less supervisor, gets a hidden console instead of a black window
    # (stdout/stderr are redirected to the run-log files below).
    subprocess_kwargs: dict[str, Any] = processes.process_group_kwargs()
    if os.name == "nt":
        # SPEC-163 Phase 2a (H1): born suspended so the Job Object is assigned + capped
        # BEFORE the worker (or any grandchild) runs — the same assign-race close as the
        # bounded seam (run_process_tree_bounded). Resumed by assign_job_to_pid below.
        subprocess_kwargs["creationflags"] = subprocess_kwargs.get("creationflags", 0) | 0x00000004  # CREATE_SUSPENDED
    # SPEC-118 live logs (root-cause fix): the child writes stdout/stderr STRAIGHT
    # into the run-log files (opened "wb" pre-spawn, inherited as the child's
    # stdio), so `workflow tail` and the panel drill-in can read them WHILE the
    # worker runs. Pre-SPEC-118 this was PIPE + communicate(), buffered in memory
    # until settle — no file existed to tail. Same proven handle-inheritance
    # pattern as supervisor.stdout.log; a failover hop re-opens "wb" (overwrite).
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    async_job = None  # SPEC-163 Phase 2a: the worker's Job Object handle (nt), closed in finally
    out_fh = stdout_path.open("wb")
    err_fh = stderr_path.open("wb")
    try:
        proc = await asyncio.create_subprocess_exec(*[str(x) for x in cmd], cwd=str(cwd), env=env, stdout=out_fh, stderr=err_fh, **subprocess_kwargs)
        # SPEC-163 Phase 2a (H1, invariant 6): cage the async fan-out in a Job Object with the
        # SAME fork-bomb/memory cap the bounded seam gets, then resume the suspended worker.
        # FAIL-CLOSED — any assign/resume failure kills the still-suspended child and settles
        # the worker failed; never a resumed, uncapped run (a procApplied:true that lied would
        # invert Phase 1's honesty). nt-only; POSIX has no Job Object and no CREATE_SUSPENDED,
        # so procApplied stays the honest default False with no behavior change.
        if os.name == "nt":
            try:
                async_job = processes.assign_job_to_pid(proc.pid, sandbox_spawn.DEFAULT_JOB_LIMITS)
                sandbox_receipt["procApplied"] = True
            except Exception as exc:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
                reason = f"sandbox job-assign failed: {exc}"
                task.update({"status": "rejected", "promiseState": "rejected", "pid": proc.pid, "settledAt": now(), "error": reason})
                write_async_task(wfid, task)
                async with state_lock:
                    base2, wf2 = load_workflow(wfid)
                    set_worker_status(wf2, worker_id, "failed", force=True, run={"error": reason})
                    workflow_update(base2, wf2, force=True)
                append_async_event(wfid, "async_task_settled", {"asyncTaskId": async_task_id, "workerId": worker_id, "status": "rejected", "workerStatus": "failed", "error": reason})
                return task
        task.update({"status": "running", "promiseState": "pending", "pid": proc.pid, "startedAt": started})
        write_async_task(wfid, task)
        async with state_lock:
            base2, wf2 = load_workflow(wfid)
            workers2 = {str(w.get("workerId")): w for w in wf2.get("workers", [])}
            worker2 = workers2.get(worker_id, worker)
            set_worker_status(wf2, worker_id, "running", force=True, attempts=int(worker2.get("attempts", 0)) + 1)
            workflow_update(base2, wf2, status="running", force=True)
        append_async_event(wfid, "async_task_started", {"asyncTaskId": async_task_id, "workerId": worker_id, "pid": proc.pid, "executor": executor_name, "sandboxTier": sandbox_receipt["tier"], "sandboxManifest": sandbox_spawn.manifest(sandbox_receipt)})
        worker_event(wfid, worker_id, "workflow_worker_started", {"executor": executor_name, "asyncTaskId": async_task_id,
                                                                  # L9 mirror: same C13 tuple as the blocking twin
                                                                  "tuple": route_tuple.route_tuple(
                                                                      executor=executor_name, task_profile=role,
                                                                      topology="workflow-worker",
                                                                      write_allowed=bool(worker.get("writeAllowed")), root=ROOT)})
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            # Prefer a graceful group termination, then force-kill. Bound each wait
            # so a stubborn descendant holding the inherited log handle cannot wedge
            # the async supervisor or the aggregate workflow gate.
            safe_signal_pid(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                safe_signal_pid(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass  # give up reaping; read back what was flushed to the files
    finally:
        out_fh.close()
        err_fh.close()
        # SPEC-163 v4 invariant 11, as widened by v5: a worker killed by TIMEOUT or by an
        # OPERATOR CANCEL is swept from its CAGE before the handle is released. The v4
        # sentence read "`timed_out` ONLY ... never on cancellation, whose policy is the
        # still-open `proc-cancel-graded-destruction` row"; that row is now decided and the
        # sentence is superseded, not merely outgrown.
        # EXP-21 probe P1 (owner-ratified, verdict `shipped`) measured
        # the gap on this seam: per timed-out worker, codex left 3,3,3 and claude 5,5,5 live
        # GRANDCHILDREN, against 0,0,0 on the bounded control, zero within-cell variance.
        # THIS coroutine holds the worker's job handle -- the job is unnamed, so nothing
        # else could reach it, and nothing else has to: `await proc.wait()` above hands
        # control back the instant the worker dies, with `async_job` still open. One
        # TerminateJobObject, no pid enumeration, no cross-process reach. It also covers
        # the give-up wedge at the end of the ladder: a worker that survives SIGTERM and
        # SIGKILL now dies WITH its tree instead of leaking it. Sweep BEFORE the close --
        # closing the last handle destroys the job and would make this a silent no-op.
        # SPEC-163 v5 widens the TRIGGER, never the reach: a timeout OR an operator cancel.
        # An operator cancel is the same destruction request a timeout is, so it gets the
        # same blast radius; on POSIX it has had it all along (`killpg` takes the worker's
        # whole group, and `start_new_session` children escape it), so v5 is nt catching up
        # to the behaviour the owner already lives with, not new policy.
        # The three terms that are NOT the trigger, and why:
        #   - normal exit: a breakaway-denied caged gate runner must survive its parent's
        #     clean exit (invariant 7, recover-sweep frontier). Hence the nonzero-exit term.
        #   - internal settlement: the await-policy `cancelRest*` writer marks `requested`
        #     WITHOUT `source`, so an internal settlement can never fire this.
        #   - a bystander: `cancel --worker-id X` must not sweep worker Y's cage.
        # The marker is written BEFORE any signal is sent, so by the time `proc.wait()`
        # returns the read below cannot be stale. Known divergence from the settle
        # classifier: that branch also spares a nonzero-exit worker whose stdout still
        # yields an extractable result, but extraction runs AFTER this `finally` and the
        # cage is swept here anyway -- deliberate, the operator said stop and a salvaged
        # receipt does not un-say it. Keep the CALL BELOW on ONE line and do not repeat it
        # in prose: hsb-13 pins it as a contiguous source string, so a line break unpins the
        # premise silently (how esh-3 was lost) and a copy in a comment would keep that
        # premise green after the call itself was deleted.
        op_cancelled = False
        if async_job and not timed_out:  # `async_job` implies `proc` exists (spawn/assign failures return early)
            _cancel = workflow_async_cancellation(wfid)
            op_cancelled = (bool(_cancel.get("requested"))
                            and _cancel.get("source") == "operator"
                            and _cancel.get("workerId") in (None, worker_id)
                            and proc.returncode not in (0, None))
        cage_trigger = "timeout" if timed_out else ("operator-cancel" if op_cancelled else None)
        if cage_trigger and async_job:
            processes.terminate_win_job(async_job)
        processes.close_win_handle(async_job)  # SPEC-163 Phase 2a: release the job handle (no-op off nt / None)
        if cage_trigger and async_job:  # observability only, AFTER the close so a write failure cannot leak the handle
            append_async_event(wfid, "async_cage_swept", {"asyncTaskId": async_task_id, "workerId": worker_id, "trigger": cage_trigger})
    if timed_out:  # marker in the file (mode "a"), as the string carried it before
        with stderr_path.open("a", encoding="utf-8") as f:
            f.write(f"\nTIMEOUT after {timeout}s\n")
    # Read-back from the closed files feeds the same names the PIPE produced, so
    # settle semantics (result extraction, rate-limit, returnCode) are untouched.
    stdout_raw = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_raw = stderr_path.read_text(encoding="utf-8", errors="replace")
    stdout, stdout_truncated = truncate_text(stdout_raw, max_stdout)
    stderr, stderr_truncated = truncate_text(stderr_raw, max_stderr)
    # SPEC-118 v3: parse the RAW (untruncated) NDJSON once — it stays the single
    # source; we derive only a compact summary (finalText/usage/activity).
    parsed = stream_json.parse(stdout_raw)
    if stdout_truncated:  # only rewrite when the post-run cap actually clipped it
        # token-economy (d): head-truncation drops the tail `result` event; append
        # it back after the marker so the load-bearing event survives for drawers.
        if parsed["isStreamJson"] and parsed["finalResultLine"]:
            stdout_path.write_text(stdout + parsed["finalResultLine"] + "\n", encoding="utf-8")
        else:
            stdout_path.write_text(stdout, encoding="utf-8")
    if stderr_truncated:
        stderr_path.write_text(stderr, encoding="utf-8")
    result_path = base / str(worker.get("resultPath"))
    extracted = False
    if not result_path.exists():
        data = extract_worker_result(parsed["finalText"] if parsed["isStreamJson"] else stdout_raw)
        if data:
            write_json(result_path, data)
            extracted = True
            worker_event(wfid, worker_id, "workflow_worker_result_extracted", {"resultPath": str(result_path.relative_to(ROOT)), "asyncTaskId": async_task_id})
    run_payload = {
        "workerId": worker_id,
        "returnCode": proc.returncode,
        "timeout": timed_out,
        "startedAt": started,
        "finishedAt": now(),
        "stdout": str(stdout_path.relative_to(ROOT)),
        "stderr": str(stderr_path.relative_to(ROOT)),
        "stdoutTruncated": stdout_truncated,
        "stderrTruncated": stderr_truncated,
        "stdoutBytes": len(stdout_raw.encode("utf-8")),  # M1: measurement rung only — record, never mask
        "stderrBytes": len(stderr_raw.encode("utf-8")),
        # stream-json: scan finalText + targeted error events, not the raw
        # envelope (benign rate_limit_event "rateLimitType" false-positives "ratelimit").
        "rateLimitDetected": rate_limit_detected(parsed["finalText"] + "\n" + parsed["errorText"], stderr) if parsed["isStreamJson"] else rate_limit_detected(stdout, stderr),
        "resultExtractedFromStdout": extracted,
        "asyncTaskId": async_task_id,
    }
    if parsed["isStreamJson"]:  # compact derived telemetry rides the run payload + task JSON
        if parsed["usage"]:
            run_payload["observedUsage"] = parsed["usage"]
        run_payload["activity"] = parsed["activity"]
    limits = workflow.get("limits", {}) or {}
    worker_status = "failed"
    task_status = "rejected"
    promise_state = "rejected"
    error: str | None = None
    cancellation = workflow_async_cancellation(wfid)
    if cancellation.get("requested") and proc.returncode not in {0, None} and not result_path.exists():
        worker_status = "cancelled"
        task_status = "cancelled"
        error = cancellation.get("reason") or "cancelled"
    elif timed_out:
        worker_status = "failed"
        task_status = "timeout"
        error = f"timeout after {timeout}s"
    elif result_path.exists():
        # Row recover-raises-on-partial-receipt: a half-written receipt used to
        # raise HERE first, killing the supervisor mid-settlement and leaving the
        # task `running` forever; the parse failure is a validation error of the
        # receipt's shape, classified through the existing rejected branch.
        data, parse_error = read_receipt(result_path)
        # M2 / row wr-schema-discards-work: soft fields are bounded INSIDE
        # validate_worker_result now (the ingest seam), so every call site gets it --
        # this one used to be the only one that did. Persist the bounded result and
        # surface the degradation on the run payload; validate_worker_result records
        # the same list on data["softWarnings"], which is what reaches the reducer.
        errors = [parse_error] if parse_error else validate_worker_result(data, expected_workflow_id=wfid, expected_worker_id=worker_id, max_chars=int(limits.get("maxWorkerOutputChars", 4000)), expected_scope_paths=worker_scope_paths(base, worker), repo_access=result_contracts.executor_repo_access(ROOT, executor_name), result_path=result_path)
        soft = data.get("softWarnings") or []
        if soft:
            write_json(result_path, data)
            run_payload["softWarnings"] = list(soft)
        run_payload["validationErrors"] = errors
        if errors:
            worker_status = "failed"
            task_status = "rejected"
            error = "; ".join(errors[:5])
        else:
            worker_status = str(data.get("status", "done"))
            task_status = "fulfilled"
            promise_state = "fulfilled"
    elif run_payload.get("rateLimitDetected") and workflow_runtime_limits().get("stopOnRateLimit", True):
        worker_status = "blocked"
        task_status = "rejected"
        error = "rate_limit_or_runtime_limit"
        run_payload["blockedReason"] = error
    elif proc.returncode == 0:
        worker_status = "missing"
        task_status = "rejected"
        error = "worker exited 0 but did not produce WORKER_RESULT"
    else:
        worker_status = "failed"
        task_status = "rejected"
        error = f"worker exited with return code {proc.returncode}"
    _record_executor_outcome(executor_name, task_status, worker_status, error)
    # SPEC-115 mid-workflow failover: a blocked rate_limit_or_runtime_limit worker
    # re-spawns on the next fallback card instead of finalizing, bounded by the
    # chain (failover_history). Canonical => no fallbacks => this is skipped and
    # the finalize below is byte-identical to pre-SPEC-115.
    if worker_status == "blocked" and error == "rate_limit_or_runtime_limit" and not cancellation.get("requested"):
        def _circuit_ok(ex: str) -> bool:
            st = workflow_executor_circuit_state(ex)
            return not (st.get("enabled") and st.get("state") == "open")
        inv1_refused: list[str] = []
        def _failover_ok(ex: str) -> bool:
            if not _circuit_ok(ex):
                return False
            conflicts = workflow_spawn.seed_isolation_conflicts(worker, str(ex), workflow)
            if conflicts:
                inv1_refused.append(f"{ex} produced seed source {', '.join(conflicts)}")
                return False
            return True
        nxt = workflow_next_failover(ROOT, role, origin_executor, failover_target, failover_history, is_usable=_failover_ok)
        if not nxt and inv1_refused:
            error = f"seed-isolation failover refused: {'; '.join(inv1_refused)} - INV-1 research isolation"
            run_payload["blockedReason"] = error
            append_async_event(wfid, "worker_failover_refused", {"asyncTaskId": async_task_id, "workerId": worker_id, "reason": error})
        if nxt:
            try:
                _override = {"model": nxt.get("card"), "effort": nxt.get("effort")}
                new_cmd = workflow_spawn_command_for_prompt(worker["promptPath"], role, str(nxt.get("executor")),
                                                            spawn_override=_override,
                                                            write_allowed=bool(worker.get("writeAllowed")))
                # atomic with the command: the fallback seat rides the same override,
                # so cmd and env can never describe different hops (the exact split
                # that made a fallback lane present the primary's seat).
                new_session_env = workflow_spawn.session_env(role, str(nxt.get("executor")), _override)
            except HarnessError:
                new_cmd = None  # misconfigured chain entry: fall through to blocked, don't crash the supervisor
            if new_cmd:
                failover_history = failover_history + [{"executor": nxt.get("executor"), "card": nxt.get("card"), "effort": nxt.get("effort")}]
                task.update({"command": new_cmd, "sessionEnv": new_session_env, "failoverHistory": failover_history})
                write_async_task(wfid, task)
                append_async_event(wfid, "worker_failover", {"asyncTaskId": async_task_id, "workerId": worker_id,
                                    "from": executor_name, "to": nxt.get("executor"), "card": nxt.get("card"), "reason": error})
                return str(nxt.get("executor"))
    if failover_history:
        run_payload["failoverHistory"] = failover_history
    task.update({
        "status": task_status,
        "promiseState": promise_state,
        "pid": proc.pid,
        "settledAt": now(),
        "returnCode": proc.returncode,
        "stdoutPath": str(stdout_path.relative_to(ROOT)),
        "stderrPath": str(stderr_path.relative_to(ROOT)),
        "resultPath": str(result_path.relative_to(base)),
        "error": error,
        "run": run_payload,
    })
    write_async_task(wfid, task)
    async with state_lock:
        base2, wf2 = load_workflow(wfid)
        # async failover: re-stamp the FINAL seat so reduce (workflow_reduce.py:147)
        # judges the fallback's access class, not the primary's. None on the
        # canonical path -> dropped by set_worker_status, byte-identical pre-fix.
        set_worker_status(wf2, worker_id, worker_status, force=True, run=run_payload,
                          resolvedExecutor=(executor_name if failover_history else None))
        workflow_update(base2, wf2, force=True)
    append_async_event(wfid, "async_task_settled", {"asyncTaskId": async_task_id, "workerId": worker_id, "status": task_status, "workerStatus": worker_status, "error": error})
    worker_event(wfid, worker_id, "workflow_worker_finished", {"returnCode": proc.returncode, "asyncTaskId": async_task_id})
    return task

async def workflow_async_supervisor_main(wfid: str, executor_name: str, concurrency: int, timeout: int) -> dict[str, Any]:
    state_lock = asyncio.Lock()
    group = read_async_group(wfid)
    if not group:
        raise HarnessError(f"async group missing for workflow {wfid}")
    group.update({"status": "running", "startedAt": group.get("startedAt") or now(), "supervisorPid": os.getpid(), "heartbeatAt": now()})
    write_async_group(wfid, group)
    write_text(workflow_async_supervisor_pid_path(wfid), str(os.getpid()) + "\n")
    append_async_event(wfid, "async_group_started", {"asyncGroupId": group.get("asyncGroupId"), "supervisorPid": os.getpid(), "concurrency": concurrency})
    tasks = [t for t in list_async_tasks(wfid) if t.get("status") in {"pending", "scheduled"}]
    worker_executors = {str(t.get("workerId")): str(t.get("executor") or executor_name) for t in tasks}
    executor_policies = {ex: workflow_async_scheduler_policy(ex, concurrency) for ex in dict.fromkeys(worker_executors.values())}
    per_executor_cfg = {ex: policy.get("perExecutorConcurrency", {}).get(ex) for ex, policy in executor_policies.items()}
    executor_caps = workflow_spawn.async_executor_caps(worker_executors, executor_concurrency_limit, per_executor_cfg, concurrency)
    throttles = {ex: asyncio.Semaphore(cap) for ex, cap in executor_caps.items()}
    queue_size = max(1, int(group.get("queueMaxSize", workflow_async_scheduler_policy(executor_name, concurrency).get("queueMaxSize", 32))))
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=queue_size)
    results: list[dict[str, Any]] = []

    async def producer() -> None:
        for task in tasks:
            if workflow_async_cancellation(wfid).get("requested"):
                current = read_async_task(wfid, str(task.get("asyncTaskId")))
                current.update({"status": "cancelled", "promiseState": "rejected", "settledAt": now(), "error": workflow_async_cancellation(wfid).get("reason") or "cancelled before scheduling"})
                write_async_task(wfid, current)
                continue
            await queue.put(task)
            append_async_event(wfid, "async_task_scheduled", {"asyncTaskId": task.get("asyncTaskId"), "workerId": task.get("workerId")})
        for _ in range(max(1, concurrency)):
            await queue.put(None)

    async def consumer() -> None:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                res = await workflow_async_run_one_worker(wfid, str(item.get("asyncTaskId")), str(item.get("executor") or executor_name), timeout, state_lock, throttles)
                results.append(res)
                group2 = workflow_async_update_group_settlement(wfid)
                if group2.get("mode") in {"race", "first-success", "quorum"}:
                    policy = group2.get("settlementPolicy", {}) if isinstance(group2.get("settlementPolicy", {}), dict) else {}
                    satisfied, _ = workflow_await_condition(list_async_tasks(wfid), policy)
                    mode = str(group2.get("mode"))
                    cancel_rest = (
                        (mode == "race" and bool(policy.get("cancelRestOnRace")))
                        or (mode == "first-success" and bool(policy.get("cancelRestOnFirstSuccess")))
                        or (mode == "quorum" and bool(policy.get("cancelRestOnQuorum")))
                    )
                    if satisfied and cancel_rest:
                        cancel_path = workflow_async_cancellation_path(wfid)
                        if not (read_json(cancel_path, {}) or {}).get("requested"):
                            write_json(cancel_path, {"requested": True, "reason": f"await policy {group2.get('mode')} satisfied", "requestedAt": now(), "graceSeconds": 5, "killAfterGrace": True})
            finally:
                queue.task_done()

    consumers = [asyncio.create_task(consumer()) for _ in range(max(1, concurrency))]
    producer_task = asyncio.create_task(producer())
    await producer_task
    await queue.join()
    for c in consumers:
        if not c.done():
            c.cancel()
    await asyncio.gather(*consumers, return_exceptions=True)
    group = workflow_async_update_group_settlement(wfid)
    base, workflow = load_workflow(wfid)
    workflow["phase"] = "awaiting_reduce"
    workflow_update(base, workflow, force=True)
    append_async_event(wfid, "async_supervisor_completed", {"asyncGroupId": group.get("asyncGroupId"), "status": group.get("status"), "tasks": len(results)})
    return {"workflowId": wfid, "group": group, "tasksRun": len(results)}

def workflow_async_supervisor(wfid: str, executor_name: str, concurrency: int, timeout: int) -> dict[str, Any]:
    try:
        return asyncio.run(workflow_async_supervisor_main(wfid, executor_name, concurrency, timeout))
    finally:
        release_workflow_lock(wfid)

def workflow_start(
    wfid: str,
    executor_name: str | None = None,
    concurrency: int | None = None,
    timeout: int | None = None,
    *,
    mode: str | None = None,
    min_success: int | None = None,
    only_missing: bool = True,
    dry_run: bool = False,
    allow_placeholder: bool = False,
    force_round: bool = False,
    force_lock: bool = False,
    allow_frontier: bool = False,
    card_resolver: Any = None,
    executor_resolver: Any = None,
    spawn_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    executor = active_executor(executor_name)
    if not dry_run and (executor_name is not None or executor_resolver is None):
        ensure_executor_runnable(executor, allow_placeholder=allow_placeholder)
        workflow_executor_circuit_assert_usable(executor)
    limits = workflow.get("limits", {}) or {}
    max_concurrency = int(concurrency or limits.get("concurrency", workflow_limits()["concurrency"]))
    scheduler = workflow_async_scheduler_policy(executor, max_concurrency)
    if executor_resolver is None:
        per_executor_limit = executor_concurrency_limit(executor)
        if per_executor_limit is not None and max_concurrency > per_executor_limit:
            workflow_event(wfid, "workflow_concurrency_capped", {"executor": executor, "requested": max_concurrency, "limit": per_executor_limit})
            max_concurrency = per_executor_limit
        scheduler = workflow_async_scheduler_policy(executor, max_concurrency)
        max_concurrency = int(scheduler.get("effectiveConcurrency", max_concurrency))
    else:
        max_concurrency = max(1, min(max_concurrency, int(scheduler.get("maxInFlight", max_concurrency))))
        scheduler["effectiveConcurrency"] = max_concurrency
    policy = workflow_effective_await_policy(workflow, mode=mode, timeout=timeout, min_success=min_success)
    timeout_s = int(timeout or policy.get("workerTimeoutSeconds") or limits.get("timeoutSeconds", workflow_limits()["timeoutSeconds"]))
    if workflow.get("policy", {}).get("writeAllowed"):
        validate_write_workflow_ready(workflow)
        if not workflow.get("policy", {}).get("parallelWritesAllowed") and max_concurrency > 1:
            workflow_event(wfid, "workflow_write_concurrency_capped", {"requested": max_concurrency, "limit": 1})
            max_concurrency = 1
    existing_group = read_async_group(wfid)
    if existing_group.get("status") == "running" and workflow_lock_active(workflow_lock_path(wfid)) and not force_lock:
        raise HarnessError(f"Workflow {wfid} already has a running async group; use workflow async-status/await/cancel")
    runnable = workflow_async_runnable_workers(base, workflow, only_missing=only_missing)
    if executor_resolver is None and any(isinstance(w.get("spawn"), dict) for w in runnable):
        raise HarnessError(
            "workflow start requires executor_resolver when runnable workers carry branch.spawn; "
            "the None-resolver path supports spawn-less workflows only")
    if executor_resolver is None:
        worker_execs = {w["workerId"]: executor for w in runnable}
        worker_pins = None
        executor_caps = {}
    else:
        _worker_res = {w["workerId"]: executor_resolver(w, executor_name) for w in runnable}
        worker_execs = {wid: er[0] for wid, er in _worker_res.items()}
        worker_pins = {wid: er[1] for wid, er in _worker_res.items()}
        executor_policies = {ex: workflow_async_scheduler_policy(ex, max_concurrency) for ex in dict.fromkeys(worker_execs.values())}
        per_executor_cfg = {ex: cfg.get("perExecutorConcurrency", {}).get(ex) for ex, cfg in executor_policies.items()}
        executor_caps = workflow_spawn.async_executor_caps(worker_execs, executor_concurrency_limit, per_executor_cfg, max_concurrency)
        for w_exec, cap in executor_caps.items():
            if cap < max_concurrency:
                workflow_event(wfid, "workflow_concurrency_capped", {"executor": w_exec, "requested": max_concurrency, "limit": cap})
        if not dry_run:
            for w_exec in dict.fromkeys(worker_execs.values()):
                ensure_executor_runnable(w_exec, allow_placeholder=allow_placeholder)
                workflow_executor_circuit_assert_usable(w_exec)
    for worker in runnable:
        resolved = worker_execs[worker["workerId"]]
        conflicts = workflow_spawn.seed_isolation_conflicts(worker, resolved, workflow)
        if conflicts:
            raise HarnessError(f"seed-isolation: {worker['workerId']} executor {resolved} produced seed source {', '.join(conflicts)} - INV-1 research isolation")
    worker_overrides = workflow_spawn.worker_spawn_overrides(runnable, spawn_override)
    if dry_run:
        result = {"workflowId": wfid, "executor": executor, "mode": policy.get("mode"), "concurrency": max_concurrency, "timeoutSeconds": timeout_s, "workersToStart": [w.get("workerId") for w in runnable], "scheduler": scheduler, "dryRun": True}
        if executor_resolver is not None:
            result.update({"workerExecutors": worker_execs, "executorCaps": executor_caps})
        return result
    if not runnable:
        return {"workflowId": wfid, "executor": executor, "workersStarted": 0, "message": "No runnable workers"}
    # DD-mechanization P0a (SPEC-119 v13): a profile/override minSuccess above the runnable
    # group can never reach quorum (3 of 2 hangs a healthy quorum-mode run). Clamp to THIS
    # round's runnable count -- but ONLY on the round-scoped async group settlementPolicy
    # (below), never on the shared `policy` object. Persisting the clamp onto the workflow's
    # awaitPolicy/settlementPolicy would make it sticky across rounds: a later, larger round
    # would merge the shrunk value back in (workflow_effective_await_policy merges
    # workflow["awaitPolicy"] last) and never heal to the profile's intent. workflow_await
    # reads this round's clamp back from the group settlementPolicy. An explicit
    # `workflow await --min-success N` stays an honored operator override.
    round_min_success = max(1, min(int(policy.get("minSuccess", 1)), len(runnable)))
    # SPEC-153 v3 spawn-economy TEETH (async funnel): the SAME vendor-independent gate
    # the blocking `workflow run` carries, at the async choke point -- BEFORE any async
    # task is written or spawned. card_resolver (executor_profile_spawn) is injected by
    # cmd_workflow_start to keep this module leaf; a None resolver (a future internal
    # caller) skips the gate, backward-safe. Ack is --allow-frontier or
    # HARNESS_ALLOW_FRONTIER=1 ONLY. warn -> event + proceed; block -> event + raise.
    if card_resolver is not None:
        guard_mode = spawn_guard.guard_mode(ROOT)
        if guard_mode != "off":
            kind = str(workflow.get("type") or "")
            ack = bool(allow_frontier) or os.environ.get("HARNESS_ALLOW_FRONTIER") == "1"
            verdict = spawn_guard.first_denied_worker(
                ROOT, kind, len(runnable), runnable, executor, card_resolver, ack,
                pins=worker_pins, spawn_override=spawn_override, worker_executors=worker_execs,
                branch_spawn_pins={w["workerId"]: (w.get("spawn") or {}) for w in runnable})
            if verdict["denied"]:
                event = {"reason": verdict["reason"], "workerId": verdict["workerId"],
                         "card": verdict["card"], "workers": len(runnable), "kind": kind}
                if guard_mode == "warn":
                    workflow_event(wfid, "workflow_spawn_economy_warned", event)
                    print(f"spawn-economy guard (warn): {verdict['reason']}", file=sys.stderr)
                else:
                    workflow_event(wfid, "workflow_spawn_economy_refused", event)
                    raise HarnessError(
                        "Spawn-economy guard refused `workflow start` (SPEC-153): "
                        f"{verdict['reason']} [executor={executor} profile={verdict['profile']} "
                        f"workers={len(runnable)} kind={kind}]. Zero async tasks were started.")
    current_round = int(workflow.get("round", 0))
    max_rounds = int(workflow.get("maxRounds", limits.get("maxRounds", workflow_limits()["maxRounds"])))
    if current_round >= max_rounds and not force_round:
        raise HarnessError(f"Workflow {wfid} reached maxRounds={max_rounds}; use --force-round only after human review")
    # svc-autostart-owners (P1): bring up this workflow target's declared
    # autoStart:["workflow"] services now -- after every abort/refusal check,
    # right before the first async task is written (mirrors the ui/chat
    # ensure_autostart form; the sink is a workflow event because this call's
    # stdout is the result JSON). NO atexit stop here: workflow_start RETURNS
    # immediately and the workers run in a SEPARATE detached supervisor process,
    # so an atexit on this ephemeral start process would stop the services before
    # the workers use them. Cleanup is the existing status() orphan-reap (owner
    # dies -> stopped-orphan); an explicit supervisor-owned stop is deferred (P2).
    if workflow.get("target") and not dry_run:
        from harness_lib import services as services_lib
        for row in services_lib.ensure_autostart(ROOT, "workflow", workflow.get("target")):
            workflow_event(wfid, "workflow_service_autostart",
                           {"name": row.get("name"), "status": row.get("status"),
                            "error": row.get("error")})
    async_dir = workflow_async_dir(wfid)
    tasks_dir = workflow_async_tasks_dir(wfid)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    cancellation = {"requested": False, "reason": None, "requestedAt": None, "graceSeconds": 10, "killAfterGrace": True}
    write_json(workflow_async_cancellation_path(wfid), cancellation)
    workflow_state_put_artifact(wfid, "async-cancellation", workflow_async_cancellation_path(wfid))
    group_id = async_group_id(wfid)
    task_ids: list[str] = []
    for idx, worker in enumerate(runnable, start=1):
        w_exec = worker_execs[worker["workerId"]]
        w_override = worker_overrides[worker["workerId"]]
        async_task_id = f"AT-{idx:03d}"
        task_ids.append(async_task_id)
        cmd = workflow_spawn_command_for_prompt(worker["promptPath"], worker.get("taskProfile", "scan"), w_exec,
                                                spawn_override=w_override, write_allowed=bool(worker.get("writeAllowed")))
        session_env = workflow_spawn.session_env(worker.get("taskProfile", "scan"), w_exec, w_override)
        task = {
            "schemaVersion": "1.0",
            "asyncTaskId": async_task_id,
            "asyncGroupId": group_id,
            "workflowId": wfid,
            "workerId": worker.get("workerId"),
            "executor": w_exec,
            "command": cmd,
            "sessionEnv": session_env,  # the worker's seat, persisted beside its command
            "commandDisplay": " ".join(shlex.quote(str(x)) for x in cmd),
            "pid": None,
            "status": "scheduled",
            "promiseState": "pending",
            "createdAt": now(),
            "startedAt": None,
            "settledAt": None,
            "timeoutSeconds": timeout_s,
            "resultPath": str(worker.get("resultPath")),
            "stdoutPath": str((base / "run-logs" / f"{worker.get('workerId')}.stdout.log").relative_to(ROOT)),
            "stderrPath": str((base / "run-logs" / f"{worker.get('workerId')}.stderr.log").relative_to(ROOT)),
            "error": None,
            "cancellation": cancellation,
        }
        write_async_task(wfid, task)
    group = {
        "schemaVersion": "1.0",
        "asyncGroupId": group_id,
        "workflowId": wfid,
        "mode": policy.get("mode"),
        "status": "pending",
        "tasks": task_ids,
        "concurrency": max_concurrency,
        "queueMaxSize": int(scheduler.get("queueMaxSize", 32)),
        "timeoutSeconds": int(policy.get("groupTimeoutSeconds", timeout_s)),
        "settlementPolicy": {**policy, "minSuccess": round_min_success},
        "scheduler": scheduler,
        "executor": executor,
        "createdAt": now(),
        "startedAt": None,
        "settledAt": None,
        "supervisorPid": None,
        "counts": workflow_async_status_counts(list_async_tasks(wfid)),
    }
    if executor_resolver is not None:
        group.update({"executors": sorted(set(worker_execs.values())), "executorCaps": executor_caps})
    write_async_group(wfid, group)
    for worker in runnable:
        w_exec = worker_execs[worker["workerId"]]
        set_worker_status(workflow, str(worker.get("workerId")), "queued", force=True, asyncGroupId=group_id, resolvedExecutor=w_exec)
        worker_event(wfid, str(worker.get("workerId")), "workflow_worker_queued", {"executor": w_exec, "asyncGroupId": group_id})
    workflow["round"] = current_round + 1
    round_history = {"round": workflow["round"], "startedAt": now(), "executor": executor, "workersPlanned": [w.get("workerId") for w in runnable], "asyncGroupId": group_id, "mode": policy.get("mode")}
    if executor_resolver is not None:
        round_history["workerExecutors"] = worker_execs
    workflow.setdefault("roundHistory", []).append(round_history)
    workflow["phase"] = "running_workers"
    workflow["asyncPolicy"] = {"enabled": True, "mode": "detached-supervisor", "stateDir": str(async_dir.relative_to(ROOT))}
    workflow["awaitPolicy"] = policy
    workflow["scheduler"] = scheduler
    workflow["cancellationPolicy"] = {"graceSeconds": cancellation["graceSeconds"], "killAfterGrace": cancellation["killAfterGrace"]}
    workflow["settlementPolicy"] = policy
    # SPEC-149 regra 2: born-with-epoch (inherit or acquire+publish) before the
    # first ownerEpoch-stamping update; the spawned supervisor inherits the env
    # pin, so its workflow_update calls stay the same owner (not a legacy writer).
    run_epoch, epoch_degraded = epoch.ensure_run_epoch(ROOT)
    workflow_event(wfid, "workflow_epoch_acquired", {"ownerEpoch": run_epoch, "degraded": epoch_degraded})
    workflow_update(base, workflow, status="running", force=True)
    supervisor_log = async_dir / "supervisor.stdout.log"
    supervisor_err = async_dir / "supervisor.stderr.log"
    cmd = [sys.executable, str(HARNESS_ROUTER_SCRIPT), "workflow", "async-supervisor", wfid, "--executor", executor, "--concurrency", str(max_concurrency), "--timeout", str(timeout_s)]
    # Hidden console (CREATE_NO_WINDOW via process_group_kwargs), NOT DETACHED_PROCESS:
    # the two are mutually exclusive, and a hidden console keeps taskkill /T tree-cleanup
    # working while still not popping a visible window. stdio already goes to the log
    # files below and the new process group provides parent-lifetime detachment.
    out_f = supervisor_log.open("a", encoding="utf-8")
    err_f = supervisor_err.open("a", encoding="utf-8")
    try:
        # esh-3: DEVNULL here protects the whole chain — workers inherit the
        # supervisor's stdin, and a stdin-reading executor CLI would block on it.
        # SECREV H1 Fase 2b ceiling (D044, option b): this detached supervisor is
        # deliberately NOT resource-capped by cap_process_best_effort. Option (b) holds the
        # Job Object handle for the SPAWNING parent's lifetime, but here that parent is the
        # ephemeral `workflow start` CLI, which exits right after this spawn — the cap would
        # be destroyed immediately (last handle closed, no KILL_ON_JOB_CLOSE), so it buys
        # nothing. Capping the supervisor waits for option (a) (a persistent owner holding
        # the handle, distant backlog) or the POSIX setrlimit parity row. No cap here.
        # Keep `stdin=subprocess.DEVNULL, stdout=out_f` on ONE line: esh-3 pins that
        # exact literal to prove this spawn closes the worker chain's stdin, and a
        # line break between the two arguments silently unpins it.
        proc = processes.popen_detached(cmd, stdin=subprocess.DEVNULL, stdout=out_f,
                                        stderr=err_f, cwd=str(ROOT))
    finally:
        out_f.close()
        err_f.close()
    write_text(workflow_async_supervisor_pid_path(wfid), str(proc.pid) + "\n")
    group["supervisorPid"] = proc.pid
    group["status"] = "running"
    group["startedAt"] = group.get("startedAt") or now()
    group["supervisorCommand"] = " ".join(shlex.quote(str(x)) for x in cmd)
    write_async_group(wfid, group)
    write_json(workflow_lock_path(wfid), {"pid": proc.pid, "startedAt": now(), "command": "workflow start/async-supervisor", "cwd": str(ROOT), "asyncGroupId": group_id})
    append_async_event(wfid, "async_group_launched", {"asyncGroupId": group_id, "supervisorPid": proc.pid, "tasks": len(task_ids), "mode": policy.get("mode")})
    return {
        "workflowId": wfid,
        "asyncGroupId": group_id,
        "executor": executor,
        "supervisorPid": proc.pid,
        "tasksStarted": len(task_ids),
        "concurrency": max_concurrency,
        "mode": policy.get("mode"),
        "stateDir": str(async_dir.relative_to(ROOT)),
        "nextCommands": [
            f"python scripts/harness.py workflow async-status {wfid}",
            f"python scripts/harness.py workflow await {wfid} --mode {policy.get('mode')}",
            f"python scripts/harness.py workflow watch {wfid} --follow",
            f"python scripts/harness.py workflow collect {wfid}",
        ],
    }

def workflow_await(wfid: str, *, mode: str | None = None, timeout: int | None = None, min_success: int | None = None, poll_interval: float = 1.0, cancel_on_timeout: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    group = read_async_group(wfid)
    if not group:
        raise HarnessError(f"Workflow {wfid} has no async group. Run workflow start first.")
    # DD-mechanization P0a: seed minSuccess from THIS round's group settlementPolicy (the
    # round-scoped clamp), not the workflow's cross-round awaitPolicy default. An explicit
    # --min-success still overrides.
    policy = workflow_effective_await_policy(
        workflow, mode=mode or group.get("mode"), timeout=timeout,
        min_success=min_success if min_success is not None else (group.get("settlementPolicy") or {}).get("minSuccess"))
    deadline = time.time() + int(policy.get("groupTimeoutSeconds", 1800))
    reason = ""
    timed_out = False
    while True:
        workflow_async_recover(wfid)
        tasks = list_async_tasks(wfid)
        satisfied, reason = workflow_await_condition(tasks, policy)
        if satisfied:
            break
        if time.time() >= deadline:
            timed_out = True
            reason = "await timeout"
            if cancel_on_timeout:
                workflow_cancel(wfid, reason="await timeout", grace_seconds=5)
            break
        time.sleep(max(0.1, float(poll_interval)))
    group = workflow_async_update_group_settlement(wfid)
    collect = workflow_async_collect(wfid)
    result = {
        "schemaVersion": "1.0",
        "workflowId": wfid,
        "asyncGroupId": group.get("asyncGroupId"),
        "mode": policy.get("mode"),
        "status": "timeout" if timed_out else group.get("status"),
        "satisfied": not timed_out,
        "reason": reason,
        "counts": collect.get("counts", {}),
        "fulfilled": collect.get("fulfilled", []),
        "rejected": collect.get("rejected", []),
        "timeout": collect.get("timeout", []),
        "cancelled": collect.get("cancelled", []),
        "pending": collect.get("pending", []),
        "canReduce": collect.get("canReduce", False),
        "allowPartialReduce": collect.get("allowPartialReduce", True),
        "metrics": collect.get("metrics", {}),
        "awaitedAt": now(),
    }
    write_json(workflow_async_await_result_path(wfid), result)
    append_async_event(wfid, "await_policy_satisfied" if not timed_out else "await_policy_timeout", {"asyncGroupId": group.get("asyncGroupId"), "mode": policy.get("mode"), "reason": reason, "status": result.get("status")})
    return result

def workflow_cancel(wfid: str, *, worker_id: str | None = None, reason: str | None = None, grace_seconds: int = 10) -> dict[str, Any]:
    _base, _workflow = load_workflow(wfid)
    tasks = list_async_tasks(wfid)
    target_tasks = [t for t in tasks if not worker_id or t.get("workerId") == worker_id]
    # SPEC-163 v5: `source` stamps OPERATOR intent on the marker. It is the deterministic
    # seam the supervisor's cage sweep keys on: the internal settlement writer (the
    # await-policy `cancelRest*` arm) and the `workflow start` initializer do NOT carry it,
    # so an internal settlement can never trigger a destruction the operator did not ask
    # for. Every reader uses `.get`, so the key is additive and backward-safe.
    cancellation = {"requested": True, "source": "operator", "reason": reason or "operator requested cancellation", "requestedAt": now(), "graceSeconds": max(0, int(grace_seconds)), "killAfterGrace": True, "workerId": worker_id}
    write_json(workflow_async_cancellation_path(wfid), cancellation)
    workflow_state_put_artifact(wfid, "async-cancellation", workflow_async_cancellation_path(wfid))

    def terminate_pid(pid: int | None) -> dict[str, Any]:
        if not pid:
            return {"pid": pid, "signalled": False, "killedAfterGrace": False, "aliveAfterGrace": False}
        sent = safe_signal_pid(pid, signal.SIGTERM)
        deadline = time.time() + max(0, int(cancellation["graceSeconds"]))
        while sent and workflow_process_alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        killed = False
        if sent and workflow_process_alive(pid) and cancellation.get("killAfterGrace"):
            kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
            killed = safe_signal_pid(pid, kill_sig)
            deadline = time.time() + 2
            while killed and workflow_process_alive(pid) and time.time() < deadline:
                time.sleep(0.1)
        return {"pid": pid, "signalled": sent, "killedAfterGrace": killed, "aliveAfterGrace": workflow_process_alive(pid)}

    signalled: list[dict[str, Any]] = []
    for task in target_tasks:
        pid = int(task.get("pid")) if task.get("pid") else None
        if task.get("status") in ASYNC_TASK_TERMINAL_STATES:
            continue
        signal_result = terminate_pid(pid)
        task.update({
            "status": "cancelled",
            "promiseState": "rejected",
            "settledAt": now(),
            "error": cancellation["reason"],
            "cancellation": cancellation,
        })
        write_async_task(wfid, task)
        signalled.append({"asyncTaskId": task.get("asyncTaskId"), "workerId": task.get("workerId"), **signal_result})
    if worker_id:
        base, wf = load_workflow(wfid)
        for worker in wf.get("workers", []):
            if worker.get("workerId") == worker_id and worker.get("status") in {"pending", "queued", "running"}:
                set_worker_status(wf, worker_id, "cancelled", force=True, run={"cancelled": True, "reason": cancellation["reason"]})
        workflow_update(base, wf, force=True)
    group = workflow_async_update_group_settlement(wfid) if tasks else read_async_group(wfid)
    append_async_event(wfid, "async_task_cancel_requested" if worker_id else "async_group_cancel_requested", {"workerId": worker_id, "reason": cancellation["reason"], "signalled": signalled})
    return {"workflowId": wfid, "workerId": worker_id, "cancellation": cancellation, "signalled": signalled, "groupStatus": group.get("status")}

def workflow_async_status(wfid: str, *, recover: bool = False) -> dict[str, Any]:
    if recover:
        workflow_async_recover(wfid)
    group = read_async_group(wfid)
    tasks = list_async_tasks(wfid)
    lock = read_json(workflow_lock_path(wfid), {}) or {}
    supervisor_pid = group.get("supervisorPid") or (int(read_text(workflow_async_supervisor_pid_path(wfid), "0").strip() or 0) if workflow_async_supervisor_pid_path(wfid).exists() else None)
    return {
        "workflowId": wfid,
        "asyncGroup": group,
        "tasks": tasks,
        "counts": workflow_async_status_counts(tasks),
        "supervisor": {"pid": supervisor_pid, "alive": workflow_process_alive(int(supervisor_pid) if supervisor_pid else None)},
        "lock": lock,
        "cancellation": workflow_async_cancellation(wfid),
        "metrics": workflow_async_metrics(wfid, group, tasks),
    }

def workflow_doctor(wfid: str) -> dict[str, Any]:
    """SPEC-114 diagnose-only triage: ONE read-only verdict about a workflow's
    health plus the single recovery command to run — it NAMES the command as a
    string and NEVER executes it (doctor consolidates, never accretes).

    Composes ``workflow_async_status(recover=False)`` (no writes) with the
    would-orphan predicate READ straight out of ``workflow_async_recover``:
    a running task whose pid is dead while the supervisor is dead would be
    marked orphaned by a recover — doctor only counts it. Touches no
    workflow.json / async / lock / event state.
    """
    status = workflow_async_status(wfid, recover=False)
    group = status.get("asyncGroup") or {}
    tasks = status.get("tasks") or []
    counts = status.get("counts") or {}
    supervisor = status.get("supervisor") or {}
    supervisor_alive = bool(supervisor.get("alive"))
    lock = status.get("lock") or {}

    would_orphan = 0   # recover would mark orphaned: running + dead pid + dead supervisor
    stuck_pending = 0  # scheduled/pending with no supervisor to ever start them
    for task in tasks:
        st = str(task.get("status", ""))
        if st not in {"running", "scheduled", "pending"}:
            continue
        pid = task.get("pid")
        alive = workflow_process_alive(int(pid) if pid else None)
        if st == "running" and not alive and not supervisor_alive:
            would_orphan += 1
        elif st in {"scheduled", "pending"} and not supervisor_alive:
            stuck_pending += 1

    # wall-clock stall since the last heartbeat (fallback: since start)
    ref = group.get("heartbeatAt") or group.get("startedAt")
    ref_dt = parse_iso_datetime(str(ref)) if ref else None
    now_dt = parse_iso_datetime(now())
    stalled_seconds = round((now_dt - ref_dt).total_seconds(), 1) if ref_dt and now_dt else None

    # stale lock: a lock file whose recorded pid is dead, with no live supervisor
    lock_pid = lock.get("pid")
    lock_stale = bool(lock) and not workflow_process_alive(int(lock_pid) if lock_pid else None)

    # G3b: advisory only — a drifted shared digest is a health signal, but it has no
    # recovery *command* (the digest is non-authoritative), so it never enters the
    # single-command verdict below; doctor reports it and moves on.
    try:
        digest_drift = context_digest.check_digest_drift(ROOT, workflow_dir(wfid))
    except Exception:
        digest_drift = []

    base_cmd = "python scripts/harness.py workflow"
    # single highest-priority issue → one command (consolidate, don't list five)
    if would_orphan:
        verdict = f"supervisor dead, {would_orphan} running task(s) would orphan"
        suggested = f"{base_cmd} async-recover {wfid}"
    elif lock_stale and not supervisor_alive:
        verdict = "stale lock held by a dead pid, no live supervisor"
        suggested = f"{base_cmd} unlock {wfid} --stale-only"
    elif stuck_pending:
        verdict = f"{stuck_pending} task(s) stuck scheduled/pending with a dead supervisor"
        suggested = f"{base_cmd} resume {wfid}"
    else:
        verdict = "healthy"
        suggested = None

    return {
        "workflowId": wfid,
        "groupStatus": group.get("status"),
        "supervisor": {"pid": supervisor.get("pid"), "alive": supervisor_alive},
        "tasks": {"total": len(tasks), "running": int(counts.get("running", 0)),
                  "wouldOrphan": would_orphan, "stuckPending": stuck_pending, "counts": counts},
        "stalledSeconds": stalled_seconds,
        "digestDrift": digest_drift,
        "verdict": verdict,
        "suggestedCommand": suggested,
        "healthy": suggested is None,
    }

def workflow_async_events(wfid: str, *, limit: int = 100) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    path = workflow_async_events_path(wfid)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events[-limit:]

"""Synchronous fork-join workflow execution engine for Universal Agent Harness.

This module owns the ``workflow run`` engine: per-worker spawn-env assembly,
the SPEC-165 hop/fallback spawn chain walk (``run_one_worker``), executor
resolution, and the round orchestration loop that fans workers out over a
thread pool and folds their results back into the workflow record. It was
extracted from ``scripts/harness.py`` under the line-budget policy and lives
behind a harness_lib boundary like its ``workflow_plan``/``workflow_reduce``/
``workflow_spawn`` siblings.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from harness_lib import async_state, epoch, model_routing, route_tuple, sandbox_spawn, spawn_guard, stream_json, targets as targets_lib, workflow_spawn
from harness_lib.common import HarnessError, normalize, now, read_json, truncate_text, write_json
from harness_lib.processes import filter_spawn_env


def bind(env: dict) -> None:
    """Receive the harness.py router namespace (callbacks, state accessors,
    module-level constants) needed by the functions below. Mirrors
    ``gate_fixtures_workflow.bind``: the moved functions call many harness.py
    helpers (load_workflow, workflow_update, set_worker_status, and friends)
    that only exist once the router module has finished defining itself, so
    this runs late (after harness.py's own def block), unlike the early
    function-object aliasing harness.py does for EXPORTED_FUNCTIONS below."""
    globals().update(env)


def worker_harness_vars(wfid: str, base: Path, worker: dict[str, Any], worker_id: str, cwd: Path) -> dict[str, str]:
    """The HARNESS_* vars the runtime sets for a worker (shared by both spawn paths)."""
    return {
        "HARNESS_WORKFLOW_ID": wfid,
        "HARNESS_WORKER_ID": worker_id,
        # A worker obeys its packet, not the SessionStart canonical-context reinjection;
        # skip it so the worker (esp. the SPEC-144 cheap router) does not pay ~5K tokens
        # of overseer context. Honored by tools/hooks/reload_context_after_compact.py.
        "HARNESS_SKIP_REINJECT": "1",
        "HARNESS_WORKER_RESULT_PATH": str((base / str(worker.get("resultPath"))).resolve()),
        "HARNESS_WORKFLOW_WRITE_ALLOWED": "1" if worker.get("writeAllowed") else "0",
        "HARNESS_WORKER_PROMPT_PATH": str((ROOT / str(worker.get("promptPath"))).resolve()),
        "HARNESS_WORKER_WORKSPACE": str(cwd),
        "HARNESS_WORKER_LOCKED_PATHS": json.dumps(
            list(worker.get("lockedPaths", []) or [])
            + list(worker.get("createLockedPaths", []) or [])
            + list(worker.get("deleteLockedPaths", []) or [])
            + [p for item in (worker.get("renameLocks", []) or []) for p in [item.get("oldPath"), item.get("newPath")] if p]
        ),
    }


def worker_target_env(base: Path) -> dict[str, str]:
    """Target-scoped env (deny-by-default) + coordinates when the workflow has a target."""
    _base_wf = read_json(base / "workflow.json", {}) or {}
    if not _base_wf.get("target"):
        return {}
    # HT-T5: agents working on a target get its declared env (deny-by-default)
    # and the subject coordinates; missing target fails legibly via resolve().
    _t_entry = targets_lib.resolve(str(_base_wf["target"]))
    env = dict(targets_lib.spawn_env(_t_entry))
    env["HARNESS_TARGET_NAME"] = str(_base_wf["target"])
    env["HARNESS_TARGET_ROOT"] = str(targets_lib.target_root(_t_entry))
    return env


def build_worker_spawn_env(executor_name: str, harness_vars: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    """SPEC-119 v5 / E3-C6a: least-privilege spawn env for a workflow worker.

    OS base allowlist + this executor's ``envKeepList`` + the HARNESS_* vars the
    runtime sets (+ any target env). Every other parent-env entry (unrelated API
    keys, tokens, secrets) is DROPPED. Escape hatch to restore full inheritance:
    project.json ``workflows.workerEnvFilter = false``.
    """
    enabled = (workflow_config() or {}).get("workerEnvFilter", True) is not False
    keep = executor_config(executor_name).get("envKeepList", []) if executor_name else []
    return filter_spawn_env(os.environ, harness_vars, keep_list=keep, extra=extra, enabled=enabled)


def _hop_usable(executor_name: str) -> tuple[bool, str]:
    """SPEC-165 R7 pre-spawn hop gate: (usable, reason), deterministic, no spawn.
    1. executor exists and runnable is not False; 2. circuit -- unusable iff the
    breaker is ENABLED and open (mirrors async_runtime._circuit_ok: a disabled
    breaker never blocks); 3. http hop needs a keyless template OR the api-key env
    present (default OPENAI_API_KEY when --api-key-env is absent)."""
    try:
        executor = executor_config(executor_name)
    except HarnessError:
        return (False, f"unknown executor {executor_name!r}")
    if executor.get("runnable", True) is False:
        return (False, "executor not runnable")
    st = async_state.workflow_executor_circuit_state(executor_name)
    if st.get("enabled") and st.get("state") == "open":
        return (False, "circuit open")
    if executor.get("type") == "http":
        template = executor.get("commandTemplate", []) or []
        if "--keyless-hosts" in template:
            return (True, "ok")
        env_name = "OPENAI_API_KEY"
        if "--api-key-env" in template:
            idx = template.index("--api-key-env")
            if idx + 1 < len(template):
                env_name = str(template[idx + 1])
        if not os.environ.get(env_name):
            return (False, f"no api key ({env_name})")
    return (True, "ok")


def _classify_spawn_failure(executor_name: str, text: str) -> str | None:
    """SPEC-165 R8: 'rate-limit' | 'auth' | 'quota' | None from a lowercase
    substring match against the executor's runtimeLimits pattern lists."""
    try:
        limits = executor_config(executor_name).get("runtimeLimits") or {}
    except HarnessError:
        return None
    haystack = normalize(text or "")
    for key, label in (("rateLimitPatterns", "rate-limit"),
                       ("authFailurePatterns", "auth"),
                       ("quotaFailurePatterns", "quota")):
        for pat in limits.get(key) or []:
            if normalize(str(pat)) in haystack:
                return label
    return None


def run_one_worker(wfid: str, worker: dict[str, Any], executor_name: str, timeout: int, *,
                   fanout_width: int = 1, frontier_ack: bool = False,
                   workflow_kind: str | None = None, workflow: dict[str, Any], spawn_override: dict[str, Any] | None = None) -> dict[str, Any]:
    base = workflow_dir(wfid)
    worker_id = str(worker.get("workerId"))
    logs_dir = base / "run-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / f"{worker_id}.stdout.log"
    stderr_path = logs_dir / f"{worker_id}.stderr.log"
    started = now()
    workspace = worker.get("workspacePath")
    cwd = (ROOT / str(workspace)).resolve() if workspace else ROOT
    harness_vars = worker_harness_vars(wfid, base, worker, worker_id, cwd)
    log_budget = workflow_budget_config().get("logBudget", {}) if isinstance(workflow_budget_config().get("logBudget", {}), dict) else {}
    max_stdout = int(log_budget.get("maxStdoutChars", 20000))
    max_stderr = int(log_budget.get("maxStderrChars", 20000))
    task_profile = worker.get("taskProfile", "scan")

    def _attempt(hop_executor: str, spawn_override: dict[str, Any] | None) -> dict[str, Any]:
        cmd = workflow_spawn_command_for_prompt(worker["promptPath"], task_profile, hop_executor,
                                                spawn_override=spawn_override,
                                                write_allowed=bool(worker.get("writeAllowed")))
        # The worker's SEAT rides the same resolved spawn as its command (never the
        # parent's, never the routing pin). Computed per hop, so a fallback attempt
        # names the fallback card, not the failed primary. Merged into harness_vars
        # so the least-privilege filter carries it and overwrites any parent triplet.
        session_env = workflow_spawn.session_env(task_profile, hop_executor, spawn_override)
        env = build_worker_spawn_env(hop_executor, {**harness_vars, **session_env}, extra=worker_target_env(base))
        worker_event(wfid, worker_id, "workflow_worker_started", {
            "executor": hop_executor,  # rule 12: attribution follows the actual hop
            # C13/A2: versioned route pin -- additive/observational, comparability substrate.
            "tuple": route_tuple.route_tuple(
                executor=hop_executor, task_profile=task_profile,
                topology="workflow-worker", write_allowed=bool(worker.get("writeAllowed")), root=ROOT)})
        try:
            # SPEC-148 rule 1: worker spawns route through the sandbox chokepoint
            # (R0 = env scoping only; R1+ adds workspace-isolation + protected-path ACL).
            # SPEC-151: thread the break-glass (workerSandbox=false) + the task's egress
            # declaration so the manifest and invariant-4 refusal ride the same seam.
            proc = sandbox_spawn.sandbox_spawn(
                cmd, vendor=hop_executor, write_allowed=bool(worker.get("writeAllowed")),
                cwd=cwd, env=env, root=ROOT, mode="bounded", timeout=timeout,
                declares_egress=bool(worker.get("declaresEgress")),
                # workflow_config() is typed -> dict (never None); the `or {}` here was
                # a dead guard AND an equivalent-mutant trap for `oracle mutate` (the
                # swap-bool-op landed on it, unkillable). Dropped: byte-identical behavior.
                sandbox_enabled=workflow_config().get("workerSandbox", True) is not False,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout_raw = proc.stdout or ""
            stdout, stdout_truncated = truncate_text(stdout_raw, max_stdout)
            stderr, stderr_truncated = truncate_text(proc.stderr or "", max_stderr)
            parsed = stream_json.parse(stdout_raw)
            # SPEC-118 v3 / token-economy (d): when truncation clips the tail, the
            # load-bearing final `result` event lives at the END and is lost — append
            # it after the marker so post-mortem drawers still have it (one line).
            if stdout_truncated and parsed["isStreamJson"] and parsed["finalResultLine"]:
                stdout_path.write_text(stdout + parsed["finalResultLine"] + "\n", encoding="utf-8")
            else:
                stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            result_path = base / str(worker.get("resultPath"))
            extracted = False
            if not result_path.exists():
                data = extract_worker_result(parsed["finalText"] if parsed["isStreamJson"] else stdout_raw)
                if data:
                    write_json(result_path, data)
                    extracted = True
                    worker_event(wfid, worker_id, "workflow_worker_result_extracted", {"resultPath": str(result_path.relative_to(ROOT))})
            payload = {
                "workerId": worker_id,
                "returnCode": proc.returncode,
                "startedAt": started,
                "finishedAt": now(),
                "stdout": str(stdout_path.relative_to(ROOT)),
                "stderr": str(stderr_path.relative_to(ROOT)),
                "stdoutTruncated": stdout_truncated,
                "stderrTruncated": stderr_truncated,
                # stream-json: scan the model's finalText + targeted error events, NOT
                # the raw envelope (the benign rate_limit_event key "rateLimitType"
                # normalizes to a false-positive "ratelimit" needle).
                "rateLimitDetected": rate_limit_detected(parsed["finalText"] + "\n" + parsed["errorText"], stderr) if parsed["isStreamJson"] else rate_limit_detected(stdout, stderr),
                "resultExtractedFromStdout": extracted,
                "cpuSeconds": getattr(proc, "cpu_seconds", None),
            }
            if parsed["isStreamJson"]:  # compact derived telemetry — never a second event copy
                if parsed["usage"]:
                    payload["observedUsage"] = parsed["usage"]
                payload["activity"] = parsed["activity"]
            worker_event(wfid, worker_id, "workflow_worker_finished", {"returnCode": proc.returncode, "sandboxManifest": sandbox_spawn.manifest(getattr(proc, "sandbox_receipt", {}))})
            # SPEC-165 R8 classification input: finalText + errorText + stderr for
            # stream-json (never the raw envelope), else stdout + stderr.
            classify_text = (parsed["finalText"] + parsed["errorText"] + stderr) if parsed["isStreamJson"] else (stdout + stderr)
            return {"payload": payload, "timeout": False, "classifyText": classify_text}
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stdout, stdout_truncated = truncate_text(out, max_stdout)
            stderr, stderr_truncated = truncate_text(err + f"\nTIMEOUT after {timeout}s\n", max_stderr)
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            worker_event(wfid, worker_id, "workflow_worker_timeout", {"timeoutSeconds": timeout})
            payload = {"workerId": worker_id, "returnCode": None, "timeout": True, "startedAt": started, "finishedAt": now(), "stdout": str(stdout_path.relative_to(ROOT)), "stderr": str(stderr_path.relative_to(ROOT)), "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}
            return {"payload": payload, "timeout": True, "classifyText": None}

    # SPEC-165 R7 spawn-time chain walk, resolved against the chosen executor
    # (mirrors async): an unrouted executor yields canonical's one attempt.
    chain = model_routing.spawn_chain(ROOT, task_profile, executor=executor_name)
    if not chain:
        return _attempt(executor_name, spawn_override)["payload"]
    reasons: list[str] = []
    last = len(chain) - 1
    for i, hop in enumerate(chain):
        hop_exec = str(hop["executor"])
        hop_card = hop.get("card")
        nxt = chain[i + 1] if i < last else None
        # 1. pre-spawn usability; 2. rule 8 guard on the actual hop card. A skip is
        # NOT a whole-run refusal unless the chain exhausts.
        usable, reason = _hop_usable(hop_exec)
        if usable:
            verdict = spawn_guard.guard_spawn({"kind": workflow_kind, "workers": fanout_width,
                                               "card": ((spawn_override or {}).get("model") if i == 0 else None) or hop_card, "profile": task_profile,
                                               "hasExplicitAck": frontier_ack}, ROOT)
            if not verdict["allow"]:
                usable, reason = False, verdict["reason"]
        if conflicts := workflow_spawn.seed_isolation_conflicts(worker, hop_exec, workflow):
            usable, reason = False, f"seed-isolation: {worker_id} executor {hop_exec} produced seed source {', '.join(conflicts)} - INV-1 research isolation"
        if not usable:
            reasons.append(f"{hop_exec}·{hop_card}: {reason}")
            if nxt is None:
                break
            sys.stderr.write(f"[routing] hop {hop_exec}·{hop_card} unusable ({reason}); "
                             f"trying {nxt['executor']}·{nxt.get('card')}\n")
            continue
        result = _attempt(hop_exec, {**{"model": hop_card, "effort": hop.get("effort")}, **({k: v for k, v in spawn_override.items() if v is not None} if i == 0 and spawn_override else {})})
        # A timeout is terminal (mid-run death is the workflow retry layer's job, not
        # a failover trigger). Otherwise classify: a rate-limit/auth/quota with a next
        # hop advances the walk (one attempt per hop), anything else is terminal.
        cls = None if result["timeout"] else _classify_spawn_failure(hop_exec, result["classifyText"])
        if not (cls and nxt):
            payload = result["payload"]
            if i > 0:  # rule 12: a non-primary hop produced the terminal result
                payload["fellBackTo"] = {"executor": hop_exec, "card": hop_card}
                payload["fallbackReason"] = "; ".join(reasons)
            return payload
        reasons.append(f"{hop_exec}·{hop_card}: {cls}")
        sys.stderr.write(f"[routing] hop {hop_exec}·{hop_card} {cls}; "
                         f"trying {nxt['executor']}·{nxt.get('card')}\n")
    raise HarnessError(f"worker spawn chain exhausted for {task_profile}: {'; '.join(reasons)}")

def resolve_worker_executor(worker: dict[str, Any], explicit: str | None = None) -> tuple[str, bool]:
    # SPEC-165 R10 + DD-mechanization P6: run override > branch seat > routed role > default.
    # Every deliberate leg is pinned for SPEC-153 Rule 1b; canonical fallback is not.
    if explicit:
        return active_executor(explicit), True
    if branch_executor := workflow_spawn.branch_executor(worker):
        return branch_executor, True
    res = model_routing.resolve_role(ROOT, worker.get("taskProfile", "scan"))
    primary = res.get("primary")
    if res.get("profile") != model_routing.CANONICAL and isinstance(primary, dict):
        return primary.get("executor") or model_routing.DEFAULT_EXECUTOR, True
    return active_executor(None), False

def workflow_run(
    wfid: str,
    executor_name: str | None = None,
    concurrency: int | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
    only_missing: bool = True,
    *,
    allow_placeholder: bool = False,
    force_round: bool = False,
    force_lock: bool = False,
    allow_frontier: bool = False, spawn_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    executor = active_executor(executor_name)
    if not dry_run:
        ensure_executor_runnable(executor, allow_placeholder=allow_placeholder)
    limits = workflow.get("limits", {}) or {}
    max_concurrency = int(concurrency or limits.get("concurrency", workflow_limits()["concurrency"]))
    per_executor_limit = executor_concurrency_limit(executor)
    if per_executor_limit is not None and max_concurrency > per_executor_limit:
        workflow_event(wfid, "workflow_concurrency_capped", {"executor": executor, "requested": max_concurrency, "limit": per_executor_limit})
        max_concurrency = per_executor_limit
    timeout_s = int(timeout or limits.get("timeoutSeconds", workflow_limits()["timeoutSeconds"]))
    if workflow.get("policy", {}).get("writeAllowed"):
        validate_write_workflow_ready(workflow)
        if not workflow.get("policy", {}).get("parallelWritesAllowed") and max_concurrency > 1:
            workflow_event(wfid, "workflow_write_concurrency_capped", {"requested": max_concurrency, "limit": 1})
            max_concurrency = 1
    runnable = []
    for worker in workflow.get("workers", []):
        result_path = base / str(worker.get("resultPath", ""))
        if only_missing and result_path.exists() and not validate_worker_result(read_json(result_path, {}) or {}, expected_workflow_id=wfid, expected_worker_id=worker.get("workerId"), max_chars=int(limits.get("maxWorkerOutputChars", 4000)), expected_scope_paths=worker_scope_paths(base, worker), result_path=result_path):
            continue
        if worker.get("status") in {"done", "partial", "blocked", "skipped"} and only_missing:
            continue
        runnable.append(worker)
    _seed_res = {w["workerId"]: resolve_worker_executor(w, executor_name) for w in runnable} if (workflow.get("seed") or {}).get("workflowIds") else {}
    for worker in runnable:
        if _seed_res:
            resolved = _seed_res[worker["workerId"]][0]
            if conflicts := workflow_spawn.seed_isolation_conflicts(worker, resolved, workflow):
                raise HarnessError(f"seed-isolation: {worker['workerId']} executor {resolved} produced seed source {', '.join(conflicts)} - INV-1 research isolation")
    if dry_run:
        return {"workflowId": wfid, "executor": executor, "concurrency": max_concurrency, "timeoutSeconds": timeout_s, "workersToRun": [w.get("workerId") for w in runnable]}
    if not runnable:
        return {"workflowId": wfid, "executor": executor, "workersRun": 0, "message": "No runnable workers"}
    # SPEC-165 R10: resolve each runnable worker's executor ONCE (omitted --executor
    # -> its branch taskProfile's routed primary; canonical/explicit -> the run-level
    # default, byte-compat). Reused for BOTH the queued event and the spawn submit so
    # attribution follows the resolved executor. Pre-check each DISTINCT resolved
    # executor that differs from the run-level one runnable before the round starts.
    _worker_res = _seed_res or {w["workerId"]: resolve_worker_executor(w, executor_name) for w in runnable}
    worker_execs = {wid: er[0] for wid, er in _worker_res.items()}
    worker_pins = {wid: er[1] for wid, er in _worker_res.items()}
    worker_overrides = workflow_spawn.worker_spawn_overrides(runnable, spawn_override)
    max_concurrency = workflow_spawn.cap_worker_concurrency(wfid, max_concurrency, worker_execs, executor_concurrency_limit, workflow_event)
    for w_exec in dict.fromkeys(worker_execs.values()):
        if w_exec != executor:
            ensure_executor_runnable(w_exec, allow_placeholder=allow_placeholder)
    # SPEC-153 spawn-economy TEETH: vendor-independent refusal BEFORE any worker
    # spawns (all spawns funnel through here; harness code, not a vendor hook). DRY
    # (v3): delegate to the shared funnel gate spawn_guard.first_denied_worker (which
    # runs spawn_guard.guard_spawn per runnable worker, resolving each real card via
    # the exact seam run_one_worker uses, executor_profile_spawn -- so defaultSpawn
    # executors read truly, not as unresolvable) -- the SAME helper the async
    # `workflow start` teeth use, so neither funnel escapes. Ack is --allow-frontier
    # or HARNESS_ALLOW_FRONTIER=1 ONLY; --executor claude is not an ack. Behavior is
    # identical to the old inline loop: warn -> event + proceed; block -> event +
    # raise (zero workers spawned); off -> silent. Fail-open by design.
    # SPEC-165 R7/R8: the fan-out width, workflow kind, and frontier ack thread into
    # each worker's spawn walk (per-hop rule-8 guard) as well as the run-level teeth.
    kind = str(workflow.get("type") or "")
    ack = bool(allow_frontier) or os.environ.get("HARNESS_ALLOW_FRONTIER") == "1"
    guard_mode = spawn_guard.guard_mode(ROOT)
    if guard_mode != "off":
        verdict = spawn_guard.first_denied_worker(
            ROOT, kind, len(runnable), runnable, executor, executor_profile_spawn, ack, pins=worker_pins, spawn_override=spawn_override,
            worker_executors=worker_execs, branch_spawn_pins={w["workerId"]: (w.get("spawn") or {}) for w in runnable})
        if verdict["denied"]:
            event = {"reason": verdict["reason"], "workerId": verdict["workerId"],
                     "card": verdict["card"], "workers": len(runnable), "kind": kind}
            if guard_mode == "warn":
                workflow_event(wfid, "workflow_spawn_economy_warned", event)
                print(f"spawn-economy guard (warn): {verdict['reason']}", file=sys.stderr)
            else:
                workflow_event(wfid, "workflow_spawn_economy_refused", event)
                raise HarnessError(
                    "Spawn-economy guard refused `workflow run` (SPEC-153): "
                    f"{verdict['reason']} [executor={executor} profile={verdict['profile']} "
                    f"workerId={verdict['workerId']} workers={len(runnable)} kind={kind}]. Zero workers were spawned.")
        elif verdict.get("warning"):
            # Rule 1b (SPEC-153): an unpinned frontier spawn at width 1 -- event + stderr
            # only, the run proceeds (block-unpinned mode would have denied above).
            warn = verdict["warning"]
            workflow_event(wfid, "workflow_spawn_economy_warned",
                           {"reason": warn["reason"], "workerId": warn["workerId"],
                            "card": warn["card"], "workers": len(runnable), "kind": kind})
            print(f"spawn-economy guard (warn): {warn['reason']}", file=sys.stderr)
    current_round = int(workflow.get("round", 0))
    max_rounds = int(workflow.get("maxRounds", limits.get("maxRounds", workflow_limits()["maxRounds"])))
    if current_round >= max_rounds and not force_round:
        raise HarnessError(f"Workflow {wfid} reached maxRounds={max_rounds}; use --force-round only after human review")
    # SPEC-149 regra 2: this run is born with an ownership epoch (inherit a parent
    # dispatch's, else acquire one) BEFORE the first ownerEpoch-stamping update, so
    # a stale re-runner is fenced by workflow_update.
    run_epoch, epoch_degraded = epoch.ensure_run_epoch(ROOT)
    workflow_event(wfid, "workflow_epoch_acquired", {"ownerEpoch": run_epoch, "degraded": epoch_degraded})
    acquire_workflow_lock(wfid, "workflow run", force=force_lock)
    round_record = {"round": current_round + 1, "startedAt": now(), "executor": executor, "workersPlanned": [w.get("workerId") for w in runnable]}
    try:
        workflow["round"] = current_round + 1
        workflow.setdefault("roundHistory", []).append(round_record)
        workflow_update(base, workflow, status="running", force=workflow.get("status") not in WORKFLOW_TRANSITIONS)
        for worker in runnable:
            set_worker_status(workflow, worker["workerId"], "queued", force=False, resolvedExecutor=worker_execs[worker["workerId"]])
            worker_event(wfid, worker["workerId"], "workflow_worker_queued", {"executor": worker_execs[worker["workerId"]]})
        workflow_update(base, workflow, force=True)
        run_results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
            futures = {}
            for worker in runnable:
                set_worker_status(workflow, worker["workerId"], "running", attempts=int(worker.get("attempts", 0)) + 1)
                workflow_update(base, workflow, force=True)
                futures[pool.submit(run_one_worker, wfid, worker, worker_execs[worker["workerId"]], timeout_s,
                                    fanout_width=len(runnable), frontier_ack=ack, workflow_kind=kind, workflow=workflow, spawn_override=worker_overrides[worker["workerId"]])] = worker
            for future in as_completed(futures):
                worker = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"workerId": worker.get("workerId"), "error": str(exc), "finishedAt": now()}
                    worker_event(wfid, str(worker.get("workerId")), "workflow_worker_failed", {"error": str(exc)})
                run_results.append(result)
                result_path = base / str(worker.get("resultPath", ""))
                status = "failed"
                if result_path.exists():
                    data = read_json(result_path, {}) or {}
                    errors = validate_worker_result(data, expected_workflow_id=wfid, expected_worker_id=worker.get("workerId"), max_chars=int(limits.get("maxWorkerOutputChars", 4000)), expected_scope_paths=worker_scope_paths(base, worker), result_path=result_path)
                    status = data.get("status", "done") if not errors else "failed"
                    result["validationErrors"] = errors
                    worker_event(wfid, str(worker.get("workerId")), "workflow_worker_validated", {"status": status, "errors": len(errors)})
                elif result.get("rateLimitDetected") and workflow_runtime_limits().get("stopOnRateLimit", True):
                    status = "blocked"
                    result["blockedReason"] = "rate_limit_or_runtime_limit"
                    worker_event(wfid, str(worker.get("workerId")), "workflow_worker_rate_limited", {})
                elif result.get("returnCode") == 0:
                    status = "missing"
                    worker_event(wfid, str(worker.get("workerId")), "workflow_worker_missing_result", {})
                set_worker_status(workflow, worker["workerId"], status, run=result)
                workflow_update(base, workflow, force=True)
        workflow["roundHistory"][-1].update({"finishedAt": now(), "workersRun": len(run_results)})
        workflow["phase"] = "awaiting_reduce"
        workflow_update(base, workflow, force=True)
        workflow_event(wfid, "workflow_run_completed", {"executor": executor, "workersRun": len(run_results), "concurrency": max_concurrency})
        validation = workflow_validate_results(wfid)
        return {"workflowId": wfid, "executor": executor, "round": workflow.get("round"), "workersRun": len(run_results), "runResults": run_results, "validation": validation}
    finally:
        release_workflow_lock(wfid)


EXPORTED_FUNCTIONS = ["worker_harness_vars", "worker_target_env", "build_worker_spawn_env", "run_one_worker", "resolve_worker_executor", "workflow_run"]

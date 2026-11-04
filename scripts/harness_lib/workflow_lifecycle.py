"""Workflow lifecycle operations: retry/finalize/promote/unlock/scrub/execute (extracted from harness.py, MF.1 round 2).

Shared harness helpers/constants arrive via bind(env) at import time —
the same idiom as the other extracted harness_lib modules (MF discipline;
this extraction also unblocks HT-T5 subject routing).
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_lib import context_checkpoint, processes, secret_scan, targets as targets_lib


def bind(env: dict[str, Any]) -> None:
    """Receive shared runtime helpers/constants from scripts/harness.py."""
    globals().update(env)


SCRUB_SAFE_PHASES = {"planned", "finalized", "rolled_back"}

def workflow_retry(wfid: str, worker_id: str, clear_result: bool = False, *, force_round: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    current_round = int(workflow.get("round", 0))
    max_rounds = int(workflow.get("maxRounds", workflow.get("limits", {}).get("maxRounds", workflow_limits()["maxRounds"])))
    if current_round >= max_rounds and not force_round:
        raise HarnessError(f"Workflow {wfid} reached maxRounds={max_rounds}; retry requires --force-round after human review")
    worker = set_worker_status(workflow, worker_id, "pending", retryRequestedAt=now())
    # SPEC-103 M3.3: keep retry-chain intermediates comparable — archive the
    # current result before it gets cleared/overwritten, so reduce can pick
    # among attempts by criteria instead of assuming the last one is best.
    result_path = base / str(worker.get("resultPath", ""))
    archived = None
    if result_path.exists():
        attempts_dir = base / "results" / "attempts"
        attempts_dir.mkdir(parents=True, exist_ok=True)
        attempt_n = len(list(attempts_dir.glob(f"{worker_id}.attempt-*.json"))) + 1
        archive_path = attempts_dir / f"{worker_id}.attempt-{attempt_n:02d}.json"
        archive_path.write_bytes(result_path.read_bytes())
        archived = str(archive_path.relative_to(base))
    if clear_result and result_path.exists():
        result_path.unlink()
    workflow_update(base, workflow, status="partial" if workflow.get("status") == "done" else None, force=True)
    worker_event(wfid, worker_id, "workflow_worker_retry_requested", {"clearResult": clear_result, "archivedAttempt": archived})
    return worker

def workflow_finalize(wfid: str, *, update_context: bool = True, regenerate_handoff: bool = True, skip_review: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    reduce_result = read_json(base / "reduce" / "reducer.result.json", None)
    if reduce_result is None:
        reduce_result = workflow_reduce(wfid)
        base, workflow = load_workflow(wfid)
    review_gate = workflow_review_gate_status(wfid, workflow, reduce_result)
    if review_gate["required"] and review_gate["enforced"] and not skip_review and not review_gate["approved"]:
        raise HarnessError(
            f"Workflow {wfid} requires reduce review before finalize. Run "
            f"`python scripts/harness.py workflow review-reduce {wfid} --executor <executor>`, save reviewer result at {review_gate['path']}, "
            "or pass --skip-review after human approval."
        )
    merge_review_gate = workflow_merge_review_gate_status(wfid, workflow)
    if merge_review_gate["required"] and merge_review_gate["enforced"] and not skip_review and not merge_review_gate["approved"]:
        raise HarnessError(
            f"Workflow {wfid} requires semantic merge review before finalize. Run "
            f"`python scripts/harness.py workflow review-merge {wfid} --executor <executor>`, save reviewer result at {merge_review_gate['path']}, "
            "or pass --skip-review after human approval."
        )
    status = reduce_result.get("status", "partial")
    workflow_update(base, workflow, status=status, force=True)
    harness_result = workflow_harness_result(workflow, reduce_result)
    harness_result["reviewGate"] = review_gate
    harness_result["mergeReviewGate"] = merge_review_gate
    schema_errors = validate_json_schema(harness_result, "harnessResult")
    if schema_errors:
        harness_result["schemaWarnings"] = schema_errors
    if jsonschema is None:
        harness_result["schemaValidation"] = "inactive"
    write_json(base / "reduce" / "harness-result.json", harness_result)
    workflow_state_put_artifact(wfid, "harness-result", base / "reduce" / "harness-result.json")
    # handoff-subject-confinement: a TARGET workflow's finalize must NOT clobber the
    # harness's global handoff/context — its regenerated handoff/context land in the
    # target's per-subject scope. subject None/"self" -> global (byte-identical). The
    # subject is the workflow's canonical target field (same source records.add_entry uses).
    subject = workflow.get("target")
    if update_context:
        update_context_from_workflow(wfid, workflow, reduce_result, subject=subject)
    if regenerate_handoff:
        generate_handoff(task=str(workflow.get("task") or reduce_result.get("summary") or "Review workflow result."), subject=subject)
    workflow["phase"] = "finalized"
    workflow["reviewGate"] = review_gate
    workflow["mergeReviewGate"] = merge_review_gate
    workflow["harnessResultPath"] = str((base / "reduce" / "harness-result.json").relative_to(ROOT))
    workflow_update(base, workflow, force=True)
    try:  # SPEC-111 R26 + ctx-medidor-custo: telemetry loss must never break finalize
        from harness_lib import cost_metrics
        # capsule_cost reads artifacts already on disk (token-audit/validation/run-logs)
        # -- no new model calls. None only when there is nothing to measure.
        cc = cost_metrics.capsule_cost(base)
        if cc:
            write_json(base / "reduce" / "capsule-cost.json", cc)
        cost_metrics.record_workflow(ROOT, workflow, status, capsule_cost=cc)
    except Exception:
        pass
    try:  # CQ.3: queryable provenance record; ledger loss must never break finalize
        from harness_lib import evidence_decay, records
        # Wave B (worker-result-pipeline): attach each worker's
        # oracleEvidence.artifactPath as a ref HANDLE (the repo-relative path the
        # worker declared, never the body) so the changelog entry links straight to
        # its evidence. Cap 5 refs total. Any collection hiccup degrades back to just
        # harnessResultPath — evidence loss must never cost the provenance record, let
        # alone break finalize.
        refs = [workflow["harnessResultPath"]]
        try:
            for worker in workflow.get("workers") or []:
                if len(refs) >= 5:
                    break
                if not isinstance(worker, dict):
                    continue
                rel = str(worker.get("resultPath") or "")
                result = read_json(base / rel, None) if rel else None
                oe = result.get("oracleEvidence") if isinstance(result, dict) else None
                artifact = oe.get("artifactPath") if isinstance(oe, dict) else None
                if artifact and artifact not in refs:
                    refs.append(artifact)
        except Exception:
            refs = [workflow["harnessResultPath"]]
        records.add_entry(
            ROOT, "change", f"workflow {wfid} finalized: {status}",
            body=f"profile={workflow.get('workflowProfile')} taskProfile={workflow.get('taskProfile')} status={status} requiresSecurityReview={bool(reduce_result.get('requiresSecurityReview', False))} sha={evidence_decay.head_sha(ROOT) or '(none)'}",
            refs=refs,
            tags=["workflow", "provenance"],
            subject=str(workflow.get("target") or "self"),
        )
    except Exception:
        pass
    workflow_event(wfid, "workflow_finalized", {"status": status, "updateContext": update_context, "regenerateHandoff": regenerate_handoff, "reviewGate": review_gate, "mergeReviewGate": merge_review_gate})
    return {"workflowId": wfid, "status": status, "harnessResult": str((base / 'reduce' / 'harness-result.json').relative_to(ROOT)), "reviewGate": review_gate, "mergeReviewGate": merge_review_gate, "contextUpdated": update_context, "handoffRegenerated": regenerate_handoff}

def workflow_promote(wfid: str, limit: int = 10, *, target: str = "draft") -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    reduce_result = read_json(base / "reduce" / "reducer.result.json", None)
    if reduce_result is None:
        reduce_result = workflow_reduce(wfid)
    recommended = (reduce_result.get("recommendedTasks") or [])[:limit]
    tasks_dir = ROOT / "tasks" / "generated"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    out_path = tasks_dir / f"{wfid}-recommended-tasks.md"
    lines = [f"# Recommended Tasks from {wfid}", "", reduce_result.get("summary", ""), "", "## Tasks", ""]
    for idx, task in enumerate(recommended, start=1):
        if isinstance(task, dict):
            lines.append(f"### {idx}. {task.get('title', 'Untitled task')}")
            lines.append("")
            lines.append(f"- Priority: `{task.get('priority', 'unknown')}`")
            lines.append(f"- Source finding IDs: `{', '.join(str(x) for x in task.get('sourceFindingIds', []) or [])}`")
            lines.append(f"- Recommendation: {task.get('recommendation', '')}")
        else:
            lines.append(f"### {idx}. {task}")
        lines.append("")
    write_text(out_path, "\n".join(lines).rstrip() + "\n")
    canonical_paths: list[str] = []
    if target == "canonical-task":
        canonical_dir = ROOT / "tasks"
        existing = len(list(canonical_dir.glob("TASK-*.md")))
        for idx, task in enumerate(recommended, start=existing + 1):
            if not isinstance(task, dict):
                task = {"title": str(task), "priority": "medium", "recommendation": str(task)}
            task_id = next_task_id(idx, str(task.get("title", "Workflow recommended task")))
            path = canonical_dir / f"{task_id}.md"
            body = f"""# {task_id}

## Title

{task.get('title', 'Workflow recommended task')}

## Source workflow

- Workflow ID: `{wfid}`
- Reducer result: `.harness/workflows/active/{wfid}/reduce/reducer.result.json`
- Source finding IDs: `{', '.join(str(x) for x in task.get('sourceFindingIds', []) or [])}`

## Priority

`{task.get('priority', 'unknown')}`

## Recommendation

{task.get('recommendation', '')}

## Evidence

"""
            for item in task.get("evidence", []) or []:
                body += f"- `{item}`\n"
            body += "\n## Acceptance criteria\n\n- The recommendation is implemented or explicitly rejected with rationale.\n- Relevant validation gates are run and recorded.\n"
            write_text(path, body)
            canonical_paths.append(str(path.relative_to(ROOT)))
        task_state_path = HARNESS / "state" / "task-state.json"
        task_state = read_json(task_state_path, {}) or {}
        task_state["lastPromotedWorkflowId"] = wfid
        task_state["canonicalTasks"] = list(dict.fromkeys((task_state.get("canonicalTasks") or []) + canonical_paths))
        task_state["updatedAt"] = now()
        write_json(task_state_path, task_state)
    append_event("workflow_tasks_promoted", {"workflowId": wfid, "path": str(out_path.relative_to(ROOT)), "target": target, "canonicalTasks": canonical_paths})
    return {"workflowId": wfid, "path": str(out_path.relative_to(ROOT)), "target": target, "canonicalTasks": canonical_paths, "tasks": len(recommended)}

def workflow_unlock(wfid: str, *, stale_only: bool = False, force: bool = False) -> dict[str, Any]:
    path = workflow_lock_path(wfid)
    if not path.exists():
        return {"workflowId": wfid, "unlocked": False, "reason": "no lock"}
    active = workflow_lock_active(path)
    if stale_only and active and not force:
        raise HarnessError(f"Workflow {wfid} lock appears active; use --force to remove")
    data = read_json(path, {}) or {}
    path.unlink()
    workflow_event(wfid, "workflow_lock_removed", {"staleOnly": stale_only, "force": force, "lock": data})
    return {"workflowId": wfid, "unlocked": True, "wasActive": active}

def workflow_fold(wfid: str) -> dict[str, Any]:
    """F1 (gap G2, deterministic slice): emit a NON-DESTRUCTIVE fold manifest for a
    FINISHED workflow. Moves and deletes nothing — manifest-first, dry-run only.

    Inventories the workflow's durable result/reduce artifacts (sha256 + bytes +
    workerRole), runs the secret-scan as the seal check, and reports the bytes a future
    spill-to-archive could reclaim. FINISHED is verified via the STATE STORE (the
    `harness-result` artifact that finalize writes), never the dir name — folding a
    non-finalized workflow is a hard error. Absent finalize evidence or any secret-scan
    hit renders the manifest UNSEALED (``sealed: false``).

    The destructive spill-to-archive is deliberately DEFERRED (round F1, cost lens):
    ship the measurement first; only build the move if reclaimable bytes prove
    non-trivial. Eviction ≠ deletion will apply there, not here.
    """
    base = workflow_dir(wfid)
    if not base.exists():
        raise HarnessError(f"workflow {wfid} has no active dir to fold\nfix: python scripts/harness.py workflow list")
    _, workflow = load_workflow(wfid)
    finalized_state = workflow_state_get(wfid, "harness-result")
    if finalized_state is None and str(workflow.get("phase")) != "finalized":
        raise HarnessError(
            f"workflow {wfid} is not FINISHED (phase={workflow.get('phase')!r})\n"
            "cause: fold only collapses a finalized workflow, verified via the state store\n"
            f"fix: python scripts/harness.py workflow finalize {wfid}"
        )
    artifacts: list[dict[str, Any]] = []
    total = 0
    scan_hits = 0
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        relp = p.relative_to(base).as_posix()
        # durable result/reduce artifacts only — skip volatile async/run-logs/locks
        if not (relp.startswith("workers/") or relp.startswith("reduce/") or relp == "workflow.json"):
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        role, hits = "", 0
        if relp.endswith(".result.json"):
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
                role = str(obj.get("workerRole") or "")
                hits = len(secret_scan.scan(obj))
            except Exception:
                pass
        scan_hits += hits
        total += len(raw)
        artifacts.append({"path": relp, "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()[:16],
                          "workerRole": role, "secretScanHits": hits})
    sealed = finalized_state is not None and scan_hits == 0
    manifest = {
        "schemaVersion": "1.0",
        "workflowId": wfid,
        "generatedAt": now(),
        "dryRun": True,  # manifest-first: this call NEVER moves or deletes anything
        "finalizedViaStateStore": finalized_state is not None,
        "sealed": sealed,
        "unsealedReason": None if sealed else ("secret-scan hits in artifacts" if scan_hits else "no finalize evidence in state store"),
        "artifactCount": len(artifacts),
        "reclaimableBytes": total,
        "secretScanHits": scan_hits,
        "artifacts": artifacts,
        "note": "destructive spill-to-archive is deferred (round F1) until reclaimable bytes prove non-trivial",
    }
    out = base / "fold-manifest.json"
    write_json(out, manifest)  # manifest-first: the ONLY write, and it moves/deletes nothing
    return manifest

def _kill_live_workflow_tree(wfid: str) -> None:
    """F2 helper: SIGTERM (bounded ~5s), then SIGKILL, the workflow's live supervisor +
    worker process trees before scrub deletes their state dir. Best-effort throughout:
    any failure (unknown pid, missing async group, dead process) is swallowed so scrub
    proceeds to delete exactly as it does today. read_async_group / list_async_tasks /
    workflow_process_alive / workflow_async_supervisor_pid_path arrive via bind(); the
    tree kill reuses processes.signal_process_tree (taskkill /T on nt reaches the venv
    shim's real interpreter + worker grandchildren)."""
    try:
        group = read_async_group(wfid)
        records: dict[int, float] = {}
        supervisor_pid = group.get("supervisorPid")
        sup_at = group.get("startedAt")
        pid_path = workflow_async_supervisor_pid_path(wfid)
        if not supervisor_pid and pid_path.exists():
            try:
                supervisor_pid = int(pid_path.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                supervisor_pid = 0
        if supervisor_pid:
            records[int(supervisor_pid)] = processes._record_epoch(sup_at, pid_path)
        for task in list_async_tasks(wfid):
            if task.get("pid"):
                records[int(task["pid"])] = processes._record_epoch(
                    task.get("startedAt"), workflow_async_supervisor_pid_path(wfid))
        # identity guard: a recorded pid re-issued to an innocent process is
        # skipped (audited skip-stale), never killed by number (wv/w29/rt6 class)
        live = processes.filter_stale_records(
            records, f"workflow_lifecycle._kill_live_workflow_tree:{wfid}")
        if not live:
            return
        for p in live:
            processes.signal_process_tree(p, signal.SIGTERM)
        deadline = time.time() + 5
        while any(workflow_process_alive(p) for p in live) and time.time() < deadline:
            time.sleep(0.1)
        for p in live:
            if workflow_process_alive(p):
                processes.signal_process_tree(p, getattr(signal, "SIGKILL", signal.SIGTERM))
    except Exception:
        pass  # kill path is best-effort; never block or raise from scrub


def workflow_scrub(wfid: str, *, force: bool = False) -> dict[str, Any]:
    base = workflow_dir(wfid)
    mirror = HARNESS / "state-store" / "workflows" / wfid
    if not base.exists() and not mirror.exists():
        raise HarnessError(
            f"workflow {wfid} has nothing to scrub\n"
            "cause: neither .harness/workflows/active/ nor the state-store mirror has this id\n"
            "fix: python scripts/harness.py workflow list"
        )
    workflow = read_json(base / "workflow.json", {}) or {}
    phase = str(workflow.get("phase") or "missing-workflow-json")
    if base.exists() and workflow_lock_active(workflow_lock_path(wfid)) and not force:
        raise HarnessError(
            f"workflow {wfid} is locked by an active run\n"
            "cause: scrub would delete state under a running workflow\n"
            f"fix: wait or unlock first: python scripts/harness.py workflow unlock {wfid}"
        )
    if workflow and phase not in SCRUB_SAFE_PHASES and not force:
        raise HarnessError(
            f"workflow {wfid} is not in a scrub-safe phase (phase={phase})\n"
            f"cause: scrub deletes worker results and reduce artifacts; safe phases: {', '.join(sorted(SCRUB_SAFE_PHASES))}\n"
            f"fix: finalize or roll back first, or rerun with --force if the artifacts are disposable"
        )
    if base.exists():
        # F2: never rmtree under a LIVE supervisor/worker tree. `workflow start` spawns a
        # DETACHED supervisor (own process group) that outlives its parent; --force used to
        # delete workflows/active/<wf> out from under it, leaving it spawning workers and
        # holding log handles into the NEXT scenario (the cross-scenario leak, root cause C1).
        # --force now means "kill THEN delete", never "delete under a live tree". Non-force
        # with an active lock still refuses above; a scrub-safe phase has no live tree here.
        # Fail-safe: an unknown/already-dead pid is a no-op and this path NEVER raises.
        _kill_live_workflow_tree(wfid)
    removed: list[str] = []
    for target in (base, mirror):
        if target.exists():
            _rmtree_robust(target)
            removed.append(str(target.relative_to(ROOT)))
    # The incident that motivated scrub: an empty mirror parent dir tripped release-hygiene.
    # rmdir gets the same retry as rmtree — a swallowed transient OneDrive failure
    # here left an empty dir behind and tripped the gate again (M4 scenario).
    for parent in (mirror.parent, ACTIVE_WORKFLOWS_DIR):
        for attempt in range(3):
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    removed.append(str(parent.relative_to(ROOT)) + "/ (empty)")
                break
            except OSError:
                if attempt == 2:
                    break
                time.sleep(0.5 * (attempt + 1))
    # scrubbedWorkflowId, not workflowId: the tracing hook mirrors workflowId
    # events into the workflow-local trace.jsonl, which would recreate the dir
    # this command just deleted.
    append_event("workflow_scrubbed", {"scrubbedWorkflowId": wfid, "phase": phase, "removed": removed, "forced": force})
    return {"workflowId": wfid, "phase": phase, "removed": removed, "forced": force}

def update_context_from_workflow(wfid: str, workflow: dict[str, Any], reduce_result: dict[str, Any], subject: str | None = None) -> None:
    # handoff-subject-confinement: TARGET subject -> per-subject scope (state_dir),
    # never the harness global. subject None/"self" -> global (byte-identical).
    if subject and subject != "self":
        state_base = targets_lib.state_dir(subject)
        context_base = state_base / "context"
    else:
        state_base = HARNESS / "state"
        context_base = HARNESS / "context"
    state_path = state_base / "workflow-state.json"
    state = read_json(state_path, {}) or {}
    status = reduce_result.get("status", "partial")
    state.update({
        "status": "workflow_" + str(status),
        "lastWorkflowId": wfid,
        "lastWorkflowType": workflow.get("type"),
        "lastWorkflowSummary": reduce_result.get("summary"),
        "lastWorkflowStatus": status,
        "lastReducerResultPath": f".harness/workflows/active/{wfid}/reduce/reducer.result.json",
        "lastHarnessResultPath": f".harness/workflows/active/{wfid}/reduce/harness-result.json",
        "lastWorkflowRequiresReview": reduce_result.get("requiresReview", False),
        "lastWorkflowRequiresSecurityReview": reduce_result.get("requiresSecurityReview", False),
        "nextRecommendedAction": "Review reducer output and promote recommended tasks." if reduce_result.get("recommendedTasks") else "Review reducer output.",
        "updatedAt": now(),
    })
    write_json(state_path, state)
    task_state_path = state_base / "task-state.json"
    task_state = read_json(task_state_path, {}) or {}
    task_state.update({
        "lastWorkflowId": wfid,
        "lastWorkflowStatus": status,
        "lastWorkflowSummary": reduce_result.get("summary"),
        "nextStep": state["nextRecommendedAction"],
        "updatedAt": now(),
    })
    write_json(task_state_path, task_state)
    next_steps = f"""# Next Steps\n\nCurrent dynamic next-step context for any agent. Keep this file compact.\n\n## Last workflow\n\n- Workflow: `{wfid}`\n- Type: `{workflow.get('type')}`\n- Status: `{status}`\n- Summary: {reduce_result.get('summary', '')}\n\n## Recommended action\n\n{state['nextRecommendedAction']}\n"""
    next_steps_path = context_base / "NEXT_STEPS.md"
    old_next_steps = next_steps_path.read_text(encoding="utf-8", errors="ignore") if next_steps_path.exists() else ""
    write_text(next_steps_path, context_checkpoint.merge_preserving_inflight(next_steps, old_next_steps))

def workflow_status_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    workers = workflow.get("workers", []) if isinstance(workflow.get("workers", []), list) else []
    counts: dict[str, int] = {}
    warnings: list[str] = []
    for worker in workers:
        status = str(worker.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        if worker.get("scopeTruncated"):
            warnings.append(f"{worker.get('workerId')} scope truncated; see {worker.get('scopePath')}")
        if worker.get("promptTruncated"):
            warnings.append(f"{worker.get('workerId')} prompt truncated; see {worker.get('scopePath')}")
    token_audit = workflow.get("tokenAudit") if isinstance(workflow.get("tokenAudit"), dict) else {}
    if token_audit.get("status") in {"warn", "fail"}:
        warnings.append(f"token audit {token_audit.get('status')}: total planned tokens {token_audit.get('totalPlannedTokens')}")
    return {
        "workflowId": workflow.get("workflowId"),
        "type": workflow.get("type"),
        "profile": workflow.get("workflowProfile"),
        "status": workflow.get("status"),
        "phase": workflow.get("phase", workflow.get("status")),
        "workers": len(workers),
        "workerStatusCounts": counts,
        "tokenAuditStatus": token_audit.get("status"),
        "estimatedTotalPlannedTokens": token_audit.get("totalPlannedTokens"),
        "warnings": warnings[:20],
    }

def workflow_execute(
    task: str,
    *,
    workflow_type: str | None = None,
    profile_name: str | None = None,
    split: str = "roots",
    shards: list[str] | None = None,
    branches: list[str] | None = None,
    executor_name: str | None = None,
    concurrency: int | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
    allow_placeholder: bool = False,
    finalize: bool = True,
    promote: bool = False,
    promote_target: str = "draft",
    pause_before_run: bool = False,
    pause_before_reduce: bool = False,
    pause_before_finalize: bool = False,
    pause_before_promote: bool = False,
    write_allowed: bool = False,
    parallel_writes: bool = False,
    isolation_mode: str | None = None,
    approval_token: str | None = None,
    override_budget: bool = False,
) -> dict[str, Any]:
    workflow = plan_workflow(task, workflow_type, shards, branches, profile_name=profile_name, split=split, concurrency=concurrency, write_allowed=write_allowed, parallel_writes=parallel_writes, isolation_mode=isolation_mode, approval_token=approval_token)
    wfid = workflow["workflowId"]
    if dry_run:
        # SPEC-102: preview only — the persisted plan is reusable via `workflow
        # start <id>`, but nothing runs: no prepare-write workspaces, no locks,
        # no run/validation state transitions, no worker spawns.
        plan = read_json(workflow_dir(wfid) / "plan.json", {}) or {}
        spawn_commands = [
            {
                "workerId": worker.get("workerId"),
                "command": " ".join(shlex.quote(p) for p in workflow_spawn_command_for_prompt(worker["promptPath"], worker.get("taskProfile", "scan"), executor_name)),
            }
            for worker in workflow.get("workers", [])
        ]
        return {
            "workflowId": wfid,
            "dryRun": True,
            "planned": True,
            "executed": False,
            "workers": len(workflow.get("workers", [])),
            "spawnCommands": spawn_commands,
            "tokenAudit": plan.get("tokenAudit", {}),
            "writeWorkspacesPrepared": False,
            "nextCommand": f"python scripts/harness.py workflow start {wfid} --executor {executor_name or active_executor(None)}",
        }
    enforce_token_budget(wfid, workflow, override=override_budget)
    if workflow.get("policy", {}).get("writeAllowed"):
        workflow_prepare_write(wfid, mode=isolation_mode or "temp-copy", approval_token=approval_token)
    if pause_before_run:
        return {"workflowId": wfid, "paused": "before-run", "nextCommand": f"python scripts/harness.py workflow run {wfid} --executor {executor_name or active_executor(None)}"}
    run_result = workflow_run(wfid, executor_name, concurrency, timeout, False, True, allow_placeholder=allow_placeholder)
    validation_result = run_result.get("validation")
    if pause_before_reduce:
        return {"workflowId": wfid, "planned": True, "run": run_result, "validation": validation_result, "paused": "before-reduce", "nextCommand": f"python scripts/harness.py workflow reduce {wfid}"}
    reduce_result = workflow_reduce(wfid)
    if pause_before_finalize:
        return {"workflowId": wfid, "planned": True, "run": run_result, "validation": validation_result, "reduce": reduce_result, "paused": "before-finalize", "nextCommand": f"python scripts/harness.py workflow finalize {wfid}"}
    final_result = workflow_finalize(wfid) if finalize else None
    if promote and pause_before_promote:
        return {"workflowId": wfid, "planned": True, "run": run_result, "validation": validation_result, "reduce": reduce_result, "finalize": final_result, "paused": "before-promote", "nextCommand": f"python scripts/harness.py workflow promote {wfid}"}
    promote_result = workflow_promote(wfid, target=promote_target) if promote else None
    return {"workflowId": wfid, "planned": True, "run": run_result, "validation": validation_result, "reduce": reduce_result, "finalize": final_result, "promote": promote_result}

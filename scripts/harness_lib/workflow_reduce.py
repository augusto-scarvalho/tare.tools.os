"""Workflow reduce/reviewer helpers for Universal Agent Harness.

This module owns deterministic reducer aggregation, reducer/reviewer packets,
review gate status, and compact harness-result assembly. Keeping these helpers
outside ``scripts/harness.py`` prevents the CLI router from carrying high-token
semantic-reduction logic in every agent read.
"""
from __future__ import annotations

from harness_lib import result_contracts

import json
import re
import shlex
import time
from pathlib import Path
from typing import Any

from harness_lib import merge_candidates, secret_scan
from harness_lib.common import HarnessError, ROOT, normalize, read_json, truncate_text, write_json, write_text

_BOUND_NAMES: set[str] = set()


def bind(env: dict[str, Any]) -> None:
    """Bind router callbacks used by reducer/reviewer helpers.

    Shared dependency-free helpers come straight from ``harness_lib.common``;
    only true router callbacks/state accessors are bound here.
    """
    allowed = {
        "controlled_write_config", "has_active_or_queued_workers", "jsonschema",
        "load_workflow", "validate_json_schema",
        "validate_worker_result", "worker_scope_paths", "workflow_budget_config", "workflow_dir",
        "workflow_event", "workflow_limits", "workflow_lock_active", "workflow_lock_path",
        "workflow_spawn_command_for_prompt", "workflow_state_put_artifact", "workflow_update",
        "append_event",
    }
    missing = sorted(name for name in allowed if name not in env)
    if missing:
        raise RuntimeError("workflow_reduce bind missing dependencies: " + ", ".join(missing))
    globals().update({name: env[name] for name in allowed})
    _BOUND_NAMES.update(allowed)


# ponytail: constants, not config -- the wait absorbs a sub-second supervisor-teardown
# race, not machine policy; promote to project.json workflows.runLock if a box ever proves
# it needs tuning. Module-level so tests can zero them.
_SETTLE_WAIT_SECONDS = 10.0
_SETTLE_POLL_SECONDS = 0.5


def _wait_run_lock_release(wfid: str) -> bool:
    """Bounded settlement wait before the run-lock refusal.

    The async supervisor releases the run lock strictly AFTER the last task settles
    (async_runtime.workflow_async_supervisor: settlement writes, THEN
    release_workflow_lock in a finally). `workflow await` satisfies on task settlement
    alone, so a client can legitimately arrive here inside that teardown window -- under
    CPU stall it widens to seconds. Poll briefly; a genuinely-live run keeps its lock
    active (live pid, workflow_lock_active) past the wait and still refuses. Bounded:
    can never hang."""
    deadline = time.monotonic() + _SETTLE_WAIT_SECONDS
    while workflow_lock_active(workflow_lock_path(wfid)):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_SETTLE_POLL_SECONDS)
    return True


EXPORTED_FUNCTIONS = [
    "worker_status_counts", "workflow_validate_results", "normalize_finding_key", "apply_join_policy",
    "workflow_reduce", "validate_reviewer_result", "workflow_review_required", "workflow_review_gate_status",
    "workflow_merge_review_gate_status", "workflow_harness_result", "prepare_agentic_reducer_packet",
    "workflow_adopt_agent_reduce", "workflow_prepare_reduce_review_packet",
]


def worker_status_counts(worker_results: list[dict[str, Any]], failures: list[dict[str, Any]], expected: int) -> dict[str, int]:
    counts = {"done": 0, "partial": 0, "blocked": 0, "failed": 0, "missing": 0, "invalid": 0, "skipped": 0, "degraded": 0, "valid": len(worker_results), "expected": expected}
    for data in worker_results:
        status = str(data.get("status", "done"))
        if status in counts:
            counts[status] += 1
        if data.get("degraded") is True:
            counts["degraded"] += 1
    for failure in failures:
        kind = str(failure.get("kind", "invalid"))
        if kind in counts:
            counts[kind] += 1
        elif kind == "missing":
            counts["missing"] += 1
        else:
            counts["invalid"] += 1
    counts["failedTotal"] = counts["failed"] + counts["missing"] + counts["invalid"]
    return counts

def workflow_validate_results(wfid: str) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    limits = workflow.get("limits", {}) or {}
    max_chars = int(limits.get("maxWorkerOutputChars", workflow_limits()["maxWorkerOutputChars"]))
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    requires_security_review = False
    for worker in workflow.get("workers", []):
        result_path = base / str(worker.get("resultPath", ""))
        if not result_path.exists():
            current_status = str(worker.get("status", "pending"))
            if current_status == "blocked":
                failures.append({"workerId": worker.get("workerId"), "kind": "blocked", "error": worker.get("note") or "worker blocked without result", "path": str(result_path.relative_to(ROOT))})
            elif current_status == "skipped":
                failures.append({"workerId": worker.get("workerId"), "kind": "skipped", "error": worker.get("note") or "worker skipped without result", "path": str(result_path.relative_to(ROOT))})
            else:
                failures.append({"workerId": worker.get("workerId"), "kind": "missing", "error": "missing result", "path": str(result_path.relative_to(ROOT))})
                if worker.get("status") in {"pending", "queued", "running", "spawned"}:
                    worker["status"] = "missing"
            continue
        try:
            data = read_json(result_path, {}) or {}
        except Exception as exc:
            failures.append({"workerId": worker.get("workerId"), "kind": "invalid", "error": f"invalid json: {exc}"})
            worker["status"] = "failed"
            continue
        # SPEC-119 v5 / E3-C6a: deterministic secret-scrub at the collect boundary.
        # A secret-shaped string invalidates the result (redacted, never echoed) and
        # flags the workflow requiresSecurityReview. Runs before schema validation so
        # a schema-valid-but-leaky result is still withheld.
        secret_hits = secret_scan.scan(data)
        if secret_hits:
            detail = "; ".join(f"{h['pattern']} at {h['location']} ({h['redacted']})" for h in secret_hits[:10])
            failures.append({"workerId": worker.get("workerId"), "kind": "invalid",
                             "error": f"secret-shaped value(s) detected in result — withheld: {detail}",
                             "secretScan": secret_hits[:10]})
            worker["status"] = "failed"
            requires_security_review = True
            continue
        errors = validate_worker_result(
            data,
            expected_workflow_id=wfid,
            expected_worker_id=worker.get("workerId"),
            max_chars=max_chars,
            expected_scope_paths=worker_scope_paths(base, worker),
            # DD-mechanization P0b (SPEC-119 v13): the access class follows each WORKER's
            # effective seat (resolvedExecutor, stamped at spawn on both run paths); an
            # unstamped/homogeneous worker falls back to the workflow-level executor, so
            # unwired workflows validate byte-identical to v12.
            repo_access=result_contracts.executor_repo_access(ROOT, worker.get("resolvedExecutor") or workflow.get("executor")),
            result_path=result_path,
        )
        if errors:
            failures.append({"workerId": worker.get("workerId"), "kind": "invalid", "error": "; ".join(errors)})
            worker["status"] = "failed"
        else:
            results.append(data)
            worker["status"] = data.get("status", "done")
    counts = worker_status_counts(results, failures, len(workflow.get("workers", [])))
    summary = {
        "workflowId": wfid,
        "workersExpected": len(workflow.get("workers", [])),
        "workersValid": len(results),
        "workersCompleted": counts["done"],
        "workersPartial": counts["partial"],
        "workersBlocked": counts["blocked"],
        "workersFailed": counts["failed"],
        "workersMissing": counts["missing"],
        "workersInvalid": counts["invalid"],
        "workersDegraded": counts["degraded"],
        "schemaValidation": "jsonschema" if jsonschema is not None else "manual-fallback",
        "statusCounts": counts,
        "failures": failures,
        "requiresSecurityReview": requires_security_review,
    }
    write_json(base / "reduce" / "validation.json", summary)
    workflow_state_put_artifact(wfid, "validation", base / "reduce" / "validation.json")
    workflow["phase"] = "awaiting_reduce" if not has_active_or_queued_workers(workflow) else "running_workers"
    workflow_update(base, workflow)
    append_event("workflow_results_validated", {"workflowId": wfid, "valid": len(results), "failures": len(failures), "workersDegraded": counts["degraded"], "statusCounts": counts})
    return summary

SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# SPEC-103 M3.3: non-linear reduce — later attempts are sometimes worse than
# middle ones (Anthropic long-running-apps), so retry-chain intermediates are
# compared by criteria and recency only breaks ties.
_ATTEMPT_STATUS_RANK = {"done": 3, "partial": 2, "blocked": 1, "failed": 0}


def _attempt_score(data: dict[str, Any]) -> tuple[int, int]:
    status_rank = _ATTEMPT_STATUS_RANK.get(str(data.get("status")), 0)
    graphify = data.get("graphify") if isinstance(data.get("graphify"), dict) else {}
    evidence = (
        int(bool(data.get("sourceFilesVerified")))
        + int(graphify.get("status") == "used")
        + int(bool(data.get("itemsAnalyzed")))
        + int(bool(data.get("findings")))
    )
    return (status_rank, evidence)


def select_worker_attempt(base: Path, workflow: dict[str, Any], worker: dict[str, Any], wfid: str, max_chars: int):
    """Pick the best valid result among the current one and archived retry attempts.

    Returns (data, selection_note); selection_note is None when there was
    nothing to choose between (zero or one valid candidate).
    """
    wid = str(worker.get("workerId"))
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    attempts_dir = base / "results" / "attempts"
    if attempts_dir.exists():
        for path in sorted(attempts_dir.glob(f"{wid}.attempt-*.json")):
            candidates.append((path.name, path, read_json(path, {}) or {}))
    result_path = base / str(worker.get("resultPath", ""))
    if result_path.exists():
        candidates.append(("current", result_path, read_json(result_path, {}) or {}))
    # Same access-class resolution as workflow_validate_results (the count
    # validator): without it, a repo-blind (repoAccess=none) seat's HIGH findings
    # with an empty sourceFilesVerified fail the strict default here and get
    # dropped SILENTLY, while the count validator keeps them -> divergent tallies
    # (row reduce-drops-repo-blind-highs, 2026-08-04). One predicate, one place.
    repo_access = result_contracts.executor_repo_access(ROOT, worker.get("resolvedExecutor") or workflow.get("executor"))
    valid: list[tuple[int, str, dict[str, Any]]] = []
    for idx, (label, path, data) in enumerate(candidates):
        if not validate_worker_result(data, expected_workflow_id=wfid, expected_worker_id=wid,
                                      max_chars=max_chars, expected_scope_paths=worker_scope_paths(base, worker),
                                      repo_access=repo_access, result_path=path):
            valid.append((idx, label, data))
    if not valid:
        return None, None
    if len(valid) == 1:
        return valid[0][2], None
    best_idx, best_label, best_data = max(valid, key=lambda item: (_attempt_score(item[2]), item[0]))
    score = _attempt_score(best_data)
    note = {
        "workerId": wid,
        "attemptsConsidered": len(valid),
        "selected": best_label,
        "reason": (f"criteria score status={score[0]} evidence={score[1]} "
                   f"(status rank, then source/graph/items/findings evidence; recency breaks ties) "
                   f"over {', '.join(label for _, label, _ in valid if label != best_label)}"),
    }
    return best_data, note

def normalize_finding_key(finding: dict[str, Any]) -> tuple[str, str, str]:
    title = normalize(str(finding.get("title", "untitled"))).strip()
    category = normalize(str(finding.get("category", "other"))).strip()
    evidence = finding.get("evidence", []) if isinstance(finding.get("evidence", []), list) else []
    path_hint = ""
    for item in evidence:
        item = str(item)
        if "/" in item or "." in Path(item).name:
            path_hint = item.split(":", 1)[0].strip()
            break
    return (category, title, path_hint)

def apply_join_policy(workflow: dict[str, Any], validation: dict[str, Any], findings: list[dict[str, Any]], conflicts: list[dict[str, Any]], worker_results: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    policy = workflow.get("joinPolicy", {}) if isinstance(workflow.get("joinPolicy", {}), dict) else {}
    counts = validation.get("statusCounts", {}) if isinstance(validation.get("statusCounts", {}), dict) else {}
    failures = validation.get("failures", []) if isinstance(validation.get("failures", []), list) else []
    blockers = [item for item in findings if item.get("severity") == "blocker"]
    security_categories = {"security", "privacy", "secrets", "auth", "authorization", "dependency"}
    security_blockers = [item for item in findings if item.get("severity") == "blocker" and str(item.get("category", "")).lower() in security_categories]
    if policy.get("securityBlockerBlocksWorkflow", True) and security_blockers:
        return "blocked", blockers, conflicts
    if int(counts.get("blocked", 0)) > 0:
        return "blocked", blockers, conflicts
    if policy.get("testingFailureBlocksRelease"):
        worker_by_id = {w.get("workerId"): w for w in workflow.get("workers", [])}
        failed_ids = [str(f.get("workerId")) for f in failures]
        failed_ids += [str(r.get("workerId")) for r in worker_results if r.get("status") == "failed"]
        for worker_id in failed_ids:
            worker = worker_by_id.get(worker_id, {})
            title = normalize(str(worker.get("unitId", "") + " " + worker.get("workerRole", "") + " " + worker.get("taskProfile", "") + " " + worker.get("promptPath", "")))
            if "test" in title or "coverage" in title:
                return "blocked", blockers, conflicts
    if blockers:
        return "blocked", blockers, conflicts
    if int(counts.get("failed", 0)) > 0:
        return "failed" if int(counts.get("done", 0)) == 0 and int(counts.get("partial", 0)) == 0 else "partial", blockers, conflicts
    if int(counts.get("partial", 0)) > 0:
        return "partial", blockers, conflicts
    if (int(counts.get("missing", 0)) > 0 or int(counts.get("invalid", 0)) > 0) and policy.get("missingWorkerMakesResultPartial", True):
        return "partial" if worker_results else "failed", blockers, conflicts
    if failures:
        return "partial" if worker_results else "failed", blockers, conflicts
    return "done", blockers, conflicts

def emit_merge_candidates(base: Path, findings: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """EXP-18 shadow-mode: emit advisory merge-candidate pairs over the already
    deduplicated findings. MEASURE-ONLY and fail-open -- the dedup keys/counts/
    statuses are unchanged and ANY exception is swallowed so a detector fault can
    never break a reduce. Writes ``<wf>/reduce/merge-candidates.jsonl`` (one row
    per pair) and sets ``result["mergeCandidates"]`` (advisory) only on success.
    """
    try:
        pairs = merge_candidates.candidate_pairs(findings)
        out = base / "reduce" / "merge-candidates.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="\n") as fh:
            for row in pairs:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        result["mergeCandidates"] = {
            "experiment": "EXP-18", "advisory": True,
            "count": len(pairs), "pairs": pairs[:30],
        }
    except Exception:
        return  # fail-open: the shadow detector is never allowed to break a reduce


def workflow_reduce(wfid: str, *, allow_partial: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    if workflow_lock_active(workflow_lock_path(wfid)) and not allow_partial:
        # Settlement race, not a live run? Wait bounded, then re-judge.
        if not _wait_run_lock_release(wfid):
            raise HarnessError(f"Workflow {wfid} has an active run lock; use --allow-partial only after confirming workers should not continue")
        base, workflow = load_workflow(wfid)  # supervisor finished during the wait; refresh worker statuses
    active_workers = has_active_or_queued_workers(workflow)
    if active_workers and not allow_partial:
        raise HarnessError(
            "Cannot reduce while workers are queued/running: " + ", ".join(active_workers) + "\n"
            f"cause: reduce requires every worker settled, or its result would be silently dropped\n"
            f"fix: python scripts/harness.py workflow status {wfid}; for orphaned workers: workflow collect {wfid} --recover"
        )
    workflow_update(base, workflow, status="reducing", force=True)
    workflow["phase"] = "reducing"
    workflow_update(base, workflow, force=True)
    validation = workflow_validate_results(wfid)
    base, workflow = load_workflow(wfid)
    worker_results = []
    attempt_selections: list[dict[str, Any]] = []
    for worker in workflow.get("workers", []):
        data, selection_note = select_worker_attempt(
            base, workflow, worker, wfid,
            int(workflow.get("limits", {}).get("maxWorkerOutputChars", 4000)),
        )
        if data is not None:
            worker_results.append(data)
        if selection_note is not None:
            attempt_selections.append(selection_note)
    reduce_budget = workflow_budget_config().get("reduceBudget", {}) if isinstance(workflow_budget_config().get("reduceBudget", {}), dict) else {}
    max_findings = int(reduce_budget.get("maxFindings", 100))
    max_conflicts = int(reduce_budget.get("maxConflicts", 50))
    max_tasks = int(reduce_budget.get("maxRecommendedTasks", 20))
    max_md = int(reduce_budget.get("maxMarkdownChars", 12000))
    findings_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for data in worker_results:
        for finding in data.get("findings", []) or []:
            if not isinstance(finding, dict):
                continue
            key = normalize_finding_key(finding)
            item = findings_by_key.get(key)
            if item is None:
                item = dict(finding)
                item["workers"] = [data.get("workerId")]
                item["evidence"] = list(dict.fromkeys(str(x) for x in finding.get("evidence", []) or []))[:20]
                findings_by_key[key] = item
            else:
                item.setdefault("workers", []).append(data.get("workerId"))
                item["evidence"] = list(dict.fromkeys([*item.get("evidence", []), *[str(x) for x in finding.get("evidence", []) or []]]))[:20]
                if SEVERITY_ORDER.get(str(finding.get("severity", "info")), 99) < SEVERITY_ORDER.get(str(item.get("severity", "info")), 99):
                    item["severity"] = finding.get("severity")
                if finding.get("recommendation") and finding.get("recommendation") != item.get("recommendation"):
                    conflicts.append({
                        "category": item.get("category"),
                        "title": item.get("title"),
                        "workers": list(dict.fromkeys(item.get("workers", []) + [data.get("workerId")])) ,
                        "recommendations": list(dict.fromkeys([str(item.get("recommendation", "")), str(finding.get("recommendation", ""))])),
                    })
    findings = sorted(findings_by_key.values(), key=lambda f: (SEVERITY_ORDER.get(str(f.get("severity", "info")), 99), str(f.get("category", "")), str(f.get("title", ""))))[:max_findings]
    for idx, finding in enumerate(findings, start=1):
        finding.setdefault("id", f"FINDING-{idx:03d}")
        finding.setdefault("sourceWorkerIds", list(dict.fromkeys(finding.get("workers", []))))
    conflicts = conflicts[:max_conflicts]
    status, blockers, conflicts = apply_join_policy(workflow, validation, findings, conflicts, worker_results)
    counts = validation.get("statusCounts", {}) if isinstance(validation.get("statusCounts", {}), dict) else {}
    failed_total = int(counts.get("failed", 0)) + int(counts.get("missing", 0)) + int(counts.get("invalid", 0))
    recommended_tasks = []
    for idx, item in enumerate(findings[:max_tasks], start=1):
        recommended_tasks.append({
            "title": f"Address {item.get('category', 'finding')}: {item.get('title', 'untitled')}",
            "priority": item.get("severity", "info"),
            "sourceFindingIds": [item.get("id")],
            "sourceFindings": [item.get("title", "untitled")],
            "recommendation": item.get("recommendation", ""),
            "evidence": item.get("evidence", [])[:5],
        })
    reduce_result = {
        "workflowId": wfid,
        "status": status,
        "summary": f"Reduced {len(worker_results)} valid worker result(s), {len(findings)} deduplicated finding(s), {len(blockers)} blocker(s), {len(conflicts)} conflict(s), {failed_total} failed/missing/invalid worker(s), {counts.get('partial', 0)} partial worker(s), and {counts.get('blocked', 0)} blocked worker(s).",
        "workersCompleted": int(counts.get("done", 0)),
        "workersPartial": int(counts.get("partial", 0)),
        "workersBlocked": int(counts.get("blocked", 0)),
        "workersFailed": failed_total,
        "workerStatusCounts": counts,
        "dedupedFindings": findings,
        "conflicts": conflicts,
        "blockers": blockers,
        "recommendedTasks": recommended_tasks,
        "requiresReview": bool(findings or failed_total or int(counts.get("partial", 0)) or int(counts.get("blocked", 0))),
        "requiresSecurityReview": bool(validation.get("requiresSecurityReview")) or any(str(item.get("category", "")).lower() in {"security", "privacy", "secrets", "auth", "authorization"} or item.get("severity") in {"blocker", "high"} for item in findings),
        "joinPolicyApplied": workflow.get("joinPolicy", {}),
        "allowPartialReduce": allow_partial,
        "attemptSelection": attempt_selections,
    }
    # SPEC-119 v5 / E3-C6a: scan the reduce output too (defense-in-depth — an agentic
    # reducer or an aggregated field could reintroduce a secret shape). Redacted, flags review.
    reduce_secret_hits = secret_scan.scan({k: v for k, v in reduce_result.items() if k != "requiresSecurityReview"})
    if reduce_secret_hits:
        reduce_result["requiresSecurityReview"] = True
        reduce_result["secretScanFindings"] = reduce_secret_hits[:10]
    schema_errors = validate_json_schema(reduce_result, "reduceResult")
    if schema_errors:
        reduce_result["schemaWarnings"] = schema_errors
    if jsonschema is None:
        reduce_result["schemaValidation"] = "inactive"
    write_json(base / "reduce" / "reducer.result.json", reduce_result)
    workflow_state_put_artifact(wfid, "reduce-result", base / "reduce" / "reducer.result.json")
    md = "# Reduce Result\n\n" + reduce_result["summary"] + "\n\n"
    if blockers:
        md += "## Blockers\n\n" + "\n".join(f"- {b.get('title', 'untitled')}: {b.get('recommendation', '')}" for b in blockers[:20]) + "\n\n"
    if conflicts:
        md += "## Conflicts\n\n" + "\n".join(f"- {c.get('title', 'untitled')}: conflicting recommendations from {', '.join(c.get('workers', []))}" for c in conflicts[:20]) + "\n\n"
    if findings:
        md += "## Findings\n\n" + "\n".join(f"- [{f.get('severity', 'info')}] {f.get('category', 'other')}: {f.get('title', 'untitled')}" for f in findings[:25]) + "\n"
    md, _ = truncate_text(md, max_md)
    write_text(base / "reduce" / "reducer.result.md", md)
    emit_merge_candidates(base, findings, reduce_result)
    workflow["phase"] = "reduced"
    workflow_update(base, workflow, status=status, force=True)
    workflow_event(wfid, "workflow_reduced", {"status": status, "findings": len(findings), "failedTotal": failed_total, "partial": counts.get("partial", 0), "blocked": counts.get("blocked", 0)})
    return reduce_result

# SPEC-103 M3.1: shared skeptical-review contract for every reviewer prompt.
# Grounding (Anthropic long-running-apps article): reviewers identify real issues
# and then talk themselves into approving — hard per-criterion thresholds and a
# no-self-waiver rule are what fixed it. Wording is deliberately plain: no
# loaded language (F3 audit).
REVIEW_CRITERIA = ("correctness-of-claims", "test-evidence", "security-and-secrets", "scope-conformance", "regression-risk")
SKEPTICAL_REVIEW_BLOCK = f"""Skeptical review contract:
- Grade each named criterion pass or fail with one line of evidence: {", ".join(REVIEW_CRITERIA)}. One failed criterion means the review cannot approve.
- Report it in the JSON as "criteria": [{{"name": "...", "verdict": "pass|fail", "evidence": "..."}}] covering all {len(REVIEW_CRITERIA)} criteria — also when everything passes.
- List every issue found under reviewFindings; for each one, state whether it blocks approval ("blocksApproval": true|false) and why not when it does not ("whyNotBlocking").
- Actively probe edge cases and failure paths; absent test evidence is a finding, not a pass.
- No self-waiver: you may not waive a blocker or a failed criterion you identified. If any stands, set status to changes_requested or blocked, approvedForFinalize=false and requiresHumanReview=true — waiving is a human decision."""


def validate_reviewer_result(data: dict[str, Any], *, expected_workflow_id: str | None = None) -> list[str]:
    errors: list[str] = []
    # TSV/JSON boundary P1: reviewFindings may arrive as one agent-written TSV table
    # string — same ingest normalization as WORKER_RESULT.findings (result_contracts).
    if isinstance(data.get("reviewFindings"), str):
        from harness_lib.common import tsv_table
        data["reviewFindings"] = tsv_table(data["reviewFindings"])
    errors.extend(validate_json_schema(data, "reviewerResult"))
    for key in ["workflowId", "status", "summary", "reviewFindings", "approvedForFinalize", "requiresHumanReview"]:
        if key not in data:
            errors.append(f"missing {key}")
    if expected_workflow_id and data.get("workflowId") != expected_workflow_id:
        errors.append("workflowId mismatch")
    if data.get("status") not in {"approved", "approved_with_notes", "changes_requested", "blocked"}:
        errors.append("invalid reviewer status")
    if not isinstance(data.get("reviewFindings", []), list):
        errors.append("reviewFindings must be a list")
    if not isinstance(data.get("approvedForFinalize", False), bool):
        errors.append("approvedForFinalize must be boolean")
    if not isinstance(data.get("requiresHumanReview", False), bool):
        errors.append("requiresHumanReview must be boolean")
    # W29.N3: optional evidence anchor echo; manual shape check mirrors the
    # rest of this function (the jsonschema dependency is optional)
    capsule_sha = data.get("capsuleHeadSha")
    if capsule_sha is not None and not (
            isinstance(capsule_sha, str) and re.fullmatch(r"[0-9a-f]{7,64}", capsule_sha)):
        errors.append("capsuleHeadSha must be a lowercase hex sha (7-64 chars) or null")
    # SPEC-103 M3.1: optional named criteria; when present they must be well-formed.
    criteria = data.get("criteria")
    failed_criteria: list[str] = []
    if criteria is not None:
        if not isinstance(criteria, list) or not criteria:
            errors.append("criteria must be a non-empty list when present")
        else:
            for item in criteria:
                if not isinstance(item, dict) or not str(item.get("name") or "").strip() or item.get("verdict") not in {"pass", "fail"}:
                    errors.append("each criterion needs a name and a verdict of pass|fail")
                    break
            failed_criteria = [str(c.get("name")) for c in criteria if isinstance(c, dict) and c.get("verdict") == "fail"]
    # SPEC-103 M3.1: no self-waiver — approving past a failed criterion or an
    # unresolved blocker is mechanically rejected, not just discouraged in prose.
    approved = bool(data.get("approvedForFinalize")) or data.get("status") in {"approved", "approved_with_notes"}
    if approved and failed_criteria:
        errors.append(
            f"approved with failed criteria: {', '.join(failed_criteria)}\n"
            "cause: one failed criterion means the review cannot approve (no self-waiver)\n"
            "fix: set status changes_requested|blocked, approvedForFinalize=false, requiresHumanReview=true — waiving is a human decision"
        )
    blockers = [str(f.get("title") or "untitled") for f in data.get("reviewFindings", []) if isinstance(f, dict) and f.get("severity") == "blocker"]
    if approved and blockers:
        errors.append(
            f"approved with unresolved blocker findings: {', '.join(blockers[:5])}\n"
            "cause: a reviewer cannot waive blockers it identified (no self-waiver)\n"
            "fix: set status changes_requested|blocked, approvedForFinalize=false, requiresHumanReview=true — waiving is a human decision"
        )
    # L3 (defect-ledger-to-fix-queue): a finding MAY carry an optional structured `defect`
    # capsule; when present it must declare a controlled class/evidence/complete-seat or
    # the reviewer result is invalid. intake_queue owns the vocab gate (no duplication).
    from harness_lib import intake_queue
    errors.extend(intake_queue.validate_review_defects(data))
    return list(dict.fromkeys(errors))

def workflow_review_required(workflow: dict[str, Any], reduce_result: dict[str, Any]) -> bool:
    policy = workflow.get("reviewPolicy", {}) if isinstance(workflow.get("reviewPolicy", {}), dict) else {}
    required = str(policy.get("required", "on-risk"))
    if required in {"never", "false", "disabled"}:
        return False
    if required in {"always", "required"}:
        return True
    risk_statuses = set(str(x) for x in policy.get("riskStatuses", ["blocked", "partial"]) or [])
    risk_flags = set(str(x) for x in policy.get("riskFlags", ["requiresReview", "requiresSecurityReview"]) or [])
    if str(reduce_result.get("status")) in risk_statuses:
        return True
    return any(bool(reduce_result.get(flag)) for flag in risk_flags)

def freshness_events(decay_class: str | None) -> list[str]:
    """Decay class -> W29.N2 invalidation-event names (pure; teeth hit both arms)."""
    return ["head-move"] if decay_class in {"aging", "stale"} else []


def _review_freshness(workflow: dict[str, Any], data: dict[str, Any] | None) -> dict[str, Any] | None:
    """W29.N3 observe-only: is the approving testimony anchored where the
    workflow's evidence was produced (capsuleHeadSha vs the N1 headSha), and
    has HEAD moved past that anchor? Event names come from the W29.N2 shared
    vocabulary (invalidation_events); flags NEVER touch approved/errors —
    enforcement is a future owner decision (EXP-31 consumes the flags)."""
    try:
        from harness_lib import evidence_decay
        capsule = (data or {}).get("capsuleHeadSha") or None
        anchor = workflow.get("headSha") or None
        decay = evidence_decay.assess(ROOT, capsule or anchor)
        return {
            "capsuleHeadSha": capsule,
            "workflowHeadSha": anchor,
            "anchorMatch": (capsule == anchor) if (capsule and anchor) else None,
            "decay": decay,
            "events": freshness_events(decay.get("class")),
        }
    except Exception:
        return None  # measurement must never break the gate (SPEC-159 R8 doctrine)


_BAD_EXIT_CLASSES = {"failed", "error", "timeout"}


def _testimony_cross_check(wfid: str, approved: bool) -> list[dict[str, Any]]:
    """W29.N3 cross-check APENAS (recompute = SEC.7, out of scope): an APPROVE
    over a worker whose own oracle capsule says it did not pass is a
    false-DONE candidate. Observe-only; [] when not approved or unreadable."""
    if not approved:
        return []
    try:
        from harness_lib import evidence_bundle
        workers = evidence_bundle.bundle(ROOT, wfid)["workers"]
    except Exception:
        return []
    return [{"workerId": w.get("workerId"),
             "exitClass": (w.get("oracleEvidence") or {}).get("exitClass")}
            for w in workers
            if (w.get("oracleEvidence") or {}).get("exitClass") in _BAD_EXIT_CLASSES]


def workflow_review_gate_status(wfid: str, workflow: dict[str, Any], reduce_result: dict[str, Any]) -> dict[str, Any]:
    base = workflow_dir(wfid)
    policy = workflow.get("reviewPolicy", {}) if isinstance(workflow.get("reviewPolicy", {}), dict) else {}
    required = workflow_review_required(workflow, reduce_result)
    enforce = bool(policy.get("enforceBeforeFinalize", False))
    path = base / "reduce" / "reviewer.result.json"
    data = read_json(path, None) if path.exists() else None
    errors: list[str] = []
    approved = False
    if data is not None:
        errors = validate_reviewer_result(data, expected_workflow_id=wfid)
        approved = not errors and bool(data.get("approvedForFinalize")) and data.get("status") in {"approved", "approved_with_notes"}
    return {
        "required": required,
        "enforced": enforce,
        "path": str(path.relative_to(ROOT)),
        "present": data is not None,
        "approved": approved,
        "errors": errors,
        # W29.N3 observe-only blocks: flags persist via workflow["reviewGate"]
        # at finalize; they never flip approved/errors.
        "freshness": _review_freshness(workflow, data),
        "testimonyCrossCheck": _testimony_cross_check(wfid, approved),
    }

def workflow_merge_review_gate_status(wfid: str, workflow: dict[str, Any]) -> dict[str, Any]:
    base = workflow_dir(wfid)
    cw = controlled_write_config()
    apply_result = read_json(base / "reduce" / "merge-apply-result.json", None)
    required = bool(workflow.get("policy", {}).get("writeAllowed")) and bool(apply_result) and bool(cw.get("requiresSemanticMergeReview", True))
    path = base / "reduce" / "merge-reviewer.result.json"
    data = read_json(path, None) if path.exists() else None
    errors: list[str] = []
    approved = False
    if data is not None:
        errors = validate_reviewer_result(data, expected_workflow_id=wfid)
        approved = not errors and bool(data.get("approvedForFinalize")) and data.get("status") in {"approved", "approved_with_notes"}
    return {
        "required": required,
        "enforced": required,
        "path": str(path.relative_to(ROOT)),
        "present": data is not None,
        "approved": approved,
        "errors": errors,
    }

def workflow_harness_result(workflow: dict[str, Any], reduce_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "taskId": workflow.get("workflowId"),
        "status": reduce_result.get("status", "partial"),
        "summary": reduce_result.get("summary", ""),
        "filesChanged": [],
        "testsRun": [],
        "failedTests": [],
        "coverage": {"status": "not_applicable", "summary": "Workflow analysis does not directly produce coverage."},
        "universalSpecsApplied": [
            "specs/00-universal/agentic-map-reduce.md" if workflow.get("type") == "map-reduce" else "specs/00-universal/agentic-fork-join.md",
            "specs/00-universal/structural-discovery.md",
        ],
        "universalSpecDeviations": [],
        "contextDiscovery": {"graphify": {
            "status": "used" if str((workflow.get("graphifyPartitioning") or {}).get("partitioning", "")).startswith("graphify") else "not_applicable",
            "reportRead": bool((workflow.get("graphifyPartitioning") or {}).get("reportExists", False)),
            "queries": [],
            "sourceFilesVerified": [],
            "reason": "Workflow partitioning metadata: " + str((workflow.get("graphifyPartitioning") or {}).get("partitioning", "not_used")),
        }},
        "workflow": {
            "workflowId": workflow.get("workflowId"),
            "type": workflow.get("type"),
            "profile": workflow.get("workflowProfile"),
            "workersCompleted": reduce_result.get("workersCompleted", 0),
            "workersFailed": reduce_result.get("workersFailed", 0),
            "requiresReview": reduce_result.get("requiresReview", False),
            "requiresSecurityReview": reduce_result.get("requiresSecurityReview", False),
        },
        "nextStep": "Review reducer output and promote recommended tasks." if reduce_result.get("recommendedTasks") else "Review reducer output.",
        "requiresReview": reduce_result.get("requiresReview", False),
        "requiresSecurityReview": reduce_result.get("requiresSecurityReview", False),
        "requiresEscalation": reduce_result.get("requiresEscalation", False),
        "suggestedProfile": reduce_result.get("suggestedProfile"),
        # esc-subject-scope D1 (G3): a target workflow's escalation carries its
        # subject; None for harness-self workflows (reads as "self").
        "target": workflow.get("target"),
    }

def prepare_agentic_reducer_packet(wfid: str, reduce_result: dict[str, Any], executor_name: str | None = None) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    compact_input = {
        "workflowId": wfid,
        "task": workflow.get("task"),
        "status": reduce_result.get("status"),
        "summary": reduce_result.get("summary"),
        "workerStatusCounts": reduce_result.get("workerStatusCounts"),
        "dedupedFindings": reduce_result.get("dedupedFindings", [])[:50],
        "conflicts": reduce_result.get("conflicts", [])[:25],
        "recommendedTasks": reduce_result.get("recommendedTasks", [])[:20],
    }
    input_path = base / "reduce" / "agent-reducer.input.compact.json"
    prompt_path = base / "reduce" / "agent-reducer.prompt.md"
    candidate_path = base / "reduce" / "agent-reducer.candidate.json"
    write_json(input_path, compact_input)
    prompt = f"""# Agentic Reducer Packet

- Workflow: `{wfid}`
- Compact input: `{input_path.relative_to(ROOT)}`
- Candidate output: `{candidate_path.relative_to(ROOT)}`

Review the compact reducer input semantically. Do not read worker logs unless absolutely necessary. Produce a valid REDUCE_RESULT JSON object. Preserve blockers, conflicts, source finding IDs, source worker IDs, and evidence paths. If you cannot improve the deterministic reduce result, return the same status and explain why in the summary.
"""
    write_text(prompt_path, prompt)
    result: dict[str, Any] = {"workflowId": wfid, "inputPath": str(input_path.relative_to(ROOT)), "promptPath": str(prompt_path.relative_to(ROOT)), "candidatePath": str(candidate_path.relative_to(ROOT)), "preparedOnly": True, "executionRequired": True}
    if executor_name:
        cmd = workflow_spawn_command_for_prompt(str(prompt_path.relative_to(ROOT)), "review", executor_name)
        result["spawnCommand"] = " ".join(shlex.quote(p) for p in cmd)
    workflow_event(wfid, "workflow_agentic_reducer_prepared", result)
    return result


def workflow_adopt_agent_reduce(wfid: str, *, force: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    candidate_path = base / "reduce" / "agent-reducer.candidate.json"
    data = read_json(candidate_path, None)
    if data is None:
        raise HarnessError(f"agent reducer candidate missing at {candidate_path.relative_to(ROOT)}")
    errors = validate_json_schema(data, "reduceResult")
    if data.get("workflowId") != wfid:
        errors.append("workflowId mismatch")
    if errors and not force:
        raise HarnessError("agent reducer candidate invalid: " + "; ".join(errors))
    target = base / "reduce" / "reducer.result.json"
    backup = base / "reduce" / "reducer.result.deterministic.backup.json"
    if target.exists() and not backup.exists():
        write_json(backup, read_json(target, {}) or {})
    write_json(target, data)
    workflow_event(wfid, "workflow_agentic_reducer_adopted", {"candidatePath": str(candidate_path.relative_to(ROOT)), "forced": force})
    return {"workflowId": wfid, "adopted": True, "candidatePath": str(candidate_path.relative_to(ROOT)), "backupPath": str(backup.relative_to(ROOT))}


def workflow_prepare_reduce_review_packet(wfid: str, *, executor_name: str | None = None, validate_existing: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    reduce_result = read_json(base / "reduce" / "reducer.result.json", None)
    if reduce_result is None:
        raise HarnessError("reduce result missing; run workflow reduce first")
    prompt_path = base / "reduce" / "reviewer.prompt.md"
    review_path = base / "reduce" / "reviewer.result.json"
    if validate_existing:
        data = read_json(review_path, None)
        if data is None:
            raise HarnessError(f"reviewer result missing at {review_path.relative_to(ROOT)}")
        errors = validate_reviewer_result(data, expected_workflow_id=wfid)
        out = {"workflowId": wfid, "reviewResultPath": str(review_path.relative_to(ROOT)),
               "valid": not errors, "errors": errors}
        # L3: only a VALID reviewer result may transcribe its declared defect capsules
        # to the ledger + clustered intake. Re-running is safe (idempotent per key).
        if not errors:
            from harness_lib import intake_queue
            out["defectIngest"] = intake_queue.ingest_review_defects(
                ROOT, data, source_path=str(review_path.relative_to(ROOT)))
        return out
    from harness_lib import intake_queue  # L3 capsule doc, built from the live vocab
    prompt = f"""# Workflow Reduce Reviewer Packet

- Workflow: `{wfid}`
- Reduce result: `{(base / 'reduce' / 'reducer.result.json').relative_to(ROOT)}`
- Expected review result: `{review_path.relative_to(ROOT)}`
- Evidence anchor (headSha): `{workflow.get("headSha") or "unknown"}`

Review blockers, high findings, security/privacy/secrets findings, conflicts, failed/missing workers, and recommended tasks. Keep output compact. Return a valid REVIEWER_RESULT JSON object with keys: workflowId, status, summary, reviewFindings, approvedForFinalize, requiresHumanReview, capsuleHeadSha (echo the evidence anchor sha above IF the evidence you reviewed is anchored there; omit or null when you cannot anchor — never echo blindly).

Allowed status values: approved, approved_with_notes, changes_requested, blocked.

{SKEPTICAL_REVIEW_BLOCK}

{intake_queue.defect_capsule_prompt_block()}
"""
    write_text(prompt_path, prompt)
    result = {"workflowId": wfid, "promptPath": str(prompt_path.relative_to(ROOT)), "reviewResultPath": str(review_path.relative_to(ROOT)), "schema": "schemas/reviewer-result.schema.json", "preparedOnly": True, "executionRequired": True}
    if executor_name:
        cmd = workflow_spawn_command_for_prompt(str(prompt_path.relative_to(ROOT)), "review", executor_name)
        result["spawnCommand"] = " ".join(shlex.quote(p) for p in cmd)
    workflow_event(wfid, "workflow_reduce_review_prepared", result)
    return result

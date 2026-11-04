"""Workflow-plan compiler for Universal Agent Harness.

This module owns profile resolution and worker-packet compilation: the pure
"resolve + validate" core shared by the real plan path (``materialize=True`` ->
writes the active/WF-* dir) and the N1 zero-materialization compile loop
(``materialize=False`` -> writes nothing). Keeping it outside
``scripts/harness.py`` holds the CLI router under its line-budget ceiling while
the plan/validate contract and its secret-scan trust boundaries stay
byte-identical.

Shared dependency-free helpers come straight from ``harness_lib.common`` and the
standalone sibling modules (secret_scan, context_digest, topology_router,
workflow_schema, targets); only true router callbacks/state accessors are bound.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harness_lib import context_digest, secret_scan, seed_context, targets as targets_lib, topology_router, worker_prompt, workflow_schema
from harness_lib.common import HarnessError, ROOT, now, read_json, rmtree_robust, write_json, write_text

_BOUND_NAMES: set[str] = set()


def width_declaration_required(profile_name: str | None, profile_cfg: dict[str, Any] | None) -> bool:
    """D010 predicate (single source of truth): research-* profiles, and any profile carrying
    widthDeclarationRequired, must declare an explicit fan-out width before planning. The
    profile-planning and token-budget gate fixtures import this so they mirror the real guard
    below instead of re-hardcoding the rule (the drift was fail-closed, but still drift)."""
    return bool((profile_cfg or {}).get("widthDeclarationRequired")) or (profile_name or "").startswith("research-")


def _plan_head_sha() -> str | None:
    """W29 decay anchor: the commit the wave's evidence is bound to (None off-git)."""
    try:
        from harness_lib import evidence_decay
        return evidence_decay.head_sha(ROOT)
    except Exception:
        return None


def bind(env: dict[str, Any]) -> None:
    """Bind router callbacks/state accessors used by the plan compiler.

    Mirrors ``harness_lib.workflow_reduce.bind``: shared dependency-free helpers
    are imported above; only harness.py-owned callbacks are bound here.
    """
    allowed = {
        "append_event", "build_seed_context", "build_seed_contexts", "classify", "default_fork_branches",
        "estimate_tokens_from_text", "graphify_status", "make_map_shards",
        "require_write_approval", "schema_path_for", "token_budget_limit",
        "token_budget_status", "workflow_async_config", "workflow_await_config",
        "workflow_budget_config", "workflow_dir", "workflow_id", "workflow_limits",
        "workflow_profile_config", "workflow_state_put", "workflow_token_audit",
        "workflow_token_budget_config", "write_worker_prompt",
    }
    missing = sorted(name for name in allowed if name not in env)
    if missing:
        raise RuntimeError("workflow_plan bind missing dependencies: " + ", ".join(missing))
    globals().update({name: env[name] for name in allowed})
    _BOUND_NAMES.update(allowed)


EXPORTED_FUNCTIONS = ["validate_workflow_plan", "plan_workflow"]


def reducer_prompt_text(wfid: str, workflow_type: str, task: str) -> str:
    """The reducer packet template — shared by the real plan (writes it) and the
    validate-only token verdict (estimates it in memory) so the planned-token count
    stays faithful without a second copy of the template."""
    return f"""# Workflow Reducer Packet

- Workflow: `{wfid}`
- Type: `{workflow_type}`
- Task: {task}

Read all available `workers/*.result.json` files for this workflow. Produce `REDUCE_RESULT` at `reduce/reducer.result.json` and a concise Markdown synthesis at `reduce/reducer.result.md`.

Do not concatenate worker outputs. Deduplicate, rank by severity, preserve conflicts, enforce the join policy, and recommend next bounded tasks.
"""


def declared_width_meta(mode: str, why: str, width: int, max_workers: int) -> dict[str, Any]:
    """D010: fan-out width is parametric. Build the declared-width stamp — the band
    for the mode, the resolved width (final worker count), and whether width sits in
    band. Out-of-band is ALLOWED (D010 is a rule of thumb; the justification is
    mandatory by construction), so this never raises — ``withinBand`` is informational."""
    lo, hi = {"focused": (1, 2), "exploratory": (4, 5)}.get(mode, (1, max_workers))
    return {"mode": mode, "width": width, "justification": why, "band": [lo, hi], "withinBand": lo <= width <= hi}


def _stamp_declared_width(workflow: dict[str, Any], width_mode: str | None, width_why: str | None,
                          width_by_construction: bool, profile_name: str | None, profile_cfg: dict[str, Any] | None,
                          worker_count: int, max_workers: int) -> None:
    """D010: stamp workflow["slots"]["declaredWidth"] (width == final worker count) --
    explicit width_mode/width_why, else a by-construction branch list. Trailing tail
    of _compile_workflow_plan, split out to hold that function under the line-budget
    policy (testing/engineering-guardrails.json maxFunctionLines)."""
    if width_mode is not None and width_why is not None:  # D010: stamp declared width (width == final worker count)
        workflow.setdefault("slots", {})["declaredWidth"] = declared_width_meta(width_mode, width_why, worker_count, max_workers)
    elif width_by_construction and width_declaration_required(profile_name, profile_cfg):
        # D010 by-construction: an explicit branch list IS the width choice — stamp it custom.
        workflow.setdefault("slots", {})["declaredWidth"] = declared_width_meta(
            "custom", "explicit branch list (width chosen by construction)", worker_count, max_workers)


def _compile_workflow_plan(task: str, workflow_type: str | None, shards: list[str] | None = None, branches: list[str] | None = None, *, profile_name: str | None = None, split: str = "roots", max_workers: int | None = None, concurrency: int | None = None, includes: list[str] | None = None, excludes: list[str] | None = None, diff_scope: str = "working-tree", diff_base: str | None = None, diff_range: str | None = None, write_allowed: bool = False, parallel_writes: bool = False, isolation_mode: str | None = None, approval_token: str | None = None, target_name: str | None = None, seed: str | list[str] | None = None, width_mode: str | None = None, width_why: str | None = None, materialize: bool = True) -> tuple[str, Path, dict[str, Any]]:
    """Resolve the profile and build the workflow dict + branch/shard units + worker
    packets (incl. per-worker estimatedPromptTokens). This is the pure "resolve +
    validate" core shared by the real plan path (materialize=True → writes the
    active/WF-* dir) and the N1 compile loop (materialize=False → writes NOTHING).
    All raises (unknown profile, bad taskProfile, seed/approval checks) fire here,
    so the compiler stays the trust boundary in both modes."""
    profile_cfg = workflow_profile_config(profile_name)
    # D010: fan-out width is PARAMETRIC — research profiles must declare it (dual flag+name
    # guard, cloned from build_seed_context's seedForbidden pattern). No behavior change off
    # research profiles: the flags are optional there and only recorded when both are present.
    # An EXPLICIT branch list (--branch/--branch-json/composed GUI branches) is a width
    # declared by construction — the caller chose exactly N branches — so only plans falling
    # back to the profile's DEFAULT branch fan-out (the EXP-15 redundancy trap) must declare.
    width_by_construction = bool(branches)
    if width_declaration_required(profile_name, profile_cfg) \
            and not width_by_construction and (width_mode is None or width_why is None):
        raise HarnessError(
            "D010 (fan-out width is parametric): declare --width-mode {focused,exploratory,custom} "
            "and --width-why for this research profile — No width written = do not start the wave."
        )
    profile_type = str(profile_cfg.get("type", "")) if profile_cfg.get("type") else ""
    if workflow_type and profile_type and workflow_type != profile_type:
        raise HarnessError(f"workflow type {workflow_type!r} conflicts with profile {profile_name!r} type {profile_type!r}")
    workflow_type = workflow_type or profile_type
    if workflow_type not in {"map-reduce", "fork-join"}:
        raise HarnessError("workflow type must be map-reduce or fork-join")
    seed_md, seed_meta, findings_index = "", None, []
    seeds = [seed] if isinstance(seed, str) else list(seed or [])
    if len(seeds) == 1:
        # legacy first: byte-identical digest + legacy error texts (depth/divergence/missing-reduce)
        seed_md, seed_meta = build_seed_context(seeds[0], profile_name, profile_cfg)
        text, meta, index = build_seed_contexts(seeds, profile_name, profile_cfg)
        if len(meta["sources"][0].get("sourceExecutors") or []) > 1:  # heterogeneous source (worker-level) -> attributed seed
            seed_md, seed_meta, findings_index = text, meta, index
    elif len(seeds) >= 2:
        seed_md, seed_meta, findings_index = build_seed_contexts(seeds, profile_name, profile_cfg)
    attributed_seed = bool(findings_index)
    packet_task = task if attributed_seed else (f"{task}\n\n{seed_md}" if seed_md else task)
    if write_allowed or parallel_writes:
        require_write_approval(approval_token)
    if parallel_writes and not write_allowed:
        raise HarnessError("--parallel-writes requires --write-allowed")
    target_entry = None
    if target_name:
        # HT-T5: read-only analysis workflows over a registered target repository.
        target_entry = targets_lib.resolve(target_name)
        if write_allowed or parallel_writes:
            raise HarnessError(
                f"controlled writes on target repositories are not supported yet (HT-T5b)\n"
                "cause: locks/backups/merge machinery is harness-root-relative\n"
                "fix: run read-only analysis workflows on the target, or adopt the harness inside the target for write workflows"
            )
    split = split or str(profile_cfg.get("splitStrategy", "roots"))
    overrides = {}
    if max_workers is not None:
        overrides["maxWorkers"] = max_workers
    if concurrency is not None:
        overrides["concurrency"] = concurrency
    for key in ["maxWorkers", "maxRounds", "maxWorkerOutputChars", "concurrency", "timeoutSeconds"]:
        if key in profile_cfg:
            overrides.setdefault(key, profile_cfg[key])
    limits = workflow_limits(overrides)
    wfid = workflow_id()
    base = workflow_dir(wfid)
    # B2: the active/WF-* dir is created inside the per-type `if materialize:` blocks
    # below — AFTER make_map_shards/default_fork_branches resolve (they raise on a bad
    # taskProfile), so a plan whose branch resolution raises leaves no partial WF dir.
    profile, task_profile, scores = classify(task)
    graph_partitioning: dict[str, Any] = {"partitioning": "not_used"}
    workflow = {
        "schemaVersion": "1.1",
        "workflowId": wfid,
        "type": workflow_type,
        "workflowProfile": profile_name,
        "sharedContextDigest": bool(profile_cfg.get("sharedContextDigest")),
        "seed": seed_meta,
        "splitStrategy": split,
        "task": packet_task,
        "status": "planned",
        "phase": "planned",
        "taskProfile": profile,
        "risk": task_profile.get("risk", "unknown"),
        "createdAt": now(),
        "headSha": _plan_head_sha(),
        "updatedAt": now(),
        "policy": {
            "writeAllowed": bool(write_allowed or profile_cfg.get("writeAllowed", False)),
            "graphifyPolicy": graphify_status().get("policy", "graph-first-source-verified"),
            "sourceVerificationRequired": True,
            "parallelWritesAllowed": bool(parallel_writes),
            "requiresHumanApprovalForWrites": True,
            "writeApproval": "approved-at-plan" if write_allowed else "not_requested",
        },
        "limits": limits,
        "round": 0,
        "maxRounds": limits.get("maxRounds", 2),
        "roundHistory": [],
        "executionIsolation": {
            "mode": str(isolation_mode or ("temp-copy" if write_allowed else "shared-readonly")),
            "writeSandbox": bool(write_allowed),
            "allowedWritePaths": [
                f".harness/workflows/active/{wfid}/workers/*.result.json",
                f".harness/workflows/active/{wfid}/run-logs/*",
                f".harness/workflows/active/{wfid}/workspaces/*"
            ]
        },
        "budgets": workflow_budget_config(),
        "scores": scores,
        "target": target_name,
        "targetRoot": str(targets_lib.target_root(target_entry)) if target_entry else None,
        "joinPolicy": profile_cfg.get("joinPolicy", {
            "securityBlockerBlocksWorkflow": True,
            "missingWorkerMakesResultPartial": True,
            "preserveConflicts": True,
            "severityPrecedence": ["blocker", "high", "medium", "low", "info"],
        }),
        "reviewPolicy": profile_cfg.get("reviewPolicy", {
            "required": "on-risk",
            "enforceBeforeFinalize": False,
            "riskStatuses": ["blocked", "partial"],
            "riskFlags": ["requiresReview", "requiresSecurityReview"],
        }),
        "asyncPolicy": {
            "enabled": True,
            "mode": "detached-supervisor",
            "stateDirTemplate": ".harness/workflows/active/<workflowId>/async"
        },
        "awaitPolicy": profile_cfg.get("awaitPolicy", workflow_await_config()),
        "scheduler": workflow_async_config(),
        "cancellationPolicy": {"graceSeconds": 10, "killAfterGrace": True},
        "settlementPolicy": profile_cfg.get("awaitPolicy", workflow_await_config()),
    }
    if workflow_type == "map-reduce":
        profile_shards = profile_cfg.get("shards") if isinstance(profile_cfg.get("shards"), list) else None
        explicit_items = shards or profile_shards
        kind = "map"
        units, graph_partitioning = make_map_shards(split, explicit_items, limits["maxWorkers"], includes, excludes, diff_scope=diff_scope, diff_base=diff_base, diff_range=diff_range)
        if materialize:
            (base / "workers").mkdir(parents=True, exist_ok=True)
            (base / "reduce").mkdir(parents=True, exist_ok=True)
            write_json(base / "shards.json", {"workflowId": wfid, "splitStrategy": split, "graphifyPartitioning": graph_partitioning, "diffSource": graph_partitioning.get("diffSource"), "shards": units})
    else:
        profile_branches = profile_cfg.get("branches") if isinstance(profile_cfg.get("branches"), list) else None
        kind = "branch"
        units = default_fork_branches(limits["maxWorkers"], branches or profile_branches)
        if materialize:
            (base / "workers").mkdir(parents=True, exist_ok=True)
            (base / "reduce").mkdir(parents=True, exist_ok=True)
            write_json(base / "branches.json", {"workflowId": wfid, "branches": units})
    packet_units = units[: limits["maxWorkers"]]
    # Branch briefing: fold the parent task at the fork boundary. It only pays off when the
    # SAME long task would otherwise be re-inlined into >=2 packets — a single packet (or a
    # task under the 2x threshold) keeps the inline format, byte-identical and file-free.
    if len(packet_units) >= 2 and len(packet_task) > 2 * worker_prompt.CORE_CHARS:
        workflow.setdefault("slots", {})["parentTaskPointer"] = True
        if materialize:  # written ONCE per wave; the N1 loop renders the same text, no file
            write_text(base / worker_prompt.PARENT_TASK_NAME, packet_task)
    seed_texts: dict[str, str] = {}
    seed_pointers: list[str | None] = [None] * len(packet_units)
    if attributed_seed:
        merged_rel = (base / "seed-context.md").relative_to(ROOT).as_posix()
        seed_texts[merged_rel] = seed_md
        for i, unit in enumerate(packet_units, start=1):
            if workflow_type != "fork-join":
                seed_pointers[i - 1] = merged_rel
                continue
            branch_executor = (unit.get("spawn") or {}).get("executor")  # validated_branch_spawn accepts configured IDs; active_executor preserves explicit IDs verbatim.
            if branch_executor is None:
                selected = findings_index
                notice = "Warning: unpinned critic branch; own-executor exclusion is deferred to the run-time INV-1 refusal."
            else:
                selected = [item for item in findings_index if branch_executor not in item["executors"]]
                notice = (f"{len(findings_index) - len(selected)} finding(s) withheld - produced by this "
                          "branch's own executor (INV-1 research isolation).")
            rel = (base / "workers" / f"worker-{i:03d}.seed-context.md").relative_to(ROOT).as_posix()
            seed_texts[rel] = seed_context.render_seed_context(selected, seeds, notice)
            seed_pointers[i - 1] = rel
        if materialize:
            for rel, text in seed_texts.items():
                write_text(ROOT / rel, text)
    workers = [write_worker_prompt(base, workflow, unit, kind, i, write=materialize,
                                   seed_pointer=seed_pointers[i - 1])
               for i, unit in enumerate(packet_units, start=1)]
    if workflow["policy"].get("writeAllowed"):
        for worker in workers:
            worker["writeAllowed"] = True
    if materialize and workflow.get("sharedContextDigest"):  # E1: one wave-shared, non-authoritative digest
        context_digest.build_digest(ROOT, base, workflow_type)
    if materialize and seed_md and not attributed_seed:  # non-attributed seed materialization stays byte-identical
        write_text(base / "seed-context.md", seed_md)
    if materialize:  # DW.1: the fork boundary is a trust boundary. Secret-scan the RENDERED
        # fan-out surface (worker prompt files + context-digest) BEFORE the plan is
        # dispatchable — the K1 argv scan (validate_workflow_plan) never sees seed content,
        # the digest, or role framing that only exists post-render, and those replicate N×
        # across possibly-different-vendor executors. Same secret_scan contract as K1: hits
        # are redacted (first-4 + length), never echoed. On a hit, scrub the partial WF dir
        # (no secret left on disk, no dangling dir — same invariant as B2) and refuse to plan.
        surface = {w["promptPath"]: (ROOT / w["promptPath"]).read_text(encoding="utf-8") for w in workers}
        # parent-task.md joins the digest on this surface: under the pointer flag the prompts
        # carry only a HEAD of the task, so the body would otherwise land on disk unscanned.
        for extra in (base / context_digest.DIGEST_NAME, base / worker_prompt.PARENT_TASK_NAME, base / "seed-context.md"):
            if extra.exists():
                surface[extra.relative_to(ROOT).as_posix()] = extra.read_text(encoding="utf-8")
        surface.update(seed_texts)
    else:
        surface = seed_texts  # validate-only renders these in memory; scan the same seed surface.
    hits = secret_scan.scan(surface)
    if hits:
        if materialize:
            rmtree_robust(base)
        joined = "; ".join(f"{h['location']} ({h['pattern']}): {h['redacted']}" for h in hits)
        raise HarnessError(f"secret-shaped value in rendered fan-out surface — refusing to plan/dispatch: {joined}")
    workflow["workers"] = workers
    workflow["graphifyPartitioning"] = graph_partitioning
    _stamp_declared_width(workflow, width_mode, width_why, width_by_construction, profile_name, profile_cfg, len(workers), limits["maxWorkers"])
    return wfid, base, workflow


def _validate_token_verdict(workflow: dict[str, Any], task: str) -> dict[str, Any]:
    """Token-budget verdict for the compile loop, computed from the in-memory worker
    packets (estimatedPromptTokens are byte-identical to what workflow_token_audit
    recomputes from the on-disk prompts) plus the shared token_budget_status/limit
    helpers — no token-audit file written, no drift in the numbers that matter."""
    profile_name = workflow.get("workflowProfile")
    workflow_type = workflow.get("type")
    budget = workflow_token_budget_config(str(profile_name) if profile_name else None, str(workflow_type) if workflow_type else None)
    warn_ratio = float(budget.get("warnAtRatio", 0.8) or 0.8)
    workers = workflow.get("workers", []) if isinstance(workflow.get("workers"), list) else []
    prompt_tokens = [int(w.get("estimatedPromptTokens", 0) or 0) for w in workers]
    total_prompt = sum(prompt_tokens)
    max_prompt_seen = max(prompt_tokens) if prompt_tokens else 0
    planned_output = sum(int(w.get("estimatedMaxOutputTokens", 0) or 0) for w in workers)
    reducer_tokens = estimate_tokens_from_text(
        reducer_prompt_text(str(workflow.get("workflowId")), str(workflow_type), task),
        profile_name=str(profile_name) if profile_name else None,
        workflow_type=str(workflow_type) if workflow_type else None,
    )
    total_planned = total_prompt + planned_output + reducer_tokens
    max_worker_prompt = token_budget_limit(budget, "maxWorkerPromptTokens", 3000)
    max_total_worker_prompt = token_budget_limit(budget, "maxTotalWorkerPromptTokens", 24000)
    max_total_planned = token_budget_limit(budget, "maxTotalPlannedTokens", 32000)
    # wf-validate-only-reducer-ceiling: the SAME per-reducer ceiling the on-disk
    # token-audit enforces (workflow_token_audit maxReducerPromptTokens, same
    # default) — 2089/2000 sailed past `valid: true` on 2026-07-27 because the
    # reducer estimate only fed the TOTAL, and cost two replans.
    max_reducer = token_budget_limit(budget, "maxReducerPromptTokens", 1500)
    checks = [
        ("workerPrompt:max", max_prompt_seen, max_worker_prompt),
        ("workerPrompt:total", total_prompt, max_total_worker_prompt),
        ("reducerPrompt:max", reducer_tokens, max_reducer),
        ("plannedTokens:total", total_planned, max_total_planned),
    ]
    statuses = [(name, val, lim, token_budget_status(val, lim, warn_ratio)) for name, val, lim in checks]
    seen = {s for _, _, _, s in statuses}
    status = "fail" if "fail" in seen else ("warn" if "warn" in seen else "pass")
    return {
        "status": status,
        "totalWorkerPromptTokens": total_prompt,
        "maxWorkerPromptTokensSeen": max_prompt_seen,
        "plannedWorkerOutputTokens": planned_output,
        "reducerPromptTokens": reducer_tokens,
        "totalPlanned": total_planned,
        "maxWorkerPromptTokens": max_worker_prompt,
        "maxTotalWorkerPromptTokens": max_total_worker_prompt,
        "maxReducerPromptTokens": max_reducer,
        "maxTotalPlannedTokens": max_total_planned,
        "checks": [{"name": n, "value": v, "limit": lim, "status": s} for n, v, lim, s in statuses],
    }


def validate_workflow_plan(task: str, workflow_type: str | None, shards: list[str] | None = None, branches: list[str] | None = None, *, profile_name: str | None = None, split: str = "roots", max_workers: int | None = None, concurrency: int | None = None, includes: list[str] | None = None, excludes: list[str] | None = None, diff_scope: str = "working-tree", diff_base: str | None = None, diff_range: str | None = None, write_allowed: bool = False, parallel_writes: bool = False, isolation_mode: str | None = None, approval_token: str | None = None, target_name: str | None = None, seed: str | list[str] | None = None, width_mode: str | None = None, width_why: str | None = None, override_budget: bool = False) -> dict[str, Any]:
    """N1: zero-materialization compile loop. Resolve + validate a candidate plan
    (profile, branches, budgets, token-audit) and return the report WITHOUT writing
    any active/WF-* dir, token-audit file, digest, or event. The renderer-first
    composer (N2) calls this via `workflow plan --validate-only`."""
    errors: list[str] = []
    warnings: list[str] = []
    # K1 trust boundary: secret-scan the candidate content (argv task/branch/shard
    # strings) BEFORE any render/materialize; N2/E7 add user prompt overrides on this
    # same seam. Hits are redacted by secret_scan (first-4 + length), never echoed.
    for hit in secret_scan.scan({"task": task, "branches": branches or [], "shards": shards or []}):
        errors.append(f"secret-shaped value in {hit['location']} ({hit['pattern']}): {hit['redacted']}")
    workers_report: list[dict[str, Any]] = []
    token_audit: dict[str, Any] | None = None
    declared_width: dict[str, Any] | None = None
    profile_out, type_out = profile_name, workflow_type
    try:
        _wfid, _base, workflow = _compile_workflow_plan(
            task, workflow_type, shards, branches, profile_name=profile_name, split=split,
            max_workers=max_workers, concurrency=concurrency, includes=includes, excludes=excludes,
            diff_scope=diff_scope, diff_base=diff_base, diff_range=diff_range, write_allowed=write_allowed,
            parallel_writes=parallel_writes, isolation_mode=isolation_mode, approval_token=approval_token,
            target_name=target_name, seed=seed, width_mode=width_mode, width_why=width_why, materialize=False)
        profile_out, type_out = workflow.get("workflowProfile"), workflow.get("type")
        declared_width = (workflow.get("slots") or {}).get("declaredWidth")  # D010: rule 4 report parity (or the refusal below)
        workers_report = [{
            "workerId": w.get("workerId"), "workerRole": w.get("workerRole"),
            "taskProfile": w.get("taskProfile"), "estimatedPromptTokens": w.get("estimatedPromptTokens"),
            "promptTokenBudgetStatus": w.get("promptTokenBudgetStatus"),
        } for w in workflow.get("workers", [])]
        token_audit = _validate_token_verdict(workflow, task)
        if token_audit["status"] == "fail":
            breach = (f"token budget exceeded (compile error): worker-prompt total "
                      f"{token_audit['totalWorkerPromptTokens']}/{token_audit['maxTotalWorkerPromptTokens']}, "
                      f"reducer prompt {token_audit['reducerPromptTokens']}/{token_audit['maxReducerPromptTokens']}, "
                      f"planned {token_audit['totalPlanned']}/{token_audit['maxTotalPlannedTokens']} tokens")
            if override_budget:  # preserve the existing escape: breach becomes a warning
                warnings.append(breach + " — accepted via --override-budget")
            else:
                errors.append(breach + "; rerun with --override-budget to accept it as a warning")
        elif token_audit["status"] == "warn":
            warnings.append(f"token budget near limit: worker-prompt total "
                            f"{token_audit['totalWorkerPromptTokens']}/{token_audit['maxTotalWorkerPromptTokens']} tokens")
    except HarnessError as exc:  # unknown profile / bad taskProfile / seed/approval / D010 width refusal
        errors.append(str(exc))
    report: dict[str, Any] = {
        "validateOnly": True,
        "profile": profile_out,
        "type": type_out,
        "workers": workers_report,
        "tokenAudit": token_audit,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }
    if declared_width is not None:  # D010: present only when a declaration was made (no key = zero behavior change)
        report["declaredWidth"] = declared_width
    return report


def plan_workflow(task: str, workflow_type: str | None, shards: list[str] | None = None, branches: list[str] | None = None, *, profile_name: str | None = None, split: str = "roots", max_workers: int | None = None, concurrency: int | None = None, includes: list[str] | None = None, excludes: list[str] | None = None, diff_scope: str = "working-tree", diff_base: str | None = None, diff_range: str | None = None, write_allowed: bool = False, parallel_writes: bool = False, isolation_mode: str | None = None, approval_token: str | None = None, target_name: str | None = None, seed: str | list[str] | None = None, width_mode: str | None = None, width_why: str | None = None, validate_only: bool = False, override_budget: bool = False) -> dict[str, Any]:
    if validate_only:
        return validate_workflow_plan(
            task, workflow_type, shards, branches, profile_name=profile_name, split=split,
            max_workers=max_workers, concurrency=concurrency, includes=includes, excludes=excludes,
            diff_scope=diff_scope, diff_base=diff_base, diff_range=diff_range, write_allowed=write_allowed,
            parallel_writes=parallel_writes, isolation_mode=isolation_mode, approval_token=approval_token,
            target_name=target_name, seed=seed, width_mode=width_mode, width_why=width_why, override_budget=override_budget)
    wfid, base, workflow = _compile_workflow_plan(
        task, workflow_type, shards, branches, profile_name=profile_name, split=split,
        max_workers=max_workers, concurrency=concurrency, includes=includes, excludes=excludes,
        diff_scope=diff_scope, diff_base=diff_base, diff_range=diff_range, write_allowed=write_allowed,
        parallel_writes=parallel_writes, isolation_mode=isolation_mode, approval_token=approval_token,
        target_name=target_name, seed=seed, width_mode=width_mode, width_why=width_why, materialize=True)
    workflow_type = workflow["type"]
    workers = workflow["workers"]
    schema_file = schema_path_for("workflow")
    # DW.4: observe-only topology verdict, stamped BEFORE the schema gate so the
    # typed IR validates it. recommendedWorkers is pinned to 1 — no auto-fork
    # until DW.3 metrics are trusted (measurement-before-control).
    workflow["topologyRouter"] = topology_router.verdict(
        workflow.get("limits") or {}, workflow.get("splitStrategy"),
        len(workflow.get("workers") or []), topology_router.open_circuits())
    if schema_file is not None:  # DW.2: activate workflow.json as the typed IR — reject a
        workflow_schema.validate_packet(workflow, schema_file)  # malformed packet before it hits disk
    if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
        # gate-scenarios-leak-wf-fixture-dirs: a workflow a gate SCENARIO plans is a disposable
        # fixture. A SIDECAR marker (NOT a workflow.json field — the workflow contract stays
        # byte-identical to a real plan, so every downstream validate/apply-merge is unchanged)
        # tells scenario_isolation.sweep_fixture_workflows to remove it. Written BEFORE
        # workflow.json (base already holds workers/ + reduce/) so a scenario hard-killed
        # mid-plan can never leave an UNMARKED fixture that then looks like a real round and
        # escapes the sweep. ONLY the gate sets this env; real owner rounds and the EXP-21
        # canaries are planned OUTSIDE it, carry no marker, and survive as intended.
        (base / ".scenario-fixture").write_text("gate scenario fixture\n", encoding="utf-8")
    write_json(base / "workflow.json", workflow)
    workflow_state_put(wfid, "workflow", workflow)
    plan_payload = {"workflowId": wfid, "task": task, "type": workflow_type, "workflowProfile": profile_name, "seed": workflow.get("seed"), "splitStrategy": workflow.get("splitStrategy"), "workers": workers, "nextStep": "Run worker prompts, save WORKER_RESULT files, validate, reduce, then finalize/promote as needed."}
    write_json(base / "plan.json", plan_payload)
    workflow_state_put(wfid, "plan", plan_payload)
    write_text(base / "reduce" / "reducer.prompt.md", reducer_prompt_text(wfid, workflow_type, task))
    token_audit = workflow_token_audit(wfid, write_report=True, update_workflow=True)
    workflow = read_json(base / "workflow.json", workflow) or workflow
    plan = read_json(base / "plan.json", {}) or {}
    plan["tokenAudit"] = token_audit.get("summary", {})
    plan["tokenAuditReportPath"] = token_audit.get("reportPath")
    write_json(base / "plan.json", plan)
    workflow_state_put(wfid, "plan", plan)
    append_event("workflow_planned", {"workflowId": wfid, "type": workflow_type, "workers": len(workers), "taskProfile": workflow.get("taskProfile"), "workflowProfile": profile_name, "splitStrategy": workflow.get("splitStrategy"), "tokenAuditStatus": token_audit.get("summary", {}).get("status")})
    return workflow

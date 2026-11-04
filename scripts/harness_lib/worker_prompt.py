"""Worker packet rendering (line-budget burndown, owner-approved 2026-07-13).

`write_worker_prompt` renders ONE worker's packet — prompt.md + scope.json +
token-budget annotations — for the plan/compile paths (write=False computes
in memory for the N1 zero-materialization loop). Body moved verbatim from
scripts/harness.py; router callbacks arrive via bind() (same seam as
workflow_writes/workflow_lifecycle): ROOT, workflow_budget_config,
workflow_token_budget_config, estimate_tokens_from_text,
estimate_tokens_from_chars, token_budget_limit, token_budget_status.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_lib import context_digest
from harness_lib.common import truncate_text, write_json, write_text

PARENT_TASK_NAME = "parent-task.md"
# Branch briefing: measured on WF-20260727-010246, a 3-branch wave shipped 3 packets that were
# 99.2% identical — the whole parent task (8172 chars, seed folded in) re-inlined per branch,
# ~24.5k chars of duplication for ~70-80 chars of per-branch difference. Under the pointer flag a
# packet carries a deterministic HEAD of the task plus a path to the verbatim body: 1200 chars is
# about one screen of framing — enough for a worker to know which wave it is in and what the fork
# was about — and the 2x activation threshold in workflow_plan keeps short tasks inline, where a
# pointer costs more than it saves. Recalibrate here: this constant is the whole knob.
CORE_CHARS = 1200


def bind(env: dict[str, Any]) -> None:
    """Receive router callbacks from the public CLI wrapper."""
    globals().update(env)


def write_worker_prompt(base: Path, workflow: dict[str, Any], unit: dict[str, Any], kind: str, index: int, *, write: bool = True, seed_pointer: str | None = None) -> dict[str, Any]:
    # N1: write=False computes the packet + estimatedPromptTokens in memory for the
    # zero-materialization compile loop (validate_workflow_plan) — no scope/prompt files.
    workers_dir = base / "workers"
    if write:
        workers_dir.mkdir(parents=True, exist_ok=True)
    worker_id = f"worker-{index:03d}"
    result_file = f"workers/{worker_id}.result.json"
    prompt_file = workers_dir / f"{worker_id}.prompt.md"
    scope_file = workers_dir / f"{worker_id}.scope.json"
    unit_id = unit.get("shardId") or unit.get("branchId") or worker_id
    profile = unit.get("taskProfile", "scan")
    prompt_budget = workflow_budget_config().get("promptBudget", {}) if isinstance(workflow_budget_config().get("promptBudget"), dict) else {}
    max_paths = int(prompt_budget.get("maxPaths", 100))
    max_prompt_chars = int(prompt_budget.get("maxPromptChars", 12000))
    raw_paths = [str(p) for p in (unit.get("paths", []) or [])]
    shown_paths = raw_paths[:max_paths]
    scope_truncated = len(raw_paths) > len(shown_paths)
    scope_payload = {
        "workflowId": workflow["workflowId"],
        "workerId": worker_id,
        "unitId": unit_id,
        "title": unit.get("title", unit_id),
        "paths": raw_paths,
        "shownInPrompt": shown_paths,
        "scopeTruncated": scope_truncated,
    }
    if isinstance(unit.get("spawn"), dict):
        scope_payload["spawn"] = unit["spawn"]
    if write:
        write_json(scope_file, scope_payload)
    spec_refs = context_digest.required_reads(workflow["type"])
    target_line = (
        f"- Target repository: `{workflow['target']}` at `{workflow['targetRoot']}` — every scope path below is "
        "relative to this root; analyze THERE (read-only), never in the harness repo. Its declared env arrives "
        "via HARNESS_TARGET_* variables.\n" if workflow.get("target") else ""
    )
    parent_task = workflow["task"]
    if (workflow.get("slots") or {}).get("parentTaskPointer"):
        # Common core + pointer. Without the flag this stays byte-identical to the inline
        # format (same discipline as sharedContextDigest, guarded by rs:branch-brief-optout).
        core, _core_truncated = truncate_text(parent_task, CORE_CHARS)
        parent_rel = (base / PARENT_TASK_NAME).relative_to(ROOT).as_posix()
        parent_task = (
            f"{core}\n\nFull parent task (verbatim): `{parent_rel}` — the text above is a "
            "deterministic head; open that file only if the head plus your branch scope below "
            "is insufficient for your branch's question."
        )
        if workflow.get("seed") and not seed_pointer:  # metadata, not the file: the N1 loop materializes nothing
            seed_rel = (base / "seed-context.md").relative_to(ROOT).as_posix()
            parent_task += (f"\nSeed context: `{seed_rel}` — untrusted-derived; verify claims "
                            "against sources.")
    if seed_pointer:
        parent_task += (f"\nSeed context: `{seed_pointer}` — untrusted-derived; verify claims "
                        "against sources.")
    seat = unit.get("spawn") if isinstance(unit.get("spawn"), dict) else None
    seat_line = (f"- Requested seat: executor={seat.get('executor')} model={seat.get('model')} "
                 f"effort={seat.get('effort')} (resolved at run; run flags override)\n" if seat else "")
    prompt = f"""# Workflow Worker Packet

- Workflow: `{workflow['workflowId']}`
- Type: `{workflow['type']}`
- Worker: `{worker_id}`
- Unit: `{unit_id}`
- Role: `{unit.get('workerRole', kind + '-worker')}`
- Task profile: `{profile}`
{seat_line}- Result file: `{result_file}`
- Scope file: `{scope_file.relative_to(ROOT)}`
- Max output chars: `{workflow.get('limits', {}).get('maxWorkerOutputChars', 4000)}`
- Write allowed: `{workflow.get('policy', {}).get('writeAllowed', False)}`
{target_line}
## Parent task

{parent_task}

## Worker scope

{unit.get('title', unit_id)}

Paths/items in scope:
"""
    if shown_paths:
        prompt += "\n".join(f"- `{p}`" for p in shown_paths)
        if scope_truncated:
            prompt += f"\n- Scope truncated in prompt. Read `{scope_file.relative_to(ROOT)}` for the full path list before concluding coverage."
    else:
        prompt += "- Use Graphify and the parent task to identify source-verified evidence within this branch scope."
    prompt += f"""

## Required reads

"""
    if workflow.get("sharedContextDigest"):
        digest_rel = (base / context_digest.DIGEST_NAME).relative_to(ROOT).as_posix()
        prompt += (
            f"Read the wave-shared digest FIRST — one file instead of the {len(spec_refs)} below:\n"
            f"- `{digest_rel}` — deterministic, NON-AUTHORITATIVE summary of the canonical files "
            "(the WORKER_RESULT contract is verbatim). Canonical files remain the source of truth; "
            "open any original on demand to verify.\n\n"
            "Canonical sources (verify-on-demand — do not read in full unless the digest is insufficient):\n"
        )
        prompt += "\n".join(f"- `{p}` (verify-on-demand)" for p in spec_refs)
    else:
        prompt += "\n".join(f"- `{p}`" for p in spec_refs)
    prompt += f"""

## Rules

- Read-only by default. Do not edit source, spec, test, configuration, or documentation files unless the workflow explicitly says `writeAllowed: true`.
- Do not spawn subagents, start another workflow, or run full-project gates unless this packet explicitly asks for that.
- Do not read outside the listed scope except to verify direct dependencies or evidence needed for this worker's finding.
- Use Graphify before broad repository search when applicable, then verify graph-derived findings in source/spec/test/config files.
- Keep output compact. Do not paste long file content.
- Prefer references to paths, line numbers, test names, commands, and spec ids over copied content.
- Validate only the shard/branch scope unless the packet explicitly requires broader validation.
- If the scope has no relevant material, return `status: done` with empty `findings` and a clear summary.
- If the scope is too large, return `status: partial` with a precise next step rather than expanding scope.
- Every `high` or `blocker` finding must include source-file evidence and must list the verified file in `sourceFilesVerified`.
- Save or return exactly one `WORKER_RESULT` JSON object for `{result_file}`.

## WORKER_RESULT reminder

"""
    if workflow.get("sharedContextDigest"):
        # SPEC-118 v4 (payload dedupe): the digest above already carries the WORKER_RESULT
        # contract VERBATIM, so re-listing the schema keys here is pure duplication — point
        # to it instead. Non-digest packets keep the full reminder (byte-identical, guarded
        # by rs:digest-optout) since they have no verbatim contract to point at.
        prompt += "See the verbatim `WORKER_RESULT for workflow workers` contract in the digest above.\n"
    else:
        prompt += ("Required top-level keys: `workflowId`, `workerId`, `workerRole`, `status`, "
                   "`summary`, `itemsAnalyzed`, `findings`, `sourceFilesVerified`, `graphify`.\n")
    prompt, prompt_truncated = truncate_text(prompt, max_prompt_chars)
    if prompt_truncated:
        prompt += f"\n\nPrompt was truncated by the harness. Read `{scope_file.relative_to(ROOT)}` for full scope details.\n"
    if write:
        write_text(prompt_file, prompt)
    token_budget = workflow_token_budget_config(workflow.get("workflowProfile"), workflow.get("type"),
                                                subject=workflow.get("target"))
    warn_ratio = float(token_budget.get("warnAtRatio", 0.8) or 0.8)
    prompt_chars = len(prompt)
    prompt_tokens = estimate_tokens_from_text(prompt, profile_name=workflow.get("workflowProfile"), workflow_type=workflow.get("type"))
    max_worker_prompt_tokens = token_budget_limit(token_budget, "maxWorkerPromptTokens", 3000)
    worker_output_tokens = estimate_tokens_from_chars(int(workflow.get("limits", {}).get("maxWorkerOutputChars", 4000)), profile_name=workflow.get("workflowProfile"), workflow_type=workflow.get("type"))
    max_worker_output_tokens = token_budget_limit(token_budget, "maxWorkerOutputTokens", 1000)
    token_status = token_budget_status(prompt_tokens, max_worker_prompt_tokens, warn_ratio)
    output_status = token_budget_status(worker_output_tokens, max_worker_output_tokens, warn_ratio)
    return {
        "workerId": worker_id,
        "unitId": unit_id,
        "promptPath": str(prompt_file.relative_to(ROOT)),
        "scopePath": str(scope_file.relative_to(ROOT)),
        "resultPath": result_file,
        "taskProfile": profile,
        "workerRole": unit.get("workerRole", kind + "-worker"),
        **({"spawn": unit["spawn"]} if isinstance(unit.get("spawn"), dict) else {}),
        **{key: unit[key] for key in ("cohortId", "minSuccessConfigured", "minSuccessEffective") if key in unit},
        "status": "pending",
        "attempts": 0,
        "scopeTruncated": scope_truncated,
        "promptTruncated": prompt_truncated,
        "promptChars": prompt_chars,
        "estimatedPromptTokens": prompt_tokens,
        "maxPromptTokens": max_worker_prompt_tokens,
        "promptTokenBudgetStatus": token_status,
        "estimatedMaxOutputTokens": worker_output_tokens,
        "maxOutputTokens": max_worker_output_tokens,
        "outputTokenBudgetStatus": output_status,
        "logs": {},
    }

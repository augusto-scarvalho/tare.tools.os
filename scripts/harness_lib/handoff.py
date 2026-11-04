"""Minimal-first handoff generation for the universal agent harness.

This module owns the handoff context-pack policy and rendering so the CLI
runtime can stay focused on routing/execution.  It is dependency-free and uses
callbacks for project-specific runtime signals such as workflow suggestions and
prompt rendering.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import token_economics

# Tiny state/context heads (~1.7k tok total) — always fit, always required.
CORE_REQUIRED_READS = [
    ".harness/context/CONTEXT.md",
    ".harness/context/STATE.md",
    ".harness/context/NEXT_STEPS.md",
    ".harness/state/task-state.json",
    ".harness/state/quality-state.json",
]
# Large canonical docs — kept required while the minimal-first budget holds, else
# demoted to verify-on-demand pointers (same ladder as the profile spec reads).
# Ordered most-critical-first so the survivor is the one a spawned worker needs.
DEMOTABLE_REQUIRED_READS = [
    ".harness/prompts/subagent-contract.md",
    "AGENTS.md",
]

BASE_CONDITIONAL_READS = [
    "specs/00-universal/structural-discovery.md",
    "specs/00-universal/UNIVERSAL_BASELINE.md",
]

PROFILE_SPEC_READS: dict[str, list[str]] = {
    "cheap": ["specs/00-universal/code-quality.md"],
    "scan": ["specs/00-universal/ai-agent-safety.md"],
    "docs": ["specs/00-universal/code-quality.md", "specs/00-universal/ai-agent-safety.md"],
    "plan": ["specs/00-universal/code-quality.md", "specs/00-universal/testing-and-quality-gates.md"],
    "implementation": [
        "specs/00-universal/code-quality.md",
        "specs/00-universal/testing-and-quality-gates.md",
        "specs/00-universal/coverage-and-regression.md",
    ],
    "debug": [
        "specs/00-universal/testing-and-quality-gates.md",
        "specs/00-universal/coverage-and-regression.md",
        "specs/00-universal/observability-and-operability.md",
    ],
    "review": [
        "specs/00-universal/code-quality.md",
        "specs/00-universal/testing-and-quality-gates.md",
        "specs/00-universal/coverage-and-regression.md",
        "specs/00-universal/dependency-and-supply-chain.md",
    ],
    "security": [
        "specs/00-universal/secure-engineering.md",
        "specs/00-universal/secrets-and-configuration.md",
        "specs/00-universal/data-protection-and-privacy.md",
        "specs/00-universal/api-and-interface-security.md",
        "specs/00-universal/ai-agent-safety.md",
    ],
}

PROMOTE_PROFILE_READS = {"implementation", "debug", "review", "security"}


def _file_bytes(root: Path, paths: list[str]) -> int:
    total = 0
    for item in paths:
        path = root / item
        if path.exists() and path.is_file():
            total += path.stat().st_size
    return total


def _append_unique(paths: list[str], item: str) -> None:
    if item not in paths:
        paths.append(item)


def _fits_required_budget(
    root: Path,
    required_reads: list[str],
    candidate: str,
    *,
    max_required_read_tokens: int,
    token_budget: dict[str, Any],
) -> bool:
    if max_required_read_tokens <= 0:
        return True
    candidate_reads = required_reads if candidate in required_reads else [*required_reads, candidate]
    candidate_tokens = token_economics.estimate_tokens_from_chars(_file_bytes(root, candidate_reads), token_budget)
    return candidate_tokens <= max_required_read_tokens


def select_context_reads(
    root: Path,
    profile_name: str,
    graph: dict[str, Any],
    *,
    max_required_read_tokens: int = 0,
    token_budget: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return required/conditional read packs and budget-demoted reads.

    High-risk profile specs are useful early, but the startup pack must stay
    bounded.  Candidates that would exceed maxRequiredReadTokens are moved to
    conditional reads instead of silently violating the minimal-first contract.
    """
    token_budget = token_budget or {}
    required_reads = list(CORE_REQUIRED_READS)
    conditional_reads = list(BASE_CONDITIONAL_READS)
    budget_demoted_reads: list[str] = []

    def add_required_or_defer(path: str) -> None:
        if _fits_required_budget(
            root,
            required_reads,
            path,
            max_required_read_tokens=max_required_read_tokens,
            token_budget=token_budget,
        ):
            _append_unique(required_reads, path)
        else:
            _append_unique(conditional_reads, path)
            _append_unique(budget_demoted_reads, path)

    # The generator must fit its own budget: the big canonical docs go through the
    # same demotion ladder as spec reads instead of being force-required (the M1
    # defect — a fresh handoff overflowed maxRequiredReadTokens after E2's calibration).
    for path in DEMOTABLE_REQUIRED_READS:
        add_required_or_defer(path)

    promote_profile_reads = profile_name in PROMOTE_PROFILE_READS
    for spec_path in PROFILE_SPEC_READS.get(profile_name, []):
        if not (root / spec_path).exists():
            continue
        if promote_profile_reads:
            add_required_or_defer(spec_path)
        else:
            _append_unique(conditional_reads, spec_path)
    if (root / "specs" / "MANIFEST.yaml").exists():
        _append_unique(conditional_reads, "specs/MANIFEST.yaml")
    if graph.get("reportExists"):
        graph_report = str(graph.get("reportPath", "graphify-out/GRAPH_REPORT.md"))
        add_required_or_defer(graph_report)
    return required_reads, conditional_reads, budget_demoted_reads


def _quality_summary(quality: Any) -> Any:
    summary = quality.get("lastValidation", quality) if isinstance(quality, dict) else quality
    if not isinstance(summary, dict):
        return summary
    return {
        "gate": summary.get("gate"),
        "status": summary.get("status", "unknown"),
        "validatedAt": summary.get("validatedAt"),
        "counts": summary.get("counts", {}),
        "summary": summary.get("summary", ""),
        "details": summary.get("details"),
    }


def render_markdown(
    *,
    project: dict[str, Any],
    task_state: dict[str, Any],
    workflow: dict[str, Any],
    project_name: str,
    task_text: str,
    profile_name: str,
    profile: dict[str, Any],
    executor: str,
    files: list[str],
    shown_changed_files: list[str],
    changed_files_truncated: bool,
    commit: str | None,
    graph: dict[str, Any],
    required_reads: list[str],
    conditional_reads: list[str],
    budget_demoted_reads: list[str],
    required_read_bytes: int,
    conditional_read_bytes: int,
    required_read_tokens: int,
    conditional_read_tokens: int,
) -> str:
    """Render the human-readable handoff markdown."""
    md = f"""
# Agent Handoff

## Project

- Project: {project_name}
- Project type: {project.get('projectType', 'generic')}
- Active task: {task_state.get('taskId', workflow.get('currentTaskId', 'unknown'))} — {task_state.get('title', 'unspecified')}
- Current status: {workflow.get('status', 'unknown')}
- Last commit: {commit or 'unknown'}
- Working tree changed files: {len(files)}

## Next task

{task_text}

## Routing recommendation

- Task profile: `{profile_name}`
- Risk: `{profile.get('risk', 'unknown')}`
- Active executor: `{executor}`

The harness chooses profiles by spawning a new bounded execution. The current agent must not mutate its own model or reasoning level.

## Structural discovery

- Required skill: `{graph.get('requiredSkill', 'graphify')}`
- Policy: `{graph.get('policy', 'graph-first-source-verified')}`
- Graph status: `{graph.get('status', 'unknown')}`
- Report path: `{graph.get('reportPath', 'graphify-out/GRAPH_REPORT.md')}`

Use Graphify before broad repository search when applicable, then verify graph-derived findings in source/spec/test/config files. Record usage in `HARNESS_RESULT.contextDiscovery.graphify`.

## Last workflow

"""
    last_workflow = workflow if isinstance(workflow, dict) else {}
    if last_workflow.get("lastWorkflowId"):
        md += f"""- Workflow ID: `{last_workflow.get('lastWorkflowId')}`
- Type: `{last_workflow.get('lastWorkflowType', 'unknown')}`
- Status: `{last_workflow.get('lastWorkflowStatus', last_workflow.get('status', 'unknown'))}`
- Summary: {last_workflow.get('lastWorkflowSummary', '')}
- Review required: `{last_workflow.get('lastWorkflowRequiresReview', False)}`
- Security review required: `{last_workflow.get('lastWorkflowRequiresSecurityReview', False)}`
- Reducer result: `{last_workflow.get('lastReducerResultPath', 'not recorded')}`
- Harness result: `{last_workflow.get('lastHarnessResultPath', 'not recorded')}`

"""
    else:
        md += "No finalized workflow recorded.\n\n"
    md += f"""## Required reads

Minimal-first startup set: {required_read_bytes} bytes, approximately {required_read_tokens} tokens.

"""
    md += "\n".join(f"- `{p}`" for p in required_reads)
    if budget_demoted_reads:
        md += (
            "\n\nBudget-demoted to verify-on-demand pointers (open when the task needs them): "
            + ", ".join(f"`{p}`" for p in budget_demoted_reads)
            + "."
        )
    md += """

## Conditional reads

Load these only when the task scope requires deeper baseline/spec context or when escalation changes the profile.

"""
    md += "\n".join(f"- `{p}`" for p in conditional_reads) if conditional_reads else "No conditional reads configured."
    md += f"""

Conditional pack: {conditional_read_bytes} bytes, approximately {conditional_read_tokens} tokens.

## Universal baseline guidance

Apply the relevant files under `specs/00-universal/` to non-trivial changes. Use `specs/00-universal/structural-discovery.md` for repository search/impact mapping. Escalate to `review` or `security` when universal specs indicate higher risk.

## Guardrails

- Keep the diff minimal and scoped to the task.
- Do not load long historical files unless explicitly needed.
- Do not store canonical state in agent-specific directories.
- Return `HARNESS_RESULT` when done, partial, blocked, or failed.
- If scope/risk increases, request escalation instead of silently expanding work.

## Changed files from Git status

"""
    md += "\n".join(f"- `{p}`" for p in shown_changed_files) if shown_changed_files else "No changed files detected."
    if changed_files_truncated:
        md += f"\n- … truncated {len(files) - len(shown_changed_files)} additional changed file(s); inspect Git status directly if needed."
    return md + "\n"


def generate_handoff(
    *,
    root: Path,
    handoff_md: Path,
    handoff_json: Path,
    next_prompt: Path,
    project: dict[str, Any],
    task_state: dict[str, Any],
    workflow: dict[str, Any],
    quality: dict[str, Any],
    task_text: str,
    profile_name: str,
    profile: dict[str, Any],
    scores: dict[str, int],
    executor: str,
    files: list[str],
    commit: str | None,
    graph: dict[str, Any],
    workflow_budget: dict[str, Any],
    token_budget: dict[str, Any],
    build_prompt: Callable[[str, str], str],
    workflow_suggestion: Callable[..., dict[str, Any]],
    write_text: Callable[[Path, str], None],
    write_json: Callable[[Path, Any], None],
    now: Callable[[], str],
    append_event: Callable[[str, dict[str, Any] | None], None],
) -> dict[str, Any]:
    """Write handoff markdown/JSON/next prompt and return the JSON payload."""
    project_name = project.get("projectName") or workflow.get("project") or root.name
    handoff_budget = workflow_budget.get("handoffBudget", {}) if isinstance(workflow_budget.get("handoffBudget", {}), dict) else {}
    max_changed_files = int(handoff_budget.get("maxChangedFiles", 50) or 50)
    shown_changed_files = files[:max_changed_files]
    changed_files_truncated = len(files) > len(shown_changed_files)
    max_required_read_tokens = int(handoff_budget.get("maxRequiredReadTokens", 0) or 0)
    required_reads, conditional_reads, budget_demoted_reads = select_context_reads(
        root,
        profile_name,
        graph,
        max_required_read_tokens=max_required_read_tokens,
        token_budget=token_budget,
    )
    required_read_bytes = _file_bytes(root, required_reads)
    conditional_read_bytes = _file_bytes(root, conditional_reads)
    required_read_tokens = token_economics.estimate_tokens_from_chars(required_read_bytes, token_budget)
    conditional_read_tokens = token_economics.estimate_tokens_from_chars(conditional_read_bytes, token_budget)

    write_text(handoff_md, render_markdown(
        project=project,
        task_state=task_state,
        workflow=workflow,
        project_name=project_name,
        task_text=task_text,
        profile_name=profile_name,
        profile=profile,
        executor=executor,
        files=files,
        shown_changed_files=shown_changed_files,
        changed_files_truncated=changed_files_truncated,
        commit=commit,
        graph=graph,
        required_reads=required_reads,
        conditional_reads=conditional_reads,
        budget_demoted_reads=budget_demoted_reads,
        required_read_bytes=required_read_bytes,
        conditional_read_bytes=conditional_read_bytes,
        required_read_tokens=required_read_tokens,
        conditional_read_tokens=conditional_read_tokens,
    ))

    payload = {
        "schemaVersion": "1.0",
        "generatedAt": now(),
        "project": project_name,
        "task": task_text,
        "taskProfile": profile_name,
        "risk": profile.get("risk"),
        "executor": executor,
        "scores": scores,
        "changedFiles": shown_changed_files,
        "changedFilesTotal": len(files),
        "changedFilesTruncated": changed_files_truncated,
        "requiredReads": required_reads,
        "conditionalReads": conditional_reads,
        "contextBudget": {
            "strategy": "minimal-first",
            "maxChangedFiles": max_changed_files,
            "requiredReadBytes": required_read_bytes,
            "conditionalReadBytes": conditional_read_bytes,
            "estimatedRequiredReadTokens": required_read_tokens,
            "estimatedConditionalReadTokens": conditional_read_tokens,
            "maxRequiredReadTokens": max_required_read_tokens,
            "budgetDemotedReads": budget_demoted_reads,
        },
        "lastCommit": commit,
        "quality": _quality_summary(quality),
        "knowledgeGraph": graph,
        "workflowRecommendation": workflow_suggestion(task_text, files=files),
        "lastWorkflow": {
            "workflowId": workflow.get("lastWorkflowId"),
            "type": workflow.get("lastWorkflowType"),
            "status": workflow.get("lastWorkflowStatus", workflow.get("status")),
            "summary": workflow.get("lastWorkflowSummary"),
            "reducerResult": workflow.get("lastReducerResultPath"),
            "harnessResult": workflow.get("lastHarnessResultPath"),
            "requiresReview": workflow.get("lastWorkflowRequiresReview"),
            "requiresSecurityReview": workflow.get("lastWorkflowRequiresSecurityReview"),
        },
    }
    write_json(handoff_json, payload)
    write_text(next_prompt, build_prompt(task_text, profile_name) + "\n")
    append_event("handoff_generated", {"taskProfile": profile_name, "executor": executor, "changedFiles": len(files)})
    return payload

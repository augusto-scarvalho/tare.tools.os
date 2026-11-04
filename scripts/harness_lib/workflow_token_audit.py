"""Workflow token-audit report assembly and markdown rendering.

This module keeps token accounting/report formatting outside ``scripts/harness.py``
so the public CLI can stay a thin router while token economics remain testable as
an independent harness domain.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # direct self-check execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib import token_economics, context_digest
from harness_lib.common import read_text, write_json, write_text

StateArtifactPut = Callable[[str, str, Path], None]
StatePut = Callable[[str, str, dict[str, Any]], None]
NowFunc = Callable[[], str]


def _audit_workers(workers: list[dict[str, Any]], *, root: Path, base: Path,
                   target_root: Path | None, budget: dict[str, Any],
                   warn_ratio: float, max_output_budget: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per-worker prompt/result token rows; also annotates each worker in place.

    Returns (by_worker, totals) where totals carries the six aggregates
    audit_workflow folds into its summary."""
    by_worker: list[dict[str, Any]] = []
    t = {"promptChars": 0, "promptTokens": 0, "maxPromptTokensSeen": 0,
         "resultChars": 0, "resultTokens": 0, "maxResultTokensSeen": 0}
    for worker in workers:
        prompt_rel = str(worker.get("promptPath", ""))
        prompt_path = root / prompt_rel
        if not prompt_path.exists() and target_root is not None and (target_root / prompt_rel).exists():
            prompt_path = target_root / prompt_rel  # target-relative shard (QA.5)
        prompt_text = read_text(prompt_path)
        chars = len(prompt_text)
        tokens = token_economics.estimate_tokens_from_text(prompt_text, budget)
        t["promptChars"] += chars
        t["promptTokens"] += tokens
        t["maxPromptTokensSeen"] = max(t["maxPromptTokensSeen"], tokens)
        max_prompt = token_economics.token_budget_limit(budget, "maxWorkerPromptTokens", 3000)
        result_rel = str(worker.get("resultPath", ""))
        result_path = base / result_rel if result_rel else base / "workers" / f"{worker.get('workerId')}.result.json"
        result_text = read_text(result_path) if result_path.exists() else ""
        result_chars = len(result_text)
        result_tokens = token_economics.estimate_tokens_from_text(result_text, budget) if result_text else 0
        t["resultChars"] += result_chars
        t["resultTokens"] += result_tokens
        t["maxResultTokensSeen"] = max(t["maxResultTokensSeen"], result_tokens)
        result_status = token_economics.token_budget_status(result_tokens, max_output_budget, warn_ratio) if result_text else "missing"
        item = {
            "workerId": worker.get("workerId"),
            "unitId": worker.get("unitId"),
            "taskProfile": worker.get("taskProfile"),
            "promptPath": prompt_rel,
            "promptChars": chars,
            "estimatedPromptTokens": tokens,
            "maxPromptTokens": max_prompt,
            "promptTokenBudgetStatus": token_economics.token_budget_status(tokens, max_prompt, warn_ratio),
            "resultPath": result_rel,
            "resultPresent": bool(result_text),
            "resultChars": result_chars,
            "estimatedResultTokens": result_tokens,
            "maxResultTokens": max_output_budget,
            "resultTokenBudgetStatus": result_status,
            "scopeTruncated": bool(worker.get("scopeTruncated")),
            "promptTruncated": bool(worker.get("promptTruncated")),
        }
        by_worker.append(item)
        worker["promptChars"] = chars
        worker["estimatedPromptTokens"] = tokens
        worker["maxPromptTokens"] = max_prompt
        worker["promptTokenBudgetStatus"] = item["promptTokenBudgetStatus"]
        worker["estimatedResultTokens"] = result_tokens
        worker["resultTokenBudgetStatus"] = result_status
    return by_worker, t


def audit_workflow(
    *,
    root: Path,
    base: Path,
    workflow: dict[str, Any],
    budget: dict[str, Any],
    now: NowFunc,
    state_put_artifact: StateArtifactPut,
    state_put: StatePut,
    write_report: bool = True,
    update_workflow: bool = True,
    target_root: Path | None = None,
) -> dict[str, Any]:
    """Build, persist, and optionally mirror a workflow token-audit report.

    QA.5: targeted workflows (HT-T5) carry target-relative shard/prompt paths;
    resolution tries the harness root first, then target_root, so estimates
    stop silently zeroing for target workflows."""
    wfid = str(workflow.get("workflowId"))
    profile_name = workflow.get("workflowProfile")
    workflow_type = workflow.get("type")
    warn_ratio = float(budget.get("warnAtRatio", 0.8) or 0.8)
    workers = workflow.get("workers", []) if isinstance(workflow.get("workers", []), list) else []
    max_worker_output_chars = int(workflow.get("limits", {}).get("maxWorkerOutputChars", 4000) or 4000)
    max_worker_output_tokens = token_economics.estimate_tokens_from_chars(max_worker_output_chars, budget)
    max_output_budget = token_economics.token_budget_limit(budget, "maxWorkerOutputTokens", 1000)
    by_worker, worker_totals = _audit_workers(
        workers, root=root, base=base, target_root=target_root, budget=budget,
        warn_ratio=warn_ratio, max_output_budget=max_output_budget)
    total_worker_prompt_chars = worker_totals["promptChars"]
    total_worker_prompt_tokens = worker_totals["promptTokens"]
    max_worker_prompt_tokens_seen = worker_totals["maxPromptTokensSeen"]
    total_worker_result_chars = worker_totals["resultChars"]
    total_worker_result_tokens = worker_totals["resultTokens"]
    max_worker_result_tokens_seen = worker_totals["maxResultTokensSeen"]

    prompt_files = sorted((base / "reduce").glob("*.prompt.md"))
    reducer_prompts: list[dict[str, Any]] = []
    total_reducer_tokens = 0
    max_reducer = token_economics.token_budget_limit(budget, "maxReducerPromptTokens", 1500)
    max_reviewer = token_economics.token_budget_limit(budget, "maxReviewerPromptTokens", 1500)
    for prompt_path in prompt_files:
        text = read_text(prompt_path)
        tokens = token_economics.estimate_tokens_from_text(text, budget)
        total_reducer_tokens += tokens
        rel = str(prompt_path.relative_to(root))
        kind = "reviewer" if "review" in prompt_path.name else ("agentic-reducer" if "agent-reducer" in prompt_path.name else "reducer")
        limit = max_reviewer if kind == "reviewer" else max_reducer
        reducer_prompts.append({
            "path": rel,
            "kind": kind,
            "chars": len(text),
            "estimatedTokens": tokens,
            "maxTokens": limit,
            "tokenBudgetStatus": token_economics.token_budget_status(tokens, limit, warn_ratio),
        })

    reducer_outputs: list[dict[str, Any]] = []
    total_reducer_result_tokens = 0
    total_reducer_result_chars = 0
    for result_path in sorted((base / "reduce").glob("*.result.*")):
        if result_path.name.startswith("token-audit"):
            continue
        text = read_text(result_path)
        tokens = token_economics.estimate_tokens_from_text(text, budget)
        total_reducer_result_tokens += tokens
        total_reducer_result_chars += len(text)
        reducer_outputs.append({
            "path": str(result_path.relative_to(root)),
            "chars": len(text),
            "estimatedTokens": tokens,
        })

    # E1: required-reads token cost per worker/wave, with and without the wave-shared
    # digest, so the saving is a measured number (not counted in the packet — workers
    # read these files at the executor). digest present == an opted-in profile.
    reads = context_digest.required_reads(str(workflow_type))
    reads_chars = sum(len(read_text(root / r)) for r in reads if (root / r).exists())
    per_worker_without = token_economics.estimate_tokens_from_chars(reads_chars, budget)
    digest_path = base / context_digest.DIGEST_NAME
    digest_present = digest_path.exists()
    per_worker_with = (token_economics.estimate_tokens_from_text(read_text(digest_path), budget)
                       if digest_present else per_worker_without)
    n_workers = max(1, len(workers))
    required_reads_report = {
        "sources": reads,
        "digestPresent": digest_present,
        "digestPath": str(digest_path.relative_to(root)) if digest_present else None,
        "perWorkerWithoutDigest": per_worker_without,
        "perWorkerWithDigest": per_worker_with,
        "waveWithoutDigest": per_worker_without * n_workers,
        "waveWithDigest": per_worker_with * n_workers,
        "estimatedInputCutRatio": round(1 - (per_worker_with / per_worker_without), 4) if per_worker_without else 0.0,
    }

    total_planned_worker_output_tokens = max_worker_output_tokens * max(1, len(workers))
    total_planned_tokens = total_worker_prompt_tokens + total_reducer_tokens + total_planned_worker_output_tokens
    total_observed_tokens = total_worker_prompt_tokens + total_reducer_tokens + total_worker_result_tokens + total_reducer_result_tokens
    limits = {
        "maxWorkerPromptTokens": token_economics.token_budget_limit(budget, "maxWorkerPromptTokens", 3000),
        "maxTotalWorkerPromptTokens": token_economics.token_budget_limit(budget, "maxTotalWorkerPromptTokens", 24000),
        "maxReducerPromptTokens": max_reducer,
        "maxReviewerPromptTokens": max_reviewer,
        "maxWorkerOutputTokens": max_output_budget,
        "maxTotalPlannedTokens": token_economics.token_budget_limit(budget, "maxTotalPlannedTokens", 32000),
        "warnAtRatio": warn_ratio,
        "charsPerToken": token_economics.chars_per_token(budget),
        "estimationMode": str(budget.get("estimationMode", "chars-divisor")),
    }
    observed_worker_total_limit = max_output_budget * max(1, len(workers))
    checks = [
        {"name": "workerPrompt:max", "value": max_worker_prompt_tokens_seen, "limit": limits["maxWorkerPromptTokens"], "status": token_economics.token_budget_status(max_worker_prompt_tokens_seen, limits["maxWorkerPromptTokens"], warn_ratio)},
        {"name": "workerPrompt:total", "value": total_worker_prompt_tokens, "limit": limits["maxTotalWorkerPromptTokens"], "status": token_economics.token_budget_status(total_worker_prompt_tokens, limits["maxTotalWorkerPromptTokens"], warn_ratio)},
        {"name": "plannedWorkerOutput:perWorker", "value": max_worker_output_tokens, "limit": limits["maxWorkerOutputTokens"], "status": token_economics.token_budget_status(max_worker_output_tokens, limits["maxWorkerOutputTokens"], warn_ratio)},
        {"name": "plannedTokens:total", "value": total_planned_tokens, "limit": limits["maxTotalPlannedTokens"], "status": token_economics.token_budget_status(total_planned_tokens, limits["maxTotalPlannedTokens"], warn_ratio)},
        {"name": "observedWorkerResult:max", "value": max_worker_result_tokens_seen, "limit": limits["maxWorkerOutputTokens"], "status": token_economics.token_budget_status(max_worker_result_tokens_seen, limits["maxWorkerOutputTokens"], warn_ratio)},
        {"name": "observedWorkerResult:total", "value": total_worker_result_tokens, "limit": observed_worker_total_limit, "status": token_economics.token_budget_status(total_worker_result_tokens, observed_worker_total_limit, warn_ratio)},
    ]
    for prompt in reducer_prompts:
        checks.append({"name": f"{prompt['kind']}:prompt", "value": prompt["estimatedTokens"], "limit": prompt["maxTokens"], "status": prompt["tokenBudgetStatus"]})

    statuses = {str(c.get("status")) for c in checks}
    status = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    warnings = [f"{c['name']} {c['value']}/{c['limit']} tokens" for c in checks if c.get("status") in {"warn", "fail"}]
    summary = {
        "status": status,
        "workflowId": wfid,
        "workflowProfile": profile_name,
        "type": workflow_type,
        "workers": len(workers),
        "totalWorkerPromptChars": total_worker_prompt_chars,
        "totalWorkerPromptTokens": total_worker_prompt_tokens,
        "maxWorkerPromptTokensSeen": max_worker_prompt_tokens_seen,
        "avgWorkerPromptTokens": int(round(total_worker_prompt_tokens / len(workers))) if workers else 0,
        "totalReducerPromptTokens": total_reducer_tokens,
        "plannedWorkerOutputTokens": total_planned_worker_output_tokens,
        "totalPlannedTokens": total_planned_tokens,
        "totalWorkerResultChars": total_worker_result_chars,
        "totalWorkerResultTokens": total_worker_result_tokens,
        "maxWorkerResultTokensSeen": max_worker_result_tokens_seen,
        "avgWorkerResultTokens": int(round(total_worker_result_tokens / len(workers))) if workers else 0,
        "totalReducerResultChars": total_reducer_result_chars,
        "totalReducerResultTokens": total_reducer_result_tokens,
        "totalObservedTokens": total_observed_tokens,
        "maxTotalPlannedTokens": limits["maxTotalPlannedTokens"],
        "maxTotalWorkerPromptTokens": limits["maxTotalWorkerPromptTokens"],
        "maxWorkerOutputTokens": limits["maxWorkerOutputTokens"],
        "observedWorkerResultTotalLimit": observed_worker_total_limit,
        "requiredReads": required_reads_report,
        "warnings": warnings[:20],
    }
    try:  # observe-only projection calibration; ledger problems never fail the audit
        cal = profile_calibration(_cost_records(root), profile_name, total_planned_tokens)
    except Exception:  # noqa: BLE001
        cal = None
    if cal:
        summary["profileCalibration"] = cal
        if cal["medianObservedOverEst"] >= 3:
            summary["warnings"].append(
                f"estimator calibration: observed/est median {cal['medianObservedOverEst']}x "
                f"over {cal['samples']} {profile_name} run(s) — projected ~{cal['calibratedProjectedTokens']} tokens")
    report = {
        "schemaVersion": "1.1",
        "generatedAt": now(),
        "estimator": {"mode": limits["estimationMode"], "charsPerToken": limits["charsPerToken"]},
        "summary": summary,
        "budgets": {**limits, "observedWorkerResultTotalLimit": observed_worker_total_limit},
        "checks": checks,
        "requiredReads": required_reads_report,
        "workers": by_worker,
        "reducerPrompts": reducer_prompts,
        "reducerOutputs": reducer_outputs,
        "recommendations": recommendations(summary, checks),
    }
    if write_report:
        report_rel = str(budget.get("reportPath", "reduce/token-audit.json"))
        report_path = base / report_rel
        write_json(report_path, report)
        state_put_artifact(wfid, "token-audit", report_path)
        write_text(report_path.with_suffix(".md"), markdown(report))
        report["reportPath"] = str(report_path.relative_to(root))
        report["markdownReportPath"] = str(report_path.with_suffix(".md").relative_to(root))
    if update_workflow:
        workflow["workers"] = workers
        workflow["tokenAudit"] = {**summary, "reportPath": report.get("reportPath"), "markdownReportPath": report.get("markdownReportPath")}
        write_json(base / "workflow.json", workflow)
        state_put(wfid, "workflow", workflow)
    state_put(wfid, "token-audit", report)
    return report


def profile_calibration(records: list[dict[str, Any]], profile_name: str | None,
                        planned_total: int) -> dict[str, Any] | None:
    """Observed/estimated calibration for cost PROJECTION (wf-estimator-calibration,
    observe-only: budgets and routing untouched, human-gated). From cost-metrics
    workflow records of the SAME profile carrying BOTH estTokens and observedTokens,
    the median observed/est ratio scales totalPlannedTokens into a projection that
    stops lying by an order of magnitude (research-divergence measured 78x on
    WF-20260712-004425-895025: est 22,878 vs observed 1,796,816). None when there
    is no profile or fewer than 2 usable samples (one sample is an anecdote)."""
    if not profile_name:
        return None
    ratios: list[float] = []
    for r in records or []:
        if not (isinstance(r, dict) and r.get("kind") == "workflow"
                and r.get("profile") == profile_name):
            continue
        try:
            est, obs = float(r.get("estTokens")), float(r.get("observedTokens"))
        except (TypeError, ValueError):
            continue
        if est > 0 and obs > 0:
            ratios.append(obs / est)
    if len(ratios) < 2:
        return None
    med = statistics.median(ratios)
    return {
        "profile": profile_name,
        "samples": len(ratios),
        "medianObservedOverEst": round(med, 2),
        "calibratedProjectedTokens": int(round(planned_total * med)),
        "note": "projection only; budgets/routing unchanged (human-gated)",
    }


def _cost_records(root: Path) -> list[dict[str, Any]]:
    """cost-metrics records, fail-open (missing/corrupt ledger -> no calibration)."""
    try:
        data = json.loads((root / ".harness" / "state" / "cost-metrics.json").read_text(encoding="utf-8"))
        recs = data.get("records", [])
        return recs if isinstance(recs, list) else []
    except Exception:  # noqa: BLE001
        return []


def _calibration_line(cal: dict[str, Any] | None) -> str:
    if not isinstance(cal, dict):
        return ""
    return (f"- Calibrated projection: `~{cal.get('calibratedProjectedTokens')}` tokens "
            f"(observed/est median `{cal.get('medianObservedOverEst')}x` over "
            f"`{cal.get('samples')}` `{cal.get('profile')}` runs; projection only)")


def recommendations(summary: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    """Return actionable recommendations for warn/fail token-budget checks."""
    del summary  # Reserved for future recommendation context.
    recs: list[str] = []
    failed_or_warn = {str(c.get("name")): c for c in checks if c.get("status") in {"warn", "fail"}}
    if "workerPrompt:max" in failed_or_warn:
        recs.append("Reduce required-read boilerplate, lower maxPaths, or split oversized shards before running workers.")
    if "workerPrompt:total" in failed_or_warn:
        recs.append("Lower maxWorkers for this profile or use changed-files/Graphify partitioning instead of broad roots.")
    if "plannedWorkerOutput:perWorker" in failed_or_warn:
        recs.append("Lower maxWorkerOutputChars or require finding-only output for this workflow profile.")
    if "plannedTokens:total" in failed_or_warn:
        recs.append("Reduce worker count, shrink output budget, or run profiles in smaller batches before reducing.")
    if "observedWorkerResult:max" in failed_or_warn:
        recs.append("Tighten WORKER_RESULT compactness requirements or lower maxWorkerOutputChars for verbose workers.")
    if "observedWorkerResult:total" in failed_or_warn:
        recs.append("Reduce fan-out or require more structured findings-only worker outputs before reduction.")
    if not recs:
        recs.append("No token-budget action required for this workflow plan.")
    return recs


def _required_reads_line(rr: dict[str, Any] | None) -> str:
    """One Markdown line for the E1 required-reads with/without-digest saving."""
    if not isinstance(rr, dict):
        return ""
    if rr.get("digestPresent"):
        return (f"- Required reads (wave): `{rr.get('waveWithDigest')}` with digest vs "
                f"`{rr.get('waveWithoutDigest')}` without "
                f"(cut `{round(float(rr.get('estimatedInputCutRatio') or 0) * 100, 1)}%`)")
    return (f"- Required reads (wave): `{rr.get('waveWithoutDigest')}` tokens "
            "(no wave-shared digest for this profile)")


def markdown(report: dict[str, Any]) -> str:
    """Render a stable Markdown companion for a token-audit JSON report."""
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    budgets = report.get("budgets", {}) if isinstance(report.get("budgets"), dict) else {}
    checks = report.get("checks", []) if isinstance(report.get("checks", []), list) else []
    workers = report.get("workers", []) if isinstance(report.get("workers", []), list) else []
    lines = [
        "# Workflow Token Audit",
        "",
        f"- Workflow: `{summary.get('workflowId')}`",
        f"- Profile: `{summary.get('workflowProfile')}`",
        f"- Type: `{summary.get('type')}`",
        f"- Status: `{summary.get('status')}`",
        f"- Estimator: `{report.get('estimator', {}).get('mode')}` at `{report.get('estimator', {}).get('charsPerToken')}` chars/token",
        "",
        "## Summary",
        "",
        f"- Workers: `{summary.get('workers')}`",
        f"- Total worker prompt tokens: `{summary.get('totalWorkerPromptTokens')}` / `{budgets.get('maxTotalWorkerPromptTokens')}`",
        f"- Max worker prompt tokens: `{summary.get('maxWorkerPromptTokensSeen')}` / `{budgets.get('maxWorkerPromptTokens')}`",
        f"- Avg worker prompt tokens: `{summary.get('avgWorkerPromptTokens')}`",
        f"- Reducer prompt tokens: `{summary.get('totalReducerPromptTokens')}`",
        f"- Planned worker output tokens: `{summary.get('plannedWorkerOutputTokens')}`",
        f"- Total planned tokens: `{summary.get('totalPlannedTokens')}` / `{budgets.get('maxTotalPlannedTokens')}`",
        f"- Observed worker result tokens: `{summary.get('totalWorkerResultTokens')}` / `{budgets.get('observedWorkerResultTotalLimit')}`",
        f"- Reducer/reviewer result tokens: `{summary.get('totalReducerResultTokens')}`",
        f"- Total observed tokens: `{summary.get('totalObservedTokens')}`",
        _calibration_line(summary.get("profileCalibration")),
        _required_reads_line(summary.get("requiredReads")),
        "",
        "## Checks",
        "",
        "| Check | Status | Value | Limit |",
        "|---|---:|---:|---:|",
    ]
    for check in checks:
        lines.append(f"| `{check.get('name')}` | `{check.get('status')}` | `{check.get('value')}` | `{check.get('limit')}` |")
    lines.extend(["", "## Largest worker prompts", "", "| Worker | Tokens | Budget status | Prompt |", "|---|---:|---:|---|"])
    for worker in sorted(workers, key=lambda x: int(x.get("estimatedPromptTokens", 0) or 0), reverse=True)[:10]:
        lines.append(f"| `{worker.get('workerId')}` | `{worker.get('estimatedPromptTokens')}` | `{worker.get('promptTokenBudgetStatus')}` | `{worker.get('promptPath')}` |")
    lines.extend(["", "## Observed worker result output", "", "| Worker | Present | Tokens | Budget status | Result |", "|---|---:|---:|---:|---|"])
    for worker in sorted(workers, key=lambda x: int(x.get("estimatedResultTokens", 0) or 0), reverse=True)[:10]:
        lines.append(f"| `{worker.get('workerId')}` | `{worker.get('resultPresent')}` | `{worker.get('estimatedResultTokens')}` | `{worker.get('resultTokenBudgetStatus')}` | `{worker.get('resultPath')}` |")
    lines.extend(["", "## Recommendations", ""] + [f"- {item}" for item in report.get("recommendations", [])])
    return "\n".join(lines) + "\n"


def _self_check() -> int:
    recs = [
        {"kind": "workflow", "profile": "research-divergence", "estTokens": 20000, "observedTokens": 1600000},
        {"kind": "workflow", "profile": "research-divergence", "estTokens": 10000, "observedTokens": 700000},
        {"kind": "workflow", "profile": "research-divergence", "estTokens": "bad", "observedTokens": 1},
        {"kind": "workflow", "profile": "other", "estTokens": 100, "observedTokens": 100},
        {"kind": "gate", "profile": "research-divergence", "estTokens": 1, "observedTokens": 999},
    ]
    cal = profile_calibration(recs, "research-divergence", 21749)
    assert cal and cal["samples"] == 2, cal                       # bad row + other profile + non-workflow excluded
    assert cal["medianObservedOverEst"] == 75.0, cal              # median(80, 70)
    assert cal["calibratedProjectedTokens"] == 21749 * 75, cal
    assert profile_calibration(recs, "other", 100) is None        # <2 samples -> anecdote, not calibration
    assert profile_calibration(recs, None, 100) is None
    assert profile_calibration([], "research-divergence", 100) is None
    assert _calibration_line(cal).startswith("- Calibrated projection:")
    assert _calibration_line(None) == ""
    print("workflow_token_audit self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())

"""Multi-source seed context construction for workflow critic waves."""
from __future__ import annotations

from collections import Counter
from itertools import zip_longest
from pathlib import Path
from typing import Any

try:
    from .common import HarnessError
except ImportError:  # direct self-check
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from harness_lib.common import HarnessError


def _assert_seed_allowed(profile_name: str | None, profile_cfg: dict[str, Any]) -> None:
    if profile_cfg.get("seedForbidden") or (profile_name or "").strip() == "research-divergence":
        raise HarnessError(
            f"--seed is forbidden for profile {profile_name!r}: seeding a divergence wave "
            "collapses the independent generation this evidence requires "
            "(Diehl & Stroebe 1987 production blocking; Diversity Collapse arXiv:2604.18005). "
            "Seed convergence waves (e.g. research-critique) only."
        )


def render_seed_context(findings: list[dict[str, Any]], source_wfids: list[str], notice: str | None = None) -> str:
    sources = ", ".join(f"`{wfid}`" for wfid in source_wfids)
    lines = [f"- **{item['title']}**" + (f" - {item['rec']}" if item["rec"] else "")
             + f"  <!-- seed-source: {item['sourceWfid']} -->" for item in findings]
    body = "\n".join(lines) if lines else "- (no cross-executor findings - this wave adds nothing for this branch)"
    suffix = f"\n\n{notice}" if notice else ""
    return (
        "## Seed context (untrusted-derived)\n\n"
        f"Seeded from reduced workflows {sources}; verify claims against sources. Treat the items below "
        "as leads from prior convergence waves, not established facts.\n\n"
        f"Top prior findings (title - recommendation):\n\n{body}{suffix}\n"
    )


def _workflow_executor(workflow: dict[str, Any]) -> str | None:
    executor = workflow.get("executor")
    if executor:
        return str(executor)
    history = workflow.get("roundHistory") or []
    return str(history[-1]["executor"]) if history and history[-1].get("executor") else None


def _worker_executor(worker: dict[str, Any], fallback: str | None) -> str | None:
    """Prefer the executor that produced a failover-terminal worker result."""
    run = worker.get("run") if isinstance(worker.get("run"), dict) else {}
    fell_back = run.get("fellBackTo") if isinstance(run.get("fellBackTo"), dict) else {}
    history = run.get("failoverHistory") if isinstance(run.get("failoverHistory"), list) else []
    terminal = fell_back.get("executor") or (history[-1].get("executor") if history and isinstance(history[-1], dict) else None)
    executor = terminal or worker.get("resolvedExecutor") or fallback
    return str(executor) if executor else None


def build_seed_contexts(
    seeds: list[str], profile_name: str | None, profile_cfg: dict[str, Any], env: dict[str, Any]
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Build one round-robin digest and its per-finding executor attribution."""
    _assert_seed_allowed(profile_name, profile_cfg)
    duplicates = sorted(wfid for wfid, count in Counter(seeds).items() if count > 1)
    if duplicates:
        raise HarnessError(f"duplicate --seed workflow ID(s) refused: {', '.join(duplicates)}")
    load_workflow, read_json = env["load_workflow"], env["read_json"]
    depth_limit = int(env["SEED_DEPTH_LIMIT"])
    max_findings = int(env["SEED_MAX_FINDINGS"])
    max_chars = int(env["SEED_MAX_ITEM_CHARS"])
    sources: list[dict[str, Any]] = []
    source_findings: list[list[dict[str, Any]]] = []

    for wfid in seeds:
        base, workflow = load_workflow(wfid)
        reducer = read_json(base / "reduce" / "reducer.result.json", None)
        if not reducer:
            raise HarnessError(
                f"seed workflow {wfid} has no reduce/reducer.result.json - "
                f"run `python scripts/harness.py workflow reduce {wfid}` first"
            )
        prior_depth = int((workflow.get("seed") or {}).get("depth", 0) or 0)
        source_executor = _workflow_executor(workflow)
        worker_executors = {
            str(worker.get("workerId")): _worker_executor(worker, source_executor)
            for worker in (workflow.get("workers") or [])
            if worker.get("workerId") and _worker_executor(worker, source_executor)
        }
        indexed: list[dict[str, Any]] = []
        for finding in reducer.get("dedupedFindings") or []:
            if not isinstance(finding, dict):
                continue
            executors = {source_executor} if source_executor else set()
            executors.update(worker_executors[wid] for wid in map(str, finding.get("sourceWorkerIds") or [])
                             if wid in worker_executors)
            indexed.append({
                "sourceWfid": wfid,
                "title": " ".join(str(finding.get("title", "untitled")).split())[:max_chars],
                "rec": " ".join(str(finding.get("recommendation", "")).split())[:max_chars],
                "executors": executors,
            })
        sources.append({"workflowId": wfid, "depth": prior_depth, "executor": source_executor,
                         "sourceExecutors": sorted(set(worker_executors.values()))})
        source_findings.append(indexed)

    deepest = max(sources, key=lambda item: item["depth"])
    depth = int(deepest["depth"]) + 1
    if depth > depth_limit:
        raise HarnessError(
            f"--seed depth {depth} exceeds bound {depth_limit}: deepest source {deepest['workflowId']} "
            f"is depth-{deepest['depth']} (seeded-from-seeded chains are capped to keep waves grounded)"
        )
    selected: list[dict[str, Any]] = []
    for group in zip_longest(*source_findings):
        for finding in group:
            if finding is not None:
                selected.append(finding)
                if len(selected) == max_findings:
                    break
        if len(selected) == max_findings:
            break

    distribution = {wfid: 0 for wfid in seeds}
    attributed = {wfid: set() for wfid in seeds}
    for finding in selected:
        distribution[finding["sourceWfid"]] += 1
        attributed[finding["sourceWfid"]].update(finding["executors"])
    for source in sources:
        source["executors"] = sorted(attributed[source["workflowId"]])
    meta = {"workflowIds": list(seeds), "depth": depth, "sources": sources, "distribution": distribution}
    return render_seed_context(selected, seeds), meta, selected


def _self_check() -> None:
    workflows = {
        "WF-A": {"workers": [{"workerId": "worker-001", "resolvedExecutor": "claude",
                                "run": {"fellBackTo": {"executor": "codex"}}},
                               {"workerId": "worker-002", "resolvedExecutor": "claude",
                                "run": {"failoverHistory": [{"executor": "codex"}, {"executor": "nvidia-compat"}]}}]},
        "WF-B": {"seed": {"workflowId": "WF-X", "depth": 1}, "executor": "codex", "workers": []},
    }
    reducers = {
        "WF-A": {"dedupedFindings": [{"title": f"A{i}", "recommendation": "a", "sourceWorkerIds": ["worker-001"]} for i in range(10)]},
        "WF-B": {"dedupedFindings": [{"title": f"B{i}", "recommendation": "b"} for i in range(10)]},
    }
    env = {
        "load_workflow": lambda wfid: (Path(wfid), workflows[wfid]),
        "read_json": lambda path, default: reducers.get(path.parts[0], default),
        "SEED_DEPTH_LIMIT": 2, "SEED_MAX_FINDINGS": 12, "SEED_MAX_ITEM_CHARS": 240,
    }
    text, meta, findings = build_seed_contexts(["WF-A", "WF-B"], "research-critique", {}, env)
    assert [item["sourceWfid"] for item in findings] == ["WF-A", "WF-B"] * 6
    assert meta["distribution"] == {"WF-A": 6, "WF-B": 6} and meta["depth"] == 2
    assert text.count("<!-- seed-source:") == 12
    # P6 INV-1 fix #2: sourceExecutors is the FULL worker-level set (terminal executors of
    # every WF-A worker: codex + nvidia-compat), independent of which findings got selected.
    by_wfid = {source["workflowId"]: source for source in meta["sources"]}
    assert by_wfid["WF-A"]["sourceExecutors"] == ["codex", "nvidia-compat"]
    assert by_wfid["WF-B"]["sourceExecutors"] == []  # no workers -> nothing produced material
    assert _worker_executor(workflows["WF-A"]["workers"][0], None) == "codex"
    assert _worker_executor(workflows["WF-A"]["workers"][1], None) == "nvidia-compat"
    try:
        build_seed_contexts(["WF-A", "WF-A"], "research-critique", {}, env)
    except HarnessError as exc:
        assert "duplicate --seed" in str(exc) and "WF-A" in str(exc)
    else:
        raise AssertionError("duplicate seeds were not refused")
    try:
        build_seed_contexts(["WF-A", "WF-B"], "research-divergence", {}, env)
    except HarnessError as exc:
        assert "Diehl" in str(exc) and "Diversity Collapse" in str(exc)
    else:
        raise AssertionError("divergence seed was not refused")
    print("seed_context self-check ok")


if __name__ == "__main__":
    _self_check()

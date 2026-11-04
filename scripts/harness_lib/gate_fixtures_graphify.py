"""Gate fixtures — graphify domain (SPEC — MF.2 extraction).

Moved out of scripts/spec_test_gate.py to keep the gate runner readable
and cheap to load; shared gate helpers arrive via bind(env) from the
gate at import time (same idiom as harness_lib.workflow_reduce).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from spec_test_gate."""
    globals().update(env)


def check_graphify_adapter_fixture() -> list[dict[str, str]]:
    """Validate Graphify graph adapter/normalizer shapes with deterministic local fixtures."""
    cleanup_test_artifacts()
    graph_dir = ROOT / "graphify-out"
    graph_path = graph_dir / "graph.json"
    original_graph = graph_path.read_text(encoding="utf-8") if graph_path.exists() else None
    out: list[dict[str, str]] = []
    wfids: list[str] = []
    sample_paths = [
        "AGENTS.md",
        "docs/AGENTIC_WORKFLOWS.md",
        "scripts/harness.py",
        "schemas/workflow.schema.json",
    ]
    samples: list[tuple[str, dict[str, Any], set[str], str]] = [
        (
            "communities-dict",
            {
                "communities": {
                    "docs": ["docs/AGENTIC_WORKFLOWS.md"],
                    "runtime": ["scripts/harness.py"],
                }
            },
            {"graphify-communities"},
            "used",
        ),
        (
            "node-metadata",
            {
                "nodes": [
                    {"id": "AGENTS.md", "path": "AGENTS.md", "community": "root"},
                    {"id": "docs/AGENTIC_WORKFLOWS.md", "path": "docs/AGENTIC_WORKFLOWS.md", "community": "docs"},
                    {"id": "scripts/harness.py", "path": "scripts/harness.py", "module": "runtime"},
                ],
                "edges": [
                    {"source": "AGENTS.md", "target": "docs/AGENTIC_WORKFLOWS.md"},
                    {"source": "AGENTS.md", "target": "scripts/harness.py"},
                ],
            },
            {"graphify-communities"},
            "used",
        ),
        (
            "edge-path-fallback",
            {
                "edges": [
                    {"source": "AGENTS.md", "target": "docs/AGENTIC_WORKFLOWS.md"},
                    {"from": "scripts/harness.py", "to": "schemas/workflow.schema.json"},
                ]
            },
            {"graphify-roots-fallback"},
            "used",
        ),
        (
            "unknown-schema-fallback",
            {"metadata": {"shape": "unsupported"}, "items": ["not-a-path"]},
            {"fallback-roots"},
            "fallback",
        ),
    ]
    try:
        graph_dir.mkdir(parents=True, exist_ok=True)
        for name, graph, expected_partitioning, expected_status in samples:
            try:
                write_json(graph_path, graph)
                payload = call_harness_function(
                    "plan_workflow",
                    f"graphify adapter fixture {name}",
                    "map-reduce",
                    split="graphify",
                    max_workers=4,
                    concurrency=1,
                )
                wfid = str(payload.get("workflowId"))
                wfids.append(wfid)
                mark_fixture_workflow(wfid, "graphify-adapter")
                workflow = read_json(HARNESS / "workflows" / "active" / wfid / "workflow.json", {}) or {}
                meta = workflow.get("graphifyPartitioning", {}) if isinstance(workflow.get("graphifyPartitioning"), dict) else {}
                workers = workflow.get("workers", []) if isinstance(workflow.get("workers"), list) else []
                partitioning = str(meta.get("partitioning") or meta.get("actualStrategy") or "")
                graph_status = str(meta.get("graphStatus") or "")
                normalizer = meta.get("graphNormalizer", {}) if isinstance(meta.get("graphNormalizer"), dict) else {}
                covered_paths: set[str] = set()
                for worker in workers:
                    if not isinstance(worker, dict):
                        continue
                    direct_paths = worker.get("paths") or []
                    if isinstance(direct_paths, list):
                        covered_paths.update(str(path) for path in direct_paths)
                    scope_path = worker.get("scopePath")
                    if isinstance(scope_path, str):
                        scope = read_json(ROOT / scope_path, {}) or {}
                        if isinstance(scope, dict) and isinstance(scope.get("paths"), list):
                            covered_paths.update(str(path) for path in scope.get("paths") or [])
                expected_paths = set() if name == "unknown-schema-fallback" else set(
                    p for p in sample_paths if p in json.dumps(graph)
                )
                ok = (
                    partitioning in expected_partitioning
                    and graph_status == expected_status
                    and bool(workers)
                    and bool(normalizer.get("adapter"))
                    and (not expected_paths or expected_paths.issubset(covered_paths))
                )
                detail = f"partitioning={partitioning}; graphStatus={graph_status}; adapter={normalizer.get('adapter')}; workers={len(workers)}"
                if expected_paths and not expected_paths.issubset(covered_paths):
                    detail += f"; missingPaths={sorted(expected_paths - covered_paths)}"
                out.append(result(f"workflow:graphify-adapter-fixture:{name}", "pass" if ok else "fail", detail))
            except Exception as exc:
                out.append(result(f"workflow:graphify-adapter-fixture:{name}", "fail", str(exc)[:300]))
        return out
    finally:
        for wfid in wfids:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        if original_graph is None:
            shutil.rmtree(graph_dir, ignore_errors=True)
        else:
            graph_path.parent.mkdir(parents=True, exist_ok=True)
            graph_path.write_text(original_graph, encoding="utf-8")
        cleanup_test_artifacts()

def check_graphify_code_ast_fixture() -> list[dict[str, str]]:
    """Verify Graphify code AST-first graph generation is offline, usable by Graphify adapter, and budget-policy wired."""
    cleanup_test_artifacts()
    graph_dir = ROOT / "graphify-out"
    original_graph = (graph_dir / "graph.json").read_bytes() if (graph_dir / "graph.json").exists() else None
    original_report = (graph_dir / "GRAPH_REPORT.md").read_bytes() if (graph_dir / "GRAPH_REPORT.md").exists() else None
    out: list[dict[str, str]] = []
    wfids: list[str] = []
    try:
        shutil.rmtree(graph_dir, ignore_errors=True)
        meta = graphify_code_ast.build_graphify_code_ast(ROOT, output_dir=graph_dir, max_files=300)
        graph = read_json(graph_dir / "graph.json", {}) or {}
        report = (graph_dir / "GRAPH_REPORT.md").read_text(encoding="utf-8", errors="ignore") if (graph_dir / "GRAPH_REPORT.md").exists() else ""
        nodes = graph.get("nodes") if isinstance(graph, dict) else []
        edges = graph.get("edges") if isinstance(graph, dict) else []
        out.append(result("graphify:graphify-code-ast-build", "pass" if graph.get("source") == "graphify-code-ast" and nodes else "fail", f"filesParsed={meta.get('filesParsed')}; edges={meta.get('edges')}"))
        out.append(result("graphify:graphify-code-ast-report", "pass" if "Graphify Code AST Graph Report" in report and "source-verified" in report else "fail", "report generated" if report else "missing"))
        payload = call_harness_function("plan_workflow", "local ast graph fixture", "map-reduce", split="graphify", max_workers=4, concurrency=1)
        wfid = str(payload.get("workflowId"))
        wfids.append(wfid)
        mark_fixture_workflow(wfid, "graphify-code-ast")
        workflow = read_json(HARNESS / "workflows" / "active" / wfid / "workflow.json", {}) or {}
        meta2 = workflow.get("graphifyPartitioning", {}) if isinstance(workflow.get("graphifyPartitioning"), dict) else {}
        ok_partition = str(meta2.get("graphStatus")) == "used" and str(meta2.get("graphAdapter")) in {"graphify-communities", "graphify-nodes-edges", "graphify-nodes"}
        out.append(result("graphify:graphify-code-ast-partition", "pass" if ok_partition else "fail", f"graphStatus={meta2.get('graphStatus')}; adapter={meta2.get('graphAdapter')}; partitioning={meta2.get('partitioning')}"))
        project = project_config()
        kg = project.get("knowledgeGraph", {}) if isinstance(project.get("knowledgeGraph"), dict) else {}
        llm = kg.get("llmAssisted", {}) if isinstance(kg.get("llmAssisted"), dict) else {}
        no_network_default = llm.get("provider") == "gemini-api" and llm.get("usagePolicy") == "free-tier-only" and llm.get("networkDefault") is False and llm.get("allowPaidTier") is False and llm.get("inputKinds") == ["text", "image"] and llm.get("sourceVerificationRequired") is True
        out.append(result("graphify:gemini-text-image-offline-default", "pass" if no_network_default else "fail", json.dumps(llm, sort_keys=True)))
        return out
    except Exception as exc:
        out.append(result("graphify:graphify-code-ast-fixture", "fail", str(exc)[:300]))
        return out
    finally:
        for wfid in wfids:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        shutil.rmtree(graph_dir, ignore_errors=True)
        if original_graph is not None or original_report is not None:
            graph_dir.mkdir(parents=True, exist_ok=True)
            if original_graph is not None:
                (graph_dir / "graph.json").write_bytes(original_graph)
            if original_report is not None:
                (graph_dir / "GRAPH_REPORT.md").write_bytes(original_report)
        cleanup_test_artifacts()

def check_graphify_api_providers_fixture() -> list[dict[str, str]]:
    """SPEC-107: cheap discovery wrapper — config shape, provider selftests, and no-network refusal paths."""
    out: list[dict[str, str]] = []
    kg = project_config().get("knowledgeGraph", {}) if isinstance(project_config().get("knowledgeGraph"), dict) else {}
    providers = kg.get("apiAssistedProviders", {}) if isinstance(kg.get("apiAssistedProviders"), dict) else {}
    nvidia = providers.get("nvidia", {}) if isinstance(providers.get("nvidia"), dict) else {}
    order_ok = providers.get("order") == ["gemini-api", "nvidia-build"]
    out.append(result("graphify-api-providers:order", "pass" if order_ok else "fail", ", ".join(providers.get("order", [])) if order_ok else f"expected knowledgeGraph.apiAssistedProviders.order=[gemini-api, nvidia-build] in .harness/project.json; got {providers.get('order')!r}"))
    nvidia_ok = (
        nvidia.get("networkDefault") is False and nvidia.get("allowPaidTier") is False
        and nvidia.get("requiresEnv") == ["NVIDIA_API_KEY"]
        and "source-code-ast-generation" in (nvidia.get("forbiddenInputKinds") or [])
        and nvidia.get("sourceVerificationRequired") is True
    )
    out.append(result("graphify-api-providers:nvidia-policy", "pass" if nvidia_ok else "fail", "opt-in, no paid tier, env-key, code-graphing forbidden" if nvidia_ok else f"expected networkDefault/allowPaidTier=false, requiresEnv=[NVIDIA_API_KEY], source-code-ast-generation forbidden; got {json.dumps(nvidia, sort_keys=True)[:300]}"))
    for name, cmd in (
        ("openai-compat-selftest", [sys.executable, "tools/graphify/openai_compat.py"]),
        ("nvidia-selftest", [sys.executable, "tools/graphify/nvidia_extract_fallback.py", "--selftest"]),
        ("gemini-selftest", [sys.executable, "tools/graphify/gemini_extract_fallback.py", "--selftest"]),
    ):
        proc = run_fixture_command(cmd, cwd=ROOT, timeout=fixture_timeout())
        out.append(result(f"graphify-api-providers:{name}", "pass" if proc.returncode == 0 else "fail", "ok" if proc.returncode == 0 else (proc.stderr or proc.stdout)[-300:]))
    stripped_env = {k: v for k, v in os.environ.items() if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY"}}
    stripped_env["HARNESS_NO_VAULT"] = "1"  # a real operator VAULT must not turn this refusal test green
    proc = run_fixture_command([sys.executable, "scripts/harness.py", "discover", "README.md"], cwd=ROOT, timeout=fixture_timeout(), env=stripped_env)
    refused_ok = False
    detail = "discover did not run"
    if proc.returncode == 0:
        try:
            payload = json.loads(proc.stdout or "{}")
            entry = next((r for r in payload.get("results", []) if r.get("path") == "README.md"), {})
            reason = str(entry.get("reason", ""))
            refused_ok = entry.get("status") == "refused" and "cause:" in reason and "fix:" in reason and "coder LLM" in reason
            detail = "refusal is self-announcing (cause+fix, no coder-LLM fallback)" if refused_ok else f"got status={entry.get('status')!r} reason={reason[:200]!r}"
        except json.JSONDecodeError:
            detail = "discover stdout is not JSON"
    else:
        detail = (proc.stderr or "")[-300:]
    out.append(result("graphify-api-providers:refusal-path", "pass" if refused_ok else "fail", detail))
    return out

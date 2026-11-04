"""Executable software-engineering guardrails for harness maintainability.

The harness is allowed to keep ``scripts/harness.py`` as the stable public CLI,
but product hardening must not regress into a single unbounded file or an
unbounded startup context pack.  This module keeps those limits data-driven so
validation gates can enforce the engineering spec without importing the runtime
CLI or executing workflows.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from harness_lib import baseline_catalog

DEFAULT_CONFIG = {
    "schemaVersion": "1.0",
    "maxHarnessLines": 5200,
    "maxSpecTestGateLines": 3200,
    "maxFunctionLines": 220,
    "maxLibFileLines": 900,
    "maxRequiredReadTokens": 5000,
    "maxRootHandoffLines": 24,
    "requiredModules": [
        "common.py",
        "token_economics.py",
        "workflow_token_audit.py",
        "workflow_reduce.py",
        "workflow_plan.py",
        "processes.py",
        "graphify_adapter.py",
        "handoff.py",
        "workflow_ids.py",
        "workflow_state.py",
        "tracing.py",
        "state_store.py",
        "observability_exporters.py",
        "engineering_guardrails.py",
        "baseline_catalog.py",
        "graphify_code_ast.py",
        "protected_files.py",
        "deprecation_hygiene.py",
        "fixture_liveness.py",
        "controlled_writes.py",
    ],
    "requiredSpecs": [
        "specs/00-universal/software-engineering-guardrails.md",
        "specs/00-universal/canonical-file-protection.md",
    ],
    "requiredBaselineDocs": [
        "specs/00-universal/UNIVERSAL_BASELINE.md",
    ],
    "requiredReferenceAnchors": [
        "OWASP ASVS 5.0.0",
        "OWASP Top 10 2025",
        "OWASP API Security Top 10 2023",
        "OWASP Top 10 for LLM Applications 2025",
        "OWASP SCVS",
        "OWASP SAMM",
        "NIST SSDF SP 800-218",
        "Graphify code AST-first structural discovery",
        "Gemini API text/image free tier only",
        "Canonical agent file protection",
    ],
}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    except Exception:
        return 0


def largest_functions(path: Path) -> list[tuple[str, int, int]]:
    """Return ``(name, start_line, length)`` for top-level and nested functions."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    rows: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            rows.append((node.name, node.lineno, int(end) - int(node.lineno) + 1))
    rows.sort(key=lambda item: item[2], reverse=True)
    return rows


def load_config(root: Path) -> dict[str, Any]:
    configured = read_json(root / "testing" / "engineering-guardrails.json", {}) or {}
    merged = dict(DEFAULT_CONFIG)
    for key, value in configured.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def evaluate(root: Path) -> list[dict[str, str]]:
    """Evaluate executable engineering guardrails for the harness tree.

    The return shape intentionally matches ``scripts/spec_test_gate.py`` result
    records without depending on that module.
    """
    config = load_config(root)
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    harness_path = root / "scripts" / "harness.py"
    gate_path = root / "scripts" / "spec_test_gate.py"
    harness_lines = line_count(harness_path)
    gate_lines = line_count(gate_path)
    add(
        "engineering-guardrails:harness-line-budget",
        harness_lines <= int(config.get("maxHarnessLines", 0) or 0),
        f"lines={harness_lines}; max={config.get('maxHarnessLines')}",
    )
    add(
        "engineering-guardrails:spec-gate-line-budget",
        gate_lines <= int(config.get("maxSpecTestGateLines", 0) or 0),
        f"lines={gate_lines}; max={config.get('maxSpecTestGateLines')}",
    )

    max_function_lines = int(config.get("maxFunctionLines", 0) or 0)
    oversized: list[str] = []
    for rel in ["scripts/harness.py", "scripts/spec_test_gate.py", *[f"scripts/harness_lib/{name}" for name in config.get("requiredModules", []) or []]]:
        path = root / rel
        if not path.exists() or path.suffix != ".py":
            continue
        for name, line, size in largest_functions(path):
            if size > max_function_lines:
                oversized.append(f"{rel}:{name}@{line}={size}")
    add(
        "engineering-guardrails:function-size-budget",
        not oversized,
        "largest functions within budget" if not oversized else "; ".join(oversized[:10]),
    )

    missing_modules = [name for name in config.get("requiredModules", []) or [] if not (root / "scripts" / "harness_lib" / str(name)).exists()]
    add(
        "engineering-guardrails:required-modules",
        not missing_modules,
        "present" if not missing_modules else "missing: " + ", ".join(missing_modules),
    )

    missing_specs = [rel for rel in config.get("requiredSpecs", []) or [] if not (root / str(rel)).exists()]
    add(
        "engineering-guardrails:required-specs",
        not missing_specs,
        "present" if not missing_specs else "missing: " + ", ".join(missing_specs),
    )

    manifest_text = (root / "specs" / "MANIFEST.yaml").read_text(encoding="utf-8", errors="ignore") if (root / "specs" / "MANIFEST.yaml").exists() else ""
    project = read_json(root / ".harness" / "project.json", {}) or {}
    project_specs = set(((project.get("universalSpecs") or {}).get("required") or []) if isinstance(project.get("universalSpecs"), dict) else [])
    spec_wiring_errors: list[str] = []
    for rel in config.get("requiredSpecs", []) or []:
        if rel not in manifest_text:
            spec_wiring_errors.append(f"manifest missing {rel}")
        if rel not in project_specs:
            spec_wiring_errors.append(f"project missing {rel}")
    add(
        "engineering-guardrails:spec-wiring",
        not spec_wiring_errors,
        "manifest/project list guardrail spec" if not spec_wiring_errors else "; ".join(spec_wiring_errors),
    )

    handoff = read_json(root / ".harness" / "handoff" / "handoff.json", {}) or {}
    budget = handoff.get("contextBudget", {}) if isinstance(handoff.get("contextBudget"), dict) else {}
    required_tokens = int(budget.get("estimatedRequiredReadTokens", 0) or 0)
    max_required_tokens = int(config.get("maxRequiredReadTokens", 0) or 0)
    add(
        "engineering-guardrails:handoff-required-token-budget",
        required_tokens > 0 and required_tokens <= max_required_tokens,
        f"requiredTokens={required_tokens}; max={max_required_tokens}",
    )
    root_handoff_lines = line_count(root / "AGENT_HANDOFF.md")
    add(
        "engineering-guardrails:root-handoff-shim",
        root_handoff_lines > 0 and root_handoff_lines <= int(config.get("maxRootHandoffLines", 0) or 0),
        f"lines={root_handoff_lines}; max={config.get('maxRootHandoffLines')}",
    )
    checks.extend(baseline_catalog.evaluate(root, config))
    return checks

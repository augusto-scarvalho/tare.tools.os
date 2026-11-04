"""IP-2 strict-subject gate: explicit subject-root contract, no live-root inference.

Batch B3 (SPEC-170 IP-2 implementation train). Extracted from
scripts/spec_test_gate.py to keep it under the frozen gs-7 line budget
(gate-lines-burndown r3 precedent: gs-1..gs-6 already moved structure/release/
content checks the same way). Shared gate helpers (result/ROOT) arrive via
bind(env), matching the existing gate_checks_structure/release/content pattern.

Owns the gate-name -> argv translation for the strict-subject-contract gate.
trusted_verifier.py calls gate_argv() generically and falls back to its own
default when this returns None -- it never spells out this gate's name or
flag itself, preserving the ratified P1 negative evidence
(ip2-no-strict-subject-contract / ip2-no-strict-subject-root-flag in
p1_fsv_verifier_scenarios.py) that trusted_verifier.py's own source carries no
strict-subject semantics.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STRICT_SUBJECT_GATE_NAME = "strict-subject-contract"

REQUIRED_SUBJECT_FILES = (
    "AGENTS.md",
    ".harness/project.json",
    ".harness/context/CONTEXT.md",
    ".harness/context/STATE.md",
    ".harness/context/NEXT_STEPS.md",
    ".harness/routing/task-profiles.json",
    ".harness/routing/executors.json",
    ".harness/prompts/subagent-contract.md",
    "scripts/harness.py",
    "scripts/harness-test.py",
    "scripts/package-release.py",
    "scripts/harness-test.sh",
    "docs/RUNTIME_PORTABILITY.md",
    "docs/GRAPHIFY_INTEGRATION.md",
    "docs/AGENTIC_WORKFLOWS.md",
    ".harness/workflows/WORKFLOWS.md",
    ".harness/workflows/workflow-profiles.json",
    "schemas/workflow.schema.json",
    "schemas/worker-result.schema.json",
    "schemas/reduce-result.schema.json",
    "tools/hooks/graphify_search_guard.py",
)


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from scripts/spec_test_gate.py."""
    globals().update(env)


def check_required_files_for(subject_root: Path) -> list[dict[str, str]]:
    """Structural presence check over an EXPLICIT subject root (never inferred)."""
    out: list[dict[str, str]] = []
    for item in REQUIRED_SUBJECT_FILES:
        p = subject_root / item
        out.append(result(f"required:{item}", "pass" if p.exists() else "fail", "exists" if p.exists() else "missing"))
    return out


def run_strict_subject_gate(subject_root: Path) -> int:
    """Narrow explicit-root production gate; never infers subject from verifier.

    ``security-baseline.json`` is verifier-local authority policy. It is not
    read from the subject and malformed policy fails the strict mechanism.
    """
    try:
        policy = json.loads((ROOT / ".harness" / "state" / "security-baseline.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 2
    if not isinstance(policy, dict):
        return 2
    rows = check_required_files_for(subject_root)
    return 0 if not any(row["status"] == "fail" for row in rows) else 1


def gate_argv(gate: str, subject_root: Path) -> list[str] | None:
    """Return this gate's specialized argv, or None to defer to the generic path.

    The caller (trusted_verifier.py) never needs to know this gate's name or
    flag; it just calls this generically and falls back on None.
    """
    if gate == STRICT_SUBJECT_GATE_NAME:
        return ["smoke", "--strict-subject-root", str(subject_root)]
    return None

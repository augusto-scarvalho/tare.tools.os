"""Content/policy checks for the validation gate (extracted from spec_test_gate.py).

Testing artifacts, validation-policy shape, handles-lint (observe-only) and the
SPEC-122 EN-default string guard. Shared gate helpers arrive via bind(env).

Note: this module quotes PT diacritics and curated PT words (the guard's own
signal data). EN_GUARD_SCOPE is a positive file list, so this home is out of
the guard's scan scope by construction — no allowlist entry is needed.
"""
from __future__ import annotations

import ast
import re
from typing import Any


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from scripts/spec_test_gate.py."""
    globals().update(env)


def check_testing_artifacts() -> list[dict[str, str]]:
    required = [
        "testing/TESTING.md",
        "testing/QUALITY_GATES.md",
        "testing/COVERAGE_POLICY.md",
        "testing/CI_MATRIX.md",
        "testing/validation-matrix.example.yaml",
        "testing/release-scope.example.yaml",
        "schemas/harness-result.schema.json",
        "schemas/validation-policy.schema.json",
        "specs/00-universal/testing-and-quality-gates.md",
        "specs/00-universal/coverage-and-regression.md",
        "docs/AGENT_TESTING_STRATEGY.md",
    ]
    out: list[dict[str, str]] = []
    for item in required:
        p = ROOT / item
        out.append(result(f"testing-artifact:{item}", "pass" if p.exists() else "fail", "exists" if p.exists() else "missing"))
    contract = ROOT / ".harness" / "prompts" / "subagent-contract.md"
    text = contract.read_text(encoding="utf-8", errors="ignore") if contract.exists() else ""
    for token in ["testsRun", "failedTests", "coverage", "contextDiscovery", "graphify", "universalSpecDeviations"]:
        out.append(result(f"harness-result-field:{token}", "pass" if token in text else "fail", "present" if token in text else "missing"))
    return out


def check_validation_policy() -> list[dict[str, str]]:
    project = project_config()
    out: list[dict[str, str]] = []
    validation = project.get("validation")
    if not isinstance(validation, dict):
        return [result("validation-policy", "fail", ".harness/project.json validation must be an object")]
    for gate in GATES:
        commands, required, run_detected, description = command_config(project, gate)
        if gate not in validation:
            out.append(result(f"validation-gate:{gate}", "fail", "missing"))
        else:
            detail = f"{len(commands)} command(s); required={required}; runDetected={run_detected}"
            if description:
                detail += f"; {description}"
            out.append(result(f"validation-gate:{gate}", "pass", detail))
    coverage = project.get("coverage", {})
    if not isinstance(coverage, dict):
        out.append(result("coverage-policy", "fail", "coverage must be an object"))
    else:
        mode = coverage.get("mode", "disabled")
        allowed = {"disabled", "informational", "enforced", "risk-targeted"}
        out.append(result("coverage-policy:mode", "pass" if mode in allowed else "fail", str(mode)))
        if coverage.get("enabled") and not (coverage.get("commands") or command_config(project, "coverage")[0]):
            out.append(result("coverage-policy:commands", "fail", "enabled coverage needs coverage.commands or validation.coverage.commands"))
        else:
            out.append(result("coverage-policy:commands", "pass" if coverage.get("commands") else "skip", "configured" if coverage.get("commands") else "not configured"))
    # Duplicate command detection: advisory skip to avoid blocking templates.
    seen: dict[str, list[str]] = {}
    for gate in GATES:
        commands, *_ = command_config(project, gate)
        for cmd in commands:
            seen.setdefault(cmd, []).append(gate)
    duplicates = {cmd: gates for cmd, gates in seen.items() if len(gates) > 1}
    if duplicates:
        detail = "; ".join(f"{cmd} -> {','.join(gates)}" for cmd, gates in list(duplicates.items())[:5])
        out.append(result("validation-duplicates", "skip", "review intentional duplicate commands: " + detail))
    else:
        out.append(result("validation-duplicates", "pass", "no duplicate configured commands"))
    return out


def check_handles_lint() -> list[dict[str, str]]:
    """CE.3 (G5): OBSERVE-ONLY lint for oversized inline payloads in WORKER_RESULTs.

    Flags string fields longer than handles_lint.INLINE_PAYLOAD_MAX that should
    have been passed by reference (200-char HANDLE, SPEC-112) instead of inlined.
    Report-only: emits 'pass'/'skip' but never 'fail', so it cannot block the
    gate — enforcement comes later, per the observe-first critique.
    """
    from harness_lib import handles_lint
    active = HARNESS / "workflows" / "active"
    findings: list[dict[str, Any]] = []
    if active.exists():
        for wf in sorted(active.glob("WF-*")):
            findings.extend(handles_lint.scan_workflow_dir(wf))
    if not findings:
        return [result("handles-lint:inline-payloads", "pass", f"no inline payloads over {handles_lint.INLINE_PAYLOAD_MAX} chars (observe-only)")]
    offenders = "; ".join(f"{f['workerId']}:{f['field']}={f['chars']}c" for f in findings[:10])
    return [result("handles-lint:inline-payloads", "skip", f"OBSERVE-ONLY: {len(findings)} inline payload(s) > {handles_lint.INLINE_PAYLOAD_MAX}c should be handles — {offenders}")]


# ── SPEC-122 (en-default-guard, P1): keep English the default UI language ──────
# en-gui-strings + en-lib-strings translated the user-facing strings; this
# ENFORCING guard fails if Portuguese REGRESSES back into them. It scans only
# Python string literals (AST — comments, docstrings and identifiers never enter
# the tree), strips comments embedded inside page strings (dev notes living in the
# PAGE template are not user-facing, matching the "comments out of scope" rule),
# then unions two deterministic signals: PT diacritics and a curated PT UI-word list.
EN_GUARD_SCOPE = [
    "scripts/harness_ui_page.py",            # PAGE strings (SPEC-114 panel)
    "scripts/harness_lib/ui_panel.py",       # panel-render user-facing strings
    "scripts/harness_lib/model_routing.py",  # routing labels / descriptions
]
# Lowercased tokens exempted from the guard (legitimate non-English UI text).
# Empty today; add a token with a one-line reason if a real exception appears.
EN_GUARD_ALLOWLIST: frozenset[str] = frozenset()
EN_PT_DIACRITICS = "ãõçáéíóúâêàÃÕÇÁÉÍÓÚÂÊÀ"
EN_PT_WORDS = ["erro", "salvar", "enviar", "aguardando", "concluído", "falhou",
               "executar", "cancelar", "configurações", "trabalhador", "fila",
               "não", "você"]
# One pass: a whole word carrying a PT diacritic, OR a curated PT word. Matching a
# whole word (not the bare diacritic char) keeps the reported token greppable.
EN_PT_TOKEN_RE = re.compile(
    r"\b\w*[" + EN_PT_DIACRITICS + r"]\w*\b|\b(?:" + "|".join(EN_PT_WORDS) + r")\b",
    re.IGNORECASE,
)


def _strip_embedded_comments(text: str) -> str:
    """Drop HTML/CSS/JS comments embedded in a page string so developer notes
    (often Portuguese) stay out of scope, like Python comments do.
    ponytail: `//` stripping also eats `https://…` tails — only ever a false
    negative (URLs are not user-facing prose), never a false positive."""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)   # HTML
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)    # CSS / JS block
    text = re.sub(r"//[^\n]*", " ", text)                      # JS line
    return text


def scan_en_default(source: str, filename: str = "<memory>") -> list[dict[str, Any]]:
    """One finding per user-facing string literal carrying Portuguese. AST scopes
    to string constants (comments + identifiers never enter the tree); docstrings
    are excluded; embedded HTML/CSS/JS comments are stripped before matching.
    ponytail: first hit per literal — re-run surfaces the next in the same string."""
    tree = ast.parse(source, filename=filename)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docstrings:
            continue
        match = EN_PT_TOKEN_RE.search(_strip_embedded_comments(node.value))
        if not match:
            continue
        token = match.group(0)
        if token.lower() in EN_GUARD_ALLOWLIST:
            continue
        signal = "diacritic" if any(ch in EN_PT_DIACRITICS for ch in token) else "word"
        findings.append({"file": filename, "line": getattr(node, "lineno", 0), "signal": signal, "token": token})
    return findings


def check_en_default_strings() -> list[dict[str, str]]:
    """SPEC-122 (en-default-guard, P1): ENFORCING guard that English stays the
    default UI language. Fails on any Portuguese in user-facing string literals of
    the translated files (EN_GUARD_SCOPE); passes clean on the current tree."""
    out: list[dict[str, str]] = []
    for rel in EN_GUARD_SCOPE:
        path = ROOT / rel
        if not path.exists():
            out.append(result(f"en-default:{rel}", "skip", "scoped file not present"))
            continue
        try:
            findings = scan_en_default(path.read_text(encoding="utf-8"), rel)
        except SyntaxError as exc:
            out.append(result(f"en-default:{rel}", "skip", f"unparseable: {exc}"))
            continue
        if findings:
            detail = "; ".join(f"L{f['line']}:{f['signal']}={f['token']!r}" for f in findings[:6])
            out.append(result(f"en-default:{rel}", "fail", f"{len(findings)} Portuguese string(s) — {detail}"))
        else:
            out.append(result(f"en-default:{rel}", "pass", "English default preserved"))
    return out

"""Always-run structure/syntax checks for the validation gate (extracted from spec_test_gate.py).

JSON/YAML syntax, Python compilation, directory guides, runtime portability,
and markdown link integrity. Shared gate helpers arrive via bind(env).
"""
from __future__ import annotations

import json
import py_compile
import re
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from scripts/spec_test_gate.py."""
    globals().update(env)


def check_json_syntax() -> list[dict[str, str]]:
    roots = [HARNESS, ROOT / ".codex", ROOT / ".claude", ROOT / "schemas"]
    files: list[Path] = []
    for base in roots:
        if base.exists():
            files.extend(base.rglob("*.json"))
    out: list[dict[str, str]] = []
    for path in sorted(set(files)):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            out.append(result(f"json:{path.relative_to(ROOT)}", "pass"))
        except Exception as exc:
            out.append(result(f"json:{path.relative_to(ROOT)}", "fail", str(exc)))
    return out


def check_yaml_syntax() -> list[dict[str, str]]:
    roots = [ROOT / "specs", ROOT / "schemas", ROOT / "testing", HARNESS]
    files: list[Path] = []
    for base in roots:
        if base.exists():
            files.extend(list(base.rglob("*.yaml")) + list(base.rglob("*.yml")))
    if not files:
        return [result("yaml", "skip", "no yaml files")]
    if yaml is None:
        return [result("yaml", "skip", "PyYAML not installed")]
    out: list[dict[str, str]] = []
    for path in sorted(set(files)):
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
            out.append(result(f"yaml:{path.relative_to(ROOT)}", "pass"))
        except Exception as exc:
            out.append(result(f"yaml:{path.relative_to(ROOT)}", "fail", str(exc)))
    return out


def check_python_compile() -> list[dict[str, str]]:
    files = [ROOT / "scripts" / "harness.py", ROOT / "scripts" / "spec_test_gate.py", ROOT / "scripts" / "package-release.py", ROOT / "scripts" / "release_integrity.py"]
    harness_lib = ROOT / "scripts" / "harness_lib"
    if harness_lib.exists():
        files.extend(harness_lib.rglob("*.py"))
    hooks = ROOT / "tools" / "hooks"
    if hooks.exists():
        files.extend(hooks.glob("*.py"))
    out: list[dict[str, str]] = []
    for path in sorted(set(files)):
        if not path.exists():
            out.append(result(f"py_compile:{path.relative_to(ROOT)}", "skip", "missing"))
            continue
        try:
            py_compile.compile(str(path), doraise=True)
            out.append(result(f"py_compile:{path.relative_to(ROOT)}", "pass"))
        except Exception as exc:
            out.append(result(f"py_compile:{path.relative_to(ROOT)}", "fail", str(exc)))
    return out


def check_directory_guides() -> list[dict[str, str]]:
    """Ensure directory-level guides use semantic filenames, not nested README placeholders."""
    required = [
        ".harness/HARNESS.md",
        "specs/SPECS.md",
        "specs/00-universal/UNIVERSAL_BASELINE.md",
        "specs/10-project/PROJECT.md",
        "specs/20-architecture/ARCHITECTURE.md",
        "specs/30-stack/STACK.md",
        "specs/40-features/FEATURES.md",
        "specs/90-operations/OPERATIONS.md",
        "tasks/TASKS.md",
        "schemas/SCHEMAS.md",
        "testing/TESTING.md",
    ]
    out: list[dict[str, str]] = []
    for item in required:
        path = ROOT / item
        out.append(result(f"directory-guide:{item}", "pass" if path.exists() else "fail", "exists" if path.exists() else "missing"))
    # Scan the repo's own tree only — gitignored dependency dirs (the uv venv,
    # node_modules) ship their own READMEs and are not part of the template.
    _skip_dirs = {".git", ".venv", "venv", "node_modules", "site-packages"}
    nested_readmes = [p for p in ROOT.rglob("README.md")
                      if p != ROOT / "README.md" and not _skip_dirs.intersection(p.parts)]
    if nested_readmes:
        detail = "; ".join(str(p.relative_to(ROOT)) for p in nested_readmes[:10])
        out.append(result("directory-guide:nested-readmes", "fail", detail))
    else:
        out.append(result("directory-guide:nested-readmes", "pass", "root README.md only"))
    return out


def check_runtime_portability() -> list[dict[str, str]]:
    """Prevent agent/OS-specific Python runtime assumptions from entering the template."""
    out: list[dict[str, str]] = []
    forbidden = [
        "codex-runtimes",
        "codex-primary-runtime",
        ".cache/codex",
        "Microsoft Store execution-alias",
        "bundled Codex runtime",
    ]
    scan_files = [
        ROOT / "tools" / "agent-sync" / "py-run.sh",
        ROOT / "tools" / "agent-sync" / "AGENT_SYNC.md",
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "docs" / "RUNTIME_PORTABILITY.md",
    ]
    hits: list[str] = []
    for path in scan_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for term in forbidden:
            if term in text:
                hits.append(f"{path.relative_to(ROOT)} contains {term!r}")
    out.append(result("runtime-portability:forbidden-agent-runtime-fallbacks", "fail" if hits else "pass", "; ".join(hits[:10]) if hits else "no forbidden runtime assumptions"))
    project = project_config()
    runtime = project.get("runtime", {}) if isinstance(project.get("runtime"), dict) else {}
    python_cfg = runtime.get("python", {}) if isinstance(runtime.get("python"), dict) else {}
    required = {
        "policy": "project-or-operator-managed",
        "envOverride": "HARNESS_PYTHON",
    }
    missing = [key for key, expected in required.items() if python_cfg.get(key) != expected]
    out.append(result("runtime-portability:project-config", "fail" if missing else "pass", "missing/invalid: " + ", ".join(missing) if missing else "configured"))
    return out


def check_markdown_links() -> list[dict[str, str]]:
    bases = [ROOT, ROOT / "docs", ROOT / "specs", ROOT / "tasks", ROOT / "testing", ROOT / "schemas", HARNESS / "context", HARNESS / "prompts"]
    files: list[Path] = []
    for base in bases:
        if base.exists():
            files.extend(base.rglob("*.md"))
    # Skip gitignored dependency dirs (uv venv, node_modules): their bundled docs
    # carry their own relative links and are not part of this repo's link graph.
    _skip = {".git", ".venv", "venv", "node_modules", "site-packages"}
    files = [f for f in files if not _skip.intersection(f.parts)]
    if not files:
        return [result("markdown-links", "skip", "no markdown files")]
    bad: list[str] = []
    pat = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in sorted(set(files)):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for link in pat.findall(text):
            if link.startswith(("http://", "https://", "mailto:", "#")) or link.startswith("<"):
                continue
            target = link.split("#", 1)[0].strip()
            if not target:
                continue
            candidate = (path.parent / target).resolve()
            try:
                candidate.relative_to(ROOT.resolve())
            except ValueError:
                bad.append(f"{path.relative_to(ROOT)} -> {link} escapes repo")
                continue
            if not candidate.exists():
                bad.append(f"{path.relative_to(ROOT)} -> {link}")
    if bad:
        return [result("markdown-links", "fail", "; ".join(bad[:10]) + (" ..." if len(bad) > 10 else ""))]
    return [result("markdown-links", "pass")]

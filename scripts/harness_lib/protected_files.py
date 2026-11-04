"""Canonical agent-file protection utilities.

Installer scripts, skills and plugin synchronizers often write project-level
agent guidance files as a convenience.  In this harness those files are part of
its security/control plane, so installation workflows must not silently replace
or mutate them.  This module keeps the policy dependency-free and usable from
CI, hooks and the public harness CLI.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
from typing import Any

CONFIG_REL = ".harness/protected-files.json"
SNAPSHOT_REL = ".harness/protected-files.snapshot.json"

DEFAULT_PROTECTED_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "AGENT_HANDOFF.md",
    "CODEX_HANDOFF.md",
    "AGENT_IMPLEMENTATION_ROADMAP.md",
    "CODEX_IMPLEMENTATION_ROADMAP.md",
    ".harness/prompts/subagent-contract.md",
    ".harness/prompts/task/00-start-here.md",
    ".harness/HARNESS.md",
    ".kiro/KIRO.md",
]

INSTALL_CONTEXTS = [
    "script-install",
    "skill-install",
    "plugin-install",
    "mcp-sync",
    "agent-sync",
    "template-adoption",
]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(root: Path) -> dict[str, Any]:
    configured = read_json(root / CONFIG_REL, {}) or {}
    defaults: dict[str, Any] = {
        "schemaVersion": "1.0",
        "policy": "canonical-agent-file-protection",
        "protectedFiles": [
            {
                "path": path,
                "owner": "harness-canonical",
                "allowInstallOverwrite": False,
                "allowedInstallAction": "preserve-existing",
            }
            for path in DEFAULT_PROTECTED_FILES
        ],
        "installContexts": INSTALL_CONTEXTS,
        "snapshotPath": SNAPSHOT_REL,
        "hashAlgorithm": "sha256",
        "failClosedWhenSnapshotMissing": True,
    }
    if isinstance(configured, dict):
        defaults.update(configured)
    return defaults


def protected_entries(root: Path) -> list[dict[str, Any]]:
    config = load_config(root)
    entries = config.get("protectedFiles", [])
    if not isinstance(entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in entries:
        if isinstance(item, str):
            normalized.append({"path": item, "allowInstallOverwrite": False})
        elif isinstance(item, dict) and item.get("path"):
            normalized.append(item)
    return normalized


def protected_paths(root: Path) -> list[str]:
    return [str(item.get("path")) for item in protected_entries(root) if item.get("path")]


def protected_patterns(root: Path) -> list[dict[str, Any]]:
    config = load_config(root)
    rows = config.get("protectedPatterns", [])
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, str):
            normalized.append({"pattern": item, "snapshotIfPresent": True, "blockInstallerWrite": True})
        elif isinstance(item, dict) and item.get("pattern"):
            normalized.append(item)
    return normalized


def known_agent_instruction_sources(root: Path) -> list[dict[str, Any]]:
    config = load_config(root)
    registry = config.get("knownAgentInstructionFiles", {})
    rows = registry.get("sources", []) if isinstance(registry, dict) else []
    return rows if isinstance(rows, list) else []


def _is_runtime_or_generated(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    blocked_prefixes = (
        ".git/",
        "graphify-out/",
        ".harness/workflows/active/",
        # _lib.push_isolated_state parks active/ (pruned above) into a SIBLING
        # `.stash-<pid>` while a scenario runs; unpruned, that stash exposed the
        # parked scratch as protected-file drift (m4 false ambient drift, 2026-08-03).
        # Covers `.stash-<pid>` and its `-recovered` variants.
        ".harness/workflows/.stash-",
        ".harness/runs/",
        ".harness/state-store/",
        "__pycache__/",
    )
    # Third-party dependency trees (gitignored, ANY depth: ui/node_modules/, .venv/).
    # Their files are vendor content, never this repo's canonical instruction files —
    # Playwright ships `*.agent.md` under node_modules that the market-instruction
    # matcher would otherwise flag as drift (2026-07-21 wind-down probe caught it).
    for vendored in ("node_modules/", ".venv/", "site-packages/"):
        if rel.startswith(vendored) or f"/{vendored}" in rel:
            return True
    return rel.startswith(blocked_prefixes) or "/__pycache__/" in rel


def _repo_files(root: Path) -> list[str]:
    """ONE pruned walk of the repo (rel posix paths). EXP-13 root cause: the old
    per-pattern rglob("*") walked the whole tree (incl. .git/.venv) once per
    pattern — ~44 full walks per compare_snapshot, 12-13s per status --html.
    Blocked subtrees are now pruned at walk time; the per-file filter is the
    same _is_runtime_or_generated, so the match set is byte-identical."""
    out: list[str] = []
    for base, dirs, names in os.walk(root):
        rel_base = os.path.relpath(base, root).replace("\\", "/")
        prefix = "" if rel_base == "." else rel_base + "/"
        dirs[:] = [d for d in dirs if not _is_runtime_or_generated(prefix + d + "/")]
        for n in names:
            rel = prefix + n
            if not _is_runtime_or_generated(rel):
                out.append(rel)
    return out


def snapshot_paths(root: Path) -> list[str]:
    paths: set[str] = set(protected_paths(root))
    patterns = [str(row.get("pattern")) for row in protected_patterns(root)
                if row.get("snapshotIfPresent", True) is not False]
    if patterns:
        for rel in _repo_files(root):
            name = rel.rsplit("/", 1)[-1]
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns):
                paths.add(rel)
    return sorted(paths)


def write_block_patterns(root: Path) -> list[str]:
    patterns = [str(row.get("pattern")) for row in protected_patterns(root) if row.get("blockInstallerWrite", True) is not False and row.get("pattern")]
    return sorted(set(patterns))

def file_record(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    if not path.exists() or not path.is_file():
        return {"path": rel, "exists": False}
    text = path.read_text(encoding="utf-8", errors="ignore")
    return {
        "path": rel,
        "exists": True,
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
        "lines": len(text.splitlines()),
    }


def snapshot(root: Path) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "policy": load_config(root).get("policy", "canonical-agent-file-protection"),
        "hashAlgorithm": "sha256",
        "files": [file_record(root, rel) for rel in snapshot_paths(root)],
    }


def write_snapshot(root: Path, output: Path | None = None) -> Path:
    config = load_config(root)
    target = output or (root / str(config.get("snapshotPath") or SNAPSHOT_REL))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot(root), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return target


def compare_snapshot(root: Path, baseline: dict[str, Any] | None = None) -> list[str]:
    config = load_config(root)
    baseline = baseline if baseline is not None else read_json(root / str(config.get("snapshotPath") or SNAPSHOT_REL), {})
    if not isinstance(baseline, dict) or not baseline:
        return ["missing protected-file snapshot"]
    expected = {str(item.get("path")): item for item in baseline.get("files", []) if isinstance(item, dict)}
    errors: list[str] = []
    current = snapshot_paths(root)  # one walk; was computed twice per compare
    for rel in current:
        actual = file_record(root, rel)
        prior = expected.get(rel)
        if prior is None:
            errors.append(f"snapshot missing {rel}")
            continue
        if not actual.get("exists"):
            errors.append(f"protected file missing {rel}")
            continue
        if prior.get("sha256") != actual.get("sha256"):
            errors.append(f"protected file changed {rel}")
    extra = sorted(set(expected) - set(current))
    if extra:
        errors.append("snapshot has unconfigured protected files: " + ", ".join(extra[:5]))
    return errors


def reconcile(root: Path) -> dict[str, Any]:
    """protection-lists-reconcile: the registry (CONFIG_REL) is CANONICAL — the
    sibling lists exist for bootstrap/config and may only diverge from it with
    an explicit declaration (`reconciliation.registryOnly: {path: reason}` in
    the registry). Empty problem lists == reconciled; `declared` is
    informational. The pattern list is a different axis (installer-write
    blocks); only an EXACT string collision with a protected path is drift."""
    config = load_config(root)
    registry = set(protected_paths(root))
    rec = config.get("reconciliation")
    raw_declared = rec.get("registryOnly", {}) if isinstance(rec, dict) else {}
    declared = ({str(k): str(v) for k, v in raw_declared.items()}
                if isinstance(raw_declared, dict) else {})
    project = read_json(root / ".harness" / "project.json", {}) or {}
    proj_block = project.get("protectedFiles") if isinstance(project, dict) else {}
    proj_defaults = set((proj_block or {}).get("defaultProtectedFiles") or [])
    return {
        "undeclared": sorted((registry - proj_defaults) - set(declared)),
        "projectNotInRegistry": sorted(proj_defaults - registry),
        "fallbackExcess": sorted(set(DEFAULT_PROTECTED_FILES) - registry),
        "staleDeclarations": sorted(set(declared) - (registry - proj_defaults)),
        "patternFileOverlap": sorted(
            {str(p.get("pattern")) for p in protected_patterns(root)} & registry),
        "declared": sorted(declared),
    }


def evaluate(root: Path) -> list[dict[str, str]]:
    config = load_config(root)
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    config_path = root / CONFIG_REL
    snapshot_path = root / str(config.get("snapshotPath") or SNAPSHOT_REL)
    entries = protected_entries(root)
    paths = protected_paths(root)

    add("protected-files:config", config_path.exists(), CONFIG_REL if config_path.exists() else "missing")
    add("protected-files:snapshot", snapshot_path.exists(), str(snapshot_path.relative_to(root)) if snapshot_path.exists() else "missing")
    missing_files = [rel for rel in paths if not (root / rel).is_file()]
    add("protected-files:files-exist", not missing_files, "present" if not missing_files else "missing: " + ", ".join(missing_files[:10]))

    mutable = [str(item.get("path")) for item in entries if item.get("allowInstallOverwrite") is not False]
    add("protected-files:no-install-overwrite", not mutable, "all protected files fail closed" if not mutable else "mutable: " + ", ".join(mutable[:10]))

    install_contexts = config.get("installContexts", [])
    expected_contexts = {"script-install", "skill-install", "plugin-install"}
    add(
        "protected-files:install-contexts",
        expected_contexts.issubset(set(install_contexts if isinstance(install_contexts, list) else [])),
        ", ".join(install_contexts) if isinstance(install_contexts, list) else "invalid",
    )

    registry = known_agent_instruction_sources(root)
    registry_tools = {str(item.get("tool", "")) for item in registry if isinstance(item, dict)}
    required_tools = {"OpenAI Codex", "Claude Code", "GitHub Copilot / VS Code", "Cursor", "Gemini CLI", "Cline", "Devin Desktop / Windsurf Cascade", "Roo Code", "Kiro", "Harness adapters"}
    add("protected-files:market-registry", required_tools.issubset(registry_tools), ", ".join(sorted(registry_tools)) if registry_tools else "missing")
    missing_refs = [str(item.get("tool")) for item in registry if isinstance(item, dict) and item.get("tool") != "Harness adapters" and not item.get("referenceUrls")]
    add("protected-files:market-reference-urls", not missing_refs, "official/reference URLs present" if not missing_refs else ", ".join(missing_refs[:10]))

    patterns = write_block_patterns(root)
    required_patterns = {"AGENTS.override.md", "GEMINI.md", ".github/copilot-instructions.md", ".cursor/rules/*.mdc", ".clinerules/*.md", ".devin/rules/*.md", ".windsurf/rules/*.md", ".roorules", "AGENT.md"}
    add("protected-files:installer-write-patterns", required_patterns.issubset(set(patterns)), ", ".join(patterns[:20]) if patterns else "missing")

    # protection-lists-reconcile: the sibling lists may only diverge from the
    # canonical registry with an explicit declaration (drift found 2026-07-27).
    rec = reconcile(root)
    problems = {k: v for k, v in rec.items() if v and k != "declared"}
    add("protected-files:lists-reconciled", not problems,
        f"declared={len(rec['declared'])}" if not problems else json.dumps(problems)[:200])

    drift = compare_snapshot(root)
    add("protected-files:snapshot-match", not drift, "current" if not drift else "; ".join(drift[:10]))

    guard_hook = root / "tools" / "hooks" / "protect_canonical_files.py"
    add("protected-files:hook", guard_hook.exists(), "tools/hooks/protect_canonical_files.py" if guard_hook.exists() else "missing")

    spec_text = (root / "specs" / "00-universal" / "canonical-file-protection.md").read_text(encoding="utf-8", errors="ignore") if (root / "specs" / "00-universal" / "canonical-file-protection.md").exists() else ""
    spec_ok = all(token in spec_text for token in ["AGENTS.md", "CLAUDE.md", "GEMINI.md", "copilot-instructions.md", ".clinerules", ".devin/rules", ".roorules"]) and "plugin" in spec_text.lower() and "install" in spec_text.lower()
    add("protected-files:spec", spec_ok, "canonical file protection spec" if spec_ok else "missing/incomplete spec")
    return checks

"""SPEC-110 HT: target-repository registry — the harness governs other codebases.

One directory per target, one canonical tracked file
(`.harness/targets/<name>/target.json`); generated state lives under the
harness (`graphify-out/targets/<name>/`, `.harness/state/targets/<name>/`) so
target repos stay clean and there is a single supervision funnel.

The harness is a governance engine parameterized by subject: everything here is
root/name-plumbing so the existing root-parameterized libs (AST graph, discover,
protected files, self-review) and the gate runner can operate on any registered
repository exactly as they operate on the harness itself.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from harness_lib.common import HARNESS, HarnessError, ROOT, read_json, write_json

PROFILES_DIR = HARNESS / "targets"
OUT_BASE = ROOT / "graphify-out" / "targets"
STATE_BASE = HARNESS / "state" / "targets"

DEFAULT_PROFILE: dict[str, Any] = {
    "enabled": True,
    "sourceGlobs": ["**/*.py"],
    "lineBudgets": {"maxFileLines": 800, "targetFileLines": 500},
    "validation": {},
    "requiresEnv": [],
    "mcp": {"servers": {}},
    "skills": [],
    "plugins": [],
    "selfReview": {},
    "protectedFiles": [],
}

EXCLUDE_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".harness", "graphify-out"}


def load_targets() -> dict[str, dict[str, Any]]:
    """All registered target profiles keyed by name (merged over defaults)."""
    out: dict[str, dict[str, Any]] = {}
    if not PROFILES_DIR.exists():
        return out
    for profile_path in sorted(PROFILES_DIR.glob("*/target.json")):
        raw = read_json(profile_path, None)
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or profile_path.parent.name)
        merged = json.loads(json.dumps(DEFAULT_PROFILE))
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        merged["name"] = name
        merged["profilePath"] = str(profile_path.relative_to(ROOT))
        out[name] = merged
    return out


def resolve(name: str) -> dict[str, Any]:
    """Resolve a target by name; legible refusals for every failure mode."""
    targets = load_targets()
    if name not in targets:
        raise HarnessError(
            f"unknown target {name!r}\n"
            f"cause: no profile at .harness/targets/{name}/target.json\n"
            f"fix: python scripts/harness.py targets (list); create the profile to register a repository"
        )
    entry = targets[name]
    if not entry.get("enabled", True):
        raise HarnessError(
            f"target {name!r} is disabled\n"
            "cause: enabled=false in its target.json\n"
            f"fix: set enabled=true in {entry['profilePath']} after confirming the repo should be governed again"
        )
    path = target_root(entry)
    if not path.exists() or not path.is_dir():
        raise HarnessError(
            f"target {name!r} path does not exist: {path}\n"
            "cause: the repository moved or the profile path is wrong\n"
            f"fix: update \"path\" in {entry['profilePath']} or restore the repository"
        )
    return entry


def target_root(entry: dict[str, Any]) -> Path:
    raw = str(entry.get("path", ""))
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / raw)


def register(root: Path, name: str, path: Path | str) -> dict[str, Any]:
    """Single write path for target registration (R32): persist the minimal
    governed-target profile at `.harness/targets/<name>/target.json`. A governed
    target must be an existing git repository; refuse duplicates naming the path."""
    name = str(name).strip()
    p = Path(path)
    if not name:
        raise HarnessError("target name is required")
    if not p.exists() or not p.is_dir():
        raise HarnessError(f"target path is not a directory: {p}")
    if not (p / ".git").exists():
        raise HarnessError(
            f"not a git repository (no .git): {p}\n"
            "cause: a governed target must be a repository\n"
            "fix: point at a repo root (or run `git init` there first)")
    profile = Path(root) / ".harness" / "targets" / name / "target.json"
    if profile.exists():
        existing = read_json(profile, {}) or {}
        raise HarnessError(f"target {name!r} already registered at {existing.get('path', profile)}")
    write_json(profile, {"name": name, "path": str(p)})
    return {"registered": name, "path": str(p), "profilePath": str(profile)}


def out_dir(name: str) -> Path:
    return OUT_BASE / name


def state_dir(name: str) -> Path:
    return STATE_BASE / name


def source_files(entry: dict[str, Any], *, max_files: int = 5000) -> list[Path]:
    root = target_root(entry)
    seen: dict[Path, None] = {}
    for glob in entry.get("sourceGlobs", ["**/*.py"]):
        for path in root.glob(str(glob)):
            if not path.is_file() or any(part in EXCLUDE_PARTS for part in path.parts):
                continue
            seen[path] = None
            if len(seen) >= max_files:
                break
    return list(seen)


def git_status(entry: dict[str, Any]) -> dict[str, Any]:
    """Target git state via `git -C`; calm degrade when the target is not a repo."""
    root = target_root(entry)
    try:
        proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if proc.returncode != 0:
            return {"isGitRepo": False}
        dirty = [line for line in proc.stdout.splitlines() if line.strip()]
        return {"isGitRepo": True, "dirtyFiles": len(dirty)}
    except Exception:
        return {"isGitRepo": False}


def inspect(entry: dict[str, Any]) -> dict[str, Any]:
    """Health snapshot for `harness.py targets` and the self-review collector."""
    root = target_root(entry)
    files = source_files(entry)
    budgets = entry.get("lineBudgets", {})
    max_lines = int(budgets.get("maxFileLines", 800))
    over_budget: list[dict[str, Any]] = []
    biggest = {"path": None, "lines": 0}
    for path in files:
        try:
            lines = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        if lines > biggest["lines"]:
            biggest = {"path": rel, "lines": lines}
        if lines > max_lines:
            over_budget.append({"path": rel, "lines": lines})
    over_budget.sort(key=lambda item: -item["lines"])
    graph_path = out_dir(entry["name"]) / "graph.json"
    newest_src = max((p.stat().st_mtime for p in files), default=0.0)
    graph_stale = (not graph_path.exists()) or graph_path.stat().st_mtime < newest_src
    missing_env = [key for key in entry.get("requiresEnv", []) if not os.environ.get(key)]
    quality = read_json(state_dir(entry["name"]) / "quality-state.json", {}) or {}
    return {
        "name": entry["name"],
        "path": str(root),
        "exists": True,
        "sourceFiles": len(files),
        "biggestFile": biggest,
        "overBudget": over_budget[:10],
        "maxFileLines": max_lines,
        "graphStale": graph_stale,
        "missingEnv": missing_env,
        "git": git_status(entry),
        "lastGate": (quality.get("lastValidation") or {}).get("gate"),
        "lastGateStatus": (quality.get("lastValidation") or {}).get("status"),
        "declared": {"mcpServers": sorted((entry.get("mcp", {}).get("servers") or {}).keys()),
                     "skills": entry.get("skills", []), "plugins": entry.get("plugins", []),
                     "requiresEnv": entry.get("requiresEnv", [])},
    }


def spawn_env(entry: dict[str, Any]) -> dict[str, str]:
    """Deny-by-default env for agents working on this target: only requiresEnv keys."""
    return {key: os.environ[key] for key in entry.get("requiresEnv", []) if key in os.environ}


def sync_adapter(entry: dict[str, Any]) -> dict[str, Any]:
    """Generate on-demand adapter artifacts (never persisted per project as tracked files).

    Writes graphify-out/targets/<name>/adapter/{mcp-config.json, spawn-env.json}
    from the canonical target.json — regenerable, gitignored, single source of truth.
    """
    adapter_dir = out_dir(entry["name"]) / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    mcp = {"mcpServers": entry.get("mcp", {}).get("servers", {})}
    write_json(adapter_dir / "mcp-config.json", mcp)
    env = spawn_env(entry)
    missing = [key for key in entry.get("requiresEnv", []) if key not in env]
    write_json(adapter_dir / "spawn-env.json", {
        "note": "deny-by-default: only this target's requiresEnv keys are injected into its agents",
        "keys": sorted(env),
        "missing": missing,
        "skills": entry.get("skills", []),
        "plugins": entry.get("plugins", []),
    })
    return {"adapterDir": str(adapter_dir.relative_to(ROOT)), "mcpServers": sorted(mcp["mcpServers"]),
            "envKeys": sorted(env), "missingEnv": missing}


def self_review_config(entry: dict[str, Any], global_config: dict[str, Any]) -> dict[str, Any]:
    """Layered config: global defaults merged with the target's selfReview overrides."""
    merged = json.loads(json.dumps(global_config))
    overrides = entry.get("selfReview", {}) or {}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged

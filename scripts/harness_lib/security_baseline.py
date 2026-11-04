#!/usr/bin/env python3
"""Baseline security evaluator — OBSERVE-ONLY (measurement before control).

`evaluate(root)` runs three deterministic, stdlib-only scans over a tree and
diffs the findings against a snapshot so day one is GREEN and only NEW findings
surface afterwards:

  (a) secret-scan   — every tracked/text file through harness_lib.secret_scan
                      (its anchored patterns; NOT re-implemented here);
  (b) sink-scan     — stdlib `ast` over `scripts/` for eval()/exec()/
                      subprocess(..., shell=True)/pickle.loads;
  (c) config-hygiene — a tracked `.env` (real env file, not `.env.example`).

Snapshot at `.harness/state/security-baseline.json` mirrors the write-once
snapshot precedent (gate_generic.check_protected_instructions): first run records
ALL findings and returns `new == []`; later runs report findings ABSENT from the
snapshot as `new`. Nothing consumes `new` as a block yet — it only REPORTS.

Finding ids are name/path-based (never the secret value or its length), so the
snapshot is safe to commit and never self-triggers the secret scan.

Self-check: `python scripts/harness_lib/security_baseline.py`.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import secret_scan  # noqa: E402  (reuse anchored patterns, do not re-implement)

# Suffixes worth reading as text for the secret scan. Suffix-less files (e.g.
# `.env`, `Dockerfile`) are read too; anything else (binaries) is skipped.
_TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".env", ".sh", ".ps1", ".js", ".ts", ".html", ".css", ".xml",
}
_SKIP_DIRS = {".git", ".venv", "graphify-out", "__pycache__", "node_modules"}
_ENV_EXEMPT_SUFFIXES = (".example", ".sample", ".template", ".dist")


def _iter_files(root: Path):
    """(rel, path) for candidate files. Prefer `git ls-files` (tracked only —
    deterministic, skips gitignored operator state and big logs); fall back to a
    bounded rglob for non-git trees (e.g. the scenario temp tree)."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            for rel in proc.stdout.splitlines():
                rel = rel.strip()
                if rel:
                    yield rel, root / rel
            return
    except Exception:
        pass
    for path in root.rglob("*"):
        if path.is_file() and not (set(path.parts) & _SKIP_DIRS):
            yield path.relative_to(root).as_posix(), path


def _secret_findings(root: Path) -> list[str]:
    out: list[str] = []
    for rel, path in _iter_files(root):
        if path.suffix.lower() not in _TEXT_SUFFIXES and path.suffix:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for hit in secret_scan.scan(content):  # id keeps pattern name + path, never the value
            out.append(f"secret:{hit['pattern']}:{rel}")
    return out


def _classify_sink(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id in {"eval", "exec"}:
        return func.id
    if (isinstance(func, ast.Attribute) and func.attr == "loads"
            and isinstance(func.value, ast.Name) and func.value.id == "pickle"):
        return "pickle.loads"
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return "shell-true"
    return None


def _sink_findings(root: Path, sink_scope: str = "scripts") -> list[str]:
    # ponytail: (kind, file) granularity, no lineno in the id — a line inserted
    # above a sink must not masquerade as a NEW finding. Add lineno only if a file
    # ever needs per-occurrence tracking of the same sink kind.
    out: list[str] = []
    if sink_scope == "tree":
        # dual-subject (security-baseline-targets): a governed target has no
        # scripts/ convention, so its scan covers every tracked .py (its own
        # snapshot starts day-one green, so this widens nothing retroactively).
        paths = [p for rel, p in _iter_files(root) if p.suffix == ".py"]
    else:
        scripts = root / "scripts"
        if not scripts.is_dir():
            return out
        paths = list(scripts.rglob("*.py"))
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except Exception:
            continue
        for node in ast.walk(tree):
            kind = _classify_sink(node)
            if kind:
                out.append(f"sink:{kind}:{rel}")
    return out


def _config_findings(root: Path) -> list[str]:
    out: list[str] = []
    for rel, path in _iter_files(root):
        name = path.name
        if name == ".env" or (name.startswith(".env.") and not name.endswith(_ENV_EXEMPT_SUFFIXES)):
            out.append(f"config:tracked-env:{rel}")
    return out


def _snapshot_path(root: Path) -> Path:
    return root / ".harness" / "state" / "security-baseline.json"


def evaluate(root, snapshot_path: Path | None = None, sink_scope: str = "scripts") -> dict:
    """Run the three scans, diff against the snapshot. Day-one green: the first
    run writes ALL findings and returns `new == []`.

    Dual-subject (security-baseline-targets): `snapshot_path` relocates the
    snapshot OUTSIDE the scanned tree (a governed target must gain no file from
    a harness flow — the isolation-audit invariant); `sink_scope="tree"` widens
    the AST sink scan beyond the harness's scripts/ convention."""
    root = Path(root)
    findings = sorted(set(_secret_findings(root) + _sink_findings(root, sink_scope)
                          + _config_findings(root)))
    snap = Path(snapshot_path) if snapshot_path else _snapshot_path(root)
    previous = None
    if snap.exists():
        try:
            previous = json.loads(snap.read_text(encoding="utf-8")).get("findings")
        except Exception:
            previous = None
    if previous is None:  # write-once baseline, mirrors gate_generic snapshot precedent
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps({"schemaVersion": "1.0", "findings": findings}, indent=2) + "\n",
                        encoding="utf-8")
        return {"findings": findings, "new": [], "baselined": findings}
    known = set(previous)
    return {"findings": findings,
            "new": [f for f in findings if f not in known],
            "baselined": [f for f in findings if f in known]}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "config.py").write_text(
            'API = "sk-FAKEKEYdonotleak0123456789abcdef"  # obvious fake\n', encoding="utf-8")
        (root / "scripts" / "danger.py").write_text("x = eval('1+1')\n", encoding="utf-8")

        first = evaluate(root)
        assert first["new"] == [], first
        assert any(f.startswith("secret:") for f in first["baselined"]), first
        assert any(f.startswith("sink:eval:") for f in first["baselined"]), first

        # A second planted key in a NEW file surfaces as exactly one new finding.
        (root / "leak2.py").write_text(
            'K = "sk-SECONDfakekey0123456789abcdefzz"\n', encoding="utf-8")
        second = evaluate(root)
        assert len(second["new"]) == 1, second
        assert second["new"][0] == "secret:openai-style-key:leak2.py", second
    print("security_baseline self-check: ok")


if __name__ == "__main__":
    _demo()

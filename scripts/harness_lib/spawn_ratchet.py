#!/usr/bin/env python3
"""no-raw-subprocess-gate — a ratchet on unmediated process spawns.

`processes.py` is the mediation layer (hidden consoles, bounded timeouts,
tree-kill semantics); every raw `subprocess.*` / `asyncio.create_subprocess_*`
call site outside it is a place those guarantees silently don't hold — the
black-terminal-windows incident came from exactly one such site. The honest
posture (SEC.5: ~48 legacy sites) is a RATCHET, not a purge: a write-once
snapshot baselines today's sites (day-one green) and the gate fails only on a
NEW one — route it through `processes.py` helpers, or consciously re-baseline.

Finding ids are `(kind, file)`-granular (`spawn:<kind>:<rel>`, the
security-baseline sink-scan idiom): moving lines inside a file never
masquerades as a new site; a new KIND in a file, or a first site in a new
file, does. Stdlib `ast`, tracked-tree scan under `scripts/`.

Self-check: `python scripts/harness_lib/spawn_ratchet.py`.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))

SNAPSHOT_REL = ".harness/state/raw-subprocess-baseline.json"
MEDIATION = "scripts/harness_lib/processes.py"  # the layer itself is exempt
_SUBPROCESS_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
_ASYNC_ATTRS = {"create_subprocess_exec", "create_subprocess_shell"}


def _spawn_kind(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    func = node.func
    base = func.value
    if isinstance(base, ast.Name) and base.id == "subprocess" and func.attr in _SUBPROCESS_ATTRS:
        return f"subprocess.{func.attr}"
    if func.attr in _ASYNC_ATTRS:  # asyncio.create_subprocess_* (any base alias)
        return f"asyncio.{func.attr}"
    return None


def findings(root: Path | str) -> list[str]:
    root = Path(root)
    scripts = root / "scripts"
    out: set[str] = set()
    if not scripts.is_dir():
        return []
    for path in scripts.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel == MEDIATION:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for node in ast.walk(tree):
            kind = _spawn_kind(node)
            if kind:
                out.add(f"spawn:{kind}:{rel}")
    return sorted(out)


def evaluate(root: Path | str) -> dict:
    """Write-once baseline diff (the security-baseline snapshot idiom):
    the first run records ALL sites and returns new == []."""
    root = Path(root)
    found = findings(root)
    snap = root / SNAPSHOT_REL
    previous = None
    if snap.exists():
        try:
            previous = json.loads(snap.read_text(encoding="utf-8")).get("findings")
        except Exception:
            previous = None
    if previous is None:
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps({"schemaVersion": "1.0", "findings": found},
                                   indent=2) + "\n", encoding="utf-8")
        return {"findings": found, "new": [], "baselined": found}
    known = set(previous)
    return {"findings": found,
            "new": [f for f in found if f not in known],
            "baselined": [f for f in found if f in known]}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lib = root / "scripts" / "harness_lib"
        lib.mkdir(parents=True)
        (lib / "processes.py").write_text(
            "import subprocess\nsubprocess.run(['x'])\n", encoding="utf-8")  # exempt
        (lib / "a.py").write_text(
            "import subprocess\nsubprocess.Popen(['x'])\n", encoding="utf-8")
        (lib / "b.py").write_text(
            "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n",
            encoding="utf-8")
        first = evaluate(root)
        assert first["new"] == [] and sorted(first["baselined"]) == [
            "spawn:asyncio.create_subprocess_exec:scripts/harness_lib/b.py",
            "spawn:subprocess.Popen:scripts/harness_lib/a.py"], first
        # a second Popen in the SAME file is not a new finding (file granularity)…
        (lib / "a.py").write_text(
            "import subprocess\nsubprocess.Popen(['x'])\nsubprocess.Popen(['y'])\n",
            encoding="utf-8")
        assert evaluate(root)["new"] == []
        # …but a new FILE (or a new kind) is.
        (lib / "c.py").write_text(
            "import subprocess\nsubprocess.check_output(['x'])\n", encoding="utf-8")
        second = evaluate(root)
        assert second["new"] == ["spawn:subprocess.check_output:scripts/harness_lib/c.py"], second
    print("spawn_ratchet self-check: ok")


if __name__ == "__main__":
    _demo()

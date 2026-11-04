"""Graphify code AST-first structural graph builder.

This module gives the harness a dependency-free, offline graph source before any
LLM/API-assisted structural discovery. It intentionally supports Python in the
universal template because Python AST is available in the standard library; other
languages can add project-specific adapters without changing the public CLI.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".harness/workflows",
    ".harness/state-store",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "graphify-out",
    "dist",
    "build",
    "coverage",
}


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _excluded(rel: str, exclude_dirs: set[str]) -> bool:
    parts = rel.split("/")
    prefixes = {"/".join(parts[:idx]) for idx in range(1, len(parts) + 1)}
    return bool(prefixes & exclude_dirs or set(parts) & exclude_dirs)


def discover_python_files(root: Path, *, max_files: int = 1000, exclude_dirs: set[str] | None = None) -> list[Path]:
    """Return repository Python files suitable for Graphify code AST graphing."""
    excludes = exclude_dirs or DEFAULT_EXCLUDE_DIRS
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        rel = _rel(path, root)
        if _excluded(rel, excludes):
            continue
        files.append(path)
        if len(files) >= max_files:
            break
    return files


def _module_guess(rel: str) -> str:
    path = Path(rel)
    if path.name == "__init__.py":
        return ".".join(path.parent.parts)
    return ".".join(path.with_suffix("").parts)


def _import_targets(tree: ast.AST) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    parts = alias.name.split(".")
                    targets.add(alias.name)
                    targets.add(parts[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets.add(node.module)
                targets.add(node.module.split(".")[0])
                for alias in node.names:
                    if alias.name and alias.name != "*":
                        targets.add(f"{node.module}.{alias.name}")
    return targets


def _symbols(tree: ast.AST) -> dict[str, list[str]]:
    classes: list[str] = []
    functions: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
    return {"classes": sorted(classes), "functions": sorted(functions)}


INPUT_KEY_VERSION = "graphify-code-ast-inputs/v2"


class UnreadableInput(OSError):
    """A selected input could not be read while computing the content key.

    keys-keyring/graph decision (plan-graph-round2 Q2, 2026-07-24): an unreadable file
    FAILS LOUDLY rather than folding in as a marker. v1 hashed a `<unreadable>` marker,
    so a transiently locked file (plausible on Windows during a gate) changed the key and
    reported a FALSE STALE — the exact noise class the content key exists to kill. The
    caller (graph-staleness / graph-status) catches this and reports the honest
    `unreadable` state; a non-answer, never a wrong answer. The bumped v2 version tag
    keeps a v1-stamped graph from ever matching a v2 key.
    """


def _atomic_write_text(path: Path, text: str) -> None:
    """Publish a file atomically: write a sibling temp, then os.replace onto the target.

    plan-graph-round2 T1. The old in-place write_text let a concurrent reader (two gate
    shards, or the owner + the gate) observe a torn file. `os.replace` is the atomic
    primitive that works on Windows — `os.rename` FAILS onto an existing path there. The
    temp is in the SAME directory (a cross-volume replace is not atomic) and its suffix
    comes AFTER the full filename (`graph.json.tmp-<pid>`) so a `*.json` glob cannot
    mistake a crashed build's orphan for a real artifact.

    Windows caveat, load-bearing: unlike POSIX, `os.replace` over a destination that ANY
    handle has open (a concurrent reader, an antivirus, an indexer) raises PermissionError
    — Windows has no rename-over-open-file. A reader never sees a TORN file (it opened
    either the old or the new inode), but the replace itself can fail transiently, so it is
    wrapped in a bounded retry (~2s total). A reader hammering in a zero-gap loop can still
    exhaust it; a real consumer that reads occasionally will not. That residual is the
    accepted cost of not building the object store (plan-graph-round2 §5).
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    last: OSError | None = None
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # Windows: destination held open, usually transient
            last = exc
            time.sleep(0.05 + 0.01 * attempt)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last  # type: ignore[misc]


def _key_add(h: "hashlib._Hash", rel: str, data: bytes) -> None:
    """One file's framed contribution to the content key. The SINGLE definition of what
    the key is over, shared by `input_key` (freshness check) and the build's accumulation
    (plan-graph-round2 T1.5) so the two can never silently diverge."""
    h.update(b"\0")
    h.update(rel.encode("utf-8"))
    h.update(b"\0")
    h.update(data)


def _read_source_bytes(path: Path) -> bytes:
    """Read one input's bytes, raising UnreadableInput on failure (fail-loud, Q2).
    Indirected so a test can wrap it to mutate an already-read file mid-build (gra-3)."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UnreadableInput(f"{path}: {exc}") from exc


def input_key(root: Path, *, max_files: int = 1000) -> str:
    """SHA-256 over what the graph is built FROM: schema version + each selected file's
    repo-relative path and bytes, in sorted order.

    Freshness answered by CONTENT, not clock. mtime lied constantly here (checkout /
    branch switch / gate worktree rewrite timestamps without changing a byte), so the
    first cut cried stale on an identical tree. Same bytes -> same key, on any machine,
    after any checkout. An unreadable file RAISES `UnreadableInput` (Q2): the caller
    reports `unreadable`, an honest non-answer, rather than a false stale.
    """
    h = hashlib.sha256()
    h.update(INPUT_KEY_VERSION.encode("utf-8"))
    for path in discover_python_files(root, max_files=max_files):
        _key_add(h, _rel(path, root), _read_source_bytes(path))
    return h.hexdigest()


def freshness(root: Path, *, output_dir: Path | None = None, max_files: int = 1000) -> dict[str, Any]:
    """The ONE freshness computation, shared by `doctor`'s graph-staleness and
    `graph-status` (plan-graph-round2 T2 — one truth, two surfaces; never a second
    comparison that can silently disagree). Returns:
      state ∈ fresh|stale|no_graph|unkeyed|unreadable, inputKey, storedKey, detail.
    Never raises: an unreadable input becomes state=unreadable, not a crash.
    """
    output_dir = output_dir or (root / "graphify-out")
    graph = output_dir / "graph.json"
    if not graph.is_file():
        return {"state": "no_graph", "inputKey": None, "storedKey": None,
                "detail": "no graph built"}
    try:
        stored = str((json.loads(graph.read_text(encoding="utf-8")) or {}).get("inputKey") or "")
    except (OSError, ValueError):
        stored = ""
    if not stored:
        return {"state": "unkeyed", "inputKey": None, "storedKey": None,
                "detail": "graph predates input-key tracking — rebuild: harness.py graph-build-code-ast"}
    try:
        current = input_key(root, max_files=max_files)
    except UnreadableInput as exc:
        return {"state": "unreadable", "inputKey": None, "storedKey": stored,
                "detail": f"a code input is unreadable; freshness unanswerable ({exc})"}
    if current == stored:
        return {"state": "fresh", "inputKey": current, "storedKey": stored,
                "detail": "graph inputs match its key"}
    return {"state": "stale", "inputKey": current, "storedKey": stored,
            "detail": f"code-AST inputs changed since the graph was built "
                      f"({stored[:12]} -> {current[:12]}) — rebuild: harness.py graph-build-code-ast"}


def build_graphify_code_ast(root: Path, *, output_dir: Path | None = None, max_files: int = 1000,
                            _after_read: "Callable[[str], None] | None" = None) -> dict[str, Any]:
    """Build a compact Graphify-compatible graph from Graphify Python ASTs.

    The generated graph uses common `nodes`/`edges` keys so the existing
    Graphify adapter can shard it without special runtime dependencies.

    The stamped `inputKey` ACCUMULATES from the bytes this loop actually reads
    (plan-graph-round2 T1.5), not a post-build re-scan. That distinction is load-bearing:
    a re-scan would re-read a file mutated mid-build and stamp a key matching CURRENT disk
    over a graph built from OLD bytes — a false FRESH. Hashing the bytes we parse means a
    post-parse edit correctly reports STALE. `_after_read` is a test-only seam (gra-3):
    called with each rel right after its bytes are read, so a test can mutate an
    already-parsed file before the loop ends. It is NOT the T3 resolve seam (cut).
    """
    output_dir = output_dir or (root / "graphify-out")
    build_t0 = time.monotonic()  # perf-metrics rung: the one plausible PyO3 seam self-measures
    files = discover_python_files(root, max_files=max_files)
    key_hash = hashlib.sha256()
    key_hash.update(INPUT_KEY_VERSION.encode("utf-8"))
    modules: dict[str, str] = {}
    parsed: dict[str, ast.AST] = {}
    errors: list[dict[str, str]] = []
    for path in files:
        rel = _rel(path, root)
        module_name = _module_guess(rel)
        modules[module_name] = rel
        if module_name.startswith("scripts."):
            modules[module_name[len("scripts."):]] = rel
        if path.name != "__init__.py":
            modules[path.stem] = rel
        data = _read_source_bytes(path)          # OSError -> UnreadableInput (fail loud, Q2)
        _key_add(key_hash, rel, data)            # accumulate from the SAME bytes we parse
        if _after_read is not None:
            _after_read(rel)
        try:
            parsed[rel] = ast.parse(data.decode("utf-8", errors="ignore"), filename=rel)
        except SyntaxError as exc:
            errors.append({"path": rel, "error": f"SyntaxError: {exc.msg}"})
        except Exception as exc:  # pragma: no cover - defensive filesystem guard
            errors.append({"path": rel, "error": type(exc).__name__})

    module_to_path = {mod: rel for mod, rel in modules.items() if mod}
    for mod, rel in list(modules.items()):
        if mod:
            module_to_path.setdefault(mod.split(".")[0], rel)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    communities: dict[str, list[str]] = {}
    # MF.3: symbol index sidecar (name -> path:line). The graph node keeps only
    # names for adapter compatibility; the index is what lets an agent jump to
    # a definition without a grep round.
    symbols_index: dict[str, list[str]] = {}
    # I5 (Voyager's retrieval half): module docstrings become a searchable index
    # of executable artifacts (scenarios, tools, libs) so agents find existing
    # skills before writing new ones. Consumed by `doc-find`.
    modules_index: dict[str, str] = {}
    for rel, tree in sorted(parsed.items()):
        top = rel.split("/", 1)[0]
        communities.setdefault(top, []).append(rel)
        doc = ast.get_docstring(tree)
        if doc:
            modules_index[rel] = " ".join(line.strip() for line in doc.strip().splitlines()[:6] if line.strip())[:300]
        for ast_node in ast.walk(tree):
            if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols_index.setdefault(ast_node.name, []).append(f"{rel}:{ast_node.lineno}")
        node = {
            "id": rel,
            "path": rel,
            "name": rel,
            "community": top,
            "language": "python",
            "source": "graphify-code-ast",
            **_symbols(tree),
        }
        nodes.append(node)
        for target in sorted(_import_targets(tree)):
            target_path = module_to_path.get(target)
            if target_path and target_path != rel:
                edges.append({"source": rel, "target": target_path, "type": "imports", "sourceKind": "graphify-code-ast"})

    graph: dict[str, Any] = {
        "schemaVersion": "graphify-code-ast/v1",
        # The identity of the inputs this graph was built FROM, ACCUMULATED from the
        # bytes the parse loop above actually read (T1.5) — NOT a re-scan of current disk,
        # which would false-FRESH over a file mutated mid-build. `graph-staleness` compares
        # this against a freshly computed key; equal means fresh regardless of the clock.
        "inputKey": key_hash.hexdigest(),
        "generator": "scripts/harness_lib/graphify_code_ast.py",
        "source": "graphify-code-ast",
        "policy": "graphify-code-ast-first-source-verified",
        "nodes": nodes,
        "edges": edges,
        "communities": {key: sorted(value) for key, value in sorted(communities.items())},
        "stats": {"filesDiscovered": len(files), "filesParsed": len(parsed), "parseErrors": len(errors), "edges": len(edges),
                  "durationS": round(time.monotonic() - build_t0, 3)},
        "errors": errors[:50],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "# Graphify Code AST Graph Report",
        "",
        "Source: Graphify Python AST, dependency-free and offline.",
        "Policy: graphify-code-ast-first, source-verified.",
        "",
        f"- Files discovered: {len(files)}",
        f"- Files parsed: {len(parsed)}",
        f"- Import edges: {len(edges)}",
        f"- Parse errors: {len(errors)}",
        f"- Symbols indexed: {len(symbols_index)} (`symbols.json`: name -> path:line — check it before grepping for a definition)",
        "",
        "This report is a navigation aid only. Verify all graph-derived findings in source/spec/test/config files before editing or closing work.",
    ]
    graph_path = output_dir / "graph.json"
    # Crash-safe publication ORDER (T1.4): graph.json LAST. It carries the inputKey the
    # freshness surface reads; if it landed first and a crash followed, its new key would
    # pair with stale sidecars forever and read false-FRESH. Publishing it last means any
    # crash leaves the OLD graph.json (old key) in place -> honest STALE, never false-fresh.
    _atomic_write_text(output_dir / "symbols.json",
                       json.dumps({k: symbols_index[k] for k in sorted(symbols_index)}, indent=1, ensure_ascii=False) + "\n")
    _atomic_write_text(output_dir / "modules.json",
                       json.dumps(modules_index, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    _atomic_write_text(output_dir / "GRAPH_REPORT.md", "\n".join(report_lines) + "\n")
    _atomic_write_text(graph_path, json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    def _display(path: Path) -> str:
        # SPEC-110: the output may live under the harness while the subject root
        # is an external target repository — relative_to(root) no longer holds.
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    return {"graphPath": _display(graph_path),
            "reportPath": _display(output_dir / "GRAPH_REPORT.md"), **graph["stats"]}


#!/usr/bin/env python3
"""Read-only docs+specs inventory: the top-level `docs-tree` verb (Wave-0).

Walks `docs/` and `specs/` ONLY and reports one row per entry: `path` (posix,
relative to the repo root), `depth`, `kind` (`dir`|`md`|`other`), `title`
(first `# ` heading for .md files, read errors-ignore) and `tag` (the
top-level scope segment: `docs`, `docs/roadmap`, `specs/40-features`, ...).
Depth-first, children sorted by name: sorted, deterministic, byte-stable.
Observe-only: prints an indented text tree (or --json), always exits 0,
changes nothing. Registered via cli_registry.register() with zero harness.py
edits (spec: specs/40-features/docs-tree.md).
"""
from __future__ import annotations

import json
from pathlib import Path

from harness_lib.common import ROOT
from harness_lib.spec_conformance import _HEADING

_ROOTS = ("docs", "specs")


def _title(path: Path) -> str:
    """First level-1 `# ` heading of a markdown file; '' when absent."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    first_h1 = next((m for m in _HEADING.finditer(text) if len(m.group(1)) == 1), None)
    return first_h1.group(2).strip() if first_h1 else ""


def _tag(parts: tuple[str, ...], kind: str) -> str:
    """Scope tag: the containing directory (the entry itself for dirs),
    truncated to at most two segments -- `docs`, `docs/roadmap`,
    `specs/40-features`, ..."""
    scope = parts if kind == "dir" else parts[:-1]
    return "/".join(scope[:2])


def collect(root: Path | str) -> list[dict]:
    """One row per entry under docs/ and specs/: path, depth, kind, title, tag."""
    root = Path(root)
    rows: list[dict] = []

    def walk(rel_parts: tuple[str, ...]) -> None:
        path = root.joinpath(*rel_parts)
        kind = ("dir" if path.is_dir()
                else "md" if path.suffix.lower() == ".md" else "other")
        rows.append({
            "path": "/".join(rel_parts),
            "depth": len(rel_parts) - 1,
            "kind": kind,
            "title": _title(path) if kind == "md" else "",
            "tag": _tag(rel_parts, kind),
        })
        if kind == "dir":
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                walk(rel_parts + (child.name,))

    for top in _ROOTS:
        if (root / top).is_dir():
            walk((top,))
    return rows


def cmd_docs_tree(args) -> int:
    """`harness.py docs-tree`: read-only inventory; indented tree or --json, rc 0."""
    rows = collect(ROOT)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for r in rows:
        name = r["path"].rsplit("/", 1)[-1] + ("/" if r["kind"] == "dir" else "")
        # Title last and unpadded so one long heading never widens the tree.
        print("  " * r["depth"] + name + (f"  {r['title']}" if r["title"] else ""))
    print(f"\n{len(rows)} entries")
    return 0

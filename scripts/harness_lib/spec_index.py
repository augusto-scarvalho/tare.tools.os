#!/usr/bin/env python3
"""Read-only spec inventory: the top-level `spec-index` verb (Panel/UX Wave-0).

Walks `specs/**/*.md` and reports, per spec: scope (first path segment under
specs/), title (first `# ` heading), its `Scenario:[id]`s, and legacy-frozen
status (`.harness/project.json` -> `specConformance.legacy`). Scenario ids are
parsed with the SPEC-116 conformance gate's own gherkin parser so both surfaces
always agree on what counts as a scenario. Observe-only: prints a table (or
--json), always exits 0, changes nothing. Registered via cli_registry.register()
with zero harness.py edits (spec: specs/40-features/spec-index.md).
"""
from __future__ import annotations

import json
from pathlib import Path

from harness_lib.common import ROOT, read_json
from harness_lib.spec_conformance import _HEADING, _gherkin_ids


def collect(root: Path | str) -> list[dict]:
    """One row per specs/**/*.md: path, scope, title, scenarioIds, legacyFrozen."""
    root = Path(root)
    specs_dir = root / "specs"
    project = read_json(root / ".harness" / "project.json", {}) or {}
    legacy = set(((project.get("specConformance") or {}).get("legacy")) or [])
    rows: list[dict] = []
    if not specs_dir.is_dir():
        return rows
    for path in sorted(specs_dir.rglob("*.md")):
        rel = path.relative_to(specs_dir).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        first_h1 = next((m for m in _HEADING.finditer(text) if len(m.group(1)) == 1), None)
        rows.append({
            "path": rel,
            "scope": rel.split("/", 1)[0] if "/" in rel else "-",
            "title": first_h1.group(2).strip() if first_h1 else "",
            "scenarioIds": _gherkin_ids(text),
            # The live legacy list stores basenames (spec_conformance matches
            # spec.name); accept a relpath entry too so the flag can't rot.
            "legacyFrozen": rel in legacy or path.name in legacy,
        })
    return rows


def cmd_spec_index(args) -> int:
    """`harness.py spec-index`: read-only inventory; text table or --json, rc 0."""
    rows = collect(ROOT)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    # SCENARIOS last: it is the one unbounded column, and the last column is
    # never padded, so one long id list can't widen every row.
    header = ("SCOPE", "SPEC", "TITLE", "FROZEN", "SCENARIOS")
    table = [(r["scope"], r["path"], r["title"],
              "frozen" if r["legacyFrozen"] else "-",
              ",".join(r["scenarioIds"]) or "-") for r in rows]
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h)
              for i, h in enumerate(header)]
    for row in (header, *table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    print(f"\n{len(table)} spec(s)")
    return 0

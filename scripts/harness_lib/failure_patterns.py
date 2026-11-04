#!/usr/bin/env python3
"""OB.1 failure-pattern clustering: the top-level `failure-patterns` verb.

Deterministic rollup over the two durable failure sources:

- the escalations ledger (`.harness/state/escalations.json`) — raised AND
  resolved records, the same enumeration `records.collect_records` uses
  (`read_json` + `records.ESCALATIONS_REL`; the resolved half is exactly the
  repeat-failure history a pending-only view would hide);
- worklog/archive entries whose kind/tags mark a failure, read via
  `records._load` (the ledger's canonical reader).

Groups by `(failureClass, role, executor)` with missing fields bucketed as
`"(none)"` — mirroring `cost_metrics._group`'s tolerance — sorted by count
desc, then lastAt desc. Verify-on-demand: pure reads, ZERO writes, no LLM,
no daemon (observation must pay for itself). Registered via
`cli_registry.register()` with zero harness.py edits
(spec: specs/40-features/failure-patterns.md).
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from harness_lib import records
from harness_lib.common import ROOT, read_json

# ponytail: escalations_lib.list_escalations() is bound to harness.py globals,
# compacts (writes) on read, and drops resolved records — so this rollup reads
# the durable ledger file the way records.collect_records does instead.


def _events(root: Path) -> Iterator[tuple[dict[str, Any], str]]:
    """(entry, source-rel) per failure event; every entry carries an `at`."""
    esc = read_json(root / records.ESCALATIONS_REL, {}) or {}
    raised = list((esc.get("raised") or {}).values())
    resolved = list((esc.get("resolvedRecords") or {}).values())
    for v in raised + resolved:
        if isinstance(v, dict):
            yield {**v, "at": v.get("resolvedAt") or v.get("raisedAt")}, records.ESCALATIONS_REL
    for rel in (records.WORKLOG_REL, records.ARCHIVE_REL):
        for e in records._load(root, rel):
            marks = [str(e.get("kind") or ""), *(str(t) for t in e.get("tags") or [])]
            if any("failure" in m.lower() for m in marks):
                yield e, rel


def rollup(root: Path | str) -> list[dict[str, Any]]:
    """Cluster failure events by (failureClass, role, executor); pure read.

    Missing fields group under "(none)" (cost_metrics._group tolerance).
    Sorted by count desc, then lastAt desc (ISO timestamps sort lexically).
    """
    root = Path(root)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e, src in _events(root):
        key = tuple(str(e.get(f) or "(none)") for f in ("failureClass", "role", "executor"))
        g = groups.setdefault(key, {"failureClass": key[0], "role": key[1], "executor": key[2],
                                    "count": 0, "lastAt": "", "sources": []})
        g["count"] += 1
        g["lastAt"] = max(g["lastAt"], str(e.get("at") or ""))
        if src not in g["sources"]:
            g["sources"].append(src)
    return sorted(groups.values(), key=lambda g: (g["count"], g["lastAt"]), reverse=True)


def cmd_failure_patterns(args) -> int:
    """`harness.py failure-patterns`: read-only; text table or --json, rc 0."""
    rows = rollup(ROOT)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    header = ("COUNT", "CLASS", "ROLE", "EXECUTOR", "LAST", "SOURCES")
    table = [(str(r["count"]), r["failureClass"], r["role"], r["executor"],
              r["lastAt"] or "-", ",".join(r["sources"])) for r in rows]
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h)
              for i, h in enumerate(header)]
    for row in (header, *table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    print(f"\n{len(table)} pattern(s)")
    return 0

#!/usr/bin/env python3
"""Research-round admin: the top-level `research` verb (RF.2).

Throwaway research rounds (docs/research/*.md) accumulate with lingering
active workflows and records references and no cleanup path. `research list`
is a deterministic inventory (slug, tracked-in-git, linked WFs, records
references); `research delete <slug>` is DRY-RUN BY DEFAULT — it reports what
it WOULD remove and deletes nothing until `--apply`, and a git-tracked round
additionally requires `--allow-committed`. Apply reuses the existing
`workflow scrub --force` for linked WFs and writes a tombstone record via
records.add_entry. Registered via cli_registry.register() with zero
harness.py edits (spec: specs/40-features/research-admin.md).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness_lib import records
from harness_lib.common import ROOT, HarnessError, read_json

RESEARCH_REL = "docs/research"
INDEX_DOC = "RESEARCH.md"  # the index, never a deletable round


def _tracked(root: Path, rel: str) -> bool:
    """True iff git tracks the doc. Fail-closed: when git cannot answer
    (rc != 0, missing git, timeout), report tracked=True so a delete refuses
    rather than silently destroying committed history."""
    try:
        proc = subprocess.run(["git", "ls-files", "--", rel], cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except Exception:
        return True
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def _active_wf_tasks(root: Path) -> list[tuple[str, str]]:
    active = root / ".harness" / "workflows" / "active"
    out: list[tuple[str, str]] = []
    if not active.is_dir():
        return out
    for wf in sorted(active.iterdir()):
        try:
            data = read_json(wf / "workflow.json", {}) or {}
        except Exception:
            continue
        if isinstance(data, dict):
            out.append((wf.name, str(data.get("task") or "")))
    return out


def rounds(root: Path | str) -> list[dict]:
    """One row per docs/research/*.md round (RESEARCH.md index excluded):
    {slug, path, tracked, linkedWfs, recordsRefs}. Read-only inventory —
    records.search may refresh its derived (gitignored) index, canonical
    state is never touched."""
    root = Path(root)
    rdir = root / RESEARCH_REL
    rows: list[dict] = []
    if not rdir.is_dir():
        return rows
    wf_tasks = _active_wf_tasks(root)
    for path in sorted(rdir.glob("*.md")):
        if path.name == INDEX_DOC:
            continue
        slug = path.stem
        rows.append({
            "slug": slug,
            "path": f"{RESEARCH_REL}/{path.name}",
            "tracked": _tracked(root, f"{RESEARCH_REL}/{path.name}"),
            "linkedWfs": [wfid for wfid, task in wf_tasks if slug in task],
            "recordsRefs": len(records.search(root, slug)),
        })
    return rows


def delete_round(root: Path | str, slug: str, *, apply: bool = False,
                 allow_committed: bool = False) -> dict:
    """Guarded delete of one research round; returns a status report dict.

    HARD invariants:
    - the slug is UNTRUSTED input: the target must resolve() DIRECTLY under
      docs/research/ (prefix check) — traversal/nesting is refused;
    - dry-run (the default) performs ZERO writes/deletes — records and
      workflows are never touched, the report only says what --apply would do;
    - a git-tracked round is refused unless allow_committed is set.
    """
    root = Path(root)
    base = (root / RESEARCH_REL).resolve()
    doc = (base / f"{slug}.md").resolve()
    if not doc.is_relative_to(base) or doc.parent != base or doc.name == INDEX_DOC:
        return {"slug": slug, "status": "refused",
                "reason": "target does not resolve to a round directly under docs/research/"}
    row = next((r for r in rounds(root) if r["slug"] == doc.stem), None)
    if row is None:
        return {"slug": slug, "status": "not-found",
                "reason": f"no research round '{doc.stem}' under docs/research/"}
    if not apply:
        return {**row, "status": "dry-run",
                "wouldRemove": {"doc": row["path"], "linkedWfs": row["linkedWfs"],
                                "recordsRefs": row["recordsRefs"]},
                "reason": "dry-run: nothing was deleted; pass --apply to act"}
    if row["tracked"] and not allow_committed:
        return {**row, "status": "refused",
                "reason": "round is git-tracked; re-run with --allow-committed "
                          "to delete committed research"}
    doc.unlink()
    scrubbed = []
    for wfid in row["linkedWfs"]:
        # ponytail: reuse the existing scrub verb via subprocess instead of a
        # second lifecycle implementation — scrub owns the safety rules.
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "harness.py"),
             "workflow", "scrub", wfid, "--force"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        scrubbed.append({"wfid": wfid, "rc": proc.returncode})
    records.add_entry(root, "note", f"research round {doc.stem} deleted",
                      tags=["research", "tombstone"])
    return {**row, "status": "deleted", "scrubbed": scrubbed}


def cmd_research(args) -> int:
    """`harness.py research list|show|delete <slug> [--json|--apply|--allow-committed]`."""
    if args.action == "round":
        from harness_lib import research_round
        return research_round.cmd_round(args)
    if args.action == "show":
        # research-index R0: the parsed, NON-AUTHORITATIVE round view (or the
        # full snapshot with --json for the future gallery screen).
        from harness_lib import research_index
        if getattr(args, "json", False) and not args.slug:
            print(json.dumps(research_index.research_snapshot(ROOT),
                             indent=2, ensure_ascii=False))
            return 0
        if not args.slug:
            raise HarnessError("research show requires a <slug> (or --json for the full snapshot)\n"
                               "fix: python scripts/harness.py research list")
        print(research_index.render_show(ROOT, args.slug))
        return 0
    if args.action == "list":
        rows = rounds(ROOT)
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        header = ("SLUG", "TRACKED", "WFS", "RECORDS", "PATH")
        table = [(r["slug"], "yes" if r["tracked"] else "no",
                  ",".join(r["linkedWfs"]) or "-", str(r["recordsRefs"]), r["path"])
                 for r in rows]
        widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h)
                  for i, h in enumerate(header)]
        for row in (header, *table):
            print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        print(f"\n{len(table)} research round(s)")
        return 0
    if not args.slug:
        raise HarnessError("research delete requires a <slug>\n"
                           "fix: python scripts/harness.py research list, "
                           "then research delete <slug>")
    report = delete_round(ROOT, args.slug, apply=args.apply,
                          allow_committed=args.allow_committed)
    if report["status"] in ("refused", "not-found"):
        raise HarnessError(f"research delete {args.slug}: {report['reason']}\n"
                           "fix: python scripts/harness.py research list")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0

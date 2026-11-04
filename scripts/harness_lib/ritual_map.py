#!/usr/bin/env python3
"""ritual-enforcement-map — no overseer ritual step stays silently unenforced.

Sibling of `security_directives` (same id scheme, imported not copied): the two
overseer playbooks stay the single source of RITUAL STEPS; this module derives a
stable id per top-level step (`<stem>:<sha1(normalized full text)[:8]>`) and
checks every id against the tracked mapping file
`testing/ritual-enforcement-map.json`, whose entries declare — explicitly and
diff-reviewably — who enforces each step:

  status "hook:<locator>"      a git/agent hook blocks it at an edge;
  status "gate:<check-name>"   a gate check fails on it;
  status "leg:<verify-leg>"    a verify leg (mutate/reckon/audit) joins on it;
  status "doctor:<rule>"       a doctor/advisor WARN watches it live;
  status "advisory:<what>"     a non-blocking nudge only — named, evadable;
  status "gap:<reason>"        accepted UNENFORCED — visible debt, never silent.

The gate check `ritual-enforcement-map` FAILS when a step has no mapping (a new
OR REWORDED step forces a fresh enforcement decision in the same commit — the id
hashes the step's FULL text, so editing its prose retires the old id) or when a
mapping is stale (its step no longer exists). Mapping-file-only: the playbooks
are read, NEVER written.

Self-check: `python scripts/harness_lib/ritual_map.py`.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib.security_directives import directive_id  # noqa: E402  (shared id scheme)

RITUAL_SOURCES = (".harness/prompts/overseer-playbook.md",
                  ".harness/prompts/overseer-loop-playbook.md")
MAP_REL = "testing/ritual-enforcement-map.json"
_STATUS_RE = re.compile(r"^(hook:.+|gate:.+|leg:.+|doctor:.+|advisory:.+|gap:.+)$")
_STEP_RE = re.compile(r"^(-\s|\d+[a-z]?\.\s)")  # top-level bullet or numbered step, column 0


def _steps_in(text: str) -> list[str]:
    """Full text of each top-level step: starts at a bullet/numbered line, runs to the
    next step start, `#` heading, fence, or blank-line-then-unindented-text."""
    out: list[str] = []
    cur: list[str] = []
    in_fence = False
    blank = False
    for raw in text.splitlines():
        line = raw.strip()
        start = end = False
        if line.startswith("```"):
            in_fence = not in_fence
            end = True
        elif in_fence or line.startswith("<!--"):
            continue  # fenced code and HTML comment lines are never steps
        elif not line:
            blank = True
            continue
        elif line.startswith("#"):
            end = True
        elif _STEP_RE.match(raw):
            start = True
        elif blank and raw[:1] not in " \t":
            end = True  # blank line then unindented prose: the step ended
        if (start or end) and cur:
            out.append(" ".join(cur))
            cur = []
        if start:
            cur = [line]
        elif not end and cur:
            cur.append(line)  # wrapped/indented continuation of the open step
        blank = False
    if cur:
        out.append(" ".join(cur))
    return out


def steps(root: Path | str) -> list[dict[str, str]]:
    """One row per top-level ritual step across the two overseer playbooks."""
    root = Path(root)
    out: list[dict[str, str]] = []
    for rel in RITUAL_SOURCES:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for step in _steps_in(text):
            out.append({"id": directive_id(path.stem, step), "file": rel, "text": step})
    return out


def load_map(root: Path | str) -> dict[str, Any]:
    try:
        doc = json.loads((Path(root) / MAP_REL).read_text(encoding="utf-8"))
        return doc.get("map") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def evaluate(root: Path | str) -> dict[str, Any]:
    """{count, unmapped, stale, invalid, byStatus} — the gate consumes this."""
    root = Path(root)
    rows = steps(root)
    mapping = load_map(root)
    ids = {r["id"] for r in rows}
    unmapped = [r for r in rows if r["id"] not in mapping]
    stale = sorted(set(mapping) - ids)
    invalid = sorted(i for i, entry in mapping.items()
                     if not (isinstance(entry, dict)
                             and _STATUS_RE.match(str(entry.get("status") or ""))))
    by_status: dict[str, int] = {}
    for i, entry in mapping.items():
        if i in ids and isinstance(entry, dict):
            kind = str(entry.get("status") or "?").split(":", 1)[0]
            by_status[kind] = by_status.get(kind, 0) + 1
    return {"count": len(rows), "unmapped": unmapped, "stale": stale,
            "invalid": invalid, "byStatus": by_status}


def seed_entries(root: Path | str) -> dict[str, dict[str, str]]:
    """Skeleton entries for every unmapped step (status left an honest dated `gap:`
    for the human to upgrade) — `python ritual_map.py --seed` prints them; nothing
    is written automatically."""
    report = evaluate(root)
    return {r["id"]: {"text": r["text"][:100],
                      "status": "gap:needs-triage (baseline 2026-08-03)"}
            for r in report["unmapped"]}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prompts = root / ".harness" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "overseer-playbook.md").write_text(
            "# X\n\n<!-- ambient:start -->\n- Bullet   step one\n  wrapped.\n"
            "<!-- ambient:end -->\n\n## Ritual\n\n1. Numbered step.\n\n"
            "```\n- not a step (fenced)\n```\n\nplain prose, not a step\n",
            encoding="utf-8")
        (prompts / "overseer-loop-playbook.md").write_text(
            "## Loop\n\n- Loop step.\n", encoding="utf-8")
        rows = steps(root)
        assert [r["text"] for r in rows] == [
            "- Bullet   step one wrapped.", "1. Numbered step.", "- Loop step."], rows
        assert rows[0]["id"].startswith("overseer-playbook:")
        assert rows[2]["id"].startswith("overseer-loop-playbook:")
        assert directive_id("s", "  A   b\n") == directive_id("s", "a B")  # normalized
        report = evaluate(root)
        assert len(report["unmapped"]) == 3 and report["stale"] == []
        (root / "testing").mkdir()
        (root / MAP_REL).write_text(json.dumps({"map": {
            rows[0]["id"]: {"text": "s1", "status": "hook:commit-msg (x)"},
            rows[1]["id"]: {"text": "s2", "status": "gap:needs-triage (baseline 2026-08-03)"},
            rows[2]["id"]: {"text": "s3", "status": "gate:some-check"},
            "ghost:00000000": {"text": "gone", "status": "gap:retired"},
            }}), encoding="utf-8")
        report = evaluate(root)
        assert report["unmapped"] == [] and report["stale"] == ["ghost:00000000"]
        assert report["byStatus"] == {"hook": 1, "gap": 1, "gate": 1}, report
        (root / MAP_REL).write_text(json.dumps({"map": {
            rows[0]["id"]: {"status": "banana"}}}), encoding="utf-8")
        assert evaluate(root)["invalid"] == [rows[0]["id"]]
    print("ritual_map self-check: ok")


if __name__ == "__main__":
    if "--seed" in sys.argv:
        # Playbook steps carry non-cp1252 punctuation (`gate ‖ reckon`), and this
        # seed is meant to be redirected into the UTF-8 map file — pin the stream.
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        repo = Path(__file__).resolve().parents[2]
        print(json.dumps(seed_entries(repo), indent=2, ensure_ascii=False))
    else:
        _demo()

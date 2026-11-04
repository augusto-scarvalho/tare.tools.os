#!/usr/bin/env python3
"""research-index (R0) — structured index over docs/research round docs.

`parse_round(path)` derives, best-effort, the gallery-grade shape of one
hand-written Double-Diamond round doc: phases present, question, declared
budget, evidence-matrix counts by confidence class, wave WF-ids, experiment
headings — with a `parseErrors` field instead of ever crashing on a
hand-written doc (the round doc stays canonical; every parsed view is a
NON-AUTHORITATIVE derived view, the context-digest stance).

`research_snapshot(root)` joins the parsed rounds with the admin inventory
(tracked/linkedWfs from research_admin) and any LIVE workflow whose profile
starts with `research-`. Consumed by `research show <slug>` (CLI, this slice)
and later by `GET /api/research` (gui-research-screen, OPEN).

Self-check: `python scripts/harness_lib/research_index.py`.
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
from harness_lib.common import read_json  # noqa: E402

_PHASE = re.compile(r"^##\s+Phase\s+(\d)\b", re.MULTILINE)
_WFID = re.compile(r"WF-\d{8}-\d{6}-\d+")
_CONFIDENCE = ("forte", "moderada", "fraca")
_ROUND_STATE = re.compile(
    r"<!-- round-state:start -->\s*```json\s*(.*?)\s*```\s*<!-- round-state:end -->",
    re.IGNORECASE | re.DOTALL,
)


def _section(text: str, marker: str, cap: int = 300) -> str:
    """First paragraph following a bold marker like `**Question.**`."""
    idx = text.find(marker)
    if idx < 0:
        return ""
    body = text[idx + len(marker):].strip()
    para = body.split("\n\n", 1)[0].replace("\n", " ").strip()
    return para[:cap]


def _evidence_counts(text: str) -> dict[str, Any]:
    """Data rows of the first table under the Phase 1 heading, counted by
    confidence class (a cell matching forte/moderada/fraca)."""
    m = re.search(r"^##\s+Phase\s+1\b.*?$", text, re.MULTILINE)
    if not m:
        return {"count": 0, "byConfidence": {}}
    tail = text[m.end():]
    nxt = re.search(r"^##\s", tail, re.MULTILINE)
    block = tail[: nxt.start()] if nxt else tail
    count = 0
    by_conf: dict[str, int] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().lower() for c in line.strip("|").split("|")]
        if not cells or all(re.match(r"^:?-+:?$", c) for c in cells if c):
            continue
        if cells[0] in ("claim", "evidência", "evidencia"):
            continue  # header row
        count += 1
        conf = next((c for cell in cells for c in _CONFIDENCE if c == cell), None)
        if conf:
            by_conf[conf] = by_conf.get(conf, 0) + 1
    return {"count": count, "byConfidence": by_conf}


def _round_spec(text: str) -> dict[str, Any]:
    error: json.JSONDecodeError | None = None
    for match in re.finditer(r"^```json\s*$\n(.*?)^```\s*$", text,
                             re.IGNORECASE | re.MULTILINE | re.DOTALL):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            error = exc
            continue
        if isinstance(value, dict) and "schemaVersion" in value:
            return value
    if error:
        raise error
    return {}


def _round_state(text: str) -> dict[str, Any]:
    match = _ROUND_STATE.search(text)
    if not match:
        return {}
    value = json.loads(match.group(1))
    return value if isinstance(value, dict) else {}


def parse_round(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    out: dict[str, Any] = {"slug": path.stem, "title": "", "question": "",
                           "budget": "", "phases": [], "evidence": {"count": 0, "byConfidence": {}},
                           "waves": [], "experiments": 0, "roundSpec": {}, "roundState": {},
                           "parseErrors": []}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        out["parseErrors"].append(f"read: {type(exc).__name__}")
        return out
    for name, fn in (
        ("title", lambda: next((ln.lstrip("# ").strip() for ln in text.splitlines()
                                if ln.startswith("# ")), "")),
        ("phases", lambda: sorted({int(n) for n in _PHASE.findall(text)})),
        ("question", lambda: _section(text, "**Question.**")),
        ("budget", lambda: _section(text, "**Declared budget.**", cap=200)),
        ("evidence", lambda: _evidence_counts(text)),
        ("waves", lambda: sorted(set(_WFID.findall(text)))),
        ("experiments", lambda: len(re.findall(r"^###.*experiment", text,
                                               re.IGNORECASE | re.MULTILINE))),
        ("roundSpec", lambda: _round_spec(text)),
        ("roundState", lambda: _round_state(text)),
    ):
        try:
            out[name] = fn()
        except Exception as exc:  # a hand-written doc must never crash the index
            out["parseErrors"].append(f"{name}: {type(exc).__name__}")
    return out


def _live_research_workflows(root: Path) -> list[dict[str, Any]]:
    active = root / ".harness" / "workflows" / "active"
    rows: list[dict[str, Any]] = []
    if not active.is_dir():
        return rows
    for wf_file in sorted(active.glob("WF-*/workflow.json")):
        try:
            wf = read_json(wf_file, {}) or {}
        except Exception:
            continue
        profile = str(wf.get("workflowProfile") or "")
        if profile.startswith("research"):
            rows.append({"workflowId": str(wf.get("workflowId") or wf_file.parent.name),
                         "profile": profile, "phase": str(wf.get("phase") or "planned"),
                         "task": str(wf.get("task") or "")[:120]})
    return rows


def research_snapshot(root: Path | str) -> dict[str, Any]:
    """Rounds (parsed + admin fields) + live research workflows."""
    from harness_lib import research_admin  # local: admin also lives on this verb

    root = Path(root)
    admin = {row["slug"]: row for row in research_admin.rounds(root)}
    rounds: list[dict[str, Any]] = []
    for slug, row in admin.items():
        parsed = parse_round(root / row["path"])
        rounds.append({**parsed, "tracked": row["tracked"],
                       "linkedWfs": row["linkedWfs"]})
    return {"rounds": rounds, "liveWorkflows": _live_research_workflows(root),
            "count": len(rounds)}


def render_show(root: Path | str, slug: str) -> str:
    """`research show <slug>` text — the derived, non-authoritative view."""
    from harness_lib.common import HarnessError

    root = Path(root)
    path = root / "docs" / "research" / f"{slug}.md"
    if not path.is_file():
        known = sorted(p.stem for p in (root / "docs" / "research").glob("*.md")
                       if p.name != "RESEARCH.md")
        raise HarnessError(f"unknown research round: {slug}\n"
                           f"fix: one of {', '.join(known) or '(none)'}")
    r = parse_round(path)
    conf = ", ".join(f"{k}={v}" for k, v in sorted(r["evidence"]["byConfidence"].items()))
    lines = [
        f"round: {r['slug']} — {r['title']}",
        f"phases present: {', '.join(str(p) for p in r['phases']) or '(none parsed)'}",
        f"question: {r['question'] or '(not parsed)'}",
        f"budget: {r['budget'] or '(not parsed)'}",
        f"evidence rows: {r['evidence']['count']}" + (f" ({conf})" if conf else ""),
        f"waves: {', '.join(r['waves']) or '(none referenced)'}",
        f"experiments: {r['experiments']}",
    ]
    if r["parseErrors"]:
        lines.append(f"parse errors: {', '.join(r['parseErrors'])}")
    lines.append(f"source of truth: docs/research/{slug}.md (this view is derived)")
    return "\n".join(lines)


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp) / "round.md"
        doc.write_text(
            "# Research round — demo\n\n"
            "## Phase 0 — Question, criteria, budget\n\n"
            "**Question.** What should we adopt?\n\n"
            "**Declared budget.** one wave.\n\n"
            "## Phase 1 — Evidence matrix\n\n"
            "| claim | source | conf |\n|---|---|---|\n"
            "| a | s | forte |\n| b | s | moderada |\n| c | s | forte |\n\n"
            "## Phase 3 — Waves\n\nWF-20260101-000000-000001 ran.\n\n"
            "### Experiment: probe X\n", encoding="utf-8")
        r = parse_round(doc)
        assert r["phases"] == [0, 1, 3] and r["question"].startswith("What should")
        assert r["evidence"] == {"count": 3, "byConfidence": {"forte": 2, "moderada": 1}}, r
        assert r["waves"] == ["WF-20260101-000000-000001"] and r["experiments"] == 1
        assert r["parseErrors"] == [], r
        garbage = Path(tmp) / "bad.md"
        garbage.write_text("no headings | broken | table |\n", encoding="utf-8")
        rb = parse_round(garbage)
        assert rb["phases"] == [] and rb["evidence"]["count"] == 0
    print("research_index self-check: ok")


if __name__ == "__main__":
    _demo()

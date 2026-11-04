"""SPEC-104 M4: read-only supervision snapshot page.

Pure renderer: ``render_status_html(data)`` takes the dict assembled by
``harness.py collect_status_snapshot()`` and returns one self-contained HTML
document (inline CSS, native <details>, no JS, no external references).
It answers the five supervisor questions at a glance — what is running, what
waits on a human, is the last gate green, is anything drifted, what is the
next handoff — grouped by supervision point, never a raw event stream.

Rendering only: any missing datum here is a CLI/state-layer gap and must be
fixed there (SPEC-104 M4.2), not patched in this module.
"""
from __future__ import annotations

import html
from typing import Any

SECTION_CAP = 20

_CSS = """
body { font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; margin: 2rem auto; max-width: 68rem; padding: 0 1rem; color: #1a1d21; background: #fafafa; }
h1 { font-size: 1.35rem; margin-bottom: 0.2rem; }
.stamp { color: #666; font-size: 0.85rem; margin-bottom: 1.6rem; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(11.5rem, 1fr)); gap: 0.7rem; margin-bottom: 1.8rem; }
.tile { background: #fff; border: 1px solid #ddd; border-left: 4px solid #999; border-radius: 4px; padding: 0.7rem 0.9rem; }
.tile .q { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: #777; }
.tile .a { font-size: 1.05rem; font-weight: 600; margin-top: 0.2rem; overflow-wrap: anywhere; }
.tile .sub { font-size: 0.8rem; color: #555; margin-top: 0.15rem; overflow-wrap: anywhere; }
.tile.ok { border-left-color: #2e8b57; }
.tile.warn { border-left-color: #cc8400; }
.tile.bad { border-left-color: #c0392b; }
details { background: #fff; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 0.8rem; padding: 0.5rem 0.9rem; }
summary { cursor: pointer; font-weight: 600; padding: 0.25rem 0; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; font-size: 0.86rem; }
th, td { text-align: left; border-bottom: 1px solid #eee; padding: 0.3rem 0.5rem; vertical-align: top; overflow-wrap: anywhere; }
th { color: #666; font-weight: 600; }
.muted { color: #777; font-style: italic; }
.unavailable { color: #c0392b; font-style: italic; }
code { background: #f0f0f0; padding: 0.05rem 0.3rem; border-radius: 3px; font-size: 0.85em; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def _tile(question: str, answer: str, tone: str, sub: str = "") -> str:
    sub_html = f'<div class="sub">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="tile {tone}"><div class="q">{_esc(question)}</div>'
            f'<div class="a">{_esc(answer)}</div>{sub_html}</div>')


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="muted">nothing here</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows[:SECTION_CAP])
    cap = f'<p class="muted">showing first {SECTION_CAP} of {len(rows)}</p>' if len(rows) > SECTION_CAP else ""
    return f"<table><tr>{head}</tr>{body}</table>{cap}"


def _section(title: str, data: Any, body_fn) -> str:
    if isinstance(data, dict) and data.get("unavailable"):
        inner = f'<p class="unavailable">unavailable: {_esc(data["unavailable"])}</p>'
    else:
        inner = body_fn(data)
    return f"<details open><summary>{_esc(title)}</summary>{inner}</details>"


def render_status_html(data: dict[str, Any]) -> str:
    esc = data.get("escalations", {})
    wfs = data.get("workflows", {})
    quality = data.get("quality", {})
    drift = data.get("protectedDrift", {})
    handoff = data.get("handoff", {})

    def unavailable(section: Any) -> bool:
        return isinstance(section, dict) and bool(section.get("unavailable"))

    # -- five answer tiles ---------------------------------------------------
    tiles: list[str] = []
    if unavailable(wfs):
        tiles.append(_tile("What is running?", "unknown", "warn", wfs["unavailable"]))
    else:
        active = wfs.get("active", [])
        tiles.append(_tile("What is running?", f"{len(active)} active workflow(s)",
                           "ok" if not active else "warn",
                           "; ".join(w.get("workflowId", "?") for w in active[:3])))
    if unavailable(esc):
        tiles.append(_tile("Waiting on a human?", "unknown", "warn", esc["unavailable"]))
    else:
        count = int(esc.get("count", 0))
        tiles.append(_tile("Waiting on a human?", f"{count} escalation(s)",
                           "ok" if count == 0 else "bad",
                           "nothing pending" if count == 0 else "see escalations below"))
    if unavailable(quality):
        tiles.append(_tile("Last gate green?", "unknown", "warn", quality["unavailable"]))
    else:
        last = quality.get("lastValidation", {}) or {}
        status = str(last.get("status", "never run"))
        tiles.append(_tile("Last gate green?", f"{last.get('gate', '—')}: {status}",
                           "ok" if status == "pass" else ("warn" if status == "never run" else "bad"),
                           str(last.get("validatedAt", ""))))
    if unavailable(drift):
        tiles.append(_tile("Anything drifted?", "unknown", "warn", drift["unavailable"]))
    else:
        errors = drift.get("errors", [])
        tiles.append(_tile("Anything drifted?", "no drift" if not errors else f"{len(errors)} drifted file(s)",
                           "ok" if not errors else "bad",
                           "" if not errors else str(errors[0])[:80]))
    if unavailable(handoff):
        tiles.append(_tile("Next handoff?", "unknown", "warn", handoff["unavailable"]))
    else:
        tiles.append(_tile("Next handoff?", str(handoff.get("taskProfile", "—")),
                           "ok", str(handoff.get("task", ""))[:120]))

    # -- detail sections -----------------------------------------------------
    def esc_body(section: dict[str, Any]) -> str:
        rows = [[e.get("escalationId"), e.get("suggestedProfile"), e.get("failureClass"),
                 e.get("reason"), e.get("raisedAt")] for e in section.get("pending", [])]
        hint = ('<p class="muted">resolve with: <code>python scripts/harness.py escalations --resolve &lt;id&gt;</code></p>'
                if rows else "")
        return _table(["id", "suggested profile", "failure class", "reason", "raised at"], rows) + hint

    def wf_body(section: dict[str, Any]) -> str:
        rows = []
        for w in section.get("active", []):
            counts = w.get("workerStatusCounts", {})
            rows.append([w.get("workflowId"), w.get("phase"), w.get("task", "")[:80],
                         ", ".join(f"{k}={v}" for k, v in counts.items() if v) or "—",
                         w.get("plannedTokens", "—")])
        return _table(["workflow", "phase", "task", "workers", "planned tokens"], rows)

    def quality_body(section: dict[str, Any]) -> str:
        last = section.get("lastValidation", {}) or {}
        counts = last.get("counts", {}) or {}
        rows = [[last.get("gate"), last.get("status"), counts.get("pass"), counts.get("fail"),
                 counts.get("skip"), last.get("validatedAt")]]
        return _table(["gate", "status", "pass", "fail", "skip", "when"], rows)

    def drift_body(section: dict[str, Any]) -> str:
        errors = section.get("errors", [])
        if not errors:
            return '<p class="muted">all protected files match the snapshot</p>'
        fix = ('<p class="muted">intentional? <code>python tools/hooks/protect_canonical_files.py snapshot</code>'
               " — otherwise inspect the diff first</p>")
        return _table(["drift"], [[e] for e in errors]) + fix

    def handoff_body(section: dict[str, Any]) -> str:
        budget = section.get("contextBudget", {}) or {}
        rows = [["task", section.get("task")], ["profile", section.get("taskProfile")],
                ["generated", section.get("generatedAt")],
                ["required read tokens", budget.get("estimatedRequiredReadTokens")]]
        return _table(["field", "value"], rows)

    sections = [
        _section("Escalations (waiting on a human)", esc, esc_body),
        _section("Active workflows", wfs, wf_body),
        _section("Validation (last gate)", quality, quality_body),
        _section("Protected files", drift, drift_body),
        _section("Current handoff", handoff, handoff_body),
    ]

    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>Harness supervision — {_esc(data.get('project', 'harness'))}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<h1>Harness supervision — {_esc(data.get('project', 'harness'))}</h1>\n"
        f'<p class="stamp">snapshot generated {_esc(data.get("generatedAt"))} — static page, not live; '
        "regenerate with <code>python scripts/harness.py status --html</code></p>\n"
        f'<div class="tiles">{"".join(tiles)}</div>\n'
        + "\n".join(sections)
        + "\n</body>\n</html>\n"
    )

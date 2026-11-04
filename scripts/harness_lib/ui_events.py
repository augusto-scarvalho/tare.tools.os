"""Read-only /api/events tail over `.harness/runs/events.jsonl` (api-events-endpoint).

The backend gap the GUI plan §12 tracked: the hash-chained event log (parentEventId,
T-CAUSALPARENT) had no API — WB5's deterministic-transitions timeline and the GUI-OP3
workflow drill both need it. One endpoint, two consumers:

    /api/events?limit=100            — global tail (the Workbench LEDGER footer)
    /api/events?workflow=WF-…        — one workflow's transitions (OP3 drill)
    /api/events?event=workflow_…     — filter by event name

Contract: read-only over the live file; ascending (file) order; malformed lines are
SKIPPED (never crash, never fabricate); every string VALUE passes secret_scan.redact_text
before egress (the P5 pre-egress discipline — events carry task text/paths); `limit`
capped at 500. GUI-writes-no-state: pure GET.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct self-check execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib.secret_scan import redact_text

_LIMIT_DEFAULT = 100
_LIMIT_CAP = 500


def _redact_values(obj: Any, depth: int = 0) -> Any:
    """Every string value redacted; structure preserved. Depth-bounded (events are
    shallow; a pathological nest degrades to its repr, redacted)."""
    if depth > 6:
        return redact_text(str(obj))
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_values(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_values(v, depth + 1) for v in obj]
    return obj


def events_tail(root: Path | str, *, limit: int | None = None,
                event: str | None = None, workflow: str | None = None) -> dict[str, Any]:
    """The filtered tail, ascending file order. Missing/unreadable log -> empty
    (honest zero, never an error the panel must special-case)."""
    root = Path(root)
    try:
        n = int(limit) if limit is not None else _LIMIT_DEFAULT
    except (TypeError, ValueError):
        n = _LIMIT_DEFAULT
    n = max(1, min(n, _LIMIT_CAP))
    path = root / ".harness" / "runs" / "events.jsonl"
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        if event and str(row.get("event") or "") != event:
            continue
        if workflow and str(row.get("workflowId") or "") != workflow:
            continue
        rows.append(row)
    tail = [_redact_values(r) for r in rows[-n:]]
    return {"count": len(tail), "total": len(rows), "skipped": skipped,
            "filters": {"event": event, "workflow": workflow, "limit": n},
            "events": tail}


def _self_check() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runs = root / ".harness" / "runs"
        runs.mkdir(parents=True)
        lines = [
            json.dumps({"event": "workflow_started", "workflowId": "WF-1", "task": "a"}),
            "NOT JSON {",
            json.dumps({"event": "workflow_reduced", "workflowId": "WF-2",
                        "note": "key sk-FAKEKEYdonotleak0123456789abcdef here"}),  # mojibake-ok: planted fixture
            json.dumps(["a", "list"]),
            json.dumps({"event": "workflow_started", "workflowId": "WF-2"}),
        ]
        (runs / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        out = events_tail(root)
        assert out["count"] == 3 and out["skipped"] == 2, out
        assert "sk-FAKEKEYdonotleak0123456789abcdef" not in json.dumps(out), "redaction must fire"

        assert events_tail(root, workflow="WF-2")["count"] == 2
        assert events_tail(root, event="workflow_started")["count"] == 2
        assert events_tail(root, event="workflow_started", workflow="WF-2")["count"] == 1
        assert events_tail(root, limit=1)["count"] == 1
        assert events_tail(root, limit=99999)["filters"]["limit"] == _LIMIT_CAP
        assert events_tail(root, limit="bogus")["filters"]["limit"] == _LIMIT_DEFAULT  # type: ignore[arg-type]
        assert events_tail(Path(tmp) / "nowhere")["count"] == 0  # missing log -> honest zero
    print("ui_events self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())

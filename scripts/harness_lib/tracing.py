"""Dependency-free workflow trace helpers.

The harness keeps its core portable by writing OpenTelemetry-shaped JSONL spans
without requiring an OTLP exporter at template time. Product adopters can tail
or transform these files into OTLP/LangSmith/Phoenix later.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from harness_lib import log_rotation


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def stable_trace_id(workflow_id: str | None = None) -> str:
    if workflow_id:
        return hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:32]
    return secrets.token_hex(16)


def span_id(seed: str | None = None) -> str:
    if seed:
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return secrets.token_hex(8)


def trace_path(root: Path, workflow_id: str | None = None) -> Path:
    if workflow_id:
        safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in workflow_id)
        return root / ".harness" / "workflows" / "active" / safe / "trace.jsonl"
    return root / ".harness" / "runs" / "harness-trace.jsonl"


def build_span(
    name: str,
    *,
    workflow_id: str | None = None,
    worker_id: str | None = None,
    status: str = "ok",
    attributes: dict[str, Any] | None = None,
    event_time: str | None = None,
) -> dict[str, Any]:
    attrs = dict(attributes or {})
    if workflow_id:
        attrs.setdefault("workflow.id", workflow_id)
    if worker_id:
        attrs.setdefault("worker.id", worker_id)
    timestamp = event_time or utc_now()
    return {
        "schemaVersion": "1.0",
        "traceId": stable_trace_id(workflow_id),
        "spanId": span_id(f"{workflow_id or 'harness'}:{worker_id or ''}:{name}:{timestamp}"),
        "parentSpanId": attrs.pop("parentSpanId", None),
        "name": name,
        "kind": attrs.pop("span.kind", "internal"),
        "startTime": timestamp,
        "endTime": timestamp,
        "status": status,
        "attributes": attrs,
    }


def append_span(root: Path, span: dict[str, Any], *, workflow_id: str | None = None) -> Path:
    path = trace_path(root, workflow_id or span.get("attributes", {}).get("workflow.id"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.name == "runs":  # R1: rotate only the unbounded GLOBAL log; per-WF traces die with the WF
        log_rotation.spill_if_over(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(span, ensure_ascii=False) + "\n")
    return path


def append_event_span(root: Path, event: str, payload: dict[str, Any] | None = None) -> Path:
    payload = dict(payload or {})
    workflow_id = str(payload.get("workflowId") or payload.get("workflow.id") or "") or None
    worker_id = str(payload.get("workerId") or payload.get("worker.id") or "") or None
    status = str(payload.get("status") or payload.get("phase") or "ok")
    attrs = {f"harness.{k}": v for k, v in payload.items() if k not in {"workflowId", "workerId", "timestamp"}}
    attrs["harness.event"] = event
    if "timestamp" in payload:
        attrs["event.timestamp"] = payload["timestamp"]
    span = build_span(
        f"harness.{event}",
        workflow_id=workflow_id,
        worker_id=worker_id,
        status=status,
        attributes=attrs,
        event_time=str(payload.get("timestamp")) if payload.get("timestamp") else None,
    )
    return append_span(root, span, workflow_id=workflow_id)


def read_trace(root: Path, workflow_id: str) -> list[dict[str, Any]]:
    path = trace_path(root, workflow_id)
    if not path.exists():
        return []
    spans: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            spans.append(item)
    return spans


def summarize_trace(spans: list[dict[str, Any]]) -> dict[str, Any]:
    names: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for span in spans:
        names[str(span.get("name", "unknown"))] = names.get(str(span.get("name", "unknown")), 0) + 1
        statuses[str(span.get("status", "unknown"))] = statuses.get(str(span.get("status", "unknown")), 0) + 1
    trace_id = spans[0].get("traceId") if spans else None
    return {"traceId": trace_id, "spanCount": len(spans), "nameCounts": names, "statusCounts": statuses}

"""Workflow observability: trace read/export/opt-in push + the tail handler.

Extracted from scripts/harness.py (line-budget burndown, owner-approved
2026-07-13; codex drafted the trace trio, overseer added tail). Router
callbacks arrive via bind() — same seam as workflow_writes/workflow_lifecycle:
ROOT, append_event, workflow_config, workflow_dir, workflow_tail,
workflow_async_status, ASYNC_GROUP_TERMINAL_STATES.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from harness_lib import observability_exporters, tracing
from harness_lib.common import HarnessError


def bind(env: dict[str, Any]) -> None:
    """Receive workflow callbacks from the public CLI wrapper."""
    globals().update(env)


def workflow_trace(wfid: str, *, limit: int | None = None) -> dict[str, Any]:
    spans = tracing.read_trace(ROOT, wfid)
    if limit is not None and limit >= 0:
        spans = spans[-limit:]
    return {"workflowId": wfid, "summary": tracing.summarize_trace(spans), "spans": spans}


def trace_digest(wfid: str, limit: int | None = None) -> dict[str, Any]:
    spans = workflow_trace(wfid, limit=limit)["spans"]
    if not spans:
        return {"workflowId": wfid, "spans": 0, "timeline": [], "failures": []}

    error_indexes: list[int] = []
    phase_indexes: list[int] = []
    workers: dict[str, dict[str, Any]] = {}
    for index, span in enumerate(spans):
        name = str(span.get("name", "unknown"))
        status = str(span.get("status", "unknown"))
        attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
        worker_id = attributes.get("worker.id")
        errorish = status not in {"ok", "running", "done", "fulfilled"}
        if errorish:
            error_indexes.append(index)
        if name.startswith("harness.workflow"):
            phase_indexes.append(index)
        if worker_id is not None:
            worker = workers.setdefault(str(worker_id), {"lastStatus": status})
            worker["lastStatus"] = status
            if errorish and "firstErrorSpan" not in worker:
                worker["firstErrorSpan"] = {"name": name, "time": span.get("startTime")}

    indexes = sorted({0, len(spans) - 1, *error_indexes, *phase_indexes})
    if len(indexes) > 20:
        kept = {0, len(spans) - 1}
        for index in [*error_indexes, *phase_indexes]:
            if len(kept) == 20:
                break
            kept.add(index)
        indexes = sorted(kept)

    timeline = []
    for index in indexes:
        span = spans[index]
        entry = {"time": span.get("startTime"), "name": span.get("name"), "status": span.get("status")}
        attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
        if "worker.id" in attributes:
            entry["workerId"] = attributes["worker.id"]
        timeline.append(entry)

    failures = []
    for worker_id, worker in workers.items():
        if "firstErrorSpan" not in worker:
            continue
        logs = ROOT / ".harness" / "workflows" / "active" / wfid / "run-logs"
        evidence = {}
        for key, suffix in (("stdoutLog", "stdout"), ("stderrLog", "stderr")):
            path = logs / f"{worker_id}.{suffix}.log"
            evidence[key] = path.relative_to(ROOT).as_posix() if path.exists() else None
        failures.append({"workerId": worker_id, **worker, "evidence": evidence})

    summary = tracing.summarize_trace(spans)
    summary.update({"errorSpanCount": len(error_indexes), "workersWithErrors": len(failures)})
    return {"workflowId": wfid, "spans": len(spans), "summary": summary,
            "timeline": timeline, "failures": failures}


def workflow_trace_export(wfid: str, *, export_format: str = "otlp-json", output: str | None = None, limit: int | None = None) -> dict[str, Any]:
    trace = workflow_trace(wfid, limit=limit)
    spans = trace.get("spans", []) if isinstance(trace.get("spans"), list) else []
    out_path = ROOT / output if output else None
    text = observability_exporters.export_trace(spans, export_format, out_path)
    if spans:
        append_event("workflow_trace_exported", {"workflowId": wfid, "format": export_format, "spans": len(spans), "output": str(out_path.relative_to(ROOT)) if out_path else None})
    return {
        "workflowId": wfid,
        "format": export_format,
        "spanCount": len(spans),
        "outputPath": str(out_path.relative_to(ROOT)) if out_path else None,
        "bytes": len(text.encode("utf-8")),
        "preview": None if out_path else text,
    }


def workflow_trace_push(wfid: str, *, target: str = "otlp-http", endpoint: str | None = None, api_key_env: str | None = None, limit: int | None = None, send: bool = False, timeout: int = 10) -> dict[str, Any]:
    trace = workflow_trace(wfid, limit=limit)
    spans = trace.get("spans", []) if isinstance(trace.get("spans"), list) else []
    configured = workflow_config().get("observability", {}) if isinstance(workflow_config().get("observability"), dict) else {}
    endpoints = configured.get("tracePushEndpoints", {}) if isinstance(configured.get("tracePushEndpoints"), dict) else {}
    endpoint = endpoint or str(endpoints.get(target) or "")
    if not endpoint:
        raise HarnessError("trace-push requires --endpoint or workflows.observability.tracePushEndpoints[target]")
    policy = configured.get("networkExportPolicy", {}) if isinstance(configured.get("networkExportPolicy"), dict) else {}
    allowed_hosts = policy.get("allowedHosts", []) if isinstance(policy.get("allowedHosts", []), list) else []
    max_payload_bytes = int(policy.get("maxPayloadBytes", 1_000_000) or 1_000_000)
    request_plan = observability_exporters.build_trace_push_request(spans, target=target, endpoint=endpoint, api_key_env=api_key_env, timeout_seconds=timeout, allowed_hosts=allowed_hosts)
    request_plan.update({
        "workflowId": wfid,
        "dryRun": not send,
        "sendPolicy": {"optInEnv": "HARNESS_ALLOW_NETWORK_EXPORT", "allowedHosts": allowed_hosts, "maxPayloadBytes": max_payload_bytes},
        "payloadPolicy": {"maxPayloadBytes": max_payload_bytes, "bodyBytes": int(request_plan.get("bodyBytes") or 0), "allowed": int(request_plan.get("bodyBytes") or 0) <= max_payload_bytes},
    })
    if send:
        if os.environ.get("HARNESS_ALLOW_NETWORK_EXPORT") != "1":
            raise HarnessError("network trace push is disabled by default; set HARNESS_ALLOW_NETWORK_EXPORT=1 to opt in")
        if not request_plan.get("endpointPolicy", {}).get("allowed"):
            reason = str(request_plan.get("endpointPolicy", {}).get("reason") or "endpoint blocked")
            raise HarnessError(f"network trace push endpoint blocked by workflows.observability.networkExportPolicy.allowedHosts and endpoint secret policy: {reason}")
        if not request_plan.get("payloadPolicy", {}).get("allowed"):
            raise HarnessError("network trace push payload exceeds workflows.observability.networkExportPolicy.maxPayloadBytes")
        import urllib.request

        body = observability_exporters.export_trace(spans, request_plan["format"], output=None).encode("utf-8")
        headers = observability_exporters.default_headers(target, api_key_env=api_key_env)
        secret_env = api_key_env or ("LANGSMITH_API_KEY" if target == "langsmith" else "PHOENIX_API_KEY" if target == "phoenix" else "")
        if secret_env and os.environ.get(secret_env):
            headers["x-api-key" if target == "langsmith" else "authorization"] = os.environ[secret_env] if target == "langsmith" else "Bearer " + os.environ[secret_env]
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec - explicit operator opt-in only
            request_plan["responseStatus"] = int(getattr(response, "status", 0) or 0)
    if spans or workflow_dir(wfid).exists():
        append_event("workflow_trace_push_planned" if not send else "workflow_trace_pushed", {"workflowId": wfid, "target": target, "spans": len(spans), "dryRun": not send})
    return request_plan


def cmd_workflow_tail(args) -> None:
    lines = max(1, min(1000, args.lines))
    if not args.follow:
        print(json.dumps(workflow_tail(args.workflow_id, worker_id=args.worker_id, lines=lines), indent=2, ensure_ascii=False))
        return
    # SPEC-118 --follow: per-file byte offsets (seeded at current size), read new
    # bytes each poll, print complete lines prefixed by worker ([id]=stdout,
    # [id!]=stderr). Read-only; exits on the group's terminal state or --timeout.
    snap = workflow_tail(args.workflow_id, worker_id=args.worker_id, lines=1)
    streams: list[dict[str, Any]] = []
    for w in snap.get("workers", []):
        for key, suffix in (("stdout", ""), ("stderr", "!")):
            rel = w.get(key + "Path")
            if not rel:
                continue
            streams.append({"label": f"[{w['workerId']}{suffix}]", "path": ROOT / rel,
                            "offset": int((w.get(key) or {}).get("size") or 0), "buf": ""})

    def _drain(s: dict[str, Any]) -> None:
        if not s["path"].exists():
            return
        try:
            with s["path"].open("rb") as fh:
                fh.seek(s["offset"])
                chunk = fh.read()
                s["offset"] = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        text = s["buf"] + chunk.decode("utf-8", errors="replace")
        parts = text.split("\n")
        s["buf"] = parts.pop()  # trailing partial line waits for the next poll
        for line in parts:
            print(f"{s['label']} {line.rstrip(chr(13))}", flush=True)  # normalize CRLF

    deadline = time.time() + args.timeout if args.timeout else None
    poll = max(0.1, args.poll_interval)
    while True:
        for s in streams:
            _drain(s)
        # R6: tail is read-only — recover=False (it can list pids and MARK orphaned).
        status = workflow_async_status(args.workflow_id, recover=False)
        if (status.get("asyncGroup") or {}).get("status") in ASYNC_GROUP_TERMINAL_STATES:
            for s in streams:
                _drain(s)  # final pass so lines written just before settle are seen
            break
        if deadline is not None and time.time() >= deadline:
            break
        time.sleep(poll)
    # R9 (v2): a final line naming the group's state and the next command to run.
    group = (status.get("asyncGroup") or {}).get("status")
    nxt = "collect" if group in ASYNC_GROUP_TERMINAL_STATES else "await"
    print(json.dumps({"asyncGroup": group,
                      "next": f"python scripts/harness.py workflow {nxt} {args.workflow_id}"},
                     ensure_ascii=False), flush=True)

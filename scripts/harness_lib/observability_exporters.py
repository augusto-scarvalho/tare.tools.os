"""Dependency-free trace exporters for product observability handoff.

The harness writes portable workflow-local spans to trace.jsonl. This module
turns that contract into collector-friendly files without requiring network
access or vendor SDKs in the template.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

SUPPORTED_TRACE_EXPORT_FORMATS = {"otlp-json", "langsmith-jsonl", "phoenix-jsonl"}

SENSITIVE_KEY_PARTS = {
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "private_key",
    "client_secret",
    "access_key",
    "x-api-key",
}


def is_sensitive_key(key: str) -> bool:
    normalized = str(key).lower().replace("-", "_").replace(".", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_string(value: str) -> tuple[str, int]:
    import re

    redactions = 0
    patterns = [
        (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer <redacted>"),
        (re.compile(r"sk-[A-Za-z0-9]{8,}", re.IGNORECASE), "sk-<redacted>"),
        (re.compile(r"ghp_[A-Za-z0-9_]{8,}", re.IGNORECASE), "ghp_<redacted>"),
        (re.compile(r"glpat-[A-Za-z0-9_\-]{8,}", re.IGNORECASE), "glpat-<redacted>"),
        (re.compile(r"AKIA[0-9A-Z]{12,}", re.IGNORECASE), "AKIA<redacted>"),
        (re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}", re.IGNORECASE), "nvapi-<redacted>"),
        (re.compile(r"AIza[A-Za-z0-9_\-]{8,}"), "AIza<redacted>"),
    ]
    output = value
    for pattern, replacement in patterns:
        output, count = pattern.subn(replacement, output)
        redactions += count
    return output, redactions


def redact_url(value: str) -> tuple[str, int]:
    from urllib.parse import parse_qsl, urlencode, urlunparse

    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return redact_string(value)
    redactions = 0
    username = parsed.username
    password = parsed.password
    hostname = parsed.hostname or ""
    netloc = parsed.netloc
    if username or password:
        redactions += 1
        host_part = hostname
        if parsed.port:
            host_part += f":{parsed.port}"
        netloc = f"<redacted>@{host_part}"
    query_pairs = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if is_sensitive_key(key):
            query_pairs.append((key, "<redacted>"))
            redactions += 1
        else:
            item, n = redact_string(item)
            redactions += n
            query_pairs.append((key, item))
    path, n = redact_string(parsed.path)
    redactions += n
    fragment, n = redact_string(parsed.fragment)
    redactions += n
    return urlunparse((parsed.scheme, netloc, path, parsed.params, urlencode(query_pairs), fragment)), redactions


def sanitize_trace_value(value: Any, *, key: str | None = None) -> tuple[Any, int]:
    if key is not None and is_sensitive_key(key):
        return "<redacted>", 1
    if isinstance(value, dict):
        redactions = 0
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            sanitized_value, count = sanitize_trace_value(child_value, key=str(child_key))
            sanitized[str(child_key)] = sanitized_value
            redactions += count
        return sanitized, redactions
    if isinstance(value, list):
        redactions = 0
        sanitized_list = []
        for item in value:
            sanitized_value, count = sanitize_trace_value(item)
            sanitized_list.append(sanitized_value)
            redactions += count
        return sanitized_list, redactions
    if isinstance(value, tuple):
        sanitized, count = sanitize_trace_value(list(value))
        return sanitized, count
    if isinstance(value, str):
        if "://" in value:
            return redact_url(value)
        return redact_string(value)
    return value, 0


def sanitize_spans(spans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sanitized_spans: list[dict[str, Any]] = []
    total_redactions = 0
    for span in spans:
        sanitized, count = sanitize_trace_value(span)
        if isinstance(sanitized, dict):
            sanitized_spans.append(sanitized)
        total_redactions += count
    return sanitized_spans, {
        "enabled": True,
        "redactedFields": total_redactions,
        "sensitiveKeyParts": sorted(SENSITIVE_KEY_PARTS),
    }



def _ns_to_unix_nano(timestamp: str | None) -> str:
    """Best-effort RFC3339-ish timestamp to unix-nano string.

    The harness trace contract stores ISO timestamps. OTLP JSON accepts integer
    nanoseconds encoded as strings. Keep the conversion defensive so malformed
    local evidence never breaks exporter invocation.
    """
    if not timestamp:
        return "0"
    try:
        import datetime as dt

        text = timestamp.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return str(int(parsed.timestamp() * 1_000_000_000))
    except Exception:
        return "0"


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"stringValue": ""}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [{"key": str(k), "value": _any_value(v)} for k, v in sorted(value.items(), key=lambda item: str(item[0]))]}}
    return {"stringValue": str(value)}


def _attrs(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(k), "value": _any_value(v)} for k, v in sorted(attributes.items(), key=lambda item: str(item[0]))]


def to_otlp_json(spans: list[dict[str, Any]], *, service_name: str = "universal-agent-harness") -> dict[str, Any]:
    otel_spans: list[dict[str, Any]] = []
    for span in spans:
        attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
        otel_spans.append({
            "traceId": str(span.get("traceId") or ""),
            "spanId": str(span.get("spanId") or ""),
            "parentSpanId": str(span.get("parentSpanId") or "") if span.get("parentSpanId") else "",
            "name": str(span.get("name") or "harness.span"),
            "kind": 1,
            "startTimeUnixNano": _ns_to_unix_nano(str(span.get("startTime") or "")),
            "endTimeUnixNano": _ns_to_unix_nano(str(span.get("endTime") or span.get("startTime") or "")),
            "attributes": _attrs(attributes),
            "status": {"code": 2 if str(span.get("status") or "ok").lower() in {"error", "fail", "failed"} else 1, "message": str(span.get("status") or "ok")},
        })
    return {
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
            "scopeSpans": [{"scope": {"name": "universal-agent-harness"}, "spans": otel_spans}],
        }]
    }


def to_langsmith_jsonl(spans: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for span in spans:
        attrs = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
        payload = {
            "id": span.get("spanId"),
            "trace_id": span.get("traceId"),
            "name": span.get("name"),
            "run_type": attrs.get("harness.event", "chain"),
            "start_time": span.get("startTime"),
            "end_time": span.get("endTime"),
            "status": span.get("status"),
            "metadata": attrs,
        }
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def to_phoenix_jsonl(spans: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for span in spans:
        attrs = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
        payload = {
            "context": {"trace_id": span.get("traceId"), "span_id": span.get("spanId"), "parent_id": span.get("parentSpanId")},
            "name": span.get("name"),
            "start_time": span.get("startTime"),
            "end_time": span.get("endTime"),
            "status_code": "ERROR" if str(span.get("status") or "ok").lower() in {"error", "fail", "failed"} else "OK",
            "attributes": attrs,
        }
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def export_trace(spans: list[dict[str, Any]], export_format: str, output: Path | None = None, *, sanitize: bool = True) -> str:
    fmt = export_format.lower()
    if fmt not in SUPPORTED_TRACE_EXPORT_FORMATS:
        raise ValueError(f"unsupported trace export format: {export_format}")
    export_spans = sanitize_spans(spans)[0] if sanitize else spans
    if fmt == "otlp-json":
        text = json.dumps(to_otlp_json(export_spans), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    elif fmt == "langsmith-jsonl":
        text = to_langsmith_jsonl(export_spans)
    else:
        text = to_phoenix_jsonl(export_spans)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return text


def trace_push_endpoint_policy(endpoint: str, *, allowed_hosts: Iterable[str] | None = None) -> dict[str, Any]:
    """Validate a trace-push endpoint against a conservative send allowlist.

    Dry-runs may build plans for any http(s) endpoint so teams can review the
    request shape. Actual ``--send`` operations should be guarded by the caller
    with this policy result and an explicit operator opt-in. The policy fails
    closed when endpoint credentials or sensitive query params are present,
    even for allowlisted hosts, because those values can bypass trace-payload
    redaction and leak through collector URLs, logs, proxies, or access logs.
    """
    from urllib.parse import parse_qsl

    parsed = urlparse(endpoint)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    allowed = {str(item).lower() for item in (allowed_hosts or []) if str(item).strip()}
    sensitive_query_params = sorted({key for key, _value in parse_qsl(parsed.query, keep_blank_values=True) if is_sensitive_key(key)})
    has_userinfo = bool(parsed.username or parsed.password)
    base = {
        "scheme": scheme,
        "host": host,
        "hasUserInfo": has_userinfo,
        "sensitiveQueryParams": sensitive_query_params,
    }
    if scheme not in {"http", "https"}:
        return {**base, "allowed": False, "reason": "endpoint scheme must be http or https"}
    if not host:
        return {**base, "allowed": False, "reason": "endpoint host is required"}
    if has_userinfo:
        return {**base, "allowed": False, "reason": "endpoint must not contain embedded credentials"}
    if sensitive_query_params:
        return {**base, "allowed": False, "reason": "endpoint query contains sensitive parameters"}
    if allowed and host.lower() not in allowed:
        return {**base, "allowed": False, "reason": "endpoint host is not in workflows.observability.networkExportPolicy.allowedHosts"}
    return {**base, "allowed": True, "reason": "allowed"}

SUPPORTED_TRACE_PUSH_TARGETS = {"otlp-http", "langsmith", "phoenix"}


def target_export_format(target: str) -> str:
    target_norm = target.lower()
    if target_norm == "otlp-http":
        return "otlp-json"
    if target_norm == "langsmith":
        return "langsmith-jsonl"
    if target_norm == "phoenix":
        return "phoenix-jsonl"
    raise ValueError(f"unsupported trace push target: {target}")


def default_headers(target: str, *, api_key_env: str | None = None) -> dict[str, str]:
    target_norm = target.lower()
    headers = {"content-type": "application/json" if target_norm == "otlp-http" else "application/x-ndjson"}
    if target_norm == "langsmith":
        headers["x-api-key"] = f"${{{api_key_env or 'LANGSMITH_API_KEY'}}}"
    elif target_norm == "phoenix":
        headers["authorization"] = f"Bearer ${{{api_key_env or 'PHOENIX_API_KEY'}}}"
    elif api_key_env:
        headers["authorization"] = f"Bearer ${{{api_key_env}}}"
    return headers


def build_trace_push_request(
    spans: list[dict[str, Any]],
    *,
    target: str,
    endpoint: str,
    api_key_env: str | None = None,
    timeout_seconds: int = 10,
    allowed_hosts: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a safe, dependency-free trace push request plan.

    The template defaults to dry-run planning. It never reads or prints secret
    values; headers contain only env-var placeholders so adopters can wire the
    request into their own network/exporter layer.
    """
    policy = trace_push_endpoint_policy(endpoint, allowed_hosts=allowed_hosts)
    if not policy.get("scheme") in {"http", "https"}:
        raise ValueError("trace push endpoint must start with http:// or https://")
    fmt = target_export_format(target)
    sanitized_spans, redaction = sanitize_spans(spans)
    body = export_trace(sanitized_spans, fmt, output=None, sanitize=False)
    body_bytes = len(body.encode("utf-8"))
    headers = default_headers(target, api_key_env=api_key_env)
    redacted_headers = {key: ("<redacted-env>" if "$" in value or "Bearer" in value or is_sensitive_key(key) else value) for key, value in headers.items()}
    redacted_endpoint, endpoint_redactions = redact_url(endpoint)
    if endpoint_redactions:
        redaction["redactedFields"] = int(redaction.get("redactedFields", 0)) + endpoint_redactions
    return {
        "target": target.lower(),
        "format": fmt,
        "method": "POST",
        "endpoint": redacted_endpoint,
        "endpointRedacted": redacted_endpoint != endpoint,
        "endpointHost": policy.get("host"),
        "endpointPolicy": policy,
        "headers": redacted_headers,
        "timeoutSeconds": timeout_seconds,
        "spanCount": len(spans),
        "bodyBytes": body_bytes,
        "bodySha256": __import__("hashlib").sha256(body.encode("utf-8")).hexdigest(),
        "redaction": redaction,
        "networkRequired": True,
        "safeByDefault": True,
    }

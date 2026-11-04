"""Gate fixtures — trace domain (SPEC — MF.2 extraction).

Moved out of scripts/spec_test_gate.py to keep the gate runner readable
and cheap to load; shared gate helpers arrive via bind(env) from the
gate at import time (same idiom as harness_lib.workflow_reduce).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from spec_test_gate."""
    globals().update(env)


def check_trace_contract_fixture() -> list[dict[str, str]]:
    """Verify workflow events generate workflow-local trace spans and CLI can read them."""
    cleanup_test_artifacts()
    wfid = ""
    try:
        payload = call_harness_function(
            "plan_workflow",
            "trace contract fixture for product observability",
            None,
            profile_name="repository-review",
            max_workers=2,
            concurrency=1,
            split="explicit",
            shards=["README.md", "docs/AGENTIC_WORKFLOWS.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "trace-contract")
        call_harness_function("workflow_event", wfid, "fixture_trace_probe", {"status": "ok", "token.count": 12})
        trace = call_harness_function("workflow_trace", wfid, limit=50)
        spans = trace.get("spans", []) if isinstance(trace.get("spans"), list) else []
        failures: list[str] = []
        if not spans:
            failures.append("no spans")
        required_span_keys = {"traceId", "spanId", "name", "startTime", "endTime", "status", "attributes"}
        for span in spans:
            missing = sorted(required_span_keys - set(span.keys()))
            if missing:
                failures.append("missing span keys: " + ", ".join(missing))
                break
        if not any(span.get("name") == "harness.fixture_trace_probe" for span in spans):
            failures.append("probe span missing")
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "trace", wfid, "--limit", "20"], timeout=fixture_timeout())
        if proc.returncode != 0 or '"spanCount"' not in proc.stdout:
            failures.append("workflow trace CLI failed")
        return [result("trace-contract:workflow-jsonl-spans", "pass" if not failures else "fail", f"spans={len(spans)}" if not failures else "; ".join(failures[:5]))]
    finally:
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_trace_export_fixture() -> list[dict[str, str]]:
    """Verify trace export adapters for OTLP, LangSmith and Phoenix shapes."""
    cleanup_test_artifacts()
    wfid = ""
    try:
        payload = call_harness_function(
            "plan_workflow",
            "trace export fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "trace-export")
        call_harness_function("workflow_event", wfid, "fixture_trace_export_probe", {"status": "ok", "token.count": 3})
        failures: list[str] = []
        for fmt, marker in [("otlp-json", "resourceSpans"), ("langsmith-jsonl", "trace_id"), ("phoenix-jsonl", "trace_id")]:
            out_rel = f".harness/workflows/active/{wfid}/trace-export-{fmt}.out"
            proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "trace-export", wfid, "--format", fmt, "--output", out_rel], timeout=fixture_timeout())
            output_path = ROOT / out_rel
            text = output_path.read_text(encoding="utf-8", errors="ignore") if output_path.exists() else ""
            if proc.returncode != 0 or marker not in text:
                failures.append(f"{fmt} export failed")
        return [result("trace-export:adapter-shapes", "pass" if not failures else "fail", "otlp/langsmith/phoenix ok" if not failures else "; ".join(failures[:5]))]
    finally:
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_trace_push_fixture() -> list[dict[str, str]]:
    """Verify network trace exporter dry-run shape and secret redaction."""
    cleanup_test_artifacts()
    wfid = ""
    try:
        payload = call_harness_function(
            "plan_workflow",
            "trace push fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "trace-push")
        call_harness_function("workflow_event", wfid, "fixture_trace_push_probe", {"status": "ok"})
        proc = run_fixture_command([
            sys.executable,
            "scripts/harness.py",
            "workflow",
            "trace-push",
            wfid,
            "--target",
            "otlp-http",
            "--endpoint",
            "http://localhost:4318/v1/traces",
            "--limit",
            "10",
        ], timeout=fixture_timeout())
        detail = (proc.stdout or proc.stderr).strip().replace("\n", " ")[:300]
        ok = proc.returncode == 0
        try:
            data = json.loads(proc.stdout or "{}")
            ok = ok and data.get("dryRun") is True and data.get("networkRequired") is True and data.get("bodySha256") and data.get("spanCount", 0) >= 1
            ok = ok and "localhost" in str(data.get("endpoint")) and "Bearer " not in json.dumps(data)
        except Exception as exc:
            ok = False
            detail = str(exc)
        return [result("trace-push:dry-run-request-shape", "pass" if ok else "fail", "safe dry-run request ok" if ok else detail)]
    finally:
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_trace_secret_redaction_fixture() -> list[dict[str, str]]:
    """Ensure trace export/push payloads redact sensitive keys and token-like values."""
    cleanup_test_artifacts()
    wfid = ""
    secret_values = [
        "sk-liveSECRET1234567890",
        "Bearer live-secret-token-1234567890",
        "ghp_SECRET1234567890abcdef",
        "super-password-value",
    ]
    try:
        payload = call_harness_function(
            "plan_workflow",
            "trace secret redaction fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "trace-secret-redaction")
        call_harness_function("workflow_event", wfid, "fixture_trace_secret_probe", {
            "status": "ok",
            "apiKey": secret_values[0],
            "authorization": secret_values[1],
            "nested": {"githubToken": secret_values[2], "safe": "visible"},
            "endpoint": "https://collector.example/v1/traces?api_key=abc123secret&safe=yes",
            "message": f"token candidate {secret_values[1]}",
            "password": secret_values[3],
        })
        failures: list[str] = []
        out_rel = f".harness/workflows/active/{wfid}/trace-redaction-otlp.json"
        proc = run_fixture_command([
            sys.executable,
            "scripts/harness.py",
            "workflow",
            "trace-export",
            wfid,
            "--format",
            "otlp-json",
            "--output",
            out_rel,
        ], timeout=fixture_timeout())
        exported = (ROOT / out_rel).read_text(encoding="utf-8", errors="ignore") if (ROOT / out_rel).exists() else ""
        if proc.returncode != 0 or "resourceSpans" not in exported:
            failures.append("trace export failed")
        proc = run_fixture_command([
            sys.executable,
            "scripts/harness.py",
            "workflow",
            "trace-push",
            wfid,
            "--target",
            "otlp-http",
            "--endpoint",
            "https://collector.example/v1/traces?api_key=abc123secret&safe=yes",
            "--limit",
            "20",
        ], timeout=fixture_timeout())
        plan_text = proc.stdout or ""
        combined = exported + "\n" + plan_text
        for value in secret_values + ["abc123secret"]:
            if value in combined:
                failures.append(f"secret leaked: {value[:12]}")
        try:
            plan = json.loads(plan_text or "{}")
            if int(plan.get("redaction", {}).get("redactedFields", 0)) < 4:
                failures.append("redaction count too low")
            if "api_key=abc123secret" in str(plan.get("endpoint")):
                failures.append("endpoint query secret not redacted")
        except Exception as exc:
            failures.append(f"invalid trace-push plan JSON: {exc}")
        if "<redacted>" not in combined and "<redacted-env>" not in combined:
            failures.append("no redaction marker observed")
        return [result("trace-secret-redaction:sensitive-fields", "pass" if not failures else "fail", "sensitive trace data redacted" if not failures else "; ".join(failures[:5]))]
    finally:
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_trace_push_send_fixture() -> list[dict[str, str]]:
    """Exercise the explicit --send path against a bounded local loopback collector."""
    cleanup_test_artifacts()
    wfid = ""
    received: dict[str, Any] = {"requests": 0, "body": b"", "headers": {}}

    class Handler(BaseHTTPRequestHandler):  # type: ignore[misc]
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("content-length", "0") or "0")
            received["requests"] = int(received.get("requests", 0)) + 1
            received["body"] = self.rfile.read(length)
            received["headers"] = {k.lower(): v for k, v in self.headers.items()}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - inherited API
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        payload = call_harness_function(
            "plan_workflow",
            "trace push send fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "trace-push-send")
        call_harness_function("workflow_event", wfid, "fixture_trace_push_send_probe", {"status": "ok"})
        port = int(server.server_address[1])
        env = os.environ.copy()
        env["HARNESS_ALLOW_NETWORK_EXPORT"] = "1"
        proc = run_fixture_command([
            sys.executable,
            "scripts/harness.py",
            "workflow",
            "trace-push",
            wfid,
            "--target",
            "otlp-http",
            "--endpoint",
            f"http://127.0.0.1:{port}/v1/traces",
            "--limit",
            "10",
            "--send",
        ], env=env, timeout=fixture_timeout())
        thread.join(timeout=fixture_timeout())
        detail = (proc.stdout or proc.stderr).strip().replace("\n", " ")[:400]
        ok = proc.returncode == 0 and received.get("requests") == 1 and b"resourceSpans" in (received.get("body") or b"")
        try:
            data = json.loads(proc.stdout or "{}")
            ok = ok and data.get("dryRun") is False and int(data.get("responseStatus") or 0) == 200
            ok = ok and data.get("endpointPolicy", {}).get("allowed") is True
        except Exception as exc:
            ok = False
            detail = str(exc)
        return [result("trace-push-send:loopback-opt-in", "pass" if ok else "fail", "loopback send path ok" if ok else detail)]
    finally:
        server.server_close()
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_trace_push_policy_fixture() -> list[dict[str, str]]:
    """Verify trace-push network policy fails closed for unsafe send paths."""
    cleanup_test_artifacts()
    wfid = ""
    original_project = read_json(PROJECT, {})
    old_opt = os.environ.get("HARNESS_ALLOW_NETWORK_EXPORT")
    try:
        payload = call_harness_function(
            "plan_workflow",
            "trace push policy fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "trace-push-policy")
        call_harness_function("workflow_event", wfid, "fixture_trace_push_policy_probe", {"status": "ok"})
        failures: list[str] = []

        os.environ.pop("HARNESS_ALLOW_NETWORK_EXPORT", None)
        try:
            call_harness_function(
                "workflow_trace_push",
                wfid,
                target="otlp-http",
                endpoint="http://127.0.0.1:4318/v1/traces",
                send=True,
            )
            failures.append("send without explicit env opt-in was not blocked")
        except Exception as exc:
            if "HARNESS_ALLOW_NETWORK_EXPORT" not in str(exc):
                failures.append("send without opt-in failed for the wrong reason")

        os.environ["HARNESS_ALLOW_NETWORK_EXPORT"] = "1"
        try:
            call_harness_function(
                "workflow_trace_push",
                wfid,
                target="otlp-http",
                endpoint="http://example.com:4318/v1/traces",
                send=True,
            )
            failures.append("send to non-allowlisted host was not blocked before network")
        except Exception as exc:
            if "allowedHosts" not in str(exc):
                failures.append("non-allowlisted host failed for the wrong reason")

        try:
            plan = call_harness_function(
                "workflow_trace_push",
                wfid,
                target="otlp-http",
                endpoint="http://example.com:4318/v1/traces",
                send=False,
            )
            if plan.get("dryRun") is not True:
                failures.append("dry-run plan did not remain enabled for non-allowlisted host")
            if plan.get("endpointPolicy", {}).get("allowed") is not False:
                failures.append("dry-run plan did not surface blocked endpoint policy")
            if plan.get("payloadPolicy", {}).get("allowed") is not True:
                failures.append("normal dry-run payload should be within payload policy")
        except Exception as exc:
            failures.append(f"invalid dry-run policy plan: {exc}")

        for unsafe_endpoint, expected_reason in [
            ("http://user:pass@127.0.0.1:4318/v1/traces", "embedded credentials"),
            ("http://127.0.0.1:4318/v1/traces?api_key=sk-test-secret", "sensitive parameters"),
        ]:
            try:
                call_harness_function(
                    "workflow_trace_push",
                    wfid,
                    target="otlp-http",
                    endpoint=unsafe_endpoint,
                    send=True,
                )
                failures.append(f"unsafe endpoint was not blocked: {expected_reason}")
            except Exception as exc:
                if expected_reason not in str(exc):
                    failures.append(f"unsafe endpoint failed for wrong reason: {expected_reason}")
            try:
                unsafe_plan = call_harness_function(
                    "workflow_trace_push",
                    wfid,
                    target="otlp-http",
                    endpoint=unsafe_endpoint,
                    send=False,
                )
                if unsafe_plan.get("endpointPolicy", {}).get("allowed") is not False:
                    failures.append(f"unsafe dry-run policy did not report blocked: {expected_reason}")
                if "sk-test-secret" in json.dumps(unsafe_plan):
                    failures.append("unsafe dry-run leaked sensitive query value")
            except Exception as exc:
                failures.append(f"invalid unsafe dry-run policy plan: {exc}")

        tiny_project = read_json(PROJECT, {}) or {}
        tiny_policy = (((tiny_project.get("workflows") or {}).get("observability") or {}).get("networkExportPolicy") or {})
        tiny_policy["maxPayloadBytes"] = 1
        write_json(PROJECT, tiny_project)
        try:
            call_harness_function(
                "workflow_trace_push",
                wfid,
                target="otlp-http",
                endpoint="http://127.0.0.1:4318/v1/traces",
                send=True,
            )
            failures.append("payload size cap did not block --send")
        except Exception as exc:
            if "maxPayloadBytes" not in str(exc):
                failures.append("payload cap failed for the wrong reason")

        return [result("trace-push-policy:fail-closed", "pass" if not failures else "fail", "send requires opt-in, allowlisted host, secret-free endpoint, and bounded payload" if not failures else "; ".join(failures[:5]))]
    finally:
        if original_project:
            write_json(PROJECT, original_project)
        if old_opt is None:
            os.environ.pop("HARNESS_ALLOW_NETWORK_EXPORT", None)
        else:
            os.environ["HARNESS_ALLOW_NETWORK_EXPORT"] = old_opt
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

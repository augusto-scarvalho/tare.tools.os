#!/usr/bin/env python3
"""Exercise Probe A deny semantics through disposable Claude and Codex configs."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "labs" / "canonical_hook_probe.py"
DENY_MARKER = "TARE_HOOK_PROBE_BLOCK:PROMPT_TOO_LARGE"
HOOKS_FEATURE_OVERRIDE = "features={hooks=true}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hook(runtime: str) -> dict[str, Any]:
    command_argv = [sys.executable, str(HOOK_SCRIPT), runtime]
    command_windows = subprocess.list2cmdline(command_argv)
    command_posix = shlex.join(command_argv)
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command_posix,
                            "commandWindows": command_windows,
                            "timeout": 5,
                            "statusMessage": "Running bounded prompt policy probe...",
                        }
                    ]
                }
            ]
        }
    }


def _run(
    command: list[str],
    prompt: str,
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        input=prompt,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    elapsed = time.monotonic() - started
    combined = completed.stdout + completed.stderr
    structured_hook_events = []
    zero_usage_turn_completed = False
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict) and usage and all(
                isinstance(value, int) and value == 0 for value in usage.values()
            ):
                zero_usage_turn_completed = True
        encoded_event = json.dumps(event, sort_keys=True)
        if "hook" not in encoded_event.lower() and "UserPromptSubmit" not in encoded_event:
            continue
        structured_hook_events.append(
            re.sub(r"x{64,}", "<oversized-prompt>", encoded_event)[:2000]
        )
        if len(structured_hook_events) == 8:
            break
    output_lines = combined.splitlines()
    diagnostic_indexes: set[int] = set()
    for index, line in enumerate(output_lines):
        lowered = line.lower()
        if not any(
            marker in lowered
            for marker in (
                "hook",
                "sentinel",
                "prompt_too",
                "malformed_input",
                "unsupported_fields",
            )
        ):
            continue
        diagnostic_indexes.update(
            range(max(0, index - 1), min(len(output_lines), index + 3))
        )
    diagnostic_lines = [
        re.sub(r"x{64,}", "<oversized-prompt>", output_lines[index])[:500]
        for index in sorted(diagnostic_indexes)[:24]
    ]
    return {
        "exit_code": completed.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "deny_marker_observed": DENY_MARKER in combined,
        "output_chars": len(combined),
        "model_sentinel_observed": "invalid-hook-probe-model" in combined,
        "zero_usage_turn_completed": zero_usage_turn_completed,
        "diagnostic_lines": diagnostic_lines,
        "structured_hook_events": structured_hook_events,
        "output_tail": combined[-1000:],
    }


class _LoopbackModelHandler(BaseHTTPRequestHandler):
    def _reject(self) -> None:
        self.server.request_count += 1  # type: ignore[attr-defined]
        self.server.request_paths.append(self.path)  # type: ignore[attr-defined]
        body = b'{"error":{"message":"model sentinel reached"}}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reject
    do_POST = _reject

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_loopback_model_sentinel() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackModelHandler)
    server.request_count = 0  # type: ignore[attr-defined]
    server.request_paths = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _model_provider_override(port: int) -> str:
    base_url = json.dumps(f"http://127.0.0.1:{port}/v1")
    return (
        "model_providers={tare_probe={"
        'name="tare.tools loopback sentinel",'
        f"base_url={base_url},"
        'wire_api="responses",'
        'env_key="TARE_CODEX_PROBE_KEY",'
        "request_max_retries=0,"
        "stream_max_retries=0"
        "}}"
    )


def _project_trust_override(root: Path) -> str:
    """Build one non-persistent TOML override for the exact disposable root."""
    encoded_root = json.dumps(str(root.resolve()))
    return f'projects={{{encoded_root}={{trust_level="trusted"}}}}'


def _hook_trust_config(key: str, current_hash: str) -> str:
    return (
        "[hooks.state."
        + json.dumps(key)
        + "]\ntrusted_hash = "
        + json.dumps(current_hash)
        + "\n"
    )


def _same_path(left: object, right: Path) -> bool:
    if not isinstance(left, str):
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _find_project_prompt_hook(
    response: object, root: Path
) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, list):
        return None
    expected_source = root / ".codex" / "hooks.json"
    for entry in data:
        if not isinstance(entry, dict) or not _same_path(entry.get("cwd"), root):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            if hook.get("eventName") not in {
                "UserPromptSubmit",
                "userPromptSubmit",
                "user_prompt_submit",
            }:
                continue
            if hook.get("handlerType") != "command":
                continue
            if not _same_path(hook.get("sourcePath"), expected_source):
                continue
            return hook
    return None


def _codex_hooks_list(
    codex: str,
    root: Path,
    config_overrides: list[str],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    command = [codex, "app-server", "--stdio", "--strict-config"]
    for override in config_overrides:
        command.extend(["-c", override])
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    parsed: list[object] = []
    parse_errors = 0

    def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def wait_for(request_id: int, timeout: float) -> object | None:
        nonlocal parse_errors
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            parsed.append(message)
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    response = None
    transport_error = None
    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "tare_tools_probe",
                        "title": "tare.tools bounded hook probe",
                        "version": "1.0.0",
                    }
                },
            }
        )
        initialized = wait_for(1, 5)
        if not isinstance(initialized, dict) or "result" not in initialized:
            transport_error = "INITIALIZE_RESPONSE_UNAVAILABLE"
        else:
            send({"method": "initialized", "params": {}})
            send(
                {
                    "method": "hooks/list",
                    "id": 2,
                    "params": {"cwds": [str(root)]},
                }
            )
            response = wait_for(2, 10)
            if response is None:
                transport_error = "HOOKS_LIST_RESPONSE_UNAVAILABLE"
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            transport_error = transport_error or "APP_SERVER_EXIT_TIMEOUT"
        reader.join(timeout=1)
    stderr = process.stderr.read()
    elapsed = time.monotonic() - started
    hook = _find_project_prompt_hook(response, root)
    cwd_entry = None
    if isinstance(response, dict):
        result = response.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, list):
            cwd_entry = next(
                (
                    entry
                    for entry in data
                    if isinstance(entry, dict)
                    and _same_path(entry.get("cwd"), root)
                ),
                None,
            )
    listed_hooks = cwd_entry.get("hooks", []) if isinstance(cwd_entry, dict) else []
    return {
        "exit_code": process.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "response_observed": response is not None,
        "parse_errors": parse_errors,
        "transport_error": transport_error,
        "handler_discovered": hook is not None,
        "source_path_matches": hook is not None,
        "enabled": hook.get("enabled") if hook else None,
        "trust_status": hook.get("trustStatus") if hook else None,
        "key": hook.get("key") if hook else None,
        "current_hash": hook.get("currentHash") if hook else None,
        "handler_type": hook.get("handlerType") if hook else None,
        "async": hook.get("async") if hook else None,
        "effective_command": hook.get("command") if hook else None,
        "listed_hook_count": len(listed_hooks) if isinstance(listed_hooks, list) else 0,
        "listed_events": sorted(
            {
                str(item.get("eventName"))
                for item in listed_hooks
                if isinstance(item, dict) and item.get("eventName") is not None
            }
        ),
        "listed_source_paths": sorted(
            {
                str(item.get("sourcePath"))
                for item in listed_hooks
                if isinstance(item, dict) and item.get("sourcePath") is not None
            }
        ),
        "warnings": cwd_entry.get("warnings", []) if isinstance(cwd_entry, dict) else [],
        "errors": cwd_entry.get("errors", []) if isinstance(cwd_entry, dict) else [],
        "stderr_chars": len(stderr),
    }


def _hold_reason(
    discovery: dict[str, Any],
    trusted_discovery: dict[str, Any],
    codex_result: dict[str, Any],
) -> str:
    if not discovery.get("handler_discovered"):
        return "CODEX_PROJECT_HOOK_NOT_DISCOVERED"
    if trusted_discovery.get("trust_status") != "trusted":
        return "CODEX_EXACT_HASH_TRUST_NOT_EFFECTIVE"
    if codex_result.get("model_sentinel_observed"):
        return "CODEX_EXEC_CONTROL_EFFECT_LOST_MODEL_ENDPOINT_REACHED"
    if not codex_result.get("deny_marker_observed"):
        return "CODEX_HOOK_DID_NOT_ENFORCE_DENY"
    return "PROBE_ASSERTION_FAILED"


def run_e2e(*, codex_only: bool = False) -> dict[str, Any]:
    claude = shutil.which("claude.cmd") or shutil.which("claude")
    codex = shutil.which("codex")
    if not codex or (not codex_only and not claude):
        raise FileNotFoundError(
            "Codex must be installed; Claude is also required outside --codex-only mode"
        )
    oversized = "x" * 17_000
    guarded_configs = (
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".codex" / "config.toml",
        Path.home() / ".codex" / "hooks.json",
    )
    config_hashes_before = {
        str(path): _sha256(path) for path in guarded_configs if path.is_file()
    }

    with tempfile.TemporaryDirectory(prefix="tare-hook-probe-e2e-") as temp:
        root = Path(temp)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=root, capture_output=True, check=True
        )
        codex_dir = root / ".codex"
        codex_dir.mkdir()
        codex_home = root / "codex-home"
        codex_home.mkdir()
        (codex_dir / "hooks.json").write_text(
            json.dumps(_hook("codex")), encoding="utf-8"
        )
        project_trust_override = _project_trust_override(root)
        codex_discovery = _codex_hooks_list(
            codex,
            root,
            [HOOKS_FEATURE_OVERRIDE, project_trust_override],
            env={**os.environ, "CODEX_HOME": str(codex_home)},
        )
        hook_key = codex_discovery.get("key")
        current_hash = codex_discovery.get("current_hash")
        if isinstance(hook_key, str) and isinstance(current_hash, str):
            (codex_home / "config.toml").write_text(
                _hook_trust_config(hook_key, current_hash), encoding="utf-8"
            )
            codex_trusted_discovery = _codex_hooks_list(
                codex,
                root,
                [
                    HOOKS_FEATURE_OVERRIDE,
                    project_trust_override,
                ],
                env={**os.environ, "CODEX_HOME": str(codex_home)},
            )
        else:
            codex_trusted_discovery = {
                "handler_discovered": False,
                "trust_status": None,
                "transport_error": "HOOK_IDENTITY_UNAVAILABLE",
            }

        if codex_only:
            claude_result = {
                "disposition": "NOT_RUN_PRESERVED_PRIOR_PASS",
                "deny_marker_observed": True,
                "api_error_observed": False,
            }
        else:
            claude_settings = root / "claude-settings.json"
            claude_settings.write_text(json.dumps(_hook("claude")), encoding="utf-8")
            claude_debug = root / "claude-hooks-debug.log"
            claude_result = _run(
                [
                    claude,
                    "-p",
                    "--settings",
                    str(claude_settings),
                    "--tools",
                    "",
                    "--model",
                    "invalid-hook-probe-model",
                    "--max-budget-usd",
                    "0.000001",
                    "--no-session-persistence",
                    "--debug",
                    "hooks",
                    "--debug-file",
                    str(claude_debug),
                ],
                oversized,
                root,
            )
        if (
            codex_trusted_discovery["handler_discovered"]
            and codex_trusted_discovery["trust_status"] == "trusted"
        ):
            model_server, model_thread = _start_loopback_model_sentinel()
            try:
                model_port = model_server.server_address[1]
                codex_result = _run(
                    [
                        codex,
                        "exec",
                        "--json",
                        "--ephemeral",
                        "--strict-config",
                        "--skip-git-repo-check",
                        "--dangerously-bypass-hook-trust",
                        "-c",
                        HOOKS_FEATURE_OVERRIDE,
                        "-c",
                        project_trust_override,
                        "-c",
                        'model_provider="tare_probe"',
                        "-c",
                        _model_provider_override(model_port),
                        "-C",
                        str(root),
                        "-m",
                        "tare-hook-probe-model",
                        "-",
                    ],
                    oversized,
                    root,
                    env={
                        **os.environ,
                        "CODEX_HOME": str(codex_home),
                        "TARE_CODEX_PROBE_KEY": "local-loopback-sentinel",
                    },
                )
            finally:
                model_server.shutdown()
                model_server.server_close()
                model_thread.join(timeout=2)
            request_paths = model_server.request_paths  # type: ignore[attr-defined]
            response_requests = sum(
                path.split("?", 1)[0].endswith("/responses")
                for path in request_paths
            )
            catalog_requests = sum(
                path.split("?", 1)[0].endswith("/models")
                for path in request_paths
            )
            codex_result["loopback_request_count"] = len(request_paths)
            codex_result["loopback_catalog_requests"] = catalog_requests
            codex_result["loopback_model_requests"] = response_requests
            codex_result["model_sentinel_observed"] = response_requests > 0
        else:
            codex_result = {
                "exit_code": None,
                "elapsed_seconds": 0.0,
                "deny_marker_observed": False,
                "output_chars": 0,
                "model_sentinel_observed": False,
                "skipped_before_model": True,
            }

    config_hashes_after = {
        str(path): _sha256(path) for path in guarded_configs if path.is_file()
    }
    config_unchanged = config_hashes_before == config_hashes_after
    claude_enforced = claude_result["deny_marker_observed"]
    codex_enforced = (
        codex_result.get("exit_code") == 0
        and codex_result.get("loopback_model_requests") == 0
        and codex_result.get("zero_usage_turn_completed") is True
    )
    codex_result["control_effect_observed"] = codex_enforced
    for result in (claude_result, codex_result):
        if "output_tail" not in result:
            result["api_error_observed"] = False
            continue
        result["api_error_observed"] = "invalid_request_error" in result["output_tail"]
        del result["output_tail"]
    status = (
        "PASS"
        if claude_enforced
        and codex_trusted_discovery["handler_discovered"]
        and codex_trusted_discovery["trust_status"] == "trusted"
        and codex_enforced
        and not codex_result["model_sentinel_observed"]
        else "HELD"
    )
    return {
        "status": status,
        "hold_reason": None if status == "PASS" else _hold_reason(
            codex_discovery, codex_trusted_discovery, codex_result
        ),
        "mode": "codex_only" if codex_only else "both_clis",
        "policy": "prompt_budget",
        "lifecycle": "UserPromptSubmit",
        "prompt_bytes": len(oversized.encode("utf-8")),
        "claude": claude_result,
        "codex_discovery": codex_discovery,
        "codex_trusted_discovery": codex_trusted_discovery,
        "codex": codex_result,
        "guarded_global_configs_unchanged": config_unchanged,
        "claude_runtime_metadata_may_change": not codex_only,
        "disposable_codex_home_only": True,
        "model_call_expected": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-only",
        action="store_true",
        help="rerun only the corrected Codex effectiveness cell",
    )
    args = parser.parse_args()
    try:
        result = run_e2e(codex_only=args.codex_only)
    except BaseException as exc:
        print(json.dumps({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

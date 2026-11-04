from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.labs import canonical_hook_probe as probe
from scripts.labs import run_canonical_hook_probe_e2e as e2e


def payload(
    prompt: object = "ship the smallest useful change",
    *,
    event_name: str = "UserPromptSubmit",
    **extra,
):
    return {
        "session_id": "shadow-session",
        "transcript_path": "C:/ignored/transcript.jsonl",
        "cwd": "C:/ignored/workspace",
        "permission_mode": "default",
        "hook_event_name": event_name,
        "prompt": prompt,
        **extra,
    }


def encoded(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def runtime_payload(
    runtime: str, prompt: object = "ship the smallest useful change", **extra
):
    return payload(prompt, event_name="UserPromptSubmit", **extra)


@pytest.mark.parametrize("runtime", probe.SUPPORTED_RUNTIMES)
def test_allow_parity_and_bounded_ephemeral_evidence(runtime):
    result = probe.dispatch(runtime, encoded(runtime_payload(runtime)))
    assert result.exit_code == 0
    assert result.stderr == ""
    output = json.loads(result.stdout)
    evidence = output["tare_probe"]
    assert evidence == {
        "schema_version": "1.0.0",
        "policy_id": "prompt_budget",
        "phase": "user_prompt_submit",
        "decision": "allow",
        "reason_code": "WITHIN_PROMPT_BUDGET",
        "prompt_bytes": 31,
        "evidence_scope": "ephemeral_shadow_stdout",
    }


@pytest.mark.parametrize("runtime", probe.SUPPORTED_RUNTIMES)
@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        ("   ", "EMPTY_PROMPT"),
        ("x" * (probe.MAX_PROMPT_BYTES + 1), "PROMPT_TOO_LARGE"),
    ],
)
def test_deny_parity_uses_each_vendor_block_contract(runtime, prompt, reason):
    result = probe.dispatch(runtime, encoded(runtime_payload(runtime, prompt)))
    marker = f"TARE_HOOK_PROBE_BLOCK:{reason}"
    if runtime == "claude":
        assert result == probe.AdapterResult(2, stderr=f"{marker}\n")
    else:
        assert result == probe.AdapterResult(
            0,
            stdout=json.dumps(
                {"decision": "block", "reason": marker}, separators=(",", ":")
            ),
        )


@pytest.mark.parametrize("runtime", (*probe.SUPPORTED_RUNTIMES, "unknown"))
@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff",
        encoded([]),
        encoded(payload(None)),
        encoded(payload(event_name="Stop")),
        encoded(payload(root="C:/caller-controlled")),
        encoded(payload(policy_id="other")),
        encoded(payload(module="dangerous.module")),
        encoded(payload(callable="run")),
    ],
)
def test_unknown_or_malformed_input_fails_closed(runtime, raw):
    result = probe.dispatch(runtime, raw)
    if runtime == "codex":
        assert result.exit_code == 0
        assert result.stderr == ""
        assert json.loads(result.stdout)["decision"] == "block"
        assert json.loads(result.stdout)["reason"].startswith("TARE_HOOK_PROBE_BLOCK:")
    else:
        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr.startswith("TARE_HOOK_PROBE_BLOCK:")


@pytest.mark.parametrize("runtime", probe.SUPPORTED_RUNTIMES)
def test_input_boundary_fails_closed(runtime):
    result = probe.dispatch(runtime, b"x" * (probe.MAX_INPUT_BYTES + 1))
    if runtime == "claude":
        assert result == probe.AdapterResult(
            2, stderr="TARE_HOOK_PROBE_BLOCK:INPUT_TOO_LARGE\n"
        )
    else:
        assert json.loads(result.stdout) == {
            "decision": "block",
            "reason": "TARE_HOOK_PROBE_BLOCK:INPUT_TOO_LARGE",
        }


def test_policy_has_no_ambient_authority(monkeypatch, tmp_path):
    sentinel = tmp_path / "must-not-be-read-or-written"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARE_ROOT", str(sentinel))
    monkeypatch.setenv("POLICY_ID", "attacker-policy")
    before = sentinel.read_bytes()
    claude = probe.dispatch(
        "claude",
        encoded(
            runtime_payload(
                "claude",
                "safe prompt",
                cwd="../../escape",
                transcript_path=str(sentinel),
            )
        ),
    )
    codex = probe.dispatch(
        "codex",
        encoded(
            runtime_payload(
                "codex",
                "safe prompt",
                cwd="../../escape",
                transcript_path=str(sentinel),
            )
        ),
    )
    assert claude.exit_code == codex.exit_code == 0
    assert (
        json.loads(claude.stdout)["tare_probe"]
        == json.loads(codex.stdout)["tare_probe"]
    )
    assert sentinel.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == [sentinel]


def test_concurrent_dual_adapter_dispatch_is_stateless_and_atomic():
    runtimes = [probe.SUPPORTED_RUNTIMES[index % 2] for index in range(128)]
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(
            executor.map(
                lambda runtime: probe.dispatch(
                    runtime, encoded(runtime_payload(runtime))
                ),
                runtimes,
            )
        )
    assert all(result.exit_code == 0 for result in results)
    assert all(
        len(result.stdout.encode("utf-8")) <= probe.MAX_OUTPUT_BYTES
        for result in results
    )
    evidence = [json.loads(result.stdout)["tare_probe"] for result in results]
    assert len({json.dumps(item, sort_keys=True) for item in evidence}) == 1


class BrokenOutput(io.StringIO):
    def write(self, value):
        raise BrokenPipeError


def test_shadow_output_failure_converts_allow_to_block():
    stderr = io.StringIO()
    code = probe.run_command(
        "claude",
        stdin=io.BytesIO(encoded(payload())),
        stdout=BrokenOutput(),
        stderr=stderr,
    )
    assert code == 2
    assert stderr.getvalue() == "TARE_HOOK_PROBE_BLOCK:OUTPUT_UNAVAILABLE\n"


@pytest.mark.parametrize("runtime", probe.SUPPORTED_RUNTIMES)
def test_adapter_contract_is_explicit_bounded_and_semantic_only(runtime):
    contract = probe.ADAPTER_CONTRACTS[runtime]
    assert contract.runtime == runtime
    assert contract.vendor_event == "UserPromptSubmit"
    assert contract.canonical_phase == "user_prompt_submit"
    assert contract.supported_decisions == (
        probe.Decision.ALLOW,
        probe.Decision.DENY,
        probe.Decision.ERROR,
    )
    assert contract.supported_control_effects == ("block",)
    assert contract.verification_scope == "semantic_translation_only"
    assert "rewrite_input" not in contract.supported_control_effects
    assert "replace_tool" not in contract.supported_control_effects
    assert "retry" not in contract.supported_control_effects
    assert "resume" not in contract.supported_control_effects


@pytest.mark.parametrize("runtime", probe.SUPPORTED_RUNTIMES)
@pytest.mark.parametrize(
    ("raw", "expected_code", "expected_marker"),
    [
        (encoded(payload()), 0, "WITHIN_PROMPT_BUDGET"),
        (encoded(payload("   ")), 2, "EMPTY_PROMPT"),
        (
            encoded(payload("x" * (probe.MAX_PROMPT_BYTES + 1))),
            2,
            "PROMPT_TOO_LARGE",
        ),
        (b"not-json", 2, "MALFORMED_INPUT"),
        (encoded(payload(event_name="Stop")), 2, "MALFORMED_INPUT"),
    ],
    ids=("allow", "empty-deny", "oversized-deny", "malformed", "wrong-event"),
)
def test_leaf_process_event_outcome_matrix(
    runtime, raw, expected_code, expected_marker, tmp_path
):
    script = Path(probe.__file__).resolve()
    before = tuple(tmp_path.iterdir())
    completed = subprocess.run(
        [sys.executable, str(script), runtime],
        input=raw,
        cwd=tmp_path,
        env={**os.environ, "TARE_ROOT": str(tmp_path / "escape")},
        capture_output=True,
        timeout=2,
        check=False,
    )
    effective_code = 0 if runtime == "codex" else expected_code
    assert completed.returncode == effective_code
    if expected_code == 0:
        assert completed.stderr == b""
        output = json.loads(completed.stdout)
        assert output["tare_probe"]["reason_code"] == expected_marker
        assert output["tare_probe"]["decision"] == "allow"
        assert ("systemMessage" in output) is (runtime == "codex")
    elif runtime == "claude":
        assert completed.stdout == b""
        assert completed.stderr.decode("utf-8").splitlines() == [
            f"TARE_HOOK_PROBE_BLOCK:{expected_marker}"
        ]
    else:
        assert completed.stderr == b""
        assert json.loads(completed.stdout) == {
            "decision": "block",
            "reason": f"TARE_HOOK_PROBE_BLOCK:{expected_marker}",
        }
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("runtime", probe.SUPPORTED_RUNTIMES)
def test_leaf_command_blocks_input_transport_overflow(runtime, tmp_path):
    script = Path(probe.__file__).resolve()
    completed = subprocess.run(
        [sys.executable, str(script), runtime],
        input=b"x" * (probe.MAX_INPUT_BYTES + 1),
        cwd=tmp_path,
        capture_output=True,
        timeout=2,
        check=False,
    )
    if runtime == "claude":
        assert completed.returncode == 2
        assert completed.stdout == b""
        assert completed.stderr.decode("utf-8").splitlines() == [
            "TARE_HOOK_PROBE_BLOCK:INPUT_TOO_LARGE"
        ]
    else:
        assert completed.returncode == 0
        assert completed.stderr == b""
        assert json.loads(completed.stdout) == {
            "decision": "block",
            "reason": "TARE_HOOK_PROBE_BLOCK:INPUT_TOO_LARGE",
        }
    assert tuple(tmp_path.iterdir()) == ()


def test_closed_dispatcher_has_no_dynamic_extension_surface():
    assert set(probe._ADAPTERS) == {"claude", "codex"}
    assert tuple(probe.ADAPTER_CONTRACTS) == probe.SUPPORTED_RUNTIMES
    assert not any(
        name in vars(probe)
        for name in ("register", "load_plugin", "import_module", "discover", "provider")
    )


def test_codex_project_trust_override_is_one_quoted_inline_table(tmp_path):
    root = tmp_path / "repo.with.dots"
    override = e2e._project_trust_override(root)
    assert override.startswith("projects={")
    assert json.dumps(str(root.resolve())) in override
    assert ".trust_level" not in override
    assert override.endswith('{trust_level="trusted"}}')


def test_codex_hook_trust_config_binds_exact_key_and_hash():
    key = r"C:\probe\.codex\hooks.json:user_prompt_submit:0:0"
    current_hash = "sha256:abc123"
    config = e2e._hook_trust_config(key, current_hash)
    assert config.startswith("[hooks.state.")
    assert json.dumps(key) in config
    assert f"trusted_hash = {json.dumps(current_hash)}" in config


def test_codex_hold_reason_prioritizes_model_endpoint_reach_after_trust():
    assert (
        e2e._hold_reason(
            {"handler_discovered": True},
            {"trust_status": "trusted"},
            {"model_sentinel_observed": True, "deny_marker_observed": False},
        )
        == "CODEX_EXEC_CONTROL_EFFECT_LOST_MODEL_ENDPOINT_REACHED"
    )


def test_codex_accepts_official_runtime_metadata_before_enforcing_policy():
    result = probe.dispatch(
        "codex",
        encoded(
            runtime_payload(
                "codex",
                "x" * (probe.MAX_PROMPT_BYTES + 1),
                turn_id="turn-1",
                agent_id="agent-1",
                agent_type="default",
                model="gpt-5.6",
            )
        ),
    )
    assert json.loads(result.stdout) == {
        "decision": "block",
        "reason": "TARE_HOOK_PROBE_BLOCK:PROMPT_TOO_LARGE",
    }


def test_codex_still_rejects_unknown_runtime_metadata():
    result = probe.dispatch(
        "codex", encoded(runtime_payload("codex", unknown_metadata="closed"))
    )
    assert json.loads(result.stdout) == {
        "decision": "block",
        "reason": "TARE_HOOK_PROBE_BLOCK:UNSUPPORTED_FIELDS:unknown_metadata",
    }


def test_codex_loopback_model_provider_override_is_bounded_to_localhost():
    override = e2e._model_provider_override(43123)
    assert 'base_url="http://127.0.0.1:43123/v1"' in override
    assert 'wire_api="responses"' in override
    assert 'env_key="TARE_CODEX_PROBE_KEY"' in override
    assert "request_max_retries=0" in override
    assert "stream_max_retries=0" in override


def test_codex_windows_hook_command_is_cmd_compatible():
    handler = e2e._hook("codex")["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert handler["commandWindows"] == subprocess.list2cmdline(
        [sys.executable, str(Path(probe.__file__).resolve()), "codex"]
    )
    assert "[Console]" not in handler["commandWindows"]
    assert " | & " not in handler["commandWindows"]


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line regression")
def test_codex_windows_hook_command_executes_vendor_block_contract():
    handler = e2e._hook("codex")["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", handler["commandWindows"]],
        input=encoded(runtime_payload("codex", "x" * (probe.MAX_PROMPT_BYTES + 1))),
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert json.loads(completed.stdout) == {
        "decision": "block",
        "reason": "TARE_HOOK_PROBE_BLOCK:PROMPT_TOO_LARGE",
    }


def test_codex_discovery_preflight_selects_only_exact_project_hook(tmp_path):
    root = tmp_path / "repo"
    source = root / ".codex" / "hooks.json"
    expected = {
        "eventName": "userPromptSubmit",
        "handlerType": "command",
        "sourcePath": str(source),
        "enabled": True,
        "trustStatus": "untrusted",
    }
    response = {
        "id": 2,
        "result": {
            "data": [
                {
                    "cwd": str(root),
                    "hooks": [
                        {
                            "eventName": "userPromptSubmit",
                            "handlerType": "command",
                            "sourcePath": str(tmp_path / "plugin" / "hooks.json"),
                        },
                        expected,
                    ],
                }
            ]
        },
    }
    assert e2e._find_project_prompt_hook(response, root) == expected


def test_codex_discovery_preflight_fails_closed_without_exact_source(tmp_path):
    root = tmp_path / "repo"
    response = {
        "id": 2,
        "result": {
            "data": [
                {
                    "cwd": str(root),
                    "hooks": [
                        {
                            "eventName": "userPromptSubmit",
                            "handlerType": "command",
                            "sourcePath": str(tmp_path / "other" / "hooks.json"),
                        }
                    ],
                }
            ]
        },
    }
    assert e2e._find_project_prompt_hook(response, root) is None

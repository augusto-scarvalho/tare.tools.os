import json
from pathlib import Path

import pytest

from relay import round_table_engine


@pytest.fixture
def isolated_round_tables(monkeypatch, tmp_path):
    root = tmp_path / "round_tables"
    root.mkdir()
    monkeypatch.setattr(round_table_engine, "ROUND_TABLES_DIR", root)
    return root


def _vote(seat, nonce, verdict="APPROVE"):
    return {
        "seat": seat,
        "execution_nonce": nonce,
        "verdict": verdict,
        "confidence": 0.95,
        "summary": "Independent audit completed.",
        "strengths": ["Deterministic"],
        "issues": [],
        "recommendations": [],
    }


def _stream_result(raw, returncode=0, timed_out=False):
    data = raw.encode("utf-8")
    return returncode, raw, {
        "attempt_id": "test-attempt",
        "status": "INTERRUPTED" if timed_out else "COMPLETED",
        "stream_file": "test.stream.log",
        "cursor": {
            "byte_offset": len(data),
            "chunk_sequence": 1 if data else 0,
            "partial_sha256": round_table_engine._sha256(raw),
        },
        "first_output_ms": 1.0 if data else None,
        "last_output_at": "2026-08-21T00:00:00+00:00" if data else None,
        "timed_out": timed_out,
    }


def test_pinned_commands_have_exact_models_and_no_fallback_flags(tmp_path):
    for deployment in round_table_engine.ROSTER['chairs']['fixed']:
        command = round_table_engine._build_deployment_command(deployment, 'prompt')
        assert round_table_engine.round_table_roster.runtime_model(round_table_engine.ROSTER, deployment) in command
        assert not any('fallback-model' in v or 'dangerously' in v for v in command)
        if deployment['runner'] == 'codex':  # the user's ~/.codex/config.toml stays out; default tier
            assert '--ignore-user-config' in command and 'service_tier="default"' in command
        if deployment['runner'] == 'claude':  # the user's settings and Claude Code's prompt stay out
            assert command[command.index('--setting-sources') + 1] == '' and '--strict-mcp-config' in command
            assert command[command.index('--system-prompt') + 1] == round_table_engine.SEAT_SYSTEM
    retired = {'id':'old','runner':'aider','model':'qwen-3.8-27b'}
    ok, _, receipt = round_table_engine._run_deployment(retired, 'prompt')
    assert not ok and receipt['failure_class']=='RETIRED_TRANSPORT'
    assert receipt['generation_calls']==0































def test_case_runtime_contract_rejects_roster_drift(isolated_round_tables):
    round_table_engine.init_round_table("RT-CONTRACT", "Contract", "# Proposal")
    case = round_table_engine.RoundTableCase("RT-CONTRACT")
    state = case.load_state()
    state["runtime_contract"]["roster_sha256"] = "stale"

    with pytest.raises(ValueError, match="runtime contract drifted"):
        round_table_engine._validate_runtime_contract(state)


def test_author_bias_domain_is_excluded_before_dispatch(isolated_round_tables, monkeypatch):
    state = round_table_engine.init_round_table(
        "RT-AUTHOR-EXCLUSION",
        "Author exclusion",
        "# Proposal",
        excluded_bias_domains=["openai/gpt"],
    )

    assert state["chair_policy"] == {
        "schema_version": 1,
        "excluded_bias_domains": ["openai/gpt"],
        "initial_seat_deployments": {
            "anthropic": "chair-anthropic-cli",
            "openai": "chair-kimi-cli",
            "google": "chair-google-cli",
        },
    }

    fixed_calls = []
    replacement_calls = []

    def fixed(seat_id, prompt, expected_nonce, timeout=300):
        fixed_calls.append(seat_id)
        return True, _vote(seat_id, expected_nonce), "ok"

    def replacement(seat_id, prompt, expected_nonce, deployment, timeout=300, **kwargs):
        replacement_calls.append(deployment["id"])
        return True, _vote(seat_id, expected_nonce), "ok", {
            "deployment_id": deployment["id"],
            "bias_domain": round_table_engine.round_table_roster.bias_domain(round_table_engine.ROSTER, deployment),
        }

    monkeypatch.setattr(round_table_engine, "_execute_seat", fixed)
    monkeypatch.setattr(round_table_engine, "_execute_seat_deployment", replacement)
    result = round_table_engine.conduct_round("RT-AUTHOR-EXCLUSION", parallel=False)

    assert result["status"] == "APPROVED"
    assert fixed_calls == ["anthropic", "google"]
    assert replacement_calls == ["chair-kimi-cli"]
    domains = {
        round_table_engine.round_table_roster.bias_domain(round_table_engine.ROSTER, deployment)
        for deployment in result["seat_deployments"].values()
    }
    assert domains == {"anthropic/fable", "kimi/kimi-k3", "google/gemini"}
    assert "openai/gpt" not in domains


def test_author_exclusion_rejects_unknown_bias_domain_without_creating_case(isolated_round_tables):
    with pytest.raises(ValueError, match="Unknown excluded bias_domain"):
        round_table_engine.init_round_table(
            "RT-AUTHOR-UNKNOWN",
            "Unknown author",
            "# Proposal",
            excluded_bias_domains=["unknown/model-family"],
        )

    assert not round_table_engine.RoundTableCase("RT-AUTHOR-UNKNOWN").state_file.exists()


def test_author_exclusion_is_bound_to_runtime_contract(isolated_round_tables):
    round_table_engine.init_round_table(
        "RT-AUTHOR-CONTRACT",
        "Author contract",
        "# Proposal",
        excluded_bias_domains=["openai/gpt"],
    )
    case = round_table_engine.RoundTableCase("RT-AUTHOR-CONTRACT")
    state = case.load_state()
    state["chair_policy"]["initial_seat_deployments"]["openai"] = "chair-openai-cli"

    with pytest.raises(ValueError, match="selected an excluded bias domain"):
        round_table_engine._validate_runtime_contract(state)


def test_failed_fixed_chair_is_replaced_by_one_independent_fallback(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-FALLBACK", "Fallback", "# Proposal")

    def fixed(seat_id, prompt, expected_nonce, timeout=300):
        if seat_id == "openai":
            return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), "timeout"
        return True, _vote(seat_id, expected_nonce), "ok"

    attempted = []

    def fallback(seat_id, prompt, expected_nonce, deployment, timeout=300):
        attempted.append(deployment["id"])
        return True, _vote(seat_id, expected_nonce), "ok", {"deployment_id": deployment["id"]}

    monkeypatch.setattr(round_table_engine, "_execute_seat", fixed)
    monkeypatch.setattr(round_table_engine, "_execute_seat_deployment", fallback)
    result = round_table_engine.conduct_round("RT-FALLBACK", parallel=False)

    assert result["status"] == "APPROVED"
    assert result["tally"]["total_seats"] == 3
    assert result["tally"]["deployments"]["openai"] == "chair-kimi-cli"
    assert attempted == ["chair-kimi-cli"]


def test_quota_and_endpoint_circuits_route_to_next_domain(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-DOMAINS", "Domains", "# Proposal")

    def fixed(seat_id, prompt, expected_nonce, timeout=300):
        if seat_id == "openai":
            return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), "unavailable"
        return True, _vote(seat_id, expected_nonce), "ok"

    attempted = []

    def fallback(seat_id, prompt, expected_nonce, deployment, timeout=300):
        attempted.append(deployment["id"])
        if deployment["id"] == "chair-kimi-cli":
            return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), "quota exhausted", {
                "deployment_id": deployment["id"], "failure_class": "QUOTA_EXHAUSTED"
            }
        if deployment["id"] == "chair-qwen-local":
            return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), "connection error", {
                "deployment_id": deployment["id"], "failure_class": "SERVER_UNAVAILABLE"
            }
        return True, _vote(seat_id, expected_nonce), "ok", {"deployment_id": deployment["id"]}

    monkeypatch.setattr(round_table_engine, "_execute_seat", fixed)
    monkeypatch.setattr(round_table_engine, "_execute_seat_deployment", fallback)
    result = round_table_engine.conduct_round("RT-DOMAINS", parallel=False)
    state = round_table_engine.RoundTableCase("RT-DOMAINS").load_state()

    assert result["status"] == "HELD_UNAVAILABLE"
    assert attempted == ["chair-kimi-cli", "chair-qwen-local"]
    assert {(block["domain_type"], block["domain"]) for block in state["availability_blocks"]} == {
        ("quota_domain", "kimi-cli-plan"),
        ("endpoint_domain", "local-llm-8080"),
    }


def test_resume_reuses_valid_seat_checkpoints(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-CHECKPOINT", "Checkpoint", "# Proposal")

    def first_run(seat_id, prompt, expected_nonce, timeout=300):
        if seat_id == "openai":
            return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), "timeout"
        return True, _vote(seat_id, expected_nonce), "ok"

    monkeypatch.setattr(round_table_engine, "_execute_seat", first_run)
    held = round_table_engine.conduct_round("RT-CHECKPOINT", parallel=False, allow_fallbacks=False)
    assert held["status"] == "HELD_UNAVAILABLE"

    calls = []

    def resumed(seat_id, prompt, expected_nonce, timeout=300):
        calls.append(seat_id)
        return True, _vote(seat_id, expected_nonce), "ok"

    monkeypatch.setattr(round_table_engine, "_execute_seat", resumed)
    result = round_table_engine.conduct_round("RT-CHECKPOINT", parallel=False, resume=True)

    assert result["status"] == "APPROVED"
    assert calls == ["openai"]


def test_mediator_and_scribe_selection_exclude_same_bias(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-MEDIATOR", "Mediator", "# Proposal")
    calls = []

    def text_role(deployment, prompt, timeout=300, **kwargs):
        calls.append(deployment["id"])
        return True, "# Objetivos\n\nResolved independently.\n\n# Não-objetivos\n\nNo scope expansion. " * 3, {"deployment_id": deployment["id"]}

    monkeypatch.setattr(round_table_engine, "_execute_text_role", text_role)
    deployments = {seat: info["deployment"] for seat, info in round_table_engine.SEATS.items()}
    result = {
        "blocking_issues": [{"id": "B1", "severity": "blocking", "claim": "gap", "falsifier": "case", "resolution": "fix"}],
        "seat_votes": {},
        "seat_deployments": deployments,
    }
    round_table_engine.mediate_and_refine("RT-MEDIATOR", result)
    state = round_table_engine.RoundTableCase("RT-MEDIATOR").load_state()

    assert calls == ["mediator-qwen-local"]
    assert state["last_mediator"] == "mediator-qwen-local"
    mediator = round_table_engine._deployment_by_id(state["last_mediator"])
    scribes = round_table_engine.round_table_roster.eligible_scribes(round_table_engine.ROSTER, mediator)
    assert scribes[0]["id"] == "scribe-anthropic-cli"


def test_mediator_never_seals_runner_error_as_revision(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-MEDIATOR-ERROR", "Mediator error", "# Proposal")
    monkeypatch.setattr(round_table_engine, "_execute_text_role", lambda deployment, prompt, **kwargs: (
        True, "litellm.Timeout: APITimeoutError - Request timed out. " * 4, {"deployment_id": deployment["id"]}
    ))
    result = {
        "blocking_issues": [{"id": "B1", "severity": "blocking", "claim": "gap", "falsifier": "case", "resolution": "fix"}],
        "seat_votes": {},
        "seat_deployments": {seat: info["deployment"] for seat, info in round_table_engine.SEATS.items()},
    }

    with pytest.raises(RuntimeError, match="No cross-bias mediator"):
        round_table_engine.mediate_and_refine("RT-MEDIATOR-ERROR", result)

    case = round_table_engine.RoundTableCase("RT-MEDIATOR-ERROR")
    assert not (case.proposals_dir / "v002.md").exists()


def test_mediator_exhaustion_holds_and_skips_shared_local_endpoint(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-MEDIATOR-DOWN", "Mediator down", "# Proposal")
    calls = []

    def unavailable(deployment, prompt, timeout=300, **kwargs):
        calls.append(deployment["id"])
        if deployment["id"] == "mediator-qwen-local":
            return False, "I cannot comply because the server is unavailable", {
                "deployment_id": deployment["id"], "failure_class": "SERVER_UNAVAILABLE"
            }
        return False, "credits exhausted", {
            "deployment_id": deployment["id"], "failure_class": "CREDITS_EXHAUSTED"
        }

    monkeypatch.setattr(round_table_engine, "_execute_text_role", unavailable)
    deployments = {seat: info["deployment"] for seat, info in round_table_engine.SEATS.items()}
    result = {
        "blocking_issues": [{"id": "B1", "severity": "blocking", "claim": "gap", "falsifier": "case", "resolution": "fix"}],
        "seat_votes": {},
        "seat_deployments": deployments,
    }

    with pytest.raises(RuntimeError, match="No cross-bias mediator"):
        round_table_engine.mediate_and_refine("RT-MEDIATOR-DOWN", result)
    state = round_table_engine.RoundTableCase("RT-MEDIATOR-DOWN").load_state()

    assert calls == ["mediator-qwen-local"]
    assert state["status"] == "HELD_UNAVAILABLE"
    assert {(block["domain_type"], block["domain"]) for block in state["availability_blocks"]} == {
        ("endpoint_domain", "local-llm-8080"),
    }


def test_model_timeout_does_not_block_hotswap_on_shared_endpoint():
    qwen = round_table_engine._deployment_by_id("mediator-qwen-local")
    fable_tc = round_table_engine._deployment_by_id("mediator-fable-tc-local")
    state = {"availability_blocks": []}

    block = round_table_engine._record_availability_failure(
        state, qwen, {"failure_class": "TIMEOUT"}
    )

    assert (block["domain_type"], block["domain"]) == ("deployment", "mediator-qwen-local")
    assert round_table_engine._deployment_blocked(state, qwen)
    assert round_table_engine._deployment_blocked(state, fable_tc) is None


def test_scribe_outage_uses_recorded_deterministic_summary(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-SCRIBE-DOWN", "Scribe down", "# Proposal")
    case = round_table_engine.RoundTableCase("RT-SCRIBE-DOWN")
    state = case.load_state()

    monkeypatch.setattr(round_table_engine, "_execute_text_role", lambda deployment, prompt, timeout=300, **kwargs: (
        False,
        "request timed out",
        {"deployment_id": deployment["id"], "failure_class": "TIMEOUT"},
    ))
    narrative = round_table_engine._scribe_narrative(case, state, "APPROVED", "Deterministic summary")
    artifact = round_table_engine.relay_io.read_json(case.decision_dir / "scribe.json")

    assert narrative == "Deterministic summary"
    assert artifact["fallback"] == "deterministic_operator_summary"
    assert artifact["authoritative"] is False


def test_red_team_fallback_requires_refusal_signal():
    assert not round_table_engine._is_refusal_or_evasion({}, "connection timeout")
    assert round_table_engine._is_refusal_or_evasion({}, "I cannot comply with this request")
    assert round_table_engine._is_refusal_or_evasion({}, "Não tenho contexto suficiente para avaliar")
    assert not round_table_engine._is_refusal_or_evasion({}, "Independent audit completed concisely")


def test_valid_vote_with_refusal_words_never_triggers_fallback(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-VALID-REFUSAL-WORDS", "Valid refusal words", "# Proposal")
    fallbacks = []

    def valid_vote(seat_id, prompt, expected_nonce, timeout=300):
        vote = _vote(seat_id, expected_nonce)
        vote["summary"] = "I cannot comply with approving weak evidence, but this proposal is sound."
        return True, vote, "valid JSON"

    monkeypatch.setattr(round_table_engine, "_execute_seat", valid_vote)
    monkeypatch.setattr(round_table_engine, "_execute_seat_deployment", lambda *args, **kwargs: fallbacks.append(args))

    result = round_table_engine.conduct_round("RT-VALID-REFUSAL-WORDS", parallel=False)

    assert result["status"] == "APPROVED"
    assert fallbacks == []


def test_substantively_empty_json_refusal_is_invalid():
    nonce = "refusal-nonce"
    vote = _vote("openai", nonce)
    vote.update({
        "summary": "I cannot comply with this audit.",
        "strengths": [],
        "issues": [],
        "recommendations": [],
    })

    ok, parsed, raw = round_table_engine._parse_seat_execution("openai", nonce, True, json.dumps(vote))

    assert not ok
    assert parsed["verdict"] == "INVALID_SCHEMA"
    assert round_table_engine._is_refusal_or_evasion(parsed, raw)


def test_operational_error_with_refusal_words_never_enables_red_team(isolated_round_tables, monkeypatch):
    round_table_engine.init_round_table("RT-ERROR-REFUSAL-WORDS", "Error refusal words", "# Proposal")

    def fixed(seat_id, prompt, expected_nonce, timeout=300):
        if seat_id != "openai":
            return True, _vote(seat_id, expected_nonce), "ok"
        raw = "litellm.APIError: unable to comply; cannot process; insufficient context"
        return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), raw, {
            "deployment_id": "chair-openai-cli",
            "failure_class": "SERVER_UNAVAILABLE",
        }

    attempted = []

    def fallback(seat_id, prompt, expected_nonce, deployment, timeout=300):
        attempted.append(deployment["id"])
        raw = "litellm.APIError: unable to comply; cannot process; insufficient context"
        return False, _vote(seat_id, expected_nonce, "UNAVAILABLE"), raw, {
            "deployment_id": deployment["id"],
            "failure_class": "TIMEOUT",
        }

    monkeypatch.setattr(round_table_engine, "_execute_seat", fixed)
    monkeypatch.setattr(round_table_engine, "_execute_seat_deployment", fallback)

    result = round_table_engine.conduct_round("RT-ERROR-REFUSAL-WORDS", parallel=False)

    assert result["status"] == "HELD_UNAVAILABLE"
    assert "chair-fable-tc-local" not in attempted


@pytest.mark.parametrize(("message", "expected"), [
    ("insufficient_quota", "QUOTA_EXHAUSTED"),
    ("credit balance is empty", "CREDITS_EXHAUSTED"),
    ("429 too many requests", "RATE_LIMITED"),
    ("invalid api key", "AUTH_FAILED"),
    ("connection refused", "SERVER_UNAVAILABLE"),
    ("request timed out", "TIMEOUT"),
    ("unknown model", "MODEL_UNAVAILABLE"),
    ("model reached its end of life and is no longer available", "MODEL_UNAVAILABLE"),
])
def test_failure_classifier(message, expected):
    assert round_table_engine._classify_failure_text(message) == expected










def test_failed_runner_cannot_smuggle_a_valid_vote_before_error():
    nonce = "quota-nonce"
    raw = json.dumps(_vote("openai", nonce)) + "\n403 usage limit reached"

    ok, vote, _ = round_table_engine._parse_seat_execution("openai", nonce, False, raw)

    assert not ok
    assert vote["verdict"] == "UNAVAILABLE"


def test_streaming_runner_persists_live_cursor(tmp_path):
    attempt_dir = tmp_path / "streams"
    returncode, raw, meta = round_table_engine._stream_subprocess(
        [
            round_table_engine.sys.executable,
            "-u",
            "-c",
            "import sys,time;sys.stdout.write('first ');sys.stdout.flush();time.sleep(.15);sys.stdout.write('second');sys.stdout.flush()",
        ],
        env=round_table_engine.os.environ.copy(),
        cwd=tmp_path,
        stdin_text=None,
        timeout=5,
        attempt_dir=attempt_dir,
        attempt_label="stream-smoke",
    )

    checkpoint = json.loads((attempt_dir / f"{meta['attempt_id']}.json").read_text(encoding="utf-8"))
    assert returncode == 0
    assert raw == "first second"
    assert checkpoint["status"] == "COMPLETED"
    assert checkpoint["cursor"]["byte_offset"] == len(raw)
    assert checkpoint["cursor"]["chunk_sequence"] >= 1
    assert checkpoint["first_output_ms"] is not None
    assert checkpoint["first_content_ms"] is not None






def test_streaming_timeout_preserves_partial_checkpoint(tmp_path):
    returncode, raw, meta = round_table_engine._stream_subprocess(
        [
            round_table_engine.sys.executable,
            "-u",
            "-c",
            "import sys,time;sys.stdout.write('partial response');sys.stdout.flush();time.sleep(5)",
        ],
        env=round_table_engine.os.environ.copy(),
        cwd=tmp_path,
        stdin_text=None,
        timeout=1,
        attempt_dir=tmp_path / "streams",
        attempt_label="timeout-smoke",
    )

    assert returncode == -1
    assert raw == "partial response"
    assert meta["status"] == "INTERRUPTED"
    assert meta["timed_out"] is True
    assert meta["cursor"]["byte_offset"] == len(raw)


def test_chair_vote_remains_atomic_after_partial_failure(monkeypatch):
    deployment = round_table_engine._deployment_by_id("chair-qwen-local")
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        return False, '{"seat":"openai"', {
            "deployment_id": deployment["id"],
            "failure_class": "SERVER_UNAVAILABLE",
            "cursor": {"byte_offset": 17},
        }

    monkeypatch.setattr(round_table_engine, "_run_deployment", interrupted)
    ok, vote, _, _ = round_table_engine._execute_seat_deployment(
        "openai", "prompt", "nonce", deployment
    )

    assert not ok
    assert vote["verdict"] == "UNAVAILABLE"
    assert len(calls) == 1


def test_text_role_semantically_resumes_an_interrupted_partial(monkeypatch, tmp_path):
    deployment = round_table_engine._deployment_by_id("mediator-qwen-local")
    partial = "# Refined proposal\n" + ("resolved invariant; " * 8) + "BOUNDARY"
    continuation = partial[-32:] + "\n\nFinal verified section."
    calls = []

    def interrupted_then_complete(entry, prompt, timeout=300, **kwargs):
        calls.append((prompt, timeout, kwargs["attempt_label"]))
        if len(calls) == 1:
            return False, partial, {
                "deployment_id": entry["id"],
                "failure_class": "SERVER_UNAVAILABLE",
                "cursor": {"byte_offset": len(partial.encode("utf-8"))},
            }
        return True, continuation, {
            "deployment_id": entry["id"],
            "cursor": {"byte_offset": len(continuation.encode("utf-8"))},
        }

    monkeypatch.setattr(round_table_engine, "_execute_text_role", interrupted_then_complete)
    ok, artifact, receipt = round_table_engine._execute_resumable_text_role(
        deployment,
        "Original mediation mandate",
        timeout=30,
        attempt_dir=tmp_path,
        attempt_label="mediator-test",
    )

    assert ok
    assert artifact == partial + "\n\nFinal verified section."
    assert receipt["semantic_resume"] is True
    assert len(receipt["semantic_attempts"]) == 2
    assert [call[2] for call in calls] == ["mediator-test-a1", "mediator-test-a2"]
    assert 0 < calls[1][1] <= calls[0][1] <= 30
    assert partial in calls[1][0]




def test_codex_served_model_mismatch_fails_closed(monkeypatch):
    deployment = round_table_engine._deployment_by_id("chair-openai-cli")
    monkeypatch.setattr(round_table_engine, "_stream_subprocess", lambda *args, **kwargs: _stream_result("model: gpt-5.5\nresponse"))

    ok, _, receipt = round_table_engine._run_deployment(deployment, "prompt")

    assert not ok
    assert receipt["model_attestation"] == "mismatch"
    assert receipt["failure_class"] == "SERVED_MODEL_MISMATCH"


def test_a_cli_seat_runs_in_an_empty_folder_of_its_own(monkeypatch):
    # The seat folder was never assigned: agy and kimi seats ran in tare's own folder and could read its
    # AGENTS.md or CLAUDE.md as instructions (found 2026-10-07).
    seen = []
    def stream(cmd, **kwargs):
        seen.append(kwargs["cwd"])
        assert kwargs["cwd"] is not None and Path(kwargs["cwd"]).is_dir() and not any(Path(kwargs["cwd"]).iterdir())
        return _stream_result("model: gpt-5.5\nresponse")
    monkeypatch.setattr(round_table_engine, "_stream_subprocess", stream)
    round_table_engine._run_deployment(round_table_engine._deployment_by_id("chair-openai-cli"), "prompt")
    assert len(seen) == 1 and not Path(seen[0]).exists()  # removed afterwards

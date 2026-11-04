"""Native text roles through real loopback HTTP; no provider or Aider required."""
import copy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import time

import pytest

from compute_plane.native_models import ModelProfile, NativeModelAdapter
from relay import native_generation, round_table_engine as engine, round_table_roster as roster
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl


def test_compaction_profile_is_model_specific_and_separate_from_role_budgets(endpoint):
    from relay.conversation_review import context_contract
    from relay.native_profiles import describe
    small = deployment(endpoint)
    large = copy.deepcopy(small)
    large['native_profile'].update(context_window=200000, output_tokens=16384)
    large['native_context_policy'] = {'trigger': .9, 'target': .6, 'source': 'Fixture measured recommendation'}
    one, two = context_contract(small), context_contract(large)
    assert (one['window'], one['output_reserve']) != (two['window'], two['output_reserve'])
    assert one['policy'] == {'trigger': .8, 'target': .5, 'max_compactions': 1}
    assert two['policy'] == {'trigger': .9, 'target': .6, 'max_compactions': 1}
    assert 'Tare default' in describe('small', small)['compaction_policy']['source']
    assert describe('large', large)['compaction_policy']['source'] == 'Fixture measured recommendation'
    assert 'native_execution_limits' not in small and 'native_execution_limits' not in large


@pytest.mark.parametrize('declared', [None, [], {'trigger': True, 'source': 'fixture'},
    {'trigger': .5, 'target': .8, 'source': 'fixture'}, {'trigger': 1, 'source': 'fixture'},
    {'trigger': .9}, {'source': 'fixture'}, {'trigger': .9, 'source': ' '},
    {'trigger': .9, 'source': 'fixture', 'max_compactions': 100}])
def test_invalid_model_compaction_policy_is_rejected_before_inference(endpoint, declared):
    dep = deployment(endpoint)
    dep['native_context_policy'] = declared
    with pytest.raises(ValueError):
        native_generation.deployment_profile(dep, dep['native_profile']['model'])
    assert not endpoint['calls']


def test_explicit_estimate_makes_one_http_call_and_never_becomes_usage(endpoint):
    dep = deployment(endpoint, 'bedrock-converse')
    dep['native_counter_protocol'] = 'utf8-envelope-estimate-v1'
    endpoint['usage'] = {'inputTokens': 51, 'outputTokens': 7, 'cacheReadInputTokens': 0, 'cacheWriteInputTokens': 0}
    ok, _, receipt = native_generation.generate_text(dep, 'fixture-model', 'Return JSON.', 10)
    assert ok and len(endpoint['calls']) == 1 and endpoint['calls'][0][0].endswith('/converse')
    assert receipt['count']['status'] == 'estimated' and receipt['count']['input_tokens'] is None
    assert receipt['count']['estimated_input_tokens'] > 51
    assert native_generation.token_totals([receipt], {'layout': 'converse-exclusive', 'source': 'fixture'}) == {
        'input': 51, 'output': 7}


def test_a_local_route_learns_which_tare_frontend_calls(endpoint, monkeypatch):
    from relay.challenger import client_headers
    monkeypatch.setenv('TARE_CLIENT', 'tare.tools TUI')
    dep = deployment(endpoint)  # a loopback route, like the GPU gateway
    native_generation.generate_text(dep, 'fixture-model', 'Return JSON.', 10)
    assert len(endpoint['agents']) > 1 and set(endpoint['agents']) == {'tare.tools TUI'}  # counting and generation
    cloud = {**dep, 'native_profile': {**dep['native_profile'], 'endpoint': 'https://api.example.test/v1'}}
    assert client_headers(cloud) == {}


def test_estimated_request_over_window_is_blocked_without_http(endpoint):
    dep = deployment(endpoint)
    dep['native_counter_protocol'] = 'utf8-envelope-estimate-v1'
    ok, _, receipt = native_generation.generate_text(dep, 'fixture-model', 'x' * 4096, 10)
    assert not ok and receipt['failure_class'] == 'CONTEXT_LIMIT'
    assert not endpoint['calls'] and receipt['generation_calls'] == 0


@pytest.fixture
def endpoint():
    state = {"calls": [], "count": 64, "text": '{"answer":42}', "usage": None,
             "model": "fixture-model", "partial": False, "tools": False, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append((self.path, body, self.headers.get("Authorization")))
            state.setdefault("agents", []).append(self.headers.get("User-Agent"))
            status = 200
            if self.path == "/apply-template":
                result = {"prompt": "fixture template"}
            elif self.path == "/tokenize":
                result = {"tokens": list(range(state["count"]))}
            elif self.path.endswith("/count-tokens"):
                result = {"inputTokens": state["count"]}
            elif self.path == "/chat/completions":
                status = state["status"]
                message = {"role": "assistant", "content": state["text"], "reasoning_content": "internal"}
                if state["tools"]:
                    message["tool_calls"] = [{"id": "bad", "type": "function", "function": {
                        "name": "tare_deliver", "arguments": '{"summary":"bad"}'}}]
                result = {"model": state["model"], "usage": state["usage"], "choices": [{
                    "finish_reason": "length" if state["partial"] else "stop", "message": message}]}
            else:
                assert self.path.endswith("/converse")
                status = state["status"]
                blocks = [{"reasoningContent": {"reasoningText": {"text": "internal", "signature": "s"}}},
                          {"text": state["text"]}]
                if state["tools"]:
                    blocks.append({"toolUse": {"toolUseId": "bad", "name": "tare_deliver", "input": {}}})
                result = {"usage": state["usage"], "stopReason": "max_tokens" if state["partial"] else "end_turn",
                          "output": {"message": {"role": "assistant", "content": blocks}}}
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def deployment(endpoint, protocol="openai-chat"):
    params = ({"chat_template_kwargs": {"enable_thinking": True, "thinking_budget": 256}}
              if protocol == "openai-chat" else {
                  "additionalModelRequestFields": {"thinking": {"type": "enabled", "budget_tokens": 256}}})
    profile = ModelProfile(protocol, "fixture-model", 4096, 512, "loopback-fixture", endpoint["endpoint"],
                           parameters_json=json.dumps(params), cache_system=protocol == "bedrock-converse",
                           cache_support_source="fixture-only" if protocol == "bedrock-converse" else None)
    return {"id": "fixture", "runner": "native-http", "model": "qwen-3.8-27b", "effort": None,
            "provider": "Local", "native_profile": asdict(profile),
            "native_counter_endpoint": endpoint["endpoint"]}


@pytest.mark.parametrize("protocol", ["openai-chat", "bedrock-converse"])
def test_one_text_call_without_editing_protocol_and_with_count_cache_and_receipt(endpoint, tmp_path, protocol):
    dep = deployment(endpoint, protocol)
    ok, text, receipt = native_generation.generate_text(dep, "fixture-model", "Return JSON.", 10,
                                                       attempt_dir=tmp_path)
    assert ok and text == '{"answer":42}'
    assert receipt["usage"] is None and receipt["generation_calls"] == 1
    assert [r["purpose"] for r in receipt["native_observations"]] == ["count", "model"]
    wire = endpoint["calls"][-1][1]
    assert not {"tools", "tool_choice", "toolConfig"} & wire.keys()
    assert len(wire["messages"]) == (2 if protocol == "openai-chat" else 1)
    if protocol == "openai-chat":
        assert receipt["model_attestation"] == "runtime_reported_match"
        assert wire["chat_template_kwargs"]["thinking_budget"] == 256
    else:
        assert receipt["model_attestation"] == "request_pinned_unverified"
        assert receipt["served_model"] == "UNATTESTED"
        assert wire["system"][-1] == {"cachePoint": {"type": "default"}}
        assert endpoint["calls"][0][1]["input"]["converse"] == {
            k: wire[k] for k in ("system", "messages")}
    saved = json.loads(next(tmp_path.glob("*.native.json")).read_text())
    assert saved == receipt
    assert next(tmp_path.glob("*.response.txt")).read_text() == text
    assert all(c["cleanup_verified"] for row in saved["native_observations"] for c in row["transport_calls"])


@pytest.mark.parametrize("protocol", ["openai-chat", "bedrock-converse"])
@pytest.mark.parametrize("fault,expected", [("partial", "RESPONSE_INCOMPLETE"),
                                          ("tools", "INVALID_PROVIDER_RESPONSE"),
                                          ("empty", "EMPTY_RESPONSE"), ("429", "HTTP_429")])
def test_text_failure_does_not_retry_or_fabricate_success(endpoint, protocol, fault, expected):
    if fault == "429":
        endpoint["status"] = 429
    elif fault == "empty":
        endpoint["text"] = ""
    else:
        endpoint[fault] = True
    endpoint["usage"] = {"fixture": 7}
    ok, _, receipt = native_generation.generate_text(deployment(endpoint, protocol), "fixture-model", "Answer", 10)
    assert not ok and receipt["failure_class"] == expected
    assert receipt["generation_calls"] == 1
    assert len(endpoint["calls"]) == (3 if protocol == "openai-chat" else 2)
    assert receipt["usage"] == (None if fault == "429" else {"fixture": 7})


def test_identity_mismatch_preserves_usage_without_accepting_answer(endpoint):
    endpoint.update(model="wrong-model", usage={"prompt_tokens": 20, "completion_tokens": 5})
    ok, text, receipt = native_generation.generate_text(deployment(endpoint), "fixture-model", "Answer", 10)
    assert not ok and not text
    assert receipt["failure_class"] == "SERVED_MODEL_MISMATCH"
    assert receipt["model_attestation"] == "mismatch"
    assert receipt["usage"] == endpoint["usage"]


@pytest.mark.parametrize("fault", ["missing-counter", "context-overflow", "cancelled"])
def test_no_generation_without_context_room_or_current_control(endpoint, fault):
    dep = deployment(endpoint)
    cancellation = CancellationToken()
    control = ExecutionControl(BindingRef("text", "binding", "caller", 1, "token"),
                               cancellation, time.monotonic() + 10)
    if fault == "missing-counter":
        dep.pop("native_counter_endpoint")
    elif fault == "context-overflow":
        endpoint["count"] = 3585  # 4096 window minus 512 reserved output plus one.
    else:
        cancellation.cancel()
    ok, text, receipt = native_generation.generate_text(dep, "fixture-model", "Answer", 10, control=control)
    assert not ok and not text and receipt["generation_calls"] == 0
    assert receipt["usage"] is None
    assert not any(path.endswith(("completions", "converse")) for path, _, _ in endpoint["calls"])


def test_cancel_after_completed_response_keeps_usage_but_refuses_success(endpoint, monkeypatch):
    cancellation = CancellationToken()
    control = ExecutionControl(BindingRef("text", "binding", "caller", 1, "token"),
                               cancellation, time.monotonic() + 10)
    original = NativeModelAdapter.generate

    def generate(self, *args):
        result = original(self, *args)
        cancellation.cancel()
        return result

    monkeypatch.setattr(NativeModelAdapter, "generate", generate)
    endpoint["usage"] = {"prompt_tokens": 12, "completion_tokens": 5}
    ok, _, receipt = native_generation.generate_text(deployment(endpoint), "fixture-model", "Answer", 10,
                                                     control=control)
    assert not ok and receipt["failure_class"] == "CONTROL_STOPPED"
    assert receipt["usage"] == endpoint["usage"]


def test_credentials_are_resolved_without_persisting_or_using_legacy_environment(endpoint, tmp_path):
    dep = deployment(endpoint)
    dep["native_credential_env"] = "TARE_FIXTURE_KEY"
    ok, _, receipt = native_generation.generate_text(dep, "fixture-model", "Answer", 10,
        attempt_dir=tmp_path, credential_resolver=lambda name: "fixture-secret")
    assert ok and endpoint["calls"][-1][2] == "Bearer fixture-secret"
    assert "fixture-secret" not in json.dumps(receipt)
    assert all("fixture-secret" not in p.read_text() for p in tmp_path.iterdir())


@pytest.mark.parametrize("answer,valid", [("valid", True), ("wrong-nonce", False), ("not-json", False)])
def test_round_table_calls_native_and_keeps_vote_validation(endpoint, monkeypatch, tmp_path, answer, valid):
    data = copy.deepcopy(engine.ROSTER)
    dep = next(e for e in data["chairs"]["fallbacks"] if e["id"] == "chair-qwen-local")
    for key in list(dep):
        if key.startswith('native_'):
            del dep[key]
    dep.update(deployment(endpoint))
    data['models']['native-fixture'] = copy.deepcopy(data['models'][dep['model']])
    dep['model'] = 'native-fixture'
    data["models"][dep["model"]]["runtime"]["native-http"] = "fixture-model"
    roster.validate_roster(data)
    monkeypatch.setattr(engine, "ROSTER", data)

    def forbidden(*args, **kwargs):
        pytest.fail("native generation must not enter the CLI subprocess path")

    for name in ("_build_deployment_command", "_stream_subprocess"):
        monkeypatch.setattr(engine, name, forbidden)
    vote = {"seat": "codex", "execution_nonce": "nonce" if answer == "valid" else "wrong",
            "verdict": "APPROVE", "confidence": 0.9, "summary": "Bounded fixture", "issues": []}
    endpoint["text"] = json.dumps(vote) if answer != "not-json" else "not JSON"
    ok, parsed, raw, receipt = engine._execute_seat_deployment("codex", "Return the vote", "nonce", dep,
                                                             timeout=10, attempt_dir=tmp_path)
    assert ok is valid
    assert receipt["runner"] == "native-http" and receipt["generation_calls"] == 1
    if valid:
        assert parsed["verdict"] == "APPROVE"


def test_native_roster_rejects_unpinned_profile_ignored_effort_and_bedrock_policy_change(endpoint):
    dep = deployment(endpoint)
    with pytest.raises(ValueError, match="exactly"):
        roster.validate_native_deployment(dep, "different")
    dep["effort"] = "high"
    with pytest.raises(ValueError, match="thinking parameters"):
        roster.validate_native_deployment(dep, "fixture-model")
    with pytest.raises(ValueError, match="not a Round Table transport"):
        roster.validate_native_deployment(deployment(endpoint, "bedrock-converse"), "fixture-model")


def test_native_rate_limit_uses_existing_round_table_availability_policy(endpoint, monkeypatch):
    dep = deployment(endpoint)
    dep["quota_domain"] = "fixture-quota"
    data = copy.deepcopy(engine.ROSTER)
    data["models"][dep["model"]]["runtime"]["native-http"] = "fixture-model"
    monkeypatch.setattr(engine, "ROSTER", data)
    endpoint["status"] = 429
    ok, _, receipt = engine._run_deployment(dep, "Answer", 10)
    assert not ok and receipt["failure_class"] == "RATE_LIMITED"
    assert receipt["native_failure_code"] == "HTTP_429"
    block = engine._record_availability_failure({}, dep, receipt)
    assert block["domain_type"] == "quota_domain" and block["domain"] == "fixture-quota"


def test_no_legacy_function_tool_can_be_smuggled_into_text_profile(endpoint):
    dep = deployment(endpoint)
    dep["native_profile"]["parameters_json"] = '{"functions":[{"name":"edit"}]}'
    with pytest.raises(ValueError, match="legacy function tools"):
        native_generation.generate_text(dep, "fixture-model", "Answer", 10)
    assert endpoint["calls"] == []

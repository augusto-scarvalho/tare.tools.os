"""Offline codec tests using the actual compute-plane dataclasses."""

import hashlib
import json
import time
from dataclasses import MISSING, fields, replace
from typing import get_type_hints

import pytest

from compute_plane.action_assessment import AssessmentRequest
from relay.action_model_assessment import (
    REASON_CODES, SCHEMA, SYSTEM_GUIDANCE, build_model_prompt, parse_model_answer,
)


@pytest.fixture
def assessment():
    context = '{"scope":"local","note":"ação"}'
    deadline = time.monotonic() + 60
    snapshot_type = get_type_hints(AssessmentRequest)["action"]
    hints = get_type_hints(snapshot_type)
    values = {}
    for field in fields(snapshot_type):
        if field.default is not MISSING:
            values[field.name] = field.default
        elif field.default_factory is not MISSING:
            values[field.name] = field.default_factory()
        else:
            values[field.name] = 1 if hints.get(field.name) is int else "fixture"
        if field.name.endswith("sha256"):
            values[field.name] = "a" * 64
    overrides = {
        "operation": "process.exec", "repository": "fixture",
        "cwd": "C:\\fixture", "execution_profile": "local",
        "deadline": deadline, "argv": ("python", "-c", "print('ação')"),
        "script": None, "stdin": b"", "payload_json": None,
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
    }
    values.update({key: value for key, value in overrides.items() if key in values})
    return AssessmentRequest(action=snapshot_type(**values), stage="classifier",
                             backend="tare", public_context=context,
                             max_output_tokens=512, deadline=deadline)


def answer(assessment, **changes):
    result = dict(schema=SCHEMA, request_sha256=assessment.request_sha256,
                  verdict="ROUTINE_ELIGIBLE" if assessment.stage == "classifier"
                  else "APPROVE_RECOMMENDED", reason_codes=["ROUTINE_LOCAL"],
                  authority="NONE")
    result.update(changes)
    return json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("stage", ["classifier", "reviewer"])
def test_valid_stages(assessment, stage):
    assessment = replace(assessment, stage=stage)
    system, prompt = build_model_prompt(assessment)
    obj = json.loads(prompt)
    assert system == SYSTEM_GUIDANCE[stage]
    assert obj["request_sha256"] == assessment.request_sha256
    assert obj["public_context"] == assessment.public_context
    assert obj["action"]["argv"] == list(assessment.action.argv)
    assert obj["output_contract"]["authority"] == "NONE"
    assert parse_model_answer(answer(assessment), assessment)[1] == ("ROUTINE_LOCAL",)


@pytest.mark.parametrize("changes", [
    {"authority": "ALLOW"}, {"request_sha256": "0" * 64},
    {"schema": "other"}, {"verdict": "APPROVE_RECOMMENDED"},
    {"actions": []}, {"reason_codes": ["ARBITRARY"]},
    {"reason_codes": []}, {"reason_codes": list(REASON_CODES[:9])},
    {"reason_codes": ["NETWORK", "NETWORK"]}, {"reason_codes": [1]},
    {"reason_codes": "NETWORK"}, {"verdict": []},
])
def test_invalid_fields(assessment, changes):
    with pytest.raises(ValueError, match="^INVALID_ANSWER$"):
        parse_model_answer(answer(assessment, **changes), assessment)


@pytest.mark.parametrize("content", [
    '{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
    '```json\n{}\n```', '{}', '[]', 'null', '\ud800', b'{}',
])
def test_invalid_json(assessment, content):
    with pytest.raises(ValueError, match="^INVALID_ANSWER$"):
        parse_model_answer(content, assessment)


def test_duplicate_authority_rejected(assessment):
    valid = answer(assessment)
    parse_model_answer(valid, assessment)
    content = valid.replace('"authority": "NONE"',
                            '"authority": "ALLOW", "authority": "NONE"')
    assert json.loads(content) == json.loads(valid)
    with pytest.raises(ValueError, match="^INVALID_ANSWER$"):
        parse_model_answer(content, assessment)


@pytest.mark.parametrize("limit", [True, False, 0, -1, float("nan"), 1.5, None])
def test_invalid_limits(assessment, limit):
    for codec, args in ((build_model_prompt, (assessment,)),
                        (parse_model_answer, (answer(assessment), assessment))):
        with pytest.raises(ValueError, match="^INVALID_LIMIT$"):
            codec(*args, max_bytes=limit)


def test_byte_boundaries(assessment):
    _, prompt = build_model_prompt(assessment)
    size = len(prompt.encode("utf-8"))
    assert build_model_prompt(assessment, max_bytes=size)[1] == prompt
    with pytest.raises(ValueError, match="^INVALID_INPUT$"):
        build_model_prompt(assessment, max_bytes=size - 1)
    content = answer(assessment)
    size = len(content.encode("utf-8"))
    parse_model_answer(content, assessment, max_bytes=size)
    with pytest.raises(ValueError, match="^INVALID_ANSWER$"):
        parse_model_answer(content, assessment, max_bytes=size - 1)


def test_exact_utf8_and_binary_rejection(assessment):
    script, stdin = "é\r\ne\u0301\n", "entrada\x00\r\nç"
    action = replace(assessment.action, argv=None, script=script.encode("utf-8"),
                     stdin=stdin.encode("utf-8"))
    scripted = replace(assessment, action=action)
    obj = json.loads(build_model_prompt(scripted)[1])["action"]
    assert obj["script"] == script and obj["stdin"] == stdin
    for key in ("script", "stdin"):
        binary = replace(scripted, action=replace(action, **{key: b"\xff"}))
        with pytest.raises(ValueError, match="^INVALID_INPUT$"):
            build_model_prompt(binary)


def test_request_guard_and_guidance(assessment):
    assert SYSTEM_GUIDANCE["classifier"] != SYSTEM_GUIDANCE["reviewer"]
    assert "mandatory review" in SYSTEM_GUIDANCE["classifier"]
    assert "native auto-review" in SYSTEM_GUIDANCE["reviewer"]
    with pytest.raises(ValueError, match="^INVALID_REQUEST$"):
        build_model_prompt(replace(assessment, backend="other"))
    with pytest.raises(ValueError, match="^INVALID_REQUEST$"):
        build_model_prompt(object())


@pytest.fixture
def jev_host(tmp_path, assessment, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from compute_plane.native_journal import NativeJournal
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay.action_model_assessment import TareJevActionClassifier, JEV_MODEL
    def frontier(*args, **kwargs):
        raise AssertionError('Frontier generation is forbidden for Jev classification')
    monkeypatch.setattr('relay.native_generation.generate_text', frontier)
    calls=[];reply={'verdict':'ROUTINE_ELIGIBLE'}
    journal=NativeJournal(tmp_path/'jev.jsonl')
    action=assessment.action
    control=ExecutionControl(BindingRef(action.work_id,action.binding_id,'operator',action.fence_seq,'opaque'),
        CancellationToken(),assessment.deadline)
    def transport(request,**kwargs):
        calls.append((request,kwargs))
        if 'callback' in reply:reply['callback']()
        if 'error' in reply:raise ValueError(reply['error'])
        choice=reply['verdict'];options=request['options']
        return {'choice':choice,'probabilities':{k:0.97 if k==choice else 0.01 for k in options},
            'usage':{'input_tokens':345,'output_tokens':0},
            'model_identity':{'model':reply.get('model',JEV_MODEL),'method':'jev-systemone-choice-v1'}}
    def build(disclose=True,eligible=True,key='offline fixture key'):
        return TareJevActionClassifier(journal_factory=lambda:journal,custody_factory=nullcontext,
            disclosure_check=lambda r:disclose,eligibility_check=lambda:eligible,
            key_factory=lambda:key,assess_choice=transport,facts_factory=lambda a:{'code':'print(123)'})
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('verdict',['ROUTINE_ELIGIBLE','REVIEW_REQUIRED','DENY_RECOMMENDED','INSUFFICIENT_EVIDENCE'])
def test_jev_classification_is_one_pinned_choice_without_frontier(jev_host,verdict):
    h=jev_host;h.reply['verdict']=verdict
    result=h.build().assess(h.assessment,h.control)
    assert result.status=='OBSERVED' and result.verdict==verdict and result.matches(h.assessment)
    assert result.authority=='NONE' and json.loads(result.usage_json)['input_tokens']==345
    assert len(h.calls)==1 and h.calls[0][1]['model']==h.JEV_MODEL
    assert 0<h.calls[0][1]['timeout']<=5
    state=json.loads(h.calls[0][0]['state'])
    assert state['action']['action_sha256']==h.assessment.action.action_sha256
    assert state['operator_context']==h.assessment.public_context
    assert state['host_facts']['code']=='print(123)'
    second=h.build().assess(h.assessment,h.control)
    assert second.status=='ABSTAINED' and second.reason_codes==('ATTEMPT_ALREADY_CONSUMED',)
    assert len(h.calls)==1


@pytest.mark.parametrize('options',[{'disclose':False},{'eligible':False},{'key':''}])
def test_jev_disclosure_setup_and_missing_key_send_nothing(jev_host,options):
    h=jev_host;result=h.build(**options).assess(h.assessment,h.control)
    assert result.status in ('ABSTAINED','UNAVAILABLE') and h.calls==[]


def test_jev_cancel_after_response_does_not_return_approval(jev_host):
    h=jev_host;h.reply['callback']=h.control.cancellation.cancel
    result=h.build().assess(h.assessment,h.control)
    assert result.status=='CANCELLED' and result.verdict is None and len(h.calls)==1


def test_jev_busy_is_not_retried_or_replaced_by_a_frontier_model(jev_host):
    h=jev_host;h.reply['error']='ASSESSMENT_RESOURCE_BUSY'
    assert h.build().assess(h.assessment,h.control).status=='UNAVAILABLE'
    assert h.build().assess(h.assessment,h.control).status=='ABSTAINED'
    assert len(h.calls)==1


def test_jev_changed_model_and_invalid_distribution_cannot_approve(jev_host):
    h=jev_host;h.reply['model']='jev-other'
    result=h.build().assess(h.assessment,h.control)
    assert result.status=='MALFORMED' and result.verdict is None


def test_jev_oversized_context_is_not_truncated_or_sent(jev_host):
    h=jev_host;context='important fact '*1000
    action=replace(h.assessment.action,context_sha256=hashlib.sha256(context.encode()).hexdigest())
    request=replace(h.assessment,action=action,public_context=context)
    result=h.build().assess(request,h.control)
    assert result.status=='ABSTAINED' and h.calls==[]


def test_jev_requires_durable_intent_before_any_api_call(jev_host,monkeypatch):
    h=jev_host;monkeypatch.setattr(h.journal,'append',lambda event:False)
    result=h.build().assess(h.assessment,h.control)
    assert result.status=='UNAVAILABLE' and h.calls==[]


def test_jev_expired_request_is_a_timeout_and_sends_nothing(jev_host):
    h=jev_host;request=replace(h.assessment,deadline=time.monotonic()-1)
    result=h.build().assess(request,h.control)
    assert result.status=='TIMEOUT' and result.reason_codes==('DEADLINE_EXCEEDED',) and h.calls==[]


def test_jev_invalid_candidate_distribution_is_malformed(jev_host):
    h=jev_host;h.reply['verdict']='UNKNOWN_OPTION'
    result=h.build().assess(h.assessment,h.control)
    assert result.status=='MALFORMED' and result.verdict is None and len(h.calls)==1

import json

import pytest
from tare_tools_agent_runtime import ConversationLog
from relay import conversation_recheck,conversation_tasks as tasks,work_units
from test_conversation_tasks import host,intake,planned  # noqa: F401
from test_native_agent import setup,Client,deployment  # noqa: F401


@pytest.fixture(autouse=True)
def controlled_consent_judges(monkeypatch):
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {})


@pytest.mark.parametrize('applied',[False,True])
def test_host_recheck_keeps_native_journal_untouched_and_dispatches_only_reused_candidate(host,monkeypatch,applied):
    tools,control,*_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    task['delivery']={'verdict':'NOT_VERIFIED','candidate':{'patch':'retained','sha256':'a'*64}}
    tasks.save(tools.root,value)
    journal=ConversationLog();journal.append({'kind':'model_requested','call_id':'unresolved-read-only-proposal'})
    before=journal.snapshot().revision
    seen=[]
    def run(*args,**kwargs):
        seen.append(kwargs)
        return {'verdict':'VERIFIED' if applied else 'NOT_VERIFIED','applied':applied,'files':['a.py']}
    monkeypatch.setattr(work_units,'run_unit',run)
    outcome=conversation_recheck.recheck(tools,control,tools.root,'host-test',journal)
    assert outcome.status==('proposed' if applied else 'blocked')
    assert journal.snapshot().revision==before and outcome.model_calls==1
    assert len(seen)==1 and seen[0]['candidate']['review_only'] is True and seen[0]['review'] is not None
    assert 'host_recheck_result' in (tools.root/'host-actions.jsonl').read_text()


def test_no_retained_candidate_causes_no_dispatch_or_host_log(host):
    tools,control,*_=host
    intake(host)
    with pytest.raises(ValueError,match='No saved candidate'):
        conversation_recheck.recheck(tools,control,tools.root,'no-candidate',ConversationLog())
    assert not (tools.root/'host-actions.jsonl').exists()


def test_retry_delivery_starts_a_fresh_implementer_for_a_planned_demand_without_a_candidate(host,monkeypatch):
    tools,control,*_=host
    intake(host);planned(host)  # a guard stopped this demand before any candidate existed
    seen=[]
    def run(*args,**kwargs):
        seen.append((args[1].get('operator_delivery_retry'),kwargs.get('candidate')))
        return {'verdict':'VERIFIED','applied':True,'files':['a.py']}
    monkeypatch.setattr(work_units,'run_unit',run)
    with pytest.raises(ValueError,match='No saved candidate'):  # a recheck still needs one
        conversation_recheck.recheck(tools,control,tools.root,'recheck',ConversationLog())
    outcome=conversation_recheck.recheck(tools,control,tools.root,'retry',ConversationLog(),recheck_only=False)
    assert outcome.status=='proposed' and seen==[(True,None)]  # the user's retry, on a fresh implementer


def test_failed_planning_handoff_is_blocked_instead_of_claiming_repaired(host,monkeypatch):
    from compute_plane.native_journal import save_json
    from tare_tools_agent_runtime import EffectResult
    tools,control,*_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    receipt=tools.root/'units'/'prior'/'receipt.json';receipt.parent.mkdir(parents=True)
    save_json(receipt,{'result':{'audit':{'summary':'Scope repair required.','findings':[],'remaining':[]}}})
    task['delivery']={'candidate':{'sha256':'a'*64},'audit':{'verdict':'REVISE'},'receipt':str(receipt)}
    tasks.save(tools.root,value)
    monkeypatch.setattr(tools,'execute',lambda request,control:EffectResult(request.effect_id,
        'succeeded',json.dumps({'status':'EXHAUSTED'}),True))
    result=conversation_recheck.recheck(tools,control,tools.root,'failed-plan',ConversationLog(),repair_plan=True)
    assert result.status=='blocked' and result.reason=='HOST_PLAN_NOT_REPAIRED'


def test_public_recheck_command_uses_no_model_or_native_journal_rewrite(setup,monkeypatch):
    from tare_tools_agent_runtime import NativeOutcome
    from relay import native_agent
    from relay.console_backend import Selection,command
    config,custody,root=setup;config['verifiers']={}
    first=Client(steps=[('tare_deliver',{'summary':'First answer.'})])
    native_agent.run_session(config,deployment(),custody,'Inspect only.',conversation=True,
        client=first,local_counter=first)
    before=(custody/'journal.jsonl').read_bytes()
    calls=[]
    def recover(tools,control,*args):
        calls.append(True)
        assert not control.should_stop()
        return NativeOutcome('proposed','HOST_RECHECK_COMPLETED',1,'Checked and independently audited.')
    monkeypatch.setattr(conversation_recheck,'recheck',recover)
    client=Client(steps=[])
    result=native_agent.run_session(None,None,custody,resume=True,recheck_candidate=True,
        client=client,local_counter=client)
    assert result['status']=='PROPOSED' and result['coordinator_calls_started']==0
    assert result['host_action']=='RECHECK_CANDIDATE' and calls==[True] and client.generations==0
    assert (custody/'journal.jsonl').read_bytes()==before
    selected=Selection(str(root),session=str(custody),recheck_candidate=True,
        resume_coordinator_calls=90,resume_coordinator_seconds=1800)
    argv=command(selected,resume=True,assistant=True)
    assert '--recheck-candidate' in argv and '--follow-up' not in argv
    assert argv[argv.index('--active-call-budget')+1]=='90'


@pytest.mark.parametrize('answer,decision', [('sim, tenta de novo', 'yes'), ('manda ver', 'yes'),
    ('pode sim, manda bala', 'yes'), ('não, obrigado', 'no'), ('deixa pra depois', 'no'),
    ('espera, me explica antes', 'pending'), ('Por que?', 'pending')])
def test_retry_custody_uses_isolated_decision_and_consumes_once(host, answer, decision, monkeypatch):
    monkeypatch.setattr('relay.consent_decisions.select_rule', lambda: 'judge')
    from test_consent_decisions import verdict
    choice = {'yes':'allow', 'no':'deny', 'pending':'unclear'}[decision]
    monkeypatch.setattr('relay.turn_engine.judges', lambda packet, **k:
        {'jev': verdict(choice)} if set(packet['options']) == {'allow', 'deny', 'unclear'} else {})
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    assert not decide(tools.root, task, unit)
    user_reply(host, answer)
    tools.__dict__['_host_input_id'] = 'synthetic-yes'
    from relay import consent_decisions as consent
    q = consent.pending(tools.root, tools.session_id)[0]
    consent.record(tools.root, tools.session_id, q['question_id'], q['messages'][-1]['message_id'])
    assert decide(tools.root, task, unit) is (decision == 'yes')
    assert not decide(tools.root, task, unit)
    user_reply(host, 'yes')
    assert not decide(tools.root, task, unit)


@pytest.mark.parametrize('damage', ['scope', 'boundary', 'prefix', 'status', 'malformed'])
def test_stale_or_malformed_retry_custody_fails_closed(host, damage):
    from compute_plane.native_journal import save_json
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    from relay import consent_decisions as consent
    path = tools.root/consent.FILE
    value = json.loads(path.read_text())
    question = value['questions'][0]
    if damage == 'scope': question['scope']['task'] = 'stale'
    elif damage == 'boundary': question['boundary'] = True
    elif damage == 'prefix': question['prefix'] = 'wrong'
    elif damage == 'status': question['status'] = 'ANSWERED'
    if damage == 'malformed':
        path.write_text('{')
        with pytest.raises(ValueError): decide(tools.root, task, unit)
        return
    save_json(path, value)
    user_reply(host, 'yes')
    assert not decide(tools.root, task, unit)


def test_explicit_retry_settles_pending_question_without_extra_allowance(host):
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    user_reply(host, 'yes')
    assert not decide(tools.root, task, unit, explicit=True)
    assert not decide(tools.root, task, unit)


def test_competing_retry_claims_release_exactly_one_attempt(host):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    user_reply(host, 'sim')
    from relay import consent_decisions as consent
    q = consent.pending(tools.root, tools.session_id)[0]
    consent.record(tools.root, tools.session_id, q['question_id'], choice='allow')
    barrier = Barrier(2)
    def claim(_):
        barrier.wait()
        return decide(tools.root, task, unit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]
    assert not decide(tools.root, task, unit)


def test_synthetic_input_id_and_old_yes_cannot_answer_retry_question(host, monkeypatch):
    from test_consent_decisions import verdict
    monkeypatch.setattr('relay.turn_engine.judges', lambda packet, **k:
        {'jev': verdict('deny')} if set(packet['options']) == {'allow', 'deny', 'unclear'} else {})
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    user_reply(host, 'yes')
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    tools.__dict__['_host_input_id'] = 'host-yes'
    assert not decide(tools.root, task, unit)
    user_reply(host, 'no')
    assert not decide(tools.root, task, unit)
    user_reply(host, 'yes')
    assert not decide(tools.root, task, unit)


def test_a_host_observation_is_not_an_answer_and_a_yes_after_it_still_counts(host, monkeypatch):
    monkeypatch.setattr('relay.consent_decisions.select_rule', lambda: 'fallback')
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {})
    from relay.native_refresh import FOLLOW_UP, OBSERVED
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    user_reply(host, OBSERVED + ' These are observations. {"source_changes": []}')  # the host alone
    assert not decide(tools.root, task, unit)  # not answered yet
    user_reply(host, OBSERVED + ' These are observations. {"source_changes": []}' + FOLLOW_UP + 'sim')
    from relay import consent_decisions as consent
    q = consent.pending(tools.root, tools.session_id)[0]
    consent.record(tools.root, tools.session_id, q['question_id'], q['messages'][-1]['message_id'])
    assert decide(tools.root, task, unit)  # only the cited user's own sim counts
    assert not decide(tools.root, task, unit)


@pytest.mark.parametrize('words', ['sim', 'não', 'pode ler, claro'])
def test_retry_fallback_contract_is_preserved_and_final(host, monkeypatch, words):
    from relay import consent_decisions as consent
    from test_conversation_tasks import user_reply
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    monkeypatch.setattr(consent, 'select_rule', lambda: 'fallback')
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    original = consent.load(tools.root)['questions'][-1]
    assert original['rule'] == 'fallback'
    assert consent.guidance('fallback') in original['question']
    monkeypatch.setattr(consent, 'select_rule', lambda: 'judge')
    decide(tools.root, task, unit, register=True)
    assert consent.load(tools.root)['questions'][-1] == original
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: pytest.fail('fallback consulted judge'))
    user_reply(host, words)
    assert decide(tools.root, task, unit) == (words == 'sim')
    assert not decide(tools.root, task, unit)
    user_reply(host, 'sim')
    assert not decide(tools.root, task, unit)
    q = consent.load(tools.root)['questions'][-1]
    assert q['status'] == ('CONSUMED' if words == 'sim' else 'DECLINED')


@pytest.mark.parametrize('approved', [False, True])
def test_retry_revision_preserves_question_and_single_allowance(host, approved):
    from copy import deepcopy
    from relay import consent_decisions as consent
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    original = deepcopy(consent.load(tools.root)['questions'][0])
    expected = {'task': task['id'], 'repository': unit['repository'], 'key': unit['key']}
    assert original['scope'] == expected
    if approved:
        consent.record(tools.root, tools.session_id, original['id'], choice='allow')
    task['plan']['sha256'] = 'revised-plan'
    unit.update(target_paths=['new.py'], verification_contract_digest='revised-contract')
    if not approved:
        decide(tools.root, task, unit, register=True)
        pending = consent.load(tools.root)['questions']
        assert len(pending) == 1
        assert pending[0]['id'] == original['id']
        assert pending[0]['boundary'] == original['boundary']
        assert pending[0]['scope'] == expected
        request = json.loads((tools.root/'conversation-retry/request.json').read_text())
        assert request['question_id'] == original['id'] and request['scope'] == expected
        consent.record(tools.root, tools.session_id, original['id'], choice='allow')
    assert decide(tools.root, task, unit)
    assert not decide(tools.root, task, unit)
    decide(tools.root, task, unit, register=True)
    request = json.loads((tools.root/'conversation-retry/request.json').read_text())
    assert request['question_id'] != original['id'] and request['scope'] == expected
    assert not decide(tools.root, task, unit)


@pytest.mark.parametrize('field', ['task', 'repository', 'key'])
def test_retry_identity_mismatch_neither_authorizes_nor_consumes(host, field):
    from copy import deepcopy
    from relay import consent_decisions as consent
    tools = host[0]
    intake(host); task = planned(host); unit = task['plan']['unit']
    decide = conversation_recheck.retry_decision
    decide(tools.root, task, unit, register=True)
    original = consent.load(tools.root)['questions'][0]
    consent.record(tools.root, tools.session_id, original['id'], choice='allow')
    other_task, other_unit = deepcopy(task), deepcopy(unit)
    if field == 'task': other_task['id'] += '-other'
    else: other_unit[field] += '-other'
    assert not decide(tools.root, other_task, other_unit)
    assert next(q for q in consent.load(tools.root)['questions'] if q['id'] == original['id'])['status'] == 'APPROVED'
    decide(tools.root, other_task, other_unit, register=True)
    assert not decide(tools.root, other_task, other_unit)
    assert decide(tools.root, task, unit)
    assert not decide(tools.root, task, unit)

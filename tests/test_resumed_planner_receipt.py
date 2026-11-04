"""Explicit read-only continuation keeps its original receipt and real accounting."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from compute_plane.native_journal import NativeJournal,save_json
from relay import planning_tools as plans,conversation_tasks as tasks
from relay.native_artifacts import KINDS
from test_conversation_tasks import host,intake  # noqa: F401
from test_planning_tools import planner,stub_child,execution,request,wrap  # noqa: F401


@pytest.fixture
def recovered(host):
    tools,_,source,_=host
    intake(host)
    current=tasks.current(tasks.load(tools.root))
    session=tools.root/'planning/first/session';(session/'artifacts').mkdir(parents=True)
    source_hash=hashlib.sha256((source/'a.py').read_bytes()).hexdigest()
    payload={'summary':'Change a.py.','criteria':['value equals two'],'steps':['Set two.'],
        'remaining':[],'sources':[{'repository':'repo','path':'a.py','sha256':source_hash}],
        'tasks':[{'key':'change','title':'Set two','summary':'Set two.','cluster':'test','priority':'P2',
            'target_paths':['a.py'],'criteria':['value equals two'],'depends_on':[],'test_paths':[],'verifiers':[]}]}
    state={'session_id':'planner','deployment':{'id':'chosen'},'budget':{'max_model_calls':2},
        'config':{'role':'planner','writable':{},'verifiers':{},'capabilities':['read','list','search']},
        'invocations':[{'id':'old','status':'RECONCILIATION_REQUIRED','ended_at':1},
            {'id':'continued','status':'PROPOSED','ended_at':2,'journal_start':2}]}
    artifact={'schema':'tare.tools/native-artifact/1','authority':'NONE','role':'planner','kind':KINDS['planner'],
        'session_id':'planner','profile_digest':'a'*64,'call_id':'planner/model/2','payload':payload}
    save_json(session/'native-agent.json',state)
    save_json(session/'artifact.json',artifact);save_json(session/'artifacts/final.json',artifact)
    save_json(session/'outcome.json',{'status':'PROPOSED','invocation_id':'continued','session_id':'planner',
        'profile_digest':'a'*64,'artifact_file':str(session/'artifacts/final.json')})
    journal=NativeJournal(session/'journal.jsonl')
    for event in [{'kind':'model_requested','call_id':'planner/model/1'},
            {'kind':'invocation_interrupted','invocation_id':'planner/model/1'},
            {'kind':'model_abandoned','call_id':'planner/model/1','resolution_id':'continued','usage':'unknown','remote_stop':'unknown'},
            {'kind':'model_requested','call_id':'planner/model/2'},
            {'kind':'model_returned','call_id':'planner/model/2','reply':{'metadata_json':json.dumps({'delivery_payload':payload})}}]:
        journal.append(event)
    row={'status':'FINISHED','route':'chosen','calls_reserved':2,'calls_used':1,'amount_committed':'0',
        'demand_id':current['id'],'planning_revision':0,
        'result':{'status':'RECONCILIATION_REQUIRED','reason':'UNFINISHED_INVOCATION','session':str(session)}}
    receipt=session.parent/'receipt.json';save_json(receipt,row)
    return tools,source,session,receipt,row


def test_projection_counts_the_resume_without_rewriting_failure_or_unknown_usage(recovered):
    tools,_,session,receipt,original=recovered
    before=receipt.read_bytes();history=(session/'journal.jsonl').read_bytes()
    row=plans.records(tools.root)[0]
    assert row['result']['status']=='PLANNED' and row['calls_used']==2 and row['calls_reserved']==2
    assert row['explicit_resume']['prior_result']==original['result']
    assert row['explicit_resume']['source_receipt_sha256']==hashlib.sha256(before).hexdigest()
    assert receipt.read_bytes()==before and (session/'journal.jsonl').read_bytes()==history


@pytest.mark.parametrize('changed',[False,True])
def test_interrupted_continuation_projection_counts_the_original_child_once(recovered,changed):
    tools,_,_,original,_=recovered
    alias=tools.root/'planning/attempt/receipt.json';alias.parent.mkdir()
    row={'status':'RUNNING','calls_reserved':0,'calls_used':0,'continuation_of':str(original),
        'source_receipt_sha256':('b'*64 if changed else hashlib.sha256(original.read_bytes()).hexdigest())}
    save_json(alias,row);before=alias.read_bytes()
    result=plans.record(alias)
    assert result['status']==('RUNNING' if changed else 'FINISHED')
    assert sum(r['calls_used'] for r in plans.records(tools.root))==2
    assert alias.read_bytes()==before


@pytest.mark.parametrize('changed',[False,True])
def test_owner_rebinds_only_the_completed_same_scope_plan_with_current_sources(recovered,changed):
    tools,source,_,receipt,_=recovered
    if changed:(source/'a.py').write_text('value = 99\n')
    before=receipt.read_bytes();value=tasks.load(tools.root)
    assert tasks.recover_completed_plan(tools.root,tools.state['config'],value) is (not changed)
    task=tasks.current(tasks.load(tools.root))
    assert bool(task.get('plan')) is (not changed)
    if not changed:
        assert task['plan']['unit']['target_paths']==['a.py']
        assert task['planning_resume']['prior_result']['status']=='RECONCILIATION_REQUIRED'
    assert receipt.read_bytes()==before


@pytest.mark.parametrize('damage',['unfinished','failed','writable','profile','route','allowance','immutable','no_ack','payload','outcome_missing'])
def test_projection_refuses_unproven_or_expanded_recovery(recovered,damage):
    _,_,session,receipt,original=recovered
    state=json.loads((session/'native-agent.json').read_text())
    if damage=='unfinished':state['invocations'][-1]['status']='PREPARING'
    if damage=='failed':state['invocations'][-1]['status']='BLOCKED'
    if damage=='writable':state['config']['writable']={'repo':['a.py']}
    if damage=='route':state['deployment']['id']='other'
    if damage=='allowance':state['budget']['max_model_calls']=1
    if damage=='no_ack':state['invocations'][-1]['journal_start']=3
    save_json(session/'native-agent.json',state)
    if damage=='profile':
        o=json.loads((session/'outcome.json').read_text());o['profile_digest']='b'*64;save_json(session/'outcome.json',o)
    if damage in {'immutable','payload'}:
        a=json.loads((session/'artifact.json').read_text());a['payload']['summary']='Changed after delivery'
        save_json(session/'artifact.json',a)
        if damage=='payload':save_json(session/'artifacts/final.json',a)
    if damage=='outcome_missing':(session/'outcome.json').unlink()
    before=receipt.read_bytes()
    assert plans.record(receipt)=={**original,'calls_used':2}
    assert receipt.read_bytes()==before


def test_call_accounting_never_reduces_existing_commitment(recovered):
    _,_,_,receipt,original=recovered
    original['calls_used']=3
    save_json(receipt,original)
    row=plans.record(receipt)
    assert row['calls_used']==3
    assert row['result']==original['result'] and 'explicit_resume' not in row


@pytest.mark.parametrize('damage', [None, 'unknown', 'authority', 'no_message', 'no_response'])
def test_completed_delivery_recovery_keeps_original_receipt_and_usage(recovered, damage):
    from relay.native_model_recovery import completed_delivery_refusal
    _, _, session, receipt, original = recovered
    events = [r.event() for r in NativeJournal(session/'journal.jsonl').snapshot().records]
    prior = [events[0], {'kind': 'model_failed', 'code': 'INVALID_DELIVERY', 'call_id': 'planner/model/1', 'retryable': False}]
    observation = {'purpose': 'model', 'call_id': 'planner/model/1', 'status': 'invalid_delivery',
        'error': 'INVALID_DELIVERY', 'response_sha256': 'a'*64,
        'delivery_validation': {'issues': [{'code': 'native_artifact_ignored_target'}]},
        'transport_calls': [{'cleanup_verified': True, 'response_received': True}]}
    if damage == 'unknown': observation['status'] = 'interrupted'
    if damage == 'authority': observation['delivery_validation']['issues'][0]['code'] = 'native_artifact_authority'
    if damage == 'no_response': observation.pop('response_sha256')
    assert bool(completed_delivery_refusal(prior, [observation], 'planner')) is (damage not in {'unknown', 'authority', 'no_response'})
    (session/'journal.jsonl').unlink()
    ack = [] if damage == 'no_message' else [{'kind': 'user_message', 'message_id': 'repair', 'text': 'Continue.', 'interrupts_turn': True}]
    for event in prior + ack + events[3:]: NativeJournal(session/'journal.jsonl').append(event)
    NativeJournal(session/'observations.jsonl').append(observation)
    state = json.loads((session/'native-agent.json').read_text())
    state['invocations'][0]['status'] = 'BLOCKED'
    save_json(session/'native-agent.json', state)
    original['result'].update(status='BLOCKED', reason='MODEL_FAILURE', failure_code='INVALID_DELIVERY')
    save_json(receipt, original)
    before = receipt.read_bytes()
    assert (plans.record(receipt)['result']['status'] == 'PLANNED') is (damage is None)
    assert receipt.read_bytes() == before


@pytest.mark.parametrize('proof', ['not_started', 'unknown_dispatch', 'different_error', 'missing_request', 'no_ack'])
def test_preflight_resume_requires_proven_no_dispatch_and_retains_old_receipt(recovered, proof):
    _, _, session, receipt, original = recovered
    events = [r.event() for r in NativeJournal(session/'journal.jsonl').snapshot().records]
    prior = [{'kind': 'model_requested', 'call_id': 'planner/model/1'},
             {'kind': 'model_failed', 'call_id': 'planner/model/1',
              'code': 'CLI_VERSION_UNAVAILABLE', 'not_started': proof != 'unknown_dispatch', 'retryable': False}]
    if proof == 'different_error': prior[-1]['code'] = 'OTHER_ERROR'
    if proof == 'missing_request': prior = prior[1:]
    (session/'journal.jsonl').unlink()
    journal = NativeJournal(session/'journal.jsonl')
    ack = [] if proof == 'no_ack' else [{'kind': 'model_retry_authorized',
           'call_id': 'planner/model/1', 'resolution_id': 'continued'}]
    for event in prior + ack + events[3:]: journal.append(event)
    state = json.loads((session/'native-agent.json').read_text())
    state['invocations'][0]['status'] = 'BLOCKED'
    state['invocations'][-1]['journal_start'] = len(prior)
    save_json(session/'native-agent.json', state)
    original['result'].update(status='BLOCKED', reason='CLI_VERSION_UNAVAILABLE')
    save_json(receipt, original)
    before = receipt.read_bytes(); history = (session/'journal.jsonl').read_bytes()
    row = plans.record(receipt)
    assert (row['result']['status'] == 'PLANNED') is (proof == 'not_started')
    if proof == 'not_started':
        assert row['calls_used'] == 2
        assert row['explicit_resume']['prior_result'] == original['result']
    assert receipt.read_bytes() == before and (session/'journal.jsonl').read_bytes() == history


@pytest.mark.parametrize('remaining,expected',[(1000,600),(80,75)])
def test_new_planning_request_uses_its_bounded_phase_not_the_short_profile_timeout(planner,monkeypatch,remaining,expected):
    parent,state,candidate=planner
    candidate['native_execution_limits']={'request_seconds':120}
    before=deepcopy(candidate);calls=[];stub_child(monkeypatch,candidate,calls)
    control=execution();monkeypatch.setattr(type(control),'remaining_seconds',lambda self:remaining)
    result=wrap(parent,state,control).execute(request(),control)
    assert result.status=='succeeded',result.output_json
    limits=calls[0][4]['limits']
    assert limits['request_seconds']==limits['max_seconds']==expected
    assert candidate==before

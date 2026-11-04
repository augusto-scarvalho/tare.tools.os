"""A large frozen CLI adapter belongs to custody; an interrupted record write cannot erase its rejected candidate."""
from copy import deepcopy
import hashlib
import json

import pytest

from relay import conversation_tasks as tasks, work_units
from test_conversation_tasks import host, intake, planned  # noqa: F401
from test_native_agent import deployment


def test_graph_authoring_contract_repairs_stale_metadata_without_weakening_dod():
    from graph_backlog.validation import validate_work_graph
    contract=tasks.backlog_authoring_contract()
    graph=deepcopy(contract['example'])
    graph['nodes'][0]['title']='A real changed title'
    graph.pop('revision',None)
    assert validate_work_graph(graph)['status']=='PASS'
    assert 'optional' in contract['derived_revision'] and 'remove a stale revision' in contract['derived_revision']
    assert 'DONE requires completion.dod_satisfied=true' in contract['completion_rules']
    graph['nodes'][0]['completion'].update(status='DONE',dod_satisfied=False)
    assert 'DONE_DOD' in {e['code'] for e in validate_work_graph(graph)['errors']}


@pytest.mark.parametrize('stop',['error','length','private unknown stop'])
def test_stopped_implementer_exposes_only_known_provider_stop_without_transcript(tmp_path,stop):
    from compute_plane.native_journal import NativeJournal
    journal=NativeJournal(tmp_path/'journal.jsonl')
    journal.append({'kind':'model_returned','call_id':'s/model/1','reply':{
        'text':'private provider text','complete':False,'proposes_delivery':False,'tool_calls':[],
        'metadata_json':json.dumps({'provider_stop_reason':stop,'assistant':{'reasoning':'private reasoning'}})}})
    feedback=work_units.interruption_feedback(tmp_path,{'child_status':'BLOCKED','child_reason':'PARTIAL_REPLY','model_calls':1})
    if stop in {'error','length'}:assert feedback['provider_stop_reason']==stop
    else:assert 'provider_stop_reason' not in feedback
    assert 'private' not in json.dumps(feedback)
    result=tasks.delivery_view({'verdict':'INCONCLUSIVE','files':[],
        'reason':'the implementer changed nothing','implementation_stop':feedback})
    if stop=='error':
        assert result['reason']=='The inference provider ended the implementer response with an error; no files changed.'
        assert 'Do not repeat' in result['next'] and 'same plan' in result['next']


def test_bounded_check_output_keeps_first_diagnostic_and_final_summary(tmp_path):
    from types import SimpleNamespace
    output='FIRST concrete failure\n'+'x'*5000+'\nFINAL 30 failed'
    token=work_units.CHECK_PORT.set(lambda *a,**kw:SimpleNamespace(returncode=1,stdout=output,stderr=''))
    try:
        status,text=work_units.run_checks(tmp_path,{'focused':['fixture-check']})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert status=='red' and 'FIRST concrete failure' in text and 'FINAL 30 failed' in text
    assert len(text.encode())<1600 and 'omitted' in text


def test_producer_reference_preserves_the_exact_large_adapter_and_detects_tampering(tmp_path):
    folder=tmp_path/'units'/'job';folder.mkdir(parents=True)
    profile={**deployment(),'native_cli':{'adapter':'x'*70000}}
    reference=work_units.producer_reference(folder,profile)
    assert len(json.dumps(reference))<500 and work_units.candidate_producer(tmp_path,reference)==profile
    assert work_units.producer_reference(folder,profile)==reference
    (folder/'producer.json').write_text('{}')
    with pytest.raises(ValueError,match='snapshot changed'):
        work_units.candidate_producer(tmp_path,reference)
    with pytest.raises(ValueError,match='outside'):
        work_units.candidate_producer(tmp_path/'other',reference)


@pytest.mark.parametrize('case',['legacy','bound','foreign','duplicate','tampered','applied','cancelled'])
def test_completed_unapplied_receipt_recovery_never_repeats_or_infers_application(host,case):
    intake(host);planned(host)
    tools,*_=host;value=tasks.load(tools.root);task=tasks.current(value)
    original=deepcopy(task)
    task.update(stage='AUDITING',attempt_invocation='finished')
    if case!='legacy':task['attempt_effect']='the-effect'
    state={**tools.state,'invocations':[{'id':'finished','started_at':90,'ended_at':120}]}
    folder=tools.root/'units'/(task['id']+'-fixture');folder.mkdir(parents=True)
    patch=folder/'patch.diff';patch.write_bytes(b'retained unapplied patch')
    targets={'repo':['a.py']}
    result={'verdict':'NOT_REVIEWED','reason':'Independent audit requested corrections.',
            'files':['a.py'],'audit':{'verdict':'REVISE','findings':[{'claim':'Keep the actual obligation.'}]},
            'candidate':{'patch':str(patch),'sha256':hashlib.sha256(patch.read_bytes()).hexdigest(),
                'targets':targets,'basis':tasks.source_state(state['config'],targets),
                'producer':{**deployment(),'native_cli':{'adapter':'x'*70000}}}}
    if case=='applied':result['applied']=True
    receipt={'unit':task['plan']['unit'],'started':'1970-01-01T00:01:40Z','finished':'1970-01-01T00:01:50Z',
             'effect_id':'another-effect' if case=='foreign' else 'the-effect','result':result}
    if case=='cancelled':receipt['effect_id']='prior-effect'
    (folder/'receipt.json').write_text(json.dumps(receipt),encoding='utf8')
    if case=='cancelled':
        from compute_plane.native_journal import NativeJournal
        journal=NativeJournal(tools.root/'journal.jsonl')
        journal.append({'kind':'effect_returned','result':{'effect_id':'prior-effect','status':'succeeded',
            'output_json':json.dumps({'candidate_preserved':True,'verdict':'NOT_REVIEWED'}),'progressed':True}})
        journal.append({'kind':'effect_requested','effect_id':'the-effect',
            'tool_call':{'name':'implement','call_id':'call','arguments_json':'{}'}})
        cancelled=tools.root/'units'/(task['id']+'-cancelled');cancelled.mkdir()
        latest={'verdict':'INCONCLUSIVE','reason':'cancelled before worker start','receipt':str(cancelled/'receipt.json')}
        (cancelled/'receipt.json').write_text(json.dumps({'unit':task['plan']['unit'],
            'effect_id':'the-effect','result':latest,'started':receipt['started'],'finished':receipt['finished']}))
        task.update(stage='PENDING',delivery=latest)
    if case=='tampered':patch.write_bytes(b'changed after the receipt')
    if case=='duplicate':
        duplicate=tools.root/'units'/(task['id']+'-second');duplicate.mkdir()
        (duplicate/'receipt.json').write_text(json.dumps(receipt),encoding='utf8')
    tasks.save(tools.root,value)
    recovered=tasks.recover_unapplied_delivery(tools.root,state,value)
    saved=tasks.current(tasks.load(tools.root))
    if case in {'legacy','bound','cancelled'}:
        assert recovered and saved['stage']=='PENDING' and saved['delivery']['audit']['verdict']=='REVISE'
        assert saved['delivery']['candidate']['producer']['artifact'] and not saved['delivery'].get('applied')
        assert (tools.root/tasks.FILE).stat().st_size<tasks.MAX_BYTES
        assert saved['objective']==original['objective'] and saved['criteria']==original['criteria']
        assert saved['plan']==original['plan']
        if case=='cancelled':assert saved['delivery']['last_attempt']['reason']=='cancelled before worker start'
    else:
        assert not recovered and saved['stage']=='AUDITING'
    assert tools.state['config']['repositories'] and not list(tools.root.glob('units/*/session-*'))


def test_cancelled_preflight_preserves_the_previous_candidate_audit_and_feedback(host,monkeypatch):
    from test_conversation_tasks import effect
    intake(host);planned(host)
    tools,execution,*_=host
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    receipt=folder/'receipt.json'
    audit={'verdict':'REVISE','summary':'A real obligation remains.','findings':[],'remaining':['Reconcile actual data.']}
    receipt.write_text(json.dumps({'result':{'audit':audit}}))
    previous={'verdict':'NOT_REVIEWED','candidate':{'patch':'retained'},'receipt':str(receipt),'audit':audit}
    value=tasks.load(tools.root);tasks.current(value)['delivery']=previous;tasks.save(tools.root,value)
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:{'verdict':'INCONCLUSIVE','reason':'cancelled during baseline',
        'receipt':str(tools.root/'units/new/receipt.json')})
    result=tools.execute(effect('implement',{}),execution)
    assert result.status=='succeeded' and json.loads(result.output_json)['candidate_preserved']
    saved=tasks.current(tasks.load(tools.root))['delivery']
    assert saved['candidate']==previous['candidate'] and saved['audit']==previous['audit']
    assert saved['receipt']==str(receipt) and saved['last_attempt']['reason']=='cancelled during baseline'


def test_exhausted_worker_feedback_keeps_the_observed_error_without_provider_reasoning(tmp_path):
    from compute_plane.native_journal import NativeJournal
    journal=NativeJournal(tmp_path/'journal.jsonl')
    journal.append({'kind':'effect_requested','effect_id':'failed','tool_call':{
        'name':'command','call_id':'call','arguments_json':'{"private_argument":"not copied"}'}})
    journal.append({'kind':'effect_returned','result':{'effect_id':'failed','status':'failed',
        'progressed':False,'output_json':json.dumps({'output':('x'*3000)+'specific observed failure'})}})
    (tmp_path/'outcome.json').write_text(json.dumps({'reason':'DEADLINE','output':'unverified model claim not copied'}))
    feedback=work_units.interruption_feedback(tmp_path,{'child_status':'EXHAUSTED','model_calls':17})
    assert feedback['reason']=='DEADLINE' and feedback['last_tool_failure']['tool']=='command'
    assert feedback['last_tool_failure']['truncated'] and feedback['last_tool_failure']['detail'].endswith('specific observed failure')
    assert len(feedback['last_tool_failure']['detail'])==2048
    assert 'not copied' not in json.dumps(feedback) and 'model claim' not in json.dumps(feedback)

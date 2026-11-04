"""Continue a proven read-only interruption without a new budget or lost source history."""
import hashlib
import json
from pathlib import Path

import pytest
from compute_plane.native_journal import NativeJournal
from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
from relay import conversation_review as review, native_agent, native_installation
import test_conversation_review_recovery as fixtures
from test_conversation_review_recovery import reviewed  # noqa: F401
from test_conversation_tasks import host, user_reply  # noqa: F401
from test_native_agent import Client, deployment, setup  # noqa: F401


@pytest.mark.parametrize('changed,budget,stall', [(False,30,False),(True,30,False),(False,3,False),(False,30,True)])
def test_same_auditor_keeps_observations_unknown_call_and_original_budget(reviewed,monkeypatch,changed,budget,stall):
    owner_host,run,*_=reviewed
    owner,control,tree,*_=owner_host
    clients=[]
    if stall:
        from relay import execution_resources, turn_engine
        dep=deployment();dep['native_profile'].update(protocol='vendor-cli',endpoint='cli://agy',
            parameters_json='{}',context_window=200000,output_tokens=16384)
        dep['native_counter_protocol']='utf8-envelope-estimate-v1'
        dep.pop('native_counter_endpoint',None)
        dep.pop('native_usage_contract',None)
        dep['native_cli']={'executable':str(tree/'agy.exe'),'version':'1.4.12'}
        monkeypatch.setattr(turn_engine,'pick_auditors',lambda *a,**kw:[('independent',dep)])
        monkeypatch.setattr(execution_resources,'child_calls',lambda *a,**kw:3)
    class Interrupted(Client,NativeHttpClient):
        def __init__(self,**kwargs):
            super().__init__(**kwargs);self.interrupted=False;clients.append(self)
            if stall:self.endpoint='cli://agy'

        def post_observed(self,path,body,execution):
            if (stall or path.endswith('/chat/completions')) and self.generations==2 and not self.interrupted:
                self.interrupted=True
                diagnostics=({'vendor':'agy','timeout':True,'cancelled':False,'output_limit':False,
                    'incomplete_stream_lines':0,'hook_phases':['pre','tool','post','stop'],
                    'step_tool_names':['finish'],'stream_event_types':['result'],'inference_invocations_observed':1} if stall else {})
                raise NativeTransportInterrupted('CLI_INCOMPLETE_EXCHANGE' if stall else 'HTTP_INCOMPLETE_STREAM',{
                    'started':True,'cleanup_verified':True,'remote_stop':'unknown','http_attempts_upper_bound':1,**diagnostics})
            return self.post(path,body,execution),{'started':True,'cleanup_verified':True}

    monkeypatch.setattr(fixtures,'Client',Interrupted)
    run.task['attempt_input']='prior-worker-input'
    user_reply(owner_host,'Review this retained candidate without another worker.')
    first=run('ACCEPT')
    assert first['reason']=='UNFINISHED_INVOCATION'
    job=Path(first['session']).parent
    before=(job/'receipt.json').read_bytes()
    state=json.loads((job/'session/native-agent.json').read_text())
    original_budget=state['budget']
    assert original_budget['max_model_calls']==(3 if stall else 8)
    assert review.resumable_review_state(job,json.loads(before)) is not None
    run.task['delivery']={'audit':first}
    run.task['attempt_input']=owner.input_id()
    user_reply(owner_host,'Continue the saved audit only.')
    owner.invocation_id='explicit-audit-resume'
    owner.state['limits']['max_model_calls']=budget
    if changed:(tree/'a.py').write_bytes(b'value = 3\n')
    # The fixture replaces run_session; use the original function captured by this module.
    calls=[]
    def resume(config,dep,session,**kwargs):
        calls.append((config,dep,session,kwargs))
        return RUN_SESSION(config,dep,session,client=clients[0],local_counter=clients[0],**kwargs)
    monkeypatch.setattr(native_agent,'run_session',resume)
    result=review.resume_review(owner,run.task,job,json.loads(before),tree,run.task['plan']['unit'],
                               {'a.py':b'value = 2\n'},control)
    after=json.loads((job/'receipt.json').read_text())
    if budget==3:
        assert not calls and after==json.loads(before)
        assert 'shared call budget' in result['reason']
        return
    assert calls[0][0:2]==(None,None)
    assert calls[0][3]['resume_interrupted_model'] is True
    backup=job/'continuations'/owner.invocation_id/'prior-receipt.json'
    assert backup.read_bytes()==before
    assert after['model_recovery']['prior_receipt_sha256']==hashlib.sha256(before).hexdigest()
    restored=json.loads((job/'session/native-agent.json').read_text())
    assert restored['session_id']==state['session_id'] and restored['budget']==original_budget
    events=[r.event() for r in NativeJournal(job/'session/journal.jsonl').snapshot().records]
    assert len([e for e in events if e['kind']=='effect_requested' and e['tool_call']['name']=='read'])==2
    if changed:
        assert result['verdict']=='INCONCLUSIVE' and clients[0].generations==2
    else:
        assert result['verdict']=='ACCEPT' and after['calls_used']==4
        if stall:
            assert after['calls_reserved']==4 and after['transport_completion_recovery']['call_ceiling']==4
            grants=[e for e in events if e['kind']=='user_message' and e.get('model_call_limit')]
            assert [e['model_call_limit'] for e in grants]==[4]
        abandoned=[e for e in events if e['kind']=='model_abandoned']
        assert len(abandoned)==1 and abandoned[0]['remote_stop']=='unknown' and abandoned[0]['usage'] is None
        assert review.resume_review(owner,run.task,job,after,tree,run.task['plan']['unit'],{},control) is None


RUN_SESSION=native_agent.run_session


@pytest.mark.parametrize('fault',[None,'cancelled','cleanup','truncated','tool','vendor','missing_stop'])
def test_stall_diagnostics_cannot_authorize_a_grant_after_an_unrelated_or_uncertain_failure(tmp_path,fault):
    from relay.native_model_recovery import agy_terminal_process_stall, model_call_ceiling
    from compute_plane.native_journal import NativeObservationJournal
    row={'vendor':'agy','timeout':True,'cancelled':False,'output_limit':False,'cleanup_verified':True,
        'incomplete_stream_lines':0,'hook_phases':['pre','tool','post','stop'],
        'step_tool_names':['finish'],'stream_event_types':['result'],'inference_invocations_observed':1}
    if fault=='cancelled':row['cancelled']=True
    if fault=='cleanup':row['cleanup_verified']=False
    if fault=='truncated':row['incomplete_stream_lines']=1
    if fault=='tool':row['step_tool_names']=['read']
    if fault=='vendor':row['vendor']='claude'
    if fault=='missing_stop':row['hook_phases'].pop()
    obs=NativeObservationJournal(tmp_path/'observations.jsonl')
    obs.append({'purpose':'model','error':'CLI_INCOMPLETE_EXCHANGE','transport_calls':[row]})
    assert agy_terminal_process_stall(obs) is (fault is None)
    journal=NativeJournal(tmp_path/'journal.jsonl')
    journal.append({'kind':'user_message','model_call_limit':10})
    journal.append({'kind':'user_message','model_call_limit':12})
    assert model_call_ceiling(journal,8)==12 and model_call_ceiling(journal,12)==12


@pytest.mark.parametrize('name,exists,changed', [('a1b2c3d4',False,False),('outside',False,False),
    ('a1b2c3d4',True,False),('a1b2c3d4',False,True)])
def test_reconstruction_uses_only_absent_owned_view_for_the_same_task(host,tmp_path,monkeypatch,name,exists,changed):
    owner=host[0]
    from test_conversation_tasks import intake,planned
    intake(host);task=planned(host)
    view=tmp_path/'wt'/name
    if exists:view.mkdir(parents=True)
    job=tmp_path/'review';(job/'input').mkdir(parents=True)
    (job/'input/request.json').write_text(json.dumps({'objective':task['objective'],
        'criteria':['different'] if changed else task['plan']['unit']['criteria']}))
    monkeypatch.setattr(review,'recorded_failure',lambda *_:(job,{'task':task['id']},{}))
    monkeypatch.setattr(review,'resumable_review_state',lambda *_:{'deployment':deployment(),
        'config':{'repositories':{'candidate':str(view)}}})
    monkeypatch.setattr(native_installation,'storage_root',lambda:tmp_path)
    assert review.resume_tree(owner,task)==(view if name=='a1b2c3d4' and not exists and not changed else None)

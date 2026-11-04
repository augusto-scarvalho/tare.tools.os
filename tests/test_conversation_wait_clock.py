"""Idle conversations do not spend active time or renew a worker's budget."""
from copy import deepcopy
import pytest
from relay.worker_wait import resume_clock
from relay.worker_wait import grant_conversation_time
from test_native_agent import setup,Client,deployment  # noqa: F401


def test_explicit_public_resume_grant_reaches_runtime_without_changing_original_identity(setup):
    import json
    from compute_plane.native_journal import NativeJournal
    from relay import native_agent
    config,custody,_=setup
    config['verifiers']={}
    client=Client(steps=[('tare_deliver',{'summary':'First answer.'})])
    first=native_agent.run_session(config,deployment(),custody,'Inspect only.',conversation=True,
        client=client,local_counter=client,limits={'max_model_calls':1})
    assert first['model_calls']==1
    initial=NativeJournal(custody/'journal.jsonl').snapshot().records[0].canonical_bytes
    client=Client(steps=[('tare_deliver',{'summary':'Continued answer.'})])
    result=native_agent.run_session(None,None,custody,resume=True,follow_up='Continue this conversation.',
        message_id='operator-grant',active_call_budget=3,client=client,local_counter=client)
    assert result['status']=='PROPOSED' and result['model_calls']==2
    records=NativeJournal(custody/'journal.jsonl').snapshot().records
    assert records[0].canonical_bytes==initial
    assert next(r.event() for r in records if r.event()['kind']=='user_message')['model_call_limit']==3
    state=json.loads((custody/'native-agent.json').read_text())
    assert state['budget']['max_model_calls']==1 and state['limits']['max_model_calls']==3


def conversation():
    return {'conversation_flow':True,'deadline_utc':1000.0,'budget':{'max_model_calls':30},
            'invocations':[{'started_at':100.0,'ended_at':350.0}]}


def test_completed_conversation_excludes_idle_without_renewing_active_allowance(tmp_path):
    state=conversation()
    resume_clock(tmp_path,state,'continue',1000.0,message_id='first')
    assert state['deadline_utc']==1650.0
    assert state['deadline_utc']-1000==900-(350-100)
    assert state['budget']=={'max_model_calls':30}
    frozen=deepcopy(state)
    resume_clock(tmp_path,state,'continue',1800.0,message_id='first')
    assert state==frozen


def test_legacy_conversation_recovers_prior_idle_gaps_only_once(tmp_path):
    state=conversation()
    state['invocations'].append({'started_at':900.0,'ended_at':1000.0})
    resume_clock(tmp_path,state,'continue',1300.0,message_id='second')
    assert state['deadline_utc']==1850.0
    state['invocations'].append({'started_at':1300.0,'ended_at':1400.0})
    resume_clock(tmp_path,state,'continue',2000.0,message_id='third')
    assert state['deadline_utc']==2450.0
    assert state['deadline_utc']-2000==450.0
    assert state['budget']=={'max_model_calls':30}


def test_actually_spent_active_budget_stays_exhausted(tmp_path):
    state=conversation();state['invocations'][0]['ended_at']=1000.0
    resume_clock(tmp_path,state,'continue',2000.0,message_id='fresh')
    assert state['deadline_utc']==2000.0


@pytest.mark.parametrize('fault',['unfinished','nan','overlap','backwards','leaf','no_message','no_followup'])
def test_uncertain_or_nonconversation_wait_cannot_grant_time(tmp_path,fault):
    state=conversation();instant=1000.0;followup='continue';identity='fresh'
    if fault=='unfinished':state['invocations'][0]['ended_at']=None
    elif fault=='nan':state['invocations'][0]['ended_at']=float('inf')
    elif fault=='overlap':state['invocations'].append({'started_at':200.0,'ended_at':400.0})
    elif fault=='backwards':instant=300.0
    elif fault=='leaf':state['delegated_leaf']=True
    elif fault=='no_message':identity=None
    elif fault=='no_followup':followup=None
    before=deepcopy(state)
    resume_clock(tmp_path,state,followup,instant,message_id=identity)
    assert state==before


def test_explicit_total_time_grant_preserves_overdraw_and_call_budget():
    state=conversation();state['limits']={'max_seconds':900}
    state['deadline_utc']=570
    grant_conversation_time(state,3600,'operator-message')
    assert state['deadline_utc']==3270
    assert state['budget']=={'max_model_calls':30}
    assert state['limits']=={'max_seconds':900}
    frozen=deepcopy(state)
    grant_conversation_time(state,3600,'operator-message')
    assert state==frozen
    with pytest.raises(ValueError):grant_conversation_time(state,1800,'reduce')


@pytest.mark.parametrize('fault',['leaf','ordinary','zero','negative','bool','unlimited','message'])
def test_time_grant_requires_explicit_finite_coordinator_input(fault):
    state=conversation();state['limits']={'max_seconds':900};total=3600;identity='operator'
    if fault=='leaf':state['delegated_leaf']=True
    elif fault=='ordinary':state['conversation_flow']=False
    elif fault=='zero':total=0
    elif fault=='negative':total=-1
    elif fault=='bool':total=True
    elif fault=='unlimited':state['limits']['max_seconds']=None;state['deadline_utc']=None
    elif fault=='message':identity=None
    frozen=deepcopy(state)
    with pytest.raises(ValueError):grant_conversation_time(state,total,identity)
    assert state==frozen


def test_console_time_grant_is_explicit_and_only_for_a_saved_assistant_followup(tmp_path):
    import json
    from relay.console_backend import Selection,command
    session=tmp_path/'session';session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'conversation_flow':True,
        'config':{'repositories':{'repo':str(tmp_path)}}}))
    selection=Selection(str(tmp_path),session=str(session),prompt='continue',resume_coordinator_seconds=3600)
    argv=command(selection,resume=True,assistant=True)
    assert argv[argv.index('--active-time-budget')+1]=='3600'
    assert '--max-model-calls' not in argv
    assert '--active-time-budget' not in command(selection,resume=True,assistant=False)


def test_operator_call_cap_keeps_history_and_spent_actor_records():
    from relay.worker_wait import grant_conversation_calls
    state=conversation();state['limits']={'max_seconds':900,'max_model_calls':30}
    original=deepcopy(state['invocations'])
    grant_conversation_calls(state,90,'operator')
    assert state['budget']['max_model_calls']==30
    assert state['limits']['max_model_calls']==90
    assert state['invocations']==original
    assert state['operator_call_budget']['original_limit']==30
    frozen=deepcopy(state)
    grant_conversation_calls(state,90,'operator')
    assert state==frozen


def test_shared_call_grant_preserves_the_real_runtime_session_identity(tmp_path):
    from dataclasses import asdict
    from compute_plane.native_journal import NativeJournal
    from tare_tools_agent_runtime import NativeBudget,NativeSession,BindingRef,CancellationToken,ExecutionControl
    from relay.worker_wait import grant_conversation_calls
    journal=NativeJournal(tmp_path/'journal.jsonl')
    budget=NativeBudget(max_model_calls=30)
    options=dict(session_id='same',instruction='Inspect facts.',capabilities=('read',),model=None,tools=None,journal=journal)
    cancel=CancellationToken();cancel.cancel()
    control=ExecutionControl(BindingRef('work','binding','owner',1,'opaque'),cancel,float('inf'))
    NativeSession(**options,budget=budget).run(control)
    identity=NativeSession(**options,budget=budget).binding_identity()
    before=(tmp_path/'journal.jsonl').read_bytes()
    state=conversation();state['budget']=asdict(budget);state['limits']={'max_model_calls':30,'max_seconds':900}
    grant_conversation_calls(state,90,'operator')
    assert NativeSession(**options,budget=NativeBudget(**state['budget'])).binding_identity()==identity
    # Repair the rejected pre-release host grant from the immutable journal;
    # increasing the shared pool must never relabel the tactical session.
    state['budget']['max_model_calls']=90
    grant_conversation_calls(state,90,'operator',native_budget=journal.snapshot().records[0].event()['budget'])
    assert state['budget']==asdict(budget) and state['limits']['max_model_calls']==90
    assert NativeSession(**options,budget=NativeBudget(**state['budget'])).binding_identity()==identity
    assert (tmp_path/'journal.jsonl').read_bytes()==before
    frozen=deepcopy(state)
    with pytest.raises(ValueError):grant_conversation_calls(state,30,'reset')
    assert state==frozen


@pytest.mark.parametrize('fault',[None,'source','message','original','total','leaf','ordinary','stagnation'])
def test_operator_pool_survives_serialized_host_resume_without_changing_tactical_identity(fault):
    import json
    from relay import native_limits
    from relay.worker_wait import grant_conversation_calls
    state=conversation()
    state.update(schema='tare.tools/native-agent/2',timeout=900,
                 budget=dict(native_limits.BUDGET_DEFAULTS,max_model_calls=30))
    state['limits']=native_limits.resolve({},dict(state['budget'],max_seconds=900))
    grant_conversation_calls(state,90,'operator')
    state=json.loads(json.dumps(state))  # Reopening goes through from_state before the next grant.
    if fault=='source':state['operator_call_budget']['source']='model'
    elif fault=='message':state['operator_call_budget']['message_id']=''
    elif fault=='original':state['operator_call_budget']['original_limit']=29
    elif fault=='total':state['operator_call_budget']['total_calls']=100
    elif fault=='leaf':state['delegated_leaf']=True
    elif fault=='ordinary':state['conversation_flow']=False
    elif fault=='stagnation':state['limits']['max_stagnant_turns']+=1
    if fault:
        with pytest.raises(ValueError,match='limits_identity_mismatch'):native_limits.from_state(state)
    else:
        assert native_limits.from_state(state)['max_model_calls']==90
        grant_conversation_calls(state,90,'another-message')
        assert state['budget']['max_model_calls']==30
        assert native_limits.from_state(state)['max_model_calls']==90


def test_public_tui_flags_reach_the_saved_conversation_without_calling_models(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from relay import native_console,native_installation
    seen=[]
    monkeypatch.setattr(native_console.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(native_console.sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(native_installation,'default_storage',lambda:tmp_path/'history')
    monkeypatch.setattr(native_console,'WorkConsole',lambda *args,**kwargs:
        seen.append(kwargs) or SimpleNamespace(run=lambda:None))
    assert native_console.main([str(tmp_path),'--resume-coordinator-seconds','3600',
                                '--resume-coordinator-calls','90'])==0
    assert seen[0]['resume_coordinator_seconds']==3600
    assert seen[0]['resume_coordinator_calls']==90

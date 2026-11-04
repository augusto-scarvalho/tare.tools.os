import json

import pytest
from tare_tools_agent_runtime import ConversationLog
from relay import conversation_recheck,conversation_tasks as tasks,work_units
from test_conversation_tasks import host,intake,planned  # noqa: F401
from test_native_agent import setup,Client,deployment  # noqa: F401


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

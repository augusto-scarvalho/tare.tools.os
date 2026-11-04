"""Read-only describes planner authority, not permission to omit a delivery plan."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import sys
import pytest
from compute_plane.native_journal import save_json
from relay import conversation_tasks, native_artifacts, planning_tools
from test_planning_tools import planner, wrap, request, execution, stub_child, PLAN
from test_conversation_tasks import host


def test_delivery_contract_overrides_an_exploration_only_coordinator_note(planner,monkeypatch):
    root,state,candidate=planner; state['conversation_flow']=True
    state['config']['delegated_verifiers']={'project-check':{'repository':'repo','argv':['python','-m','pytest']}}
    calls=[];stub_child(monkeypatch,candidate,calls)
    control=execution();tool=wrap(root,state,control)
    owner=conversation_tasks.ConversationTools(tool,root,state,'turn')
    owner.understand({'intent':'deliver','relation':'new','objective':'Fix the defect.',
        'criteria':['The corrected behavior is verified.']})
    result=tool.execute(request({'instruction':'Only explore; the implementation plan will be requested separately.'}),control)
    assert result.status=='succeeded'
    mission=json.loads(calls[0][3].rsplit('\n',1)[-1])
    assert mission['required_output']=='executable_tasks'
    assert 'cannot postpone' in mission['phase_authority']
    assert mission['host_planner_selection']['requested_reasoning']=='high'
    assert mission['host_planner_selection']['pro_model'] is False
    assert mission['objective']=='Fix the defect.'
    assert 'project-check' in mission['available_verifiers']
    assert not calls[0][0]['writable'] and not calls[0][0]['verifiers']


@pytest.fixture
def incomplete(tmp_path):
    session=tmp_path/'planning'/'request'/'session';session.mkdir(parents=True)
    artifact=session/'artifact.json'
    payload=deepcopy(PLAN)
    save_json(artifact,{'schema':'tare.tools/native-artifact/1','role':'planner','kind':native_artifacts.KINDS['planner'],
        'authority':'NONE','session_id':'planner','call_id':'planner/1','profile_digest':'a'*64,'payload':payload})
    receipt=session.parent/'receipt.json'
    row={'status':'FINISHED','demand_id':'same','result':{'status':'PLANNED','plan':payload,'artifact':str(artifact)}}
    save_json(receipt,row)
    return tmp_path,artifact,receipt,row


def test_completed_findings_remain_untrusted_input_for_the_failed_planning_slice(incomplete):
    root,artifact,_,_=incomplete
    prior=planning_tools.incomplete_input(root,{'id':'same'})
    assert prior['artifact']==str(artifact) and len(prior['sha256'])==64
    value=native_artifacts.read_input(artifact)
    assert value['authority']=='NONE' and not value['artifact']['payload']['tasks']


@pytest.mark.parametrize('fault',['other_demand','modified','outside','already_executable'])
def test_unrelated_or_changed_planner_output_is_not_reused(incomplete,fault,tmp_path):
    root,artifact,receipt,row=incomplete
    if fault=='other_demand':row['demand_id']='another'
    elif fault=='modified':
        value=json.loads(artifact.read_text());value['payload']['summary']='changed';save_json(artifact,value)
    elif fault=='outside':row['result']['artifact']=str(tmp_path/'elsewhere.json')
    elif fault=='already_executable':row['result']['plan']['tasks']=[{'unverified':'task'}]
    save_json(receipt,row)
    assert planning_tools.incomplete_input(root,{'id':'same'}) is None


def declared_plan(host):
    from test_conversation_tasks import intake,planned
    tools,*_=host;intake(host);task=planned(host)
    artifact=Path(task['plan']['artifact'])
    payload=json.loads(artifact.read_text())
    payload.update(schema='tare.tools/native-artifact/1',role='planner',kind=native_artifacts.KINDS['planner'],
        authority='NONE',session_id='planner',call_id='planner/1',profile_digest='a'*64)
    payload['payload']['tasks'][0]['verifiers']=['check']
    save_json(artifact,payload)
    check={'repository':task['plan']['unit']['repository'],'argv':[sys.executable,'-c','pass']}
    tools.state['config']['delegated_verifiers']={'check':check}
    tools.state['config']['verifiers']={}
    return tools,task,artifact,payload


def test_plan_validation_uses_declared_checks_without_granting_execution(host):
    tools,task,artifact,_=declared_plan(host)
    before=deepcopy(tools.state['config'])
    conversation_tasks.task_plan(tools.root,tools.state['config'],task,{'artifact':str(artifact)})
    assert task['plan']['unit']['verifiers']==['check']
    assert tools.state['config']==before and tools.state['config']['verifiers']=={}


def pending_completed(host):
    tools,task,artifact,envelope=declared_plan(host)
    value=conversation_tasks.load(tools.root);active=conversation_tasks.current(value)
    active.pop('plan');active['stage']='DISCOVERY'
    result={'status':'PLANNED','plan':envelope['payload'],'artifact':str(artifact)}
    save_json(artifact.parent.parent/'receipt.json',{'status':'FINISHED','demand_id':active['id'],
        'planning_revision':active.get('planning_revision',0),'result':result})
    return tools,value,artifact,envelope


def test_completed_valid_plan_is_rebound_without_another_planner_call(host):
    tools,value,artifact,_=pending_completed(host)
    active=conversation_tasks.current(value)
    assert conversation_tasks.recover_completed_plan(tools.root,tools.state['config'],value)
    assert active['stage']=='PLANNED' and active['plan']['artifact']==str(artifact)
    assert active['plan']['unit']['verifiers']==['check']


@pytest.mark.parametrize('tampered',[False,True])
def test_large_plan_reference_keeps_all_obligations_and_rejects_changed_artifact(host,tampered):
    tools,value,artifact,envelope=pending_completed(host)
    criteria=['Obrigação '+str(i)+': '+('preservar o contrato e a evidência. '*20) for i in range(32)]
    envelope['payload']['tasks'][0]['criteria']=criteria
    save_json(artifact,envelope)
    result=planning_tools.handoff(artifact,envelope['payload'])
    assert result['status']=='PLANNED' and 'plan' not in result
    assert len(json.dumps(result).encode())<4096
    receipt=artifact.parent.parent/'receipt.json';row=json.loads(receipt.read_text());row['result']=result
    save_json(receipt,row)
    if tampered:
        envelope['payload']['tasks'][0]['criteria'].pop();save_json(artifact,envelope)
    before=receipt.read_bytes()
    assert conversation_tasks.recover_completed_plan(tools.root,tools.state['config'],value) is (not tampered)
    assert receipt.read_bytes()==before
    if not tampered:
        assert set(criteria)<=set(conversation_tasks.current(value)['plan']['unit']['criteria'])


@pytest.mark.parametrize('tampered',[False,True])
def test_legacy_oversized_plan_recovery_requires_original_model_delivery(host,tampered):
    from compute_plane.native_journal import NativeJournal
    from compute_plane.native_models import encode
    tools,value,artifact,envelope=pending_completed(host)
    receipt=artifact.parent.parent/'receipt.json';row=json.loads(receipt.read_text())
    row['result']={'status':'HANDOFF_TOO_LARGE','artifact':str(artifact)};save_json(receipt,row)
    journal=NativeJournal(artifact.parent/'journal.jsonl')
    journal.append({'kind':'model_returned','call_id':envelope['call_id'],
        'reply':{'metadata_json':encode({'delivery_payload':envelope['payload']})}})
    if tampered:
        envelope['payload']['summary']='Unrecorded revision';save_json(artifact,envelope)
    before=journal.snapshot().revision
    assert conversation_tasks.recover_completed_plan(tools.root,tools.state['config'],value) is (not tampered)
    assert journal.snapshot().revision==before


@pytest.mark.parametrize('fault',['revision','demand','artifact_changed','source_changed','unknown_check','outside','investigation'])
def test_completed_plan_recovery_rejects_incompatible_evidence(host,fault):
    tools,value,artifact,envelope=pending_completed(host)
    receipt=artifact.parent.parent/'receipt.json';row=json.loads(receipt.read_text())
    if fault=='revision':row['planning_revision']+=1
    elif fault=='demand':row['demand_id']='unrelated'
    elif fault=='artifact_changed':envelope['payload']['summary']='modified'
    elif fault=='source_changed':
        repo,root=next(iter(tools.state['config']['repositories'].items()))
        source=Path(root)/'a.py'
        refs=[{'repository':repo,'path':'a.py','sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]
        envelope['payload']['sources']=refs;row['result']['plan']['sources']=refs
        source.write_text('user change')
    elif fault=='unknown_check':
        envelope['payload']['tasks'][0]['verifiers']=['not-configured']
        row['result']['plan']['tasks'][0]['verifiers']=['not-configured']
    elif fault=='outside':row['result']['artifact']=str(tools.root/'elsewhere.json')
    elif fault=='investigation':conversation_tasks.current(value)['intent']='investigate'
    save_json(artifact,envelope);save_json(receipt,row)
    assert not conversation_tasks.recover_completed_plan(tools.root,tools.state['config'],value)
    assert not conversation_tasks.current(value).get('plan')

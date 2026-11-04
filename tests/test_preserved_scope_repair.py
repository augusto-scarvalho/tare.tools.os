"""Preservation repairs stay bounded and do not authorize a wider executable scope."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay import conversation_tasks as tasks, work_units
from relay.candidate_checks import ScopeRepair
from test_conversation_tasks import user_reply, host, intake, planned, effect  # noqa: F401
from test_planning_tools import planner, wrap, stub_child, execution, request  # noqa: F401


def test_missing_retained_output_names_the_omission_without_mutating_scope():
    plan={'unit':{'target_paths':['a.py']}}
    previous={'files':['a.py','docs/kept.md','other.py'],'candidate':{'sha256':'a'*64}}
    before=deepcopy((plan,previous))
    with pytest.raises(ScopeRepair) as error:
        tasks.replanned_delivery({},plan,previous)
    assert error.value.missing_target_paths==['docs/kept.md','other.py']
    assert (plan,previous)==before


@pytest.mark.parametrize('slot', ['delivery', 'replan_candidate', 'prior_delivery'])
def test_real_implement_gate_allows_one_scope_repair_without_refunding_prior_calls(host,monkeypatch,slot):
    tools,control,_,seen=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    old_plan=deepcopy(task['plan']);criteria=deepcopy(task['criteria'])
    artifact=Path(old_plan['artifact']);original=artifact.read_bytes()
    candidate={'patch':'preserved.diff','sha256':'a'*64}
    task[slot]={'verdict':'INCONCLUSIVE','files':['a.py','docs/kept.md'],'candidate':candidate}
    prior={}
    for number in range(3):
        p=tools.root/'planning'/('old-'+str(number))/'receipt.json';p.parent.mkdir()
        p.write_text(json.dumps({'demand_id':task['id'],'planning_revision':0,
            'calls_used':2,'calls_reserved':2,'status':'FINISHED','amount_committed':'unknown'}))
        prior[p]=p.read_bytes()
    tasks.save(tools.root,value)
    config=deepcopy(tools.state['config']);limits=deepcopy(tools.state['limits'])
    original_save=tasks.save
    copies=[]

    def save_repair(root, value):
        current=tasks.current(value)
        if current.get('scope_repair_input') and current.get(slot):
            retained=current['replan_candidate']
            source=current[slot]
            assert retained==source
            if slot!='replan_candidate':
                assert retained is not source and retained['candidate'] is not source['candidate']
            copies.append(deepcopy(retained))
        return original_save(root,value)

    monkeypatch.setattr(tasks,'save',save_repair)
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:pytest.fail('No worker for omitted scope.'))
    monkeypatch.setattr(work_units,'check_failures',lambda *a,**kw:pytest.fail('No checks for omitted scope.'))
    result=tools.execute(effect('implement',{},'omitted-retained'),control)
    body=json.loads(result.output_json);saved=tasks.current(tasks.load(tools.root))
    assert result.status=='refused' and body['missing_target_paths']==['docs/kept.md']
    assert body['required_action']['tool']=='delegate_plan'
    assert saved['planning_revision']==1 and not saved.get('plan')
    assert saved['criteria']==criteria and saved['replan_candidate']['candidate']==candidate
    assert copies and copies[0]['candidate']==candidate
    assert saved['inherited_criteria']==old_plan['unit']['criteria']
    assert saved['replan_from']['artifact']==str(artifact)
    assert artifact.read_bytes()==original and all(p.read_bytes()==b for p,b in prior.items())
    assert tools.state['config']==config and tools.state['limits']==limits
    assert seen==[]
    value=tasks.load(tools.root);tasks.current(value)['plan']=old_plan;tasks.save(tools.root,value)
    repeated=tools.execute(effect('implement',{},'same-omission'),control)
    assert repeated.status=='refused' and 'do not repeat planning' in repeated.output_json
    assert tasks.current(tasks.load(tools.root))['planning_revision']==1


@pytest.mark.parametrize('applied',[False,True])
def test_planner_receives_unapplied_output_scope_but_not_stale_applied_history(planner,monkeypatch,applied):
    parent,state,candidate=planner;state['conversation_flow']=True
    calls=[];stub_child(monkeypatch,candidate,calls)
    control=execution();tool=wrap(parent,state,control)
    coordinator=tasks.ConversationTools(tool,parent,state,'turn')
    coordinator.understand({'intent':'deliver','relation':'new','objective':'Keep the patch and fix dispatch.',
        'criteria':['All preserved changes and the dispatch fix are verified.']})
    value=tasks.load(parent);task=tasks.current(value)
    previous={'files':['kept.py','docs/kept.md'],'candidate':{'sha256':'b'*64}}
    task['delivery']={**previous,'applied':applied}
    task['prior_delivery']=deepcopy(previous)
    tasks.save(parent,value)
    schema=tool.tool_specs_json
    result=tool.execute(request({'instruction':'Repair only dispatch; keep the candidate.'}),control)
    assert result.status=='succeeded',result.output_json
    mission=json.loads(calls[0][3].rsplit('\n',1)[-1])
    if applied:
        assert 'retained_candidate_outputs' not in mission
    else:
        assert mission['retained_candidate_outputs']['paths']==previous['files']
        assert mission['retained_candidate_outputs']['patch_sha256']=='b'*64
        assert 'not additional write authority' in mission['retained_candidate_outputs']['instruction']
    assert tool.tool_specs_json==schema


def test_preserved_protected_application_gate(tmp_path, monkeypatch):
    from test_conversation_tasks import test_real_candidate_is_verified_then_audited_before_application
    original = work_units.suite_allows

    def checked_suite(*args, **kwargs):
        allowed, reason = original(*args, **kwargs)
        assert allowed, reason
        return allowed, reason

    monkeypatch.setattr(work_units, 'suite_allows', checked_suite)
    test_real_candidate_is_verified_then_audited_before_application(tmp_path, monkeypatch, 'PROTECTED')


@pytest.mark.parametrize('slots,expected', [
    ({}, None),
    ({'prior_delivery':False}, 'prior_delivery'),
    ({'delivery':False,'prior_delivery':False}, 'delivery'),
    ({'replan_candidate':False,'delivery':False,'prior_delivery':False}, 'replan_candidate'),
    ({'delivery':True,'prior_delivery':False}, None),
    ({'replan_candidate':True,'delivery':False}, None),
    ({'replan_candidate':False,'delivery':True}, 'replan_candidate'),
    ({'prior_delivery':True}, None),
])
def test_planner_candidate_precedence_and_applied_history_barriers(planner,monkeypatch,slots,expected):
    parent,state,deployment=planner;state['conversation_flow']=True
    calls=[];stub_child(monkeypatch,deployment,calls)
    control=execution();tool=wrap(parent,state,control)
    owner=tasks.ConversationTools(tool,parent,state,'turn')
    owner.understand({'intent':'deliver','relation':'new','objective':'Preserve the current private work.',
        'criteria':['Do not resurrect applied history.']})
    value=tasks.load(parent);task=tasks.current(value)
    for number,(slot,applied) in enumerate(slots.items(),1):
        task[slot]={'files':[slot+'.py'],'candidate':{'sha256':str(number)*64},'applied':applied}
    before=deepcopy(task)
    tasks.save(parent,value)
    selected=tasks.retained_candidate(task)
    assert selected==(task[expected] if expected else None)
    assert task==before
    result=tool.execute(request(),control)
    assert result.status=='succeeded',result.output_json
    mission=json.loads(calls[0][3].rsplit('\n',1)[-1])
    if expected:
        hint=mission['retained_candidate_outputs']
        assert hint['paths']==task[expected]['files']
        assert hint['patch_sha256']==task[expected]['candidate']['sha256']
        assert 'not additional write authority' in hint['instruction']
    else:
        assert 'retained_candidate_outputs' not in mission
    assert tasks.current(tasks.load(parent))==before


def test_complete_retained_scope_keeps_existing_dependency_proofs_and_independent_copy(tmp_path):
    (tmp_path/'a.py').write_text('original\n')
    config={'repositories':{'repo':str(tmp_path)}}
    targets={'repo':['a.py']}
    plan={'unit':{'repository':'repo','target_paths':['a.py']},'sources':[]}
    previous={'files':['a.py'],'candidate':{'patch':'retained.diff','sha256':'c'*64,
        'targets':targets,'basis':tasks.source_state(config,targets)},
        'audit':{'verdict':'INCONCLUSIVE'},'producer':{'id':'preserved'}}
    before=deepcopy((config,plan,previous))
    assert tasks.require_retained_scope(plan,previous) is None
    result=tasks.replanned_delivery(config,plan,previous)
    assert result==previous and result is not previous
    assert result['candidate'] is not previous['candidate']
    result['candidate']['basis']['repo:a.py']='changed-copy'
    assert (config,plan,previous)==before


@pytest.mark.parametrize('relation',['revise','continue'])
def test_new_saved_revision_executes_while_unchanged_resume_only_rechecks(host,monkeypatch,relation):
    tools,control,_,_=host
    intake(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    previous={'verdict':'INCONCLUSIVE','files':['a.py'],
        'audit':{'verdict':'INCONCLUSIVE','reason':'NO_PROGRESS'},
        'candidate':{'patch':'retained.diff','sha256':'c'*64,'producer':{'id':'original'},
            'targets':{'repo':['a.py']},'basis':tasks.source_state(tools.state['config'],{'repo':['a.py']})}}
    task.update(delivery=deepcopy(previous),attempt_input='old-input')
    tasks.save(tools.root,value)
    user_reply(host,'Revise the saved delivery.' if relation=='revise' else 'Continue unchanged.')
    extra={'objective':'Change value to two and document it.','criteria':['value equals two','Document the change.']}
    intake(host,relation=relation,**(extra if relation=='revise' else {}))
    planned(host)
    seen=[]
    def execute(*args,**kwargs):
        seen.append(deepcopy(kwargs['candidate']))
        return {'verdict':'INCONCLUSIVE','candidate':deepcopy(previous['candidate']),
            'audit':deepcopy(previous['audit']),'files':['a.py'],'applied':False}
    monkeypatch.setattr(work_units,'run_unit',execute)
    result=tools.execute(effect('implement',{},'execute-revision'),control)
    assert result.status=='succeeded',result.output_json
    assert len(seen)==1 and seen[0]['review_only']==(relation=='continue')
    assert {k:v for k,v in seen[0].items() if k!='review_only'}==previous['candidate']
    repeated=tools.execute(effect('implement',{},'repeat-revision'),control)
    assert repeated.status=='succeeded' and 'Stop and explain' in repeated.output_json
    assert len(seen)==1


@pytest.mark.parametrize('operator_retry',[False,True])
def test_explicit_operator_retry_does_not_reset_failure_history(host,monkeypatch,operator_retry):
    tools,control,_,_=host
    intake(host);planned(host)
    task=tasks.current(tasks.load(tools.root))
    ledger={'units':{task['id']:{'history':[{'verdict':'NOT_VERIFIED'}]*2}}}
    original=deepcopy(ledger)
    monkeypatch.setattr(work_units,'read_ledger',lambda root:ledger)
    calls=[]
    monkeypatch.setattr(work_units,'run_unit',lambda root,state,*a,**kw:
        calls.append(state.get('operator_delivery_retry')) or {'verdict':'NOT_VERIFIED','applied':False})
    tools.__dict__['_host_retry_delivery']=operator_retry
    result=tools.execute(effect('implement',{},'operator-retry'),control)
    assert result.status==('succeeded' if operator_retry else 'refused')
    assert calls==([True] if operator_retry else [])
    assert ledger==original


def test_rechecking_retained_candidate_does_not_admit_another_implementer(host,monkeypatch):
    tools,control,_,_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    task['delivery']={'verdict':'INCONCLUSIVE','files':['a.py'],
        'audit':{'verdict':'INCONCLUSIVE','reason':'DEADLINE'},
        'candidate':{'patch':'retained.diff','sha256':'d'*64}}
    tasks.save(tools.root,value)
    monkeypatch.setattr(work_units,'check_failures',lambda *a,**kw:pytest.fail('No new implementer to admit.'))
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:
        seen.append(kw['candidate']['review_only']) or {'verdict':'INCONCLUSIVE','applied':False})
    result=tools.execute(effect('implement',{'recheck_candidate':True},'checks-only'),control)
    assert result.status=='succeeded',result.output_json
    assert seen==[True]


def test_audit_time_budget_is_part_of_the_recovery_contract():
    from relay import conversation_review
    from test_native_agent import deployment
    contract=conversation_review.context_contract(deployment())
    assert contract['max_seconds']==1200

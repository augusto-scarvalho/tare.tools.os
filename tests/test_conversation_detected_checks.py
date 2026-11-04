"""Automatic project checks precede paid reviews without changing saved chats."""
from copy import deepcopy
from pathlib import Path
import sys
import pytest
from relay import conversation_tasks as tasks, turn_engine
from test_conversation_tasks import host, intake, planned, effect


def test_godot_detection_requires_project_smoke_and_an_installed_engine(tmp_path,monkeypatch):
    monkeypatch.setattr(turn_engine.shutil,'which',lambda name: '/installed/godot' if name=='godot_console' else None)
    assert not turn_engine.config_for(tmp_path)['verifiers']
    (tmp_path/'project.godot').write_text('[application]\n')
    assert not turn_engine.config_for(tmp_path)['verifiers']
    (tmp_path/'tests').mkdir();(tmp_path/'tests/smoke.gd').write_text('extends SceneTree\n')
    checks=turn_engine.config_for(tmp_path)['verifiers']
    assert list(checks)==['godot-import','tests']
    assert checks['godot-import']['argv']==['/installed/godot','--headless','--path','.','--import','--quiet']
    assert checks['tests']['argv']==['/installed/godot','--headless','--fixed-fps','60','--path','.','-s','tests/smoke.gd']
    monkeypatch.setattr(turn_engine.shutil,'which',lambda name: None)
    assert not turn_engine.config_for(tmp_path)['verifiers']


def test_explicit_checks_take_precedence_and_detection_never_changes_saved_configuration(tmp_path,monkeypatch):
    config={'repositories':{'repo':str(tmp_path)},'verifiers':{}}
    monkeypatch.setattr(turn_engine,'config_for',lambda root,mode: {'verifiers':{'tests':{'repository':'workspace','argv':['python','check.py']}}})
    before=deepcopy(config)
    assert tasks.detected_checks(config)=={'tests':{'repository':'repo','argv':['python','check.py']}}
    assert config==before
    config['verifiers']['scoped']={'repository':'repo','argv':['python','focused.py']}
    assert tasks.detected_checks(config)=={}


def test_implementation_uses_detected_tests_before_audit_and_keeps_saved_plan_and_tools(host,monkeypatch):
    tools,control,project,_=host
    intake(host);planned(host)
    state=deepcopy(tools.state);saved_plan=deepcopy(tasks.current(tasks.load(tools.root))['plan'])
    checks={'tests':{'repository':'workspace','argv':[sys.executable,'-c','raise SystemExit(1)']}}
    monkeypatch.setattr(turn_engine,'config_for',lambda root,mode: {'verifiers':checks})
    seen=[]
    # The real executor is called positionally; capture its resolved verifier contract.
    from relay import work_units
    import inspect
    original=inspect.signature(work_units.run_unit)
    def capture(*args,**kwargs):
        bound=original.bind(*args,**kwargs)
        seen.append(bound.arguments)
        return {'verdict':'NOT_VERIFIED','reason':'The detected test failed before audit.'}
    monkeypatch.setattr(work_units,'run_unit',capture)
    result=tools.execute(effect('implement',{}),control)
    assert result.status=='succeeded',result.output_json
    assert seen
    unit=work_units.read_ledger(seen[0]['custody'])['plan']['units'][0]
    state_arg=seen[0]['state']
    assert unit['verifiers'][0]=='tests'
    assert state_arg['config']['verifiers']['tests']['repository']=='repo'
    assert tools.state==state
    assert tasks.current(tasks.load(tools.root))['plan']==saved_plan


def test_later_failed_check_cannot_return_an_old_green_delivery_or_authorize_edits(host,monkeypatch):
    from test_conversation_tasks import user_reply
    from relay import work_units
    tools,control,project,_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    task.update(stage='DELIVERED',delivery={'verdict':'VERIFIED','applied':True})
    tasks.save(tools.root,value)
    tools.state['config']['verifiers']={'tests':{'repository':'repo','argv':[sys.executable,'-c','raise SystemExit(1)']}}
    user_reply(host,'Only run tests and tell me the result.')
    result=tools.execute(effect('check',{'names':['tests']},'check-red'),control)
    assert result.status=='succeeded',result.output_json
    task=tasks.current(tasks.load(tools.root))
    assert task['stage']=='PENDING' and task['verification']['status']=='red'
    assert tasks.recovery_view(tools.root,task)['verdict']=='NOT_VERIFIED'
    called=[];monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:called.append(a))
    refused=tools.execute(effect('implement',{},'cannot-edit'),control)
    assert refused.status=='refused' and not called
    assert tools.execute(effect('demand',{'intent':'deliver','relation':'correct'},'cannot-promote'),control).status=='refused'
    saved=deepcopy(task['plan'])
    user_reply(host,'Now fix the failing check using the same plan.')
    intake(host,intent='deliver',relation='correct')
    current=tasks.current(tasks.load(tools.root))
    assert current['plan']==saved and current['delivery'] is None
    replan=tools.execute(effect('delegate_plan',{'instruction':'Fix the check'},'no-paid-replan'),control)
    assert replan.status=='refused' and 'planning again' in replan.output_json


def test_unstarted_route_refusal_does_not_relabel_an_older_failure_as_current(host,monkeypatch):
    import json
    from relay import work_units
    tools,control,project,_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    task['delivery']={'verdict':'INCONCLUSIVE','reason':'old failure'}
    task['delivery_invocation']='older-invocation'
    tasks.save(tools.root,value)
    calls=[]
    def run(*args,**kwargs):
        calls.append(args[2])
        if len(calls)==1:raise ValueError('The requested route is unavailable.')
        return {'verdict':'INCONCLUSIVE','reason':'current failure'}
    monkeypatch.setattr(work_units,'run_unit',run)
    assert tools.execute(effect('implement',{'route':'unavailable'},'bad-route'),control).status=='refused'
    result=tools.execute(effect('implement',{},'default-route'),control)
    assert result.status=='succeeded' and json.loads(result.output_json)['reason']=='current failure'
    repeated=tools.execute(effect('implement',{},'unchanged-repeat'),control)
    assert repeated.status=='succeeded' and json.loads(repeated.output_json)['reason']=='current failure'
    assert len(calls)==2

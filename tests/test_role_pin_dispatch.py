"""Exact role selections across offline dispatch and custody boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay import execution_scope as scope, environment, model_choices, native_catalog
from relay import native_routes, turn_engine, native_agent, work_units
from test_model_choices import profile
from test_native_agent import setup  # noqa: F401
from test_implement_unit import repository
from test_role_pins import parse


@pytest.fixture
def pin_catalog(monkeypatch, tmp_path):
    worker=profile(model='claude-opus-5-5', effort='medium')
    worker['model']='claude-opus-5.5'
    worker['id']='test-cli'
    producer=profile('cli://codex','gpt-6.1-sol','high')
    producer['id']='producer-cli'
    values={'test-cli':worker,'producer-cli':producer}
    monkeypatch.setattr(native_catalog,'select',lambda name:deepcopy(values[name]))
    monkeypatch.setattr(native_catalog,'resolve_host_mode',lambda value:value)
    monkeypatch.setattr(native_catalog,'offline_deployments',lambda **kw:(
        (name,deepcopy(value),True) for name,value in values.items()))
    monkeypatch.setattr(native_routes,'configured',lambda:{'deployments':[
        {'id':name,'native_catalog':name,'roles':list(model_choices.ROLES)} for name in values]})
    monkeypatch.setattr(turn_engine,'policy',lambda:{
        'implementer':[['test-cli']], 'planner':['test-cli'], 'planner_effort':'high',
        'researcher':['test-cli'],'auditor':['test-cli'],'auditor_selection':'configured','auditor_effort':'high'})
    monkeypatch.setattr(environment,'read',lambda:{})
    monkeypatch.setattr(environment,'listed',lambda *a:True)
    monkeypatch.setattr(environment,'allows',lambda value:scope.current().allows(value))
    monkeypatch.setattr(turn_engine,'usage_limited',lambda value:None)
    monkeypatch.setattr(turn_engine,'avoid_local_gpu',lambda:False)
    monkeypatch.setattr(turn_engine,'local_gpu_busy',lambda value:None)
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'storage'))
    monkeypatch.delenv(scope.ENV,raising=False)
    return values


def test_implementer_selection_precedes_preflight_limits_call_and_producer(setup,pin_catalog,monkeypatch):
    config,custody,root=setup
    repository(root)
    observations=[]
    class Review:
        def preflight(self,value):
            observations.append(('preflight',deepcopy(value)))
            return True
        def __call__(self,tree,value,*args):
            observations.append(('review',deepcopy(value)))
            return {'verdict':'REVISE'}
    original_limits=work_units.child_limits
    def limits(*args,**kwargs):
        observations.append(('limits',deepcopy(kwargs['deployment'])))
        return original_limits(*args,**kwargs)
    monkeypatch.setattr(work_units,'child_limits',limits)
    monkeypatch.setattr(work_units,'run_checks',lambda *a:('green','fixture check'))
    def worker(child,value,session,instruction,**kwargs):
        observations.append(('call',deepcopy(value)))
        assert child['role']=='implementer' and kwargs['delegated_leaf']
        assert kwargs['limits']['request_seconds']==kwargs['limits']['max_seconds']
        (Path(child['repositories']['repo'])/'a.py').write_text('value = 2\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    before=deepcopy(pin_catalog)
    state={'config':config,'conversation_flow':True,'deployment':pin_catalog['producer-cli'],
           'limits':{'max_model_calls':30},'deadline_utc':None}
    with scope.activated(parse(['--implementer','anthropic/test-cli@max'])):
        with scope.for_role('coordinator'):
            selected=list(work_units.routes(state['deployment']))
            assert len(selected)==1
            result=work_units.run_unit(custody,state,{'title':'Set two','target_paths':['a.py'],
                'criteria':['two'],'verifiers':['unit']},lambda:False,review=Review())
    assert result['verdict']=='NOT_REVIEWED'
    producer=work_units.candidate_producer(custody,result['candidate']['producer'])
    assert {kind for kind,_ in observations}=={'preflight','limits','call','review'}
    for _,value in observations:
        assert scope.pin_matches('anthropic/test-cli@max',value)
        assert value['native_profile']==producer['native_profile']
    assert pin_catalog==before and (root/'a.py').read_bytes()==b'value = 1\n'


@pytest.mark.parametrize('arguments',[{'chosen':'producer-cli'},{'effort':'high'}])
def test_explicit_worker_conflicts_fail_without_dispatch(pin_catalog,arguments):
    with scope.activated(parse(['--implementer','anthropic/test-cli@max'])):
        with pytest.raises(ValueError,match='tare models'):
            list(work_units.routes(pin_catalog['producer-cli'],**arguments))


def test_failed_worker_pin_never_falls_back_to_parent(pin_catalog,monkeypatch):
    with scope.activated(parse(['--implementer','anthropic/test-cli@max'])):
        monkeypatch.setattr(turn_engine,'usage_limited',lambda value:9999999999)
        with pytest.raises(ValueError,match='No fallback'):
            list(work_units.routes(pin_catalog['producer-cli'],allow_parent=True))


@pytest.mark.parametrize('kind,role',[('plan','planner'),('explore','explorer')])
def test_serve_flow_calls_exact_selection_before_session_role(pin_catalog,monkeypatch,tmp_path,kind,role):
    from relay import serve_agent
    observed=[]
    monkeypatch.setattr(native_agent,'session_root',lambda:tmp_path/'sessions')
    monkeypatch.setattr(serve_agent,'with_folder_access',lambda config,root:config)
    def run(config,value,*args,**kwargs):
        observed.append(deepcopy(value))
        assert config['role']==role
        return {'status':'PROPOSED','output':'fixture'}
    monkeypatch.setattr(native_agent,'run_session',run)
    with scope.activated(parse(['--'+role,'anthropic/test-cli@max'])):
        record,_=serve_agent.run_flow(kind,'Inspect',tmp_path,lambda text:None,None)
    assert record['route']=='test-cli'
    assert scope.pin_matches('anthropic/test-cli@max',observed[0])


def test_unpinned_worker_keeps_catalog_default(pin_catalog):
    name,value=next(work_units.routes(pin_catalog['test-cli']))
    assert name=='test-cli' and value==pin_catalog['test-cli']


def test_an_explorer_pinned_for_the_launch_explores_while_exploration_is_off(pin_catalog):
    from relay import delegation
    assert delegation.snapshot('session','coordinator') is None  # the default policy keeps exploration off
    with scope.activated(parse(['--explorer','anthropic/test-cli@max'])):
        frozen=delegation.snapshot('session','coordinator')
    assert frozen['selection']=='EXACT_TEMPORARY_PIN'
    assert scope.pin_matches('anthropic/test-cli@max',frozen['variant']['deployment'])

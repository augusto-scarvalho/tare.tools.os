"""Slow baseline verification must not dispatch an actor on a stale time allowance."""
from relay import work_units,turn_engine,native_agent
from test_implement_unit import repository
from test_native_agent import setup,deployment  # noqa: F401


def test_budget_is_rechecked_after_baseline_before_any_actor_dispatch(setup,tmp_path,monkeypatch):
    config,custody,root=setup
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine,'policy',lambda:{'implementer':[['child-route']]})
    monkeypatch.setattr(turn_engine,'available',lambda name:(deployment(),None))
    baseline=[];dispatched=[]
    def limits(*args,**kwargs):
        if baseline:raise ValueError('R5: active time expired after baseline verification')
        return {'max_model_calls':4,'max_seconds':30}
    def checks(*args):baseline.append(True);return 'green','Observed unchanged baseline.'
    def actor(*args,**kwargs):
        dispatched.append(True)
        raise AssertionError('Actor started on the stale precheck allowance')
    monkeypatch.setattr(work_units,'child_limits',limits)
    monkeypatch.setattr(work_units,'run_checks',checks)
    monkeypatch.setattr(native_agent,'run_session',actor)
    result=work_units.run_unit(custody,{'config':config,'conversation_flow':True,
        'deployment':deployment(),'limits':{'max_model_calls':30},'deadline_utc':None},
        {'key':'budget','title':'Change value','repository':'repo','target_paths':['a.py'],
         'criteria':['value is two'],'test_paths':[],'verifiers':['unit']},lambda:False)
    assert not dispatched
    assert len(baseline)==1
    assert 'expired after baseline' in result['reason']

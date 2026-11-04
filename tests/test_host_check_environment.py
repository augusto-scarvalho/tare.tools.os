"""Host checks use the same ambient-environment boundary as the native verify tool."""
import json
import subprocess
import sys
import pytest
from pathlib import Path

from relay import work_units


@pytest.mark.parametrize('unbounded',[False,True])
def test_unprotected_host_checks_do_not_inherit_the_frontend_or_operator_secrets(tmp_path, monkeypatch,unbounded):
    forbidden=('TARE_PROFILE_DIR','TARE_ACTION_APPROVAL_ROOT','TARE_SESSION_SCOPE','ANTHROPIC_API_KEY')
    for name in forbidden:
        monkeypatch.setenv(name,'offline-do-not-inherit')
    # This is the host's execution policy, not just a child-environment marker.
    # Exercise a valid operator scope while proving the child cannot inherit it.
    monkeypatch.setenv('TARE_SESSION_SCOPE',json.dumps({'unlimited':['time'] if unbounded else []}))
    script='import os,json; print(json.dumps({k:os.environ.get(k) for k in '+repr(forbidden)+'}))'
    state,output=work_units.run_checks(tmp_path,{'probe':[sys.executable,'-c',script]})
    assert state=='green'
    assert json.loads(output.split('\n',1)[1])==dict.fromkeys(forbidden)
    assert 'offline-do-not-inherit' not in output


def test_invalid_host_scope_refuses_before_starting_a_check(tmp_path,monkeypatch):
    monkeypatch.setenv('TARE_SESSION_SCOPE','offline-invalid-policy')
    seen=[]
    token=work_units.CHECK_PORT.set(lambda *args,**kwargs:seen.append(args))
    try:
        with pytest.raises(ValueError,match='Invalid temporary execution policy'):
            work_units.run_checks(tmp_path,{'probe':['must-not-start']})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert not seen


def test_protected_check_port_keeps_ownership_of_its_environment(tmp_path):
    seen=[]
    def port(argv,**kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv,0,'protected','')
    token=work_units.CHECK_PORT.set(port)
    try:
        assert work_units.run_checks(tmp_path,{'probe':['owned-check']})==('green','$ probe (exit 0)\nprotected')
    finally:
        work_units.CHECK_PORT.reset(token)
    assert 'env' not in seen[0]


def test_rechecking_a_preserved_candidate_runs_no_worker_and_still_requires_audit(tmp_path, monkeypatch):
    from relay import native_agent
    from relay.work_forms import record_plan
    from test_native_agent import deployment
    root=tmp_path/'project';root.mkdir();(root/'a.py').write_text('value = 1\n')
    custody=tmp_path/'session';custody.mkdir()
    ready=tmp_path/'check-environment-ready'
    script="from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'; assert Path("+repr(str(ready))+").exists()"
    unit={'key':'change','title':'Set two','target_paths':['a.py'],'criteria':['value equals two'],
        'verifiers':['value']}
    config={'repositories':{'repo':str(root)},'writable':{'repo':['a.py']},'conversation_flow':True,
        'verifiers':{'value':{'repository':'repo','argv':[sys.executable,'-I','-c',script]}}}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
        'limits':{'max_model_calls':40},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    calls=[];audits=[]
    def worker(config,*args,**kwargs):
        calls.append(True)
        (Path(config['repositories']['repo'])/'a.py').write_text('value = 2\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    def audit(tree,*args):
        audits.append(True)
        assert (root/'a.py').read_text()=='value = 1\n'
        return {'verdict':'ACCEPT' if len(audits)>1 else 'REVISE'}
    first=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=audit)
    assert first['verdict']=='NOT_VERIFIED' and first['candidate'] and not audits
    ready.touch()
    second=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=audit,
        candidate={**first['candidate'],'review_only':True})
    assert second['verdict']=='NOT_REVIEWED' and not second.get('applied')
    assert (root/'a.py').read_text()=='value = 1\n'
    third=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=audit,
        candidate={**second['candidate'],'review_only':True})
    assert third['applied'] and (root/'a.py').read_text()=='value = 2\n'
    assert len(calls)==1 and len(audits)==2 and work_units.unit_calls(custody)==1


@pytest.mark.parametrize('recheck_state',['green','red','inconclusive'])
def test_readonly_recheck_does_not_repeat_an_unchanged_check_set(tmp_path,monkeypatch,recheck_state):
    from relay import native_agent
    from relay.work_forms import record_plan
    from test_native_agent import deployment
    root=tmp_path/'project';root.mkdir();(root/'a.py').write_text('value = 1\n')
    custody=tmp_path/'session';custody.mkdir()
    checks=[];workers=[];audits=[]
    unit={'key':'change','title':'Set two','target_paths':['a.py'],'criteria':['value equals two'],'verifiers':['one','two']}
    config={'repositories':{'repo':str(root)},'writable':{'repo':['a.py']},'conversation_flow':True,
        'verifiers':{name:{'repository':'repo','argv':[sys.executable,'-c','pass']} for name in ['one','two']}}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),'limits':{'max_model_calls':40},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('worker',deployment())]))
    monkeypatch.setattr(work_units,'run_checks',lambda tree,commands:(checks.append(tuple(commands)) or ('green','passed')))
    def worker(config,*args,**kwargs):
        workers.append(True);(Path(config['repositories']['repo'])/'a.py').write_text('value = 2\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    def review(*args):audits.append(True);return {'verdict':'REVISE'}
    first=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=review)
    checks.clear()
    monkeypatch.setattr(work_units,'run_checks',lambda tree,commands:(checks.append(tuple(commands)) or (recheck_state,'observed')))
    second=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=review,
        candidate={**first['candidate'],'review_only':True})
    assert second['verdict']=={'green':'NOT_REVIEWED','red':'NOT_VERIFIED','inconclusive':'INCONCLUSIVE'}[recheck_state]
    assert not second.get('applied')
    assert len(checks)==1 and len(workers)==1 and len(audits)==(2 if recheck_state=='green' else 1)
    assert (root/'a.py').read_text()=='value = 1\n'

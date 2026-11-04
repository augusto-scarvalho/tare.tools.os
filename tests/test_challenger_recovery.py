"""Retry only the failed arm; a real completed reference and every prior receipt remain intact."""
import copy
import hashlib
import json
import os
from pathlib import Path
import psutil
import pytest

from relay import challenger as c, challenger_worker as worker, native_agent
from relay import challenger_recovery as recovery
from relay.native_profiles import install
from test_challenger import project, tmp_path
from test_native_agent import Client, deployment, steps


@pytest.fixture
def failed_arm(project,tmp_path,monkeypatch,request):
    if getattr(request,'param',None)=='short-root':
        if os.name!='nt':pytest.skip('Windows path aliases')
        import ctypes
        short=ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(tmp_path),short,len(short)):
            pytest.skip('This host does not expose short path aliases')
        if os.path.normcase(short.value)==os.path.normcase(str(tmp_path)):
            pytest.skip('This volume does not create short path aliases')
        monkeypatch.setenv('TARE_STORAGE_ROOT',str(Path(short.value)/'state'))
    alternate=deployment();alternate['native_profile']['model']='alternate'
    alternate['native_challenger_accounting']='quota'
    profile=tmp_path/'alternate.json';profile.write_text(json.dumps(alternate))
    install('retry-fixture',profile)
    settings=copy.deepcopy(c.DEFAULT)
    settings.update(enabled=True,models=['retry-fixture'],external_percent=100,local_percent=0,
        seconds=15,calls=5,daily_quota_calls=20,allow_unknown_quota=True)
    c.configure(project,settings)
    launches=[]
    def spawn(job):launches.append(job);return os.getpid(),psutil.Process().create_time()
    monkeypatch.setattr(c,'spawn',spawn)
    config={'schema':'tare.tools/native-agent-config/1','role':'implementer',
        'repositories':{'repo':str(project)},'writable':{'repo':['a.py']},
        'specgraph_edit_context':False,'capabilities':['read','edit','verify'],
        'verifiers':{'unit':{'repository':'repo','argv':[os.sys.executable,'-c',
            "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]}}}
    source=tmp_path/'reference-producer';client=Client(steps=steps())
    result=native_agent.run_session(config,deployment(),source,'Set value to 2 and verify.',
        client=client,local_counter=client)
    assert result['status']=='PROPOSED' and len(launches)==1
    job=launches[0];row=c.read_run(job/'run.json')
    # No worker was dispatched: this fixture represents its recorded transport refusal.
    row.update(status='FAILED',challenger_outcome='BLOCKED',challenger_reason='CLI_UNEXPECTED_INVOCATION',checks_passed=False)
    (job/'run.json').write_text(json.dumps(row))
    # A delegated workflow keeps its original project at this input while the
    # separate reference tree contains the completed producer's real output.
    (project/'a.py').write_bytes(b'value = 1\n')
    return source,job,client,launches


@pytest.mark.parametrize('failed_arm',[None,'short-root'],indirect=True)
def test_retry_dispatches_only_new_challenger_and_preserves_real_reference(failed_arm,project,monkeypatch):
    source,old,primary,launches=failed_arm
    originals={p:p.read_bytes() for p in (old/'run.json',old/'reference.json',source/'native-agent.json',source/'outcome.json')}
    calls=primary.generations
    result=recovery.retry(project,old);new=Path(result['job'])
    assert new!=old and launches==[old,new]
    row=c.read_run(new/'run.json')
    assert Path(row['source']).samefile(source) and Path(row['reference_reuse']['job']).samefile(old)
    assert row['reference_reuse']['source_state_sha256']==hashlib.sha256(originals[source/'native-agent.json']).hexdigest()
    assert row['execution_contract']['reference_reuse']==1
    reference=native_agent.read_json(new/'reference.json')
    assert reference['outcome']['status']=='PROPOSED' and reference['reused_from']==str(old)
    def execute(config,dep,variant,session,instruction,row,cancellation):
        class Alternate(Client):
            def post(self,*args,**kwargs):
                value=super().post(*args,**kwargs)
                if 'model' in value:value['model']=dep['native_profile']['model']
                return value
        client=Alternate(steps=steps())
        return native_agent.run_session(config,dep,session,instruction,
            client=client,local_counter=client,cancellation=cancellation)
    monkeypatch.setattr(worker,'execute',execute)
    worker.run(new)
    ended=c.read_run(new/'run.json')
    assert ended['checks_passed'],{k:ended.get(k) for k in ('status','reason','challenger_outcome','challenger_reason')}
    assert primary.generations==calls and (project/'a.py').read_bytes()==b'value = 1\n'
    assert all(p.read_bytes()==raw for p,raw in originals.items())


@pytest.mark.parametrize('damage',['input','reference','producer','qualified','active','scope'])
def test_retry_refuses_invalid_or_already_qualified_evidence_before_dispatch(failed_arm,project,damage):
    source,old,primary,launches=failed_arm
    row=c.read_run(old/'run.json')
    if damage=='input':(project/'a.py').write_bytes(b'user edit\n')
    elif damage=='reference':
        from relay.challenger_workspace import tree
        (tree(old,'reference')/'a.py').write_bytes(b'changed reference\n')
    elif damage=='producer':
        value=native_agent.read_json(source/'native-agent.json');value['invocations'][-1]['status']='CANCELLED'
        (source/'native-agent.json').write_text(json.dumps(value))
    elif damage=='qualified':row.update(checks_passed=True,challenger_verdict='accepted')
    elif damage=='active':row['status']='RUNNING'
    elif damage=='scope':row['config']['writable']={'repo':['.']}
    if damage in {'qualified','active','scope'}:(old/'run.json').write_text(json.dumps(row))
    with pytest.raises(ValueError):recovery.retry(project,old)
    assert launches==[old]


def test_stop_is_cooperative_idempotent_and_does_not_rewrite_terminal_rows(failed_arm,project):
    source,old,primary,launches=failed_arm
    before=(old/'run.json').read_bytes()
    assert 'already stopped' in recovery.stop(project,old)
    assert not (old/'cancel').exists() and (old/'run.json').read_bytes()==before
    row=c.read_run(old/'run.json');row['status']='RUNNING';(old/'run.json').write_text(json.dumps(row))
    recovery.stop(project,old);(old/'cancel').write_bytes(b'retained stop')
    recovery.stop(project,old)
    assert (old/'cancel').read_bytes()==b'retained stop'


def test_changed_reference_producer_after_admission_stops_before_new_model_dispatch(failed_arm,project,monkeypatch):
    source,old,primary,launches=failed_arm
    new=Path(recovery.retry(project,old)['job'])
    value=native_agent.read_json(source/'native-agent.json');value['changed_after_admission']=True
    (source/'native-agent.json').write_text(json.dumps(value))
    monkeypatch.setattr(worker,'execute',lambda *a:pytest.fail('model after changed reference producer'))
    worker.run(new)
    row=c.read_run(new/'run.json')
    assert row['status']=='FAILED' and 'reference producer changed' in row['reason']


@pytest.fixture
def failed_review(failed_arm, project, monkeypatch):
    source, old, primary, launches = failed_arm
    settings=c.policy(project);settings.update(reviewer='retry-fixture',daily_quota_calls=100)
    c.configure(project,settings)
    job=Path(recovery.retry(project,old)['job'])
    def execute(config,dep,variant,session,instruction,row,cancellation):
        class Alternate(Client):
            def post(self,*args,**kwargs):
                value=super().post(*args,**kwargs)
                if 'model' in value:value['model']=dep['native_profile']['model']
                return value
        client=Alternate(steps=steps())
        return native_agent.run_session(config,dep,session,instruction,
            client=client,local_counter=client,cancellation=cancellation)
    monkeypatch.setattr(worker,'execute',execute)
    monkeypatch.setattr(worker,'audit',lambda *args:('unknown','CLI_UNEXPECTED_TOOL'))
    worker.run(job)
    row=c.read_run(job/'run.json')
    assert row['status']=='FAILED' and row['checks_passed'] and row['challenger_outcome']=='PROPOSED'
    return job


def test_review_retry_reuses_both_implementations_without_new_worktrees_or_implementer_calls(failed_review,project,monkeypatch):
    from relay.challenger_workspace import tree
    old=failed_review
    original={p:p.read_bytes() for p in (old/'run.json',old/'implementation/native-agent.json',old/'implementation/outcome.json')}
    new=Path(recovery.retry(project,old)['job'])
    row=c.read_run(new/'run.json')
    assert row['implementation_reuse']['source']==str(old/'implementation')
    assert row['quota_calls_reserved']==10  # two five-call reviews; no new implementer reservation
    assert tree(new,'candidate')==tree(old,'candidate') and tree(new,'reference')==tree(old,'reference')
    monkeypatch.setattr(worker,'execute',lambda *args:pytest.fail('Completed implementation was rerun'))
    reviews=[]
    monkeypatch.setattr(worker,'audit',lambda row,job,variant,cancel:(reviews.append(variant) or 'accepted','qualified'))
    worker.run(new)
    ended=c.read_run(new/'run.json')
    assert ended['status']=='COMPLETED' and ended['checks_passed'],ended.get('reason')
    assert reviews==['challenger','reference']
    assert all(p.read_bytes()==raw for p,raw in original.items())


@pytest.mark.parametrize('damage',['candidate','producer','outcome'])
def test_review_retry_refuses_damaged_completed_implementation(failed_review,project,damage):
    from relay.challenger_workspace import tree
    old=failed_review
    if damage=='candidate':(tree(old,'candidate')/'a.py').write_text('changed candidate')
    elif damage=='producer':
        p=old/'implementation/native-agent.json';s=native_agent.read_json(p)
        s['invocations'][-1]['status']='CANCELLED';p.write_text(json.dumps(s))
    else:(old/'implementation/outcome.json').write_text('{"status":"BLOCKED"}')
    with pytest.raises(ValueError):recovery.retry(project,old)


def test_changed_completed_implementation_after_admission_blocks_paid_review(failed_review,project,monkeypatch):
    old=failed_review
    new=Path(recovery.retry(project,old)['job'])
    p=old/'implementation/native-agent.json';value=native_agent.read_json(p)
    value['changed_after_admission']=True;p.write_text(json.dumps(value))
    monkeypatch.setattr(worker,'audit',lambda *args:pytest.fail('Review after producer changed'))
    worker.run(new)
    row=c.read_run(new/'run.json')
    assert row['status']=='FAILED' and 'challenger producer changed' in row['reason']


def test_storage_alias_normalization_still_refuses_links(project,tmp_path,monkeypatch):
    target=tmp_path/'physical-state';target.mkdir()
    link=tmp_path/'linked-state'
    try:link.symlink_to(target,target_is_directory=True)
    except OSError:pytest.skip('Host does not permit directory symlinks')
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(link))
    with pytest.raises(ValueError,match='native_custody_link_forbidden'):
        c.directory(project)
    assert not list(target.iterdir())

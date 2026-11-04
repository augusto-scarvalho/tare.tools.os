"""Real operator check execution, custody, receipts and bounded cancellation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import sys
import threading
import time
from uuid import uuid4

import pytest

from relay import native_review_checks as checks
from relay import native_agent
from relay.native_review import review
from relay.native_review_actions import undo_edit
from test_native_agent import setup, Client, run, steps


@pytest.fixture
def checked(setup):
    config,custody,root = setup
    config['verifiers']['unit']['argv'] = [sys.executable,'-B','-c',
        "from pathlib import Path; print('unit output'); assert Path('a.py').read_text() == 'value = 2\\n'"]
    client = Client(steps=steps());run(config,custody,client)
    return config,custody,root,client


def view(saved):
    _,custody,root,_=saved
    return checks.review_checks(custody,project=root)


def execute(saved, *, check=None, run_id=None, **kwargs):
    _,custody,root,_=saved
    check=check or view(saved)['checks'][0]
    return checks.run_check(custody,project=root,check_id=check['id'],expected_plan=check['plan'],
                            run_id=run_id or uuid4().hex,**kwargs)


def set_command(saved,source):
    _,custody,_,_=saved
    path=custody/'native-agent.json';state=json.loads(path.read_text())
    state['config']['verifiers']['unit']['argv']=[sys.executable,'-B','-c',source]
    path.write_text(json.dumps(state))


def test_actual_check_result_output_reopen_freshness_and_replay(checked):
    _,custody,root,client=checked
    journal=(custody/'journal.jsonl').read_bytes();calls=len(client.calls)
    preview=view(checked)['checks'][0];identity=uuid4().hex
    result=execute(checked,check=preview,run_id=identity)
    assert result['status']=='PASS' and result['cleanup_verified']
    output=checks.read_check_run(custody,run_id=identity)
    assert 'unit output' in output['output'] and not output['output_truncated']
    assert view(checked)['runs'][0]['freshness']=='OBSERVED_FILES_MATCH'
    assert review(custody)['checks'][0]['freshness']=='STALE'
    with pytest.raises(ValueError):execute(checked,check=preview,run_id=identity)
    (root/'a.py').write_text('value = 3\n')
    assert view(checked)['runs'][0]['freshness']=='STALE'
    failed=execute(checked)
    assert failed['status']=='FAIL' and failed['returncode']!=0 and failed['cleanup_verified']
    assert 'AssertionError' in checks.read_check_run(custody,run_id=failed['run_id'])['output']
    assert (custody/'journal.jsonl').read_bytes()==journal and len(client.calls)==calls


@pytest.mark.parametrize('change',['file','config','permissions','project','turn'])
def test_changed_preview_cannot_execute(checked,change):
    _,custody,root,_=checked;preview=view(checked)['checks'][0]
    if change=='file':(root/'a.py').write_text('value = 4\n')
    if change in {'config','permissions'}:
        path=custody/'native-agent.json';state=json.loads(path.read_text())
        if change=='config':state['config']['verifiers']['unit']['argv']=[sys.executable,'-c','print(42)']
        else:state['config']['capabilities']=['read','edit']
        path.write_text(json.dumps(state))
    kwargs={'project':root.parent if change=='project' else root,'invocation_id':'missing' if change=='turn' else None}
    with pytest.raises(ValueError):
        checks.run_check(custody,check_id=preview['id'],expected_plan=preview['plan'],run_id=uuid4().hex,**kwargs)
    assert not (custody/'review'/'check-runs').exists()


def test_live_output_cancellation_and_writer_exclusion(checked):
    _,custody,root,_=checked
    set_command(checked,"import time; print('ready',flush=True); time.sleep(30)")
    preview=view(checked)['checks'][0];identity=uuid4().hex;cancel=threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(execute,checked,check=preview,run_id=identity,cancelled=cancel.is_set)
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:
                    if 'ready' in checks.read_check_run(custody,run_id=identity)['output']:break
                except (OSError,ValueError):pass
                time.sleep(.03)
            else:pytest.fail('Child never produced live output')
            with pytest.raises(ValueError,match='active_writer'):view(checked)
        finally:
            cancel.set()
        result=future.result(timeout=10)
    assert result['status']=='CANCELLED' and result['cleanup_verified']
    assert view(checked)['runs'][0]['status']=='CANCELLED'


def test_undo_invalidates_check_and_check_intent_failure_never_executes(checked,monkeypatch):
    _,custody,root,_=checked
    result=execute(checked)
    edit=review(custody)['edits'][0]
    undo_edit(custody,project=root,effect_id=edit['effect_id'])
    assert view(checked)['runs'][0]['freshness']=='STALE'
    def broken(*args,**kwargs):raise OSError('full disk')
    monkeypatch.setattr(checks,'save_json',broken)
    preview=view(checked)['checks'][0]
    with pytest.raises(OSError,match='full disk'):execute(checked,check=preview)
    assert (root/'a.py').read_text()=='value = 1\n'


def test_unknown_cleanup_blocks_rerun_undo_and_resume(checked,monkeypatch):
    import compute_plane.managed_process as owner
    _,custody,root,_=checked
    original=owner.run_process
    def uncertain(*args,**kwargs):
        result=original(*args,**kwargs);result['cleanup_verified']=False;return result
    monkeypatch.setattr(owner,'run_process',uncertain)
    result=execute(checked)
    assert result['status']=='UNKNOWN'
    assert view(checked)['checks'][0]['plan'] is None
    with pytest.raises(ValueError):execute(checked)
    with pytest.raises(ValueError,match='unconfirmed'):
        undo_edit(custody,project=root,effect_id=review(custody)['edits'][0]['effect_id'])
    with pytest.raises(ValueError,match='unconfirmed'):
        native_agent.run_session(None,None,custody,resume=True,client=Client(steps=[]))


def test_orphaned_intent_is_unknown_and_output_is_bounded(checked):
    _,custody,_,_=checked
    set_command(checked,"print('x'*100000)")
    result=execute(checked)
    output=checks.read_check_run(custody,run_id=result['run_id'],max_bytes=512)
    assert output['output_truncated'] and len(output['output'])<=512
    path=custody/'review'/'check-runs'/result['run_id']/'receipt.json'
    receipt=json.loads(path.read_text());receipt.update(status='RUNNING',cleanup_verified=False)
    path.write_text(json.dumps(receipt))
    current=view(checked)
    assert current['runs'][0]['status']=='UNKNOWN' and current['checks'][0]['plan'] is None
    with pytest.raises(ValueError):checks.read_check_run(custody,run_id='../escape')


def test_recorded_command_requires_original_authority_and_completed_process(checked):
    from types import SimpleNamespace
    _,custody,root,_=checked
    state=json.loads((custody/'native-agent.json').read_text());config=native_agent.configuration(state['config'])
    config.update(terminal=True,capabilities=['terminal_start'])
    argv=[sys.executable,'-c','print(123)']
    events=[{'kind':'session_started','capabilities':['terminal_start']},
            {'kind':'effect_requested','effect_id':'command1','tool_call':{'name':'terminal_start','arguments_json':json.dumps({'repository':'repo','argv':argv})}},
            {'kind':'effect_returned','result':{'effect_id':'command1','status':'succeeded','output_json':json.dumps({'handle':'h','status':'running'})}},
            {'kind':'effect_requested','effect_id':'read1','tool_call':{'name':'terminal_read','arguments_json':json.dumps({'handle':'h'})}},
            {'kind':'effect_returned','result':{'effect_id':'read1','status':'succeeded','output_json':json.dumps({'handle':'h','status':'exited','exit_code':0,'cleanup_verified':True})}}]
    records=[SimpleNamespace(event=lambda e=e:e) for e in events]
    row=checks._catalog(state,config,records,'repo',None)[-1]
    assert row['argv']==argv and row['disabled_reason'] is None
    assert checks._catalog(state,config,records[:-1],'repo',None)[-1]['disabled_reason']
    config['capabilities']=['read']
    assert checks._catalog(state,config,records,'repo',None)[-1]['disabled_reason']


def test_real_terminal_command_is_rerun_without_model_or_journal_mutation(setup):
    from relay.console_terminal import ConsoleTerminals
    from compute_plane.terminal_host import TerminalClient
    config,custody,root=setup
    config.update(terminal=True,capabilities=['read','terminal_start','terminal_read'])
    argv=[sys.executable,'-B','-c',"from pathlib import Path; p=Path('runs.txt'); p.write_text((p.read_text() if p.exists() else '')+'run\\n'); print('recorded command completed')"]
    read_args={'handle':'pending','wait_ms':1000}
    terminals=ConsoleTerminals()
    try:
        client_port=TerminalClient(terminals.connection(custody,config['repositories']))
        model=Client(steps=[('read',{'repository':'repo','path':'a.py'}),
            ('terminal_start',{'repository':'repo','argv':argv,'mode':'pipes','timeout_seconds':5}),
            ('terminal_read',read_args),('tare_deliver',{'summary':'Command completed.'})])
        def ready():
            if model.generations!=3:return
            rows=client_port.request('list',include_completed=True);assert len(rows)==1
            read_args['handle']=rows[0]['handle']
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                observed=client_port.request('read',handle=rows[0]['handle'],cursors={},limit=4096)
                if observed['status']=='exited' and observed['cleanup_verified']:return
                time.sleep(.03)
            pytest.fail('Original terminal command did not finish')
        model.after_model=ready
        from test_native_agent import deployment
        native_agent.run_session(config,deployment(),custody,'Run this command.',client=model,local_counter=model,
                                 terminal_client=client_port)
    finally:
        terminals.close()
    journal=(custody/'journal.jsonl').read_bytes();calls=len(model.calls)
    saved=(config,custody,root,model)
    selected=next(r for r in view(saved)['checks'] if r['kind']=='command')
    assert selected['plan'] and selected['argv']==argv
    result=execute(saved,check=selected)
    assert result['status']=='PASS' and (root/'runs.txt').read_text()=='run\nrun\n'
    assert (custody/'journal.jsonl').read_bytes()==journal and len(model.calls)==calls


def test_missing_executable_refuses_without_poisoning_conversation(checked):
    _,custody,root,_=checked
    path=custody/'native-agent.json';state=json.loads(path.read_text())
    state['config']['verifiers']['unit']['argv']=[str(root/'missing-executable-74921')]
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match='Executable not found'):execute(checked)
    assert not (custody/'review'/'check-runs').exists()
    checks.ensure_checks_quiescent(custody)

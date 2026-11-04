"""Ordinary Work runs pause complete rounds and resume the same durable attempt."""
import json
from pathlib import Path

import pytest
import tare_ops
from relay import native_work_host, relay_mesh, work_control, work_run
from test_work_resume import host, run
from test_work_audit import reviewers
from test_work_run import os_root

real_plan_audit = native_work_host.plan_audit


def options(host):
    return {'graph_path':relay_mesh.WORK_GRAPH_FILE,'repository_root':host[1]}


def request_pause(host):
    view = work_control.inspect('work-one', **options(host))
    return work_control.command('work-one', 'pause', view['token'], **options(host))


def pause_implementation(host, monkeypatch, capsys, *, calls=None):
    client = host[3]
    original_source = (host[1]/'src/value.txt').read_bytes()
    post = client.post
    def invoke(path, body, control):
        reply = post(path, body, control)
        if not path.endswith('/count-tokens') and len(client.generations)==1:
            request_pause(host)
        return reply
    monkeypatch.setattr(client, 'post', invoke)
    if calls is None:
        assert run(host)==0
        receipt = json.loads(capsys.readouterr().out)
    else:
        receipt = work_run.run_work('work-one',repository_root=host[1],
            native_deployment=json.loads(host[2].read_bytes()),
            limits={'max_model_calls':calls,'max_seconds':30})
    assert receipt['status']=='PAUSED', receipt
    assert len(client.generations)==1
    assert receipt['candidate_commit'] is None and receipt['landing'] is None
    train = work_run.train_dir_for('work-one')
    assert not (train/'CANDIDATE_CHANGE.json').exists()
    assert relay_mesh.current_lease(work_run.train_id_for('work-one')) is None
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    from relay import native_work_credentials
    from relay.cluster_coordinator import CoordinatorDB
    binding = json.loads((Path(origin['implementation_custody'])/'host.json').read_bytes())['binding']
    assert not CoordinatorDB(work_run.coordinator_path()).is_agent_runtime_binding_current(
        work_id=binding['work_id'], binding_id=binding['binding_id'], owner_id=binding['owner_id'],
        fence_seq=binding['fence'], token=native_work_credentials.read(binding['binding_id'])['token'])
    journal = Path(origin['implementation_custody'])/'journal.jsonl'
    events = [json.loads(row)['event'] for row in journal.read_bytes().splitlines()]
    assert sum(e['kind']=='model_requested' for e in events)==1
    assert sum(e['kind']=='effect_returned' for e in events)==1
    assert (host[1]/'src/value.txt').read_bytes()==original_source
    assert work_run.status('work-one')['operational_control']['status']=='PAUSED'
    return train, origin, journal


def explicit_resume(host):
    view = work_control.inspect('work-one', **options(host))
    return work_control.command('work-one','resume',view['token'],**options(host))


def test_pause_reopen_intervene_resume_keeps_journal_and_call_budget(host, monkeypatch, capsys):
    train, origin, journal = pause_implementation(host,monkeypatch,capsys)
    frozen = journal.read_bytes()
    for _ in range(2):
        assert tare_ops.main(['run','work-one','--resume'])==0
        assert json.loads(capsys.readouterr().out)['status']=='PAUSED'
        assert journal.read_bytes()==frozen and len(host[3].generations)==1
    paused = work_control.inspect('work-one',**options(host))
    note = work_control.command('work-one','intervene',paused['token'],'Keep the newline.',**options(host))
    note_id = note['record']['pending_note']['id']
    explicit_resume(host)
    assert tare_ops.main(['run','work-one','--resume'])==0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status']=='SETTLED',receipt
    assert len(host[3].generations)==5 and len(host[4])==1
    assert journal.read_bytes().startswith(frozen)
    events = [json.loads(row)['event'] for row in journal.read_bytes().splitlines()]
    assert [e['message_id'] for e in events if e['kind']=='user_message']==[note_id]
    assert sum(e['kind']=='model_requested' for e in events)==5
    assert sum(e['kind']=='effect_returned' for e in events)==4
    assert (host[1]/'src/value.txt').read_text()=='strict\n'
    assert work_run.status('work-one')['operational_control'] is None


def test_pause_seal_preserves_custody_bytes_until_explicit_resume(host, monkeypatch, capsys):
    checkpoint = work_control.checkpoint
    sealed = {}
    def capture_seal(*args, **kwargs):
        result = checkpoint(*args, **kwargs)
        if kwargs.get('paused_result') is not None and args[2]['status'] == 'PAUSED':
            custody = Path(kwargs['custody'])
            sealed.update({name: (custody/name).read_bytes() for name in ('host.json', 'attempt.json')})
        return result
    monkeypatch.setattr(work_control, 'checkpoint', capture_seal)
    train, origin, journal = pause_implementation(host, monkeypatch, capsys)
    custody = Path(origin['implementation_custody'])
    assert set(sealed) == {'host.json', 'attempt.json'}
    attempts = json.loads(sealed['host.json'])['attempts']
    assert attempts[-1]['status'] == 'DISPATCHING'
    assert 'reason' not in attempts[-1]
    assert {name: (custody/name).read_bytes() for name in sealed} == sealed
    for _ in range(2):
        assert tare_ops.main(['run', 'work-one', '--resume']) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'PAUSED'
        assert {name: (custody/name).read_bytes() for name in sealed} == sealed
        assert len(host[3].generations) == 1
    explicit_resume(host)
    assert {name: (custody/name).read_bytes() for name in sealed} == sealed
    assert tare_ops.main(['run', 'work-one', '--resume']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'SETTLED'
    settled = json.loads((custody/'host.json').read_bytes())['attempts']
    assert settled[:-1] == attempts[:-1]
    assert settled[-1] == {**attempts[-1], 'status': 'FINISHED', 'reason': 'ACCEPTED'}


@pytest.mark.parametrize('name', ['host.json', 'attempt.json'])
def test_paused_custody_tampering_is_refused_before_model_dispatch(host, monkeypatch, capsys, name):
    train, origin, journal = pause_implementation(host, monkeypatch, capsys)
    path = Path(origin['implementation_custody'])/name
    value = json.loads(path.read_bytes())
    if name == 'host.json':
        value['attempts'][-1]['status'] = 'FINISHED'
    else:
        value['identity'] = 'tampered-identity'
    path.write_text(json.dumps(value), encoding='utf-8')
    tampered = path.read_bytes()
    frozen_journal = journal.read_bytes()
    explicit_resume(host)
    assert tare_ops.main(['run', 'work-one', '--resume']) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'FAILED'
    assert receipt['error'] == 'ValueError:WORK_CONTROL_CUSTODY_CHANGED'
    assert len(host[3].generations) == 1
    assert journal.read_bytes() == frozen_journal
    assert path.read_bytes() == tampered


def test_finish_runner_default_settles_only_latest_attempt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from relay import native_work_credentials
    earlier = {'status': 'FINISHED', 'reason': 'NOT_ACCEPTED', 'deployment': 'earlier'}
    current = {'status': 'DISPATCHING', 'deployment': 'current'}
    path = tmp_path/'host.json'
    path.write_text(json.dumps({'attempts': [earlier, current]}), encoding='utf-8')
    discarded = []
    monkeypatch.setattr(native_work_credentials, 'discard', lambda binding: discarded.append(binding))
    runner = SimpleNamespace(settings={'custody': tmp_path,
        'control': SimpleNamespace(binding=SimpleNamespace(binding_id='fixture-binding'))})
    native_work_host.finish_runner(runner, accepted=True)
    assert json.loads(path.read_bytes())['attempts'] == [
        earlier, {**current, 'status': 'FINISHED', 'reason': 'ACCEPTED'}]
    assert discarded == ['fixture-binding']


def test_resume_does_not_renew_exhausted_model_calls(host, monkeypatch, capsys):
    train, origin, journal = pause_implementation(host,monkeypatch,capsys,calls=1)
    manifest = Path(origin['implementation_custody'])/'attempt.json'
    identity = json.loads(manifest.read_bytes())['identity']
    explicit_resume(host)
    assert tare_ops.main(['run','work-one','--resume'])==1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status']=='IMPLEMENTATION_EXHAUSTED',receipt
    assert len(host[3].generations)==1
    assert json.loads(manifest.read_bytes())['identity']==identity
    assert not (train/'CANDIDATE_CHANGE.json').exists()


@pytest.mark.parametrize('fault',['work','target'])
def test_stale_work_or_target_refuses_before_resuming_model(host, monkeypatch, capsys, fault):
    from test_work_run import _git
    pause_implementation(host,monkeypatch,capsys)
    explicit_resume(host)
    if fault=='target':
        _git('commit','--allow-empty','-qm','moved target',cwd=host[1])
    else:
        raw=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        raw['nodes'][0]['summary']='A changed Work'
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(raw))
    assert tare_ops.main(['run','work-one','--resume'])==1
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['status']=='FAILED' and ('CHANGED' in receipt['error'] or 'MOVED' in receipt['error'])
    assert len(host[3].generations)==1


def test_real_plan_pause_is_not_a_rejected_plan(host, monkeypatch, capsys):
    from compute_plane import native_models
    from test_native_agent import Client
    client = Client('bedrock-converse',steps=[('read',{'repository':'train','path':'PACKET.md'})],
                    after_model=lambda:request_pause(host))
    monkeypatch.setattr(native_models,'NativeHttpClient',lambda *a,**kw:client)
    monkeypatch.setattr(native_work_host,'plan_audit',real_plan_audit)
    assert run(host)==0
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['status']=='PAUSED',receipt
    train=work_run.train_dir_for('work-one')
    assert json.loads((train/'STATE.json').read_bytes())['status']=='PLAN_AUDIT_PENDING'
    assert not (train/'PLAN_AUDIT_REPORT.md').exists()
    assert not list(train.glob('WORKSPACE_INTENT.*'))
    assert client.generations==1
    assert tare_ops.main(['run','work-one','--resume'])==0
    assert json.loads(capsys.readouterr().out)['status']=='PAUSED'
    assert client.generations==1


def test_real_code_audit_pause_keeps_candidate_and_no_cached_failure(host, reviewers, monkeypatch, capsys):
    from test_native_agent import Client
    from relay.work_audit import audit_custody
    post = Client.post
    requested = []
    def pause_auditor(self, path, body, control):
        reply = post(self,path,body,control)
        if path.endswith('/converse') and not requested:
            request_pause(host); requested.append(True)
        return reply
    monkeypatch.setattr(Client,'post',pause_auditor)
    assert run(host,'--auditor-profile',str(reviewers[0][0]))==0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status']=='PAUSED',receipt
    train = work_run.train_dir_for('work-one')
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    assert (train/'CANDIDATE_CHANGE.json').exists()
    assert not (audit_custody(origin)/'0/audit-result.json').exists()
    assert json.loads((train/'STATE.json').read_bytes())['status']=='AUDIT_PENDING'
    assert len(host[3].generations)==5 and reviewers[1]==['one']
    assert tare_ops.main(['run','work-one','--resume'])==0
    assert json.loads(capsys.readouterr().out)['status']=='PAUSED'
    assert len(host[3].generations)==5 and reviewers[1]==['one']
    explicit_resume(host)
    assert tare_ops.main(['run','work-one','--resume'])==0
    assert json.loads(capsys.readouterr().out)['status']=='SETTLED'
    assert len(host[3].generations)==5 and reviewers[1]==['one','one']


def test_pause_immediately_before_landing_keeps_target_until_explicit_resume(host, monkeypatch, capsys):
    from test_work_run import _git
    before = _git('rev-parse','HEAD',cwd=host[1])
    original = relay_mesh.prepare_admission_verdict
    requested = []
    def verdict(*args, **kwargs):
        result = original(*args,**kwargs)
        if not requested:
            request_pause(host); requested.append(True)
        return result
    monkeypatch.setattr(relay_mesh,'prepare_admission_verdict',verdict)
    assert run(host)==0
    assert json.loads(capsys.readouterr().out)['status']=='PAUSED'
    assert _git('rev-parse','HEAD',cwd=host[1])==before
    view = work_control.inspect('work-one',**options(host))
    with pytest.raises(ValueError,match='NO_AGENT_AT_BOUNDARY'):
        work_control.command('work-one','intervene',view['token'],'A late edit',**options(host))
    explicit_resume(host)
    assert tare_ops.main(['run','work-one','--resume'])==0
    assert json.loads(capsys.readouterr().out)['status']=='SETTLED'
    assert len(host[3].generations)==5


def test_prior_pause_never_blocks_reconciliation_of_own_completed_git_landing(host, monkeypatch, capsys):
    from test_work_resume import interrupt_after_git
    pause_implementation(host,monkeypatch,capsys)
    explicit_resume(host)
    with monkeypatch.context() as fault:
        interrupt_after_git(fault)
        with pytest.raises(SystemExit):
            tare_ops.main(['run','work-one','--resume'])
    capsys.readouterr()
    assert len(host[3].generations)==5
    assert tare_ops.main(['run','work-one','--resume'])==0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status']=='SETTLED',receipt
    assert receipt['recovery']['git_already_landed'] is True
    assert len(host[3].generations)==5

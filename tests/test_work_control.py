"""Phase A owner controls: real trains, no provider execution."""
import json

import pytest
import tare_ops
from relay import work_control as control, work_run, relay_mesh
from relay.work_change import work_locks
from test_work_resume import host, run
from test_work_run import os_root, _git


@pytest.fixture
def selected(host, capsys):
    assert run(host, '--wait-before-implement', 'Proceed?') == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'WAITING_FOR_HUMAN'
    options = {'graph_path': relay_mesh.WORK_GRAPH_FILE, 'repository_root': host[1]}
    assert not host[3].generations
    return host, options, work_run.train_dir_for('work-one')


def view(selected):
    return control.inspect('work-one', **selected[1])


def send(selected, operation, token, note=''):
    return control.command('work-one', operation, token, note, **selected[1])


def ack(selected, request, *, safe=True, run_id=None):
    identity = request['identity']
    return control.acknowledge_pause('work-one', request['token'],
        run_id or identity['run_id'], identity['descriptor_sha256'],
        verify_safe_boundary=lambda: safe, remaining_active_seconds=12.5, **selected[1])


def test_observation_has_no_writes_and_pause_does_not_take_execution_lock(selected):
    train = selected[2]
    before = {str(p.relative_to(train)): p.read_bytes() for p in train.rglob('*') if p.is_file()}
    assert view(selected)['status'] == 'RUNNING'
    assert view(selected)['status'] == 'RUNNING'
    assert before == {str(p.relative_to(train)): p.read_bytes() for p in train.rglob('*') if p.is_file()}
    assert not (train/'work-control-custody').exists()
    with work_locks(['work-one'], graph_path=selected[1]['graph_path']):
        request = send(selected, 'pause', view(selected)['token'])
        assert request['status'] == 'PAUSE_REQUESTED'
        with pytest.raises(ValueError): ack(selected, request)
    assert not selected[0][3].generations


def test_safe_ack_resume_and_exact_last_command_replay(selected):
    initial = view(selected)
    request = send(selected, 'pause', initial['token'])
    before = (selected[2]/control.FILE).read_bytes()
    assert send(selected, 'pause', initial['token']) == request
    assert (selected[2]/control.FILE).read_bytes() == before
    with pytest.raises(ValueError, match='SAFE_PAUSE'):
        send(selected, 'resume', request['token'])
    with pytest.raises(ValueError, match='UNCERTAIN_BOUNDARY'):
        ack(selected, request, safe=None)
    with pytest.raises(ValueError, match='ACK_CHANGED'):
        ack(selected, request, run_id='0'*32)
    paused = ack(selected, request)
    assert paused['status'] == 'PAUSED'
    assert send(selected, 'pause', initial['token']) == paused
    resumed = send(selected, 'resume', paused['token'])
    assert resumed['status'] == 'RESUME_REQUESTED'
    assert resumed['record']['remaining_active_seconds'] == 12.5
    before = (selected[2]/control.FILE).read_bytes()
    assert send(selected, 'resume', paused['token']) == resumed
    assert (selected[2]/control.FILE).read_bytes() == before
    assert resumed['record']['resume_at']
    assert not selected[0][3].generations


def test_note_bound_stable_id_and_stale_replay(selected):
    custody = session_state(selected)
    paused = safe_pause(selected, custody)
    with pytest.raises(ValueError, match='NOTE_INVALID'):
        send(selected, 'intervene', paused['token'], 'é'*4097)
    note = 'é'*4096
    saved = send(selected, 'intervene', paused['token'], note)
    assert saved['record']['pending_note']['text'] == note
    assert send(selected, 'intervene', paused['token'], note) == saved
    with pytest.raises(ValueError, match='STALE_TOKEN'):
        send(selected, 'intervene', paused['token'], 'altered')
    with pytest.raises(ValueError, match='NOTE_PENDING'):
        send(selected, 'intervene', saved['token'], 'another')
    resumed = send(selected, 'resume', saved['token'])
    assert resumed['record']['pending_note'] == saved['record']['pending_note']
    with pytest.raises(ValueError, match='STALE_TOKEN'):
        send(selected, 'intervene', paused['token'], note)
    assert not selected[0][3].generations


@pytest.mark.parametrize('fault', ['project', 'graph', 'descriptor', 'work', 'target', 'origin', 'terminal', 'malformed'])
def test_scope_and_identity_changes_refuse_without_record(selected, fault, tmp_path):
    host, options, train = selected
    initial = view(selected)
    options = dict(options)
    if fault == 'project': options['repository_root'] = tmp_path
    elif fault == 'graph':
        graph = tmp_path/'other.json'; graph.write_bytes(options['graph_path'].read_bytes())
        options['graph_path'] = graph
    elif fault == 'target': _git('commit', '--allow-empty', '-qm', 'moved', cwd=host[1])
    elif fault == 'work':
        path = options['graph_path']; value = json.loads(path.read_bytes())
        value['nodes'][0]['title'] = 'changed'; path.write_text(json.dumps(value))
    else:
        path = train/('EXECUTION_DESCRIPTOR.json' if fault == 'descriptor' else
                      'STATE.json' if fault == 'terminal' else 'WORK_RUN.json')
        value = json.loads(path.read_bytes())
        if fault == 'descriptor': value['work_id'] = 'other'
        elif fault == 'origin': value['run_id'] = 'invalid'
        elif fault == 'terminal': value['status'] = 'LANDED'
        else: value = []
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError, KeyError, TypeError, AttributeError)):
        control.command('work-one', 'pause', initial['token'], **options)
    assert not (train/control.FILE).exists() and not host[3].generations


def test_unrelated_node_does_not_invalidate_token(selected):
    before = view(selected)
    path = selected[1]['graph_path']; graph = json.loads(path.read_bytes())
    other = dict(graph['nodes'][0]); other['id'] = 'unrelated'; other['title'] = 'another node'
    graph['nodes'].append(other); path.write_text(json.dumps(graph))
    assert view(selected)['token'] == before['token']
    assert send(selected, 'pause', before['token'])['status'] == 'PAUSE_REQUESTED'


def test_cli_structured_status_and_refusal(selected, capsys):
    args = ['work-control', 'work-one', '--graph', str(selected[1]['graph_path']),
            '--repository-root', str(selected[0][1])]
    assert tare_ops.main(args) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial['status'] == 'RUNNING'
    assert tare_ops.main(['work-control', 'work-one', 'pause', *args[2:], '--expected', '0'*64]) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'REFUSED'
    assert not selected[0][3].generations


@pytest.mark.parametrize('fault', ['status', 'note', 'last', 'budget'])
def test_malformed_saved_control_refuses(selected, fault):
    saved = send(selected, 'pause', view(selected)['token'])
    path = selected[2]/control.FILE
    value = json.loads(path.read_bytes())
    if fault == 'status': value['record']['status'] = 'STOPPED'
    elif fault == 'note': value['record']['pending_note'] = {'id': 'x', 'text': 'bad'}
    elif fault == 'last': value['record']['last_command'] = []
    else: value['record']['remaining_active_seconds'] = -1
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        send(selected, 'resume', saved['token'])
    assert path.read_bytes() == before and not selected[0][3].generations


def test_wrong_work_refuses(selected):
    with pytest.raises((ValueError, OSError)):
        control.inspect('another-work', **selected[1])
    assert not (selected[2]/control.FILE).exists()


def session_state(selected, *, remaining=30):
    import time
    from pathlib import Path
    from compute_plane.native_journal import save_json
    origin = json.loads((selected[2]/'WORK_RUN.json').read_bytes())
    custody = Path(origin['implementation_custody']); custody.mkdir(exist_ok=True)
    deadline = None if remaining is None else time.time() + remaining
    save_json(custody/'host.json', {'schema':'tare.tools/native-work-host/1',
        'deadline_utc':deadline, 'work_id':'work-one', 'session_id':'original',
        'limits':{'max_model_calls':6, 'max_seconds':30}})
    save_json(custody/'attempt.json', {'identity':{'session_id':'original'},
        'deadline_utc':deadline, 'attempts':[{'status':'PAUSED', 'calls':5}]})
    return custody


def host_checkpoint(selected, **kwargs):
    from tare_tools_agent_runtime import CancellationToken
    origin = json.loads((selected[2]/'WORK_RUN.json').read_bytes())
    receipt = {}
    with work_locks(['work-one'], graph_path=selected[1]['graph_path']):
        result = control.checkpoint(selected[2], origin, receipt, lambda *a,**k:None,
                                    kwargs.pop('cancellation', CancellationToken()), **kwargs)
    return result, receipt


def safe_pause(selected, custody=None):
    send(selected, 'pause', view(selected)['token'])
    allowed, receipt = host_checkpoint(selected, custody=custody,
        paused_result={'status':'PAUSED'} if custody is not None else None)
    assert allowed is False and receipt['status'] == 'PAUSED'
    return view(selected)


@pytest.mark.parametrize('remaining', [30, 0, None])
def test_overnight_pause_preserves_only_remaining_active_budget(selected, monkeypatch, remaining):
    from datetime import datetime, timezone
    custody = session_state(selected, remaining=remaining)
    paused = safe_pause(selected, custody)
    snapshots = paused['record']['deadline_snapshots']
    assert len(snapshots) == 2
    before = {p.name:json.loads(p.read_bytes()) for p in custody.glob('*.json')}
    future = 2_000_000_000.0
    monkeypatch.setattr(work_run, 'now', lambda:datetime.fromtimestamp(future, timezone.utc).isoformat())
    resumed = send(selected, 'resume', paused['token'])
    assert send(selected, 'resume', paused['token']) == resumed
    assert host_checkpoint(selected, consume_resume=True)[0] is True
    running = view(selected)
    assert running['status'] == 'RUNNING'
    for saved in snapshots:
        value = json.loads((custody/saved['file']).read_bytes())
        assert value['deadline_utc'] == (None if remaining is None else future + saved['remaining'])
        assert {k:v for k,v in value.items() if k!='deadline_utc'} == {
            k:v for k,v in before[saved['file']].items() if k!='deadline_utc'}
        if remaining == 0: assert value['deadline_utc'] == future
    after = {p.name:p.read_bytes() for p in custody.glob('*.json')}
    assert host_checkpoint(selected, consume_resume=True)[0] is True
    assert after == {p.name:p.read_bytes() for p in custody.glob('*.json')}
    assert send(selected, 'resume', paused['token']) == running
    assert not selected[0][3].generations


def test_partial_restore_is_repeatable_and_custody_change_refuses_all_writes(selected, monkeypatch):
    custody = session_state(selected)
    paused = safe_pause(selected, custody)
    send(selected, 'resume', paused['token'])
    original = control.save_json
    writes = []
    def crash(path, value):
        if path.name == 'attempt.json': raise OSError('simulated crash before second replacement')
        writes.append(path.name); original(path, value)
    with monkeypatch.context() as fault:
        fault.setattr(control, 'save_json', crash)
        with pytest.raises(OSError, match='simulated crash'):
            host_checkpoint(selected, consume_resume=True)
    assert writes == ['host.json'] and view(selected)['status'] == 'RESUME_REQUESTED'
    fixed = json.loads((custody/'host.json').read_bytes())['deadline_utc']
    attempt = json.loads((custody/'attempt.json').read_bytes())
    original(custody/'attempt.json', {**attempt, 'identity':{'session_id':'foreign'}})
    before = (custody/'host.json').read_bytes()
    with pytest.raises(ValueError, match='CUSTODY_CHANGED'):
        host_checkpoint(selected, consume_resume=True)
    assert (custody/'host.json').read_bytes() == before
    original(custody/'attempt.json', attempt)
    assert host_checkpoint(selected, consume_resume=True)[0]
    assert json.loads((custody/'host.json').read_bytes())['deadline_utc'] == fixed


def test_boundary_pause_has_no_message_receiver_and_observation_never_resumes(selected):
    paused = safe_pause(selected)
    with pytest.raises(ValueError, match='NO_AGENT_AT_BOUNDARY'):
        send(selected, 'intervene', paused['token'], 'Do something')
    assert host_checkpoint(selected)[1]['status'] == 'PAUSED'
    assert host_checkpoint(selected)[1]['status'] == 'PAUSED'
    assert not selected[0][3].generations
    send(selected, 'resume', paused['token'])
    assert host_checkpoint(selected)[1]['status'] == 'RESUME_REQUESTED'
    assert host_checkpoint(selected, consume_resume=True)[0]
    assert not selected[0][3].generations


def test_note_receiver_delivers_only_to_same_stage_and_clears_after_journal_ack(selected):
    custody = session_state(selected)
    paused = safe_pause(selected, custody)
    saved = send(selected, 'intervene', paused['token'], 'Preserve the newline.')
    origin = json.loads((selected[2]/'WORK_RUN.json').read_bytes())
    receive = control.ports(selected[2], origin, custody)['receive_message']
    assert receive(()) is None
    send(selected, 'resume', saved['token'])
    assert host_checkpoint(selected, consume_resume=True)[0]
    first = receive(())
    assert first.text == 'Preserve the newline.'
    assert receive(()) == first  # A speculative read never consumes it.
    assert control.ports(selected[2], origin, origin['plan_custody'])['receive_message'](()) is None
    assert view(selected)['record']['pending_note']
    assert receive((first.message_id,)) is None
    assert view(selected)['record']['pending_note'] is None
    assert receive((first.message_id,)) is None


def test_cancel_wins_and_uncertain_native_result_never_becomes_paused(selected):
    from tare_tools_agent_runtime import CancellationToken
    custody = session_state(selected)
    send(selected, 'pause', view(selected)['token'])
    with pytest.raises(ValueError, match='UNCERTAIN_BOUNDARY'):
        host_checkpoint(selected, custody=custody, paused_result={'status':'RECONCILIATION_REQUIRED','pending_id':'effect'})
    assert view(selected)['status'] == 'PAUSE_REQUESTED'
    cancellation = CancellationToken(); cancellation.cancel()
    allowed, receipt = host_checkpoint(selected, cancellation=cancellation, custody=custody, paused_result={'status':'PAUSED'})
    assert allowed is False and receipt['status'] == 'CANCELLED'
    assert view(selected)['status'] == 'PAUSE_REQUESTED'


@pytest.mark.parametrize('kind', ['model', 'unknown_effect', 'split_round', 'summary'])
def test_native_journal_uncertainty_cannot_be_acknowledged_as_pause(selected, kind):
    from compute_plane.native_journal import NativeJournal
    custody = session_state(selected)
    journal = NativeJournal(custody/'journal.jsonl')
    if kind == 'summary':
        journal.append({'kind':'context_summary_requested','call_id':'summary'})
        journal.append({'kind':'context_summary_interrupted','call_id':'summary'})
    else:
        journal.append({'kind':'model_requested','call_id':'model'})
        if kind != 'model':
            journal.append({'kind':'model_returned','call_id':'model',
                            'reply':{'tool_calls':[{},{}] if kind=='split_round' else [{}]}})
            journal.append({'kind':'effect_requested','effect_id':'effect'})
            journal.append({'kind':'effect_returned','result':{
                'effect_id':'effect','status':'unknown' if kind=='unknown_effect' else 'succeeded'}})
    send(selected,'pause',view(selected)['token'])
    before = (selected[2]/control.FILE).read_bytes()
    with pytest.raises(ValueError,match='UNCERTAIN_BOUNDARY'):
        host_checkpoint(selected,custody=custody,paused_result={'status':'PAUSED'})
    assert (selected[2]/control.FILE).read_bytes()==before


def test_custody_link_cannot_be_resolved_before_deadline_validation(selected):
    import os
    import subprocess
    custody = session_state(selected)
    origin = json.loads((selected[2]/'WORK_RUN.json').read_bytes())
    alias = custody.parent/'redirect'
    if os.name == 'nt':
        subprocess.run(['cmd','/c','mklink','/J',str(alias),str(custody)],
                       check=True,capture_output=True)
    else:
        alias.symlink_to(custody,target_is_directory=True)
    before = (custody/'host.json').read_bytes()
    with pytest.raises(ValueError,match='native_custody_link_forbidden'):
        control._snapshots(origin,alias)
    assert (custody/'host.json').read_bytes()==before

"""Ready-node dispatch through the existing Work/sequence owners."""
import copy
import json
from pathlib import Path

import pytest

from relay import relay_mesh, work_sequence
from test_work_sequence import sequence, save_graph, host, os_root

pytestmark = pytest.mark.verifies('SPEC-OS-WORKFLOW-IR-COMPILER-001', 'AC-05')


def start(case):
    return work_sequence.create(case['dir'], ['work-one', 'work-two'], case['repo'],
                                case['profile'], unlimited=True)


def test_an_external_block_does_not_hide_an_independent_ready_work(sequence):
    data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    external = copy.deepcopy(data['nodes'][0])
    external.update(id='external', canonical_id='external')
    data['nodes'].append(external)
    data['edges'] = [{**data['edges'][0], 'from': 'external', 'to': 'work-one'}]
    save_graph(data)
    work_sequence.create(sequence['dir'], ['work-one', 'work-two'], sequence['repo'],
                         sequence['profile'], unlimited=True)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'BLOCKED'
    by_id = {row['work_id']: row for row in result['items']}
    assert by_id['work-two']['status'] == 'SETTLED'
    assert by_id['work-one']['status'] == 'PENDING'
    assert len(sequence['calls']) == 5
    before = copy.deepcopy(result)
    assert work_sequence.execute(sequence['dir']) == before
    assert len(sequence['calls']) == 5


@pytest.mark.timeout(90)  # Two real Git-backed Works plus interruption/recovery: Windows CI reached the 30 s unit default during the second settlement.
@pytest.mark.parametrize('schedule', ['resume', 'cancel-before-resume', 'contended-resume'])
@pytest.mark.parametrize('point', ['reservation-before-dispatch', 'effect-before-receipt',
    'receipt-before-transition', 'landing-before-backlog', 'expiry-rebind', 'cancel-late-result'])
def test_fault_matrix_preserves_work_without_reexecution(sequence, monkeypatch, point, schedule):
    from compute_plane.native_journal import NativeJournal, custody_lock
    from compute_plane import native_journal
    from tare_tools_agent_runtime import CancellationToken
    from relay.cluster_coordinator import CoordinatorDB
    from relay.runtime_authority import CoordinatorAuthorityGate

    start(sequence)
    cancel = CancellationToken()
    clock = [100.0]
    recovered = []
    if point == 'expiry-rebind':
        initialize = CoordinatorDB.__init__
        def controlled_clock(self, *args, **kwargs):
            kwargs.setdefault('clock', lambda: clock[0])
            initialize(self, *args, **kwargs)
        monkeypatch.setattr(CoordinatorDB, '__init__', controlled_clock)
        recover = CoordinatorAuthorityGate.recover
        def observed_recovery(self, binding, **kwargs):
            result = recover(self, binding, **kwargs)
            if result:
                recovered.append((binding.binding_id, binding.token != result.token))
            return result
        monkeypatch.setattr(CoordinatorAuthorityGate, 'recover', observed_recovery)
    with monkeypatch.context() as fault:
        if point in {'reservation-before-dispatch', 'cancel-late-result'}:
            execute = work_sequence.work_run.run_work
            def interrupted(*args, **kwargs):
                if point == 'reservation-before-dispatch':
                    raise SystemExit('reserved, not entered')
                result = execute(*args, **kwargs)
                assert result['status'] == 'SETTLED'
                cancel.cancel()
                return dict(result, run_id='0'*32)
            fault.setattr(work_sequence.work_run, 'run_work', interrupted)
        elif point in {'effect-before-receipt', 'expiry-rebind'}:
            append = NativeJournal.append
            def interrupted(journal, event):
                wanted = 3 if point == 'effect-before-receipt' else 1
                hit = event['kind'] == 'effect_returned' and len(sequence['calls']) == wanted
                if hit and point == 'effect-before-receipt':
                    raise SystemExit('effect happened, receipt not written')
                result = append(journal, event)
                if hit:
                    raise SystemExit('process lost with expiring binding')
                return result
            fault.setattr(NativeJournal, 'append', interrupted)
        elif point == 'receipt-before-transition':
            save = native_journal.save_json
            def interrupted(path, value):
                result = save(path, value)
                if (Path(path) == sequence['dir']/work_sequence.FILE
                        and value['items'][0]['status'] == 'RECONCILING'):
                    raise SystemExit('receipt acknowledged, sequence transition not written')
                return result
            fault.setattr(native_journal, 'save_json', interrupted)
        else:
            from test_work_resume import interrupt_after_git
            interrupt_after_git(fault)
        if point == 'cancel-late-result':
            result = work_sequence.execute(sequence['dir'], cancellation=cancel)
            assert result['status'] == 'NEEDS_REVIEW'
            assert 'LATE_OR_REBOUND' in result['reason']
        else:
            with pytest.raises(SystemExit):
                work_sequence.execute(sequence['dir'], cancellation=cancel)
    saved = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
    assert not saved['items'][1]['attempts']
    assert saved['workflow']['revision'] == 1
    before_calls = len(sequence['calls'])
    binding_before = None
    if point == 'expiry-rebind':
        origin = json.loads((work_sequence.work_run.train_dir_for('work-one')/'WORK_RUN.json').read_bytes())
        custody = Path(origin['implementation_custody'])
        binding_before = json.loads((custody/'host.json').read_bytes())['binding']
        clock[0] += 10000
    if schedule == 'cancel-before-resume':
        stop = CancellationToken(); stop.cancel()
        assert work_sequence.execute(sequence['dir'], cancellation=stop)['status'] == 'CANCELLED'
    elif schedule == 'contended-resume':
        before = (sequence['dir']/work_sequence.FILE).read_bytes()
        with custody_lock(sequence['dir']), pytest.raises(ValueError):
            work_sequence.execute(sequence['dir'])
        assert (sequence['dir']/work_sequence.FILE).read_bytes() == before
    assert len(sequence['calls']) == before_calls
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', result
    assert result['completed'] == 2 and len(sequence['calls']) == 10
    assert len(sequence['host'][4]) == 2
    assert result['workflow_revision'] == 1  # Recovery is an attempt, not a plan revision.
    assert work_sequence.execute(sequence['dir']) == result
    assert len(sequence['calls']) == 10
    assert (sequence['repo']/'src/value.txt').read_text() == 'strict\n'
    assert (sequence['repo']/'src/second.txt').read_text() == 'strict\n'
    if binding_before:
        binding_after = json.loads((custody/'host.json').read_bytes())['binding']
        assert binding_after['binding_id'] == binding_before['binding_id']
        assert (binding_before['binding_id'], True) in recovered


def test_unknown_prior_effect_never_becomes_a_fresh_dispatch(sequence, monkeypatch):
    start(sequence)
    def unknown(*args, **kwargs):
        custody = work_sequence.work_run.receipt_path(kwargs['run_id']).parent
        custody.mkdir(parents=True)
        raise SystemExit('owner entered; no durable outcome or recovery origin')
    with monkeypatch.context() as fault:
        fault.setattr(work_sequence.work_run, 'run_work', unknown)
        with pytest.raises(SystemExit): work_sequence.execute(sequence['dir'])
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'NEEDS_REVIEW'
    assert result['frontier']['status'] == 'NEEDS_RECONCILIATION'
    assert result['completed'] == 0 and not sequence['calls']
    assert not result['items'][1]['attempts']
    assert work_sequence.execute(sequence['dir']) == result


@pytest.mark.parametrize('fault', ['digest', 'rewired', 'operator', 'schema', 'membership', 'dispatch-fence'])
def test_changed_ir_or_dispatch_identity_never_releases_work(sequence, fault):
    start(sequence)
    path = sequence['dir']/work_sequence.FILE
    state = json.loads(path.read_bytes())
    if fault == 'digest': state['workflow']['revision'] += 1
    elif fault == 'rewired':
        state['workflow']['nodes'][1]['depends_on'] = []
        state['workflow_sha256'] = work_sequence.sha256_canonical(state['workflow'])
    elif fault == 'operator': state['workflow']['nodes'][0]['operator_ref'] = 'shell.execute/v1'
    elif fault == 'schema': state['workflow']['schema'] = 'tare.tools/workflow-ir/2'
    elif fault == 'membership': state['workflow']['nodes'][1]['id'] = 'not-selected'
    else:
        state['items'][0]['status'] = 'RUNNING'
        state['items'][0]['attempts'] = [{'run_id': 'a'*32, 'fence': 'b'*64}]
    path.write_text(json.dumps(state))
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] in {'PAUSED', 'NEEDS_REVIEW'}
    assert result['completed'] == 0 and not sequence['calls']
    assert not result['items'][1]['attempts']


def test_legacy_sequence_gains_ir_without_restarting_settled_work(sequence):
    start(sequence)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED'
    path = sequence['dir']/work_sequence.FILE
    state = json.loads(path.read_bytes())
    state.pop('workflow'); state.pop('workflow_sha256'); state.pop('frontier')
    for row in state['items']:
        for attempt in row['attempts']: attempt.pop('fence', None)
    path.write_text(json.dumps(state))
    retained = copy.deepcopy(state['items'])
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED' and result['workflow_revision'] == 1
    assert result['items'] == retained and len(sequence['calls']) == 10


def test_legacy_selection_retains_its_saved_serial_order(sequence):
    data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    external = copy.deepcopy(data['nodes'][0])
    external.update(id='external', canonical_id='external')
    data['nodes'].append(external)
    data['edges'] = [{**data['edges'][0], 'from': 'external', 'to': 'work-one'}]
    save_graph(data)
    start(sequence)
    path = sequence['dir']/work_sequence.FILE
    state = json.loads(path.read_bytes())
    for field in ('workflow', 'workflow_sha256', 'workflow_order'): state.pop(field, None)
    path.write_text(json.dumps(state))
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'BLOCKED' and not sequence['calls']
    assert result['completed'] == 0
    saved = json.loads(path.read_bytes())
    assert saved['workflow_order'] == 'saved-sequence'
    assert saved['workflow']['nodes'][1]['depends_on'] == ['work-one']


@pytest.mark.parametrize('field', ['run_id', 'work_id', 'train_id', 'graph_path', 'repository_root'])
def test_late_or_rebound_result_cannot_release_a_descendant(sequence, monkeypatch, field):
    start(sequence)
    def old_result(key, **kwargs):
        result = {'run_id': kwargs['run_id'], 'work_id': key, 'status': 'SETTLED',
            'train_id': work_sequence.work_run.train_id_for(key),
            'graph_path': str(relay_mesh.WORK_GRAPH_FILE.resolve()),
            'repository_root': str(sequence['repo'].resolve())}
        result[field] = ('0'*32 if field == 'run_id' else str(sequence['host'][0]/'other')
                         if field.endswith('path') or field == 'repository_root' else 'other')
        return result
    monkeypatch.setattr(work_sequence.work_run, 'run_work', old_result)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'NEEDS_REVIEW' and 'LATE_OR_REBOUND' in result['reason']
    assert result['completed'] == 0 and not result['items'][1]['attempts']
    assert not sequence['calls']

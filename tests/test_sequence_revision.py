"""Localized sequence revision with real Work settlement and durable console recovery."""
import asyncio
import copy
import json

import pytest
from compute_plane.native_journal import custody_lock, save_json
from graph_backlog.jsonutil import sha256_canonical
from relay import relay_mesh, work_run, work_sequence
from relay.native_console import WorkConsole
from textual.widgets import Input, Select, Button
from test_mutation_recovery import LocalBackend
from test_work_sequence import sequence, create, save_graph
from test_work_resume import host
from test_work_run import os_root


def change_second():
    data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    data['nodes'][1]['title'] = 'Reviewed second Work'
    save_graph(data)


def stop_after_first(case, monkeypatch):
    from tare_tools_agent_runtime import CancellationToken
    token = CancellationToken()
    original = work_run.run_work
    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        if result['status'] == 'SETTLED':
            token.cancel()
        return result
    with monkeypatch.context() as cut:
        cut.setattr(work_run, 'run_work', run)
        result = work_sequence.execute(case['dir'], cancellation=token)
    assert result['status'] == 'CANCELLED' and result['completed'] == 1, result
    return result


@pytest.mark.timeout(90)  # Two Git-backed Work settlements plus cancellation/review/replay; Windows CI exhausted the 30 s unit-test default.
def test_revision_after_real_settlement_preserves_results_and_only_runs_remaining(sequence, monkeypatch):
    create(sequence)
    stopped = stop_after_first(sequence, monkeypatch)
    first = copy.deepcopy(stopped['items'][0])
    graph_before = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0]
    change_second()
    assert work_sequence.execute(sequence['dir'])['status'] == 'NEEDS_REVIEW'
    before = (sequence['dir']/work_sequence.FILE).read_bytes()
    view = work_sequence.preview_revision(sequence['dir'], 'Clarified remaining work')
    assert view['retained'] == ['work-one'] and len(view['changes']) == 1
    assert (sequence['dir']/work_sequence.FILE).read_bytes() == before and len(sequence['calls']) == 5
    result = work_sequence.revise(sequence['dir'], view['sha256'], view['reason'])
    assert result['status'] == 'READY' and result['items'][0] == first
    assert len(sequence['calls']) == 5
    stored = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
    assert stored['revisions'][0]['previous_sha256'] == sha256_canonical(json.loads(before))
    assert stored['revisions'][0]['previous']['items'][0] == first
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED' and result['items'][0] == first, result
    assert len(sequence['calls']) == 10
    assert json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0] == graph_before
    assert work_sequence.execute(sequence['dir']) == result and len(sequence['calls']) == 10


@pytest.mark.parametrize('fault', ['graph', 'state', 'reason', 'lock', 'work-lock'])
def test_revision_confirmation_requires_current_preview_and_exclusive_custody(sequence, fault):
    create(sequence); change_second()
    view = work_sequence.preview_revision(sequence['dir'], 'Changed remaining Work')
    if fault == 'graph':
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph['nodes'][1]['summary'] = 'Changed again'; save_graph(graph)
    if fault == 'state':
        state = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
        state['reason'] = 'Another observation'; save_json(sequence['dir']/work_sequence.FILE, state)
    before = (sequence['dir']/work_sequence.FILE).read_bytes()
    def confirm():
        return work_sequence.revise(sequence['dir'], view['sha256'],
                                    'Different reason' if fault == 'reason' else view['reason'])
    if fault == 'lock':
        with custody_lock(sequence['dir']), pytest.raises(ValueError): confirm()
    elif fault == 'work-lock':
        from relay.work_change import work_locks
        with work_locks(['work-two']), pytest.raises(ValueError): confirm()
    else:
        with pytest.raises(ValueError, match='CHANGED_REVIEW'): confirm()
    assert before == (sequence['dir']/work_sequence.FILE).read_bytes() and not sequence['calls']


@pytest.mark.parametrize('fault', ['completed', 'completion', 'dependencies', 'lost-result', 'outside-attempt', 'unprepared'])
def test_revision_never_relabels_existing_or_unknown_execution(sequence, monkeypatch, fault):
    create(sequence)
    if fault in {'completed', 'completion', 'dependencies'}:
        stop_after_first(sequence, monkeypatch)
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        if fault == 'completed': graph['nodes'][0]['title'] = 'Changed completed contract'
        elif fault == 'completion': graph['nodes'][0]['completion'].update(status='NOT_DONE', dod_satisfied=False)
        else:
            graph['edges'] = [{**graph['edges'][0], 'from': 'work-two', 'to': 'work-one'}]
        save_graph(graph)
    elif fault == 'lost-result':
        state = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
        state['status'] = 'RUNNING'; state['items'][0]['status'] = 'RUNNING'
        state['items'][0]['attempts'] = [{'started_at': 'lost'}]
        save_json(sequence['dir']/work_sequence.FILE, state)
    elif fault == 'outside-attempt':
        original = work_run.status
        monkeypatch.setattr(work_run, 'status', lambda key: {'receipt_errors': ['lost']} if key == 'work-two' else original(key))
    else:
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph['nodes'][1]['metrics']['verification_checks'] = []; save_graph(graph)
    change_second()
    before = (sequence['dir']/work_sequence.FILE).read_bytes(); calls = len(sequence['calls'])
    with pytest.raises(ValueError): work_sequence.preview_revision(sequence['dir'], 'Update')
    assert before == (sequence['dir']/work_sequence.FILE).read_bytes() and len(sequence['calls']) == calls


def test_unstarted_dependency_revision_is_reviewed_and_reordered(sequence):
    create(sequence)
    graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    graph['edges'][0].update({'from': 'work-two', 'to': 'work-one'}); save_graph(graph)
    view = work_sequence.preview_revision(sequence['dir'], 'Reverse prerequisites')
    assert [r['work_id'] for r in view['items']] == ['work-two', 'work-one']
    assert all(r['dependencies_changed'] for r in view['changes'])
    work_sequence.revise(sequence['dir'], view['sha256'], view['reason'])
    assert not sequence['calls']


def test_old_sequence_without_dependency_snapshots_accepts_contract_only_revision(sequence):
    create(sequence)
    state = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
    state.pop('work_dependencies'); save_json(sequence['dir']/work_sequence.FILE, state)
    change_second()
    view = work_sequence.preview_revision(sequence['dir'], 'Legacy selection')
    assert work_sequence.revise(sequence['dir'], view['sha256'], view['reason'])['status'] == 'READY'
    with pytest.raises(ValueError, match='No Work changes'):
        work_sequence.preview_revision(sequence['dir'], 'No change')
    assert not sequence['calls']


def test_cli_preview_confirmation_does_not_start_execution(sequence, capsys):
    create(sequence); change_second()
    args = ['--sequence', str(sequence['dir']), '--reason', 'Reviewed change']
    assert work_sequence.main([*args, '--preview-revision']) == 0
    view = json.loads(capsys.readouterr().out)
    assert work_sequence.main([*args, '--revise', '--expected-revision', view['sha256']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'READY'
    assert not sequence['calls']


def test_tui_revision_recovers_lost_checkpoint_and_preserves_reason(sequence):
    create(sequence); change_second()
    async def scenario():
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'sequence'
            app.query_one('#sequence', Input).value = str(sequence['dir'])
            app.query_one('#sequence-revision-reason', Input).value = 'Clarify remaining Work'
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision
            assert not app.query_one('#confirm-sequence-revision', Button).disabled
            original = app.checkpoint
            def lose(kind, *args, **kwargs):
                if kind == 'sequence_revised': return None
                return original(kind, *args, **kwargs)
            app.checkpoint = lose
            app.confirm_sequence_revision(); app.confirm_sequence_revision()
            while app.busy: await pilot.pause(.05)
            assert app.last_result['status'] == 'READY'
            assert app.demand_state['events'][-1]['kind'] == 'sequence_revision_requested'
            requested_digest = app.demand_state['events'][-1]['detail']['sha256']
            stored = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
            assert stored['revisions'][-1]['preview_sha256'] == requested_digest
            identity = app.demand_id
        before = (sequence['dir']/work_sequence.FILE).read_bytes()
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete()
            assert app.demand_state['events'][-1]['kind'] == 'mutation_reconciled'
            assert app.last_result['status'] == 'READY'
            assert app.selection().sequence_revision_reason == 'Clarify remaining Work'
            assert (sequence['dir']/work_sequence.FILE).read_bytes() == before
            assert not app.reviewed_sequence_revision and not sequence['calls']
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['sequence-lock', 'work-lock', 'write-failure'])
def test_tui_revision_distinguishes_prewrite_refusal_from_unknown_write(sequence, monkeypatch, fault):
    from contextlib import nullcontext
    from compute_plane import native_journal
    from relay.console_recovery import pending
    from relay.work_change import work_locks
    create(sequence); change_second()
    async def scenario():
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'sequence'
            app.query_one('#sequence', Input).value = str(sequence['dir'])
            app.query_one('#sequence-revision-reason', Input).value = 'Reviewed remaining Work'
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision
            before = (sequence['dir']/work_sequence.FILE).read_bytes()
            original = native_journal.save_json
            def fail_save(path, value):
                if path == sequence['dir']/work_sequence.FILE:
                    raise OSError('Disk write outcome unavailable')
                return original(path, value)
            if fault == 'write-failure':
                monkeypatch.setattr(native_journal, 'save_json', fail_save)
            lock = (custody_lock(sequence['dir']) if fault == 'sequence-lock' else
                    work_locks(['work-two']) if fault == 'work-lock' else nullcontext())
            with lock:
                app.confirm_sequence_revision()
                while app.busy: await pilot.pause(.05)
            assert (sequence['dir']/work_sequence.FILE).read_bytes() == before
            assert not sequence['calls']
            if fault == 'write-failure':
                assert pending(app.demand_state)
                await app.reconcile_demand().wait()
                assert app.demand_state['events'][-1]['detail']['state'] == 'UNKNOWN'
                assert app.last_result is None
            else:
                assert app.last_result['status'] == 'REFUSED'
                assert pending(app.demand_state) is None
                await app.review_sequence_revision().wait()
                assert app.reviewed_sequence_revision
    asyncio.run(scenario())


def test_tui_changed_reason_invalidates_preview(sequence):
    create(sequence); change_second()
    async def scenario():
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'sequence'
            app.query_one('#sequence', Input).value = str(sequence['dir'])
            app.query_one('#sequence-revision-reason', Input).value = 'Original reason'
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision
            app.query_one('#sequence-revision-reason', Input).value = 'Different reason'
            app.confirm_sequence_revision()
            assert not app.busy and not app.reviewed_sequence_revision
            assert not any(e['kind'] == 'sequence_revision_requested' for e in app.demand_state['events'])
    asyncio.run(scenario())


def test_tui_graph_changed_after_review_records_refusal_and_allows_new_preview(sequence):
    from relay.console_recovery import pending
    create(sequence); change_second()
    async def scenario():
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'sequence'
            app.query_one('#sequence', Input).value = str(sequence['dir'])
            app.query_one('#sequence-revision-reason', Input).value = 'Updated work'
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision
            graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
            graph['nodes'][1]['title'] = 'Updated again'; save_graph(graph)
            before = (sequence['dir']/work_sequence.FILE).read_bytes()
            app.confirm_sequence_revision()
            while app.busy: await pilot.pause(.05)
            assert app.last_result['status'] == 'REFUSED'
            assert pending(app.demand_state) is None
            assert (sequence['dir']/work_sequence.FILE).read_bytes() == before
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision
            assert not sequence['calls']
    asyncio.run(scenario())

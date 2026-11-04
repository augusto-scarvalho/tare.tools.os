import asyncio
from dataclasses import asdict
import json
import sys

import pytest
from compute_plane.native_journal import custody_lock, save_json
from relay import console_demand, console_mutation, console_recovery, relay_mesh
from relay import native_plan, work_intake, work_prepare, work_change, human_wait
from relay.console_backend import Backend, Selection
from relay.native_console import WorkConsole
from textual.widgets import Select, Input, TextArea
from test_source_admission import admission, git
from test_work_prepare import proposal, preview as preparation_preview
from test_native_plan import proposal as graph_proposal
from test_work_change import completed, preview as change_preview
from test_work_resume import host
from test_work_run import os_root
from test_human_wait import pause


class LocalBackend(Backend):
    def catalog(self, root):
        return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}],
                'sessions': [], 'works': [], 'errors': []}
    async def launch(self, *args):
        pytest.fail('Recovery must not launch a flow or model.')


class LostCheckpoint(WorkConsole):
    def checkpoint(self, kind, *args, **kwargs):
        if kind in console_mutation.OPERATIONS.values():
            return None
        return super().checkpoint(kind, *args, **kwargs)


def bind(monkeypatch, root, control):
    if relay_mesh.RELAY_ROOT != control:
        monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(control/'test-custody'))


async def reopen(root, identity, expected, *, work_id, linked=()):
    app = WorkConsole(root, backend=LocalBackend())
    async with app.run_test(size=(140, 52)) as pilot:
        await pilot.pause(); await app.list_demands().wait()
        app.query_one('#saved-demand', Select).value = identity
        app.open_demand(); await app.workers.wait_for_complete()
        assert app.last_result == expected
        assert app.selection().work == work_id and app.selection().mode == 'work'
        for key in linked:
            assert any(s.get('work') == key for s in app.demand_stages.values())
        assert app.query_one('#message', TextArea).text == 'Unsent note'
        assert all(getattr(app, 'reviewed_'+k) is None for k in ('plan', 'preparation', 'revision', 'decision'))
        assert app.demand_state['events'][-1]['kind'] == 'mutation_reconciled'
        revision = app.demand_revision
        await app.reconcile_demand().wait()
        assert app.demand_revision == revision and app.process is None


def test_graph_apply_recovers_links_without_adding_nodes_again(graph_proposal, monkeypatch):
    directory, graph, proposal, _ = graph_proposal
    root = directory.parent/'project'; bind(monkeypatch, root, graph.parent)
    async def scenario():
        app = LostCheckpoint(root, backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            app.query_one('#message', TextArea).load_text('Unsent note')
            await app.review_selection().wait(); app.apply_reviewed_plan()
            while app.busy: await pilot.pause(.05)
            result, identity = app.last_result, app.demand_id
            assert result['status'] == 'APPLIED'
        before = graph.read_bytes()
        monkeypatch.setattr(native_plan, 'apply', lambda *a: pytest.fail('Repeated graph mutation'))
        await reopen(root, identity, result, work_id=result['work_ids'][0], linked=result['work_ids'])
        assert graph.read_bytes() == before
    asyncio.run(scenario())


@pytest.mark.parametrize('intake', [False, True])
def test_preparation_recovers_without_pinning_or_admitting_again(proposal, monkeypatch, intake):
    root = proposal['owner_roots']['owner']; graph = proposal['work_graph']
    bind(monkeypatch, root, graph.parent)
    git(root, 'config', 'user.name', 'Test'); git(root, 'config', 'user.email', 'test@example.invalid')
    view = (work_intake.preview(graph, 'WORK-1', root, ['Preserve intent'], [sys.executable, '-m', 'pytest'])
            if intake else preparation_preview(proposal))
    async def scenario():
        app = LostCheckpoint(root, backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'WORK-1'
            app.query_one('#message', TextArea).load_text('Unsent note')
            await app.save_preparation(view).wait()
            result, identity = app.last_result, app.demand_id
            assert result['status'] == 'PREPARED'
        before = graph.read_bytes(); refs = git(root, 'for-each-ref', 'refs/tare/work-intents')
        monkeypatch.setattr(work_prepare, 'apply', lambda *a: pytest.fail('Repeated preparation'))
        monkeypatch.setattr(work_intake, '_pin', lambda *a: pytest.fail('Repeated source pin'))
        await reopen(root, identity, result, work_id='WORK-1')
        assert graph.read_bytes() == before and git(root, 'for-each-ref', 'refs/tare/work-intents') == refs
    asyncio.run(scenario())


def test_revision_recovers_historical_result_after_later_graph_change(completed, monkeypatch):
    root = completed['owner_roots']['owner']; graph = completed['work_graph']
    bind(monkeypatch, root, graph.parent); view = change_preview(completed)
    async def scenario():
        app = LostCheckpoint(root, backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'WORK-1'
            app.query_one('#message', TextArea).load_text('Unsent note')
            await app.save_revision(view).wait()
            result, identity = app.last_result, app.demand_id
            assert result['status'] == 'REVISED'
        # A completion receipt is historical; current graph state is not reasserted.
        value = json.loads(graph.read_bytes()); value['nodes'][0]['title'] = 'Later title'
        from graph_backlog.jsonutil import compute_revision_hash
        value['revision'] = compute_revision_hash(value); save_json(graph, value); before = graph.read_bytes()
        monkeypatch.setattr(work_change, 'apply', lambda *a: pytest.fail('Repeated revision'))
        await reopen(root, identity, result, work_id='WORK-1')
        assert graph.read_bytes() == before
    asyncio.run(scenario())


def test_human_answer_recovers_without_answering_or_starting_work(host, capsys, monkeypatch):
    train, view = pause(host, capsys); root = host[1]
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(train.parent/'test-custody'))
    async def scenario():
        app = LostCheckpoint(root, backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'work-one'
            app.query_one('#message', TextArea).load_text('Unsent note')
            await app.save_decision(app.selection(), view['request_sha256'], 'approve', 'Scoped').wait()
            result, identity = app.last_result, app.demand_id
            assert result['status'] == 'HUMAN_APPROVED'
        before = (train/human_wait.FILE).read_bytes()
        monkeypatch.setattr(human_wait, 'answer', lambda *a: pytest.fail('Repeated answer'))
        await reopen(root, identity, result, work_id='work-one')
        assert (train/human_wait.FILE).read_bytes() == before and not host[3].generations
    asyncio.run(scenario())


@pytest.fixture
def requested(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir(); control = tmp_path/'control'; control.mkdir()
    bind(monkeypatch, root, control)
    snapshot = {'selection': asdict(Selection(str(root), mode='work', work='x')),
        'entry_history': [], 'entry_questions': [], 'entry_proposal': None, 'entry_selection': None,
        'last_result': None}
    return console_demand.save(None, 0, root, control, 'preparation_requested', snapshot,
        {'sha256': 'a'*64, 'mutation_id': 'b'*32})


@pytest.mark.parametrize('fault', ['before_owner', 'after_owner', 'final_receipt'])
def test_interrupted_mutation_never_becomes_completion_or_replays(requested, monkeypatch, fault):
    effects = []; save = console_mutation.save_json
    def writing(path, value):
        if (fault == 'before_owner' and value['state'] == 'RUNNING'
                or fault == 'final_receipt' and value['state'] == 'FINISHED'):
            raise OSError('disk failure')
        save(path, value)
    def action():
        effects.append('applied')
        if fault == 'after_owner': raise OSError('owner lost acknowledgement')
        return {'status': 'PREPARED'}
    monkeypatch.setattr(console_mutation, 'save_json', writing)
    with pytest.raises(OSError): console_mutation.record(requested, action)
    assert console_recovery.observe(requested)['state'] == 'UNKNOWN'
    assert effects == ([] if fault == 'before_owner' else ['applied'])
    with pytest.raises(FileExistsError): console_mutation.record(requested, action)
    assert len(effects) <= 1


@pytest.mark.parametrize('fault', ['request', 'result', 'state', 'schema', 'root', 'control', 'corrupt', 'missing'])
def test_wrong_or_incomplete_receipt_is_not_accepted(requested, fault):
    console_mutation.record(requested, lambda: {'status': 'PREPARED'})
    directory, _ = console_mutation.binding(requested, requested['events'][-1]); file = directory/'result.json'
    receipt = json.loads(file.read_bytes())
    if fault == 'request': receipt['request']['detail']['sha256'] = 'c'*64
    elif fault == 'result': receipt['result']['status'] = 'REVISED'
    elif fault == 'state': receipt['state'] = 'RUNNING'
    elif fault == 'schema': receipt['schema'] = 'unknown'
    elif fault in {'root', 'control'}: requested['control_root' if fault == 'control' else 'root'] += '-wrong'
    save_json(file, receipt)
    if fault == 'corrupt': file.write_text('{')
    if fault == 'missing': file.unlink()
    assert console_recovery.observe(requested)['state'] == 'UNKNOWN'


def test_busy_missing_and_superseded_requests(requested):
    directory, _ = console_mutation.binding(requested, requested['events'][-1])
    assert console_recovery.observe(requested)['state'] == 'UNKNOWN' and not directory.exists()
    console_mutation.record(requested, lambda: {'status': 'PREPARED'})
    with custody_lock(directory):
        assert console_recovery.observe(requested)['state'] == 'BUSY'
    assert console_recovery.observe(requested)['state'] == 'RECOVERED'
    requested['events'] += [{'kind': 'execution_requested'}, {'kind': 'execution_observed'}]
    assert console_recovery.observe(requested) is None


def test_missing_required_checkpoint_never_dispatches():
    from relay.demand_console import DemandConsole
    class Unwritable:
        def checkpoint(self, *args): raise OSError('Demand unavailable')
    async def scenario():
        with pytest.raises(OSError):
            await DemandConsole.confirmed_mutation(Unwritable(), 'preparation_requested', {},
                lambda: pytest.fail('Owner called without durable request'))
    asyncio.run(scenario())

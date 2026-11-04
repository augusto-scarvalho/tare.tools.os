import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import pytest
from textual.widgets import Input, Select, TextArea

from relay import console_demand, relay_mesh
from relay.console_backend import Selection
from relay.native_console import WorkConsole


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    return root, control


def snapshot(root):
    return {'selection': asdict(Selection(str(root), prompt='A task')), 'entry_history': [],
        'entry_questions': [], 'entry_proposal': None, 'entry_selection': None, 'last_result': None}


def test_saved_conversation_has_project_control_identity_and_rejects_lost_updates(project):
    root, control = project
    first = console_demand.save(None, 0, root, control, 'draft_saved', snapshot(root))
    second = console_demand.save(first['id'], 1, root, control, 'execution_requested', snapshot(root))
    with pytest.raises(ValueError, match='another console'):
        console_demand.save(first['id'], 1, root, control, 'draft_saved', snapshot(root))
    assert console_demand.read(first['id'], root, control) == second
    for wrong_root, wrong_control in [(root.parent, control), (root, control.parent)]:
        with pytest.raises(ValueError, match='another project'):
            console_demand.read(first['id'], wrong_root, wrong_control)
        assert console_demand.inventory(wrong_root, wrong_control)[0] == []
    assert 'completion is not recorded' in console_demand.guidance(second)


def test_unreadable_record_visible_and_invalid_identity_refused(project):
    root, control = project
    value = console_demand.save(None, 0, root, control, 'draft_saved', snapshot(root))
    console_demand.path(value['id']).write_text('{')
    rows, errors = console_demand.inventory(root, control)
    assert not rows and errors
    with pytest.raises(ValueError): console_demand.path('../escape')


def test_backlog_checkpoint_uses_rollback_compatible_display_without_changing_live_activity(project):
    from copy import deepcopy
    from relay.native_activity import activity_view
    root, control = project
    current = snapshot(root)
    current['assistant_history'] = [
        {'kind': 'tool-activity', 'id': str(i), 'operation': operation,
         'state': 'succeeded', 'target': 'flow-08', 'receipt': '7 receipt', 'detail': 'Recorded'}
        for i, operation in enumerate(('backlog_read', 'backlog_record'))]
    before = deepcopy(current)
    saved = console_demand.save(None, 0, root, control, 'draft_saved', current)
    history = console_demand.read(saved['id'], root, control)['snapshot']['assistant_history']
    assert [row['operation'] for row in history] == ['read', 'edit']
    assert [row['target'] for row in history] == ['backlog_read: flow-08', 'backlog_record: flow-08']
    assert all(activity_view(row)['receipt'] == '7 receipt' for row in history)
    assert current == before
    repeated = console_demand.save(saved['id'], 1, root, control, 'draft_saved', saved['snapshot'])
    assert repeated['snapshot']['assistant_history'] == history


class Backend:
    def __init__(self): self.calls = []
    def catalog(self, root):
        return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}], 'sessions': [], 'works': [], 'errors': []}
    def propose_entry(self, selection, cancellation, history, questions=()):
        self.calls.append((selection.prompt, history, questions))
        return {'status': 'NEEDS_INPUT', 'flow': 'clarify', 'reason': 'Need scope', 'question': 'Which component?',
            'directory': 'saved-entry', 'messages': [*history, selection.prompt], 'root': selection.root}


async def open_saved(app, identity):
    await app.list_demands().wait()
    app.query_one('#saved-demand', Select).value = identity
    app.open_demand()


def test_reopen_clarification_keeps_speakers_and_no_interpretation_until_answer(project):
    root, _ = project
    async def scenario():
        backend = Backend(); app = WorkConsole(root, backend=backend, initial_mode='conversation')
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#prompt', TextArea).load_text('Plan the change')
            app.begin_entry()
            while app.busy: await pilot.pause(.05)
            identity = app.demand_id
            assert identity and app.entry_questions == ('Which component?',)
        second = WorkConsole(root, backend=backend, initial_mode='conversation')
        async with second.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await open_saved(second, identity)
            assert len(backend.calls) == 1 and second.process is None
            assert second.entry_history == ('Plan the change',)
            assert second.entry_questions == ('Which component?',)
            second.query_one('#prompt', TextArea).load_text('Only the console')
            second.begin_entry()
            while second.busy: await pilot.pause(.05)
            assert backend.calls[-1] == ('Only the console', ('Plan the change',), ('Which component?',))
    asyncio.run(scenario())


def test_saved_suggestion_cannot_be_reauthorized_by_saving_changed_inputs(project):
    root, _ = project
    async def scenario():
        app = WorkConsole(root, backend=Backend(), initial_mode='conversation')
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#prompt', TextArea).load_text('/explore inspect')
            selection = app.selection()
            app.entry_proposal = (selection, {'status': 'PROPOSED', 'flow': 'explore', 'demand': 'inspect', 'root': str(root)})
            app.query_one('#prompt', TextArea).load_text('Different demand')
            app.save_demand(); identity = app.demand_id
        reopened = WorkConsole(root, backend=Backend(), initial_mode='conversation')
        async with reopened.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await open_saved(reopened, identity)
            reopened.start_suggested()
            assert reopened.entry_proposal is None and reopened.process is None and not reopened.busy
    asyncio.run(scenario())


def test_plan_to_multiple_works_reopens_any_stage_without_restoring_approvals(project):
    root, _ = project
    async def scenario():
        app = WorkConsole(root, backend=Backend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(root.parent/'plan')
            app.checkpoint('execution_observed', {'status': 'REVIEWED'})
            app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'WORK-A'
            app.checkpoint('graph_applied', {'work_ids': ['WORK-A', 'WORK-B']})
            identity = app.demand_id
        second = WorkConsole(root, backend=Backend())
        async with second.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await open_saved(second, identity)
            assert second.selection().work == 'WORK-A'
            second.query_one('#demand-stage', Select).value = '1:WORK-B'
            second.open_demand_stage()
            assert second.selection().work == 'WORK-B'
            assert second.reviewed_preparation is None and second.reviewed_plan is None
            assert not second.busy and second.process is None
            second.new_demand()
            assert second.demand_id is None and second.selection().mode == 'conversation'
    asyncio.run(scenario())

import asyncio
import json
import os
from pathlib import Path

import pytest
from textual.widgets import Input, Select, TextArea

from relay import console_demand, native_installation, relay_mesh
from relay.console_backend import Backend
from relay.native_console import WorkConsole
from test_native_console import FakeBackend, Process


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    projects = tmp_path/'projects'; projects.mkdir()
    project = projects/'one'; project.mkdir()
    preferred = projects/'.state'
    fallback = tmp_path/'user-state'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(preferred))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    return project, preferred, fallback


def test_external_history_selection_and_invalid_workspace_are_non_destructive(workspace):
    project, preferred, fallback = workspace
    choose = native_installation.workspace_storage
    assert choose(project, preferred, fallback) == preferred
    assert choose(project.parent, preferred, fallback) == fallback
    with pytest.raises(ValueError, match='contains its history'):
        choose(project.parent.parent, preferred, fallback)
    with pytest.raises(ValueError, match='unavailable'):
        choose(project/'missing', preferred, fallback)
    assert not preferred.exists() and not fallback.exists()


def test_switch_parent_and_back_preserves_old_history_and_clears_execution_context(workspace):
    project, preferred, fallback = workspace
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant',
                          storage_preferred=preferred, storage_fallback=fallback)
        async with app.run_test(size=(130, 48)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt', TextArea).load_text('Original project conversation')
            app.save_demand(); identity = app.demand_id
            old_file = console_demand.path(identity); old_bytes = old_file.read_bytes()
            app.assistant_session = 'old-session'
            app.reviewed_demand_sequence = ('old-approval',)
            app.query_one('#work', Select).set_options([('Old Work', 'WORK-OLD')])
            app.query_one('#work', Select).value = 'WORK-OLD'
            await pilot.pause()
            app.query_one('#root', Input).value = str(project.parent)
            app.query_one('#prompt', TextArea).load_text('Inspect all projects')
            await app.action_reload().wait()
            assert not app.assistant_session and app.reviewed_demand_sequence is None
            assert not app.demand_id and not app.selection().work and not app.selection().session
            assert app.selection().prompt == 'Inspect all projects'
            assert Path(os.environ['TARE_STORAGE_ROOT']) == fallback
            app.save_demand()
            assert app.demand_id != identity and app.demand_state['root'] == str(project.parent)
            assert old_file.read_bytes() == old_bytes
            app.query_one('#root', Input).value = str(project)
            await app.list_demands().wait()
            assert Path(os.environ['TARE_STORAGE_ROOT']) == preferred
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand()
            assert app.demand_id == identity and app.selection().prompt == 'Original project conversation'
            app.query_one('#root', Input).value = str(project/'missing')
            app.save_demand()
            assert app.demand_id == identity and old_file.read_bytes() == old_bytes
            assert Path(os.environ['TARE_STORAGE_ROOT']) == preferred
    asyncio.run(scenario())


class AssistantBackend(FakeBackend):
    propose_entry = Backend.propose_entry

    async def launch(self, args, root):
        self.process = Process()
        session = Path(args[args.index('--session')+1])
        session.mkdir(parents=True, exist_ok=True)
        (session/'native-agent.json').write_text(json.dumps({'config': {'repositories': {'repo': root}}}))
        self.process.done.set()
        return await super().launch(args, root)


def test_composer_resize_during_partial_unmount(workspace):
    project, preferred, _ = workspace
    async def scenario():
        backend = AssistantBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=preferred)
        async with app.run_test(size=(130, 48)):
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            chips = app.query_one('#attachment-chips')
            slash = app.query_one('#slash-box')
            app.query_one('#context-box').display = False
            prompt.load_text('one\ntwo\nthree')
            slash.display = False
            app.resize_composer()
            assert prompt.styles.height.value == 3
            assert not chips.has_class('completion-open')

            app.screen.set_class(True, 'compact')
            slash.display = True
            app.resize_composer()
            assert prompt.styles.height.value == 1
            assert chips.has_class('completion-open')
            slash.display = False
            app.resize_composer()
            assert prompt.styles.height.value == 3
            assert not chips.has_class('completion-open')

            await chips.remove()
            assert app.is_mounted
            assert app.query_one_optional('#prompt') is prompt
            assert app.query_one_optional('#mode') is not None
            assert app.query_one_optional('#attachment-chips') is None
            prompt.styles.height = 6
            app.resize_composer()
            assert prompt.styles.height.value == 6
            assert backend.launches == []
    asyncio.run(scenario())


def test_assistant_sends_directly_resumes_and_reopens_without_graph(workspace, monkeypatch):
    project, preferred, fallback = workspace
    def forbidden(*a, **k):
        pytest.fail('Ordinary conversation called the flow interpreter')
    async def scenario():
        backend = AssistantBackend(); backend.propose_entry = forbidden
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=preferred)
        async with app.run_test(size=(130, 48)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt', TextArea).load_text('Veja os arquivos desta pasta')
            app.execute(False)
            await app.workers.wait_for_complete()
            assert len(backend.launches) == 1
            args = backend.launches[0]
            instruction = args[args.index('--instruction')+1]
            assert 'tare.tools' in instruction and instruction.endswith('Veja os arquivos desta pasta')
            assert '--root' in args and args[0] == 'agent'
            session, identity = app.assistant_session, app.demand_id
            assert session and identity and not (project/'work-graph.json').exists()
            app.query_one('#prompt', TextArea).load_text('Agora explique o segundo arquivo')
            app.execute(False)
            await app.workers.wait_for_complete()
            args = backend.launches[-1]
            assert '--resume' in args and args[args.index('--session')+1] == session
            assert args[args.index('--follow-up')+1] == 'Agora explique o segundo arquivo'
            assert app.demand_id == identity and app.selection().mode == 'assistant'
        reopened = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=preferred)
        async with reopened.run_test(size=(130, 48)) as pilot:
            await reopened.workers.wait_for_complete()
            await reopened.list_demands().wait()
            reopened.query_one('#saved-demand', Select).value = identity
            reopened.open_demand()
            await reopened.workers.wait_for_complete()
            assert reopened.assistant_session == session and reopened.selection().mode == 'assistant'
            assert any('Veja os arquivos desta pasta' in text for text in reopened.assistant_history)
            assert any(isinstance(entry, dict) and entry.get('status') == 'CANCELLED'
                       for entry in reopened.assistant_history)
            assert len(backend.launches) == 2
            reopened.query_one('#prompt', TextArea).load_text('Continue')
            reopened.execute(False)
            await reopened.workers.wait_for_complete()
            assert backend.launches[-1][backend.launches[-1].index('--session')+1] == session
            reopened.query_one('#root', Input).value = str(project.parent)
            reopened.storage_fallback = fallback
            reopened.query_one('#prompt', TextArea).load_text('Veja todos os projetos')
            reopened.execute(False)
            await reopened.workers.wait_for_complete()
            assert '--resume' not in backend.launches[-1]
            assert reopened.assistant_session != session
    asyncio.run(scenario())


def test_assistant_slash_completion_and_explicit_flow_remain_optional(workspace):
    from test_console_slash import edit, shown
    project, preferred, _ = workspace
    async def scenario():
        backend = AssistantBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=preferred)
        async with app.run_test(size=(130, 48)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            await edit(pilot, prompt, '/pl organize files', (0, 3))
            assert shown(app) == ['/plan']
            await pilot.press('tab')
            assert prompt.text == '/plan organize files'
            assert not backend.launches and not app.demand_id
            app.execute(False)
            await app.workers.wait_for_complete()
            assert app.entry_proposal[1]['flow'] == 'plan' and not backend.launches
            assert app.assistant_session is None
    asyncio.run(scenario())


@pytest.mark.parametrize('pending', ['execution', 'message'])
def test_busy_workspace_switch_is_rejected(workspace, pending):
    project, preferred, fallback = workspace
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant',
                          storage_preferred=preferred, storage_fallback=fallback)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.controls(pending == 'execution')
            app.sending_message = pending == 'message'
            app.query_one('#root', Input).value = str(project.parent)
            with pytest.raises(ValueError, match='current operation'):
                app.prepare_workspace()
            assert app.workspace_root == str(project)
            assert Path(os.environ['TARE_STORAGE_ROOT']) == preferred
            app.sending_message = False
            app.controls(False)
    asyncio.run(scenario())


def test_a_queued_work_selection_does_not_come_back_after_a_workspace_change(workspace):
    project, preferred, fallback = workspace
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant',
                          storage_preferred=preferred, storage_fallback=fallback)
        async with app.run_test(size=(130, 48)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#work', Select).set_options([('Old Work', 'WORK-OLD')])
            app.query_one('#work', Select).value = 'WORK-OLD'  # its Changed event is still queued (a slow host)
            app.query_one('#root', Input).value = str(project.parent)
            await app.action_reload().wait()
            await pilot.pause()
            assert not app.selection().work
    asyncio.run(scenario())

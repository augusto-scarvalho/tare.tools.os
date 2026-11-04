import asyncio

import pytest
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea

from relay import console_demand, console_entry, relay_mesh
from relay.native_console import WorkConsole
from test_console_slash import RecordingBackend, edit, settle, shown


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_assistant_completion_preserves_draft_and_visible_actions(tmp_path, size, theme):
    async def scenario():
        app = WorkConsole(tmp_path, backend=RecordingBackend(), initial_mode='assistant')
        checkpoints = []
        app.checkpoint = lambda *args, **kwargs: checkpoints.append(args)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.theme = theme
            app.assistant_say('You: Explain the parser and check the tests.')
            app.assistant_say('Assistant: The parser preserves the original input.\n\nThe next step is to check empty values.')
            app.context_root, app.context_paths = str(tmp_path), ['notes.md']
            app.refresh_context_chips()
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            draft = '/\n'+'Keep this draft intact.\n'*8
            await edit(pilot, prompt, draft, (0, 1))
            names = [row[0] for row in console_entry.commands_for('assistant')]
            assert shown(app) == names
            await pilot.press('down', 'down')
            await settle(pilot)
            description = app.query_one('#slash-description', Static)
            assert 'continue this task with another provider' in str(description.render())
            assert app.query_one('#slash-menu', OptionList).highlighted == names.index('/model')
            for name in ('slash-menu', 'slash-description', 'slash-hint', 'prompt', 'start', 'composer-hint', 'context-chips'):
                widget = app.query_one('#'+name)
                assert app.query_one('#composer').scrollable_content_region.contains_region(widget.region), (name, widget.region)
                assert app.screen.region.contains_region(widget.region), name
            assert prompt.text == draft and app.focused is prompt
            assert app.query_one('#slash-menu').max_scroll_x == 0
            await pilot.press('escape')
            await settle(pilot)
            assert not shown(app) and prompt.text == draft and prompt.size.height > 1
            await edit(pilot, prompt, '/mod', (0, 4))
            await pilot.press('enter')
            await settle(pilot)
            assert prompt.text == '/model' and app.panel != 'workspace'
            app.controls(True)
            await settle(pilot)
            assert str(app.query_one('#start', Button).label) == 'Queue'
            assert 'queue' in str(app.query_one('#composer-hint', Static).render())
            assert 'Queue' in app.query_one('#start').render_line(0).text
            assert app.focused is prompt and not prompt.disabled
            chip = app.query_one('#context-chips Button', Button)
            assert not chip.disabled
            await pilot.click(chip); await settle(pilot)
            assert not app.context_paths and prompt.text == '/model'
            app.controls(False)
            assert not app.backend.launches and not app.backend.proposals and not checkpoints
    asyncio.run(scenario())


def test_commands_open_existing_controls_and_preserve_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=RecordingBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            app.assistant_say('You: Keep this conversation')
            app.context_root, app.context_paths = str(root), ['notes.md']
            app.checkpoint('draft_saved')
            identity = app.demand_id
            original_model = app.query_one('#model', Select).value
            await edit(pilot, prompt, '/model', (0, 6))
            await pilot.press('ctrl+enter'); await settle(pilot)
            from relay.model_picker import ModelPicker
            assert isinstance(app.screen, ModelPicker) and app.focused.id == 'model-search'
            assert app.demand_id == identity and app.context_paths == ['notes.md']
            assert app.query_one('#model', Select).value == original_model and not prompt.text
            await pilot.press('escape'); await settle(pilot)
            await edit(pilot, prompt, '/sessions', (0, 9))
            await pilot.press('ctrl+enter')
            await app.workers.wait_for_complete(); await settle(pilot)
            assert app.panel == 'navigation' and app.focused.id == 'conversation-search'
            assert any(row['id'] == identity for row in app.navigation_rows)
            app.close_navigation()
            await edit(pilot, prompt, '/new', (0, 4))
            await pilot.press('ctrl+enter'); await settle(pilot)
            assert app.demand_id is None and not prompt.text and not app.context_paths
            saved = console_demand.read(identity, str(root), relay_mesh.RELAY_ROOT)
            assert saved['snapshot']['selection']['prompt'] == ''
            assert saved['snapshot']['selection']['context_paths'] == ['notes.md']
            assert not app.backend.launches and not app.backend.proposals
    asyncio.run(scenario())


def test_command_refusals_keep_draft_and_never_queue_literal_commands(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=RecordingBackend(), initial_mode='assistant')
        queued = []
        app.send_operator_message = lambda: queued.append(True)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            for command in ('/new unexpected', '/model\nextra', '/diff', '/terminal'):
                await edit(pilot, prompt, command, (0, 1))
                app.execute(False)
                assert prompt.text == command
            app.controls(True)
            for command in ('/new', '/model', '/diff', '/sessions'):
                prompt.load_text(command); app.execute(False)
                assert prompt.text == command
            app.controls(False)
            app.query_one('#root', Input).value = str(tmp_path)
            prompt.load_text('/new'); app.execute(False)
            assert prompt.text == '/new'
            app.query_one('#root', Input).value = str(root)
            app.assistant_say('You: Preserve me if saving fails')
            def fail(*args, **kwargs):
                raise OSError('Checkpoint unavailable')
            monkeypatch.setattr(app, 'checkpoint', fail)
            app.execute(False)
            assert prompt.text == '/new' and app.assistant_history
            assert not queued and not app.backend.launches and not app.backend.proposals
    asyncio.run(scenario())


def test_diff_and_terminal_commands_open_bound_panels_without_execution(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from relay.changes_panel import ChangesPanel
    from relay.terminal_panel import TerminalPanel

    calls = []
    def read(panel, **kwargs):
        calls.append((panel.session, panel.project))
        return dict(turns=[], edits=[], checks=[], unresolved_effects=False,
                    changed_since_observation=False, unavailable_files=[])
    monkeypatch.setattr(ChangesPanel, 'read', read)

    async def scenario():
        backend = RecordingBackend()
        custody = []
        def terminals(session):
            custody.append(session)
            return SimpleNamespace(title='This conversation', list=lambda: [])
        backend.terminals = terminals
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            app.assistant_session = str(tmp_path/'saved-session')
            app.context_root, app.context_paths = str(tmp_path), ['notes.md']
            await edit(pilot, prompt, '/diff', (0, 5))
            await pilot.press('ctrl+enter'); await settle(pilot)
            assert isinstance(app.screen, ChangesPanel)
            assert calls == [(tmp_path/'saved-session', tmp_path)]
            assert not prompt.text and app.context_paths == ['notes.md']
            await pilot.press('escape'); await settle(pilot)
            await edit(pilot, prompt, '/terminal', (0, 9))
            app.controls(True)
            await pilot.press('ctrl+enter'); await settle(pilot)
            assert isinstance(app.screen, TerminalPanel)
            assert custody == [str(tmp_path/'saved-session')]
            assert not prompt.text and app.context_paths == ['notes.md']
            await pilot.press('f4'); await settle(pilot)
            app.controls(False)
            assert not backend.launches and not backend.proposals
    asyncio.run(scenario())

import asyncio
import threading

import pytest
from textual.widgets import Input, OptionList, TextArea

from relay import console_demand, relay_mesh
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_search_reopen_and_new_preserve_drafts_without_inference(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    (root/'notes.md').write_text('Notes')

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Investigate the parser')
            app.assistant_say('You: Explain the parser')
            app.checkpoint('draft_saved')
            parser_id = app.demand_id
            app.new_demand()
            prompt.load_text('Unsent documentation draft')
            app.context_root, app.context_paths = str(root), ['notes.md']
            await pilot.click('#toolbar-conversations')
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.panel == 'navigation'
            drawer = app.query_one('#navigation')
            assert drawer.region.bottom <= size[1]
            assert app.query_one('#conversation-close').region.bottom <= size[1]
            if size[0] < 100:
                assert not app.query_one('#detail').display
            else:
                assert app.query_one('#detail').region.x >= drawer.region.right
            search = app.query_one('#conversation-search', Input)
            search.value = 'parser'; await pilot.pause()
            assert [r['id'] for r in app.navigation_shown] == [parser_id]
            menu = app.query_one('#conversation-list', OptionList)
            visible = ''.join(menu.render_line(i).text for i in range(menu.size.height))
            assert app.navigation_shown[0]['updated_at'][:10] in visible
            await pilot.press('ctrl+enter'); await pilot.pause()
            assert not app.backend.launches
            if size[0] >= 100:
                with monkeypatch.context() as patch:
                    submitted = []
                    patch.setattr(app, 'action_start', lambda: submitted.append(True))
                    prompt.focus(); await pilot.press('ctrl+enter'); await pilot.pause()
                    assert submitted == [True]  # The visible composer still works beside the drawer.
            search.focus(); await pilot.press('enter')
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.demand_id == parser_id
            assert prompt.text == 'Investigate the parser'
            assert app.panel != 'navigation'
            assert app.focused is prompt
            assert len(app.query_one('#output').items) == 1  # No raw checkpoint events in chat.
            rows, errors = console_demand.inventory(str(root), relay_mesh.RELAY_ROOT)
            assert not errors and len(rows) == 2
            draft_id = next(r['id'] for r in rows if r['id'] != parser_id)
            saved = console_demand.read(draft_id, str(root), relay_mesh.RELAY_ROOT)
            assert saved['snapshot']['selection']['context_paths'] == ['notes.md']
            await pilot.press('f1'); await app.workers.wait_for_complete()
            search.value = 'documentation'; await pilot.pause()
            if size[0] >= 100:
                await pilot.click('#conversation-list', offset=(2, 1))
            else:
                await pilot.press('down', 'enter')
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.demand_id == draft_id and app.context_paths == ['notes.md']
            assert prompt.text == 'Unsent documentation draft'
            await pilot.press('f1'); await app.workers.wait_for_complete()
            await pilot.click('#conversation-new'); await pilot.pause()
            assert app.demand_id is None and not prompt.text and not app.context_paths
            prompt.load_text('A fresh unsent draft')
            app.new_conversation()
            assert app.demand_id is None and not prompt.text
            rows, errors = console_demand.inventory(str(root), relay_mesh.RELAY_ROOT)
            assert not errors and any(r['title'] == 'A fresh unsent draft' for r in rows)
            await pilot.press('f2'); await pilot.pause()
            assert app.screen.region.contains_region(app.query_one('#settings-close').region)
            if size[0] < 100:
                assert not app.query_one('#detail').display
                assert app.query_one('#selection').region.width == size[0]
                assert app.query_one('#selection').max_scroll_x == 0
                await pilot.press('ctrl+enter'); await pilot.pause()
            await pilot.click('#settings-close'); await pilot.pause()
            assert app.panel != 'workspace' and app.focused is prompt
            prompt.load_text('Preserve the draft from Settings too')
            await pilot.press('f2'); await pilot.pause()
            await pilot.click('#new-demand'); await pilot.pause()
            assert app.demand_id is None and not prompt.text
            assert app.panel != 'workspace'
            rows, errors = console_demand.inventory(str(root), relay_mesh.RELAY_ROOT)
            assert not errors and any(r['title'] == 'Preserve the draft from Settings too' for r in rows)
            assert not app.backend.launches
    asyncio.run(scenario())


def test_navigation_rejects_pending_workspace_busy_and_failed_draft_save(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'one'; root.mkdir()
    other = tmp_path/'two'; other.mkdir()

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Saved destination'); app.checkpoint('draft_saved')
            app.new_demand(); prompt.load_text('Keep this unsent draft')
            await pilot.press('f1'); await app.workers.wait_for_complete()
            app.query_one('#root', Input).value = str(other)
            app.open_conversation(0)
            assert app.demand_id is None and app.workspace_root == str(root)
            app.query_one('#root', Input).value = str(root)
            app.controls(True)
            app.open_conversation(0)
            assert app.demand_id is None
            await pilot.press('escape'); await pilot.pause()
            assert app.panel != 'navigation' and app.busy
            app.controls(False)
            app.sending_message = True; app.open_conversation(0)
            assert app.demand_id is None
            app.sending_message = False
            def fail(*args, **kwargs):
                raise OSError('Cannot write checkpoint')
            monkeypatch.setattr(app, 'checkpoint', fail)
            app.open_conversation(0)
            assert app.demand_id is None and prompt.text == 'Keep this unsent draft'
            assert not app.backend.launches
    asyncio.run(scenario())


def test_late_conversation_list_cannot_cross_project_or_reopen_drawer(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'one'; root.mkdir()
    other = tmp_path/'two'; other.mkdir()
    entered, release = threading.Event(), threading.Event()

    def inventory(project, control):
        if project == str(root):
            entered.set()
            assert release.wait(10)
            return [{'id': '1'*32, 'title': 'Old project', 'updated_at': '2026-09-15'}], []
        return [], []
    monkeypatch.setattr(console_demand, 'inventory', inventory)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_conversations()
            try:
                for _ in range(50):
                    await pilot.pause(.02)
                    if entered.is_set(): break
                assert entered.is_set()
                app.query_one('#root', Input).value = str(other)
                app.prepare_workspace()
                app.close_navigation()
            finally:
                release.set()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert not app.navigation_rows and not app.navigation_shown
            assert app.query_one('#conversation-list', OptionList).option_count == 0
            assert app.panel != 'navigation'
            assert not app.backend.launches
    asyncio.run(scenario())

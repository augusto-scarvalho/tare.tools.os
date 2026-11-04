import asyncio
import json

import pytest
from textual.widgets import Select, TextArea

from relay.console_appearance import read_appearance, save_appearance, SCHEMA
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@pytest.mark.parametrize('raw', [b'{', b'[]', b'x'*4097,
    json.dumps({'schema': SCHEMA, 'theme': []}).encode(),
    json.dumps({'schema': SCHEMA+'/future', 'theme': 'tare-light'}).encode()])
def test_unreadable_preference_is_preserved(tmp_path, raw):
    file = tmp_path/'appearance.json'
    file.write_bytes(raw)
    with pytest.raises(ValueError):
        read_appearance(tmp_path)
    with pytest.raises(ValueError):
        save_appearance(tmp_path, 'tare-light')
    assert file.read_bytes() == raw


def test_settings_and_palette_theme_survive_reopen_without_project_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    async def scenario():
        project = tmp_path/'project'; project.mkdir()
        history = tmp_path/'history'
        other = tmp_path/'another-project'; other.mkdir()
        def console(root=project):
            return WorkConsole(root, backend=FakeBackend(), initial_mode='assistant',
                               storage_preferred=history)
        app = console()
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.theme == 'tare-dark'
            assert not (history/'console/appearance.json').exists()
            app.query_one('#prompt', TextArea).load_text('Keep my unfinished message')
            app.controls(True)
            await pilot.press('f2'); await pilot.pause()
            choice = app.query_one('#appearance', Select)
            assert not choice.disabled
            choice.focus()
            await pilot.press('enter', 'down', 'enter'); await pilot.pause()
            assert app.theme == 'tare-light'
            assert read_appearance(history/'console') == 'tare-light'
            assert app.query_one('#prompt', TextArea).text == 'Keep my unfinished message'
            assert not app.backend.launches
            app.controls(False)
        reopened = console(other)
        async with reopened.run_test(size=(130,44)) as pilot:
            await reopened.workers.wait_for_complete(); await pilot.pause()
            assert reopened.theme == 'tare-light'
            assert reopened.query_one('#appearance', Select).value == 'tare-light'
            command = next(c for c in reopened.get_system_commands(reopened.screen)
                           if c.title == 'Appearance: High contrast')
            command.callback(); await pilot.pause()
            assert reopened.theme == 'tare-contrast'
            assert read_appearance(history/'console') == 'tare-contrast'
            assert reopened.query_one('#appearance', Select).value == 'tare-contrast'
            assert not reopened.backend.launches
        final = console()
        async with final.run_test(size=(80,24)) as pilot:
            await final.workers.wait_for_complete(); await pilot.pause()
            assert final.theme == 'tare-contrast'
        assert list(project.iterdir()) == list(other.iterdir()) == []
    asyncio.run(scenario())


def test_containing_workspace_uses_external_preference_and_save_failure_keeps_ui(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'user-history'))
    async def scenario():
        project = tmp_path/'all-projects'; project.mkdir()
        preferred = project/'history'
        fallback = tmp_path/'user-history'
        file = fallback/'console/appearance.json'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'unknown preference format')
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant',
                          storage_preferred=preferred, storage_fallback=fallback)
        warnings = []
        app.notify = lambda message, **kwargs: warnings.append(message)
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.theme == 'tare-dark'
            app.select_appearance('tare-light'); await pilot.pause()
            assert app.theme == 'tare-light'
            assert file.read_bytes() == b'unknown preference format'
            assert any('could not save appearance' in message for message in warnings)
            assert not preferred.exists()
            assert not app.backend.launches
    asyncio.run(scenario())

import asyncio
import json
import os
from pathlib import Path

import pytest
from textual.widgets import Input, Select, TextArea

from relay import console_demand, relay_mesh
from relay.console_projects import recent_projects, remember_projects
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def test_recent_projects_bound_deduplicate_and_preserve_corrupt_file(tmp_path):
    prefs = tmp_path/'preferences'
    paths = [str(tmp_path/str(i)) for i in range(45)]
    remember_projects(prefs, paths)
    assert recent_projects(prefs) == paths[:40]
    remember_projects(prefs, [paths[2], paths[2]])
    assert recent_projects(prefs) == [paths[2], *paths[:2], *paths[3:40]]
    file = prefs/'recent-projects.json'
    file.write_text('{broken')
    with pytest.raises(Exception):
        remember_projects(prefs, [paths[0]])
    assert file.read_text() == '{broken'


@pytest.mark.parametrize('size', [(80,24), (130,44)])
def test_switch_saves_original_draft_and_reopens_after_restart(tmp_path, monkeypatch, size):
    projects = tmp_path/'projects'; projects.mkdir()
    one = projects/'one'; one.mkdir()
    (one/'notes.txt').write_text('Project context')
    preferred = projects/'.state'; fallback = tmp_path/'fallback'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(preferred))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(one, backend=backend, initial_mode='assistant',
                          storage_preferred=preferred, storage_fallback=fallback)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt',TextArea)
            prompt.load_text('Unsent draft for ONE only')
            app.context_root, app.context_paths = str(one), ['notes.txt']
            app.query_one('#message',TextArea).load_text('Unsent follow-up')
            app.assistant_say('Original answer')
            app.checkpoint('draft_saved')
            identity = app.demand_id
            prompt.load_text('Edited draft for ONE only')
            assert app.open_project(str(projects), app.project_navigation_scope())
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.workspace_root == str(projects) and not app.demand_id
            assert not app.assistant_session and not prompt.text and not app.context_paths
            assert not app.query_one('#message',TextArea).text and not app.assistant_history
            assert Path(os.environ['TARE_STORAGE_ROOT']) == fallback
            oldfile = preferred/'demands'/identity/'conversation.json'
            saved = json.loads(oldfile.read_text())
            assert saved['root'] == str(one)
            assert saved['snapshot']['selection']['root'] == str(one)
            assert saved['snapshot']['selection']['prompt'] == 'Edited draft for ONE only'
            assert saved['snapshot']['selection']['context_paths'] == ['notes.txt']
            assert saved['snapshot']['message_draft'] == 'Unsent follow-up'
            assert recent_projects(fallback/'console')[:2] == [str(projects),str(one)]
            assert not backend.launches
        # Restart under the new root, then switch back and reopen the saved draft.
        reopened = WorkConsole(projects, backend=backend, initial_mode='assistant',
                               storage_preferred=preferred, storage_fallback=fallback)
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete()
            assert reopened.open_project(str(one), reopened.project_navigation_scope(), recent_projects(fallback/'console'))
            await reopened.workers.wait_for_complete()
            await reopened.list_demands().wait()
            reopened.query_one('#saved-demand',Select).value = identity
            reopened.open_demand()
            await reopened.workers.wait_for_complete(); await pilot.pause()
            assert reopened.demand_id == identity
            assert reopened.selection().prompt == 'Edited draft for ONE only'
            assert reopened.context_paths == ['notes.txt']
            assert reopened.query_one('#message',TextArea).text == 'Unsent follow-up'
            assert not backend.launches
    asyncio.run(scenario())


def test_failed_or_stale_switch_keeps_draft_scope_and_context(tmp_path, monkeypatch):
    one=tmp_path/'one'; one.mkdir(); two=tmp_path/'two'; two.mkdir()
    state=tmp_path/'state'; monkeypatch.setenv('TARE_STORAGE_ROOT',str(state))
    async def scenario():
        app=WorkConsole(one, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            prompt=app.query_one('#prompt',TextArea); prompt.load_text('Keep this draft')
            app.assistant_say('Keep this answer')
            scope=app.project_navigation_scope()
            assert not app.open_project(str(two/'missing'),scope)
            assert not app.open_project(str(tmp_path),scope) # Contains active history.
            app.sending_message=True
            assert not app.open_project(str(two),scope)
            app.sending_message=False
            app.checkpoint('draft_saved')
            assert not app.open_project(str(two),scope) # Revision/identity changed.
            with monkeypatch.context() as patch:
                def fail(*args,**kwargs): raise OSError('Disk write failed')
                patch.setattr(app,'checkpoint',fail)
                assert not app.open_project(str(two),app.project_navigation_scope())
            assert app.workspace_root == str(one) and app.selection().root == str(one)
            assert prompt.text == 'Keep this draft' and app.assistant_history == ['Keep this answer']
            assert Path(os.environ['TARE_STORAGE_ROOT']) == state
            assert not app.backend.launches
    asyncio.run(scenario())


def test_projects_command_and_keyboard_preserve_draft_on_cancel(tmp_path,monkeypatch):
    from relay.project_picker import ProjectPicker
    one=tmp_path/'one'; one.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    async def scenario():
        app=WorkConsole(one,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            prompt=app.query_one('#prompt',TextArea);prompt.load_text('Keep while browsing')
            await pilot.press('f5');await pilot.pause()
            assert isinstance(app.screen,ProjectPicker)
            await pilot.press('ctrl+enter');await pilot.pause()
            assert not app.backend.launches
            await pilot.press('escape');await pilot.pause()
            assert prompt.text == 'Keep while browsing' and app.workspace_root == str(one)
            assert not app.demand_id
            prompt.load_text('/project')
            assert app.dispatch_conversation_command('/project','')
            await pilot.pause();assert isinstance(app.screen,ProjectPicker)
            await pilot.press('escape');await pilot.pause()
            assert not prompt.text and not app.backend.launches
    asyncio.run(scenario())

import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest
from textual.widgets import Input, Select, Static

from relay import console_header
from relay.console_header import HeaderDetails, git_branch, read_details
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def descriptor(directory, root, model='saved-model'):
    directory.mkdir(exist_ok=True)
    (directory/'native-agent.json').write_text(json.dumps({
        'schema': 'tare.tools/native-agent/2', 'config': {'repositories': {'repo': str(root)}},
        'deployment': {'native_profile': {'model': model}}}))


def test_saved_model_is_project_bound_and_never_replaced_by_selected_profile(tmp_path, monkeypatch):
    session = tmp_path/'session'; descriptor(session, tmp_path)
    def forbidden(*a, **k):
        pytest.fail('A saved session consulted a new model preference')
    monkeypatch.setattr('relay.native_catalog.select', forbidden)
    key = (str(tmp_path), str(session), 'other-profile.json', 'other-model', True)
    assert read_details(key).model == 'saved-model'
    descriptor(session, tmp_path/'elsewhere')
    assert read_details(key).model == 'Saved model unavailable'
    (session/'native-agent.json').write_text('{broken')
    assert read_details(key).model == 'Saved model unavailable'
    assert read_details((str(tmp_path), '', '', 'other-model', True)).model == 'Auto-select on start'


def test_git_branch_supports_unborn_detached_changed_branch_and_non_repository(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], stderr=subprocess.STDOUT)
    assert git_branch(str(tmp_path)) == 'No Git branch'
    git('init', '-b', 'initial')
    assert git_branch(str(tmp_path)) == 'initial'
    git('-c', 'user.name=Header Test', '-c', 'user.email=header@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-m', 'fixture')
    git('checkout', '-b', 'changed')
    assert git_branch(str(tmp_path)) == 'changed'
    git('checkout', '--detach')
    assert git_branch(str(tmp_path)).startswith('detached ')


def test_header_uses_absolute_host_git_and_disables_project_monitor(tmp_path, monkeypatch):
    project, host = tmp_path/'project', tmp_path/'host'
    project.mkdir(); host.mkdir()
    name = 'git.exe' if os.name == 'nt' else 'git'
    for directory in [project, host]:
        path = directory/name
        path.write_bytes(b'fixture program; not executed')
        path.chmod(0o755)
    monkeypatch.chdir(project)
    monkeypatch.setenv('PATH', os.pathsep.join(['.', str(host)]))
    seen = []
    def observe(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'host-branch\n', '')
    monkeypatch.setattr(console_header.subprocess, 'run', observe)
    assert git_branch(str(project)) == 'host-branch'
    assert seen[0][0] == str(host/name)
    assert ['-c', 'core.fsmonitor=false'] == seen[0][1:3]


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_header_reopens_saved_model_and_preserves_actual_status(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    session = tmp_path/'session'; descriptor(session, tmp_path, 'actual-saved-model')
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.assistant_session = str(session); app.update_mode()
            await app.workers.wait_for_complete(); await pilot.pause()
            context = app.query_one('#session-context', Static)
            assert 'actual-saved-model' in str(context.render())
            assert 'Connection unchecked' in str(context.render())
            app.query_one('#profile', Input).value = 'different-model.json'
            await pilot.pause(); await app.workers.wait_for_complete()
            assert 'actual-saved-model' in str(context.render())
            app.controls(True); app.set_status('Tool: run tests · 3s')
            assert 'Tool: run tests' in str(app.query_one('#status', Static).render())
            assert app.screen.region.contains_region(context.region)
            assert app.query_one('#start').region.bottom <= size[1]
            app.controls(False)
            app.new_demand(); await app.workers.wait_for_complete()
            assert str(app.query_one('#status', Static).render()) == 'Ready'
            assert 'actual-saved-model' not in str(context.render())
            assert not app.backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('ready', [True, False, None])
def test_header_waits_for_the_running_session_descriptor(tmp_path, monkeypatch, ready):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    session = tmp_path/'preparing'

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.controls(True)
            app.assistant_session = str(session)
            app.update_mode()
            await pilot.pause(.2)
            context = app.query_one('#session-context', Static)
            assert 'Reading saved model' in str(context.render())
            assert 'unavailable' not in str(context.render())
            if ready:
                descriptor(session, tmp_path, 'prepared-model')
            elif ready is False:
                app.controls(False)  # a failed launch must stop waiting, not hide the missing profile
            await asyncio.wait_for(app.workers.wait_for_complete(), timeout=5)
            await pilot.pause()
            expected = ('prepared-model' if ready else
                        'Saved model unavailable' if ready is False else 'Reading saved model')
            assert expected in str(context.render())
            app.controls(False)
            if ready is None:
                app.sync_header(force=True)
                await app.workers.wait_for_complete()
                assert 'Saved model unavailable' in str(context.render())
            assert not app.backend.launches
    asyncio.run(scenario())


def test_late_header_observation_cannot_label_a_new_project(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    first = tmp_path/'first'; first.mkdir()
    second = tmp_path/'second'; second.mkdir()
    entered, release = threading.Event(), threading.Event()
    def read(key):
        if key[0] == str(first):
            entered.set(); assert release.wait(10)
            return HeaderDetails('old-branch', 'old-model', 'Saved model')
        return HeaderDetails('new-branch', 'new-model', 'Next execution')
    monkeypatch.setattr(console_header, 'read_details', read)
    async def scenario():
        app = WorkConsole(first, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await pilot.pause(.01)
                assert entered.is_set()
                app.query_one('#root', Input).value = str(second)
                app.prepare_workspace()
                assert 'old-model' not in str(app.query_one('#session-context', Static).render())
            finally:
                release.set()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert 'new-model' in str(app.query_one('#session-context', Static).render())
            assert 'new-branch' in str(app.query_one('#flow-heading', Static).render())
            assert not app.backend.launches
    asyncio.run(scenario())

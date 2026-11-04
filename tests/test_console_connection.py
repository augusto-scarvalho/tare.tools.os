"""Real GET-only endpoint observations, project binding and responsive TUI checks."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from tare_tools_agent_runtime import CancellationToken
from textual.widgets import Button, Input, Static, TextArea
from relay.console_connection import probe, target_for
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@contextmanager
def endpoint():
    state = SimpleNamespace(value={'data': [{'id': 'fixture-model'}]}, code=200,
                            entered=Event(), release=None, requests=[])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            state.requests.append((self.command, self.path))
            raw = json.dumps(state.value).encode(); state.entered.set()
            if state.release is not None: state.release.wait(10)
            self.send_response(state.code); self.send_header('Content-Length', str(len(raw)))
            try:
                self.end_headers(); self.wfile.write(raw)
            except OSError: pass
        def do_POST(self):
            state.requests.append((self.command, self.path)); self.send_error(500)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state.url = f'http://127.0.0.1:{server.server_port}/v1'
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield state
    finally:
        if state.release is not None: state.release.set()
        server.shutdown(); server.server_close(); thread.join(3)


def profile(path, url, *, fleet=False, model='fixture-model'):
    value = {'id': 'fixture', 'runner': 'native-http', 'native_profile': {
        'protocol': 'openai-chat', 'model': model, 'endpoint': url,
        'source': 'fixture', 'context_window': 8192, 'output_tokens': 1024}}
    if fleet: value['native_counter_protocol'] = 'slop-fleet-v1'
    path.write_text(json.dumps(value))
    return value


def key(root, path):
    return (str(root), '', str(path), 'unused', False)


@pytest.mark.parametrize('case, expected', [
    ('listed', 'Model listed'), ('missing', 'Model not listed'), ('invalid', 'Check failed'),
    ('healthy', 'Model healthy'), ('loading', 'Backend not ready'), ('other', 'Gateway reached'),
    ('401', 'Access refused'), ('404', 'Check unavailable'), ('429', 'Rate limited'), ('503', 'Check failed')])
def test_real_connection_observation_does_not_generate_or_switch(tmp_path, case, expected):
    with endpoint() as state:
        fleet = case in {'healthy', 'loading', 'other'}
        if fleet:
            state.value = {'role': 'qualified-model-gateway', 'status': 'ok', 'backend_healthy': case != 'loading',
                           'current_model': 'another' if case == 'other' else 'fixture-model', 'requested_name': None}
        elif case == 'missing': state.value = {'data': [{'id': 'another'}]}
        elif case == 'invalid': state.value = {'secret': 'private-response-body'}
        elif case.isdecimal(): state.code = int(case); state.value = {'secret': 'private-response-body'}
        path = tmp_path/'profile.json'; profile(path, state.url, fleet=fleet)
        result = probe(key(tmp_path, path), CancellationToken())
        assert result.status == expected
        assert result.identity == target_for(key(tmp_path, path)).identity
        assert result.transport['method'] == 'GET' and result.transport['cleanup_verified']
        assert state.requests == [('GET', '/v1/fleet/status' if fleet else '/v1/models')]
        assert 'private-response-body' not in repr(result)
        assert 'stale' in result.summary(result.monotonic+61)


def test_saved_profile_wins_and_wrong_project_auto_and_protocol_make_no_requests(tmp_path):
    with endpoint() as state:
        path = tmp_path/'profile.json'; value = profile(path, state.url)
        session = tmp_path/'session'; session.mkdir()
        saved = {'schema': 'tare.tools/native-agent/2', 'config': {'repositories': {'project': str(tmp_path)}}, 'deployment': value}
        (session/'native-agent.json').write_text(json.dumps(saved))
        selected = (str(tmp_path), str(session), 'nonexistent.json', 'another', True)
        assert probe(selected, CancellationToken()).status == 'Model listed'
        assert probe((str(tmp_path/'other'), *selected[1:]), CancellationToken()).status == 'Profile unavailable'
        assert probe((str(tmp_path), '', '', 'auto', True), CancellationToken()).status == 'Model not selected'
        value['native_profile']['protocol'] = 'bedrock-converse'
        path.write_text(json.dumps(value))
        assert probe(key(tmp_path, path), CancellationToken()).status == 'Check unavailable'
        assert state.requests == [('GET', '/v1/models')]


def test_missing_credential_never_contacts_endpoint_and_timeout_finishes_cleanup(tmp_path, monkeypatch):
    with endpoint() as state:
        path = tmp_path/'profile.json'; value = profile(path, state.url)
        value['native_credential_env'] = 'TARE_CONNECTION_TEST_CREDENTIAL'
        monkeypatch.delenv('TARE_CONNECTION_TEST_CREDENTIAL', raising=False)
        monkeypatch.setattr('relay.native_generation.vault_secret', lambda name: None)
        path.write_text(json.dumps(value))
        result = probe(key(tmp_path, path), CancellationToken())
        assert result.status == 'Credential unavailable' and not state.requests
        profile(path, state.url)
        state.release = Event()
        result = probe(key(tmp_path, path), CancellationToken(), timeout=.5)
        assert result.status == 'Check timed out'
        assert result.transport['cleanup_verified']
        assert all(method == 'GET' for method, _ in state.requests)


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('saved', [False, True])
def test_tui_explicit_check_keeps_draft_execution_and_profile_binding(tmp_path, monkeypatch, size, saved):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    async def scenario(state):
        project = tmp_path/'project'; project.mkdir()
        path = tmp_path/'profile.json'; deployment = profile(path, state.url, fleet=True)
        state.value = {'role':'qualified-model-gateway', 'status':'ok', 'backend_healthy':True,
                       'current_model':'fixture-model', 'requested_name':None}
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            app.query_one('#profile', Input).value = str(path)
            if saved:
                session = tmp_path/'session'; session.mkdir()
                descriptor = {'schema':'tare.tools/native-agent/2',
                    'config':{'repositories':{'project':str(project)}}, 'deployment':deployment}
                (session/'native-agent.json').write_text(json.dumps(descriptor))
                app.assistant_session = str(session)
                app.query_one('#profile', Input).value = 'unavailable-next-profile.json'
                app.update_mode()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert not state.requests  # Mount, settings and model labels do not probe.
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Keep my unfinished draft')
            app.controls(True); app.set_status('Tool running · preserve this status')
            await pilot.click('#connection-check'); await app.workers.wait_for_complete(); await pilot.pause()
            assert app.connection_observation.status == 'Model healthy'
            assert 'Model healthy' in str(app.query_one('#session-context', Static).render())
            assert app.busy and prompt.text == 'Keep my unfinished draft'
            assert 'Tool running' in str(app.query_one('#status', Static).render())
            assert not app.backend.launches
            assert 'Check' in app.query_one('#connection-check', Button).render_line(0).text
            app.connection_observation = replace(app.connection_observation,
                                                monotonic=app.connection_observation.monotonic-70)
            app.sync_header(); assert 'stale' in str(app.query_one('#session-context', Static).render())
            if saved:
                descriptor['deployment']['native_profile']['model'] = 'new-profile-model'
                (session/'native-agent.json').write_text(json.dumps(descriptor))
            else:
                profile(path, state.url, fleet=True, model='new-profile-model')
            app.sync_header(force=True); await app.workers.wait_for_complete(); await pilot.pause()
            assert app.connection_observation is None
            assert 'Connection unchecked' in str(app.query_one('#session-context', Static).render())
            assert len(state.requests) == 1
            app.controls(False)
    with endpoint() as state: asyncio.run(scenario(state))


@pytest.mark.parametrize('action', ['cancel', 'project', 'profile', 'close'])
def test_late_or_cancelled_check_cannot_publish_for_another_source(tmp_path, monkeypatch, action):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    async def scenario(state):
        project = tmp_path/'project'; project.mkdir()
        other = tmp_path/'other'; other.mkdir()
        path = tmp_path/'profile.json'; profile(path, state.url)
        state.release = Event()
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            app.query_one('#profile', Input).value = str(path)
            await app.workers.wait_for_complete(); await pilot.pause()
            app.action_check_connection()
            for _ in range(100):
                if state.entered.is_set(): break
                await pilot.pause(.02)
            assert state.entered.is_set()
            assert 'Cancel check' in app.query_one('#connection-check', Button).render_line(0).text
            if action == 'cancel': app.action_check_connection()
            elif action == 'project':
                app.query_one('#root', Input).value = str(other); app.prepare_workspace()
            elif action == 'profile':
                profile(path, state.url, model='changed-model'); state.release.set()
            if action != 'close':
                await app.workers.wait_for_complete(); await pilot.pause()
                if action == 'cancel': assert app.connection_observation.status == 'Check cancelled'
                else: assert app.connection_observation is None
            assert not app.backend.launches
        assert app.connection_task is None
        assert len(state.requests) == 1
    with endpoint() as state: asyncio.run(scenario(state))


def test_profile_refresh_requested_during_metadata_read_is_not_lost(tmp_path, monkeypatch):
    from relay import console_header
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    entered, release = Event(), Event()
    original = console_header.read_details
    def delayed(key):
        result = original(key)
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return result
    async def scenario(state):
        project = tmp_path/'project'; project.mkdir()
        path = tmp_path/'profile.json'; profile(path, state.url)
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            app.query_one('#profile', Input).value = str(path)
            await app.workers.wait_for_complete(); await pilot.pause()
            app.action_check_connection()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.connection_observation.status == 'Model listed'
            monkeypatch.setattr(console_header, 'read_details', delayed)
            try:
                app.sync_header(force=True)
                for _ in range(100):
                    if entered.is_set(): break
                    await pilot.pause(.02)
                assert entered.is_set()
                profile(path, state.url, model='changed-during-read')
                app.sync_header(force=True)
                assert app.header_refresh_pending
            finally:
                release.set()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.header_details.model == 'changed-during-read'
            assert app.connection_observation is None
            assert len(state.requests) == 1
    with endpoint() as state: asyncio.run(scenario(state))

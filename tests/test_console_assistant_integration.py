import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import pytest

from textual.widgets import Checkbox, Input, TextArea

from relay import native_console, native_installation, relay_mesh
from relay.console_backend import Backend
from relay.native_console import WorkConsole
from test_native_agent import Client, deployment, steps


def test_default_cli_opens_assistant_without_a_control_graph(tmp_path, monkeypatch):
    import tare_ops
    seen = []
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(WorkConsole, 'run', lambda self: seen.append(self))
    monkeypatch.setattr(tare_ops, 'main', lambda argv: native_console.main(argv[1:]))
    monkeypatch.setenv('TARE_OS_ROOT', str(tmp_path/'no-graph'))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    project = tmp_path/'projects'; project.mkdir()
    original_root = relay_mesh.RELAY_ROOT
    try:
        assert native_installation.main(['console', '--root', str(project)]) == 0
    finally:
        relay_mesh.rebind_root(original_root)
    assert seen[0].initial_mode == 'assistant' and not seen[0].initial_guided
    assert seen[0].root == str(project)


def test_installed_catalog_needs_no_os_graph_but_reports_corruption(tmp_path, monkeypatch):
    from relay import native_sessions, native_catalog
    graph = tmp_path/'work-graph.json'
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', graph)
    monkeypatch.setattr(native_sessions, 'inventory', lambda root: {'unavailable': [], 'sessions': []})
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [])
    catalog = Backend().catalog(str(tmp_path))
    assert catalog['errors'] == [] and catalog['works'] == []
    graph.write_text('{broken')
    assert 'Work graph unavailable' in Backend().catalog(str(tmp_path))['errors'][0]


@pytest.mark.parametrize('newline',[b'\n',b'\r\n'])
def test_saved_legacy_tui_process_keeps_edits_and_followups_without_git_or_graph(tmp_path, monkeypatch,newline):
    """An existing direct conversation keeps its tools when opened by the newer TUI.

    New coordinated chats have separate all-role tests and real ConPTY dogfood.
    """
    project = tmp_path/'projects'; project.mkdir()
    source = b'value = 1'+newline
    (project/'a.py').write_bytes(source)
    file_steps = [(name, {**args, 'repository': project.name}) for name, args in steps()[:2]]
    for name,args in file_steps:
        if name=='edit': args['sha256']=hashlib.sha256(source).hexdigest()
    class ConversationClient(Client):
        def post(self, path, body, control):
            raw = super().post(path, body, control)
            for choice in raw.get('choices', []):
                message = choice['message']
                calls = message.get('tool_calls', [])
                if calls and calls[0]['function']['name'] == 'tare_deliver':
                    message['content'] = json.loads(calls[0]['function']['arguments'])['summary']
                    del message['tool_calls']
                    choice['finish_reason'] = 'stop'
            return raw

    fixture = ConversationClient(steps=[*file_steps, ('tare_deliver', {'summary': 'Updated a.py.'}),
                           ('tare_deliver', {'summary': 'The value is now 2.'})])
    requests=[]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            raw = json.dumps(fixture.post(self.path, body, None)).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    profile = tmp_path/'profile.json'
    profile.write_text(json.dumps(deployment(endpoint=f'http://127.0.0.1:{server.server_port}')))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'no-control-graph')
    from relay import native_agent
    from relay.console_entry import ASSISTANT_CONTEXT
    legacy = tmp_path/'state/agent-sessions/legacy'
    initial = Client(steps=[('tare_deliver', {'summary': 'Legacy conversation ready.'})])
    initial.endpoint = f'http://127.0.0.1:{server.server_port}'
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
        'repositories': {project.name: str(project)}, 'writable': {project.name: ['a.py']},
        'verifiers': {}, 'capabilities': ['read', 'list', 'search', 'edit'], 'specgraph_edit_context': False}
    seed = native_agent.run_session(config, json.loads(profile.read_bytes()), legacy, ASSISTANT_CONTEXT+'An existing direct session.',
        accept_messages=True, accept_attachments=True, client=initial, local_counter=initial)
    assert seed['status'] == 'PROPOSED'
    diagnostics = []
    class CapturingBackend(Backend):
        async def launch(self, args, root):
            process = await super().launch(args, root)
            reader = process.stderr
            class Tee:
                async def read(self, count):
                    data = await reader.read(count)
                    diagnostics.append(data.decode('utf-8', errors='replace'))
                    return data
            process.stderr = Tee()
            return process

    async def scenario():
        app = WorkConsole(project, backend=CapturingBackend(), initial_mode='assistant',
                          storage_preferred=tmp_path/'state')
        async with app.run_test(size=(130, 48)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#profile', Input).value = str(profile)
            app.query_one('#streaming', Checkbox).value = False  # This fixture serves non-streaming JSON.
            app.assistant_session = str(legacy)
            app.set_pending_permissions({'mode': 'edit', 'write_paths': ['a.py']}, project)
            app.context_root=str(project)
            app.context_paths=['a.py']
            app.query_one('#prompt', TextArea).load_text('Read the selected file and change value to 2.')
            app.execute(False)
            await app.workers.wait_for_complete()
            from textual.widgets import Static
            assert app.last_result and app.last_result['status'] == 'PROPOSED', (
                app.last_result, str(app.query_one('#status', Static).render()), ''.join(diagnostics)[-1800:])
            assert (project/'a.py').read_bytes() == b'value = 2'+newline
            assert fixture.generations == 3
            assert 'User-selected paths' in json.dumps(requests[0]) and 'a.py' in json.dumps(requests[0])
            assert app.context_paths==[]
            assert not any('--resume --interactive' in text for text in app.assistant_history)
            session = app.assistant_session
            demand = app.demand_id
            app.new_conversation()
            assert app.assistant_session is None
            await pilot.pause()
            app.action_conversations()
            await app.workers.wait_for_complete()
            for _ in range(40):
                await pilot.pause(.05)
                if any(row['id'] == demand for row in app.navigation_shown):
                    break
            app.open_conversation(next(i for i, row in enumerate(app.navigation_shown) if row['id'] == demand))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.assistant_session == session and fixture.generations == 3
            assert (project/'a.py').read_bytes() == b'value = 2'+newline
            app.query_one('#prompt', TextArea).load_text('Explain the change.')
            app.context_root=str(project)
            app.context_paths=['a.py']
            app.execute(False)
            await app.workers.wait_for_complete()
            assert app.last_result['status'] == 'PROPOSED' and fixture.generations == 4
            assert any(m.get('content') == 'Updated a.py.' for m in requests[-1]['messages'])
            assert not any('Continue the task' in str(m) for m in requests[-1]['messages'])
            assert 'references only' in json.dumps(requests[-1])
            assert app.assistant_session == session
            assert not (project/'.git').exists() and not (project/'work-graph.json').exists()
            from relay.native_review import review
            state=json.loads((Path(session)/'native-agent.json').read_text())
            _, first, second=state['invocations']
            assert 0 <= first['journal_start'] < first['journal_end'] <= second['journal_start'] < second['journal_end']
            assert len(review(Path(session),project=project,invocation_id=first['id'])['edits'])==1
            assert review(Path(session),project=project,invocation_id=second['id'])['edits']==[]
            import pytest
            with pytest.raises(ValueError,match='another project'):
                review(Path(session),project=tmp_path/'elsewhere')
            from relay.changes_panel import ChangesPanel
            from textual.widgets import Static
            app.query_one('#prompt',TextArea).load_text('Keep this draft unsent.')
            app.action_changes()
            assert isinstance(app.screen,ChangesPanel)
            for _ in range(60):
                await pilot.pause(.05)
                widget=app.screen.query_one('#changes-diff',Static)
                if '+value = 2' in str(getattr(widget,'content',widget.render())): break
            assert '+value = 2' in str(getattr(widget,'content',widget.render()))
            await pilot.press('ctrl+enter','f2'); await pilot.pause()
            assert not app.busy and fixture.generations==4
            await pilot.press('f3'); await pilot.pause()
            assert not isinstance(app.screen,ChangesPanel)
            assert app.query_one('#prompt',TextArea).text=='Keep this draft unsent.'
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_native_sse_is_visible_in_tui_before_tool_delivery_and_keeps_draft(tmp_path, monkeypatch):
    import native_stream_fixture as wire
    from relay.console_output import Transcript
    original = wire.chat_parts
    monkeypatch.setattr(wire, 'chat_parts', lambda tool=False: [original(False)[0], *original(True)])
    project = tmp_path/'project'; project.mkdir()
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'no-control-graph')
    with wire.serving(tool=True, gate=True) as (url, requests, release, first):
        profile = tmp_path/'profile.json'
        fixture_profile = deployment(endpoint=url)
        # The explicit UTF-8 upper bound also includes native tool schemas.
        fixture_profile['native_profile']['context_window'] = 131072
        fixture_profile['native_counter_protocol'] = 'utf8-envelope-estimate-v1'
        profile.write_text(json.dumps(fixture_profile))
        async def scenario():
            app = WorkConsole(project, initial_mode='assistant')
            async with app.run_test(size=(100, 36)) as pilot:
                await app.workers.wait_for_complete()
                app.query_one('#profile', Input).value = str(profile)
                prompt = app.query_one('#prompt', TextArea)
                prompt.load_text('Give a greeting.'); app.execute(False)
                output = app.query_one('#output', Transcript)
                # Include Windows subprocess startup while the server gates delivery.
                deadline = asyncio.get_running_loop().time() + 15
                while asyncio.get_running_loop().time() < deadline:
                    await pilot.pause(.05)
                    if output.live_text:
                        break
                # A slow subprocess start and an early agent failure look alike here: say which it was.
                assert first.is_set() and output.live_text == 'olá ', (
                    'agent process', getattr(app.process, 'returncode', 'not started'), 'busy', app.busy,
                    'result', app.last_result)
                assert app.busy and app.last_result is None and app.process.returncode is None
                prompt.load_text('My next unsent question')
                release.set()
                await app.workers.wait_for_complete()
                assert app.last_result['status'] == 'PROPOSED', app.last_result
                assert app.last_result['streaming'] is True
                assert prompt.text == 'My next unsent question'
                assert len(requests) == 1 and requests[0][1]['stream'] is True
        asyncio.run(scenario())

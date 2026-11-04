"""Real TUI/CLI queue, cancellation and restart, using a controlled HTTP model."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread

import pytest

from textual.widgets import Checkbox, Input, TextArea

from relay import native_messages, relay_mesh
from relay.console_backend import Backend
from relay.native_console import WorkConsole
from test_native_agent import deployment, response


async def until(pilot, predicate, app, seconds=25):
    deadline = asyncio.get_running_loop().time()+seconds
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await pilot.pause(.05)
    assert predicate(), (app.last_result, str(app.query_one('#status').render()))


@pytest.mark.parametrize('size', [(80,24),(130,44)])
@pytest.mark.timeout(90)  # Two real agent processes, cancellation and TUI restart; retain each step's existing bounds.
def test_saved_direct_chat_steer_cancel_restart_resume_keeps_edits_and_unsent_draft(tmp_path, monkeypatch, size):
    project = tmp_path/'project'; project.mkdir()
    (project/'a.py').write_bytes(b'value = 1\n')
    (project/'note.txt').write_text('Keep this user note.\n')
    (project/'check.py').write_text("from pathlib import Path\nassert Path('a.py').read_bytes() == b'value = 3\\n'\nassert Path('note.txt').read_text() == 'Keep this user note.\\n'\nprint('Checks passed')\n")
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None,[str(Path(__file__).resolve().parents[1]),os.environ.get('PYTHONPATH')])))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    first, first_release, stop, stop_release = (Event() for _ in range(4))
    requests, errors = [], []
    first_text = 'Use value 3 instead, and preserve note.txt.'
    second_text = 'Do not edit again; verify and report the result.'
    draft = 'Unsent draft: explain another file later.'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/apply-template': value = {'prompt': 'fixture'}
                elif self.path == '/tokenize': value = {'tokens': list(range(100))}
                else:
                    requests.append(body); number = len(requests)
                    arguments = {'repository': project.name, 'path': 'a.py'}
                    if number == 1: tool = 'read'
                    elif number in (2, 4):
                        before, after = (1, 2) if number == 2 else (2, 3)
                        if number == 4: assert first_text in json.dumps(body)
                        tool = 'edit'; arguments.update(sha256=hashlib.sha256(f'value = {before}\n'.encode()).hexdigest(),
                            search=f'value = {before}', replace=f'value = {after}')
                    elif number == 3:
                        first.set(); assert first_release.wait(35)
                        tool = 'read'
                    elif number == 5:
                        stop.set(); assert stop_release.wait(35)
                        tool, arguments = 'tare_deliver', {'summary': 'This cancelled response must not be consumed.'}
                    elif number == 6:
                        assert second_text in json.dumps(body)
                        assert draft not in json.dumps(body)
                        tool, arguments = 'verify', {'name': 'check'}
                    elif number == 7:
                        tool, arguments = 'tare_deliver', {'summary': 'Value is 3; verification passed and the user note is unchanged.'}
                    else: raise AssertionError('Unexpected repeated model request')
                    value = response('openai-chat', tool, arguments)
                raw = json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # The fifth request is deliberately cancelled by the client.
            except Exception as exc:
                errors.append(repr(exc)); self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    profile = tmp_path/'profile.json'
    profile.write_text(json.dumps(deployment(endpoint=f'http://127.0.0.1:{server.server_port}')))
    # A saved direct chat keeps the tools it began with. New coordinated chats
    # exercise their separate role/effect gates in test_conversation_tasks.
    from relay import native_agent
    from relay.console_entry import ASSISTANT_CONTEXT
    from test_native_agent import Client
    legacy = tmp_path/'state/agent-sessions/legacy'
    initial = Client(steps=[('tare_deliver', {'summary': 'Saved direct conversation ready.'})])
    initial.endpoint = f'http://127.0.0.1:{server.server_port}'
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
        'repositories': {project.name: str(project)}, 'writable': {project.name: ['a.py']},
        'verifiers': {'check': {'repository': project.name, 'argv': [sys.executable, 'check.py']}},
        'capabilities': ['read', 'list', 'search', 'edit', 'verify'], 'specgraph_edit_context': False}
    seeded = native_agent.run_session(config, json.loads(profile.read_bytes()), legacy,
        ASSISTANT_CONTEXT+'Existing direct conversation.', accept_messages=True, accept_attachments=True,
        client=initial, local_counter=initial)
    assert seeded['status'] == 'PROPOSED'

    async def scenario():
        app = WorkConsole(project, backend=Backend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.set_pending_permissions({'mode': 'edit', 'write_paths': ['a.py']}, str(project))
            app.query_one('#profile', Input).value = str(profile)
            app.query_one('#streaming', Checkbox).value = False
            app.assistant_session = str(legacy)
            app.query_one('#verifier', Input).value = json.dumps([sys.executable, 'check.py'])
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Read a.py and change value to 2.'); app.execute(False)
            await until(pilot, first.is_set, app)
            assert (project/'a.py').read_bytes() == b'value = 2\n'
            prompt.load_text(first_text); app.execute(False)
            await until(pilot, lambda:not app.sending_message, app)
            session, identity = app.assistant_session, app.demand_id
            assert native_messages.inspect(session)['items'][0]['status'] == 'QUEUED'
            first_release.set()
            await until(pilot, stop.is_set, app)
            assert (project/'a.py').read_bytes() == b'value = 3\n'
            prompt.load_text(second_text); app.execute(False)
            await until(pilot, lambda:not app.sending_message, app)
            assert [r['status'] for r in native_messages.inspect(session)['items']] == ['IN_CONTEXT','QUEUED']
            app.action_cancel()
            await until(pilot, lambda:not app.busy, app)
            assert app.last_result['status'] == 'CANCELLED', app.last_result
            stop_release.set()
            if (lot := os.environ.get('TARE_EXPERIMENT_RUN_DIR')):
                app.save_screenshot(filename=f'resume-cancelled-{sys.platform}-{size[0]}.svg', path=lot)
            assert (project/'a.py').read_bytes() == b'value = 3\n'
        assert not errors, errors
        reopened = WorkConsole(project, backend=Backend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete()
            reopened.action_conversations(); await reopened.workers.wait_for_complete()
            reopened.open_conversation(next(i for i,r in enumerate(reopened.navigation_shown) if r['id']==identity))
            await reopened.workers.wait_for_complete()
            assert reopened.assistant_session == session and len(requests) == 5
            prompt = reopened.query_one('#prompt', TextArea); prompt.load_text(draft)
            # Explicit Resume remains available when the user wants the original turn.
            assert prompt.text == draft and len(requests) == 5
            # A newly selected profile must not replace the saved execution profile.
            reopened.query_one('#profile', Input).value = str(tmp_path/'missing-profile.json')
            command = next(c for c in reopened.get_system_commands(reopened.screen) if c.title == 'Resume conversation')
            command.callback()
            await reopened.workers.wait_for_complete()
            assert reopened.last_result['status'] == 'PROPOSED', (reopened.last_result, errors)
            assert len(requests) == 7 and not errors, errors
            assert reopened.last_result['model_calls'] == 8  # one saved pre-existing turn, then seven fixture requests
            usage = json.loads((Path(session)/'usage.json').read_text())
            assert usage['coverage']['generation_intents'] == 8
            assert usage['calls'][5]['normalized']['tokens']['total_tokens'] is None
            assert prompt.text == draft and reopened.assistant_session == session
            assert [r['status'] for r in native_messages.inspect(session)['items']] == ['IN_CONTEXT','IN_CONTEXT']
            assert (project/'a.py').read_bytes() == b'value = 3\n'
            assert (project/'note.txt').read_text() == 'Keep this user note.\n'
            from relay.native_review import review
            view = review(Path(session), project=project)
            assert len(view['edits']) == 2, view
            assert not (project/'.git').exists() and not (project/'work-graph.json').exists()
            assert not any('You: '+draft in line for line in reopened.assistant_history)
            assert reopened.demand_state['snapshot']['selection']['prompt'] == draft
            from relay.console_output import ExecutionResult, Transcript, ToolActivity
            output = reopened.query_one('#output', Transcript)
            actors = [w for w in output.query(ToolActivity) if w.value['operation'] == 'agent_run']
            tools = [w for w in output.query(ToolActivity) if w.value['operation'] != 'agent_run']
            assert len(actors) == 2 and all(w.value.get('actor') for w in actors)
            assert len(tools) == 5
            assert [row.value['operation'] for row in tools].count('edit') == 2
            assert all(row.value['state'] == 'succeeded' and row.value['duration_ms'] >= 0 for row in tools)
            assert len({row.value['id'] for row in tools}) == 5
            latest = list(output.query(ExecutionResult))[-1]
            for _ in range(60):
                if ((output.max_scroll_y == 0 or output.is_vertical_scroll_end) and
                        latest.query_one('.result-details').region.bottom <= output.region.bottom):
                    break
                await pilot.pause(.05)
            # Textual anchors short conversations with a negative offset to
            # align them at the bottom; is_vertical_scroll_end is then false.
            assert (output.max_scroll_y == 0 or output.is_vertical_scroll_end) and latest.query_one('.result-details').region.bottom <= output.region.bottom, {
                'scroll_y': output.scroll_y, 'maximum': output.max_scroll_y,
                'anchor_released': output._anchor_released, 'viewport': output.region,
                'details': latest.query_one('.result-details').region, 'focus': repr(reopened.focused)}
            if (lot := os.environ.get('TARE_EXPERIMENT_RUN_DIR')):
                await pilot.pause()
                reopened.save_screenshot(filename=f'activity-resume-completed-{sys.platform}-{size[0]}.svg', path=lot)
            print(json.dumps({'queue_cancel_resume':'PASS','model_requests':len(requests),
                'confirmed_edits':len(view['edits']),'session_reused':True,'unsent_draft_preserved':True}))
    try:
        asyncio.run(scenario())
    finally:
        first_release.set(); stop_release.set()
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.mark.parametrize('entry', ['button','slash','palette'])
def test_explicit_resume_routes_without_sending_draft_or_selected_context(tmp_path, monkeypatch, entry):
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    project = tmp_path/'project'; project.mkdir()
    session = tmp_path/'saved'; session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config':{'repositories':{'repo':str(project)}}}))
    async def scenario():
        backend = FakeBackend(); backend.process.done.set()
        app = WorkConsole(project, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session); app.update_mode(); await pilot.pause()
            prompt = app.query_one('#prompt',TextArea)
            original = '/attach do-not-open.txt' if entry!='slash' else '/resume'
            prompt.load_text(original)
            app.context_root, app.context_paths = str(project), ['note.txt']
            # Loading /attach can reflow the composer and move Resume.
            await pilot.pause()
            if entry == 'button': assert await pilot.click('#resume')
            elif entry == 'slash': app.execute(False)
            else: next(c for c in app.get_system_commands(app.screen) if c.title=='Resume conversation').callback()
            # Drain the button message before waiting for the worker it starts.
            await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert len(backend.launches) == 1, str(app.query_one('#status').render())
            args = backend.launches[0]
            assert '--resume' in args and '--resume-interrupted-model' in args
            assert '--follow-up' not in args and '--attachment-input' not in args
            assert app.context_paths == ['note.txt']
            assert prompt.text == ('' if entry=='slash' else original)
            assert not any(isinstance(line, str) and line.startswith('You:') for line in app.assistant_history)
    asyncio.run(scenario())


@pytest.mark.parametrize('draft', ['Keep this unsent draft', '/attach do-not-open.txt'])
def test_direct_session_resume_is_explicit_recovery_without_sending_draft(tmp_path, monkeypatch, draft):
    from textual.widgets import Select
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    project = tmp_path/'project'; project.mkdir()
    (project/'note.txt').write_text('Context for a different task.', encoding='utf-8')
    session = tmp_path/'saved'; session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config': {'repositories': {'repo': str(project)}}}))

    async def scenario():
        backend = FakeBackend(); backend.process.done.set()
        app = WorkConsole(project, backend=backend, initial_mode='session')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            selector = app.query_one('#session', Select)
            selector.set_options([('Interrupted session', str(session))]); selector.value = str(session)
            prompt = app.query_one('#prompt', TextArea); prompt.load_text(draft)
            app.context_root, app.context_paths = str(project), ['note.txt']
            await pilot.pause()
            assert await pilot.click('#resume')
            await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert len(backend.launches) == 1, str(app.query_one('#status').render())
            args = backend.launches[0]
            assert '--resume' in args and '--resume-interrupted-model' in args
            assert args[args.index('--session')+1] == str(session)
            assert not any(flag in args for flag in (
                '--follow-up', '--attachment-input', '--deployment', '--native-deployment',
                '--unlimited', '--max-model-calls', '--max-seconds'))
            assert prompt.text == draft and app.context_paths == ['note.txt']
    asyncio.run(scenario())


def test_resume_guards_keep_draft_without_launching(tmp_path, monkeypatch):
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt',TextArea); prompt.load_text('Keep this draft')
            app.action_resume_conversation()
            app.assistant_session = 'saved'
            app.sending_message = True; app.action_resume_conversation(); app.sending_message = False
            app.query_one('#root',Input).value = str(tmp_path/'other'); app.action_resume_conversation()
            app.query_one('#root',Input).value = str(tmp_path)
            prompt.load_text('/resume invalid'); app.execute(False)
            assert prompt.text == '/resume invalid' and not app.backend.launches
    asyncio.run(scenario())

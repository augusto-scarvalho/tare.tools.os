"""Real TUI, CLI invocations and persistent commands; inference alone is simulated."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from threading import Thread

from textual.widgets import Checkbox, Input, Select, TextArea

from compute_plane.terminal_host import TerminalClient, TerminalTransportError
from relay import relay_mesh
from relay.console_backend import Backend
from relay.native_console import WorkConsole
from test_native_agent import deployment, response
import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows TUI command journey')
def test_windows_host_context_shell_exit_wait_timeout_and_followup(tmp_path, monkeypatch):
    project = tmp_path/'project with spaces [1]'; project.mkdir()
    (project/'ação [um].txt').write_text('café São Paulo', encoding='utf-8')
    replies, environments, step, handle = [], [], 0, None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            nonlocal step, handle
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/apply-template': result = {'prompt':'fixture'}
            elif self.path == '/tokenize': result = {'tokens':list(range(80))}
            else:
                context = json.JSONDecoder().raw_decode(body['messages'][0]['content'].split(
                    'Current command host facts (apply on this invocation): ',1)[1])[0]
                environments.append(context)
                for message in body['messages']:
                    if message.get('role') == 'tool':
                        try: value=json.loads(message['content'])
                        except json.JSONDecodeError: continue
                        if isinstance(value,dict) and value.get('handle'):
                            handle=value['handle']; replies.append(value)
                step += 1
                if step == 1:
                    script=(context['shell_script_prefix']+'$ErrorActionPreference="Stop"; '
                        "Get-Content -LiteralPath '"+str(project/'ação [um].txt').replace("'","''")+"' -Encoding UTF8; & '"+
                        context['host_python'].replace("'","''")+"' -c 'raise SystemExit(7)'; exit $LASTEXITCODE")
                    name,args='terminal_start',dict(repository=project.name,argv=[*context['shell_argv'],script])
                elif step in (2,5,6,9):
                    name,args='terminal_read',dict(handle=handle,wait_for_exit=True,wait_ms=100 if step==5 else 5000)
                elif step in (4,8):
                    code=('import time; print("ONCE",flush=True); time.sleep(2); print("DONE")' if step==4 else
                          'import time; print("PARTIAL",flush=True); time.sleep(20)')
                    name,args='terminal_start',dict(repository=project.name,argv=[context['host_python'],'-u','-c',code],
                                                    timeout_seconds=1 if step==8 else 10)
                else:
                    name,args='tare_deliver',dict(summary='Observed process state and exit result.')
                result=response('openai-chat',name,args)
            raw=json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True); thread.start()
    configured=deployment(endpoint=f'http://127.0.0.1:{server.server_port}')
    configured['native_challenger_accounting']='usd'  # stands for a hosted model: a local one gets no terminal
    configured['native_profile']['context_window']=131072
    profile=tmp_path/'profile.json'; profile.write_text(json.dumps(configured))
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    monkeypatch.setenv('TOP_SECRET','must-not-reach-model')
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',tmp_path/'control')

    async def scenario():
        app=WorkConsole(project,backend=Backend(),initial_mode='session',storage_preferred=tmp_path/'state')
        async with app.run_test(size=(130,48)) as pilot:
            await app.workers.wait_for_complete()
            app.set_pending_permissions({'mode': 'commands', 'write_paths': ['.']}, str(project))
            app.query_one('#profile',Input).value=str(profile)
            app.query_one('#streaming',Checkbox).value=False
            app.query_one('#terminal',Checkbox).value=True
            custody=None
            for task in ('Run the PowerShell check.', 'Run the delayed command.', 'Observe a process timeout.'):
                app.query_one('#prompt',TextArea).load_text(task)
                app.query_one('#prompt',TextArea).focus()
                if custody is None:
                    await pilot.press('ctrl+enter')
                else:
                    selector = app.query_one('#session', Select)
                    selector.set_options([('Interactive session', str(custody))]); selector.value = str(custody)
                    app.execute(True)
                # The key handler queues dispatch. Waiting on an empty worker
                # set can otherwise tear down the screen before execution starts.
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                diagnostic = {key: app.last_result.get(key) for key in
                              ('status', 'reason', 'failure_code', 'model_calls')}
                diagnostic.update(task=task, model_step=step, terminal_replies=replies[-2:])
                assert app.last_result['status']=='PROPOSED',json.dumps(diagnostic,ensure_ascii=False)
                if custody is None: custody=app.active.session
                assert app.active.session==custody
            saved=json.loads((Path(custody)/'native-agent.json').read_text())
            assert all(i['execution_environment']['platform']=='Windows' for i in saved['invocations'])
    try:
        asyncio.run(scenario())
        assert step==10 and all(e['preferred_shell'] in ('pwsh','powershell') for e in environments)
        assert 'must-not-reach-model' not in json.dumps(environments)
        assert any(r['exit_code']==7 and 'café São Paulo' in r['output']['stdout']['text'] for r in replies)
        assert any(r.get('read_wait_expired') and not r['process_timed_out'] for r in replies)
        assert any(r['exit_code']==0 and 'DONE' in r['output']['stdout']['text'] for r in replies)
        assert any(r['process_timed_out'] and r['cleanup_verified'] and 'PARTIAL' in r['output']['stdout']['text'] for r in replies)
        assert len({r['handle'] for r in replies})==3
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_terminal_survives_agent_turn_and_closes_with_tui(tmp_path, monkeypatch):
    project = tmp_path/'project'; project.mkdir()
    observed, handle, generation, stdout_cursor = [], None, 0, 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal handle, generation, stdout_cursor
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/apply-template':
                result = {'prompt':'fixture prompt'}
            elif self.path == '/tokenize':
                result = {'tokens':list(range(80))}
            else:
                generation += 1
                # Inspect tool observations, never model reasoning or raw journals.
                for message in body['messages']:
                    if message.get('role') == 'tool':
                        try:
                            value = json.loads(message['content'])
                        except json.JSONDecodeError:
                            continue  # Deliver acknowledgements can be plain text.
                        observed.append(value)
                        if isinstance(value, dict) and value.get('handle'):
                            handle = value['handle']
                            stdout_cursor = value.get('output',{}).get('stdout',{}).get('cursor',stdout_cursor)
                names = {tool['function']['name'] for tool in body['tools']}
                assert 'terminal_start' in names
                assert 'TARE_TERMINAL_CONNECTION' not in json.dumps(body)
                if generation == 1:
                    name, args = 'terminal_start', {'repository':project.name,
                        'argv':[sys.executable,'-B','-u','-c',
                                "import time; print('READY'); print('ACK:'+input()); time.sleep(60)"],
                        'timeout_seconds':None}
                elif generation == 2:
                    name, args = 'terminal_read', {'handle':handle}
                elif generation == 3:
                    name, args = 'tare_deliver', {'summary':'Command is waiting for input.'}
                elif generation == 4:
                    name, args = 'terminal_input', {'handle':handle,'text':'second turn\n'}
                elif generation == 5:
                    name, args = 'terminal_read', {'handle':handle,'stdout_cursor':stdout_cursor}
                else:
                    name, args = 'tare_deliver', {'summary':'Input sent and output observed.'}
                result = response('openai-chat',name,args)
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = Thread(target=server.serve_forever,daemon=True); thread.start()
    profile = tmp_path/'profile.json'
    configured = deployment(endpoint=f'http://127.0.0.1:{server.server_port}')
    configured['native_challenger_accounting'] = 'usd'  # stands for a hosted model: a local one gets no terminal
    configured['native_profile']['context_window'] = 131072
    profile.write_text(json.dumps(configured))
    monkeypatch.setenv('PYTHONPATH',str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',tmp_path/'control')

    async def scenario():
        backend = Backend()
        app = WorkConsole(project,backend=backend,initial_mode='session',storage_preferred=tmp_path/'state')
        async with app.run_test(size=(130,48)):
            await app.workers.wait_for_complete()
            app.set_pending_permissions({'mode': 'commands', 'write_paths': ['.']}, str(project))
            app.query_one('#profile',Input).value = str(profile)
            app.query_one('#streaming',Checkbox).value = False
            app.query_one('#terminal',Checkbox).value = True
            app.query_one('#prompt',TextArea).load_text('Start an interactive command and leave it waiting.')
            app.execute(False)
            await app.workers.wait_for_complete()
            assert app.last_result and app.last_result['status'] == 'PROPOSED', app.last_result
            assert generation == 3 and handle
            custody = Path(app.active.session)
            saved = json.loads((custody/'native-agent.json').read_text())
            assert saved['config']['terminal'] is True
            descriptor = backend._terminals.connection(custody,{project.name:str(project.resolve())})
            client = TerminalClient(descriptor)
            assert client.request('read',handle=handle)['status'] == 'running'
            app.query_one('#prompt',TextArea).load_text('Send second turn and read its response.')
            selector = app.query_one('#session', Select)
            selector.set_options([('Interactive session', str(custody))]); selector.value = str(custody)
            app.execute(True)
            await app.workers.wait_for_complete()
            assert app.last_result['status'] == 'PROPOSED', app.last_result
            assert generation == 6 and Path(app.active.session) == custody
            assert any('ACK:second turn' in v.get('output',{}).get('stdout',{}).get('text','') for v in observed)
            assert descriptor['token'] not in json.dumps(saved)
            assert descriptor['token'] not in json.dumps(app.last_result)
            assert client.request('read',handle=handle)['status'] == 'running'
        with pytest.raises(TerminalTransportError):
            client.request('list')
        # The supervisor retains its receipt after transport shutdown.
        from relay.console_terminal import session_owner
        final = backend._terminals._host.sessions.read(owner=session_owner(custody),handle=handle)
        assert final['cleanup_verified'] and final['status'] == 'stopped'
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)

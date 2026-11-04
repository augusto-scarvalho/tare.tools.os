"""Real TUI, subprocess, parallel workers and tools; controlled HTTP inference."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Thread

import pytest
from textual.widgets import Select, TextArea

from relay import parallel_explore, relay_mesh
from relay.console_backend import Backend, Selection
from relay.console_output import ToolActivity
from relay.native_activity import full_output
from relay.native_console import WorkConsole
from test_console_resume import until
from test_native_agent import deployment, response


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_parallel_cli_streams_and_reopens_child_output(tmp_path, monkeypatch, size):
    project = tmp_path/'project'; project.mkdir()
    (project/'a.py').write_bytes(b'value = 1\n')
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    (tmp_path/'control').mkdir()
    (tmp_path/'control'/'work-graph.json').write_text(json.dumps({
        'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}))
    release = Event()
    requests, errors = [], []
    payload = {'summary': 'Observed value = 1', 'remaining': [], 'sources': [
        {'repository': 'project', 'path': 'a.py',
         'sha256': hashlib.sha256((project/'a.py').read_bytes()).hexdigest()}]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/apply-template': value = {'prompt': 'fixture'}
                elif self.path == '/tokenize': value = {'tokens': list(range(100))}
                else:
                    requests.append(body)
                    if any(m.get('role') == 'tool' for m in body['messages']):
                        assert release.wait(25), 'TUI did not observe live child activity'
                        value = response('openai-chat', 'tare_deliver', payload)
                    else:
                        value = response('openai-chat', 'read', {'repository': 'project', 'path': 'a.py'})
                raw = json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            except Exception as exc:
                errors.append(repr(exc))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    directory = tmp_path/'batch'
    parallel_explore.create(directory, project, ['Inspect value', 'Inspect assignment'],
        deployment(endpoint=f'http://127.0.0.1:{server.server_port}'), workers=2, unlimited=True)

    async def scenario():
        app = WorkConsole(project, backend=Backend(), initial_mode='parallel')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            selection = Selection(root=str(project), mode='parallel', batch=str(directory), prompt='Keep this draft.')
            app.restore_selection(selection.__dict__)
            reported = []
            say = app.say
            def observe(text, *, literal=False):
                reported.append(text)
                say(text, literal=literal)
            app.say = observe
            def tools():
                return [w for w in app.query(ToolActivity) if w.value['operation'] != 'agent_run']
            app.controls(True)
            worker = app.run_command(['parallel-explore', '--batch', str(directory), '--resume', '--format', 'json'], selection)
            try:
                await until(pilot, lambda: (not app.busy or len(tools()) == 2 and
                            all(w.value['state'] == 'succeeded' and 'Explore' in w.title
                                for w in tools())), app)
                assert app.process is not None and app.process.returncode is None, '\n'.join(map(str, reported))
            finally:
                release.set(); await worker.wait()
            await app.workers.wait_for_complete()
            assert app.last_result['status'] == 'COMPLETED', app.last_result
            assert len(requests) == 4 and not errors
            agents = [w for w in app.query(ToolActivity) if w.value['operation'] == 'agent_run']
            assert len(agents) == 2 and all(w.value['actor']['role'] == 'explorer' for w in agents)
            assert all(not w.query_one('.tool-full-output').display for w in agents)
            rows = [widget.value for widget in tools()]
            assert {Path(row['source']['session']).name for row in rows} == {'1', '2'}
            for row in rows:
                assert 'value = 1' in full_output(None, project, row)
                assert 'reasoning retained' not in json.dumps(row)
            app.query_one('#prompt', TextArea).load_text('Unsent follow-up')
            app.checkpoint('draft_saved'); identity = app.demand_id
            app.new_demand(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause()
            assert len(tools()) == 2 and len(app.query(ToolActivity)) == 4 and len(requests) == 4
            assert app.query_one('#prompt', TextArea).text == 'Unsent follow-up'
            assert all('value = 1' in full_output(None, project, w.value) for w in tools())
            from textual.widgets import Collapsible
            history = next(w for w in app.query(Collapsible) if w.title == 'Saved flow history')
            assert history.collapsed
            app.save_screenshot(filename=f'flow-parallel-{size[0]}.svg', path=str(tmp_path))
    try:
        asyncio.run(scenario())
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(timeout=5)

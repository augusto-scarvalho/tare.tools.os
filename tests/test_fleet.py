"""tare fleet and /fleet: the owner picks what the GPU node serves from tare.tools' catalog (shown first) and their
own models; tare auto runs the fleet's default."""
import asyncio
import json

import pytest

from relay import environment, fleet, native_catalog, turn_engine

ORIGIN = 'http://gpu-node.test:8080'
CATALOG = [{'id': 'qwen38-gsq', 'display_name': 'Qwen3.8 27B GSQ', 'qualified_for': ['general'], 'on_node': True},
           {'id': 'hauhaucs', 'display_name': 'HauhauCS', 'qualified_for': ['coding'], 'on_node': True},
           {'id': 'muse-vision', 'display_name': 'Muse', 'qualified_for': ['vision-hard'], 'on_node': False}]


def node_view(value):
    return {'catalog': CATALOG, 'fleet': value, 'served': value['models'] + list(value.get('custom') or {}),
            'default': value['default'], 'missing': []}


@pytest.fixture
def node(monkeypatch):
    """The node's fleet API, in memory: what it was sent, with which token."""
    state = {'fleet': {'default': 'qwen38-gsq', 'models': ['qwen38-gsq'], 'custom': {}}, 'tokens': []}
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', ORIGIN)
    monkeypatch.setattr(fleet, 'view', lambda origin: node_view(state['fleet']))
    def save(origin, token, value):
        state['tokens'].append(token)
        state['fleet'] = value
        return node_view(value)
    monkeypatch.setattr(fleet, 'save', save)
    monkeypatch.setattr(fleet, 'remember', lambda origin: state.update(remembered=origin))
    monkeypatch.setattr('relay.gpu.node_token', lambda: 'node-token')
    return state


def test_tare_fleet_adds_own_models_sets_the_default_and_removes_on_the_node(node, capsys):
    assert fleet.main([]) == 0
    shown = capsys.readouterr().out
    assert '[*] qwen38-gsq' in shown and '[ ] hauhaucs' in shown and 'Muse · vision-hard · not on the node' in shown
    assert fleet.main(['add', 'hauhaucs']) == 0 and node['fleet']['models'] == ['qwen38-gsq', 'hauhaucs']
    assert fleet.main(['own', 'mine', '/models/mine.gguf', '--', '-c', '32768']) == 0
    assert node['fleet']['custom'] == {'mine': {'artifact': {'path': '/models/mine.gguf'},
                                                'runtime': {'args': ['-c', '32768']}}}
    assert fleet.main(['default', 'mine']) == 0 and node['fleet']['default'] == 'mine'
    assert '[*] mine /models/mine.gguf -c 32768' in capsys.readouterr().out
    assert fleet.main(['remove', 'mine']) == 0
    assert node['fleet'] == {'default': 'qwen38-gsq', 'models': ['qwen38-gsq', 'hauhaucs'], 'custom': {}}
    assert fleet.main(['own', 'bad', 'relative/path.gguf']) == 1 and 'absolute path' in capsys.readouterr().out
    assert node['tokens'] == ['node-token'] * 4 and node['remembered'] == ORIGIN


def test_tare_auto_runs_the_default_of_the_nodes_fleet(monkeypatch):
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', ORIGIN)
    def saved(default=None):
        gateway = {'origin': ORIGIN, 'models': ['qwen38-gsq', 'hauhaucs', 'mine'], **({'default': default} if default else {})}
        return {'local_models': {'use': True, 'gateways': [gateway]}}
    assert turn_engine.DEFAULT_POLICY['implementer'][0] == ['local-default']
    assert native_catalog.local_default(saved()) == 'qwen38-gsq'  # the node has not said
    assert native_catalog.local_default(saved('hauhaucs')) == 'hauhaucs'  # tare's measured profile
    assert native_catalog.local_default(saved('mine')) == 'local-gpu-node-test-8080-mine'  # the generic one
    monkeypatch.setattr(environment, 'read', lambda: saved('hauhaucs'))
    assert native_catalog.select('local-default')['native_profile']['model'] == 'hauhaucs'


def test_setup_remembers_the_default_the_gateway_says(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    answers = {'/v1/models': {'data': [{'id': 'qwen38-gsq'}, {'id': 'mine'}]},
               '/v1/fleet/status': {'role': 'qualified-model-gateway', 'default_model': 'mine'}}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(answers[self.path]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        asked = environment.ask_gateway(url)
        assert asked['default'] == 'mine' and asked['kind'] == 'ninfer'
        answers['/v1/fleet/status'] = {'role': 'qualified-model-gateway', 'default_model': 'qwen38-gsq'}
        value = environment.refresh_gateway({'local_models': {'use': True, 'gateways': [asked]}})
        assert environment.gateway_default(value) == 'qwen38-gsq'
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_fleet_in_the_tui_shows_the_catalog_first_and_saves_on_the_node(tmp_path, node, size):
    from textual.widgets import Button, Checkbox, Select, TextArea
    from relay.console_fleet import FleetPanel
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/fleet', '')
            panel = app.screen
            assert isinstance(panel, FleetPanel)
            for _ in range(50):
                await pilot.pause(0.05)
                if not panel.query_one('#fleet-save', Button).disabled:
                    break
            assert panel.region.contains_region(panel.query_one('#fleet-save', Button).region)
            boxes = list(panel.query(Checkbox))
            assert [b.value for b in boxes] == [True, False, False]
            boxes[1].value = True
            panel.query_one('#fleet-own', TextArea).load_text('mine /models/mine.gguf -c 32768\n')
            panel.query_one('#fleet-default', Select).value = 'hauhaucs'
            await pilot.click('#fleet-save')
            for _ in range(50):
                await pilot.pause(0.05)
                if node['tokens']:
                    break
            assert node['fleet'] == {'default': 'hauhaucs', 'models': ['qwen38-gsq', 'hauhaucs'],
                                     'custom': {'mine': {'artifact': {'path': '/models/mine.gguf'},
                                                         'runtime': {'args': ['-c', '32768']}}}}
            assert not app.backend.launches
            await pilot.press('escape')
            assert app.screen is not panel
    asyncio.run(scenario())

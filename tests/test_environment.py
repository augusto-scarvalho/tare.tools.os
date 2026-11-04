"""D15 phase 0: what this machine's owner chose to use leaves routes out, and no file changes nothing."""
import json

import pytest

from relay import environment, turn_engine, work_forms

REAL_JEV_KEY = turn_engine.jev_key  # before conftest keeps tests away from the operator's vault

CODEX = {'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://codex'}}
AGY = {'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://agy'}}
CODEX_NATIVE = {'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'https://chatgpt.com/backend-api'}}
OPENROUTER = {'native_profile': {'protocol': 'openai-chat', 'endpoint': 'https://openrouter.ai/api/v1'},
              'native_credential_env': 'OPENROUTER_API_KEY'}
LOCAL = {'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://127.0.0.1:8080/v1'}}
OTHER_LOCAL = {'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://192.168.0.9:11434/v1'}}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    path = tmp_path/'environment.json'
    monkeypatch.setenv('TARE_ENVIRONMENT_STORE', str(path))
    monkeypatch.setattr('relay.native_generation.vault_secret', lambda name: None)  # never the operator's vault
    def save(**categories):
        path.write_text(json.dumps({'schema': environment.SCHEMA, **categories}))
    return save


def test_no_file_leaves_every_route_in(machine):
    for route in (CODEX, AGY, CODEX_NATIVE, OPENROUTER, LOCAL, OTHER_LOCAL):
        assert environment.allows(route) == (True, None)
    assert environment.decides('jev') and environment.decides('local_judge')
    assert not environment.summary()['saved']


def test_a_category_or_one_item_left_out(machine):
    machine(clis={'use': True, 'items': {'cli://agy': False}}, api_providers={'use': False})
    assert environment.allows(CODEX) == (True, None) and environment.allows(CODEX_NATIVE) == (True, None)
    assert environment.allows(AGY) == (False, 'cli://agy not used on this machine (tare setup)')
    assert environment.allows(OPENROUTER) == (False, 'API providers not used on this machine (tare setup)')
    assert environment.allows(LOCAL) == (True, None)  # undecided: as today
    machine(clis={'use': False}, local_models={'use': True, 'gateways': [{'origin': 'http://127.0.0.1:8080'}]})
    assert not environment.allows(CODEX_NATIVE)[0]  # the same account as the Codex CLI
    assert environment.allows(LOCAL) == (True, None)
    assert environment.allows(OTHER_LOCAL)[1] == \
        'local gateway http://192.168.0.9:11434 is not set up on this machine (tare setup)'
    report = environment.summary()['categories']
    assert report['clis']['state'] == 'not used' and report['api_providers']['state'] == 'undecided'


def test_an_unreadable_file_refuses_every_route_and_says_why(machine, tmp_path):
    (tmp_path/'environment.json').write_text('{not json')
    used, why = environment.allows(CODEX)
    assert not used and why.startswith('machine setup unreadable') and not environment.decides('jev')
    assert 'setup unreadable, every route refused' in work_forms.availability_text(work_forms.availability(tmp_path))


def test_a_route_left_out_is_skipped_with_its_reason_and_never_probed(machine, monkeypatch):
    machine(local_models={'use': False})
    def probe(*a, **kw):
        raise AssertionError('a route left out must not reach the gateway')
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', probe)
    assert turn_engine.available('qwen38-gsq') == (None, 'local models not used on this machine (tare setup)')
    skipped = turn_engine.choose([['qwen38-gsq']])[3]
    assert len(skipped) == 1 and skipped[0]['route'] == 'qwen38-gsq'
    assert skipped[0]['reason'] == 'local models not used on this machine (tare setup)'
    assert skipped[0]['diagnostic']['code'] == 'MACHINE_POLICY'


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """A local OpenAI-compatible server without fleet status (Ollama-like); `models` is what it publishes."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_PROFILE_DIR', raising=False)
    monkeypatch.delenv('TARE_LOCAL_GATEWAY', raising=False)
    published = ['qwen38-gsq', 'minha-llama']
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'data': [{'id': m} for m in published]}).encode() if self.path == '/v1/models' else b''
            self.send_response(200 if body else 404)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}', published
    server.shutdown(); server.server_close()


def test_setup_by_flags_offers_only_what_the_gateway_publishes(machine, gateway, capsys):
    from relay import native_catalog, native_installation, native_profiles, native_serve
    url, published = gateway
    assert environment.main(['--local-gateway', url, '--no-use', 'api', '--use', 'cli:codex']) == 0
    saved = environment.read()
    assert saved['local_models']['gateways'][0] | {'observed': None} == {
        'origin': url, 'kind': 'openai', 'models': ['minha-llama', 'qwen38-gsq'], 'observed': None}
    assert saved['clis'] == {'items': {'cli://codex': {'use': True}}} and saved['api_providers'] == {'use': False}
    assert json.loads((native_installation.storage_root()/'local-gateway.json').read_text())['origin'] == url
    generic = 'local-127-0-0-1-' + url.rsplit(':', 1)[1] + '-minha-llama'
    offered = {r['deployment'] for r in native_catalog.available_models()}
    assert {'qwen38-gsq', 'tare-auto', generic} <= offered
    assert not offered & {'swift27b', 'hauhaucs', 'bedrock-glm', 'openrouter-glm-5-3'}  # unpublished; API declined
    assert 'not qualified' in native_catalog.select(generic)['native_profile']['source']
    assert native_serve.default_models() == {'tare-qwen38': 'qwen38-gsq', 'tare-' + generic[6:]: generic}
    assert environment.default_route() == 'tare-auto'
    # The gateway stops publishing qwen38-gsq; a generic profile the owner edited is kept, never retired.
    published[:] = ['other-model']
    edited = native_profiles.local_path(generic)
    edited.write_text(edited.read_text().replace('32768', '65536'))
    assert environment.main(['--refresh']) == 0
    offered = {r['deployment'] for r in native_catalog.available_models()}
    assert 'qwen38-gsq' not in offered and generic in offered and edited.exists()
    assert 'local-127-0-0-1-' + url.rsplit(':', 1)[1] + '-other-model' in offered
    assert 'Generic local profiles' in capsys.readouterr().err


def test_a_credential_goes_to_the_vault_from_stdin_and_is_never_printed(machine, monkeypatch, capsys):
    import io
    import keyring
    stored = {}
    monkeypatch.setattr(keyring, 'set_password', lambda service, name, secret: stored.update({name: secret}))
    monkeypatch.delenv('FIXTURE_API_KEY', raising=False)
    monkeypatch.setattr('sys.stdin', io.StringIO('sk-fixture-secret\n'))
    assert environment.main(['--credential', 'FIXTURE_API_KEY', '--json']) == 0
    assert stored == {'FIXTURE_API_KEY': 'sk-fixture-secret'}
    assert 'sk-fixture-secret' not in capsys.readouterr().out
    assert 'sk-fixture-secret' not in environment.store().read_text()
    assert environment.read()['api_providers']['items'] == {'FIXTURE_API_KEY': {'use': True}}
    monkeypatch.setenv('FIXTURE_API_KEY', 'set')
    assert environment.main(['--credential', 'FIXTURE_API_KEY']) == 1  # the environment would win over the vault


def test_without_a_terminal_setup_asks_nothing(machine, monkeypatch, capsys):
    import io
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    assert environment.main([]) == 2 and not environment.store().exists()
    assert 'This machine is not set up yet' in capsys.readouterr().out
    class Nul(io.StringIO):  # Windows reports NUL as a terminal
        def isatty(self):
            return True
    monkeypatch.setattr('sys.stdin', Nul(''))
    assert environment.main([]) == 2 and not environment.store().exists()
    answers = iter(['n', 'y', '', 'n', ''])  # local, CLIs, API, decision models, search APIs
    value = environment.dialog({}, ask=lambda question: next(answers))
    assert value == {'local_models': {'use': False}, 'clis': {'use': True}, 'decision_models': {'use': False}}


def test_a_declined_cli_is_never_located_or_asked_to_answer(machine, monkeypatch, tmp_path):
    from relay import cli_discovery
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    machine(clis={'items': {'cli://agy': False}})
    located = []
    monkeypatch.setattr(cli_discovery, 'locate', lambda endpoint: located.append(endpoint))
    report = {r['endpoint']: r['status'] for r in cli_discovery.discover()}
    assert report['cli://agy'] == 'not used' and 'cli://agy' not in located


def test_the_tui_setup_panel_saves_item_by_item(machine, tmp_path, monkeypatch):
    import asyncio
    from textual.widgets import Select, Static
    from relay.console_setup import SetupPanel
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: None)
    async def scenario():
        monkeypatch.delenv('TARE_CLI_DISCOVERY', raising=False)
        monkeypatch.setattr('relay.cli_discovery.discover', lambda: [])  # the interactive entry, without CLIs
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant', discover_clis=True)
        shown = []
        app.notify = lambda message, **kwargs: shown.append(message)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            # A machine never set up says so once, as a toast: a status line that long would move the transcript.
            assert any('/setup chooses' in m for m in shown)
            assert app.dispatch_conversation_command('/setup', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, SetupPanel)
            panel.query_one('#setup-clis', Select).value = 'use'
            panel.query_one('#setup-cli-agy', Select).value = 'no'
            panel.query_one('#setup-api_providers', Select).value = 'no'
            await pilot.click('#setup-save')
            await pilot.pause()
            assert str(panel.query_one('#setup-status', Static).render()).startswith('Saved.')
            from textual.widgets import Button, TabbedContent
            panel.query_one('#setup-tabs', TabbedContent).active = 'setup-advanced'
            panel.query_one('#setup-prove', Select).value = 'cli:agy'
            panel.query_one('#setup-cost', Button).scroll_visible()
            await pilot.pause()
            await pilot.click('#setup-cost')
            await pilot.pause()
            assert 'cli://agy is not installed' in str(panel.query_one('#setup-status', Static).render())
            assert panel.query_one('#setup-run-proof', Button).disabled  # nothing to spend on
            assert not app.backend.launches
    asyncio.run(scenario())
    saved = environment.read()
    assert saved['clis'] == {'use': True, 'items': {'cli://agy': {'use': False}}}
    assert saved['api_providers'] == {'use': False, 'items': {}} and 'gateways' not in saved.get('local_models', {})
    assert environment.default_route() == 'tare-auto'


def test_serve_says_once_per_chat_that_the_machine_is_not_set_up_and_shows_it(machine, monkeypatch, tmp_path):
    from relay import serve_agent
    from test_serve_agent import copilot
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: None)
    project = tmp_path/'project'; project.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'test: no route'))
    out = []
    serve_agent.run_turn(copilot(project, 'explain main'), out.append)
    assert any('this machine is not set up yet' in line for line in out)
    answer = serve_agent.run_turn(copilot(project, '/setup'), out.append)
    assert 'This machine is not set up yet' in answer and 'tare setup' in answer
    machine(clis={'use': True})
    out.clear()
    serve_agent.run_turn(copilot(project, 'explain again'), out.append)
    assert not any('not set up' in line for line in out)


def test_an_internet_openai_provider_is_not_labelled_local_gpu():
    from relay.model_choices import describe
    profile = {'protocol': 'openai-chat', 'model': 'm', 'endpoint': 'https://api.example.com/v1', 'parameters_json': '{}'}
    assert describe('x', {'native_profile': profile})['route']['label'] == 'API · api.example.com'
    profile['endpoint'] = 'http://192.168.0.9:11434/v1'
    assert describe('x', {'native_profile': profile})['route']['label'] == 'Local GPU'


def test_a_proof_says_its_cost_first_and_spends_only_when_agreed(machine, monkeypatch, capsys):
    import io
    from relay import native_generation
    paid = {**OPENROUTER, 'native_profile': {**OPENROUTER['native_profile'], 'model': 'm'}, 'id': 'paid',
            'native_usage_tariff': {'currency': 'USD', 'source': 'fixture',
            'effective_at': '2026-10-05', 'rates_json': json.dumps({'uncached_input_tokens': '2', 'output_tokens': '10'})}}
    monkeypatch.setattr(turn_engine, 'available', lambda name: (paid, None))
    calls = []
    monkeypatch.setattr(native_generation, 'generate_text',
                        lambda *a, **kw: calls.append(kw['attempt_label']) or (True, 'OK', {}))
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    assert environment.main(['--prove', 'paid']) == 1 and calls == []  # no terminal and no --yes: nothing spent
    assert 'under US$ 0.000400' in capsys.readouterr().out
    assert environment.main(['--prove', 'paid', '--yes']) == 0 and calls == ['setup-proof']
    assert environment.read()['proofs']['paid']['ok'] is True
    local = {'native_profile': {**LOCAL['native_profile'], 'model': 'm'}, 'id': 'local'}
    monkeypatch.setattr('compute_plane.model_facts.read_json_observation', lambda url, timeout: {'status': 'UNKNOWN'})
    assert environment.cost_line(local) == 'one minimal call on the local GPU: its state is unknown'


def test_the_jev_key_comes_from_the_environment_then_the_vault(monkeypatch):
    monkeypatch.setattr('relay.native_generation.vault_secret', lambda name: 'vault-key' if name == 'TYPESAFE_API_KEY' else None)
    assert REAL_JEV_KEY() == 'vault-key'
    monkeypatch.setenv('TYPESAFE_API_KEY', 'env-key')
    assert REAL_JEV_KEY() == 'env-key'


def test_a_declined_explorer_hands_over_to_an_allowed_overflow(machine, monkeypatch):
    from copy import deepcopy
    from relay import challenger_variants, delegation
    machine(local_models={'use': False})
    value = dict(delegation.DEFAULT, enabled=True, profile='local', overflow='hosted')
    monkeypatch.setattr(delegation, 'policy', lambda: deepcopy(value))
    monkeypatch.setattr(delegation, 'validate', lambda v: v)
    routes = {'local': (LOCAL, 'local'), 'hosted': (OPENROUTER, 'usd')}
    monkeypatch.setattr(challenger_variants, 'realize', lambda name: {
        'deployment': routes[name][0], 'accounting': routes[name][1], 'task_types': ['exploration']})
    assert delegation.snapshot('s', 'implementer')['selection'] == 'OVERFLOW_EXPLORER'
    machine(local_models={'use': False}, api_providers={'use': False})
    assert delegation.snapshot('s', 'implementer') is None  # nothing it may use: the session does not delegate


def test_classification_with_no_route_here_says_so_without_trying(machine, tmp_path, monkeypatch):
    from tare_tools_agent_runtime import CancellationToken
    from relay import console_entry
    from relay.console_backend import Selection
    project = tmp_path/'project'; project.mkdir()
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: (None, 'not used on this machine'))
    def generate(*a, **kw):
        raise AssertionError('no route: nothing to try')
    result = console_entry.propose(Selection(str(project), deployment='codex-cli', prompt='Fix it.'),
                                   CancellationToken(), directory=tmp_path/'entry', generate=generate)
    assert result['status'] == 'UNAVAILABLE' and 'No model route is available on this machine' in result['reason']


def test_older_cli_models_are_left_out_unless_the_owner_wants_them(machine, tmp_path, monkeypatch, capsys):
    from relay.vendor_models import older
    listed = ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna']
    assert older(listed) == {'gpt-6-sol', 'gpt-5.6-sol', 'gpt-5.6-luna'}  # per line: terra has no newer version
    assert older(listed + ['gpt-7-luna']) == {'gpt-6-sol', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-6-luna'}
    assert older(['claude-fable-5-1', 'claude-opus-5-5', 'claude-sonnet-5-5']) == set()
    # Without an account catalogue, the shipped Luna 6.1 replaces both older Luna versions.
    assert environment.older_models() == {'gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-6-luna'}
    home = tmp_path/'codex'; home.mkdir()
    (home/'models_cache.json').write_text(json.dumps({'models': [{'slug': s, 'visibility': 'list'} for s in listed]}))
    monkeypatch.setenv('CODEX_HOME', str(home))
    # This account still lists Luna 6 as its newest: templates must not hide it.
    assert environment.older_models() == {'gpt-6-sol', 'gpt-5.6-sol', 'gpt-5.6-luna'}
    old = {'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'x', 'model': 'gpt-5.6-luna'}}
    new = {'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'x', 'model': 'gpt-6-luna'}}
    assert not environment.older(new) and environment.older(old)
    assert environment.allows(old) == (True, None)  # a route the owner named keeps working
    assert not environment.listed('x', old) and environment.listed('x', new)  # but tare does not offer it
    assert environment.main(['--old-models', 'on']) == 0
    assert environment.listed('x', old) and 'older models offered' in capsys.readouterr().out
    assert environment.main(['--old-models', 'off']) == 0 and environment.older(old)


def test_a_newer_model_is_offered_for_a_route_the_owner_named_never_switched_silently(machine, tmp_path, monkeypatch):
    from relay import delegation, native_catalog
    home = tmp_path/'codex'; home.mkdir()
    (home/'models_cache.json').write_text(json.dumps({'models': [{'slug': s, 'visibility': 'list'}  # a future luna
                                                                 for s in ('gpt-6.1-luna', 'gpt-6-luna', 'gpt-6.1-sol')]}))
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path/'profiles'))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    (tmp_path/'profiles').mkdir(); (tmp_path/'profiles'/'codex-native-gpt-6-1-luna.json').write_text('{}')
    routing = tmp_path/'storage'/'serve'/'routing.json'
    routing.parent.mkdir(parents=True)
    routing.write_text(json.dumps({'implementer': [['codex-native-gpt-6-luna', 'bedrock-opus']]}))
    models = {'codex-native-gpt-6-luna': 'gpt-6-luna', 'bedrock-opus': 'claude-opus'}
    monkeypatch.setattr(native_catalog, 'select', lambda name, *a, **k: {'native_profile': {
        'protocol': 'openai-codex-responses' if 'codex' in name else 'bedrock-converse', 'endpoint': 'x',
        'model': models[name]}})
    monkeypatch.setattr(delegation, 'policy', lambda: dict(delegation.DEFAULT, enabled=True,
                                                           profile='codex-native-gpt-6-luna'))
    saved = []
    monkeypatch.setattr(delegation, 'configure', lambda value: saved.append(value) or value)
    found = environment.replacements()
    assert [(r['where'], r['newer'], r['to'], r['ready']) for r in found] == [
        ('/delegation profile', 'gpt-6.1-luna', 'codex-native-gpt-6-1-luna', True),
        ('routing implementer', 'gpt-6.1-luna', 'codex-native-gpt-6-1-luna', True)]
    assert environment.allows(native_catalog.select('codex-native-gpt-6-luna')) == (True, None)  # still runs
    assert 'Newer model: gpt-6.1-luna replaces gpt-6-luna' in environment.show({}, {
        'clis': {}, 'api_providers': {}, 'decision_models': {'jev': 'absent'}})
    assert len(environment.replace_older(found)) == 2
    assert saved[0]['profile'] == 'codex-native-gpt-6-1-luna'
    assert json.loads(routing.read_text())['implementer'] == [['codex-native-gpt-6-1-luna', 'bedrock-opus']]
    environment.save(environment.keep_older({}, found[:1]))  # the owner keeps gpt-6-luna for the explorer
    assert environment.replacements() == []  # not asked again; the routing policy already moved


def test_the_picker_starts_where_this_machine_can_run(machine, tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_LOCAL_GATEWAY')
    assert environment.default_route() == 'tare-auto'  # no gateway named: the first route it can run
    (tmp_path/'storage').mkdir(); (tmp_path/'storage'/'local-gateway.json').write_text('{"origin": "http://10.0.0.5:8080"}')
    assert environment.default_route() == 'local-qwen'  # a machine with a gateway, never set up: as before
    machine(clis={'use': True})
    assert environment.default_route() == 'tare-auto'


def test_decision_models_left_out_are_never_asked(machine, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'fixture-key')
    machine(**environment.choose(environment.choose({}, 'decision:jev', False), 'decision:local_judge', False))
    assert not environment.decides('jev') and not environment.decides('local_judge')
    assert turn_engine.judges({'state': 's', 'options': {'a': 'A'}, 'question': 'q'}) == {}
    assert turn_engine.judge_many('s', {'a': 'q'}) == {} and turn_engine.local_judge() is None
    assert work_forms.judge_form('Fix the typo.')() is None

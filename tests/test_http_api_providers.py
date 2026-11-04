"""Offline contract tests for the explicitly selected HTTP API presets."""
import pytest
from relay.native_catalog import select
from relay.native_generation import deployment_profile

PRESETS = [
    ('openai-api', 'gpt-6.1-sol', 'openai-responses', 'OPENAI_API_KEY'),
    ('gemini-api', 'gemini-4-argon', 'openai-chat', 'GEMINI_API_KEY'),
    ('claude-api', 'claude-haiku-5-5', 'anthropic-messages', 'ANTHROPIC_API_KEY'),
    ('claude-api-sonnet-5-5', 'claude-sonnet-5-5', 'anthropic-messages', 'ANTHROPIC_API_KEY'),
    ('claude-api-opus-5-5', 'claude-opus-5-5', 'anthropic-messages', 'ANTHROPIC_API_KEY'),
    ('claude-api-fable-5-1', 'claude-fable-5-1', 'anthropic-messages', 'ANTHROPIC_API_KEY'),
    ('openai-api-gpt-6-astra', 'gpt-6-astra', 'openai-responses', 'OPENAI_API_KEY'),
    ('openai-api-gpt-6-luna', 'gpt-6-luna', 'openai-responses', 'OPENAI_API_KEY'),
    ('openai-api-gpt-6-1-sol-pro', 'gpt-6.1-sol', 'openai-responses', 'OPENAI_API_KEY'),
    ('openai-api-gpt-6-astra-pro', 'gpt-6-astra', 'openai-responses', 'OPENAI_API_KEY'),
    ('gemini-api-3-8-flash', 'gemini-3.8-flash', 'openai-chat', 'GEMINI_API_KEY'),
]

@pytest.mark.parametrize('name,model,protocol,env', PRESETS)
def test_explicit_presets(name, model, protocol, env):
    dep = select(name)
    profile = deployment_profile(dep, model)
    assert profile.protocol == protocol
    assert dep['native_credential_env'] == env
    assert dep['native_counter_protocol'] == 'utf8-envelope-estimate-v1'
    with pytest.raises(ValueError, match='roster runtime model'):
        deployment_profile(dep, 'not-the-model')

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from textual.app import App
from textual.widgets import Input, Select, Static
from relay import native_generation, native_agent, console_connection, environment
from tare_tools_agent_runtime import CancellationToken

@pytest.fixture
def api():
    state = {'calls': [], 'status': 200, 'tools': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.reply({'data': [{'id': state['model']}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['body'] = body
            model = state['model']
            if self.path.endswith('/responses'):
                output = [{'type': 'message', 'id': 'msg-1', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'done'}]}]
                if state['tools']:
                    output = [{'type': 'function_call', 'id': 'fc-1', 'status': 'completed', 'call_id': 'call-1', 'name': 'tare_deliver', 'arguments': '{"summary":"done"}'}]
                value = {'id': 'resp-1', 'status': 'completed', 'model': model, 'output': output,
                         'usage': {'input_tokens': 20, 'output_tokens': 2, 'total_tokens': 22}}
            elif self.path.endswith('/messages'):
                content = [{'type': 'text', 'text': 'done'}]
                if state['tools']:
                    content = [{'type': 'tool_use', 'id': 'call-1', 'name': 'tare_deliver', 'input': {'summary': 'done'}}]
                value = {'id': 'msg-1', 'type': 'message', 'role': 'assistant', 'model': model,
                         'stop_reason': 'tool_use' if state['tools'] else 'end_turn', 'content': content}
            else:
                msg = {'role': 'assistant', 'content': 'done'}
                if state['tools']:
                    msg['tool_calls'] = [{'id': 'call-1', 'type': 'function', 'function': {'name': 'tare_deliver', 'arguments': '{"summary":"done"}'}}]
                value = {'model': model, 'choices': [{'finish_reason': 'tool_calls' if state['tools'] else 'stop', 'message': msg}]}
            self.reply(value)
        def reply(self, value):
            state['calls'].append((self.command, self.path, dict(self.headers)))
            raw = json.dumps(value).encode()
            self.send_response(state['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['endpoint'] = f'http://127.0.0.1:{server.server_port}/v1'
    try: yield state
    finally:
        server.shutdown(); server.server_close(); thread.join(2)

def local_dep(api, name):
    dep = select(name)
    api['model'] = dep['native_profile']['model']
    dep['native_profile']['endpoint'] = api['endpoint']
    return dep

@pytest.mark.parametrize('name,model,protocol,env', PRESETS)
def test_text_and_read_only_check(api, monkeypatch, name, model, protocol, env):
    import keyring
    dep = local_dep(api, name)
    monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(keyring, 'get_password', lambda ns, key: 'vault-fixture')
    ok, text, receipt = native_generation.generate_text(dep, model, 'Answer.', 10,
        credential_resolver=native_generation.vault_secret)
    assert ok and text == 'done', json.dumps(receipt)
    assert receipt['usage'] is None if protocol != 'openai-responses' else receipt['usage']['input_tokens'] == 20
    assert len(api['calls']) == 1
    assert api['body']['model'] == model
    if protocol == 'openai-responses':
        assert 1 <= len(api['body']['prompt_cache_key']) <= 64
    if name.endswith('-pro'):
        assert api['body']['reasoning'] == {'effort': 'medium', 'mode': 'pro'}
    headers = {k.lower(): v for k, v in api['calls'][0][2].items()}
    if protocol == 'anthropic-messages':
        assert headers['authorization'] == 'Bearer vault-fixture'
        assert headers['anthropic-version'] == '2023-06-01'
        assert 'x-api-key' not in headers
    else:
        assert headers['authorization'] == 'Bearer vault-fixture'
    monkeypatch.setenv(env, 'env-fixture')
    monkeypatch.setattr(console_connection, 'deployment_for', lambda key: dep)
    observation = console_connection.probe(('root', None, None, name, False), CancellationToken())
    assert observation.status == 'Model listed', observation
    assert [(c[0], c[1]) for c in api['calls']][1:] == [('GET', '/v1/models')]
    headers = {k.lower(): v for k, v in api['calls'][-1][2].items()}
    assert headers.get('x-api-key', headers.get('authorization')) in {'env-fixture', 'Bearer env-fixture'}
    api['status'] = 401
    before = len(api['calls'])
    failed = console_connection.probe(('root', None, None, name, False), CancellationToken())
    assert failed.status == 'Access refused' and len(api['calls']) == before + 1
    assert 'fixture' not in failed.detail

@pytest.mark.parametrize('name,model,protocol,env', PRESETS)
def test_public_cli_tools(api, tmp_path, monkeypatch, name, model, protocol, env):
    dep = local_dep(api, name)
    api['tools'] = True
    monkeypatch.setenv(env, 'offline-secret')
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(dep), encoding='utf-8')
    root = tmp_path / 'project'; root.mkdir()
    args = ['--root', str(root), '--session', str(tmp_path/'session'), '--native-deployment', str(path),
            '--instruction', 'Finish.', '--no-context']
    # The public console preference must work for the non-streaming Claude codec.
    if protocol == 'anthropic-messages' or env=='GEMINI_API_KEY': args.append('--stream')
    assert native_agent.main(args) == 0
    assert len(api['calls']) == 1
    assert 'tools' in api['body']
    assert api['body']['model'] == model
    if name.endswith('-pro'):
        assert api['body']['reasoning'] == {'effort': 'medium', 'mode': 'pro'}
    assert not api['body'].get('stream', False)
    saved = (tmp_path/'session'/'native-agent.json').read_text(encoding='utf-8')
    assert 'offline-secret' not in saved


def test_saved_gemini_streaming_session_uses_supported_transport_without_replacing_its_profile(api,tmp_path,monkeypatch):
    dep=local_dep(api,'gemini-api-3-8-flash');dep['native_streaming']=True
    api['tools']=True
    monkeypatch.setenv('GEMINI_API_KEY','offline-secret')
    path=tmp_path/'profile.json';path.write_text(json.dumps(dep),encoding='utf8')
    project=tmp_path/'project';project.mkdir();session=tmp_path/'session'
    assert native_agent.main(['--root',str(project),'--session',str(session),'--native-deployment',str(path),
        '--instruction','Finish.','--stream','--no-context'])==0
    before=json.loads((session/'native-agent.json').read_text(encoding='utf8'))
    assert before['deployment']['native_streaming'] is True and not api['body'].get('stream')
    assert native_agent.main(['--session',str(session),'--resume','--follow-up','Confirm.',
        '--message-id','next-gemini-message','--stream'])==0
    after=json.loads((session/'native-agent.json').read_text(encoding='utf8'))
    for key in ('deployment','instruction','prompt_capsule','conversation_tool_specs'):
        assert after.get(key)==before.get(key)
    assert not api['body'].get('stream') and len(api['calls'])==2


@pytest.mark.parametrize('key_name', ['OPENAI_API_KEY', 'CUSTOM_API_KEY'])
def test_setup_key_save_preserves_pending_choices(monkeypatch, key_name):
    from relay.console_setup import SetupPanel
    import keyring
    saved = []
    monkeypatch.setattr(environment, 'detect', lambda: {'clis': {}, 'api_providers': {key_name: 'missing'}, 'jev': False})
    if key_name == 'CUSTOM_API_KEY':
        from relay import native_catalog, native_profiles
        custom = native_catalog.select('openai-api')
        custom['native_credential_env'] = key_name
        monkeypatch.setattr(native_profiles, 'names', lambda: ['custom-api'])
        def selected(name):
            if name != 'custom-api':
                raise ValueError('invalid profile skipped without exposing its contents')
            return custom
        monkeypatch.setattr(native_catalog, 'select', selected)
    monkeypatch.setattr(environment, 'replacements', lambda: [])
    monkeypatch.setattr(environment, 'show', lambda *args: 'Offline setup')
    monkeypatch.setattr(keyring, 'set_password', lambda *args: saved.append(args))
    class Host(App):
        def on_mount(self): self.push_screen(SetupPanel())
    async def exercise():
        app = Host()
        async with app.run_test(size=(140, 80)) as pilot:
            panel = app.screen
            choice = panel.query_one('#setup-api-'+key_name, Select)
            choice.value = 'no'
            button = panel.query_one('#setup-key-'+key_name)
            button.scroll_visible(); await pilot.pause()
            await pilot.click('#setup-key-'+key_name); await pilot.pause()
            field = app.screen.query_one('#credential-value', Input)
            await pilot.press(*'fixture-secret')
            await pilot.click('#credential-cancel'); await pilot.pause()
            assert not saved and field.value == '' and choice.value == 'no'
            await pilot.click('#setup-key-'+key_name); await pilot.pause()
            field = app.screen.query_one('#credential-value', Input)
            assert field.password
            await pilot.press(*'fixture-secret')
            await pilot.click('#credential-save'); await pilot.pause()
            assert field.value == '' and choice.value == 'no'
            assert saved == [('universal-agent-harness', key_name, 'fixture-secret')]
    asyncio.run(exercise())


@pytest.mark.parametrize('name,param', [('openai-api', 'reasoning'), ('gemini-api', 'reasoning_effort')])
def test_api_reasoning_uses_its_protocol_and_keeps_original_profile(monkeypatch, name, param):
    from copy import deepcopy
    from relay import model_choices, native_catalog
    deployment = select(name)
    original = deepcopy(deployment)
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [{'deployment': name}])
    monkeypatch.setattr(native_catalog, 'select', lambda name: deepcopy(deployment))
    choices = model_choices.inventory()
    selected = model_choices.realization(choices, {'key': deployment['native_profile']['model'],
        'connection': name, 'reasoning': 'high'})
    params = json.loads(selected['native_profile']['parameters_json'])
    assert params[param] == ({'effort': 'high'} if param == 'reasoning' else 'high')
    assert deployment == original


def test_picker_searches_wire_model_and_connection_ids(monkeypatch):
    from textual.widgets import OptionList
    from relay.model_picker import ModelPicker
    from relay import model_choices, native_catalog
    deployment = select('openai-api')
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [{'deployment': 'openai-api'}])
    monkeypatch.setattr(native_catalog, 'select', lambda name: deployment)
    choices = model_choices.inventory()
    class Host(App):
        def on_mount(self): self.push_screen(ModelPicker(choices))
    async def exercise():
        app = Host()
        async with app.run_test(size=(120, 44)) as pilot:
            for query in ('gpt-6.1-sol', 'openai-api', 'OpenAI API'):
                app.screen.query_one('#model-search', Input).value = query
                await pilot.pause()
                assert 'gpt-6.1-sol' in app.screen.visible_keys
                assert app.screen.query_one('#model-list', OptionList).option_count == 2
    asyncio.run(exercise())


def test_vault_backend_read_error_never_exposes_its_message(monkeypatch):
    import keyring
    def fail(*args):
        raise RuntimeError('private-secret-in-backend-error')
    monkeypatch.setattr(keyring, 'get_password', fail)
    assert native_generation.vault_secret('OPENAI_API_KEY') is None


def test_check_uses_one_credential_and_endpoint_snapshot(api, monkeypatch):
    from copy import deepcopy
    dep = local_dep(api, 'claude-api')
    dep['native_anthropic_workspace'] = 'wrk_fixture'
    moved = deepcopy(dep)
    moved['native_profile']['endpoint'] = 'http://127.0.0.1:1/moved'
    snapshots = []
    def selected(key):
        snapshots.append(key)
        return dep if len(snapshots) == 1 else moved
    monkeypatch.setattr(console_connection, 'deployment_for', selected)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-key')
    observation = console_connection.probe(('root', None, None, 'claude-api', False), CancellationToken())
    assert observation.status == 'Model listed' and len(snapshots) == 1
    assert len(api['calls']) == 1
    headers = {k.lower(): v for k,v in api['calls'][0][2].items()}
    assert headers['anthropic-workspace-id'] == 'wrk_fixture'
    assert headers['authorization'] == 'Bearer fixture-key'

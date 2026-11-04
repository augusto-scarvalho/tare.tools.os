from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from relay import native_catalog, native_serve


class Upstream:
    """A fake OpenAI-chat backend that records what tare forwarded."""

    def __init__(self):
        self.requests = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                upstream.requests.append(body)
                if body.get('stream'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    for chunk in ({'choices': [{'delta': {'content': 'Hel'}}]},
                                  {'choices': [{'delta': {'content': 'lo'}}]},
                                  {'choices': [], 'usage': {'prompt_tokens': 11, 'completion_tokens': 2}}):
                        self.wfile.write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    raw = json.dumps({'choices': [{'message': {'role': 'assistant', 'tool_calls': [
                        {'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
                        'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 1}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/v1'


def last_usage(storage):
    path = storage/'serve'/'usage.jsonl'
    for _ in range(50):  # the record is written just after the reply
        if path.exists() and path.read_text(encoding='utf-8').strip():
            return json.loads(path.read_text(encoding='utf-8').splitlines()[-1])
        time.sleep(0.05)
    raise AssertionError('no usage record')


@pytest.fixture(autouse=True)
def unmanaged_serve_environment(monkeypatch):
    monkeypatch.delenv('TARE_MANAGED_HOME', raising=False)
    monkeypatch.delenv('TARE_RELEASE_ID', raising=False)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    upstream = Upstream()
    profiles = {
        'local-model': {'id': 'fleet-local', 'runner': 'native-http', 'native_profile': {
            'protocol': 'openai-chat', 'model': 'backend-model', 'endpoint': upstream.endpoint,
            'context_window': 131072, 'output_tokens': 6272,
            'parameters_json': json.dumps({'temperature': 1.0, 'chat_template_kwargs': {'reasoning_effort': 'low'}})}},
        'cli-model': {'id': 'cli', 'runner': 'native-http', 'native_profile': {
            'protocol': 'vendor-cli', 'model': 'x', 'endpoint': 'cli://codex', 'context_window': 1000}},
    }
    monkeypatch.setattr(native_catalog, 'select', lambda name: json.loads(json.dumps(profiles[name])))
    models = {'tare-local': 'local-model', 'tare-cli': 'cli-model'}
    server = ThreadingHTTPServer(('127.0.0.1', 0), native_serve.handler(models, 'secret'))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/v1'

    def call(path, body=None, key='secret'):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    yield call, upstream, tmp_path
    server.shutdown()
    upstream.server.shutdown()


def test_models_are_listed_with_their_context_window(serve):
    call, _, _ = serve
    status, raw = call('/models')
    assert status == 200
    assert {'id': 'tare-local', 'object': 'model', 'owned_by': 'tare', 'context_window': 131072,
            'tare_local': True} in json.loads(raw)['data']  # a loopback route runs on this machine


def test_client_tools_pass_through_with_the_profile_settings_and_guidance(serve):
    call, upstream, storage = serve
    tools = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}]
    status, raw = call('/chat/completions', {'model': 'tare-local', 'temperature': 0.1, 'max_tokens': 64000, 'tools': tools,
                                             'messages': [{'role': 'system', 'content': 'You are Copilot.'},
                                                          {'role': 'user', 'content': 'fix it'}]})
    assert status == 200 and json.loads(raw)['choices'][0]['finish_reason'] == 'tool_calls'
    sent = upstream.requests[0]
    assert sent['model'] == 'backend-model' and sent['tools'] == tools
    assert sent['temperature'] == 1.0 and sent['chat_template_kwargs'] == {'reasoning_effort': 'low'}
    assert sent['max_tokens'] == 6272
    assert sent['messages'][0]['content'].startswith('You are Copilot.') and native_serve.guidance({}) in sent['messages'][0]['content']
    row = last_usage(storage)
    assert row['model'] == 'tare-local' and row['usage'] == {'prompt_tokens': 5, 'completion_tokens': 1} and row['tools'] == 1


def test_streaming_is_relayed_in_order_and_usage_is_recorded(serve):
    call, upstream, storage = serve
    status, raw = call('/chat/completions', {'model': 'tare-local', 'stream': True,
                                             'messages': [{'role': 'user', 'content': 'hi'}]})
    text = ''.join(json.loads(line[6:])['choices'][0]['delta']['content']
                   for line in raw.decode().splitlines() if line.startswith('data: {') and '"delta"' in line)
    assert status == 200 and text == 'Hello' and raw.rstrip().endswith(b'data: [DONE]')
    assert upstream.requests[0]['stream_options'] == {'include_usage': True}
    assert upstream.requests[0]['messages'][0] == {'role': 'system', 'content': native_serve.guidance({})}
    row = last_usage(storage)
    assert row['stream'] is True and row['usage'] == {'prompt_tokens': 11, 'completion_tokens': 2}


@pytest.mark.parametrize('model,key,status', [('nope', 'secret', 404), ('tare-cli', 'secret', 400), ('tare-local', 'wrong', 401)])
def test_unknown_models_cli_routes_and_bad_keys_are_refused(serve, model, key, status):
    call, upstream, _ = serve
    with pytest.raises(urllib.error.HTTPError) as error:
        call('/chat/completions', {'model': model, 'messages': [{'role': 'user', 'content': 'x'}]}, key=key)
    assert error.value.code == status and upstream.requests == []


@pytest.mark.parametrize('path,key,status', [('/v1/chat/completions', 'wrong', 401), ('/v1/elsewhere', 'secret', 404)])
def test_a_refused_request_body_is_read_so_the_connection_stays_usable(path, key, status):
    """Unread body bytes became the next keep-alive request (and, on Windows, reset the socket)."""
    import http.client
    server = ThreadingHTTPServer(('127.0.0.1', 0), native_serve.handler({}, 'secret'))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        body = json.dumps({'model': 'tare-local', 'messages': [{'role': 'user', 'content': 'x' * 4096}]})
        connection.request('POST', path, body=body, headers={'Authorization': 'Bearer ' + key,
                                                             'Content-Type': 'application/json'})
        refused = connection.getresponse()
        assert refused.status == status and b'error' in refused.read()
        connection.request('GET', '/v1/models', headers={'Authorization': 'Bearer secret'})
        listed = connection.getresponse()
        assert listed.status == 200 and json.loads(listed.read())['data']
    finally:
        server.shutdown()


def test_a_request_that_allows_no_local_models_is_refused_a_local_single_route(serve):
    call, upstream, _ = serve
    body = {'model': 'tare-local', 'messages': [{'role': 'user', 'content': 'hi'}]}
    with pytest.raises(urllib.error.HTTPError) as refused:
        call('/chat/completions', {**body, 'tare_local_models': False})
    assert refused.value.code == 409 and not upstream.requests
    with pytest.raises(urllib.error.HTTPError) as invalid:
        call('/chat/completions', {**body, 'tare_local_models': 'no'})
    assert invalid.value.code == 400
    status, _ = call('/chat/completions', {**body, 'tare_local_models': True})
    assert status == 200 and 'tare_local_models' not in upstream.requests[0]  # never sent upstream

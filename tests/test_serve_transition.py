"""Offline HTTP contract for the stdlib updater's Serve transition boundary."""
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import uuid

import pytest

from relay import native_serve, serve_agent
from test_native_serve import serve  # reuse the existing upstream/profile fixture


@pytest.fixture(autouse=True)
def unmanaged(monkeypatch):
    monkeypatch.delenv('TARE_MANAGED_HOME', raising=False)
    monkeypatch.delenv('TARE_RELEASE_ID', raising=False)
    monkeypatch.setattr(native_serve, 'record', lambda row: None)


@pytest.fixture
def servers():
    running = []

    def start(handler=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler or native_serve.handler({}, 'secret'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def request(server, path='/health', body=None, key='secret'):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        conn.request('POST' if body is not None else 'GET', path,
                     body=json.dumps(body) if body is not None else None,
                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def control(server, action='drain', owner=None, **overrides):
    _, health = request(server)
    body = {k: health[k] for k in ('pid', 'release', 'installation')}
    body.update(action=action, owner=owner or str(uuid.uuid4()))
    body.update(overrides)
    return request(server, '/control/drain', body)


def observe_idle(monkeypatch):
    idle = threading.Event()
    original = native_serve.WorkTicket.done

    def done(ticket):
        original(ticket)
        with ticket.admission.lock:
            if ticket.admission.busy == 0:
                idle.set()

    monkeypatch.setattr(native_serve.WorkTicket, 'done', done)
    return idle


def test_inflight_drain_rejects_before_dispatch_and_stop_waits(servers, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    idle = observe_idle(monkeypatch)
    calls, result = [], []
    handler = native_serve.handler({}, 'secret')

    def chat(self):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        self.reply(200, {'finished': True})

    monkeypatch.setattr(handler, 'chat', chat)
    server = servers(handler)
    client = threading.Thread(target=lambda: result.append(request(server, '/chat/completions', {})))
    client.start()
    owner = str(uuid.uuid4())
    try:
        assert entered.wait(3)
        status, health = control(server, owner=owner)
        assert status == 200 and health['busy'] == 1 and health['draining']
        status, refused = request(server, '/chat/completions', {})
        assert status == 503 and refused['error']['retryable'] is True
        assert len(calls) == 1
        status, refused = control(server, 'stop', owner)
        assert status == 409 and refused['error'] == 'Serve is busy'
        assert request(server)[1]['busy'] == 1
    finally:
        release.set()
        client.join(3)
    assert not client.is_alive() and result == [(200, {'finished': True})]
    assert idle.wait(3)
    status, stopped = control(server, 'stop', owner)
    assert status == 200 and stopped['stopping'] and stopped['busy'] == 0
    assert server._tare_admission.admit() is None


def test_lease_renewal_resume_expiry_and_invalid_identity(servers, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(native_serve.time, 'monotonic', lambda: clock[0])
    server = servers()
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    assert control(server, owner=owner)[0] == 200
    state = server._tare_admission
    assert state.deadline == 1120
    for action in ('drain', 'resume', 'stop'):
        assert control(server, action, other)[0] == 409
        assert state.deadline == 1120 and state.owner == owner
    for overrides in ({'pid': -1}, {'release': 'another'}, {'installation': 'another'}):
        assert control(server, owner=owner, **overrides)[0] == 409
        assert state.deadline == 1120 and state.owner == owner
    assert control(server, owner='not-a-uuid')[0] == 400
    assert control(server, 'unknown', owner)[0] == 400
    clock[0] = 1100
    assert control(server, owner=owner)[0] == 200
    assert state.deadline == 1220
    clock[0] = 1121
    assert request(server)[1]['draining'] is True
    assert control(server, 'resume', owner)[0] == 200
    assert request(server)[1]['draining'] is False
    assert control(server, 'stop', owner)[0] == 409
    assert control(server, owner=other)[0] == 200
    clock[0] = 1241  # exactly 120 seconds; no sleeps or wall-clock dependency
    assert request(server)[1]['draining'] is False
    assert control(server, 'stop', other)[0] == 409
    ticket = state.admit()
    assert ticket is not None
    ticket.done()
    assert control(server, owner=owner)[0] == 200


def test_handler_class_can_be_reused_without_sharing_fence(servers):
    handler = native_serve.handler({}, 'secret')
    first, second = servers(handler), servers(handler)
    assert control(first)[0] == 200
    assert request(first)[1]['draining'] is True
    assert request(second)[1]['draining'] is False
    assert first._tare_admission is not second._tare_admission
    ticket = second._tare_admission.admit()
    assert ticket is not None
    assert request(first)[1]['busy'] == 0 and request(second)[1]['busy'] == 1
    ticket.done()


@pytest.mark.parametrize('failure', ['headers', 'disconnected'])
def test_agent_worker_stays_busy_after_client_handler_exits(servers, monkeypatch, failure):
    entered, release, handler_done = threading.Event(), threading.Event(), threading.Event()
    disconnected = threading.Event()
    idle = observe_idle(monkeypatch)
    calls = []

    def run_turn(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return 'finished'

    monkeypatch.setattr(serve_agent, 'run_turn', run_turn)
    handler = native_serve.handler({}, 'secret')
    original_post, original_headers = handler.do_POST, handler.end_headers
    injected = []

    def post(self):
        try:
            original_post(self)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            handler_done.set()

    def headers(self):
        inject = self.path == '/chat/completions' and not injected
        if inject:
            injected.append(True)
            assert entered.wait(3)
            if failure == 'headers':
                raise BrokenPipeError('deterministic header send failure')
        original_headers(self)
        if inject and failure == 'disconnected':
            stream = self.wfile

            class DisconnectedStream:
                closed = False

                def write(self, data):
                    assert disconnected.wait(3)
                    raise BrokenPipeError('client disconnected before stream write')

                def flush(self):
                    return stream.flush()

                def close(self):
                    self.closed = True
                    return stream.close()

            self.wfile = DisconnectedStream()

    monkeypatch.setattr(handler, 'do_POST', post)
    monkeypatch.setattr(handler, 'end_headers', headers)
    server = servers(handler)
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        conn.request('POST', '/chat/completions', json.dumps({'model': 'tare', 'stream': True, 'messages': []}),
                     headers={'Authorization': 'Bearer secret'})
        assert entered.wait(3)
        if failure == 'disconnected':
            response = conn.getresponse()
            assert response.status == 200
            # Close a real HTTP client, then inject its write failure deterministically:
            # TCP reset timing differs between Windows and Unix.
            response.close()
            conn.close()
            disconnected.set()
        else:
            with pytest.raises(http.client.RemoteDisconnected):
                conn.getresponse()
        assert handler_done.wait(3)
        owner = str(uuid.uuid4())
        status, health = control(server, owner=owner)
        assert status == 200 and health['busy'] == 1
        assert control(server, 'stop', owner)[0] == 409
        assert request(server, '/chat/completions', {'model': 'tare', 'messages': []})[0] == 503
        assert calls == [True]
    finally:
        conn.close()
        disconnected.set()
        release.set()
    assert handler_done.wait(3)
    assert idle.wait(3)
    assert request(server)[1]['busy'] == 0


def test_existing_upstream_fixture_health(serve):
    call, upstream, _ = serve
    status, raw = call('/health')
    health = json.loads(raw)
    assert status == 200 and health['busy'] == 0 and health['control_protocol'] == 1
    assert not upstream.requests


def test_managed_stale_process_refused_before_listener_and_on_admission(tmp_path, monkeypatch, servers):
    home = tmp_path/'managed'
    home.mkdir()
    active = home/'active.json'
    active.write_text(json.dumps({'current': 'new'}), encoding='utf-8')
    monkeypatch.setenv('TARE_MANAGED_HOME', str(home))
    monkeypatch.setenv('TARE_RELEASE_ID', 'old')

    def forbidden(*args, **kwargs):
        pytest.fail('stale process opened a listener')

    with monkeypatch.context() as patch:
        patch.setattr(native_serve, 'ThreadingHTTPServer', forbidden)
        assert native_serve.main(['--model', 'test=fixture', '--no-discovery']) == 2
    active.write_text(json.dumps({'current': 'old'}), encoding='utf-8')
    server = servers()
    _, health = request(server)
    assert health['release'] == 'old' and len(health['installation']) == 64
    active.write_text(json.dumps({'current': 'new'}), encoding='utf-8')
    assert request(server, '/chat/completions', {'model': 'tare', 'messages': []})[0] == 503
    assert request(server)[1]['busy'] == 0
    # Control remains available to drain the old process after a selection change.
    assert control(server)[0] == 200


@pytest.mark.parametrize('case,status', [('auth', 401), ('json', 400), ('owner', 400), ('identity', 409)])
def test_control_refusal_consumes_body_and_preserves_keepalive(servers, case, status):
    server = servers()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    _, health = request(server)
    body = {k: health[k] for k in ('pid', 'release', 'installation')}
    body.update(action='drain', owner=str(uuid.uuid4()))
    if case == 'owner':
        body['owner'] = 'invalid'
    if case == 'identity':
        body['installation'] = 'wrong-installation'
    raw = '{' if case == 'json' else json.dumps(body)
    try:
        conn.request('POST', '/control/drain', raw,
                     headers={'Authorization': 'Bearer ' + ('wrong' if case == 'auth' else 'secret')})
        response = conn.getresponse()
        assert response.status == status
        response.read()
        conn.request('GET', '/health', headers={'Authorization': 'Bearer secret'})
        response = conn.getresponse()
        assert response.status == 200 and not json.loads(response.read())['draining']
    finally:
        conn.close()


@pytest.mark.parametrize('framing', ['oversize', 'chunked', 'duplicate', 'missing'])
def test_control_bad_framing_closes_connection(servers, framing):
    server = servers()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        conn.putrequest('POST', '/control/drain')
        conn.putheader('Authorization', 'Bearer secret')
        if framing == 'oversize':
            conn.putheader('Content-Length', str(native_serve.CONTROL_MAX_BODY + 1))
        elif framing == 'chunked':
            conn.putheader('Transfer-Encoding', 'chunked')
        elif framing == 'duplicate':
            conn.putheader('Content-Length', '2')
            conn.putheader('Content-Length', '2')
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == (413 if framing == 'oversize' else 400)
        response.read()
        assert response.fp is None
        # Verify the close header and the client's release of the connection.
        assert response.getheader('Connection').lower() == 'close'
        assert conn.sock is None
    finally:
        conn.close()
    assert not request(server)[1]['draining']


def test_control_rejects_nonloopback_peer(servers, monkeypatch):
    handler = native_serve.handler({}, 'secret')
    original = handler.control

    def remote(self):
        self.client_address = ('192.0.2.1', self.client_address[1])
        return original(self)

    monkeypatch.setattr(handler, 'control', remote)
    server = servers(handler)
    status, payload = control(server)
    assert status == 403 and 'loopback' in payload['error']['message']
    assert not request(server)[1]['draining']

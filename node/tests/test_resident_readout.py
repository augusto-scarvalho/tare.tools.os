"""Resident-only requests keep GPU ownership in the server, even if clients die."""
import contextlib
import json
import socket
import threading
import time
from types import SimpleNamespace
import pytest
from test_gateway_gpu_coordination import serving, post, gateway


class Lease:
    is_enabled = True
    held = False
    timeout_seen = None

    @contextlib.contextmanager
    def hold(self, *args, timeout=None, **_):
        self.timeout_seen = timeout
        self.held = True
        try: yield {}
        finally: self.held = False


def payload(pid):
    return {'model': 'one', 'prompt': [1, 2], 'n_predict': 1,
            '_tare_resident_backend_pid': pid, 'stream': False}


def test_exact_resident_is_not_reloaded_and_internal_field_is_removed(monkeypatch):
    with serving() as rt:
        rt.ensure_model('one'); pid = rt.process.pid
        rt.gpu_lease = Lease()
        monkeypatch.setattr(rt, 'ensure_model', lambda *_: pytest.fail('resident request must never load'))
        def proxy(handler, body, **kwargs):
            assert rt.gpu_lease.held and kwargs['timeout'] == 15
            assert '_tare_resident_backend_pid' not in json.loads(body)
            gateway.send_json(handler, 200, {'ok': True})
        monkeypatch.setattr(gateway, 'proxy_request', proxy)
        code, value = post(rt.endpoint, '/completion', payload(pid))
        assert code == 200 and value['ok']
        assert rt.gpu_lease.timeout_seen == pytest.approx(.15, abs=.01)  # less the time in the text queue


@pytest.mark.parametrize('change', ['pid', 'model', 'dead', 'coordination'])
def test_residency_race_fails_before_any_loading_or_inference(monkeypatch, change):
    with serving() as rt:
        rt.ensure_model('one'); pid = rt.process.pid
        rt.gpu_lease = Lease()
        if change == 'pid': pid += 1
        elif change == 'model': rt.model_id = 'two'
        elif change == 'dead': rt.process.terminate(); rt.process.wait()
        else: rt.gpu_lease.is_enabled = False
        monkeypatch.setattr(rt, 'ensure_model', lambda *_: pytest.fail('load'))
        monkeypatch.setattr(gateway, 'proxy_request', lambda *_a, **_kw: pytest.fail('inference'))
        code, value = post(rt.endpoint, '/completion', payload(pid))
        assert code == 409 and value['error']['type'] == 'resident_backend_unavailable'


def test_disconnected_client_does_not_release_server_lease(monkeypatch):
    with serving() as rt:
        rt.ensure_model('one'); rt.gpu_lease = Lease()
        entered, finish, drained = threading.Event(), threading.Event(), threading.Event()
        def proxy(*args, **kwargs):
            entered.set(); finish.wait(3)
            assert rt.gpu_lease.held
            drained.set()
            raise ConnectionResetError('client disappeared')
        monkeypatch.setattr(gateway, 'proxy_request', proxy)
        port = int(rt.endpoint.rsplit(':',1)[1]); client = socket.create_connection(('127.0.0.1',port))
        raw = json.dumps(payload(rt.process.pid)).encode()
        client.sendall(b'POST /completion HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw)
        assert entered.wait(2); client.close()
        assert rt.gpu_lease.held
        finish.set(); assert drained.wait(2)
        deadline=time.monotonic()+2
        while rt.gpu_lease.held and time.monotonic()<deadline: time.sleep(.01)
        assert not rt.gpu_lease.held and rt.process is None

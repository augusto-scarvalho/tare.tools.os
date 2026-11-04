import json

import pytest

from relay import gpu


def test_gpu_status_says_who_holds_the_gpu_who_waits_and_how_long(monkeypatch, capsys):
    busy = {'current_model': 'qwen38', 'gpu_coordination': {
        'enabled': True, 'held': True, 'owner': {'kind': 'image', 'reason': 'lora cohort-26', 'nonce': 'never-show'},
        'queue': [{'kind': 'text'}], 'eta_seconds': 250}}
    text = gpu.describe(busy)
    assert text.splitlines() == ['GPU held by image (lora cohort-26); model loaded: qwen38', '  waiting: text',
                                 '  text gets it in about 4 min']
    assert gpu.describe({'gpu_coordination': {'enabled': True, 'held': False, 'queue': []}}) == 'GPU free'
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://gpu-node.test:8080')
    monkeypatch.setattr(gpu, 'status', lambda origin: busy)
    assert gpu.main(['status']) == 0 and 'never-show' not in capsys.readouterr().out
    def down(origin):
        raise OSError('refused')
    monkeypatch.setattr(gpu, 'status', down)
    assert gpu.main(['status']) == 1 and 'did not answer' in capsys.readouterr().out


def test_gpu_report_sums_what_serve_spent_elsewhere_while_an_image_job_held_the_gpu(tmp_path):
    import json
    from relay.native_installation import storage_root
    from relay.serve_agent import gpu_cause
    from relay.turn_engine import GPU_BUSY
    chats = storage_root()/'serve'/'chats'
    chats.mkdir(parents=True)
    def session(name, tokens, cost=None):
        folder = tmp_path/name
        folder.mkdir()
        (folder/'usage.json').write_text(json.dumps({'tokens': {'total_tokens': {'known_sum': tokens}},
                                                     'known_cost_by_currency': cost or {}}), encoding='utf-8')
        return str(folder)
    assert gpu_cause({'reason': 'gpu_busy', 'retry_after': 250}) == GPU_BUSY + ', back in about 4 min'
    assert gpu_cause({'reason': 'gpu_busy', 'retry_after': 42000}) == GPU_BUSY + ', back in about 11 h 40 min'
    assert gpu_cause({'reason': 'gpu_busy', 'retry_after': None}) == GPU_BUSY and gpu_cause(None) is None
    (chats/'a.json').write_text(json.dumps({'sessions': [
        {'session': session('s1', 1000), 'route': 'qwen38-gsq', 'skipped': []},  # the local turn itself
        {'session': session('s2', 5000), 'route': 'codex-native-gpt-6-luna', 'skipped': [],
         'reason': gpu_cause({'retry_after': 250})},  # moved mid-chat
        {'session': session('s3', 2000, {'USD': '0.25'}), 'route': 'bedrock-sonnet',
         'skipped': [{'route': 'qwen38-gsq', 'reason': GPU_BUSY}]},  # local skipped when the chat started
        {'session': session('s4', 2000, {'USD': '0.5'}), 'route': 'bedrock-sonnet',
         'skipped': [{'route': 'qwen38-gsq', 'reason': GPU_BUSY}]},
        {'session': session('s5', 9000), 'route': 'bedrock-sonnet', 'skipped': [], 'reason': 'BLOCKED'}]}), encoding='utf-8')
    rows = gpu.report(7)
    assert rows == {'codex-native-gpt-6-luna': {'sessions': 1, 'tokens': 5000, 'cost': {}},
                    'bedrock-sonnet': {'sessions': 2, 'tokens': 4000, 'cost': {'USD': '0.75'}}}
    text = gpu.report_text(rows, 7)
    assert '3 tare serve session(s)' in text and 'bedrock-sonnet: 2 session(s), 4,000 tokens, USD 0.75' in text
    assert gpu.report(7, now=10**12) == {} and 'no tare serve turn' in gpu.report_text({}, 7)


def fake_hold(monkeypatch, refusals=()):
    """compute_plane.gpu_lease_client.hold without a node: records what it was asked and whether it released."""
    import contextlib
    from compute_plane import gpu_lease_client as lease
    calls, pending = [], list(refusals)
    @contextlib.contextmanager
    def hold(origin, token, name, max_seconds, **options):
        if pending:
            raise pending.pop(0)
        calls.append({'origin': origin, 'token': token, 'name': name, 'max_seconds': max_seconds, **options})
        try:
            yield {'id': 'r1', 'secret': 's3cret', 'fence': 1, 'hard_until': 0}
        finally:
            calls[-1]['released'] = True
    monkeypatch.setattr(lease, 'hold', hold)
    monkeypatch.setenv('TARE_GPU_TOKEN', 'node-token')
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://gpu-node.test:8080')
    return calls


def test_gpu_run_gives_its_command_the_reservation_and_releases_it(monkeypatch):
    import sys
    calls = fake_hold(monkeypatch)
    check = ("import os, sys; sys.exit(0 if (os.environ['TARE_GPU_RESERVATION'], os.environ['TARE_GPU_RESERVATION_ID'])"
             " == ('s3cret', 'r1') else 3)")
    assert gpu.main(['run', 'bench', '2h', '--', sys.executable, '-c', check]) == 0
    [call] = calls
    assert call['origin'] == 'http://gpu-node.test:8080' and call['token'] == 'node-token'
    assert call['name'] == 'bench' and call['max_seconds'] == 7200 and call['released']


def test_gpu_hold_waits_while_the_gpu_is_taken_and_gives_up_on_other_refusals(monkeypatch):
    from compute_plane.gpu_lease_client import ReservationRefused
    import contextlib
    calls = fake_hold(monkeypatch, [ReservationRefused(503, 'gpu_busy', 5), ReservationRefused(409, 'gpu_reserved')])
    slept = []
    with contextlib.ExitStack() as stack:
        assert gpu.reserve(stack, 'bench', 3600, 600, sleep=slept.append)['secret'] == 's3cret'
    assert slept == [5, 30] and calls[0]['released']
    fake_hold(monkeypatch, [ReservationRefused(503, 'gpu_busy', 5)])
    with contextlib.ExitStack() as stack, pytest.raises(SystemExit):  # no --wait: one try
        gpu.reserve(stack, 'bench', 3600, 0, sleep=slept.append)
    fake_hold(monkeypatch, [ReservationRefused(401, 'unauthorized')])
    with contextlib.ExitStack() as stack, pytest.raises(SystemExit, match='unauthorized'):
        gpu.reserve(stack, 'bench', 3600, 600, sleep=slept.append)
    assert gpu.seconds('2h') == 7200 and gpu.seconds('90m') == 5400 and gpu.seconds('45') == 45


def test_only_the_reservations_process_shows_its_secret_and_others_skip_the_reserved_gpu(monkeypatch):
    import io
    from relay import turn_engine
    from relay.challenger import client_headers
    local = {'native_profile': {'endpoint': 'http://127.0.0.1:8080/v1'}}
    hosted = {'native_profile': {'endpoint': 'https://api.example.test/v1'}}
    monkeypatch.delenv('TARE_GPU_RESERVATION', raising=False)
    assert 'X-Tare-GPU-Reservation' not in client_headers(local)
    monkeypatch.setenv('TARE_GPU_RESERVATION', 's3cret')
    assert client_headers(local)['X-Tare-GPU-Reservation'] == 's3cret' and client_headers(hosted) == {}
    monkeypatch.setattr('relay.challenger.local', lambda deployment: True)
    body = json.dumps({'gpu_coordination': {'enabled': True, 'held': True, 'owner': {'kind': 'text'},
                                            'reservation': {'id': 'r1', 'name': 'bench', 'holder': 'acer'}}})
    monkeypatch.setattr('urllib.request.urlopen', lambda url, timeout: io.StringIO(body))
    monkeypatch.delenv('TARE_GPU_RESERVATION_ID', raising=False)
    why = turn_engine.local_gpu_busy(local)
    assert why == 'local GPU reserved for bench by acer' and turn_engine.gpu_taken(why)
    monkeypatch.setenv('TARE_GPU_RESERVATION_ID', 'r1')
    assert turn_engine.local_gpu_busy(local) is None  # the bench itself
    from relay.serve_agent import gpu_cause
    assert gpu_cause({'reason': 'gpu_reserved', 'retry_after': 5400}) == (
        'local GPU reserved by another holder, back in about 1 h 30 min')

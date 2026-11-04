"""Offline observer evidence, not a production rotation or data-survival proof."""
import hashlib
import json

import pytest

from relay import native_product as product

A, B, C = 'a'*64, 'b'*64, 'c'*64


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def observed(tmp_path, monkeypatch):
    home = tmp_path
    release = home/'releases'/B[:16]
    release.mkdir(parents=True)
    payload = {'object': 'list', 'data': [{'id': 'tare', 'object': 'model',
               'owned_by': 'tare', 'context_window': 1000}]}
    catalog = product.run_catalog_trial(lambda: payload, lambda: payload)
    catalog.update(incumbent=A, candidate=B)
    save(release/'catalog-trial.json', catalog)
    save(release/'ready.json', {'bundle': B})
    selected = {'current': B, 'previous': A, 'last_transition': {
        'from': A, 'to': B, 'operation': 'update',
        'qualification': 'releases/'+B[:16]+'/ready.json',
        'catalog_trial': {'path': 'releases/'+B[:16]+'/catalog-trial.json',
                          'sha256': hashlib.sha256((release/'catalog-trial.json').read_bytes()).hexdigest()}}}
    save(home/'active.json', selected)
    health = {'status': 'ok', 'pid': 7, 'release': B, 'installation': product._installation(home),
              'control_protocol': 1, 'busy': 0, 'draining': False, 'stopping': False}
    monkeypatch.setattr(product, 'serve_health', lambda: health)
    return home, release, selected, health


def report(observed):
    return product.transition_status(observed[0])


def test_positive_and_public_command_are_read_only(observed, monkeypatch, capsys):
    home, _, _, _ = observed
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail('observer attempted an effect')
    for name in ('install', 'rollback', 'auto_update', 'restart_serve', 'serve_control',
                 'write_json', 'exclusive', 'check_release', 'release_command'):
        monkeypatch.setattr(product, name, forbidden)
    for name in ('run', 'call', 'Popen'):
        monkeypatch.setattr(product.subprocess, name, forbidden)
    monkeypatch.setattr(product.Path, 'write_text', forbidden)
    monkeypatch.setattr(product.Path, 'write_bytes', forbidden)
    assert product.main(['--home', str(home), 'transition-status']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['completed'] is True
    assert result['reasons'] == ['qualified_rotation_complete']
    assert result['current'] == B and result['previous'] == A
    assert result['server']['release'] == B
    assert before == {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change,reason', [
    ('missing_trial', 'catalog_trial_missing'), ('no_receipt', 'receipt_missing'),
    ('stale', 'receipt_pair_mismatch'), ('digest', 'catalog_digest_mismatch'),
    ('ready', 'ready_identity_mismatch'), ('path', 'evidence_path_invalid'),
    ('missing', 'evidence_missing'), ('malformed', 'evidence_malformed'),
    ('oversized', 'evidence_oversized'), ('wrong_old', 'catalog_trial_mismatch'),
    ('wrong_new', 'catalog_trial_mismatch'), ('not_pass', 'catalog_trial_mismatch'),
])
def test_evidence_fail_closed(observed, change, reason):
    home, release, selected, _ = observed
    receipt = selected['last_transition']
    if change == 'missing_trial':
        receipt.pop('catalog_trial')
    elif change == 'no_receipt':
        selected.pop('last_transition')
    elif change == 'stale':
        receipt['from'] = C
    elif change == 'digest':
        (release/'catalog-trial.json').write_bytes(b'{}')
    elif change == 'ready':
        save(release/'ready.json', {'bundle': A})
    elif change == 'path':
        receipt['catalog_trial']['path'] = '../../outside.json'
    elif change == 'missing':
        (release/'catalog-trial.json').unlink()
    elif change in ('malformed', 'oversized'):
        (release/'catalog-trial.json').write_bytes(b'{' if change == 'malformed' else b' '*65537)
    else:
        catalog = json.loads((release/'catalog-trial.json').read_text())
        catalog[{'wrong_old': 'incumbent', 'wrong_new': 'candidate', 'not_pass': 'status'}[change]] = C
        save(release/'catalog-trial.json', catalog)
        receipt['catalog_trial']['sha256'] = product.digest(release/'catalog-trial.json')
    save(home/'active.json', selected)
    result = report(observed)
    assert not result['completed']
    assert reason in result['reasons']


@pytest.mark.parametrize('changes,reason', [
    ({'release': A}, 'incumbent_still_serving'),
    ({'release': C}, 'server_identity_mismatch'),
    ({'installation': 'other'}, 'server_identity_mismatch'),
    ({'control_protocol': 0}, 'server_identity_mismatch'),
    ({'draining': True}, 'server_not_settled'),
    ({'stopping': True}, 'server_not_settled'),
    ({'draining': None}, 'server_not_settled'),
])
def test_health_is_identity_bound(observed, changes, reason):
    observed[3].update(changes)
    result = report(observed)
    assert not result['completed'] and reason in result['reasons']


@pytest.mark.parametrize('operation', ['rollback', 'auto_rollback', 'install'])
def test_non_rotations(observed, operation):
    home, _, selected, _ = observed
    selected['last_transition']['operation'] = operation
    if operation == 'install':
        selected['previous'] = selected['last_transition']['from'] = None
    save(home/'active.json', selected)
    assert report(observed)['reasons'] == ['initial_install' if operation == 'install' else operation]
    assert not report(observed)['completed']


def test_pointer_changes_during_health(observed, monkeypatch):
    home, _, selected, health = observed
    def changed():
        selected['current'] = C
        save(home/'active.json', selected)
        return health
    monkeypatch.setattr(product, 'serve_health', changed)
    result = report(observed)
    assert result['reasons'] == ['pointer_changed']
    assert not result['completed'] and result['serve'] == 'unknown' and 'server' not in result


@pytest.mark.parametrize('content,reason', [(b'{', 'evidence_malformed'),
                                            (b' '*65537, 'evidence_oversized'),
                                            (b'[]', 'evidence_malformed')],
                         ids=['malformed-pointer', 'oversized-pointer', 'non-object-pointer'])
def test_bad_pointer(observed, content, reason):
    (observed[0]/'active.json').write_bytes(content)
    assert report(observed)['reasons'] == [reason]


def test_health_failure_preserves_uncertainty(observed, monkeypatch):
    def failed():
        raise OSError('offline')
    monkeypatch.setattr(product, 'serve_health', failed)
    assert report(observed)['reasons'] == ['health_unavailable']


@pytest.mark.parametrize('payload', [b'['*2000+b'0'+b']'*2000, b'{', b' '*65537, b'[]', b'null'],
                         ids=['deep-json', 'malformed-json', 'oversized-response', 'list', 'null'])
def test_bounded_health_decode_failure_is_unavailable(monkeypatch, payload):
    import urllib.request
    import sys
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 65537
            return payload
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: Reply())
    prior = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        assert product.serve_health() is None
    finally:
        sys.setrecursionlimit(prior)


@pytest.mark.parametrize('change', [{'phase': 'shadow'}, {'selected': 'incumbent'},
                                    {'phase': None}, {'selected': None}])
def test_only_completed_canary_selecting_candidate_qualifies(observed, change):
    home, release, selected, _ = observed
    path = release/'catalog-trial.json'
    catalog = json.loads(path.read_bytes())
    for key, value in change.items():
        if value is None:
            catalog.pop(key)
        else:
            catalog[key] = value
    save(path, catalog)
    selected['last_transition']['catalog_trial']['sha256'] = product.digest(path)
    save(home/'active.json', selected)
    result = report(observed)
    assert result['completed'] is False
    assert result['reasons'] == ['catalog_trial_mismatch']


def test_path_escape_is_not_read(observed, monkeypatch):
    home, _, selected, _ = observed
    outside = home/'outside.json'
    save(outside, {'secret': True})
    selected['last_transition']['catalog_trial']['path'] = str(outside)
    save(home/'active.json', selected)
    original = product.Path.open
    def guarded(path, *args, **kwargs):
        assert path != outside
        return original(path, *args, **kwargs)
    monkeypatch.setattr(product.Path, 'open', guarded)
    assert report(observed)['reasons'] == ['evidence_path_invalid']

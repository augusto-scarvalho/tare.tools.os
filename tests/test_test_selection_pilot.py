"""Offline custody/control boundaries; no runner or subprocess execution."""
import json
import sys

import pytest

from relay import test_selection_pilot as pilot


@pytest.fixture
def project(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    monkeypatch.setattr(pilot.native_installation, 'storage_root', lambda: storage)
    root = tmp_path / 'project'
    root.mkdir()
    return root


def start(project, **kwargs):
    result = pilot.start(project, [sys.executable, '-m', 'pytest', '-q'], now=100, **kwargs)
    return result['wrapper_argv'][-1]


def observation(**kwargs):
    return dict(complete=True, omitted=False, seconds=1.25, **kwargs)


def test_start_is_explicit_bound_and_never_executes(project, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail('start must not execute')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    result = pilot.start(project, [sys.executable, '-m', 'pytest', '-q'], now=100)
    wrapper = result['wrapper_argv']
    assert wrapper[:4] == [sys.executable, '-m', 'relay.test_selection_pilot', 'run']
    assert 'pytest' not in wrapper
    state = pilot.read(now=101)
    assert state['argv'] == [sys.executable, '-m', 'pytest', '-q']
    assert state['baseline'] is None and state['max_runs'] == 5
    assert state['deadline'] == 100 + 7 * 86400
    with pytest.raises(ValueError):
        with pilot.locked_state(state['id']) as edited:
            edited['argv'].append('--collect-only')
    assert pilot.read(now=101)['argv'] == state['argv']
    with pytest.raises(ValueError, match='review'):
        start(project)
    pilot.review('keep-full', now=102)
    replacement = start(project)
    assert replacement != wrapper[-1]
    with pytest.raises(ValueError, match='Stale'):
        pilot.begin_run(wrapper[-1], now=103)


@pytest.mark.parametrize('node', ['../test_a.py', '/test_a.py', 'a/../test_a.py',
                                 'a\\test_a.py', 'a.py::', 'a.py::../bad',
                                 'C:/test_a.py', '-bad.py', 'a.py::test;bad'])
def test_unsafe_mandatory_rejected(project, node):
    with pytest.raises(ValueError):
        start(project, mandatory=[node])


@pytest.mark.parametrize('argv', [['pytest'], ['python', '-c', 'pytest'],
                                 ['sh', '-m', 'pytest'],
                                 ['python', '-m', 'pytest', 'x' * 4097]])
def test_wrong_commands_rejected(project, argv):
    with pytest.raises(ValueError):
        pilot.start(project, argv, now=100)


def test_canonical_nodes_and_byte_limits(project):
    identity = start(project, mandatory=['tests/a.py::test_ok[x-1]', 'tests/a.py::test_ok[x-1]'])
    assert pilot.read(now=101)['mandatory'] == ['tests/a.py::test_ok[x-1]']
    with pytest.raises(ValueError, match='budget'):
        with pilot.locked_state(identity) as state:
            state['baseline'] = 'x' * pilot.BASELINE_LIMIT
    assert pilot.read(now=101)['baseline'] is None
    pilot.begin_run(identity, now=101)
    with pytest.raises(ValueError):
        pilot.record_observation(identity, {'stdout': 'private'}, now=102)
    with pytest.raises(ValueError):
        pilot.write_hook(identity, 'x' * pilot.HOOK_LIMIT)
    with pytest.raises(ValueError):
        pilot._observation({'command_digest': 'x' * (pilot.OBS_LIMIT + 1)})


def test_five_invocations_retire_without_qualification(project):
    identity = start(project)
    with pilot.locked_state(identity) as state:
        state['baseline'] = {'collection_digest': 'a' * 64}
    for index in range(5):
        assert pilot.begin_run(identity, now=101 + index)['capture_pending']
        pilot.record_observation(identity, observation(), now=101 + index)
    state = pilot.read(now=110)
    assert state['status'] == 'REVIEW_REQUIRED'
    assert state['reason'] == 'invocation cap'
    assert state['baseline'] is None and len(state['observations']) == 5
    assert pilot.begin_run(identity, now=111)['runs'] == 5
    with pytest.raises(ValueError):
        pilot.record_observation(identity, observation(), now=112)
    report = pilot._load('reviews.json', pilot.REPORT_LIMIT)
    assert report[-1]['observations'] == 5
    assert report[-1]['net_savings'] is None


def test_summary_write_failure_preserves_baseline_and_hook(project, monkeypatch):
    identity = start(project)
    with pilot.locked_state(identity) as state:
        state['baseline'] = {'bounded': 'baseline'}
    pilot.begin_run(identity, now=101)
    pilot.write_hook(identity, {'collected': 3})
    save = pilot.save_json

    def failing(path, value):
        if path.name == 'reviews.json':
            raise OSError('disk full')
        return save(path, value)

    monkeypatch.setattr(pilot, 'save_json', failing)
    with pytest.raises(OSError, match='disk full'):
        pilot.read(now=100 + 7 * 86400)
    with pilot.locked_state(identity) as state:
        assert state['baseline'] == {'bounded': 'baseline'}
    assert (pilot.directory() / 'hook.json').exists()
    monkeypatch.setattr(pilot, 'save_json', save)
    state = pilot.read(now=100 + 7 * 86400)
    assert state['status'] == 'REVIEW_REQUIRED' and state['baseline'] is None
    assert not (pilot.directory() / 'hook.json').exists()
    assert pilot._load('reviews.json', pilot.REPORT_LIMIT)[-1]['reason'] == 'deadline'


@pytest.mark.parametrize('capture', [{'complete': False, 'omitted': False},
                                    {'complete': True, 'omitted': True}, {}])
def test_incomplete_or_omitted_capture_stops(project, capture):
    identity = start(project)
    pilot.begin_run(identity, now=101)
    pilot.record_observation(identity, capture, now=102)
    assert pilot.read(now=103)['status'] == 'REVIEW_REQUIRED'


def test_abandoned_capture_requires_review(project):
    identity = start(project)
    pilot.begin_run(identity, now=101)
    assert pilot.read(now=102)['reason'] == 'incomplete capture'


def test_review_usage_and_rotation(project):
    for index in range(10):
        identity = start(project)
        pilot.review('keep-full', now=110 + index)
    state = pilot.read(now=200)
    assert state['decision'] == 'keep-full' and state['status'] == 'REVIEWED'
    report = pilot._load('reviews.json', pilot.REPORT_LIMIT)
    assert len(report) == 8 and report[-1]['id'] == identity
    assert (pilot.directory() / 'reviews.json').stat().st_size <= pilot.REPORT_LIMIT
    body = pilot.usage_rows(project, now=200)[0]['body']
    assert str(pilot.directory()) in body
    assert 'Next action: tare test-selection status' in body
    assert 'Next action: tare test-selection review' not in body
    assert 'net savings Unknown' in body
    assert 'full configured check remains authoritative' in body
    assert pilot.usage_rows(project.parent, now=200) == []
    assert pilot._bytes() < pilot.TOTAL_LIMIT


def test_reviews_envelope_roundtrip_two_cycles(project):
    identities = []
    for index in range(2):
        identity = start(project)
        identities.append(identity)
        pilot.stop(identity, now=101)
        pilot.review('keep-full', now=102)
        path = pilot.directory() / 'reviews.json'
        envelope = pilot.read_json(path, max_bytes=pilot.REPORT_LIMIT)
        assert envelope['schema'] == pilot.REVIEWS_SCHEMA
        reports = pilot._load('reviews.json', pilot.REPORT_LIMIT)
        assert envelope['reviews'] == reports
        assert [report['id'] for report in reports] == identities
        assert all(report['status'] == 'REVIEWED' for report in reports)


@pytest.mark.parametrize('envelope', [
    {}, {'schema': 'wrong', 'reviews': []},
    {'schema': pilot.REVIEWS_SCHEMA, 'reviews': {}},
    {'schema': pilot.REVIEWS_SCHEMA, 'reviews': [None]},
    {'schema': pilot.REVIEWS_SCHEMA, 'reviews': [{}] * 9},
    {'schema': pilot.REVIEWS_SCHEMA, 'reviews': [], 'extra': True},
])
def test_malformed_reviews_preserve_baseline_and_hook(project, envelope):
    identity = start(project)
    with pilot.locked_state(identity) as state:
        state['baseline'] = {'bounded': 'baseline'}
    pilot.begin_run(identity, now=101)
    pilot.write_hook(identity, {'collected': 3})
    root = pilot.directory()
    pilot.save_json(root / 'reviews.json', envelope)
    before = {name: (root / name).read_bytes()
              for name in ('pilot.json', 'hook.json', 'reviews.json')}
    with pytest.raises(ValueError, match='Invalid reviews report'):
        pilot._load('reviews.json', pilot.REPORT_LIMIT)
    with pytest.raises(ValueError, match='Invalid reviews report'):
        pilot.review('keep-full', now=102)
    for name, content in before.items():
        assert (root / name).read_bytes() == content
    with pilot.locked_state(identity) as state:
        assert state['baseline'] == {'bounded': 'baseline'}
        assert state['decision'] is None


def test_reviews_budget_includes_envelope_and_atomic_pending(project, monkeypatch):
    root = pilot.directory()
    root.mkdir(parents=True)
    envelope = {'schema': pilot.REVIEWS_SCHEMA, 'reviews': []}
    size = pilot._size(envelope)
    with pytest.raises(ValueError, match='byte budget'):
        pilot._save('reviews.json', [], size - 1)
    assert not (root / 'reviews.json').exists()
    monkeypatch.setattr(pilot, 'TOTAL_LIMIT', 2 * size)
    with pytest.raises(ValueError, match='Total owned storage budget'):
        pilot._save('reviews.json', [], pilot.REPORT_LIMIT)
    assert not (root / 'reviews.json').exists()


def test_investigation_is_only_disposition(project):
    start(project)
    result = pilot.review('investigate-canary', now=101)
    assert result['status'] == 'REVIEWED'
    assert result['authority'] == 'full configured check remains authoritative'


def test_redirected_cleanup_never_deletes_target(project, tmp_path):
    identity = start(project)
    external = tmp_path / 'private.json'
    external.write_text('private transcript', encoding='utf-8')
    hook = pilot.directory() / 'hook.json'
    try:
        hook.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError, match='Redirected'):
        pilot.stop(identity, now=101)
    assert external.read_text(encoding='utf-8') == 'private transcript'
    assert hook.is_symlink()


def test_redirected_storage_ancestor_refused(project, tmp_path, monkeypatch):
    target = tmp_path / 'elsewhere'
    target.mkdir()
    link = tmp_path / 'redirect'
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    monkeypatch.setattr(pilot.native_installation, 'storage_root', lambda: link / 'nested')
    with pytest.raises(ValueError, match='Redirected'):
        start(project)
    assert not (target / 'nested').exists()


def test_cli_start_status_and_errors(project, capsys):
    assert pilot.main(['start', '--root', str(project), '--', sys.executable, '-m', 'pytest']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['destination'] == str(pilot.directory())
    assert pilot.main(['status']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'ACTIVE'
    assert report['destination'] == str(pilot.directory())
    assert report['limits']['owned_bytes'] == 4*1024*1024
    assert 'baseline' not in report and report['reviews'] == []
    assert pilot.main(['run', '--pilot-id', 'stale']) == 2
    assert 'Stale' in capsys.readouterr().err


def test_global_review_is_visible_after_switching_project(project):
    identity=start(project)
    pilot.begin_run(identity,now=101)
    pilot.record_observation(identity,{'complete':True,'omitted':True,
        'omitted_tests':['tests/a.py::test_bad'],'reason_codes':['OMITTED_FAILURE']},now=102)
    row=pilot.usage_rows(now=103)[0]
    assert str(project) in row['body'] and 'REVIEW_REQUIRED' in row['body']
    assert 'Next action: tare test-selection review --decision keep-full' in row['body']
    assert pilot.usage_rows(project.parent,now=103)==[]
    pilot.review('keep-full',now=104)
    saved=pilot._load('reviews.json',pilot.REPORT_LIMIT)[-1]
    assert saved['example_omitted_test']=='tests/a.py::test_bad'
    assert 'Next action: tare test-selection status' in pilot.usage_rows(now=105)[0]['body']


@pytest.mark.parametrize('capture', [{'complete': True},
                                     {'complete': True, 'omitted': None}])
def test_baseline_unknown_stays_unknown_and_active(project, capture):
    identity = start(project)
    pilot.begin_run(identity, now=101)
    report = pilot.record_observation(identity, capture, now=102)
    state = pilot.read(now=103)
    assert state['status'] == 'ACTIVE'
    assert state['observations'] == [capture]
    assert report['confirmed_omissions'] is None
    assert report['seconds'] is None and report['net_savings'] is None


def test_execution_reservation_survives_exception_and_rejects_retry(project):
    identity = start(project)
    with pytest.raises(RuntimeError, match='command failed'):
        with pilot.execution_state(identity, now=101) as state:
            stored = pilot._load('pilot.json', pilot.PILOT_LIMIT)
            assert stored['capture_pending'] and stored['runs'] == 1
            assert state == stored
            raise RuntimeError('command failed')
    stored = pilot._load('pilot.json', pilot.PILOT_LIMIT)
    assert stored['capture_pending'] and stored['runs'] == 1
    with pytest.raises(ValueError, match='review'):
        with pilot.execution_state(identity, now=102):
            pytest.fail('orphan must not execute again')
    assert pilot.read(now=103)['reason'] == 'incomplete capture'
    with pytest.raises(ValueError, match='review'):
        with pilot.execution_state(identity, now=104):
            pytest.fail('retired orphan must still require review')


def test_execution_stale_id_never_enters_body(project):
    start(project)
    with pytest.raises(ValueError, match='Stale'):
        with pilot.execution_state('stale', now=101):
            pytest.fail('stale wrapper executed')
    assert pilot.read(now=102)['runs'] == 0


def test_execution_record_composes_without_nested_lock(project, monkeypatch):
    identity = start(project)
    with pilot.execution_state(identity, now=101) as state:
        def forbidden(*args, **kwargs):
            pytest.fail('record_in_state must not acquire custody')
        with monkeypatch.context() as patch:
            patch.setattr(pilot, 'custody_lock', forbidden)
            result = pilot.record_in_state(state, {'complete': True}, now=102)
        assert result['confirmed_omissions'] is None
        state['baseline'] = {'collection_digest': 'a' * 64}
    state = pilot.read(now=103)
    assert not state['capture_pending'] and state['runs'] == 1
    assert state['baseline'] == {'collection_digest': 'a' * 64}


def test_execution_holds_custody_against_concurrent_reader(project):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    identity = start(project)
    entered, acquired = Event(), Event()

    def reader():
        entered.set()
        try:
            with pilot.locked_state(identity):
                acquired.set()
        except (OSError, ValueError, RuntimeError):
            # A fail-fast native lock is also a valid denial.
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pilot.execution_state(identity, now=101) as state:
            future = pool.submit(reader)
            assert entered.wait(2)
            assert not acquired.wait(0.1)
            pilot.record_in_state(state, observation(), now=102)
        future.result(timeout=5)


@pytest.mark.parametrize('finish', ['deadline', 'cap', 'review'])
def test_execution_finished_pilot_yields_unchanged_full_command(project, finish):
    identity = start(project)
    original = pilot.read(now=101)['argv']
    if finish == 'cap':
        for index in range(5):
            pilot.begin_run(identity, now=101 + index)
            pilot.record_observation(identity, observation(), now=101 + index)
    elif finish == 'review':
        pilot.review('keep-full', now=102)
    now = 100 + 7 * 86400 if finish == 'deadline' else 110
    with pilot.execution_state(identity, now=now) as state:
        assert state['status'] != 'ACTIVE'
        assert state['argv'] == original and not state['capture_pending']


def test_compact_identity_reasons_costs_and_unknowns(project):
    identity = start(project)
    phases = {key: 10 for key in ('capture_seconds', 'recapture_seconds',
              'selection_seconds', 'execution_seconds', 'invalidation_seconds',
              'retry_seconds', 'correction_seconds', 'audit_seconds')}
    capture = dict(observation(), observed_at=101, target_digest='a' * 64,
                   baseline_digest='b' * 64, mode='baseline',
                   reason_codes=['BASELINE_UNKNOWN', 'FULL_CHECK'],
                   omitted_tests=['tests/a.py::test_ok[x-1]'],
                   baseline_comparable=False, net_operational_savings=None, **phases)
    with pilot.execution_state(identity, now=101) as state:
        pilot.record_in_state(state, capture, now=102)
    state = pilot.read(now=103)
    assert state['observations'] == [capture]
    report = pilot.summarize(state)
    assert report['seconds'] == 1.25  # phase breakdowns must not double count
    assert report['reason_codes'] == ['BASELINE_UNKNOWN', 'FULL_CHECK']
    assert report['confirmed_omissions'] == 0 and report['net_savings'] is None
    assert pilot._size(report) <= 8 * 1024
    pilot.begin_run(identity, now=104)
    pilot.record_observation(identity, {'complete': True, 'reason_codes': ['FULL_CHECK']}, now=105)
    report = pilot.summarize(pilot.read(now=106))
    assert report['seconds'] is None and report['confirmed_omissions'] is None
    assert report['reason_codes'] == ['BASELINE_UNKNOWN', 'FULL_CHECK']


@pytest.mark.parametrize('bad', [
    {'observed_at': None}, {'observed_at': -1}, {'observed_at': True},
    {'observed_at': float('inf')}, {'target_digest': 'a' * 63},
    {'baseline_digest': 'G' * 64}, {'target_digest': None},
    {'baseline_digest': None}, {'mode': 'selected'}, {'mode': None},
    {'reason_codes': ['lower']}, {'reason_codes': ['X' * 65]},
    {'reason_codes': ['OK'] * 17}, {'reason_codes': 'OK'},
    {'omitted_tests': ['../test_a.py']}, {'omitted_tests': ['a.py'] * 17},
    {'omitted_tests': ['a.py::bad;command']}, {'omitted_tests': [None]},
    {'baseline_comparable': True}, {'baseline_comparable': None},
    {'net_operational_savings': 0}, {'raw_trace': 'private'},
])
def test_new_compact_fields_reject_invalid_values(bad):
    with pytest.raises(ValueError):
        pilot._observation(bad)


@pytest.mark.parametrize('key', ['capture_seconds', 'recapture_seconds',
    'selection_seconds', 'execution_seconds', 'invalidation_seconds',
    'retry_seconds', 'correction_seconds', 'audit_seconds'])
def test_phase_costs_are_finite_nonnegative_or_unknown(key):
    for value in (None, 0, 1.5):
        assert pilot._observation({key: value}) == {key: value}
    for value in (-1, True, '1', float('nan'), float('inf')):
        with pytest.raises(ValueError):
            pilot._observation({key: value})


def test_confirmed_omission_is_actionable_but_not_savings(project):
    identity = start(project)
    with pilot.execution_state(identity, now=101) as state:
        report = pilot.record_in_state(state, {'complete': True, 'omitted': True,
            'reason_codes': ['OMITTED_FAILURE'], 'omitted_tests': ['tests/a.py::test_bad']},
            now=102)
    assert report['confirmed_omissions'] == 1
    assert report['reason_codes'] == ['OMITTED_FAILURE']
    assert report['net_savings'] is None
    with pilot.execution_state(identity, now=103) as inactive:
        assert inactive['status']=='REVIEW_REQUIRED' and not inactive['capture_pending']
        assert inactive['runs']==1  # only the original full check is permitted; no new collection


@pytest.mark.parametrize('reason', [
    'TEXT_LIMIT', 'BYTE_LIMIT', 'NODEID_LIMIT', 'UNKNOWN_NODEID',
    'INVALID_DURATION', 'PAIR_LIMIT', None,
])
@pytest.mark.parametrize('later', [False, True])
def test_incomplete_codes_retained_in_compact_summary_and_keep_full_review(project, reason, later):
    identity = start(project)
    if later:
        pilot.begin_run(identity, now=101)
        pilot.record_observation(identity, observation(reason_codes=['BASELINE_CAPTURED']), now=102)
    codes = ['HOOK_INCOMPLETE'] + ([reason] if reason else [])
    pilot.begin_run(identity, now=103)
    result = pilot.record_observation(identity, {
        'complete': False, 'omitted': None, 'mode': 'fallback',
        'reason_codes': codes, 'exit_code': 0,
        'baseline_comparable': False, 'net_operational_savings': None,
    }, now=104)
    expected = sorted(codes + (['BASELINE_CAPTURED'] if later else []))
    saved = pilot.read(now=105)
    assert saved['observations'][-1]['reason_codes'] == codes
    assert saved['status'] == 'REVIEW_REQUIRED'
    assert result['reason_codes'] == pilot.summarize(saved)['reason_codes'] == expected
    assert pilot._load('reviews.json', pilot.REPORT_LIMIT)[-1]['reason_codes'] == expected
    reviewed = pilot.review('keep-full', now=106)
    assert reviewed['decision'] == 'keep-full' and reviewed['status'] == 'REVIEWED'
    assert reviewed['reason_codes'] == expected and reviewed['net_savings'] is None
    assert pilot._load('reviews.json', pilot.REPORT_LIMIT)[-1]['reason_codes'] == expected


@pytest.mark.parametrize('code', ['', 'A1', 'A-B', 'A B', 'A\n', 'Á', 'a', 'A' * 65, None, 1, True])
def test_reason_code_grammar_rejects_non_codes(code):
    with pytest.raises(ValueError, match='Invalid observation aggregate'):
        pilot._observation({'reason_codes': [code]})


def test_reason_code_exact_length_and_count_boundaries():
    codes = ['A', '_' * 64] + ['REASON_' + chr(65 + i) for i in range(14)]
    assert len(codes) == 16
    assert pilot._observation({'reason_codes': codes}) == {'reason_codes': codes}
    with pytest.raises(ValueError, match='Invalid observation aggregate'):
        pilot._observation({'reason_codes': codes + ['EXTRA']})


def test_large_reason_unions_rotate_within_report_envelope(project):
    for cycle in range(8):
        identity = start(project)
        codes = []
        for index in range(5):
            batch = ['REASON_' + chr(65 + index) + chr(65 + n) + '_' * 55
                     for n in range(16)]
            codes.extend(batch)
            with pilot.execution_state(identity, now=101 + index) as state:
                pilot.record_in_state(state, dict(observation(), reason_codes=batch),
                                      now=101 + index)
        pilot.review('keep-full', now=110)
        reports = pilot._load('reviews.json', pilot.REPORT_LIMIT)
        assert 1 <= len(reports) <= 8
        assert reports[-1]['id'] == identity
        assert reports[-1]['reason_codes'] == sorted(codes)
        assert (pilot.directory() / 'reviews.json').stat().st_size <= pilot.REPORT_LIMIT
        assert pilot._bytes() < pilot.TOTAL_LIMIT

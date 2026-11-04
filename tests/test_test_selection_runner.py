"""Offline custody tests and a real Kernel/pytest baseline capture."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from relay import test_selection_pilot as pilot
from relay import test_selection_runner as runner


@pytest.fixture
def project(tmp_path, monkeypatch):
    # The integration cases rewrite equal-size modules between subprocesses.
    # Timestamp-based pyc caches can otherwise hide the edit within one second.
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    storage = tmp_path / 'storage'
    monkeypatch.setattr(pilot.native_installation, 'storage_root', lambda: storage)
    root = tmp_path / 'project'
    root.mkdir()
    return root


def start(root, **options):
    argv = [sys.executable, '-m', 'pytest', '-q', '-x', 'tests']
    result = pilot.start(root, argv, **options)
    return result['wrapper_argv'][-1], argv


def metadata(**changes):
    return dict(started=True, cleanup_verified=True, timeout=False,
                cancelled=False, output_limit=False, exit_code=0,
                wall_ms=1250, **changes)


def port(monkeypatch, result=None, failure=None):
    calls = []
    result = metadata() if result is None else result

    def execute(argv, **options):
        calls.append((argv, options))
        if failure is not None:
            raise failure
        return result, b'original stdout\n', b'original stderr\n'

    def unsupported(state):
        raise runner._CaptureError('PROJECT_UNSUPPORTED')

    # These unit tests exercise the uninstrumented fallback/custody boundary.
    # The integration test below uses the actual SDK, probe, plugin and Kernel.
    monkeypatch.setattr(runner, '_prepare_baseline', unsupported)
    monkeypatch.setattr(runner, '_process', execute)
    return calls


def state(identity):
    # Do not use read(): its finalization behavior is intentionally independent.
    with pilot.locked_state(identity) as value:
        import copy
        return copy.deepcopy(value)


def test_original_once_with_cwd_flags_streams_and_bounds(project, monkeypatch, capsys):
    identity, argv = start(project)
    calls = port(monkeypatch)
    assert runner.run_pilot(identity) == 0
    assert len(calls) == 1
    command, options = calls[0]
    assert command == argv
    assert options['cwd'] == project
    assert options['timeout'] == 300
    assert options['max_output'] == 1048576
    assert isinstance(options['env'], dict)
    assert 'on_stdout' not in options
    output = capsys.readouterr()
    assert output.out.count('original stdout') == 1
    assert output.err.count('original stderr') == 1
    saved = state(identity)
    assert saved['status'] == 'REVIEW_REQUIRED'
    observation = saved['observations'][0]
    assert observation['omitted'] is None
    assert observation['complete'] is False
    assert observation['baseline_comparable'] is False
    assert observation['net_operational_savings'] is None
    assert observation['full_seconds'] == observation['execution_seconds'] == 1.25
    assert saved['baseline'] is None
    assert 'target_digest' not in observation
    assert 'collection_digest' not in observation
    assert observation['capture_seconds'] is None
    assert observation['correction_seconds'] is None
    assert observation['invalidation_seconds'] is None
    for phase in ('recapture_seconds', 'retry_seconds', 'audit_seconds'):
        assert observation[phase] == 0


def test_failed_full_status_preserved(project, monkeypatch):
    identity, _ = start(project)
    receipt = metadata()
    receipt['exit_code'] = 1
    calls = port(monkeypatch, receipt)
    assert runner.run_pilot(identity) == 1
    assert len(calls) == 1
    report = pilot._load('reviews.json', pilot.REPORT_LIMIT)[-1]
    assert report['full_seconds'] == 1.25
    assert report['net_savings'] is None
    assert report['confirmed_omissions'] is None


def test_stale_wrapper_cannot_execute_replacement(project, monkeypatch):
    old, _ = start(project)
    pilot.review('keep-full')
    current, _ = start(project)
    calls = port(monkeypatch)
    assert current != old
    assert runner.run_pilot(old) == 2
    assert not calls


def test_reviewed_pilot_runs_uninstrumented_original(project, monkeypatch):
    identity, argv = start(project)
    pilot.review('keep-full')
    calls = port(monkeypatch)
    assert runner.run_pilot(identity) == 0
    assert len(calls) == 1 and calls[0][0] == argv
    assert state(identity)['observations'] == []


def test_expired_runs_original_without_observation(project, monkeypatch):
    identity, argv = start(project)
    saved = state(identity)
    monkeypatch.setattr(pilot.time, 'time', lambda: saved['deadline'] + 1)
    calls = port(monkeypatch)
    assert runner.run_pilot(identity) == 0
    assert calls[0][0] == argv
    assert state(identity)['observations'] == []


def test_orphan_never_reexecutes(project, monkeypatch):
    identity, _ = start(project)
    calls = port(monkeypatch, failure=OSError('port failed'))
    assert runner.run_pilot(identity) == 2
    assert state(identity)['capture_pending'] is True
    assert runner.run_pilot(identity) == 2
    assert len(calls) == 1


@pytest.mark.parametrize('field,value', [
    ('cleanup_verified', False), ('started', False),
    ('exit_code', None), ('timeout', True), ('output_limit', True),
    ('cancelled', True),
])
def test_uncontained_or_incomplete_process_fails_closed(project, monkeypatch, field, value):
    identity, _ = start(project)
    receipt = metadata()
    receipt[field] = value
    calls = port(monkeypatch, receipt)
    assert runner.run_pilot(identity) == 2
    assert len(calls) == 1
    assert state(identity)['capture_pending'] is True


@pytest.mark.parametrize('code', [0, 1])
def test_metric_failure_preserves_completed_full_result(project, monkeypatch, capsys, code):
    identity, _ = start(project)
    receipt = metadata()
    receipt['exit_code'] = code
    calls = port(monkeypatch, receipt)

    def fail(*args, **kwargs):
        raise OSError('storage failed\n\x1b[31m' + 'x' * 500)

    monkeypatch.setattr(pilot, 'record_in_state', fail)
    assert runner.run_pilot(identity) == code
    assert len(calls) == 1
    assert state(identity)['capture_pending'] is True
    warning = capsys.readouterr().err
    assert 'OSError: storage failed' in warning
    assert 'tare test-selection status' in warning
    assert 'review' in warning and 'no retry performed' in warning
    assert '\x1b' not in warning
    assert 'x' * 241 not in warning


def test_canary_unsupported_project_keeps_full_check(project, monkeypatch):
    identity, argv = start(project, mode='canary')
    calls = port(monkeypatch)
    assert runner.run_pilot(identity) == 0
    assert [call[0] for call in calls] == [argv]
    observation = state(identity)['observations'][0]
    assert observation['mode'] == 'fallback'
    assert observation['selected_seconds'] is None
    assert observation['reason_codes'] == ['PROJECT_UNSUPPORTED', 'FULL_CHECK_ONLY']


def test_output_emission_is_bounded(capsys):
    runner._emit(b'x' * (runner.OUTPUT_LIMIT + 10), sys.stdout)
    assert len(capsys.readouterr().out) == runner.OUTPUT_LIMIT


def test_optional_collection_failure_leaves_plain_full_checks_available(project, monkeypatch):
    identity, _ = start(project)
    calls = port(monkeypatch)
    assert runner.run_pilot(identity) == 0
    assert runner.run_pilot(identity) == 0
    assert len(calls) == 2
    assert state(identity)['runs'] == 1


@pytest.mark.parametrize('code', [0, 1])
def test_broken_output_sink_preserves_contained_full_result(project, monkeypatch, code):
    identity, _ = start(project)
    receipt = metadata()
    receipt['exit_code'] = code
    calls = port(monkeypatch, receipt)

    def broken_sink(*args):
        raise OSError('output sink closed')

    monkeypatch.setattr(runner, '_emit', broken_sink)
    assert runner.run_pilot(identity) == code
    assert len(calls) == 1
    assert state(identity)['capture_pending'] is True
    assert runner.run_pilot(identity) == 2
    assert len(calls) == 1


def test_real_initial_baseline_uses_original_python_and_one_pytest(project, monkeypatch):
    from specgraph import test_selection as selection

    (project / 'src').mkdir()
    (project / 'tests').mkdir()
    (project / 'src' / 'calc.py').write_text(
        'def add(a, b):\n    return a + b\n', encoding='utf-8')
    (project / 'tests' / 'test_calc.py').write_text(
        'from calc import add\n'
        'def test_positive():\n    assert add(1, 2) == 3\n'
        'def test_zero():\n    assert add(0, 0) == 0\n', encoding='utf-8')
    (project / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding='utf-8')
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    identity, original = start(project)
    real_process = runner._process
    calls = []
    environments = []

    def execute(argv, **options):
        calls.append((list(argv), options))
        result = real_process(argv, **options)
        if argv[1:2] == ['-c']:
            assert runner._outcome(result[0]) == 0
            environments.append(json.loads(result[1]))
        return result

    monkeypatch.setattr(runner, '_process', execute)
    assert runner.run_pilot(identity) == 0
    assert len(calls) == 2
    probe, full = calls
    assert probe[0][:3] == [original[0], '-c', runner.ENVIRONMENT_SCRIPT]
    assert full[0] == original + ['-p',pilot.OBSERVER]
    assert original[0] == sys.executable
    assert probe[1]['cwd'] == full[1]['cwd'] == project
    assert probe[1]['env'] == full[1]['env']
    assert probe[1]['timeout'] == 30
    saved = state(identity)
    assert saved['status'] == 'ACTIVE'
    assert saved['capture_pending'] is False
    receipt = saved['baseline']
    assert receipt['schema'] == selection.SCHEMA
    assert receipt['environment'] == environments[0]
    assert receipt['environment_sha256'] == runner._digest(environments[0])
    assert receipt['pytest_args'] == original[3:]
    assert receipt['suite_returncode'] == 0
    assert receipt['complete'] is True and receipt['truncated'] is False
    assert receipt['failed'] == []
    assert receipt['fingerprints'] == selection._files(project)
    import hashlib
    config_digest = hashlib.sha256((project / 'pyproject.toml').read_bytes()).hexdigest()
    assert receipt['fingerprints']['pyproject.toml'] == config_digest
    assert receipt['config_sha256'] == runner._digest({'pyproject.toml': config_digest})
    assert receipt['config_sha256'] != runner._digest({})
    assert len(receipt['tests']) == 2
    assert set(receipt['executed_blocks']) == set(receipt['tests'])
    assert any(receipt['executed_blocks'].values())
    assert receipt['dependency_closure'] == 'UNKNOWN'
    assert receipt['attestation'] == 'NONE'
    assert len(json.dumps(receipt, sort_keys=True).encode()) <= runner.OUTPUT_LIMIT
    observation, = saved['observations']
    assert observation['complete'] is True
    assert observation['omitted'] is None
    assert observation['mode'] == 'baseline'
    assert observation['reason_codes'] == ['BASELINE_CAPTURED']
    assert observation['collected'] == 2 and observation['failed'] == 0
    assert observation['baseline_comparable'] is False
    assert observation['net_operational_savings'] is None
    assert observation['selected'] is observation['selected_seconds'] is None
    assert observation['seconds'] >= observation['full_seconds']
    assert observation['full_seconds'] == observation['execution_seconds']
    assert observation['command_digest'] == saved['command_digest']
    assert observation['target_digest'] == runner._digest(selection._files(project))
    assert observation['target_digest'] != runner._digest(original[3:])
    assert observation['selection_seconds'] == 0
    assert observation['capture_seconds'] is None
    assert observation['correction_seconds'] is None
    assert observation['invalidation_seconds'] is None
    for phase in ('recapture_seconds', 'retry_seconds', 'audit_seconds'):
        assert observation[phase] == 0
    assert observation['collection_digest'] == runner._digest(receipt['tests'])
    assert not (pilot.directory() / 'hook.json').exists()


def test_invalid_baseline_keeps_full_check(project, monkeypatch):
    identity, original = start(project)
    with pilot.locked_state(identity) as saved:
        saved['baseline'] = {'schema': 'local-observation/2'}
    calls = port(monkeypatch)
    assert runner.run_pilot(identity) == 0
    assert [call[0] for call in calls] == [original]
    observation = state(identity)['observations'][0]
    assert observation['mode'] == 'fallback'
    assert observation['reason_codes'] == ['BASELINE_INVALID', 'FULL_CHECK_ONLY']


@pytest.mark.parametrize('problem,reason', [
    ('missing', 'HOOK_MISSING'),
    ('truncated', 'HOOK_INCOMPLETE'),
    ('oversized', 'HOOK_OUTPUT_LIMIT'),
    ('changed', 'SOURCE_CHANGED'),
])
def test_invalid_initial_capture_keeps_full_result_and_requires_review(
        project, monkeypatch, problem, reason):
    from types import SimpleNamespace

    identity, original = start(project)
    hook = pilot.directory() / 'hook.json'
    before = {'tests/test_example.py': 'a' * 64}
    selection = SimpleNamespace(SCHEMA='local-observation/2', CONFIG=())
    capture = dict(selection=selection, root=project, before=before, hook=hook,
                   childenv={}, environment={'python': original[0]})
    result = dict(tests=['tests/test_example.py::test_one'],
                  executed_blocks={'tests/test_example.py::test_one': []},
                  failed=[], exitstatus=0, complete=True, truncated=False)
    calls = []

    def prepare(saved):
        assert not hook.exists()
        return capture

    def execute(argv, **options):
        calls.append(list(argv))
        if problem == 'truncated':
            result['truncated'] = True
        if problem == 'oversized':
            hook.write_bytes(b'x' * (runner.OUTPUT_LIMIT + 1))
        elif problem != 'missing':
            hook.write_text(json.dumps(result), encoding='utf-8')
        return metadata(), b'full stdout', b''

    monkeypatch.setattr(runner, '_prepare_baseline', prepare)
    monkeypatch.setattr(runner, '_process', execute)
    monkeypatch.setattr(runner, '_snapshot', lambda *args: {})
    assert runner.run_pilot(identity) == 0
    assert calls == [original + ['-p',pilot.OBSERVER]]
    saved = state(identity)
    assert saved['status'] == 'REVIEW_REQUIRED'
    assert saved['baseline'] is None
    observation, = saved['observations']
    assert observation['complete'] is False
    assert observation['omitted'] is None
    assert observation['reason_codes'] == [reason]
    assert 'collection_digest' not in observation
    assert observation['target_digest'] == runner._digest(before)
    assert observation['exit_code'] == 0
    assert not hook.exists()


# Literal producer contract, deliberately independent of the runner allowlist.
INCOMPLETE_REASONS = (
    'TEXT_LIMIT', 'BYTE_LIMIT', 'NODEID_LIMIT', 'UNKNOWN_NODEID',
    'INVALID_DURATION', 'PAIR_LIMIT',
)


def test_imported_observer_contract_and_identity(project, tmp_path):
    import ast
    import hashlib
    import importlib.metadata
    import site
    import specgraph
    from specgraph import test_selection as selection

    producer = Path(specgraph.__file__).with_name('_pytest_x5.py')
    raw = producer.read_bytes()
    tree = ast.parse(raw)
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and type(node.value) is str}
    assert set(INCOMPLETE_REASONS) <= literals
    assert 'reason' in literals
    start(project)
    _, sdk, observer, identity = runner._observer_environment(specgraph, selection)
    assert observer.read_bytes() == raw
    assert identity == {
        'observer_sha256': hashlib.sha256(raw).hexdigest(),
        'analyzer_sha256': hashlib.sha256(Path(selection.__file__).read_bytes()).hexdigest(),
        'sdk_origin': str(producer.resolve().parent.parent),
    }
    assert sdk == identity['sdk_origin']
    # Temporary audit evidence follows the actual import, not a candidate HEAD.
    (tmp_path / 'producer.py').write_bytes(raw)
    (tmp_path / 'import-identity.json').write_text(json.dumps({
        'python': sys.executable,
        'version': importlib.metadata.version('tare-tools-specgraph'),
        'producer': str(producer.resolve()), 'analyzer': str(Path(selection.__file__).resolve()),
        'observer_identity': identity,
        'pth': {str(path): path.read_text(encoding='utf-8')
                for directory in site.getsitepackages()
                for path in Path(directory).glob('*.pth')
                if 'specgraph' in path.read_text(encoding='utf-8').lower()},
    }, sort_keys=True), encoding='utf-8')
    pilot.clean_scratch()


@pytest.fixture
def incomplete_capture(project, monkeypatch):
    """Real JSON ingestion and custody; only the process/analysis ports are fake."""
    from types import SimpleNamespace

    def run(payload, phase):
        identity, original = start(project)
        hook = pilot.directory() / 'hook.json'
        node = 'tests/test_example.py::test_one'
        before = {'tests/test_example.py': 'a' * 64}
        capture = dict(selection=SimpleNamespace(SCHEMA='local-observation/2', CONFIG=()),
                       root=project, before=before, hook=hook, childenv={},
                       environment={}, observer_identity={}, selected=[],
                       applicable=False, reason_codes=['NO_CHANGED_TARGET'])
        complete = dict(tests=[node], executed_blocks={node: []}, failed=[],
                        exitstatus=0, complete=True, truncated=False)
        calls = []

        def execute(argv, **options):
            calls.append(list(argv))
            result = complete if phase == 'later' and len(calls) == 1 else payload
            hook.write_text(json.dumps(result), encoding='utf-8')
            return metadata(), b'', b''

        monkeypatch.setattr(runner, '_prepare_baseline', lambda saved: capture)
        monkeypatch.setattr(runner, '_prepare_shadow', lambda saved: capture)
        monkeypatch.setattr(runner, '_snapshot', lambda *args: before)
        monkeypatch.setattr(runner, '_process', execute)
        if phase == 'later':
            assert runner.run_pilot(identity) == 0
            assert state(identity)['status'] == 'ACTIVE'
        assert runner.run_pilot(identity) == 0
        assert calls == [original + ['-p', pilot.OBSERVER]] * (2 if phase == 'later' else 1)
        assert not hook.exists()
        return identity

    return run


def assert_incomplete_diagnostics(identity, phase, extra, capsys):
    codes = ['HOOK_INCOMPLETE', *extra]
    assert capsys.readouterr().err == (
        'test-selection: full check only; ' + ', '.join(codes) +
        '; no adequacy or savings claim.\n')
    saved = state(identity)
    assert saved['status'] == 'REVIEW_REQUIRED'
    assert saved['decision'] is None and not saved['capture_pending']
    assert saved['baseline'] is None  # retirement discards even an earlier valid capture
    observation = saved['observations'][-1]
    assert observation['reason_codes'] == codes
    assert observation['complete'] is False and observation['omitted'] is None
    assert observation['mode'] == 'fallback' and observation['exit_code'] == 0
    assert observation['selected'] is observation['selected_seconds'] is None
    assert observation['baseline_comparable'] is False
    assert observation['net_operational_savings'] is None
    assert 'collection_digest' not in observation
    expected = sorted(codes + (['BASELINE_CAPTURED'] if phase == 'later' else []))
    assert pilot.summarize(saved)['reason_codes'] == expected
    assert pilot._load('reviews.json', pilot.REPORT_LIMIT)[-1]['reason_codes'] == expected
    reviewed = pilot.review('keep-full')
    assert reviewed['reason_codes'] == expected
    assert reviewed['decision'] == 'keep-full' and reviewed['status'] == 'REVIEWED'
    assert reviewed['net_savings'] is None
    assert pilot._load('reviews.json', pilot.REPORT_LIMIT)[-1]['reason_codes'] == expected


@pytest.mark.parametrize('phase', ['initial', 'later'])
@pytest.mark.parametrize('flags', [
    {'complete': False, 'truncated': False},
    {'complete': True, 'truncated': True},
])
@pytest.mark.parametrize('reason', INCOMPLETE_REASONS)
def test_known_incomplete_reason_survives_to_review(incomplete_capture, capsys, phase, flags, reason):
    identity = incomplete_capture(dict(flags, reason=reason), phase)
    assert_incomplete_diagnostics(identity, phase, [reason], capsys)


@pytest.mark.parametrize('phase', ['initial', 'later'])
@pytest.mark.parametrize('fields', [
    {}, {'reason': None}, {'reason': 'UNKNOWN_REASON'},
    {'reason': 'private /home/operator/token\nsecret=123\x1b[31m'},
    {'reason': ''}, {'reason': 'text_limit'}, {'reason': 'TEXT_LIMIT\n'},
    {'reason': ' TEXT_LIMIT'}, {'reason': 'TEXT_LIMIT,BYTE_LIMIT'},
    {'reason': 'X' * 65}, {'reason': 1}, {'reason': True},
    {'reason': 1.5}, {'reason': []}, {'reason': ['TEXT_LIMIT']},
    {'reason': {'reason': 'TEXT_LIMIT'}}, {'reasons': ['TEXT_LIMIT']},
    {'reason_codes': ['TEXT_LIMIT']}, {'detail': {'reason': 'TEXT_LIMIT'}},
])
def test_untrusted_incomplete_reason_never_reaches_diagnostics_or_review(
        incomplete_capture, capsys, phase, fields):
    identity = incomplete_capture(dict(complete=False, truncated=True, **fields), phase)
    assert_incomplete_diagnostics(identity, phase, [], capsys)
    serialized = json.dumps(state(identity)) + (pilot.directory() / 'reviews.json').read_text()
    for marker in ('UNKNOWN_REASON', 'private /home', 'secret=123', 'text_limit', 'X' * 65):
        assert marker not in serialized


@pytest.mark.parametrize('phase', ['initial', 'later'])
@pytest.mark.parametrize('payload', [None, [], ['TEXT_LIMIT'], 'TEXT_LIMIT', 1, True])
def test_non_object_capture_has_no_reason(incomplete_capture, capsys, phase, payload):
    identity = incomplete_capture(payload, phase)
    assert_incomplete_diagnostics(identity, phase, [], capsys)


def live_pilot(project, monkeypatch, mode='shadow'):
    files = {
        'src/calc.py':'def value():\n    return 1\n',
        'tests/test_calc.py':'from calc import value\ndef test_calc():\n    assert value() == 1\n',
        'tests/test_other.py':'def test_other():\n    assert True\n',
        'tests/test_required.py':'def test_required():\n    assert 2 + 2 == 4\n',
        'conftest.py':('from pathlib import Path\ndef pytest_sessionstart(session):\n'
            '    p=Path(__file__).with_name("invocations.txt")\n'
            '    p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'),
        'pyproject.toml':'[tool.pytest.ini_options]\npythonpath=["src"]\n',
    }
    for name, content in files.items():
        path=project/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content,encoding='utf-8')
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD','1')
    required='tests/test_required.py::test_required'
    result=pilot.start(str(project),[sys.executable,'-m','pytest','tests','-q'],
                       mandatory=[required],mode=mode)
    identity=result['wrapper_argv'][-1]
    assert runner.run_pilot(identity)==0
    assert state(identity)['status']=='ACTIVE'
    return identity, required


@pytest.mark.parametrize('mode', ['shadow','canary'])
def test_real_changed_target_advice_precedes_full_check(project,monkeypatch,mode):
    identity, required=live_pilot(project,monkeypatch,mode)
    baseline=state(identity)['baseline']
    (project/'src/calc.py').write_text('def value():\n    return 1 + 0\n',encoding='utf-8')
    real=runner._process; calls=[]
    def execute(argv,**options):
        calls.append(list(argv))
        return real(argv,**options)
    monkeypatch.setattr(runner,'_process',execute)
    assert runner.run_pilot(identity)==0
    saved=state(identity); observation=saved['observations'][-1]
    assert saved['status']=='ACTIVE' and not saved['capture_pending']
    assert saved['baseline']==baseline
    assert calls[0][1:3]==['-c',runner.ADVICE_SCRIPT]
    checks=[argv for argv in calls if argv[1:3]==['-m','pytest']]
    assert len(checks)==(2 if mode=='canary' else 1)
    assert checks[-1]==saved['argv']+['-p',pilot.OBSERVER]
    if mode=='canary':
        assert required in checks[0]
        assert observation['selected_seconds']>0
        assert observation['audit_seconds']==observation['full_seconds']
        assert observation['execution_seconds']==observation['selected_seconds']
    else:
        assert observation['selected_seconds'] is None and observation['audit_seconds']==0
    assert (project/'invocations.txt').read_text()==('3' if mode=='canary' else '2')
    assert observation['mode']==mode and observation['complete'] is True
    assert observation['omitted'] is False
    assert observation['selected']==2 and observation['collected']==3
    assert observation['selection_seconds']>0
    assert observation['net_operational_savings'] is None and not observation['baseline_comparable']
    assert not (pilot.directory()/'hook.json').exists()


def test_real_omitted_failure_pauses_and_preserves_failure(project,monkeypatch):
    identity, required=live_pilot(project,monkeypatch)
    (project/'src/calc.py').write_text('def value():\n    return 1 + 0\n',encoding='utf-8')
    (project/'tests/test_other.py').write_text('def test_other():\n    assert False\n',encoding='utf-8')
    real=runner._process
    def faulty_advice(argv,**options):
        meta,out,err=real(argv,**options)
        if argv[1:3]==['-c',runner.ADVICE_SCRIPT]:
            data=json.loads(out)
            data['policy_tests']=['tests/test_calc.py::test_calc',required]
            out=json.dumps(data).encode()
        return meta,out,err
    monkeypatch.setattr(runner,'_process',faulty_advice)
    assert runner.run_pilot(identity)==1
    saved=state(identity)
    assert saved['status']=='REVIEW_REQUIRED' and saved['baseline'] is None
    observation=saved['observations'][-1]
    assert observation['omitted'] is True and observation['exit_code']==1
    assert observation['omitted_tests']==['tests/test_other.py::test_other']
    assert 'OMITTED_FAILURE' in observation['reason_codes']
    assert pilot.summarize(saved)['confirmed_omissions']==1
    assert (project/'invocations.txt').read_text()=='2'


def test_real_no_change_remains_unassessed(project,monkeypatch):
    identity,_=live_pilot(project,monkeypatch)
    assert runner.run_pilot(identity)==0
    saved=state(identity); observation=saved['observations'][-1]
    assert saved['status']=='ACTIVE'
    assert observation['omitted'] is None and observation['selected'] is None
    assert observation['reason_codes']==['NO_CHANGED_TARGET']
    assert observation['net_operational_savings'] is None


def test_real_canary_fails_fast_before_audit(project,monkeypatch):
    identity,_=live_pilot(project,monkeypatch,'canary')
    (project/'src/calc.py').write_text('def value():\n    return 2\n',encoding='utf-8')
    assert runner.run_pilot(identity)==2
    saved=state(identity); observation=saved['observations'][-1]
    assert saved['status']=='REVIEW_REQUIRED'
    assert observation['reason_codes']==['CANDIDATE_FAILED']
    assert observation['full_seconds'] is None and observation['exit_code'] is None
    assert observation['selected_seconds']>0
    assert (project/'invocations.txt').read_text()=='2'


def test_ambiguous_canary_arguments_never_reconstruct_a_smaller_command():
    with pytest.raises(runner._CaptureError,match='CANARY_ARGV_UNSUPPORTED'):
        runner._candidate_argv({'argv':[sys.executable,'-m','pytest','tests','-k','smoke']},
                               ['tests/a.py::test_one'])


def test_public_wrapper_survives_kernel_environment_and_rejects_other_project(project,monkeypatch,tmp_path):
    import compute_plane, specgraph, tare_tools_agent_runtime
    from compute_plane.workspace import isolated_child_environment
    # Pin pytest's root to the fixture, even when basetemp is in this checkout.
    # Otherwise parent configuration prefixes nodeids with the temporary path.
    (project/'pyproject.toml').write_text('[tool.pytest.ini_options]\n', encoding='utf-8')
    (project/'tests').mkdir()
    (project/'tests/test_one.py').write_text('def test_one():\n    assert True\n',encoding='utf-8')
    original=[sys.executable,'-m','pytest','tests','-q']
    prepared=pilot.start(str(project),original)
    wrapper=prepared['wrapper_argv']
    assert wrapper[4:6]==['--store',str(pilot.directory())]
    env=isolated_child_environment()
    assert 'TARE_STORAGE_ROOT' not in env
    # Source checkouts use their explicitly imported owners; managed releases
    # already have these packages installed. No operator storage is consulted.
    env['PYTHONPATH']=os.pathsep.join(str(p) for p in (
        Path(pilot.__file__).resolve().parents[1],
        Path(specgraph.__file__).resolve().parents[1],
        Path(compute_plane.__file__).resolve().parents[1],
        Path(tare_tools_agent_runtime.__file__).resolve().parents[1]))
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    other=tmp_path/'different';other.mkdir()
    denied=subprocess.run(wrapper,cwd=other,env=env,capture_output=True,timeout=30)
    assert denied.returncode==2 and b'another project' in denied.stderr
    assert state(wrapper[-1])['runs']==0
    actual=subprocess.run(wrapper,cwd=project,env=env,capture_output=True,timeout=30)
    assert actual.returncode==0,(actual.stdout,actual.stderr)
    saved=state(wrapper[-1])
    assert saved['runs']==1 and saved['status']=='ACTIVE'
    assert saved['observations'][0]['complete'] is True
    assert not (pilot.directory()/'hook.json').exists()


def test_observer_never_imports_installed_specgraph_over_the_project(project,monkeypatch):
    (project/'src/specgraph').mkdir(parents=True)
    (project/'tests').mkdir()
    (project/'src/specgraph/__init__.py').write_text('LOCAL="project"\n',encoding='utf-8')
    target=project/'src/specgraph/target.py'
    target.write_text('def value():\n    return 1\n',encoding='utf-8')
    (project/'tests/test_target.py').write_text(
        'import specgraph\nfrom specgraph.target import value\n'
        'def test_value():\n    assert specgraph.LOCAL=="project"\n    assert value()==1\n',encoding='utf-8')
    (project/'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\npythonpath=["src"]\n',encoding='utf-8')
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD','1')
    prepared=pilot.start(str(project),[sys.executable,'-m','pytest','tests','-q'])
    identity=prepared['wrapper_argv'][-1]
    assert runner.run_pilot(identity)==0
    assert state(identity)['status']=='ACTIVE'
    target.write_text('def value():\n    return 2\n',encoding='utf-8')
    assert runner.run_pilot(identity)==1
    saved=state(identity)
    assert saved['observations'][-1]['failed']==1
    assert saved['observations'][-1]['omitted'] is False
    assert not (pilot.directory()/(pilot.OBSERVER+'.py')).exists()
    assert pilot._observer_cache()==[]

"""Conservative execution port for the explicit test-selection wrapper.

The configured full check remains authoritative. Shadow observes it once;
an explicit canary also measures candidates before the full audit. No C5
authority is conferred and no locking store helper is called in custody.
"""
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

from . import test_selection_pilot as pilot

OUTPUT_LIMIT = 1024 * 1024
TIMEOUT = 300


def _process(argv, **options):
    from compute_plane.managed_process import run_pipe_process
    return run_pipe_process(argv, **options)


def _warn(message):
    print('test-selection: ' + message, file=sys.stderr)


def _emit(data, stream):
    if not isinstance(data, bytes):
        raise ValueError('Kernel output must be bytes')
    # The Kernel enforces its bound; also bound a malformed port response.
    data = data[:OUTPUT_LIMIT]
    binary = getattr(stream, 'buffer', None)
    if binary is not None:
        binary.write(data)
        binary.flush()
    else:
        stream.write(data.decode('utf-8', errors='replace'))
        stream.flush()


def _seconds(metadata):
    value = metadata.get('wall_ms')
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value / 1000
    return None


def _outcome(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('Invalid Kernel metadata')
    if metadata.get('started') is not True:
        raise ValueError('Original check did not start')
    if metadata.get('cleanup_verified') is not True:
        raise ValueError('Process cleanup is unconfirmed')
    code = metadata.get('exit_code')
    if type(code) is not int:
        raise ValueError('Original check has no known exit status')
    # Never turn a failed full check into success. An interrupted apparent zero
    # is not an authoritative completed check either.
    if code == 0 and any(metadata.get(key) is not False for key in
                         ('timeout', 'cancelled', 'output_limit')):
        raise ValueError('Original check was interrupted or output-limited')
    return code


_ANALYZER = """
import sys, importlib.util
from pathlib import Path
sys.path.insert(0,sys.argv[1])
found=importlib.util.find_spec('_tare_x5_observer')
if found is None or Path(found.origin).resolve()!=Path(sys.argv[2]).resolve():
    raise SystemExit('Observer module name conflicts with the project environment')
"""
ENVIRONMENT_SCRIPT = _ANALYZER + ('\nimport json;from specgraph.test_selection import '
                                 '_environment;print(json.dumps(_environment()))')
_SKIP = {'.git', '.hg', '.svn', '.venv', 'venv', 'env', '__pycache__',
         '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache', '.tox', '.nox'}


class _CaptureError(ValueError):
    def __init__(self, code, *, reason_codes=()):
        super().__init__(code)
        self.reason_codes = reason_codes


def _incomplete_reasons(result):
    # Only the producer's singular, literal reason may leave the raw capture.
    # Keep it separate from HOOK_INCOMPLETE; never stringify arbitrary payloads.
    reason = result.get('reason') if isinstance(result, dict) else None
    if type(reason) is str and reason in {
            'TEXT_LIMIT', 'BYTE_LIMIT', 'NODEID_LIMIT', 'UNKNOWN_NODEID',
            'INVALID_DURATION', 'PAIR_LIMIT'}:
        return (reason,)
    return ()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _safe(path):
    # Only this fixed, non-symlink artifact belongs to the runner.
    directory = Path(pilot.directory())
    path = Path(path)
    if (directory.is_symlink() or path != directory / 'hook.json'
            or path.is_symlink()
            or path.parent.resolve() != directory.resolve()):
        raise _CaptureError('UNSAFE_HOOK_PATH')
    return path


def _snapshot(selection, root):
    """Bound the project before allowing the SDK scanner to traverse it."""
    count = size = visited = 0
    config = set(selection.CONFIG)

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = [name for name in dirs if name not in _SKIP]
        visited += len(dirs) + len(files)
        if visited > 100000:
            raise _CaptureError('PROJECT_SCAN_LIMIT')
        for name in dirs:
            pilot._safe(Path(directory) / name)
        for name in files:
            if not (Path(name).suffix in config or name.startswith('requirements')
                    or name.endswith('.py')):
                continue
            path = Path(directory) / name
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise _CaptureError('UNSAFE_SOURCE_PATH')
            count += 1
            size += resolved.stat().st_size
            if count > 2048 or size > 32 * 1024 * 1024:
                raise _CaptureError('PROJECT_SCAN_LIMIT')
    files = selection._files(root)
    if not isinstance(files, dict) or len(files) > 2048:
        raise _CaptureError('INVALID_FINGERPRINTS')
    total = 0
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise _CaptureError('INVALID_FINGERPRINTS')
        path = Path(name)
        if path.is_absolute() or '..' in path.parts:
            raise _CaptureError('UNSAFE_SOURCE_PATH')
        resolved = (root / path).resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise _CaptureError('UNSAFE_SOURCE_PATH')
        total += resolved.stat().st_size
        if total > 32 * 1024 * 1024:
            raise _CaptureError('PROJECT_SCAN_LIMIT')
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise _CaptureError('INVALID_FINGERPRINTS')
    return files


def _json(data):
    def invalid_constant(value):
        raise ValueError('Non-finite JSON number')
    return json.loads(data, parse_constant=invalid_constant)


def _prepare_baseline(state):
    import specgraph
    from specgraph import test_selection as selection

    root = Path(state['root']).resolve(strict=True)
    before = _snapshot(selection, root)
    hook = _safe(Path(pilot.directory()) / 'hook.json')
    if hook.exists():
        raise _CaptureError('HOOK_ALREADY_EXISTS')
    childenv, sdk, observer, identity = _observer_environment(specgraph,selection)
    childenv.update(SG_X5_ROOT=str(root), SG_X5_OUTPUT=str(hook), SG_X5_TRACE='1')
    meta, stdout, stderr = _process(
        [state['argv'][0], '-c', ENVIRONMENT_SCRIPT,sdk,str(observer)], cwd=root,
        timeout=30, env=childenv, max_output=65536)
    if (_outcome(meta) != 0 or not isinstance(stdout, bytes)
            or not isinstance(stderr, bytes)
            or len(stdout) > 65536 or len(stderr) > 65536):
        raise _CaptureError('ENVIRONMENT_PROBE_FAILED')
    environment = _json(stdout)
    if not isinstance(environment, dict):
        raise _CaptureError('ENVIRONMENT_PROBE_INVALID')
    return dict(selection=selection, root=root, before=before, hook=hook,
                childenv=childenv, environment=environment,observer_identity=identity)


def _observer_environment(package,selection):
    sdk=str(Path(package.__file__).resolve().parent.parent)
    source=Path(package.__file__).with_name('_pytest_x5.py')
    with source.open('rb') as stream:
        raw=stream.read(32*1024+1)
    if len(raw)>32*1024:
        raise _CaptureError('OBSERVER_SOURCE_LIMIT')
    observer=pilot._safe(pilot.directory()/(pilot.OBSERVER+'.py'))
    if pilot._bytes()+len(raw)+128*1024>=pilot.TOTAL_LIMIT:
        raise _CaptureError('OBSERVER_STORAGE_LIMIT')
    with observer.open('xb') as stream:
        stream.write(raw)
    childenv=dict(os.environ)
    # A standalone plugin must not import/shadow the package under test.
    childenv['PYTHONPATH']=(childenv['PYTHONPATH']+os.pathsep if childenv.get('PYTHONPATH') else '')+str(observer.parent)
    identity={'observer_sha256':hashlib.sha256(raw).hexdigest(),
              'analyzer_sha256':hashlib.sha256(Path(selection.__file__).read_bytes()).hexdigest(),
              'sdk_origin':sdk}
    return childenv,sdk,observer,identity


ADVICE_SCRIPT = _ANALYZER + """
import json, sys, time
from pathlib import Path
from specgraph import test_selection as s
start = time.perf_counter()
p = json.load(sys.stdin)
environment = s._environment()
r = s.recommend_from_receipt(p['changed'], p['receipt'], root=Path.cwd(),
                             mandatory=p['mandatory']) if p['changed'] else None
print(json.dumps({'environment':environment, 'mode':r['mode'] if r else 'FULL_SUITE',
    'policy_tests':r['policy_tests'] if r else [],
    'applicability':r['applicability'] if r else None,
    'selection_seconds':time.perf_counter()-start}, allow_nan=False))
"""


def _prepare_shadow(state):
    import specgraph
    from specgraph import test_selection as selection
    root = Path(state['root']).resolve(strict=True)
    before = _snapshot(selection, root)
    baseline = state['baseline']
    if (not isinstance(baseline, dict) or baseline.get('schema') != selection.SCHEMA
            or not isinstance(baseline.get('fingerprints'), dict)
            or baseline.get('pytest_args') != state['argv'][3:]):
        raise _CaptureError('BASELINE_INVALID')
    hook = _safe(Path(pilot.directory())/'hook.json')
    if hook.exists():
        raise _CaptureError('HOOK_ALREADY_EXISTS')
    childenv,sdk,observer,identity=_observer_environment(specgraph,selection)
    if baseline.get('observer_identity')!=identity:
        raise _CaptureError('OBSERVER_IDENTITY_CHANGE')
    childenv.update(SG_X5_ROOT=str(root), SG_X5_OUTPUT=str(hook), SG_X5_TRACE='0')
    changed = sorted(p for p in before.keys() | baseline['fingerprints'].keys()
                     if before.get(p) != baseline['fingerprints'].get(p))
    request = json.dumps({'receipt':baseline,'changed':changed,
                          'mandatory':state['mandatory']}, allow_nan=False).encode()
    if len(request) > pilot.PILOT_LIMIT:
        raise _CaptureError('ADVICE_INPUT_LIMIT')
    meta, stdout, stderr = _process([state['argv'][0],'-c',ADVICE_SCRIPT,sdk,str(observer)],
        cwd=root,timeout=30,env=childenv,stdin=request,max_output=65536)
    if _outcome(meta) != 0 or len(stdout) > 65536 or len(stderr) > 65536:
        raise _CaptureError('ADVICE_UNAVAILABLE')
    reply = _json(stdout)
    if not isinstance(reply,dict) or reply.get('mode') != 'FULL_SUITE':
        raise _CaptureError('ADVICE_INVALID')
    if reply.get('environment') != baseline.get('environment'):
        raise _CaptureError('ENVIRONMENT_CHANGE')
    selected = reply.get('policy_tests')
    if not isinstance(selected,list) or not all(isinstance(t,str) for t in selected):
        raise _CaptureError('ADVICE_INVALID')
    codes = []
    applicable = False
    if changed:
        advice = reply.get('applicability')
        if (not isinstance(advice,dict) or advice.get('execution_authorized') is not False
                or advice.get('supports_success') is not False
                or advice.get('dependency_closure') != 'UNKNOWN'):
            raise _CaptureError('ADVICE_INVALID')
        codes = advice.get('reason_codes')
        if (not isinstance(codes,list) or len(codes)>16
                or any(not isinstance(c,str) or not c or len(c)>64
                       or any(x not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for x in c)
                       for c in codes)):
            raise _CaptureError('ADVICE_INVALID')
        applicable = (advice.get('status') == 'CANDIDATE' and not codes
                      and bool(selected) and len(set(selected)) == len(selected)
                      and set(selected) <= set(baseline.get('tests',[]))
                      and set(state['mandatory']) <= set(selected))
        if not applicable and not codes:
            codes = ['ADVICE_INVALID']
    else:
        codes = ['NO_CHANGED_TARGET']
    return dict(selection=selection,root=root,before=before,hook=hook,
                childenv=childenv,environment=reply['environment'],changed=changed,
                selected=selected,applicable=applicable,reason_codes=codes,
                analysis_seconds=_seconds(meta))


def _outcomes(capture, code, expected):
    hook = _safe(capture['hook'])
    if not hook.is_file():
        raise _CaptureError('HOOK_MISSING')
    with hook.open('rb') as stream:
        raw = stream.read(OUTPUT_LIMIT+1)
    if len(raw)>OUTPUT_LIMIT:
        raise _CaptureError('HOOK_OUTPUT_LIMIT')
    result = _json(raw)
    if (not isinstance(result,dict) or result.get('complete') is not True
            or result.get('truncated') is not False):
        raise _CaptureError('HOOK_INCOMPLETE', reason_codes=_incomplete_reasons(result))
    if type(result.get('exitstatus')) is not int or result['exitstatus'] != code:
        raise _CaptureError('HOOK_EXIT_MISMATCH')
    tests, failed = result.get('tests'), result.get('failed')
    if (not isinstance(tests,list) or not tests
            or not all(isinstance(t,str) for t in tests)
            or len(tests)!=len(set(tests)) or set(tests)!=set(expected)
            or not isinstance(failed,list) or not all(isinstance(t,str) for t in failed)
            or len(failed)!=len(set(failed)) or not set(failed)<=set(tests)):
        raise _CaptureError('INVENTORY_CHANGE')
    if _snapshot(capture['selection'],capture['root']) != capture['before']:
        raise _CaptureError('SOURCE_CHANGED')
    return result


def _candidate_argv(state, selected):
    # These switches have no following value. Ambiguous pytest arguments retain
    # the original full check rather than being reconstructed speculatively.
    safe = {'-q','-qq','-v','-vv','-x','--disable-warnings','--strict-markers'}
    options = []
    for arg in state['argv'][3:]:
        if arg in safe:
            options.append(arg)
        elif arg.startswith('-'):
            raise _CaptureError('CANARY_ARGV_UNSUPPORTED')
    return [*state['argv'][:3],*options,*selected,'-p',pilot.OBSERVER]


def _baseline(state, capture, code):
    hook = _safe(capture['hook'])
    if not hook.is_file():
        raise _CaptureError('HOOK_MISSING')
    with hook.open('rb') as stream:
        raw = stream.read(OUTPUT_LIMIT + 1)
    if len(raw) > OUTPUT_LIMIT:
        raise _CaptureError('HOOK_OUTPUT_LIMIT')
    result = _json(raw)
    if (not isinstance(result, dict) or result.get('complete') is not True
            or result.get('truncated') is not False):
        raise _CaptureError('HOOK_INCOMPLETE', reason_codes=_incomplete_reasons(result))
    if type(result.get('exitstatus')) is not int or result['exitstatus'] != code:
        raise _CaptureError('HOOK_EXIT_MISMATCH')
    tests = result.get('tests')
    if not isinstance(tests, list) or not tests:
        raise _CaptureError('INVALID_TEST_INVENTORY')
    for nodeid in tests:
        if (not isinstance(nodeid, str) or not nodeid or len(nodeid) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in nodeid)):
            raise _CaptureError('UNSAFE_NODEID')
        parts = nodeid.split('::')
        file = parts[0]
        if (len(parts) < 2 or not all(parts) or '\\' in file or ':' in file
                or file.startswith('/') or '..' in file.split('/')
                or file not in capture['before'] or not file.endswith('.py')):
            raise _CaptureError('UNSAFE_NODEID')
    if len(set(tests)) != len(tests):
        raise _CaptureError('DUPLICATE_TEST_INVENTORY')
    blocks = result.get('executed_blocks')
    if not isinstance(blocks, dict) or set(blocks) != set(tests):
        raise _CaptureError('INCOMPLETE_EXECUTED_BLOCKS')
    if result.get('failed') != [] or code != 0:
        raise _CaptureError('FULL_CHECK_FAILED')
    selection = capture['selection']
    if _snapshot(selection, capture['root']) != capture['before']:
        raise _CaptureError('SOURCE_CHANGED')
    receipt = {
        'schema': selection.SCHEMA, 'tests': tests, 'executed_blocks': blocks,
        'fingerprints': capture['before'], 'environment': capture['environment'],
        'config_sha256': _digest({name: digest for name, digest in capture['before'].items()
                                 if Path(name).suffix in selection.CONFIG}),
        'environment_sha256': _digest(capture['environment']),
        'pytest_args': list(state['argv'][3:]), 'suite_returncode': 0,
        'failed': [], 'complete': True, 'truncated': False,
        'dependency_closure': 'UNKNOWN', 'attestation': 'NONE',
        'observer_identity':capture['observer_identity'],
    }
    if len(json.dumps(receipt, sort_keys=True).encode()) > OUTPUT_LIMIT:
        raise _CaptureError('BASELINE_OUTPUT_LIMIT')
    return receipt


def run_pilot(pilot_id, *, store=None):
    # A -m entry has its own __main__ module. Bind custody in the actual owner,
    # rather than depending on its environment or another module's ContextVar.
    with pilot.using_store(store):
        return _run_pilot(pilot_id)


def _run_pilot(pilot_id):
    """Observe an explicit check; a candidate pass never replaces its audit."""
    authoritative = None
    try:
        with pilot.execution_state(pilot_id) as state:
            active = state['status'] == 'ACTIVE'
            started = time.monotonic()
            capture = None
            reasons = []
            shadow = active and state.get('baseline') is not None
            candidate_meta = candidate_code = candidate_result = None
            if active:
                try:
                    capture = _prepare_shadow(state) if shadow else _prepare_baseline(state)
                    if shadow:
                        reasons = capture['reason_codes']
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, _CaptureError) else 'PREPARATION_FAILED'
                    reasons = [reason, 'FULL_CHECK_ONLY']
            if (shadow and capture is not None and capture['applicable']
                    and state['mode'] == 'canary'):
                try:
                    if len(capture['selected']) >= len(state['baseline']['tests']):
                        raise _CaptureError('NO_REDUCTION')
                    argv = _candidate_argv(state,capture['selected'])
                except _CaptureError as exc:
                    reasons = [str(exc)]
                else:
                    candidate_meta, out, err = _process(argv,cwd=capture['root'],
                        timeout=30,env=capture['childenv'],max_output=OUTPUT_LIMIT)
                    candidate_code = _outcome(candidate_meta)
                    _warn('candidate comparison; the full audit remains required.')
                    _emit(out,sys.stdout); _emit(err,sys.stderr)
                    candidate_result = _outcomes(capture,candidate_code,capture['selected'])
                    if candidate_code != 0:
                        reasons = ['CANDIDATE_FAILED']
            argv = list(state['argv'])
            childenv = dict(os.environ)
            if capture is not None:
                argv += ['-p',pilot.OBSERVER]
                childenv = capture['childenv']
            metadata = None
            stdout = stderr = b''
            # Fail fast on a known candidate failure. No full check is claimed.
            if candidate_code in (None,0):
                metadata, stdout, stderr = _process(
                    argv, cwd=Path(state['root']), timeout=TIMEOUT,
                    env=childenv, max_output=OUTPUT_LIMIT)
            # Establish custody before optional output or observation handling.
                authoritative = _outcome(metadata)
            _emit(stdout, sys.stdout)
            _emit(stderr, sys.stderr)
            if active:
                receipt = None
                outcomes = None
                omitted = None
                missing = []
                if len(stdout) > OUTPUT_LIMIT or len(stderr) > OUTPUT_LIMIT:
                    reasons.append('OUTPUT_BOUND_EXCEEDED')
                if not shadow and authoritative != 0:
                    reasons.append('FULL_CHECK_FAILED')
                if capture is not None and metadata is not None and _seconds(metadata) is None:
                    reasons.append('EXECUTION_TIMING_UNAVAILABLE')
                if shadow and capture is not None and metadata is not None:
                    try:
                        outcomes = _outcomes(capture,authoritative,state['baseline']['tests'])
                        if capture['applicable']:
                            missing = sorted(set(outcomes['failed'])-set(capture['selected']))
                            omitted = bool(missing)
                            if omitted:
                                reasons = ['OMITTED_FAILURE']
                        if candidate_result is not None and (
                                set(candidate_result['failed']) !=
                                (set(outcomes['failed']) & set(capture['selected']))):
                            reasons = list(dict.fromkeys([*reasons,'INCONSISTENT_CANDIDATE_OUTCOME']))
                    except Exception as exc:
                        reasons = ([str(exc), *exc.reason_codes] if isinstance(exc, _CaptureError)
                                   else ['OUTCOMES_INVALID'])
                if not shadow and capture is not None and not reasons:
                    try:
                        receipt = _baseline(state, capture, authoritative)
                    except Exception as exc:
                        reasons = ([str(exc), *exc.reason_codes] if isinstance(exc, _CaptureError)
                                   else ['BASELINE_INVALID'])
                complete = (receipt is not None or
                            outcomes is not None and reasons in ([],['NO_CHANGED_TARGET'],['OMITTED_FAILURE']))
                selected_seconds = _seconds(candidate_meta) if candidate_meta else None
                full_seconds = _seconds(metadata) if metadata else None
                count = len(receipt['tests']) if receipt else len(outcomes['tests']) if outcomes else None
                mode = ('canary' if candidate_meta is not None else
                        'shadow' if shadow and capture is not None and capture['applicable'] else
                        'baseline' if receipt else 'fallback')
                observation = {
                    'complete': complete, 'omitted': omitted,
                    'mode': mode,
                    'reason_codes': ['BASELINE_CAPTURED'] if receipt else reasons,
                    'seconds': max(0.0, time.monotonic() - started),
                    'full_seconds': full_seconds,
                    'execution_seconds': selected_seconds if candidate_meta else full_seconds,
                    'selected_seconds': selected_seconds,
                    'selected': len(capture['selected']) if shadow and capture is not None and capture['applicable'] else None,
                    'collected': count,
                    'passed': None, 'failed': len(outcomes['failed']) if outcomes else 0 if receipt else None,
                    'exit_code': authoritative if authoritative is not None and authoritative >= 0 else None,
                    'command_digest': state['command_digest'],
                    'baseline_comparable': False, 'net_operational_savings': None,
                    'recapture_seconds': 0, 'retry_seconds': 0,
                    'audit_seconds': full_seconds if candidate_meta is not None else 0,
                    'capture_seconds': None,
                    'selection_seconds': capture.get('analysis_seconds') if shadow and capture else 0 if receipt else None,
                    'correction_seconds': None, 'invalidation_seconds': None,
                    'observed_at': time.time(),
                }
                if capture is not None:
                    observation['target_digest'] = _digest(capture['before'])
                if outcomes:
                    observation['collection_digest'] = _digest(outcomes['tests'])
                if shadow:
                    observation['baseline_digest'] = _digest(state['baseline'])
                    if capture is not None and capture['applicable']:
                        observation['selection_digest'] = _digest(capture['selected'])
                if missing:
                    observation['omitted_tests'] = missing[:16]
                if receipt is not None:
                    observation['collection_digest'] = _digest(receipt['tests'])
                    state['baseline'] = receipt
                pilot.record_in_state(state, observation)
                # Active records otherwise commit only when custody exits.
                if state['status']=='ACTIVE':
                    pilot._save('pilot.json',pilot._validate(state),pilot.PILOT_LIMIT)
                pilot.clean_scratch()
                if not complete:
                    _warn('full check only; ' + ', '.join(reasons) +
                          '; no adequacy or savings claim.')
        return authoritative if authoritative is not None else 2
    except Exception as exc:
        # A completed full result survives optional bookkeeping failures.
        # An uncontained execution leaves custody pending; never retry it.
        reason = ''.join(c if c.isprintable() else ' ' for c in str(exc)[:240])
        _warn('collection/execution stopped (' + type(exc).__name__ + ': ' +
              reason + '); no retry performed. '
              'Run tare test-selection status and review before proceeding.')
        return authoritative if authoritative is not None else 2

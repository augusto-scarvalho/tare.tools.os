"""Bounded, explicit pytest pilot custody; never authorizes selection.

Runner API: with execution_state(id, now=...) as state holds custody across
the actual command and persists its reservation before entering the body. Only
ACTIVE state permits instrumentation; otherwise execute state['argv'] unchanged.
Call record_in_state(state, compact, now=...) inside that context, never the
locking record_observation/read/write_hook helpers. Baseline updates may use
the yielded state. Exceptions preserve the pending reservation: do not retry.
Stale IDs and unreviewed incomplete captures raise before entering the body.
begin_run/record_observation remain separate legacy reservation APIs, not a
command-spanning custody API. The full configured check remains authoritative.
"""
import argparse
import copy
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from compute_plane.native_journal import custody_lock, read_json, regular, save_json
from . import native_installation

PILOT_LIMIT = 1280 * 1024
BASELINE_LIMIT = HOOK_LIMIT = 1024 * 1024
OBS_LIMIT = 16 * 1024
REPORT_LIMIT = 32 * 1024  # two copies, including atomic pending, <=64 KiB
TOTAL_LIMIT = 4 * 1024 * 1024
OBSERVER = '_tare_x5_observer'
SCHEMA = 'tare.tools/test-selection/1'
_STORE = ContextVar('test_selection_store',default=None)
REVIEWS_SCHEMA = 'tare.tools/test-selection-reviews/1'
OWNED = ('writer.lock', 'pilot.json', 'pilot.pending', 'pilot.json.pending', 'hook.json',
         'hook.pending', 'hook.json.pending', 'reviews.json', 'reviews.pending',
         'reviews.json.pending',OBSERVER+'.py')
EPHEMERAL = ('hook.json', 'hook.pending', 'hook.json.pending',
             'pilot.pending', 'pilot.json.pending',OBSERVER+'.py')
BINDING = ('schema', 'id', 'root', 'argv', 'mandatory', 'mode', 'created_at',
           'deadline', 'command_digest', 'max_runs')
OBS_FIELDS = {'complete', 'omitted', 'seconds', 'full_seconds', 'selected_seconds',
              'collected', 'selected', 'passed', 'failed', 'exit_code',
              'command_digest', 'collection_digest', 'selection_digest',
              'observed_at', 'target_digest', 'baseline_digest', 'mode',
              'reason_codes', 'omitted_tests', 'baseline_comparable',
              'net_operational_savings', 'capture_seconds', 'recapture_seconds',
              'selection_seconds', 'execution_seconds', 'invalidation_seconds',
              'retry_seconds', 'correction_seconds', 'audit_seconds'}


def _clock(now):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid clock')
    return value


def _safe(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Redirected custody path refused')
    return regular(path)


def directory():
    selected = _STORE.get()
    return _safe(selected if selected is not None else native_installation.storage_root() / 'test-selection')


@contextmanager
def using_store(path):
    """An explicit verifier binding survives the Kernel's filtered environment."""
    if path is None:
        yield
        return
    path=Path(path)
    if not path.is_absolute() or path.name!='test-selection':
        raise ValueError('Expected an absolute test-selection custody directory')
    path=_safe(path)
    if not path.is_dir():
        raise ValueError('Pilot custody directory is unavailable')
    token=_STORE.set(path)
    try:
        yield
    finally:
        _STORE.reset(token)


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False).encode()) + 1


def _bounded(value, limit):
    if _size(value) > limit:
        raise ValueError('Pilot byte budget exceeded')
    return value


def _reviews(envelope):
    if (not isinstance(envelope, dict) or set(envelope) != {'schema', 'reviews'}
            or envelope.get('schema') != REVIEWS_SCHEMA
            or not isinstance(envelope.get('reviews'), list)
            or len(envelope['reviews']) > 8
            or any(not isinstance(report, dict) for report in envelope['reviews'])):
        raise ValueError('Invalid reviews report')
    return envelope['reviews']


def _load(name, limit, default=None):
    path = _safe(directory() / name)
    if not path.exists():
        return default
    value = read_json(path, max_bytes=limit)
    return _reviews(value) if name == 'reviews.json' else value


def _bytes():
    total = 0
    for name in OWNED:
        path = _safe(directory() / name)
        if path.exists():
            limit = (HOOK_LIMIT if name.startswith('hook') else
                     REPORT_LIMIT if name.startswith('reviews') else PILOT_LIMIT)
            # Count malformed hook bytes so a bounded rejection summary can be
            # saved before this owned scratch is removed. Never load it here.
            if not path.is_file() or (path.stat().st_size > limit and not name.startswith('hook')):
                raise ValueError('Owned file exceeds its byte budget')
            total += path.stat().st_size
    total += sum(p.stat().st_size for p in _observer_cache())
    return total


def _observer_cache():
    cache=_safe(directory()/'__pycache__')
    if not cache.exists():
        return []
    paths=list(cache.glob(OBSERVER+'*'))
    if len(paths)>8:
        raise ValueError('Observer cache count exceeds its bound')
    for path in paths:
        _safe(path)
        if not path.is_file() or path.stat().st_size>128*1024:
            raise ValueError('Observer cache exceeds its byte bound')
    return paths


def clean_scratch():
    """Caller holds custody and has persisted the observed result."""
    paths=[_safe(directory()/name) for name in EPHEMERAL]
    paths+=_observer_cache()
    for path in paths:
        if path.exists():
            path.unlink()


def _save(name, value, limit):
    if name == 'reviews.json':
        value = {'schema': REVIEWS_SCHEMA, 'reviews': value}
        _reviews(value)
    _bounded(value, limit)
    # Reserve the new atomic pending while the old destination still exists.
    # Also bound the final destination plus a same-sized interrupted pending.
    used = _bytes()
    path = _safe(directory() / name)
    previous = path.stat().st_size if path.exists() else 0
    if max(used + _size(value), used - previous + 2 * _size(value)) >= TOTAL_LIMIT:
        raise ValueError('Total owned storage budget exceeded')
    save_json(_safe(directory() / name), value)


def _argv(argv):
    if (not isinstance(argv, (list, tuple)) or not 3 <= len(argv) <= 256
            or any(not isinstance(s, str) or not s or len(s) > 4096
                   or any(ord(c) < 32 for c in s) for s in argv)
            or sum(len(s.encode()) for s in argv) > 32768
            or argv[1:3] not in (['-m', 'pytest'], ('-m', 'pytest'))
            or not re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?',
                                Path(argv[0]).name, re.I)):
        raise ValueError('Expected bounded Python -m pytest argv')
    return list(argv)


def _mandatory(values):
    if not isinstance(values, (list, tuple)) or len(values) > 128:
        raise ValueError('Invalid mandatory nodeids')
    for node in values:
        if (not isinstance(node, str) or len(node) > 1024
                or not re.fullmatch(r'[A-Za-z0-9_./:-]+(?:\[[A-Za-z0-9_,=.-]+\])?', node)):
            raise ValueError('Invalid mandatory nodeid')
        file, *parts = node.split('::')
        if (not file.endswith('.py') or file.startswith(('/', '-'))
                or ':' in file or any(p in ('', '.', '..') for p in file.split('/'))
                or any(not p or ':' in p or '/' in p for p in parts)):
            raise ValueError('Unsafe mandatory nodeid')
    return sorted(set(values))


def _digest(argv):
    return hashlib.sha256(json.dumps(argv, ensure_ascii=True).encode()).hexdigest()


def _observation(value):
    if not isinstance(value, dict) or set(value) - OBS_FIELDS:
        raise ValueError('Only compact observation aggregates are allowed')
    _bounded(value, OBS_LIMIT)
    for key, item in value.items():
        if key == 'net_operational_savings':
            valid = item is None
        elif key == 'baseline_comparable':
            valid = item is False
        elif key == 'mode':
            valid = item in ('baseline', 'shadow', 'canary', 'fallback')
        elif key == 'reason_codes':
            valid = (isinstance(item, list) and len(item) <= 16
                     and all(isinstance(code, str)
                             and re.fullmatch(r'[A-Z_]{1,64}', code)
                             for code in item))
        elif key == 'omitted_tests':
            valid = isinstance(item, list) and len(item) <= 16
            if valid:
                _mandatory(item)
        elif item is None and key not in ('observed_at', 'target_digest', 'baseline_digest'):
            continue
        elif key in ('complete', 'omitted'):
            valid = type(item) is bool
        elif key.endswith('digest'):
            valid = isinstance(item, str) and bool(re.fullmatch('[0-9a-f]{64}', item))
        else:
            valid = type(item) in (int, float) and math.isfinite(item) and item >= 0
        if not valid:
            raise ValueError('Invalid observation aggregate')
    return copy.deepcopy(value)


def _validate(s):
    if (not isinstance(s, dict) or s.get('schema') != SCHEMA
            or not re.fullmatch('[0-9a-f]{32}', str(s.get('id', '')))
            or s.get('status') not in ('ACTIVE', 'REVIEW_REQUIRED', 'REVIEWED')
            or s.get('mode') not in ('shadow', 'canary') or s.get('max_runs') != 5
            or not isinstance(s.get('root'), str) or not Path(s['root']).is_absolute()
            or type(s.get('runs')) is not int or not 0 <= s['runs'] <= 5
            or type(s.get('capture_pending')) is not bool
            or not isinstance(s.get('observations'), list)
            or len(s['observations']) > s['runs']
            or s.get('decision') not in (None, 'keep-full', 'investigate-canary')):
        raise ValueError('Invalid pilot state')
    if (_argv(s['argv']) != s['argv'] or _digest(s['argv']) != s['command_digest']
            or _mandatory(s['mandatory']) != s['mandatory']
            or _clock(s['deadline']) != _clock(s['created_at']) + 7 * 86400
            or (s['status'] == 'REVIEWED') != (s['decision'] is not None)):
        raise ValueError('Invalid immutable pilot binding')
    for obs in s['observations']:
        _observation(obs)
    _bounded(s.get('baseline'), BASELINE_LIMIT)
    return _bounded(s, PILOT_LIMIT)


def summarize(s):
    result = {k: s.get(k) for k in ('id', 'root', 'status', 'mode', 'created_at',
              'deadline', 'runs', 'max_runs', 'decision', 'reason', 'finalized_at')}
    result['observations'] = len(s['observations'])
    for key in ('seconds', 'full_seconds', 'selected_seconds'):
        values = [o.get(key) for o in s['observations']]
        result[key] = sum(values) if values and all(v is not None for v in values) else None
    omissions = [o.get('omitted') for o in s['observations']]
    result['unknown_omission_observations'] = sum(v is None for v in omissions)
    result['confirmed_omissions'] = (sum(v is True for v in omissions)
                                     if any(v is True for v in omissions) else
                                     0 if omissions and all(v is False for v in omissions)
                                     else None)
    result['reason_codes'] = sorted({code for o in s['observations']
                                     for code in o.get('reason_codes', [])})
    # seconds is the total; phase timings are breakdowns, never added again.
    result.update(command_digest=s['command_digest'], net_savings=None,
                  authority='full configured check remains authoritative')
    example = next((test for o in s['observations'] for test in o.get('omitted_tests', [])), None)
    if example is not None:
        result['example_omitted_test'] = example
        if _size(result)>8*1024:
            del result['example_omitted_test']
    return _bounded(result, 8 * 1024)


def _retire(s, reason, now):
    s.update(status='REVIEW_REQUIRED' if s['decision'] is None else 'REVIEWED',
             reason=reason, finalized_at=now, capture_pending=False)
    reports = _load('reviews.json', REPORT_LIMIT, [])
    if not isinstance(reports, list) or len(reports) > 8:
        raise ValueError('Invalid reviews report')
    reports = [r for r in reports if isinstance(r, dict) and r.get('id') != s['id']]
    reports = (reports + [summarize(s)])[-8:]
    # Keep the newest reports within both the count and envelope byte limits.
    while len(reports) > 1 and _size(
            {'schema': REVIEWS_SCHEMA, 'reviews': reports}) > REPORT_LIMIT:
        reports.pop(0)
    _save('reviews.json', reports, REPORT_LIMIT)
    # Persist the compact terminal state before deleting even owned scratch.
    s['baseline'] = None
    _save('pilot.json', _validate(s), PILOT_LIMIT)
    clean_scratch()  # fixed owned files only; never recursive or receipt-supplied


def _due(s, now, *, abandoned=True):
    if s['status'] == 'ACTIVE':
        reason = ('deadline' if now >= s['deadline'] else
                  'incomplete capture' if abandoned and s['capture_pending'] else
                  'invocation cap' if s['runs'] >= 5 else None)
        if reason:
            _retire(s, reason, now)


@contextmanager
def locked_state(pilot_id=None, *, now=None):
    """Yield validated state under native custody; binding cannot be changed.

    Baseline is bounded JSON (<=1 MiB). Runner owns its schema; no stdout or
    project-file copies belong here. This context is not an execution lease.
    """
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    with custody_lock(root):
        s = _load('pilot.json', PILOT_LIMIT)
        if s is None:
            if pilot_id is not None:
                raise ValueError('Pilot no longer exists')
            yield None
            return
        _validate(s)
        if pilot_id is not None and pilot_id != s['id']:
            raise ValueError('Stale pilot wrapper; configured command not executed')
        binding = {k: copy.deepcopy(s[k]) for k in BINDING}
        before = copy.deepcopy(s)
        yield s
        if any(s.get(k) != v for k, v in binding.items()):
            raise ValueError('Original command/pilot binding is immutable')
        _validate(s)
        if s != before:
            _save('pilot.json', s, PILOT_LIMIT)


def read(*, pilot_id=None, now=None):
    with locked_state(pilot_id) as s:
        if s is not None:
            _due(s, _clock(now))
        return copy.deepcopy(s)


def start(root, argv, *, mandatory=(), mode='shadow', now=None):
    if not Path(root).is_absolute():
        raise ValueError('Explicit absolute project root required')
    root = _safe(root)
    if not root.is_dir() or mode not in ('shadow', 'canary'):
        raise ValueError('Existing project root and shadow/canary mode required')
    argv, mandatory, now = _argv(argv), _mandatory(mandatory), _clock(now)
    dest = directory()
    if dest.is_relative_to(root):
        raise ValueError('Pilot storage must be outside the project')
    dest.mkdir(parents=True, exist_ok=True)
    with custody_lock(dest):
        old = _load('pilot.json', PILOT_LIMIT)
        if old is not None and _validate(old)['status'] != 'REVIEWED':
            raise ValueError('Explicit review required before replacing pilot')
        s = dict(schema=SCHEMA, id=uuid.uuid4().hex, root=str(root), argv=argv,
                 mandatory=mandatory, mode=mode, created_at=now, deadline=now+7*86400,
                 command_digest=_digest(argv), status='ACTIVE', max_runs=5, runs=0,
                 observations=[], baseline=None, decision=None, reason=None,
                 capture_pending=False)
        _save('pilot.json', _validate(s), PILOT_LIMIT)
    return {'wrapper_argv': [sys.executable, '-m', 'relay.test_selection_pilot',
                            'run', '--store',str(dest),'--pilot-id', s['id']], 'destination': str(dest)}


def write_hook(pilot_id, value):
    """Runner-only bounded temporary collection JSON; no transcript copies."""
    with locked_state(pilot_id) as s:
        if s['status'] != 'ACTIVE' or not s['capture_pending']:
            raise ValueError('No active invocation reservation')
        _save('hook.json', value, HOOK_LIMIT)


def read_hook(pilot_id):
    with locked_state(pilot_id):
        return _load('hook.json', HOOK_LIMIT)


def begin_run(pilot_id, *, now=None):
    with locked_state(pilot_id) as s:
        _due(s, _clock(now))
        if s['status'] == 'ACTIVE':
            s['runs'] += 1
            s['capture_pending'] = True
        return copy.deepcopy(s)


@contextmanager
def execution_state(pilot_id, *, now=None):
    """Reserve durably, then hold custody through the caller's actual command.

    Trusted same-owner composition: finish with record_in_state, not another
    locking API. An exception leaves the on-disk reservation pending.
    """
    now = _clock(now)
    with locked_state(pilot_id) as s:
        if s['capture_pending']:
            _retire(s, 'incomplete capture', now)
            raise ValueError('Incomplete capture requires explicit review; command not executed')
        _due(s, now)
        if s['status'] != 'REVIEWED' and s.get('reason') == 'incomplete capture':
            raise ValueError('Unfinished execution requires explicit review; command not executed')
        if s['status'] == 'ACTIVE':
            s['runs'] += 1
            s['capture_pending'] = True
            _save('pilot.json', _validate(s), PILOT_LIMIT)
        yield s


def record_in_state(state, observation, *, now=None):
    """Record under caller-held custody; does not acquire another lock.

    Active changes commit when the surrounding context exits normally.
    Terminal observations use the existing retirement/report persistence.
    """
    observation, now = _observation(observation), _clock(now)
    s = _validate(state)
    if s['status'] != 'ACTIVE' or not s['capture_pending']:
        raise ValueError('No active invocation reservation')
    s['observations'].append(observation)
    s['capture_pending'] = False
    if observation.get('complete') is not True or observation.get('omitted') is True:
        _retire(s, 'omission or incomplete capture', now)
    else:
        _due(s, now, abandoned=False)
    return summarize(s)


def record_observation(pilot_id, observation, *, now=None):
    with locked_state(pilot_id) as s:
        return record_in_state(s, observation, now=now)


def stop(pilot_id, reason='instrumentation stopped', *, now=None):
    if reason not in ('instrumentation stopped', 'omission', 'incomplete capture', 'capture failure'):
        raise ValueError('Unsupported stop reason')
    with locked_state(pilot_id) as s:
        if s['status'] == 'ACTIVE':
            _retire(s, reason, _clock(now))
        return summarize(s)


def review(decision, *, now=None):
    if decision not in ('keep-full', 'investigate-canary'):
        raise ValueError('Review requires keep-full or investigate-canary')
    with locked_state() as s:
        if s is None:
            raise ValueError('No pilot to review')
        if s['decision'] is not None:
            raise ValueError('Pilot already reviewed')
        s['decision'] = decision
        _retire(s, 'explicit review', _clock(now))
        return summarize(s)


def usage_rows(root=None, *, now=None):
    s = read(now=now)
    if s is None or root is not None and Path(root).resolve() != Path(s['root']).resolve():
        return []
    summary = summarize(s)
    action = ('tare test-selection status' if s['status']=='REVIEWED' else
              'tare test-selection review --decision keep-full')
    deadline = datetime.fromtimestamp(s['deadline'],timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    seconds = lambda v: 'Unknown' if v is None else f'{v:.3f} s'
    body = (f"Project: {s['root']}\nStatus: {s['status']}; decision: {s['decision'] or 'pending'}\n"
            f"Budget: {s['runs']}/5 checks or until {deadline}; review at any time.\n"
            f"Destination: {directory()}\nOwned bytes: {_bytes()}/{TOTAL_LIMIT}\n"
            f"Observed validation time: {seconds(summary['seconds'])}; "
            f"full checks: {seconds(summary['full_seconds'])}; candidates: {seconds(summary['selected_seconds'])}\n"
            f"Confirmed omission runs: {summary['confirmed_omissions'] if summary['confirmed_omissions'] is not None else 'Unknown'}; "
            f"unassessed: {summary['unknown_omission_observations']}\n"
            "full configured check remains authoritative; net savings Unknown\n"
            "A green full check does not establish selection adequacy.\n"
            f"Next action: {action}")
    return [{'id':'test-selection-'+s['id'],'label':'Test selection · '+s['status'],'body':body}]


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('start')
    p.add_argument('--root', required=True)
    p.add_argument('--mandatory', action='append', default=[])
    p.add_argument('--mode', choices=('shadow', 'canary'), default='shadow')
    p.add_argument('original', nargs=argparse.REMAINDER)
    commands.add_parser('status')
    p = commands.add_parser('review')
    p.add_argument('--decision', required=True, choices=('keep-full', 'investigate-canary'))
    p = commands.add_parser('run')
    p.add_argument('--pilot-id', required=True)
    p.add_argument('--store',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'start':
            original = args.original[1:] if args.original[:1] == ['--'] else args.original
            result = start(args.root, original, mandatory=args.mandatory, mode=args.mode)
        elif args.command == 'status':
            s = read()
            result = {**(summarize(s) if s else {'status':'NONE'}),'destination':str(directory()),
                      'limits':{'checks':5,'days':7,'owned_bytes':TOTAL_LIMIT,
                                'baseline_bytes':BASELINE_LIMIT,'observation_bytes':OBS_LIMIT},
                      'reviews':_load('reviews.json',REPORT_LIMIT,[])}
        elif args.command == 'review':
            result = review(args.decision)
        else:
            with using_store(args.store):
                state=read(pilot_id=args.pilot_id)
                if Path.cwd().resolve()!=Path(state['root']).resolve():
                    raise ValueError('Pilot belongs to another project; command not executed')
                from .test_selection_runner import run_pilot
                return run_pilot(args.pilot_id,store=args.store)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print('test-selection: ' + str(exc).replace('\n', ' ')[:240], file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

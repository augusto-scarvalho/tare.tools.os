"""Durable operator requests and custody-held, quiescent Work checkpoints."""
import argparse
import json
import math
import subprocess
import time
from datetime import datetime
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular, save_json
from graph_backlog.jsonutil import sha256_canonical
from .native_agent import read_json
from . import work_run

FILE = 'WORK_CONTROL.json'
LIMIT = 1024 * 1024
STATES = {'RUNNING', 'PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED'}


def _binding(work_id, graph_path, repository_root):
    from . import relay_mesh
    from .train_execution import read_execution_descriptor, validate_descriptor_for_train
    _, canonical, graph = work_run._resolve_graph_scope(graph_path)
    node = work_run.load_node(graph, work_id)
    train = regular(work_run.train_dir_for(work_id, graph_path=graph))
    origin = read_json(regular(train/'WORK_RUN.json'), LIMIT)
    descriptor = read_execution_descriptor(regular(train/'EXECUTION_DESCRIPTOR.json'))
    state = read_json(regular(train/'STATE.json'), LIMIT)
    tid = work_run.train_id_for(work_id, graph_path=graph)
    root = regular(Path(origin['repository_root']).resolve())
    selected_root = Path(repository_root).resolve() if repository_root is not None else Path.cwd().resolve()
    if selected_root != root:
        raise ValueError('WORK_CONTROL_REPOSITORY_CHANGED')
    if (origin.get('schema') not in {'tare.tools/work-run-origin/1', 'tare.tools/work-run-origin/2'}
            or (origin.get('schema') == 'tare.tools/work-run-origin/2') != ('shared_context' in origin)
            or origin.get('work_id') != work_id or not isinstance(origin.get('run_id'), str)
            or len(origin['run_id']) != 32 or any(c not in '0123456789abcdef' for c in origin['run_id'])
            or origin.get('node') != node
            or work_run._canonicalize_receipt_graph_path(origin.get('graph_path')) != canonical
            or (Path(relay_mesh.RELAY_ROOT)/descriptor.work_graph).resolve() != Path(graph)
            or descriptor.work_id != work_id or descriptor.train_id != tid
            or origin.get('descriptor_sha256') != descriptor.sha256
            or state.get('train_id') != tid or state.get('task_ids') != [work_id]
            or (state.get('status') != 'PLAN_AUDIT_PENDING'
                and (state.get('execution_descriptor') or {}).get('sha256') != descriptor.sha256)):
        raise ValueError('WORK_CONTROL_IDENTITY_CHANGED')
    if 'shared_context' in origin:
        from .work_context_reuse import snapshot
        snapshot(origin['shared_context'])
    if state.get('status') not in {'PLAN_AUDIT_PENDING', 'PLAN_APPROVED', 'IN_PROGRESS_IMPLEMENTER', 'AUDIT_PENDING', 'READY_FOR_LANDING'}:
        raise ValueError('WORK_CONTROL_TERMINAL_OR_UNCERTAIN')
    validate_descriptor_for_train(descriptor, train_id=tid, task_ids=[work_id], work_graph_file=graph)
    target = work_run.git(root, 'rev-parse', descriptor.target_ref)
    if target != descriptor.base_commit:
        raise ValueError('WORK_CONTROL_TARGET_MOVED')
    return train, {'graph_path': canonical, 'work_id': work_id, 'repository_root': str(root),
                   'run_id': origin['run_id'], 'descriptor_sha256': descriptor.sha256,
                   'node': node, 'target_ref': descriptor.target_ref, 'target_commit': target}


def _empty():
    return {'schema': 'tare.tools/work-control/1', 'status': 'RUNNING',
            'pending_note': None, 'last_command': None, 'paused_at': None,
            'resume_at': None, 'remaining_active_seconds': None,
            'deadline_snapshots': [], 'receiver': None}


def _note(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 8192:
        raise ValueError('WORK_CONTROL_NOTE_INVALID')


def _record(train, identity):
    path = regular(train/FILE)
    if not path.exists():
        return _empty()
    saved = read_json(path, LIMIT)
    if set(saved) != {'identity', 'record'} or saved['identity'] != identity:
        raise ValueError('WORK_CONTROL_IDENTITY_CHANGED')
    record = saved['record']
    if (not isinstance(record, dict) or set(record) != set(_empty())
            or record.get('schema') != 'tare.tools/work-control/1'
            or record.get('status') not in STATES):
        raise ValueError('WORK_CONTROL_RECORD_INVALID')
    last = record['last_command']
    if last is not None and (not isinstance(last, dict) or set(last) != {'operation', 'expected', 'note'}
            or last['operation'] not in {'pause', 'resume', 'intervene'}
            or not isinstance(last['note'], str) or not isinstance(last['expected'], str)
            or len(last['expected']) != 64 or any(c not in '0123456789abcdef' for c in last['expected'])):
        raise ValueError('WORK_CONTROL_RECORD_INVALID')
    pending = record['pending_note']
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {'id', 'text'}:
            raise ValueError('WORK_CONTROL_RECORD_INVALID')
        _note(pending['text'])
        if not isinstance(pending['id'], str) or len(pending['id']) != 64:
            raise ValueError('WORK_CONTROL_RECORD_INVALID')
    budget = record['remaining_active_seconds']
    if budget is not None and (type(budget) not in (int, float) or not math.isfinite(budget) or budget < 0):
        raise ValueError('WORK_CONTROL_RECORD_INVALID')
    for key in ('paused_at', 'resume_at'):
        if record[key] is not None and (not isinstance(record[key], str) or not record[key]):
            raise ValueError('WORK_CONTROL_RECORD_INVALID')
    status = record['status']
    if ((last is None and status != 'PAUSED')
            or (status == 'RUNNING' and last['operation'] != 'resume')
            or (status == 'PAUSE_REQUESTED' and (last['operation'] != 'pause' or record['paused_at'] is not None))
            or (status in {'PAUSED', 'RESUME_REQUESTED'} and record['paused_at'] is None)
            or (status == 'RESUME_REQUESTED' and (last['operation'] != 'resume' or record['resume_at'] is None))
            or (status not in {'RESUME_REQUESTED', 'RUNNING'} and record['resume_at'] is not None)):
        raise ValueError('WORK_CONTROL_RECORD_INVALID')
    if (not isinstance(record['deadline_snapshots'], list) or len(record['deadline_snapshots']) > 2
            or (record['receiver'] is not None and
                (not isinstance(record['receiver'], str) or not record['receiver'] or len(record['receiver']) > 4096))):
        raise ValueError('WORK_CONTROL_RECORD_INVALID')
    return record


def _view(identity, record):
    return {'identity': identity, 'record': record, 'status': record['status'],
            'token': sha256_canonical({'identity': identity, 'record': record})}


def inspect(work_id, *, graph_path=None, repository_root=None):
    """Observe without creating a record, directory or custody lock."""
    train, identity = _binding(work_id, graph_path, repository_root)
    return _view(identity, _record(train, identity))


def _save(train, identity, record):
    value = {'identity': identity, 'record': record}
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > LIMIT:
        raise ValueError('WORK_CONTROL_RECORD_TOO_LARGE')
    save_json(regular(train/FILE), value)
    return _view(identity, record)


def command(work_id, operation, expected, note='', *, graph_path=None, repository_root=None):
    if operation not in {'pause', 'resume', 'intervene'}:
        raise ValueError('WORK_CONTROL_COMMAND_INVALID')
    if operation == 'intervene':
        _note(note)
    elif note != '':
        raise ValueError('WORK_CONTROL_UNEXPECTED_NOTE')
    train, identity = _binding(work_id, graph_path, repository_root)
    lock = regular(train/'work-control-custody')
    lock.mkdir(exist_ok=True)
    with custody_lock(lock):
        current_train, current_identity = _binding(work_id, graph_path, repository_root)
        if current_train != train or current_identity != identity:
            raise ValueError('WORK_CONTROL_IDENTITY_CHANGED')
        record = _record(train, identity)
        request = {'operation': operation, 'expected': expected, 'note': note}
        if record['last_command'] == request:
            return _view(identity, record)
        if expected != _view(identity, record)['token']:
            raise ValueError('WORK_CONTROL_STALE_TOKEN')
        if operation == 'pause':
            if record['status'] != 'RUNNING':
                raise ValueError('WORK_CONTROL_PAUSE_REQUIRES_RUNNING')
            record.update(status='PAUSE_REQUESTED', paused_at=None, resume_at=None,
                          remaining_active_seconds=None, deadline_snapshots=[])
        else:
            if record['status'] != 'PAUSED':
                raise ValueError('WORK_CONTROL_REQUIRES_SAFE_PAUSE')
            if operation == 'resume':
                record.update(status='RESUME_REQUESTED', resume_at=work_run.now())
            else:
                if record['receiver'] is None:
                    raise ValueError('WORK_CONTROL_NO_AGENT_AT_BOUNDARY')
                if record['pending_note'] is not None:
                    raise ValueError('WORK_CONTROL_NOTE_PENDING')
                record['pending_note'] = {'id': sha256_canonical([identity, request]), 'text': note}
        record['last_command'] = request
        return _save(train, identity, record)


def acknowledge_pause(work_id, expected, run_id, descriptor_sha256, *, verify_safe_boundary,
                      remaining_active_seconds, graph_path=None, repository_root=None):
    """Internal host API, NOT a quiescence detector.

    Acquire Work custody, then short control custody. The host callback must
    positively verify no active model/process or uncertain effects and return
    exactly True. Phase B supplies that verifier; absence/uncertainty refuses.
    None budget means the frozen execution was unlimited, not a refreshed limit.
    """
    from .work_change import work_locks
    if (not callable(verify_safe_boundary) or (remaining_active_seconds is not None
            and (type(remaining_active_seconds) not in (int, float)
                 or not math.isfinite(remaining_active_seconds) or remaining_active_seconds < 0))):
        raise ValueError('WORK_CONTROL_UNCERTAIN_BOUNDARY')
    with work_locks([work_id], graph_path=graph_path):
        train, identity = _binding(work_id, graph_path, repository_root)
        lock = regular(train/'work-control-custody'); lock.mkdir(exist_ok=True)
        with custody_lock(lock):
            record = _record(train, identity)
            if (identity['run_id'] != run_id or identity['descriptor_sha256'] != descriptor_sha256
                    or _view(identity, record)['token'] != expected or record['status'] != 'PAUSE_REQUESTED'):
                raise ValueError('WORK_CONTROL_ACK_CHANGED')
            if verify_safe_boundary() is not True:
                raise ValueError('WORK_CONTROL_UNCERTAIN_BOUNDARY')
            if _binding(work_id, graph_path, repository_root) != (train, identity):
                raise ValueError('WORK_CONTROL_IDENTITY_CHANGED')
            record.update(status='PAUSED', paused_at=work_run.now(),
                          remaining_active_seconds=remaining_active_seconds)
            return _save(train, identity, record)


def _host(train, origin):
    selected, identity = _binding(origin['work_id'], origin['graph_path'], origin['repository_root'])
    if (selected.resolve() != Path(train).resolve() or identity['run_id'] != origin['run_id']
            or identity['descriptor_sha256'] != origin['descriptor_sha256']):
        raise ValueError('WORK_CONTROL_IDENTITY_CHANGED')
    return selected, identity


def _files(origin, custody):
    """Only the three existing stage owners can have their deadlines restored."""
    root = regular(custody).resolve()
    implementation = regular(origin['implementation_custody']).resolve()
    plan = regular(origin['plan_custody']).resolve()
    if root.is_relative_to(Path(origin['repository_root']).resolve()):
        raise ValueError('WORK_CONTROL_CUSTODY_IN_PROJECT')
    if root == implementation and not origin.get('prepared_implementation'):
        return [(root/'host.json', 'host'), (root/'attempt.json', 'attempt')]
    from .work_audit import audit_custody
    audit = regular(audit_custody(origin)).resolve()
    numbers = {str(i) for i, route in enumerate((origin.get('audit') or {}).get('routes', [])) if route['eligible']}
    if root == plan or (root.parent == audit and root.name in numbers):
        return [(root/'native-agent.json', 'agent')]
    raise ValueError('WORK_CONTROL_CUSTODY_CHANGED')


def _state(path, kind):
    value = read_json(regular(path), LIMIT)
    if (not isinstance(value, dict) or 'deadline_utc' not in value
            or (kind == 'host' and value.get('schema') != 'tare.tools/native-work-host/1')
            or (kind == 'agent' and value.get('schema') not in {'tare.tools/native-agent/1', 'tare.tools/native-agent/2'})
            or (kind == 'attempt' and (not isinstance(value.get('identity'), dict) or not isinstance(value.get('attempts'), list)))):
        raise ValueError('WORK_CONTROL_CUSTODY_CHANGED')
    deadline = value['deadline_utc']
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('WORK_CONTROL_CUSTODY_CHANGED')
    return value


def _quiescent(custody):
    """Conservatively reject pending invocations, unknown effects and split rounds."""
    from compute_plane.native_journal import NativeJournal
    pending, tools = set(), 0
    for row in NativeJournal(regular(Path(custody)/'journal.jsonl')).snapshot().records:
        event = row.event(); kind = event['kind']
        if kind in {'model_requested', 'context_summary_requested'}:
            pending.add(event['call_id'])
        elif kind in {'model_returned', 'model_failed', 'model_abandoned'}:
            pending.discard(event['call_id'])
            if kind == 'model_returned':
                tools = len(event['reply']['tool_calls'])
        elif kind in {'context_summary_returned', 'context_summary_failed', 'context_summary_interrupted'}:
            # An interrupted context request remains uncertain.
            if kind != 'context_summary_interrupted':
                pending.discard(event['call_id'])
        elif kind == 'effect_requested':
            pending.add(event['effect_id'])
        elif kind == 'effect_returned' and event['result']['status'] != 'unknown':
            pending.discard(event['result']['effect_id']); tools = max(0, tools - 1)
        elif kind == 'invocation_not_started':
            pending.discard(event['invocation_id'])
        elif kind == 'terminal':
            tools = 0
    if pending or tools:
        raise ValueError('WORK_CONTROL_UNCERTAIN_BOUNDARY')


def _snapshots(origin, custody):
    from compute_plane.native_journal import custody_lock
    if custody is None:
        return []
    files = _files(origin, custody)
    with custody_lock(Path(custody)):
        _quiescent(custody)
        stamp = time.time()
        snapshots = []
        for path, kind in files:
            if not path.exists():
                continue
            value = _state(path, kind)
            deadline = value['deadline_utc']
            snapshots.append({'file': path.name, 'kind': kind,
                'sha256': sha256_canonical({k: v for k, v in value.items() if k != 'deadline_utc'}),
                'deadline': deadline, 'remaining': None if deadline is None else max(0, deadline-stamp)})
        return snapshots


def _restore(origin, record):
    snapshots, custody = record['deadline_snapshots'], record['receiver']
    if not snapshots:
        return
    known = {p.name: (p, kind) for p, kind in _files(origin, custody)}
    stamp = datetime.fromisoformat(record['resume_at'].replace('Z', '+00:00')).timestamp()
    with custody_lock(Path(custody)):
        _quiescent(custody)
        writes, seen = [], set()
        for saved in snapshots:
            if (not isinstance(saved, dict) or set(saved) != {'file','kind','sha256','deadline','remaining'}
                    or saved['file'] not in known or saved['file'] in seen):
                raise ValueError('WORK_CONTROL_CUSTODY_CHANGED')
            seen.add(saved['file']); path, kind = known[saved['file']]
            value = _state(path, kind)
            remaining = saved['remaining']
            if ((remaining is None) != (saved['deadline'] is None)
                    or remaining is not None and (type(remaining) not in (int, float)
                        or not math.isfinite(remaining) or remaining < 0)):
                raise ValueError('WORK_CONTROL_CUSTODY_CHANGED')
            restored = None if remaining is None else stamp + remaining
            if (kind != saved['kind'] or
                    sha256_canonical({k: v for k, v in value.items() if k != 'deadline_utc'}) != saved['sha256']
                    or value['deadline_utc'] not in (saved['deadline'], restored)):
                raise ValueError('WORK_CONTROL_CUSTODY_CHANGED')
            writes.append((path, {**value, 'deadline_utc': restored}))
        # Validate the whole set before any write. Each replacement is repeatable
        # after a crash; all use the one operator-recorded resume timestamp.
        for path, value in writes:
            save_json(path, value)


def checkpoint(train, origin, receipt, stage, cancellation, *, custody=None, paused_result=None, consume_resume=False):
    """Called only under Work custody, between stages or after native PAUSED."""
    if cancellation is not None and cancellation.is_cancelled():
        receipt['status'] = 'CANCELLED'; return False
    if not regular(Path(train)/FILE).exists():
        return True
    train, identity = _host(train, origin)
    lock = regular(train/'work-control-custody'); lock.mkdir(exist_ok=True)
    with custody_lock(lock):
        record = _record(train, identity)
        if record['status'] == 'RESUME_REQUESTED' and consume_resume:
            _restore(origin, record)
            record['status'] = 'RUNNING'
            _save(train, identity, record)
        if record['status'] == 'PAUSE_REQUESTED':
            if paused_result is not None and (paused_result.get('status') != 'PAUSED'
                    or paused_result.get('pending_id') or (paused_result.get('outcome') or {}).get('pending_id')):
                raise ValueError('WORK_CONTROL_UNCERTAIN_BOUNDARY')
            snapshots = _snapshots(origin, custody)
            if cancellation is not None and cancellation.is_cancelled():
                receipt['status'] = 'CANCELLED'; return False
            receiver = str(Path(custody).resolve()) if paused_result is not None else None
            if record['pending_note'] is not None and record['receiver'] != receiver:
                raise ValueError('WORK_CONTROL_NOTE_PENDING')
            record.update(status='PAUSED', paused_at=work_run.now(), deadline_snapshots=snapshots,
                remaining_active_seconds=min((s['remaining'] for s in snapshots if s['remaining'] is not None), default=None),
                receiver=receiver)
            # Budget restoration needs the paused custody even at a checkpoint
            # before resuming an already-created native stage.
            if snapshots:
                record['receiver'] = str(Path(custody).resolve())
            _save(train, identity, record)
        if record['status'] != 'RUNNING':
            receipt.update(status='PAUSED' if record['status'] == 'PAUSED' else record['status'],
                           operational_control=_view(identity, record))
            stage('paused', control_status=record['status'])
            return False
    return True


def ports(train, origin, custody):
    """No callback consumes budget. Messages are cleared only after journal ack."""
    def requested():
        if not regular(Path(train)/FILE).exists():
            return False
        selected, identity = _host(train, origin)
        return _record(selected, identity)['status'] in {'PAUSE_REQUESTED', 'PAUSED'}

    def receive(consumed):
        if not regular(Path(train)/FILE).exists():
            return None
        selected, identity = _host(train, origin)
        lock = regular(selected/'work-control-custody'); lock.mkdir(exist_ok=True)
        with custody_lock(lock):
            record = _record(selected, identity)
            note = record['pending_note']
            if not note or record['receiver'] != str(Path(custody).resolve()):
                return None
            if note['id'] in consumed:
                record['pending_note'] = None
                _save(selected, identity, record)
                return None
            if record['status'] != 'RUNNING':
                return None
            from tare_tools_agent_runtime import NativeMessage
            return NativeMessage(note['id'], note['text'])
    return {'pause_requested': requested, 'receive_message': receive}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work_id')
    parser.add_argument('operation', nargs='?', default='inspect', choices=['inspect', 'pause', 'resume', 'intervene'])
    parser.add_argument('--expected'); parser.add_argument('--note', default='')
    parser.add_argument('--graph'); parser.add_argument('--repository-root')
    args = parser.parse_args(argv)
    try:
        options = {'graph_path': args.graph, 'repository_root': args.repository_root}
        value = inspect(args.work_id, **options) if args.operation == 'inspect' else command(
            args.work_id, args.operation, args.expected, args.note, **options)
        print(json.dumps(value, ensure_ascii=False)); return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)})); return 2

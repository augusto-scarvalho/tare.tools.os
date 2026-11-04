"""Explicit operator check runs; Kernel executes, saved model history stays intact."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from compute_plane.native_journal import regular, save_json
from compute_plane.native_review import digest
from .native_review_actions import review_custody, undo_observation, undo_path

MAX_RUNS = 512
SCOPE = 'Freshness compares observed files only, not every test input. A zero exit code is not independent acceptance.'


def _id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid check run identity.')
    return value


def _runs(root):
    directory = regular(root/'review'/'check-runs')
    if not directory.exists():
        return []
    paths = list(directory.iterdir())
    if len(paths) > MAX_RUNS:
        raise ValueError('Check history exceeds the review limit.')
    from .native_agent import read_json
    rows = []
    for path in paths:
        _id(path.name)
        row = read_json(regular(path/'receipt.json'), 128*1024)
        if (row.get('schema') != 'tare.tools/review-check/1' or row.get('run_id') != path.name
                or row.get('status') not in {'RUNNING','UNKNOWN','PASS','FAIL','CANCELLED','TIMEOUT','OUTPUT_LIMIT'}
                or type(row.get('journal_sequence')) is not int or row['journal_sequence'] < 0
                or not isinstance(row.get('started_at'), (int,float))
                or not all(isinstance(row.get(k),str) for k in ('session_id','check_id','label','repository','scope_sha256'))):
            raise ValueError('Check receipt identity mismatch; inspect saved results.')
        if row['status'] not in {'RUNNING','UNKNOWN'}:
            process = row.get('process',{})
            if (process.get('cleanup_verified') is not True or row.get('cleanup_verified') is not True
                    or row.get('returncode') != process.get('exit_code')
                    or row['status']=='PASS' and (process.get('exit_code') != 0 or any(
                        process.get(k) for k in ('cancelled','timeout','output_limit')))):
                raise ValueError('Check completion is inconsistent with its process receipt.')
        rows.append(row)
    return sorted(rows, key=lambda r: (r['started_at'], r['run_id']))


def ensure_checks_quiescent(root):
    """An orphaned intent or unverified cleanup cannot authorize another effect."""
    for row in _runs(Path(root)):
        if row.get('status') in {'RUNNING', 'UNKNOWN'} or row.get('cleanup_verified') is not True:
            raise ValueError('A previous check has an unconfirmed outcome. Inspect its process and receipt before continuing.')


def check_boundaries(root):
    try:
        return [r['journal_sequence'] for r in _runs(Path(root))]
    except (ValueError, OSError, KeyError, TypeError):
        return [None]


def _scope(root, state, config, records, project):
    from compute_plane.mediated_tools import MediatedTools
    roots = config['repositories']
    selected = {k for k, p in roots.items() if Path(p).resolve() == Path(project).resolve()}
    if len(selected) != 1:
        raise ValueError('This conversation belongs to another project.')
    pending, unfinished, names, undos = {}, set(), set(), {}
    for record in records:
        event = record.event()
        if event['kind'] == 'effect_requested':
            pending[event['effect_id']] = event
            unfinished.add(event['effect_id'])
        elif event['kind'] == 'effect_returned':
            result = event['result']
            if result['status'] != 'unknown':
                unfinished.discard(result['effect_id'])
            request = pending.get(result['effect_id'])
            if request and request['tool_call']['name'] in {'read', 'edit'}:
                args = json.loads(request['tool_call']['arguments_json'])
                if args['repository'] in selected:
                    names.add((args['repository'], args['path']))
    if unfinished:
        raise ValueError('Reconcile unresolved conversation operations before running checks.')
    if len(names) > 256:
        raise ValueError('Observed file scope exceeds the 256-file review limit.')
    tools = MediatedTools(config)
    files, budget = {}, 16*1024*1024
    for repository, name in sorted(names):
        path = regular(tools.path(repository, name))
        if path.exists():
            with path.open('rb') as stream:
                raw = stream.read(budget+1)
            budget -= len(raw)
            if budget < 0:
                raise ValueError('Observed files exceed the 16 MiB review budget.')
            sha = digest(raw)
        else:
            sha = None
        files[json.dumps([repository,name])] = sha
    for effect in pending:
        status = undo_observation(root, state['session_id'], effect)
        if status in {'UNKNOWN', 'UNAVAILABLE'}:
            raise ValueError('A previous undo is uncertain; inspect it before running checks.')
        if status is not None:
            undos[effect] = digest(regular(undo_path(root,effect)).read_bytes())
    proof = {'files':files, 'undo':undos, 'config':config, 'journal_head':records[-1].sha256}
    return next(iter(selected)), proof


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _catalog(state, config, records, repository, invocation_id):
    start, end = 0, len(records)
    if invocation_id is not None:
        turn = next((r for r in state['invocations'] if r['id'] == invocation_id), None)
        if (turn is None or type(turn.get('journal_start')) is not int or type(turn.get('journal_end')) is not int
                or not 0 <= turn['journal_start'] <= turn['journal_end'] <= len(records)):
            raise ValueError('Select a turn with recorded review boundaries.')
        start, end = turn['journal_start'], turn['journal_end']
    original = set(records[0].event().get('capabilities', []))
    allowed = original & set(config.get('capabilities', original))
    checks = []
    for name, check in config['verifiers'].items():
        if check['repository'] == repository:
            checks.append({'id':'verify:'+name, 'label':name, 'kind':'verifier', 'repository':repository,
                           'argv':check['argv'], 'timeout_seconds':300,
                           'disabled_reason':None if 'verify' in allowed else 'Verification is not permitted in this conversation.'})
    pending, commands, states = {}, [], {}
    for sequence, record in enumerate(records):
        event = record.event()
        if event['kind'] == 'effect_requested':
            pending[event['effect_id']] = event
        elif event['kind'] == 'effect_returned':
            result = event['result']; request = pending.get(result['effect_id'])
            if not request:
                continue
            name = request['tool_call']['name']
            if name not in {'terminal_start','terminal_read','terminal_stop'}:
                continue
            args = json.loads(request['tool_call']['arguments_json'])
            output = json.loads(result['output_json'])
            handle = output.get('handle') or args.get('handle')
            if handle:
                states[handle] = output
            if name == 'terminal_start' and args['repository'] == repository and start <= sequence < end:
                commands.append((request, args, handle, result['status']))
    for number, (request, args, handle, status) in enumerate(commands,1):
        observed = states.get(handle, {})
        timeout = args.get('timeout_seconds',300)
        reason = None
        if config['role'] != 'implementer' or not config.get('terminal') or 'terminal_start' not in allowed:
            reason = 'Commands are not permitted in this conversation.'
        elif args.get('mode','pipes') != 'pipes' or timeout is None:
            reason = 'Interactive or background commands require the Terminal view.'
        elif (status not in {'succeeded','failed'} or observed.get('status') not in {'exited','stopped'}
              or observed.get('cleanup_verified') is not True):
            reason = 'The original command has no confirmed completion and cleanup.'
        checks.append({'id':request['effect_id'], 'label':f'Recorded command {number}', 'kind':'command',
                       'repository':repository, 'argv':args['argv'], 'timeout_seconds':timeout,
                       'disabled_reason':reason})
    for row in checks:
        if (type(row['timeout_seconds']) is not int or not 1 <= row['timeout_seconds'] <= 3600
                or len(row['argv']) > 256 or len(json.dumps(row['argv']).encode()) > 32768):
            row['disabled_reason'] = 'Command exceeds the bounded review execution limits.'
    return checks


def _view(root, state, config, records, project, invocation_id):
    repository, proof = _scope(root,state,config,records,project)
    runs = _runs(root)
    if any(row.get('session_id') != state['session_id'] for row in runs):
        raise ValueError('Check receipt belongs to another conversation.')
    reason = None
    try:
        ensure_checks_quiescent(root)
    except ValueError as exc:
        reason = str(exc)
    if len(runs) >= MAX_RUNS:
        reason = 'Check history is full; preserve and archive it before adding another run.'
    checks = _catalog(state,config,records,repository,invocation_id)
    for row in checks:
        row['disabled_reason'] = reason or row['disabled_reason']
        row['plan'] = None if row['disabled_reason'] else _fingerprint({
            'session':state['session_id'], 'turn':invocation_id, 'scope':proof, 'check':row,
            'runs':[(r['run_id'],r['status']) for r in runs]})
    history = []
    for row in reversed(runs):
        if row['repository'] != repository or invocation_id is not None and row.get('invocation_id') != invocation_id:
            continue
        item = {k:row.get(k) for k in ('run_id','check_id','label','status','started_at','elapsed_ms','returncode','reason')}
        if item['status'] == 'RUNNING':
            item.update(status='UNKNOWN',reason='Interrupted receipt; process completion is unconfirmed.')
        item['freshness'] = ('STALE' if row.get('scope_sha256') != _fingerprint(proof) or row != runs[-1]
                             else 'OBSERVED_FILES_MATCH' if proof['files'] else 'UNKNOWN')
        history.append(item)
    return {'session_id':state['session_id'],'invocation_id':invocation_id,'checks':checks,
            'runs':history[:50],'reason':reason,'scope':SCOPE}, proof


def review_checks(root, *, project, invocation_id=None):
    with review_custody(root) as (root,state,config,records):
        return _view(root,state,config,records,project,invocation_id)[0]


def read_check_run(root, *, run_id, max_bytes=65536):
    from .native_agent import read_json
    _id(run_id)
    if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
        raise ValueError('Invalid output preview budget.')
    directory = regular(Path(root)/'review'/'check-runs'/run_id)
    receipt = read_json(regular(directory/'receipt.json'),128*1024)
    if receipt.get('schema') != 'tare.tools/review-check/1' or receipt.get('run_id') != run_id:
        raise ValueError('Check receipt identity mismatch.')
    remaining, output, truncated = max_bytes, [], False
    for name in ('stdout.txt','stderr.txt'):
        path = regular(directory/name)
        if not path.exists():
            continue
        with path.open('rb') as stream:
            raw = stream.read(remaining+1)
        truncated |= len(raw) > remaining
        if raw:
            output.append(('\n[stderr]\n' if name == 'stderr.txt' else '')+raw[:remaining].decode('utf-8','replace'))
        remaining = max(0,remaining-min(len(raw),remaining))
    return {**receipt,'output':''.join(output),'output_truncated':truncated}


def run_check(root, *, project, check_id, expected_plan, run_id, invocation_id=None, cancelled=lambda:False):
    from .native_agent import workspace_locks
    from compute_plane.managed_process import run_process
    from compute_plane.tool_hooks import ToolCall, ToolDecision, SpecGraphFirstGate
    from compute_plane.workspace import isolated_child_environment
    _id(run_id)
    with review_custody(root) as (root,state,config,records), ExitStack() as stack:
        workspace_locks(stack,config['repositories'])
        view, proof = _view(root,state,config,records,project,invocation_id)
        check = next((row for row in view['checks'] if row['id'] == check_id), None)
        if check is None or not expected_plan or check['plan'] != expected_plan:
            raise ValueError('The check preview changed or is unavailable; refresh before running it.')
        directory = regular(root/'review'/'check-runs'/run_id)
        if directory.exists():
            raise ValueError('This check run was already requested; inspect its result instead of retrying.')
        cwd = Path(config['repositories'][check['repository']])
        gate = SpecGraphFirstGate().evaluate(ToolCall('native','PreToolUse','run_command',
                                                    {'CommandLine':' '.join(check['argv'])},str(cwd)))
        if gate.decision is ToolDecision.DENY:
            raise ValueError(gate.reason)
        env = isolated_child_environment()
        if check['kind'] == 'command':
            from .console_terminal import child_environment
            env = child_environment()
        executable = check['argv'][0]
        if not Path(executable).is_absolute() and any(s and s in executable for s in (os.sep,os.altsep)):
            executable = str(cwd/executable)
        if shutil.which(executable, path=env.get('PATH')) is None:
            raise ValueError('Executable not found: '+check['argv'][0]+'. Fix the command in the conversation before rerunning it.')
        directory.mkdir(parents=True)
        receipt = {'schema':'tare.tools/review-check/1','run_id':run_id,'session_id':state['session_id'],
                   'invocation_id':invocation_id,'check_id':check_id,'label':check['label'],
                   'repository':check['repository'],'argv':check['argv'],'timeout_seconds':check['timeout_seconds'],
                   'status':'RUNNING','cleanup_verified':False,'started_at':time.time(),'elapsed_ms':0,
                   'returncode':None,'journal_sequence':len(records),'scope_sha256':_fingerprint(proof),
                   'scope':SCOPE}
        save_json(directory/'receipt.json',receipt)
        try:
            process = run_process(check['argv'],cwd=cwd,dest=directory,timeout=check['timeout_seconds'],
                                  env=env,cancelled=cancelled,max_output=1024*1024)
            status = ('UNKNOWN' if not process['cleanup_verified'] else
                      'CANCELLED' if process['cancelled'] else 'TIMEOUT' if process['timeout'] else
                      'OUTPUT_LIMIT' if process['output_limit'] else 'PASS' if process['exit_code']==0 else 'FAIL')
            receipt.update(status=status,cleanup_verified=process['cleanup_verified'],process=process,
                           elapsed_ms=process['wall_ms'],returncode=process['exit_code'])
        except Exception as exc:
            receipt.update(status='UNKNOWN',reason='Execution outcome is unconfirmed: '+type(exc).__name__)
        try:
            save_json(directory/'receipt.json',receipt)
        except (ValueError,OSError):
            return {**receipt,'status':'UNKNOWN','reason':'Result could not be saved. Inspect this run before continuing.'}
        return receipt

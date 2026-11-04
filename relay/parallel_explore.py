"""Independent read-only questions composed over native sessions."""
from . import execution_resources
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from compute_plane.native_journal import custody_lock, save_json
from . import native_agent, native_artifacts, native_limits


def batch_root():
    from .native_installation import storage_root
    return storage_root()/'exploration-batches'


def create(directory, root, questions, deployment, *, workers=1, unlimited=False, limits=None):
    directory, root = Path(directory).resolve(), Path(root).resolve()
    limits = dict(native_limits.validate({} if limits is None else limits))
    if type(workers) is not int or workers < 1:
        raise ValueError('Workers must be a positive integer; use 1 for serial serving.')
    if (not isinstance(questions, list) or not questions or
            any(not isinstance(q, str) or not q.strip() or len(q.encode()) > 16000 for q in questions)):
        raise ValueError('Supply independent questions, one per item.')
    config = native_agent.configuration({'schema': 'tare.tools/native-agent-config/1',
        'role': 'explorer', 'repositories': {'project': str(root)}, 'writable': {}, 'verifiers': {},
        'capabilities': ['read', 'list', 'search'], 'typed_output': True,
        'context_policy': execution_resources.context({'max_compactions': None} if execution_resources.selected('compactions', unlimited) else {})})
    if directory.is_relative_to(root):
        raise ValueError('Batch custody must be outside the project.')
    state = {'schema': 'tare.tools/parallel-exploration/2' if limits else 'tare.tools/parallel-exploration/1', 'directory': str(directory),
        'root': str(root), 'workers': workers, 'config': config, 'deployment': deployment,
        'limits': (({k: None for k in native_limits.FIELDS} | {'max_model_retries': 0}) if unlimited is True else execution_resources.limits({}, unlimited)) | limits,
        'status': 'READY', 'items': [{'id': str(i+1), 'question': q, 'status': 'PENDING',
            'attempts': []} for i, q in enumerate(questions)]}
    directory.mkdir(parents=True, exist_ok=True)
    with custody_lock(directory):
        if (directory/'batch.json').exists():
            raise ValueError('Batch already exists; resume it.')
        save_json(directory/'batch.json', state)
    return inspect(directory)


def read(directory):
    directory = Path(directory).resolve()
    state = native_agent.read_json(directory/'batch.json', 4*1024*1024)
    if state['schema'] not in {'tare.tools/parallel-exploration/1', 'tare.tools/parallel-exploration/2'} or state['directory'] != str(directory):
        raise ValueError('Batch identity changed.')
    if state['schema'] == 'tare.tools/parallel-exploration/2' and not state.get('limits'):
        raise ValueError('Saved exploration execution limits are missing.')
    native_limits.validate(state['limits'])
    if directory.is_relative_to(Path(state['root']).resolve()):
        raise ValueError('Batch custody must be outside the project.')
    config = native_agent.configuration(state['config'])
    if (config['role'] != 'explorer' or config['writable'] or config['verifiers']
            or config.get('capabilities') != ['read', 'list', 'search'] or not config.get('typed_output')
            or config['repositories'] != {'project': state['root']}
            or type(state['workers']) is not int or state['workers'] < 1
            or not state['items'] or [r['id'] for r in state['items']] != [str(i+1) for i in range(len(state['items']))]):
        raise ValueError('Saved batch must contain independent read-only sessions.')
    return state


def evidence(state, row):
    artifact = native_artifacts.read_input(Path(state['directory'])/row['id']/'artifact.json')['artifact']
    if artifact['role'] != 'explorer':
        raise ValueError('Unexpected result role.')
    payload = artifact['payload']
    if not payload['sources']:
        raise ValueError('No observed sources; the question still needs evidence.')
    root = Path(state['root']).resolve()
    for source in payload['sources']:
        file = (root/source['path']).resolve()
        if (source['repository'] != 'project' or not file.is_relative_to(root)
                or hashlib.sha256(file.read_bytes()).hexdigest() != source['sha256']):
            raise ValueError('A cited source changed; review before reusing this result.')
    return payload


def admission_measurement(state):
    # Bounded last-execution summary, not a history or a provider usage estimate.
    return state.get('admission') or {
        'schema': 'tare.tools/exploration-admission/1',
        'scope': 'Latest execution: admitted native sessions in this batch; not provider serving capacity.',
        'semantics': ('Monotonic time from eligibility at execution start to dispatch; '
            'no time between executions. Queue samples exclude undispatched items. '
            'Pending means eligible but undispatched; active means dispatched without an observed outcome. '
            'Resume dispatches are a subset of dispatches. Complete means the execution returned, not all items succeeded.'),
        'coverage': 'UNKNOWN', 'complete': None, 'eligible_items': None,
        'dispatches': None, 'resume_dispatches': None, 'reused_results': None,
        'peak_admitted_sessions': None, 'queue_delay_ms': None,
        'elapsed_ms': None, 'pending_items': None, 'active_sessions': None,
        'outcomes': None, 'cancellation_requested': None,
        'physical_model_attempts': None,
    }


def inspect(directory):
    state = read(directory)
    rows = []
    for row in state['items']:
        row = dict(row)
        row['session'] = str(Path(state['directory'])/row['id'])
        if row['status'] == 'PROPOSED':
            try:
                row['report'] = evidence(state, row)
            except (OSError, ValueError, KeyError) as exc:
                row.update(status='NEEDS_REVIEW', error=str(exc))
        rows.append(row)
    return {'batch': state['directory'], 'root': state['root'], 'workers': state['workers'],
        'status': 'NEEDS_REVIEW' if any(r['status'] == 'NEEDS_REVIEW' for r in rows) else state['status'],
        'completed': sum(r['status'] == 'PROPOSED' for r in rows), 'total': len(rows), 'items': rows,
        'authority': 'NONE', 'serving_capacity': 'OPERATOR_SELECTED_NOT_MEASURED',
        'admission': admission_measurement(state)}


def execute(directory, *, cancellation=None, invocation_id=None):
    from .flow_invocation import record
    return record(directory, invocation_id, 'parallel', 'batch.json',
                  lambda: _execute(directory, cancellation=cancellation))


def _execute(directory, *, cancellation=None):
    from tare_tools_agent_runtime import CancellationToken
    directory = Path(directory).resolve()
    cancellation = cancellation or CancellationToken()
    with custody_lock(directory):
        state = read(directory)
        if inspect(directory)['status'] == 'NEEDS_REVIEW':
            return inspect(directory)
        eligible = [r for r in state['items'] if r['status'] != 'PROPOSED']
        pending = iter(eligible)
        started = time.monotonic()
        measurement = admission_measurement({})
        measurement.update(coverage='OBSERVED_THIS_EXECUTION', complete=False,
            eligible_items=len(eligible), dispatches=0, resume_dispatches=0,
            reused_results=len(state['items'])-len(eligible), peak_admitted_sessions=0,
            queue_delay_ms={'count': 0, 'total': None, 'max': None},
            elapsed_ms=0, pending_items=len(eligible), active_sessions=0,
            outcomes={}, cancellation_requested=False)
        state['admission'] = measurement
        def save():
            measurement['elapsed_ms'] = (time.monotonic()-started)*1000
            measurement['cancellation_requested'] = cancellation.is_cancelled()
            save_json(directory/'batch.json', state)
        def dispatch(row):
            session = directory/row['id']
            resume = (session/'native-agent.json').exists()
            delay = (time.monotonic()-started)*1000
            queue = measurement['queue_delay_ms']
            queue['count'] += 1
            queue['total'] = (queue['total'] or 0) + delay
            queue['max'] = max(queue['max'] or 0, delay)
            measurement['dispatches'] += 1
            measurement['resume_dispatches'] += int(resume)
            measurement['pending_items'] -= 1
            measurement['active_sessions'] += 1
            measurement['peak_admitted_sessions'] = max(
                measurement['peak_admitted_sessions'], measurement['active_sessions'])
            row['status'] = 'RUNNING'
            row['attempts'].append({'started_at': time.time(), 'resume': resume})
            save()  # Recovery uses the native journal if the caller loses the result.
            from contextvars import copy_context
            return pool.submit(copy_context().run, native_agent.run_session, state['config'], state['deployment'], session,
                row['question'], resume=resume, limits=state['limits'], cancellation=cancellation)
        state['status'] = 'RUNNING'; save()
        active = {}
        with ThreadPoolExecutor(max_workers=min(state['workers'], len(state['items']))) as pool:
            while True:
                while len(active) < state['workers'] and not cancellation.is_cancelled():
                    row = next(pending, None)
                    if row is None:
                        break
                    active[dispatch(row)] = row
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    row = active.pop(future)
                    try:
                        result = future.result()
                        row.update(status=result['status'], result=result)
                        row.pop('error', None)
                        if row['status'] == 'PROPOSED':
                            evidence(state, row)
                    except Exception as exc:
                        row.update(status='FAILED', error=type(exc).__name__+': '+str(exc))
                    row['attempts'][-1].update(ended_at=time.time(), status=row['status'])
                    measurement['active_sessions'] -= 1
                    outcomes = measurement['outcomes']
                    outcomes[row['status']] = outcomes.get(row['status'], 0) + 1
                    save()
        state['status'] = ('COMPLETED' if all(r['status'] == 'PROPOSED' for r in state['items'])
                           else 'CANCELLED' if cancellation.is_cancelled() else 'PAUSED')
        measurement['complete'] = True
        save()
        return inspect(directory)


def render(view):
    from .native_display import render_failure
    if 'batch' not in view:
        return str(view)
    lines = [f"Exploration: {view['batch']}",
        f"{view['status']} · {view['completed']}/{view['total']} · workers={view['workers']}"]
    measurement = view.get('admission') or admission_measurement({})
    lines += ['Admission v1: '+measurement['scope'], measurement['semantics']]
    if measurement['coverage'] == 'UNKNOWN':
        lines += ['Admission measurement: unknown (missing/legacy execution data).']
    else:
        queue = measurement['queue_delay_ms']
        delay = ('unknown' if queue['max'] is None else f"{queue['max']:.3f} ms")
        lines += [f"Latest execution · complete={measurement['complete']} · "
            f"dispatches={measurement['dispatches']} · resume dispatches={measurement['resume_dispatches']} · "
            f"reused results={measurement['reused_results']} · peak admitted={measurement['peak_admitted_sessions']}",
            f"Eligible-item queue delay max={delay} · samples={queue['count']} · "
            f"pending={measurement['pending_items']} · active={measurement['active_sessions']} · "
            f"cancellation requested={measurement['cancellation_requested']} · outcomes={measurement['outcomes']}",
            f"Within-execution elapsed={measurement['elapsed_ms']:.3f} ms"]
    lines += ['Dispatch/resume is not a model retry; result reuse is not another call. Physical model attempts: unknown.']
    for row in view['items']:
        lines += [row['id']+' · '+row['status']+' · '+row['question'], 'Session: '+row['session']]
        if row.get('report'):
            lines += [json.dumps(row['report'], ensure_ascii=False)]
        lines.extend(render_failure(row.get('result', {})))
        if row.get('error'):
            lines += [row['error']]
    return '\n'.join(lines+['Read-only claims, not independent approval. Worker count does not measure server capacity.'])


@execution_resources.cli
def main(argv=None):
    # Redirected Windows stdout otherwise uses a legacy codepage and can fail
    # after a successful delivery merely because a source/model emits Unicode.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Explore independent questions using saved native sessions.')
    parser.add_argument('--batch', type=Path); parser.add_argument('--root', type=Path)
    parser.add_argument('--invocation-id', help='Optional caller identity for this execution')
    parser.add_argument('--question', action='append'); parser.add_argument('--workers', type=int)
    parser.add_argument('--deployment'); parser.add_argument('--native-deployment', type=Path)
    execution_resources.add_argument(parser)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--max-seconds', type=float)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--resume', action='store_true'); action.add_argument('--show', action='store_true')
    parser.add_argument('--format', choices=['json', 'text'], default='text')
    args = parser.parse_args(argv)
    try:
        limits = native_limits.validate({k: getattr(args, k) for k in ('max_model_calls', 'max_seconds')
                                         if getattr(args, k) is not None})
        if limits and (args.resume or args.show):
            raise ValueError('Saved exploration limits are frozen; overrides require a new batch.')
        if args.show and args.invocation_id is not None:
            raise ValueError('Invocation identity applies to execution, not show.')
        if args.resume or args.show:
            if not args.batch or any((args.root, args.question, args.workers is not None,
                    args.deployment, args.native_deployment, args.unlimited)):
                raise ValueError('Resume/show takes only the saved batch path.')
            directory = args.batch
        else:
            from .native_catalog import select
            directory = args.batch or batch_root()/uuid4().hex
            create(directory, args.root or Path.cwd(), args.question,
                select(args.deployment, args.native_deployment, role='explorer'),
                workers=args.workers if args.workers is not None else 1, unlimited=args.unlimited, limits=limits)
        from . import native_signals
        from tare_tools_agent_runtime import CancellationToken
        cancel = CancellationToken(); previous = native_signals.install(cancel)
        try:
            result = inspect(directory) if args.show else execute(directory, cancellation=cancel, invocation_id=args.invocation_id)
        finally:
            native_signals.restore(previous)
        print(json.dumps(result, ensure_ascii=False) if args.format == 'json' else render(result))
        return 0 if result['status'] in {'COMPLETED', 'READY', 'CANCELLED'} else 1
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'error': str(exc)})); return 2

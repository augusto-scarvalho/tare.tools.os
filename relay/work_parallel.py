"""Two isolated preparations; existing Work execution owns serial incorporation."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from pathlib import Path, PurePosixPath
import time

from graph_backlog.algorithms import readiness
from graph_backlog.jsonutil import sha256_canonical
from . import work_run


def paths(node):
    return node['repository_scopes'][0].get('target_paths') or node.get('target_paths') or []


def overlap(left, right):
    # Case-folding is conservative on Linux and necessary for Windows worktrees.
    normal = lambda path: PurePosixPath(path.replace('\\', '/').casefold())
    right = [normal(b) for b in right]
    for a in map(normal, left):
        for b in right:
            if a == b or a in b.parents or b in a.parents:
                return True
    return False


def resources(node):
    value = node.get('metrics', {}).get('shared_resources', [])
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError('Invalid shared_resources for '+node['id'])
    return set(value)


def prepare_ready(state, selected, wg, cancellation, save):
    """Dispatch only explicitly isolated, ready Works; retain every proposal on failure."""
    from . import work_prepared
    from .challenger import local
    config = state['config']
    if selected.get('prepared'):
        artifact = Path(selected['prepared'])/'prepared.json'
        if artifact.exists():
            work_prepared.inspect(selected['prepared'])
            return
    if not config.get('isolated_effects'):
        selected['parallel_note'] = 'Serial: effects outside the workspace have not been ruled out.'
        return
    if local(config['native_deployment']):
        selected['parallel_note'] = 'Serial: local model capacity is one slot.'
        return
    group = [selected]
    first = wg.by_id[selected['work_id']]
    for row in state['items']:
        if config.get('workers', 1) == 1:
            break
        if row is selected or row['status'] in {'SETTLED', 'OBSERVED_DONE'}:
            continue
        node = wg.by_id[row['work_id']]
        if (wg.is_satisfied(node['id']) or not readiness(wg, node, 'operational')['ready']
                or (work_run.train_dir_for(node['id'])/'WORK_RUN.json').exists()):
            continue
        if not paths(first) or not paths(node) or overlap(paths(first), paths(node)):
            row['parallel_note'] = 'Serial: writable paths overlap or are unknown.'
            continue
        if resources(first) & resources(node):
            row['parallel_note'] = 'Serial: a declared resource is shared.'
            continue
        group.append(row)
        break
    # An interrupted preparation must still be reconciled, even without a new peer.
    if len(group) == 1 and not selected.get('prepared'):
        selected.setdefault('parallel_note', 'Serial: no independent ready Work is available.')
        return
    root = Path(config['repository_root'])
    for row in group:
        row.setdefault('prepared', str(Path(state['directory'])/'prepared'/sha256_canonical(
            [row['work_id'], row['identity']])[:24]))
        row['status'] = 'PREPARING'
    save()  # Durable dispatch identity precedes every model or workspace effect.
    started = time.monotonic()
    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(copy_context().run, work_prepared.prepare, row['prepared'],
                    wg.by_id[row['work_id']], root, config['native_deployment'],
                    cancellation=cancellation, unlimited=config['unlimited'],
                    limits=config.get('limits'), **({'findings': config['findings'],
                        'findings_sha256': config['findings_sha256']} if config.get('findings') else {})): row for row in group}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
                row['preparation'] = {key: result.get(key) for key in
                    ('status', 'artifact', 'session', 'session_id', 'base_commit', 'sha256', 'reason')}
                row['status'] = result['status']
                if result['status'] != 'PREPARED':
                    failures.append(row['work_id']+': '+result['status'])
            except (ValueError, OSError, RuntimeError, KeyError) as exc:
                row['status'] = 'PREPARATION_FAILED'
                row['preparation_error'] = type(exc).__name__+': '+str(exc)
                failures.append(row['work_id']+': '+str(exc))
            save()
    state.setdefault('parallel_waves', []).append({'works': [r['work_id'] for r in group],
        'elapsed_ms': round((time.monotonic()-started)*1000), 'ended_at': work_run.now()})
    save()
    if failures and not cancellation.is_cancelled():
        raise ValueError('Preparation paused; proposals retained. '+'; '.join(failures))


def incorporation(state, row, node):
    from . import work_prepared
    if not row.get('prepared'):
        return None
    root = Path(state['config']['repository_root'])
    work_prepared.inspect(row['prepared'])
    # The preparer validates its immutable source; the sequence owns Work selection identity.
    from .work_sequence import identity
    if identity(node) != row['identity']:
        raise ValueError('Work changed after preparation: '+row['work_id'])
    result = work_prepared.check(row['prepared'], root, work_run.git(root, 'rev-parse', 'HEAD'), paths(node))
    row['incorporation'] = result
    if result['status'] == 'CONFLICT':
        row['parallel_note'] = 'Conflict: retained proposal; this Work will be implemented serially on the current base.'
        return None
    if result['status'] != 'READY':
        raise ValueError('Prepared incorporation is unavailable: '+str(result))
    row['parallel_note'] = 'Prepared in isolation; verifying and incorporating on the current base.'
    return row['prepared']

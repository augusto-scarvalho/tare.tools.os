"""Explicit Work revision: pinned criteria, bounded invalidation, preserved receipts."""
import argparse
import copy
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path

from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.mutations import execute_graph_transaction
from graph_backlog.validation import validate_work_graph

from . import relay_mesh, work_prepare, work_run
from .native_agent import read_json


def change_root():
    from .native_installation import storage_root
    return storage_root()/'work-changes'


def affected(wg, work_id):
    seen, pending = {work_id}, [work_id]
    while pending:
        for _, dependent in wg.block_out[pending.pop()]:
            if dependent not in seen:
                seen.add(dependent); pending.append(dependent)
    return sorted(seen)


def finished_failure(work_id, graph_path, root, train):
    """Observe an ended attempt with no candidate or unresolved command effects."""
    execution = work_run.status(work_id, graph_path=graph_path)
    receipt = execution.get('last_run') or {}
    allowed = {'FAILED', 'CANCELLED', 'PLAN_REJECTED', 'PLAN_INVALID_DELIVERY',
               'IMPLEMENTATION_FAILED', 'IMPLEMENTATION_BLOCKED', 'IMPLEMENTATION_CANCELLED',
               'IMPLEMENTATION_INTERRUPTED'}
    if (execution.get('receipt_errors') or execution.get('human_wait')
            or receipt.get('status') not in allowed or not receipt.get('ended_at')
            or receipt.get('train_id') != Path(train).name
            or Path(receipt.get('repository_root') or '').resolve() != Path(root).resolve()
            or receipt.get('candidate_commit') or (Path(train)/'CANDIDATE_CHANGE.json').exists()
            or (Path(train)/'NATIVE_CLAIM.json').exists()):
        raise ValueError('Finish or reconcile the existing execution before revising '+work_id)
    origin_file = Path(train)/'WORK_RUN.json'
    origin = read_json(origin_file) if origin_file.exists() else {}
    custody = origin.get('implementation_custody')
    if custody and (Path(custody)/'attempt.json').exists():
        from compute_plane.native_journal import NativeJournal
        attempt = read_json(Path(custody)/'attempt.json', 4*1024*1024)
        if not attempt.get('attempts') or not attempt['attempts'][-1].get('ended_at'):
            raise ValueError('Reconcile the interrupted implementation before revising '+work_id)
        pending = set()
        for record in NativeJournal(Path(custody)/'journal.jsonl').snapshot().records:
            event = record.event()
            if event['kind'] == 'effect_requested': pending.add(event['effect_id'])
            if event['kind'] == 'effect_returned':
                result = event['result']
                if result['status'] == 'unknown':
                    raise ValueError('Reconcile the uncertain command effect before revising '+work_id)
                pending.discard(result['effect_id'])
        if pending:
            raise ValueError('Reconcile the pending command effect before revising '+work_id)
    return {'run_id': receipt['run_id'], 'status': receipt['status'],
            'receipt_sha256': sha256_canonical(receipt), 'train': str(train),
            'implementation_custody': custody}


def retire_revised_attempts(node, graph_path, root):
    """Release prior ownership when starting its explicitly revised successor.

    Runs under the Work writer lock. Existing train transitions are idempotently
    completed before init-train; artifacts and failed workspaces stay in place.
    """
    for path in node.get('metrics', {}).get('work_change_receipts', []):
        view = read_json(path, 4*1024*1024); change = view['change']
        if (view['sha256'] != sha256_canonical(change) or change['graph_path'] != str(Path(graph_path).resolve())
                or change['root'] != str(Path(root).resolve())):
            raise ValueError('Work revision receipt changed.')
        prior = change.get('prior_executions', {}).get(node['id'])
        if not prior: continue
        train = Path(prior['train'])
        saved = read_json(work_run.receipt_path(prior['run_id']))
        if (sha256_canonical(saved) != prior['receipt_sha256']
                or train.parent != relay_mesh.TRAINS_DIR or saved['train_id'] != train.name
                or (train/'CANDIDATE_CHANGE.json').exists() or (train/'NATIVE_CLAIM.json').exists()):
            raise ValueError('Reconcile the previous execution before starting its revision.')
        state = read_json(train/'STATE.json')
        if state['status'] != 'RETIRED':
            if not relay_mesh.transition_train(train.name, 'RETIRED', 'completed', work_run.AGENT_ID,
                    'operator', 'Revised by '+str(path), expected_revision=state.get('revision', 0)):
                raise ValueError('Previous execution retirement refused.')
        relay_mesh.release_lease(train.name, force=True)


@contextmanager
def work_locks(ids, *, graph_path=None):
    from compute_plane.native_journal import custody_lock
    root = relay_mesh.RELAY_ROOT.parent/'.reconciliation'/'work-runs'/'locks'
    with ExitStack() as stack:
        for work_id in sorted(ids):
            path = work_run.lock_root_for(work_id, graph_path=graph_path)
            path.mkdir(parents=True, exist_ok=True)
            stack.enter_context(custody_lock(path))
        yield


def preview(graph_path, work_id, root, source_ref, criteria, argv, reason, paths=None):
    graph_path = Path(graph_path).resolve()
    if not reason.strip():
        raise ValueError('Explain why this Work needs revision.')
    if paths is not None and (not isinstance(paths, list) or not paths
            or any(not isinstance(p, str) or not p.strip() for p in paths)):
        raise ValueError('Scope must be a nonempty JSON array of paths.')
    graph = read_json(graph_path, 32*1024*1024)
    if validate_work_graph(graph)['status'] != 'PASS':
        raise ValueError('Invalid Work graph.')
    wg = WorkGraph(graph)
    node = wg.by_id.get(work_id)
    if node is None or wg.status_of(node) == 'SUPERSEDED':
        raise ValueError('Select an existing Work that has not been superseded.')
    if len(node.get('repository_scopes', [])) != 1:
        raise ValueError('Work revision requires one repository scope.')
    cone = affected(wg, work_id)
    base_revision = compute_revision_hash(graph)
    change_id = sha256_canonical([base_revision, work_id, str(Path(root).resolve()),
                                 source_ref, criteria, argv, reason, paths])[:24]
    receipt_path = str((change_root()/change_id/'change.json').resolve())
    shadow = copy.deepcopy(graph)
    before, after, prior_executions = [], [], {}
    for item in shadow['nodes']:
        if item['id'] not in cone:
            continue
        train = work_run.train_dir_for(item['id'], graph_path=graph_path)
        state = read_json(train/'STATE.json') if (train/'STATE.json').exists() else {}
        if train.exists() and not state:
            raise ValueError('Reconcile the missing execution state before revising '+item['id'])
        if (train/'CANDIDATE_CHANGE.json').exists() and state.get('status') != 'LANDED':
            raise ValueError('Settle the recorded candidate before revising '+item['id'])
        if state and state.get('status') != 'LANDED':
            prior_executions[item['id']] = finished_failure(item['id'], graph_path, root, train)
        if item['id'] != work_id and wg.status_of(item) == 'NOT_DONE' and not state:
            continue  # No existing completion or execution to invalidate; prerequisites will block it.
        if wg.status_of(item) == 'SUPERSEDED':
            raise ValueError('A dependent is superseded; reconcile its successor before revising '+item['id'])
        before.append(copy.deepcopy(item))
        item['completion'] = {'status': 'NOT_DONE', 'dod_satisfied': False,
            'evidence_grade': None, 'materialization_scope': item['completion'].get('materialization_scope', 'local')}
        item['admission_state'] = work_prepare.PROPOSED
        item['work_status'] = 'PROPOSED'
        item['canonical_revision'] = 'revision-'+change_id
        item['metrics']['execution_revision'] = change_id
        item['metrics']['work_change_receipts'] = [*item['metrics'].get('work_change_receipts', []), receipt_path]
        item['metrics'].pop('verification_checks', None)
        item['notes'] = [*item.get('notes', []), 'Requires new verification after revision of '+work_id+': '+reason]
    selected = next(n for n in shadow['nodes'] if n['id'] == work_id)
    if paths is not None:
        selected['repository_scopes'][0]['target_paths'] = paths
    shadow['revision'] = compute_revision_hash(shadow)
    preparation = work_prepare.prepare(graph_path, work_id, root, source_ref, criteria, argv,
                                       graph_value=shadow)['preparation']
    replacement = preparation['after']
    if (node['exit_criteria'] == replacement['exit_criteria']
            and node['repository_scopes'] == replacement['repository_scopes']
            and node.get('spec_ref') == replacement.get('spec_ref')
            and node.get('source_refs') == replacement.get('source_refs')
            and node.get('metrics', {}).get('verification_checks') == replacement['metrics']['verification_checks']):
        raise ValueError('No criteria, source, scope or verifier changed. No revision is needed.')
    shadow['nodes'] = [replacement if n['id'] == work_id else n for n in shadow['nodes']]
    shadow['sources'][preparation['source_key']] = preparation['source_record']
    shadow['revision'] = compute_revision_hash(shadow)
    if validate_work_graph(shadow)['status'] != 'PASS':
        raise ValueError('Revision violates the Work graph contract.')
    changed_ids = {n['id'] for n in before}
    after = [n for n in shadow['nodes'] if n['id'] in changed_ids]
    value = {'schema': 'tare.tools/work-change/1', 'change_id': change_id, 'graph_path': str(graph_path),
        'base_revision': base_revision, 'result_revision': shadow['revision'], 'work_id': work_id,
        'root': str(Path(root).resolve()), 'source_ref': source_ref,
        'criteria': criteria, 'argv': argv, 'reason': reason, 'paths': paths,
        'dependency_cone': cone, 'before': before, 'after': after,
        'source_key': preparation['source_key'], 'source_record': preparation['source_record'],
        'preparation_sha256': sha256_canonical(preparation), 'receipt_path': receipt_path,
        'prior_trains': {n['id']: str(work_run.train_dir_for(n['id'], graph_path=graph_path)) for n in before},
        'prior_executions': prior_executions}
    return {'status': 'PREVIEW', 'sha256': sha256_canonical(value), 'change': value}


def apply(view):
    from compute_plane.native_journal import save_json
    value = view['change']
    if view['sha256'] != sha256_canonical(value):
        raise ValueError('Work revision changed since preview.')
    recorded = Path(value['receipt_path'])
    if recorded != (change_root()/value['change_id']/'change.json').resolve():
        raise ValueError('Work revision custody differs from the selected storage root.')
    with work_locks(value['dependency_cone'], graph_path=value['graph_path']):
        def mutation(wg):
            graph = wg.to_dict()
            revision = compute_revision_hash(graph)
            if revision == value['result_revision']:
                if not recorded.exists() or read_json(recorded, 4*1024*1024) != view:
                    raise ValueError('Previous revision receipt is unavailable.')
                return graph
            if revision != value['base_revision']:
                raise ValueError('Graph changed; preview the Work revision again.')
            fresh = preview(value['graph_path'], value['work_id'], value['root'],
                value['source_ref'], value['criteria'], value['argv'], value['reason'], value['paths'])
            if fresh != view:
                raise ValueError('Source, repository or execution changed; preview the revision again.')
            recorded.parent.mkdir(parents=True, exist_ok=True)
            if recorded.exists() and read_json(recorded, 4*1024*1024) != view:
                raise ValueError('Work revision receipt collision.')
            save_json(recorded, view)  # Preserve previous claims before the graph stops asserting them.
            replacements = {n['id']: n for n in value['after']}
            graph['nodes'] = [replacements.get(n['id'], n) for n in graph['nodes']]
            graph['sources'][value['source_key']] = value['source_record']
            graph['revision'] = compute_revision_hash(graph)
            return graph
        result = execute_graph_transaction(value['graph_path'], mutation)
    return {'status': 'REVISED', 'work_id': value['work_id'], 'revision': result['revision'],
        'invalidated': [n['id'] for n in value['before']], 'receipt': str(recorded),
        'note': 'Previous evidence is historical. Start creates a new execution; dependent Works require preparation.'}


def render(view):
    value = view['change']; old = next(n for n in value['before'] if n['id'] == value['work_id'])
    new = next(n for n in value['after'] if n['id'] == value['work_id'])
    return '\n'.join(['Revise '+value['work_id'], 'Reason: '+value['reason'],
        'Previous criteria:', *old['exit_criteria'], 'New pinned criteria:', *new['exit_criteria'],
        'New scope: '+json.dumps(new['repository_scopes'], ensure_ascii=False),
        'Verification argv: '+json.dumps(value['argv'], ensure_ascii=False),
        'Require new verification: '+', '.join(n['id'] for n in value['before']),
        'Other Works retain their current records. Dependency impact is conservative, not proof of a defect.',
        'Previous evidence retained at: '+value['receipt_path'], 'Preview SHA-256: '+view['sha256']])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview an explicit Work revision and dependent verification invalidation.')
    parser.add_argument('work_id'); parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source', required=True); parser.add_argument('--criteria', required=True)
    parser.add_argument('--argv-json', required=True); parser.add_argument('--reason', required=True)
    parser.add_argument('--path', action='append'); parser.add_argument('--apply')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    args = parser.parse_args(argv)
    try:
        view = preview(relay_mesh.WORK_GRAPH_FILE, args.work_id, args.root, args.source,
            [s.strip() for s in args.criteria.split(',')], json.loads(args.argv_json), args.reason, args.path)
        if args.apply:
            if args.apply != view['sha256']: raise ValueError('Revision differs from the reviewed digest.')
            print(json.dumps(apply(view), ensure_ascii=False))
        else:
            print(json.dumps(view, ensure_ascii=False) if args.format == 'json' else render(view))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)})); return 2

"""Durable serial Work DAG dispatch over the existing execution and recovery owners."""
from . import execution_resources
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from uuid import uuid4

from graph_backlog.algorithms import readiness
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import sha256_canonical
from graph_backlog.validation import validate_work_graph
from . import relay_mesh, work_run
from .native_agent import read_json
from .workflow_ir import compile_workflow, validate_workflow
from .workflow_reducer import reduce_workflow

FILE = 'sequence.json'
SCHEMAS = ('tare.tools/work-sequence/1', 'tare.tools/work-sequence/2', 'tare.tools/work-sequence/3')


class RevisionRefused(ValueError):
    """The revision was rejected before changing sequence custody."""


def sequence_root():
    from .native_installation import storage_root
    return storage_root()/'sequences'


def graph():
    data = read_json(relay_mesh.WORK_GRAPH_FILE, 32*1024*1024)
    if validate_work_graph(data)['status'] != 'PASS':
        raise ValueError('SEQUENCE_GRAPH_INVALID')
    return WorkGraph(data)


def _has_landing_receipt(node, train_id):
    """Classify owner output only; landing retains responsibility for authority."""
    provenance = node.get('provenance')
    completion = node.get('completion', {})
    if (not isinstance(provenance, list) or not provenance
            or not isinstance(completion, dict) or completion.get('status') != 'DONE'
            or node.get('work_status') != 'done'):
        return False
    reference = provenance[-1]
    evidence = completion.get('dod_evidence')
    if (not isinstance(reference, str) or not reference.startswith('relay-signoff:')
            or not isinstance(evidence, list) or not evidence or evidence[-1] != reference):
        return False
    try:
        receipt = json.loads(reference[len('relay-signoff:'):])
    except ValueError:
        return False
    fields = {'train', 'readyRevision', 'authority', 'capability_sha256', 'artifacts'}
    strict = {'descriptor_sha256', 'candidate_sha256', 'candidate_commit'}
    if not isinstance(receipt, dict) or set(receipt) not in (fields, fields | strict):
        return False
    if (receipt['train'] != train_id
            or receipt['authority'] not in ('HUMAN_OPERATOR', 'SMART_POLICY')
            or type(receipt['readyRevision']) is not int or receipt['readyRevision'] < 0
            or not isinstance(receipt['artifacts'], dict)):
        return False
    if any(not isinstance(name, str) or not name or not isinstance(digest, str)
           or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
           for name, digest in receipt['artifacts'].items()):
        return False
    for field in {'capability_sha256'} | (strict if set(receipt) == fields | strict else set()):
        value = receipt[field]
        lengths = (40, 64) if field == 'candidate_commit' else (64,)
        if (not isinstance(value, str) or len(value) not in lengths
                or any(c not in '0123456789abcdef' for c in value)):
            return False
    return True


def identity(node):
    # Project owner output without changing the node or normalizing historical fields.
    value = {k: v for k, v in node.items() if k not in {'completion', 'work_status'}}
    train_id = work_run.train_id_for(node['id'], node)
    marker = '[RELAY_LANDED] Landed via Train '+train_id+' on '
    if 'notes' in value:
        value['notes'] = [n for n in value['notes'] if not n.startswith(marker)]
    if _has_landing_receipt(node, train_id):
        value['provenance'] = value['provenance'][:-1]
    return sha256_canonical(value)


def dependencies(wg, ids):
    return sha256_canonical({key: sorted(wg.block_in[key], key=sha256_canonical) for key in ids})


def workflow(items, wg, revision=1, *, preserve_order=False):
    selected = {row['work_id'] for row in items}
    return compile_workflow([{'id': row['work_id'], 'operator_ref': 'work.execute/v1',
        'depends_on': sorted({pre for _, pre in wg.block_in[row['work_id']] if pre in selected}
                             | ({items[i-1]['work_id']} if preserve_order and i else set()))}
        for i, row in enumerate(items)], revision=revision)


def _saved_workflow(state):
    plan = state.get('workflow')
    if plan is not None:
        validate_workflow(plan)
        if state.get('workflow_order', 'ready-dag') not in {'ready-dag', 'saved-sequence'}:
            raise ValueError('SEQUENCE_WORKFLOW_ORDER_INVALID')
        if sha256_canonical(plan) != state.get('workflow_sha256'):
            raise ValueError('SEQUENCE_WORKFLOW_CHANGED')
        if [node['id'] for node in plan['nodes']] != [row['work_id'] for row in state['items']]:
            raise ValueError('SEQUENCE_WORKFLOW_MEMBERSHIP_CHANGED')
    elif 'workflow_sha256' in state:
        raise ValueError('SEQUENCE_WORKFLOW_MISSING')
    return plan


def preview(ids, root):
    """Freeze the selected graph inputs without creating custody or calling a model."""
    return _preview(ids, root, graph())


def _preview(ids, root, wg):
    root = Path(root).resolve()
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('Select distinct Work IDs.')
    if not (root/'.git').exists():
        raise ValueError('SEQUENCE_PROJECT_UNAVAILABLE')
    if any(key not in wg.by_id for key in ids):
        raise ValueError('SEQUENCE_WORK_NOT_FOUND')
    repositories = set()
    for key in ids:
        node = wg.by_id[key]
        if wg.status_of(node) == 'SUPERSEDED':
            raise ValueError('Select the current successor of '+key)
        if not wg.is_satisfied(key):
            work_run.load_node(relay_mesh.WORK_GRAPH_FILE, key)
            if not node.get('metrics', {}).get('verification_checks'):
                raise ValueError('Prepare verifiers before sequencing '+key)
        scopes = node.get('repository_scopes', [])
        if len(scopes) != 1:
            raise ValueError('SEQUENCE_REQUIRES_ONE_PROJECT')
        repositories.add(scopes[0]['repository'])
    if len(repositories) != 1:
        raise ValueError('SEQUENCE_REQUIRES_ONE_PROJECT')
    order, pending = [], list(ids)
    while pending:
        ready = next((key for key in pending if not any(pre in pending for _, pre in wg.block_in[key])), None)
        if ready is None:
            raise ValueError('SEQUENCE_DEPENDENCY_CYCLE')
        order.append(ready); pending.remove(ready)
    rows = [{'work_id': key, 'identity': identity(wg.by_id[key]),
             'train_id': work_run.train_id_for(key, wg.by_id[key]),
             'status': 'OBSERVED_DONE' if wg.is_satisfied(key) else 'PENDING',
             'completed_node_sha256': sha256_canonical(wg.by_id[key]) if wg.is_satisfied(key) else None,
             'attempts': []} for key in order]
    value = {'root': str(root), 'graph_path': str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()),
             'requested_order': list(ids), 'dependency_sha256': dependencies(wg, ids), 'items': rows,
             'work_dependencies': {key: dependencies(wg, [key]) for key in ids},
             'workflow': workflow(rows, wg)}
    return {**value, 'sha256': sha256_canonical(value)}


def create(directory, ids, root, deployment, *, unlimited=False, wait_before_implement='', expected_selection=None,
           workers=1, isolated_effects=False, limits=None, findings=None, findings_sha256=None):
    from . import native_limits
    from compute_plane.native_journal import custody_lock, save_json
    # Explicit overrides are per native stage, not a shared sequence quota.
    limits = dict(native_limits.validate({} if limits is None else limits))
    directory, root = Path(directory).resolve(), Path(root).resolve()
    if bool(findings) != bool(findings_sha256):
        raise ValueError('Provide reviewed findings and their SHA-256 together.')
    selected_findings = {}
    if findings is not None:
        from .work_context_reuse import prepare
        prepare(findings, findings_sha256, root)
        selected_findings = {'findings': str(Path(findings).resolve()), 'findings_sha256': findings_sha256}
    if type(workers) is not int or workers not in (1, 2) or type(isolated_effects) is not bool:
        raise ValueError('Choose one or two implementation slots.')
    if workers == 2 and wait_before_implement:
        raise ValueError('Use one slot when each implementation needs a human response.')
    if directory.is_relative_to(root):
        raise ValueError('SEQUENCE_CUSTODY_MUST_BE_OUTSIDE_PROJECT')
    view = preview(ids, root)
    if expected_selection is not None and view['sha256'] != expected_selection:
        raise ValueError('SEQUENCE_SELECTION_CHANGED_REVIEW_AGAIN')
    rows = view['items']
    state = {'schema': SCHEMAS[2] if selected_findings else SCHEMAS[1] if limits else SCHEMAS[0], 'directory': str(directory),
        'graph_path': str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()), 'created_at': work_run.now(),
        'requested_order': ids, 'dependency_sha256': view['dependency_sha256'],
        'config': {'repository_root': str(root), 'native_deployment': deployment,
                   'unlimited': unlimited, 'wait_before_implement': wait_before_implement,
                   'workers': workers, 'isolated_effects': isolated_effects, 'limits': limits, **selected_findings},
        'status': 'READY', 'reason': None, 'active_work': None, 'items': rows}
    state['work_dependencies'] = view['work_dependencies']
    state.update(workflow=view['workflow'], workflow_sha256=sha256_canonical(view['workflow']),
                 workflow_order='ready-dag')
    directory.mkdir(parents=True, exist_ok=True)
    with custody_lock(directory):
        if (directory/FILE).exists():
            raise ValueError('SEQUENCE_EXISTS_USE_RESUME')
        save_json(directory/FILE, state)
    return inspect(directory)


def _selection(state, work_ids):
    if work_ids is None:
        return list(state['requested_order'])
    if (not isinstance(work_ids, (list, tuple)) or not work_ids
            or any(not isinstance(k, str) or not k for k in work_ids) or len(set(work_ids)) != len(work_ids)):
        raise ValueError('Select distinct Work IDs for the revised sequence.')
    return list(work_ids)


def _untouched(key, train_ids):
    """No owner evidence of execution: receipts, train state, human wait, or any saved/current train directory."""
    execution = work_run.status(key)
    return not (execution.get('receipt_errors') or execution.get('train') or execution.get('last_run')
                or execution.get('human_wait') or any((relay_mesh.TRAINS_DIR/t).exists()
                or any(relay_mesh.HISTORY_DIR.glob(t+'_*')) for t in train_ids))


def _contract(wg, key):
    node = wg.by_id[key]
    return {'title': node['title'], 'exit_criteria': node['exit_criteria'],
            'repository_scopes': node['repository_scopes'],
            'verification_checks': node.get('metrics', {}).get('verification_checks', []),
            'prerequisites': [pre for _, pre in wg.block_in[key]]}


def _reviewed_restart(old, node, state):
    """Require the Work owner's explicit revision before retiring a prior attempt."""
    key = old['work_id']
    revision = node.get('metrics', {}).get('execution_revision')
    chain = []
    for path in reversed(node.get('metrics', {}).get('work_change_receipts', [])):
        receipt = read_json(path, 4*1024*1024)
        change = receipt['change']
        if (receipt['sha256'] != sha256_canonical(change) or change['graph_path'] != state['graph_path']
                or change['root'] != state['config']['repository_root']):
            raise ValueError('Work revision receipt does not match this sequence: '+key)
        before = next((n for n in change['before'] if n['id'] == key), None)
        if before is None or revision != change['change_id']:
            continue
        chain.append(path)
        if identity(before) != old['identity']:
            revision = before.get('metrics', {}).get('execution_revision')
            continue
        prior = change.get('prior_executions', {}).get(key)
        if old['status'] not in {'SETTLED', 'OBSERVED_DONE'}:
            if not prior:
                raise ValueError('Revision has no finished execution evidence for '+key)
            saved = read_json(work_run.receipt_path(prior['run_id']))
            if sha256_canonical(saved) != prior['receipt_sha256']:
                raise ValueError('Previous execution evidence changed: '+key)
            if (Path(prior['train'])/'CANDIDATE_CHANGE.json').exists():
                raise ValueError('Reconcile the previous candidate before revising '+key)
        return {'work_id': key, 'previous_status': old['status'], 'previous_identity': old['identity'],
                'revision_receipt': path, 'revision_receipts': chain, 'reason': change['reason']}
    raise ValueError('Previously started or completed Work changed without an applied Work revision: '+key)


def preview_revision(directory, reason, *, work_ids=None):
    """Review current contracts and, optionally, an explicit replacement of requested_order.

    Removing or adding Works never restarts, resets or relabels attempted Work."""
    directory = Path(directory).resolve()
    state = read_json(directory/FILE, 4*1024*1024)
    if (state.get('schema') not in SCHEMAS or state['directory'] != str(directory)
            or state['graph_path'] != str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())):
        raise ValueError('SEQUENCE_CONTROL_ROOT_CHANGED')
    if directory.is_relative_to(Path(state['config']['repository_root']).resolve()):
        raise ValueError('SEQUENCE_CUSTODY_MUST_BE_OUTSIDE_PROJECT')
    if state['status'] not in {'READY', 'NEEDS_REVIEW', 'BLOCKED', 'CANCELLED', 'PAUSED', 'COMPLETED'}:
        raise ValueError('Stop and reconcile the sequence before reviewing changes.')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Explain why the remaining Works need revision.')
    selected, previous_order = _selection(state, work_ids), state['requested_order']
    wg = graph()
    originals = {r['work_id']: r for r in state['items']}
    removed = []
    for key in previous_order:
        if key in selected:
            continue
        # A superseded or edited node still has an owner record; a missing node cannot be proven unstarted.
        row, node = originals[key], wg.by_id.get(key)
        if (node is None or row['attempts'] or row['status'] != 'PENDING' or row['completed_node_sha256']
                or wg.status_of(node) not in {'NOT_DONE', 'SUPERSEDED'}
                or not _untouched(key, {row['train_id'], work_run.train_id_for(key, node)})):
            raise ValueError('SEQUENCE_REMOVAL_UNSAFE: '+key)
        removed.append({'work_id': key, 'identity': row['identity'], 'train_id': row['train_id'],
                        'graph_status': wg.status_of(node)})
    view = _preview(selected, state['config']['repository_root'], wg)
    items, changes, retained, added, rerun = [], [], [], [], []
    work_dependencies = view['work_dependencies']
    for current in view['items']:
        key = current['work_id']; node = wg.by_id[key]
        if key not in originals:
            if wg.status_of(node) != 'NOT_DONE' or not _untouched(key, {current['train_id']}):
                raise ValueError('SEQUENCE_ADDED_WORK_NOT_UNSTARTED: '+key)
            added.append({'work_id': key, 'identity': current['identity'], **_contract(wg, key)})
            items.append(current)
            continue
        old = originals[key]
        protected = bool(old['attempts'] or old['status'] != 'PENDING' or old['completed_node_sha256'])
        deps_changed = (work_dependencies[key] != state['work_dependencies'][key]
                        if 'work_dependencies' in state else view['dependency_sha256'] != state['dependency_sha256'])
        if protected:
            if current['train_id'] != old['train_id'] and current['status'] == 'PENDING':
                proof = _reviewed_restart(old, node, state)
                if not _untouched(key, {current['train_id']}):
                    raise ValueError('Inspect the revised execution before changing the sequence: '+key)
                rerun.append(proof)
                changes.append({**proof, 'identity': current['identity'],
                                'dependencies_changed': deps_changed, **_contract(wg, key)})
                items.append(current)
                continue
            if (current['identity'] != old['identity'] or current['train_id'] != old['train_id'] or deps_changed
                    or (old['completed_node_sha256'] and
                        current['completed_node_sha256'] != old['completed_node_sha256'])):
                raise ValueError('Previously started or completed Work changed: '+key)
            if old['status'] not in {'SETTLED', 'OBSERVED_DONE'}:
                raise ValueError('Reconcile the existing attempt before revising the sequence: '+key)
            items.append(old); retained.append(key)
            continue
        if current['status'] != 'PENDING' or not _untouched(key, {current['train_id'], old['train_id']}):
            raise ValueError('Inspect the existing execution before revising the sequence: '+key)
        if current['identity'] != old['identity'] or deps_changed:
            changes.append({'work_id': key, 'previous_identity': old['identity'],
                            'identity': current['identity'], 'dependencies_changed': deps_changed,
                            **_contract(wg, key)})
        items.append(current)
    if added:
        # Every previous node exists here: retained ones passed _preview, removed ones were checked above.
        before = {s['repository'] for key in previous_order for s in wg.by_id[key].get('repository_scopes', [])}
        if before != {wg.by_id[selected[0]]['repository_scopes'][0]['repository']}:
            raise ValueError('SEQUENCE_REQUIRES_ONE_PROJECT')
    if work_ids is not None:
        for row in items:
            if row['status'] == 'PENDING':
                for edge, pre in wg.block_in[row['work_id']]:
                    if pre not in selected and not wg.prerequisite_satisfies(pre, edge):
                        raise ValueError('SEQUENCE_PREREQUISITE_OUTSIDE_SELECTION: '+row['work_id']+' requires '+pre)
    if not (changes or added or removed or selected != previous_order):
        raise ValueError('No Work changes to review; use Resume for the saved sequence.')
    old_plan = _saved_workflow(state)
    revision = old_plan['revision'] if old_plan else len(state.get('revisions', [])) + 1
    order = state.get('workflow_order', 'ready-dag' if old_plan else 'saved-sequence')
    result = {'sequence': str(directory), 'root': view['root'], 'reason': reason.strip(),
              'previous_sha256': sha256_canonical(state), 'selection_sha256': view['sha256'],
              'requested_order': selected, 'previous_requested_order': previous_order,
              'items': items, 'changes': changes, 'retained': retained, 'added': added, 'removed': removed,
              'rerun': rerun,
              'dependency_sha256': view['dependency_sha256'], 'work_dependencies': work_dependencies,
              'workflow': workflow(items, wg, revision=revision + 1, preserve_order=order == 'saved-sequence'),
              'workflow_order': order}
    return {**result, 'sha256': sha256_canonical(result)}


def revise(directory, expected_revision, reason, *, work_ids=None):
    """Confirm a fresh preview under the same lock used by execution; Resume stays explicit."""
    from compute_plane.native_journal import custody_lock, save_json
    from .work_change import work_locks
    directory = Path(directory).resolve()
    # Same per-Work locks as execution and graph revision prevent an independent
    # dispatch from starting between the unstarted check and confirmation, for
    # both the removed and the added Works.
    with ExitStack() as locks:
        try:
            locks.enter_context(custody_lock(directory))
            state = read_json(directory/FILE, 4*1024*1024)
            locks.enter_context(work_locks(set(state['requested_order']) | set(_selection(state, work_ids))))
            view = preview_revision(directory, reason, work_ids=work_ids)
            if view['sha256'] != expected_revision:
                raise ValueError('SEQUENCE_REVISION_CHANGED_REVIEW_AGAIN')
        except (ValueError, OSError, KeyError) as exc:
            raise RevisionRefused(str(exc)) from exc
        previous = {key: state[key] for key in ('items', 'requested_order', 'dependency_sha256', 'status',
                                                'reason', 'active_work')}
        if 'work_dependencies' in state:
            previous['work_dependencies'] = state['work_dependencies']
        if 'workflow' in state:
            previous.update(workflow=state['workflow'], workflow_sha256=state['workflow_sha256'])
            previous['workflow_order'] = state.get('workflow_order', 'ready-dag')
        state.setdefault('revisions', []).append({'at': work_run.now(), 'reason': view['reason'],
            'preview_sha256': view['sha256'], 'previous_sha256': view['previous_sha256'], 'previous': previous,
            'retained': view['retained'], 'rerun': view['rerun']})
        state.update(items=view['items'], requested_order=view['requested_order'],
                     dependency_sha256=view['dependency_sha256'],
                     work_dependencies=view['work_dependencies'], status='READY', reason=None, active_work=None)
        state.update(workflow=view['workflow'], workflow_sha256=sha256_canonical(view['workflow']),
                     workflow_order=view['workflow_order'])
        state.pop('frontier', None)
        save_json(directory/FILE, state)
    return inspect(directory)


def inspect(directory):
    state = read_json(Path(directory)/FILE, 4*1024*1024)
    if state.get('schema') not in SCHEMAS:
        raise ValueError('SEQUENCE_SCHEMA_INVALID')
    return {'sequence': state['directory'], 'status': state['status'], 'reason': state['reason'],
        'active_work': state['active_work'], 'requested_order': state['requested_order'],
        'workers': state['config'].get('workers', 1),
        'root': state['config']['repository_root'],
        'completed': sum(r['status'] in {'SETTLED', 'OBSERVED_DONE'} for r in state['items']),
        'total': len(state['items']), 'items': state['items'],
        'workflow_revision': state.get('workflow', {}).get('revision'), 'frontier': state.get('frontier'),
        'workflow_order': state.get('workflow_order', 'ready-dag' if state.get('workflow') else 'saved-sequence')}


def execute(directory, *, cancellation=None, invocation_id=None):
    from .flow_invocation import record
    return record(directory, invocation_id, 'sequence', FILE,
                  lambda: _execute(directory, cancellation=cancellation))


def serial(directory):
    """Reduce capacity only after active dispatch has stopped; retain prepared work."""
    from compute_plane.native_journal import custody_lock, save_json
    directory = Path(directory).resolve()
    with custody_lock(directory):
        state = read_json(directory/FILE, 4*1024*1024)
        if state.get('schema') not in SCHEMAS or state['directory'] != str(directory):
            raise ValueError('SEQUENCE_CONTROL_ROOT_CHANGED')
        state['config']['workers'] = 1
        save_json(directory/FILE, state)


def _fence(state, row, run_id):
    return sha256_canonical([state['workflow_sha256'], row['work_id'], row['identity'],
                             row['train_id'], run_id])


def _observation(state, row, wg):
    """Observe existing owners; an unclassified effect never becomes a fresh retry."""
    key = row['work_id']
    if row['status'] in {'SETTLED', 'OBSERVED_DONE'}:
        if not row['completed_node_sha256']:
            raise ValueError('SEQUENCE_COMPLETION_PROOF_MISSING: '+key)
        return {'state': 'SUCCEEDED'}
    attempt = row['attempts'][-1] if row['attempts'] else None
    if attempt and 'fence' in attempt and attempt['fence'] != _fence(state, row, attempt['run_id']):
        return {'state': 'UNKNOWN', 'reason': 'Dispatch identity changed: '+key}
    train = work_run.train_dir_for(key)
    if (train/'WORK_RUN.json').is_file():
        from .human_wait import inspect as inspect_wait
        wait = inspect_wait(train)
        if wait and wait['status'] in {'WAITING_FOR_HUMAN', 'HUMAN_DECLINED'}:
            return {'state': 'WAITING', 'reason': wait['status']}
        if (train/'WORK_CONTROL.json').is_file():
            from .work_control import inspect as inspect_control
            control = inspect_control(key, repository_root=state['config']['repository_root'])
            if control['status'] in {'PAUSED', 'PAUSE_REQUESTED'}:
                return {'state': 'WAITING', 'reason': control['status']}
        # Only Work's resume path can validate the descriptor, reservation and effects.
        return {'state': 'RECOVERABLE'}
    if attempt:
        run_id = attempt.get('run_id')
        # Work creates its unique run directory before any effect. Its absence proves
        # a reserved dispatch was never entered; an existing directory is not that proof.
        if (not run_id or work_run.receipt_path(run_id).parent.exists() or train.exists()
                or 'fence' not in attempt):
            return {'state': 'UNKNOWN', 'reason': 'No recoverable Work origin for prior dispatch: '+key}
    elif train.exists():
        return {'state': 'UNKNOWN', 'reason': 'Existing train has no recoverable Work origin: '+key}
    available = readiness(wg, wg.by_id[key], 'operational')
    selected = set(state['requested_order'])
    external = [p for p in available['unresolved_prerequisites'] if p['id'] not in selected]
    # The reducer owns the selected DAG dependencies; Backlog still gates all
    # other readiness requirements, including prerequisites outside the selection.
    ready = not external and not (set(available['reasons']) - {'unresolved_prerequisites'})
    reasons = [f"Requires {p['id']} ({p['completion']})" for p in external]
    reasons += [reason.replace('_', ' ') for reason in available['reasons']
                if reason != 'unresolved_prerequisites']
    return {'state': 'PENDING', 'ready': ready, 'reason': '; '.join(reasons)}


def _execute(directory, *, cancellation=None):
    from compute_plane.native_journal import custody_lock, save_json
    from tare_tools_agent_runtime import CancellationToken
    directory = Path(directory).resolve()
    cancellation = cancellation or CancellationToken()
    with custody_lock(directory):
        state = read_json(directory/FILE, 4*1024*1024)
        if (state.get('schema') not in SCHEMAS or state['directory'] != str(directory)
                or state['graph_path'] != str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())):
            raise ValueError('SEQUENCE_CONTROL_ROOT_CHANGED')
        if directory.is_relative_to(Path(state['config']['repository_root']).resolve()):
            raise ValueError('SEQUENCE_CUSTODY_MUST_BE_OUTSIDE_PROJECT')
        from . import native_limits
        if state['schema'] in SCHEMAS[1:] and 'limits' not in state['config']:
            raise ValueError('SEQUENCE_EXECUTION_LIMITS_MISSING')
        if state['schema'] == SCHEMAS[2] and not all(state['config'].get(k) for k in ('findings', 'findings_sha256')):
            raise ValueError('SEQUENCE_FINDINGS_MISSING')
        native_limits.validate(state['config'].get('limits', {}))
        def stop(status, reason=None):
            state.update(status=status, reason=reason)
            save_json(directory/FILE, state)
            return inspect(directory)
        try:
            while True:
                wg = graph()
                if dependencies(wg, state['requested_order']) != state['dependency_sha256']:
                    return stop('NEEDS_REVIEW', 'Selected Work dependencies changed.')
                # Revalidate all earlier completion claims, including when resuming a completed sequence.
                for previous in state['items']:
                    node = wg.by_id.get(previous['work_id'])
                    if node is None or identity(node) != previous['identity']:
                        return stop('NEEDS_REVIEW', 'Work changed: '+previous['work_id'])
                    if previous['completed_node_sha256'] and (not wg.is_satisfied(previous['work_id'])
                            or sha256_canonical(node) != previous['completed_node_sha256']):
                        return stop('NEEDS_REVIEW', 'Completion changed: '+previous['work_id'])
                plan = _saved_workflow(state)
                preserve_order = plan is None or state.get('workflow_order') == 'saved-sequence'
                expected = workflow(state['items'], wg, revision=plan['revision'] if plan else
                                    len(state.get('revisions', [])) + 1, preserve_order=preserve_order)
                if plan is not None and plan != expected:
                    return stop('NEEDS_REVIEW', 'SEQUENCE_WORKFLOW_DEPENDENCIES_CHANGED')
                if plan is None:  # Legacy selections gain an IR only after their frozen inputs pass.
                    state.update(workflow=expected, workflow_sha256=sha256_canonical(expected),
                                 workflow_order='saved-sequence')
                    plan = expected
                observations = {r['work_id']: _observation(state, r, wg) for r in state['items']}
                decision = reduce_workflow(plan, observations)
                state['frontier'] = decision
                if decision['status'] == 'COMPLETED':
                    state['active_work'] = None
                    return stop('COMPLETED')
                if cancellation.is_cancelled():
                    return stop('CANCELLED', 'Execution cancelled; prior results retained.')
                key = decision['next_node']
                if key is None:
                    pending = decision['waiting'] or list(decision['blocked'])
                    state['active_work'] = pending[0] if pending else None
                    status = {'WAITING': 'PAUSED', 'BLOCKED': 'BLOCKED',
                              'NEEDS_RECONCILIATION': 'NEEDS_REVIEW'}[decision['status']]
                    return stop(status, {'work_id': state['active_work'], 'frontier': decision})
                row = next(r for r in state['items'] if r['work_id'] == key)
                node = wg.by_id[key]
                state['active_work'] = key
                if work_run.train_id_for(key, node) != row['train_id']:
                    return stop('NEEDS_REVIEW', 'Work execution revision changed: '+key)
                resume = decision['status'] == 'RECOVER'
                if not wg.is_satisfied(key):
                    available = readiness(wg, node, 'operational')
                    if not available['ready']:
                        return stop('BLOCKED', {'work_id': key, 'readiness': available})
                elif not resume:
                    return stop('NEEDS_REVIEW', 'Completion has no recoverable native run: '+key)
                if (state['config'].get('workers', 1) == 2 or row.get('prepared')) and not resume:
                    from .work_parallel import prepare_ready, incorporation
                    prepare_ready(state, row, wg, cancellation, lambda: stop('RUNNING'))
                    if cancellation.is_cancelled():
                        return stop('CANCELLED', 'Execution cancelled; prepared proposals retained.')
                    prepared = incorporation(state, row, node)
                    stop('RUNNING')
                else:
                    prepared = None
                # Persist dispatch before any Work effect. Work recovery reconciles a lost result.
                run_id = uuid4().hex
                row['status'] = 'RUNNING'
                row['attempts'].append({'started_at': work_run.now(), 'resume': resume,
                    'run_id': run_id, 'fence': _fence(state, row, run_id)})
                stop('RUNNING')
                config = state['config']
                options = {'repository_root': Path(config['repository_root']), 'cancellation': cancellation,
                           'expected_work_sha256': sha256_canonical(node), 'run_id': run_id}
                options.update({'resume': True} if resume else {
                    'native_deployment': config['native_deployment'], 'unlimited': config['unlimited'],
                    'wait_before_implement': config['wait_before_implement'],
                    'limits': config.get('limits')})
                if not resume and config.get('findings'):
                    options.update(findings=config['findings'], findings_sha256=config['findings_sha256'])
                if prepared is not None:
                    options['prepared_implementation'] = prepared
                if row.get('prepared') and not resume:
                    options['preparation_source'] = row['prepared']
                result = work_run.run_work(key, **options)
                if (result['run_id'] != run_id or result.get('work_id') != key
                        or Path(result.get('graph_path', '')).resolve() != Path(state['graph_path']).resolve()
                        or Path(result.get('repository_root', '')).resolve() != Path(config['repository_root']).resolve()
                        or (result['status'] == 'SETTLED' and result.get('train_id') != row['train_id'])):
                    return stop('NEEDS_REVIEW', 'SEQUENCE_LATE_OR_REBOUND_RESULT: '+key)
                row['status'] = 'RECONCILING' if result['status'] == 'SETTLED' else result['status']
                row['attempts'][-1].update(ended_at=work_run.now(), run_id=result['run_id'], status=result['status'],
                    receipt=str(work_run.receipt_path(result['run_id'])))
                stop('RUNNING')  # Receipt acknowledgment precedes the sequence transition.
                if result['status'] != 'SETTLED':
                    return stop('CANCELLED' if result['status'] == 'CANCELLED' else 'PAUSED',
                                {'work_id': key, 'status': result['status'], 'error': result.get('error')})
                after = graph(); settled = after.by_id[key]
                if not after.is_satisfied(key) or identity(settled) != row['identity']:
                    return stop('NEEDS_REVIEW', 'Settlement does not match the selected Work: '+key)
                row['completed_node_sha256'] = sha256_canonical(settled)
                row['status'] = 'SETTLED'
                stop('RUNNING')
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            return stop('PAUSED', type(exc).__name__+': '+str(exc))


def render(view):
    if 'sequence' not in view:
        return view.get('status', 'UNAVAILABLE')+': '+str(view.get('error', 'Sequence unavailable.'))
    frontier = view.get('frontier') or {}
    explanation = []
    if frontier.get('status') == 'WAITING':
        explanation.append('Waiting for a response or intervention: '+', '.join(frontier['waiting'])+
                           '. Open the active Work, respond, then Resume.')
    elif frontier.get('status') == 'NEEDS_RECONCILIATION':
        explanation.append('Inspect the active Work before retrying; the prior result is not known.')
    explanation += [key+': '+reason for key, reason in frontier.get('blocked', {}).items()]
    return '\n'.join([f"Sequence: {view['sequence']}",
        f"{view['status']} · {view['completed']}/{view['total']} complete",
        *([f"Workflow revision: {view['workflow_revision']}"] if view.get('workflow_revision') else []),
        *(['Order: saved serial sequence.'] if view.get('workflow_order') == 'saved-sequence' else []),
        f"Implementation slots: {view.get('workers', 1)}; incorporation is serial.",
        *[r['work_id']+' · '+r['status']+f" · {len(r['attempts'])} dispatches" for r in view['items']],
        *[r['work_id']+' · '+r['parallel_note'] for r in view['items'] if r.get('parallel_note')],
        *explanation,
        *([] if explanation else ['Reason: '+json.dumps(view['reason'], ensure_ascii=False)]),
        'OBSERVED_DONE is existing graph evidence, not execution performed by this sequence.'])


@execution_resources.cli
def main(argv=None):
    from . import native_limits
    parser = argparse.ArgumentParser(description='Run or resume an explicit sequence of prepared Works.')
    parser.add_argument('--sequence', type=Path); parser.add_argument('--works', nargs='+')
    parser.add_argument('--expected-selection', help='Require the exact previewed Work selection')
    parser.add_argument('--expected-revision', help='Confirm the SHA-256 from --preview-revision')
    parser.add_argument('--reason', default='', help='Reason for reviewing unstarted Work changes')
    parser.add_argument('--revision-works', nargs='+',
                        help='Proposed Work IDs replacing the saved order (revision only)')
    parser.add_argument('--invocation-id', help='Optional caller identity for this execution')
    parser.add_argument('--root', type=Path); parser.add_argument('--deployment'); parser.add_argument('--native-deployment', type=Path)
    execution_resources.add_argument(parser); parser.add_argument('--wait-before-implement', default='')
    parser.add_argument('--findings', type=Path, help='Reviewed exploration reused by each unstarted Work')
    parser.add_argument('--findings-sha256', help='Reviewed findings manifest identity')
    for flag, kind in (('max-model-calls', int), ('max-stagnant-turns', int), ('max-retries', int),
                       ('max-seconds', float), ('request-seconds', float),
                       ('connect-seconds', float), ('read-seconds', float)):
        parser.add_argument('--'+flag, type=native_limits.cli_limit(kind), default=argparse.SUPPRESS,
                            help='Per-native-stage limit; omitted values inherit the profile/default.')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=None)
    parser.add_argument('--isolated-effects', action='store_true',
                        help='Confirm selected tasks and checks affect only their isolated workspaces')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--resume', action='store_true'); action.add_argument('--show', action='store_true')
    action.add_argument('--preview-revision', action='store_true')
    action.add_argument('--revise', action='store_true')
    parser.add_argument('--format', choices=['json', 'text'], default='text')
    args = parser.parse_args(argv)
    try:
        limits = native_limits.validate({name: getattr(args, 'max_retries' if name == 'max_model_retries' else name)
            for name in native_limits.FIELDS
            if hasattr(args, 'max_retries' if name == 'max_model_retries' else name)})
        if (args.findings is not None or args.findings_sha256 is not None) and (args.resume or args.show or args.preview_revision or args.revise):
            raise ValueError('Saved sequence findings are frozen; select findings for a new sequence.')
        if limits and (args.resume or args.show or args.preview_revision or args.revise):
            raise ValueError('Saved sequence execution limits are frozen; overrides require a new sequence.')
        if args.preview_revision or args.revise:
            if (not args.sequence or args.works or args.root or args.deployment or args.native_deployment
                    or args.unlimited or args.wait_before_implement or args.expected_selection or args.invocation_id
                    or args.workers is not None or args.isolated_effects):
                raise ValueError('Revision uses only the saved sequence, reason and reviewed digest.')
            if args.preview_revision:
                if args.expected_revision:
                    raise ValueError('A preview does not accept an approval digest.')
                result = preview_revision(args.sequence, args.reason, work_ids=args.revision_works)
            else:
                if not args.expected_revision:
                    raise ValueError('Review the sequence changes before confirming their digest.')
                result = revise(args.sequence, args.expected_revision, args.reason, work_ids=args.revision_works)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.expected_revision or args.reason or args.revision_works:
            raise ValueError('Revision arguments require --preview-revision or --revise.')
        if args.show and args.invocation_id is not None:
            raise ValueError('Invocation identity applies to execution, not show.')
        if args.resume or args.show:
            if not args.sequence or args.works or args.root or args.deployment or args.native_deployment or args.unlimited or args.wait_before_implement or args.expected_selection or args.workers not in (None, 1) or args.isolated_effects or args.show and args.workers is not None:
                raise ValueError('Resume/show requires only the saved sequence path; saved configuration is reused.')
            directory, named = args.sequence, None
            if args.resume and args.workers == 1:
                serial(directory)
        else:
            from .native_catalog import select
            directory = args.sequence or sequence_root()/uuid4().hex
            deployment = select(args.deployment, args.native_deployment, role='implementer')
            named = deployment if args.native_deployment or args.deployment not in (None, 'auto') else None
            create(directory, args.works, args.root or Path.cwd(), deployment,
                   unlimited=args.unlimited, wait_before_implement=args.wait_before_implement, expected_selection=args.expected_selection,
                   workers=args.workers or 1, isolated_effects=args.isolated_effects, limits=limits,
                   findings=args.findings, findings_sha256=args.findings_sha256)
        from tare_tools_agent_runtime import CancellationToken
        from . import native_signals
        from .execution_scope import user_choice
        cancel = CancellationToken(); previous = native_signals.install(cancel)
        try:
            with user_choice('implementer', named):  # the user's own implementer (the routing profile's backstop)
                result = inspect(directory) if args.show else execute(directory, cancellation=cancel, invocation_id=args.invocation_id)
        finally:
            native_signals.restore(previous)
        print(json.dumps(result, ensure_ascii=False) if args.format == 'json' else render(result))
        return 0 if result['status'] in {'COMPLETED', 'READY', 'PAUSED', 'BLOCKED', 'CANCELLED'} else 1
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'error': str(exc)})); return 2

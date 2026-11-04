"""Reviewed scheduling changes over Backlog transactions, preserving completed work."""
import copy
from pathlib import Path

from compute_plane.native_journal import regular, save_json
from graph_backlog.algorithms import readiness
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.mutations import execute_graph_transaction
from graph_backlog.validation import validate_work_graph
from . import relay_mesh, work_change, work_run
from .native_agent import read_json
from .native_installation import storage_root


def graph():
    value = read_json(relay_mesh.WORK_GRAPH_FILE, 32*1024*1024)
    if validate_work_graph(value)['status'] != 'PASS':
        raise ValueError('Invalid Work graph.')
    return WorkGraph(value)


def current(work_id):
    wg = graph()
    if work_id not in wg.by_id:
        raise ValueError('Unknown Work: '+work_id)
    node = wg.by_id[work_id]
    return {'priority': node['priority'], 'prerequisites': sorted({pre for _, pre in wg.block_in[work_id]})}


def preview(root, edits, reason, *, raw=None):
    root = Path(root).resolve(); control = Path(relay_mesh.RELAY_ROOT).resolve()
    if not isinstance(edits, dict) or not 1 <= len(edits) <= 64 or not reason.strip():
        raise ValueError('Stage 1–64 Works and explain the scheduling change.')
    wg = graph() if raw is None else WorkGraph(raw)
    before = wg.to_dict(); updated = copy.deepcopy(before)
    changes, removed, added, locked = [], [], [], set()
    for key, edit in edits.items():
        if key not in wg.by_id or set(edit) != {'priority', 'prerequisites'}:
            raise ValueError('Select existing Works, priorities and prerequisites only.')
        deps = edit['prerequisites']
        if (not isinstance(deps, list) or any(not isinstance(d, str) or d not in wg.by_id for d in deps)
                or len(set(deps)) != len(deps) or key in deps):
            raise ValueError('Prerequisites must be distinct existing Works other than the target.')
        if edit['priority'] not in {'P0', 'P1', 'P2', 'P3'}:
            raise ValueError('Select priority P0–P3.')
        node = wg.by_id[key]; old_deps = sorted({pre for _, pre in wg.block_in[key]})
        if node['priority'] == edit['priority'] and old_deps == sorted(deps):
            continue
        cone = work_change.affected(wg, key) if old_deps != sorted(deps) else [key]
        locked.update(cone)
        for affected in cone:
            if wg.status_of(wg.by_id[affected]) != 'NOT_DONE':
                raise ValueError('Scheduling affects completed/superseded Work '+affected+'; use explicit Work revision.')
            state = work_run.status(affected)
            if state['train'] or state['last_run'] or state['receipt_errors']:
                raise ValueError('Reconcile or revise the existing execution of '+affected+' before scheduling.')
        replacement = copy.deepcopy(node); replacement['priority'] = edit['priority']
        replacement['canonical_revision'] = 'order-'+sha256_canonical([node, edit])[:24]
        updated['nodes'] = [replacement if n['id'] == key else n for n in updated['nodes']]
        # Retain original relation type, evidence and notes for every unchanged prerequisite.
        removed.extend(e for e, pre in wg.block_in[key] if pre not in deps)
        for pre in sorted(set(deps)-set(old_deps)):
            added.append({'from': pre, 'to': key, 'type': 'UNLOCKS', 'semantic': True,
                'source_refs': [], 'source_details': [], 'confidence': {'score': 1},
                'notes': ['Operator scheduling: '+reason], 'requirements': {}})
        changes.append({'work_id': key, 'before': node, 'after': replacement,
                        'prerequisites_before': old_deps, 'prerequisites_after': sorted(deps)})
    if not changes:
        raise ValueError('No scheduling change to apply.')
    updated['edges'] = [e for e in updated['edges'] if e not in removed]+added
    updated['revision'] = compute_revision_hash(updated)
    validation = validate_work_graph(updated)
    if validation['status'] != 'PASS':
        raise ValueError('Scheduling violates graph constraints: '+str(validation['errors']))
    after = WorkGraph(updated)
    def ready(g, key):
        value = readiness(g, g.by_id[key], 'operational')
        return {'ready': value['ready'], 'reasons': value['reasons'],
                'prerequisites': value['unresolved_prerequisites']}
    value = {'schema': 'tare.tools/work-order/1', 'root': str(root), 'control_root': str(control),
        'graph_path': str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()), 'reason': reason, 'edits': edits,
        'base_revision': compute_revision_hash(before), 'result_revision': updated['revision'],
        'changes': changes, 'removed_edges': removed, 'added_edges': added, 'locked_works': sorted(locked),
        'impact': {key: {'before': ready(wg, key), 'after': ready(after, key)} for key in sorted(locked)}}
    return {'status': 'PREVIEW', 'sha256': sha256_canonical(value), 'order': value}


def apply(view):
    value = view['order']; digest = view['sha256']
    if (sha256_canonical(value) != digest or value['control_root'] != str(Path(relay_mesh.RELAY_ROOT).resolve())
            or value['graph_path'] != str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())):
        raise ValueError('Scheduling review or control root changed.')
    directory = regular(storage_root()/'work-order'/digest)
    if directory.resolve().is_relative_to(Path(value['root'])):
        raise ValueError('Scheduling receipts must remain outside the project.')
    file = directory/'review.json'
    with work_change.work_locks(value['locked_works']):
        def mutate(wg):
            raw = wg.to_dict(); revision = compute_revision_hash(raw)
            if revision == value['result_revision']:
                if not file.exists() or read_json(file, 4*1024*1024) != view:
                    raise ValueError('Previous scheduling receipt is unavailable.')
                return raw
            if revision != value['base_revision']:
                raise ValueError('Graph changed; preview scheduling again.')
            fresh = preview(value['root'], value['edits'], value['reason'], raw=raw)
            if fresh != view:
                raise ValueError('Scheduling or execution changed; preview again.')
            directory.mkdir(parents=True, exist_ok=True); save_json(file, view)
            replacements = {row['work_id']: row['after'] for row in value['changes']}
            raw = copy.deepcopy(raw)
            raw['nodes'] = [replacements.get(n['id'], n) for n in raw['nodes']]
            raw['edges'] = [e for e in raw['edges'] if e not in value['removed_edges']]+value['added_edges']
            return raw
        result = execute_graph_transaction(value['graph_path'], mutate)
    return {'status': 'ORDER_UPDATED', 'work_ids': [r['work_id'] for r in value['changes']],
        'revision': result['revision'], 'receipt': str(file), 'generation_calls': 0}


def render(view):
    value = view['order']; lines = ['Scheduling review: '+value['reason'],
        'Graph: '+value['graph_path'], 'Base revision: '+value['base_revision'],
        'Proposal: '+view['sha256']]
    for row in value['changes']:
        lines += [row['work_id']+': '+row['before']['priority']+' → '+row['after']['priority'],
            '  Prerequisites: '+(', '.join(row['prerequisites_before']) or 'none')+' → '+
                (', '.join(row['prerequisites_after']) or 'none')]
    for key, impact in value['impact'].items():
        before, after = impact['before'], impact['after']
        label = lambda item: 'Ready' if item['ready'] else 'Blocked'
        lines.append('  '+key+' readiness: '+label(before)+' → '+label(after))
        if after['reasons']:
            lines.append('    Reasons: '+', '.join(after['reasons']))
        unresolved = [item['id'] for item in after['prerequisites'] if not item.get('satisfied')]
        if unresolved:
            lines.append('    Waiting for: '+', '.join(unresolved))
    lines += ['Confirm applies only these scheduling changes. Execution requires its own checks.']
    return '\n'.join(lines)

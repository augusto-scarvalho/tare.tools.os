"""Reviewed replacement of unstarted Works; Backlog owns supersession and CAS."""
import copy
from datetime import datetime, timezone
from pathlib import Path

from compute_plane.native_journal import regular, save_json
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.mutations import execute_graph_transaction, supersede_node_in_graph
from graph_backlog.validation import validate_work_graph
from . import relay_mesh, work_order, work_change, work_run
from .native_agent import read_json
from .native_installation import storage_root


def preview(root, replacements, reason, *, raw=None, observed_at=None):
    wg = work_order.graph() if raw is None else WorkGraph(raw)
    if (not isinstance(replacements, dict) or not 1 <= len(replacements) <= 64 or not reason.strip()
            or any(old not in wg.by_id or new not in wg.by_id or old == new for old, new in replacements.items())
            or set(replacements) & set(replacements.values())):
        raise ValueError('Map existing old Works to distinct successor identities outside the replaced set.')
    locked = sorted({key for item in set(replacements) | set(replacements.values())
                     for key in work_change.affected(wg, item)})
    for key in locked:
        state = work_run.status(key)
        if wg.status_of(wg.by_id[key]) != 'NOT_DONE' or state['train'] or state['last_run'] or state['receipt_errors']:
            raise ValueError('Replacement affects completed or executed Work '+key+'; reconcile/revise it explicitly first.')
    before = wg.to_dict(); updated = copy.deepcopy(before); moves = []
    blocking = [e for rows in wg.block_in.values() for e, _ in rows]
    kept = []; remapped = []
    for edge in updated['edges']:
        if edge not in blocking or not ({edge['from'], edge['to']} & set(replacements)):
            kept.append(edge); continue
        if edge.get('requirements'):
            raise ValueError('Review criterion-specific dependency requirements explicitly before replacement.')
        source, target = replacements.get(edge['from'], edge['from']), replacements.get(edge['to'], edge['to'])
        new = None
        if source != target:
            new = {**edge, 'from': source, 'to': target, 'source_refs': [], 'source_details': [],
                   'confidence': {'score': 1}, 'notes': ['Operator replacement: '+reason]}
            remapped.append(new)
        moves.append({'before': edge, 'after': new})
    # Preserve existing edge evidence; never relabel old evidence as proof about a successor.
    for edge in remapped:
        if not any((e['from'], e['to'], e['type'], e.get('semantic', True)) ==
                   (edge['from'], edge['to'], edge['type'], edge.get('semantic', True)) for e in kept):
            kept.append(edge)
    updated['edges'] = kept; updated['revision'] = compute_revision_hash(updated)
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    for old, new in replacements.items():
        updated = supersede_node_in_graph(WorkGraph(updated), old, new, reason)
        node = next(n for n in updated['nodes'] if n['id'] == old)
        node['completion']['superseded_at'] = observed_at
        updated['revision'] = compute_revision_hash(updated)
    if validate_work_graph(updated)['status'] != 'PASS':
        raise ValueError('Replacement violates graph constraints.')
    changed = [n for n in updated['nodes'] if n != wg.by_id[n['id']]]
    value = {'schema': 'tare.tools/work-replacement/1', 'root': str(Path(root).resolve()),
        'control_root': str(Path(relay_mesh.RELAY_ROOT).resolve()),
        'graph_path': str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()), 'replacements': replacements,
        'reason': reason, 'observed_at': observed_at, 'locked_works': locked,
        'base_revision': compute_revision_hash(before), 'result_revision': updated['revision'],
        'before': [wg.by_id[n['id']] for n in changed], 'after': changed,
        'removed_edges': [e for e in before['edges'] if e not in updated['edges']],
        'added_edges': [e for e in updated['edges'] if e not in before['edges']], 'relations': moves,
        'successors': [wg.by_id[key] for key in dict.fromkeys(replacements.values())]}
    return {'status': 'PREVIEW', 'sha256': sha256_canonical(value), 'replacement': value}


def apply(view):
    value = view['replacement']; digest = view['sha256']
    if (digest != sha256_canonical(value) or value['control_root'] != str(Path(relay_mesh.RELAY_ROOT).resolve())
            or value['graph_path'] != str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())):
        raise ValueError('Replacement review/control root changed.')
    directory = regular(storage_root()/'work-replacements'/digest); file = directory/'review.json'
    if directory.resolve().is_relative_to(Path(value['root'])):
        raise ValueError('Replacement receipts must remain outside the project.')
    with work_change.work_locks(value['locked_works']):
        def mutate(wg):
            raw = wg.to_dict(); revision = compute_revision_hash(raw)
            if revision == value['result_revision']:
                if not file.exists() or read_json(file, 4*1024*1024) != view:
                    raise ValueError('Previous replacement receipt unavailable.')
                return raw
            if revision != value['base_revision']:
                raise ValueError('Graph changed; preview replacement again.')
            fresh = preview(value['root'], value['replacements'], value['reason'], raw=raw, observed_at=value['observed_at'])
            if fresh != view:
                raise ValueError('Replacement or execution changed; preview again.')
            directory.mkdir(parents=True, exist_ok=True); save_json(file, view)
            changed = {n['id']: n for n in value['after']}; raw = copy.deepcopy(raw)
            raw['nodes'] = [changed.get(n['id'], n) for n in raw['nodes']]
            raw['edges'] = [e for e in raw['edges'] if e not in value['removed_edges']]+value['added_edges']
            return raw
        result = execute_graph_transaction(value['graph_path'], mutate)
    return {'status': 'WORKS_REPLACED', 'replacements': value['replacements'],
            'work_ids': list(dict.fromkeys(value['replacements'].values())), 'revision': result['revision'],
            'receipt': str(file), 'generation_calls': 0}


def render(view):
    value = view['replacement']; nodes = {n['id']: n for n in value['before']+value['successors']}
    lines = ['Replacement review: '+value['reason']]
    for old, new in value['replacements'].items():
        lines += [old+' → '+new, '  Previous criteria: '+str(nodes[old]['exit_criteria']),
                  '  Successor criteria: '+str(nodes[new]['exit_criteria'])]
    for row in value['relations']:
        edge, new = row['before'], row['after']
        lines.append(edge['from']+' → '+edge['to']+' becomes '+
            (new['from']+' → '+new['to'] if new else 'internal to the merged successor'))
    return '\n'.join(lines+['Old Works and original relation evidence remain in history/receipt. Successor criteria are not inherited.'])

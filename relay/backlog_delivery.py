"""Record reviewed progress on existing backlog items, without granting admission.

Preview: python -m relay.backlog_delivery --graph ABS_JSON --record RECORDS_JSON
Apply with the returned --expected-revision. Each record has work_id, summary,
evidence (references), remaining (explicit list), and criteria (exact criterion
and evidence text). Empty remaining requires coverage of every exit criterion.
References are reviewed claims, not independently verified proof. Managed Work
uses its own settlement/recovery; this command does not create train receipts.
Complete deliveries use work_status=done. Admission and Git integration are
separate facts; this recorder never invents a pending approval.

--repair-statuses previews correction of old complete delivery labels only.
Apply that preview with --apply --expected-revision REV; evidence and dates stay intact.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, UsageError
from graph_backlog.mutations import execute_graph_transaction
from graph_backlog.validation import validate_work_graph, validate_completion_evidence

LEGACY_DONE = 'delivered_pending_admission'


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _records(graph, records):
    if validate_work_graph(graph)['status'] != 'PASS':
        raise ValueError('Invalid backlog graph.')
    if not isinstance(records, list) or not records:
        raise ValueError('Record must be a non-empty JSON list.')
    nodes, seen, result = WorkGraph(graph).by_id, set(), []
    for row in records:
        required_fields = {'work_id', 'summary', 'evidence', 'remaining', 'criteria'}
        optional_fields = {'evidence_grade', 'evidence_grade_reason', 'source_refs'}
        if (not isinstance(row, dict) or not required_fields <= set(row)
                or set(row) - required_fields - optional_fields):
            raise ValueError('Each record needs work_id, summary, evidence, remaining and criteria; '
                             'only evidence_grade, evidence_grade_reason and source_refs are optional.')
        key = row['work_id']
        if not _text(key) or key in seen or key not in nodes:
            raise ValueError('Unknown or duplicate Work ID: '+str(key))
        seen.add(key)
        node = nodes[key]
        if node['completion']['status'] == 'SUPERSEDED':
            raise ValueError('Cannot reconcile SUPERSEDED Work: '+key)
        if node.get('metrics', {}).get('verification_checks') or any(
                'SMART_POLICY' in str(e) for e in (node['completion'].get('dod_evidence') or [])):
            raise ValueError('Use managed Work settlement/recovery for '+key)
        if not _text(row['summary']):
            raise ValueError('Describe the delivered result.')
        for field in ('evidence', 'remaining'):
            value = row[field]
            if not isinstance(value, list) or any(not _text(x) for x in value):
                raise ValueError(field+' must be an explicit list of nonempty strings.')
        if not row['evidence']:
            raise ValueError('Evidence references are required.')
        criteria = row['criteria']
        if not isinstance(criteria, list) or any(
                not isinstance(x, dict) or set(x) != {'criterion', 'evidence'}
                or not _text(x['criterion']) or not _text(x['evidence']) for x in criteria):
            raise ValueError('Criteria entries need exact criterion and nonempty evidence text.')
        supplied = [x['criterion'] for x in criteria]
        required = node['exit_criteria']
        if len(set(supplied)) != len(supplied) or any(x not in required for x in supplied):
            raise ValueError('Use exact existing exit criteria, once each.')
        if not row['remaining'] and (not required or set(supplied) != set(required)):
            raise ValueError('DONE requires evidence for every existing exit criterion.')
        if 'evidence_grade' in row and row['evidence_grade'] not in ('A', 'B', 'C', 'D'):
            raise ValueError('EVIDENCE_GRADE_INVALID: declare A, B, C or D explicitly.')
        if 'evidence_grade_reason' in row and not _text(row['evidence_grade_reason']):
            raise ValueError('EVIDENCE_GRADE_REASON_INVALID: supply a nonempty explanation.')
        if 'source_refs' in row:
            refs = row['source_refs']
            if not isinstance(refs, list) or any(not _text(ref) for ref in refs):
                raise ValueError('SOURCE_REFS_INVALID: supply a list of existing registry IDs.')
            for ref in refs:
                if ref not in graph.get('sources', {}):
                    raise ValueError('SOURCE_REF_UNKNOWN: '+ref+'; select an existing registry ID.')
        result.append(copy.deepcopy(row))
    return result


def preview(graph_path, records, *, sources=None):
    from tare_ops import read_graph
    path = Path(graph_path)
    if not path.is_absolute():
        raise ValueError('Select an absolute graph path.')
    graph = read_graph(path)
    _, records, _ = _prepare(graph, records, sources=sources)
    return {'revision': compute_revision_hash(graph), 'records': records,
            **({'sources': copy.deepcopy(sources)} if sources else {})}


class _Unchanged(Exception):
    def __init__(self, graph):
        self.graph = graph


def _prepare(before, rows, *, operation=None, sources=None):
    """Build and validate the same candidate for preview and the locked writer."""
    if validate_work_graph(before)['status']!='PASS':
        raise ValueError('Invalid backlog graph.')
    graph = copy.deepcopy(before)
    if sources is not None:
        if not isinstance(sources,dict) or len(sources)>8:
            raise ValueError('Register at most eight observed source records.')
        for key,value in sources.items():
            if not _text(key) or not isinstance(value,dict):
                raise ValueError('Invalid observed source record.')
            existing=graph['sources'].get(key)
            if existing is not None:
                identity=lambda row:{k:v for k,v in row.items() if k!='observed_at'}
                if identity(existing)!=identity(value):
                    raise ValueError('Existing source differs; source records cannot be overwritten: '+key)
            else:
                graph['sources'][key]=copy.deepcopy(value)
        # The saved revision validated above describes the original graph.
        # This candidate is mutable; the owner computes its revision at commit.
        graph.pop('revision',None)
    records = _records(graph, rows)
    if sources and set(sources)-{ref for row in records for ref in row.get('source_refs',[])}:
        raise ValueError('Every registered source must be referenced by the reviewed records.')
    nodes = {node['id']: node for node in graph['nodes']}
    now, changed = datetime.now(timezone.utc).isoformat(), bool(set(graph['sources'])-set(before['sources']))
    for item in records:
        node = nodes[item['work_id']]
        metrics, completion = node['metrics'], node['completion']
        if not isinstance(metrics.get('delivery_history', []), list):
            raise ValueError('Delivery history is invalid for '+item['work_id'])
        record = {k: copy.deepcopy(v) for k, v in item.items() if k != 'work_id'}
        done = not item['remaining']
        status = 'DONE' if done else 'PARTIAL'
        work_status = 'done' if done else 'IN_PROGRESS'
        explicit = any(k in item for k in ('evidence_grade', 'evidence_grade_reason', 'source_refs'))
        if (metrics.get('delivery_latest') == record and completion['status'] == status
                and (node['work_status'] == work_status or done and node['work_status'] == LEGACY_DONE)
                and completion['dod_satisfied'] == done
                and completion.get('residuals') == item['remaining']
                and all(ref in (completion.get('dod_evidence') or []) for ref in item['evidence'])
                and all(completion.get(k) == item[k] for k in ('evidence_grade', 'evidence_grade_reason') if k in item)
                and all(ref in node.get('source_refs', []) for ref in item.get('source_refs', []))
                and (not explicit or all(ref in node.get('provenance', []) for ref in item['evidence']))
                and (not done or completion.get('completed_at'))):
            # Exact legacy replay must not acquire grades, provenance or timestamps.
            continue
        if done and 'evidence_grade' not in item:
            raise ValueError('EVIDENCE_GRADE_REQUIRED: new or revised DONE requires explicit A/B/C/D; '
                             'declare it or record remaining obligations as PARTIAL.')
        if done and 'evidence_grade_reason' not in item:
            raise ValueError('EVIDENCE_GRADE_REASON_REQUIRED: explain the declared DONE grade explicitly.')
        for field in ('provenance', 'source_refs'):
            if not isinstance(node.get(field, []), list):
                raise ValueError('DELIVERY_CONTEXT_INVALID: repair '+field+' for '+item['work_id'])
        metrics.setdefault('delivery_history', []).append({
            'observed_at': now, 'record': copy.deepcopy(record),
            **({'operation': operation} if operation is not None else {}),
            'previous_completion': copy.deepcopy(completion),
            'previous_work_status': node['work_status'],
            'previous_observed_at': node.get('observed_at'),
            'previous_provenance': copy.deepcopy(node.get('provenance', [])),
            'previous_source_refs': copy.deepcopy(node.get('source_refs', []))})
        metrics['delivery_latest'] = record
        completion.update(status=status, dod_satisfied=done,
                          completed_at=now if done else None,
                          evidence_observed_at=now, residuals=copy.deepcopy(item['remaining']))
        for field in ('evidence_grade', 'evidence_grade_reason'):
            if field in item:
                completion[field] = item[field]
        existing = completion.get('dod_evidence') or []
        completion['dod_evidence'] = existing
        for ref in item['evidence']:
            if ref not in existing:
                existing.append(ref)
            if ref not in node.setdefault('provenance', []):
                node['provenance'].append(ref)
        for ref in item.get('source_refs', []):
            if ref not in node.setdefault('source_refs', []):
                node['source_refs'].append(ref)
        node['work_status'], node['observed_at'] = work_status, now
        changed = True
    # The owner interprets grades and A/B recoverability; text is not proof.
    validate_completion_evidence(before, graph)
    return graph, records, changed


def apply(graph_path, plan, expected_revision, *, operation=None, verify_sources=None):
    if not expected_revision or plan['revision'] != expected_revision:
        raise ValueError('Expected revision must match the preview revision.')

    def mutate(wg):
        if verify_sources is not None:
            verify_sources()
        graph, _, changed = _prepare(wg.to_dict(), plan['records'], operation=operation, sources=plan.get('sources'))
        if not changed:
            # Stop inside the owner's lock, before its unconditional writer.
            raise _Unchanged(wg.to_dict())
        return graph

    try:
        return execute_graph_transaction(graph_path, mutate, expected_rev=expected_revision)
    except _Unchanged as result:
        return result.graph


def _status_repair_plan(graph):
    if validate_work_graph(graph)['status'] != 'PASS':
        raise ValueError('Invalid backlog graph.')
    repairs, skipped = [], []
    for node in graph['nodes']:
        if node.get('work_status') != LEGACY_DONE:
            continue
        completion = node.get('completion') or {}
        record = node.get('metrics', {}).get('delivery_latest')
        if (not isinstance(record, dict) or node.get('metrics', {}).get('verification_checks')
                or any('SMART_POLICY' in str(ref) for ref in (completion.get('dod_evidence') or []))):
            skipped.append(node['id'])
            continue
        criteria = record.get('criteria')
        evidence = record.get('evidence')
        valid_criteria = (isinstance(criteria, list) and bool(criteria)
            and all(isinstance(row, dict) and set(row) == {'criterion', 'evidence'}
                    and _text(row['criterion']) and _text(row['evidence']) for row in criteria))
        covered = [row['criterion'] for row in criteria] if valid_criteria else []
        intact = (completion.get('status') == 'DONE' and completion.get('dod_satisfied') is True
            and completion.get('residuals') == [] and record.get('remaining') == []
            and valid_criteria and len(covered) == len(set(covered))
            and set(covered) == set(node['exit_criteria'])
            and isinstance(evidence, list) and bool(evidence) and all(_text(ref) for ref in evidence)
            and isinstance(completion.get('dod_evidence'), list)
            and all(ref in completion['dod_evidence'] for ref in evidence))
        (repairs if intact else skipped).append(node['id'])
    return {'revision': compute_revision_hash(graph), 'repairs': repairs, 'skipped': skipped}


def preview_status_repair(graph_path):
    """Review known legacy labels; do not reinterpret completion or old grades."""
    from tare_ops import read_graph
    path = Path(graph_path)
    if not path.is_absolute():
        raise ValueError('Select an absolute graph path.')
    return _status_repair_plan(read_graph(path))


def repair_statuses(graph_path, plan, expected_revision):
    """Change only work_status on intact, already completed delivery records."""
    if not expected_revision or plan.get('revision') != expected_revision:
        raise ValueError('Expected revision must match the preview revision.')
    def mutate(wg):
        before = wg.to_dict()
        if plan != _status_repair_plan(before):
            raise ValueError('Status repair changed; preview again.')
        if not plan['repairs']:
            raise _Unchanged(before)
        graph = copy.deepcopy(before)
        ids = set(plan['repairs'])
        for node in graph['nodes']:
            if node['id'] in ids:
                node['work_status'] = 'done'
        return graph
    try:
        return execute_graph_transaction(graph_path, mutate, expected_rev=expected_revision)
    except _Unchanged as result:
        return result.graph


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', required=True, help='Absolute Work graph JSON path')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--record', help='JSON list of reviewed delivery records')
    mode.add_argument('--repair-statuses', action='store_true',
                      help='Preview correction of old complete delivery labels; preserve evidence and dates.')
    parser.add_argument('--apply', action='store_true', help='Write the reviewed records')
    parser.add_argument('--expected-revision', help='Embedded owner revision returned by preview')
    args = parser.parse_args(argv)
    if args.apply and not args.expected_revision:
        parser.error('--apply requires --expected-revision')
    try:
        if args.repair_statuses:
            plan = preview_status_repair(args.graph)
            if args.apply:
                graph = repair_statuses(args.graph, plan, args.expected_revision)
                print(json.dumps({'revision': compute_revision_hash(graph),
                                  'repaired': plan['repairs'], 'skipped': plan['skipped'], 'authority': 'NONE'}))
            else:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        with Path(args.record).open('rb') as stream:
            data = stream.read(4*1024*1024+1)
        if len(data) > 4*1024*1024:
            raise ValueError('Delivery records exceed 4 MiB.')
        plan = preview(args.graph, json.loads(data))
        if args.apply:
            graph = apply(args.graph, plan, args.expected_revision)
            print(json.dumps({'revision': compute_revision_hash(graph),
                              'recorded': [r['work_id'] for r in plan['records']], 'authority': 'NONE'}))
        else:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
    except (ValueError, OSError, UsageError) as exc:
        print(json.dumps({'error': str(exc), 'authority': 'NONE'}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

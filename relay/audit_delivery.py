"""Read a Work run store as deliveries, retaining failed runs and missing telemetry."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path


def read(path):
    from .native_agent import read_json
    return read_json(path, 16 * 1024 * 1024)


def interval(start, end):
    try:
        value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        return round(value, 3) if value >= 0 else None
    except (TypeError, ValueError):
        return None


def total(values):
    return round(sum(values), 3) if all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values) else None


def audit_rounds(receipt):
    """Stage elapsed time includes orchestration; it is not model inference time."""
    rows, previous, opened = [], receipt.get('started_at'), None
    for event in receipt.get('stages', []):
        name = event.get('stage')
        if name == 'code_audit' and event.get('session'):
            opened = event['at']
        elif name in {'plan_audit', 'code_audit_result'}:
            rows.append({'stage': 'audit' if name == 'code_audit_result' else 'plan',
                         'verdict': event.get('verdict', event.get('status')),
                         'failure_code': event.get('failure_code'),
                         'seconds': interval(opened or previous, event.get('at')),
                         'findings': event.get('findings', [])})
            opened = None
        previous = event.get('at')
    if opened is not None:
        rows.append({'stage': 'audit', 'verdict': None, 'failure_code': None,
                     'seconds': interval(opened, receipt.get('ended_at')), 'findings': []})
    return rows


def session_roots(run):
    # Inspect only known custody slots. Never descend into worktrees or test fixtures.
    for slot in sorted(run.iterdir()):
        if not slot.is_dir() or slot.is_symlink():
            continue
        if slot.name == 'implement' or slot.name.startswith('plan-audit'):
            yield ('plan' if slot.name.startswith('plan') else 'implement'), slot
        elif slot.name == 'code-audit' or slot.name.startswith('audit-'):
            for route in sorted(slot.iterdir()):
                if route.is_dir() and route.name.isdigit():
                    yield 'audit', route


def usage(roots, issues):
    from .execution_usage import aggregate_sessions
    from .execution_usage_sources import session_entry
    from .delegation import children
    entries, legacy, seen = [], [], set()
    for stage, root in roots:
        try:
            if (root/'native-agent.json').is_file() or (root/'host.json').is_file():
                entry = session_entry(root, stage)
                entries.append(entry)
                entries.extend(dict(session_entry(child, stage), auxiliary=True) for child in children(root))
            elif (root/'session.json').is_file():
                state = read(root/'session.json')
                identity = state.get('id')
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                tokens, helper = state.get('tokens', {}), state.get('delegation', {})
                legacy.append({'session_id': identity, 'stage': stage, 'source': str(root),
                    'calls': total([state.get('calls'), helper.get('calls', 0)]),
                    'input_tokens': total([tokens.get('input'), helper.get('input_tokens', 0)]),
                    'output_tokens': total([tokens.get('output'), helper.get('output_tokens', 0)]),
                    'cost_estimate_usd': state.get('estimated_cost_usd') if not helper.get('calls') else None})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append({'source': str(root), 'error': str(exc)[:200]})
    return {'native': aggregate_sessions(entries), 'legacy': legacy,
            'recorded_sessions': [{'source': e['source'], 'session_id': e['session_id'], 'stage': e['stage'],
                                   'auxiliary': e.get('auxiliary', False)} for e in entries]}


def report(store):
    store = Path(store).resolve()
    if not store.is_dir():
        raise ValueError('Work run folder does not exist: '+str(store))
    paths = sorted(store.glob('*/run.json'))
    if len(paths) > 10000:
        raise ValueError('Select a Work run store with at most 10000 receipts.')
    records, issues, identities = [], [], defaultdict(set)
    for path in paths:
        try:
            receipt = read(path)
            if receipt.get('schema') != 'tare.tools/work-run/1':
                raise ValueError('Unsupported Work receipt')
            work = receipt['work_id']
            if not isinstance(work, str) or not isinstance(receipt.get('run_id'), str):
                raise ValueError('Missing Work or run identity')
            if not isinstance(receipt.get('stages', []), list) or any(not isinstance(e, dict) for e in receipt.get('stages', [])):
                raise ValueError('Invalid stage history')
            project = receipt.get('graph_path') or receipt.get('repository_root') or next(
                (e.get('repository_root') for e in receipt.get('stages', []) if e.get('repository_root')), None)
            if project:
                identities[work].add(project)
            records.append((path, receipt, project))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append({'source': str(path), 'error': str(exc)[:200]})
    groups = defaultdict(list)
    seen = {}
    for path, receipt, project in records:
        work = receipt['work_id']
        project = project or (next(iter(identities[work])) if len(identities[work]) == 1 else 'Unknown project')
        key = (project, work, receipt['run_id'])
        if key in seen:
            if seen[key] != receipt:
                issues.append({'source': str(path), 'error': 'Conflicting duplicate run identity'})
            continue
        seen[key] = receipt
        groups[project, work].append((path, receipt))
    deliveries = []
    for (project, work), runs in sorted(groups.items()):
        rows, roots, accepted_plans = [], [], {}
        for path, receipt in sorted(runs, key=lambda row: row[1].get('started_at', '')):
            rounds = audit_rounds(receipt)
            descriptor = next((e.get('sha256') for e in receipt.get('stages', []) if e.get('stage') == 'descriptor'), None)
            duration = receipt.get('duration_ms')
            run_roots = list(session_roots(path.parent))
            prepared = receipt.get('prepared_implementation')
            if isinstance(prepared, dict) and prepared.get('session'):
                run_roots.append(('implement', Path(prepared['session'])))
            run_roots.extend(('implement', Path(p)) for p in receipt.get('preparation_sessions', []))
            roots.extend(run_roots)
            plan_ids = set()
            for stage, root in run_roots:
                if stage != 'plan':
                    continue
                for name, key in [('native-agent.json', 'session_id'), ('session.json', 'id')]:
                    if (root/name).is_file():
                        try:
                            identity = read(root/name).get(key)
                            if isinstance(identity, str):
                                plan_ids.add(identity)
                        except (OSError, ValueError, TypeError):
                            pass  # usage() below reports unreadable custody explicitly.
            repeated = descriptor in accepted_plans and bool(plan_ids - accepted_plans[descriptor])
            if descriptor and any(r['stage'] == 'plan' and r['verdict'] == 'ACCEPT' for r in rounds):
                accepted_plans.setdefault(descriptor, set()).update(plan_ids)
            rows.append({'run_id': receipt['run_id'], 'source': str(path),
                'sha256': sha256(path.read_bytes()).hexdigest(), 'status': receipt.get('status'),
                'seconds': duration/1000 if type(duration) in (int, float) and duration >= 0 else None,
                'audit_rounds': rounds, 'descriptor_sha256': descriptor,
                'repeated_accepted_plan': repeated, 'plan_session_ids': sorted(plan_ids),
                'error': receipt.get('error'), 'candidate': receipt.get('candidate_commit'),
                'settled': receipt.get('status') == 'SETTLED' and (receipt.get('landing') or {}).get('landed') is True})
        observed = usage(roots, issues)
        recorded_reviews = len({r['session_id'] for r in observed['legacy'] + observed['recorded_sessions']
                                if r['stage'] in {'plan', 'audit'} and not r.get('auxiliary')})
        measured_reviews = sum(len(r['audit_rounds']) for r in rows)
        # Legacy admission can fail after the auditor finished but before the stage
        # receipt was written. Keep those sessions and flag their unmeasured time.
        missing_intervals = max(0, recorded_reviews - measured_reviews)
        deliveries.append({'project': project, 'work_id': work, 'settled': any(r['settled'] for r in rows),
            'runs': rows, 'attempt_count': len(rows), 'seconds': total([r['seconds'] for r in rows]),
            'audit_round_count': recorded_reviews, 'audit_stage_visits': measured_reviews,
            'audit_seconds': None if missing_intervals else total([a['seconds'] for r in rows for a in r['audit_rounds']]),
            'known_audit_seconds': sum(a['seconds'] for r in rows for a in r['audit_rounds'] if a['seconds'] is not None),
            'unmeasured_review_intervals': missing_intervals,
            'repeated_accepted_plans': sum(r['repeated_accepted_plan'] for r in rows),
            'usage': observed})
    settled = sum(d['settled'] for d in deliveries)
    return {'schema': 'tare.tools/audit-delivery/1', 'authority': 'NONE', 'store': str(store),
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'delivery_count': len(deliveries), 'settled_count': settled, 'deliveries': deliveries, 'issues': issues,
        'next_review_at_settled_count': 10, 'cohort_review_due': settled >= 10,
        'basis': 'All attempts in this store, grouped by project and Work. Settlement is the recorded landing, not a new admission. '
                 'Audit seconds are elapsed stage intervals, including orchestration. Legacy helpers are included. '
                 'Unrecorded sessions, charges and adjudications remain unknown. Repeated accepted plans require investigation; '
                 'equal descriptor hashes alone do not establish a false block or safe reuse.',
        'false_blocks_adjudicated': None}


def render(value):
    lines = [f"{value['delivery_count']} Work; {value['settled_count']} recorded deliveries.",
             ('Cohort review due. ' if value['cohort_review_due'] else
              f"First cohort review at {value['next_review_at_settled_count']} recorded deliveries. ")
             +'Refresh reads new receipts; no model calls.', '']
    for row in value['deliveries']:
        native, legacy = row['usage']['native'], row['usage']['legacy']
        seconds = row['audit_seconds']
        lines.extend([row['work_id']+' | '+('Delivered' if row['settled'] else 'Pending'), row['project'],
            f"  {row['attempt_count']} attempts; {row['audit_round_count']} review sessions; audit elapsed: "
            +(f'{seconds/60:.2f} min' if seconds is not None else
              f"at least {row['known_audit_seconds']/60:.2f} min; incomplete"),
            f"  Repeated accepted plan: {row['repeated_accepted_plans']} (inspect before deciding)",
            f"  Native calls: {native['generation_intents'] - native['not_started']}; "
            f"legacy calls: {total([r['calls'] for r in legacy]) if legacy else 'Unknown / absent'}",
            f"  Native input/output tokens (known): {native['tokens']['input_tokens']['known_sum']}/"
            f"{native['tokens']['output_tokens']['known_sum']}; "
            f"calls missing input/output: {native['tokens']['input_tokens']['unknown_calls']}/"
            f"{native['tokens']['output_tokens']['unknown_calls']}",
            f"  Legacy input/output tokens: {total([r['input_tokens'] for r in legacy]) if legacy else 'Unknown / absent'}/"
            f"{total([r['output_tokens'] for r in legacy]) if legacy else 'Unknown / absent'}",
            f"  Native recorded charges: {native['known_cost_by_currency'] or 'Unknown'}; "
            f"unpriced calls: {native['unpriced_or_partial_calls']}",
            f"  Legacy review calls: {total([r['calls'] for r in legacy if r['stage'] in {'plan', 'audit'}])}; "
            'full delivery price: Unknown' if legacy else '  Full delivery price: Unknown when stages or prices are missing'])
        for run in row['runs']:
            lines.append('    '+run['run_id'][:8]+' '+str(run['status'])+' | '+str(run['error'] or ''))
    lines.extend(['', value['basis']])
    lines.extend('Unavailable evidence: '+issue['source']+' | '+issue['error'] for issue in value['issues'])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(report(args.store), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

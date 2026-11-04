"""Read existing execution receipts under their owners' custody; no inference."""
from compute_plane.native_usage import deployment_contract
from pathlib import Path
from contextlib import nullcontext


def session_entry(path, stage='direct', *, locked=False, cost_snapshot=None):
    from compute_plane.native_journal import NativeObservationJournal, custody_lock, regular, NativeJournal
    from compute_plane.native_usage import native_usage_report, UsageContract, enrich_provider_costs
    from .native_agent import read_json
    from .native_spend import settings
    root = regular(Path(path))
    if not root.is_dir():
        raise ValueError('usage_session_missing')
    with nullcontext() if locked else custody_lock(root):
        if (root/'native-agent.json').exists():
            state = read_json(root/'native-agent.json', 1024*1024)
            outcome = read_json(root/'outcome.json', 4*1024*1024) if (root/'outcome.json').exists() else None
            from compute_plane.native_usage import MAX_USAGE_BYTES
            report = read_json(root/'usage.json', MAX_USAGE_BYTES) if (root/'usage.json').exists() else None
            if report is not None:
                report = enrich_provider_costs(report, state.get('deployment', {}))
            session_id = state['session_id']
            deployment = state.get('deployment', {})
            attempts = state.get('invocations', [])
        else:
            host = read_json(root/'host.json', 1024*1024)
            state = read_json(root/'attempt.json', 4*1024*1024) if (root/'attempt.json').exists() else {}
            session_id = host['session_id']
            deployment = host['deployment']
            if state and state['identity']['session_id'] != session_id:
                raise ValueError('usage_attempt_identity_mismatch')
            attempts = state.get('attempts', [])
            result = attempts[-1].get('outcome', {}) if attempts else {}
            outcome = result.get('outcome')
            report = None
            if (root/'journal.jsonl').exists():
                journal = NativeJournal(root/'journal.jsonl')
                observations = ([r.event() for r in NativeObservationJournal(root/'observations.jsonl').snapshot().records]
                                if (root/'observations.jsonl').exists() else [])
                deployment = host['deployment']
                contract = deployment_contract(deployment)
                _, tariff = settings(deployment)
                digests = {o['profile_digest'] for o in observations if o.get('profile_digest')}
                if journal.snapshot().records:
                    report = native_usage_report(journal, observations,
                        contracts={d: UsageContract(**contract) for d in digests} if contract else {},
                        tariffs={d: tariff for d in digests} if tariff else {},
                        observation_failures=result.get('observation_failures'))
        entry = {'stage': stage, 'source': str(root), 'session_id': session_id,
                'usage': report, 'outcome': outcome,
                'recovery': (outcome or {}).get('source_recovery'),
                'profile': {'deployment': deployment.get('id'),
                            'model': deployment.get('native_profile', {}).get('model')},
                'attempts': attempt_usage(root, attempts, report, session_id, stage)}
        if cost_snapshot is not None:
            from .delivery_costs import apply
            entry = apply(entry, deployment, cost_snapshot)
            # Dispatch totals must use the same valuation as the session total.
            entry['attempts'] = attempt_usage(root, attempts, entry['usage'], session_id, stage)
            if cost_snapshot['profiles'].get(deployment.get('id'), {}).get('mode') == 'hourly':
                for attempt in entry['attempts']:
                    detail = attempt.get('dispatch_usage')
                    if detail:
                        detail.update(known_cost_by_currency={}, cost_allocation='session_total_only',
                                      unpriced_or_partial_calls=detail['generation_intents']-detail['not_started'])
        return entry



def attempt_usage(root, attempts, report, session_id, stage):
    from compute_plane.native_journal import NativeJournal
    from .execution_usage import aggregate_sessions
    result = []
    events = None
    for attempt in attempts:
        row = {k: attempt[k] for k in ('id', 'number', 'status', 'reason',
               'started_at', 'ended_at', 'journal_start', 'journal_end') if k in attempt}
        row['dispatch_usage'] = None
        start, end = attempt.get('journal_start'), attempt.get('journal_end')
        if report is not None and type(start) is int and type(end) is int and 0 <= start <= end:
            if events is None:
                events = NativeJournal(root/'journal.jsonl').snapshot().records
            if end > len(events):
                raise ValueError('usage_attempt_journal_range_invalid')
            ids = {r.event()['call_id'] for r in events[start:end]
                   if r.event()['kind'] in {'model_requested', 'context_summary_requested'}}
            selected = dict(report, calls=[c for c in report['calls'] if c['call_id'] in ids])
            summary = aggregate_sessions([{'stage': stage, 'source': str(root),
                'session_id': session_id, 'usage': selected}])
            row['dispatch_usage'] = {k: summary[k] for k in ('generation_intents',
                'summary_intents', 'retry_intents', 'not_started', 'tokens',
                'known_cost_by_currency', 'unpriced_or_partial_calls')}
        result.append(row)
    return result


def execution_usage(*, sessions=(), train=None, costs=None):
    from .execution_usage import aggregate_sessions
    from .native_agent import read_json
    from compute_plane.native_journal import regular
    from .native_continuity import chain
    from .delivery_costs import load
    cost_snapshot = load(str(costs)) if costs is not None else None
    paths = [p for path in sessions for p in
             (chain(path) if (Path(path)/'native-agent.json').exists() else [path])]
    entries = [session_entry(path, cost_snapshot=cost_snapshot) for path in dict.fromkeys(paths)]
    absent = []
    work = None
    if train is not None:
        origin = read_json(regular(Path(train)/'WORK_RUN.json'), 4*1024*1024)
        work = origin['work_id']
        plans = [*origin.get('plan_audit_attempts', []), *([origin['plan_custody']] if origin.get('plan_custody') else [])]
        if not plans:
            absent.append({'stage': 'plan', 'source': None, 'status': 'not_recorded'})
        paths = [('plan', p) for p in plans]
        paths.append(('implement', origin['implementation_custody']))
        paths.extend(('implement', p) for p in origin.get('preparation_sessions', []))
        audit_roots = [*origin.get('audit_attempts', []), origin.get('audit_custody') or
                       str(Path(origin['implementation_custody']).parent/'code-audit')]
        for root in audit_roots:
            paths.extend(('audit', str(Path(root)/str(i))) for i, route in
                         enumerate((origin.get('audit') or {}).get('routes', [])) if route['eligible'])
        for stage, path in paths:
            root = regular(Path(path))
            if not (root/'native-agent.json').exists() and not (root/'host.json').exists():
                absent.append({'stage': stage, 'source': str(root), 'status': 'not_recorded'})
            else:
                entries.append(session_entry(root, stage, cost_snapshot=cost_snapshot))
    from .delegation import children
    # Children keep their own provider contract and tariff; aggregate once by
    # session/call identity, never add raw token totals to the parent's report.
    for entry in list(entries):
        for child in children(Path(entry['source'])):
            entries.append(session_entry(child, 'exploration', cost_snapshot=cost_snapshot))
        from .planning_tools import children as planning_children
        for child in planning_children(Path(entry['source'])):
            entries.append(session_entry(child, 'planning', cost_snapshot=cost_snapshot))
        from .conversation_review import children as review_children
        for child in review_children(Path(entry['source'])):
            entries.append(session_entry(child, 'audit', cost_snapshot=cost_snapshot))
        # Implementer units have separate journals too; their tokens are not part
        # of their coordinator's report. Include interrupted children as unknown.
        for child in regular(Path(entry['source'])/'units').glob('*/session-*'):
            child = regular(child)
            if regular(child/'native-agent.json').is_file():
                entries.append(session_entry(child, 'implementation', cost_snapshot=cost_snapshot))
    unique = {(e['stage'], e['session_id'], e['source']): e for e in entries}
    recorded = [{k: e[k] for k in ('stage', 'session_id', 'source', 'profile', 'attempts', 'recovery')}
                for _, e in sorted(unique.items())]
    return {**aggregate_sessions(entries), 'recorded_sessions': recorded, 'execution_path': 'managed_work' if train else 'direct',
            'work_id': work, 'unrecorded_stages': absent,
            'cost_basis': cost_snapshot['basis'] if cost_snapshot else 'recorded_charges_and_configured_tariffs',
            'cost_sources': sorted({c.get('cost', {}).get('source', 'configured_tariff_or_unknown')
                for e in entries for c in (e.get('usage') or {}).get('calls', [])}),
            'acceptance': 'unknown', 'acceptance_note':
            'Generation outcomes are not independent verification or landing evidence.',
            'subscription_quota': 'unknown'}

"""Read-only usage projection of console-entry owner receipts, without inference."""
import json
from pathlib import Path
import re

from compute_plane.native_journal import read_json, regular
from .native_installation import storage_root


def _object(value):
    return value if isinstance(value, dict) else {}


def _count(value):
    return str(value) if type(value) is int and value >= 0 else 'Unknown'


def _stage(identity, label, *, status=None, model=None, calls=None, tokens=None,
           cost=None, elapsed=None, extra=()):
    tokens, cost = _object(tokens), _object(cost)
    lines = [label, 'Recorded status: '+str(status or 'Unknown'),
             'Model identity: '+(json.dumps(model, ensure_ascii=False) if model else 'Unknown'),
             'Model calls: '+_count(calls),
             'Input tokens: '+_count(tokens.get('input_tokens')),
             'Output tokens: '+_count(tokens.get('output_tokens'))]
    amount, currency = cost.get('known_amount'), cost.get('currency')
    lines.append('Known model cost: '+(f'{currency} {amount}' if amount is not None and currency else 'Unknown'))
    if amount is not None and currency:
        lines.append('Cost coverage: '+('Complete' if cost.get('complete') is True else 'Partial'))
        from .provider_prices import is_reference, reference_note
        if is_reference(cost.get('tariff')):
            lines.append('Cost basis: '+reference_note(cost['tariff']))
    lines.extend(extra)
    if type(elapsed) in (int, float) and elapsed >= 0:
        lines.append(f'Recorded duration: {elapsed} ms')
    lines.append('Stages are separate observations, not an additional conversation total. No provider queried.')
    return {'id': identity, 'label': label, 'body': '\n'.join(lines)}


def entry_usage(directory, root):
    """Accept only an immediate entry in local custody, bound to this workspace."""
    directory = Path(directory)
    owner = regular(storage_root()/'console-entries')
    if (not directory.is_absolute() or directory.parent != owner
            or not re.fullmatch(r'[0-9a-f]{32}', directory.name)):
        raise ValueError('Flow usage is outside console-entry custody.')
    directory = regular(directory)
    request = read_json(directory/'request.json', max_bytes=131072)
    proposal = read_json(directory/'proposal.json', max_bytes=4*1024*1024)
    if any(not isinstance(v.get('root'), str) or Path(v['root']).resolve() != Path(root).resolve()
           for v in (request, proposal)):
        raise ValueError('Flow usage belongs to another project.')
    prefix = directory.name
    decision_path = regular(directory/'flow-assessment'/'decision.json')
    if decision_path.exists():
        decision = read_json(decision_path, max_bytes=131072)
        assessment = _object(decision.get('assessment'))
        answer = _object(assessment.get('answer'))
        rows = [_stage(prefix+'-local', 'Flow selection · local evaluator',
            status=assessment.get('status', decision.get('status')),
            model=answer.get('model_identity'), tokens=answer.get('usage'),
            elapsed=assessment.get('elapsed_ms'), extra=(
                'Worker attempts: '+_count(decision.get('inference_attempts'))+' (not proven model calls)',
                'Reason: '+str(assessment.get('reason') or decision.get('reason') or 'Unknown'),
                'Worker stage: '+str(assessment.get('worker_stage') or 'Unknown')))]
        fallback = _object(decision.get('fallback'))
        if fallback:
            receipt = _object(fallback.get('receipt'))  # Never follow receipt-supplied file paths.
            profile = _object(receipt.get('effective_profile'))
            tokens = _object(fallback.get('tokens'))
            rows.append(_stage(prefix+'-fallback', 'Flow selection · vendor fallback',
                status=fallback.get('status'), model=receipt.get('served_model') or profile.get('model'),
                calls=fallback.get('generation_calls'),
                tokens={'input_tokens': tokens.get('input'), 'output_tokens': tokens.get('output')},
                cost=receipt.get('cost'), elapsed=receipt.get('duration_ms'), extra=(
                    'Deployment: '+str(fallback.get('deployment') or 'Unknown'),
                    'Fallback trigger: '+str(fallback.get('trigger') or 'Unknown'))))
        return rows
    if proposal.get('flow_assessment'):
        raise ValueError('Recorded flow assessment receipt is missing.')
    receipt = _object(proposal.get('receipt'))
    tokens = _object(proposal.get('tokens'))
    return [_stage(prefix+'-selection', 'Flow selection · interpretation',
        status=proposal.get('status'), model=receipt.get('served_model') or
            _object(receipt.get('effective_profile')).get('model'),
        calls=proposal.get('generation_calls'),
        tokens={'input_tokens': tokens.get('input'), 'output_tokens': tokens.get('output')},
        cost=receipt.get('cost'), elapsed=receipt.get('duration_ms'))]


def flow_usage_rows(root, *, proposal=None, demand=None):
    """Current and retained proposals, once each; no filesystem-wide discovery."""
    directories = []
    if demand:
        if Path(demand['root']).resolve() != Path(root).resolve():
            raise ValueError('Flow history belongs to another project.')
        directories.extend(_object(e.get('detail')).get('directory')
                           for e in demand.get('events', []) if e.get('kind') == 'interpretation_finished')
    if proposal:
        directories.append(proposal.get('directory'))
    directories = list(dict.fromkeys(d for d in directories if isinstance(d, str) and d))
    rows = []
    if len(directories) > 32:
        rows.append({'id': 'older-flow-usage', 'label': 'Older selection receipts omitted',
                     'body': 'Showing the latest 32 proposals. Earlier receipts remain in console-entry custody.'})
    for directory in directories[-32:]:
        try:
            rows.extend(entry_usage(directory, root))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rows.append({'id': directory+'-unavailable', 'label': 'Flow selection · usage unavailable',
                         'body': 'Could not read recorded flow usage: '+str(exc)})
    return rows


def _behavioral_profile_row(report, baseline):
    """An optional projection cannot hide existing usage, including an empty cohort."""
    try:
        from .behavioral_profiles import (
            behavioral_profile_review, render_behavioral_profiles, load_behavioral_evidence,
        )
        if 'saved_delivery_observations' not in report:
            report = {**report, 'saved_delivery_observations': []}
        profile = behavioral_profile_review(report, baseline=baseline,
                                            audit_evidence=load_behavioral_evidence())
        suffix = '' if profile.get('status') == 'available' else ' · unavailable'
        return {'id': 'behavioral-profiles', 'label': 'Behavioral profiles'+suffix,
                'body': render_behavioral_profiles(profile),
                'review_targets': profile.get('delivery_evidence', {}).get('review_targets', [])}
    except Exception:
        return {'id': 'behavioral-profiles', 'label': 'Behavioral profiles · unavailable',
                'body': 'Optional descriptive view unavailable. Other usage and Works are unchanged.'}


def completed_work_usage_rows(control_root, *, include_provider_pilot=False,
                              include_behavioral_profiles=False, baseline=None):
    """Saved owner receipts and closed attempt usage snapshots only."""
    unavailable = {'id': 'completed-work-review', 'label': 'Completed Work review · unavailable',
                   'body': 'Saved control-owner Work receipts could not be projected. Other usage is unchanged.'}
    try:
        from scripts.round_table.report_round_table_telemetry import collect, completed_work_review
        control = Path(control_root)
        if not control.is_absolute():
            raise ValueError('An explicit absolute control owner is required.')
        control = regular(control)
        custody = regular(control.parent/'.reconciliation'/'work-runs')
        sources, source_warnings = [], 0
        owners, audit_observations, delivery_observations = [], [], []
        terminal = {'SETTLED', 'FAILED', 'CANCELLED', 'PLAN_REJECTED',
                    'PLAN_INVALID_DELIVERY', 'IMPLEMENTATION_FAILED',
                    'IMPLEMENTATION_BLOCKED', 'IMPLEMENTATION_CANCELLED',
                    'IMPLEMENTATION_INTERRUPTED',
                    'IMPLEMENTATION_RECONCILIATION_REQUIRED',
                    'AUDIT_REVISE', 'AUDIT_INCONCLUSIVE', 'AUDIT_FAILED',
                    'AUDIT_EXHAUSTED', 'AUDIT_BLOCKED', 'AUDIT_CANCELLED'}
        if custody.exists():
            for candidate in sorted(custody.iterdir()):
                if candidate.name == 'locks':
                    continue
                try:
                    directory = regular(candidate)
                    if not directory.is_dir():
                        continue
                    receipt = regular(directory/'run.json')
                    if not receipt.is_file():
                        continue
                    saved = read_json(receipt, max_bytes=32*1024*1024)
                    if not isinstance(saved, dict) or saved.get('schema') != 'tare.tools/work-run/1':
                        raise ValueError('Invalid saved Work receipt.')
                    sources.append(receipt)
                    owners.append((directory, saved.get('run_id')))
                    if include_behavioral_profiles and saved.get('ended_at'):
                        from .delivery_evidence import capture
                        delivery_observations.append(capture(saved))
                    audit = _object(saved.get('audit'))
                    if (include_behavioral_profiles and saved.get('ended_at')
                            and isinstance(audit.get('observation'), dict)):
                        audit_observations.append({
                            'id': saved.get('run_id'), 'work_id': saved.get('work_id'),
                            'project': saved.get('repository_root'),
                            'source_revision': saved.get('candidate_commit'),
                            'auditor': audit.get('deployment'),
                            'observation': audit['observation'],
                        })
                except (OSError, ValueError, TypeError, AttributeError):
                    source_warnings += 1
        if not sources:
            if source_warnings:
                return [unavailable] + ([_behavioral_profile_row({}, baseline)]
                                        if include_behavioral_profiles else [])
            return ([_behavioral_profile_row({}, baseline)] if include_behavioral_profiles else [])
        # collect() resolves every explicit resume before any snapshot is selected.
        report = collect(sources, include_work_metadata=True)
        runs = {run['run_id']: run for run in report['work_runs']}
        groups = {}
        for run in runs.values():
            root = run.get('execution_origin_run_id')
            if root:
                groups.setdefault(root, []).append(run)
        closed = {root for root, attempts in groups.items()
                  if any(run.get('execution_settled') for run in attempts)
                  and all(run.get('ended_at') and run.get('status') in terminal
                          for run in attempts)}
        snapshots = []
        for directory, identity in owners:
            run = runs.get(identity, {})
            if run.get('execution_origin_run_id') not in closed:
                continue
            try:
                paths = [directory/'implement'/'scratch'/'usage.json']
                for candidate in sorted(directory.iterdir()):
                    if candidate.name.startswith('plan-audit'):
                        stage = regular(candidate)
                        if stage.is_dir():
                            paths.append(stage/'usage.json')
                    elif (candidate.name.startswith('code-audit')
                          or candidate.name.startswith('audit-') and candidate.name[6:].isdigit()):
                        stage = regular(candidate)
                        if stage.is_dir():
                            for candidate_seat in sorted(stage.iterdir()):
                                seat = regular(candidate_seat)
                                if seat.is_dir() and seat.name not in {
                                        'scratch', 'workspaces', 'experiments', 'native-agent', 'host'}:
                                    paths.append(seat/'usage.json')
                # No manifests, configs, journals or receipt-supplied paths.
                for path in paths:
                    try:
                        snapshot = regular(regular(path.parent)/'usage.json')
                        if not snapshot.is_file():
                            continue
                        value = read_json(snapshot, max_bytes=32*1024*1024)
                        if not isinstance(value, dict) or value.get('schema') != 'tare-native-usage/1':
                            raise ValueError('Invalid usage snapshot.')
                        snapshots.append(snapshot)
                    except (OSError, ValueError, TypeError, AttributeError):
                        source_warnings += 1
            except (OSError, ValueError, TypeError):
                source_warnings += 1
        if snapshots:
            report = collect(sources + snapshots, include_work_metadata=True)
        if include_behavioral_profiles and audit_observations:
            # A compact snapshot, not an instruction to open artifact_file/session.
            # Use only explicitly resolved owner lineage, never a title/time join.
            for item in audit_observations:
                item['execution_origin_run_id'] = runs.get(item['id'], {}).get('execution_origin_run_id')
            report['saved_audit_observations'] = audit_observations
        if include_behavioral_profiles:
            report['saved_delivery_observations'] = delivery_observations
        warning_count = source_warnings + len(report['warnings'])
        if not report['work_runs'] and warning_count:
            return [unavailable]
        result = completed_work_review(report)
        lines = ['Scope: saved control-owner Work receipts (can span projects).',
                 f"{result['selected_count']}/{result['target_count']} unique executions · {result['cohort_status']}",
                 f"Remaining: {result['remaining']} · excluded records: {result['excluded_record_count']}",
                 f"Eligible: {result['eligible_execution_count']} · not completed: {result['not_completed_execution_count']}"]
        lines.append(f'Optional source warnings: {warning_count}')
        def known(value):
            return str(value) if value is not None else 'Unknown'
        for item in result['rows']:
            models = sorted({c['observed_model'] for c in item['attributed_calls'] if c['observed_model']})
            requested = sorted({c['requested_model'] for c in item['attributed_calls'] if c['requested_model']})
            statuses = {}
            for attempt in item['attempts']:
                status = known(attempt['status'])
                statuses[status] = statuses.get(status, 0) + 1
            attempts = ', '.join(status+': '+str(count) for status, count in sorted(statuses.items()))
            lines.extend([
                f"Work {item['work_id']} · execution {item['execution_origin_run_id']}",
                'Recorded project: '+known(item['repository_root'])+' · observed model: '+(', '.join(models) or 'Unknown')
                +' · requested model: '+(', '.join(requested) or 'Unknown'),
                f"Attempts: {len(item['attempts'])} ({attempts})",
                f"Attributed calls: {item['recorded_attributed_call_count']} · coverage: {item['coverage']}"
                f" · missing usage receipts: {item['usage_receipts_missing']} · attributed calls with unknown cost: {item['unknown_cost_calls']}",
                'Recorded input tokens: '+known(item['recorded_input_tokens'])
                +' · recorded output tokens: '+known(item['recorded_output_tokens']),
                'Reported USD: '+known(item['reported_cost_usd'])+' · additional estimated USD (not billed): '
                +known(item['estimated_cost_usd'])+' · full call count: Unknown · full cost: Unknown'])
        lines.extend(['Recorded settlement is closure, not a general quality verdict; code quality and independent audit: Unknown.',
                      'Subscription quota, control/tool/infrastructure and human costs: Unknown.',
                      'Explicit call attribution is not full coverage. Saved usage snapshots are read; no live journals/config files, provider calls or receipt writes.',
                      'Fewer than ten deliveries is a partial cohort, never authorization or a block on further Work.'])
        rows = [{'id': 'completed-work-review', 'label': 'Completed Work review · '+result['cohort_status'],
                 'body': '\n'.join(lines)}]
        if include_provider_pilot:
            try:
                from scripts.round_table.report_round_table_telemetry import provider_pilot_review
                # Reuse the same loaded report and closed snapshots; no new discovery.
                pilot = provider_pilot_review(report)
                details = [f"{pilot['selected_count']}/5 distinct settled Work candidates · descriptive only",
                           f"Window: {pilot['window_execution_count']}/{pilot['window_limit']} completed executions · additional candidates needed: {pilot['remaining']}",
                           f'Optional source warnings: {warning_count}',
                           f"Excluded records: {pilot['source_review']['excluded_record_count']}",
                           'Qualification / representativeness / provider equivalence: Unknown.',
                           'Settlement and recorded verdicts are not quality, independence, preference or authorization.']
                labels = {'repository_root': 'Recorded project', 'runtime_identity': 'Requested runtime',
                          'task_class': 'Declared task class', 'recorded_audit_outcome': 'Recorded review verdicts'}
                for item in pilot['rows']:
                    details.append(f"Work {item['work_id']} · train {item['train_id']} · "
                                   + ' · '.join(label+': '+known(item[key]) for key, label in labels.items()))
                for key, counts in pilot['metadata_counts'].items():
                    details.append(f"{labels[key]}: known attempts {counts['known_attempts']} · missing attempts {counts['missing_attempts']} · conflicting Works {counts['conflicting_works']}")
                details.extend(['Usage/cost gaps remain as recorded in Completed Work review; costs are not additive.',
                                'No vendor inferred from model/alias. Five candidates do not qualify a cohort.',
                                'Next: review ordinary Work results across providers and record why five comparable Works are representative.'])
                rows.append({'id': 'provider-pilot-review', 'label': 'Provider pilot',
                             'body': '\n'.join(details)})
            except Exception:
                rows.append({'id': 'provider-pilot-review', 'label': 'Provider pilot · unavailable',
                             'body': 'Optional descriptive view unavailable. Other usage and Works are unchanged.'})
        if include_behavioral_profiles:
            rows.append(_behavioral_profile_row(report, baseline))
        return rows
    except Exception:
        # Audit citations are independent of ordinary owner receipt custody.
        return [unavailable] + ([_behavioral_profile_row({}, baseline)]
                                if include_behavioral_profiles else [])

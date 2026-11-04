"""Small, reconstructible joins of owner deliveries, audit claims and recorded use.

The only write is an explicit operator review in the existing closed run receipt.
No citations are opened and no provider, ranking, preference or admission is used.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from compute_plane.native_journal import custody_lock, read_json, regular, save_json

SCHEMA = 'tare.tools/delivery-review/1'
WITHDRAWN_SCHEMA = 'tare.tools/delivery-review/2'
DISPOSITIONS = ('unresolved', 'confirmed', 'corrected', 'rejected')
CAUSES = ('unknown', 'implementation', 'tooling', 'guidance', 'external')


def text(value):
    return value if (type(value) is str and value.strip() and len(value) <= 1024
                     and value.isprintable()) else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False).encode()).hexdigest()


def subject(record):
    audit = record.get('audit') if isinstance(record.get('audit'), dict) else {}
    observation = audit.get('observation') if isinstance(audit.get('observation'), dict) else {}
    candidate = text(record.get('candidate_commit'))
    candidate = candidate if candidate and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', candidate) else None
    return {**{k: text(record.get(k)) for k in ('run_id', 'work_id', 'repository_root')},
            'candidate_commit': candidate,
            'audit_artifact_sha256': text(observation.get('artifact_sha256'))}


def capture(receipt):
    """Whitelist a saved owner receipt; paths remain declarations, never instructions."""
    identity = subject(receipt)
    runtime = receipt.get('runtime_observation')
    runtime = runtime if isinstance(runtime, dict) else {}
    observation = {
        **identity, 'token': digest(receipt),
        'audit_recorded': isinstance((receipt.get('audit') or {}).get('observation'), dict)
                          if isinstance(receipt.get('audit'), dict) else False,
        'runtime': ({k: text(runtime.get(k)) for k in
                     ('runtime_version', 'requested_model', 'protocol', 'deployment_id', 'effort')}
                    if runtime.get('schema') == 'tare.tools/work-runtime-observation/1' else {}),
        'review': receipt.get('delivery_review'),
    }
    return observation


def validate_review(value, expected, findings):
    """A review records operator assertions; it does not independently attest them."""
    if (not isinstance(value, dict) or value.get('schema') not in (SCHEMA, WITHDRAWN_SCHEMA)
            or value.get('subject') != expected):
        raise ValueError('This review belongs to another delivery or audit. Reopen Usage.')
    if value['schema'] == WITHDRAWN_SCHEMA and 'withdrawal' not in value:
        raise ValueError('The withdrawn review metadata is missing. Reopen Usage.')
    result = {'schema': value['schema'], 'subject': expected}
    for key in ('accepted', 'material_rework'):
        item = value.get(key)
        if item is not None and type(item) is not bool:
            raise ValueError('Delivery outcome and rework must be yes, no or unknown.')
        result[key] = item
    reference = text(value.get('reference'))
    if not reference:
        raise ValueError('Add a short reference explaining this review (up to 1024 characters).')
    result['reference'] = reference
    decisions = value.get('findings', [])
    if not isinstance(decisions, list) or len(decisions) != len(findings):
        raise ValueError('The audit findings changed. Reopen Usage.')
    result['findings'] = []
    for index, (decision, finding) in enumerate(zip(decisions, findings)):
        if (not isinstance(decision, dict) or decision.get('index') != index
                or decision.get('disposition') not in DISPOSITIONS or decision.get('cause') not in CAUSES):
            raise ValueError('Choose a recorded decision and cause for each finding.')
        correction = text(decision.get('correction_reference'))
        if decision['disposition'] == 'corrected' and not correction:
            raise ValueError('A corrected finding needs a correction reference.')
        if result['accepted'] is True and decision['disposition'] == 'confirmed':
            raise ValueError('A confirmed unresolved defect conflicts with acceptance of this candidate.')
        result['findings'].append({'index': index, 'disposition': decision['disposition'],
                                  'cause': decision['cause'], 'correction_reference': correction})
    # Presence, not validity, excludes an assertion. Corrupt metadata must never
    # restore a positive signal or introduce arbitrary payloads into the view.
    if 'withdrawal' in value:
        metadata = value['withdrawal']
        result['withdrawal'] = {
            key: text(metadata.get(key)) if isinstance(metadata, dict) else None
            for key in ('reason', 'recorded_by', 'original_schema')}
    return result


def active_review(value):
    return value if value and 'withdrawal' not in value else None


def withdrawal_state(value):
    if not value or 'withdrawal' not in value:
        return None
    metadata = value['withdrawal']
    reason = text(metadata.get('reason')) if isinstance(metadata, dict) else None
    return 'Review withdrawn; excluded from quality counts. Reason: '+(reason or 'Unknown (invalid withdrawal metadata).')


def withdraw_review(control_root, run_id, token, reason):
    """Exclude the saved assertion without copying or deleting its sources."""
    return record_review(control_root, run_id, token, None, withdrawal_reason=reason)


def record_review(control_root, run_id, token, draft, *, withdrawal_reason=None):
    """Use the Work writer lock and an exact preview token; no arbitrary receipt path."""
    from . import relay_mesh, work_run
    control = regular(Path(control_root))
    if not control.is_absolute() or control != regular(Path(relay_mesh.RELAY_ROOT)):
        raise ValueError('Reopen Usage for the active control owner.')
    if type(run_id) is not str or not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Invalid saved run identity.')
    path = regular(regular(control.parent/'.reconciliation'/'work-runs'/run_id)/'run.json')
    before = read_json(path, max_bytes=32*1024*1024)
    if before.get('schema') != 'tare.tools/work-run/1' or before.get('run_id') != run_id:
        raise ValueError('The saved Work identity changed. Reopen Usage.')
    lock = regular(work_run.lock_root_for(before['work_id'], graph_path=before.get('graph_path')))
    lock.mkdir(parents=True, exist_ok=True)
    with custody_lock(lock):
        receipt = read_json(path, max_bytes=32*1024*1024)
        if digest(receipt) != token or not receipt.get('ended_at'):
            raise ValueError('The delivery changed or is still running. Reopen Usage.')
        expected = subject(receipt)
        if not all(expected[k] for k in ('run_id', 'work_id', 'repository_root', 'candidate_commit')):
            raise ValueError('This run has no recorded candidate to review.')
        from .behavioral_profiles import project_work_audits
        audit = receipt.get('audit') or {}
        observations = ([{'id': run_id, 'work_id': receipt['work_id'], 'observation': audit['observation']}]
                        if isinstance(audit, dict) and 'observation' in audit else [])
        projected = project_work_audits(observations)
        if projected.get('invalid_record_count') or projected.get('status') != 'available':
            raise ValueError('The saved audit is invalid. Inspect it before recording a review.')
        findings = projected['reviews'][0]['findings'] if projected['reviews'] else []
        if draft is None:
            reason = text(withdrawal_reason)
            if not reason:
                raise ValueError('Add a short withdrawal reason (up to 1024 characters).')
            saved = receipt.get('delivery_review')
            validate_review(saved, expected, findings)
            if 'withdrawal' in saved:
                raise ValueError('This review is already excluded. Save a review explicitly to replace it.')
            # Keep every original assertion, reference and attribution in place.
            # Old readers reject v2 rather than ignore the withdrawal and count
            # the original positive assertion after a product rollback.
            value = dict(saved, schema=WITHDRAWN_SCHEMA, withdrawal={
                'reason': reason, 'recorded_by': 'operator_tui', 'original_schema': saved['schema']})
        else:
            value = validate_review(draft, expected, findings)
            value.pop('withdrawal', None)  # Only an explicit save reactivates.
            value['schema'] = SCHEMA
            value.update(recorded_at=datetime.now(timezone.utc).isoformat(), recorded_by='operator_tui')
        if len(json.dumps(value).encode()) > 16*1024:
            raise ValueError('Review exceeds 16 KiB. Shorten its references.')
        receipt['delivery_review'] = value
        save_json(path, receipt, max_bytes=32*1024*1024)
    return capture(receipt)


def usage(calls, coverage):
    result = {'call_intents': len(calls), 'attribution_coverage': coverage}
    for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'duration_ms'):
        values = [c.get(key) for c in calls]
        known = [v for v in values if type(v) in (int, float) and 0 <= v <= 10**15]
        result[key] = {'recorded_sum': sum(known) if known else None,
                       'unknown_calls': len(values)-len(known)}
    amounts = []
    for call in calls:
        raw = call.get('reported_cost_usd')
        if raw is None:
            raw = call.get('estimated_cost_usd')
        try:
            amount = Decimal(str(raw))
            amounts.append(amount if amount.is_finite() and amount >= 0 else None)
        except (InvalidOperation, ValueError):
            amounts.append(None)
    known = [v for v in amounts if v is not None]
    result.update(recorded_model_cost_usd=str(sum(known)) if known else None,
                  unpriced_calls=len(amounts)-len(known), full_cost_usd=None,
                  cost_scope='Recorded model charges/estimates only; human and infrastructure cost unknown.')
    return result


def project(report):
    """Join exact owner run/candidate identities, preserving failed attempts and gaps."""
    from .behavioral_profiles import _unique_records, project_work_audits
    from scripts.round_table.report_round_table_telemetry import completed_work_review
    unavailable = {'status': 'unavailable', 'deliveries': [], 'groups': [], 'review_targets': []}
    try:
        records, conflicts, invalid = _unique_records(
            [dict(r, id=r.get('run_id')) for r in report['saved_delivery_observations']], 128)
        owners = {r['run_id']: r for r in report.get('work_runs', [])}
        audits = project_work_audits(report.get('saved_audit_observations', []))
        audit_map = {r['id']: r for r in audits['reviews']}
        linked, targets = {}, []
        for record in records:
            run = owners.get(record.get('run_id'), {})
            if (not run.get('execution_origin_run_id') or not run.get('ended_at')
                    or any(record.get(a) != run.get(b) for a, b in
                           (('work_id', 'work_id'), ('repository_root', 'repository_root'),
                            ('candidate_commit', 'candidate_commit')))):
                invalid += 1
                continue
            audit = audit_map.get(run['run_id'])
            if audit and (audit.get('source_revision') != run.get('candidate_commit')
                          or audit.get('project') != run.get('repository_root')
                          or audit.get('work_id') != run.get('work_id')
                          or audit.get('execution_origin_run_id') != run['execution_origin_run_id']
                          or audit.get('artifact_sha256') != record.get('audit_artifact_sha256')):
                audit = None
                invalid += 1
            audit_gap = record.get('audit_recorded') is True and audit is None
            expected = {k: record.get(k) for k in
                        ('run_id', 'work_id', 'repository_root', 'candidate_commit', 'audit_artifact_sha256')}
            review = None
            if record.get('review') is not None and not audit_gap:
                try:
                    review = validate_review(record['review'], expected, audit['findings'] if audit else [])
                except ValueError:
                    invalid += 1
            entry = {'run_id': run['run_id'], 'candidate_commit': run.get('candidate_commit'),
                     'runtime': record.get('runtime') or {}, 'audit': audit, 'review': review,
                     'audit_link_missing': audit_gap}
            linked[run['run_id']] = entry
            if not audit_gap and all(expected[k] for k in ('run_id', 'work_id', 'repository_root', 'candidate_commit')):
                targets.append({**entry, 'work_id': run['work_id'], 'subject': expected,
                                'token': record.get('token'), 'ended_at': run['ended_at']})
        deliveries, grouped = [], {}
        for execution in completed_work_review(report)['rows']:
            attempts, gaps = [], []
            dimensions = {k: set() for k in ('project', 'task_class', 'runtime_version', 'effort', 'protocol',
                                            'requested_model', 'observed_model')}
            for attempt in execution['attempts']:
                entry = linked.get(attempt['run_id'])
                if entry is None:
                    gaps.append('delivery_snapshot_missing')
                    continue
                calls = [c for c in execution['attributed_calls'] if c['work_run_id'] == attempt['run_id']
                         and c['work_stage'] == 'implementer']
                models = {key: sorted({text(c.get(key)) for c in calls} - {None})
                          for key in ('requested_model', 'observed_model')}
                attempts.append({**entry, 'implementer_models': models})
                if entry['audit_link_missing']:
                    gaps.append('audit_link_missing')
                if not calls:
                    continue
                for key, value in (('project', text(attempt.get('repository_root'))),
                                   ('task_class', text(attempt.get('task_class')))):
                    dimensions[key].add(value)
                runtime = entry['runtime']
                for key in ('runtime_version', 'effort', 'protocol'):
                    dimensions[key].add(text(runtime.get(key)))
                for key in models:
                    dimensions[key].update(text(c.get(key)) for c in calls)
                if runtime.get('requested_model') not in models['requested_model']:
                    gaps.append('runtime_call_identity_mismatch')
            for key, values in dimensions.items():
                if len(values) != 1 or None in values:
                    gaps.append(key+'_missing_or_mixed')
            if execution['coverage'] != 'recorded':
                gaps.append('usage_attribution_partial')
            settled = [a for a in attempts if a['run_id'] in execution['settled_run_ids']]
            final = settled[-1] if len(settled) == 1 else None
            review = active_review(final['review']) if final else None
            clean = None
            if review:
                unresolved = any(f['disposition'] == 'unresolved' for f in review['findings'])
                previous = [a for a in attempts if a is not final and a['candidate_commit']]
                prior_rework = any(active_review(a['review']) and a['review']['material_rework'] is True for a in previous)
                prior_unknown = any(not active_review(a['review']) or a['review']['material_rework'] is None for a in previous)
                if review['accepted'] is False or review['material_rework'] is True or prior_rework:
                    clean = False
                elif (review['accepted'] is True and review['material_rework'] is False
                      and not unresolved and not prior_unknown):
                    clean = True
            confirmed = {cause: 0 for cause in CAUSES}
            unresolved = 0
            for attempt in attempts:
                audit, assessment = attempt['audit'], active_review(attempt['review'])
                for index, finding in enumerate(audit['findings'] if audit else []):
                    decision = assessment['findings'][index] if assessment else None
                    if decision and decision['disposition'] in ('confirmed', 'corrected'):
                        confirmed[decision['cause']] += 1
                    elif not decision or decision['disposition'] == 'unresolved':
                        unresolved += 1
            row = {'work_id': execution['work_id'], 'execution_origin_run_id': execution['execution_origin_run_id'],
                   'attempts': attempts, 'attempt_count': len(execution['attempts']),
                   'accepted_without_material_rework': clean, 'confirmed_findings_by_cause': confirmed,
                   'unresolved_findings': unresolved,
                   'usage': usage(execution['attributed_calls'], execution['coverage']),
                   'comparison_gaps': sorted(set(gaps))}
            deliveries.append(row)
            if not gaps:
                key = tuple(next(iter(values)) for values in dimensions.values())
                grouped.setdefault(key, []).append(row)
        groups = []
        for key, rows in sorted(grouped.items()):
            groups.append({'scope': dict(zip(dimensions, key)), 'deliveries': len(rows),
                'accepted_without_material_rework': {
                    'yes': sum(r['accepted_without_material_rework'] is True for r in rows),
                    'no': sum(r['accepted_without_material_rework'] is False for r in rows),
                    'unknown': sum(r['accepted_without_material_rework'] is None for r in rows)},
                'confirmed_findings_by_cause': {c: sum(r['confirmed_findings_by_cause'][c] for r in rows) for c in CAUSES},
                'unresolved_findings': sum(r['unresolved_findings'] for r in rows),
                'recorded_use': combine_usage([r['usage'] for r in rows])})
        return {'status': 'available', 'deliveries': deliveries, 'groups': groups,
                'review_targets': sorted(targets, key=lambda r: (r['ended_at'], r['run_id']), reverse=True),
                'conflicting_ids': conflicts, 'invalid_record_count': invalid,
                'model_preference': None, 'full_cost_usd': None}
    except (KeyError, TypeError, ValueError, AttributeError):
        return unavailable


def combine_usage(rows):
    """Partial sums retain missingness; never turn a sum of known prices into full cost."""
    result = {key: sum(r[key] for r in rows) for key in ('call_intents', 'unpriced_calls')}
    for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'duration_ms'):
        known = [r[key]['recorded_sum'] for r in rows if r[key]['recorded_sum'] is not None]
        result[key] = {'recorded_sum': sum(known) if known else None,
                       'unknown_calls': sum(r[key]['unknown_calls'] for r in rows)}
    costs = [Decimal(r['recorded_model_cost_usd']) for r in rows if r['recorded_model_cost_usd'] is not None]
    result.update(recorded_model_cost_usd=str(sum(costs)) if costs else None, full_cost_usd=None)
    return result


def render(evidence):
    lines = ['Linked delivery evidence (ordinary managed Works; operator-recorded review).']
    if evidence.get('status') != 'available':
        return '\n'.join(lines + ['Optional delivery join unavailable. Other evidence is unchanged.'])
    lines.append(f"Linked completed executions: {len(evidence['deliveries'])}; comparison groups: {len(evidence['groups'])}.")
    lines.append('Compare within the same project and task class. Groups retain exact model, runtime and effort.')
    for group in evidence['groups']:
        lines.append('Comparable group: '+json.dumps(group, ensure_ascii=False))
    for row in evidence['deliveries']:
        lines.append(f"Delivery {row['work_id']} / {row['execution_origin_run_id']} · attempts: {row['attempt_count']}")
        lines.append('  Accepted without material rework: '+
                     {None: 'Unknown', True: 'Yes', False: 'No'}[row['accepted_without_material_rework']])
        lines.append('  Confirmed findings by cause: '+json.dumps(row['confirmed_findings_by_cause'])+
                     '; unresolved: '+str(row['unresolved_findings']))
        lines.append('  Recorded use, including attributed failed attempts and audits: '+json.dumps(row['usage']))
        lines.append('  Comparison gaps: '+', '.join(row['comparison_gaps']))
        for attempt in row['attempts']:
            state = withdrawal_state(attempt['review'])
            if state:
                lines.append('  '+state)
            lines.append('  Candidate / implementer / audit: '+json.dumps({
                'run_id': attempt['run_id'], 'revision': attempt['candidate_commit'],
                'models': attempt['implementer_models'], 'runtime': attempt['runtime'],
                'audit_artifact': attempt['audit']['artifact_sha256'] if attempt['audit'] else None,
                'review': attempt['review']}, ensure_ascii=False))
    lines.append('Operator assertions retain their references. Audit verdicts and CI alone do not establish quality.')
    lines.append('Unknown values remain unknown; direct sessions and controlled replays remain separate populations.')
    lines.append('Use Review delivery for an optional recorded decision. No automatic model preference is applied.')
    return '\n'.join(lines)

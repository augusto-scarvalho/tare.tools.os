"""Reconstructible, read-only FLOW-18 description; no preference or eligibility state.

Observable vocabulary (not an audit taxonomy): metadata_missing and
metadata_conflict are data gaps; implementer_identity_missing is a data gap;
audit_verdict_recorded is a recorded verdict, NOT an adjudicated audit finding.
Ordinary Work metadata does not establish adjudicated findings or independence.
Optional packaged source reviews describe only their recorded, bounded scope.
Ten recent executions (or test fixtures) do not establish a representative cohort.
"""
import json
from pathlib import Path
import re

from compute_plane.native_journal import read_json, regular

from scripts.round_table.report_round_table_telemetry import (
    WORK_METADATA, completed_work_review, declared_metadata,
)

FINDINGS = {
    'metadata_missing': 'Data gap: one or more attempt declarations absent.',
    'metadata_conflict': 'Data gap: exact declarations disagree across attempts.',
    'implementer_identity_missing': 'Data gap: implementer model evidence absent or incomplete.',
    'audit_verdict_recorded': 'Recorded verdict only; not an adjudicated audit finding or quality measure.',
}


def selected_baseline(deployment, deployment_id=None):
    """Whitelist configuration only. Never return auth, endpoints or arbitrary payloads."""
    if not isinstance(deployment, dict):
        return None
    profile = deployment.get('native_profile')
    if not isinstance(profile, dict):
        return None
    parameters = profile.get('parameters_json', {})
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except (ValueError, TypeError):
            parameters = {}
    reasoning = parameters.get('reasoning', {}) if isinstance(parameters, dict) else {}
    return {'requested_model': declared_metadata(profile.get('model')),
            'protocol': declared_metadata(profile.get('protocol')),
            'deployment_id': declared_metadata(deployment.get('id')) or declared_metadata(deployment_id),
            'effort': declared_metadata(reasoning.get('effort')) if isinstance(reasoning, dict) else None,
            'version': None}


def _comparison(calls, key, model):
    values = [declared_metadata(c.get(key)) for c in calls]
    agreement = sum(v == model for v in values if v is not None) if model else 0
    mismatch = sum(v != model for v in values if v is not None) if model else 0
    unknown = sum(v is None or model is None for v in values)
    return {'agreement': agreement, 'mismatch': mismatch, 'unknown': unknown,
            'status': 'mismatch' if mismatch else 'unknown' if unknown or not values else 'agreement'}


def _work_profile_review(report, baseline=None):
    """Pure bounded projection; filtering, closure and lineage belong to the owner.

    Models are used only from exact work_stage == 'implementer' claims. Older
    'implement' labels are deliberately not normalized. Runtime declarations are
    opaque exact strings, never parsed into guessed versions or effort.
    """
    try:
        base = completed_work_review(report, limit=10)
        # Re-whitelist even caller-supplied baselines; arbitrary fields never escape.
        baseline = ({key: declared_metadata(baseline.get(key)) for key in
                     ('requested_model', 'protocol', 'deployment_id', 'effort', 'version')}
                    if isinstance(baseline, dict) else None)
        rows = []
        counts = {key: {'known_attempts': 0, 'missing_attempts': 0,
                        'conflicting_executions': 0} for key in WORK_METADATA}
        for execution in base['rows']:
            metadata, findings = {}, []
            for key in WORK_METADATA:
                values = [declared_metadata(a.get(key)) for a in execution['attempts']]
                known = sorted(set(values) - {None})
                missing = sum(v is None for v in values)
                conflict = len(known) > 1
                metadata[key] = {'values': known, 'known_attempts': len(values)-missing,
                                 'missing_attempts': missing, 'conflict': conflict}
                counts[key]['known_attempts'] += len(values)-missing
                counts[key]['missing_attempts'] += missing
                counts[key]['conflicting_executions'] += int(conflict)
                if missing:
                    findings.append('metadata_missing')
                if conflict:
                    findings.append('metadata_conflict')
            if metadata['recorded_audit_outcome']['values']:
                findings.append('audit_verdict_recorded')
            calls = execution['attributed_calls']
            implementer = [c for c in calls if c.get('work_stage') == 'implementer']
            stages = {}
            for call in calls:
                stage = declared_metadata(call.get('work_stage')) or 'Unknown'
                stages[stage] = stages.get(stage, 0) + 1
            models = {key: sorted({declared_metadata(c.get(key)) for c in implementer} - {None})
                      for key in ('requested_model', 'observed_model')}
            if not implementer or any(not declared_metadata(c.get(key)) for c in implementer
                                      for key in models):
                findings.append('implementer_identity_missing')
            model = baseline.get('requested_model') if baseline else None
            rows.append({'work_id': execution['work_id'],
                         'execution_origin_run_id': execution['execution_origin_run_id'],
                         'attempts': execution['attempts'], 'metadata': metadata,
                         'implementer_models': models,
                         'model_call_usage': {'count': len(calls), 'by_stage': stages,
                                              'coverage': execution['coverage']},
                         'comparison': {key: _comparison(implementer, key, model) for key in models},
                         'runtime_version': None, 'runtime_effort': None,
                         'reported_cost_usd': execution['reported_cost_usd'],
                         'additional_estimated_cost_usd': execution['estimated_cost_usd'],
                         'full_cost_usd': None, 'quality': None, 'independence': None,
                         'adjudicated_audit_findings': None, 'findings': sorted(set(findings))})
        return {'status': 'available', 'completed_execution_count': len(rows),
                'excluded_record_count': base['excluded_record_count'],
                'not_completed_execution_count': base['not_completed_execution_count'],
                'metadata_coverage': counts, 'baseline': baseline, 'rows': rows,
                'representative_quality_cohort': None, 'model_preference': None}
    except Exception:
        return {'status': 'unavailable', 'rows': [], 'baseline': None}


AUDIT_SCHEMA = 'tare.tools/behavioral-audit-evidence/1'
AUDIT_MAX_BYTES = 256 * 1024
AUDIT_VOCABULARY = {
    'correctness': 'Recorded functional obligation assertion, not a universal score.',
    'scope_discipline': 'Recorded scope assertion, not a universal score.',
    'test_integrity': 'Recorded test integrity assertion, not a universal score.',
    'incomplete_delivery': 'Recorded delivery gap, not necessarily a code defect.',
}


def load_behavioral_evidence():
    """Read only the single adjacent packaged artifact; citations are never opened."""
    try:
        return read_json(regular(Path(__file__).with_name('behavioral_evidence.json')),
                         max_bytes=AUDIT_MAX_BYTES)
    except Exception:
        return {'schema': None}  # Optional absence/malformed data, not a valid empty cohort.


def _audit_string(value, limit=2048):
    # Reject rather than truncate: exact identities and claims must not be rewritten.
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _audit_hash(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) else None


def _unique_records(records, limit):
    """Conflicting identities are all excluded; identical repeats count once."""
    if not isinstance(records, list) or len(records) > limit:
        raise ValueError('Invalid bounded record array')
    unique, conflicts, invalid = {}, set(), 0
    for row in records:
        identity = _audit_string(row.get('id'), 256) if isinstance(row, dict) else None
        if identity is None:
            invalid += 1
        elif identity in unique and unique[identity] != row:
            conflicts.add(identity)
        else:
            unique[identity] = row
    return [r for k, r in unique.items() if k not in conflicts], sorted(conflicts), invalid


def _audit_comparison(rows, baseline, key='requested_model'):
    model = _audit_string(baseline.get('requested_model')) if isinstance(baseline, dict) else None
    values = [r.get(key) for r in rows]
    agreement = sum(v == model for v in values if v is not None) if model is not None else 0
    mismatch = sum(v != model for v in values if v is not None) if model is not None else 0
    unknown = sum(v is None or model is None for v in values)
    return {'agreement': agreement, 'mismatch': mismatch, 'unknown': unknown,
            'status': 'mismatch' if mismatch else 'unknown' if unknown or not values else 'agreement'}


def project_audit_evidence(evidence, baseline=None):
    """Pure whitelist of derived citations, never a decision store or current attestation.

    Array/string limits reject oversized input without discarding ordinary Work data.
    Missing or ill-typed observations stay None (Unknown), never a passing assertion.
    """
    unavailable = {'status': 'unavailable', 'reviews': [], 'findings': [], 'sources': []}
    try:
        if not isinstance(evidence, dict) or evidence.get('schema') != AUDIT_SCHEMA:
            return unavailable
        sources, conflicts, invalid = _unique_records(evidence.get('sources'), 64)
        source_map = {}
        for source in sources:
            projected = {k: _audit_string(source.get(k)) for k in ('id', 'path', 'revision')}
            projected['sha256'] = _audit_hash(source.get('sha256'))
            if not all(projected[k] for k in ('id', 'path', 'sha256')):
                invalid += 1
                continue
            source_map[projected['id']] = projected
        review_input, finding_input = evidence.get('reviews'), evidence.get('findings')
        if not isinstance(review_input, list) or not isinstance(finding_input, list):
            return unavailable
        # A record identity is unique across both regimes, not merely within a list.
        records, record_conflicts, bad = _unique_records(review_input + finding_input, 256)
        invalid += bad
        conflicts += record_conflicts
        reviews, findings = [], []
        strings = ('id', 'original_id', 'regime', 'project', 'work_id', 'execution_id',
                   'source_revision', 'requested_model', 'observed_model', 'runtime_version',
                   'effort', 'protocol', 'treatment', 'auditor', 'source_id', 'source_pointer',
                   'reason', 'claim', 'category', 'historical_disposition', 'current_status',
                   'verification_source_id', 'verification_verdict', 'model_observation_source_id')
        booleans = ('functional_obligation_passed', 'unjustified_scope',
                    'material_corrective_required', 'accepted', 'incomplete_delivery',
                    'test_integrity', 'independence')
        for record in records:
            row = {k: _audit_string(record.get(k)) for k in strings}
            if row['source_id'] not in source_map or row['source_pointer'] is None:
                invalid += 1
                continue
            verification = row['verification_source_id']
            if record.get('verification_source_id') is not None and verification not in source_map:
                invalid += 1
                continue
            observed_source = row['model_observation_source_id']
            if record.get('model_observation_source_id') is not None and observed_source not in source_map:
                invalid += 1
                continue
            for key in ('candidate_sha256', 'verification_result_sha256'):
                row[key] = _audit_hash(record.get(key))
            for key in booleans:
                row[key] = record.get(key) if type(record.get(key)) is bool else None
            for key in ('model_calls', 'reported_total_tokens', 'model_observation_count'):
                value = record.get(key)
                row[key] = value if type(value) is int and 0 <= value <= 10**12 else None
            value = record.get('full_cost_usd')
            row['full_cost_usd'] = (value if type(value) in (int, float)
                                    and 0 <= value <= 10**12 else None)
            row['source'] = source_map[row['source_id']]
            row['verification_source'] = source_map.get(verification)
            row['model_observation_source'] = source_map.get(observed_source)
            if row['regime'] == 'controlled_replay' and record in review_input:
                row['baseline_requested_model_comparison'] = _audit_comparison([row], baseline)
                row['baseline_observed_model_comparison'] = _audit_comparison([row], baseline, 'observed_model')
                reviews.append(row)
            elif (row['regime'] == 'historical_audit' and record in finding_input
                  and row['category'] in AUDIT_VOCABULARY
                  and row['historical_disposition'] in ('confirmed_corrected', 'unreviewed')
                  and row['original_id'] and row['claim']):
                findings.append(row)
            else:
                invalid += 1
        treatments = {}
        for treatment in ('A', 'B'):
            rows = [r for r in reviews if r['treatment'] == treatment]
            summary = {'review_count': len(rows)}
            for key in ('accepted', 'incomplete_delivery'):
                summary[key] = {'true': sum(r[key] is True for r in rows),
                                'false': sum(r[key] is False for r in rows),
                                'unknown': sum(r[key] is None for r in rows)}
            for key in ('model_calls', 'reported_total_tokens'):
                known = [r[key] for r in rows if r[key] is not None]
                summary[key] = {'recorded_total': sum(known) if known else None,
                                'known_reviews': len(known), 'unknown_reviews': len(rows)-len(known)}
            summary['baseline_requested_model_comparison'] = _audit_comparison(rows, baseline)
            summary['baseline_observed_model_comparison'] = _audit_comparison(rows, baseline, 'observed_model')
            treatments[treatment] = summary
        return {'status': 'available', 'sources': list(source_map.values()),
                'reviews': reviews, 'findings': findings, 'vocabulary': AUDIT_VOCABULARY.copy(),
                'regime_counts': {'controlled_replay': len(reviews), 'historical_audit': len(findings)},
                'historical_disposition_counts': {d: sum(r['historical_disposition'] == d for r in findings)
                    for d in ('confirmed_corrected', 'unreviewed')},
                'controlled_treatments': treatments, 'conflicting_ids': sorted(set(conflicts)),
                'invalid_record_count': invalid}
    except Exception:
        return unavailable


def behavioral_profile_review(report, baseline=None, audit_evidence=None):
    """Add independently readable source evidence; omitted evidence preserves old output."""
    review = _work_profile_review(report, baseline)
    if audit_evidence is not None:
        review['audit_evidence'] = project_audit_evidence(audit_evidence, baseline)
    if isinstance(report, dict) and 'saved_audit_observations' in report:
        review['work_audits'] = project_work_audits(report['saved_audit_observations'])
    if isinstance(report, dict) and 'saved_delivery_observations' in report:
        from .delivery_evidence import project
        review['delivery_evidence'] = project(report)
    return review


def project_work_audits(observations):
    """Recorded Work audit claims, without inferring acceptance, model or adjudication."""
    try:
        records, conflicts, invalid = _unique_records(observations, 128)
        result = []
        def sources(values):
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError('Invalid audit source list')
            rows = [{key: _audit_string(v.get(key)) for key in ('repository', 'path', 'sha256')}
                    for v in values if isinstance(v, dict)]
            if len(rows) != len(values) or any(not all(v.values()) or not _audit_hash(v['sha256']) for v in rows):
                raise ValueError('Invalid audit source')
            return rows
        for record in records:
            observation = record.get('observation')
            if (not isinstance(observation, dict)
                    or observation.get('schema') != 'tare.tools/work-audit-observation/1'
                    or not _audit_hash(observation.get('artifact_sha256'))
                    or not _audit_string(record.get('work_id'))
                    or observation.get('verdict') not in ('ACCEPT', 'REVISE', 'INCONCLUSIVE')):
                invalid += 1
                continue
            try:
                raw_findings = observation.get('findings')
                if not isinstance(raw_findings, list) or len(raw_findings) > 32:
                    raise ValueError('Invalid finding array')
                findings = []
                for finding in raw_findings:
                    claim = _audit_string(finding.get('claim')) if isinstance(finding, dict) else None
                    if not claim:
                        raise ValueError('Missing finding claim')
                    cited = sources(finding.get('sources'))
                    if not cited:
                        raise ValueError('Finding without cited source')
                    findings.append({'claim': claim, 'sources': cited, 'adjudication': None})
                if observation['verdict'] == 'ACCEPT' and findings:
                    raise ValueError('Contradictory audit acceptance')
                result.append({**{key: _audit_string(record.get(key)) for key in
                                  ('id', 'work_id', 'execution_origin_run_id', 'project', 'source_revision', 'auditor')},
                    'artifact_sha256': observation['artifact_sha256'],
                    'recorded_verdict': observation['verdict'],
                    'summary': _audit_string(observation.get('summary')),
                    'findings': findings, 'sources': sources(observation.get('sources')),
                    'implementer_model': None, 'quality': None, 'independence': None})
            except (ValueError, TypeError, AttributeError):
                invalid += 1
        return {'status': 'available', 'reviews': result, 'conflicting_ids': conflicts,
                'invalid_record_count': invalid}
    except Exception:
        return {'status': 'unavailable', 'reviews': []}


def _render_audit_evidence(evidence):
    def unknowns(value):
        if isinstance(value, dict):
            return {k: unknowns(v) for k, v in value.items()}
        if isinstance(value, list):
            return [unknowns(v) for v in value]
        return 'Unknown' if value is None else value
    def text(value):
        return json.dumps(unknowns(value), ensure_ascii=False)
    lines = ['Source-backed audit evidence (derived, bounded starting slice; not canonical decisions).',
             'Controlled results are not ordinary Work completions or delivery metadata gaps.',
             'Historical corrected dispositions do not attest to current source bytes; current status remains as recorded.',
             'Auditor is not implementer; requested model is not observed model. No vendor-quality comparison.']
    if evidence.get('status') != 'available':
        return '\n'.join(lines + ['Optional audit evidence unavailable; ordinary Work data is unchanged.'])
    for key in ('regime_counts', 'historical_disposition_counts'):
        lines.append(key+': '+text(evidence[key]))
    for treatment, row in evidence['controlled_treatments'].items():
        lines.append('Controlled '+treatment+': '+text(row))
    for key in ('vocabulary', 'conflicting_ids', 'invalid_record_count'):
        lines.append(key+': '+text(evidence[key]))
    lines.append('Recorded assertions/dispositions only, not universal scores; absent independence/full cost/version: Unknown.')
    for key in ('reviews', 'findings'):
        rows = evidence[key]
        for row in rows[:24]:
            identity = ('id', 'project', 'work_id', 'execution_id', 'source_revision',
                        'requested_model', 'observed_model', 'effort', 'auditor')
            lines.append(key+': '+text({k: row[k] for k in identity}))
            values = (('accepted', 'incomplete_delivery', 'functional_obligation_passed',
                       'unjustified_scope', 'material_corrective_required', 'test_integrity')
                      if key == 'reviews' else ('original_id', 'category', 'historical_disposition', 'current_status'))
            lines.append('  Recorded: '+text({k: row[k] for k in values}))
            lines.append('  '+str(row['reason'] if key == 'reviews' else row['claim']))
            lines.append('  Source: '+str(row['source_id'])+'#'+str(row['source_pointer'])
                         +' sha256='+str(row['source']['sha256']))
        lines.append(f'{key} omitted detail count: {max(0, len(rows)-24)} (full provenance in structured result).')
    return '\n'.join(lines)


def render_behavioral_profiles(review):
    """Brief text; structured values retain exact declarations and unknowns."""
    if review.get('status') != 'available':
        message = 'Behavioral profiles unavailable. Other Usage rows and Work eligibility are unchanged.'
        if 'audit_evidence' in review:
            message += '\n'+_render_audit_evidence(review['audit_evidence'])
        return message
    def text(value):
        # JSON null denotes Unknown; never rewrite substrings in exact identities.
        return 'Unknown' if value is None else json.dumps(value, ensure_ascii=False)
    lines = [f"Completed Work executions: {review['completed_execution_count']}/10 (not model calls).",
             f"Excluded records: {review['excluded_record_count']} · not completed: {review['not_completed_execution_count']}",
             'Coverage: '+text(review['metadata_coverage']),
             'Selected baseline configuration: '+text(review['baseline']),
             'Descriptive configuration comparison only; not a quality preference or equivalence judgment.',
             'Version, effort and lineage are not normalized; current configuration never fills historical evidence.']
    if 'delivery_evidence' in review:
        from .delivery_evidence import render
        lines.insert(1, render(review['delivery_evidence']))
    if 'audit_evidence' in review:
        lines.append(_render_audit_evidence(review['audit_evidence']))
        lines.append('Ordinary Work delivery metadata / gaps (separate from source reviews):')
    if 'work_audits' in review:
        audits = review['work_audits']
        lines.append('Saved Work audit claims (recorded, not adjudicated): '+text(audits))
    for row in review['rows']:
        lines.extend([f"Work {row['work_id']} · execution {row['execution_origin_run_id']}",
                      'Recorded declarations: '+text(row['metadata']),
                      'Attempts / lineage: '+text([{k: a.get(k) for k in
                          ('run_id', 'status', 'resumed_from', 'lineage')} for a in row['attempts']]),
                      'Implementer models: '+text(row['implementer_models']),
                      'Exact baseline comparison: '+text(row['comparison']),
                      'Model-call usage (audit and other stages separate): '+text(row['model_call_usage']),
                      ('Runtime version: Unknown · runtime effort: Unknown' if 'delivery_evidence' not in review
                       else 'Version and effort observations are shown per controller attempt above.'),
                      'Reported USD: '+text(row['reported_cost_usd'])+
                      ' · additional estimate (not billed): '+text(row['additional_estimated_cost_usd']),
                      'Gaps / recorded verdicts: '+text({k: FINDINGS[k] for k in row['findings']})])
    lines.extend([('Full cost and independent model-quality qualification remain Unknown; operator reviews are shown above.'
                  if 'delivery_evidence' in review else
                  'Full cost, quality, adjudicated audit findings and independence: Unknown.'
                  if 'audit_evidence' not in review else
                  'Ordinary Work full cost, quality, adjudicated audit findings and independence: Unknown.'),
                  'SETTLED / ACCEPT / ADMITTED mean closure or recorded verdicts, not measured delivery quality.',
                  'Ten rows or fixtures do not establish a representative quality cohort.',
                  ('No recommendations, preference updates, authorization or eligibility changes. '
                   'Opening makes no provider calls or writes; Review delivery saves only an explicit operator observation.'
                   if 'delivery_evidence' in review else
                   'No recommendations, preference updates, authorization or eligibility changes. No provider calls or persistence.')])
    return '\n'.join(lines)

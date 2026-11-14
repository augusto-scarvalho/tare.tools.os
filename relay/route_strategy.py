"""Joint implementation/review preferences over OS-admitted pairs."""
import json
import math
import time

from compute_plane.choice_assessment import digest, request_digest, validate_request
from compute_plane.native_journal import save_json

from . import route_facts

SCHEMA = 'tare.tools/route-strategy/1'


def validate_policy(policy):
    if policy.get('selection') not in {None, 'measured_delivery'}:
        raise ValueError('ROUTE_STRATEGY_SELECTION_INVALID')
    if policy.get('selection') == 'measured_delivery' and policy.get('mode') != 'guarded':
        raise ValueError('MEASURED_SELECTION_REQUIRES_GUARDED_MODE')
    if policy.get('mode') not in {'shadow', 'guarded'}:
        raise ValueError('ROUTE_STRATEGY_REQUIRES_SHADOW_MODE')
    if policy.get('mode') == 'guarded':
        floor = policy.get('acceptance_floor')
        if type(floor) not in (int, float) or not math.isfinite(floor) or not 0 < floor <= 1:
            raise ValueError('ACCEPTANCE_FLOOR_INVALID')
    rows = policy.get('strategies')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 3:
        raise ValueError('ROUTE_STRATEGY_REQUIRES_ONE_TO_THREE_PAIRS')
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'implementer', 'auditor', 'evidence'}:
            raise ValueError('ROUTE_STRATEGY_INVALID')
        for role in ('implementer', 'auditor'):
            pref = row.get(role)
            if (not isinstance(pref, dict) or set(pref) - {'deployment', 'effort'}
                    or not isinstance(pref.get('deployment'), str) or not pref['deployment']
                    or not isinstance(pref.get('effort', 'default'), str)):
                raise ValueError('ROUTE_STRATEGY_ROLE_INVALID')
    for key, default in [('timeout_seconds', 30), ('total_timeout_seconds', 60)]:
        value = policy.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
            raise ValueError('ROUTE_STRATEGY_TIMEOUT_INVALID')


def delivery_evidence(record, pair_sha256):
    """Operator-supplied cohort outcomes, tied to both exact effort realizations.

    Costs include failed attempts and rework, not just successful first calls.
    This is historical evidence, never a prediction of success or savings.
    """
    if record is None:
        return {'status': 'UNKNOWN'}
    if (not isinstance(record, dict) or record.get('pair_sha256') != pair_sha256
            or record.get('scope') != 'implementation+audit+rework'
            or record.get('currency') != 'USD'
            or not all(isinstance(record.get(k), str) and record[k]
                       for k in ('source', 'observed_at', 'cohort'))):
        raise ValueError('STRATEGY_EVIDENCE_INVALID')
    accepted, attempted = record.get('accepted'), record.get('attempted')
    if type(accepted) is not int or type(attempted) is not int or not 0 <= accepted <= attempted or attempted == 0:
        raise ValueError('STRATEGY_EVIDENCE_INVALID')
    for key in ('total_cost_usd', 'total_seconds'):
        value = record.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('STRATEGY_EVIDENCE_INVALID')
    return {'status': 'SUPPLIED_COHORT', 'cohort': record['cohort'],
            'accepted': accepted, 'attempted': attempted,
            'usd_per_accepted_delivery': record['total_cost_usd'] / accepted if accepted else None,
            'seconds_per_accepted_delivery': record['total_seconds'] / accepted if accepted else None,
            'scope': record['scope'], 'source_ref': 'evidence'}


def request_for(task, candidates):
    def label(candidate):
        implementation, audit = candidate['implementer'], candidate['auditor']
        value = (implementation['model'] + '@' + implementation['effort'] + ' + '
                 + audit['model'] + '@' + audit['effort'])
        delivery = candidate['delivery']
        if delivery['status'] == 'SUPPLIED_COHORT' and delivery['accepted']:
            value += (f"; accepted {delivery['accepted']}/{delivery['attempted']}; "
                      f"USD {delivery['usd_per_accepted_delivery']:.4g}/delivery")
        return value

    return {'state': json.dumps({'task': task, 'strategies': [
        {k: c[k] for k in ('key', 'implementer', 'auditor', 'delivery')}
        for c in candidates]}, ensure_ascii=False, separators=(',', ':')),
        'question': 'Choose a complete implementation and audit strategy. OS has admitted the roles. '
                    'Prefer adequate quality, then lower cost and time per accepted delivery, including '
                    'audit and rework. Compare only matching cohorts or benchmark setups. '
                    'Unknown cost is not zero. These are suggestions, not execution authority.',
        'options': {**{c['key']: label(c) for c in candidates},
            'insufficient': 'Keep the existing route; no supported preference.'}}


def _card(deployment, facts):
    from .route_plan import compact_facts
    projected = compact_facts(facts)
    # Preserve selected versioned quality references and actual tariffs. Full facts,
    # including all omitted measurements and provenance, remain in custody.
    return {'model': deployment['model'], 'effort': str(facts['configured_effort'] or 'default'),
            'quality': projected['quality'], 'price': projected['price'],
            'quota': projected['quota']}


def observe(node, implementer, audit, policy, cancellation, root, assess, *, automatic_implementation=False):
    from .adaptive_routes import proposed_route, request_for as task_request
    from .native_routes import ranked
    from .work_audit import identity

    validate_policy(policy)
    if policy.get('selection') == 'measured_delivery':
        from .delivery_comparison import validate_evidence
        validate_evidence(policy)
    started = time.monotonic()
    deadline = started + policy.get('total_timeout_seconds', 60)
    result = {'schema': SCHEMA, 'status': 'UNKNOWN', 'applied': False, 'authority': 'NONE',
              'qualification': 'UNQUALIFIED', 'primary': None, 'candidates': [], 'exclusions': [],
              'inference_attempts': 0, 'input_tokens': 0, 'output_tokens': 0, 'requires_live_revalidation': True,
              'stop_on': ['REVISE', 'AUTH_FAILED', 'AUTHORITY_DENIED', 'PENDING_EFFECT'],
              'fallback': 'KEEP_BASELINE', 'economics': 'No counterfactual savings measured.'}

    def finish(status, reason=None):
        result.update(status=status, elapsed_ms=round((time.monotonic()-started)*1000, 3))
        if reason:
            result['reason'] = reason
        save_json(root / 'route-strategy.json', result)
        return result

    if not automatic_implementation or not audit.get('selection'):
        return finish('SKIPPED', 'EXPLICIT_ROLE_SELECTION')
    if cancellation.is_cancelled():
        return finish('SKIPPED', 'CANCELLED')
    implementers, _ = ranked('implementer', check_resources=True)
    supplied = route_facts.supplied_evidence()
    cache, seen = {}, set()
    for index, configured in enumerate(policy['strategies'], 1):
        key = 'strategy_' + str(index)
        if cancellation.is_cancelled() or time.monotonic() >= deadline:
            return finish('SKIPPED', 'CANCELLED' if cancellation.is_cancelled() else 'ASSESSMENT_BUDGET_EXHAUSTED')
        try:
            producer = proposed_route(configured['implementer'], implementers)
            # Independence and availability are evaluated against THIS proposed
            # implementer, never against the implementation baseline.
            auditors, _ = ranked('auditor', producer, check_resources=True)
            reviewer = proposed_route(configured['auditor'], auditors)
            from .landing_policy import independence
            if independence(identity(producer, 'implementer'), identity(reviewer, 'auditor')) == 'AUDITOR_NOT_INDEPENDENT':
                raise ValueError('AUDITOR_NOT_INDEPENDENT')
            profiles = {'implementer': producer, 'auditor': reviewer}
            from .delivery_comparison import strategy_profile
            hashes = {role: digest(strategy_profile(value)) for role, value in profiles.items()}
            pair_hash = digest(hashes)
            if pair_hash in seen:
                raise ValueError('DUPLICATE_PAIR')
            facts = {role: route_facts.collect(value, live=True, cache=cache, evidence=supplied)
                     for role, value in profiles.items()}
            for value in facts.values():
                if route_facts.admission_reason(value):
                    raise ValueError('PAIR_RESOURCE_UNAVAILABLE')
                if value['quality']['status'] == 'UNKNOWN':
                    raise ValueError('PAIR_LACKS_QUALITY_EVIDENCE')
            delivery = delivery_evidence(configured.get('evidence'), pair_hash)
            artifact = {'profiles': hashes, 'facts': facts, 'evidence': configured.get('evidence')}
            fact_path = key + '-facts.json'
            save_json(root / fact_path, artifact)
            result['candidates'].append({'key': key, 'pair_sha256': pair_hash,
                'facts_path': fact_path, 'facts_sha256': digest(artifact),
                'routes': {role: {'deployment': value['id'], 'model': value['model'],
                    'effort': facts[role]['configured_effort'], 'profile_sha256': hashes[role]}
                    for role, value in profiles.items()},
                **{role: _card(value, facts[role]) for role, value in profiles.items()},
                'delivery': delivery})
            seen.add(pair_hash)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            safe = {'DUPLICATE_PAIR', 'PAIR_RESOURCE_UNAVAILABLE', 'PAIR_LACKS_QUALITY_EVIDENCE',
                    'STRATEGY_EVIDENCE_INVALID', 'AUDITOR_NOT_INDEPENDENT'}
            result['exclusions'].append({'key': key, 'reason': str(exc) if str(exc) in safe
                                         else 'PAIR_NOT_CURRENTLY_ELIGIBLE'})
    if not result['candidates']:
        return finish('ABSTAINED', 'NO_ELIGIBLE_PAIR')
    if policy.get('selection') == 'measured_delivery' and len(result['candidates']) != len(policy['strategies']):
        return finish('ABSTAINED', 'MEASURED_PAIR_NO_LONGER_ELIGIBLE')
    if policy.get('mode') == 'guarded':
        from .route_admission import eligible_winners
        try:
            cohort = (node.get('metrics') or {}).get('routing_cohort')
            result['evidence_gate'] = {'cohort': cohort, 'acceptance_floor': policy['acceptance_floor'],
                'eligible_winners': eligible_winners(result['candidates'], cohort=cohort,
                    acceptance_floor=policy['acceptance_floor'])}
        except ValueError as exc:
            return finish('ABSTAINED', str(exc))
    if policy.get('selection') == 'measured_delivery':
        from .native_agent import read_json
        study = read_json(policy['delivery_study']['path'], 256*1024)
        index = next(i for i, candidate in enumerate(study['candidates'], 1)
                     if candidate['id'] == policy['delivery_study']['winner'])
        key = 'strategy_'+str(index)
        if key not in result['evidence_gate']['eligible_winners']:
            return finish('ABSTAINED', 'SUGGESTION_FAILS_EVIDENCE_GATE')
        candidate = next(c for c in result['candidates'] if c['key'] == key)
        result['primary'] = {k: candidate[k] for k in ('key', 'pair_sha256', 'routes', 'facts_sha256', 'facts_path')}
        return finish('SUGGESTED', 'MEASURED_DELIVERY_PREFERENCE')
    request = request_for(json.loads(task_request(node)['state']), result['candidates'])
    try:
        validate_request(request)
    except ValueError:
        return finish('ABSTAINED', 'ASSESSMENT_INPUT_INVALID')
    remaining = deadline - time.monotonic()
    if cancellation.is_cancelled() or remaining <= 0:
        return finish('SKIPPED', 'CANCELLED' if cancellation.is_cancelled() else 'ASSESSMENT_BUDGET_EXHAUSTED')
    save_json(root / 'strategy-request.json', request)
    result['assessment'] = {'status': 'UNKNOWN', 'request_sha256': request_digest(request),
                            'request_path': 'strategy-request.json'}
    save_json(root / 'route-strategy.json', result)
    answer = assess(policy.get('command'), request,
                    timeout_seconds=min(policy.get('timeout_seconds', 30), remaining))
    result['assessment']['result'] = answer
    result['assessment']['status'] = answer['status']
    result['inference_attempts'] = answer.get('inference_attempts', 0)
    result['input_tokens'] = answer.get('answer', {}).get('usage', {}).get('input_tokens', 0)
    result['output_tokens'] = answer.get('answer', {}).get('usage', {}).get('output_tokens', 0)
    if cancellation.is_cancelled():
        return finish('SKIPPED', 'CANCELLED')
    if answer['status'] != 'OBSERVED':
        return finish('ABSTAINED', answer.get('reason', 'ASSESSMENT_FAILED'))
    chosen = answer['answer']['choice']
    candidate = next((c for c in result['candidates'] if c['key'] == chosen), None)
    if candidate is None:
        return finish('ABSTAINED', 'NO_SUPPORTED_PREFERENCE' if chosen == 'insufficient' else 'ASSESSMENT_ANSWER_INVALID')
    if policy.get('mode') == 'guarded' and chosen not in result['evidence_gate']['eligible_winners']:
        return finish('ABSTAINED', 'SUGGESTION_FAILS_EVIDENCE_GATE')
    result['primary'] = {k: candidate[k] for k in ('key', 'pair_sha256', 'routes', 'facts_sha256', 'facts_path')}
    return finish('SUGGESTED')


def summary(result):
    return {k: result[k] for k in ('schema', 'status', 'primary', 'inference_attempts',
                                  'input_tokens', 'output_tokens', 'elapsed_ms', 'fallback', 'reason') if k in result}

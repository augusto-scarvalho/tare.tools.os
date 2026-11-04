"""Bounded, evidence-backed route suggestions; never execution authority."""
import json
import math
import time

from compute_plane.choice_assessment import digest, request_digest, validate_request
from compute_plane.native_journal import save_json
from graph_backlog.jsonutil import sha256_canonical

from . import route_facts

SCHEMA = 'tare.tools/route-plan/1'


def validate_policy(policy):
    if policy.get('mode') != 'shadow':
        raise ValueError('ROUTE_PLAN_REQUIRES_SHADOW_MODE')
    roles = policy.get('candidates')
    if not isinstance(roles, dict) or not roles or set(roles) - {'implementer', 'auditor'}:
        raise ValueError('ROUTE_PLAN_ROLES_INVALID')
    for role, rows in roles.items():
        if not isinstance(rows, list) or not 1 <= len(rows) <= 3:
            raise ValueError('ROUTE_PLAN_REQUIRES_ONE_TO_THREE_CANDIDATES')
        for row in rows:
            if (not isinstance(row, dict) or set(row) - {'deployment', 'effort', 'purpose'}
                    or not isinstance(row.get('deployment'), str) or not row['deployment']
                    or not isinstance(row.get('effort', 'default'), str)
                    or row.get('purpose', 'execution') not in {'execution', 'escalation'}):
                raise ValueError('ROUTE_PLAN_CANDIDATE_INVALID')
        if sum(r.get('purpose') == 'escalation' for r in rows) > 1:
            raise ValueError('ROUTE_PLAN_MULTIPLE_ESCALATIONS')
        if role == 'auditor' and any(r.get('purpose') == 'escalation' for r in rows):
            raise ValueError('ROUTE_PLAN_CANNOT_ESCALATE_AUDIT_VERDICTS')
    for key, default in [('timeout_seconds', 30), ('total_timeout_seconds', 60)]:
        value = policy.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
            raise ValueError('ROUTE_PLAN_TIMEOUT_INVALID')


def compact_facts(facts):
    """A declared projection; full source records remain in the custody artifact."""
    quality = facts['quality']
    records = quality.get('records', [])
    price = facts['price']
    # Select one public comparison and one runtime observation where available.
    # Keep provenance and full protocol details in the separately hashed facts.
    selected = [i for i, row in enumerate(records)
                if row.get('applicability', {}).get('level') == 'EXACT_PROFILE'][:1]
    for kind in ('public-benchmark', 'native-canary'):
        if len(selected) >= 2:
            break
        found = next((i for i, row in enumerate(records) if row.get('kind') == kind), None)
        if found is not None and found not in selected:
            selected.append(found)
    # Do not fill remaining space with another public benchmark: three current
    # CLI candidates otherwise exceed the bounded helper's context. Full facts
    # preserve all references; this projection declares its omissions.
    if not selected and records:
        selected.append(0)
    rows = []
    for index in sorted(selected):
        record = records[index]
        rows.append({k: record[k] for k in ('kind', 'metric', 'value', 'unit', 'scope',
            'tested_effort', 'applicability') if k in record} | {'source_ref': f'quality.records[{index}]'})
    projected = {k: facts.get(k) for k in ('context_window', 'output_cap', 'configured_effort')}
    projected['quality'] = {'status': quality['status'], 'records': rows,
                            'omitted_records': max(0, len(records) - len(rows))}
    owner = quality.get('owner_catalog')
    if owner:
        projected['quality']['owner_claim'] = {k: owner.get(k) for k in
            ('qualified_for', 'not_for', 'limits', 'quant')}
    projected['price'] = {k: price[k] for k in
        ('status', 'currency', 'per_million_tokens', 'scope') if k in price}
    if price.get('status') == 'CONFIGURED_TARIFF':
        projected['price']['per_million_tokens'] = json.loads(price['rates_json'])
    projected['price']['schedule_overrides_present'] = bool(price.get('schedule_overrides'))
    if price['status'] == 'UNKNOWN' and facts.get('price_references'):
        reference = facts['price_references'][0]
        projected['price_reference'] = {k: reference[k] for k in
            ('currency', 'per_million_tokens', 'scope', 'observed_at')}
        projected['price_reference']['source_ref'] = 'price_references[0]'
    projected['quota'] = {k: facts['quota'][k] for k in ('status', 'remaining', 'unit') if k in facts['quota']}
    projected['resource'] = {k: facts['resource'][k] for k in
        ('status', 'immediate', 'owner_kind', 'loading_required') if k in facts['resource']}
    return projected


def request_for(task, role, candidates, *, escalation=False):
    purpose = 'a conditional quality escalation' if escalation else 'the next execution preference'
    return {'state': json.dumps({'task': task, 'role': role,
        'candidates': [{k: c[k] for k in ('key', 'model', 'wire_model', 'protocol', 'effort', 'facts')
                        if k in c} for c in candidates]},
        ensure_ascii=False, separators=(',', ':')),
        'question': f'Choose {purpose}: adequate task quality first, then cost and time. '
                    'Compare only matching benchmark versions/setups. Unknown is not free or qualified. '
                    'If evidence is inadequate, choose insufficient.',
        'options': {**{c['key']: 'Select candidate ' + c['key'] for c in candidates},
                    'insufficient': 'No remaining candidate is supported by enough evidence.'}}


def observe(node, implementer, audit, policy, cancellation, root, assess, *, automatic_implementation=False):
    """At most three choices per role, with a shared finite worker-time budget.

    Each fallback is chosen in a separate conditional choice. Never sort raw
    probabilities into an unassessed ranking or treat them as success rates.
    """
    from .adaptive_routes import proposed_route, request_for as task_request
    from .native_routes import ranked
    from .work_audit import FALLBACK_CODES, identity

    validate_policy(policy)
    started = time.monotonic()
    deadline = started + policy.get('total_timeout_seconds', 60)
    task = json.loads(task_request(node)['state'])
    result = {'schema': SCHEMA, 'status': 'UNKNOWN', 'applied': False, 'authority': 'NONE',
              'qualification': 'UNQUALIFIED', 'roles': {}, 'inference_attempts': 0,
              'input_tokens': 0, 'requires_live_revalidation': True}
    evidence = route_facts.supplied_evidence()
    cache = {}
    for role, configured in policy['candidates'].items():
        plan = {'primary': None, 'fallbacks': [], 'escalation': None, 'assessments': [],
                'candidates': [], 'exclusions': [], 'status': 'UNKNOWN',
                'stop_on': ['REVISE', 'AUTH_FAILED', 'AUTHORITY_DENIED', 'PENDING_EFFECT'],
                'producer_sha256': digest(implementer) if role == 'auditor' else None}
        result['roles'][role] = plan
        # Never turn an explicit role choice into a semantic proposal.
        if (role == 'implementer' and not automatic_implementation
                or role == 'auditor' and not audit.get('selection')):
            plan.update(status='SKIPPED', reason='EXPLICIT_ROLE_SELECTION')
            continue
        if cancellation.is_cancelled() or time.monotonic() >= deadline:
            plan.update(status='SKIPPED', reason='CANCELLED' if cancellation.is_cancelled() else 'ASSESSMENT_BUDGET_EXHAUSTED')
            continue
        eligible, baseline = ranked(role, implementer if role == 'auditor' else None, check_resources=True)
        plan['baseline'] = [{k: row[k] for k in ('deployment', 'profile_sha256', 'cost_tier')}
                            for row in baseline['candidates'] if row['eligible']]
        seen = set()
        for number, preference in enumerate(configured):
            key = 'candidate_' + str(number + 1)
            try:
                deployment = proposed_route(preference, eligible)
                fingerprint = sha256_canonical(deployment)
                if fingerprint in seen:
                    raise ValueError('DUPLICATE_REALIZATION')
                facts = route_facts.collect(deployment, live=True, cache=cache, evidence=evidence)
                reason = route_facts.admission_reason(facts)
                if reason:
                    raise ValueError(reason)
                # Effort realization must still have the independent auditor identity.
                if role == 'auditor':
                    producer, reviewer = identity(implementer, 'implementer'), identity(deployment, 'auditor')
                    if producer.model_id == reviewer.model_id or producer.bias_domain == reviewer.bias_domain:
                        raise ValueError('AUDITOR_NOT_INDEPENDENT')
                seen.add(fingerprint)
                fact_path = f'{role}-{key}-facts.json'
                save_json(root / fact_path, facts)
                projected = compact_facts(facts)
                projected['configured_cost_tier'] = next((row.get('cost_tier') for row in
                    baseline['candidates'] if row.get('selected_deployment') == deployment['id']), 'UNKNOWN')
                plan['candidates'].append({'key': key, 'deployment': deployment['id'],
                    'wire_model': deployment['native_profile']['model'],
                    'protocol': deployment['native_profile']['protocol'],
                    'model': deployment['model'], 'effort': deployment.get('native_model_choice', {}).get(
                        'effort') or facts['configured_effort'], 'profile_sha256': fingerprint,
                    'purpose': preference.get('purpose', 'execution'), 'facts': projected,
                    'facts_sha256': digest(facts), 'facts_path': fact_path})
            except (ValueError, OSError, KeyError, TypeError):
                plan['exclusions'].append({'key': key, 'reason': 'CANDIDATE_NOT_CURRENTLY_ELIGIBLE'})
        available = [c for c in plan['candidates'] if c['purpose'] == 'execution']
        reserve = [c for c in plan['candidates'] if c['purpose'] == 'escalation']
        sequence = []
        stopped = None
        # The reserve is assessed only after an execution candidate is supported.
        while available or (reserve and sequence):
            escalation = not available
            choices = reserve if escalation else available
            remaining = deadline - time.monotonic()
            if cancellation.is_cancelled() or remaining <= 0:
                stopped = 'CANCELLED' if cancellation.is_cancelled() else 'ASSESSMENT_BUDGET_EXHAUSTED'
                break
            request = request_for(task, role, choices, escalation=escalation)
            try:
                validate_request(request)
            except ValueError as exc:
                stopped = str(exc)
                break
            number = len(plan['assessments'])
            request_path = f'{role}-{number}-request.json'
            save_json(root / request_path, request)
            # A durable pending record precedes each call; replay never repeats it.
            call = {'status': 'UNKNOWN', 'request_sha256': request_digest(request),
                    'request_path': request_path, 'purpose': 'escalation' if escalation else 'execution'}
            plan['assessments'].append(call)
            save_json(root / 'route-plan.json', result)
            answer = assess(policy.get('command'), request,
                            timeout_seconds=min(policy.get('timeout_seconds', 30), remaining))
            call['assessment'] = answer
            call['status'] = answer['status']
            result['inference_attempts'] += answer.get('inference_attempts', 0)
            result['input_tokens'] += answer.get('answer', {}).get('usage', {}).get('input_tokens', 0)
            if cancellation.is_cancelled():
                stopped = 'CANCELLED'; break
            if answer['status'] != 'OBSERVED':
                stopped = answer.get('reason', 'ASSESSMENT_FAILED'); break
            chosen = answer['answer']['choice']
            if chosen == 'insufficient':
                if escalation:
                    reserve = []
                elif sequence:
                    available = []
                else:
                    stopped = 'INSUFFICIENT_EVIDENCE'; break
                continue
            candidate = next((c for c in choices if c['key'] == chosen), None)
            if candidate is None:
                stopped = 'ASSESSMENT_ANSWER_INVALID'; break
            # The helper may ignore the abstention instruction. Keep its answer
            # as evidence, but never promote name-based guesses into a plan.
            if candidate['facts']['quality']['status'] == 'UNKNOWN':
                stopped = 'CHOICE_LACKS_QUALITY_EVIDENCE'; break
            entry = {k: candidate[k] for k in
                ('key', 'deployment', 'model', 'effort', 'profile_sha256', 'facts_sha256', 'facts_path')}
            entry.update(assessment_index=number, reason='HELPER_CHOICE_FROM_SUPPLIED_EVIDENCE')
            if escalation:
                plan['escalation'] = {**entry, 'when': ['IMPLEMENTATION_CHECKS_FAILED'], 'max_attempts': 1,
                    'requires': ['FAILURE_EVIDENCE', 'ADMITTED_SUCCESSOR', 'REMAINING_WORK_BUDGET']}
                reserve = []
            else:
                sequence.append(entry)
                available = [c for c in available if c['key'] != chosen]
                if len(sequence) == 1:
                    plan['primary'] = entry
                else:
                    plan['fallbacks'].append({**entry, 'when': sorted(FALLBACK_CODES),
                        'requires': ['LIVE_ELIGIBILITY', 'NO_PENDING_EFFECT', 'REMAINING_WORK_BUDGET',
                                     'ADMITTED_SUCCESSOR' if role == 'implementer' else 'SAME_REVIEW_CANDIDATE']})
        plan['status'] = ('PARTIAL' if sequence else 'ABSTAINED') if stopped else ('SUGGESTED' if sequence else 'ABSTAINED')
        if stopped:
            plan['reason'] = stopped
        elif not sequence:
            plan['reason'] = 'NO_ELIGIBLE_EXECUTION_CANDIDATE'
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
    result['status'] = 'OBSERVED'
    save_json(root / 'route-plan.json', result)
    return result


def summary(plan):
    """Small projection for the existing bounded Work status, never full cards."""
    return {'schema': SCHEMA, 'inference_attempts': plan['inference_attempts'],
            'input_tokens': plan['input_tokens'], 'elapsed_ms': plan.get('elapsed_ms'),
            'roles': {role: {'status': value['status'], 'reason': value.get('reason'),
                'primary': {k: value['primary'][k] for k in ('deployment', 'model', 'effort')} if value['primary'] else None,
                'fallback_count': len(value['fallbacks']), 'has_escalation': value['escalation'] is not None}
                for role, value in plan['roles'].items()}}

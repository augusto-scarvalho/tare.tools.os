"""Observe task demand; optionally admit exact pairs using deterministic evidence.

Only an explicit guarded joint policy can alter new automatic Work selection.
It cannot remove a review, select a flow, retry a session or grant authority.
"""
import copy
import os
from pathlib import Path

from compute_plane.choice_assessment import digest, request_digest, run_assessment
from compute_plane.native_journal import custody_lock, save_json
from graph_backlog.jsonutil import sha256_canonical
from .native_agent import read_json

QUESTION = 'What kind of implementation or review does this task require?'
OPTIONS = {
    'bounded': 'A localized mechanical change with explicit executable checks.',
    'reasoning': 'Substantial reasoning about behavior, concurrency, security or architecture.',
    'insufficient': 'The supplied task does not establish the required scope or checks.',
}


def request_for(node):
    import json
    # Complete selected fields, never silently cropped text or model summaries.
    state = {key: node.get(key) for key in ('title', 'summary', 'exit_criteria', 'repository_scopes')}
    state['verification_checks'] = (node.get('metrics') or {}).get('verification_checks')
    return {'state': json.dumps(state, ensure_ascii=False), 'question': QUESTION, 'options': dict(OPTIONS)}


def proposed_route(route, eligible):
    """A configured preference must resolve to the current role allowlist."""
    from . import model_choices, native_catalog
    value = native_catalog.select(route['deployment'])
    original = next((d for d in eligible if digest(d) == digest(value)), None)
    if original is None:
        raise ValueError('ADAPTIVE_ROUTE_NOT_ELIGIBLE')
    effort = route.get('effort', 'default')
    if effort == 'default':
        return copy.deepcopy(original)
    described = model_choices.describe(route['deployment'], value)
    choices = [{k: described[k] for k in ('key', 'label', 'group')} | {'routes': [described['route']]}]
    realized = model_choices.realization(choices, {'key': described['key'],
        'connection': route['deployment'], 'reasoning': effort})
    if realized['model'] != original['model']:
        raise ValueError('ADAPTIVE_EFFORT_CHANGED_ROLE_IDENTITY')
    return realized


def observe_work(node, implementer, audit, custody, cancellation, *, automatic_implementation=False):
    # Measurement failure must not become an execution gate.
    try:
        return _observe_work(node, implementer, audit, custody, cancellation,
                             automatic_implementation=automatic_implementation)
    except (ValueError, OSError, KeyError, TypeError):
        return {'schema': 'tare.tools/adaptive-route-observation/1', 'status': 'FAILED',
                'reason': 'ADAPTIVE_OBSERVATION_UNAVAILABLE', 'applied': False, 'authority': 'NONE'}


def summary(observation, custody):
    """Do not duplicate full assessment/context cards in bounded CLI status."""
    value = {k: observation[k] for k in ('schema', 'status', 'applied', 'qualification', 'reason', 'proposals', 'route_plan', 'route_strategy') if k in observation}
    assessment = observation.get('assessment', {})
    if assessment:
        value['assessment'] = {k: assessment[k] for k in ('status', 'reason', 'elapsed_ms', 'inference_attempts') if k in assessment}
        value['assessment'].update({k: assessment['answer'][k] for k in ('choice', 'usage') if k in assessment.get('answer', {})})
    path = Path(custody) / 'route-assessment' / 'observation.json'
    value['observation_path'] = str(path) if path.is_file() else None
    return value


def _observe_work(node, implementer, audit, custody, cancellation, *, automatic_implementation=False):
    """One optional assessment per new Work dispatch, reused for both role proposals.

    Resume reads the Work origin; it does not enter this function. An interrupted
    observation remains UNKNOWN and is never silently called again in this custody.
    """
    configured = configured_policy()
    if not configured or not (automatic_implementation or audit.get('selection')):
        return None
    root = Path(custody) / 'route-assessment'
    root.mkdir(exist_ok=True)
    path = root / 'observation.json'
    with custody_lock(root):
        policy = read_json(configured, 65536)
        if (policy.get('schema') not in {'tare.tools/adaptive-route-policy/1', 'tare.tools/adaptive-route-policy/2', 'tare.tools/adaptive-route-policy/3'}
                or policy.get('mode') not in {'shadow', 'guarded'}
                or policy.get('mode') == 'guarded' and policy.get('schema') != 'tare.tools/adaptive-route-policy/3'):
            raise ValueError('ADAPTIVE_ROUTE_POLICY_REQUIRES_SHADOW_MODE')
        structured = policy['schema'] != 'tare.tools/adaptive-route-policy/1'
        joint = policy['schema'] == 'tare.tools/adaptive-route-policy/3'
        if structured:
            from . import route_plan, route_strategy
            observer = route_strategy if joint else route_plan
            observer.validate_policy(policy)
        request = request_for(node)
        binding = {'work_sha256': digest(node), 'policy_sha256': digest(policy),
                   'implementer_sha256': digest(implementer), 'audit_sha256': digest(audit),
                   'request_sha256': request_digest(request)}
        if path.exists():
            saved = read_json(path, 65536)
            if saved['binding'] != binding:
                raise ValueError('ADAPTIVE_ROUTE_INPUT_CHANGED')
            return saved
        receipt = {'schema': 'tare.tools/adaptive-route-observation/1', 'binding': binding,
                   'mode': policy['mode'], 'status': 'UNKNOWN', 'authority': 'NONE',
                   'applied': False, 'qualification': 'UNQUALIFIED', 'proposals': {},
                   'economics': 'No counterfactual quality, monetary saving or latency saving measured.'}
        receipt['route_context'] = {
            'implementation': implementer.get('native_selection', {}).get('candidates', []),
            'audit': audit.get('selection', {}).get('candidates', []),
            'scope': 'Task-demand classifier sees task input; OS resolves preferences against these source-labelled candidates.'}
        save_json(path, receipt)
        if cancellation.is_cancelled():
            receipt.update(status='SKIPPED', reason='CANCELLED')
        elif structured:
            plan = observer.observe(node, implementer, audit, policy, cancellation, root,
                                      run_assessment, automatic_implementation=automatic_implementation)
            receipt.update(status=plan['status'])
            receipt['route_strategy' if joint else 'route_plan'] = observer.summary(plan)
            receipt['route_context']['scope'] = ('Measured delivery comparison selects a cohort-specific pair without inference.'
                if policy.get('selection') == 'measured_delivery' else
                'Helper compares at most three complete OS-admitted implementation/audit pairs.'
                if joint else 'Helper sees task and projected evidence for at most three eligible realizations per role.')
            if cancellation.is_cancelled():
                receipt.update(status='SKIPPED', reason='CANCELLED')
        else:
            assessment = run_assessment(policy.get('command'), request,
                                        timeout_seconds=policy.get('timeout_seconds', 30))
            receipt.update(assessment=assessment, status=assessment['status'])
            if cancellation.is_cancelled():
                receipt.update(status='SKIPPED', reason='CANCELLED')
            elif assessment['status'] == 'OBSERVED':
                demand = assessment['answer']['choice']
                preferences = policy.get('preferences', {}).get(demand, {}) if demand != 'insufficient' else {}
                from .native_routes import ranked
                eligible = {'implementer': ranked('implementer', check_resources=True)[0] if automatic_implementation else [],
                            'auditor': [r['deployment'] for r in audit.get('routes', []) if r['eligible']]}
                for role in ('implementer', 'auditor'):
                    if role not in preferences or not eligible[role]:
                        continue
                    try:
                        selected = proposed_route(preferences[role], eligible[role])
                        receipt['proposals'][role] = {'deployment': selected['id'], 'model': selected['model'],
                            'profile_sha256': sha256_canonical(selected),
                            'effort': selected.get('native_model_choice', {}).get('effort', 'configured')}
                    except (ValueError, OSError, KeyError, TypeError):
                        receipt['proposals'][role] = {'reason': 'PREFERENCE_NOT_CURRENTLY_ELIGIBLE'}
        save_json(path, receipt)
        return receipt


def configured_policy():
    configured = os.environ.get('TARE_ADAPTIVE_ROUTE_POLICY')
    if configured:
        return None if configured.lower() == 'off' else configured
    from .native_installation import assessment_policy
    path = assessment_policy('default.json')
    return str(path) if path else None


def admitted_routes(observation, node, implementer, audit, custody, cancellation):
    """Revalidate both exact profiles atomically before a new automatic dispatch.

    Never called by resume. Failure retains the baseline and changes no role.
    The model's unqualified suggestion grants no execution or landing authority.
    """
    if not observation or observation.get('mode') != 'guarded' or observation.get('status') != 'SUGGESTED':
        return implementer, audit
    from . import route_admission, native_routes, route_facts, work_audit
    root = Path(custody)/'route-assessment'
    try:
        if cancellation.is_cancelled():
            raise ValueError('CANCELLED')
        policy = read_json(configured_policy(), 65536)
        from .delivery_comparison import validate_evidence
        validate_evidence(policy)
        if digest(policy) != observation['binding']['policy_sha256'] or digest(node) != observation['binding']['work_sha256']:
            raise ValueError('ADAPTIVE_ROUTE_INPUT_CHANGED')
        if digest(implementer) != observation['binding']['implementer_sha256'] or digest(audit) != observation['binding']['audit_sha256']:
            raise ValueError('ADAPTIVE_ROUTE_INPUT_CHANGED')
        saved = read_json(root/'route-strategy.json', 262144)
        primary = saved['primary']
        if primary != observation['route_strategy']['primary']:
            raise ValueError('ADAPTIVE_ROUTE_INPUT_CHANGED')
        from .route_strategy import delivery_evidence
        for candidate in saved['candidates']:
            preference = policy['strategies'][int(candidate['key'].split('_')[1])-1]
            evidence = delivery_evidence(preference.get('evidence'), candidate['pair_sha256'])
            if evidence != candidate['delivery']:
                raise ValueError('STRATEGY_EVIDENCE_CHANGED')
            facts = read_json(root/candidate['facts_path'], 262144)
            if digest(facts) != candidate['facts_sha256']:
                raise ValueError('STRATEGY_EVIDENCE_CHANGED')
        winners = route_admission.eligible_winners(saved['candidates'],
            cohort=(node.get('metrics') or {}).get('routing_cohort'), acceptance_floor=policy['acceptance_floor'])
        if primary['key'] not in winners:
            raise ValueError('SUGGESTION_FAILS_EVIDENCE_GATE')
        configured = policy['strategies'][int(primary['key'].split('_')[1])-1]
        producer = proposed_route(configured['implementer'], native_routes.ranked('implementer', check_resources=True)[0])
        reviewer = proposed_route(configured['auditor'], native_routes.ranked('auditor', producer, check_resources=True)[0])
        for role, value in [('implementer', producer), ('auditor', reviewer)]:
            if digest(value) != primary['routes'][role]['profile_sha256']:
                raise ValueError('ADAPTIVE_PROFILE_CHANGED')
            facts = route_facts.collect(value, live=True)
            if route_facts.admission_reason(facts) or facts['quality']['status'] == 'UNKNOWN':
                raise ValueError('PAIR_RESOURCE_UNAVAILABLE')
        alternatives = native_routes.ranked('auditor', producer, check_resources=True)[0]
        alternatives = [value for value in alternatives if value['id'] != reviewer['id']]
        updated_audit = work_audit.prepare(producer, [reviewer, *alternatives], 'required')
        if updated_audit['status'] != 'READY' or cancellation.is_cancelled():
            raise ValueError('PAIR_NOT_CURRENTLY_ELIGIBLE')
        observation.update(applied=True, selection_authority='OPERATOR_POLICY_AND_DETERMINISTIC_EVIDENCE_GATE')
        save_json(root/'observation.json', observation)
        return producer, updated_audit
    except (ValueError, OSError, KeyError, TypeError, IndexError):
        observation.update(applied=False, reason='GUARDED_ROUTE_REVALIDATION_FAILED')
        save_json(root/'observation.json', observation)
        return implementer, audit

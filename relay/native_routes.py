"""Rank configured native role candidates; existing owners execute and recover them."""
import copy
import os
from pathlib import Path

from graph_backlog.jsonutil import sha256_canonical
from . import native_catalog, native_generation, relay_mesh
from .native_agent import read_json

COST = {'free': 0, 'flat': 1, 'free-credit': 2, 'metered': 3}


def configured():
    path = Path(os.environ.get('TARE_ROLE_DEPLOYMENTS') or Path(__file__).with_name('role_deployments.json'))
    from .round_table_roster import load_role_deployments
    return load_role_deployments(path, roster=read_json(native_catalog.roster_path(), 4*1024*1024))


def roles(row, scope):
    """A role-table row's roles; in --local_only mode also its local_only_roles (an operator admission by choice
    of that mode, not a quality claim)."""
    return [*row.get('roles', []), *(row.get('local_only_roles', []) if scope.only == 'local' else [])]


def credential_available(name):
    return bool(os.environ.get(name) or native_generation.vault_secret(name))


def ranked(role, producer=None, *, check_resources=False):
    from .execution_scope import for_role
    from .execution_scope import current, pinned
    if current().pin(role):
        value = pinned(role, producer)
        return [value], {'schema': 'tare.tools/native-role-selection/1', 'role': role,
                         'reason': 'EXACT_TEMPORARY_PIN', 'generation_calls': 0,
                         'candidates': [{'eligible': True, 'catalog': value['native_model_choice']['connection'],
                                         'selected_deployment': value['id']}]}
    with for_role(role):return _ranked(role,producer,check_resources=check_resources)


def admission(row, role, author=None, credentials=None):
    """(deployment, auditor identity, reason) of one role-table row under the rules that come before the routing
    profile and any live probe; reason None when they admit it. `author`: the producer's identity, for a review role
    (None checks no independence). `credentials`: a cache shared across rows. The matrix of /routes reads it too
    (environment.auto_routes), so what it shows cannot drift from what ranks."""
    from .work_audit import identity
    from .execution_scope import constrained, current, same_family_admitted
    credentials = {} if credentials is None else credentials
    if role not in roles(row, current()):
        return None, None, 'ROLE_NOT_CONFIGURED'
    if row.get('qualified') is False:
        # Registered from a provider's metadata: an exact pin reaches it, automatic selection waits for a live
        # qualification through tare.
        return None, None, 'NOT_QUALIFIED_PIN_ONLY'
    if row.get('runner') != 'native-http':
        return None, None, 'RUNNER_NOT_SUPPORTED_BY_NATIVE_WORK' if role == 'auditor' else 'RUNNER_NOT_SUPPORTED_BY_NATIVE_FLOW'
    if row.get('cost_tier') not in COST or row.get('kind') not in {'primary', 'reserve'}:
        return None, None, 'COST_OR_PREFERENCE_UNKNOWN'
    try:
        if row.get('native_catalog') and row['native_catalog'] not in native_catalog.LOCAL:
            from .native_profiles import local_path
            if not local_path(row['native_catalog']).is_file():
                return None, None, 'PROFILE_NOT_INSTALLED'
        deployment = (native_catalog.select(row['native_catalog']) if row.get('native_catalog')
                      else copy.deepcopy(row))
        if deployment.get('model') != row.get('model'):
            raise ValueError('ROLE_PROFILE_IDENTITY_MISMATCH')
        native_generation.deployment_profile(deployment, deployment['native_profile']['model'])
        auditor = identity(deployment, 'auditor')
    except (ValueError, KeyError, TypeError, OSError):
        return None, None, 'PROFILE_OR_IDENTITY_INVALID'
    deployment=constrained(deployment,role)
    from .environment import allows, older
    if not allows(deployment)[0]:
        return deployment, auditor, 'NOT_USED_ON_THIS_MACHINE'
    if older(deployment):
        return deployment, auditor, 'OLDER_MODEL'
    from .turn_engine import usage_limited
    if usage_limited(deployment):
        return deployment, auditor, 'QUOTA_EXHAUSTED'
    from .landing_policy import independence
    same_family = bool(author) and independence(author, auditor) == 'AUDITOR_NOT_INDEPENDENT'
    if same_family and not same_family_admitted():
        return deployment, auditor, 'AUDITOR_NOT_INDEPENDENT'
    if deployment['native_profile']['protocol'] == 'vendor-cli':
        executable = deployment.get('native_cli', {}).get('executable')
        if not executable or not Path(executable).is_file():
            return deployment, auditor, 'CLI_EXECUTABLE_UNAVAILABLE'
    if deployment['native_profile']['protocol'] == 'openai-codex-responses':
        auth = deployment['native_codex_auth']
        token_key = ('codex-login', auth['path'], auth['account_id'])
        if token_key not in credentials:
            credentials[token_key] = native_generation.codex_login_available(deployment)
        if not credentials[token_key]:
            return deployment, auditor, 'LOGIN_UNAVAILABLE'
    key = deployment.get('native_credential_env')
    if key and key not in credentials:
        try:
            credentials[key] = credential_available(key)
        except Exception:
            credentials[key] = False
    if key and not credentials[key]:
        return deployment, auditor, 'CREDENTIAL_UNAVAILABLE'
    return deployment, auditor, None


def _ranked(role, producer=None, *, check_resources=False):
    """No inference. New automatic dispatch can also observe current capacity."""
    from .work_audit import identity
    if role not in {'planner', 'plan-auditor', 'auditor', 'implementer', 'explorer'} or (role in {'plan-auditor', 'auditor'} and producer is None):
        raise ValueError('ROLE_SELECTION_REQUIRES_SUPPORTED_ROLE_AND_AUTHOR')
    author = identity(producer, 'implementer') if producer is not None else None
    table = configured(); decisions = []; eligible = []; credentials = {}; seen = set()
    from . import route_facts
    from .execution_scope import current
    fact_cache = {}; evidence = route_facts.supplied_evidence(); observed_facts = {}
    for order, row in enumerate(table['deployments']):
        decision = {'deployment': row['id'], 'eligible': False}
        decisions.append(decision)
        deployment, auditor, reason = admission(row, role, author, credentials)
        if reason:
            decision['reason'] = reason; continue
        from .landing_policy import independence
        kind = independence(author, auditor) if author else None
        same_family = kind == 'AUDITOR_NOT_INDEPENDENT'
        key = deployment.get('native_credential_env')
        from .execution_scope import refusal
        if refusal(role, deployment, row['id'], row.get('native_catalog', row['id'])):
            decision['reason'] = 'SWITCHED_OFF_FOR_ROLE'; continue  # after the rules, before a live probe
        if check_resources:
            try:
                facts = route_facts.collect(deployment, live=True, cache=fact_cache, evidence=evidence)
                observed_facts[id(decision)] = facts
                # Keep status receipts small. Full benchmark/price cards belong to
                # the separate context projection, not every nested route receipt.
                material = {}
                if facts['resource'].get('immediate') is not None:
                    material['resource'] = facts['resource']
                if facts['quota']['status'] not in {'UNKNOWN', 'NOT_APPLICABLE'}:
                    material['quota'] = facts['quota']
                if material:
                    decision['facts'] = material
                unavailable = route_facts.admission_reason(facts)
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                unavailable = 'RESOURCE_OBSERVATION_INVALID'
            if unavailable:
                decision['reason'] = unavailable; continue
        fingerprint = sha256_canonical(deployment)
        if fingerprint in seen:
            decision['reason'] = 'DUPLICATE_REALIZATION'; continue
        seen.add(fingerprint)
        cross = bool(author and auditor.vendor != author.vendor)
        decision.update(eligible=True, catalog=row.get('native_catalog',row['id']),
            reason=('CONFIGURED_NATIVE_ROLE' if kind is None else 'SAME_FAMILY_LOCAL_ONLY_NOT_INDEPENDENT' if same_family
                    else 'CONFIGURED_INDEPENDENT_NATIVE_ROUTE' if kind == 'INDEPENDENT' else kind),
            cost_tier=row['cost_tier'], cross_vendor=cross, profile_sha256=fingerprint,
            selected_deployment=deployment['id'], availability='CREDENTIAL_PRESENT_HOST_UNPROBED' if key else 'HOST_UNPROBED')
        priority = row.get('review_priority', 1000) if role in {'auditor', 'plan-auditor'} else 0
        decision['review_priority'] = priority
        cli_backup = (role in {'auditor', 'plan-auditor'} and
                      (deployment['native_profile']['endpoint'] == 'cli://codex' or row['id'] == 'claude-cli'))
        # An independent auditor always precedes an admitted same-family one, and another vendor precedes the same
        # vendor before any preference, tier or priority: a same-vendor auditor runs only when no other is eligible.
        rank = (same_family, not cross, current().preference(deployment,role), COST[row['cost_tier']], priority, row['kind'] != 'primary', cli_backup, order)
        eligible.append((rank, deployment, decision))
    if check_resources:
        kept = []
        for item in eligible:
            unavailable = route_facts.admission_reason(observed_facts[id(item[2])])
            if unavailable:
                item[2].update(eligible=False, reason=unavailable)
            else:
                kept.append(item)
        eligible = kept
    eligible.sort(key=lambda item: item[0])
    for position, (_, _, decision) in enumerate(eligible):
        decision['position'] = position + 1
    from .scheduling_diagnostics import diagnostic
    for decision in decisions:
        if not decision['eligible']:
            decision['diagnostic'] = diagnostic(decision['reason'])
    return [deployment for _, deployment, _ in eligible], {
        'schema': 'tare.tools/native-role-selection/1', 'role': role,
        'reason': 'AUTO_CROSS_VENDOR_THEN_COST_AND_CONFIGURED_ORDER' if author else 'AUTO_COST_AND_CONFIGURED_ORDER',
        'policy_sha256': sha256_canonical(table), 'producer_sha256': author.sha256 if author else None,
        'roster_sha256': author.payload['roster_sha256'] if author else None,
        'candidates': decisions, 'generation_calls': 0,
        'cost_scope': 'Configured tiers and operator review preference; no price estimate or measured quality ranking.'}


def independent_auditors(producer):
    return reviewers(producer)


def reviewers(producer, role='auditor', *, check_resources=True):
    from .execution_scope import for_role
    with for_role(role):return _reviewers(producer,role,check_resources=check_resources)


def _reviewers(producer, role='auditor', *, check_resources=True):
    """One owner policy for plan reviews, Work audits and conversation audits.

    Automatic admission comes from the role table, not model-name guessing.
    Explicit route lists retain their order after the existing independence gate.
    """
    from .turn_engine import policy, pick_auditors
    from .work_audit import identity, independence
    if role not in {'auditor', 'plan-auditor'}:
        raise ValueError('REVIEW_ROLE_REQUIRED')
    rules = policy()
    from .execution_scope import current
    if current().changes(role) or rules.get('auditor_selection', 'cross_vendor_frontier') == 'cross_vendor_frontier':
        return ranked(role, producer, check_resources=check_resources)
    chosen = pick_auditors(producer, rules['auditor'], probe=check_resources, role=role)
    author = identity(producer, 'implementer')
    return [deployment for _, deployment in chosen], {
        'schema': 'tare.tools/native-role-selection/1', 'role': role,
        'reason': 'OPERATOR_AUDITOR_ROUTES_CROSS_VENDOR_FIRST',
        'policy_sha256': sha256_canonical({'auditor':rules['auditor'], 'auditor_selection':'configured'}),
        'producer_sha256': author.sha256, 'roster_sha256': author.payload['roster_sha256'],
        'candidates': [{'deployment': name, 'eligible': True, 'position': index+1,
                        'reason': 'SAME_VENDOR_NOT_INDEPENDENT' if independence(producer, deployment) == 'SAME_VENDOR_NOT_INDEPENDENT'
                                  else 'OPERATOR_SELECTED_INDEPENDENT'}
                       for index, (name, deployment) in enumerate(chosen)], 'generation_calls': 0}


def executed_decision(mode, executed_id, considered, skipped=()):
    """Deterministic executed-route record; pure, no inference or inferred alternatives.

    `considered` is the ordered list of known candidates visited; `skipped` is
    [(id, reason)] among them. Only non-skipped candidates get propensity 0/1.
    """
    considered = list(dict.fromkeys(considered))
    skipped = [{'deployment': name, 'reason': reason} for name, reason in skipped]
    gone = {row['deployment'] for row in skipped}
    propensity = {}
    fallback = None
    if executed_id is not None:
        propensity = {name: 1 if name == executed_id else 0 for name in considered if name not in gone}
        propensity[executed_id] = 1
        if considered and considered[0] != executed_id:
            fallback = {'first_ranked': considered[0], 'executed': executed_id, 'skipped': skipped}
    return {'schema': 'tare.tools/executed-route-decision/1', 'policy': 'DETERMINISTIC', 'mode': mode,
            'executed_deployment': executed_id, 'propensity': propensity, 'fallback': fallback,
            'unknown_alternatives': 'NOT_RECORDED', 'ope_supported': False,
            'counterfactual': 'No performance or savings observed for non-executed deployments.'}


_REASONING_KEYS = ('reasoning_effort', 'reasoning', 'effort', 'thinking_effort')


def _reasoning_of(params):
    """Reasoning value among known keys of one parameters object, or UNKNOWN; never a default."""
    import json as _json
    if isinstance(params, str):
        try:
            params = _json.loads(params)
        except ValueError:
            return 'UNKNOWN'
    if not isinstance(params, dict):
        return 'UNKNOWN'
    for scope in (params, params.get('chat_template_kwargs'), params.get('extra_body'), params.get('output_config')):
        if not isinstance(scope, dict):
            continue
        for key in _REASONING_KEYS:
            value = scope.get(key)
            if isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, dict) and isinstance(value.get('effort'), str):
                return value['effort']
    return 'UNKNOWN'


def executed_attempt(deployment, outcome=None):
    """Pure compact record of one executed dispatch attempt.

    Records only the model and reasoning present in the deployment handed to the
    dispatcher (its native_profile parameters first); absent values are UNKNOWN.
    No prompts, keys, profiles or code.
    """
    deployment = deployment if isinstance(deployment, dict) else {}
    profile = deployment.get('native_profile')
    profile = profile if isinstance(profile, dict) else {}
    reasoning = _reasoning_of(profile.get('parameters_json'))
    if reasoning == 'UNKNOWN':
        reasoning = _reasoning_of(deployment.get('parameters_json'))
    choice = deployment.get('native_model_choice')
    choice = choice if isinstance(choice, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    record = {'schema': 'tare.tools/executed-attempt/1',
              'deployment': choice.get('connection') or deployment.get('id') or deployment.get('name') or 'UNKNOWN',
              'model': deployment.get('model') or choice.get('model') or profile.get('model') or 'UNKNOWN',
              'protocol': deployment.get('protocol') or profile.get('protocol') or 'UNKNOWN',
              'reasoning': reasoning,
              'status': outcome.get('status') or 'UNKNOWN'}
    if isinstance(profile.get('model'), str):
        record['runtime_model'] = profile['model']
    for key in ('reason', 'failure_code'):
        if outcome.get(key) is not None:
            record[key] = outcome[key]
    return record


def settle_attempt(record, outcome):
    """Update one attempt record in place with a compact outcome; earlier attempts are never rewritten."""
    outcome = outcome if isinstance(outcome, dict) else {}
    record['status'] = outcome.get('status') or 'UNKNOWN'
    for key in ('reason', 'failure_code'):
        value = outcome.get(key)
        if value is not None:
            record[key] = str(value)[:500]
    return record


def selected_decision(deployment, pinned=False, *, default_mode='CONFIGURED'):
    """Executed decision for a deployment chosen by the existing selection, from its saved provenance only.

    A tare auto choice keeps its tier ladder skips in native_auto_selection; nothing is re-ranked here.
    """
    deployment = deployment if isinstance(deployment, dict) else {}
    executed = deployment.get('id')
    if pinned:
        return executed_decision('EXACT_TEMPORARY_PIN', executed, [executed])
    selection = deployment.get('native_auto_selection')
    if isinstance(selection, dict) and selection.get('route'):
        skipped = [(str(row['route']), str(row.get('reason'))) for row in selection.get('skipped') or []
                   if isinstance(row, dict) and row.get('route')]
        route = selection['route']
        return executed_decision('AUTOMATIC', route, [name for name, _ in skipped] + [route], skipped)
    selection = deployment.get('native_selection')
    if isinstance(selection, dict):
        rows = selection.get('candidates')
        rows = rows if isinstance(rows, list) else []
        eligible = [row for row in rows if isinstance(row, dict) and row.get('eligible') is True]
        if selection.get('reason') == 'EXACT_TEMPORARY_PIN':
            if len(eligible) == 1 and eligible[0].get('selected_deployment') == executed and executed:
                return executed_decision('EXACT_TEMPORARY_PIN', executed, [executed])
        elif str(selection.get('reason', '')).startswith('AUTO_'):
            first = [row for row in eligible if row.get('position') == 1]
            if (executed and len(first) == 1 and first[0].get('selected_deployment') == executed
                    and all(type(row.get('position')) is int and row['position'] >= 1
                            and row.get('selected_deployment') for row in eligible)):
                ordered = sorted(eligible, key=lambda row: row['position'])
                skipped = [(row['deployment'], row.get('reason')) for row in rows
                           if isinstance(row, dict) and row.get('eligible') is False and row.get('deployment')]
                return executed_decision('AUTOMATIC', executed,
                    [row['selected_deployment'] for row in ordered] + [name for name, _ in skipped], skipped)
        return executed_decision('UNKNOWN', executed, [executed])
    return executed_decision(default_mode, executed, [executed])

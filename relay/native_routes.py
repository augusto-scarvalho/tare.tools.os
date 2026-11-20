"""Rank configured native role candidates; existing owners execute and recover them."""
import copy
import json
import os
from pathlib import Path

from graph_backlog.jsonutil import sha256_canonical
from . import native_catalog, native_generation, relay_mesh
from .native_agent import read_json

COST = {'free': 0, 'flat': 1, 'free-credit': 2, 'metered': 3}


# The class (model_signals.ORDER) each role wants. Human policy, not a measurement, and the one place to change it
# (owner, 2026-11-15): planning is where the strongest model pays, so the planner wants frontier (Astra, Fable,
# Carbon). Checking code and plans for defects is done as well or better by the class below (Sol, Opus, Argon), so
# the reviewers want middling. A route that falls short goes last; one above the wanted class is used after the
# exact one on the same kind of allowance. What a user wants otherwise is ticked in /routes > Models per role, and the class a role asks for is chosen in /routes > Order (wanted()).
# The implementer and a conversation (coordinator) want cheap: the cheapest sufficient route first, the class nearest
# the floor first on each kind of allowance. tare auto's tiers are floors of class over this order, raised on a
# failure (turn_engine.implementer_tiers); a unit's `needs` reorders its own routes.
WANTED = {'planner': 'frontier', 'plan-auditor': 'middling', 'auditor': 'middling', 'implementer': 'cheap',
          'coordinator': 'cheap'}
def wanted(role):
    """The class `role` asks for: the owner's choice (/routes > Order, the routing policy's `wanted`), else WANTED.
    A saved value that is not a class of a role of WANTED counts as not set."""
    from .model_signals import ORDER
    from .turn_engine import policy
    try:
        choice = policy().get('wanted', {}).get(role)
    except (OSError, ValueError, AttributeError):
        choice = None
    return choice if role in WANTED and choice in ORDER else WANTED.get(role)


INTERNAL_WANTED = {'researcher': 'cheap', 'vision': 'cheap'}
CLI_ENDPOINTS = {'cli://codex', 'cli://claude', 'cli://agy'}
CLI_DOMAINS = {'anthropic-cli', 'google-cli', 'openai-cli', 'codex-cli'}


# The roles that want the cheapest sufficient route: among pay-per-token routes it is the best score per dollar of a
# task (model_signals.task), the planner's frontier is bought for its strength. Owner, 2026-11-16.
CHEAPEST = {'implementer', 'coordinator', 'auditor', 'plan-auditor', 'researcher', 'vision'}


_derived = (None, [])  # (the table's path and the installed route names, the rows derived for them)


def configured():
    """The role table: the shipped rows, then the rows derived() adds for this machine's routes."""
    path = Path(os.environ.get('TARE_ROLE_DEPLOYMENTS') or Path(__file__).with_name('role_deployments.json'))
    from .round_table_roster import load_role_deployments
    roster = read_json(native_catalog.roster_path(), 4*1024*1024)
    table = load_role_deployments(path, roster=roster)
    global _derived
    from .native_profiles import names
    try:
        stamp = (str(path), tuple(sorted(names())))
    except (ValueError, OSError):
        stamp = None
    if stamp is None or _derived[0] != stamp:  # deriving reads every unnamed profile: once per set of routes
        _derived = (stamp, derived(table['deployments'], roster))
    named = {native_catalog.LOCAL.get(n, n) for row in table['deployments']
             for n in (row['id'], row.get('native_catalog'))}
    # A local model in use that no row names gets a row with no role: it holds one only where the user grants it in
    # /routes (grant(); owner, 2026-11-17: tare never decides alone that a local model may plan or review).
    table['deployments'] += copy.deepcopy(_derived[1]) + [
        {'id': name, 'native_catalog': name, 'model': model, 'runner': 'native-http', 'roles': [], 'kind': 'primary',
         'cost_tier': 'free', 'note': 'Derived at load: a local model in use here; it holds the roles granted in '
                                      '/routes.'}
        for name, model in local_models().items() if name not in named]
    return table


_local = (None, {})  # (the saved gateways and the installed names, local_models())


def local_models():
    """{catalogue name: model id} of the local models in use here (environment.in_use): each shipped profile whose model
    the first saved gateway publishes and uses (local-auto, a host-aware alias, aside) and each generic profile tare
    setup wrote for a model a saved gateway uses. Read again when the gateways or the installed profiles change."""
    global _local
    from .environment import generic_profiles, in_use, read, shipped_local
    from .native_profiles import directory, names
    value = read()
    section = value.get('local_models') or {}
    gateways = section.get('gateways') or [] if section.get('use') else []
    if not gateways:
        return {}  # no local gateway saved: nothing to read
    try:
        installed = names()
        stamp = json.dumps([gateways, str(directory()), installed], sort_keys=True, default=str)
    except (ValueError, OSError):
        return {}
    if _local[0] != stamp:
        found, first = {}, in_use(gateways[0]) if gateways else set()
        shipped = sorted({native_catalog.LOCAL[n] for n in native_catalog.LOCAL if shipped_local(n)} if first else ())
        for name in [*shipped, *(n for n in generic_profiles(value) if n in installed)]:
            try:
                deployment = native_catalog.select(name)
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if name not in shipped or ('native_host_modes' not in deployment
                                       and deployment['native_profile']['model'] in first):
                found[name] = deployment.get('model')
        _local = (stamp, found)
    return dict(_local[1])


def derived(rows, roster):
    """Rows for routes installed here that no row names, so that a newer version of a known line is selectable and
    pinnable at once (owner, 2026-11-13). A route gets one when its model has a line and its endpoint is that vendor's
    own (vendor_models.origin_line: never a reseller or a local model), rows exist for that line on the same source
    (endpoint origin and protocol: codex-cli-gpt-6-1-sol for codex-cli-gpt-6-2-sol), all of older versions, and its
    identity is established. It copies the newest such row's policy under its own name and
    model, pin only (qualified false) until it answers once here (proven). Built at each load and never saved: a written
    row always wins, and with no such route installed the table is exactly the shipped one."""
    from .cli_discovery import TEMPLATES
    from .environment import origin
    from .native_profiles import names
    from .vendor_models import AGY_EFFORT, line, origin_line, parse
    from .work_audit import identity

    def source(value):  # (line, version, (endpoint origin, protocol)) of a vendor's own model, else None
        profile = value['native_profile']
        found = origin_line(profile['model'], profile['endpoint'])
        return found and (found['line'], parse(AGY_EFFORT.sub('', profile['model']))[2],
                          (origin(profile['endpoint']), profile['protocol']))
    sources = {}

    def shipped(name):  # a row's source: its route here, else its CLI template (not set up here), else None
        if name not in sources:
            try:
                value = native_catalog.select(name)
            except (ValueError, KeyError, TypeError, OSError):
                value = json.loads(TEMPLATES.read_text(encoding='utf-8')).get(name)
            try:
                sources[name] = value and source(value)
            except (KeyError, TypeError):
                sources[name] = None
        return sources[name]
    named = {name for row in rows for name in (row['id'], row.get('native_catalog'))}
    try:
        installed = [name for name in names() if name not in named]
    except (ValueError, OSError):
        return []
    found = []
    for name in installed:
        try:
            value = native_catalog.select(name)
            mine = source(value)
            if not mine:
                continue
            same = []
            for row in rows:
                wire = ((roster['models'].get(row['model']) or {}).get('runtime') or {}).get('native-http')
                if row.get('native_catalog') and wire and (line(wire) or {}).get('line') == mine[0]:
                    theirs = shipped(row['native_catalog'])  # resolved only for the rows of this line
                    if theirs and (theirs[0], theirs[2]) == (mine[0], mine[2]):
                        same.append((theirs[1], row))
            if not same or max(version for version, _ in same) >= mine[1]:
                continue  # only a version newer than every written one: an older model gains nothing here
            sibling = max(same, key=lambda item: item[0])[1]  # the newest version; a tie keeps table order
            identity(value, 'auditor')
        except (ValueError, KeyError, TypeError, OSError):
            continue
        found.append({**copy.deepcopy(sibling), 'id': name, 'native_catalog': name, 'model': value['model'],
                      'qualified': False,
                      'note': f"Derived at load from {sibling['id']}, the version before it on the same source; "
                              f"pin only until it answers once here (tare setup --prove {name})."})
    return found


def roles(row, scope):
    """A role-table row's roles; in --local_only mode also its local_only_roles (an operator admission by choice
    of that mode, not a quality claim)."""
    return [*row.get('roles', []), *(row.get('local_only_roles', []) if scope.only == 'local' else [])]


def _cli_row(row):
    """Whether `row` represents one of the vendor CLIs supported for web research and vision."""
    if 'native_profile' in row:
        profile = row['native_profile']
        return profile.get('protocol') == 'vendor-cli' and profile.get('endpoint') in CLI_ENDPOINTS
    return row.get('endpoint_domain') in CLI_DOMAINS


def role_admitted(row, role, scope):
    """Whether `row` belongs to `role` in the role table (before grants and live transport verification)."""
    if role == 'coordinator':
        return 'implementer' in roles(row, scope)
    if role in {'vision', 'researcher'}:
        return role in roles(row, scope) or _cli_row(row)
    return role in roles(row, scope)


def grant(row, role, assumed=None):
    """Whether the user granted `role` to this row's route (environment.granted: an "on" naming a local route itself).
    `assumed` True takes the grant as given (the /routes matrix shows what a tick would do); either way only a local
    route can hold one."""
    from .environment import granted, profile, source
    entries = (profile(role=role)[0].get(role) or {}).get('on', ())
    if assumed is None and not any(isinstance(rule, str) for *_, rule in entries):
        return False  # no "on" for this role names a route: nothing to resolve
    try:
        deployment = native_catalog.select(row['native_catalog']) if row.get('native_catalog') else row
        return bool(assumed) and source(deployment) == 'local' or granted(role, deployment)
    except (ValueError, KeyError, TypeError, OSError):
        return False


def credential_available(name):
    return bool(os.environ.get(name) or native_generation.vault_secret(name))


def ranked(role, producer=None, *, check_resources=False, work_type=None):
    from .execution_scope import for_role
    from .execution_scope import current, pinned
    if current().pin(role):
        value = pinned(role, producer, work_type)
        return [value], {'schema': 'tare.tools/native-role-selection/1', 'role': role,
                         'reason': 'EXACT_TEMPORARY_PIN', 'generation_calls': 0,
                         'candidates': [{'eligible': True, 'catalog': value['native_model_choice']['connection'],
                                         'selected_deployment': value['id']}]}
    with for_role(role):return _ranked(role,producer,check_resources=check_resources,work_type=work_type)


def proven(row, cache, role=None, mirrors=True):
    """Whether a pin-only row is used automatically on this machine: its setup holds a successful proof (tare setup
    --prove) under the row's catalogue name, the name prove() records for this route, and either the route serves its
    vendor's own model (vendor_models.origin_line: True) or, with `mirrors`, it mirrors a model another row already
    serves automatically for `role` (None: any role; mirror(): that row). A local model, or a reseller's model no
    other row serves, stays pin-only, proof or not; an inline row has no name tare setup --prove reaches. The setup
    is read once per `cache` (admission's)."""
    name = row.get('native_catalog')
    if not name:
        return False
    if ('proofs',) not in cache:
        from .environment import read
        proofs = read().get('proofs')
        cache[('proofs',)] = proofs if isinstance(proofs, dict) else {}
    record = cache[('proofs',)].get(name)
    if not isinstance(record, dict) or record.get('ok') is not True:
        return False
    from .vendor_models import origin_line
    try:
        deployment = native_catalog.select(name)
        if origin_line(deployment['native_profile']['model'], deployment['native_profile']['endpoint']):
            return True
    except (ValueError, KeyError, TypeError, OSError):
        return False
    return mirrors and mirror(row, deployment, role, cache) or False


def mirror(row, deployment, role=None, cache=None):
    """The row whose model `row`'s route (`deployment`) serves under another catalogue (vendor_models.same_model)
    and that automatic selection already takes (not pin only, or proven by its own vendor's line, never through
    another mirror) for `role` (None: any role), which `row` has too; None when no row does. A proof then admits `row`
    for that role, ranked as that row's reserve (owner, 2026-11-13: a mirrored model is pinnable until one proof call
    on its route, then a reserve after every origin route)."""
    from .execution_scope import current
    from .vendor_models import same_model
    cache = {} if cache is None else cache
    if role is not None and not role_admitted(row, role, current()):
        return None
    wanted = set(roles(row, current())) if role is None else None
    for other in configured()['deployments']:
        name = other.get('native_catalog')
        if (not name or name == row.get('native_catalog')
                or (not role_admitted(other, role, current()) if role is not None
                    else not (wanted & set(roles(other, current()))))
                or other.get('qualified') is False and not proven(other, cache, mirrors=False)):
            continue
        try:
            if same_model(deployment, native_catalog.select(name)):
                return other
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return None


def admission(row, role, author=None, credentials=None, granted=None, producer=None, work_type=None, kept=None):
    """(deployment, auditor identity, reason) of one role-table row under the rules that come before the routing
    profile and any live probe; reason None when they admit it. `author`: the producer's identity, for a review role
    (None checks no independence). `credentials`: a cache shared across rows (credentials, logins, this machine's
    proofs). `granted`: grant()'s `assumed`. `producer`: the producer's deployment, for a review role: a reviewer
    two categories or more below it for `work_type` (`model_signals.may_review`; `kept`, the boards) is refused. The matrix of
    /routes reads it too (environment.auto_routes), so what it shows cannot drift from what ranks."""
    from .work_audit import identity
    from .openrouter import is_route as is_openrouter_route
    from .execution_scope import constrained, current, same_family_admitted
    credentials = {} if credentials is None else credentials
    # A conversation's model is admitted as an implementer row; the routing profile then judges it as coordinator.
    # A local route also holds a role the user granted it in /routes, in any scope (except researcher and vision).
    if not role_admitted(row, role, current()):
        if role in {'researcher', 'vision'} or not grant(row, role, granted):
            return None, None, 'ROLE_NOT_CONFIGURED'
    elif (row.get('qualified') is False and not proven(row, credentials, role)
          and (role in {'researcher', 'vision'} or not grant(row, role, granted))):
        # Registered from a provider's metadata: an exact pin reaches it, automatic selection waits for a live
        # qualification through tare: one answer here (tare setup --prove NAME) for a vendor's own model, or for a
        # mirror of a model another row serves automatically in this role. The user's grant is consent enough.
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
        # A review role needs an auditor identity (a model the roster says never audits has none); others the work's.
        auditor = identity(deployment, 'auditor' if role in ('auditor', 'plan-auditor') else 'implementer')
    except (ValueError, KeyError, TypeError, OSError):
        return None, None, 'PROFILE_OR_IDENTITY_INVALID'
    deployment=constrained(deployment,role)
    profile = deployment['native_profile']
    is_cli = profile.get('protocol') == 'vendor-cli' and profile.get('endpoint') in CLI_ENDPOINTS
    if role == 'vision' and not is_cli:
        return deployment, auditor, 'ROLE_NOT_CONFIGURED'
    if role == 'researcher' and not (is_cli or is_openrouter_route(deployment) and 'researcher' in roles(row, current())):
        return deployment, auditor, 'ROLE_NOT_CONFIGURED'
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
    from .model_signals import may_review
    if producer is not None and role in ('auditor', 'plan-auditor') and not may_review(deployment, producer, work_type, kept):
        return deployment, auditor, 'REVIEWER_BELOW_AUTHOR'
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


def _ranked(role, producer=None, *, check_resources=False, work_type=None):
    """No inference. New automatic dispatch can also observe current capacity. `work_type`: the task type of the unit
    whose change is reviewed, or the types of a demand (a list: its planner and plan reviewer); it changes only
    whether a route falls short of the role's class (`model_signals.judged`), by its weakest type."""
    from .work_audit import identity
    if role not in {'planner', 'plan-auditor', 'auditor', 'implementer', 'coordinator', 'explorer', 'researcher', 'vision'} or (role in {'plan-auditor', 'auditor'} and producer is None):
        raise ValueError('ROLE_SELECTION_REQUIRES_SUPPORTED_ROLE_AND_AUTHOR')
    author = identity(producer, 'implementer') if producer is not None else None
    table = configured(); decisions = []; eligible = []; credentials = {}; seen = set()
    from . import route_facts
    from .execution_scope import current
    from .provider_quota import lapse_order, surplus
    lapsing = {}  # one ranking reads the quota file and each account's spend once
    fact_cache = {}; evidence = route_facts.supplied_evidence(); observed_facts = {}
    from .turn_engine import cross_vendor_first
    first = bool(author) and cross_vendor_first()  # off: --any_vendor_auditor or /routes
    from .model_signals import ORDER, boards, judged, klass, task
    need = wanted(role) or INTERNAL_WANTED.get(role)
    kinds = [work_type] if isinstance(work_type, str) else list(work_type or ())
    kept = boards() if kinds else None  # read once for the whole ranking
    try:
        fleet = native_catalog.local_default()
    except (OSError, ValueError, KeyError, TypeError):
        fleet = None  # unreadable setup: no model is the default here
    for order, row in enumerate(table['deployments']):
        decision = {'deployment': row['id'], 'eligible': False}
        decisions.append(decision)
        deployment, auditor, reason = admission(row, role, author, credentials, producer=producer, work_type=kinds, kept=kept)
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
        # Classes short of the wanted one, and above it: a model with no class (no line, nothing placing it) counts as cheap.
        found = klass(deployment) or {}
        # For a unit of a task type, the class a board gives the model for that type, else its general class (owner,
        # 2026-11-16: an auditor that knows nothing of the type is no use, however strong in general).
        # Several types (a demand's): the largest shortfall over them, as the weakest link.
        used = [judged(deployment, t, kept) for t in kinds]
        general = ORDER.index(found.get('class') or 'cheap') - ORDER.index(need) if need else 0
        gap = min(ORDER.index(u['class']) for u in used) - ORDER.index(need) if used and need else general
        # The type decides only whether a route falls short. "The exact class before one above it" stays a matter of
        # the general class: by the type's class, a model that is better at the type than the role asks went after the
        # others (on the first real boards a legal unit's review moved from the second best model at legal work to
        # the ninth).
        fall, above = max(0, -gap), max(0, general)
        # After a current model of a known line in the same class: an older version, or a model with no class at all.
        dated = bool(need) and ('older_than' in found or not found)
        decision['classes_short'] = fall
        if used:
            one = isinstance(work_type, str)  # a unit's one type is recorded as before; a demand's types as lists
            sources = [u['source'] for u in used]
            decision.update(kind=work_type if one else kinds, class_source=sources[0] if one else sources)
        cli_backup = (role in {'auditor', 'plan-auditor'} and
                      (deployment['native_profile']['endpoint'] == 'cli://codex' or row['id'] == 'claude-cli'))
        signal = surplus(deployment, cache=lapsing)  # an allowance about to lapse, in surplus: None otherwise
        if signal:
            decision['expiring'] = signal
        # An independent auditor always precedes an admitted same-family one, and another vendor precedes the same
        # vendor before any preference, class or tier: a same-vendor auditor runs only when no other is eligible.
        # With `first` off the vendor leaves the order. Then the user's preference, a route that reaches the wanted
        # class before one that falls short (move `fall` to change how much the class weighs against allowance and
        # tier), a lapsing allowance, tier, the exact class before one above it (Sol before Astra on one
        # subscription, from the class and not from a number per route), a current model before an older version or a model with no line
        # that lands in the same class (Opus 5.5 before Fable 5.1), a proven mirror last, primary, CLI backup, table order.
        # For an implementer or a conversation a route with no class also comes after every route that has one: a last
        # resort, so a machine with only unmeasured routes still runs; the node's own default model is not one.
        # In any role a local model the user granted it (its row does not give the role) and with no class goes after
        # every route with one, and among such routes in the order /routes shows them (`shown`: by label, then id).
        by_grant = not role_admitted(row, role, current()) and grant(row, role)
        unclassed = not found and (native_catalog.LOCAL.get(row.get('native_catalog', row['id']), row.get(
            'native_catalog', row['id'])) != fleet if role in {'implementer', 'coordinator', 'researcher', 'vision'} else by_grant)
        if unclassed and by_grant:
            from .model_choices import describe
            shown = (describe(deployment['id'], deployment)['label'] or deployment['id'], deployment['id'])
        else:
            shown = ()
        # Among pay-per-token routes tied up to here, a route with a figure goes first, the best value first; a metered
        # route without one, and every other kind of allowance, keep their order (the term is the same for them).
        value = (task(deployment) or {}).get('value') if role in CHEAPEST and row['cost_tier'] == 'metered' else None
        # A pin-only row admitted as a mirror (proven() names the row it mirrors) is that row's reserve: tied up to
        # here it goes after every route admitted otherwise; the terms before order it as any route (a cheaper
        # allowance still goes first).
        mirrored = row.get('qualified') is False and proven(row, credentials, role) is not True
        rank = (same_family, first and not cross, current().preference(deployment,role), fall, unclassed, lapse_order(signal), COST[row['cost_tier']], (value is None, -(value or 0)), above, dated, mirrored, row['kind'] != 'primary', cli_backup, shown, order)
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
        'reason': 'AUTO_CROSS_VENDOR_THEN_COST_AND_CONFIGURED_ORDER' if first else 'AUTO_COST_AND_CONFIGURED_ORDER',
        'policy_sha256': sha256_canonical(table), 'producer_sha256': author.sha256 if author else None,
        'roster_sha256': author.payload['roster_sha256'] if author else None,
        'candidates': decisions, 'generation_calls': 0,
        **({'expiring_first': True} if any(d.get('expiring') for _, _, d in eligible) else {}),
        'cost_scope': ('Wanted class of the role, then configured cost tiers' if need else 'Configured cost tiers')
                      + '; then, among pay-per-token routes, the score per dollar of a task on the LMArena agent board when listed'
                      + '; no price estimate.'}


def independent_auditors(producer):
    return reviewers(producer)


def reviewers(producer, role='auditor', *, check_resources=True, work_type=None):
    from .execution_scope import for_role
    with for_role(role):return _reviewers(producer,role,check_resources=check_resources,work_type=work_type)


def _reviewers(producer, role='auditor', *, check_resources=True, work_type=None):
    """One owner policy for plan reviews, Work audits and conversation audits.

    Automatic admission comes from the role table, not model-name guessing.
    Explicit route lists retain their order after the existing independence gate.
    `work_type`, a unit's task type (or a demand's types, for a plan's reviewer), ranks the automatic choice by the class
    for it (`_ranked`); never the owner's list.
    """
    from .turn_engine import cross_vendor_first, policy, pick_auditors
    from .work_audit import identity, independence
    if role not in {'auditor', 'plan-auditor'}:
        raise ValueError('REVIEW_ROLE_REQUIRED')
    rules = policy()
    from .execution_scope import current
    if current().changes(role) or rules.get('auditor_selection', 'cross_vendor_frontier') == 'cross_vendor_frontier':
        return ranked(role, producer, check_resources=check_resources, work_type=work_type)
    chosen = pick_auditors(producer, rules['auditor'], probe=check_resources, role=role)
    author = identity(producer, 'implementer')
    return [deployment for _, deployment in chosen], {
        'schema': 'tare.tools/native-role-selection/1', 'role': role,
        'reason': 'OPERATOR_AUDITOR_ROUTES_CROSS_VENDOR_FIRST' if cross_vendor_first() else 'OPERATOR_AUDITOR_ROUTES_IN_ORDER',
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

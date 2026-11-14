"""Offline model presentation and immutable choices over existing deployments."""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import native_catalog
from .challenger import local
from .native_generation import deployment_profile, load_deployment

EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
OPENROUTER_ENDPOINT = 'https://openrouter.ai/api/v1'
# Models outside relay/model_lines.json: local models and reseller ids. A first-party version takes its line's label.
LABELS = {'qwen38': 'Qwen3.8 27B', 'hauhaucs': 'HauhauCS · Qwen3.8',
          'fable-tc': 'Fable-TC', 'qwen36-moe': 'Qwen3.6 MoE', 'swift-next': 'Swift 1.5 Flash-Next',
          'qwen38-gsq': 'Qwen3.8 27B GSQ · NInfer', 'swift27b': 'Swift 1.5 27B · NInfer',
          'us.anthropic.claude-opus-4-6-v1': 'Claude Opus 4.6',
          'us.anthropic.claude-sonnet-4-6': 'Claude Sonnet 4.6', 'zai.glm-5': 'GLM-5',
          'deepseek/deepseek-v4.1-flash': 'DeepSeek V4.1 Flash',
          'z-ai/glm-5.3': 'GLM 5.3', 'xiaomi/mimo-v2.6-pro': 'MiMo V2.6 Pro (Preview)'}
SCHEMA = 'tare.tools/model-preferences/1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def model_label(model):
    """LABELS' name for `model`, else its line's (claude-fable-5-3 is Claude Fable 5.3), else None."""
    from .vendor_models import line
    known = line(model)
    return LABELS.get(model) or known and known['label']


def describe(name, value):
    profile = value['native_profile']
    model, protocol = profile['model'], profile['protocol']
    params = json.loads(profile.get('parameters_json', '{}'))
    endpoint = profile['endpoint']
    from .execution_team import reasoning
    from .vendor_models import line
    declared_effort = reasoning(value)
    effort, supported = (declared_effort if protocol in {'anthropic-messages', 'bedrock-converse'}
                         and declared_effort in EFFORTS else ''), []
    key = model
    label = model_label(model) or model
    known = line(model)
    group, connection = 'Local', 'Local GPU'
    if protocol == 'vendor-cli':
        effort = params.get('effort', 'high')
        group, connection = {'cli://claude': ('Anthropic', 'Claude CLI'),
                             'cli://codex': ('OpenAI', 'Codex CLI'),
                             'cli://agy': ('Google', 'AGY CLI')}[endpoint]
        if endpoint == 'cli://claude' and known:  # Claude Code lists no levels: the line's apply
            supported = known['efforts']
        elif endpoint == 'cli://codex':
            record = value['native_cli']['model_catalog']
            supported = [r['effort'] for r in record.get('supported_reasoning_levels', [])
                         if r.get('effort') in EFFORTS]
        elif endpoint == 'cli://agy':
            mapping = value.get('native_reasoning_models', {effort: model})
            supported = [e for e in EFFORTS[:3] if e in mapping]
            if model.endswith(('-low', '-medium', '-high')):
                key = model.rsplit('-', 1)[0]
            label = model_label(key) or key.replace('gemini-', 'Gemini ').replace('-flash', ' Flash').replace('-pro', ' Pro')
    elif protocol == 'openai-codex-responses':
        group, connection = 'OpenAI', 'Codex Direct'
        effort = params.get('reasoning', {}).get('effort', '')
        supported = [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS]
    elif value.get('native_credential_env') in {'OPENAI_API_KEY', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'}:
        group, connection = {'OPENAI_API_KEY': ('OpenAI', 'OpenAI API'),
                             'GEMINI_API_KEY': ('Google', 'Gemini API'),
                             'ANTHROPIC_API_KEY': ('Anthropic', 'Claude API')}[value['native_credential_env']]
        if protocol == 'openai-responses':
            effort = params.get('reasoning', {}).get('effort', '')
            supported = [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS]
        elif protocol == 'openai-chat' and value['native_credential_env'] == 'GEMINI_API_KEY':
            effort = params.get('reasoning_effort', '')
            supported = [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS[:3]]
        elif protocol == 'anthropic-messages':
            effort = params.get('output_config', {}).get('effort', '')
            # Documented top-level effort for Claude lines (relay/model_lines.json).
            # Custom endpoints need their own declared levels; a lookalike URL is no evidence.
            known = endpoint.rstrip('/') == 'https://api.anthropic.com/v1' and known
            supported = known['efforts'] if known else [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS]
    elif value.get('native_opencode'):
        from .opencode import connection as opencode_connection
        group = connection = opencode_connection(value)
        label = value['native_opencode'].get('label', label)
        effort = {'openai-chat': params.get('reasoning_effort'),
                  'openai-responses': params.get('reasoning', {}).get('effort'),
                  'anthropic-messages': params.get('output_config', {}).get('effort'),
                  'gemini-generate-content': params.get('thinkingConfig', {}).get('thinkingLevel')}.get(protocol) or ''
        supported = [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS]
    elif protocol == 'bedrock-converse':
        group, connection = 'AWS Bedrock', 'AWS Bedrock'
    elif protocol in {'openai-chat', 'openai-responses'} and endpoint.rstrip('/') == OPENROUTER_ENDPOINT:
        group, connection = 'OpenRouter', 'OpenRouter API'
        label = value.get('native_openrouter', {}).get('label', label)
        effort = params.get('reasoning', {}).get('effort', '')
        supported = [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS]
    elif protocol == 'openai-chat' and local(value):
        effort = params.get('chat_template_kwargs', {}).get('reasoning_effort', '')
        supported = [e for e in value.get('native_reasoning_levels', []) if e in EFFORTS]
    elif protocol != 'openai-chat':
        group, connection = 'Custom', protocol
    elif not local(value):  # an OpenAI-compatible provider on the internet, not this machine's GPU
        group, connection = 'API', 'API · ' + (urlsplit(endpoint).hostname or endpoint)
    if 'native_auto_routes' in value:
        # Resolved when a new session starts; a blocked or exhausted turn continues on the next tier.
        key, label, group, connection = name, 'tare auto', 'tare', 'local → cheap → intermediate'
        from .execution_scope import current
        scope=current()
        if scope.only or scope.prefer=='local':
            connection={'subscription':'Subscription policy','api':'API policy','local':'Local policy'}[scope.only or 'local']
        effort, supported = '', []
    elif 'native_host_modes' in value:
        # Resolved when a new session starts: Swift Flash-Next while the host PC is idle, else Qwen3.8.
        key, label, connection = name, 'Local auto', 'Local GPU · host-aware'
    if group == 'OpenAI':
        label = model_label(model) or model.replace('gpt-', 'GPT-', 1)
        if protocol == 'openai-responses' and params.get('reasoning', {}).get('mode') == 'pro':
            # A picker identity, never a wire model ID: Pro is an API execution mode.
            key, label, connection = model+'::pro', label+' Pro', 'OpenAI API · Pro mode'
    # An existing profile remains usable even when its capabilities aren't catalogued.
    if effort and effort not in supported and not value.get('native_reasoning_levels'):
        supported.append(effort)
    from .openrouter import details
    return {'key': key, 'label': label, 'group': group, 'route': {
        'id': name, 'label': connection, 'efforts': supported, 'default_effort': effort,
        'fingerprint': digest(value), 'model': model, 'protocol': protocol, 'openrouter': details(value)}}


ROLES = ('coordinator', 'planner', 'implementer', 'explorer', 'researcher', 'auditor', 'plan-auditor')


def model_author(value):
    """Only the attested roster identity proves authorship, never the transport."""
    from .work_audit import identity
    try:
        return identity(value, 'implementer').vendor
    except (ValueError, KeyError, TypeError, OSError):
        return 'UNKNOWN'


def filter_domains(deployments):
    from .inference_allowlist import PROVIDERS, identities
    vendors = {model_author(value).casefold() for _, value, _ in deployments}
    vendors.discard('unknown')
    try:
        roster = json.loads(native_catalog.roster_path().read_text(encoding='utf-8'))
        vendors.update(row['vendor'].casefold() for row in roster['models'].values()
                       if isinstance(row, dict) and isinstance(row.get('vendor'), str) and row['vendor'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    providers = dict(PROVIDERS)
    for name, value, _ in deployments:
        connection = identities(name, value)[0]
        providers[connection.casefold()] = connection
    return vendors, providers


def projection(*, role=None, via=None, vendor=None, provider=None, include_unconfigured=False, scope=None):
    """Shared offline identity and allowed-effort projection; availability is never inferred."""
    from .execution_scope import current, kind, is_pro
    from .coordinator_policy import APPROVED
    from .inference_allowlist import identities
    from .environment import listed, read
    from .native_routes import configured, roles as table_roles
    from .turn_engine import policy
    from .web_research import DEFAULT_ROUTES
    if role is not None and role not in ROLES:
        raise ValueError('Unknown role; consult tare models --help.')
    scope = current() if scope is None else scope
    deployments = list(native_catalog.offline_deployments(include_unconfigured=True))
    vendors, providers = filter_domains(deployments)
    if vendor is not None and vendor.casefold() not in vendors:
        raise ValueError('Unknown vendor (model author): '+vendor+'. Consult tare models --include-unconfigured; known authors: '+', '.join(sorted(vendors))+'.')
    if provider is not None:
        if provider.casefold() not in providers:
            raise ValueError('Unknown provider (connection): '+provider+'. Consult tare models --include-unconfigured; known providers/aliases: '+', '.join(sorted(providers))+'.')
        provider = providers[provider.casefold()]
    if via is not None and via not in ('cli', 'api', 'local'):
        raise ValueError('Unknown access; consult tare models --help (--via cli|api|local).')
    table = configured()['deployments']
    researchers = policy().get('researcher', DEFAULT_ROUTES)
    setup = read()
    # Work dispatch also admits the operator's configured implementation
    # ladder; the static review-role table is not that ladder's authority.
    # Aliases name the profile dispatch uses: local-qwen and qwen38 are qwen38-gsq, local-default the fleet default.
    alias = lambda n: native_catalog.LOCAL.get(n, n)
    try:
        fleet = native_catalog.local_default(setup)
    except (OSError, ValueError, KeyError, TypeError):
        fleet = native_catalog.LOCAL_DEFAULT
    implementers = {alias(fleet if name == native_catalog.LOCAL_DEFAULT else name)
                    for tier in policy().get('implementer', []) for name in tier}
    result = []
    for name, value, installed in deployments:
        if 'native_auto_routes' in value or 'native_host_modes' in value:
            continue  # Exact pins never name a future automatic choice.
        item = describe(name, value)
        connection, _, route = identities(name, value)
        roles = sorted({r for row in table if alias(row.get('native_catalog', row['id'])) == alias(name)
                        for r in table_roles(row, scope) if r != 'coordinator'})
        if alias(name) in implementers:
            roles.append('implementer')
        # Explicit coordinators use their existing policy, not delegated-route roles.
        # Live credentials/resource admission remains the responsibility of select().
        if item['key'] in APPROVED and not is_pro(value):
            roles.append('coordinator')
        if name in researchers and value['native_profile']['protocol'] == 'vendor-cli':
            roles.append('researcher')
        access = ('cli' if value['native_profile']['protocol'] == 'vendor-cli'
                  else {'subscription': 'api', 'subscription_direct': 'api'}.get(kind(value), kind(value)))
        author = model_author(value)
        if role and role not in roles or via and via != access or vendor and vendor.casefold() != author.casefold() or provider and provider != connection:
            continue
        by_role = {}
        for selected_role in ([role] if role else sorted(set(roles) & set(ROLES))):
            allowed = []
            levels = route['efforts'] or (['default'] if value.get('native_openrouter') or value.get('native_opencode') else [])
            for level in levels:
                if selected_role == 'planner' and level not in ('high', 'xhigh', 'max'):
                    continue
                # Mirror coordinator_policy.select's hosted high/local declared levels.
                if selected_role == 'coordinator' and route['model'] not in ('qwen38-gsq', 'swift-next') and level != 'high':
                    continue
                candidate = deepcopy(value)
                candidate['native_profile']['parameters_json'] = json.dumps(reasoning_parameters(candidate, level), sort_keys=True)
                if candidate['native_profile']['endpoint'] == 'cli://agy':
                    candidate['native_profile']['model'] = candidate.get('native_reasoning_models', {}).get(level, route['model'])
                candidate['native_model_choice'] = {'connection': name}
                if scope.allows(candidate, selected_role)[0]:
                    allowed.append(level)
            by_role[selected_role] = allowed
        allowed = [level for level in dict.fromkeys((*route['efforts'], 'default')) if any(level in levels for levels in by_role.values())]
        from .execution_scope import POLICY_VIEW, Scope
        token = POLICY_VIEW.set(Scope())
        try:
            configured_here = installed and listed(name, value, setup)
        finally:
            POLICY_VIEW.reset(token)
        if not include_unconfigured and not configured_here:
            continue
        result.append({'deployment': name, 'model': route['model'], 'label': item['label'], 'vendor': author,
                       'provider': connection, 'connection': route['label'], 'via': access,
                       'roles': sorted(set(roles)), 'configured': configured_here,
                       'supported_reasoning': route['efforts'], 'allowed_reasoning': allowed,
                       'selectors': [connection+'/'+name+'@'+level for level in allowed] if configured_here else [],
                       'role_arguments': {r: ['--'+('auditor' if r == 'plan-auditor' else r)+' '+connection+'/'+name+'@'+level
                                              for level in levels] if configured_here else []
                                          for r, levels in by_role.items()},
                       'configuration_evidence': 'Loaded profile and local setup eligibility only; authentication, availability and quota UNVERIFIED',
                       'availability': 'UNVERIFIED', 'quota': 'UNVERIFIED'})
        if route.get('openrouter'):
            result[-1]['openrouter'] = route['openrouter']
    return result


def select_exact(raw, role, *, scope=None):
    from .inference_allowlist import exact, parse
    pin = exact(raw)
    provider, name, levels = parse(pin)[0]
    rows = projection(role=role, include_unconfigured=True, scope=scope)
    matches = [r for r in rows if r['provider'] == provider and r['deployment'] == name]
    if len(matches) != 1 or not matches[0]['configured'] or levels[0] not in matches[0]['allowed_reasoning']:
        raise ValueError('Pin is unknown, unconfigured or incompatible; consult tare models --role '+role+'. No fallback selected.')
    value = native_catalog.select(name)
    item = describe(name, value)
    choices = [{k: item[k] for k in ('key', 'label', 'group')} | {'routes': [item['route']]}]
    return realization(choices, {'key': item['key'], 'connection': name, 'reasoning': levels[0]}, deployment=value)


PIN_HELP = ('Temporary exact pins: --planner, --implementer, --explorer, --researcher, --auditor '
            'PROVIDER/DEPLOYMENT@REASONING (syntax placeholders, not a proved connection). '
            'Auditor also selects plan-auditor; planner requires high or above. '
            'Instance and children only; reopening does not restore flags. No fallback. '
            'Author means registered model identity; provider means connection, not author. '
            'Explicit --coordinator PROVIDER/DEPLOYMENT@REASONING uses the existing coordinator policy: '
            'hosted high or declared local levels; it is separate from temporary role pins and preserves conversation selection on reopen. '
            'Discover it with tare models --role coordinator; plan-auditor uses --auditor, not a new flag. '
            'Offline alternatives: tare models --role planner --via cli --include-unconfigured. '
            'Configuration proves only a loaded profile and local eligibility; authentication, availability and quota UNVERIFIED.')


def help_summary():
    """Read-only local summary shared by installed entry points and conversation help."""
    try:
        rows = projection()
        connections = sorted({r['vendor']+' / '+r['provider']+' / '+r['via'] for r in rows})
        parts = [PIN_HELP, 'Local authors / connections / access: '+('; '.join(connections) or 'none locally eligible')+'.']
        for role in ROLES:
            argument = next((a for row in rows for a in row['role_arguments'].get(role, [])
                             if a.endswith('@high')), None)
            argument = argument or next((a for row in rows for a in row['role_arguments'].get(role, [])), None)
            parts.append(('Copy for '+role+': tare console ./project '+argument) if argument else
                         'No configured compatible example for '+role+'; consult tare models --role '+role+'.')
        return '\n'.join(parts)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return PIN_HELP+'\nLocal catalog unavailable; no configured example proved. Consult tare models. '+str(error)


def main(argv=None):
    import argparse
    from .execution_scope import add_arguments, from_arguments, normalize_arguments
    import sys
    parser = argparse.ArgumentParser(prog='tare models', description='Offline model connections; quota and live availability are unverified.',
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=PIN_HELP, allow_abbrev=False)
    parser.add_argument('--role', choices=ROLES)
    parser.add_argument('--via', choices=('cli', 'api', 'local'))
    parser.add_argument('--vendor', help='Model author, not connection provider')
    parser.add_argument('--provider', help='Connection provider, e.g. anthropic, openai, bedrock')
    parser.add_argument('--include-unconfigured', action='store_true')
    parser.add_argument('--json', action='store_true')
    add_arguments(parser)
    args = parser.parse_args(normalize_arguments(sys.argv[1:] if argv is None else argv))
    try:
        scope = from_arguments(args, discovery=True)
        rows = projection(role=args.role, via=args.via, vendor=args.vendor, provider=args.provider,
                          include_unconfigured=args.include_unconfigured, scope=scope)
    except ValueError as error:
        parser.error(str(error)+' Consult tare models --help for valid alternatives.')
    if args.json:
        print(json.dumps({'models': rows, 'availability': 'UNVERIFIED', 'quota': 'UNVERIFIED'}, indent=2))
    else:
        print('Offline connections — live availability and quota UNVERIFIED')
        print('Configuration = loaded profile + local setup eligibility only; authentication UNVERIFIED.')
        if not rows:
            print('No models match these valid filters and local configuration.')
        for row in rows:
            print('Model: '+row['label']+' | ID: '+row['model']+' | Deployment: '+row['deployment']+
                  ' | Author: '+row['vendor']+' | Connection: '+row['provider']+' ('+row['connection']+') | Access: '+row['via']+
                  ' | Local configuration: '+('configured' if row['configured'] else 'unconfigured (not executable)')+
                  ' | Reasoning supported: '+(', '.join(row['supported_reasoning']) or 'none')+
                  ' | Reasoning permitted: '+(', '.join(row['allowed_reasoning']) or 'none'))
            if (row.get('openrouter') or {}).get('notice'):
                print('  Data use: '+row['openrouter']['notice'])
            for role, arguments in row['role_arguments'].items():
                if arguments:
                    base = arguments[0].rsplit('@', 1)[0]
                    print('  '+role+' syntax: '+base+'@LEVEL; permitted levels: '+', '.join(a.rsplit('@', 1)[1] for a in arguments))
                    print('  Copy: tare console ./project '+arguments[0])
    return 0


def inventory(*, role=None):
    if role is not None:
        groups = {}
        for row in projection(role=role):
            if not row['allowed_reasoning']:continue
            value = native_catalog.select(row['deployment'])
            item = describe(row['deployment'], value)
            item['route']['efforts'] = row['allowed_reasoning']
            if item['route']['default_effort'] not in row['allowed_reasoning']:
                item['route']['default_effort'] = row['allowed_reasoning'][0]
            group = groups.setdefault(item['key'], {k:item[k] for k in ('key','label','group')} | {'routes':[]})
            group['routes'].append(item['route'])
        return sorted(groups.values(),key=lambda item:(item['group'],item['label']))
    groups = {}
    for row in native_catalog.available_models():
        name = row['deployment']
        if re.fullmatch(r'planner-[0-9a-f]{24}', name):
            continue  # Immutable role bindings are not extra user-facing connections.
        if name in ('qwen38', 'local-qwen'):
            continue  # Public CLI aliases of qwen38-gsq; one model/connection row in the picker.
        value = native_catalog.select(name)
        item = describe(name, value)
        group = groups.setdefault(item['key'], {k: item[k] for k in ('key', 'label', 'group')} | {'routes': []})
        group['routes'].append(item['route'])
    for item in groups.values():
        routes = item['routes']
        # New choices prefer the existing native path's stable cache key.
        # The picker still honors an explicitly remembered connection.
        routes.sort(key=lambda route: route['protocol'] != 'openai-codex-responses')
        labels = [r['label'] for r in routes]
        for route in routes:
            if labels.count(route['label']) > 1:
                route['label'] += ' · '+route['id']
    return sorted(groups.values(), key=lambda r: (r['group'], r['label']))


def resolve(choices, chosen):
    item = next((r for r in choices if r['key'] == chosen.get('key')), None)
    route = next((r for r in item['routes'] if r['id'] == chosen.get('connection')), None) if item else None
    if not route or chosen.get('reasoning') not in ['default', *route['efforts']]:
        raise ValueError('Choose a supported model, connection and reasoning level.')
    return item, route


def declared_effort(value, effort):
    """A policy's level (auditor_effort, the action reviewer's high), not a user's choice, for this deployment: a local
    profile declares its own levels (low, medium, xhigh) and refuses others, so it takes its lowest declared level at
    or above this one; every other deployment keeps the level as it is."""
    levels = value.get('native_reasoning_levels') or []
    if not (local(value) and levels and effort in EFFORTS and effort not in levels):
        return effort
    above = [level for level in EFFORTS[EFFORTS.index(effort):] if level in levels]
    if not above:
        raise ValueError(f'This local model declares no reasoning level at or above {effort} '
                         f'(it declares {", ".join(levels)}); no model call was made.')
    return above[0]


def reasoning_parameters(value, effort):
    """Apply a level using this connection's wire format, preserving cache and other settings. Returns the request
    parameters; a local model that declares `native_reasoning_budgets` also gets that level's output cap written to
    `value`'s profile, so pass the copy you are about to use."""
    profile = value['native_profile']
    params = json.loads(profile.get('parameters_json', '{}'))
    if effort == 'default':
        return params
    protocol = profile.get('protocol')
    if protocol == 'vendor-cli' or not protocol and profile.get('endpoint', '').startswith('cli://'):
        params['effort'] = effort
    elif effort and (protocol in {'openai-codex-responses', 'openai-responses'}
                    or protocol == 'openai-chat' and profile['endpoint'].rstrip('/') == OPENROUTER_ENDPOINT):
        params.setdefault('reasoning', {})['effort'] = effort
    elif effort and protocol == 'openai-chat' and (value.get('native_credential_env') == 'GEMINI_API_KEY'
                                                   or value.get('native_opencode')):
        params['reasoning_effort'] = effort
    elif effort and protocol == 'openai-chat' and local(value) and value.get('native_reasoning_levels'):
        if effort not in value['native_reasoning_levels']:
            raise ValueError('Unsupported local reasoning level: '+effort)
        params.setdefault('chat_template_kwargs', {})['reasoning_effort'] = effort
        # NInfer honours only a top-level thinking_budget; a level without one keeps the server's default. Thinking
        # and the answer share the output cap, so each level states its own cap and the answer keeps its room.
        # Both are set from the table on every call: what an earlier level left must not survive a switch.
        limits = (value.get('native_reasoning_budgets') or {}).get(effort)
        if limits:
            params.pop('thinking_budget', None)
            if 'thinking_budget' in limits:
                params['thinking_budget'] = limits['thinking_budget']
            profile['output_tokens'] = limits['output_tokens']
    elif effort and protocol == 'anthropic-messages':
        params.setdefault('output_config', {})['effort'] = effort
    elif effort and protocol == 'gemini-generate-content':
        params.setdefault('thinkingConfig', {})['thinkingLevel'] = effort
    return params


def realization(choices, chosen, *, deployment=None):
    item, route = resolve(choices, chosen)
    value = native_catalog.select(route['id']) if deployment is None else deployment
    if digest(value) != route['fingerprint']:
        raise ValueError('The model profile changed. Reopen the model picker.')
    value = deepcopy(value)
    profile = value['native_profile']
    effort = route['default_effort'] if chosen['reasoning'] == 'default' else chosen['reasoning']
    params = reasoning_parameters(value, effort)
    if profile['protocol'] == 'vendor-cli':
        if profile['endpoint'] == 'cli://agy':
            model = value.get('native_reasoning_models', {route['default_effort']: profile['model']}).get(effort)
            if not model:
                raise ValueError('This AGY reasoning level has no registered model route.')
            profile['model'] = value['model'] = model
            if 'expected_response_model' in profile:
                profile['expected_response_model'] = model
    profile['parameters_json'] = json.dumps(params, sort_keys=True)
    value['native_model_choice'] = {**chosen, 'label': item['label'],
                                    'connection_label': route['label'], 'effort': effort}
    deployment_profile(value, profile['model'])
    return value


def materialize(choices, chosen, root):
    """Create a content-addressed profile; never rewrite a saved selection."""
    from compute_plane.native_journal import regular, custody_lock, save_json
    value = realization(choices, chosen)
    root = regular(Path(root)/'model-selections')
    root.mkdir(parents=True, exist_ok=True)
    path = regular(root/(digest(value)+'.json'))
    with custody_lock(root):
        if path.exists():
            if load_deployment(path) != value:
                raise ValueError('Saved model selection differs from its identity.')
        else:
            save_json(path, value)
    return str(path)


def current(root, chosen):
    """A saved choice realized from today's catalog: its limits, CLI version or executable may have moved on."""
    return materialize(inventory(), {k: chosen[k] for k in ('key', 'connection', 'reasoning')}, root)


def read_preferences(root):
    from compute_plane.native_journal import regular
    empty = {'schema': SCHEMA, 'recent': [], 'models': {}, 'last': None}
    if root is None:
        return empty
    path = regular(Path(root)/'models.json')
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return empty
    if len(raw) > 65536:
        raise ValueError('Model preferences exceed their size limit.')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA
            or not isinstance(value.get('recent'), list) or not isinstance(value.get('models'), dict)
            or len(value['recent']) > 12 or len(value['models']) > 128
            or any(not isinstance(k, str) for k in value['recent'])
            or len(set(value['recent'])) != len(value['recent'])
            or any(not isinstance(v, dict) or any(not isinstance(v.get(k), str)
                   for k in ('connection', 'reasoning')) for v in value['models'].values())
            or value.get('last') is not None and not isinstance(value['last'], dict)):
        raise ValueError('Unsupported model preferences format.')
    return value


def remember(root, chosen, profile):
    from compute_plane.native_journal import regular, custody_lock, save_json
    root = regular(Path(root)); root.mkdir(parents=True, exist_ok=True)
    with custody_lock(root):
        value = read_preferences(root)
        value['recent'] = [chosen['key'], *[k for k in value['recent'] if k != chosen['key']]][:12]
        value['models'][chosen['key']] = {k: chosen[k] for k in ('connection', 'reasoning')}
        if len(value['models']) > 128:
            raise ValueError('Too many remembered models.')
        value['last'] = {**chosen, 'profile': profile}
        save_json(root/'models.json', value)
    return value

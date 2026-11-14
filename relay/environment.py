"""What this machine's owner chose to use and not to use: local model gateways, vendor CLIs, API providers and
decision models (D15). One file per machine, beside local-gpu.json, so every frontend and storage root sees the
same choices. No file, or a category left undecided, keeps today's behaviour: every route tare finds."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

SCHEMA = 'tare.tools/environment/1'
CATEGORIES = {'local_models': ('local models', 'local'), 'clis': ('vendor CLIs', 'quota'),
              'api_providers': ('API providers', 'usd'), 'decision_models': ('decision models', None),
              'search_providers': ('web search APIs', 'usd')}


def store():
    """%LOCALAPPDATA%/tare.tools/environment.json; TARE_ENVIRONMENT_STORE overrides it."""
    from .native_installation import descriptor_path
    return Path(os.environ.get('TARE_ENVIRONMENT_STORE') or descriptor_path().parent/'environment.json')


def read():
    """The saved choices; {} when none; {'error': why} when the file cannot be trusted. An unreadable file must
    not hand back a route its owner declined (a paid API, say), so allows() then refuses every route, saying why."""
    path = store()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return {'error': f'{path}: {exc.strerror or exc}'}
    try:
        value = json.loads(raw) if len(raw) <= 65536 else None
    except ValueError as exc:
        return {'error': f'{path}: {exc}'}
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or \
            any(not isinstance(value.get(c, {}), dict) for c in CATEGORIES):
        return {'error': f'{path}: not a {SCHEMA} file of at most 64 KiB'}
    return value


def origin(url):
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'.lower()


def kind(deployment):
    """(category, item) of a route: a local gateway's origin, a CLI's endpoint, an API's credential variable."""
    from .challenger import local
    profile = deployment['native_profile']
    if local(deployment):
        return 'local_models', origin(profile['endpoint'])
    if profile['protocol'] == 'vendor-cli':
        return 'clis', profile['endpoint']
    if profile['protocol'] == 'openai-codex-responses':
        return 'clis', 'cli://codex'  # the same ChatGPT account as the Codex CLI
    if deployment.get('native_credential_env'):
        return 'api_providers', deployment['native_credential_env']
    return None, None


_older = {}


def older_models():
    """The Codex models its catalogue still lists that newer ones replace (vendor_models.older), read once per
    change of the catalogue; nothing without one."""
    from .cli_discovery import TEMPLATES, codex_catalog
    from .vendor_models import older
    home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'models_cache.json'
    try:
        stamp = (str(home), home.stat().st_mtime_ns)
    except OSError:
        stamp = ('shipped templates',)  # Codex never ran here: discovery offers the shipped models
    if stamp not in _older:
        catalog = codex_catalog() if stamp[0] != 'shipped templates' else None
        slugs = [m['slug'] for m in catalog if m.get('visibility') == 'list'] if catalog else [
            t['native_profile']['model'] for t in json.loads(TEMPLATES.read_text(encoding='utf-8')).values()
            if t['native_profile']['endpoint'] == 'cli://codex']
        _older.clear()
        _older[stamp] = older([s for s in slugs if str(s).startswith('gpt-')])
    return _older[stamp]


def declined(item):
    return item is False or isinstance(item, dict) and item.get('use') is False


def allows(deployment, value=None):
    """(True, None) when this machine's setup leaves the route in, else (False, why)."""
    from .execution_scope import current
    allowed,why=current().allows(deployment)
    if not allowed:return False,why
    value = read() if value is None else value
    if 'error' in value:
        return False, 'machine setup unreadable: ' + value['error'][:160] + ' (fix it or run tare setup --reset)'
    category, item = kind(deployment)
    if category is None or 'native_auto_routes' in deployment:  # tare auto resolves to an allowed route itself
        return True, None
    section, label = value.get(category) or {}, CATEGORIES[category][0]
    if section.get('use') is False:
        return False, f'{label} not used on this machine (tare setup)'
    if declined((section.get('items') or {}).get(item)):
        return False, f'{item} not used on this machine (tare setup)'
    from .openrouter import allowed
    permitted, reason = allowed(deployment, value)
    if not permitted:
        return False, reason
    gateways = [origin(g['origin']) for g in section.get('gateways') or [] if isinstance(g, dict) and g.get('origin')]
    if category == 'local_models' and gateways and item not in gateways:
        return False, f'local gateway {item} is not set up on this machine (tare setup)'
    return True, None


def decides(name):
    """Whether a decision model (jev, local_judge) may be asked on this machine."""
    from .execution_scope import current
    if not current().judge(name):return False
    value = read()
    section = value.get('decision_models') or {}
    return 'error' not in value and section.get('use') is not False and \
        not declined((section.get('items') or {}).get(name))


# Which routes automatic routing may use, per role: `routing_profile`, set with tare setup --auto (the /routes matrix
# comes next). tare's default is today's role rows (relay/role_deployments.json and the routing policy's lists); the
# profile keeps only the owner's differences, so it can only switch routes off, and an "on" entry only undoes a
# broader "off". Rules above it (setup, qualification, older models, independence, credentials, quota) keep their
# own reasons. Only the user's own choices pass it: a pick, a pin, --coordinator, the explorers of /delegation; a route
# a model names (the coordinator's unit, planner or research route) does not. Grammar: docs/guides/SHARED_GPU.md.
PROFILE_ROLES = ('implementer', 'explorer', 'planner', 'plan-auditor', 'auditor', 'researcher', 'vision', 'heads_up')
RANKED_ROLES = PROFILE_ROLES[:5]  # the roles relay/role_deployments.json ranks (native_routes.ranked)
SOURCES = ('claude-code', 'codex', 'agy', 'opencode-sub', 'opencode-api', 'api', 'openrouter', 'bedrock', 'local')
MODEL = re.compile(r'\*|([a-z0-9]+)/(?:\*|([a-z][a-z0-9-]*?)(?:-(\d+(?:\.\d+)?))?)')


def entry(text):
    """(specificity, rule) of one profile entry, or None when it is malformed. A route id (openrouter-muse-wonder) is
    the most specific; MODEL@SOURCE ranks by its model part (claude/fable-5.2 over claude/fable over claude/* over
    *), then by a named source over *."""
    if not isinstance(text, str):
        return None
    if '@' not in text:
        return (8, text) if re.fullmatch(r'[a-z0-9][a-z0-9._-]*', text) else None
    model, _, where = text.partition('@')
    found = MODEL.fullmatch(model)
    if not found or where != '*' and where not in SOURCES:
        return None
    family, name, version = found.groups()
    level = 0 if model == '*' else 3 if version else 2 if name else 1
    return level*2 + (where != '*'), (family, name, version and tuple(map(int, version.split('.'))), where)


def source(deployment):
    """The SOURCES token of a route: who carries it and how it is paid."""
    from .challenger import local
    from .model_choices import OPENROUTER_ENDPOINT
    from .opencode import subscription
    profile = deployment.get('native_profile') or {}
    endpoint, protocol = profile.get('endpoint', ''), profile.get('protocol', '')
    if protocol == 'vendor-cli':
        return {'cli://claude': 'claude-code', 'cli://codex': 'codex', 'cli://agy': 'agy'}.get(endpoint)
    if protocol == 'openai-codex-responses':
        return 'codex'  # Codex Direct: the same ChatGPT plan as the Codex CLI
    if deployment.get('native_opencode'):
        return 'opencode-sub' if subscription(deployment) else 'opencode-api'
    if protocol.startswith('bedrock'):
        return 'bedrock'
    if endpoint.rstrip('/') == OPENROUTER_ENDPOINT:
        return 'openrouter'
    return 'local' if local(deployment) else 'api'


def route_key(deployment):
    """(family, line, version, source) of a route: claude-api-fable-5-2 is ('claude', 'fable', (5, 2), 'api'). A model
    of no line in relay/model_lines.json (a reseller's own id, a local model) has no family, line or version."""
    from .vendor_models import AGY_EFFORT, line, parse
    model = (deployment.get('native_profile') or {}).get('model', '')
    return (*(parse(AGY_EFFORT.sub('', model)) if line(model) else (None, None, None)), source(deployment))


def route_label(deployment):
    family, name, version, where = route_key(deployment)
    return (f'{family}/{name}-' + '.'.join(map(str, version)) if family else '') + '@' + (where or '?')


def profile(value=None):
    """The saved routing_profile as ({role: {'off': [(text, specificity, rule)], 'on': [...]}}, problems). What cannot
    be read is ignored and named in problems (tare setup shows them): it never stops routing, and ignoring it never
    makes more routes eligible than tare's default."""
    value = read() if value is None else value
    saved = None if 'error' in value else value.get('routing_profile')
    if saved is None:
        return {}, []
    if not isinstance(saved, dict):
        return {}, ['routing_profile is not an object; ignored']
    rules, problems = {}, [f'routing_profile.{key}: unknown; ignored' for key in sorted(set(saved) - {'off', 'on'})]
    for side in ('off', 'on'):
        roles = saved.get(side, {})
        if not isinstance(roles, dict):
            problems.append(f'routing_profile.{side} is not an object; ignored')
            continue
        for role, texts in roles.items():
            if role not in PROFILE_ROLES or not isinstance(texts, list):
                problems.append(f'routing_profile.{side}.{role}: unknown role or not a list; ignored')
                continue
            for text in texts:
                parsed = entry(text)
                if parsed is None:
                    problems.append(f'routing_profile.{side}.{role}: {text!r} is not a route id or MODEL@SOURCE; ignored')
                else:
                    rules.setdefault(role, {'off': [], 'on': []})[side].append((text, *parsed))
    return rules, problems


def profile_state(role, deployment, *names, value=None):
    """('off' or 'on', entry) of the most specific entry for this route and role (off wins a tie), or None: tare's
    default. `names`: what the caller calls the route (its policy or table name); its id is known."""
    rules = profile(value)[0].get(role)
    if not rules:
        return None
    from .native_catalog import LOCAL, LOCAL_DEFAULT, local_default
    known = {deployment.get('id'), *names}
    if LOCAL_DEFAULT in known:
        known.add(local_default())
    known |= {LOCAL.get(n, n) for n in known if n}  # local-qwen is qwen38-gsq, as tare models lists it
    key = route_key(deployment)
    found = max(((specificity, side == 'off', text) for side in ('off', 'on') for text, specificity, rule in rules[side]
                 if (rule in known if isinstance(rule, str) else
                     rule[3] in ('*', key[3]) and all(want in (None, have) for want, have in zip(rule[:3], key)))),
                default=None)
    return found and ('off' if found[1] else 'on', found[2])


def switched_off(role, deployment, *names, value=None):
    """An Unavailable (SWITCHED_OFF_FOR_ROLE) when the routing profile switches this route off for `role`, else None.
    Every choice but the user's own asks it, after its rules: automatic routing and a route a model names (the
    coordinator's unit, planner or research route). A pick, a pin or --coordinator never asks."""
    state = profile_state(role, deployment, *names, value=value)
    if state and state[0] == 'off':
        from .scheduling_diagnostics import Unavailable
        name = next((n for n in names if n), deployment.get('id'))
        return Unavailable(f'{name} is switched off for {role} in the routing profile ({state[1]}); the user can switch '
                           f'it on (tare setup --auto {role}:{name}=on) or pin it', 'SWITCHED_OFF_FOR_ROLE')
    return None


def auto_rows(role, value=None):
    """The routes automatic `role` routing considers here, checked without a live probe: their line on source, the
    profile's state ('on' is tare's default; an entry is the owner's) and why one cannot be used now (a rule)."""
    from .cli_discovery import TEMPLATES
    from .execution_scope import current
    from .native_catalog import select
    from .native_routes import configured, roles as table_roles
    from .turn_engine import available, policy
    value = read() if value is None else value
    templates = json.loads(TEMPLATES.read_text(encoding='utf-8'))
    table = {row.get('native_catalog', row['id']): row for row in configured()['deployments']
             if role in RANKED_ROLES and role in table_roles(row, current())}
    listed = [n for item in policy().get(role) or [] for n in (item if isinstance(item, list) else [item])]
    rows = []
    for name in dict.fromkeys([*listed, *table]):
        deployment, why = available(name, probe=False)
        try:
            known = deployment or select(name)
        except (ValueError, OSError, KeyError, TypeError):
            known = templates.get(name)  # a CLI route not set up here: its shipped template names its line
        state = known and profile_state(role, known, name, value=value)
        row = table.get(name, {})  # its rules (qualified, older) are the role table's; a routing list asks available()
        now = (why.diagnostic['code'] if why is not None else 'NOT_QUALIFIED_PIN_ONLY' if row.get('qualified') is False
               else 'OLDER_MODEL' if row and older(deployment, value) else 'usable')
        rows.append({'route': name, 'line': route_label(known) if known else '?',
                     'profile': f'{state[0]} ({state[1]})' if state else 'on' if known else '?', 'now': now})
    return rows


def auto_table(role, value=None):
    cells = [('route', 'line@source', 'profile', 'now')] + [tuple(r.values()) for r in auto_rows(role, value)]
    widths = [max(len(cell[i]) for cell in cells) for i in range(3)]
    return '\n'.join([f'{role}: the routes automatic routing considers (tare setup --auto {role}:PATTERN=on|off|default)']
                     + ['  ' + '  '.join(cell[i].ljust(widths[i]) for i in range(3)) + '  ' + cell[3] for cell in cells])


def set_auto(value, spec):
    """ROLE:PATTERN=on|off|default into the routing profile; it keeps only differences from tare's default."""
    role, _, rest = spec.partition(':')
    pattern, _, state = rest.rpartition('=')
    if role not in PROFILE_ROLES:
        raise ValueError(f'Unknown role {role!r}: use ' + ', '.join(PROFILE_ROLES) + '.')
    if state not in ('on', 'off', 'default') or entry(pattern) is None:
        raise ValueError(f'Give ROLE:PATTERN=on|off|default; PATTERN is a route id or MODEL@SOURCE such as '
                         f'claude/fable@api or *@openrouter (sources: {", ".join(SOURCES)}).')
    saved = _editable(value)
    for side in ('off', 'on'):
        roles = {r: [t for t in texts if t != pattern] if r == role else texts for r, texts in saved[side].items()}
        if side == state:
            roles[role] = [*roles.get(role, []), pattern]
        saved[side] = roles
    return _store_profile(value, saved)


def reset_auto(value, role=''):
    """Forget the profile's entries for `role`, or the whole profile."""
    if not role:
        value.pop('routing_profile', None)
        return value
    if role not in PROFILE_ROLES:
        raise ValueError(f'Unknown role {role!r}: use ' + ', '.join(PROFILE_ROLES) + '.')
    saved = _editable(value)
    return _store_profile(value, {**saved, **{side: {r: t for r, t in saved[side].items() if r != role}
                                              for side in ('off', 'on')}})


def _editable(value):
    saved = value.get('routing_profile') or {}
    if not isinstance(saved, dict) or any(not isinstance(saved.get(side, {}), dict) or any(
            not isinstance(texts, list) for texts in saved.get(side, {}).values()) for side in ('off', 'on')):
        raise ValueError('The saved routing profile cannot be read: tare setup --auto-reset starts it again.')
    return {**saved, 'off': dict(saved.get('off', {})), 'on': dict(saved.get('on', {}))}


def _store_profile(value, saved):
    """Keep only what differs from tare's default: no empty role, side or profile."""
    saved = {k: {r: t for r, t in v.items() if t} if k in ('off', 'on') else v for k, v in saved.items()}
    saved = {k: v for k, v in saved.items() if v}
    if saved:
        value['routing_profile'] = saved
    else:
        value.pop('routing_profile', None)
    return value


def summary():
    """Per category: used, not used or undecided, with the items it names; for the availability report."""
    value = read()
    if 'error' in value:
        return {'file': str(store()), 'error': value['error']}
    state = lambda section: {True: 'used', False: 'not used'}.get(section.get('use'), 'undecided')
    return {'file': str(store()), 'saved': decided(value), 'categories': {
        category: {'state': state(value.get(category) or {}), 'label': label, 'class': route_class,
                   'declined': sorted(k for k, v in ((value.get(category) or {}).get('items') or {}).items()
                                      if declined(v))}
        for category, (label, route_class) in CATEGORIES.items()},
        'gateways': [{k: g.get(k) for k in ('origin', 'kind', 'observed')} | {'models': len(g.get('models') or [])}
                     for g in (value.get('local_models') or {}).get('gateways') or []]}


def decided(value=None):
    """Whether the owner has chosen anything on this machine (an unreadable file counts: it refuses)."""
    value = read() if value is None else value
    return 'error' in value or value.get('setup_complete') is True or any((value.get(c) or {}).get('use') is not None for c in CATEGORIES)


def default_route():
    """The picker's first model: the local default only on a machine never set up that names a local gateway (as
    before D15); otherwise tare auto, which starts on the first route this machine can run."""
    if decided():
        return 'tare-auto'
    try:
        from .native_installation import storage_root
        gateway = os.environ.get('TARE_LOCAL_GATEWAY') or (storage_root()/'local-gateway.json').is_file()
    except ValueError:
        gateway = False
    return 'local-qwen' if gateway else 'tare-auto'


def save(value):
    """Write the choices atomically, at most 64 KiB; never a secret (credentials are named, not stored)."""
    from datetime import datetime, timezone
    value = {**{k: v for k, v in value.items() if k != 'error'}, 'schema': SCHEMA,
             'updated': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    raw = (json.dumps(value, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    if len(raw) > 65536:
        raise ValueError('This machine setup would exceed 64 KiB.')
    path = store()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + f'.{os.getpid()}.tmp')  # the TUI and tare serve may write at once
    staged.write_bytes(raw)
    os.replace(staged, path)
    return value


# Shipped local profiles (Qwen, Swift...) name the gateway placeholder; once the machine names its gateways they are
# offered only when the first gateway, which the placeholder resolves to, publishes their model (decision 4).
def shipped_local(name):
    from .native_catalog import LOCAL, LOCAL_GATEWAY_PLACEHOLDER, PROFILES
    try:
        return name in LOCAL and LOCAL_GATEWAY_PLACEHOLDER in (PROFILES/(LOCAL[name]+'.json')).read_text(encoding='utf-8')
    except OSError:
        return False


def gateway_models(value=None):
    """The models the first saved gateway published when last asked, or None while local models are undecided."""
    value = read() if value is None else value
    section = value.get('local_models') or {}
    gateways = section.get('gateways') or []
    return set(gateways[0].get('models') or []) if section.get('use') and gateways else None


def gateway_default(value=None):
    """The model the first saved gateway's fleet has as its default (tare fleet), or None."""
    value = read() if value is None else value
    section = value.get('local_models') or {}
    gateways = section.get('gateways') or []
    return gateways[0].get('default') if section.get('use') and gateways else None


def refresh_gateway(value):
    """Ask the first saved gateway again, in place (its fleet changed): what it publishes and its default."""
    gateways = (value.get('local_models') or {}).get('gateways') or []
    if gateways:
        gateways[0] = ask_gateway(gateways[0]['origin'])
    return value


def older(deployment, value=None):
    """Whether a CLI route runs a model its vendor replaced and this machine has not asked for older models. Only
    what tare offers by default leaves it out (the picker, serve's list, shipped role tables); a route the owner
    named keeps working, and tare asks whether to move it to the newer model (replacements)."""
    value = read() if value is None else value
    return kind(deployment)[0] == 'clis' and (value.get('clis') or {}).get('old_models') is not True         and deployment['native_profile'].get('model') in older_models()


def configured_routes():
    """[(where, name)]: the routes the owner named: the explorer, its overflow and challenger (/delegation) and
    the routing policy, when the owner saved one (<storage>/serve/routing.json)."""
    from .delegation import policy
    from .native_installation import storage_root
    routes = []
    try:
        settings = policy()
        for key in ('profile', 'overflow', 'challenger'):
            name = settings.get(key)
            name = name.get('profile') if isinstance(name, dict) else name
            if isinstance(name, str):
                routes.append((f'/delegation {key}', name))
    except (ValueError, OSError):
        pass
    try:
        path = storage_root()/'serve'/'routing.json'
        saved = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    except (ValueError, OSError):
        saved = {}
    for role, names in saved.items():
        for name in (n for item in names if isinstance(names, list) for n in (item if isinstance(item, list) else [item])):
            if isinstance(name, str):
                routes.append((f'routing {role}', name))
    return routes


def replacements(value=None):
    """Routes the owner named that run a model a newer one now replaces, and the route for the newer model:
    [{'where', 'route', 'model', 'newer', 'to', 'ready'}]; a pair the owner chose to keep is not asked again."""
    from .cli_discovery import codex_catalog
    from .native_catalog import select
    from .native_profiles import local_path
    from .vendor_models import newest
    value = read() if value is None else value
    if 'error' in value:
        return []
    slugs = [m['slug'] for m in codex_catalog() or [] if m.get('visibility') == 'list']
    kept = (value.get('clis') or {}).get('kept') or {}
    found = []
    for where, name in configured_routes():
        try:
            deployment = select(name)
        except (ValueError, OSError, KeyError, TypeError):
            continue
        model = deployment['native_profile'].get('model', '')
        if not older(deployment, value):
            continue
        newer = newest(slugs, model)
        if newer == model or kept.get(model) == newer:
            continue
        to = name.replace(model.replace('.', '-'), newer.replace('.', '-'))
        found.append({'where': where, 'route': name, 'model': model, 'newer': newer, 'to': to,
                      'ready': to != name and local_path(to).is_file()})
    return found


def keep_older(value, items):
    """Do not ask again about these older models and their newer ones."""
    section = dict(value.get('clis') or {})
    section['kept'] = {**(section.get('kept') or {}), **{i['model']: i['newer'] for i in items}}
    value['clis'] = section
    return value


def replace_older(items):
    """Move the named routes to their newer models, in /delegation and the routing policy; only to a route whose
    profile exists (discovery writes the Codex ones). Returns the items moved."""
    from .delegation import configure, policy
    from .native_installation import storage_root
    moved = [i for i in items if i['ready']]
    renames = {i['route']: i['to'] for i in moved}
    if not renames:
        return []
    swap = lambda v: renames.get(v, v) if isinstance(v, str) else {**v, 'profile': renames.get(v['profile'], v['profile'])} \
        if isinstance(v, dict) and isinstance(v.get('profile'), str) else v
    settings = policy()
    updated = {**settings, **{k: swap(settings.get(k)) for k in ('profile', 'overflow', 'challenger')}}
    if updated != settings:
        configure(updated)
    path = storage_root()/'serve'/'routing.json'
    if path.is_file():
        saved = json.loads(path.read_text(encoding='utf-8'))
        fixed = {role: [[swap(n) for n in item] if isinstance(item, list) else swap(item) for item in names]
                 if isinstance(names, list) else names for role, names in saved.items()}
        if fixed != saved:
            path.write_text(json.dumps(fixed, indent=1), encoding='utf-8')
    return moved


def listed(name, deployment, value=None):
    """Whether the picker and tare serve offer a route here (no network: the gateways' last answer)."""
    value = read() if value is None else value
    if not allows(deployment, value)[0] or older(deployment, value):
        return False
    models = gateway_models(value)
    return models is None or 'native_auto_routes' in deployment or not shipped_local(name) \
        or deployment['native_profile']['model'] in models


def ask_gateway(url):
    """{'origin', 'kind', 'models', 'observed'}: the ids a gateway's /v1/models publishes and whether it is a
    fleet gateway (it answers /v1/fleet/status: NInfer) or any OpenAI-compatible server (Ollama, LM Studio)."""
    from datetime import datetime, timezone
    from urllib.request import urlopen
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.path not in ('', '/', '/v1', '/v1/'):
        raise ValueError('Give the gateway as scheme://host:port, e.g. http://127.0.0.1:8080.')
    base = origin(url)
    try:
        with urlopen(base + '/v1/models', timeout=5) as reply:
            rows = json.load(reply).get('data')
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f'{base} did not list its models ({str(exc)[:120]}).') from None
    models = sorted({r['id'] for r in rows if isinstance(r, dict) and isinstance(r.get('id'), str)}) \
        if isinstance(rows, list) else []
    try:
        with urlopen(base + '/v1/fleet/status', timeout=3) as reply:
            status = json.load(reply)
    except (OSError, ValueError):
        status = None
    fleet = isinstance(status, dict)
    default = status.get('default_model') if fleet else None  # the owner's pick (tare fleet): tare auto runs it
    return {'origin': base, 'kind': 'ninfer' if fleet else 'openai', 'models': models,
            **({'default': default} if isinstance(default, str) and default in models else {}),
            'observed': datetime.now(timezone.utc).isoformat(timespec='seconds')}


def slug(text):
    return '-'.join(filter(None, ''.join(c if c.isalnum() else '-' for c in text.lower()).split('-')))


def generic_name(gateway_origin, model):
    host = urlsplit(gateway_origin)
    return ('local-' + slug(f'{host.hostname}-{host.port or ""}') + '-' + slug(model))[:64].rstrip('-_')


def generic_profiles(value):
    """{name: profile} for each model a saved gateway publishes that no shipped profile covers: unqualified,
    offered in the picker (and run by tare auto when it is the default of the node's fleet)."""
    from .native_catalog import LOCAL, select
    shipped = {select(n)['native_profile']['model'] for n in LOCAL if shipped_local(n)}
    wanted = {}
    for index, gateway in enumerate((value.get('local_models') or {}).get('gateways') or []):
        for model in gateway.get('models') or []:
            if index == 0 and model in shipped:
                continue  # the shipped, measured profile serves it
            name = generic_name(gateway['origin'], model)
            wanted[name] = {'id': name, 'runner': 'native-http', 'native_counter_protocol': 'utf8-envelope-estimate-v1',
                'native_profile': {'protocol': 'openai-chat', 'model': model, 'context_window': 32768,
                    'output_tokens': 4096, 'endpoint': gateway['origin'] + '/v1', 'parameters_json': '{}',
                    'source': f"generic profile for {model} at {gateway['origin']} ({gateway.get('observed', '')[:10]}); "
                              'not qualified: context and parameters are conservative defaults'},
                'native_usage_contract': {'layout': 'chat-inclusive',
                                          'source': 'OpenAI-compatible usage; fields absent remain unknown'}}
    return wanted


def write_generic(value):
    """Write the generic local profiles the gateways call for and retire the ones they no longer publish; a
    profile the owner edited is never touched. Returns (added, retired)."""
    from .cli_discovery import retire_profile, write_profile
    from .native_profiles import local_path
    managed, wanted, added, retired = value.setdefault('managed', {}), generic_profiles(value), [], []
    for name, profile in sorted(wanted.items()):
        if write_profile(name, profile, managed) == 'added':
            added.append(name)
    for name in sorted(set(managed) - set(wanted)):
        try:
            endpoint = json.loads(local_path(name).read_text(encoding='utf-8'))['native_profile']['endpoint']
        except (OSError, ValueError, KeyError):
            managed.pop(name, None)
            continue
        if retire_profile(name, endpoint, managed):
            retired.append(name)
    return added, retired


ALIASES = {'local': 'local_models', 'local_models': 'local_models', 'cli': 'clis', 'clis': 'clis',
           'api': 'api_providers', 'api_providers': 'api_providers', 'decision': 'decision_models',
           'decision_models': 'decision_models', 'search': 'search_providers', 'search_providers': 'search_providers'}


def detect():
    """What this machine has, found without spending anything or asking any gateway: the vendor CLIs installed,
    which API credentials are present (env or vault; never their values) and whether the Jev key is set."""
    from .cli_discovery import LOCATIONS, locate
    from .native_catalog import LOCAL, select
    from .native_generation import vault_secret
    from .native_profiles import names
    credentials = set()
    for name in sorted(set(LOCAL) | set(names())):
        try:
            credentials.add(select(name).get('native_credential_env'))
        except (ValueError, OSError, KeyError, TypeError):
            continue
    where = lambda env: 'env' if os.environ.get(env) else 'vault' if vault_secret(env) else 'absent'
    from .web_search import CREDENTIALS
    return {'search_providers': {env: where(env) for env in CREDENTIALS},
            'clis': {endpoint: locate(endpoint) for endpoint in LOCATIONS},
            'api_providers': {env: where(env) for env in sorted(credentials - {None})},
            'decision_models': {'jev': where('TYPESAFE_API_KEY')}}


def choose(value, spec, use):
    """Mark CATEGORY or CATEGORY:ITEM used (True), not used (False) or undecided (None)."""
    category, _, item = spec.partition(':')
    if category.lower() not in ALIASES:
        raise ValueError(f'Unknown category {category}: use local, cli, api, decision or search.')
    category = ALIASES[category.lower()]
    section = dict(value.get(category) or {})
    if not item:
        section['use'] = use
    elif category == 'local_models':
        raise ValueError('Name local gateways with --local-gateway URL.')
    else:
        if category == 'search_providers':
            from .web_search import PROVIDERS
            item = item.lower()
            if item not in PROVIDERS:
                raise ValueError('Choose search:deepseek, search:serpapi or search:firecrawl.')
        item = f'cli://{item.lower()}' if category == 'clis' and '://' not in item else item
        items = {k: v for k, v in (section.get('items') or {}).items() if k != item}
        if use is not None:
            items[item] = {'use': use}
        section['items'] = items
    value[category] = {k: v for k, v in section.items() if v is not None}
    return value


def show(value, found):
    lines = [f'This machine ({store()}):' if decided(value) else f'This machine is not set up yet ({store()}).']
    for category, (label, _) in CATEGORIES.items():
        section = value.get(category) or {}
        state = {True: 'used', False: 'not used'}.get(section.get('use'), 'undecided')
        items = ', '.join(f"{k} {'not used' if declined(v) else 'used'}" for k, v in (section.get('items') or {}).items())
        lines.append(f'  {label}: {state}' + (f' ({items})' if items else '') + (
            '; older models offered' if category == 'clis' and section.get('old_models') else ''))
        if category == 'local_models':
            for gateway in section.get('gateways') or []:
                lines.append(f"    gateway {gateway['origin']} ({gateway.get('kind')}): "
                             f"{len(gateway.get('models') or [])} models, asked {gateway.get('observed', '?')}")
    rules, problems = profile(value)
    if rules or problems:
        lines.append('  automatic routing: your changes for ' + (', '.join(sorted(rules)) or 'no role')
                     + ' (tare setup --auto-show ROLE)')
        lines += [f'    {p}' for p in problems]
    lines.append('Found here, without spending anything:')
    lines += [f'  {endpoint}: {path or "not installed"}' for endpoint, path in found['clis'].items()]
    hidden = sorted(older_models())
    if hidden and not (value.get('clis') or {}).get('old_models'):
        lines.append('  older models left out (--old-models on offers them): ' + ', '.join(hidden))
    lines += [f'  {env}: {state}' for env, state in found['api_providers'].items()]
    lines.append(f"  Jev (TYPESAFE_API_KEY): {found['decision_models']['jev']}")
    lines += [f'  Search · {env}: {state}' for env, state in found.get('search_providers', {}).items()]
    lines += [f"Newer model: {r['newer']} replaces {r['model']}, which {r['where']} uses ({r['route']}); "
              + ('--replace-older moves it to ' + r['to'] if r['ready'] else 'no profile for it yet')
              + ', --keep-older keeps it' for r in replacements(value)]
    return '\n'.join(lines)


def old_models(value, on):
    """Offer the CLIs' older models too (on) or not (off, the default)."""
    section = {k: v for k, v in (value.get('clis') or {}).items() if k != 'old_models'}
    value['clis'] = {**section, 'old_models': True} if on else section
    return value


def add_gateway(value, url):
    section = dict(value.get('local_models') or {})
    asked = ask_gateway(url)
    section['gateways'] = [g for g in section.get('gateways') or [] if origin(g['origin']) != asked['origin']] + [asked]
    section['use'] = True
    value['local_models'] = section
    return value


def dialog(value, ask=input):
    """The first run in a terminal: one question per category; Enter leaves it undecided."""
    for category, (label, _) in CATEGORIES.items():
        answer = ask(f'Use {label} on this machine? [y/n, Enter: decide later] ').strip().lower()[:1]
        if answer in ('y', 'n'):
            value = choose(value, category, answer == 'y')
        if category == 'local_models' and answer == 'y':
            url = ask('Local model gateway [http://127.0.0.1:8080]: ').strip() or 'http://127.0.0.1:8080'
            value = add_gateway(value, url)
    return value


PROOF = 'Reply with exactly: OK'


def route_for(name):
    """(deployment, why) for a route name, or for cli:NAME the CLI as discovery would offer it."""
    if name.startswith('cli:'):
        from .cli_discovery import ACK_PROFILE, TEMPLATES, deployment, locate
        endpoint = 'cli://' + name[4:].lower()
        if endpoint not in ACK_PROFILE:
            return None, f'unknown CLI {name}: use cli:claude, cli:codex or cli:agy'
        executable = locate(endpoint)
        if not executable:
            return None, f'{endpoint} is not installed'
        return deployment(json.loads(TEMPLATES.read_text(encoding='utf-8'))[ACK_PROFILE[endpoint]], executable), None
    from .turn_engine import available
    return available(name)


def cost_line(deployment):
    """What one minimal call costs, said before it is spent: quota, dollars or the local GPU."""
    from decimal import Decimal
    from .challenger_variants import accounting
    route_class = accounting(deployment)
    if route_class == 'quota':
        from .turn_engine import usage_account
        return f'one minimal call on the {usage_account(deployment)} account: no dollars; it counts against that plan'
    if route_class == 'local':
        from compute_plane.model_facts import gpu_availability, read_json_observation
        base = deployment['native_profile']['endpoint'].rstrip('/')
        gpu = gpu_availability(read_json_observation(base + '/fleet/status', timeout=3), deployment['native_profile']['model'])
        return 'one minimal call on the local GPU: ' + ('the model is loaded' if gpu.get('resident') else
            'the gateway may first load the model (slow)' if gpu.get('loading_required') else 'its state is unknown')
    from .provider_prices import quote
    rates = [Decimal(v) for v in (quote(deployment, live=True).get('rates') or {}).values() if v is not None]
    if not rates:
        return 'one minimal call (about 40 tokens) at an unknown price: no tariff is known for this route'
    return f'one minimal call (about 40 tokens): under US$ {max(rates)*40/1000000:.6f}'


def prove(name, confirm):
    """One minimal real call on a route, only after `confirm(cost line)` agrees; the result (never the reply) is
    kept in the setup. A proven CLI is recorded for discovery, which then offers its models."""
    from datetime import datetime, timezone
    deployment, why = route_for(name)
    if deployment is None:
        return {'ok': False, 'reason': why}
    line = cost_line(deployment)
    if not confirm(f'{name}: {line}.'):
        return {'ok': None, 'reason': 'not confirmed', 'cost': line}
    if deployment['native_profile']['protocol'] == 'vendor-cli':
        from .cli_discovery import acknowledge, proven
        from .cli_qualification import forget_failed
        forget_failed(deployment['native_profile']['endpoint'])  # the explicit retry: probe the release again
        ok, reason = acknowledge(deployment)
        if ok:
            proven(deployment['native_profile']['endpoint'], deployment['native_cli']['executable'])
    else:
        from .native_generation import generate_text, vault_secret
        answered, content, receipt = generate_text(deployment, deployment['native_profile']['model'], PROOF, 120,
                                                   attempt_label='setup-proof', credential_resolver=vault_secret)
        ok = bool(answered and content.strip())
        reason = '' if ok else str(receipt.get('failure_class') or 'empty reply')[:200]
    record = {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'ok': ok, 'cost': line,
              **({} if ok else {'reason': reason})}
    value = read()
    if 'error' not in value:
        value.setdefault('proofs', {})[name] = record
        save(value)
    return record


def apply(value):
    """Save the choices and what follows from them: the first gateway becomes this storage's local gateway (the
    shipped local profiles resolve to it) and the generic local profiles follow what the gateways publish."""
    gateways = (value.get('local_models') or {}).get('gateways') or []
    if gateways:
        from .native_installation import storage_root
        path = storage_root()/'local-gateway.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'origin': gateways[0]['origin']}), encoding='utf-8')
    added, retired = write_generic(value)
    return save(value), added, retired


def store_credential(value, name, stream):
    """An API key into the vault, read from `stream` (never argv, never printed); the setup records only where."""
    import re
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', name):
        raise ValueError('Name the credential by its variable, e.g. OPENROUTER_API_KEY.')
    if os.environ.get(name):
        raise ValueError(f'{name} is set in the environment, which takes precedence; unset it to use the vault.')
    if stream.isatty():
        from getpass import getpass
        secret = getpass(f'{name}: ')
    else:
        secret = stream.readline().strip()
    if not secret:
        raise ValueError('No credential was given.')
    import keyring
    keyring.set_password('universal-agent-harness', name, secret)
    from .web_search import CREDENTIALS
    return choose(value, 'search:'+CREDENTIALS[name] if name in CREDENTIALS else f'api:{name}', True)


def main(argv=None):
    """tare setup: choose what tare uses on this machine (local gateways, vendor CLIs, API providers, decision
    models); with no flags in a terminal it asks, one question per category; flags serve scripted installs."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog='tare setup', description=main.__doc__)
    parser.add_argument('--use', action='append', default=[], metavar='CATEGORY[:ITEM]',
                        help='local, cli, api, decision or search; e.g. cli:agy, decision:jev, search:deepseek')
    parser.add_argument('--no-use', action='append', default=[], metavar='CATEGORY[:ITEM]')
    parser.add_argument('--undecide', action='append', default=[], metavar='CATEGORY[:ITEM]')
    parser.add_argument('--local-gateway', action='append', default=[], metavar='URL',
                        help='A local model gateway; it is asked for its models. The first serves the shipped profiles')
    parser.add_argument('--refresh', action='store_true', help='Ask the saved gateways for their models again')
    parser.add_argument('--old-models', choices=('on', 'off'), help="Offer the CLIs' older models too (off by default: "
                        'a model a newer one of the same line, or a newer generation, replaces)')
    parser.add_argument('--credential', metavar='ENV_NAME', help='Store an API key in the vault, read from stdin')
    parser.add_argument('--meta-contributor', choices=('on', 'off'),
                        help='Allow Meta to use prompts, code and responses sent to Muse Spark Contributor')
    parser.add_argument('--prove', action='append', default=[], metavar='ROUTE',
                        help='One minimal real call on a route (a profile name, or cli:codex), after showing its cost')
    parser.add_argument('--yes', action='store_true', help='Prove without asking (scripts); the cost is still printed')
    parser.add_argument('--replace-older', action='store_true', help='Move routes you named to the newer models')
    parser.add_argument('--keep-older', action='store_true', help='Keep the older models routes you named run')
    parser.add_argument('--reset', action='store_true', help='Forget every choice: back to every route tare finds')
    parser.add_argument('--auto', action='append', default=[], metavar='ROLE:PATTERN=on|off|default',
                        help="Switch routes on or off for one role's automatic routing, or back to tare's default; "
                        'PATTERN is a route id or MODEL@SOURCE, e.g. auditor:claude/opus@api=off or '
                        "implementer:*@openrouter=off. Explicit choices and pins are not affected")
    parser.add_argument('--auto-reset', nargs='?', const='', metavar='ROLE',
                        help="Back to tare's default for one role's automatic routing, or for every role")
    parser.add_argument('--auto-show', action='append', default=[], metavar='ROLE',
                        help='Show the routes automatic routing considers for a role: on, off by you, or not usable '
                        'now and why')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.prove:
        def confirm(line):
            print(line)
            if args.yes:
                return True
            try:
                return sys.stdin.isatty() and input('Spend it? [y/N] ').strip().lower() == 'y'
            except EOFError:
                return False
        results = {name: prove(name, confirm) for name in args.prove}
        print(json.dumps(results, indent=1) if args.json else '\n'.join(
            f"{name}: {'proven' if r['ok'] else 'not proven: ' + r['reason']}" for name, r in results.items()))
        return 0 if all(r['ok'] for r in results.values()) else 1
    try:
        if args.reset:
            store().unlink(missing_ok=True)
        value = read()
        if 'error' in value:
            raise ValueError(value['error'] + ' (tare setup --reset starts again)')
        changes = bool(args.use or args.no_use or args.undecide or args.local_gateway or args.refresh or args.credential
                       or args.old_models or args.replace_older or args.keep_older or args.meta_contributor)
        edits = bool(args.auto) or args.auto_reset is not None
        shown = list(dict.fromkeys([*(spec.partition(':')[0] for spec in args.auto), *args.auto_show,
                                    *([args.auto_reset] if args.auto_reset else [])]))
        for role in shown:
            if role not in PROFILE_ROLES:
                raise ValueError(f'Unknown role {role!r}: use ' + ', '.join(PROFILE_ROLES) + '.')
        if not changes and not args.reset and not args.json and not shown and not edits:
            print(show(value, detect()))
            try:
                if not sys.stdin.isatty():
                    raise EOFError
                print()
                value, changes = dialog(value), True
            except EOFError:  # Windows reports NUL as a terminal: an empty read is no terminal either
                print('\nNo terminal to ask in: pass --use, --no-use, --local-gateway or --credential.', file=sys.stderr)
                return 2
        for spec in args.use:
            value = choose(value, spec, True)
        for spec in args.no_use:
            value = choose(value, spec, False)
        for spec in args.undecide:
            value = choose(value, spec, None)
        for url in args.local_gateway:
            value = add_gateway(value, url)
        if args.refresh:
            for gateway in list((value.get('local_models') or {}).get('gateways') or []):
                value = add_gateway(value, gateway['origin'])
        if args.credential:
            value = store_credential(value, args.credential, sys.stdin)
        if args.old_models:
            value = old_models(value, args.old_models == 'on')
        if args.meta_contributor:
            value['model_data_use'] = {**(value.get('model_data_use') or {}),
                                     'meta_contributor': args.meta_contributor == 'on'}
        if args.replace_older:
            moved = replace_older(replacements(value))
            print(f'Moved {len(moved)} route(s) to newer models.', file=sys.stderr)
        if args.keep_older:
            value = keep_older(value, replacements(value))
        for spec in args.auto:
            value = set_auto(value, spec)
        if args.auto_reset is not None:
            value = reset_auto(value, args.auto_reset)
        if changes:
            value, added, retired = apply(value)
            if added or retired:
                print(f'Generic local profiles: {len(added)} added, {len(retired)} retired.', file=sys.stderr)
        elif edits:
            value = save(value)
        if shown or edits:
            problems = profile(value)[1]
            print(json.dumps({'routing_profile': value.get('routing_profile'), 'problems': problems,
                              'roles': {role: auto_rows(role, value) for role in shown}}, indent=1) if args.json else
                  '\n\n'.join([*(f'Routing profile: {p}' for p in problems),
                               *([auto_table(role, value) for role in shown]
                                 or ["Automatic routing follows tare's default for every role."])]))
            if not changes and not args.reset:
                return 0
        found = detect()
        print(json.dumps({'setup': summary(), 'found': found}, indent=1) if args.json else show(value, found))
        return 0
    except (ValueError, OSError) as exc:
        print(f'tare setup: {exc}', file=sys.stderr)
        return 1

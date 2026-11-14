"""What this machine's owner chose to use and not to use: local model gateways, vendor CLIs, API providers and
decision models (D15). One file per machine, beside local-gpu.json, so every frontend and storage root sees the
same choices. No file, or a category left undecided, keeps today's behaviour: every route tare finds."""
import json
import os
from pathlib import Path
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
    price = quote(deployment, live=True)
    rates = [Decimal(v) for v in (price.get('rates') or {}).values() if v is not None]
    if not rates:
        return 'one minimal call (about 40 tokens) at an unknown price: no tariff is known for this route'
    return f'one minimal call (about 40 tokens): under US$ {max(rates)*40/1000000:.6f}' + (
        f" (at a models.dev reference price read {price['effective_at']}, not a confirmed tariff)"
        if price.get('basis') == 'reference' else '')


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
        if not changes and not args.reset and not args.json:
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
            try:  # the models.dev reference prices, if not read today; no model call, no key sent
                from .provider_prices import refresh_reading
                refresh_reading(setup=value)  # with this command's --use / --no-use, not the saved choices
            except Exception:  # optional: the last good reading stays, or none (no reference price, as before)
                pass
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
        if changes:
            value, added, retired = apply(value)
            if added or retired:
                print(f'Generic local profiles: {len(added)} added, {len(retired)} retired.', file=sys.stderr)
        found = detect()
        print(json.dumps({'setup': summary(), 'found': found}, indent=1) if args.json else show(value, found))
        return 0
    except (ValueError, OSError) as exc:
        print(f'tare setup: {exc}', file=sys.stderr)
        return 1

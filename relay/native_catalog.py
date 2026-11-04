"""Explicit named native deployments; model details remain in qualified profiles."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .native_generation import load_deployment

PROFILES = Path(__file__).with_name('native_deployments')
LOCAL = {'openai-api': 'openai-api', 'gemini-api': 'gemini-api', 'claude-api': 'claude-api',
         'openai-api-gpt-6-astra': 'openai-api-gpt-6-astra',
         'openai-api-gpt-6-luna': 'openai-api-gpt-6-luna',
         'openai-api-gpt-6-1-sol-pro': 'openai-api-gpt-6-1-sol-pro',
         'openai-api-gpt-6-astra-pro': 'openai-api-gpt-6-astra-pro',
         'claude-api-sonnet-5-5': 'claude-api-sonnet-5-5',
         'claude-api-opus-5-5': 'claude-api-opus-5-5',
         'claude-api-fable-5-1': 'claude-api-fable-5-1',
         'gemini-api-3-8-flash': 'gemini-api-3-8-flash',
         'local-qwen': 'qwen38-gsq', 'qwen38': 'qwen38-gsq', 'hauhaucs': 'hauhaucs',
         'fable-tc': 'fable-tc', 'qwen36-moe': 'qwen36-moe', 'swift-next': 'swift-next', 'local-auto': 'local-auto',
         'qwen38-gsq': 'qwen38-gsq', 'swift27b': 'swift27b', 'tare-auto': 'tare-auto',
         'bedrock-opus': 'bedrock-opus', 'bedrock-sonnet': 'bedrock-sonnet', 'bedrock-glm': 'bedrock-glm',
         'openrouter-deepseek-v4-1-flash': 'openrouter-deepseek-v4-1-flash',
         'openrouter-glm-5-3': 'openrouter-glm-5-3',
         'openrouter-mimo-v2-6-pro': 'openrouter-mimo-v2-6-pro',
         'openrouter-kimi-k3-1-pro': 'openrouter-kimi-k3-1-pro',
         'openrouter-deepseek-v4-1-flash-1017': 'openrouter-deepseek-v4-1-flash-1017',
         'openrouter-glm-5-3-flash': 'openrouter-glm-5-3-flash',
         'openrouter-mistral-large-4': 'openrouter-mistral-large-4',
         'openrouter-muse-wonder': 'openrouter-muse-wonder',
         'openrouter-step-5-preview': 'openrouter-step-5-preview',
         'openrouter-muse-spark-1-3-contributor': 'openrouter-muse-spark-1-3-contributor',
         'openrouter-grok-4-7': 'openrouter-grok-4-7',
         'openrouter-ling-3-1-flash': 'openrouter-ling-3-1-flash',
         'openrouter-qwen3-8-omni-flash': 'openrouter-qwen3-8-omni-flash'}


# Shipped local profiles name this origin; each machine resolves it to its own model gateway.
LOCAL_GATEWAY_PLACEHOLDER = 'http://local-gateway:8080'
DEFAULT_LOCAL_GATEWAY = 'http://127.0.0.1:8080'


def local_gateway():
    """This machine's model gateway: TARE_LOCAL_GATEWAY, then <storage>/local-gateway.json, else localhost."""
    value = os.environ.get('TARE_LOCAL_GATEWAY')
    if not value:
        from .native_installation import storage_root
        try:
            value = json.loads((storage_root()/'local-gateway.json').read_text(encoding='utf-8'))['origin']
        except (OSError, ValueError, KeyError, TypeError):
            value = DEFAULT_LOCAL_GATEWAY
    url = urlparse(value) if isinstance(value, str) else None
    if not url or url.scheme not in {'http', 'https'} or not url.hostname or url.path not in {'', '/'} or url.query:
        raise ValueError('local_gateway_needs_scheme_host_and_port_only')
    return value.rstrip('/')


def bundled(name):
    from .native_generation import deployment_profile
    value = load_deployment(PROFILES / (LOCAL[name] + '.json'))
    resolved = json.loads(json.dumps(value).replace(LOCAL_GATEWAY_PLACEHOLDER, local_gateway()))
    deployment_profile(resolved, resolved['native_profile']['model'])
    return resolved


def roster_path():
    """Respect the control-root override, otherwise use the installed catalog."""
    from . import relay_mesh
    override = Path(relay_mesh.RELAY_DIR)/'round_table_roster.json'
    return override if override.exists() else Path(__file__).with_name('round_table_roster.json')


def offline_deployments(*, include_unconfigured=False):
    """Read existing profiles/templates only; never discover CLIs or resolve secrets."""
    from .native_profiles import names
    from .cli_discovery import TEMPLATES
    templates = json.loads(TEMPLATES.read_text(encoding='utf-8')) if include_unconfigured else {}
    for name in sorted(set(LOCAL) | set(names()) | set(templates)):
        if name in ('local-qwen', 'qwen38') or name.startswith('planner-'):
            continue
        configured = True
        try:
            value = select(name)
        except (OSError, ValueError, KeyError, TypeError):
            if name not in templates:
                continue
            value = canonical_identity(json.loads(json.dumps(templates[name])))
            configured = False
        yield name, value, configured


def available_models():
    """Describe configured choices without resolving credentials or probing hosts; what this machine's setup
    declines, and shipped local profiles its gateway does not publish, are left out."""
    rows = []
    from .environment import listed, read
    from .native_profiles import names
    setup = read()
    for name in sorted(set(LOCAL) | set(names())):
        try:
            deployment = select(name)
            if not listed(name, deployment, setup):
                continue
            profile = deployment['native_profile']
            rows.append({'deployment': name, 'model': profile['model'],
                         'protocol': profile['protocol']})
        except (ValueError, OSError, KeyError, TypeError):
            if name in LOCAL:
                raise
            # Invalid local profiles remain visible through profiles list.
    return rows


LOCAL_DEFAULT = 'local-default'  # tare auto's local tier: whatever the node's fleet has as its default


def local_default(value=None):
    """The route of the model the node's fleet has as its default (tare fleet): tare's measured profile when one
    ships for it, else the generic profile tare setup wrote; qwen38-gsq while the node has not said."""
    from .environment import gateway_default, generic_name, read, shipped_local
    value = read() if value is None else value
    model = gateway_default(value)
    if not model:
        return 'qwen38-gsq'
    shipped = [n for n in LOCAL if shipped_local(n) and bundled(n)['native_profile']['model'] == model]
    if shipped:
        return model if model in shipped else shipped[0]
    return generic_name(value['local_models']['gateways'][0]['origin'], model)


def select(name=None, path=None, *, role=None):
    if name is not None and path is not None:
        raise ValueError('use_deployment_name_or_native_deployment_file')
    if path is not None:
        return canonical_identity(load_deployment(path))
    name = name or 'local-qwen'
    if name == LOCAL_DEFAULT:
        return select(local_default())
    if name == 'auto':
        if role not in {'implementer', 'explorer'}:
            raise ValueError('AUTO_EXECUTION_REQUIRES_IMPLEMENTER_OR_EXPLORER_ROLE')
        from .native_routes import ranked
        candidates, selection = ranked(role, check_resources=True)
        if not candidates:
            from .scheduling_diagnostics import UnschedulableError
            raise UnschedulableError('NO_ELIGIBLE_NATIVE_ROLE:'+role, selection['candidates'])
        # Saved with the full profile by every execution owner; resume never re-ranks.
        return {**candidates[0], 'native_selection': selection}
    if name not in LOCAL:
        from .native_profiles import local_path
        path = local_path(name)
        if not path.is_file():
            raise ValueError('native_deployment_unknown:' + name)
        return canonical_identity(load_deployment(path))
    result = bundled(name)
    models = {'qwen38': 'qwen-3.8-27b', 'qwen38-gsq': 'qwen-3.8-27b', 'fable-tc': 'fable-tc', 'bedrock-opus': 'claude-opus-4.6',
              'hauhaucs': 'hauhaucs-qwen38', 'qwen36-moe': 'qwen36-moe',
              'bedrock-sonnet': 'claude-sonnet-4.6', 'bedrock-glm': 'glm-5'}
    if LOCAL[name] in models:
        result['model'] = models[LOCAL[name]]
    return canonical_identity(result)


def canonical_identity(result):
    """Bundled API and installed host aliases use the same registered lineage.

    Resolve exact, unambiguous wire aliases only. Request/profile bytes stay as
    configured, and an explicit conflicting model is never silently repaired.
    """
    from .openrouter import configured
    result = configured(result)
    from .landing_policy import model_id_for_runtime
    wire = result['native_profile']['model']
    try:
        canonical = model_id_for_runtime(roster_path(), 'native', wire)
    except ValueError:
        return result
    if result.get('model') in (None, wire, canonical):
        result['model'] = canonical
    return result


def resolve_host_mode(deployment, *, observe=None):
    """Resolve a local-auto profile once, when a new session starts; the saved session keeps the result.

    The gateway recommends its solo model only while the host desktop is idle. Anything else,
    including an unreachable gateway, keeps the profile's own (shared) settings.
    """
    modes = deployment.get('native_host_modes')
    if modes is None:
        return deployment
    base = {k: v for k, v in deployment.items() if k != 'native_host_modes'}
    if observe is None:
        from compute_plane.model_facts import read_json_observation as observe
    try:
        observed = observe(modes['recommend'], timeout=3)
        if observed['status'] != 'OBSERVED':
            raise ValueError(observed.get('reason', 'RECOMMEND_UNAVAILABLE'))
        value = observed['value']
        name = modes['deployments'][value['model']]
        chosen = base if name is None else select(name)
        receipt = {k: value.get(k) for k in ('mode', 'model', 'reason', 'host')}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        chosen, receipt = base, {'mode': 'shared', 'reason': 'RECOMMEND_UNAVAILABLE', 'error': str(exc)[:200]}
    if 'native_model_choice' in deployment:
        chosen = {**chosen, 'native_model_choice': deployment['native_model_choice']}
    return {**chosen, 'native_host_selection': {**receipt, 'source': modes['recommend']}}

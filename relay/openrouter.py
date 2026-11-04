"""OpenRouter route facts and the Contributor data-use choice; no model discovery or inference."""
import json
import re
from copy import deepcopy

ENDPOINT = 'https://openrouter.ai/api/v1'
CONTRIBUTOR = 'meta/muse-spark-1.3-contributor'
NOTICE = 'Meta may use prompts, code and responses to improve its products.'
RESEARCH_ROUTES = tuple('openrouter-'+name for name in (
    'deepseek-v4-1-flash-1017', 'mimo-v2-6-pro', 'glm-5-3', 'glm-5-3-flash',
    'mistral-large-4', 'step-5-preview', 'muse-spark-1-3-contributor', 'grok-4-7',
    'ling-3-1-flash', 'qwen3-8-omni-flash'))


def is_route(value):
    profile = value.get('native_profile', {})
    return profile.get('protocol') in {'openai-chat', 'openai-responses'} and profile.get('endpoint', '').rstrip('/') == ENDPOINT


def allowed(value, settings):
    if is_route(value) and value['native_profile'].get('model') == CONTRIBUTOR:
        if (settings.get('model_data_use') or {}).get('meta_contributor') is not True:
            return False, NOTICE+' Enable Contributor in Settings > Connections to use this model.'
    return True, None


def details(value):
    if not is_route(value):
        return None
    params = json.loads(value['native_profile'].get('parameters_json') or '{}')
    provider = params.get('provider') or {}
    declared = value.get('native_openrouter') or {}
    return {'publisher': declared.get('publisher', 'UNKNOWN'),
            'requested_endpoints': provider.get('only', []),
            'requested_quantizations': provider.get('quantizations', []),
            'precision_provenance': declared.get('precision_provenance', 'unknown'),
            'data_collection': provider.get('data_collection', 'allow'),
            'notice': NOTICE if value['native_profile']['model'] == CONTRIBUTOR else None}


QUANTIZATIONS = {'int4', 'int8', 'fp4', 'mxfp4', 'nvfp4', 'fp6', 'fp8', 'mxfp8', 'fp16', 'bf16', 'fp32', 'unknown'}


def validate_route(route):
    if not isinstance(route, dict) or set(route) - {'providers', 'quantizations', 'allow_fallbacks', 'context_window', 'output_tokens', 'source', 'observed_at'}:
        raise ValueError('OpenRouter route accepts providers, quantizations and allow_fallbacks only.')
    providers = route.get('providers')
    if (not isinstance(providers, list) or not 1 <= len(providers) <= 8 or
            any(not isinstance(p, str) or not re.fullmatch(r'[a-z0-9-]+(?:/[a-z0-9._-]+)*', p) for p in providers)):
        raise ValueError('Choose one to eight exact OpenRouter provider endpoint slugs.')
    quant = route.get('quantizations', [])
    if not isinstance(quant, list) or any(q not in QUANTIZATIONS for q in quant):
        raise ValueError('Unknown OpenRouter quantization; use the endpoint catalogue, not a quality ranking.')
    if type(route.get('allow_fallbacks', False)) is not bool:
        raise ValueError('OpenRouter allow_fallbacks must be boolean.')
    for field in ('context_window', 'output_tokens'):
        if field in route and (type(route[field]) is not int or route[field] <= 0):
            raise ValueError('OpenRouter endpoint limits must be positive integers.')
    return route


def inspect_route(model, route, required_parameters=('tools', 'tool_choice')):
    """Read endpoint metadata before saving a new choice; a model's aggregate window is not sufficient."""
    from urllib.parse import quote
    from compute_plane.model_facts import read_json_observation
    address = ENDPOINT+'/models/'+quote(model, safe='/')+'/endpoints'
    observed = read_json_observation(address, ttl=60, max_bytes=512*1024)
    if observed.get('status') != 'OBSERVED':
        raise ValueError('Could not check OpenRouter endpoints. The previous route was kept; try again when connected.')
    entries = observed['value'].get('data', {}).get('endpoints', [])
    selected = []
    quantizations = set(route.get('quantizations') or [])
    for provider in route['providers']:
        matching = []
        for row in entries:
            tag, quant = row.get('tag', ''), row.get('quantization') or 'unknown'
            precision = not quantizations or quant in quantizations or quant in {'mxfp4', 'nvfp4'} and 'fp4' in quantizations or quant == 'mxfp8' and 'fp8' in quantizations
            if ((tag == provider or tag.startswith(provider+'/')) and precision
                    and set(required_parameters) <= set(row.get('supported_parameters', []))):
                matching.append(row)
        if not matching:
            raise ValueError('No endpoint matches '+provider+', precision and required parameters ('+', '.join(required_parameters)+'). The previous route was kept.')
        selected.extend(matching)
    if any(type(row.get('context_length')) is not int or type(row.get('max_completion_tokens')) is not int for row in selected):
        raise ValueError('Endpoint context/output limits are unavailable; the previous route was kept.')
    return {**route, 'context_window': min(row['context_length'] for row in selected),
            'output_tokens': min(row['max_completion_tokens'] for row in selected),
            'source': address, 'observed_at': observed.get('observed_at')}


def configured(value, settings=None):
    """Apply machine route choices on selection only. Saved session profiles never pass through here."""
    if not is_route(value):
        return value
    from .environment import read
    settings = read() if settings is None else settings
    routes = settings.get('openrouter_routes') or {}
    if not isinstance(routes, dict):
        raise ValueError('OpenRouter route settings must be an object.')
    override = routes.get(value['native_profile']['model'])
    if override is None:
        return value
    validate_route(override)
    value = deepcopy(value)
    profile = value['native_profile']
    params = json.loads(profile.get('parameters_json') or '{}')
    provider = params.setdefault('provider', {})
    provider.update(only=override['providers'], order=override['providers'],
                    allow_fallbacks=override.get('allow_fallbacks', False), require_parameters=True)
    provider.pop('quantizations', None)
    if override.get('quantizations'):
        provider['quantizations'] = override['quantizations']
    profile['parameters_json'] = json.dumps(params, sort_keys=True)
    if override.get('context_window'):
        profile['context_window'] = min(profile['context_window'], override['context_window'])
    if override.get('output_tokens'):
        profile['output_tokens'] = min(profile['output_tokens'], override['output_tokens'])
    return value


def route_command(args):
    from .environment import apply, read
    from .native_catalog import LOCAL, bundled, select
    value = bundled(args.name) if args.reset and args.name in LOCAL else select(args.name)
    if not is_route(value):
        raise ValueError('Choose an OpenRouter deployment from tare models --provider openrouter.')
    settings = read()
    if 'error' in settings:
        raise ValueError('Fix the machine settings before changing an OpenRouter route.')
    changed = args.reset or args.provider or args.quantization or args.allow_fallbacks
    if changed:
        routes = dict(settings.get('openrouter_routes') or {})
        model = value['native_profile']['model']
        if args.reset:
            if args.provider or args.quantization or args.allow_fallbacks:
                raise ValueError('Use --reset alone to restore the shipped route.')
            routes.pop(model, None)
        else:
            if not args.provider:
                raise ValueError('Name the endpoint with --provider; repeat it for an explicit fallback list.')
            parameters = json.loads(value['native_profile'].get('parameters_json') or '{}')
            required = ('tools', 'tool_choice', 'reasoning_effort' if parameters.get('reasoning', {}).get('effort') else 'reasoning')
            routes[model] = inspect_route(model, validate_route({'providers': args.provider, 'quantizations': args.quantization,
                                                                'allow_fallbacks': args.allow_fallbacks}), required)
        settings['openrouter_routes'] = routes
        from .native_generation import deployment_profile
        candidate = configured(value, settings)
        deployment_profile(candidate, candidate['native_profile']['model'])
        apply(settings)
        value = select(args.name)
    return {'deployment': args.name, **details(value), 'scope': 'New selections only; saved sessions retain their route.',
            'availability': 'UNVERIFIED; settings were not sent to a model.', 'inference_calls': 0}


class Client:
    """Keep the reported serving provider beside the existing HTTP observation, without response text."""
    def __init__(self, inner, deployment=None):
        self.inner = inner
        self.deployment = deployment
        self.served_provider = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _call(self, method, *args, **kwargs):
        self.served_provider = None
        if self.deployment and self.deployment['native_profile']['model'] == CONTRIBUTOR:
            from .environment import read
            permitted, reason = allowed(self.deployment, read())
            if not permitted:
                raise ValueError(reason)
        result = getattr(self.inner, method)(*args, **kwargs)
        reply = result[0] if isinstance(result, tuple) else result
        provider = reply.get('provider') if isinstance(reply, dict) else None
        if isinstance(provider, str) and 0 < len(provider) <= 128 and not any(ord(c) < 32 for c in provider):
            self.served_provider = provider
        return result

    def post(self, *args, **kwargs):
        return self._call('post', *args, **kwargs)

    def post_observed(self, *args, **kwargs):
        return self._call('post_observed', *args, **kwargs)

    def post_stream_observed(self, *args, **kwargs):
        return self._call('post_stream_observed', *args, **kwargs)


def observer(value, client, sink):
    requested = details(value)
    if requested is None:
        return sink

    def record(row):
        row['openrouter'] = {**requested, 'served_provider': getattr(client, 'served_provider', None) if row.get('purpose') != 'count' else None,
                             'served_quantization': 'UNKNOWN'}
        sink(row)
    return record

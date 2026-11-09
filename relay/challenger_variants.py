"""An experimental arm is a frozen deployment, task class and resource contract."""
from copy import deepcopy
import hashlib
import json
from datetime import date
from decimal import Decimal

from compute_plane.choice_assessment import digest


def accounting(deployment):
    from .challenger import local
    if deployment.get('native_challenger_accounting'):
        return deployment['native_challenger_accounting']
    protocol = deployment['native_profile']['protocol']
    if local(deployment):
        return 'local'
    from .opencode import subscription
    if protocol in {'vendor-cli', 'openai-codex-responses'} or subscription(deployment):
        return 'quota'
    return 'usd'


def realize(value):
    from .native_catalog import select
    from .adaptive_routes import proposed_route
    from .native_generation import deployment_profile
    if isinstance(value, str):
        value = {'profile': value}
    if (not isinstance(value, dict) or set(value) - {'profile', 'reasoning', 'parameters', 'task_types',
            'label', 'identity', 'resource_pool', 'accounting', 'context_projection'}
            or not isinstance(value.get('profile'), str)
            or value['profile'] == 'auto'):
        raise ValueError('Each variant needs an explicit profile, with optional reasoning, parameters and task_types.')
    selected = select(value['profile'])
    route = {'deployment': value['profile'], 'effort': value.get('reasoning', 'default')}
    selected = proposed_route(route, [selected])
    if 'accounting' in value:
        if value['accounting'] not in {'local', 'quota', 'usd'}:
            raise ValueError('Accounting must be local, quota or usd.')
        selected['native_challenger_accounting'] = value['accounting']
    if 'parameters' in value:
        parameters = value['parameters']
        if not isinstance(parameters, dict):
            raise ValueError('Variant parameters must be a JSON object.')
        selected['native_profile']['parameters_json'] = json.dumps({
            **json.loads(selected['native_profile'].get('parameters_json', '{}')), **parameters}, sort_keys=True)
    if 'context_projection' in value:
        # Same model, different context policy: e.g. {"elide_edited_reads": true}.
        selected['native_context_projection'] = value['context_projection']
    deployment_profile(selected, selected['native_profile']['model'])
    tasks = value.get('task_types', ['implementation'])
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) or not t.strip() for t in tasks):
        raise ValueError('Variant task_types must name the task groups it should sample.')
    identity = value.get('identity', {})
    if not isinstance(identity, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v
                                           for k, v in identity.items()):
        raise ValueError('Expected serving identity must map field names to exact observed values.')
    kind = accounting(selected)
    if identity and (kind != 'local' or selected.get('native_counter_protocol') != 'slop-fleet-v1'):
        raise ValueError('Expected serving identity requires a local fleet profile with observed bindings.')
    parameters = json.loads(selected['native_profile'].get('parameters_json', '{}'))
    if any(k in parameters for k in ('lora', 'lora_adapter', 'lora_adapters', 'adapter')) and not any(
            k in identity for k in ('adapter_sha256', 'adapter_configuration_sha256')):
        raise ValueError('Adapter experiments need an expected serving identity for the adapter; a label cannot prove activation.')
    if 'chat_template' in parameters and 'template_sha256' not in identity:
        raise ValueError('Template experiments need an expected serving identity for the template.')
    if 'template_sha256' in identity:
        selected['native_profile']['template_sha256'] = identity['template_sha256']
        deployment_profile(selected, selected['native_profile']['model'])
    pool = value.get('resource_pool')
    if pool is not None and (not isinstance(pool, str) or not pool.strip()):
        raise ValueError('Resource pool must be a nonempty label.')
    # Endpoint/CLI account is shared across models and efforts; never infer a fresh quota per model.
    account = selected.get('native_codex_auth', {}).get('account_id', '')
    scope = pool or selected['native_profile']['endpoint']+'|'+account
    return {'deployment': selected, 'accounting': kind, 'task_types': tasks, 'identity': identity,
            'label': value.get('label') or value['profile'], 'key': digest([selected, tasks, identity]),
            'resource_pool': hashlib.sha256(scope.encode()).hexdigest()[:24]}


def preflight(variant, *, allow_unknown_quota=False, quota_floor=25):
    from .route_facts import collect, admission_reason
    facts = collect(variant['deployment'], live=True)
    reason = admission_reason(facts)
    if reason:
        raise ValueError(reason)
    if variant['accounting'] == 'quota':
        quota = facts['quota']
        if quota['status'] == 'UNKNOWN' and not allow_unknown_quota:
            raise ValueError('Quota balance is unknown. Enable a bounded quota probe explicitly or supply a fresh provider observation.')
        if quota.get('unit') == 'percent' and isinstance(quota.get('remaining'), (float, int)) and quota['remaining'] < quota_floor:
            raise ValueError('Quota headroom reserved for normal work.')
    return facts


def price(deployment):
    """Reuse provider catalogs; no invented conversion of subscriptions into USD."""
    from .provider_prices import quote
    from .native_spend import settings
    if accounting(deployment) != 'usd':
        return
    if not deployment.get('native_usage_tariff'):
        observed = quote(deployment, live=True)
        if observed.get('valid_until') and date.today() > date.fromisoformat(observed['valid_until']):
            raise ValueError('Provider price reference expired; refresh it before spending.')
        rates = observed.get('rates')
        if not rates:
            raise ValueError('Provider price is unavailable or conditional; set a matching conservative tariff for this profile.')
        rates = dict(rates)
        for key, value in observed.get('long_rates', {}).items():
            if rates.get(key) is not None and value is not None:
                rates[key] = str(max(Decimal(rates[key]), Decimal(value)))
        from .native_installation import storage_root
        from .native_agent import read_json
        from compute_plane.native_journal import save_json
        path = storage_root()/'challenger-prices'/(digest(deployment['native_profile'])+'.json')
        source = observed.get('price_source') or observed['source']
        previous = read_json(path) if path.exists() else {}
        # Refresh the observation, retaining the effective date while the tariff
        # is unchanged. Otherwise identical deliveries never form a cohort.
        effective = (previous['effective_at'] if previous.get('rates') == rates and previous.get('source') == source
                     else observed['observed_at'])
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json(path, dict(rates=rates, source=source, effective_at=effective, observed_at=observed['observed_at']))
        deployment['native_usage_tariff'] = dict(currency='USD', source=source,
            effective_at=effective, rates_json=json.dumps(rates, sort_keys=True))
    policy, tariff = settings(deployment)
    required = ('uncached_input_tokens', 'cache_read_tokens', 'output_tokens')
    if deployment['native_usage_contract']['layout'] == 'converse-exclusive':
        required += ('cache_write_tokens',)
    if not policy or any(json.loads(tariff.rates_json).get(k) is None for k in required):
        raise ValueError('A complete provider tariff is required to reserve this API experiment.')


class IdentityClient:
    """Check owner-reported effective settings before allowing generation."""
    def __init__(self, client, expected):
        self.client, self.expected = client, deepcopy(expected)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def post(self, path, body, control):
        value = self.client.post(path, body, control)
        if path == '/fleet/count':
            profile = value.get('profile', {})
            if any(profile.get(k) != v for k, v in self.expected.items()):
                from tare_tools_agent_runtime import ModelFailure
                raise ModelFailure('CHALLENGER_EFFECTIVE_CONFIGURATION_MISMATCH', not_started=True)
        return value

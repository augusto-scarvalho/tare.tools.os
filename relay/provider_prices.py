"""Explicit price snapshots for delivery studies; no inference or billing credentials."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import re

from compute_plane.choice_assessment import digest
from compute_plane.model_facts import read_json_observation
from compute_plane.native_usage import BILLABLE, deployment_contract
from .route_facts import OPENROUTER, advertised_price

CODEX = 'https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing'
GOOGLE = 'https://ai.google.dev/gemini-api/docs/pricing'
ANTHROPIC = 'https://www-cdn.anthropic.com/files/4zrzovbb/website/3684c2faafb97418665782cea0001f439f74b1d2.pdf'
OBSERVED = '2026-09-23'


def rates(inp, cache, write, out):
    return dict(zip(BILLABLE, (inp, cache, write, out)))


def bedrock_rates(data, model, region):
    """Exact on-demand runtime SKU; never select Mantle, batch or tiered rates."""
    names = {'zai.glm-5': 'GLM 5'}
    values, skus = {k: [] for k in BILLABLE}, []
    for sku, product in data.get('products', {}).items():
        a = product.get('attributes', {})
        if (model not in names or a.get('model') != names[model] or a.get('regionCode') != region
                or a.get('feature') != 'On-demand Inference'):
            continue
        field = {'Input tokens': 'uncached_input_tokens', 'Output tokens': 'output_tokens',
                 'Cache read tokens': 'cache_read_tokens', 'Cache write tokens': 'cache_write_tokens'}.get(a.get('inferenceType'))
        if field is None:
            continue
        for term in data.get('terms', {}).get('OnDemand', {}).get(sku, {}).values():
            for dim in term.get('priceDimensions', {}).values():
                factor = {'1K tokens': 1000, '1M tokens': 1, 'tokens': 1000000}.get(dim.get('unit'))
                if factor and dim.get('beginRange') == '0' and dim.get('endRange') == 'Inf':
                    value = Decimal(dim['pricePerUnit']['USD']) * factor
                    if value.is_finite() and value >= 0:
                        values[field].append(str(value)); skus.append(sku)
    return {k: v[0] if len(v) == 1 else None for k, v in values.items()}, sorted(set(skus))


def quote(deployment, *, live=False, cache=None):
    cache = {} if cache is None else cache
    p = deployment['native_profile']; model = p['model']; endpoint = p.get('endpoint', '')
    contract = deployment_contract(deployment)
    row = {'profile_sha256': digest(deployment), 'model': model, 'usage_contract': contract,
           'mode': 'unknown', 'currency': 'USD', 'source': None, 'observed_at': OBSERVED,
           'scope': 'Estimate at snapshot prices; discounts, taxes and other costs belong in additional_cost_usd.'}
    tariff = deployment.get('native_usage_tariff')
    if tariff:
        return {**row, 'mode': 'tokens', 'currency': tariff['currency'], 'source': tariff['source'],
                'observed_at': tariff['effective_at'], 'rates': json.loads(tariff['rates_json'])}

    def fetch(url, size=4*1024*1024):
        if url not in cache:
            cache[url] = read_json_observation(url, timeout=10, ttl=3600, max_bytes=size)
        return cache[url]

    if endpoint.rstrip('/') == 'https://openrouter.ai/api/v1':
        row.update(mode='reported', source='openrouter.usage.cost', scope='Reported account charge, excluding external BYOK charges.')
        if live:
            obs = fetch(OPENROUTER)
            cards = obs.get('value', {}).get('data', [])
            card = next((c for c in cards if c.get('id') == model), None)
            row['catalog_status'] = obs['status']
            if card:
                price = advertised_price(card, obs)
                row['catalog_reference'] = price
                # A flat quote cannot safely apply a conditional provider schedule.
                if not price['schedule_overrides']:
                    row.update(rates=price['per_million_tokens'], price_source=OPENROUTER,
                               observed_at=datetime.fromtimestamp(obs['observed_at'], timezone.utc).isoformat())
    elif deployment.get('native_opencode'):
        row.update(mode='reported', source='opencode.usage.cost',
                   scope='OpenCode balance charge: $0 for Go use within its limits; list_cost and allowance inform only.')
    elif endpoint == 'cli://claude':
        row.update(mode='client_estimate', source='claude.result.total_cost_usd',
                   scope='CLI estimate of API-equivalent usage; subscription invoice not inferred.')
    elif endpoint == 'cli://codex' or p.get('protocol') == 'openai-codex-responses':
        prices = {'gpt-6-astra': ('10', '1', '0', '50'), 'gpt-6-sol': ('2', '.2', '0', '10'),
                  'gpt-6-luna': ('.1', '.01', '0', '.5'), 'gpt-5.6-sol': ('4', '.4', '0', '20'),
                  'gpt-5.6-terra': ('2', '.2', '0', '12'), 'gpt-5.6-luna': ('.2', '.02', '0', '1.2'),
                  'gpt-5.5': ('5', '.5', '0', '30')}
        row.update(source=CODEX, scope='Codex token-based USD reference; included subscription usage is not an invoice.')
        if model in prices:
            row.update(mode='tokens', rates=rates(*prices[model]), valid_until='2026-10-23')
            # Cache creation has no surcharge in Codex; its input still receives
            # the ordinary input rate. Normalized categories are disjoint.
            row['rates']['cache_write_tokens'] = row['rates']['uncached_input_tokens']
            params = json.loads(p.get('parameters_json', '{}'))
            tier = params.get('service_tier', 'default')
            if tier not in ('default', 'auto', 'priority'):
                row['mode'] = 'unknown'
            if tier == 'priority':
                row['rates'] = {k: str(Decimal(v)*Decimal('2.5')) for k, v in row['rates'].items()}
            if model != 'gpt-6-astra':
                row['long_context_above'] = 272000
                row['long_rates'] = {k: str(Decimal(v)*Decimal('1.5' if k == 'output_tokens' else '2'))
                                     for k, v in row['rates'].items()}
    elif endpoint == 'cli://agy':
        row.update(source=GOOGLE, scope='Gemini paid API-equivalent estimate; AGY subscription invoice not inferred.')
        if model in {'gemini-3.8-flash', 'gemini-3.8-flash-high', 'gemini-3.8-flash-low'}:
            row.update(mode='tokens', rates=rates('.75', '.075', '0', '3.75'), valid_until='2026-10-23')
    elif p.get('protocol') == 'bedrock-converse':
        match = re.fullmatch(r'https://bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com', endpoint.rstrip('/'))
        if not match:
            return row
        region = match[1]; row['region'] = region
        params = json.loads(p.get('parameters_json', '{}'))
        if params.get('serviceTier', {}).get('type', 'default') not in ('default', 'standard'):
            return row
        # These marketplace SKUs are absent from the AmazonBedrock bulk offer.
        table = {'us.anthropic.claude-opus-4-6-v1': ('5.5', '.55', '6.875', '27.5'),
                 'us.anthropic.claude-sonnet-4-6': ('3.3', '.33', '4.125', '16.5')}
        if model in table and region in {'us-east-1', 'us-east-2', 'us-west-2'}:
            row.update(mode='tokens', rates=rates(*table[model]), source=ANTHROPIC,
                       observed_at=OBSERVED, valid_until='2026-10-23',
                       scope='Bedrock standard US geo inference, 5-minute cache writes; list estimate.')
        elif live:
            url = f'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/{region}/index.json'
            obs = fetch(url, 32*1024*1024)
            values, skus = bedrock_rates(obs.get('value', {}), model, region)
            row.update(source=url, catalog_status=obs['status'], skus=skus)
            if skus:
                row.update(mode='tokens', rates=values,
                           observed_at=datetime.fromtimestamp(obs['observed_at'], timezone.utc).isoformat())
    elif deployment.get('native_counter_protocol') == 'slop-fleet-v1':
        row.update(mode='hourly', usd_per_hour=None, source='operator infrastructure allocation',
                   scope='Allocated cost per active session hour, including inference, verification and retries.')
    return row


def snapshot(names=(), *, live=False):
    from .native_catalog import select, LOCAL
    from .native_profiles import names as imported
    cache = {}; rows = {}
    for name in names or sorted(set(LOCAL) | set(imported())):
        try:
            deployment = select(name)
            key = deployment['id']
            if key not in rows:
                rows[key] = {**quote(deployment, live=live, cache=cache), 'aliases': []}
            rows[key]['aliases'].append(name)
        except (OSError, ValueError, KeyError, TypeError):
            rows[name] = {'mode': 'unknown', 'reason': 'INVALID_PROFILE_OR_PRICE_SOURCE'}
    return {'schema': 'tare.tools/delivery-costs/1', 'basis': 'estimated_delivery_cost',
            'profiles': rows, 'generation_calls': 0,
            'note': 'Set study.costs to the absolute path of this snapshot. For paid plans/local infrastructure, '
                    'replace the route mode with hourly and set usd_per_hour plus source. Unknown is not zero.'}

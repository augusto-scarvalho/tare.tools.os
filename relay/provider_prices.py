"""Explicit price snapshots for delivery studies, and the models.dev reference prices of pay-per-token routes;
no inference or billing credentials."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import math
import re
import time
from urllib.parse import urlsplit

from compute_plane.choice_assessment import digest
from compute_plane.model_facts import read_json_observation
from compute_plane.native_usage import BILLABLE, deployment_contract
from .route_facts import OPENROUTER, advertised_price

CODEX = 'https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing'
GOOGLE = 'https://ai.google.dev/gemini-api/docs/pricing'
ANTHROPIC = 'https://www-cdn.anthropic.com/files/4zrzovbb/website/3684c2faafb97418665782cea0001f439f74b1d2.pdf'
OBSERVED = '2026-09-23'
# models.dev: an open, community-maintained price list (docs/guides/HTTP_API_PROVIDERS.md, Prices). A tariff whose
# source starts with REFERENCE came from it: a reference, never a confirmed tariff, wherever it is shown or spent.
MODELS_DEV = 'https://models.dev/api.json'
REFERENCE = 'models.dev reference price'
READING = 'tare.tools/reference-prices/1'
# The routes a models.dev entry prices, under models.dev's provider id: the first-party APIs by endpoint (the API
# hosts of vendor_models.ORIGINS) and Bedrock by the exact wire id it sends, whose regional prefix changes the price.
REFERENCE_HOSTS = {'api.anthropic.com': 'anthropic', 'api.openai.com': 'openai',
                   'generativelanguage.googleapis.com': 'google'}
BEDROCK = re.compile(r'https://bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com')
# Their keys: with none of them set, or all declined in tare setup, models.dev is not read.
REFERENCE_KEYS = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'AWS_BEARER_TOKEN_BEDROCK')


def rates(inp, cache, write, out):
    return dict(zip(BILLABLE, (inp, cache, write, out)))


def reading_path():
    from .native_installation import storage_root
    return storage_root()/'serve'/'reference-prices.json'


_reading = (None, None)  # (file stamp, reading): one tuple, so a thread never sees half of it


def reading():
    """The last good models.dev reading ({'read_at', 'models': {'provider/id': {'rates', 'tiers', 'since'}}}), or
    None: nothing read yet, and every price stays as it was without one. Read once per change of the file."""
    global _reading
    path = reading_path()
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    seen, value = _reading
    if seen != stamp:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = None
        value = value if isinstance(value, dict) and value.get('schema') == READING and \
            type(value.get('read_at')) in (int, float) and isinstance(value.get('models'), dict) else None
        _reading = (stamp, value)
    return value


def _rates(cost):
    """A models.dev cost block in tare's categories (decimal strings per million tokens), or None without a numeric
    input and output price. A missing or non-numeric cache price is unknown (null), never guessed."""
    def number(key):
        value = cost.get(key)
        return str(Decimal(str(value))) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    found = rates(number('input'), number('cache_read'), number('cache_write'), number('output'))
    return found if found['uncached_input_tokens'] is not None and found['output_tokens'] is not None else None


def _tier(tier):
    """{'above': context size, 'rates'} of one models.dev price tier, or None when it cannot be read. A tier of
    another kind, or without a readable size, is taken to apply always: an upper bound."""
    values = _rates(tier) if isinstance(tier, dict) else None
    if values is None:
        return None
    limit = tier.get('tier')
    size = limit.get('size') if isinstance(limit, dict) and limit.get('type') == 'context' else None
    return {'above': size if type(size) is int and size > 0 else 0, 'rates': values}


def parse_reading(data):
    """{'provider/id': {'rates', 'tiers'}} for the providers whose routes take a reference price, or None when the
    file is not shaped as expected (one of them missing, or without a single priced model). Unknown fields are
    ignored; a model without numeric input and output prices, or with a tier tare cannot read (so cannot bound), is
    left out."""
    if not isinstance(data, dict):
        return None
    models = {}
    for provider in (*REFERENCE_HOSTS.values(), 'amazon-bedrock'):
        entries = data.get(provider).get('models') if isinstance(data.get(provider), dict) else None
        if not isinstance(entries, dict):
            return None
        priced = 0
        for key, entry in entries.items():
            cost = entry.get('cost') if isinstance(entry, dict) else None
            base = _rates(cost) if isinstance(cost, dict) else None
            if base is None:
                continue
            over, listed = cost.get('context_over_200k'), cost.get('tiers') or []
            tiers = [_tier({**over, 'tier': {'type': 'context', 'size': 200000}} if isinstance(over, dict) else None)
                     ] if over is not None else []
            tiers += [_tier(t) for t in listed] if isinstance(listed, list) else [None]
            if None not in tiers:
                models[provider+'/'+key] = {'rates': base, 'tiers': tiers}
                priced += 1
        if not priced:
            return None
    return models


def fetch_models_dev():
    """GET models.dev. Its CDN refuses urllib's own user agent, so it is sent a generic one; nothing else is sent:
    no key, no detail of this machine or its user."""
    from urllib.request import Request, urlopen
    with urlopen(Request(MODELS_DEV, headers={'Accept': 'application/json', 'User-Agent': 'tare.tools'}),
                 timeout=30) as reply:
        raw = reply.read(32*1024*1024 + 1)
    if len(raw) > 32*1024*1024:
        raise ValueError('models.dev reading over 32 MiB')
    return json.loads(raw)


def refresh_reading(now=None, fetch=fetch_models_dev):
    """Read models.dev at most once a day and keep it with the day it was read. A failed, empty or reshaped reading
    keeps the last good one."""
    global _reading
    now = time.time() if now is None else now
    saved = reading()
    if saved and 0 <= now - saved['read_at'] < 86400:
        return saved
    from .environment import declined, read
    from .provider_quota import secret
    setup = read()
    api = setup.get('api_providers') or {}
    if 'error' in setup or api.get('use') is False or not any(
            not declined((api.get('items') or {}).get(key)) and secret(key) for key in REFERENCE_KEYS):
        return saved  # no route that would use it is set up here
    try:
        models = parse_reading(fetch())
    except Exception:  # whatever went wrong reading it (network, HTTP, JSON), the last good reading stays
        models = None
    if not models:
        return saved
    day, before = datetime.fromtimestamp(now, timezone.utc).date().isoformat(), (saved or {}).get('models', {})
    # A price keeps the day it was first read while it stays the same, so a session spending against it can resume.
    for key, entry in models.items():
        old = before.get(key) if isinstance(before.get(key), dict) else {}
        same = (old.get('rates'), old.get('tiers')) == (entry['rates'], entry['tiers'])
        entry['since'] = old['since'] if same and isinstance(old.get('since'), str) else day
    value = {'schema': READING, 'source': MODELS_DEV, 'read_at': now, 'models': models}
    from .relay_io import write_json_atomic
    reading_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(reading_path(), value)
    _reading = (None, None)
    return value


def priced_mode(value):
    """Whether request parameters choose a mode with its own price (a service tier, fast speed), which a reference
    for the standard mode does not cover."""
    if isinstance(value, list):
        return any(priced_mode(v) for v in value)
    return isinstance(value, dict) and any(
        k in {'service_tier', 'serviceTier', 'speed'} and (v.get('type') if isinstance(v, dict) else v)
        not in ('default', 'auto', 'standard') or priced_mode(v) for k, v in value.items())


def reference(profile, saved=None):
    """The models.dev reference price of this route's model on the provider it is on, as a native_usage_tariff, or
    None: no reading, a route of another kind, a priced mode, or a model the reading does not list under exactly
    that id and provider (never another version's or another provider's price)."""
    try:
        endpoint = str(profile.get('endpoint') or '').rstrip('/')
        provider = ('amazon-bedrock' if profile.get('protocol') == 'bedrock-converse' and BEDROCK.fullmatch(endpoint)
                    else REFERENCE_HOSTS.get(urlsplit(endpoint).hostname) if endpoint.startswith('https://') else None)
        saved = (reading() if saved is None else saved) if provider else None
        if not saved:
            return None
        key = provider+'/'+str(profile.get('model'))
        entry = saved['models'].get(key)
        if not entry or priced_mode(json.loads(profile.get('parameters_json') or '{}')):
            return None
        # ponytail: a tariff has one rate per category, so a route whose window reaches a context tier is priced at
        # the highest such tier on every call, smaller calls too (an upper bound: over, never under). Exact per-call
        # tiers need a tier-aware price_usage in the Kernel; below every tier (most routes) the base price is exact.
        window = profile.get('context_window')
        applied = [entry['rates'], *(t['rates'] for t in entry['tiers']
                                     if type(window) is not int or window >= t['above'])]
        merged = {k: None if any(r[k] is None for r in applied) else str(max(Decimal(r[k]) for r in applied))
                  for k in BILLABLE}
        since = entry['since']
        if not isinstance(since, str) or not since.strip():
            return None
    except (ValueError, ArithmeticError, TypeError, KeyError, AttributeError):  # a damaged file or profile: unknown
        return None
    return {'currency': 'USD', 'source': f'{REFERENCE} (not a confirmed tariff) for {key}, {MODELS_DEV}',
            'effective_at': since, 'rates_json': json.dumps(merged, sort_keys=True)}


def is_reference(tariff):
    """Whether a tariff (or a call's cost['tariff']) is a models.dev reference price."""
    return isinstance(tariff, dict) and str(tariff.get('source', '')).startswith(REFERENCE)


def reference_note(tariff):
    """How a cost valued at this reference is labelled wherever it is shown."""
    return f"models.dev reference price read {tariff['effective_at']}, not a confirmed tariff"


def reference_totals(calls):
    """For a usage total: how many started calls were valued at a reference price, and the days those were read."""
    days = [str(c['cost']['tariff'].get('effective_at')) for c in calls
            if isinstance(c, dict) and c.get('execution') != 'not_started' and isinstance(c.get('cost'), dict)
            and is_reference(c['cost'].get('tariff'))]
    return {'reference_priced_calls': len(days), 'reference_prices_read': sorted(set(days))} if days else {}


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
                   scope='OpenCode balance charge: $0 for subscription use within its allowance; list_cost and allowance inform only.')
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
        match = BEDROCK.fullmatch(endpoint.rstrip('/'))
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
    # Without a price of tare's own that still holds, a pay-per-token API route takes its model's models.dev
    # reference (only first-party APIs and Bedrock have one; other routes and a missing reading keep the row).
    if row['mode'] == 'unknown' or row.get('valid_until', '9999-12-31') < date.today().isoformat():
        price = reference(p)
        if price:
            for stale in ('valid_until', 'skus', 'catalog_status'):
                row.pop(stale, None)
            row.update(mode='tokens', basis='reference', rates=json.loads(price['rates_json']), source=price['source'],
                       observed_at=datetime.fromtimestamp(reading()['read_at'], timezone.utc).isoformat(),
                       effective_at=price['effective_at'],
                       scope=reference_note(price) + ': an open community list that can be wrong; a spending cap '
                             'and a declared balance are only as exact as it.')
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

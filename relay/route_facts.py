"""Source-labelled model facts; no invented quality score or quota balance."""
import json
import os
import time
from decimal import Decimal, InvalidOperation

from compute_plane.model_facts import read_json_observation, gpu_availability
from graph_backlog.jsonutil import sha256_canonical

OPENROUTER = 'https://openrouter.ai/api/v1/models'


def supplied_evidence():
    path = os.environ.get('TARE_ROUTE_EVIDENCE')
    if not path:
        return {}
    from .native_agent import read_json
    try:
        data = read_json(path, 65536)
        return data.get('profiles', {}) if data.get('schema') == 'tare.tools/route-evidence/1' else {}
    except (OSError, ValueError, AttributeError):
        return {}


def quota_fact(record, *, now=None):
    now = time.time() if now is None else now
    unknown = {'status': 'UNKNOWN', 'reason': 'NO_FRESH_VENDOR_QUOTA_OBSERVATION'}
    if not isinstance(record, dict):
        return unknown
    try:
        if (record.get('status') not in {'AVAILABLE', 'EXHAUSTED'} or not record.get('source')
                or record.get('kind') != 'vendor-quota-report'
                or not record['observed_at'] <= now <= record['expires_at']
                or record['expires_at'] - record['observed_at'] > 300):
            return unknown
        return {k: record.get(k) for k in ('status', 'source', 'observed_at', 'expires_at',
                                          'remaining', 'unit', 'resets_at', 'quota_domain')}
    except (KeyError, TypeError):
        return unknown


def advertised_price(card, observation):
    price = card.get('pricing', {})
    rates = {}
    for remote, category in [('prompt', 'uncached_input_tokens'), ('completion', 'output_tokens'),
                             ('input_cache_read', 'cache_read_tokens'), ('input_cache_write', 'cache_write_tokens')]:
        value = price.get(remote)
        try:
            amount = Decimal(value) if isinstance(value, str) else None
            rates[category] = str(amount * 1000000) if amount is not None and amount.is_finite() and amount >= 0 else None
        except InvalidOperation:
            rates[category] = None
    return {'status': 'ADVERTISED', 'currency': 'USD', 'per_million_tokens': rates,
            'source': observation['source'], 'observed_at': observation['observed_at'],
            'expires_at': observation['expires_at'], 'schedule_overrides': price.get('overrides', []),
            'scope': 'Catalog reference, not a provider-bound invoice or guaranteed cache hit.'}


def fleet_route(deployment):
    """Served by this machine's model gateway, whatever it counts with (slop.cpp tokenizes, NInfer estimates)."""
    if deployment.get('native_counter_protocol') == 'slop-fleet-v1':
        return True
    from .native_catalog import local_gateway
    try:
        return deployment['native_profile']['endpoint'].rstrip('/').removesuffix('/v1') == local_gateway()
    except ValueError:
        return False


def collect(deployment, *, live=False, cache=None, evidence=None):
    """Cache lives for one selection only; never reuse GPU state across dispatches."""
    cache = {} if cache is None else cache
    evidence = supplied_evidence() if evidence is None else evidence
    profile = deployment['native_profile']
    fingerprint = sha256_canonical(deployment)
    supplemental = evidence.get(fingerprint, {}) if isinstance(evidence, dict) else {}
    if not isinstance(supplemental, dict): supplemental = {}
    from .model_choices import describe
    from .model_evidence import configured_effort, references
    route = describe(deployment['id'], deployment)['route']
    result = {'schema': 'tare.tools/route-facts/1', 'profile_sha256': fingerprint,
              'model': profile['model'], 'protocol': profile['protocol'],
              'context_window': profile.get('context_window'), 'output_cap': profile.get('output_tokens'),
              'profile_source': profile.get('source'),
              'supported_efforts': route['efforts'], 'configured_effort': configured_effort(profile, route['default_effort']),
              'quota': quota_fact(supplemental.get('quota')),
              'price': {'status': 'UNKNOWN'}, 'quality': {'status': 'UNKNOWN', 'records': []},
              'resource': {'status': 'NOT_CHECKED', 'immediate': None}}
    # Benchmarks retain their applicability and provenance; never average different
    # tasks/efforts/runtimes into an authoritative global model ranking.
    records = supplemental.get('quality', [])
    if isinstance(records, list):
        valid = [r for r in records if isinstance(r, dict) and all(r.get(k) is not None
                 for k in ('source', 'observed_at', 'task_class', 'metric', 'value', 'kind', 'scope'))]
        if valid:
            result['quality'] = {'status': 'SUPPLIED_EVIDENCE', 'records': [dict(r, applicability={
                'level': 'EXACT_PROFILE', 'current_profile_qualified': False}) for r in valid],
                                 'confidence': 'Not converted into a probability of task success.'}
    try:
        reference = references(deployment, result['configured_effort'])
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        reference = {'records': [], 'price_references': []}
        result['quality']['reference_error'] = 'MODEL_REFERENCE_UNAVAILABLE'
    if reference['records']:
        result['quality']['records'].extend(reference['records'])
        if result['quality']['status'] == 'UNKNOWN':
            result['quality']['status'] = 'SOURCED_REFERENCE'
        result['quality']['catalog_sha256'] = reference['catalog_sha256']
        result['quality']['catalog_record'] = reference['catalog_record']
    result['price_references'] = reference['price_references']
    from .provider_prices import is_reference, reference as reference_price, reference_note
    tariff = deployment.get('native_usage_tariff') or reference_price(profile)
    if tariff:
        from compute_plane.native_usage import UsageTariff
        from dataclasses import asdict
        result['price'] = {'status': 'REFERENCE_PRICE' if is_reference(tariff) else 'CONFIGURED_TARIFF',
                           **asdict(UsageTariff(**tariff)), **({'scope': reference_note(tariff)} if is_reference(tariff) else {})}

    def fetch(url, *, ttl=5, max_bytes=65536):
        if url not in cache:
            cache[url] = read_json_observation(url, ttl=ttl, max_bytes=max_bytes)
        return cache[url]

    if fleet_route(deployment):
        result['quota'] = {'status': 'NOT_APPLICABLE', 'reason': 'LOCAL_FLEET_NO_VENDOR_PLAN'}
        result['resource'] = {'status': 'UNKNOWN', 'reason': 'GPU_NOT_PROBED', 'immediate': False}
        if live:
            base = profile['endpoint'].rstrip('/')
            result['resource'] = gpu_availability(fetch(base + '/fleet/status'), profile['model'])
            cards = fetch(base + '/models', ttl=60)
            if cards['status'] == 'OBSERVED':
                rows = cards['value'].get('data', [])
                card = next((r for r in rows if isinstance(r, dict) and r.get('id') == profile['model']), None) if isinstance(rows, list) else None
                if card:
                    if result['quality']['status'] == 'UNKNOWN':
                        result['quality']['status'] = 'OWNER_CATALOG_ONLY'
                    result['quality']['owner_catalog'] = {
                        'source': cards['source'], 'observed_at': cards['observed_at'],
                        'status': 'OWNER_CATALOG_CLAIM',
                        **{k: card.get(k) for k in ('qualification', 'qualified_for', 'not_for', 'limits', 'quant', 'evidence')}}
    elif live and profile['endpoint'].rstrip('/') == 'https://openrouter.ai/api/v1':
        only = json.loads(profile.get('parameters_json') or '{}').get('provider', {}).get('only', [])
        from urllib.parse import quote
        address = (OPENROUTER+'/'+quote(profile['model'], safe='/')+'/endpoints') if only else OPENROUTER
        observation = fetch(address, ttl=3600, max_bytes=4*1024*1024)
        if observation['status'] == 'OBSERVED':
            data = observation['value'].get('data', {}) if only else observation['value'].get('data', [])
            rows = data.get('endpoints', []) if isinstance(data, dict) else data
            matches = [r for r in rows if isinstance(r, dict) and (r.get('tag') in only if only else r.get('id') == profile['model'])] if isinstance(rows, list) else []
            card = matches[0] if len(matches) == 1 else None
            if card and result['price']['status'] == 'UNKNOWN':
                result['price'] = advertised_price(card, observation)
                if only:
                    result['price'].update(endpoint=card['tag'], quantization=card.get('quantization', 'unknown'),
                        scope='Selected endpoint advertisement; not an invoice or measured cache hit.')
    return result


def admission_reason(facts, *, now=None):
    now = time.time() if now is None else now
    if facts['quota']['status'] == 'EXHAUSTED':
        return 'QUOTA_EXHAUSTED'
    resource = facts['resource']
    if resource.get('expires_at') is not None and now > resource['expires_at']:
        return 'GPU_STATUS_EXPIRED'
    if resource.get('immediate') is False:
        return resource.get('reason', 'GPU_STATUS_UNKNOWN')
    return None


def main():
    import argparse
    from .native_catalog import available_models, select
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('deployments', nargs='*')
    parser.add_argument('--live', action='store_true', help='Read status/catalog endpoints without loading models')
    args = parser.parse_args()
    names = args.deployments or [r['deployment'] for r in available_models() if r['deployment'] != 'qwen38']
    cache = {}; evidence = supplied_evidence()
    print(json.dumps({'authority': 'NONE', 'models': {name: collect(select(name), live=args.live,
          cache=cache, evidence=evidence) for name in names}}, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()

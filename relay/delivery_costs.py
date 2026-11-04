"""Value existing receipts with a bound price snapshot or explicit allocation."""
import copy
from datetime import date
from decimal import Decimal
import json
import math
from pathlib import Path

from compute_plane.choice_assessment import digest
from compute_plane.native_usage import UsageTariff, price_usage
from .native_agent import read_json


def load(path):
    if path is None:
        return None
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError('study.costs must be an absolute snapshot path.')
    value = read_json(Path(path), 1024*1024)
    if not isinstance(value, dict) or value.get('schema') != 'tare.tools/delivery-costs/1' or value.get('basis') != 'estimated_delivery_cost':
        raise ValueError('Expected delivery-costs/1 with estimated_delivery_cost basis.')
    if not isinstance(value.get('profiles'), dict):
        raise ValueError('Cost snapshot profiles must be a dictionary.')
    return value


def apply(entry, deployment, snapshot):
    if snapshot is None:
        return entry
    from .delivery_comparison import money
    result = copy.deepcopy(entry)
    quote = snapshot['profiles'].get(deployment['id'], {})
    if not isinstance(quote, dict):
        raise ValueError('Cost snapshot quote must be a dictionary.')
    if quote.get('profile_sha256') != digest(deployment):
        raise ValueError('Cost snapshot profile changed: '+deployment['id'])
    if quote.get('currency') != 'USD' or not quote.get('source') or not quote.get('observed_at'):
        raise ValueError('Cost snapshot requires USD, source and observation date.')
    valid_until = quote.get('valid_until')
    if valid_until is not None:
        # Validate supplied values even when they are falsey.
        if not isinstance(valid_until, str):
            raise ValueError('Cost snapshot valid_until must be an ISO date string or None.')
        if date.today() > date.fromisoformat(valid_until):
            raise ValueError('Cost snapshot expired: '+deployment['id'])
    report = result.get('usage')
    if report is None:
        return result
    # Validate every call before inspecting fields or allocating any cost.
    for call in report['calls']:
        if not isinstance(call, dict):
            raise ValueError('call record must be a dict')
    active = [c for c in report['calls'] if c.get('execution') != 'not_started']
    mode = quote['mode']
    if mode not in {'unknown', 'tokens', 'reported', 'client_estimate', 'hourly'}:
        raise ValueError('Unknown cost mode: '+str(mode))
    if mode == 'hourly' and quote.get('usd_per_hour') is not None:
        # One allocation per session, never one per overlapping stage reference.
        durations = []
        for attempt in entry['attempts']:
            # Validate every invocation before allocating any session cost.
            if not isinstance(attempt, dict):
                raise ValueError('Hourly allocation requires dictionary invocation records.')
            start, end = attempt.get('started_at'), attempt.get('ended_at')
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)) or end < start:
                raise ValueError('Hourly allocation requires completed finite invocation timestamps.')
            durations.append(Decimal(str(end)) - Decimal(str(start)))
        if not durations:
            raise ValueError('Hourly allocation requires recorded invocations.')
        amount = sum(durations) * money(quote['usd_per_hour']) / 3600
        # Keep generation counts intact; put the session allocation on its first call.
        for index, call in enumerate(active):
            call['cost'] = cost(str(amount if index == 0 else 0), quote['source'], 'allocated')
        if not active and amount:
            raise ValueError('Cannot allocate time to a session with no dispatched calls.')
        return result
    for call in active:
        previous = call.get('cost', {})
        mismatch = (call.get('observation') or {}).get('model_identity') == 'mismatch'
        if not mismatch and mode in {'tokens', 'reported'} and previous.get('basis') == 'provider_reported':
            continue
        call['cost'] = {'currency': None, 'known_amount': None, 'complete': False, 'source': quote['source']}
        if mismatch:
            continue
        estimate = call.get('provider_estimate')
        if mode == 'client_estimate' and estimate and estimate['currency'] == 'USD':
            call['cost'] = cost(str(money(estimate['amount'])), estimate['source'], 'client_estimate')
        elif mode in {'tokens', 'reported'} and quote.get('rates') and call.get('normalized'):
            selected = quote['rates']
            if 'long_context_above' in quote:
                tokens = call['normalized']['tokens']['input_tokens']
                if tokens is None:
                    continue
                if tokens > quote['long_context_above']:
                    selected = quote['long_rates']
            tariff = UsageTariff('USD', quote.get('price_source', quote['source']), quote['observed_at'], json.dumps(selected))
            call['cost'] = {**price_usage(call['normalized'], tariff), 'basis': 'catalog_estimate', 'source': tariff.source}
    return result


def cost(amount, source, basis):
    return {'currency': 'USD', 'known_amount': amount, 'complete': True,
            'source': source, 'basis': basis, 'unpriced_categories': [], 'tariff': None}

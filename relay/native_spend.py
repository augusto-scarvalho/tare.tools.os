"""Optional inference spend policy over native model ports and existing custody.

The host supplies exclusive custody and a journal. Reservations are predictions,
not provider billing guarantees; unknown charges retain their reservation.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json

from compute_plane.native_usage import BILLABLE, UsageContract, UsageTariff, normalize_usage, price_usage, reported_cost
from tare_tools_agent_runtime import ModelFailure


def total_amounts(values):
    values = list(values)
    return None if any(value is None for value in values) else sum((Decimal(value) for value in values), Decimal(0))


def outcome_commitment(outcome, currency='USD'):
    """A lifted cap never turns an unpriced child into a zero-cost child."""
    spend = outcome.get('spend') or {}
    if spend.get('currency') == currency:
        return spend.get('committed_amount')
    cost = outcome.get('cost') or {}
    if cost.get('unpriced_or_partial_calls') == 0:
        amounts = cost.get('known_by_currency', {})
        if set(amounts) <= {currency}:
            return str(amounts.get(currency, '0'))
    return None


@dataclass(frozen=True)
class SpendPolicy:
    max_amount: str
    currency: str
    input_margin_tokens: int = 128
    allow_estimated_counts: bool = False

    def __post_init__(self):
        try:
            amount = Decimal(self.max_amount) if isinstance(self.max_amount, str) else None
        except InvalidOperation:
            amount = None
        if (not isinstance(self.max_amount, str) or len(self.max_amount) > 64
                or amount is None or not amount.is_finite() or amount < 0):
            raise ValueError('spend_limit_requires_nonnegative_decimal_string')
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError('spend_currency_required')
        if type(self.input_margin_tokens) is not int or self.input_margin_tokens < 0:
            raise ValueError('spend_margin_requires_nonnegative_integer')
        if type(self.allow_estimated_counts) is not bool:
            raise ValueError('spend_estimate_opt_in_requires_boolean')


def settings(deployment, *, configured=False):
    tariff = UsageTariff(**deployment['native_usage_tariff']) if deployment.get('native_usage_tariff') is not None else None
    policy = SpendPolicy(**deployment['native_spend_policy']) if deployment.get('native_spend_policy') is not None else None
    if policy and (tariff is None or policy.currency != tariff.currency
                   or deployment.get('native_usage_contract') is None):
        raise ValueError('spend_limit_requires_matching_tariff_and_usage_contract')
    from .execution_resources import spend_capped
    return (policy if configured or spend_capped() else None), tariff


class SpendModel:
    """Shared by text, tool turns and summaries. No retry or model selection."""
    def __init__(self, adapter, *, policy, tariff, contract, journal, external_committed=None):
        self.adapter, self.policy, self.tariff, self.contract = adapter, policy, tariff, contract
        self.journal, self.last_count = journal, None
        self.external_committed = external_committed or (lambda: Decimal(0))
        header = {'kind': 'spend_policy', 'schema': 'tare.tools/native-spend/1',
                  'model_profile': asdict(adapter.profile), 'counter_protocol': adapter.counter_protocol,
                  'policy': asdict(policy),
                  'tariff': asdict(tariff), 'contract': asdict(contract)}
        records = journal.snapshot().records
        if records:
            if records[0].event() != header:
                raise ValueError('native_spend_identity_changed')
        else:
            journal.append(header)
        self.report()  # Validate a resumed ledger before any provider access.

    def count(self, request, control):
        self.last_count = self.adapter.count(request, control)
        return self.last_count

    def report(self):
        from .execution_resources import spend_capped
        capped = spend_capped()
        reservations, settled, denied = {}, {}, []
        for record in self.journal.snapshot().records[1:]:
            event = record.event(); call = event['call_id']
            if event['kind'] == 'spend_reserved' and call not in reservations:
                reservations[call] = event
            elif event['kind'] == 'spend_settled' and call in reservations and call not in settled:
                settled[call] = event
            elif event['kind'] == 'spend_denied':
                denied.append(event)
            else:
                raise ValueError('invalid_native_spend_transition')
        committed = Decimal(0); known = Decimal(0); uncertain = 0; overruns = []; unbounded_unknown = False
        for call, reserved in reservations.items():
            reservation = Decimal(reserved['amount']) if reserved['amount'] is not None else None
            price = settled.get(call, {}).get('cost')
            actual = Decimal(price['known_amount']) if price else Decimal(0)
            complete = price is not None and price['complete']
            known += actual
            committed += actual if complete or reservation is None else max(reservation, actual)
            unbounded_unknown |= not complete and reservation is None
            uncertain += not complete
            if reservation is not None and actual > reservation:
                overruns.append(call)
        external = self.external_committed()
        if external is not None:
            committed += external
        return {'schema': 'tare.tools/native-spend-report/1', 'currency': self.policy.currency,
            'auxiliary_committed_amount': str(external) if external is not None else None,
            'max_amount': self.policy.max_amount if capped else None, 'known_amount': str(known),
            'committed_amount': str(committed) if external is not None and not unbounded_unknown else None,
            'remaining_amount': (str(max(Decimal(0), Decimal(self.policy.max_amount)-committed)) if external is not None and not unbounded_unknown else '0') if capped else None,
            **({'unbounded_uncertain_cost': True} if unbounded_unknown else {}),
            **({'uncertain_auxiliary_cost': True} if external is None else {}),
            **({'configured_max_amount': self.policy.max_amount, 'limit_lifted': True} if not capped else {}),
            'uncertain_calls': uncertain, 'reserved_calls': len(reservations),
            'denied_calls': denied, 'reservation_overruns': overruns,
            'admission_basis': ('uncapped_accounting' if not capped else
                                'prediction_with_estimates' if self.policy.allow_estimated_counts else 'measured_count_plus_margin'),
            'scope': 'model token charges only; excludes counting fees, tools, infrastructure and provider billing adjustments'}

    def _deny(self, request, reason):
        self.journal.append({'kind': 'spend_denied', 'call_id': request.call_id, 'reason': reason})
        raise ModelFailure(reason, not_started=True)

    def generate(self, request, control):
        if control.should_stop():
            return self._deny(request, 'SPEND_CONTROL_STOPPED')
        count = self.last_count or {}
        if (count.get('call_id') != request.call_id or count.get('context_sha256') != request.context.sha256
                or count.get('request_purpose') != request.purpose
                or not request.admitted_request_sha256 or count.get('request_sha256') != request.admitted_request_sha256
                or count.get('profile_digest') != self.adapter.profile_digest
                or count.get('output_tokens') != self.adapter.profile.output_tokens
                or count.get('context_window') != self.adapter.profile.context_window):
            return self._deny(request, 'SPEND_COUNT_IDENTITY_MISMATCH')
        measured = count.get('status') == 'observed'
        from .execution_resources import spend_capped
        capped = spend_capped()
        report = self.report()
        if capped and report.get('uncertain_auxiliary_cost'):
            return self._deny(request, 'SPEND_AUXILIARY_COST_UNKNOWN')
        if capped and report.get('unbounded_uncertain_cost'):
            return self._deny(request, 'SPEND_UNRECONCILED_UNBOUNDED_CALL')
        estimated = count.get('status') == 'estimated' and (self.policy.allow_estimated_counts or not capped)
        size = count.get('input_tokens') if measured else count.get('estimated_input_tokens') if estimated else None
        if capped and (type(size) is not int or size < 0):
            return self._deny(request, 'SPEND_COUNT_UNAVAILABLE')
        rates = json.loads(self.tariff.rates_json)
        input_fields = BILLABLE[:3] if self.contract.layout in {
            'converse-exclusive', 'anthropic-exclusive', 'openrouter-inclusive', 'openrouter-responses-inclusive', 'responses-inclusive'
        } else BILLABLE[:2]
        priced = all(rates[k] is not None for k in (*input_fields, 'output_tokens'))
        if capped and not priced:
            return self._deny(request, 'SPEND_TARIFF_INCOMPLETE')
        # Assume no cache discount; cache creation can cost MORE than fresh input.
        amount = (((size + self.policy.input_margin_tokens) * max(Decimal(rates[k]) for k in input_fields)
                  + self.adapter.profile.output_tokens * Decimal(rates['output_tokens'])) / Decimal(1000000)
                  if priced and type(size) is int and size >= 0 else None)
        if spend_capped() and report['reservation_overruns']:
            return self._deny(request, 'SPEND_RESERVATION_OVERRUN')
        if spend_capped() and Decimal(report['committed_amount']) + amount > Decimal(self.policy.max_amount):
            return self._deny(request, 'SPEND_LIMIT')
        if any(r.event().get('kind') == 'spend_reserved' and r.event().get('call_id') == request.call_id
               for r in self.journal.snapshot().records):
            return self._deny(request, 'SPEND_CALL_ALREADY_RESERVED')
        # This append must succeed BEFORE inference. Interrupted calls keep this
        # reservation; resumption cannot forget them or recreate the budget.
        self.journal.append({'kind': 'spend_reserved', 'call_id': request.call_id, 'purpose': request.purpose,
            'profile_digest': self.adapter.profile_digest,
            'request_sha256': request.admitted_request_sha256, 'amount': str(amount) if amount is not None else None,
            'input_basis': count['status'], 'input_size': size, 'output_reserve': self.adapter.profile.output_tokens})
        try:
            reply = self.adapter.generate(request, control)
        except ModelFailure as exc:
            if exc.not_started:
                self.journal.append({'kind': 'spend_settled', 'call_id': request.call_id,
                    'cost': {'known_amount': '0', 'complete': True}, 'basis': 'dispatch_not_started'})
            raise
        usage = json.loads(reply.metadata_json).get('usage')
        cost = reported_cost(usage, self.contract) or price_usage(normalize_usage(usage, self.contract), self.tariff)
        self.journal.append({'kind': 'spend_settled', 'call_id': request.call_id, 'cost': cost, 'basis': 'provider_usage'})
        return reply


def compose(adapter, deployment, journal, *, external_committed=None):
    policy, tariff = settings(deployment, configured=True)
    return (SpendModel(adapter, policy=policy, tariff=tariff,
                       contract=UsageContract(**deployment['native_usage_contract']), journal=journal,
                       external_committed=external_committed)
            if policy else adapter)

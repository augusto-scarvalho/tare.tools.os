"""The ledger keeps each call's tokens and how its dollars were known; `tare quota` shows tare's own use per API account."""
import json

import pytest

from relay import provider_prices, provider_quota, provider_spend, turn_engine
from test_provider_quota import NOW, providers  # noqa: F401  (the readings fixture)

DAY = 86400
API = {'id': 'api', 'native_credential_env': 'OPENAI_API_KEY',
       'native_profile': {'endpoint': 'https://api.openai.com', 'model': 'gpt-6', 'protocol': 'openai-responses'}}
REFERENCE = {'source': provider_prices.REFERENCE + ' (openai)', 'effective_at': '2026-11-10'}


def call(usd=None, tokens=(1000, 200, 50, 10), tariff=None, basis=None, normalized=True, id='c'):
    found = {'call_id': id, 'cost': {'currency': 'USD', 'known_amount': usd, **({'tariff': tariff} if tariff else {}),
                                    **({'basis': basis} if basis else {})}}
    if normalized:
        found['normalized'] = {'tokens': dict(zip(('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'),
                                                  tokens))}
    return found


def rows():
    path = provider_spend.folder()/'provider-spend.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    return tmp_path


@pytest.mark.parametrize('kwargs,basis', [({'basis': 'provider_reported'}, 'charged'), ({}, 'tariff'),
                                          ({'tariff': REFERENCE}, 'reference')])
def test_a_priced_call_keeps_its_tokens_and_how_its_dollars_were_known(storage, kwargs, basis):
    provider_spend.declare('openai-api', 10, now=NOW - DAY)
    assert provider_spend.record(API, [call(0.5, **kwargs)], NOW) == 1
    row, = rows()
    assert row['usd'] == 0.5 and row['basis'] == basis
    assert row['tokens'] == {'in': 1000, 'out': 200, 'cache_read': 50, 'cache_write': 10}
    assert ('reference' in row, row.get('price_source')) == (basis == 'reference', REFERENCE['source'] if basis == 'reference' else None)
    assert provider_spend.spent('openai-api', 0) == 0.5 and provider_spend.remaining('openai-api', NOW)['remaining'] == 9.5


def test_only_the_token_counts_that_are_ints_are_kept(storage):
    provider_spend.record(API, [call(0.5, tokens=(7, None, 'x', 3))], NOW)
    assert rows()[0]['tokens'] == {'in': 7, 'cache_write': 3}


def test_a_call_with_tokens_and_no_dollars_leaves_an_unpriced_row_that_moves_no_balance(storage):
    provider_spend.declare('openai-api', 10, now=NOW - DAY)
    assert provider_spend.record(API, [call(None, tokens=(1000, 0, None, None)), call(0, tokens=(0, 5, None, None))], NOW) == 2
    assert [(r['usd'], r['unpriced'], r['tokens']) for r in rows()] == [(0, True, {'in': 1000, 'out': 0}),
                                                                       (0, True, {'out': 5, 'in': 0})]
    assert provider_spend.spent('openai-api', 0) == 0 and provider_spend.remaining('openai-api', NOW)['remaining'] == 10


def test_a_call_the_provider_charged_nothing_for_is_valued_at_zero_not_unpriced(storage):
    """A free model on OpenRouter: the provider said what it cost, and it cost nothing."""
    assert provider_spend.record(API, [call(0, basis='provider_reported')], NOW) == 1
    row, = rows()
    assert (row['usd'], row['basis'], 'unpriced' in row) == (0, 'charged', False)
    assert provider_spend.own_use('openai-api', NOW)['30d'] == {'usd': 0.0, 'calls': 1, 'tokens': 1200, 'unpriced': 0, 'basis': []}


def test_a_call_with_neither_dollars_nor_tokens_leaves_nothing(storage):
    assert provider_spend.record(API, [call(None, tokens=(0, 0, 9, None)), call(None, tokens=(None,) * 4), call(None, normalized=False)],
                                 NOW) == 0
    assert rows() == []


def test_a_call_without_normalized_leaves_the_row_it_left_before(storage):
    assert provider_spend.record(API, [call(0.25, normalized=False)], NOW) == 1
    row, = rows()
    assert 'tokens' not in row and row['usd'] == 0.25 and row['basis'] == 'tariff'
    assert provider_spend.record(API, [call(None, normalized=False)], NOW) == 0


def write(*lines):
    path = provider_spend.folder()/'provider-spend.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def test_own_use_counts_the_last_7_and_30_days_and_skips_what_is_not_a_call(storage):
    base = {'deployment': 'api', 'account': 'openai-api'}
    write(json.dumps({**base, 'at': NOW - DAY, 'usd': 1.0, 'basis': 'charged', 'tokens': {'in': 900, 'out': 100, 'cache_read': 5}}),
          json.dumps({**base, 'at': NOW - 10*DAY, 'usd': 2.0, 'basis': 'tariff', 'tokens': {'in': 10, 'out': 20}}),
          json.dumps({**base, 'at': NOW - 40*DAY, 'usd': 9.0, 'basis': 'tariff'}),  # older than 30 days
          json.dumps({**base, 'at': NOW - DAY, 'usd': 0, 'unpriced': True, 'tokens': {'in': 7, 'out': 3}}),
          json.dumps({**base, 'account': 'anthropic-api', 'at': NOW - DAY, 'usd': 5.0}),  # another account
          json.dumps({'account': 'openai-api', 'at': NOW - DAY, 'allowance': 1}),  # no dollars
          '{"account": "openai-api", "at": ')  # a torn line
    use = provider_spend.own_use('openai-api', NOW)
    assert use['7d'] == {'usd': 1.0, 'calls': 2, 'tokens': 1010, 'unpriced': 1, 'basis': ['charged']}
    assert use['30d'] == {'usd': 3.0, 'calls': 3, 'tokens': 1040, 'unpriced': 1, 'basis': ['charged', 'tariff']}
    assert provider_spend.own_use('gemini-api', NOW) is None
    assert provider_spend.own_use('openai-api', NOW + 60*DAY) is None  # nothing inside 30 days of then


def test_rows_from_before_this_change_count_in_dollars_and_calls(storage):
    base = {'deployment': 'api', 'account': 'openai-api', 'at': NOW - DAY}
    write(json.dumps({**base, 'usd': 1.5}), json.dumps({**base, 'usd': 0.5, 'reference': True, 'price_source': 'x'}))
    assert provider_spend.own_use('openai-api', NOW)['7d'] == {'usd': 2.0, 'calls': 2, 'tokens': None, 'unpriced': 0,
                                                               'basis': ['reference']}
    write(json.dumps({**base, 'usd': 1.5}))
    assert provider_spend.own_use('openai-api', NOW)['30d']['basis'] == []


def use(basis, tokens=True, unpriced=0):
    span = {'usd': 1.2, 'calls': 42, 'tokens': 310_000 if tokens else None, 'unpriced': 0, 'basis': basis}
    return {'7d': span, '30d': {**span, 'usd': 4.8, 'calls': 160, 'tokens': 1_200_000 if tokens else None,
                                'unpriced': unpriced}}


@pytest.mark.parametrize('basis,ending', [
    (['charged'], ' · charged by the provider'), (['tariff'], " · valued at the provider's tariff"),
    (['reference'], ' · valued at models.dev reference prices, not a confirmed tariff'),
    (['charged', 'reference'], ' · valued partly at a tariff or a reference price'), ([], '')])
def test_the_line_says_how_the_dollars_were_known(basis, ending):
    assert provider_quota.own_use_line(use(basis)) == (
        "tare's own use: 7 days US$ 1.20 in 42 calls (310k tokens); 30 days US$ 4.80 in 160 calls (1.2M tokens)" + ending)
    assert '(' not in provider_quota.own_use_line(use(basis, tokens=False))


def test_the_line_counts_what_it_could_not_value_and_formats_small_token_counts():
    line = provider_quota.own_use_line(use(['tariff'], unpriced=3))
    assert line.endswith(" · valued at the provider's tariff · 3 calls tare could not value: their tokens are counted, "
                         'their cost is not, so a stated balance counts down too slowly')
    small = use(['charged'])
    small['7d'].update(tokens=950, calls=1)
    assert '1 call (950 tokens)' in provider_quota.own_use_line(small)


def test_quota_shows_the_line_after_the_refused_call_note_and_json_carries_own_use(providers, capsys):
    provider_spend.record(API, [call(0.5, basis='provider_reported', id='a')], NOW - DAY)
    turn_engine.note_usage_limit(API, resets_at=NOW + 3600, now=NOW)
    report = provider_quota.refresh(now=NOW)
    openai = next(a for a in report['accounts'] if a['account'] == 'openai-api')
    assert openai['notes'][0].startswith('A refused call left this account out') and len(openai['notes']) == 2
    assert openai['notes'][1].startswith("tare's own use: 7 days US$ 0.50 in 1 call (1k tokens);")
    assert openai['own_use']['30d']['basis'] == ['charged']
    assert "tare's own use: 7 days US$ 0.50" in provider_quota.render(report, now=NOW)
    assert all('own_use' not in a for a in report['accounts'] if a['account'] != 'openai-api')  # no row, no line
    assert provider_quota.main(['--json']) == 0
    shown = json.loads(capsys.readouterr().out)
    assert next(a for a in shown['accounts'] if a['account'] == 'openai-api')['own_use']['30d']['calls'] == 1


OPENROUTER = {'id': 'or', 'native_credential_env': 'OPENROUTER_API_KEY', 'native_usage_contract': {
    'layout': 'openrouter-inclusive', 'source': 'test'}, 'native_profile': {'endpoint': 'https://openrouter.ai/api', 'model': 'x/y',
                                                                         'protocol': 'openai-chat'}}


def test_a_direct_call_outside_a_session_is_recorded_with_what_the_provider_charged(storage):
    """A setup proof, an explorer's or a researcher's side call: they spent credit and left no row (found setting
    OpenRouter's own count beside the ledger, 2026-11-17)."""
    usage = {'prompt_tokens': 900, 'completion_tokens': 100, 'total_tokens': 1000, 'cost': 0.004}
    unpriced = {'currency': None, 'known_amount': None, 'complete': False}  # generate_text had no tariff for the route
    assert provider_spend.record_receipt(OPENROUTER, {'attempt_id': 'a1', 'usage': usage, 'cost': unpriced}) == 1
    row, = rows()
    assert (row['account'], row['usd'], row['basis'], row['call'], row['tokens']['in'], row['tokens']['out']) == (
        'openrouter', 0.004, 'charged', 'a1', 900, 100)
    # A provider that returns no cost: the receipt's own tariff value.
    api_usage = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
    assert provider_spend.record_receipt(API, {'attempt_id': 'a2', 'usage': api_usage,
                                               'cost': {'currency': 'USD', 'known_amount': '0.5'}}) == 1
    assert (rows()[1]['account'], rows()[1]['usd'], rows()[1]['basis']) == ('openai-api', 0.5, 'tariff')
    # A call that was never sent, a route with no API account and a broken receipt add nothing and never raise.
    assert provider_spend.record_receipt(API, {'attempt_id': 'a3', 'usage': None, 'cost': {'currency': 'USD', 'known_amount': '0'}}) == 0
    assert provider_spend.record_receipt({'id': 'cli', 'native_profile': {'endpoint': 'cli://claude'}}, {'usage': usage}) == 0
    assert provider_spend.record_receipt(API, None) == 0 and len(rows()) == 2


def test_a_setup_proof_posts_its_call(storage, monkeypatch):
    from relay import environment
    usage = {'prompt_tokens': 30, 'completion_tokens': 4, 'total_tokens': 34, 'cost': 0.0002}
    monkeypatch.setattr(environment, 'route_for', lambda name: (OPENROUTER, None))
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'a cent')
    monkeypatch.setattr(environment, 'role_check', lambda deployment: None)
    monkeypatch.setattr(environment, 'remember', lambda name, record: record)
    monkeypatch.setattr('relay.native_generation.generate_text',
                        lambda *a, **kw: (True, 'ok', {'attempt_id': 'p1', 'usage': usage, 'cost': {'known_amount': None}}))
    assert environment.prove('or', lambda line: True)['ok'] is True
    assert [(r['account'], r['usd'], r['call']) for r in rows()] == [('openrouter', 0.0002, 'p1')]

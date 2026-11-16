"""What 1% of a plan bought: the estimator's robustness first, then its history, the equivalent rows and the lines."""
import json
import random
from datetime import datetime

import pytest

from relay import plan_consumption as pc, provider_quota, provider_spend

NOW = datetime(2026, 11, 16, 12, 0).timestamp()
HOUR, RATE, RESETS = 3600, 0.0002, NOW + 3 * 86400  # 1% of the window is worth US$ 50


def series(intervals, outside=(), step=0.01, rate=RATE, resets=RESETS):
    """Readings every hour of a meter that counts `rate` per dollar plus the outside use of each interval (fractions),
    shown in `step`s; and tare's calls, one per interval ((dollars, ...))."""
    start, true, points, uses = NOW - (len(intervals) + 1) * HOUR, 0.0, [], []
    shown = lambda used: round(round(used / step) * step, 6)
    points.append((start, 0.0, resets))
    for k, usd in enumerate(intervals):
        true += rate * usd + (outside[k] if k < len(outside) else 0)
        uses.append((start + k * HOUR + HOUR / 2, usd))
        points.append((start + (k + 1) * HOUR, shown(true), resets))
    return points, uses


def value(points, uses, step=0.01, now=NOW):
    return pc.estimate(points, uses, step, now)


def test_clean_spans_give_the_true_rate_within_the_resolution():
    got = value(*series([100] * 5))  # 5 x US$ 100, 2% each: the smallest step seen is 2%, so that is the resolution
    bound = 0.01 / got['usd_per_percent']  # the rate's upper bound, per dollar
    assert got['state'] == 'firm' and got['covered_usd'] == 500
    assert RATE <= bound <= RATE + 0.02 / 500 + 1e-6  # never below the truth, above it by at most resolution / U
    assert 45 <= value(*series([100, 130, 90, 110, 120]))['usd_per_percent'] <= 50  # one-step readings seen: 1%


def test_a_session_that_began_before_a_span_is_left_out_of_it():
    """A session's calls are recorded when it ends. One that began before the span's first reading may have moved the
    meter before it: counting its dollars would make the plan look better than it is, so it is left out."""
    points, uses = series([100] * 4)
    clean = value(points, uses)
    start = points[1][0]
    # US$ 900 recorded inside the span from points[1] on, by a session that began an hour before that reading
    late = [(start - HOUR, start + HOUR / 4, 900.0), *uses]
    got = value(points[1:], late)
    assert got['usd_per_percent'] <= clean['usd_per_percent'] + 1e-9  # never a better plan than the clean series shows
    assert got['covered_usd'] <= 300  # the 900 is in no span of these points
    # the same dollars from a session that began inside the span do count
    inside = [(start + 1, start + HOUR / 4, 900.0), *uses]
    assert value(points[1:], inside)['covered_usd'] > 900


def test_a_call_counts_only_with_a_start_its_caller_knows(priced, tmp_path, monkeypatch):
    """The start is the invocation's own (native_agent passes it), never a file's time: on Unix a folder's ctime moves
    when a file is saved in it, which would date old calls as new. A call with no known start counts in no span."""
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    session = tmp_path/'session'
    session.mkdir()
    usage = {'calls': [call((1_000_000, 0, 0, 0))]}
    assert provider_spend.post(DEPLOYMENT, usage, session, now=NOW - 600, since=NOW - 900) == 1
    usage['calls'].append(call((1_000_000, 0, 0, 0)))
    assert provider_spend.post(DEPLOYMENT, usage, session, now=NOW - 300) == 1  # a caller that knows no start
    first, second = provider_spend.equivalents('claude', 0)
    assert first[:2] == (NOW - 900, NOW - 600) and second[:2] == (None, NOW - 300)
    pc.history_path().parent.mkdir(parents=True, exist_ok=True)
    pc.history_path().write_text(json.dumps([{'at': t, 'account': 'claude', 'bucket': 'week', 'remaining': r, 'resets_at': NOW + 86400}
                                             for t, r in ((NOW - 1000, 0.9), (NOW - 100, 0.8))]))
    week = {'id': 'week', 'scope': None}
    pc.annotate([{'account': 'claude', 'kind': 'subscription', 'status': 'OBSERVED', 'buckets': [week]}], NOW)
    assert week['consumption']['covered_usd'] == 8  # only the call whose start is known


def test_a_call_made_while_a_reading_was_taken_counts_in_neither_span_edge():
    """A reading takes seconds. A call that began before the first reading ended may already be in it; one recorded
    after the second began may not be in it yet. Neither counts."""
    resets = NOW + 86400
    points = [(NOW - 3600, 0.10, resets, NOW - 3590), (NOW - 60, 0.20, resets, NOW - 50)]
    inside = (NOW - 3000, NOW - 2000, 100.0)
    assert pc.estimate(points, [inside], 0.01, NOW)['covered_usd'] == 100
    during_first = (NOW - 3595, NOW - 2000, 500.0)   # began while the first reading was being taken
    during_second = (NOW - 3000, NOW - 55, 500.0)    # recorded while the second was being taken
    assert pc.estimate(points, [inside, during_first, during_second], 0.01, NOW)['covered_usd'] == 100


@pytest.mark.parametrize('outside', [0.01, 0.1, 0.4])
def test_outside_use_on_other_spans_does_not_move_the_estimate_while_a_clean_span_remains(outside):
    pick = lambda got: {k: got[k] for k in ('state', 'usd_per_percent', 'covered_usd')}
    clean_only = pick(value(*series([50])))  # one clean span, one step long
    points, uses = series([50, 6], outside=[0, outside])  # then a small dirty one: the meter moves by whatever it likes
    assert pick(value(points, uses)) == clean_only and clean_only['usd_per_percent'] == 25.0


def test_outside_use_on_every_span_never_gives_a_lower_rate_than_the_truth():
    rng = random.Random(7)
    for _ in range(300):
        intervals = [rng.uniform(5, 80) for _ in range(rng.randint(2, 12))]
        outside = [rng.uniform(0, 0.03) if rng.random() < 0.8 else 0 for _ in intervals]
        got = value(*series(intervals, outside))
        assert got['state'] == 'none' or got['usd_per_percent'] <= 50 + 0.01  # a value never above the true US$ 50


def test_a_whole_percent_meter_with_many_zero_steps_converges_over_a_long_span():
    points, uses = series([10] * 100)  # 0.2% an interval: most steps read zero
    assert sum(p[1] == q[1] for p, q in zip(points, points[1:])) > 70
    got = value(points, uses)
    assert got['state'] == 'firm' and 45 <= got['usd_per_percent'] <= 50 and got['covered_usd'] >= 900


def test_a_window_reset_is_never_crossed():
    points, uses = series([100] * 4)
    points[3] = (points[3][0], 0.0, RESETS + 5 * HOUR)  # a new window from here on: the meter starts again
    points[4] = (points[4][0], 0.02, RESETS + 5 * HOUR)
    assert value(points, uses[:3]) ['state'] != 'none'  # the spans before it stand
    # a span from before to after the reset would read a negative or short delta: no bound is taken across it
    assert value([points[1], points[4]], uses) == {'state': 'none'}


def test_a_reading_at_the_limit_gives_no_bound():
    points, uses = series([200], outside=[0.96])  # the meter reads 100%: it stopped counting
    assert points[-1][1] == 1 and value(points, uses) == {'state': 'none'}


def test_spans_older_than_seven_days_are_ignored_and_a_changed_quota_moves_the_estimate_once_old_spans_age_out():
    old, uses_old = series([200] * 2, rate=RATE)  # 1% = US$ 50 ...
    new, uses_new = series([200] * 2, rate=RATE * 2)  # ... then the vendor halves the quota: 1% = US$ 25
    by = -7 * 86400 - 2 * HOUR  # the old readings end 3 h before they leave the window of NOW
    shift = lambda rows: [(t + by, *rest) for t, *rest in rows]
    points, uses = shift(old) + new, shift(uses_old) + uses_new
    before = value(points, uses, now=NOW - 7 * 86400)  # the old spans are inside, the new ones not yet read
    assert 30 <= before['usd_per_percent'] <= 50
    after = value(points, uses)  # the old ones aged out
    assert 15 <= after['usd_per_percent'] <= 25 and after['measured_from'] >= NOW - 7 * 86400
    assert value(shift(old), shift(uses_old)) == {'state': 'none'}  # nothing newer than 7 days


def test_the_three_states_at_their_thresholds():
    assert value(*series([pc.U_FLOOR - 0.01])) == {'state': 'none'}
    just = value(*series([pc.U_FLOOR]))
    assert just['state'] == 'weak' and just['covered_usd'] == pc.U_FLOOR
    # firm needs the best span to reach U_FIRM and the two halves to reproduce it
    assert value(*series([pc.U_FIRM / 2 - 0.01] * 2))['state'] == 'weak'  # one dollar short
    assert value(*series([pc.U_FIRM / 2] * 2))['state'] == 'weak'  # at 50 each half is a single step: a wide bound
    assert value(*series([100] * 4))['state'] == 'firm'
    # the halves disagree beyond the tolerance (outside use in one half): weak, though still a bound
    points, uses = series([100] * 4, outside=[0, 0, 0.03, 0.03])
    assert value(points, uses)['state'] == 'weak'


def test_the_resolution_is_the_smallest_nonzero_step_seen_else_the_sources_default():
    t = NOW - 3 * HOUR
    uses = [(t + 1.5 * HOUR, 100)]  # all of tare's use is in the last interval
    coarse = [(t, 0.0, RESETS), (t + HOUR, 0.0, RESETS), (t + 2 * HOUR, 0.02, RESETS)]  # the only step seen is 0.02
    fine = [(t, 0.0, RESETS), (t + HOUR, 0.0003, RESETS), (t + 2 * HOUR, 0.0203, RESETS)]  # a step of 0.0003 is seen
    assert value(coarse, uses)['usd_per_percent'] == round(0.01 / ((0.02 + 0.02) / 100), 2)
    assert value(fine, uses)['usd_per_percent'] == round(0.01 / ((0.02 + 0.0003) / 100), 2)
    flat = [(t, 0.0, RESETS), (t + 2 * HOUR, 0.0, RESETS)]  # no step at all: the source's default decides
    assert value(flat, uses, step=0.01)['usd_per_percent'] == round(0.01 / (0.01 / 100), 2)


def test_a_scoped_bucket_counts_only_its_models(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    points, uses = series([100] * 4)
    pc.history_path().parent.mkdir(parents=True)
    pc.history_path().write_text(json.dumps([{'at': t, 'account': 'claude', 'bucket': b, 'remaining': round(1 - used, 4),
                                              'resets_at': r} for t, used, r in points for b in ('week', 'week:fable')]))
    ledger = provider_spend.folder()/'provider-spend.jsonl'
    ledger.write_text('\n'.join(json.dumps({'at': t, 'since': t, 'account': 'claude', 'equivalent': usd, 'model': model})
                                for t, usd in uses for model in ('claude-fable-5-2', 'claude-opus-5-5')) + '\n')
    week = {'id': 'week', 'scope': None}
    fable = {'id': 'week:fable', 'scope': 'fable'}
    accounts = [{'account': 'claude', 'kind': 'subscription', 'status': 'OBSERVED', 'buckets': [week, fable]}]
    pc.annotate(accounts, NOW)
    # the general window sees both models' calls (US$ 800 for the same meter movement), the Fable window only one's
    assert week['consumption']['covered_usd'] == 800 and fable['consumption']['covered_usd'] == 400
    assert week['consumption']['usd_per_percent'] > fable['consumption']['usd_per_percent']


def test_the_history_is_appended_pruned_and_a_failed_reading_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    bucket = {'id': 'week', 'remaining': 0.5, 'resets_at': 123.0}
    ok = {'account': 'claude', 'kind': 'subscription', 'status': 'OBSERVED', 'buckets': [bucket]}
    failed = {'account': 'codex', 'kind': 'subscription', 'status': 'UNAVAILABLE', 'reason': 'x'}
    api = {'account': 'openai-api', 'kind': 'credit', 'status': 'DECLARED', 'credit': {}}
    pc.keep([failed, api], NOW)
    assert not pc.history_path().exists()
    pc.keep([ok, failed], NOW - 8 * 86400)
    pc.keep([ok, failed], NOW - 86400)
    pc.keep([ok], NOW)
    assert [r['at'] for r in pc.history()] == [NOW - 86400, NOW]  # the 8-day-old reading was pruned
    assert pc.history()[-1] == {'at': NOW, 'done': NOW, 'account': 'claude', 'bucket': 'week', 'remaining': 0.5,
                                'resets_at': 123.0}


DEPLOYMENT = {'id': 'claude-fable', 'native_profile': {'endpoint': 'cli://claude', 'protocol': 'vendor-cli',
                                                       'model': 'claude-fable-5-2'}}


def call(tokens, cid='c'):
    return {'call_id': cid, 'cost': {'currency': None, 'known_amount': None},
            'normalized': {'tokens': dict(zip(('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens'), tokens))}}


@pytest.fixture
def priced(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    rates = {'uncached_input_tokens': '8', 'output_tokens': '40', 'cache_read_tokens': '0.2', 'cache_write_tokens': '10'}
    seen = []

    def reference(profile):
        seen.append(profile['endpoint'])
        return ({'currency': 'USD', 'source': 'models.dev reference price (not a confirmed tariff) for x', 'effective_at': '2026-11-14',
                 'rates_json': json.dumps(rates)} if profile['model'] == 'claude-fable-5-2' else None)
    monkeypatch.setattr('relay.provider_prices.reference', reference)
    return seen


def test_a_subscription_call_is_recorded_as_an_equivalent_at_the_vendor_apis_reference_price(priced):
    assert provider_spend.record(DEPLOYMENT, [call((1_000_000, 10_000_000, 100_000, 50_000))], now=NOW) == 1
    row = json.loads((provider_spend.folder()/'provider-spend.jsonl').read_text().splitlines()[0])
    # 1M x 8 + 10M x 0.2 + 0.1M x 10 + 0.05M x 40 = 8 + 2 + 1 + 2
    assert row['equivalent'] == 13 and row['account'] == 'claude' and row['model'] == 'claude-fable-5-2' and 'usd' not in row
    assert priced == ['https://api.anthropic.com'] and provider_spend.equivalents('claude', 0) == [(None, NOW, 13.0, 'claude-fable-5-2')]


def test_a_model_without_a_reference_price_or_without_token_counts_is_not_recorded(priced):
    other = {'id': 'x', 'native_profile': {**DEPLOYMENT['native_profile'], 'model': 'claude-unlisted'}}
    assert provider_spend.record(other, [call((1_000_000, 0, 0, 1_000_000))], now=NOW) == 0
    assert provider_spend.record(DEPLOYMENT, [{'call_id': 'u', 'cost': {}, 'normalized': {'tokens': dict.fromkeys(
        ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens'))}}, {'call_id': 'n'}], now=NOW) == 0
    assert provider_spend.equivalents('claude', 0) == []


def test_an_equivalent_is_never_money_spent_in_spend_a_declared_balance_or_a_cap(priced):
    from test_native_spend import guard
    from relay.native_spend import SpendPolicy
    provider_spend.declare('anthropic-api', 10, now=NOW - 5)
    model = guard(policy=SpendPolicy('1', 'USD'))
    before = (provider_spend.remaining('anthropic-api', NOW), model.report())
    assert provider_spend.record(DEPLOYMENT, [call((1_000_000, 0, 0, 1_000_000))], now=NOW) == 1  # US$ 48 of equivalent
    for name in ('claude', 'anthropic-api', 'codex', 'opencode-sub'):
        assert provider_spend.spent(name, 0) == 0 and provider_spend.spent(name, 0, reference=True) == 0
    assert provider_spend.last_plan('claude') is None
    assert (provider_spend.remaining('anthropic-api', NOW), model.report()) == before  # the balance and the cap's committed amount
    assert provider_spend.equivalents('claude', 0)[0][2] == 48.0


def test_the_equivalent_reaches_a_cap_nowhere_the_reservation_reads_only_its_journal():
    import inspect
    from relay import native_spend
    assert 'provider_spend' not in inspect.getsource(native_spend)  # a cap reserves against the session journal alone


def test_a_ledger_api_account_is_unchanged(priced):
    api = {'id': 'a', 'native_credential_env': 'ANTHROPIC_API_KEY', 'native_profile': {'endpoint': 'https://api.anthropic.com/v1',
                                                                                        'model': 'claude-fable-5-2'}}
    assert provider_spend.record(api, [{'call_id': 'a', 'cost': {'currency': 'USD', 'known_amount': 0.5}}], now=NOW) == 1
    assert provider_spend.spent('anthropic-api', 0) == 0.5 and provider_spend.equivalents('anthropic-api', 0) == []


def test_the_lines_of_render_and_the_json_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    firm = {'state': 'firm', 'usd_per_percent': 46.0, 'measured_from': datetime(2026, 11, 11, 9).timestamp(),
            'measured_to': datetime(2026, 11, 16, 9).timestamp(), 'covered_usd': 900.0}
    assert pc.line(firm) == 'about US$ 46 of API-equivalent use per 1% (firm, measured 11 to 16 Nov)'
    assert pc.line({**firm, 'state': 'weak', 'usd_per_percent': 12.5}).startswith('at least US$ 12.5 of API-equivalent use per 1% (weak')
    assert pc.line({**firm, 'measured_from': datetime(2026, 10, 30).timestamp()}).endswith('measured 30 Oct to 16 Nov)')
    assert pc.line({'state': 'none'}) == pc.line(None) == 'no measure yet'
    buckets = [{'id': 'week', 'label': 'week, all models', 'window': 'week', 'scope': None, 'remaining': 0.5,
                'resets_at': NOW + 86400, 'consumption': firm},
               {'id': 'session', 'label': 'session', 'window': '5 h', 'scope': None, 'remaining': 0.9,
                'resets_at': NOW + 3600, 'consumption': {'state': 'none'}}]
    report = {'observed_at': NOW, 'accounts': [{'account': 'claude', 'name': 'Claude subscription', 'kind': 'subscription',
                                                'status': 'OBSERVED', 'observed_at': NOW, 'buckets': buckets}]}
    lines = provider_quota.render(report, NOW).splitlines()
    week = next(i for i, text in enumerate(lines) if 'week, all models' in text)
    assert lines[week + 1].strip() == 'about US$ 46 of API-equivalent use per 1% (firm, measured 11 to 16 Nov)'
    assert lines[week + 3].strip() == 'no measure yet'


def test_refresh_keeps_the_history_and_puts_the_estimate_in_the_json(tmp_path, monkeypatch, capsys):
    from test_provider_quota import CLAUDE, CLAUDE_AUTH
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: 'claude.exe' if endpoint == 'cli://claude' else None)
    monkeypatch.setattr(provider_quota, 'secret', lambda name: None)
    monkeypatch.setattr(provider_quota, 'run', lambda executable, *arguments, timeout=90: json.dumps(
        CLAUDE_AUTH if arguments[0] == 'auth' else {'result': CLAUDE}))
    now = datetime(2026, 11, 10, 8, 0).timestamp()
    report = provider_quota.refresh(now)
    assert [r['bucket'] for r in pc.history()] == ['session', 'week', 'week:fable'] and pc.history()[0]['at'] == now
    assert all(b['consumption'] == {'state': 'none'} for b in report['accounts'][0]['buckets'])
    assert provider_quota.main(['--cached', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['accounts'][0]['buckets'][0]['consumption'] == {'state': 'none'}

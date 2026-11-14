"""The Anthropic API credit read from the Console's Billing page: the parser, the statement of its window, the opt-in."""
import time
from datetime import datetime, timezone

import pytest

from relay import console_credit, environment, provider_quota, provider_spend

SAMPLE = """Credit balance
Purchased credits are used by the API, Claude Code, and Playground. Promotional credits may apply to specific models and products
$15.00
$157.93 promotional credit available
Set up auto-reload
Buy credits

Auto-reload is off. API requests will stop when your balance runs out.

Pay after-the-fact with monthly invoicing by contacting the Anthropic accounts team.
Contact sales
Promotional credits
Promotional credits granted to your organization. All dates are in UTC.
Name Remaining balance Applies to

Expires on Date granted
API credit (Ultra)
$157.93 of $185.42

Agent SDK, API, Batch API, Playground, Managed Agents
Nov 24, 2026, 00:00 UTC Oct 21, 2026
"""
LOADING = SAMPLE.split('Promotional credits\nPromotional credits granted')[0].replace('$15.00', 'Loading') + 'Loading\n'


def ts(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


NOW = ts(2026, 11, 14, 20, 18)
WANTED = {'name': 'API credit (Ultra)', 'remaining': 157.93, 'granted': 185.42, 'expires_at': ts(2026, 11, 24),
          'granted_at': ts(2026, 10, 21), 'applies_to': ['Agent SDK', 'API', 'Batch API', 'Playground', 'Managed Agents']}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    return tmp_path


def test_parse_reads_the_real_page_with_or_without_words_around_it():
    assert console_credit.parse(SAMPLE) == [WANTED]
    assert console_credit.parse('Here is the page:\n\n' + SAMPLE + '\nThat is all of it.') == [WANTED]


def test_parse_two_credits():
    second = ('Claude Code credit\n$5.00 of $20.00\n\nClaude Code\nDec 1, 2026, 00:00 UTC Nov 1, 2026\n')
    credits = console_credit.parse(SAMPLE + second)
    assert [c['name'] for c in credits] == ['API credit (Ultra)', 'Claude Code credit']
    assert credits[1]['remaining'] == 5 and credits[1]['applies_to'] == ['Claude Code']
    assert credits[1]['expires_at'] == ts(2026, 12, 1) and credits[1]['granted_at'] == ts(2026, 11, 1)


def test_parse_says_why_it_cannot():
    assert console_credit.parse(SAMPLE.split('Promotional credits\nPromotional credits granted')[0]) == []  # loaded, none
    with pytest.raises(ValueError, match='had not finished loading'):
        console_credit.parse(LOADING)
    with pytest.raises(ValueError, match='not the Billing page'):
        console_credit.parse('Plans and usage\nSomething else')
    with pytest.raises(ValueError, match='sign in to the Console'):
        console_credit.parse('SIGN_IN_REQUIRED')


def stub_read(monkeypatch, text=SAMPLE, fail=None):
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: 'claude')

    def read(executable, timeout=120):
        if fail:
            raise ValueError(fail)
        return text
    monkeypatch.setattr(console_credit, 'read', read)


def test_sync_states_the_first_expiring_api_credit_with_its_window(monkeypatch):
    other = 'Claude Code credit\n$5.00 of $20.00\n\nClaude Code\nDec 1, 2026, 00:00 UTC Nov 1, 2026\n'
    later = 'API credit (Max)\n$40.00 of $40.00\n\nAPI\nDec 20, 2026, 00:00 UTC Nov 20, 2026\n'
    stub_read(monkeypatch, SAMPLE + other + later)  # the credit that does not apply to the API is ignored
    record = console_credit.sync(NOW)
    assert record['ok'] and record['stated']['usd'] == 157.93 and console_credit.last() == record
    assert provider_spend.declared()['anthropic-api']['window'] == {
        'start': ts(2026, 10, 21), 'end': ts(2026, 11, 24), 'usd': 185.42}
    assert 'lapses Tue 24 Nov' in console_credit.describe(record)


def test_sync_with_nothing_left_states_the_latest_at_zero(monkeypatch):
    stub_read(monkeypatch, SAMPLE.replace('$157.93 of', '$0.00 of'))
    assert console_credit.sync(NOW)['stated']['usd'] == 0


def test_a_failed_reading_leaves_the_earlier_statement_and_records_the_reason(monkeypatch):
    stub_read(monkeypatch)
    console_credit.sync(NOW)
    before = provider_spend.declared()
    stub_read(monkeypatch, fail='the page had not finished loading')
    record = console_credit.sync(NOW + 60)
    assert not record['ok'] and record['reason'] == 'the page had not finished loading'
    assert provider_spend.declared() == before and console_credit.last()['at'] == NOW + 60
    assert 'Console credit not read: the page had not finished loading' in console_credit.describe(record)
    stub_read(monkeypatch, text=SAMPLE.replace('Agent SDK, API,', 'Agent SDK,'))  # no credit applies to the API
    assert 'applies to the API' in console_credit.sync(NOW + 120)['reason'] and provider_spend.declared() == before


def test_window_credit_counts_tares_spend_down_until_it_lapses():
    provider_spend.declare('anthropic-api', 157.93, now=NOW, window=(ts(2026, 10, 21), ts(2026, 11, 24), 185.42))
    provider_spend.record({'native_credential_env': 'ANTHROPIC_API_KEY', 'id': 'api'},
                          [{'call_id': 'a', 'cost': {'currency': 'USD', 'known_amount': 7.93}}], NOW + 60)
    credit = provider_spend.remaining('anthropic-api', NOW + 3600)
    assert (credit['remaining'], credit['total'], credit['grant']) == (150, 185.42, 185.42)
    assert (credit['renews_at'], credit['cycle_start']) == (ts(2026, 11, 24), ts(2026, 10, 21))
    assert provider_spend.remaining('anthropic-api', ts(2026, 11, 24) - 1) is not None
    assert provider_spend.remaining('anthropic-api', ts(2026, 11, 24)) is None  # lapsed
    with pytest.raises(ValueError):
        provider_spend.declare('anthropic-api', 1, renews=(3, 5), window=(1, 2, 3))


def test_an_api_route_with_the_window_credit_is_in_surplus_and_a_hand_statement_replaces_it():
    route = {'id': 'api', 'native_credential_env': 'ANTHROPIC_API_KEY',
             'native_profile': {'endpoint': 'https://api.anthropic.com', 'model': 'fable-5-2', 'protocol': 'anthropic-messages'}}
    provider_spend.declare('anthropic-api', 157.93, now=NOW, window=(ts(2026, 10, 21), ts(2026, 11, 24), 185.42))
    found = provider_quota.surplus(route, NOW)  # 85% left with 69% of the 34 days to run
    assert found and found['allowance'] == 'anthropic-api credit' and found['resets_at'] == ts(2026, 11, 24)
    provider_spend.declare('anthropic-api', 10, now=NOW)
    assert 'window' not in provider_spend.declared()['anthropic-api']


def test_render_says_the_credit_came_from_the_console_and_notes_a_failed_attempt(monkeypatch):
    provider_spend.declare('anthropic-api', 157.93, now=NOW, window=(ts(2026, 10, 21), ts(2026, 11, 24), 185.42))
    credit = provider_spend.remaining('anthropic-api', NOW)
    report = {'observed_at': NOW, 'accounts': [{'account': 'anthropic-api', 'name': 'Claude API', 'kind': 'credit',
                                                'status': 'DECLARED', 'observed_at': NOW, 'credit': credit}]}
    line = next(l for l in provider_quota.render(report, NOW).splitlines() if 'Claude API' in l)
    stamp = datetime.fromtimestamp(NOW).strftime('%d %b %H:%M')
    assert line.startswith(f'  Claude API: US$ 157.93 left of US$ 185.42, lapses Tue 24 Nov (read from the Console {stamp})')
    assert 'in surplus' in line
    assert console_credit.note(NOW) is None
    stub_read(monkeypatch, fail='sign in to the Console in the browser')
    console_credit.sync(NOW)
    text = console_credit.note(NOW)
    assert 'failed (sign in to the Console in the browser' in text and 'earlier statement stays' in text
    monkeypatch.setattr(provider_quota, 'secret', lambda name: None)
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: None)
    accounts = provider_quota.refresh(NOW)['accounts']
    assert [a['notes'] for a in accounts if a['account'] == 'anthropic-api'] == [[text]]
    assert text in provider_quota.render({'observed_at': NOW, 'accounts': accounts}, NOW)


def test_the_start_up_reading_needs_the_choice_and_a_last_attempt_over_12_hours_old(monkeypatch):
    calls = []
    monkeypatch.setattr(console_credit, 'sync', lambda now=None: calls.append(now) or {'ok': True})
    assert console_credit.sync_if_due(NOW) is None and calls == []  # off by default
    environment.main(['--console-credit', 'on'])
    assert environment.read()['console_credit'] == {'use': True}
    assert console_credit.sync_if_due(NOW) and calls == [NOW]  # never tried
    from relay.relay_io import write_json_atomic
    write_json_atomic(console_credit.path(), {'at': NOW - 13*3600, 'ok': False})
    assert console_credit.sync_if_due(NOW) and len(calls) == 2
    write_json_atomic(console_credit.path(), {'at': NOW - 3600, 'ok': True})
    assert console_credit.sync_if_due(NOW) is None and len(calls) == 2
    write_json_atomic(console_credit.path(), {'at': NOW - 1800, 'ok': False, 'reason': 'x'})
    assert console_credit.sync_if_due(NOW) is None and len(calls) == 2  # a failure waits an hour, not half a day
    write_json_atomic(console_credit.path(), {'at': NOW - 2*3600, 'ok': False, 'reason': 'x'})
    assert console_credit.sync_if_due(NOW) and len(calls) == 3
    environment.main(['--console-credit', 'off'])
    assert 'console_credit' not in environment.read()
    write_json_atomic(console_credit.path(), {'at': NOW - 13*3600, 'ok': True})
    assert console_credit.sync_if_due(NOW) is None and len(calls) == 3


def test_tare_quota_sync_runs_one_reading_and_prints_it(monkeypatch, capsys):
    stub_read(monkeypatch)
    monkeypatch.setattr(provider_quota, 'refresh', lambda now=None: {'observed_at': NOW, 'accounts': []})
    assert provider_quota.main(['--sync']) == 0
    assert 'Read from the Console: US$ 157.93 left of US$ 185.42' in capsys.readouterr().err
    stub_read(monkeypatch, fail='not the Billing page')
    assert provider_quota.main(['--sync']) == 1

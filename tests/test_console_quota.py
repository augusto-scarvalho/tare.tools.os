"""/quota: the console screen that shows quota, credit and cost and lets the owner state a balance and the energy price."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from textual.widgets import Button, Input, Static

from relay import console_entry, environment, provider_quota, provider_spend

NOW = time.time()


def reading(left=5.23):
    return {'schema': provider_quota.SCHEMA, 'observed_at': NOW, 'accounts': [
        {'account': 'openrouter', 'name': 'OpenRouter', 'kind': 'credit', 'status': 'OBSERVED', 'observed_at': NOW,
         'credit': {'currency': 'USD', 'total': 20, 'remaining': left}},
        {'account': 'anthropic-api', 'name': 'Claude API', 'kind': 'credit', 'status': 'NOT_REPORTED', 'observed_at': NOW,
         'reason': 'No balance endpoint for an API key.'},
        {'account': 'openai-api', 'name': 'OpenAI API', 'kind': 'credit', 'status': 'NOT_REPORTED', 'observed_at': NOW,
         'reason': 'No balance endpoint for an API key.'}]}


def save_reading(report):
    provider_quota.cache_path().parent.mkdir(parents=True, exist_ok=True)
    provider_quota.cache_path().write_text(json.dumps(report), encoding='utf-8')
    return report


@pytest.fixture
def quota(tmp_path, monkeypatch):
    """The panel's app with a saved reading and a refresh that never leaves the process."""
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    refreshed = []
    monkeypatch.delenv('TARE_CLI_DISCOVERY', raising=False)
    monkeypatch.setattr(provider_quota, 'refresh', lambda now=None: refreshed.append(1) or save_reading(reading(4.0)))
    save_reading(reading())

    def run(scenario, asks=False):
        async def main():
            app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
            async with app.run_test(size=(130, 44)) as pilot:
                await app.workers.wait_for_complete()
                app.asks_providers = asks
                assert app.dispatch_conversation_command('/quota', '')
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await scenario(app, app.screen, pilot)
        asyncio.run(main())
    return run, refreshed


def text(panel):
    return str(panel.query_one('#quota-text', Static).render())


def status(panel):
    return str(panel.query_one('#quota-status', Static).render())


def fill(panel, **fields):
    for name, value in fields.items():
        panel.query_one('#quota-' + name.replace('_', '-'), Input).value = value


async def press_save(app, panel, pilot):
    panel.query_one('#quota-save', Button).press()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_quota_is_a_command_and_opens_the_panel(quota):
    from relay.console_quota import QuotaPanel
    assert '/quota' in [name for name, _, _ in console_entry.COMMANDS] and '/quota' in console_entry.CONVERSATION_COMMANDS
    assert '/quota' in console_entry.HELP

    async def scenario(app, panel, pilot):
        assert isinstance(panel, QuotaPanel)
        assert str(panel.query_one('.panel-title', Static).render()) == 'Quota, credit and cost'
        await pilot.press('escape')
        assert app.screen is not panel
    quota[0](scenario)


def test_the_panel_shows_the_saved_reading_then_the_new_one(quota):
    run, refreshed = quota

    async def idle(app, panel, pilot):  # not the interactive entry: it asks no provider by itself
        assert 'OpenRouter: US$ 5.23 left of US$ 20.00' in text(panel) and not refreshed
        assert provider_quota.render(provider_quota.cached(), NOW + 1) in text(panel).replace('\n\n', '\n\n')
        panel.query_one('#quota-read', Button).press()  # Read again does
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert 'OpenRouter: US$ 4.00 left of US$ 20.00' in text(panel) and refreshed
    run(idle)

    async def interactive(app, panel, pilot):
        assert 'OpenRouter: US$ 4.00 left of US$ 20.00' in text(panel) and len(refreshed) == 2
    run(interactive, asks=True)


def test_a_balance_and_a_date_state_that_account_only(quota):
    async def scenario(app, panel, pilot):
        assert [a for a, _ in panel.accounts] == ['anthropic-api', 'openai-api']  # OpenRouter reports its own
        day = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%d')
        fill(panel, balance_anthropic_api='12.5', date_anthropic_api=day, day_openai_api='')
        await press_save(app, panel, pilot)
        declared = provider_spend.declared()
        assert list(declared) == ['anthropic-api'] and declared['anthropic-api']['usd'] == 12.5
        assert declared['anthropic-api']['window']['end'] == provider_quota.day_end(day)
        assert declared['anthropic-api']['window']['stated'] and 'renews' not in declared['anthropic-api']
        assert status(panel).startswith('Saved: Claude API. ')
        assert panel.query_one('#quota-balance-anthropic-api', Input).value == ''
    quota[0](scenario)


def test_a_balance_and_a_day_state_a_renewing_credit(quota):
    async def scenario(app, panel, pilot):
        fill(panel, balance_openai_api='40', day_openai_api='14')
        await press_save(app, panel, pilot)
        assert provider_spend.declared()['openai-api']['renews'] == {'day': 14, 'usd': 40.0}
    quota[0](scenario)


@pytest.mark.parametrize('fields, why', [
    ({'date_anthropic_api': '2099-01-01'}, 'a day or a date needs a balance'),
    ({'day_anthropic_api': '3'}, 'a day or a date needs a balance'),
    ({'balance_anthropic_api': '5', 'day_anthropic_api': '3', 'date_anthropic_api': '2099-01-01'}, 'not both'),
    ({'balance_anthropic_api': '5', 'date_anthropic_api': '2020-01-01'}, 'a day still to come'),
    ({'balance_anthropic_api': '5', 'date_anthropic_api': 'soon'}, 'the date \'soon\' is not valid'),
    ({'balance_anthropic_api': '5', 'day_anthropic_api': '40'}, 'a day of the month')])
def test_a_wrong_statement_saves_nothing_for_any_account_and_says_why(quota, fields, why):
    async def scenario(app, panel, pilot):
        fill(panel, balance_openai_api='9', energy='0.2', **fields)  # a good statement beside the wrong one
        await press_save(app, panel, pilot)
        assert provider_spend.declared() == {} and provider_spend.local_energy() == {}
        message = status(panel)
        assert message.startswith('Claude API: ') and why in message and message.endswith('Nothing was saved.')
    quota[0](scenario)


def test_the_energy_price_is_saved_only_when_it_changed(quota, monkeypatch):
    calls = []
    real = environment.update
    monkeypatch.setattr(environment, 'update', lambda change: calls.append(1) or real(change))

    async def scenario(app, panel, pilot):
        assert panel.query_one('#quota-energy', Input).value == ''
        await press_save(app, panel, pilot)  # nothing filled, nothing changed
        assert not calls and status(panel).startswith('Nothing to save')
        fill(panel, energy='0.15')
        await press_save(app, panel, pilot)
        assert len(calls) == 1 and provider_spend.local_energy() == {'price_kwh': 0.15}, status(panel)
        assert status(panel).startswith('Saved: energy price. ')
        await press_save(app, panel, pilot)  # the same price again
        assert len(calls) == 1
        fill(panel, energy='lots')
        await press_save(app, panel, pilot)
        assert len(calls) == 1 and status(panel).startswith('Energy price: ') and 'Nothing was saved.' in status(panel)
    quota[0](scenario)


def test_the_panel_holds_no_credential(quota, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-fixture-not-a-secret')

    async def scenario(app, panel, pilot):
        fields = list(panel.query(Input))
        assert fields and not any(f.password or 'key' in f.id or 'token' in f.id for f in fields)
        shown = ' '.join([text(panel), status(panel), *(f.value for f in fields)])
        assert 'sk-fixture' not in shown
        assert 'API_KEY' not in shown and 'token' not in shown.lower()
    quota[0](scenario)


def test_render_says_what_tare_does_because_of_each_line():
    resets = NOW + 7200
    report = {'observed_at': NOW, 'accounts': [
        {'account': 'claude', 'name': 'Claude subscription', 'kind': 'subscription', 'status': 'OBSERVED', 'observed_at': NOW,
         'buckets': [{'id': 'week', 'label': 'week, all models', 'window': 'week', 'scope': None, 'remaining': 0,
                      'resets_at': resets},
                     {'id': 'h', 'label': '5 h', 'window': '5 h', 'scope': None, 'remaining': 0, 'resets_at': None},
                     {'id': 'w2', 'label': 'week, Fable', 'window': 'week', 'scope': 'fable', 'remaining': 0.5,
                      'resets_at': resets}]}]}
    lines = provider_quota.render(report, NOW).splitlines()
    week = next(line for line in lines if 'week, all models' in line)
    assert week.endswith(' · its routes are left out until then')
    assert not next(line for line in lines if line.strip().startswith('5 h')).endswith('until then')  # no reset time
    assert 'until then' not in next(line for line in lines if 'week, Fable' in line)  # something is left
    assert provider_quota.used_first(0.9, 0.2).endswith(': used first') and provider_quota.used_first(0.2, 0.9) == ''

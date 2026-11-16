"""A new model version is proven when it arrives (console_workbench.prove_arrivals): by itself on a subscription, only
after Prove on a pay-per-token route, once per route. Offline: environment.prove is a stand-in, discovery and the
start-up readings are stubs."""
import asyncio
import json

import pytest
from textual.widgets import Button

from relay import environment, native_catalog, native_routes
from relay.console_route_matrix import ProveDialog
from relay.native_console import WorkConsole
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_derived_role_rows import SOL, discovered
from test_native_console import FakeBackend

FABLE = 'claude-fable-5-3-cli'  # derived from claude-fable-5-2-cli: a subscription (Claude Code)
API = 'claude-api-fable-5-3'  # derived from claude-api-fable-5-2: pay per token
ROLES = ('planner', 'plan-auditor', 'auditor', 'implementer', 'explorer')  # claude-fable-5-2-cli's, so its derived row's


@pytest.fixture
def quiet(monkeypatch):
    """The interactive start's worker with no CLI, reading or network; returns the stand-in prove's calls."""
    monkeypatch.delenv('TARE_CLI_DISCOVERY', raising=False)
    monkeypatch.setattr('relay.cli_discovery.discover', lambda: [])
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: None)
    monkeypatch.setattr('relay.provider_prices.refresh_reading', lambda: None)
    monkeypatch.setattr('relay.model_signals.refresh_reading', lambda: None)
    monkeypatch.setattr('relay.provider_quota.refresh', lambda: None)
    return prover(monkeypatch)


def prover(monkeypatch, outcome=lambda name: {'ok': True}):
    """No real call: like the real prove it asks `confirm` and keeps its record in the setup (never a reply)."""
    calls = []

    def prove(name, confirm):
        assert confirm(f'{name}: fixture.')
        calls.append(name)
        return environment.remember(name, {'cost': 'fixture', **outcome(name)})
    monkeypatch.setattr(environment, 'prove', prove)
    return calls


def paid(pool, monkeypatch):
    value = native_catalog.bundled('claude-api-fable-5-2')
    value['id'], value['model'], value['native_profile']['model'] = API, 'claude-fable-5-3', 'claude-fable-5-3'
    pool.mkdir(exist_ok=True)
    (pool/(API+'.json')).write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture')  # present, never used: prove is a stand-in
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'fixture cost of ' + deployment['id'])


def start(root, answer=None, discover=True):
    """One TUI start: (status lines, notices, the dialog's lines or None, events). `answer`: the dialog's button."""
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant', discover_clis=discover)
        said, shown, events, dialog = [], [], [], None
        status, push = app.set_status, app.push_screen
        app.set_status = lambda value: said.append(value) or status(value)
        app.notify = lambda message, **kwargs: shown.append(message)
        app.push_screen = lambda screen, *a, **k: (isinstance(screen, ProveDialog) and events.append('asked')) or \
            push(screen, *a, **k)
        real = environment.prove
        environment.prove = lambda name, confirm: events.append(name) or real(name, confirm)
        try:
            async with app.run_test(size=(130, 44)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                if isinstance(app.screen, ProveDialog):
                    dialog = app.screen.lines
                    if answer == 'escape':
                        await pilot.press('escape')
                    elif answer:
                        app.screen.query_one(answer, Button).press()
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                assert app.is_running
        finally:
            environment.prove = real
        return said, shown, dialog, events
    return asyncio.run(scenario())


@pytest.mark.parametrize('outcome,notice', [
    ({'ok': True}, f"{FABLE} answered and did the task: it is now used automatically where its line is ticked in "
                   "/routes, as its line's newest version."),
    ({'ok': False, 'reason': 'answered, but failed the role check: x'},
     f'{FABLE} was not proven: answered, but failed the role check: x. It stays pin only; /routes > Models per role '
     'can prove it again.')])
def test_a_subscription_arrival_is_proven_once_with_no_question_and_announced(pool, quiet, monkeypatch, tmp_path,
                                                                             outcome, notice):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    calls = prover(monkeypatch, lambda name: outcome)
    said, shown, dialog, _ = start(tmp_path)
    assert calls == [FABLE] and dialog is None
    before = next(s for s in said if s.startswith('Proving '))
    assert before.startswith(f'Proving {FABLE}, a new model version: one minimal call and one short task of at most '
                             '6 calls') and 'account: no dollars; they count against that plan' in before
    assert [m for m in shown if FABLE in m] == [notice]
    said, shown, dialog, _ = start(tmp_path)  # the record exists, passed or failed: nothing runs or asks
    assert calls == [FABLE] and dialog is None and not [m for m in shown + said if FABLE in m]


def test_a_pay_per_token_arrival_is_asked_first_and_called_only_after_prove(pool, quiet, monkeypatch, tmp_path):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    paid(pool, monkeypatch)
    said, shown, dialog, events = start(tmp_path, '#prove-go')
    assert f'{API}: fixture cost of {API}' in dialog and not any(FABLE in line for line in dialog)
    assert events == ['asked', FABLE, API]  # asked before the subscription proof; paid only after Prove
    assert quiet == [FABLE, API] and environment.read()['proofs'][API]['ok'] is True
    assert f'Proving {API}, a new model version: fixture cost of {API}…' in said
    assert any(m.startswith(f'{API} answered and did the task') for m in shown)


@pytest.mark.parametrize('answer', ['#prove-cancel', 'escape'])
def test_a_declined_pay_per_token_arrival_is_recorded_not_proven_and_not_asked_again(pool, quiet, monkeypatch,
                                                                                    tmp_path, answer):
    paid(pool, monkeypatch)
    _, _, dialog, _ = start(tmp_path, answer)
    assert dialog and quiet == []
    record = environment.read()['proofs'][API]
    assert {k: v for k, v in record.items() if k != 'at'} == {
        'ok': None, 'reason': 'not confirmed', 'cost': f'fixture cost of {API}'} and record['at']
    # Every reader takes it as not proven: admission (so ranking, the matrix and tare setup --auto-show) and the
    # newest version per line (model_signals._newest, through proven).
    assert native_routes.proven({'native_catalog': API}, {}) is False
    assert dict((n, now) for n, _, now in environment.auto_routes('planner'))[API] == 'NOT_QUALIFIED_PIN_ONLY'
    _, _, dialog, events = start(tmp_path)
    assert dialog is None and events == [] and quiet == []


def test_a_shipped_pin_only_row_is_never_proven_by_itself(pool):
    install(pool, 'agy-claude-opus-5-5-cli', 'cli://agy', 'claude-opus-5-5')
    row = next(r for r in native_routes.configured()['deployments'] if r['id'] == 'agy-claude-opus-5-5-cli')
    assert row['qualified'] is False and 'Derived' not in row.get('note', '')  # pin only, but written, not derived
    assert environment.route_for('agy-claude-opus-5-5-cli')[0] and environment.arrivals() == []


def test_a_line_switched_off_for_every_role_of_its_row_is_left_alone(pool):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    off = [arg for role in ROLES[:-1] for arg in ('--auto', f'{role}:claude/fable@claude-code=off')]
    assert environment.main(off) == 0
    assert [name for name, _ in environment.arrivals()] == [FABLE]  # still on for the explorer
    assert environment.main(['--auto', 'explorer:claude/fable@claude-code=off']) == 0
    assert environment.arrivals() == []


@pytest.mark.parametrize('discovery,interactive', [('0', True), (None, False)])
def test_no_discovery_or_a_non_interactive_entry_proves_nothing(pool, quiet, monkeypatch, tmp_path, discovery,
                                                                interactive):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    paid(pool, monkeypatch)
    if discovery:
        monkeypatch.setenv('TARE_CLI_DISCOVERY', discovery)
    _, _, dialog, events = start(tmp_path, discover=interactive)
    assert dialog is None and events == [] and quiet == [] and 'proofs' not in environment.read()


def test_a_proof_that_raises_is_reported_kept_as_failed_and_the_next_arrival_goes_on(pool, quiet, monkeypatch,
                                                                                     tmp_path):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    discovered(pool)

    def prove(name, confirm):
        quiet.append(name)
        if name == FABLE:
            raise RuntimeError('boom')
        return environment.remember(name, {'ok': True, 'cost': 'fixture'})
    monkeypatch.setattr(environment, 'prove', prove)
    _, shown, _, _ = start(tmp_path)
    assert sorted(quiet) == sorted([FABLE, SOL])
    assert f'{FABLE} was not proven: RuntimeError: boom. It stays pin only; /routes > Models per role can prove it ' \
           'again.' in shown
    assert any(m.startswith(f'{SOL} answered and did the task') for m in shown)
    assert environment.read()['proofs'][FABLE]['ok'] is False  # not spent again by itself at the next start


def test_two_tuis_that_both_saw_the_arrival_call_the_provider_once(pool, quiet, monkeypatch, tmp_path):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    seen = environment.arrivals()  # both windows looked before either wrote anything
    assert [name for name, _ in seen] == [FABLE]
    monkeypatch.setattr(environment, 'arrivals', lambda: seen)
    calls, other = [], []

    def prove(name, confirm):  # the first window's provider call is still running when the second window starts
        calls.append(name)
        if len(calls) == 1:  # once: without the reservation the second window calls too, and must not open a third
            other.append(start(tmp_path))
        return environment.remember(name, {'ok': True, 'cost': 'fixture'})
    monkeypatch.setattr(environment, 'prove', prove)
    start(tmp_path)
    said, shown, _, events = other[0]
    assert calls == [FABLE] and events == []  # one call in all; the second window made none
    assert f'{FABLE}: another tare window is proving it or answered for it.' in said
    assert not [m for m in shown if FABLE in m] and environment.read()['proofs'][FABLE]['ok'] is True


def test_one_of_many_claims_made_together_wins_and_a_dead_tares_claim_lapses(pool, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta
    from threading import Barrier
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    gate = Barrier(6)
    with ThreadPoolExecutor(6) as threads:
        won = list(threads.map(lambda _: gate.wait() is not None and environment.claim(FABLE), range(6)))
    assert won.count(True) == 1 and environment.arrivals() == []  # reserved: no other start proves it meanwhile
    assert native_routes.proven({'native_catalog': FABLE}, {}) is False  # a reservation is not a proof

    def age(value):  # the tare that reserved it died mid-proof
        record = value['proofs'][FABLE]
        record['at'] = (datetime.fromisoformat(record['at']) - timedelta(minutes=environment.CLAIM_MINUTES + 1)).isoformat()
        return value
    environment.update(age)
    assert [name for name, _ in environment.arrivals()] == [FABLE] and environment.claim(FABLE) is True


def test_a_cancel_never_overwrites_what_another_window_recorded(pool):
    environment.remember(API, {'ok': True, 'cost': 'fixture'})
    assert environment.claim(API, {'ok': None, 'reason': 'not confirmed', 'cost': 'fixture'}) is False
    assert environment.read()['proofs'][API]['ok'] is True

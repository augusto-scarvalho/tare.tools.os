"""Operator ceilings reach dispatchers; lifting money preserves accounting."""
import argparse
import asyncio
import json
import math

import pytest

from relay import execution_resources as resources, execution_scope as scope, native_agent, native_limits
from test_native_agent import Client, deployment, setup  # noqa: F401
from test_native_spend import Adapter, guard, request, control
from test_terminal_project_entry import opened  # noqa: F401
from test_planning_tools import planner, wrap, execution, request as plan_request, stub_child  # noqa: F401
from test_conversation_review_recovery import reviewed, host  # noqa: F401


@pytest.mark.parametrize('argv,expected', [
    (['--unlimited'], resources.RESOURCES),
    (['--unlimited='], resources.RESOURCES),
    (['--unlimited', 'calls', 'money'], ('calls', 'money')),
    (['--unlimited=budget,time'], ('time', 'money')),
    (['--unlimited', 'compactions', '--unlimited', 'stagnation'], ('compactions', 'stagnation')),
])
def test_resource_lists_and_bare_flag(argv, expected):
    parser = argparse.ArgumentParser()
    resources.add_argument(parser)
    assert resources.parse(parser.parse_args(resources.normalize_arguments(argv)).unlimited) == expected


def test_public_launcher_keeps_project_and_restrictions_and_restores_scope(opened, monkeypatch):
    from relay import native_console, native_installation, native_product
    project, _ = opened
    observed = []
    class Console:
        def __init__(self, root, **kwargs): observed.append((root, scope.current()))
        def run(self): pass
    monkeypatch.setattr(native_console, 'WorkConsole', Console)
    argv = ['--unlimited', 'calls', 'budget', '--prefer_subscription', str(project)]
    assert native_installation.main(argv) == 0
    assert observed[0][0] == project.resolve()
    assert observed[0][1].unlimited == ('calls', 'money') and observed[0][1].prefer == 'subscription'
    assert not scope.current().active
    assert 'console' in native_product.console_arguments(argv)


def test_typo_is_rejected_before_launch():
    with pytest.raises(ValueError, match='Unknown unlimited resource'):
        scope.Scope(unlimited=('calss',)).validate()


def test_explicit_numeric_cli_limit_overrides_only_the_matching_resource():
    @resources.cli
    def command(argv): return scope.current()
    result = command(['--unlimited', '--max-model-calls', '5'])
    assert result.unlimited == ('time', 'money', 'compactions', 'stagnation')
    assert not scope.current().active


def test_verifier_arguments_cannot_authorize_unlimited_spending():
    @resources.cli
    def command(argv): return argv, scope.current()
    argv = ['--verify-command', 'python', 'check.py', '--unlimited', 'money']
    observed, policy = command(argv)
    assert observed == argv and not policy.unlimited


def test_tui_displays_money_only_without_disabling_the_call_time_fields(tmp_path):
    from textual.widgets import Checkbox, Input
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            app.sync_budget_fields()
            widget = app.query_one('#unlimited', Checkbox)
            assert widget.disabled and 'money' in str(widget.label)
            assert not app.query_one('#max-model-calls', Input).disabled
            assert not app.query_one('#max-seconds', Input).disabled
            assert 'Unlimited: money' in str(app.query_one('#execution-scope').render())
    with scope.activated(scope.Scope(unlimited=('money',))):
        asyncio.run(scenario())


def test_money_flag_does_not_release_other_limits(setup):
    config, custody, _ = setup
    client = Client(steps=[('tare_deliver', {'summary': 'done'})])
    with scope.activated(scope.Scope(unlimited=('money',))):
        native_agent.run_session(config, deployment(), custody, 'Bounded work', client=client, local_counter=client)
    state = json.loads((custody/'native-agent.json').read_text())
    assert state['limits']['max_model_calls'] == 16 and state['limits']['max_seconds'] == 300
    assert state['limits']['max_stagnant_turns'] == 3


def test_an_explicit_null_still_lifts_the_no_progress_stop_under_unlimited(setup):
    config, custody, _ = setup
    client = Client(steps=[('tare_deliver', {'summary': 'done'})])
    with scope.activated(scope.Scope(unlimited=resources.RESOURCES)):
        native_agent.run_session(config, deployment(), custody, 'Bounded work', client=client, local_counter=client,
                                 limits={'max_stagnant_turns': None})
    assert json.loads((custody/'native-agent.json').read_text())['limits']['max_stagnant_turns'] is None


def test_new_session_runs_beyond_default_call_limit_without_disabling_cancellation(setup):
    config, custody, root = setup
    for number in range(18):  # new files each time: a repeated read would trip the no-progress stop
        (root/f'a{number}.txt').write_text('evidence')
    client = Client(steps=[*[('read', {'repository': 'repo', 'path': f'a{number}.txt'}) for number in range(18)],
                           ('tare_deliver', {'summary': 'Read all required evidence'})])
    with scope.activated(scope.Scope(unlimited=resources.RESOURCES)):
        result = native_agent.run_session(config, deployment(), custody, 'Read evidence', client=client, local_counter=client)
    state = json.loads((custody/'native-agent.json').read_text())
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 19
    assert state['limits']['max_model_calls'] is None and state['deadline_utc'] is None
    assert state['limits']['max_stagnant_turns'] == native_limits.UNLIMITED_STAGNANT_TURNS  # looser, not gone
    assert state['config']['context_policy']['max_compactions'] is None
    assert state['limits']['max_model_retries'] == 1


def test_real_child_cannot_expand_its_finite_dispatch_allowance(setup):
    config, custody, root = setup
    (root/'a.txt').write_text('evidence')
    client = Client(steps=[('read', {'repository': 'repo', 'path': 'a.txt'}) for _ in range(12)])
    with scope.activated(scope.Scope(unlimited=resources.RESOURCES)):
        result = native_agent.run_session(config, deployment(), custody, 'Read evidence',
            limits={'max_model_calls': 5}, delegated_leaf=True, client=client, local_counter=client)
    state = json.loads((custody/'native-agent.json').read_text())
    assert state['limits']['max_model_calls'] == 5 and result['model_calls'] == 5


def test_lifting_money_keeps_uncertain_reservations_and_restores_the_original_cap():
    from tare_tools_agent_runtime import ModelFailure
    model = guard(Adapter())
    with scope.activated(scope.Scope(unlimited=('money',))):
        model.generate(request(model), control())
        model.generate(request(model, 2), control())
        report = model.report()
        assert report['max_amount'] is None and report['remaining_amount'] is None
        assert report['configured_max_amount'] == model.policy.max_amount
        assert report['uncertain_calls'] == 2 and report['reserved_calls'] == 2
        assert report['committed_amount'] != '0'
        reopened = guard(Adapter(), journal=model.journal)
        assert reopened.report()['uncertain_calls'] == 2
    with pytest.raises(ModelFailure, match='SPEND_LIMIT'):
        reopened.generate(request(reopened, 3), control())
    assert reopened.adapter.calls == 0 and reopened.report()['uncertain_calls'] == 2


def test_unknown_child_cost_is_not_zero_and_prevents_reusing_a_finite_money_budget():
    from tare_tools_agent_runtime import ModelFailure
    from relay.native_spend import outcome_commitment, total_amounts
    model = guard(Adapter())
    model.external_committed = lambda: None
    assert outcome_commitment({'cost': {'known_by_currency': {}, 'unpriced_or_partial_calls': 1}}) is None
    assert total_amounts(['.2', None]) is None
    with scope.activated(scope.Scope(unlimited=('money',))):
        model.generate(request(model), control())
        assert model.report()['committed_amount'] is None
        assert model.report()['uncertain_auxiliary_cost']
    with pytest.raises(ModelFailure, match='SPEND_AUXILIARY_COST_UNKNOWN'):
        model.generate(request(model, 2), control())
    assert model.adapter.calls == 1


def test_unlimited_money_admits_unpriced_calls_but_keeps_their_cost_unknown():
    from tare_tools_agent_runtime import ModelFailure
    from test_native_spend import tariff
    model = guard(Adapter())
    model.tariff = tariff(output_tokens=None)
    with scope.activated(scope.Scope(unlimited=('money',))):
        model.generate(request(model), control())
        report = model.report()
        assert report['reserved_calls'] == 1 and report['uncertain_calls'] == 1
        assert report['committed_amount'] is None and report['unbounded_uncertain_cost']
    with pytest.raises(ModelFailure, match='SPEND_UNRECONCILED_UNBOUNDED_CALL'):
        model.generate(request(model, 2), control())
    assert model.adapter.calls == 1


@pytest.mark.parametrize('selected,expected_calls,expected_seconds', [
    ((), 30, 900), (('calls',), None, 900), (('time',), 30, None),
    (resources.RESOURCES, None, None),
])
def test_worker_receives_selected_resources_only(tmp_path, selected, expected_calls, expected_seconds):
    from relay.work_units import child_limits
    state = {'limits': {'max_model_calls': None}, 'deadline_utc': None}
    with scope.activated(scope.Scope(unlimited=selected)):
        result = child_limits(tmp_path, state)
    assert result['max_model_calls'] == expected_calls and result['max_seconds'] == expected_seconds


def test_unlimited_worker_still_obeys_a_finite_enclosing_budget(tmp_path):
    from relay.work_units import child_limits
    state = {'limits': {'max_model_calls': 40}, 'deadline_utc': None}
    with scope.activated(scope.Scope(unlimited=resources.RESOURCES)):
        result = child_limits(tmp_path, state)
    from relay.work_units import SESSION_RESERVE
    assert result['max_model_calls'] == 40-SESSION_RESERVE


def test_auditor_no_longer_has_the_hidden_eight_call_or_ten_minute_ceiling(reviewed, monkeypatch):
    host, run, *_ = reviewed
    host[0].state['limits']['max_model_calls'] = None
    monkeypatch.setattr(type(host[1]), 'remaining_seconds', lambda self: math.inf)
    with scope.activated(scope.Scope(unlimited=resources.RESOURCES)):
        run('INCONCLUSIVE')
    state = json.loads(next((host[0].root/'reviews').glob('*/session/native-agent.json')).read_text())
    assert state['limits']['max_model_calls'] is None and state['limits']['max_seconds'] is None


def test_planner_inherits_unlimited_without_losing_its_plan(planner, monkeypatch):
    parent, state, candidate = planner
    state['limits']['max_model_calls'] = None
    seen = []
    stub_child(monkeypatch, candidate, seen)
    context = execution()
    context = type(context)(context.binding, context.cancellation, math.inf, lambda: 0)
    with scope.activated(scope.Scope(unlimited=resources.RESOURCES)):
        result = wrap(parent, state, context).execute(plan_request(), context)
    assert result.status == 'succeeded', result.output_json
    assert seen and seen[0][-1]['limits']['max_model_calls'] is None and seen[0][-1]['limits']['max_seconds'] is None

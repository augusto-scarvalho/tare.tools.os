"""Temporary exact role pins fail closed without rewriting historical choices."""
import argparse
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from relay import execution_scope as scope, model_choices as models
from relay import native_catalog, turn_engine, native_routes, native_plan, planner_policy
from test_models_cli import catalog  # noqa:F401


def parse(argv):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('project', nargs='?')
    scope.add_arguments(parser)
    return scope.from_arguments(parser.parse_args(scope.normalize_arguments(argv)))


@pytest.mark.parametrize('role', scope.PIN_ROLES)
def test_all_role_flags_are_exact_transient_and_inherited(catalog, monkeypatch, role):
    before = deepcopy(catalog)
    policy = parse(['--'+role, 'anthropic/test-cli@high'])
    with scope.activated(policy):
        assert scope.current().pin(role) == 'anthropic/test-cli@high'
        assert scope.current().intersect(scope.Scope()).pin(role) == policy.pin(role)
        if role == 'auditor':
            assert scope.current().pin('plan-auditor') == policy.pin(role)
        assert not scope.current().changes('coordinator')
    assert not scope.current().pins and catalog == before
    with pytest.raises(RuntimeError):
        with scope.activated(policy):
            raise RuntimeError('child failed')
    assert not scope.current().pins


@pytest.mark.parametrize('raw', ['', 'anthropic', 'anthropic/test-cli', 'anthropic/test-cli@ultra',
                                  'anthropic/test-cli@high,medium', 'anthropic/opus@high'])
def test_incomplete_ambiguous_and_unsupported_pins_are_rejected(catalog, raw):
    with pytest.raises(ValueError, match='tare models'):
        parse(['--implementer='+raw])


def test_conflicts_and_planner_floor(catalog):
    for argv in (
        ['--planner', 'anthropic/test-cli@medium'],
        ['--implementer', 'anthropic/test-cli@high', '--implementer', 'anthropic/test-cli@max'],
        ['--api_only', '--implementer', 'anthropic/test-cli@high'],
        ['--pro_planner', '--planner', 'anthropic/test-cli@high'],
        ['--cli_only=anthropic/test-cli@medium', '--implementer', 'anthropic/test-cli@high'],
    ):
        with pytest.raises(ValueError, match='tare models'):
            parse(argv)
    high = parse(['--implementer', 'anthropic/test-cli@high'])
    maximum = parse(['--implementer', 'anthropic/test-cli@max'])
    with pytest.raises(ValueError, match='Conflicting'):
        high.intersect(maximum)


def live(monkeypatch):
    monkeypatch.setattr(turn_engine, 'usage_limited', lambda value: None)
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: False)
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda value: None)
    from relay import environment
    monkeypatch.setattr(environment, 'allows', lambda value: scope.current().allows(value))


@pytest.mark.parametrize('role', ['planner', 'implementer', 'explorer', 'researcher'])
def test_live_admission_preserves_exact_effort_or_refuses_without_fallback(catalog, monkeypatch, role):
    live(monkeypatch)
    before = deepcopy(catalog)
    with scope.activated(parse(['--'+role, 'anthropic/test-cli@max'])):
        value = scope.pinned(role)
        assert scope.pin_matches('anthropic/test-cli@max', value)
        assert json.loads(value['native_profile']['parameters_json'])['effort'] == 'max'
        monkeypatch.setattr(turn_engine, 'usage_limited', lambda value: 9999999999)
        with pytest.raises(ValueError, match='No fallback'):
            scope.pinned(role)
    assert catalog == before


def test_auditor_and_plan_auditor_keep_independence(catalog, monkeypatch):
    live(monkeypatch)
    with scope.activated(parse(['--auditor', 'anthropic/test-cli@high'])):
        for role in ('auditor', 'plan-auditor'):
            with pytest.raises(ValueError, match='not independent'):
                native_routes.ranked(role, catalog['test-cli'])


def test_dispatch_and_planner_effort_conflict(catalog, monkeypatch):
    live(monkeypatch)
    with scope.activated(parse(['--planner', 'anthropic/test-cli@max'])):
        value, receipt = native_plan.select_route('auto', 'planner', 'test')
        assert receipt['reason'] == 'EXACT_TEMPORARY_PIN'
        assert scope.pin_matches('anthropic/test-cli@max', value)
        with pytest.raises(ValueError, match='conflicts'):
            planner_policy.effective(catalog['test-cli'], 'high')
    with scope.activated(parse(['--implementer', 'anthropic/test-cli@high'])):
        assert scope.execution_tiers([['other']]) == [['test-cli']]
        with scope.for_role('implementer'):
            assert turn_engine.next_auto({'native_auto_selection': {'tier': 0}}) is None


def test_no_flags_preserves_routes_and_saved_scope_is_not_authority(catalog, tmp_path):
    tiers = [['existing']]
    assert scope.execution_tiers(tiers) is tiers
    @scope.session_role
    def run(config, deployment, custody, **kwargs):
        return scope.current()
    saved = {'config': {'role': 'implementer'}, 'deployment': catalog['test-cli'],
             'execution_policy': {'pins': [['implementer', 'anthropic/test-cli@max']]}}
    (tmp_path/'native-agent.json').write_text(json.dumps(saved))
    assert not run(None, None, tmp_path, resume=True).pins
    assert json.loads((tmp_path/'native-agent.json').read_text()) == saved



def test_explorer_pin_rebuilds_variant_metadata_without_overflow(catalog, monkeypatch):
    from relay import delegation, challenger_variants
    live(monkeypatch)
    original = challenger_variants.realize({'profile': 'test-cli', 'task_types': ['exploration']})
    frozen = {'variant': original, 'overflow': original, 'experimental': True, 'settings': {}}
    before = deepcopy(frozen)
    with scope.activated(parse(['--explorer', 'anthropic/test-cli@max'])):
        projected = delegation.scoped(frozen)
        assert projected['overflow'] is None
        assert projected['variant']['accounting'] == 'quota'
        assert projected['variant']['key'] != original['key']
        assert scope.pin_matches('anthropic/test-cli@max', projected['variant']['deployment'])
    assert frozen == before


def test_research_command_keeps_explicit_effort_and_read_only_tools(catalog, monkeypatch, tmp_path):
    from relay import research_cli
    live(monkeypatch)
    control = SimpleNamespace(remaining_seconds=lambda: 120)
    before = deepcopy(catalog)
    with scope.activated(parse(['--researcher', 'anthropic/test-cli@max'])):
        value = scope.pinned('researcher')
        argv, _, _ = research_cli.command(value, tmp_path, 'Research only', control, 4)
        assert argv[argv.index('--effort')+1] == 'max'
        assert argv[argv.index('--tools')+1] == 'WebSearch,WebFetch'
        assert '--strict-mcp-config' in argv
    assert catalog == before


def test_research_projection_never_mutates_saved_state_or_allows_route_fallback(catalog, monkeypatch, tmp_path):
    from relay import web_research, research_tools
    live(monkeypatch)
    inner = SimpleNamespace(names=(), tool_specs_json='{}')
    state = {'research': {'routes': {'old': catalog['test-cli']}, 'route_order': ['old']}}
    before = deepcopy(state)
    observed = []
    def investigate(self, request, args, control):
        observed.append((deepcopy(self.state), args))
        return {'status': 'FAILED'}
    monkeypatch.setattr(research_tools.ResearchTools, 'investigate', investigate)
    tools = web_research.research_tools(inner, tmp_path, state)
    with scope.activated(parse(['--researcher', 'anthropic/test-cli@max'])):
        result = tools.investigate(None, {'question': 'Find evidence'}, None)
        assert result['effort'] == 'max'
        assert observed[0][1]['route'] == 'test-cli'
        assert observed[0][0]['research']['route_order'] == ['test-cli']
        with pytest.raises(ValueError, match='conflicts'):
            tools.investigate(None, {'question': 'Find evidence', 'route': 'old'}, None)
    assert state == before

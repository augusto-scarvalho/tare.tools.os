"""Executed-route decision records: offline, deterministic, no inferred alternatives."""
from types import SimpleNamespace

import pytest

from relay import execution_scope, native_plan, native_routes, planner_policy, turn_engine

COUNTERFACTUAL = 'No performance or savings observed for non-executed deployments.'


def dep(name):
    return {'id': name, 'model': name, 'native_profile': {'model': name}}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(native_plan, 'route', lambda d, reason: {'deployment': d['id'], 'reason': reason})


def rank(monkeypatch, names, candidates, name='ranked'):
    seen = []
    def fake(*args, **kwargs):
        seen.append(args)
        return [dep(n) for n in names], {'reason': 'AUTO', 'candidates': candidates}
    monkeypatch.setattr(native_routes, name, fake)
    return seen


def cand(name, eligible=True, reason='OK'):
    return {'catalog': name, 'selected_deployment': name, 'eligible': eligible, 'reason': reason}


def check_shape(decision):
    assert decision['schema'] == 'tare.tools/executed-route-decision/1'
    assert decision['policy'] == 'DETERMINISTIC' and decision['ope_supported'] is False
    assert decision['unknown_alternatives'] == 'NOT_RECORDED'
    assert decision['counterfactual'] == COUNTERFACTUAL
    assert 'confidence' not in decision and 'score' not in decision
    values = list(decision['propensity'].values())
    assert all(type(v) is int and v in (0, 1) for v in values)
    assert sum(values) == (1 if decision['executed_deployment'] else 0)


def test_helper_without_executor_has_empty_propensity():
    d = native_routes.executed_decision('AUTOMATIC', None, ['a', 'b'])
    check_shape(d)
    assert d['executed_deployment'] is None and d['propensity'] == {} and d['fallback'] is None


def test_helper_fallback_lists_first_executed_and_skipped():
    d = native_routes.executed_decision('AUTOMATIC', 'b', ['a', 'b', 'c'], [('a', 'WHY')])
    check_shape(d)
    assert d['propensity'] == {'b': 1, 'c': 0}
    assert d['fallback'] == {'first_ranked': 'a', 'executed': 'b', 'skipped': [{'deployment': 'a', 'reason': 'WHY'}]}
    assert native_routes.executed_decision('AUTOMATIC', 'a', ['a', 'b'])['fallback'] is None


def test_configured_route(monkeypatch):
    chosen, receipt = native_plan.select_route(dep('mine'), 'worker', 'CFG')
    assert chosen['id'] == 'mine' and receipt['reason'] == 'CFG'
    d = receipt['executed_decision']
    check_shape(d)
    assert d['mode'] == 'CONFIGURED' and d['propensity'] == {'mine': 1} and d['fallback'] is None


def test_configured_planner_uses_effective_id(monkeypatch):
    monkeypatch.setattr(planner_policy, 'effective', lambda c: dep('effective'))
    chosen, receipt = native_plan.select_route(dep('mine'), 'planner', 'CFG')
    assert chosen['id'] == 'effective'
    assert receipt['executed_decision']['propensity'] == {'effective': 1}


def test_automatic_records_all_eligible(monkeypatch):
    seen = rank(monkeypatch, ['a', 'b'], [cand('a'), cand('b'), cand('x', False, 'OFFLINE')])
    chosen, receipt = native_plan.select_route('auto', 'worker', 'AUTO')
    assert chosen['id'] == 'a' and len(seen) == 1
    d = receipt['executed_decision']
    check_shape(d)
    assert d['mode'] == 'AUTOMATIC' and d['propensity'] == {'a': 1, 'b': 0} and d['fallback'] is None


def test_automatic_planner_fallback_records_skipped(monkeypatch):
    rank(monkeypatch, ['a', 'b'], [cand('a'), cand('b')])
    def effective(c):
        if c['id'] == 'a':
            raise ValueError('minimum reasoning is high')
        return c
    monkeypatch.setattr(planner_policy, 'effective', effective)
    chosen, receipt = native_plan.select_route('auto', 'planner', 'AUTO')
    d = receipt['executed_decision']
    check_shape(d)
    assert chosen['id'] == 'b' and d['propensity'] == {'b': 1}
    assert d['fallback'] == {'first_ranked': 'a', 'executed': 'b',
                             'skipped': [{'deployment': 'a', 'reason': 'PLANNER_REQUIRES_HIGH'}]}


def test_unavailable_paths_have_no_executor(monkeypatch):
    rank(monkeypatch, [], [])
    chosen, receipt = native_plan.select_route('auto', 'worker', 'AUTO')
    assert chosen is None and receipt['reason'] == 'NO_ELIGIBLE_NATIVE_ROLE'
    d = receipt['executed_decision']
    check_shape(d)
    assert d['executed_deployment'] is None and d['propensity'] == {}
    rank(monkeypatch, ['a'], [cand('a')])
    monkeypatch.setattr(planner_policy, 'effective', lambda c: (_ for _ in ()).throw(ValueError('low')))
    chosen, receipt = native_plan.select_route('auto', 'planner', 'AUTO')
    assert chosen is None and receipt['reason'] == 'PLANNER_REQUIRES_HIGH'
    d = receipt['executed_decision']
    check_shape(d)
    assert d['executed_deployment'] is None and d['propensity'] == {} and d['fallback'] is None


def test_plan_auditor_excludes_non_independent(monkeypatch):
    seen = rank(monkeypatch, ['ok'], [cand('ok'), cand('same', False, 'AUDITOR_NOT_INDEPENDENT')], 'reviewers')
    producer = dep('producer')
    chosen, receipt = native_plan.select_route('auto', 'plan-auditor', 'AUTO', producer)
    assert seen == [(producer, 'plan-auditor')]
    d = receipt['executed_decision']
    check_shape(d)
    assert chosen['id'] == 'ok' and d['propensity'] == {'ok': 1}


def temporary(monkeypatch, unavailable, names):
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope(prefer='api'))
    monkeypatch.setattr(execution_scope, 'planner_candidates', lambda saved: [(n, None) for n in names])
    monkeypatch.setattr(turn_engine, 'available',
                        lambda name, **kw: (None, 'offline') if name in unavailable else (dep(name), None))
    monkeypatch.setattr(planner_policy, 'effective', lambda c: c)


def test_temporary_policy_records_only_visited(monkeypatch):
    temporary(monkeypatch, {'a'}, ['a', 'b', 'c'])
    chosen, receipt = native_plan.select_route('auto', 'planner', 'AUTO')
    d = receipt['executed_decision']
    check_shape(d)
    assert chosen['id'] == 'b' and d['mode'] == 'TEMPORARY_POLICY' and d['propensity'] == {'b': 1}
    assert d['fallback'] == {'first_ranked': 'a', 'executed': 'b',
                             'skipped': [{'deployment': 'a', 'reason': 'UNAVAILABLE'}]}


def test_temporary_policy_records_actual_executor_not_alias(monkeypatch):
    temporary(monkeypatch, {'gone'}, ['gone', 'catalog-alias'])
    monkeypatch.setattr(turn_engine, 'available',
                        lambda name, **kw: (None, 'offline') if name == 'gone' else (dep('actual-id'), None))
    chosen, receipt = native_plan.select_route('auto', 'planner', 'AUTO')
    d = receipt['executed_decision']
    check_shape(d)
    assert receipt['deployment'] == 'actual-id' and d['executed_deployment'] == 'actual-id'
    assert d['propensity'] == {'actual-id': 1}
    assert d['fallback'] == {'first_ranked': 'gone', 'executed': 'actual-id',
                             'skipped': [{'deployment': 'gone', 'reason': 'UNAVAILABLE'}]}


def test_temporary_policy_without_eligible_planner(monkeypatch):
    temporary(monkeypatch, {'a'}, ['a'])
    chosen, receipt = native_plan.select_route('auto', 'planner', 'AUTO')
    assert chosen is None and receipt['reason'] == 'NO_ELIGIBLE_TEMPORARY_PLANNER'
    d = receipt['executed_decision']
    check_shape(d)
    assert d['executed_deployment'] is None and d['propensity'] == {}

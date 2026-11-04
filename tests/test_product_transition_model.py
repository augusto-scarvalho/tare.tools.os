"""Recurring executable bounded qualification; no solver or graph artifact."""
import importlib.util
from pathlib import Path
import sys
import pytest

path = Path(__file__).resolve().parents[1]/'scripts/ops/model_product_transition.py'
spec = importlib.util.spec_from_file_location('product_transition_model', path)
model = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model
spec.loader.exec_module(model)


def test_bounded_transition_and_meaningful_negative_controls():
    baseline = model.explore()
    assert baseline['ok'], baseline
    assert baseline['goals'] > 0 and baseline['states'] > 100
    for guard in model.MUTANTS:
        result = model.explore(guard)
        assert not result['ok'], (guard, result)
        assert len(result['witness']) <= 20, result


def test_bad_states_are_representable_and_old_health_is_not_upgrade():
    from dataclasses import replace
    s = model.State()
    assert model.violation(replace(s, current=2))
    assert model.violation(replace(s, owners=3))
    assert s.current == s.serving == 0
    assert not model.explore('upgrade')['ok']


def test_compact_summary(capsys):
    import json
    assert model.main() == 0
    output = capsys.readouterr().out
    assert len(output) < 10000
    assert json.loads(output)['work'] == 'I2I-06 / issue26'


@pytest.mark.parametrize('bound,value', [('LIMIT', 1), ('SECONDS', -1)])
def test_incomplete_exploration_never_reports_success(monkeypatch, bound, value):
    monkeypatch.setattr(model, bound, value)
    with pytest.raises(RuntimeError, match='qualification incomplete'):
        model.explore()


def test_failed_manual_target_never_automatically_undoes_operator_receipt():
    from dataclasses import replace
    initial = replace(model.State(), current=2, previous=1, receipt_from=1,
                      receipt_to=2, ready=7, trial=7, serving=2, operation='update')
    manual = next(n for label, n, _ in model.edges(initial)
                  if label == 'manual rollback/pause')
    assert manual.current == 1 and manual.operation == 'rollback' and manual.paused
    resumed = next(n for label, n, _ in model.edges(replace(manual, barrier=1))
                   if label == 'explicit resume')
    assert not resumed.paused and resumed.barrier == 0
    assert resumed.operation == 'rollback'
    for state in (manual, resumed):
        assert not any('replay B failed' in label for label, _, _ in model.edges(state))
        restored = next(n for label, n, _ in model.edges(state)
                        if label == 'manual rollback/pause')
        assert restored.current == 2 and restored.operation == 'rollback'

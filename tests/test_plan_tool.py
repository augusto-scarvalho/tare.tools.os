"""Ways of working, phase 1: the session's plan, checked without a model and kept in its ledger."""
import json

import pytest

from relay import delegation, native_agent, work_forms
from test_native_agent import Client, deployment, setup  # noqa: F401 (fixture)


def unit(key, *paths, **extra):
    return {'key': key, 'title': key, 'target_paths': list(paths), 'criteria': ['done'], **extra}


def test_a_sound_plan_has_no_problems(setup):
    config, _, root = setup
    (root/'tests').mkdir(); (root/'tests'/'test_a.py').write_text('')
    plan = {'units': [unit('first', 'a.py', verifiers=['unit'], test_paths=['tests/test_a.py', 'tests/test_a.py::test_b']),
                      unit('second', 'new.py', depends_on=['first'])], 'criteria': ['all']}  # a new file is fine
    assert work_forms.lint(config, plan) == []


@pytest.mark.parametrize('plan,problem', [
    ({'units': [unit('a', 'a.py'), unit('b', 'a.py')], 'criteria': ['x']}, 'a and b change the same paths'),
    ({'units': [unit('a', 'a.py', verifiers=['lint'])], 'criteria': ['x']}, 'no verifier named lint'),
    ({'units': [unit('a', 'a.py', test_paths=['tests/test_nope.py'])], 'criteria': ['x']}, 'does not exist'),
    ({'units': [unit('a', 'a.py', test_paths=['a.py::x y'])], 'criteria': ['x']}, 'does not exist'),
    ({'units': [unit('a', 'a.py', depends_on=['b']), unit('b', 'b.py', depends_on=['a'])], 'criteria': ['x']}, 'cycle'),
    ({'units': [unit('A b', 'a.py')], 'criteria': ['x']}, 'lowercase letters'),
    ({'units': [unit('a', '../a.py')], 'criteria': ['x']}, 'not a relative literal path'),
    ({'units': [unit('a', 'missing/dir/a.py')], 'criteria': ['x']}, 'nor its folder exists'),
    ({'units': [unit(f'u{i}', f'f{i}.py') for i in range(9)], 'criteria': ['x']}, 'Give 1 to 8 units'),
    ({'units': [unit('a', 'a.py')], 'criteria': []}, 'done criteria of the whole change'),
])
def test_the_lint_names_what_is_wrong(setup, plan, problem):
    config, _, _ = setup
    assert any(problem in p for p in work_forms.lint(config, plan))


def test_units_with_several_paths_each_are_compared_path_by_path(setup):
    # Found by the planning bench: overlap() reused a loop name and broke on a second path, and the plan tool's
    # AttributeError interrupted the session instead of being refused.
    from relay.work_parallel import overlap
    assert not overlap(['a.py', 'B/x.py'], ['b/y.py', 'c.py']) and overlap(['a.py', 'b'], ['c.py', 'B/x.py'])
    config, _, root = setup
    (root/'b.py').write_text('')
    plan = {'units': [unit('one', 'a.py', 'new_a.py'), unit('two', 'b.py', 'new_b.py')], 'criteria': ['x']}
    assert work_forms.lint(config, plan) == []


def test_the_session_plans_into_its_ledger_when_the_experiment_is_on(setup, tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_DELEGATION_POLICY')
    monkeypatch.delenv('TARE_WORK_FORM_SHADOW')
    config, custody, root = setup
    plan = {'units': [unit('first', 'a.py', verifiers=['unit'])], 'criteria': ['a.py holds 2']}
    steps = [('plan', plan), ('tare_deliver', {'summary': 'Planned.'})]
    parent = Client(steps=list(steps))
    native_agent.run_session(config, deployment(), custody, 'Plan it.', client=parent, local_counter=parent,
                             limits={'max_model_calls': 4})
    assert not (custody/'work.json').exists()  # off by default: no plan tool
    delegation.save_experiments(plan_tool=True)
    other = tmp_path/'other-session'
    parent = Client(steps=list(steps))
    result = native_agent.run_session(config, deployment(), other, 'Plan it.', client=parent, local_counter=parent,
                                      limits={'max_model_calls': 4})
    ledger = json.loads((other/'work.json').read_text(encoding='utf-8'))
    assert ledger['problems'] == [] and ledger['units'] == {'first': {'state': 'PENDING', 'paths': ['a.py']}}
    assert result['status'] == 'PROPOSED'
    rows = [json.loads(line) for line in (tmp_path/'storage'/'serve'/'work-form.jsonl').read_text().splitlines()]
    assert [r['form'] for r in rows] == ['direct', 'plan']
    assert rows[1]['units'] == {'PENDING': 1} and delegation.digest()['work_forms']['units'] == {'PENDING': 1}


def test_a_plan_written_again_keeps_what_its_units_went_through(setup, tmp_path):
    # Found by the phase 3 comparison: a session re-planned after its first unit was applied, the ledger
    # forgot it, and R3's count of failures could be reset the same way.
    config, _, _ = setup
    custody = tmp_path/'custody'; custody.mkdir()
    assert work_forms.record_plan(custody, config, {'units': [unit('a', 'a.py'), unit('b', 'b.py')],
                                                    'criteria': ['x']})['status'] == 'PLANNED'
    ledger = json.loads((custody/'work.json').read_text(encoding='utf-8'))
    ledger['units']['a'] = {'state': 'APPLIED', 'history': [{'verdict': 'VERIFIED'}]}
    ledger['units']['b'] = {'state': 'NOT_VERIFIED', 'history': [{'verdict': 'NOT_VERIFIED'}]}
    (custody/'work.json').write_text(json.dumps(ledger), encoding='utf-8')
    result = work_forms.record_plan(custody, config, {'units': [unit('b', 'b.py'), unit('c', 'c.py')], 'criteria': ['x']})
    units = json.loads((custody/'work.json').read_text(encoding='utf-8'))['units']
    assert result['units'] == ['b', 'c']
    assert units == {'a': ledger['units']['a'], 'b': {**ledger['units']['b'], 'paths': ['b.py']},  # a: dropped, kept as it was
                     'c': {'state': 'PENDING', 'paths': ['c.py']}}


def test_a_planned_unit_with_a_dependency_resolves_once_the_dependency_is_applied(setup, tmp_path):
    # The walkthrough of bd6ee62 (2026-10-07): every unit with depends_on was refused as depending on an unknown
    # unit, because the lint saw the unit alone. It resolves once its dependency is applied, not before.
    from relay import work_units
    config, _, _ = setup
    custody = tmp_path/'custody'; custody.mkdir()
    plan = {'units': [unit('a', 'a.py'), unit('b', 'b.py', depends_on=['a'])], 'criteria': ['x']}
    assert work_forms.record_plan(custody, config, plan)['status'] == 'PLANNED'
    with pytest.raises(ValueError, match='after the units it depends on'):
        work_units.resolve_unit(custody, config, {'unit': 'b'})
    ledger = json.loads((custody/'work.json').read_text(encoding='utf-8'))
    ledger['units']['a'] = {'state': 'APPLIED'}
    (custody/'work.json').write_text(json.dumps(ledger), encoding='utf-8')
    assert work_units.resolve_unit(custody, config, {'unit': 'b'})['key'] == 'b'


def test_a_unit_planned_again_under_another_name_keeps_its_failures(setup, tmp_path):
    # The TUI walkthrough of 2026-10-07: a unit that failed twice (R3) came back under a new key in a new plan and
    # got two more children. A unit is what it changes: the same paths carry the history.
    config, _, _ = setup
    custody = tmp_path/'custody'; custody.mkdir()
    assert work_forms.record_plan(custody, config, {'units': [unit('tui', 'a.py')], 'criteria': ['x']})['status'] == 'PLANNED'
    ledger = json.loads((custody/'work.json').read_text(encoding='utf-8'))
    ledger['units']['tui'] = {'state': 'NOT_VERIFIED', 'paths': ['a.py'],
                              'history': [{'verdict': 'NOT_VERIFIED'}, {'verdict': 'NOT_VERIFIED'}]}
    (custody/'work.json').write_text(json.dumps(ledger), encoding='utf-8')
    assert work_forms.record_plan(custody, config, {'units': [unit('tui-fix', 'a.py')], 'criteria': ['x']})['status'] == 'PLANNED'
    units = json.loads((custody/'work.json').read_text(encoding='utf-8'))['units']
    assert units['tui-fix']['state'] == 'NOT_VERIFIED' and len(units['tui-fix']['history']) == 2
    assert work_forms.record_plan(custody, config, {'units': [unit('other', 'b.py')], 'criteria': ['x']})['status'] == 'PLANNED'
    units = json.loads((custody/'work.json').read_text(encoding='utf-8'))['units']
    assert units['other'] == {'state': 'PENDING', 'paths': ['b.py']}  # other paths: a fresh unit

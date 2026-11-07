"""Per-file pytest ceilings use simulated elapsed time, never real waits."""
from copy import deepcopy
import subprocess
import sys
from types import SimpleNamespace

import pytest

from relay import work_units
from relay.execution_scope import Scope, activated


def compile_checks(tmp_path, monkeypatch, *, discovered=False, default=False):
    monkeypatch.setattr('relay.turn_engine.project_python', lambda path: sys.executable)
    monkeypatch.setattr('relay.work_forms.ecosystems', lambda path: (
        [{'name': 'pytest', 'runner': sys.executable, 'narrow': True}] if discovered else []))
    paths = ['tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py']
    config = {'conversation_flow': True, 'repositories': {'repo': str(tmp_path)},
              'verifiers': {'tests': {'repository': 'repo',
                                      'argv': [sys.executable, '-m', 'pytest', '-q']}}}
    unit = {'title': 'Fixture', 'target_paths': ['a.py'], 'criteria': ['works'],
            'verifiers': ['tests'] if default else [], 'test_paths': paths}
    before = deepcopy((config, unit))
    commands = work_units.checks(config, unit, 'repo', tmp_path)
    assert (config, unit) == before
    assert list(commands) == ['pytest-tests:' + path for path in paths]
    return config, unit, commands


def simulate(tree, commands, durations, *, red=False, managed=False):
    calls = []

    def port(argv, **options):
        paths = {work_units.test_file(arg) for arg in argv if work_units.test_file(arg) in durations}
        calls.append((paths, options['timeout']))
        duration = sum(durations[path] for path in paths)
        timeout = options['timeout']
        if timeout is not None and duration > timeout:
            if not managed:
                raise subprocess.TimeoutExpired(argv, timeout)
            return SimpleNamespace(returncode=-1, stdout='', stderr='',
                                   execution_limitation='Check timeout; process tree cleanup verified.')
        return SimpleNamespace(returncode=int(red), stdout='x' * 50000, stderr='')

    token = work_units.CHECK_PORT.set(port)
    try:
        state, output = work_units.run_checks(tree, commands)
    finally:
        work_units.CHECK_PORT.reset(token)
    return state, output, calls


@pytest.mark.parametrize('red', [False, True])
@pytest.mark.parametrize('discovered,default', [(False, False), (False, True), (True, True)])
def test_three_files_exceed_combined_budget(tmp_path, monkeypatch, red, discovered, default):
    _, unit, commands = compile_checks(tmp_path, monkeypatch, discovered=discovered, default=default)
    durations = dict.fromkeys(unit['test_paths'], 120)
    aggregate = {'old': work_units.narrowed('pytest', sys.executable, unit['test_paths'])}
    assert simulate(tmp_path, aggregate, durations)[0] == 'inconclusive'
    state, output, calls = simulate(tmp_path, commands, durations, red=red)
    assert state == ('red' if red else 'green')
    assert [timeout for _, timeout in calls] == [300, 300, 300]
    assert all(len(paths) == 1 for paths, _ in calls)
    assert all(f'{name}: exit {int(red)}' in output for name in commands)


@pytest.mark.parametrize('managed', [False, True])
def test_single_file_timeout_is_named(tmp_path, monkeypatch, managed):
    _, unit, commands = compile_checks(tmp_path, monkeypatch)
    durations = dict(zip(unit['test_paths'], [120, 301, 120]))
    state, output, calls = simulate(tmp_path, commands, durations, managed=managed)
    assert state == 'inconclusive' and len(calls) == 2
    assert 'pytest-tests:tests/test_a.py: exit 0' in output
    assert 'pytest-tests:tests/test_b.py:' in output and 'timeout' in output
    assert 'pytest-tests:tests/test_c.py: not executed' in output
    assert 'pytest-tests:tests/test_c.py: exit 0' not in output


def test_unlimited_time_applies_to_every_file(tmp_path, monkeypatch):
    _, unit, commands = compile_checks(tmp_path, monkeypatch, default=True)
    with activated(Scope(unlimited=('time',))):
        state, _, calls = simulate(tmp_path, commands, dict.fromkeys(unit['test_paths'], 301))
    assert state == 'green'
    assert [timeout for _, timeout in calls] == [None, None, None]


def test_same_plan_keeps_identity(tmp_path, monkeypatch):
    config, unit, commands = compile_checks(tmp_path, monkeypatch, default=True)
    monkeypatch.setattr(work_units, 'lint', lambda *args: [])
    inline = work_units.resolve_unit(tmp_path, config, unit)
    saved = {**inline, 'key': 'saved-plan', 'verification_contract_digest': 'fixed-contract'}
    monkeypatch.setattr(work_units, 'read_ledger', lambda path: {'plan': {'units': [saved]}})
    for _ in range(2):
        assert work_units.checks(config, saved, 'repo', tmp_path) == commands
        assert work_units.resolve_unit(tmp_path, config, unit) == inline
        assert work_units.resolve_unit(tmp_path, config, {'unit': 'saved-plan'}) == saved
    assert 'verification_contract_digest' not in inline


def test_node_groups_and_name_collision(tmp_path, monkeypatch):
    config, unit, _ = compile_checks(tmp_path, monkeypatch)
    name = 'pytest-tests:tests/test_a.py'
    config['verifiers'][name] = {'repository': 'repo', 'argv': ['custom', 'check']}
    unit.update(verifiers=[name], test_paths=['tests/test_a.py::test_one',
                                             'tests/test_b.py', 'tests/test_a.py::test_two'])
    commands = work_units.checks(config, unit, 'repo', tmp_path)
    assert commands[name] == ['custom', 'check']
    assert list(commands) == [name, name + '#2', 'pytest-tests:tests/test_b.py']
    argv = commands[name + '#2']
    assert [arg for arg in argv if '::' in arg] == ['tests/test_a.py::test_one', 'tests/test_a.py::test_two']
    assert commands == work_units.checks(config, unit, 'repo', tmp_path)

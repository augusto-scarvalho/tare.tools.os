"""Offline public-contract tests for EXP-5; no subprocesses or provider calls."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.research.compare_evidence_scheduling import (
    MAX_INPUT_BYTES, MAX_REPORT_BYTES, SCHEMA, compare, main,
)

SAMPLE = Path(__file__).resolve().parents[1] / 'docs/operations/EVIDENCE_SCHEDULING_CI_SAMPLE_20261021.json'


def task(key, duration, deps=(), pool='cpu'):
    return {'id': key, 'depends_on': list(deps), 'pool': pool,
            'duration_ms': duration, 'source_ref': 'fixture:' + key}


def replay(tasks=None, required=None, capacities=None):
    return {'schema': SCHEMA, 'basis': 'fixture', 'capacities': capacities or {'cpu': 1},
            'tasks': tasks if tasks is not None else [task('a', 1)],
            'required': required if required is not None else ['a']}


def timeline(result):
    return {entry['id']: (entry['start_ms'], entry['end_ms']) for entry in result['timeline']}


def assert_feasible(data, report):
    """Check externally visible schedules, not implementation helper state."""
    for result in report['policies'].values():
        times = timeline(result)
        assert set(times) == {row['id'] for row in data['tasks']}
        assert len(result['timeline']) == len(data['tasks'])
        for row in data['tasks']:
            start, end = times[row['id']]
            assert end - start == row['duration_ms']
            assert start >= 0
            assert all(times[dep][1] <= start for dep in row['depends_on'])
        for pool, capacity in data['capacities'].items():
            rows = [row for row in data['tasks'] if row['pool'] == pool]
            for instant in {value for row in rows for value in times[row['id']]}:
                assert sum(times[row['id']][0] <= instant < times[row['id']][1]
                           for row in rows) <= capacity
        assert result['total_service_ms'] == sum(row['duration_ms'] for row in data['tasks'])
        assert result['makespan_ms'] == max(end for start, end in times.values())
        assert result['time_to_required_evidence_ms'] == max(times[key][1] for key in data['required'])
        assert result['mean_completion_time_ms'] == pytest.approx(
            sum(end for start, end in times.values()) / len(times))


def test_actual_evidence_throughput_tradeoff_and_complete_background():
    data = replay([task('small', 1), task('background', 10), task('prep', 6),
                   task('gate', 1, ['prep']), task('other-gate', 3)], ['gate', 'other-gate'])
    original = deepcopy(data)
    report = compare(data)
    assert data == original
    assert report == compare(data)
    assert_feasible(data, report)
    plan, shortest, critical = (report['policies'][key] for key in
                                ('plan_order', 'shortest_ready', 'evidence_critical_path'))
    assert [item['time_to_required_evidence_ms'] for item in (plan, shortest, critical)] == [21, 11, 10]
    assert shortest['mean_completion_time_ms'] < critical['mean_completion_time_ms']
    assert timeline(critical)['other-gate'] == (6, 9)
    assert timeline(critical)['gate'] == (9, 10)
    assert all(item['makespan_ms'] == 21 for item in (plan, shortest, critical))
    assert plan['contended_selection_opportunities'][0] == {
        'time_ms': 0, 'pool': 'cpu', 'eligible_count': 4, 'available_slots': 1}


def test_nonrequired_descendant_does_not_inflate_evidence_path():
    data = replay([task('a', 1), task('huge-background', 100, ['a']), task('b', 5)], ['a', 'b'])
    report = compare(data)
    assert timeline(report['policies']['evidence_critical_path']) == {
        'a': (5, 6), 'huge-background': (6, 106), 'b': (0, 5)}
    assert_feasible(data, report)


def test_longest_remaining_evidence_path_not_just_immediate_duration():
    # Deliberately not topological input order; the owner permits this.
    data = replay([task('gate', 10, ['prep']), task('medium', 5), task('prep', 1)],
                  ['gate', 'medium'])
    report = compare(data)
    assert_feasible(data, report)
    assert timeline(report['policies']['plan_order'])['medium'] == (0, 5)
    assert timeline(report['policies']['evidence_critical_path']) == {
        'gate': (1, 11), 'medium': (11, 16), 'prep': (0, 1)}


def test_multislot_capacity_independent_pool_progress_and_stable_ties():
    data = replay([task('a', 5), task('b', 5), task('c', 5),
                   task('independent', 2, pool='io')], ['a', 'b', 'c'], {'cpu': 2, 'io': 1})
    report = compare(data)
    assert_feasible(data, report)
    for result in report['policies'].values():
        assert timeline(result) == {'a': (0, 5), 'b': (0, 5), 'c': (5, 10), 'independent': (0, 2)}
        assert result['contended_selection_count'] == 1
        assert result['contended_selection_opportunities'][0]['available_slots'] == 2


def test_simultaneous_completions_release_join_and_zero_time_chain():
    data = replay([task('a', 3), task('b', 3), task('join', 0, ['a', 'b']),
                   task('last', 0, ['join']), task('background', 8, pool='io')],
                  ['last'], {'cpu': 2, 'io': 1})
    report = compare(data)
    assert_feasible(data, report)
    for result in report['policies'].values():
        assert timeline(result)['join'] == (3, 3)
        assert timeline(result)['last'] == (3, 3)
        assert result['time_to_required_evidence_ms'] == 3
        assert result['makespan_ms'] == 8


@pytest.mark.parametrize('missing', [False, True])
def test_unknown_background_duration_vetoes_all_timing(missing):
    data = replay([task('a', 1), task('background', None)])
    if missing:
        del data['tasks'][1]['duration_ms']
    report = compare(data)
    assert report['status'] == 'INSUFFICIENT_DATA'
    assert report['policies'] == {}
    assert report['gaps'] == [{'id': 'background', 'field': 'duration_ms', 'reason': 'unknown duration'}]
    # Full validation still applies even when an earlier task has an unknown duration.
    data['tasks'][0]['source_ref'] = ''
    with pytest.raises(ValueError, match='source_ref'):
        compare(data)


@pytest.mark.parametrize('duration', [True, False, -1, 0.5, float('nan'), float('inf'),
                                     -float('inf'), '2', [], {}, 10**12 + 1, 10**1000])
def test_invalid_durations(duration):
    with pytest.raises(ValueError):
        compare(replay([task('a', duration)]))


def test_integral_float_resolution_is_exact():
    data = replay([task('a', 2.0)])
    assert compare(data)['policies']['plan_order']['makespan_ms'] == 2


@pytest.mark.parametrize('field,value', [
    ('schema', 'unsupported'), ('basis', 'live'), ('capacities', {}),
    ('capacities', {'cpu': True}), ('capacities', {'cpu': 0}),
    ('capacities', {'cpu': -1}), ('capacities', {'cpu': 1.5}),
    ('capacities', {' ': 1}), ('required', []), ('required', ['a', 'a']),
    ('required', ['missing']), ('required', [True]), ('required', 'a'),
    ('tasks', []), ('provenance', 'authority'), ('extra', True),
])
def test_invalid_top_level_evidence(field, value):
    data = replay()
    data[field] = value
    with pytest.raises(ValueError):
        compare(data)


@pytest.mark.parametrize('field,value', [('pool', 'missing'), ('pool', []), ('pool', ''),
                                       ('source_ref', ''), ('source_ref', None),
                                       ('source_ref', '   '), ('command', 'never execute')])
def test_invalid_task_evidence(field, value):
    data = replay()
    data['tasks'][0][field] = value
    with pytest.raises(ValueError):
        compare(data)


@pytest.mark.parametrize('tasks,error', [
    ([task('a', 1), task('a', 2)], 'WORKFLOW_DUPLICATE_NODE_ID'),
    ([task('a', 1, ['missing'])], 'WORKFLOW_DEPENDENCY_ABSENT'),
    ([task('a', 1, ['b']), task('b', 2, ['a'])], 'WORKFLOW_DEPENDENCY_CYCLE'),
    ([task('a', 1, ['a'])], 'WORKFLOW_DEPENDENCY_CYCLE'),
    ([task('a', 1, ['b', 'b']), task('b', 1)], 'WORKFLOW_DUPLICATE_DEPENDENCY'),
    ([dict(task('a', 1), depends_on='a')], 'WORKFLOW_DEPENDENCIES_INVALID'),
])
def test_malformed_dags_rejected_by_owner_ir(tasks, error):
    with pytest.raises(ValueError, match=error):
        compare(replay(tasks))


def test_real_ci_sample_is_observed_but_not_contended():
    data = json.loads(SAMPLE.read_text(encoding='utf-8'))
    report = compare(data)
    assert report['basis'] == 'observed_durations'
    assert_feasible(data, report)
    results = list(report['policies'].values())
    assert results[0] == results[1] == results[2]
    assert any('Zero contended choices' in limit for limit in report['limits'])
    for result in results:
        assert result['contended_selection_count'] == 0
        assert result['makespan_ms'] == result['time_to_required_evidence_ms'] == 2530000
        assert result['total_service_ms'] == 3847000
        assert result['mean_completion_time_ms'] == 1923500
    other = deepcopy(data)
    other['provenance']['head_sha'] = 'not authentication'
    assert compare(other)['input_identity'] != report['input_identity']
    assert compare(other)['policies'] == report['policies']


def test_pool_mapping_order_does_not_change_identical_input_identity_report():
    data = replay([task('a', 3, pool='cpu'), task('b', 2, pool='cpu'),
                   task('c', 4, pool='gpu'), task('d', 1, pool='gpu')],
                  capacities={'cpu': 1, 'gpu': 1})
    reordered = deepcopy(data)
    reordered['capacities'] = dict(reversed(list(data['capacities'].items())))
    first, second = compare(data), compare(reordered)
    assert first['input_identity'] == second['input_identity']
    assert first == second


def test_cli_real_sample_and_report_bound(capsys):
    assert main([str(SAMPLE)]) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert len(output.out.encode()) <= MAX_REPORT_BYTES
    assert json.loads(output.out) == compare(json.loads(SAMPLE.read_text(encoding='utf-8')))


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{}', b'\xff', b'{"a":NaN}',
                                b'{"schema":1,"schema":2}', b' ' * (MAX_INPUT_BYTES + 1)],
                         ids=['malformed', 'array', 'missing-fields', 'invalid-utf8',
                              'nonfinite', 'duplicate-key', 'over-byte-limit'])
def test_cli_malformed_input_clear_nonzero(tmp_path, capsys, raw):
    path = tmp_path / 'input.json'
    path.write_bytes(raw)
    assert main([str(path)]) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err.startswith('evidence-scheduling: ')


def test_cli_missing_file_and_usage(tmp_path, capsys):
    assert main([str(tmp_path / 'absent.json')]) == 2
    assert 'evidence-scheduling:' in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code != 0


def test_input_bounds_and_maximum_dag():
    data = replay([task(str(index), 1) for index in range(128)], ['127'])
    report = compare(data)
    assert_feasible(data, report)
    data['tasks'].append(task('129', 1))
    with pytest.raises(ValueError, match='128'):
        compare(data)
    data = replay()
    data['provenance'] = {'padding': 'x' * MAX_INPUT_BYTES}
    with pytest.raises(ValueError, match='256 KiB'):
        compare(data)


def test_cli_exact_raw_byte_limit(tmp_path, capsys):
    raw = json.dumps(replay()).encode()
    path = tmp_path / 'bounded.json'
    path.write_bytes(raw + b' ' * (MAX_INPUT_BYTES - len(raw)))
    assert main([str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'COMPARED'
    path.write_bytes(path.read_bytes() + b' ')
    assert main([str(path)]) == 2
    assert '256 KiB' in capsys.readouterr().err

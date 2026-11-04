"""EXP-5: bounded offline closed-batch replay, never a Work dispatcher."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from relay.workflow_ir import MAX_NODES, OPERATOR_REF, compile_workflow

SCHEMA = 'tare.tools/evidence-scheduling-replay/1'
MAX_INPUT_BYTES = 256 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_DURATION_MS = 10**12
POLICIES = ('plan_order', 'shortest_ready', 'evidence_critical_path')
ASSUMPTIONS = [
    'Counterfactual all-success closed batch; all roots admitted at t=0.',
    'Nonpreemptive tasks; dependency completion releases work; fixed pool slots.',
    'Identical work and fixed integer-millisecond service times for every policy.',
    'All simultaneous completions are applied before selecting more work.',
]
LIMITS = [
    'Observed durations are retrospective knowledge, not perfect live predictions.',
    'Fixed total service is not paid-token or compute savings.',
    'No inference about failures, arrivals, human decision time, cache/model quality or the entire Work population.',
    'Basis, source references, provenance and hashes do not authenticate observations.',
    'No commands executed, source references resolved, or production controls changed.',
]


def _json_bytes(value):
    # Reject non-JSON Python values too: compare() has the same boundary as the CLI.
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError('Input must contain only finite JSON values')
    try:
        check(value)
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('utf-8')
    except (RecursionError, OverflowError) as exc:
        raise ValueError('Input nesting or numbers exceed bounds') from exc


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _validate(data):
    encoded = _json_bytes(data)
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError('Input exceeds 256 KiB canonical JSON bound')
    fields = {'schema', 'basis', 'capacities', 'tasks', 'required'}
    if not isinstance(data, dict) or not fields <= set(data) or set(data) - fields - {'provenance'}:
        raise ValueError('Invalid replay fields')
    if data['schema'] != SCHEMA:
        raise ValueError('Unsupported replay schema')
    if data['basis'] not in ('fixture', 'observed_durations'):
        raise ValueError('Invalid basis')
    if 'provenance' in data and not isinstance(data['provenance'], dict):
        raise ValueError('Provenance must be a descriptive JSON object')
    capacities = data['capacities']
    if (not isinstance(capacities, dict) or not capacities
            or any(not _nonempty(pool) or type(slots) is not int or slots <= 0
                   for pool, slots in capacities.items())):
        raise ValueError('Invalid pools/capacities: positive integer slots required')
    tasks = data['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_NODES:
        raise ValueError('Tasks must contain 1..128 records')
    task_fields = {'id', 'depends_on', 'pool', 'duration_ms', 'source_ref'}
    for task in tasks:
        if (not isinstance(task, dict) or not task_fields - {'duration_ms'} <= set(task)
                or set(task) - task_fields):
            raise ValueError('Invalid task fields')
    # The owner, not this experiment, defines IDs, dependencies and malformed DAGs.
    plan = compile_workflow([
        {'id': task['id'], 'depends_on': task['depends_on'], 'operator_ref': OPERATOR_REF}
        for task in tasks
    ])
    for task in tasks:
        if not _nonempty(task['pool']) or task['pool'] not in capacities:
            raise ValueError('Task references an invalid pool')
        if not _nonempty(task['source_ref']):
            raise ValueError('Explicit nonempty source_ref required')
        duration = task.get('duration_ms')
        if duration is not None and (
                type(duration) not in (int, float)
                or duration < 0 or duration > MAX_DURATION_MS
                or not math.isfinite(duration) or duration != int(duration)):
            raise ValueError('duration_ms must be an integer millisecond value in 0..10^12 or null')
    required = data['required']
    ids = {node['id'] for node in plan['nodes']}
    if (not isinstance(required, list) or not required
            or any(not _nonempty(key) for key in required)
            or len(set(required)) != len(required) or not set(required) <= ids):
        raise ValueError('Required IDs must be nonempty, unique and present')
    return hashlib.sha256(encoded).hexdigest()


def _critical_scores(tasks, required):
    by_id = {task['id']: task for task in tasks}
    closure = set(required)
    pending = list(required)
    while pending:
        for dep in by_id[pending.pop()]['depends_on']:
            if dep not in closure:
                closure.add(dep)
                pending.append(dep)
    children = {key: [] for key in closure}
    for key in closure:
        for dep in by_id[key]['depends_on']:
            children[dep].append(key)
    scores = {}

    def score(key):
        if key not in scores:
            scores[key] = int(by_id[key]['duration_ms']) + max(
                (score(child) for child in children[key]), default=0)
        return scores[key]

    for key in closure:
        score(key)
    return closure, scores


def _replay(data, policy, closure, scores):
    tasks = data['tasks']
    durations = [int(task['duration_ms']) for task in tasks]
    dependencies = [set(task['depends_on']) for task in tasks]
    pending = set(range(len(tasks)))
    running, done, times = {}, set(), {}
    opportunities = []
    now = 0

    def priority(index):
        key = tasks[index]['id']
        if policy == 'shortest_ready':
            return (durations[index], index)
        if policy == 'evidence_critical_path':
            return (key not in closure, -scores.get(key, 0), index)
        return (index,)

    # Each iteration starts at least one task or completes at least one task.
    while pending or running:
        for pool in sorted(data['capacities']):
            capacity = data['capacities'][pool]
            free = capacity - sum(tasks[index]['pool'] == pool for index in running)
            if free <= 0:
                continue
            candidates = sorted((index for index in pending
                                 if tasks[index]['pool'] == pool and dependencies[index] <= done),
                                key=priority)
            if len(candidates) > free:
                opportunities.append({'time_ms': now, 'pool': pool,
                                      'eligible_count': len(candidates), 'available_slots': free})
            for index in candidates[:free]:
                pending.remove(index)
                running[index] = now + durations[index]
                times[index] = (now, running[index])
        if not running:
            raise RuntimeError('Validated DAG made no progress')
        now = min(running.values())
        completed = [index for index, end in running.items() if end == now]
        for index in completed:
            done.add(tasks[index]['id'])
            del running[index]
    return {
        'timeline': [{'id': task['id'], 'start_ms': times[index][0], 'end_ms': times[index][1]}
                     for index, task in enumerate(tasks)],
        'makespan_ms': now,
        'time_to_required_evidence_ms': max(times[index][1] for index, task in enumerate(tasks)
                                            if task['id'] in data['required']),
        'mean_completion_time_ms': sum(end for _, end in times.values()) / len(tasks),
        'total_service_ms': sum(durations),
        'contended_selection_opportunities': opportunities,
        'contended_selection_count': len(opportunities),
    }


def compare(data):
    """Validate without mutation; return retrospective replay or explicit data gaps.

    Durations are exact whole milliseconds (integral floats accepted), 0..10^12.
    Missing/null durations remain unknown; no rounding or timing imputation occurs.
    """
    identity = _validate(data)
    gaps = [{'id': task['id'], 'field': 'duration_ms', 'reason': 'unknown duration'}
            for task in data['tasks'] if task.get('duration_ms') is None]
    report = {
        'schema': 'tare.tools/evidence-scheduling-report/1',
        'basis': data['basis'],
        'input_identity': {'algorithm': 'sha256', 'encoding': 'sorted compact ASCII JSON',
                           'value': identity, 'authenticates_observations': False},
        'status': 'INSUFFICIENT_DATA' if gaps else 'COMPARED',
        'required': list(data['required']),
        'assumptions': list(ASSUMPTIONS), 'limits': list(LIMITS),
        'bounds': {'max_tasks': MAX_NODES, 'max_input_bytes': MAX_INPUT_BYTES,
                   'max_report_bytes': MAX_REPORT_BYTES, 'max_duration_ms': MAX_DURATION_MS,
                   'resolution': 'exact whole milliseconds; no rounding'},
        'gaps': gaps,
        'policies': {},
    }
    if not gaps:
        closure, scores = _critical_scores(data['tasks'], data['required'])
        report['policies'] = {policy: _replay(data, policy, closure, scores) for policy in POLICIES}
        if not any(result['contended_selection_count'] for result in report['policies'].values()):
            report['limits'].append('Zero contended choices: this cohort cannot discriminate scheduling priorities.')
    if len(_json_bytes(report)) > MAX_REPORT_BYTES:
        raise ValueError('Report exceeds 2 MiB bound')
    return report


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key[:80])
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Nonfinite JSON number: ' + value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='Local replay JSON file (at most 256 KiB)')
    args = parser.parse_args(argv)
    try:
        with Path(args.input).open('rb') as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError('Input exceeds 256 KiB byte bound')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
        output = _json_bytes(compare(data))
    except (ValueError, OSError, UnicodeError, RecursionError, OverflowError) as exc:
        print('evidence-scheduling: ' + str(exc)[:500], file=sys.stderr)
        return 2
    sys.stdout.write(output.decode('ascii') + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Offline contracts for the pure FLOW-10 IR and serial reducer."""

from copy import deepcopy
import json

import pytest

from relay.workflow_ir import compile_workflow, validate_workflow
from relay.workflow_reducer import reduce_workflow

pytestmark = pytest.mark.verifies('SPEC-OS-WORKFLOW-IR-COMPILER-001', 'AC-05')


def node(key, *dependencies):
    return {'id': key, 'operator_ref': 'work.execute/v1',
            'depends_on': list(dependencies)}


def observed(state='PENDING', **fields):
    return {'state': state, **fields}


def test_compile_preserves_priority_direction_and_owns_its_data():
    source = [node('child', 'parent'), node('independent'), node('parent')]
    before = deepcopy(source)
    plan = compile_workflow(source, revision=7)
    assert plan == {'schema': 'tare.tools/workflow-ir/1', 'revision': 7, 'nodes': before}
    assert json.loads(json.dumps(plan)) == plan
    assert validate_workflow(plan) is plan
    assert source == before
    source[0]['depends_on'].clear()
    source[1]['id'] = 'changed'
    assert plan['nodes'] == before
    plan['nodes'][2]['depends_on'].append('independent')
    assert source[2]['depends_on'] == []


@pytest.mark.parametrize('state,status', [
    ('SUCCEEDED', 'READY'), ('FAILED', 'BLOCKED'), ('CANCELLED', 'BLOCKED'),
    ('WAITING', 'WAITING'), ('UNKNOWN', 'NEEDS_RECONCILIATION'),
    ('PENDING', 'BLOCKED'), ('RECOVERABLE', 'RECOVER'),
])
def test_only_success_satisfies_dependencies(state, status):
    plan = compile_workflow([node('child', 'parent'), node('parent')])
    observations = {'child': observed(ready=True), 'parent': observed(state)}
    result = reduce_workflow(plan, observations)
    assert result['status'] == status
    if state == 'SUCCEEDED':
        assert result['next_node'] == 'child'
        assert result['ready'] == ['child']
        assert result['completed'] == ['parent']
    else:
        assert result['ready'] == []
        assert 'parent (' + state in result['blocked']['child']
        assert result['next_node'] == ('parent' if state == 'RECOVERABLE' else None)


def test_dependency_blocked_first_node_does_not_hide_independent_readiness():
    plan = compile_workflow([node('child', 'parent'), node('second'), node('parent'), node('last')])
    # Observation insertion order must not control priority.
    observations = {'last': observed(ready=True), 'parent': observed('FAILED', reason='check failed'),
                    'second': observed(ready=True), 'child': observed(ready=True)}
    result = reduce_workflow(plan, observations)
    assert result == {
        'status': 'READY', 'next_node': 'second', 'ready': ['second', 'last'],
        'blocked': {'child': 'Dependencies not succeeded: parent (FAILED: check failed)',
                    'parent': 'FAILED: check failed'},
        'completed': [], 'waiting': [],
    }


def test_recovery_is_explicit_prioritized_and_never_a_fresh_retry():
    plan = compile_workflow([node('fresh'), node('resume-one', 'failed'),
                             node('resume-two'), node('failed')])
    observations = {'fresh': observed(ready=True),
                    'resume-one': observed('RECOVERABLE', ready=False),
                    'resume-two': observed('RECOVERABLE', ready=True),
                    'failed': observed('FAILED', ready=True)}
    result = reduce_workflow(plan, observations)
    assert result['status'] == 'RECOVER'
    assert result['next_node'] == 'resume-one'
    assert result['ready'] == ['fresh']
    # Recovery does not imply success and does not authorize dependent work.
    plan = compile_workflow([node('dependent', 'resume'), node('resume')])
    result = reduce_workflow(plan, {'dependent': observed(ready=True),
                                    'resume': observed('RECOVERABLE')})
    assert result['status'] == 'RECOVER'
    assert result['next_node'] == 'resume'
    assert result['ready'] == []
    assert 'RECOVERABLE' in result['blocked']['dependent']


@pytest.mark.parametrize('ready', [False, None])
def test_pending_requires_explicit_readiness(ready):
    observation = observed(reason='not admitted')
    if ready is not None:
        observation['ready'] = ready
    result = reduce_workflow(compile_workflow([node('a')]), {'a': observation})
    assert result['status'] == 'BLOCKED'
    assert result['next_node'] is None
    assert result['ready'] == []
    assert 'not admitted' in result['blocked']['a']


def test_unknown_globally_withholds_dispatch_and_recovery():
    plan = compile_workflow([node('fresh'), node('resume'), node('uncertain'), node('done')])
    result = reduce_workflow(plan, {
        'fresh': observed(ready=True), 'resume': observed('RECOVERABLE', ready=True),
        'uncertain': observed('UNKNOWN', reason='receipt unavailable'), 'done': observed('SUCCEEDED'),
    })
    assert result['status'] == 'NEEDS_RECONCILIATION'
    assert result['next_node'] is None
    assert result['ready'] == []
    assert set(result['blocked']) == {'fresh', 'resume', 'uncertain'}
    assert 'receipt unavailable' in result['blocked']['uncertain']
    assert result['completed'] == ['done']


def test_waiting_propagates_through_dependencies_but_does_not_hide_hard_blocks():
    plan = compile_workflow([node('end', 'middle'), node('middle', 'human'), node('human')])
    observations = {'end': observed(ready=True), 'middle': observed(ready=True),
                    'human': observed('WAITING', reason='approval or inflight execution')}
    result = reduce_workflow(plan, observations)
    assert result['status'] == 'WAITING'
    assert result['next_node'] is None
    assert result['waiting'] == ['human']
    assert set(result['blocked']) == {'end', 'middle'}
    observations['end']['ready'] = False
    assert reduce_workflow(plan, observations)['status'] == 'BLOCKED'
    observations['human'] = observed('CANCELLED')
    assert reduce_workflow(plan, observations)['status'] == 'BLOCKED'


def test_waiting_does_not_hide_an_independent_ready_node():
    plan = compile_workflow([node('human'), node('independent')])
    result = reduce_workflow(plan, {'human': observed('WAITING'),
                                    'independent': observed(ready=True)})
    assert result['status'] == 'READY'
    assert result['next_node'] == 'independent'
    assert result['waiting'] == ['human']


def test_completed_empty_and_deterministic_results_do_not_mutate_inputs():
    plan = compile_workflow([node('b', 'a'), node('a')])
    observations = {'a': observed('SUCCEEDED'), 'b': observed('SUCCEEDED')}
    original = deepcopy((plan, observations))
    first = reduce_workflow(plan, observations)
    assert first == {'status': 'COMPLETED', 'next_node': None, 'ready': [],
                     'blocked': {}, 'completed': ['b', 'a'], 'waiting': []}
    assert reduce_workflow(plan, observations) == first
    assert (plan, observations) == original
    first['completed'].clear()
    assert reduce_workflow(plan, observations)['completed'] == ['b', 'a']
    assert reduce_workflow(compile_workflow([]), {})['status'] == 'COMPLETED'


def test_nonterminal_reduction_is_deterministic_and_does_not_mutate_inputs():
    plan = compile_workflow([node('child', 'parent'), node('one'), node('parent'), node('two')])
    observations = {key: observed(ready=True) for key in ('two', 'parent', 'one', 'child')}
    before = deepcopy((plan, observations))
    first = reduce_workflow(plan, observations)
    assert first['ready'] == ['one', 'parent', 'two']
    assert first['next_node'] == 'one'
    assert reduce_workflow(plan, dict(reversed(list(observations.items())))) == first
    assert (plan, observations) == before
    first['blocked'].clear()
    first['ready'].clear()
    assert reduce_workflow(plan, observations)['ready'] == ['one', 'parent', 'two']


@pytest.mark.parametrize('nodes,error', [
    (None, 'WORKFLOW_NODES_INVALID'),
    ((node('a'),), 'WORKFLOW_NODES_INVALID'),
    ([None], 'WORKFLOW_NODE_FIELDS_INVALID'),
    ([{'id': 'a'}], 'WORKFLOW_NODE_FIELDS_INVALID'),
    ([dict(node('a'), model='not-logical-ir')], 'WORKFLOW_NODE_FIELDS_INVALID'),
    ([dict(node('a'), code='print(1)')], 'WORKFLOW_NODE_FIELDS_INVALID'),
    ([node('')], 'WORKFLOW_NODE_ID_INVALID'),
    ([node('  ')], 'WORKFLOW_NODE_ID_INVALID'),
    ([node([])], 'WORKFLOW_NODE_ID_INVALID'),
    ([node('a'), node('a')], 'WORKFLOW_DUPLICATE_NODE_ID'),
    ([dict(node('a'), operator_ref='work.execute/v2')], 'WORKFLOW_UNSUPPORTED_OPERATOR'),
    ([dict(node('a'), operator_ref='shell.execute/v1')], 'WORKFLOW_UNSUPPORTED_OPERATOR'),
    ([dict(node('a'), operator_ref={})], 'WORKFLOW_UNSUPPORTED_OPERATOR'),
    ([dict(node('a'), depends_on='b')], 'WORKFLOW_DEPENDENCIES_INVALID'),
    ([node('a', [])], 'WORKFLOW_DEPENDENCIES_INVALID'),
    ([node('a', '')], 'WORKFLOW_DEPENDENCIES_INVALID'),
    ([node('a', 'b', 'b'), node('b')], 'WORKFLOW_DUPLICATE_DEPENDENCY'),
    ([node('a', 'absent')], 'WORKFLOW_DEPENDENCY_ABSENT'),
    ([node('a', 'a')], 'WORKFLOW_DEPENDENCY_CYCLE'),
    ([node('a', 'b'), node('b', 'a')], 'WORKFLOW_DEPENDENCY_CYCLE'),
    ([node('free'), node('a', 'b'), node('b', 'c'), node('c', 'a')], 'WORKFLOW_DEPENDENCY_CYCLE'),
])
def test_compile_rejects_malformed_or_unsupported_nodes_without_mutation(nodes, error):
    before = deepcopy(nodes)
    with pytest.raises(ValueError, match='^' + error):
        compile_workflow(nodes)
    assert nodes == before


@pytest.mark.parametrize('revision', [True, False, 0, -1, 1.5, '1', None])
def test_revision_is_a_positive_integer_not_boolean(revision):
    with pytest.raises(ValueError, match='^WORKFLOW_REVISION_INVALID'):
        compile_workflow([], revision=revision)


def test_size_limit_includes_all_nodes_and_accepts_128_node_chain():
    nodes = [node(str(i), *([str(i - 1)] if i else [])) for i in range(128)]
    plan = compile_workflow(list(reversed(nodes)))
    observations = {str(i): observed(ready=True) for i in range(128)}
    assert reduce_workflow(plan, observations)['next_node'] == '0'
    with pytest.raises(ValueError, match='^WORKFLOW_TOO_LARGE'):
        compile_workflow(nodes + [node('128', '127')])
    with pytest.raises(ValueError, match='^WORKFLOW_TOO_LARGE'):
        compile_workflow([node('a', *map(str, range(129)))])


@pytest.mark.parametrize('change,error', [
    ({'schema': 'tare.tools/workflow-ir/2'}, 'WORKFLOW_SCHEMA_UNSUPPORTED'),
    ({'revision': True}, 'WORKFLOW_REVISION_INVALID'),
    ({'nodes': [node('a', 'a')]}, 'WORKFLOW_DEPENDENCY_CYCLE'),
    ({'nodes': [dict(node('a'), operator_ref='work.execute/v0')]}, 'WORKFLOW_UNSUPPORTED_OPERATOR'),
    ({'nodes': [dict(node('a'), vendor='no')]}, 'WORKFLOW_NODE_FIELDS_INVALID'),
    ({'extra': 'no'}, 'WORKFLOW_PLAN_FIELDS_INVALID'),
])
def test_frozen_plan_is_revalidated_by_validator_and_reducer(change, error):
    plan = compile_workflow([node('a')])
    plan.update(change)
    before = deepcopy(plan)
    for validate in (validate_workflow, lambda value: reduce_workflow(value, {'a': observed(ready=True)})):
        with pytest.raises(ValueError, match='^' + error):
            validate(plan)
    assert plan == before


@pytest.mark.parametrize('plan', [None, [], {}, {'schema': 'tare.tools/workflow-ir/1', 'nodes': []}])
def test_invalid_frozen_plan_shape(plan):
    with pytest.raises(ValueError, match='^WORKFLOW_PLAN_FIELDS_INVALID'):
        validate_workflow(plan)


@pytest.mark.parametrize('observations,error', [
    (None, 'WORKFLOW_OBSERVATIONS_INVALID'),
    ([], 'WORKFLOW_OBSERVATIONS_INVALID'),
    ({}, 'WORKFLOW_OBSERVATION_IDS_MISMATCH'),
    ({'other': observed('SUCCEEDED')}, 'WORKFLOW_OBSERVATION_IDS_MISMATCH'),
    ({'a': observed(), 'extra': observed()}, 'WORKFLOW_OBSERVATION_IDS_MISMATCH'),
    ({'a': None}, 'WORKFLOW_OBSERVATION_FIELDS_INVALID'),
    ({'a': {}}, 'WORKFLOW_OBSERVATION_FIELDS_INVALID'),
    ({'a': observed(extra=True)}, 'WORKFLOW_OBSERVATION_FIELDS_INVALID'),
    ({'a': observed('DONE')}, 'WORKFLOW_OBSERVATION_STATE_INVALID'),
    ({'a': observed([])}, 'WORKFLOW_OBSERVATION_STATE_INVALID'),
    ({'a': observed(ready=1)}, 'WORKFLOW_OBSERVATION_READY_INVALID'),
    ({'a': observed(ready='true')}, 'WORKFLOW_OBSERVATION_READY_INVALID'),
    ({'a': observed(ready=None)}, 'WORKFLOW_OBSERVATION_READY_INVALID'),
    ({'a': observed(reason={})}, 'WORKFLOW_OBSERVATION_REASON_INVALID'),
])
def test_invalid_observations_are_not_inferred_as_success(observations, error):
    before = deepcopy(observations)
    with pytest.raises(ValueError, match='^' + error):
        reduce_workflow(compile_workflow([node('a')]), observations)
    assert observations == before


def test_missing_prerequisite_observation_is_not_success():
    plan = compile_workflow([node('child', 'parent'), node('parent')])
    with pytest.raises(ValueError, match='^WORKFLOW_OBSERVATION_IDS_MISMATCH'):
        reduce_workflow(plan, {'child': observed(ready=True)})

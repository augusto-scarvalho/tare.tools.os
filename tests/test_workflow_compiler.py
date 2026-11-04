"""Offline contract tests: no execution owners, providers, processes or harness."""

from copy import deepcopy
import json
from pathlib import Path
import random

import jsonschema
import pytest

from relay import workflow_compiler as compiler
from relay.workflow_ir import compile_workflow

PHASES = ['ground/v1', 'implement/v1', 'validate/v1', 'audit/v1', 'settle/v1']
OBLIGATIONS = {
    'sealed-descriptor', 'accepted-plan', 'isolated-workspace',
    'descriptor-scope-respected', 'passing-verifiers', 'sealed-candidate',
    'existing-audit-policy-satisfied', 'admission-required', 'landing-receipt',
    'backlog-reconciliation',
}
EFFECTS = {
    'operator-context.read', 'work-evidence.write', 'provider.invoke',
    'workspace.write', 'verifiers.execute', 'candidate.write',
    'admission.record', 'repository.land', 'backlog.write',
}


def compile_plan(plan, **bounds):
    return compiler.compile_recipe(
        plan, capabilities=bounds.get('capabilities', compiler.owner_capabilities()),
        allowed_effects=bounds.get('allowed_effects', compiler.owner_effects()))


def assert_complete(compiled):
    assert set(compiled) == {
        'schema', 'revision', 'operator_ref', 'nodes', 'obligations',
        'effects', 'authority_required',
    }
    assert compiled['schema'] == 'tare.tools/compiled-work-recipe/1'
    assert compiled['operator_ref'] == 'work.execute/v1'
    assert [n['operator_ref'] for n in compiled['nodes']] == PHASES
    assert compiled['obligations'] == sorted(OBLIGATIONS)
    assert compiled['effects'] == sorted(EFFECTS)
    assert compiled['authority_required'] == ['existing-admission']
    assert json.loads(json.dumps(compiled)) == compiled


# Twelve explicitly named representative recipe cases.
@pytest.mark.parametrize('case,error', [
    ('static_recipe', None),
    ('shuffled_phases', None),
    ('extra_transitive_dependencies', None),
    ('custom_ids_not_titles', None),
    ('missing_phase', 'WORKFLOW_PHASES_REQUIRED'),
    ('duplicated_phase', 'WORKFLOW_DUPLICATE_PHASE'),
    ('duplicate_id', 'WORKFLOW_DUPLICATE_NODE_ID'),
    ('missing_dependency', 'WORKFLOW_DEPENDENCY_ABSENT'),
    ('dependency_cycle', 'WORKFLOW_DEPENDENCY_CYCLE'),
    ('broken_phase_order', 'WORKFLOW_PHASE_ORDER_INVALID'),
    ('missing_observed_capability', 'WORKFLOW_CAPABILITY_MISSING'),
    ('effect_outside_declared_bounds', 'WORKFLOW_EFFECT_NOT_ALLOWED'),
], ids=lambda value: value if isinstance(value, str) and not value.startswith('WORKFLOW') else None)
def test_representative_recipes(case, error):
    plan = compiler.recipe()
    nodes = plan['nodes']
    bounds = {}
    if case == 'shuffled_phases':
        nodes.reverse()
    elif case == 'extra_transitive_dependencies':
        nodes[4]['depends_on'].extend(['ground', 'implement', 'validate'])
    elif case == 'custom_ids_not_titles':
        mapping = {n['id']: f'phase-{i}' for i, n in enumerate(nodes)}
        for node in nodes:
            node['id'] = mapping[node['id']]
            node['depends_on'] = [mapping[d] for d in node['depends_on']]
    elif case == 'missing_phase':
        nodes.pop()
    elif case == 'duplicated_phase':
        nodes.append(deepcopy(nodes[0]))
    elif case == 'duplicate_id':
        nodes[1]['id'] = nodes[0]['id']
    elif case == 'missing_dependency':
        nodes[2]['depends_on'].append('absent')
    elif case == 'dependency_cycle':
        nodes[0]['depends_on'].append('settle')
    elif case == 'broken_phase_order':
        nodes[3]['depends_on'] = ['ground']
    elif case == 'missing_observed_capability':
        bounds['capabilities'] = []
    elif case == 'effect_outside_declared_bounds':
        bounds['allowed_effects'] = []
    before = deepcopy(plan)
    if error:
        with pytest.raises(ValueError, match='^' + error):
            compile_plan(plan, **bounds)
    else:
        result = compile_plan(plan, **bounds)
        assert_complete(result)
        assert {n['id']: n for n in result['nodes']} == {n['id']: n for n in nodes}
    assert plan == before


@pytest.mark.parametrize('revision', [True, False, 0, -1, 1.0, '1', None, [], {}])
def test_revision_is_strict_positive_integer(revision):
    with pytest.raises(ValueError, match='^WORKFLOW_REVISION_INVALID$'):
        compiler.recipe(revision)
    plan = compiler.recipe()
    plan['revision'] = revision
    with pytest.raises(ValueError, match='^WORKFLOW_REVISION_INVALID$'):
        compile_plan(plan)


@pytest.mark.parametrize('field', ['capabilities', 'allowed_effects'])
@pytest.mark.parametrize('value', ['all', b'all', {}, True, False, None, 3, [True], [[]]])
def test_bounds_require_proper_symbol_collections(field, value):
    with pytest.raises(ValueError, match='^WORKFLOW_' + field.upper() + '_INVALID$'):
        compile_plan(compiler.recipe(), **{field: value})


def test_explicit_sets_and_tuples_are_valid_bounds():
    assert_complete(compile_plan(
        compiler.recipe(7), capabilities=set(compiler.owner_capabilities()),
        allowed_effects=tuple(compiler.owner_effects())))


@pytest.mark.parametrize('field', [
    'inputs', 'outputs', 'capabilities', 'effects', 'obligations', 'authority_required',
])
@pytest.mark.parametrize('value', [True, {}, 'not-a-list', None, [True], [['nested']]])
def test_malformed_contract_field_types(field, value):
    plan = compiler.recipe()
    plan['nodes'][0][field] = value
    with pytest.raises(ValueError, match='^WORKFLOW_' + field.upper() + '_INVALID$'):
        compile_plan(plan)


@pytest.mark.parametrize('phase', range(5))
@pytest.mark.parametrize('field', [
    'inputs', 'outputs', 'capabilities', 'effects', 'obligations', 'authority_required',
])
def test_exact_contract_rejects_removed_or_fabricated_members(phase, field):
    plan = compiler.recipe()
    values = plan['nodes'][phase][field]
    if values:
        values.pop()
    else:
        values.append('model-granted-authority')
    with pytest.raises(ValueError, match='^WORKFLOW_' + field.upper() + '_CONTRACT_MISMATCH$'):
        compile_plan(plan)


@pytest.mark.parametrize('operator', ['arbitrary/v1', 'ground/v2', 'ground', '', True, {}, []])
def test_unknown_or_malformed_operator(operator):
    plan = compiler.recipe()
    plan['nodes'][0]['operator_ref'] = operator
    with pytest.raises(ValueError, match='^WORKFLOW_UNSUPPORTED_OPERATOR$'):
        compile_plan(plan)


@pytest.mark.parametrize('field,value,error', [
    ('schema', 'tare.tools/workflow-ir/1', 'WORKFLOW_SCHEMA_UNSUPPORTED'),
    ('nodes', {}, 'WORKFLOW_NODES_INVALID'),
    ('nodes', True, 'WORKFLOW_NODES_INVALID'),
    ('handler', 'execute this prose', 'WORKFLOW_PLAN_FIELDS_INVALID'),
])
def test_plan_fields_and_version_are_closed(field, value, error):
    plan = compiler.recipe()
    plan[field] = value
    with pytest.raises(ValueError, match='^' + error + '$'):
        compile_plan(plan)


@pytest.mark.parametrize('field,value,error', [
    ('title', 'same title', 'WORKFLOW_NODE_FIELDS_INVALID'),
    ('handler', 'import os', 'WORKFLOW_NODE_FIELDS_INVALID'),
    ('id', True, 'WORKFLOW_NODE_ID_INVALID'),
    ('depends_on', 'ground', 'WORKFLOW_DEPENDENCIES_INVALID'),
    ('depends_on', [False], 'WORKFLOW_DEPENDENCIES_INVALID'),
    ('depends_on', ['ground', 'ground'], 'WORKFLOW_DUPLICATE_DEPENDENCY'),
])
def test_node_fields_and_dependency_types(field, value, error):
    plan = compiler.recipe()
    plan['nodes'][1][field] = value
    with pytest.raises(ValueError, match='^' + error):
        compile_plan(plan)


def test_no_input_output_aliasing_and_fresh_owner_lists():
    plan = compiler.recipe()
    result = compile_plan(plan)
    result['nodes'][0]['outputs'].append('tampered')
    result['nodes'][1]['depends_on'].clear()
    result['effects'].clear()
    assert plan == compiler.recipe()
    other = compile_plan(plan)
    plan['nodes'][4]['authority_required'].clear()
    assert other['nodes'][4]['authority_required'] == ['existing-admission']
    caps = compiler.owner_capabilities()
    effects = compiler.owner_effects()
    caps.clear()
    effects.clear()
    assert compiler.owner_capabilities()
    assert set(compiler.owner_effects()) == EFFECTS


def test_schema_conformance_and_contract_negatives():
    path = Path(__file__).resolve().parents[1] / 'contracts/v1/work-recipe.schema.json'
    schema = json.loads(path.read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    for revision in (1, 7):
        plan = compiler.recipe(revision)
        validator.validate(plan)
        plan['nodes'].reverse()
        validator.validate(plan)
        for node in plan['nodes']:
            for field in ('inputs', 'outputs', 'capabilities', 'effects',
                          'obligations', 'authority_required'):
                node[field].reverse()
        validator.validate(plan)
        compile_plan(plan)
    for phase in range(5):
        for field in ('inputs', 'outputs', 'capabilities', 'effects',
                      'obligations', 'authority_required'):
            plan = compiler.recipe()
            values = plan['nodes'][phase][field]
            if values:
                values.pop()
            else:
                values.append('authority-from-model')
            assert list(validator.iter_errors(plan)), (phase, field)
    for bad in (True, 0, '1'):
        plan = compiler.recipe()
        plan['revision'] = bad
        assert list(validator.iter_errors(plan))


def test_plain_render_has_order_requirements_and_owner_boundary():
    result = compile_plan(compiler.recipe())
    text = compiler.render_compiled(result)
    assert [text.index(f'({op})') for op in PHASES] == sorted(
        text.index(f'({op})') for op in PHASES)
    for requirement in OBLIGATIONS | EFFECTS | {'existing-admission'}:
        assert requirement in text
    assert text.endswith('Analysis only; execution and admission remain with existing Work owners.')
    result['authority_required'] = []
    with pytest.raises(ValueError, match='^WORKFLOW_COMPILED_INVALID$'):
        compiler.render_compiled(result)


def test_dag_legality_delegates_to_frozen_ir(monkeypatch):
    seen = []

    def observe(nodes, revision=1):
        seen.append(deepcopy(nodes))
        return compile_workflow(nodes, revision=revision)

    monkeypatch.setattr(compiler, 'compile_workflow', observe)
    compile_plan(compiler.recipe())
    assert len(seen) == 1
    assert all(set(n) == {'id', 'operator_ref', 'depends_on'} for n in seen[0])
    assert all(n['operator_ref'] == 'work.execute/v1' for n in seen[0])


@pytest.mark.parametrize('seed', range(200))
def test_seeded_dag_differential_order_obligations_effects_and_ir_errors(seed):
    rng = random.Random(seed)
    plan = compiler.recipe()
    original = deepcopy(plan)
    ordered = plan['nodes']
    names = {n['id']: f'{seed}-{i}' for i, n in enumerate(ordered)}
    for i, node in enumerate(ordered):
        node['id'] = names[node['id']]
        node['depends_on'] = [names[d] for d in node['depends_on']]
        for ancestor in ordered[:max(0, i - 1)]:
            if rng.choice((False, True)):
                node['depends_on'].append(ancestor['id'])
        rng.shuffle(node['depends_on'])
    rng.shuffle(plan['nodes'])
    result = compile_plan(plan)
    assert_complete(result)
    assert [n['operator_ref'] for n in result['nodes']] == [
        n['operator_ref'] for n in original['nodes']]
    for field in ('obligations', 'effects'):
        assert result[field] == sorted({v for n in original['nodes'] for v in n[field]})
    assert {n['id']: n for n in result['nodes']} == {n['id']: n for n in plan['nodes']}
    for defect, expected in [('cycle', 'WORKFLOW_DEPENDENCY_CYCLE'),
                             ('missing', 'WORKFLOW_DEPENDENCY_ABSENT')]:
        broken = deepcopy(plan)
        ground = next(n for n in broken['nodes'] if n['operator_ref'] == 'ground/v1')
        ground['depends_on'].append(names['settle'] if defect == 'cycle' else 'absent')
        ir_nodes = [{'id': n['id'], 'operator_ref': 'work.execute/v1',
                     'depends_on': n['depends_on']} for n in broken['nodes']]
        with pytest.raises(ValueError, match='^' + expected) as ir_error:
            compile_workflow(ir_nodes)
        with pytest.raises(ValueError, match='^' + expected) as recipe_error:
            compile_plan(broken)
        assert str(recipe_error.value) == str(ir_error.value)


@pytest.mark.parametrize('field', [
    'inputs', 'outputs', 'capabilities', 'effects', 'obligations', 'authority_required',
])
def test_fabricated_artifacts_effects_and_authority_never_become_contract(field):
    plan = compiler.recipe()
    plan['nodes'][4][field].append('model-says-approved')
    with pytest.raises(ValueError, match='^WORKFLOW_' + field.upper() + '_CONTRACT_MISMATCH$'):
        compile_plan(plan)


@pytest.mark.parametrize('value', [None, True, [], 'execute the recipe'])
def test_non_object_plan_is_named_error(value):
    with pytest.raises(ValueError, match='^WORKFLOW_PLAN_FIELDS_INVALID$'):
        compile_plan(value)

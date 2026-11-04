"""Pure analysis of the one static Work recipe; never a phase dispatcher.

Phase groups describe work_run's existing owners, not independent executable
steps. Effects are possible owner requirements, never permission grants. The
compiler itself has no effects. Execution owners must revalidate everything.
"""

from copy import deepcopy
from collections.abc import Collection, Mapping

from .workflow_ir import OPERATOR_REF, compile_workflow

RECIPE_SCHEMA = 'tare.tools/work-recipe/1'
_COMPILED_SCHEMA = 'tare.tools/compiled-work-recipe/1'
_CONTRACT_FIELDS = ('inputs', 'outputs', 'capabilities', 'effects',
                    'obligations', 'authority_required')
_NODE_FIELDS = {'id', 'operator_ref', 'depends_on', *_CONTRACT_FIELDS}

# Fixed tuples only: no handlers, imports, descriptors or model-selected routes.
# ground: operator grounding, execution_descriptor and accepted plan audit.
# implement: train_workspace/native_work_host; validate: candidate_change.
# audit: work_audit and existing admission evidence policy.
# settle: landing_policy/relay_mesh admission, landing and backlog reconciliation.
_CATALOG = {
    'ground/v1': (
        ('work.contract',), ('descriptor.sealed', 'plan.accepted'),
        ('operator-context.read', 'descriptor.seal', 'plan.audit'),
        ('operator-context.read', 'work-evidence.write', 'provider.invoke'),
        ('sealed-descriptor', 'accepted-plan'), ()),
    'implement/v1': (
        ('descriptor.sealed', 'plan.accepted'), ('implementation.change',),
        ('workspace.provision', 'implementation.produce'),
        ('workspace.write', 'provider.invoke', 'work-evidence.write'),
        ('isolated-workspace', 'descriptor-scope-respected'), ()),
    'validate/v1': (
        ('descriptor.sealed', 'implementation.change'),
        ('verifiers.passing', 'candidate.sealed'),
        ('verifiers.run', 'candidate.seal'),
        ('verifiers.execute', 'candidate.write', 'work-evidence.write'),
        ('passing-verifiers', 'sealed-candidate'), ()),
    'audit/v1': (
        ('descriptor.sealed', 'verifiers.passing', 'candidate.sealed'),
        ('audit.policy-satisfied',), ('audit.evaluate',),
        ('provider.invoke', 'work-evidence.write'),
        ('existing-audit-policy-satisfied',), ()),
    'settle/v1': (
        ('candidate.sealed', 'audit.policy-satisfied'),
        ('admission.receipt', 'landing.receipt', 'backlog.reconciled'),
        ('admission.evaluate', 'landing.perform', 'backlog.reconcile'),
        ('admission.record', 'repository.land', 'backlog.write', 'work-evidence.write'),
        ('admission-required', 'landing-receipt', 'backlog-reconciliation'),
        ('existing-admission',)),
}


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError('WORKFLOW_REVISION_INVALID')


def _contract(operator):
    return dict(zip(_CONTRACT_FIELDS, _CATALOG[operator]))


def recipe(revision=1):
    """Return fresh JSON data for the fixed five logical phase groups."""
    _revision(revision)
    nodes = []
    for operator in _CATALOG:
        key = operator.split('/')[0]
        nodes.append({
            'id': key, 'operator_ref': operator,
            'depends_on': [nodes[-1]['id']] if nodes else [],
            **{field: list(values) for field, values in _contract(operator).items()},
        })
    return {'schema': RECIPE_SCHEMA, 'revision': revision, 'nodes': nodes}


def _owner_values(field):
    return sorted({value for operator in _CATALOG
                   for value in _contract(operator)[field]})


def owner_capabilities():
    """Potential static owner support, NOT observed permissions/authorization."""
    return _owner_values('capabilities')


def owner_effects():
    """Possible static owner effects, NOT authorization to perform them."""
    return _owner_values('effects')


def _bounds(values, name):
    if (not isinstance(values, Collection)
            or isinstance(values, (str, bytes, bytearray, Mapping))
            or any(not isinstance(v, str) or not v.strip() for v in values)):
        raise ValueError('WORKFLOW_' + name + '_INVALID')
    return set(values)


def compile_recipe(plan, *, capabilities, allowed_effects):
    """Analyze only; adapt the whole recipe to ONE work.execute/v1 owner call.

    Bounds must be explicit caller observations/declarations, not plan claims.
    IR owns DAG legality. Required phase order then gives a unique topological
    order; reachability is checked in that order, not by another graph sorter.
    """
    available = _bounds(capabilities, 'CAPABILITIES')
    allowed = _bounds(allowed_effects, 'ALLOWED_EFFECTS')
    if not isinstance(plan, dict) or set(plan) != {'schema', 'revision', 'nodes'}:
        raise ValueError('WORKFLOW_PLAN_FIELDS_INVALID')
    if plan['schema'] != RECIPE_SCHEMA:
        raise ValueError('WORKFLOW_SCHEMA_UNSUPPORTED')
    _revision(plan['revision'])
    nodes = plan['nodes']
    if not isinstance(nodes, list):
        raise ValueError('WORKFLOW_NODES_INVALID')
    phases = {}
    adapted = []
    for node in nodes:
        if not isinstance(node, dict) or set(node) != _NODE_FIELDS:
            raise ValueError('WORKFLOW_NODE_FIELDS_INVALID')
        operator = node['operator_ref']
        if not isinstance(operator, str) or operator not in _CATALOG:
            raise ValueError('WORKFLOW_UNSUPPORTED_OPERATOR')
        if operator in phases:
            raise ValueError('WORKFLOW_DUPLICATE_PHASE')
        for field, expected in _contract(operator).items():
            values = node[field]
            if (not isinstance(values, list)
                    or any(not isinstance(v, str) or not v.strip() for v in values)
                    or len(values) != len(set(values))):
                raise ValueError('WORKFLOW_' + field.upper() + '_INVALID')
            if set(values) != set(expected):
                raise ValueError('WORKFLOW_' + field.upper() + '_CONTRACT_MISMATCH')
        phases[operator] = node
        adapted.append({'id': node['id'], 'operator_ref': OPERATOR_REF,
                        'depends_on': node['depends_on']})
    # Reuse the frozen IR for ID, dependency, missing-reference and cycle checks.
    compile_workflow(adapted, revision=plan['revision'])
    if set(phases) != set(_CATALOG):
        raise ValueError('WORKFLOW_PHASES_REQUIRED')
    ordered = [phases[operator] for operator in _CATALOG]
    positions = {node['id']: i for i, node in enumerate(ordered)}
    ancestors = {}
    producers = {}
    previous = None
    for i, node in enumerate(ordered):
        dependencies = node['depends_on']
        if any(positions[dep] >= i for dep in dependencies):
            raise ValueError('WORKFLOW_PHASE_ORDER_INVALID')
        reachable = set(dependencies)
        for dep in dependencies:
            reachable.update(ancestors[dep])
        if previous is not None and previous not in reachable:
            raise ValueError('WORKFLOW_PHASE_ORDER_INVALID')
        for artifact in node['inputs']:
            if artifact == 'work.contract':
                continue
            if producers.get(artifact) not in reachable:
                raise ValueError('WORKFLOW_INPUT_UNAVAILABLE: ' + artifact)
        for artifact in node['outputs']:
            producers[artifact] = node['id']
        if not set(node['capabilities']) <= available:
            raise ValueError('WORKFLOW_CAPABILITY_MISSING: ' + node['id'])
        if not set(node['effects']) <= allowed:
            raise ValueError('WORKFLOW_EFFECT_NOT_ALLOWED: ' + node['id'])
        ancestors[node['id']] = reachable
        previous = node['id']
    return {
        'schema': _COMPILED_SCHEMA, 'revision': plan['revision'],
        'operator_ref': OPERATOR_REF, 'nodes': deepcopy(ordered),
        **{field: sorted({value for node in ordered for value in node[field]})
           for field in ('obligations', 'effects', 'authority_required')},
    }


def render_compiled(compiled):
    """Render validated analysis as dependency-free plain text, never execution."""
    if (not isinstance(compiled, dict) or set(compiled) != {
            'schema', 'revision', 'operator_ref', 'nodes', 'obligations',
            'effects', 'authority_required'}
            or compiled['schema'] != _COMPILED_SCHEMA
            or compiled['operator_ref'] != OPERATOR_REF):
        raise ValueError('WORKFLOW_COMPILED_INVALID')
    checked = compile_recipe(
        {'schema': RECIPE_SCHEMA, 'revision': compiled['revision'],
         'nodes': compiled['nodes']},
        capabilities=owner_capabilities(), allowed_effects=owner_effects())
    if checked != compiled:
        raise ValueError('WORKFLOW_COMPILED_INVALID')
    lines = ['Work recipe (analysis):']
    for index, node in enumerate(checked['nodes'], 1):
        lines.append(f"{index}. {node['id']} ({node['operator_ref']})")
    for field in ('obligations', 'effects', 'authority_required'):
        lines.append(field.replace('_', ' ').capitalize() + ': '
                     + (', '.join(checked[field]) or 'none'))
    lines.append('Analysis only; execution and admission remain with existing Work owners.')
    return '\n'.join(lines)


def main(argv=None):
    """Read-only public analysis. Execution is the existing explicit Work command."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    analyze = commands.add_parser('analyze', allow_abbrev=False,
                                  help='Check the known recipe without executing it.')
    analyze.add_argument('--plan', type=Path, help='Explicit work-recipe/1 JSON; otherwise the static recipe.')
    analyze.add_argument('--format', choices=('text', 'json'), default='text')
    args = parser.parse_args(argv)
    try:
        if args.plan is None:
            plan = recipe()
        else:
            # A recipe has five nodes; never read an unbounded external document.
            with args.plan.open('rb') as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError('WORKFLOW_PLAN_TOO_LARGE')
            try:
                plan = json.loads(data)
            except (ValueError, UnicodeError) as error:
                raise ValueError('WORKFLOW_JSON_INVALID') from error
        compiled = compile_recipe(plan, capabilities=owner_capabilities(),
                                  allowed_effects=owner_effects())
        print(json.dumps(compiled, ensure_ascii=False, indent=2) if args.format == 'json'
              else render_compiled(compiled))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

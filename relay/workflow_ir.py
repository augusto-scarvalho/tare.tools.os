"""Pure, versioned Work DAG data. No execution, persistence or graph imports.

Input order is scheduling priority, not dependency direction. Empty plans are
valid. Errors have stable WORKFLOW_* names, optionally followed by context.
"""

SCHEMA = 'tare.tools/workflow-ir/1'
OPERATOR_REF = 'work.execute/v1'
MAX_NODES = 128
_NODE_FIELDS = {'id', 'operator_ref', 'depends_on'}
_PLAN_FIELDS = {'schema', 'revision', 'nodes'}


def _valid_id(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_nodes(nodes):
    if not isinstance(nodes, list):
        raise ValueError('WORKFLOW_NODES_INVALID')
    if len(nodes) > MAX_NODES:
        raise ValueError('WORKFLOW_TOO_LARGE')
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != _NODE_FIELDS:
            raise ValueError('WORKFLOW_NODE_FIELDS_INVALID')
        key = node['id']
        if not _valid_id(key):
            raise ValueError('WORKFLOW_NODE_ID_INVALID')
        if key in ids:
            raise ValueError('WORKFLOW_DUPLICATE_NODE_ID: ' + key)
        ids.add(key)
        if node['operator_ref'] != OPERATOR_REF:
            raise ValueError('WORKFLOW_UNSUPPORTED_OPERATOR: ' + key)
        dependencies = node['depends_on']
        if (not isinstance(dependencies, list)
                or any(not _valid_id(dep) for dep in dependencies)):
            raise ValueError('WORKFLOW_DEPENDENCIES_INVALID: ' + key)
        if len(dependencies) > MAX_NODES:
            raise ValueError('WORKFLOW_TOO_LARGE: ' + key)
        if len(set(dependencies)) != len(dependencies):
            raise ValueError('WORKFLOW_DUPLICATE_DEPENDENCY: ' + key)
    for node in nodes:
        for dep in node['depends_on']:
            if dep not in ids:
                raise ValueError('WORKFLOW_DEPENDENCY_ABSENT: ' + node['id'] + ' -> ' + dep)

    # Iterative cycle detection; never reorder the caller's priority list.
    remaining = {node['id']: set(node['depends_on']) for node in nodes}
    satisfied = set()
    while remaining:
        available = [key for key, deps in remaining.items() if deps <= satisfied]
        if not available:
            raise ValueError('WORKFLOW_DEPENDENCY_CYCLE')
        satisfied.update(available)
        for key in available:
            del remaining[key]


def _validate_revision(revision):
    if type(revision) is not int or revision < 1:
        raise ValueError('WORKFLOW_REVISION_INVALID')


def compile_workflow(nodes, revision=1):
    """Validate explicit node records and return an independently owned JSON plan.

    Each record must contain exactly id, operator_ref and depends_on. Dependencies
    refer only to selected IDs; neither edges nor missing fields are inferred.
    """
    _validate_revision(revision)
    _validate_nodes(nodes)
    return {
        'schema': SCHEMA,
        'revision': revision,
        'nodes': [
            {'id': node['id'], 'operator_ref': node['operator_ref'],
             'depends_on': list(node['depends_on'])}
            for node in nodes
        ],
    }


def validate_workflow(plan):
    """Validate a frozen plan without normalizing, repairing or mutating it.

    Returns the same plan object. Frozen describes its logical contract, not a
    special Python container: callers can serialize it directly as JSON.
    """
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
        raise ValueError('WORKFLOW_PLAN_FIELDS_INVALID')
    if plan['schema'] != SCHEMA:
        raise ValueError('WORKFLOW_SCHEMA_UNSUPPORTED')
    _validate_revision(plan['revision'])
    _validate_nodes(plan['nodes'])
    return plan

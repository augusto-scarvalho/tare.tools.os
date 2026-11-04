"""Pure serial scheduling decisions over a frozen Work DAG and owner observations."""

from .workflow_ir import validate_workflow

_STATES = {'PENDING', 'SUCCEEDED', 'RECOVERABLE', 'WAITING', 'UNKNOWN',
           'FAILED', 'CANCELLED'}
_OBSERVATION_FIELDS = {'state', 'ready', 'reason'}


def _validate_observations(nodes, observations):
    if not isinstance(observations, dict):
        raise ValueError('WORKFLOW_OBSERVATIONS_INVALID')
    ids = {node['id'] for node in nodes}
    if set(observations) != ids:
        raise ValueError('WORKFLOW_OBSERVATION_IDS_MISMATCH')
    for node in nodes:
        key = node['id']
        value = observations[key]
        if (not isinstance(value, dict) or 'state' not in value
                or not set(value) <= _OBSERVATION_FIELDS):
            raise ValueError('WORKFLOW_OBSERVATION_FIELDS_INVALID: ' + key)
        if not isinstance(value['state'], str) or value['state'] not in _STATES:
            raise ValueError('WORKFLOW_OBSERVATION_STATE_INVALID: ' + key)
        if 'ready' in value and type(value['ready']) is not bool:
            raise ValueError('WORKFLOW_OBSERVATION_READY_INVALID: ' + key)
        if 'reason' in value and not isinstance(value['reason'], str):
            raise ValueError('WORKFLOW_OBSERVATION_REASON_INVALID: ' + key)


def _reason(observation, fallback):
    reason = observation.get('reason')
    return fallback + (': ' + reason if reason else '')


def reduce_workflow(plan, observations):
    """Return one decision, never perform it or infer success from missing data.

    next_node is an ID or None. Only READY authorizes fresh dispatch; RECOVER
    authorizes recovery of an existing execution, even if dependencies have since
    changed state. ready lists eligible PENDING IDs, not recovery candidates.
    completed/waiting are ordered ID lists; blocked maps IDs to concrete reasons.
    All ordering follows the plan, never the observations' insertion order.

    UNKNOWN globally vetoes both recovery and fresh dispatch and clears ready.
    Otherwise recovery takes priority over new dispatch. WAITING means the only
    remaining obstacles are WAITING nodes and their dependent PENDING nodes;
    terminal failures and explicit lack of readiness instead produce BLOCKED.
    This reducer selects at most one action; execution custody stays with Work.
    """
    nodes = validate_workflow(plan)['nodes']
    _validate_observations(nodes, observations)
    completed, waiting, recoverable, ready = [], [], [], []
    blocked = {}
    unknown = any(observations[node['id']]['state'] == 'UNKNOWN' for node in nodes)
    hard_block = False

    for node in nodes:
        key = node['id']
        observation = observations[key]
        state = observation['state']
        if state == 'SUCCEEDED':
            completed.append(key)
        elif state == 'WAITING':
            waiting.append(key)
        elif state == 'UNKNOWN':
            blocked[key] = _reason(observation, 'UNKNOWN: external effect requires reconciliation')
        elif state == 'RECOVERABLE':
            if unknown:
                blocked[key] = 'Recovery withheld: workflow needs reconciliation'
            else:
                recoverable.append(key)
        elif state in {'FAILED', 'CANCELLED'}:
            blocked[key] = _reason(observation, state)
            hard_block = True
        else:  # PENDING, the only state that may authorize a fresh dispatch.
            reasons = []
            if observation.get('ready') is not True:
                reasons.append(_reason(observation, 'Not ready'))
                hard_block = True
            dependencies = [dep for dep in node['depends_on']
                            if observations[dep]['state'] != 'SUCCEEDED']
            if dependencies:
                reasons.append('Dependencies not succeeded: ' + ', '.join(
                    dep + ' (' + _reason(observations[dep], observations[dep]['state']) + ')'
                    for dep in dependencies))
            if unknown:
                reasons.append('Dispatch withheld: workflow needs reconciliation')
            if reasons:
                blocked[key] = '; '.join(reasons)
            else:
                ready.append(key)

    next_node = None
    if unknown:
        status = 'NEEDS_RECONCILIATION'
    elif recoverable:
        status, next_node = 'RECOVER', recoverable[0]
    elif ready:
        status, next_node = 'READY', ready[0]
    elif len(completed) == len(nodes):
        status = 'COMPLETED'
    elif waiting and not hard_block:
        status = 'WAITING'
    else:
        status = 'BLOCKED'
    return {'status': status, 'next_node': next_node, 'ready': ready,
            'blocked': blocked, 'completed': completed, 'waiting': waiting}

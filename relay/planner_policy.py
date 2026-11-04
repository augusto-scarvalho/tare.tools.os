"""Planner reasoning floor, applied before a new planner identity is persisted."""
from copy import deepcopy

LEVELS = ('high', 'xhigh', 'max')


def effective(deployment, requested=None):
    from .execution_team import reasoning
    from .model_choices import describe, realization
    if requested not in (None, 'default', *LEVELS):
        raise ValueError('Planner minimum reasoning is high. Use high, xhigh or max; omit effort for the minimum.')
    current = reasoning(deployment)
    if current in LEVELS and requested in (None, 'default', current):
        from .execution_scope import current as execution_scope
        if execution_scope().allows(deployment,'planner')[0]:
            return deepcopy(deployment)
    level = requested if requested not in (None, 'default') else 'high'
    item = describe(deployment['id'], deployment)
    from .execution_scope import current
    scope=current()
    if scope.pin('planner'):
        from .execution_scope import pinned
        value=pinned('planner')
        from .execution_team import reasoning
        if requested not in (None,'default',reasoning(value)):
            raise ValueError('Requested effort conflicts with --planner; consult tare models --role planner.')
        return value
    if scope.restrictions:
        allowed=[candidate for candidate in LEVELS if candidate in item['route']['efforts']]
        if requested not in (None,'default'):allowed=[requested] if requested in allowed else []
        for candidate in allowed:
            choices=[{k:item[k] for k in ('key','label','group')} | {'routes':[item['route']]}]
            value=realization(choices,{'key':item['key'],'connection':item['route']['id'],
                                      'reasoning':candidate},deployment=deployment)
            if scope.allows(value,'planner')[0]:return value
        raise ValueError('No planner reasoning level satisfies the temporary allowlist.')
    if level not in item['route']['efforts']:
        raise ValueError('Planner requires at least high reasoning. This connection has no registered '+level+
                         ' setting; select a capable planner. No model call was made.')
    choices = [{k: item[k] for k in ('key', 'label', 'group')} | {'routes': [item['route']]}]
    value = realization(choices, {'key': item['key'], 'connection': item['route']['id'],
                                  'reasoning': level}, deployment=deployment)
    if reasoning(value) not in LEVELS:
        raise ValueError('Planner connection did not apply the required reasoning level. No model call was made.')
    return value


def choices(values):
    """The planner picker exposes only connections with a registered high-or-greater setting."""
    result = deepcopy(values)
    for item in result:
        routes = []
        for route in item['routes']:
            route['efforts'] = [level for level in route['efforts'] if level in LEVELS]
            if not route['efforts']:
                continue
            if route['default_effort'] not in LEVELS:
                route['default_effort'] = min(route['efforts'], key=LEVELS.index)
            routes.append(route)
        item['routes'] = routes
    return [item for item in result if item['routes']]

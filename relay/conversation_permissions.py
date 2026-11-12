"""An operator decision for a saved plan, outside the coordinator's capabilities.

The worker waits without making model calls. Only its frontend writes the answer;
the plan and effect identity bind it to this execution. No session prompt, tools or
history are rewritten when the operator grants access.
"""
from copy import deepcopy
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import time

from compute_plane.native_journal import custody_lock, regular, save_json
from compute_plane.native_models import encode

SCHEMA = 'tare.tools/execution-permission/1'
GRANT = ('session', 'task', 'plan', 'project', 'paths', 'checks', 'tests')
CHOICES = {'once', 'project', 'deny', 'change'}


class PlanChangeRequested(PermissionError):
    """The user sent the shown plan back for changes; nothing was implemented."""


def plan_text(root, request):
    """The whole saved plan, readable, from the artifact whose sha256 the request binds."""
    path = regular(Path(request['artifact']))
    if not path.resolve().is_relative_to((Path(root)/'planning').resolve()):
        raise ValueError('The plan is outside this conversation.')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != request['plan']:
        raise ValueError('The saved plan changed after it was proposed.')
    plan = json.loads(raw)['payload']
    out = [plan.get('summary', ''), '', 'Steps:', *(f'{n}. {s}' for n, s in enumerate(plan.get('steps', []), 1))]
    for task in plan.get('tasks', []):
        out += ['', '• '+task['title'], '  '+task.get('summary', ''), '  Files: '+', '.join(task['target_paths'])]
        out += ['  Done when: '+c for c in task.get('criteria', [])]
        if task.get('test_paths') or task.get('verifiers'):
            out.append('  Checks: '+', '.join([*task.get('verifiers', []), *task.get('test_paths', [])]))
    out += ['', 'Done when:', *('- '+c for c in plan.get('criteria', []))]
    if plan.get('remaining'):
        out += ['', 'Left uncertain by the planner:', *('- '+r for r in plan['remaining'])]
    return '\n'.join(out)


def paths_allowed(paths, scopes):
    return all(any(p in ('', '.') or PurePosixPath(name).is_relative_to(p) for p in scopes) for name in paths)


def pending(root):
    from .native_agent import read_json
    path = regular(Path(root)/'execution-permission'/'request.json')
    if not path.exists():
        return None
    value = read_json(path, 32768)
    if (value.get('schema') != SCHEMA or value.get('status') != 'WAITING'
            or value.get('id') != identity(value)):
        return None
    return value


def identity(value):
    return hashlib.sha256(encode({k: value[k] for k in (
        'session', 'effect', 'task', 'plan', 'project', 'paths', 'checks', 'tests', 'objective')}).encode()).hexdigest()


@contextmanager
def decision_lock(folder):
    """A published request/answer can be visible just before its writer unlocks."""
    deadline = time.monotonic()+2
    with ExitStack() as owner:
        while True:
            try:
                owner.enter_context(custody_lock(folder))
                break
            except ValueError as exc:
                if str(exc) != 'native_custody_has_active_writer' or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
        yield  # Only lock acquisition retries; the decision's effects never replay.


def answer(root, request_id, choice, *, project, directory, control_root, changes=''):
    """Frontend-only action. A late answer cannot authorize a replacement request."""
    if choice not in CHOICES:
        raise ValueError('Choose Allow for this task, Always allow in this project, Change the plan, or Deny.')
    folder = regular(Path(root)/'execution-permission')
    with decision_lock(folder):
        request = pending(root)
        if not request or request['id'] != request_id or Path(request['project']) != Path(project).resolve():
            raise ValueError('This permission request changed or ended. Nothing was authorized.')
        if choice == 'change' and 'artifact' not in request:
            raise ValueError('This request shows no plan to change. Nothing was authorized.')
        if choice == 'project':
            from .project_permissions import read_defaults, save_defaults
            defaults, revision = read_defaults(directory, project, control_root)
            # Remember file changes and the project's checks. Do not enable
            # arbitrary commands, disable isolation or adopt project instructions.
            defaults.update(mode='commands' if defaults['mode'] == 'commands' else 'edit', write_paths=['.'])
            save_defaults(directory, project, control_root, defaults, revision)
        save_json(regular(folder/'answer.json'), {'id': request_id, 'choice': choice,
                                                  **({'changes': changes.strip()[:480]} if choice == 'change' else {})})
    return choice


def close_interrupted(root, effect_id):
    folder = regular(Path(root)/'execution-permission')
    if not folder.is_dir():
        return
    with decision_lock(folder):
        request = pending(root)
        if request and request['effect'] == effect_id:
            request['status'] = 'CLOSED'
            save_json(regular(folder/'request.json'), request)


def with_review_policy(config, directory, project, control_root):
    """Carry an operator-selected execution policy without changing saved chat scope."""
    if directory is None:
        return config
    from .project_permissions import read_defaults
    defaults, _ = read_defaults(directory, project, control_root)
    if defaults.get('command_protection'):
        config = {**config, 'command_protection': deepcopy(defaults['command_protection'])}
        if defaults['mode'] == 'commands':
            # This is the transient worker config, never the frozen coordinator toolset.
            config['capabilities'] = list(dict.fromkeys([*(config.get('capabilities') or []), 'command']))
    return config


def authorize(root, state, task, effect_id, control, directory):
    """Return a transient worker config; the coordinator's config remains frozen."""
    from . import relay_mesh
    from .native_agent import read_json
    from .project_permissions import read_defaults, shows_plan
    config = deepcopy(state['config'])
    unit = task['plan']['unit']
    repository, paths = unit['repository'], unit['target_paths']
    project = config['repositories'][repository]
    config = with_review_policy(config, directory, project, relay_mesh.RELAY_ROOT)
    allowed = paths_allowed(paths, config.get('writable', {}).get(repository, []))
    if directory is None:  # headless (tare serve, scripted runs): nobody to show the plan to
        if allowed:
            return config
        raise ValueError('This plan needs permission for its edit scope. No interactive permission frontend is connected; no implementation started.')
    defaults, _ = read_defaults(directory, project, relay_mesh.RELAY_ROOT)
    folder = regular(Path(root)/'execution-permission')
    # "Show me the plan and ask": the host, not the model, shows each new plan once, even when edits are allowed.
    approved, approval = regular(folder/'approved.json'), {'task': task['id'], 'plan': task['plan']['sha256']}
    ask = shows_plan(directory, defaults) and not (approved.exists() and read_json(approved, 4096) == approval)
    if allowed and not ask:
        return config
    if not allowed and defaults['mode'] != 'read' and paths_allowed(paths, defaults['write_paths']):
        config.setdefault('writable', {})[repository] = list(paths)
        if not ask:
            return config
        allowed = True
    folder.mkdir(exist_ok=True)
    request = {'schema': SCHEMA, 'session': state['session_id'], 'effect': effect_id,
        'task': task['id'], 'plan': task['plan']['sha256'], 'project': str(Path(project).resolve()),
        'paths': list(paths), 'checks': {n: check['argv'] for n, check in config['verifiers'].items()
                                      if check['repository'] == repository},
        'tests': list(unit.get('test_paths', [])),
        'objective': task['objective'][:2000], 'status': 'WAITING'}
    # "Allow for this task" covers its corrections: the same task, saved plan, files and checks ask only once.
    granted, grant = regular(folder/'granted.json'), {k: request[k] for k in GRANT}
    if not allowed and granted.exists() and read_json(granted, 32768) == grant:
        config.setdefault('writable', {})[repository] = list(paths)
        if not ask:
            return config
        allowed = True
    if ask:  # one dialog: the plan, and the files when they still need permission
        request.update(artifact=task['plan']['artifact'], files_allowed=allowed)
    request['id'] = identity(request)
    with custody_lock(folder):
        regular(folder/'answer.json').unlink(missing_ok=True)
        save_json(regular(folder/'request.json'), request)
    try:
        while not control.should_stop():
            response = regular(folder/'answer.json')
            if response.exists():
                value = read_json(response, 4096)
                if value.get('id') == request['id'] and value.get('choice') in CHOICES:
                    if value['choice'] == 'deny':
                        raise PermissionError('The user denied this execution. No implementer started. Do not ask again in this turn.')
                    if value['choice'] == 'change':
                        raise PlanChangeRequested(
                            'The user asked to change the plan; no implementer started. '+
                            ('Ask the planner for a revised plan with delegate_plan, passing their words; the user '
                             'sees the new plan before implementation. The user wrote: '+value['changes']
                             if value.get('changes') else
                             'Ask the user what to change; do not plan or implement again before they answer.'))
                    if ask:
                        save_json(approved, approval)
                    if allowed:
                        return config
                    if value['choice'] == 'once':
                        save_json(granted, grant)
                    config.setdefault('writable', {})[repository] = list(paths)
                    return config
            time.sleep(.1)  # no inference while the operator decides; cancellation/deadline still apply
        raise PermissionError('Execution permission was not granted before cancellation or the time limit. No implementer started.')
    finally:
        with decision_lock(folder):
            request['status'] = 'CLOSED'
            save_json(regular(folder/'request.json'), request)

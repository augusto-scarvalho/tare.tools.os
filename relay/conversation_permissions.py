"""An operator decision for a saved plan, outside the coordinator's capabilities.

The worker waits without making model calls. Only its frontend writes the answer;
the plan and effect identity bind it to this execution. No session prompt, tools or
history are rewritten when the operator grants access.
"""
from copy import deepcopy
from contextlib import contextmanager, ExitStack
import hashlib
from pathlib import Path, PurePosixPath
import time

from compute_plane.native_journal import custody_lock, regular, save_json
from compute_plane.native_models import encode

SCHEMA = 'tare.tools/execution-permission/1'
GRANT = ('session', 'task', 'plan', 'project', 'paths', 'checks', 'tests')


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


def answer(root, request_id, choice, *, project, directory, control_root):
    """Frontend-only action. A late answer cannot authorize a replacement request."""
    if choice not in {'once', 'project', 'deny'}:
        raise ValueError('Choose Allow for this task, Always allow in this project, or Deny.')
    folder = regular(Path(root)/'execution-permission')
    with decision_lock(folder):
        request = pending(root)
        if not request or request['id'] != request_id or Path(request['project']) != Path(project).resolve():
            raise ValueError('This permission request changed or ended. Nothing was authorized.')
        if choice == 'project':
            from .project_permissions import read_defaults, save_defaults
            defaults, revision = read_defaults(directory, project, control_root)
            # Remember file changes and the project's checks. Do not enable
            # arbitrary commands, disable isolation or adopt project instructions.
            defaults.update(mode='commands' if defaults['mode'] == 'commands' else 'edit', write_paths=['.'])
            save_defaults(directory, project, control_root, defaults, revision)
        save_json(regular(folder/'answer.json'), {'id': request_id, 'choice': choice})
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
    from .project_permissions import read_defaults
    config = deepcopy(state['config'])
    unit = task['plan']['unit']
    repository, paths = unit['repository'], unit['target_paths']
    project = config['repositories'][repository]
    config = with_review_policy(config, directory, project, relay_mesh.RELAY_ROOT)
    if paths_allowed(paths, config.get('writable', {}).get(repository, [])):
        return config
    if directory is None:
        raise ValueError('This plan needs permission for its edit scope. No interactive permission frontend is connected; no implementation started.')
    defaults, _ = read_defaults(directory, project, relay_mesh.RELAY_ROOT)
    if defaults['mode'] != 'read' and paths_allowed(paths, defaults['write_paths']):
        config.setdefault('writable', {})[repository] = list(paths)
        return config
    folder = regular(Path(root)/'execution-permission'); folder.mkdir(exist_ok=True)
    request = {'schema': SCHEMA, 'session': state['session_id'], 'effect': effect_id,
        'task': task['id'], 'plan': task['plan']['sha256'], 'project': str(Path(project).resolve()),
        'paths': list(paths), 'checks': {n: check['argv'] for n, check in config['verifiers'].items()
                                      if check['repository'] == repository},
        'tests': list(unit.get('test_paths', [])),
        'objective': task['objective'][:2000], 'status': 'WAITING'}
    request['id'] = identity(request)
    # "Allow for this task" covers its corrections: the same task, saved plan, files and checks ask only once.
    granted, grant = regular(folder/'granted.json'), {k: request[k] for k in GRANT}
    if granted.exists() and read_json(granted, 32768) == grant:
        config.setdefault('writable', {})[repository] = list(paths)
        return config
    with custody_lock(folder):
        regular(folder/'answer.json').unlink(missing_ok=True)
        save_json(regular(folder/'request.json'), request)
    try:
        while not control.should_stop():
            response = regular(folder/'answer.json')
            if response.exists():
                value = read_json(response, 4096)
                if value.get('id') == request['id'] and value.get('choice') in {'once', 'project', 'deny'}:
                    if value['choice'] == 'deny':
                        raise PermissionError('The user denied this execution. No implementer started. Do not ask again in this turn.')
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

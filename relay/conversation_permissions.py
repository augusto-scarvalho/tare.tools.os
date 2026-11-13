"""An operator decision for a saved plan (and, in local-only mode, for applying a change whose audit was not
independent), outside the coordinator's capabilities.

The worker waits without making model calls. Only its frontend writes the answer;
the plan and effect identity bind it to this execution. No session prompt, tools or
history are rewritten when the operator grants access.
"""
from copy import deepcopy
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import time

from compute_plane.native_journal import custody_lock, regular, save_json
from compute_plane.native_models import encode

from .worker_wait import waiting_for_user

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
        if request.get('kind') == 'audit' and choice not in ('once', 'deny'):
            raise ValueError('Apply the change or do not apply it. Nothing was authorized.')
        if request.get('kind') == 'sandbox' and choice == 'change':
            raise ValueError('Run the check outside the sandbox or do not run it. Nothing was authorized.')
        if request.get('kind') == 'sandbox' and choice == 'project' and request['sandbox'].get('always') is False:
            raise ValueError("A local model's checks are asked about each turn: run it this turn or do not run it. "
                             'Nothing was authorized.')
        if request.get('kind') == 'sandbox' and choice == 'project':
            # Only this: no edit access, mode or command protection changes with it. /permissions shows and clears it.
            from .turn_engine import set_checks_outside_sandbox
            set_checks_outside_sandbox(request['project'], True)
        elif choice == 'project':
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
    value = wait_for_answer(folder, request, control)
    if value is None:
        raise PermissionError('Execution permission was not granted before cancellation or the time limit. No implementer started.')
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


def wait_for_answer(folder, request, control):
    """Publish one question for the conversation's TUI and return its answer, or None if the turn stops first.
    The turn's time stops while the user reads and decides, and no inference runs."""
    from .native_agent import read_json
    request['id'] = identity(request)
    with custody_lock(folder):
        regular(folder/'answer.json').unlink(missing_ok=True)
        save_json(regular(folder/'request.json'), request)
    try:
        with waiting_for_user(control):
            while not control.should_stop():
                response = regular(folder/'answer.json')
                if response.exists():
                    value = read_json(response, 4096)
                    if value.get('id') == request['id'] and value.get('choice') in CHOICES:
                        return value
                time.sleep(.1)  # with a TurnClock only cancellation or a stale binding ends it
        return None
    finally:
        with decision_lock(folder):
            request['status'] = 'CLOSED'
            save_json(regular(folder/'request.json'), request)


APPLY_QUESTION = ('Local-only mode: the auditor accepted this change, but it is the same model family as the '
                  'implementer, so the audit is not independent. Apply the change to the project?')
NOT_CONFIRMED = ('The user did not confirm applying a change whose audit was not independent '
                 '(same-family audit, local-only mode). The candidate remains unapplied.')
APPLY_UNACCEPTED_QUESTION = ('Local-only mode: the auditor did NOT accept this change. It left the open items below, '
    'and it is the same model family as the implementer, so its audit is not independent either. The checks passed. '
    'Apply the change anyway?')
NOT_APPLIED_UNACCEPTED = ('The user chose not to apply a change that the same-family audit did not accept '
                          '(local-only mode). The candidate remains unapplied.')
USER_APPLIES = 'APPLY_WITHOUT_ACCEPTED_AUDIT'  # host-set only: run_unit applies a not-accepted review on this alone
OPEN_ITEMS, OPEN_ITEM_CHARS = 10, 500


def confirm_apply(root, state, task, changed, review, effect_id, control, directory):
    """--local_only: before applying a candidate that a same-family auditor accepted, or that it left with open items
    only (no findings, checks passed), ask the user. Without a TUI (tare serve, headless) nobody can confirm, and
    outside local-only mode (a conversation reopened without it) the rule is independence, so nothing is applied:
    an ACCEPT is refused as when no independent auditor exists, a not-accepted review stays as it was."""
    from .conversation_review import NO_AUDITOR
    from .execution_scope import current
    accepted = review['verdict'] == 'ACCEPT'
    if not accepted and (review['verdict'] != 'INCONCLUSIVE' or review.get('findings') or not review.get('remaining')):
        return review  # findings go to the correction path; nobody is asked to apply over them or without items
    refused = {'verdict': 'INCONCLUSIVE', 'audit_label': review['audit_label'],
               **({'actor': review['actor']} if review.get('actor') else {})}
    if directory is None or current().only != 'local':
        return {**refused, 'reason': NO_AUDITOR} if accepted else review
    unit = task['plan']['unit']
    folder = regular(Path(root)/'execution-permission')
    folder.mkdir(exist_ok=True)
    items = [str(item)[:OPEN_ITEM_CHARS] for item in review.get('remaining', [])]
    request = {'schema': SCHEMA, 'kind': 'audit', 'session': state['session_id'], 'effect': effect_id,
        'task': task['id'], 'plan': task['plan']['sha256'],
        'project': str(Path(state['config']['repositories'][unit['repository']]).resolve()),
        'paths': sorted(changed), 'checks': {}, 'tests': [],
        'objective': APPLY_QUESTION if accepted else APPLY_UNACCEPTED_QUESTION, 'status': 'WAITING',
        'audit': {'label': review['audit_label'], 'verdict': review['verdict'], 'route': review.get('route'),
                  'summary': str(review.get('summary', ''))[:2000],
                  **({} if accepted else {'open_items': items[:OPEN_ITEMS], 'open_items_total': len(items)})}}
    while len(json.dumps(request, ensure_ascii=False).encode()) > 30000 and request['audit'].get('open_items'):
        request['audit']['open_items'].pop()  # the TUI reads at most 32 KiB; the receipt keeps every item
    if not accepted and not request['audit']['open_items']:
        return review  # the user must see the open items before applying over them
    value = wait_for_answer(folder, request, control)
    if not value or value['choice'] != 'once':
        return {**refused, 'reason': NOT_CONFIRMED} if accepted else {**review, 'reason': NOT_APPLIED_UNACCEPTED}
    return review if accepted else {**review, 'user_decision': USER_APPLIES}


SANDBOX_QUESTION = 'Run this check outside the sandbox?'
SANDBOX_TURN = 'sandbox-turn.json'  # this conversation's yes for the turn: {'invocation': ...}
BLOCKED = {  # what the sandbox stopped, in plain words, by limitation code
    'ISOLATION_NETWORK_DENIED': 'a network connection on this machine (the sandbox denies every connection, '
                                'loopback included)',
    'ISOLATION_SECRET_DENIED': 'a secret file such as .env (the sandbox denies secret files to every check)',
    'ISOLATION_INTERNAL_IPC_UNAVAILABLE': 'a loopback connection Python makes for itself (the sandbox denies '
                                          'loopback)'}
BLOCKED_LINUX = {**BLOCKED, 'ISOLATION_NETWORK_DENIED': 'a network connection (the sandbox has its own loopback, '
                 'but it cannot reach the network or the services of this machine)'}


def sandbox_question(limitation):
    """(the question's text, the output line that shows the block or '')."""
    code, _, detail = limitation.partition(': ')
    blocked = BLOCKED_LINUX if sys.platform == 'linux' else BLOCKED
    if code in blocked:
        return 'The sandbox blocked ' + blocked[code] + ', so tare cannot judge this check inside it.', ''
    if code == 'ISOLATION_BLOCKED_DURING':  # a red check: the rerun outside would decide it
        what, _, line = detail.partition('. Output: ')
        return ('This check failed, and the sandbox blocked ' + what + ' during it, so the failure may come from the '
                'sandbox rather than from the code. Run outside, its result decides.'), line[:200]
    return (detail.split('. Not run')[0] if code in ('ISOLATION_UNAVAILABLE', 'ISOLATION_TIMEOUT') else limitation), ''


def local_work(local_producer):
    """Whether the code under check is a local model's: its producer is local, or local-only mode, where every
    implementer is. A local model runs only where its checks are isolated, so the saved "always" never covers it."""
    from .execution_scope import current
    return bool(local_producer) or current().only == 'local'


def outside_sandbox(root, state, task, effect_id, project, control, directory, invocation_id, *, local_producer=False):
    """`outside` for a conversation's checks (command_protection.conversation_verifier): may a check the sandbox
    blocked run outside it? The project's saved "always" (every frontend; never for a local model's work), then this
    turn's yes, then the user, asked while the turn's clock stops. Without a TUI (directory None) nobody is asked and
    nothing runs outside. `local_producer`: a local model wrote the code (run_unit passes it per implementer)."""
    from .native_agent import read_json
    from .turn_engine import checks_outside_sandbox
    folder = regular(Path(root)/'execution-permission')
    turn = {'invocation': invocation_id}

    def decide(name, argv, limitation, *, local_producer=local_producer):
        local = local_work(local_producer)
        if not local and checks_outside_sandbox(project):
            return True, 'always for this project, as the user chose'
        if (folder/SANDBOX_TURN).is_file() and read_json(regular(folder/SANDBOX_TURN), 4096) == turn:
            return True, 'for this turn, as the user chose'
        if directory is None:
            return False, 'no one could be asked: no TUI is connected'
        folder.mkdir(exist_ok=True)
        question, line = sandbox_question(limitation)
        request = {'schema': SCHEMA, 'kind': 'sandbox', 'session': state['session_id'], 'effect': effect_id,
            'task': (task or {}).get('id', ''), 'plan': ((task or {}).get('plan') or {}).get('sha256', ''),
            'project': str(Path(project).resolve()), 'paths': [], 'checks': {name: list(argv)}, 'tests': [],
            'objective': question[:2000], 'status': 'WAITING',
            'sandbox': {'code': limitation.partition(':')[0], 'check': name, **({'line': line} if line else {}),
                        **({'always': False} if local else {})}}  # no "always" button for a local model's work
        value = wait_for_answer(folder, request, control)
        if not value or value['choice'] == 'deny':
            return False, 'the user chose not to run it outside the sandbox'
        if value['choice'] == 'once':
            save_json(regular(folder/SANDBOX_TURN), turn)
            return True, 'for this turn, as the user chose'
        return True, 'always for this project, as the user chose'
    return decide

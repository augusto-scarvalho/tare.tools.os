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

from .model_signals import shown
from .worker_wait import waiting_for_user

SCHEMA = 'tare.tools/execution-permission/1'
GRANT = ('session', 'task', 'plan', 'project', 'paths', 'checks', 'tests')
CHOICES = {'once', 'project', 'deny', 'change', 'outside', 'aside', 'background'}  # outside/background: stall; aside: red_baseline


class PlanChangeRequested(PermissionError):
    """The user sent the shown plan back for changes; nothing was implemented."""


def review_lines(artifact):
    """What the critic of a critical plan said, from the receipt beside the plan's artifact (planning/<job>/); nothing
    when there was no review. First in the text: it is what the user should read before saying yes."""
    from .native_agent import read_json
    path = artifact.parent.parent/'receipt.json'
    try:
        result = read_json(regular(path), 65536).get('result') or {} if path.is_file() else {}
        review = result.get('review') or {}
        if review.get('status') != 'REVIEWED' or Path(result['artifact']).resolve() != artifact.resolve():
            return []
        own = review.get('own_model') is True  # the user ticked the planner's own local model as plan reviewer
        if review['verdict'] == 'ACCEPT' and not review.get('findings'):
            return ['A reviewer of another vendor accepted this plan.' if review.get('independent') else
                    "The planner's own model, asked again as a critic, accepted this plan (not independent)." if own else
                    'A reviewer accepted this plan (reviewed by the same vendor: not independent).', '']
        revised = review.get('revised') is True  # the plan below is then the planner's one revision with the findings
        who = ('Independent review' if review.get('independent') else
               "Review by the planner's own model as a critic (not independent)" if own else
               'Review by the same vendor (not independent)')
        return [who+' of '+('the first' if revised else 'this')+' plan: '+review['verdict'], *(
            f'- {f["claim"]}'+(' (Files: '+', '.join(f['paths'])+')' if f.get('paths') else '') for f in review['findings']),
            'The planner revised the plan once with these findings; the plan below is the revised one.' if revised else
            'The reviewer only advises. Implement as it is, or change the plan.', '']
    except (OSError, ValueError, KeyError, TypeError):
        return []  # a review that cannot be read never hides the plan


def plan_text(root, request):
    """The whole saved plan, readable, from the artifact whose sha256 the request binds."""
    path = regular(Path(request['artifact']))
    if not path.resolve().is_relative_to((Path(root)/'planning').resolve()):
        raise ValueError('The plan is outside this conversation.')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != request['plan']:
        raise ValueError('The saved plan changed after it was proposed.')
    plan = json.loads(raw)['payload']
    out = [*review_lines(path), plan.get('summary', ''), '', 'Steps:', *(f'{n}. {s}' for n, s in enumerate(plan.get('steps', []), 1))]
    for task in plan.get('tasks', []):
        out += ['', '• '+task['title'], '  '+task.get('summary', ''), '  Files: '+', '.join(task['target_paths'])]
        out += ['  Done when: '+c for c in task.get('criteria', [])]
        if task.get('test_paths') or task.get('verifiers'):
            out.append('  Checks: '+', '.join([*task.get('verifiers', []), *task.get('test_paths', [])]))
        # What the planner said that chooses the implementer: the user sees it before it runs, and "ask for changes"
        # is how to correct it.
        hints = [*(['needs a '+shown(task['needs'])+' model'] if task.get('needs') else []),
                 *([task['kind']+' work'] if task.get('kind') else []),
                 *(['critical: the best model for it, whatever it costs'] if task.get('critical') else []),
                 *(['effort '+task['effort']] if task.get('effort') else [])]
        if hints:
            out.append('  Model: '+'; '.join(hints))
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
        if request.get('kind') == 'checks' and choice not in ('once', 'deny'):
            raise ValueError('Save .tare/checks.json or do not save it. Nothing was changed.')
        stall = request.get('kind') == 'stall'
        if (stall and choice not in ('once', 'deny', 'outside', 'background')
                or choice == 'outside' and not (stall and request['stall'].get('outside'))
                or choice == 'background' and not stall):
            raise ValueError('Keep waiting or stop it. Nothing was stopped or authorized.')
        baseline = request.get('kind') == 'baseline'
        if (baseline and choice not in ('once', 'deny', 'aside')
                or choice == 'aside' and not (baseline and request['baseline'].get('aside'))):
            raise ValueError('Continue, continue without the failing checks where offered, or stop. '
                             'Nothing was authorized.')
        if request.get('kind') == 'sandbox' and choice == 'change':
            raise ValueError('Run the check outside the sandbox or do not run it. Nothing was authorized.')
        if request.get('kind') == 'sandbox' and choice == 'project' and request['sandbox'].get('always') is False:
            raise ValueError("A local model's checks are asked about each turn: run it this turn or do not run it. "
                             'Nothing was authorized.')
        if request.get('kind') == 'gui_check':
            if choice not in ('once', 'project', 'deny'):
                raise ValueError('Run this GUI check on your desktop or skip it. Nothing was authorized.')
            if choice == 'project' and (request.get('gui_check') or {}).get('always') is False:
                raise ValueError("A local model's GUI checks are asked about each turn: run it once or skip it. "
                                 'Nothing was authorized.')
        if request.get('kind') == 'gui_check' and choice == 'project':
            from .turn_engine import set_gui_check_allowed
            gui_name, gui_argv = next(iter(request['checks'].items()))
            set_gui_check_allowed(request['project'], gui_name, gui_argv, True)
        elif request.get('kind') == 'sandbox' and choice == 'project':
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
APPLY_UNACCEPTED_INDEPENDENT_QUESTION = ('The independent auditor found no defects, but did NOT accept this change '
    'because the open items below could not be verified here. The checks passed. Apply the change anyway?')
NOT_APPLIED_UNACCEPTED_INDEPENDENT = ('The user chose not to apply a change that the independent audit left '
                                      'inconclusive. The candidate remains unapplied.')
USER_APPLIES = 'APPLY_WITHOUT_ACCEPTED_AUDIT'  # host-set only: run_unit applies a not-accepted review on this alone
OPEN_ITEMS, OPEN_ITEM_CHARS = 10, 500


def confirm_apply(root, state, task, changed, review, effect_id, control, directory):
    """Ask the user in an interactive TUI before applying a candidate whose audit requires confirmation:
    - Under --local_only: a same-family ACCEPT, or a same-family INCONCLUSIVE with open items only (no findings).
      Outside --local_only or without a TUI, same-family ACCEPT is refused and same-family INCONCLUSIVE stays as-is.
    - Outside or inside --local_only: an independent completed INCONCLUSIVE with zero findings and explicit open items
      (checks passed), only when an interactive TUI can show the open items (never headless / tare serve)."""
    from .conversation_review import NO_AUDITOR
    from .execution_scope import current
    accepted = review['verdict'] == 'ACCEPT'
    same_family = bool(review.get('audit_label'))
    if not accepted and (review['verdict'] != 'INCONCLUSIVE' or review.get('findings')
                         or not review.get('remaining') or review.get('failure_code') or review.get('reason')):
        return review  # findings and failures never ask to apply over them
    if same_family:
        refused = {'verdict': 'INCONCLUSIVE', 'audit_label': review['audit_label'],
                   **({'actor': review['actor']} if review.get('actor') else {}),
                   **({'agent_signals': review['agent_signals']} if review.get('agent_signals') else {})}
        if directory is None or current().only != 'local':
            return {**refused, 'reason': NO_AUDITOR} if accepted else review
    else:
        if accepted or directory is None:
            return review
    unit = task['plan']['unit']
    folder = regular(Path(root)/'execution-permission')
    folder.mkdir(exist_ok=True)
    items = [str(item)[:OPEN_ITEM_CHARS] for item in review.get('remaining', [])]
    objective = (APPLY_QUESTION if accepted else
                 APPLY_UNACCEPTED_QUESTION if same_family else APPLY_UNACCEPTED_INDEPENDENT_QUESTION)
    audit_label = review['audit_label'] if same_family else (review.get('independence') or 'independent')
    request = {'schema': SCHEMA, 'kind': 'audit', 'session': state['session_id'], 'effect': effect_id,
        'task': task['id'], 'plan': task['plan']['sha256'],
        'project': str(Path(state['config']['repositories'][unit['repository']]).resolve()),
        'paths': sorted(changed), 'checks': {}, 'tests': [],
        'objective': objective, 'status': 'WAITING',
        'audit': {'label': audit_label, 'verdict': review['verdict'], 'route': review.get('route'),
                  'summary': str(review.get('summary', ''))[:2000],
                  **({} if accepted else {'open_items': items[:OPEN_ITEMS], 'open_items_total': len(items)})}}
    while len(json.dumps(request, ensure_ascii=False).encode()) > 30000 and request['audit'].get('open_items'):
        request['audit']['open_items'].pop()  # the TUI reads at most 32 KiB; the receipt keeps every item
    if not accepted and not request['audit']['open_items']:
        return review  # the user must see the open items before applying over them
    value = wait_for_answer(folder, request, control)
    if not value or value['choice'] != 'once':
        if same_family:
            return {**refused, 'reason': NOT_CONFIRMED} if accepted else {**review, 'reason': NOT_APPLIED_UNACCEPTED}
        return {**review, 'reason': NOT_APPLIED_UNACCEPTED_INDEPENDENT}
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
    return (detail.split('. Not run')[0] if code == 'ISOLATION_UNAVAILABLE' else limitation), ''


def duration(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    return f'{minutes} min {seconds} s' if minutes else f'{seconds} s'


def stall_question(root, state, task, effect_id, project, control, directory):
    """work_units.STALL_ASK of a conversation: the question about a check or command that showed no activity for
    work_units.STALL_SECONDS, asked while the turn's clock stops; None without a TUI (directory None: tare serve,
    scripted runs), where nobody can be asked. ask(name, argv, sample, outside) -> 'once' (keep waiting), 'deny' (stop
    it), 'outside' (stop it and run it outside the sandbox, offered only with `outside`), or None (the turn stopped).
    It states facts only: a silent wait has many causes, a connection the sandbox refused among them."""
    if directory is None:
        return None
    folder = regular(Path(root)/'execution-permission')

    def ask(name, argv, sample, outside=False):
        from .work_units import STALL_SECONDS
        folder.mkdir(exist_ok=True)
        active = sample['idle_ms'] < STALL_SECONDS * 1000 and sample.get('output_idle_ms', 0) > 0
        label = f'The check {name}' if name else 'The command'
        objective = (
            f'{label} has run for {duration(sample["elapsed_ms"]/1000)} and has produced no new output for the last '
            f'{duration(sample["output_idle_ms"]/1000)}, while still using CPU '
            f'({duration(sample.get("cpu_ms", 0)/1000)} total). tare has not stopped it.'
            if active else
            f'{label} has run for {duration(sample["elapsed_ms"]/1000)} and has shown no activity for the last '
            f'{duration(sample["idle_ms"]/1000)}: in that time it wrote no output and used no CPU time. '
            'tare has not stopped it.')
        tail = str(sample.get('output_tail') or '').strip()
        if tail:
            tail = '\n'.join(tail.splitlines()[-15:])[-1500:]
        stall_info = {'check': name or '', 'outside': bool(outside)}
        if active:
            stall_info['reason'] = 'quiet_output'
        if tail:
            stall_info['output_tail'] = tail
        request = {'schema': SCHEMA, 'kind': 'stall', 'session': state['session_id'], 'effect': effect_id,
            'task': (task or {}).get('id', ''), 'plan': ((task or {}).get('plan') or {}).get('sha256', ''),
            'project': str(Path(project).resolve()), 'paths': [], 'checks': {name or 'command': list(argv)},
            'tests': [], 'status': 'WAITING', 'objective': objective, 'stall': stall_info}
        value = wait_for_answer(folder, request, control)
        return value and value['choice']
    return ask


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


GUI_CHECK_TURN = 'gui-check-turn.json'  # this turn's exact GUI check approvals: [{'invocation', 'binding'}]


def gui_check_permission(root, state, task, effect_id, project, control, directory, invocation_id, *, local_producer=False):
    """Explicit desktop consent for declared `.tare/checks.json` `mode: "gui"` checks with `argv`.

    Always fails closed when no interactive TUI is connected (`directory is None`), even if an exact
    command was remembered for the project. Remembered approvals bind exact `(name, argv)` digests and
    are never honored or offered for a local model's work.
    """
    from .native_agent import read_json
    from .turn_engine import gui_check_allowed, gui_check_binding
    import os
    folder = regular(Path(root)/'execution-permission')
    project_key = os.path.normcase(str(Path(project).resolve()))

    def decide(name, argv, description='', *, local_producer=local_producer):
        if directory is None:
            return False, 'no interactive TUI is connected; GUI checks require explicit desktop confirmation'
        local = local_work(local_producer)
        binding = gui_check_binding(name, argv)
        if not local and gui_check_allowed(project, name, argv):
            return True, 'always for this exact GUI check in this project, as the user chose'
        turn_file = regular(folder/GUI_CHECK_TURN)
        turn_rows = []
        if turn_file.is_file():
            try:
                loaded = read_json(turn_file, 16384)
                grants = loaded.get('grants') if isinstance(loaded, dict) and loaded.get('invocation') == invocation_id else None
                if isinstance(grants, list):
                    turn_rows = [r for r in grants if isinstance(r, dict)]
            except (OSError, ValueError):
                turn_rows = []
        if any(r.get('project') == project_key and r.get('binding') == binding
               and (not local or r.get('local') is True) for r in turn_rows):
            return True, 'for this turn, as the user chose'
        folder.mkdir(exist_ok=True)
        objective = (
            f'Run the project\'s declared GUI check "{name}" directly on your desktop outside the sandbox? '
            'This opens a desktop window and runs as you.'
        )[:2000]
        request = {
            'schema': SCHEMA,
            'kind': 'gui_check',
            'session': state['session_id'],
            'effect': effect_id,
            'task': (task or {}).get('id', ''),
            'plan': ((task or {}).get('plan') or {}).get('sha256', ''),
            'project': str(Path(project).resolve()),
            'paths': [],
            'checks': {name: list(argv)},
            'tests': [],
            'objective': objective,
            'status': 'WAITING',
            'gui_check': {
                'check': name,
                **({'description': str(description)[:240]} if description else {}),
                **({'always': False} if local else {}),
            },
        }
        value = wait_for_answer(folder, request, control)
        if not value or value['choice'] == 'deny':
            return False, 'the user chose to skip this GUI check'
        if value['choice'] == 'once':
            turn_rows.append({'project': project_key, 'binding': binding, 'local': bool(local)})
            save_json(turn_file, {'invocation': invocation_id, 'grants': turn_rows})
            return True, 'for this turn, as the user chose'
        return True, 'always for this exact GUI check in this project, as the user chose'
    return decide


BASELINE_TURN = 'baseline-turn.json'  # this turn's answer: {'invocation', 'checks': [names], 'choice'}


def red_baseline(root, state, task, effect_id, project, control, directory, invocation_id):
    """run_unit's `red_baseline` for a conversation: checks fail on the unchanged project, before the implementer
    starts. ask(checks, output, aside, named) -> 'once' (continue: the change must make them pass), 'aside' (continue
    without them, offered only with `aside`), 'deny' (stop) or None (the turn stopped). An answer holds for the rest
    of the turn for the same failing checks, and is never saved beyond it. None without a TUI (directory None:
    tare serve, scripted runs): nobody is asked and nothing changes."""
    if directory is None:
        return None
    from .native_agent import read_json
    folder = regular(Path(root)/'execution-permission')

    def ask(checks, output, aside, named=False):  # named: every failing check is a test the plan names
        turn = {'invocation': invocation_id, 'checks': sorted(checks)}
        saved = regular(folder/BASELINE_TURN)
        if saved.is_file():
            value = read_json(saved, 32768)
            if {k: value.get(k) for k in turn} == turn and value.get('choice') in CHOICES:
                return value['choice']
        folder.mkdir(exist_ok=True)
        names = ', '.join(sorted(checks))
        request = {'schema': SCHEMA, 'kind': 'baseline', 'session': state['session_id'], 'effect': effect_id,
            'task': (task or {}).get('id', ''), 'plan': ((task or {}).get('plan') or {}).get('sha256', ''),
            'project': str(Path(project).resolve()), 'paths': [], 'checks': {n: list(a) for n, a in checks.items()},
            'tests': [], 'status': 'WAITING',
            'objective': ('Before any change, the project already fails ' + ('this check: ' if len(checks) == 1
                          else 'these checks: ') + names + '. No implementer has started.')[:2000],
            'baseline': {'aside': bool(aside), 'named': bool(named), 'output': output[-4000:]}}
        value = wait_for_answer(folder, request, control)
        if not value:
            return None  # the turn stopped: no answer to remember
        save_json(saved, {**turn, 'choice': value['choice']})
        return value['choice']
    return ask


def confirm_checks(root, state, *, project, proposal, from_role, summary, reason,
                   effect_id, control, directory, task=None):
    """Validate a `propose_checks` payload, show an exact diff in the TUI, and write `<project>/.tare/checks.json`
    atomically only on explicit user approval (`choice == 'once'`). Headless (`directory is None`) always refuses
    fail-closed without writing.
    """
    from .agent_signals import ADVISORY_AUTHORITY, prepare_checks_proposal
    from .conversation_tasks import _uses_detected_checks, detected_checks
    from .turn_engine import write_project_checks_doc

    config = state.get('config') or {}
    active_now = _uses_detected_checks(config)
    prepared = prepare_checks_proposal(project, proposal)
    base = {
        'path': '.tare/checks.json',
        'from_role': from_role,
        'authority': ADVISORY_AUTHORITY,
        'summary': str(summary)[:300],
        'reason': str(reason)[:1000],
        'added': prepared['added'],
        'modified': prepared['modified'],
        'removed': prepared['removed'],
    }
    if not prepared['has_changes']:
        return {**base, 'status': 'UNCHANGED', 'saved': False,
                'next': '.tare/checks.json already matches the proposed checks; nothing changed.'}
    if directory is None:
        return {**base, 'status': 'CONSENT_REQUIRED', 'saved': False,
                'diff': prepared['diff_text'][:4000],
                'next': 'No interactive TUI is connected, so .tare/checks.json was not written. '
                        'Show the proposed diff to the user so they can review or edit .tare/checks.json directly.'}
    folder = regular(Path(root)/'execution-permission')
    folder.mkdir(exist_ok=True)
    changed_names = set(prepared['added']) | set(prepared['modified'])
    doc_sha256 = hashlib.sha256(encode(prepared['document']).encode('utf-8')).hexdigest()
    request = {
        'schema': SCHEMA,
        'kind': 'checks',
        'session': state['session_id'],
        'effect': effect_id,
        'task': (task or {}).get('id', ''),
        'plan': doc_sha256,
        'project': str(Path(project).resolve()),
        'paths': ['.tare/checks.json'],
        'checks': {n: list(entry['argv']) for n, entry in prepared['after'].items()
                   if n in changed_names and 'argv' in entry},
        'tests': [],
        'objective': f'Proposed by {from_role}: {summary}'[:2000],
        'status': 'WAITING',
        'checks_proposal': {
            'from_role': from_role,
            'existed': prepared['existed'],
            'active_now': active_now,
            'summary': str(summary)[:300],
            'reason': str(reason)[:1000],
            'added': prepared['added'],
            'modified': prepared['modified'],
            'removed': prepared['removed'],
            'unchanged_count': len(prepared['unchanged']),
            'diff': prepared['diff_text'],
        },
    }
    # Fail closed rather than truncating: the user must see 100% of every check change before approving.
    if len(json.dumps(request, ensure_ascii=False).encode('utf-8')) > 30000:
        raise ValueError(
            'Proposed .tare/checks.json diff is too large to display completely for approval; '
            'split the proposal into smaller changes or edit .tare/checks.json directly.')
    value = wait_for_answer(folder, request, control)
    if not value:
        return {**base, 'status': 'CANCELLED', 'saved': False,
                'reason': 'No decision was recorded before cancellation or timeout. .tare/checks.json was not changed.'}
    if value['choice'] != 'once':
        return {**base, 'status': 'DENIED', 'saved': False,
                'reason': 'The user chose not to save .tare/checks.json. Do not propose this again in this turn.'}
    _, _, parsed = write_project_checks_doc(project, prepared['document'], expected_sha256=prepared['existing_sha256'])
    if active_now:
        resolved_project = Path(project).resolve()
        repos = config.get('repositories') or {}
        matching_repos = {repo for repo, root_path in repos.items() if Path(root_path).resolve() == resolved_project}
        catalog = detected_checks(config) if matching_repos else {}
        registered = sorted(name for name, entry in catalog.items() if entry.get('repository') in matching_repos)
        active_checks = registered if matching_repos else sorted(parsed['verifiers'])
        next_msg = '.tare/checks.json was saved and is active immediately for this conversation.'
    else:
        active_checks = []
        next_msg = ('.tare/checks.json was saved, but this conversation uses explicit scoped verifiers that take '
                    'precedence; it will take effect in new conversations without explicit verifiers.')
    return {
        **base,
        'status': 'SAVED',
        'saved': True,
        'active_in_conversation': active_now,
        'active_checks': active_checks,
        'manual_checks': sorted(parsed['manual']),
        'next': next_msg,
    }

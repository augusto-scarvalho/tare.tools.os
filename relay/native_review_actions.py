"""Explicit operator review actions; Kernel owns the actual file mutation."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path

from compute_plane.mediated_tools import MediatedTools
from compute_plane.native_journal import NativeJournal, custody_lock, regular, save_json
from compute_plane.native_review import capture, digest, receipt_path, restore_bytes


def undo_path(root, effect_id):
    return receipt_path(root/'review'/'undos', effect_id)


def undo_observation(root, session_id, effect_id):
    """An incomplete/corrupt receipt never enables a second mutation."""
    from .native_agent import read_json
    try:
        path = undo_path(root, effect_id)
        if not path.exists():
            return None
        receipt = read_json(path, 8192)
        if (receipt.get('schema') != 'tare.tools/edit-undo/1'
                or receipt.get('session_id') != session_id or receipt.get('effect_id') != effect_id
                or type(receipt.get('journal_sequence')) is not int or receipt['journal_sequence'] < 0):
            return 'UNAVAILABLE'
        original = read_json(receipt_path(root/'review',effect_id),8192)
        if (original.get('schema') != 'tare.tools/edit-review/1' or original.get('effect_id') != effect_id
                or any(key not in receipt or original.get(key) != receipt[key] for key in ('repository','path','before','after'))):
            return 'UNAVAILABLE'
        return 'UNDONE' if receipt.get('status') == 'UNDONE' else 'UNKNOWN'
    except (OSError, ValueError, TypeError):
        return 'UNAVAILABLE'


def undo_boundary(root, effect_id):
    """Checks recorded after restoration can become fresh through normal rules."""
    from .native_agent import read_json
    try:
        value = read_json(undo_path(root, effect_id), 8192).get('journal_sequence')
        return value if type(value) is int and value >= 0 else None
    except (OSError, ValueError, TypeError):
        return None


@contextmanager
def review_custody(root, *, write=False):
    from .native_agent import configuration, read_json, workspace_locks
    root = regular(root)
    if not regular(root/'journal.jsonl').is_file() or not regular(root/'writer.lock').is_file():
        raise ValueError('saved_session_journal_and_lock_required')
    with ExitStack() as stack:
        stack.enter_context(custody_lock(root))
        state = read_json(root/'native-agent.json', 1024 * 1024)
        config = configuration(state['config'])
        if write:
            from .native_review_checks import ensure_checks_quiescent
            ensure_checks_quiescent(root)
            workspace_locks(stack, config['repositories'])
        records = NativeJournal(root/'journal.jsonl').snapshot().records
        events = [record.event() for record in records]
        if (not events or events[0]['kind'] != 'session_started'
                or events[0]['session_id'] != state['session_id']):
            raise ValueError('Saved metadata and journal belong to different conversations.')
        capabilities = events[0].get('capabilities', [])
        if write and 'edit' not in capabilities and not (state.get('conversation_flow') and 'implement' in capabilities):
            raise ValueError('The recorded conversation does not permit file edits.')
        yield root, state, config, records


@contextmanager
def selected_edit(root, *, project, effect_id, invocation_id=None, write=False):
    with review_custody(root, write=write) as (root, state, config, records):
        events = [record.event() for record in records]
        start, end = 0, len(events)
        if invocation_id is not None:
            turn = next((row for row in state.get('invocations', []) if row['id'] == invocation_id), None)
            if (turn is None or type(turn.get('journal_start')) is not int or type(turn.get('journal_end')) is not int
                    or not 0 <= turn['journal_start'] <= turn['journal_end'] <= end):
                raise ValueError('This turn has no recorded review boundaries.')
            start, end = turn['journal_start'], turn['journal_end']
        pending, selected = {}, None
        unresolved = set()
        for sequence, event in enumerate(events):
            if event['kind'] == 'effect_requested':
                pending[event['effect_id']] = event
                unresolved.add(event['effect_id'])
            elif event['kind'] == 'effect_returned':
                result = event['result']
                if result['status'] != 'unknown':
                    unresolved.discard(result['effect_id'])
                request = pending.get(result['effect_id'])
                if request is not None:
                    from .native_review import confirmed_edits
                    for edit_request, edit_result in confirmed_edits(request, result):
                        pending[edit_request['effect_id']] = edit_request
                        if (edit_result['effect_id'] == effect_id and edit_result['status'] == 'succeeded'
                                and start <= sequence < end):
                            selected = edit_request, edit_result
        if selected is None:
            raise ValueError('A confirmed edit in the selected turn is required.')
        if write and unresolved:
            raise ValueError('Reconcile unresolved operations before undoing an edit.')
        if write and any(undo_observation(root, state['session_id'], effect) in {'UNKNOWN', 'UNAVAILABLE'} for effect in pending):
            raise ValueError('A previous undo has an uncertain result; inspect it before further restoration.')
        request, result = selected
        args = json.loads(request['tool_call']['arguments_json'])
        repository = args['repository']
        if Path(project).resolve() != Path(config['repositories'][repository]).resolve():
            raise ValueError('This edit belongs to another project.')
        if write and 'edit' not in config.get('capabilities', ['edit']):
            raise ValueError('This conversation does not permit file edits.')
        target = MediatedTools(config).path(repository, args['path'], write=write)
        regular(target)
        yield root, state, request, result, target, len(events)


def read_edit_file(root, *, project, effect_id, invocation_id=None):
    with selected_edit(root, project=project, effect_id=effect_id, invocation_id=invocation_id) as (_, _, _, _, target, _):
        raw = capture(target)
        if raw is None:
            raise ValueError('The current file no longer exists.')
        if len(raw) > 256 * 1024 or raw.count(b'\n') > 20000:
            raise ValueError('File exceeds the interactive preview limit.')
        return {'path': str(target), 'content': raw.decode('utf-8'), 'sha256': digest(raw)}


def undo_edit(root, *, project, effect_id, invocation_id=None):
    from .native_review import edit_snapshots
    with selected_edit(root, project=project, effect_id=effect_id, invocation_id=invocation_id, write=True) as (root, state, request, result, target, sequence):
        if undo_observation(root, state['session_id'], effect_id) is not None:
            raise ValueError('Undo was already requested for this edit; refresh and inspect the recorded result.')
        before, after = edit_snapshots(root, request, result)
        args = json.loads(request['tool_call']['arguments_json'])
        receipt = {'schema': 'tare.tools/edit-undo/1', 'session_id': state['session_id'],
                   'effect_id': effect_id, 'repository': args['repository'], 'path': args['path'],
                   'before': digest(before), 'after': digest(after), 'status': 'UNKNOWN', 'journal_sequence': sequence}
        path = undo_path(root, effect_id)
        started = False

        def intent():
            nonlocal started
            regular(path.parent).mkdir(exist_ok=True)
            save_json(path, receipt)
            started = True

        try:
            restore_bytes(target, before, after, before_write=intent)
            save_json(path, {**receipt, 'status': 'UNDONE'})
        except (OSError, ValueError) as exc:
            if started:
                raise OSError('Undo is unconfirmed; the file may have been restored. Refresh and inspect it. '+str(exc)) from exc
            raise
        return {'status': 'UNDONE', 'effect_id': effect_id, 'path': str(target)}

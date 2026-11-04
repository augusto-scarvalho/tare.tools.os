"""Review confirmed session edits and verifier receipts without a model call."""
import argparse
import difflib
import json
from pathlib import Path, PurePosixPath
import sys

from compute_plane.native_journal import NativeJournal, custody_lock, regular
from compute_plane.native_review import capture, digest, edit_args, receipt_path
from tare_tools_agent_runtime import ConversationRejected

from relay.native_display import safe_print


def current_hash(repositories, key):
    repository, name = key
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
        raise ValueError('invalid_review_path')
    return digest(capture(regular(Path(repositories[repository]) / name)))


def edit_snapshots(root, request, result):
    from .native_agent import read_json
    args = edit_args(json.loads(request['tool_call']['arguments_json']))
    output = json.loads(result['output_json'])
    receipt = read_json(receipt_path(root/'review', request['effect_id']), 8192)
    expected = {'schema': 'tare.tools/edit-review/1', 'effect_id': request['effect_id'],
                'repository': args['repository'], 'path': args['path'],
                'before': args['sha256'], 'after': output['sha256']}
    if receipt != expected:
        raise ValueError('review_receipt_mismatch')
    raw = []
    for sha in (receipt['before'], receipt['after']):
        if sha is None:
            raw.append(None)
        else:
            if not isinstance(sha,str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                raise ValueError('invalid_review_digest')
            value = capture(root/'review'/'blobs'/sha)
            if value is None or digest(value) != sha:
                raise ValueError('review_blob_missing_or_corrupt')
            raw.append(value)
    return raw


def edit_diff(root, request, result, *, max_bytes=None):
    args = json.loads(request['tool_call']['arguments_json'])
    raw = [value if value is not None else b'' for value in edit_snapshots(root, request, result)]
    if max_bytes is not None and (sum(map(len,raw)) > max_bytes or sum(v.count(b'\n') for v in raw) > 20000):
        raise ValueError('File exceeds the interactive diff preview limit.')
    diff = []
    for line in difflib.unified_diff(raw[0].decode('utf-8').splitlines(keepends=True),
                                    raw[1].decode('utf-8').splitlines(keepends=True),
                                    fromfile='before/'+args['path'], tofile='after/'+args['path']):
        diff.append(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n')
    return ''.join(diff)


def confirmed_edits(request, result):
    """Native edits or host-applied candidate files, with their real parent effect."""
    if request['tool_call']['name'] == 'edit':
        return [(request, result)]
    if request['tool_call']['name'] != 'implement' or result['status'] != 'succeeded':
        return []
    output = json.loads(result['output_json'])
    if not output.get('applied'): return []
    rows = []
    for edit in output.get('review_edits', []):
        effect = edit['effect_id']
        if not effect.startswith(request['effect_id']+':'):
            raise ValueError('host_edit_parent_mismatch')
        args = {k: edit[k] for k in ('repository', 'path', 'sha256')}
        rows.append(({'effect_id': effect, 'tool_call': {'name': 'edit', 'arguments_json': json.dumps(args)}},
                     {'effect_id': effect, 'status': 'succeeded', 'output_json': json.dumps({'sha256': edit['after']})}))
    return rows


def summarize(root, state, events, *, start=0, end=None, include_diffs=True, diff_effect_id=None, max_diff_bytes=None):
    from compute_plane.native_terminal import MUTATIONS
    from .native_review_actions import undo_observation, undo_boundary
    end = len(events) if end is None else end
    requests, returned, observed = {}, set(), {}
    edits, checks, unresolved = [], [], []
    commands = {}  # handle -> the command a terminal_start recorded and its latest observed state
    last_edit = -1
    for sequence, event in enumerate(events):
        if event['kind'] == 'effect_requested':
            requests[event['effect_id']] = (event, dict(observed), sequence)
        elif event['kind'] == 'effect_returned':
            result = event['result']
            returned.add(result['effect_id'])
            request, baseline, _ = requests[result['effect_id']]
            name = request['tool_call']['name']
            args = json.loads(request['tool_call']['arguments_json'])
            output = json.loads(result['output_json'])
            selected = start <= sequence < end
            if name in MUTATIONS and result['status'] in {'succeeded','failed','unknown'}:
                observed.clear()
                last_edit = sequence  # Commands invalidate earlier verifier freshness too.
            for edit_request, edit_result in confirmed_edits(request, result):
                edit = json.loads(edit_request['tool_call']['arguments_json'])
                edit_id = edit_result['effect_id']
                if name == 'implement':
                    requests[edit_id] = (edit_request, baseline, sequence)
                    returned.add(edit_id)
                row = {'effect_id': edit_id, 'repository': edit['repository'],
                       'path': edit['path'], 'status': edit_result['status'], 'diff': None,
                       'origin': 'implementer' if name == 'implement' else 'file-tool'}
                undo = undo_observation(root, state['session_id'], edit_id)
                if undo is not None:
                    row['undo_status'] = undo
                if edit_result['status'] == 'succeeded':
                    last_edit = sequence
                    observed[(edit['repository'], edit['path'])] = json.loads(edit_result['output_json'])['sha256']
                    if selected and include_diffs and (diff_effect_id is None or diff_effect_id == edit_id):
                        try:
                            row['diff'] = edit_diff(root, edit_request, edit_result, max_bytes=max_diff_bytes)
                        except (ValueError, OSError, KeyError, TypeError, ConversationRejected) as exc:
                            row['diff_unavailable'] = str(exc)[:256] if isinstance(exc,ValueError) else type(exc).__name__
                elif edit_result['status'] == 'unknown':
                    unresolved.append(edit_id)
                if selected: edits.append(row)
            if name == 'implement' and output.get('applied') and selected:
                proof = {tuple(key.split(':', 1)): sha for key, sha in output.get('source_state', {}).items()}
                checks.append({'effect_id': result['effect_id'], 'name': 'Candidate checks',
                    'status': 'succeeded', 'returncode': 0, 'output': output.get('checks_after', ''),
                    'output_truncated': None, 'sequence': sequence, '_baseline': proof})
            if name in {'read', 'edit'} and result['status'] == 'succeeded' and 'sha256' in output:
                observed[(args['repository'], args['path'])] = output['sha256']
            if name == 'terminal_start' and selected:
                commands[output.get('handle') or result['effect_id']] = {
                    'argv': args.get('argv'), 'status': output.get('status') or result['status'],
                    'exit_code': output.get('exit_code')}
            elif name == 'terminal_read' and args.get('handle') in commands and output.get('status'):
                commands[args['handle']].update(status=output['status'], exit_code=output.get('exit_code'))
            if name == 'verify' and selected:
                checks.append({'effect_id': result['effect_id'], 'name': args['name'],
                    'status': result['status'], 'returncode': output.get('returncode'),
                    'output': output.get('output', ''), 'output_truncated': output.get('output_truncated'),
                    'sequence': sequence, '_baseline': baseline})
    unresolved.extend(effect for effect in requests if effect not in returned)
    current, unavailable = {}, []
    for key in observed:
        try:
            current[key] = current_hash(state['config']['repositories'], key)
        except (OSError, ValueError, KeyError):
            unavailable.append(key)
    for check in checks:
        baseline = check.pop('_baseline')
        check['observed_files'] = len(baseline)
        changed = [key for key, sha in baseline.items() if key in current and current[key] != sha]
        if changed or last_edit > check['sequence']:
            check['freshness'] = 'STALE'
        elif not baseline or unavailable or unresolved:
            check['freshness'] = 'UNKNOWN'
        else:
            check['freshness'] = 'OBSERVED_FILES_MATCH'
        check['changed_files'] = [{'repository': k[0], 'path': k[1]} for k in changed]
        check['scope'] = 'Compared with files observed before this verifier; not a complete test-input snapshot.'
    # An operator restoration is separate from model execution. Even if a
    # restored hash matches an older observation, old checks are not a rerun.
    for effect in requests:
        status = undo_observation(root, state['session_id'], effect)
        if status is not None:
            boundary = undo_boundary(root, effect) if status == 'UNDONE' else None
            for check in checks:
                if boundary is None or check['sequence'] < boundary:
                    check['freshness'] = 'STALE'
    from .native_review_checks import check_boundaries
    for boundary in check_boundaries(root):
        for check in checks:
            if boundary is None or check['sequence'] < boundary:
                check['freshness'] = 'STALE'
    return {'session_id': state['session_id'], 'session': str(root), 'task': state['instruction'],
            'edits': edits, 'checks': checks, 'commands': list(commands.values()), 'unresolved_effects': unresolved,
            'changed_since_observation': [{'repository': k[0], 'path': k[1]} for k, sha in observed.items()
                                          if k in current and current[k] != sha],
            'unavailable_files': [{'repository': k[0], 'path': k[1]} for k in unavailable],
            'diff_scope': 'Each diff is one confirmed session edit. Earlier and intervening external edits are excluded.'}


def review(root, *, project=None, invocation_id=None, include_diffs=True, diff_effect_id=None, max_diff_bytes=None):
    from .native_agent import read_json
    root = regular(root)
    if not regular(root/'journal.jsonl').is_file() or not regular(root/'writer.lock').is_file():
        raise ValueError('saved_session_journal_and_lock_required')
    with custody_lock(root):
        state = read_json(root/'native-agent.json', 1024 * 1024)
        if project is not None and Path(project).resolve() not in [Path(p).resolve() for p in state['config']['repositories'].values()]:
            raise ValueError('This conversation belongs to another project.')
        records = NativeJournal(root/'journal.jsonl').snapshot().records
        options = {}
        turns = [row for row in state.get('invocations',[]) if type(row.get('journal_start')) is int
                 and type(row.get('journal_end')) is int
                 and 0 <= row['journal_start'] <= row['journal_end'] <= len(records)]
        if invocation_id is not None:
            turn = next((row for row in turns if row['id'] == invocation_id),None)
            if turn is None: raise ValueError('This turn has no recorded review boundaries; choose All turns.')
            options = {'start':turn['journal_start'],'end':turn['journal_end']}
        result = summarize(root, state, [record.event() for record in records], **options,
            include_diffs=include_diffs, diff_effect_id=diff_effect_id, max_diff_bytes=max_diff_bytes)
        result['turns'] = [{'id':row['id'],'status':row['status']} for row in turns]
        return result


def render(result):
    lines = ['Session: '+result['session_id'], result['task'], '', result['diff_scope']]
    if not result['edits']:
        lines.append('No edits recorded.')
    for edit in result['edits']:
        lines.extend(['', f"{edit['repository']}:{edit['path']} | {edit['status']} | {edit['effect_id']}"])
        lines.append(edit['diff'] if edit['diff'] is not None else 'Diff unavailable (no confirmed, matching snapshot).')
    lines.extend(['', 'Recorded checks:'])
    if not result['checks']:
        lines.append('  None recorded.')
    for check in result['checks']:
        status = ('PASS' if check['returncode'] == 0 else 'FAIL') if check['status'] in {'succeeded', 'failed'} else 'UNKNOWN'
        lines.append(f"  {check['name']}: {status} | {check['freshness']} | {check['observed_files']} observed files")
        if check['output']:
            lines.append(check['output'])
        if check['output_truncated']:
            lines.append('  Verifier output was truncated by execution capture.')
    lines.append('Freshness covers observed files only; recorded PASS is not a new test run or an audit verdict.')
    for row in result['changed_since_observation']:
        lines.append(f"Changed since observation: {row['repository']}:{row['path']}")
    for row in result['unavailable_files']:
        lines.append(f"Current file unavailable: {row['repository']}:{row['path']}")
    if result['unresolved_effects']:
        lines.append('Unresolved effects: '+', '.join(result['unresolved_effects']))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Review saved session edits and checks without invoking a model.')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--last', action='store_true')
    selection.add_argument('--session', type=Path)
    parser.add_argument('--root', type=Path, help='Project root for --last (defaults to cwd)')
    parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    args = parser.parse_args(argv)
    try:
        if args.root and not args.last:
            raise ValueError('--root requires --last')
        from .native_sessions import latest
        result = review(latest(args.root or Path.cwd()) if args.last else args.session)
    except (OSError, ValueError, KeyError, TypeError, ConversationRejected) as exc:
        print(json.dumps({'error': str(exc)}))
        return 2
    mode = args.format if args.format != 'auto' else 'text' if sys.stdout.isatty() else 'json'
    safe_print(render(result)) if mode == 'text' else print(json.dumps(result, ensure_ascii=True))
    return 0

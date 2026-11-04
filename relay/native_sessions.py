"""Discover existing native sessions from their saved descriptors; no new registry."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from relay.native_display import safe_print


def inventory(repository=None):
    from .native_agent import read_json, session_root
    from compute_plane.native_journal import regular
    from tare_tools_agent_runtime import ConversationRejected
    home = regular(session_root())
    selected = Path(repository).resolve() if repository is not None else None
    rows, unavailable = [], []
    if not home.exists():
        return {'sessions': [], 'unavailable': [], 'scope': str(home)}
    for directory in home.iterdir():
        file = directory/'native-agent.json'
        if not file.exists():
            continue
        try:
            state = read_json(file, 1024 * 1024)
            if state.get('schema') not in {'tare.tools/native-agent/1', 'tare.tools/native-agent/2'}:
                raise ValueError('unsupported_session_schema')
            roots = state['config']['repositories']
            if not isinstance(roots, dict) or not roots or any(
                    not isinstance(p, str) or not Path(p).is_absolute() for p in roots.values()):
                raise ValueError('invalid_saved_repositories')
            if selected is not None and not any(Path(p).resolve() == selected for p in roots.values()):
                continue
            invocation = state.get('invocations', [])[-1:]
            latest = invocation[0] if invocation else {}
            stamp = latest.get('ended_at', latest.get('started_at', file.stat().st_mtime))
            if type(stamp) not in (int, float) or not math.isfinite(stamp):
                raise ValueError('invalid_session_timestamp')
            datetime.fromtimestamp(stamp, timezone.utc)
            if any(not isinstance(v, str) or not v.strip() for v in (
                    state['session_id'], state['instruction'], state['config']['role'],
                    state['deployment']['id'], state['deployment']['native_profile']['model'])):
                raise ValueError('invalid_session_metadata')
            rows.append({'session_id': state['session_id'], 'session': str(directory),
                'task': state['instruction'].splitlines()[0][:160],
                'repositories': roots, 'role': state['config']['role'],
                'deployment': state['deployment']['id'],
                'model': state['deployment']['native_profile']['model'],
                'last_status': latest.get('status', 'NOT_STARTED'), 'updated_at': stamp})
        except (ValueError, OSError, KeyError, TypeError, IndexError, OverflowError, ConversationRejected) as exc:
            unavailable.append({'session': str(directory), 'reason': type(exc).__name__})
    rows.sort(key=lambda r: (r['updated_at'], r['session']), reverse=True)
    return {'sessions': rows, 'unavailable': unavailable, 'scope': str(home)}


def latest(repository):
    found = inventory(repository)
    # An unreadable descriptor could be the newest session for this repository.
    # Keep explicit --session available instead of silently choosing an older one.
    if found['unavailable']:
        raise ValueError('session_catalog_incomplete: use tare-ops sessions and select --session explicitly')
    if not found['sessions']:
        raise ValueError('no_session_for_repository: use tare-ops sessions or choose --root')
    return Path(found['sessions'][0]['session'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='List saved native sessions without invoking a model.')
    parser.add_argument('--root', type=Path, help='Filter by the saved repository root')
    parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    args = parser.parse_args(argv)
    try:
        result = inventory(args.root)
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True))
        return 2
    output = args.format if args.format != 'auto' else 'text' if sys.stdout.isatty() else 'json'
    if output == 'json':
        print(json.dumps(result, ensure_ascii=True))
    else:
        safe_print('Saved sessions, newest first (recorded status; not a live process check):')
        if not result['sessions']:
            safe_print('No sessions found for this selection.')
        for row in result['sessions']:
            updated = datetime.fromtimestamp(row['updated_at'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            safe_print(f"\n{row['session_id']} | {row['model']} | {row['last_status']} | {updated}")
            safe_print('  ' + row['task'])
            for name, path in row['repositories'].items():
                safe_print(f'  {name}: {path}')
            safe_print('  Session: ' + row['session'])
        for issue in result['unavailable']:
            safe_print(f"Unavailable: {issue['session']} ({issue['reason']})")
        safe_print('\nFrom the repository root: tare-ops agent --last --interactive --format text')
    return 0

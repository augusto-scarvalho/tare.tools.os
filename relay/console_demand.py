"""Local conversation checkpoints; flow receipts remain with their existing owners."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from compute_plane.native_journal import custody_lock, regular, save_json
from .native_agent import read_json
from .native_installation import storage_root


def path(identity):
    if not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise ValueError('Invalid demand ID.')
    return regular(storage_root()/'demands'/identity/'conversation.json')


def read(identity, root, control_root):
    value = read_json(path(identity), 16*1024*1024)
    if (value.get('schema') != 'tare.tools/console-demand/1' or value.get('id') != identity
            or Path(value['root']).resolve() != Path(root).resolve()
            or Path(value['control_root']).resolve() != Path(control_root).resolve()
            or Path(value['snapshot']['selection']['root']).resolve() != Path(root).resolve()):
        raise ValueError('Demand belongs to another project/control root or uses an unsupported schema.')
    return value


def validate_title(title):
    if not isinstance(title, str):
        raise ValueError('Title must be a string.')
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in {'\n', '\r', '\u2028', '\u2029', '\x85'} for ch in title):
        raise ValueError('Title cannot contain line breaks, ESC, or ASCII control characters.')
    trimmed = title.strip()
    if not (1 <= len(trimmed) <= 120):
        raise ValueError('Title must be between 1 and 120 characters.')
    return trimmed


def rename(identity, revision, root, control_root, title):
    title = validate_title(title)
    root, control_root = str(Path(root).resolve()), str(Path(control_root).resolve())
    if Path(storage_root()).resolve().is_relative_to(Path(root)):
        raise ValueError('Demand custody must be outside the project.')
    file = path(identity)
    if not file.exists():
        raise FileNotFoundError(f'Demand record {identity} does not exist.')
    with custody_lock(file.parent):
        value = read(identity, root, control_root)
        if value['revision'] != revision:
            raise ValueError('Demand changed in another console. Reopen it before continuing.')
        value['title'] = title
        value['revision'] += 1
        value['updated_at'] = datetime.now(timezone.utc).isoformat()
        if len(json.dumps(value, ensure_ascii=False).encode()) > 16*1024*1024:
            raise ValueError('Conversation record exceeds 16 MiB; start a new demand. Existing records are retained.')
        save_json(file, value)
    return value


def save(identity, revision, root, control_root, kind, snapshot, detail=None):
    root, control_root = str(Path(root).resolve()), str(Path(control_root).resolve())
    if Path(storage_root()).resolve().is_relative_to(Path(root)):
        raise ValueError('Demand custody must be outside the project.')
    identity = identity or uuid4().hex
    file = path(identity); file.parent.mkdir(parents=True, exist_ok=True)
    with custody_lock(file.parent):
        value = read(identity, root, control_root) if file.exists() else {
            'schema': 'tare.tools/console-demand/1', 'id': identity, 'root': root,
            'control_root': control_root, 'revision': 0, 'events': []}
        if value['revision'] != revision:
            raise ValueError('Demand changed in another console. Reopen it before continuing.')
        if Path(snapshot['selection']['root']).resolve() != Path(root):
            raise ValueError('Checkpoint project differs from demand.')
        value['revision'] += 1
        value['updated_at'] = datetime.now(timezone.utc).isoformat()
        from .native_activity import saved_history
        value['snapshot'] = dict(snapshot)
        if 'assistant_history' in snapshot:
            value['snapshot']['assistant_history'] = saved_history(snapshot['assistant_history'])
        value['events'].append({'kind': kind, 'at': value['updated_at'], 'detail': detail,
                                'selection': snapshot['selection']})
        if len(json.dumps(value, ensure_ascii=False).encode()) > 16*1024*1024:
            raise ValueError('Conversation record exceeds 16 MiB; start a new demand. Existing records are retained.')
        save_json(file, value)
    return value


def inventory(root, control_root):
    rows, errors = [], []
    for file in (storage_root()/'demands').glob('*/conversation.json'):
        try:
            value = read_json(regular(file), 16*1024*1024)
            if (Path(value['root']).resolve(), Path(value['control_root']).resolve()) != (
                    Path(root).resolve(), Path(control_root).resolve()):
                continue
            value = read(file.parent.name, root, control_root)
            selection = value['snapshot']['selection']
            if 'title' in value:
                title = validate_title(value['title'])
            else:
                title = next(((e.get('detail') or {}).get('operator') or e['selection'].get('prompt')
                    for e in value['events'] if (e.get('detail') or {}).get('operator') or e['selection'].get('prompt')),
                    selection.get('work') or 'Demand')[:80]
            rows.append({'id': value['id'], 'updated_at': value['updated_at'], 'title': title})
        except Exception as exc:
            # Strict native JSON decoding also raises its own rejection type.
            # One unreadable record must remain visible without hiding the rest.
            errors.append(file.parent.name+': '+str(exc))
    return sorted(rows, key=lambda r: r['updated_at'], reverse=True), errors


def result_summary(result):
    if result is None:
        return None
    return {k: result[k] for k in ('invocation_id', 'run_id', 'status', 'reason', 'error', 'stage', 'work_id', 'work_ids',
        'session', 'plan_dir', 'graph_path', 'train_id', 'candidate_commit', 'duration_ms', 'model_calls', 'tokens',
        'batch', 'directory', 'sequence', 'completed', 'total', 'active_work', 'revision', 'invalidated', 'replacements', 'operational_control',
        'execution', 'unschedulable') if k in result}


def guidance(value):
    significant = [e for e in value['events'] if e['kind'] not in {'draft_saved', 'message_saved', 'stage_opened'}]
    if not significant:
        return 'Saved draft. Send the demand or Start the selected flow.'
    event = significant[-1]; kind = event['kind']; detail = event.get('detail') or {}
    if kind in {'session_reconciled', 'session_recovery_checked', 'flow_reconciled', 'flow_recovery_checked', 'mutation_reconciled', 'mutation_recovery_checked'}:
        status = (detail.get('result') or {}).get('status')
        return (status+' · ' if status else '')+detail['reason']
    if kind.endswith(('_requested', '_unobserved')):
        return 'Operation was requested; completion is not recorded here. Review its owner receipts before resuming.'
    if kind == 'next_step_opened':
        return detail['reason']
    if kind == 'demand_result_confirmed':
        return ('Result reviewed · '+detail['disposition']+' · '+detail['summary']+
                (' · Remaining: '+detail['remaining'] if detail['remaining'] else ''))
    if kind == 'interpretation_finished':
        return 'Reply to the saved question.' if detail.get('status') == 'NEEDS_INPUT' else 'Review the saved suggestion before starting it.'
    if kind == 'graph_applied':
        return 'Graph proposals exist. Open a linked Work and prepare its source and verification.'
    if kind == 'work_prepared':
        return 'Work preparation is recorded. Start checks its current readiness before execution.'
    from .work_console import control_copy
    message = control_copy(detail.get('operational_control') or detail)
    if message:
        return message+' Inspect saved Work controls; Resume is explicit.'
    if detail.get('status') == 'HUMAN_DECLINED':
        return 'Implementation declined. Open the decision to reconsider it.'
    if detail.get('status') == 'WAITING_FOR_HUMAN':
        return 'Open the saved human decision, answer it, then Resume.'
    if detail.get('status') == 'SETTLED':
        return 'This Work settled. Review its receipts or open another linked stage; the whole demand is not inferred complete.'
    if detail.get('status') == 'REVIEWED':
        return 'Review the saved PLAN again to apply it, or Replan with a correction.'
    return 'Review the saved result. Resume and retries use the selected flow’s existing checks.'

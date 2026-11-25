"""Host-mediated, typed agent signal bus.

Star topology only: Child -> Host -> Coordinator/User. Implementer and Auditor
never exchange messages laterally. Every signal is host-stamped with its real
emitter role (`from_role`) and `authority='ADVISORY_ONLY'`. New signal kinds are
registered in SIGNAL_CATALOG with explicit emitter allowlists, payload
validators, and host routing policies.
"""
from copy import deepcopy
import hashlib
from pathlib import PurePosixPath

from compute_plane.native_models import encode


MAX_SIGNALS_PER_RUN = 3
MAX_SIGNAL_BYTES = 8192
ADVISORY_AUTHORITY = 'ADVISORY_ONLY'

POLICY_CONSENT_MODAL = 'CONSENT_MODAL'
POLICY_STOP_FOR_CHAT = 'STOP_FOR_CHAT'
POLICY_SURFACE_TO_OPERATOR = 'SURFACE_TO_OPERATOR'


def _clean_text(value, field, *, max_chars):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'agent_signal_invalid_{field}')
    text = value.strip()
    if len(text) > max_chars:
        raise ValueError(f'agent_signal_{field}_too_long')
    return text


def _clean_rel_paths(paths, field, *, max_items=16):
    if not isinstance(paths, list) or len(paths) > max_items:
        raise ValueError(f'agent_signal_invalid_{field}')
    cleaned = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 256 or '\0' in raw:
            raise ValueError(f'agent_signal_invalid_{field}')
        if '\\' in raw or ':' in raw or raw.startswith('/'):
            raise ValueError(f'agent_signal_invalid_{field}')
        path = raw.strip()
        while path.startswith('./'):
            path = path[2:]
        posix = PurePosixPath(path)
        if not path or posix.is_absolute() or '..' in posix.parts or '.git' in posix.parts:
            raise ValueError(f'agent_signal_invalid_{field}')
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def _normalize_propose_checks_entries(checks, *, replace):
    """Accept either the closed wire list `[{'name': ..., ...}]` or internal dict `{name: entry_or_None}`.

    Returns `(canonical_dict, canonical_wire_list)` with strict validation on every field.
    """
    from .turn_engine import PROJECT_CHECK_NAME, PROJECT_CHECK_RESERVED, PROJECT_CHECKS_MAX_COUNT, validate_project_checks_data
    raw_map = {}
    if isinstance(checks, list):
        if len(checks) > PROJECT_CHECKS_MAX_COUNT:
            raise ValueError('agent_signal_invalid_checks')
        if not checks and not replace:
            raise ValueError('agent_signal_empty_checks')
        for item in checks:
            if not isinstance(item, dict):
                raise ValueError('agent_signal_invalid_checks')
            if set(item) - {'name', 'remove', 'mode', 'argv', 'timeout_seconds', 'description', 'paths'}:
                raise ValueError('agent_signal_unknown_check_field')
            name = item.get('name')
            if not isinstance(name, str) or not name or name in raw_map:
                raise ValueError('agent_signal_invalid_check_name')
            remove = item.get('remove')
            if remove is not None and type(remove) is not bool:
                raise ValueError('agent_signal_invalid_remove')
            body = {k: v for k, v in item.items() if k not in {'name', 'remove'} and v is not None}
            if remove is True:
                if body:
                    raise ValueError('agent_signal_remove_with_fields')
                raw_map[name] = None
            else:
                raw_map[name] = body
    elif isinstance(checks, dict):
        if len(checks) > PROJECT_CHECKS_MAX_COUNT:
            raise ValueError('agent_signal_invalid_checks')
        if not checks and not replace:
            raise ValueError('agent_signal_empty_checks')
        raw_map = checks
    else:
        raise ValueError('agent_signal_invalid_checks')

    non_null = {}
    removals = []
    for name, entry in raw_map.items():
        if not isinstance(name, str) or not name:
            raise ValueError('agent_signal_invalid_check_name')
        if entry is None:
            if replace:
                raise ValueError('agent_signal_null_check_with_replace')
            if not PROJECT_CHECK_NAME.fullmatch(name) or name in PROJECT_CHECK_RESERVED:
                raise ValueError('agent_signal_invalid_check_name')
            removals.append(name)
        else:
            non_null[name] = entry
    normalized_non_null, _ = validate_project_checks_data(
        None, {'checks': non_null}, strict=True, check_executables=False)
    normalized_map = dict(normalized_non_null['checks'])
    for name in removals:
        normalized_map[name] = None
    wire_list = []
    for name, entry in normalized_map.items():
        if entry is None:
            wire_list.append({'name': name, 'remove': True})
        else:
            wire_list.append({'name': name, **entry})
    return normalized_map, wire_list


def _validate_propose_checks_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('agent_signal_invalid_payload')
    if set(payload) - {'checks', 'replace', 'repository'}:
        raise ValueError('agent_signal_unknown_payload_field')
    replace = payload.get('replace')
    if replace is None:
        replace = False
    if type(replace) is not bool:
        raise ValueError('agent_signal_invalid_replace')
    repository = payload.get('repository')
    if repository is not None and (not isinstance(repository, str) or not repository.strip() or len(repository) > 128):
        raise ValueError('agent_signal_invalid_repository')
    _, wire_list = _normalize_propose_checks_entries(payload.get('checks'), replace=replace)
    out = {'checks': wire_list}
    if replace:
        out['replace'] = True
    if repository is not None:
        out['repository'] = repository.strip()
    return out


def _validate_goal_drift_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('agent_signal_invalid_payload')
    if set(payload) - {'expected', 'discovered', 'proposed_objective', 'options'}:
        raise ValueError('agent_signal_unknown_payload_field')
    out = {
        'expected': _clean_text(payload.get('expected'), 'expected', max_chars=1000),
        'discovered': _clean_text(payload.get('discovered'), 'discovered', max_chars=1000),
    }
    if 'proposed_objective' in payload and payload['proposed_objective'] is not None:
        out['proposed_objective'] = _clean_text(payload['proposed_objective'], 'proposed_objective', max_chars=1000)
    if 'options' in payload and payload['options'] is not None:
        options = payload['options']
        if not isinstance(options, list) or not 1 <= len(options) <= 6:
            raise ValueError('agent_signal_invalid_options')
        out['options'] = [_clean_text(item, 'option', max_chars=300) for item in options]
    return out


def _validate_out_of_scope_blocker_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('agent_signal_invalid_payload')
    if set(payload) - {'paths', 'symptom', 'suggested_follow_up'}:
        raise ValueError('agent_signal_unknown_payload_field')
    out = {'symptom': _clean_text(payload.get('symptom'), 'symptom', max_chars=1000)}
    if 'paths' in payload and payload['paths'] is not None:
        out['paths'] = _clean_rel_paths(payload['paths'], 'paths', max_items=16)
    if 'suggested_follow_up' in payload and payload['suggested_follow_up'] is not None:
        out['suggested_follow_up'] = _clean_text(payload['suggested_follow_up'], 'suggested_follow_up', max_chars=500)
    return out


SIGNAL_CATALOG = {
    'propose_checks': {
        'emitters': frozenset({'coordinator', 'planner', 'implementer', 'explorer'}),
        'policy': POLICY_CONSENT_MODAL,
        'validator': _validate_propose_checks_payload,
        'description': 'Propose creating or updating <project>/.tare/checks.json (always user-confirmed in TUI; fail-closed headless).',
    },
    'goal_drift': {
        'emitters': frozenset({'planner', 'implementer', 'explorer'}),
        'policy': POLICY_STOP_FOR_CHAT,
        'validator': _validate_goal_drift_payload,
        'description': 'Report a conflict between the recorded goal/criteria and observed repository facts; stops for user chat reply.',
    },
    'out_of_scope_blocker': {
        'emitters': frozenset({'planner', 'implementer', 'explorer', 'auditor', 'plan-auditor'}),
        'policy': POLICY_SURFACE_TO_OPERATOR,
        'validator': _validate_out_of_scope_blocker_payload,
        'description': 'Surface a pre-existing failure, flake, or blocker outside the task scope without widening write scope.',
    },
}


def validate_signal(role, signal):
    """Validate one raw signal dict against role allowlist, byte budget, and per-kind payload schema."""
    if not isinstance(signal, dict):
        raise ValueError('agent_signal_invalid_shape')
    if set(signal) != {'kind', 'summary', 'reason', 'payload'}:
        raise ValueError('agent_signal_invalid_fields')
    kind = signal.get('kind')
    spec = SIGNAL_CATALOG.get(kind)
    if spec is None:
        raise ValueError('agent_signal_unknown_kind')
    if role not in spec['emitters']:
        raise ValueError(f'agent_signal_emitter_not_allowed:{role}:{kind}')
    if len(encode(signal).encode('utf-8')) > MAX_SIGNAL_BYTES:
        raise ValueError('agent_signal_over_budget')
    summary = _clean_text(signal.get('summary'), 'summary', max_chars=300)
    reason = _clean_text(signal.get('reason'), 'reason', max_chars=1000)
    payload = spec['validator'](signal.get('payload'))
    return {'kind': kind, 'summary': summary, 'reason': reason, 'payload': payload}


def validate_artifact_signals(role, signals):
    """Validate a role's emitted signals list (count, allowlist, size, per-kind schemas)."""
    if not isinstance(signals, list) or len(signals) > MAX_SIGNALS_PER_RUN:
        raise ValueError('agent_signal_limit_exceeded')
    validated = [validate_signal(role, item) for item in signals]
    if sum(1 for item in validated if item['kind'] == 'propose_checks') > 1:
        raise ValueError('agent_signal_multiple_propose_checks')
    return validated


def stamp_signals(role, signals, *, source_id=''):
    """Validate and host-stamp signals with authoritative `from_role`, `authority`, `policy`, and `signal_id`."""
    validated = validate_artifact_signals(role, signals)
    stamped = []
    for index, item in enumerate(validated):
        digest_input = {'from_role': role, 'source_id': str(source_id), 'index': index, **item}
        signal_id = hashlib.sha256(encode(digest_input).encode('utf-8')).hexdigest()[:16]
        stamped.append({
            'signal_id': signal_id,
            'from_role': role,
            'authority': ADVISORY_AUTHORITY,
            'policy': SIGNAL_CATALOG[item['kind']]['policy'],
            'kind': item['kind'],
            'summary': item['summary'],
            'reason': item['reason'],
            'payload': deepcopy(item['payload']),
        })
    return stamped


def _format_check_entry(entry):
    if not isinstance(entry, dict):
        return f'<invalid entry: {entry!r}>'
    mode = entry.get('mode', 'headless')
    if mode in ('manual', 'gui'):
        parts = [f'mode={mode}']
        if entry.get('paths') is not None:
            parts.append(f"paths={entry['paths']!r}")
        if entry.get('description') is not None:
            parts.append(str(entry['description']))
        return '; '.join(parts)
    parts = [f"argv={entry.get('argv')!r}"]
    if entry.get('timeout_seconds') is not None:
        parts.append(f"timeout={entry['timeout_seconds']}s")
    if entry.get('description') is not None:
        parts.append(str(entry['description']))
    return '; '.join(parts)


def prepare_checks_proposal(root, payload):
    """Validate a `propose_checks` payload against `root`, merge with existing `.tare/checks.json`,
    and compute an exact before->after diff. Fails closed on any invalid check or unsafe filesystem path.
    """
    from .turn_engine import PROJECT_CHECKS_MAX_COUNT, read_project_checks_doc, validate_project_checks_data
    clean_payload = _validate_propose_checks_payload(payload)
    checks_map, _ = _normalize_propose_checks_entries(clean_payload['checks'], replace=bool(clean_payload.get('replace')))
    existing_doc, existing_sha256 = read_project_checks_doc(root)
    before = deepcopy((existing_doc or {}).get('checks') or {})
    if clean_payload.get('replace'):
        merged = deepcopy(checks_map)
    else:
        merged = deepcopy(before)
        for name, entry in checks_map.items():
            if entry is None:
                merged.pop(name, None)
            else:
                merged[name] = deepcopy(entry)
    normalized_doc, parsed = validate_project_checks_data(
        root, {'checks': merged}, strict=True, check_executables=True)
    after = normalized_doc['checks']

    # Normalize existing entries best-effort for clean diff comparison.
    # If the existing file exceeded PROJECT_CHECKS_MAX_COUNT, project_checks() disabled every entry,
    # so none of them were active and every retained entry in `after` is being newly activated.
    oversized_before = len(before) > PROJECT_CHECKS_MAX_COUNT
    before_norm, _ = (validate_project_checks_data(root, {'checks': before}, strict=False, check_executables=False)
                      if before and not oversized_before else ({'checks': {}}, {}))
    before_active = before_norm['checks']
    before_cmp = dict(before_active)
    for name, entry in before.items():
        before_cmp.setdefault(name, entry)

    added = sorted(name for name in after if name not in before_cmp)
    removed = sorted(name for name in before_cmp if name not in after)
    modified = sorted(name for name in after if name in before_cmp and (
        name not in before_active or before_active[name] != after[name]))
    unchanged = sorted(name for name in after if name in before_active and before_active[name] == after[name])
    has_changes = bool(added or removed or modified)

    diff_lines = []
    if existing_doc is None:
        diff_lines.append('Action: create .tare/checks.json')
    else:
        diff_lines.append('Action: update existing .tare/checks.json' + (' (replace all)' if clean_payload.get('replace') else ' (merge)'))
    for name in removed:
        diff_lines.append(f'[-] REMOVED  {name}: {_format_check_entry(before_cmp[name])}')
    for name in modified:
        diff_lines.append(f'[~] MODIFIED {name}:')
        diff_lines.append(f'      before: {_format_check_entry(before_cmp[name])}')
        diff_lines.append(f'      after:  {_format_check_entry(after[name])}')
    for name in added:
        diff_lines.append(f'[+] ADDED    {name}: {_format_check_entry(after[name])}')
    if unchanged:
        diff_lines.append(f'[=] Unchanged ({len(unchanged)}): ' + ', '.join(unchanged))
    if not has_changes:
        diff_lines.append('(No effective changes compared to existing .tare/checks.json.)')

    return {
        'existed': existing_doc is not None,
        'existing_sha256': existing_sha256,
        'before': before_cmp,
        'after': after,
        'document': normalized_doc,
        'parsed': parsed,
        'added': added,
        'modified': modified,
        'removed': removed,
        'unchanged': unchanged,
        'has_changes': has_changes,
        'diff_text': '\n'.join(diff_lines),
    }

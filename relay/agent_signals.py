"""Host-mediated, typed agent signal bus.

Star topology only: Child -> Host -> Coordinator/User. Implementer and Auditor
never exchange messages laterally. Every signal is host-stamped with its real
emitter role (`from_role`) and `authority='ADVISORY_ONLY'`. New signal kinds are
registered in SIGNAL_CATALOG with explicit emitter allowlists, payload
validators, and host routing policies.
"""
from copy import deepcopy
import hashlib
from pathlib import Path, PurePosixPath

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode, object_json


MAX_SIGNALS_PER_RUN = 3
MAX_SIGNAL_BYTES = 8192
MAX_SIGNALS_FILE_BYTES = 32768
SIGNALS_FILE = 'agent-signals.json'
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


def _signal_validation_error(code: str, message: str, loc: tuple = ('signals',)):
    from pydantic import ValidationError
    from pydantic_core import PydanticCustomError
    return ValidationError.from_exception_data('AgentSignals', [{
        'loc': loc,
        'type': PydanticCustomError(code, '{msg}', {'msg': message}),
    }])


def validate_signal(role, signal, *, _index: int | None = None):
    """Validate one raw signal dict against role allowlist, byte budget, and per-kind payload schema."""
    base_loc = ('signals', _index) if _index is not None else ('signals',)
    if not isinstance(signal, dict):
        raise _signal_validation_error('agent_signal_invalid_shape', 'agent_signal_invalid_shape', base_loc)
    if set(signal) != {'kind', 'summary', 'reason', 'payload'}:
        raise _signal_validation_error('agent_signal_invalid_fields', 'agent_signal_invalid_fields', base_loc)
    kind = signal.get('kind')
    spec = SIGNAL_CATALOG.get(kind)
    if spec is None:
        raise _signal_validation_error('agent_signal_unknown_kind', 'agent_signal_unknown_kind', (*base_loc, 'kind'))
    if role not in spec['emitters']:
        raise _signal_validation_error(
            'agent_signal_emitter_not_allowed',
            f'agent_signal_emitter_not_allowed:{role}:{kind}',
            (*base_loc, 'kind'),
        )
    if len(encode(signal).encode('utf-8')) > MAX_SIGNAL_BYTES:
        raise _signal_validation_error('agent_signal_over_budget', 'agent_signal_over_budget', base_loc)
    try:
        summary = _clean_text(signal.get('summary'), 'summary', max_chars=300)
        reason = _clean_text(signal.get('reason'), 'reason', max_chars=1000)
        payload = spec['validator'](signal.get('payload'))
    except ValueError as exc:
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            raise
        raise _signal_validation_error('agent_signal_invalid_payload', str(exc), (*base_loc, 'payload')) from None
    return {'kind': kind, 'summary': summary, 'reason': reason, 'payload': payload}


def validate_artifact_signals(role, signals):
    """Validate a role's emitted signals list (count, allowlist, size, per-kind schemas)."""
    if not isinstance(signals, list) or len(signals) > MAX_SIGNALS_PER_RUN:
        raise _signal_validation_error('agent_signal_limit_exceeded', 'agent_signal_limit_exceeded', ('signals',))
    validated = [validate_signal(role, item, _index=idx) for idx, item in enumerate(signals)]
    if sum(1 for item in validated if item['kind'] == 'propose_checks') > 1:
        raise _signal_validation_error(
            'agent_signal_multiple_propose_checks',
            'agent_signal_multiple_propose_checks',
            ('signals',),
        )
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


REPORT_SIGNAL_SPEC = {
    'report_signal': {
        'description': 'Report a structured advisory signal to the host/user (read-only, at most 3 per session; '
            'grants no write or shell authority and never reaches the auditor). '
            'Use kind="propose_checks" (with checks[]) to propose .tare/checks.json updates for user confirmation; '
            'kind="goal_drift" (with expected, discovered, optional proposed_objective/options) if repository facts '
            'conflict with the task goal (stops safely without applying so the user can decide in chat); '
            'or kind="out_of_scope_blocker" (with symptom, optional paths/suggested_follow_up) for a pre-existing '
            'failure or flake outside your write scope.',
        'parameters': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'enum': ['propose_checks', 'goal_drift', 'out_of_scope_blocker']},
                'summary': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                'reason': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                'checks': {
                    'type': 'array',
                    'maxItems': 16,
                    'description': 'For kind="propose_checks" only: check definitions to add, update, or remove.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string', 'minLength': 1, 'maxLength': 64},
                            'remove': {'type': 'boolean'},
                            'mode': {'type': 'string', 'enum': ['headless', 'manual', 'gui']},
                            'argv': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 32},
                            'timeout_seconds': {'type': 'integer', 'minimum': 5, 'maximum': 3600},
                            'description': {'type': 'string', 'maxLength': 240},
                            'paths': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 16},
                        },
                        'required': ['name'],
                        'additionalProperties': False,
                    },
                },
                'replace': {'type': 'boolean', 'description': 'For kind="propose_checks" only.'},
                'repository': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                               'description': 'For kind="propose_checks" only.'},
                'expected': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
                             'description': 'For kind="goal_drift" only: what the recorded goal assumed.'},
                'discovered': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
                               'description': 'For kind="goal_drift" only: what repository evidence actually shows.'},
                'proposed_objective': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
                                       'description': 'For kind="goal_drift" only.'},
                'options': {'type': 'array', 'items': {'type': 'string', 'maxLength': 300}, 'minItems': 1, 'maxItems': 6,
                            'description': 'For kind="goal_drift" only: concrete choices for the user.'},
                'symptom': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
                            'description': 'For kind="out_of_scope_blocker" only: concrete failure or flake observed.'},
                'paths': {'type': 'array', 'items': {'type': 'string', 'maxLength': 256}, 'maxItems': 16,
                          'description': 'For kind="out_of_scope_blocker" only: affected repo-relative paths.'},
                'suggested_follow_up': {'type': 'string', 'minLength': 1, 'maxLength': 500,
                                        'description': 'For kind="out_of_scope_blocker" only.'},
            },
            'required': ['kind', 'summary', 'reason'],
            'additionalProperties': False,
        },
    },
}

_KIND_PAYLOAD_KEYS = {
    'propose_checks': frozenset({'checks', 'replace', 'repository'}),
    'goal_drift': frozenset({'expected', 'discovered', 'proposed_objective', 'options'}),
    'out_of_scope_blocker': frozenset({'symptom', 'paths', 'suggested_follow_up'}),
}
_ALL_PAYLOAD_KEYS = frozenset().union(*_KIND_PAYLOAD_KEYS.values())


def _load_session_records(custody):
    from .native_agent import read_json
    path = regular(Path(custody) / SIGNALS_FILE)
    if not path.is_file():
        return []
    data = read_json(path, MAX_SIGNALS_FILE_BYTES)
    if not isinstance(data, dict) or data.get('schema') != 'tare.tools/agent-signals/1' or not isinstance(data.get('signals'), list):
        raise ValueError('agent_signals_store_invalid')
    return data['signals']


def read_session_signals(custody, *, role):
    """Read and host-re-stamp recorded signals from a child session's private custody."""
    try:
        rows = _load_session_records(custody)
    except (OSError, ValueError):
        return []
    if not rows:
        return []
    raw = [{'kind': r.get('kind'), 'summary': r.get('summary'), 'reason': r.get('reason'), 'payload': r.get('payload')}
           for r in rows[:MAX_SIGNALS_PER_RUN] if isinstance(r, dict)]
    try:
        return stamp_signals(role, raw, source_id=str(Path(custody).name))
    except ValueError:
        return []


class SignalTools:
    """Lightweight read-only `report_signal` tool wrapper for delegated implementers.

    Follows the FolderTools wrapper pattern: zero write or shell power, strict per-kind validation
    against SIGNAL_CATALOG, max 3 signals per session persisted under session custody.
    """

    def __init__(self, tools, custody, *, role='implementer'):
        if role not in {'implementer'}:
            raise ValueError('SignalTools is admitted only for delegated implementers.')
        self.tools, self.custody, self.role = tools, Path(custody), role
        self.names = (*tools.names, 'report_signal')
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **REPORT_SIGNAL_SPEC})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def execute(self, request, control):
        from tare_tools_agent_runtime import EffectResult
        if request.tool_call.name != 'report_signal':
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            args = object_json(request.tool_call.arguments_json)
            if len(encode(args).encode('utf-8')) > 16384:
                raise ValueError('Use bounded report_signal arguments within 16 KiB.')
            if not isinstance(args, dict) or set(args) - ({'kind', 'summary', 'reason'} | _ALL_PAYLOAD_KEYS):
                raise ValueError('Use only the declared report_signal fields.')
            kind = args.get('kind')
            if kind not in _KIND_PAYLOAD_KEYS:
                raise ValueError('agent_signal_unknown_kind')
            allowed_keys = _KIND_PAYLOAD_KEYS[kind]
            extra_payload = {k for k in _ALL_PAYLOAD_KEYS - allowed_keys if args.get(k) is not None}
            if extra_payload:
                raise ValueError(f'Fields not allowed for kind={kind!r}: {", ".join(sorted(extra_payload))}.')
            payload = {k: args[k] for k in allowed_keys if k in args and args[k] is not None}
            validated = validate_signal(self.role, {
                'kind': kind,
                'summary': args.get('summary'),
                'reason': args.get('reason'),
                'payload': payload,
            })
            records = _load_session_records(self.custody)
            existing = next((r for r in records if isinstance(r, dict) and r.get('effect_id') == request.effect_id), None)
            if existing is not None:
                return EffectResult(request.effect_id, 'succeeded', encode({
                    'status': 'RECORDED', 'signal_id': existing['signal_id'], 'kind': existing['kind'],
                    'authority': ADVISORY_AUTHORITY, 'reused': True,
                    'remaining_signals': max(0, MAX_SIGNALS_PER_RUN - len(records)),
                }), False)
            if len(records) >= MAX_SIGNALS_PER_RUN:
                raise ValueError(f'At most {MAX_SIGNALS_PER_RUN} signals may be reported per session.')
            if kind == 'propose_checks' and any(r.get('kind') == 'propose_checks' for r in records if isinstance(r, dict)):
                raise ValueError('At most one propose_checks signal may be reported per session.')
            stamped = stamp_signals(self.role, [validated], source_id=f'{self.custody.name}:{request.effect_id}')[0]
            stored = {**stamped, 'effect_id': request.effect_id}
            records.append(stored)
            save_json(regular(self.custody / SIGNALS_FILE),
                      {'schema': 'tare.tools/agent-signals/1', 'signals': records},
                      max_bytes=MAX_SIGNALS_FILE_BYTES)
            next_hint = {
                'goal_drift': 'Signal recorded. Stop editing and deliver a concise summary explaining the goal drift; '
                              'the host will preserve your candidate unapplied and ask the user in chat.',
                'propose_checks': 'Signal recorded. The host will validate and ask the user in the TUI after you return; '
                                  'it does not modify .tare/checks.json during your run.',
                'out_of_scope_blocker': 'Signal recorded. Stay strictly within your write scope; the host will surface '
                                        'this blocker to the user.',
            }[kind]
            return EffectResult(request.effect_id, 'succeeded', encode({
                'status': 'RECORDED',
                'signal_id': stamped['signal_id'],
                'kind': kind,
                'authority': ADVISORY_AUTHORITY,
                'remaining_signals': MAX_SIGNALS_PER_RUN - len(records),
                'next': next_hint,
            }), True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)[:500]}))

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending
        from tare_tools_agent_runtime import EffectResult
        request = pending(journal, self.session_id)
        if request is not None and request.tool_call.name == 'report_signal':
            try:
                records = _load_session_records(self.custody)
            except (OSError, ValueError):
                records = []
            existing = next((r for r in records if isinstance(r, dict) and r.get('effect_id') == request.effect_id), None)
            if existing is not None:
                return request, EffectResult(request.effect_id, 'succeeded', encode({
                    'status': 'RECORDED', 'signal_id': existing['signal_id'], 'kind': existing['kind'],
                    'authority': ADVISORY_AUTHORITY, 'recovered': True,
                    'remaining_signals': max(0, MAX_SIGNALS_PER_RUN - len(records)),
                }), True)
            return request, EffectResult(request.effect_id, 'refused', encode({
                'reason': 'Signal reporting was interrupted before recording. Call report_signal again if still needed.'}))
        return self.tools.recover_effect(journal)

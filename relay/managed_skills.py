"""Managed, configurable skill content with explicit origin, typed parameters, overlays, upstream diffs,
per-runtime binding visibility, and frozen per-session activation (SKL-01 / PA-04).

Configuration lives in <storage>/serve/skills/managed.json, strictly separate from permanent /guidance
(<storage>/serve/guidance.json) and from inert bundle snapshots (<storage>/serve/skills/snapshots/).
Editing or updating a managed skill never alters already-started sessions or grants effect/tool permissions.
"""
import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from . import guidance as _guidance
from . import skill_bundles as _bundles

SCHEMA = 'tare.tools/managed-skills/1'
MAX_SKILL_TEXT_BYTES = _bundles.MAX_SKILL_DOC_BYTES  # 64 KiB
MAX_SESSION_SKILLS = 8
MAX_SESSION_SKILLS_BYTES = 128 * 1024
MAX_PARAMETERS = 32
PARAM_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9 _.-]{0,39}$')
PARAM_TYPES = ('string', 'int', 'float', 'bool', 'choice')


def store_path(storage_root=None) -> Path:
    if storage_root is None:
        from .native_installation import storage_root as _storage_root
        base = _storage_root()
    else:
        base = Path(storage_root)
    return base / 'serve' / 'skills' / 'managed.json'


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _normalize_project_key(project) -> str | None:
    if project is None:
        return None
    raw = Path(project)
    if '..' in raw.parts:
        raise ValueError('Parent traversal is not allowed in project path')
    return os.path.normcase(str(raw.resolve()))


def _validate_skill_id(skill_id: str) -> str:
    return _bundles._validate_bundle_id(skill_id)


def _validate_param_name(name: str) -> str:
    if not isinstance(name, str) or not PARAM_NAME_RE.match(name.strip()):
        raise ValueError(f'Invalid skill parameter name: {name!r}')
    return name.strip()


def _coerce_param_value(raw, param_type: str, choices=None):
    if param_type not in PARAM_TYPES:
        raise ValueError(f'Unsupported parameter type: {param_type!r}')
    if param_type == 'string':
        if not isinstance(raw, str):
            raw = str(raw)
        if len(raw.encode('utf-8')) > 4096:
            raise ValueError('Parameter string value exceeds 4096 bytes')
        return raw
    if param_type == 'int':
        if isinstance(raw, bool):
            raise ValueError('Boolean is not a valid int parameter value')
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Invalid int parameter value: {raw!r}') from exc
    if param_type == 'float':
        if isinstance(raw, bool):
            raise ValueError('Boolean is not a valid float parameter value')
        try:
            val = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Invalid float parameter value: {raw!r}') from exc
        if val != val or val in (float('inf'), float('-inf')):
            raise ValueError(f'Non-finite float parameter value: {raw!r}')
        return val
    if param_type == 'bool':
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in {'true', '1', 'yes', 'on'}:
                return True
            if low in {'false', '0', 'no', 'off'}:
                return False
        raise ValueError(f'Invalid bool parameter value: {raw!r}')
    if param_type == 'choice':
        if not isinstance(choices, (list, tuple)) or not choices or any(not isinstance(c, str) or not c for c in choices):
            raise ValueError('choice parameter requires a non-empty list of string choices')
        text = raw if isinstance(raw, str) else str(raw)
        if text not in choices:
            raise ValueError(f'Value {text!r} is not in allowed choices: {", ".join(choices)}')
        return text
    raise ValueError(f'Unsupported parameter type: {param_type!r}')


def load_store(*, storage_root=None) -> dict:
    path = store_path(storage_root)
    if not path.exists():
        return {'schema': SCHEMA, 'skills': {}}
    if _bundles._is_link_or_reparse(path):
        raise ValueError(f'Linked managed skills store is refused: {path}')
    data, _ = _bundles._read_pinned_bytes(path, 4 * 1024 * 1024)
    if data is None:
        raise ValueError('Managed skills store exceeds size limit')
    try:
        parsed = json.loads(data.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f'Corrupted managed skills store: {exc}') from exc
    if not isinstance(parsed, dict) or parsed.get('schema') != SCHEMA or not isinstance(parsed.get('skills'), dict):
        raise ValueError(f'{path} is not a valid {SCHEMA} file')
    return parsed


def _save_store(store: dict, *, storage_root=None) -> None:
    path = store_path(storage_root)
    _bundles._ensure_confined_dir(path.parent)
    if path.exists() and _bundles._is_link_or_reparse(path):
        raise ValueError(f'Linked managed skills store is refused: {path}')
    tmp = path.parent / f'.managed-{uuid.uuid4().hex[:12]}.tmp'
    try:
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def adopt_snapshot(
    bundle_id: str,
    snapshot_ref: str = 'latest',
    *,
    skill_id: str | None = None,
    storage_root=None,
) -> dict:
    """Create a managed skill from a verified inert bundle snapshot without activating it in any session."""
    manifest = _bundles.load_snapshot(bundle_id, snapshot_ref, storage_root=storage_root, verify=True)
    raw_bytes, _ = _bundles.read_snapshot_file(manifest['bundle_id'], manifest['snapshot_id'], 'SKILL.md',
                                               storage_root=storage_root)
    base_text = raw_bytes.decode('utf-8')
    if len(raw_bytes) > MAX_SKILL_TEXT_BYTES:
        raise ValueError(f'SKILL.md exceeds {MAX_SKILL_TEXT_BYTES} bytes')
    sid = _bundles.normalize_bundle_id(skill_id) if skill_id is not None else manifest['bundle_id']
    store = load_store(storage_root=storage_root)
    if sid in store['skills']:
        raise ValueError(f'Managed skill {sid!r} already exists; use update to advance its upstream snapshot')
    now = _now_iso()
    entry = {
        'skill_id': sid,
        'bundle_id': manifest['bundle_id'],
        'snapshot_id': manifest['snapshot_id'],
        'bundle_sha256': manifest['bundle_sha256'],
        'origin_path': manifest['source']['origin_path'],
        'skill_name': manifest['source']['skill_name'],
        'description': manifest['source']['description'],
        'base_sha256': _digest(base_text),
        'base_text': base_text,
        'overlay_text': None,
        'parameters': {},
        'project_overrides': {},
        'created_at': now,
        'updated_at': now,
    }
    store['skills'][sid] = entry
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, storage_root=storage_root)


def set_overlay(
    skill_id: str,
    text: str | None,
    *,
    project=None,
    storage_root=None,
) -> dict:
    """Set or clear an editable overlay (global or per-project), preserving base_text and running sessions."""
    sid = _validate_skill_id(skill_id)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    if text is not None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Overlay text must be a non-empty string (pass None to clear)')
        if len(text.encode('utf-8')) > MAX_SKILL_TEXT_BYTES:
            raise ValueError(f'Overlay text exceeds {MAX_SKILL_TEXT_BYTES} bytes')
    proj_key = _normalize_project_key(project)
    if proj_key is None:
        entry['overlay_text'] = text
    else:
        overrides = entry.setdefault('project_overrides', {})
        proj_entry = overrides.setdefault(proj_key, {'overlay_text': None, 'parameters': {}})
        proj_entry['overlay_text'] = text
        if proj_entry['overlay_text'] is None and not proj_entry.get('parameters'):
            overrides.pop(proj_key, None)
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def set_parameter(
    skill_id: str,
    name: str,
    value,
    *,
    param_type: str = 'string',
    default=None,
    choices=None,
    description: str = '',
    project=None,
    storage_root=None,
) -> dict:
    """Define or set a typed skill parameter (global or per-project override)."""
    sid = _validate_skill_id(skill_id)
    pname = _validate_param_name(name)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    params = entry.setdefault('parameters', {})
    proj_key = _normalize_project_key(project)

    if proj_key is not None:
        if pname not in params:
            raise ValueError(f'Define global parameter {pname!r} on skill {sid!r} before setting a project override')
        spec = params[pname]
        coerced = _coerce_param_value(value, spec['type'], spec.get('choices'))
        overrides = entry.setdefault('project_overrides', {})
        proj_entry = overrides.setdefault(proj_key, {'overlay_text': None, 'parameters': {}})
        proj_entry.setdefault('parameters', {})[pname] = coerced
    else:
        if pname not in params and len(params) >= MAX_PARAMETERS:
            raise ValueError(f'Skill {sid!r} already has the maximum of {MAX_PARAMETERS} parameters')
        existing = params.get(pname)
        effective_type = param_type if existing is None or param_type != 'string' else existing['type']
        effective_choices = list(choices) if choices is not None else (existing.get('choices') if existing else None)
        coerced = _coerce_param_value(value, effective_type, effective_choices)
        coerced_default = (
            _coerce_param_value(default, effective_type, effective_choices)
            if default is not None else (existing['default'] if existing and existing['type'] == effective_type else coerced)
        )
        # Validate and coerce any existing per-project overrides before persisting type/choice changes.
        revalidated_overrides = {}
        for pkey, p_entry in (entry.get('project_overrides') or {}).items():
            p_params = p_entry.get('parameters') or {}
            if pname in p_params:
                try:
                    revalidated_overrides[pkey] = _coerce_param_value(p_params[pname], effective_type, effective_choices)
                except ValueError as exc:
                    raise ValueError(
                        f'Existing project override for parameter {pname!r} ({pkey}) is incompatible with '
                        f'type {effective_type!r}: {exc}'
                    ) from exc
        spec = {
            'type': effective_type,
            'default': coerced_default,
            'value': coerced,
            'description': str(description or (existing.get('description', '') if existing else ''))[:200],
        }
        if effective_choices is not None:
            spec['choices'] = list(effective_choices)
        params[pname] = spec
        for pkey, new_val in revalidated_overrides.items():
            entry['project_overrides'][pkey]['parameters'][pname] = new_val

    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def delete_parameter(skill_id: str, name: str, *, project=None, storage_root=None) -> dict:
    """Remove a parameter (or a project override for that parameter)."""
    sid = _validate_skill_id(skill_id)
    pname = _validate_param_name(name)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    proj_key = _normalize_project_key(project)
    if proj_key is not None:
        overrides = entry.get('project_overrides') or {}
        proj_entry = overrides.get(proj_key)
        if proj_entry and pname in (proj_entry.get('parameters') or {}):
            del proj_entry['parameters'][pname]
            if proj_entry.get('overlay_text') is None and not proj_entry.get('parameters'):
                overrides.pop(proj_key, None)
    else:
        entry.get('parameters', {}).pop(pname, None)
        for pkey in list((entry.get('project_overrides') or {}).keys()):
            p_entry = entry['project_overrides'][pkey]
            (p_entry.get('parameters') or {}).pop(pname, None)
            if p_entry.get('overlay_text') is None and not p_entry.get('parameters'):
                del entry['project_overrides'][pkey]
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def remove_managed(skill_id: str, *, storage_root=None) -> bool:
    """Remove a managed skill entry; historical inert snapshots and saved sessions stay untouched."""
    sid = _validate_skill_id(skill_id)
    store = load_store(storage_root=storage_root)
    if sid not in store['skills']:
        return False
    del store['skills'][sid]
    _save_store(store, storage_root=storage_root)
    return True


def _unified(before: str, after: str, from_label: str, to_label: str) -> str:
    lines = list(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=from_label,
        tofile=to_label,
        lineterm='',
    ))
    return '\n'.join(lines[:400])


def _upstream_status(entry: dict, *, storage_root=None) -> dict:
    """Check whether a newer verified inert snapshot of the same bundle exists."""
    snaps = _bundles.list_snapshots(bundle_id=entry['bundle_id'], storage_root=storage_root)
    if not snaps:
        return {'available': False, 'reason': 'no_snapshots_in_storage'}
    latest = snaps[-1]
    if latest['snapshot_id'] == entry['snapshot_id'] and latest['bundle_sha256'] == entry['bundle_sha256']:
        return {'available': False, 'latest_snapshot_id': latest['snapshot_id']}
    try:
        raw_bytes, _ = _bundles.read_snapshot_file(
            latest['bundle_id'], latest['snapshot_id'], 'SKILL.md', storage_root=storage_root
        )
        upstream_text = raw_bytes.decode('utf-8')
    except (OSError, ValueError, UnicodeError) as exc:
        return {'available': False, 'error': str(exc)}
    return {
        'available': True,
        'latest_snapshot_id': latest['snapshot_id'],
        'latest_bundle_sha256': latest['bundle_sha256'],
        'latest_captured_at': latest['captured_at'],
        'skill_md_changed': _digest(upstream_text) != entry['base_sha256'],
        'skill_md_diff': _unified(
            entry['base_text'],
            upstream_text,
            f"base ({entry['snapshot_id']})",
            f"upstream ({latest['snapshot_id']})",
        ),
    }


def apply_upstream_update(
    skill_id: str,
    snapshot_ref: str = 'latest',
    *,
    keep_overlay: bool = False,
    storage_root=None,
) -> dict:
    """Explicitly advance a managed skill to a verified inert snapshot (keep_overlay preserves local text edit)."""
    sid = _validate_skill_id(skill_id)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    manifest = _bundles.load_snapshot(entry['bundle_id'], snapshot_ref, storage_root=storage_root, verify=True)
    raw_bytes, _ = _bundles.read_snapshot_file(
        manifest['bundle_id'], manifest['snapshot_id'], 'SKILL.md', storage_root=storage_root
    )
    new_base = raw_bytes.decode('utf-8')
    if len(raw_bytes) > MAX_SKILL_TEXT_BYTES:
        raise ValueError(f'Upstream SKILL.md exceeds {MAX_SKILL_TEXT_BYTES} bytes')
    entry['snapshot_id'] = manifest['snapshot_id']
    entry['bundle_sha256'] = manifest['bundle_sha256']
    entry['origin_path'] = manifest['source']['origin_path']
    entry['skill_name'] = manifest['source']['skill_name']
    entry['description'] = manifest['source']['description']
    entry['base_text'] = new_base
    entry['base_sha256'] = _digest(new_base)
    if not keep_overlay:
        entry['overlay_text'] = None
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, storage_root=storage_root)


def effective_view(skill_id: str, *, project=None, storage_root=None) -> dict:
    """Resolve effective content, typed parameters, provenance, overlay diff, and upstream update status."""
    sid = _validate_skill_id(skill_id)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')

    proj_key = _normalize_project_key(project)
    proj_override = (entry.get('project_overrides') or {}).get(proj_key) if proj_key else None

    merged_params = {}
    fill_values = {}
    for pname, spec in sorted((entry.get('parameters') or {}).items()):
        val = spec['value']
        source = 'global'
        if proj_override and pname in (proj_override.get('parameters') or {}):
            val = proj_override['parameters'][pname]
            source = 'project'
        merged_params[pname] = {
            'type': spec['type'],
            'default': spec['default'],
            'value': val,
            'source': source,
            'description': spec.get('description', ''),
            **({'choices': spec['choices']} if 'choices' in spec else {}),
        }
        fill_values[pname] = 'true' if val is True else 'false' if val is False else str(val)

    raw_text = entry['base_text']
    text_source = 'base'
    if entry.get('overlay_text') is not None:
        raw_text = entry['overlay_text']
        text_source = 'global_overlay'
    if proj_override and proj_override.get('overlay_text') is not None:
        raw_text = proj_override['overlay_text']
        text_source = 'project_overlay'

    rendered = _guidance.fill(raw_text, fill_values) if fill_values else raw_text
    overlay_diff = (
        _unified(entry['base_text'], raw_text, f"base ({entry['snapshot_id']})", text_source)
        if text_source != 'base' else ''
    )
    rendered_diff = (
        _unified(entry['base_text'], rendered, f"base ({entry['snapshot_id']})", 'effective')
        if rendered != entry['base_text'] else ''
    )
    upstream = _upstream_status(entry, storage_root=storage_root)

    return {
        'schema': SCHEMA,
        'authority': 'NONE',
        'effects_authorized': False,
        'skill_id': sid,
        'skill_name': entry['skill_name'],
        'description': entry['description'],
        'origin': {
            'bundle_id': entry['bundle_id'],
            'snapshot_id': entry['snapshot_id'],
            'bundle_sha256': entry['bundle_sha256'],
            'origin_path': entry['origin_path'],
            'base_sha256': entry['base_sha256'],
        },
        'text_source': text_source,
        'has_overlay': text_source != 'base',
        'has_project_override': bool(proj_override),
        'parameters': merged_params,
        'base_text': entry['base_text'],
        'raw_text': raw_text,
        'effective_text': rendered,
        'effective_sha256': _digest(rendered),
        'overlay_diff': overlay_diff,
        'rendered_diff': rendered_diff,
        'upstream_update': upstream,
        'created_at': entry['created_at'],
        'updated_at': entry['updated_at'],
    }


def list_managed(*, project=None, storage_root=None) -> list[dict]:
    """List all managed skills with provenance, overlay, and upstream-update flags."""
    store = load_store(storage_root=storage_root)
    rows = []
    for sid in sorted(store['skills']):
        view = effective_view(sid, project=project, storage_root=storage_root)
        rows.append({
            'skill_id': view['skill_id'],
            'skill_name': view['skill_name'],
            'description': view['description'],
            'bundle_id': view['origin']['bundle_id'],
            'snapshot_id': view['origin']['snapshot_id'],
            'effective_sha256': view['effective_sha256'],
            'text_source': view['text_source'],
            'has_overlay': view['has_overlay'],
            'has_project_override': view['has_project_override'],
            'parameter_count': len(view['parameters']),
            'upstream_update_available': bool(view['upstream_update'].get('available')),
            'latest_snapshot_id': view['upstream_update'].get('latest_snapshot_id'),
            'updated_at': view['updated_at'],
        })
    return rows


def runtime_bindings() -> list[dict]:
    """Honest per-runtime support and boundary table for tare managed skills (SKL-01)."""
    return [
        {
            'runtime': 'Claude CLI',
            'endpoint': 'cli://claude',
            'portable_instructions': 'Frozen into session system text at session start',
            'vendor_native_skills': 'Separate from tare managed skills; vendor tools remain off in tare sessions',
            'auxiliary_scripts_and_effects': 'Inert data only; requires explicit host permissions / Authority',
            'frozen_per_session': True,
        },
        {
            'runtime': 'Codex CLI',
            'endpoint': 'cli://codex',
            'portable_instructions': 'Frozen into session system text at session start',
            'vendor_native_skills': 'Codex skills_instructions left out by default in /guidance (#100) so tare skills stay authoritative',
            'auxiliary_scripts_and_effects': 'Inert data only; requires explicit host permissions / Authority',
            'frozen_per_session': True,
        },
        {
            'runtime': 'AGY CLI',
            'endpoint': 'cli://agy',
            'portable_instructions': 'Frozen into session system text at session start',
            'vendor_native_skills': 'Separate from tare managed skills; AGY host tools remain disabled under tare',
            'auxiliary_scripts_and_effects': 'Inert data only; requires explicit host permissions / Authority',
            'frozen_per_session': True,
        },
        {
            'runtime': 'Direct HTTP API',
            'endpoint': 'https://*',
            'portable_instructions': 'Frozen into session system text at session start',
            'vendor_native_skills': 'None (portable tare skill content only)',
            'auxiliary_scripts_and_effects': 'Inert data only; requires explicit host permissions / Authority',
            'frozen_per_session': True,
        },
        {
            'runtime': 'Local Gateway',
            'endpoint': 'http://127.0.0.1 / local',
            'portable_instructions': 'Frozen into session system text at session start',
            'vendor_native_skills': 'None (portable tare skill content only)',
            'auxiliary_scripts_and_effects': 'Inert data only; requires explicit host permissions / Authority',
            'frozen_per_session': True,
        },
    ]


def validate_skill_ids(skill_ids) -> list[str]:
    """Validate an explicit list of managed skill IDs for a new session."""
    if skill_ids is None:
        return []
    if not isinstance(skill_ids, (list, tuple)):
        raise ValueError('managed_skills must be a list of skill IDs')
    if len(skill_ids) > MAX_SESSION_SKILLS:
        raise ValueError(f'At most {MAX_SESSION_SKILLS} managed skills may be active in one session')
    out: list[str] = []
    seen: set[str] = set()
    for raw in skill_ids:
        sid = _validate_skill_id(raw)
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def freeze_for_session(skill_ids, *, project=None, storage_root=None) -> list[dict]:
    """Verify backing inert snapshots and freeze rendered managed skills for a single new session."""
    ids = validate_skill_ids(skill_ids)
    if not ids:
        return []
    frozen: list[dict] = []
    total_bytes = 0
    for sid in ids:
        view = effective_view(sid, project=project, storage_root=storage_root)
        # Fail closed if the underlying inert snapshot was tampered with or removed.
        _bundles.load_snapshot(
            view['origin']['bundle_id'],
            view['origin']['snapshot_id'],
            storage_root=storage_root,
            verify=True,
        )
        encoded_size = len(view['effective_text'].encode('utf-8'))
        if encoded_size > MAX_SKILL_TEXT_BYTES:
            raise ValueError(f'Effective skill {sid!r} exceeds {MAX_SKILL_TEXT_BYTES} bytes')
        total_bytes += encoded_size
        if total_bytes > MAX_SESSION_SKILLS_BYTES:
            raise ValueError(f'Combined active managed skills exceed {MAX_SESSION_SKILLS_BYTES} bytes')
        frozen.append({
            'skill_id': view['skill_id'],
            'skill_name': view['skill_name'],
            'bundle_id': view['origin']['bundle_id'],
            'snapshot_id': view['origin']['snapshot_id'],
            'bundle_sha256': view['origin']['bundle_sha256'],
            'base_sha256': view['origin']['base_sha256'],
            'origin_path': view['origin']['origin_path'],
            'text_source': view['text_source'],
            'parameters': {k: v['value'] for k, v in view['parameters'].items()},
            'effective_sha256': view['effective_sha256'],
            'text': view['effective_text'],
            'authority': 'NONE',
            'effects_authorized': False,
        })
    return frozen


def session_system_block(frozen_skills) -> str:
    """Format frozen managed skills for a session's system prompt, preserving PA-04 boundaries."""
    if not frozen_skills:
        return ''
    parts = [
        '\n\nExplicitly selected tare managed skills (portable guidance frozen at session start; '
        'distinct from permanent /guidance; auxiliary files, scripts, and external bindings remain inert data '
        'and are neither executed nor granted permissions by these skills):'
    ]
    for skill in frozen_skills:
        parts.append(
            f"\n--- Skill: {skill['skill_id']} ({skill['skill_name']}) "
            f"[snapshot={skill['snapshot_id']}, sha256={skill['effective_sha256'][:12]}, "
            f"source={skill['text_source']}] ---\n{skill['text']}"
        )
    return '\n'.join(parts)


def format_effective(view: dict) -> str:
    """Human-readable summary of a managed skill's origin, parameters, diff, and update status."""
    orig = view['origin']
    lines = [
        f"Managed skill: {view['skill_id']} ({view['skill_name']})",
        f"Origin: {orig['bundle_id']}@{orig['snapshot_id']} ({orig['origin_path']})",
        f"Text source: {view['text_source']}  Effective SHA-256: {view['effective_sha256']}",
        f"Authority: {view['authority']}  Effects authorized: {view['effects_authorized']}",
    ]
    if view['parameters']:
        lines.append('Parameters:')
        for pname, spec in view['parameters'].items():
            desc = f" — {spec['description']}" if spec.get('description') else ''
            lines.append(f"  - <{pname}> ({spec['type']}, {spec['source']}) = {spec['value']!r} (default {spec['default']!r}){desc}")
    up = view['upstream_update']
    if up.get('available'):
        lines.append(
            f"Upstream update available: {orig['snapshot_id']} -> {up['latest_snapshot_id']} "
            f"(captured {up['latest_captured_at']}; SKILL.md changed={up['skill_md_changed']})"
        )
    else:
        lines.append(f"Upstream status: up to date ({orig['snapshot_id']})")
    if view['rendered_diff']:
        lines.append('\nDiff vs base snapshot:')
        lines.append(view['rendered_diff'])
    return '\n'.join(lines)


def format_bindings(rows=None) -> str:
    rows = runtime_bindings() if rows is None else rows
    lines = ['Runtime support for tare managed skills (frozen per session, portable guidance only):']
    for r in rows:
        lines.append(f"\n- {r['runtime']} ({r['endpoint']})")
        lines.append(f"  Instructions: {r['portable_instructions']}")
        lines.append(f"  Native vendor skills: {r['vendor_native_skills']}")
        lines.append(f"  Auxiliary scripts/effects: {r['auxiliary_scripts_and_effects']}")
    return '\n'.join(lines)

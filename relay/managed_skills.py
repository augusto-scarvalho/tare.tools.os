"""Managed, configurable skill content with explicit origin, typed parameters, overlays, upstream diffs,
per-runtime binding visibility, and frozen per-session activation (SKL-01 / PA-04).

Configuration lives in <storage>/serve/skills/managed.json, strictly separate from permanent /guidance
(<storage>/serve/guidance.json) and from inert bundle snapshots (<storage>/serve/skills/snapshots/).
Editing or updating a managed skill never alters already-started sessions or grants effect/tool permissions.
"""
from copy import deepcopy
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
MAX_TRIGGERS = 32
MAX_TRIGGER_LEN = 64
PARAM_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9 _.-]{0,39}$')
PARAM_TYPES = ('string', 'int', 'float', 'bool', 'choice')
MODEL_CLASSES = ('cheap', 'middling', 'frontier')  # ascending, matching model_signals.ORDER

SKILL_ROLES = (
    'coordinator',
    'implementer',
    'planner',
    'plan-auditor',
    'auditor',
    'explorer',
    'researcher',
    'vision',
    'heads_up',
)
ROLE_LABELS = {
    'coordinator': 'Coordinator',
    'implementer': 'Implementer',
    'planner': 'Planner',
    'plan-auditor': 'Plan auditor',
    'auditor': 'Auditor',
    'explorer': 'Explorer',
    'researcher': 'Researcher',
    'vision': 'Vision',
    'heads_up': 'Heads-up',
}
ROLE_SHORT_LABELS = {
    'coordinator': 'coord',
    'implementer': 'impl',
    'planner': 'plan',
    'plan-auditor': 'p-aud',
    'auditor': 'aud',
    'explorer': 'expl',
    'researcher': 'res',
    'vision': 'vis',
    'heads_up': 'hu',
}
# Fail-closed defaults: lean/focused roles stay free of managed skills unless explicitly enabled by the owner.
DEFAULT_DISABLED_ROLES = frozenset({'explorer', 'researcher', 'vision', 'heads_up'})
DEFAULT_ENABLED_ROLES = tuple(r for r in SKILL_ROLES if r not in DEFAULT_DISABLED_ROLES)


def _validate_role(role: str) -> str:
    if not isinstance(role, str) or role not in SKILL_ROLES:
        raise ValueError(f'Unknown skill role {role!r}; use one of: {", ".join(SKILL_ROLES)}')
    return role


def _validate_roles_list(roles) -> list[str]:
    if not isinstance(roles, (list, tuple)):
        raise ValueError('allowed_roles must be a list of role names')
    seen: set[str] = set()
    for raw in roles:
        seen.add(_validate_role(raw))
    return [r for r in SKILL_ROLES if r in seen]


def _validate_role_policy_map(raw, *, context: str = 'role_policy') -> dict[str, bool]:
    if not isinstance(raw, dict):
        raise ValueError(f'{context} must be a mapping of role -> bool')
    out: dict[str, bool] = {}
    for k, v in raw.items():
        role = _validate_role(k)
        if type(v) is not bool:
            raise ValueError(f'{context}[{role!r}] must be a boolean')
        out[role] = v
    return out


def _validate_triggers_list(triggers) -> list[str]:
    if not isinstance(triggers, (list, tuple)):
        raise ValueError('triggers must be a list of keyword/phrase strings')
    if len(triggers) > MAX_TRIGGERS:
        raise ValueError(f'At most {MAX_TRIGGERS} triggers are allowed per managed skill')
    out: list[str] = []
    seen: set[str] = set()
    for raw in triggers:
        if not isinstance(raw, str):
            raise ValueError('Each trigger must be a string')
        cleaned = ' '.join(raw.strip().split())
        if not cleaned or len(cleaned) > MAX_TRIGGER_LEN:
            raise ValueError(f'Each trigger must be 1..{MAX_TRIGGER_LEN} characters')
        low = cleaned.lower()
        if low not in seen:
            seen.add(low)
            out.append(cleaned)
    return out


def _validate_model_class(value) -> str | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str) or value not in MODEL_CLASSES:
        raise ValueError(f'Unknown model class {value!r}; use one of: {", ".join(MODEL_CLASSES)}')
    return value


def max_model_class(classes) -> str | None:
    """Return the highest model class floor along MODEL_CLASSES (cheap < middling < frontier), or None."""
    best_idx = -1
    for raw in classes or ():
        cls = _validate_model_class(raw)
        if cls is not None:
            idx = MODEL_CLASSES.index(cls)
            if idx > best_idx:
                best_idx = idx
    return MODEL_CLASSES[best_idx] if best_idx >= 0 else None


def skills_min_model_class(skills) -> str | None:
    """Return the highest frozen min_model_class declared across a slice of managed skills, or None."""
    return max_model_class(
        item.get('min_model_class') for item in (skills or ()) if isinstance(item, dict)
    )


def _project_override_empty(proj_entry: dict) -> bool:
    return (
        proj_entry.get('overlay_text') is None
        and not proj_entry.get('parameters')
        and proj_entry.get('allowed_roles') is None
        and proj_entry.get('triggers') is None
        and proj_entry.get('min_model_class') is None
    )


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
    if 'role_policy' in parsed:
        parsed['role_policy'] = _validate_role_policy_map(parsed['role_policy'], context='role_policy')
    if 'project_role_policies' in parsed:
        raw_proj = parsed['project_role_policies']
        if not isinstance(raw_proj, dict):
            raise ValueError('project_role_policies must be a mapping of project -> role policy')
        validated_proj = {}
        for pkey, pmap in raw_proj.items():
            if not isinstance(pkey, str) or not pkey:
                raise ValueError('project_role_policies keys must be non-empty project paths')
            validated_proj[pkey] = _validate_role_policy_map(pmap, context=f'project_role_policies[{pkey!r}]')
        parsed['project_role_policies'] = validated_proj
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
    allowed_roles=None,
    min_model_class: str | None = None,
    origin_vendor: str | None = None,
    origin_vendors: list[str] | None = None,
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
    roles = _validate_roles_list(allowed_roles) if allowed_roles is not None else list(DEFAULT_ENABLED_ROLES)
    validated_class = _validate_model_class(min_model_class)
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
        'origin_vendor': origin_vendor,
        'origin_vendors': list(origin_vendors) if origin_vendors else ([origin_vendor] if origin_vendor else []),
        'skill_name': manifest['source']['skill_name'],
        'description': manifest['source']['description'],
        'base_sha256': _digest(base_text),
        'base_text': base_text,
        'overlay_text': None,
        'allowed_roles': roles,
        'min_model_class': validated_class,
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
        if _project_override_empty(proj_entry):
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
            if _project_override_empty(proj_entry):
                overrides.pop(proj_key, None)
    else:
        entry.get('parameters', {}).pop(pname, None)
        for pkey in list((entry.get('project_overrides') or {}).keys()):
            p_entry = entry['project_overrides'][pkey]
            (p_entry.get('parameters') or {}).pop(pname, None)
            if _project_override_empty(p_entry):
                del entry['project_overrides'][pkey]
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def _resolve_role_policy_detail(store: dict, proj_key: str | None = None) -> dict[str, dict]:
    global_pol = store.get('role_policy') or {}
    proj_pol = ((store.get('project_role_policies') or {}).get(proj_key) or {}) if proj_key else {}
    out: dict[str, dict] = {}
    for role in SKILL_ROLES:
        enabled = role not in DEFAULT_DISABLED_ROLES
        source = 'default'
        if role in global_pol:
            enabled = bool(global_pol[role])
            source = 'global'
        if role in proj_pol:
            enabled = bool(proj_pol[role])
            source = 'project'
        out[role] = {'enabled': enabled, 'source': source}
    return out


def get_role_policy(*, project=None, storage_root=None) -> dict[str, dict]:
    """Return effective per-role skill policy ({role: {'enabled': bool, 'source': str}})."""
    store = load_store(storage_root=storage_root)
    return _resolve_role_policy_detail(store, _normalize_project_key(project))


def effective_role_policy(*, project=None, storage_root=None) -> dict[str, bool]:
    """Return compact {role: bool} effective policy for whether a role may receive managed skills."""
    detail = get_role_policy(project=project, storage_root=storage_root)
    return {role: info['enabled'] for role, info in detail.items()}


def set_role_policy(
    role: str,
    enabled: bool | None,
    *,
    project=None,
    storage_root=None,
) -> dict[str, dict]:
    """Enable, disable, or reset (None) managed-skill access for an entire role (global or per-project)."""
    target_role = _validate_role(role)
    if enabled is not None and type(enabled) is not bool:
        raise ValueError('Role policy enabled flag must be True, False, or None')
    store = load_store(storage_root=storage_root)
    proj_key = _normalize_project_key(project)
    if proj_key is None:
        pol = store.setdefault('role_policy', {})
        if enabled is None:
            pol.pop(target_role, None)
            if not pol:
                store.pop('role_policy', None)
        else:
            pol[target_role] = enabled
    else:
        proj_pols = store.setdefault('project_role_policies', {})
        pol = proj_pols.setdefault(proj_key, {})
        if enabled is None:
            pol.pop(target_role, None)
            if not pol:
                proj_pols.pop(proj_key, None)
            if not proj_pols:
                store.pop('project_role_policies', None)
        else:
            pol[target_role] = enabled
    _save_store(store, storage_root=storage_root)
    return _resolve_role_policy_detail(store, proj_key)


def set_skill_roles(
    skill_id: str,
    roles,
    *,
    project=None,
    storage_root=None,
) -> dict:
    """Set allowed roles for one managed skill (global or per-project). Pass None to reset/clear override."""
    sid = _validate_skill_id(skill_id)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    proj_key = _normalize_project_key(project)
    if proj_key is None:
        entry['allowed_roles'] = (
            list(DEFAULT_ENABLED_ROLES) if roles is None else _validate_roles_list(roles)
        )
    else:
        overrides = entry.setdefault('project_overrides', {})
        if roles is None:
            proj_entry = overrides.get(proj_key)
            if proj_entry is not None:
                proj_entry.pop('allowed_roles', None)
                if _project_override_empty(proj_entry):
                    overrides.pop(proj_key, None)
        else:
            proj_entry = overrides.setdefault(proj_key, {'overlay_text': None, 'parameters': {}})
            proj_entry['allowed_roles'] = _validate_roles_list(roles)
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def set_triggers(
    skill_id: str,
    triggers,
    *,
    project=None,
    storage_root=None,
) -> dict:
    """Set explicit lexical trigger keywords/phrases for deterministic selection (global or per-project)."""
    sid = _validate_skill_id(skill_id)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    proj_key = _normalize_project_key(project)
    if proj_key is None:
        entry['triggers'] = [] if triggers is None else _validate_triggers_list(triggers)
    else:
        overrides = entry.setdefault('project_overrides', {})
        if triggers is None:
            proj_entry = overrides.get(proj_key)
            if proj_entry is not None:
                proj_entry.pop('triggers', None)
                if _project_override_empty(proj_entry):
                    overrides.pop(proj_key, None)
        else:
            proj_entry = overrides.setdefault(proj_key, {'overlay_text': None, 'parameters': {}})
            proj_entry['triggers'] = _validate_triggers_list(triggers)
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def set_model_class(
    skill_id: str,
    model_class: str | None,
    *,
    project=None,
    storage_root=None,
) -> dict:
    """Set or clear the minimum recommended model class (cheap, middling, frontier) for one managed skill."""
    sid = _validate_skill_id(skill_id)
    validated = _validate_model_class(model_class)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')
    proj_key = _normalize_project_key(project)
    if proj_key is None:
        entry['min_model_class'] = validated
    else:
        overrides = entry.setdefault('project_overrides', {})
        if validated is None:
            proj_entry = overrides.get(proj_key)
            if proj_entry is not None:
                proj_entry.pop('min_model_class', None)
                if _project_override_empty(proj_entry):
                    overrides.pop(proj_key, None)
        else:
            proj_entry = overrides.setdefault(proj_key, {'overlay_text': None, 'parameters': {}})
            proj_entry['min_model_class'] = validated
    entry['updated_at'] = _now_iso()
    _save_store(store, storage_root=storage_root)
    return effective_view(sid, project=project, storage_root=storage_root)


def catalog_index_fingerprint(*, project=None, storage_root=None) -> str:
    """Deterministic SHA-256 fingerprint of effective managed catalog metadata (invalidates stale selection receipts)."""
    store = load_store(storage_root=storage_root)
    proj_key = _normalize_project_key(project)
    policy = {r: info['enabled'] for r, info in _resolve_role_policy_detail(store, proj_key).items()}
    rows = []
    for sid in sorted(store['skills']):
        view = effective_view(sid, project=project, include_status=False, storage_root=storage_root)
        rows.append({
            'skill_id': sid,
            'effective_sha256': view['effective_sha256'],
            'allowed_roles': view['allowed_roles'],
            'effective_roles': view['effective_roles'],
            'triggers': view['triggers'],
            'min_model_class': view['min_model_class'],
        })
    payload = json.dumps({'schema': SCHEMA, 'role_policy': policy, 'skills': rows}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


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
    bdir = _bundles.snapshots_root(storage_root) / entry['bundle_id']
    if not bdir.is_dir():
        return {'available': False, 'reason': 'no_snapshots_in_storage'}
    snap_dirs = [
        child for child in bdir.iterdir()
        if child.is_dir() and _bundles.SNAPSHOT_ID_RE.match(child.name) and (child / 'manifest.json').is_file()
    ]
    if not snap_dirs:
        return {'available': False, 'reason': 'no_snapshots_in_storage'}
    # Fast path: when only the already-adopted snapshot exists on disk, no newer upstream snapshot can exist.
    # Full cryptographic verification still runs unconditionally on selection, freezing, and update.
    if len(snap_dirs) == 1 and snap_dirs[0].name == entry['snapshot_id']:
        return {'available': False, 'latest_snapshot_id': entry['snapshot_id']}
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


def effective_view(
    skill_id: str,
    *,
    project=None,
    role: str | None = None,
    include_status: bool = True,
    storage_root=None,
) -> dict:
    """Resolve effective content, typed parameters, role access, provenance, overlay diff, and upstream update status."""
    sid = _validate_skill_id(skill_id)
    if role is not None:
        role = _validate_role(role)
    store = load_store(storage_root=storage_root)
    entry = store['skills'].get(sid)
    if entry is None:
        raise ValueError(f'Managed skill {sid!r} not found')

    proj_key = _normalize_project_key(project)
    proj_override = (entry.get('project_overrides') or {}).get(proj_key) if proj_key else None

    policy_detail = _resolve_role_policy_detail(store, proj_key)
    role_policy = {r: info['enabled'] for r, info in policy_detail.items()}
    raw_global_roles = entry.get('allowed_roles')
    global_roles = _validate_roles_list(raw_global_roles) if raw_global_roles is not None else list(DEFAULT_ENABLED_ROLES)
    if proj_override and proj_override.get('allowed_roles') is not None:
        allowed_roles = _validate_roles_list(proj_override['allowed_roles'])
        roles_source = 'project'
    else:
        allowed_roles = global_roles
        roles_source = 'global'
    effective_roles = [r for r in allowed_roles if role_policy[r]]
    disabled_by_policy_roles = [r for r in allowed_roles if not role_policy[r]]

    raw_global_triggers = entry.get('triggers')
    global_triggers = _validate_triggers_list(raw_global_triggers) if raw_global_triggers is not None else []
    if proj_override and proj_override.get('triggers') is not None:
        triggers = _validate_triggers_list(proj_override['triggers'])
        triggers_source = 'project'
    else:
        triggers = global_triggers
        triggers_source = 'global'

    global_model_class = _validate_model_class(entry.get('min_model_class'))
    if proj_override and proj_override.get('min_model_class') is not None:
        min_model_class = _validate_model_class(proj_override['min_model_class'])
        min_model_class_source = 'project'
    elif global_model_class is not None:
        min_model_class = global_model_class
        min_model_class_source = 'global'
    else:
        min_model_class = None
        min_model_class_source = 'none'

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
    if include_status:
        upstream = _upstream_status(entry, storage_root=storage_root)
        from .vendor_skills import check_origin_drift
        origin_drift = check_origin_drift(entry.get('origin_path'), entry['base_sha256'], entry['base_text'])
    else:
        upstream = {'available': False, 'latest_snapshot_id': entry['snapshot_id']}
        origin_drift = {'available': False}

    view = {
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
            'origin_vendor': entry.get('origin_vendor'),
            'origin_vendors': list(entry.get('origin_vendors') or ()),
            'base_sha256': entry['base_sha256'],
        },
        'text_source': text_source,
        'has_overlay': text_source != 'base',
        'has_project_override': bool(proj_override),
        'allowed_roles': allowed_roles,
        'roles_source': roles_source,
        'role_policy': role_policy,
        'effective_roles': effective_roles,
        'disabled_by_policy_roles': disabled_by_policy_roles,
        'triggers': triggers,
        'triggers_source': triggers_source,
        'min_model_class': min_model_class,
        'min_model_class_source': min_model_class_source,
        'parameters': merged_params,
        'base_text': entry['base_text'],
        'raw_text': raw_text,
        'effective_text': rendered,
        'effective_sha256': _digest(rendered),
        'overlay_diff': overlay_diff,
        'rendered_diff': rendered_diff,
        'upstream_update': upstream,
        'origin_drift': origin_drift,
        'created_at': entry['created_at'],
        'updated_at': entry['updated_at'],
    }
    if role is not None:
        if not role_policy[role]:
            access_reason = 'role_disabled_by_policy'
            allowed_now = False
        elif role not in allowed_roles:
            access_reason = 'role_not_allowed_on_skill'
            allowed_now = False
        else:
            access_reason = 'allowed'
            allowed_now = True
        view['role_access'] = {'role': role, 'allowed': allowed_now, 'reason': access_reason}
    return view


def list_managed(*, project=None, role: str | None = None, storage_root=None) -> list[dict]:
    """List managed skills with provenance, overlay, role-scope, and upstream-update flags."""
    if role is not None:
        role = _validate_role(role)
    store = load_store(storage_root=storage_root)
    rows = []
    for sid in sorted(store['skills']):
        view = effective_view(sid, project=project, role=role, storage_root=storage_root)
        if role is not None and not view['role_access']['allowed']:
            continue
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
            'allowed_roles': view['allowed_roles'],
            'roles_source': view['roles_source'],
            'effective_roles': view['effective_roles'],
            'disabled_by_policy_roles': view['disabled_by_policy_roles'],
            'triggers': view['triggers'],
            'min_model_class': view['min_model_class'],
            'parameter_count': len(view['parameters']),
            'origin_vendor': view['origin'].get('origin_vendor'),
            'origin_vendors': list(view['origin'].get('origin_vendors') or ()),
            'upstream_update_available': bool(view['upstream_update'].get('available')),
            'origin_drift_available': bool(view['origin_drift'].get('available')),
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


def freeze_session_pool(skill_ids, *, project=None, storage_root=None) -> dict:
    """Verify backing inert snapshots and freeze rendered managed skills + role policy for one new session tree."""
    ids = validate_skill_ids(skill_ids)
    role_policy = effective_role_policy(project=project, storage_root=storage_root)
    if not ids:
        return {'schema': SCHEMA, 'role_policy': role_policy, 'skills': []}
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
            'allowed_roles': list(view['allowed_roles']),
            'roles_source': view['roles_source'],
            'effective_roles': list(view['effective_roles']),
            'min_model_class': view['min_model_class'],
            'parameters': {k: v['value'] for k, v in view['parameters'].items()},
            'effective_sha256': view['effective_sha256'],
            'text': view['effective_text'],
            'authority': 'NONE',
            'effects_authorized': False,
        })
    return {'schema': SCHEMA, 'role_policy': role_policy, 'skills': frozen}


def partition_pool_for_role(pool: dict, role: str) -> tuple[list[dict], list[dict]]:
    """Split a frozen session skill pool into (active_for_role, withheld_for_role) without reading disk."""
    target_role = _validate_role(role)
    if not isinstance(pool, dict) or not isinstance(pool.get('skills'), list):
        raise ValueError('Invalid frozen managed_skills_pool')
    raw_policy = pool.get('role_policy')
    if not isinstance(raw_policy, dict):
        raise ValueError('Invalid role_policy in frozen managed_skills_pool')
    role_enabled = bool(raw_policy.get(target_role, False))
    active: list[dict] = []
    withheld: list[dict] = []
    for skill in pool['skills']:
        eff_roles = list(skill.get('effective_roles') or ())
        if not role_enabled:
            withheld.append({
                'skill_id': skill['skill_id'],
                'role': target_role,
                'reason': 'role_disabled_by_policy',
                'effective_roles': eff_roles,
            })
        elif target_role in eff_roles:
            active.append(deepcopy(skill))
        else:
            withheld.append({
                'skill_id': skill['skill_id'],
                'role': target_role,
                'reason': 'role_not_allowed_on_skill',
                'effective_roles': eff_roles,
            })
    return active, withheld


def frozen_for_role(parent_state: dict | None, role: str) -> list[dict]:
    """Return the parent session's frozen managed skills allowed for a child role (never reads live managed.json)."""
    target_role = _validate_role(role)
    if not isinstance(parent_state, dict):
        return []
    pool = parent_state.get('managed_skills_pool')
    if not pool:
        return []
    active, _ = partition_pool_for_role(pool, target_role)
    return active


def validate_inherited_frozen_skills(skills, *, role: str) -> list[dict]:
    """Fail-closed validation of frozen managed skills inherited by a child session for one role."""
    target_role = _validate_role(role)
    if not isinstance(skills, (list, tuple)):
        raise ValueError('inherited_managed_skills must be a list of frozen skill records')
    if len(skills) > MAX_SESSION_SKILLS:
        raise ValueError(f'At most {MAX_SESSION_SKILLS} managed skills may be inherited by one session')
    validated: list[dict] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in skills:
        if not isinstance(item, dict):
            raise ValueError('Inherited managed skill record must be a dict')
        sid = _validate_skill_id(item.get('skill_id'))
        if sid in seen:
            raise ValueError(f'Duplicate inherited managed skill: {sid!r}')
        seen.add(sid)
        if item.get('authority') != 'NONE' or item.get('effects_authorized') is not False:
            raise ValueError(f'Inherited managed skill {sid!r} must carry authority=NONE and effects_authorized=False')
        text = item.get('text')
        if not isinstance(text, str):
            raise ValueError(f'Inherited managed skill {sid!r} is missing text')
        encoded_size = len(text.encode('utf-8'))
        if encoded_size > MAX_SKILL_TEXT_BYTES:
            raise ValueError(f'Inherited managed skill {sid!r} exceeds {MAX_SKILL_TEXT_BYTES} bytes')
        total_bytes += encoded_size
        if total_bytes > MAX_SESSION_SKILLS_BYTES:
            raise ValueError(f'Combined inherited managed skills exceed {MAX_SESSION_SKILLS_BYTES} bytes')
        if item.get('effective_sha256') != _digest(text):
            raise ValueError(f'Inherited managed skill {sid!r} effective_sha256 mismatch')
        eff_roles = _validate_roles_list(item.get('effective_roles'))
        if target_role not in eff_roles:
            raise ValueError(f'Inherited managed skill {sid!r} is not authorized for role {target_role!r}')
        _validate_model_class(item.get('min_model_class'))
        validated.append(deepcopy(item))
    return validated


def freeze_for_session(skill_ids, *, project=None, role: str | None = None, storage_root=None) -> list[dict]:
    """Verify backing inert snapshots and freeze rendered managed skills for a single new session."""
    pool = freeze_session_pool(skill_ids, project=project, storage_root=storage_root)
    if role is None:
        return pool['skills']
    active, _ = partition_pool_for_role(pool, role)
    return active


def session_system_block(frozen_skills, *, delegated_pool: dict | None = None, role: str | None = None) -> str:
    """Format frozen managed skills for a session's system prompt, preserving PA-04 and role boundaries."""
    parts: list[str] = []
    if frozen_skills:
        parts.append(
            '\n\nExplicitly selected tare managed skills (portable guidance frozen at session start; '
            'distinct from permanent /guidance; auxiliary files, scripts, and external bindings remain inert data '
            'and are neither executed nor granted permissions by these skills; if a skill references local tools '
            'or docs under its directory outside the project, request read access with request_folder_access first):'
        )
        for skill in frozen_skills:
            dir_tag = f", directory={skill['origin_path']}" if skill.get('origin_path') else ''
            parts.append(
                f"\n--- Skill: {skill['skill_id']} ({skill['skill_name']}) "
                f"[snapshot={skill['snapshot_id']}, sha256={skill['effective_sha256'][:12]}, "
                f"source={skill['text_source']}{dir_tag}] ---\n{skill['text']}"
            )
    if role == 'coordinator' and isinstance(delegated_pool, dict):
        child_only = []
        for skill in delegated_pool.get('skills') or ():
            eff_roles = [r for r in (skill.get('effective_roles') or ()) if r != 'coordinator']
            if eff_roles and 'coordinator' not in (skill.get('effective_roles') or ()):
                tier_tag = f" [tier={skill['min_model_class']}]" if skill.get('min_model_class') else ''
                child_only.append(f"{skill['skill_id']} -> {', '.join(eff_roles)}{tier_tag}")
        if child_only:
            parts.append(
                '\n\nDelegated role skills frozen for child roles (not loaded into coordinator prompt): '
                + '; '.join(child_only) + '.'
            )
    return '\n'.join(parts)


def format_role_tags(effective_roles: list[str]) -> str:
    """Compact role summary for TUI/CLI lists."""
    if not effective_roles:
        return 'no-roles'
    if tuple(effective_roles) == DEFAULT_ENABLED_ROLES:
        return '5 roles'
    return ','.join(ROLE_SHORT_LABELS.get(r, r) for r in effective_roles)


def format_role_policy(policy_detail: dict[str, dict]) -> str:
    """Human-readable table of per-role skill availability policy."""
    lines = ['Per-role managed skill policy (intersection with each skill allowed_roles):']
    for role in SKILL_ROLES:
        info = policy_detail[role]
        state = 'ENABLED ' if info['enabled'] else 'DISABLED'
        lines.append(f"  - {role:<13} {state} ({info['source']})")
    return '\n'.join(lines)


def format_effective(view: dict) -> str:
    """Human-readable summary of a managed skill's origin, roles, parameters, diff, and update status."""
    orig = view['origin']
    eff_roles = ', '.join(view['effective_roles']) if view['effective_roles'] else '(none)'
    lines = [
        f"Managed skill: {view['skill_id']} ({view['skill_name']})",
        f"Origin: {orig['bundle_id']}@{orig['snapshot_id']} ({orig['origin_path']})",
        f"Text source: {view['text_source']}  Effective SHA-256: {view['effective_sha256']}",
        f"Roles ({view['roles_source']}): effective=[{eff_roles}]  allowed=[{', '.join(view['allowed_roles']) or '(none)'}]",
        f"Authority: {view['authority']}  Effects authorized: {view['effects_authorized']}",
    ]
    if view.get('disabled_by_policy_roles'):
        lines.append(
            'Blocked by role policy: ' + ', '.join(view['disabled_by_policy_roles'])
            + ' (enable via `tare skills role-policy <role> --enable`)'
        )
    if 'role_access' in view:
        ra = view['role_access']
        lines.append(f"Role check ({ra['role']}): allowed={ra['allowed']} ({ra['reason']})")
    if view.get('triggers'):
        lines.append(f"Triggers ({view['triggers_source']}): {', '.join(view['triggers'])}")
    if view.get('min_model_class'):
        lines.append(f"Minimum model class ({view['min_model_class_source']}): {view['min_model_class']}")
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
    drift = view.get('origin_drift') or {}
    if drift.get('available'):
        lines.append(
            f"Origin drift detected on disk: live SKILL.md sha256={drift['current_sha256'][:12]} "
            '(run `tare skills update <id> --from-origin` or click Update in /skills)'
        )
    if view['rendered_diff']:
        lines.append('\nDiff vs base snapshot:')
        lines.append(view['rendered_diff'])
    if drift.get('available') and drift.get('skill_md_diff'):
        lines.append('\nDiff vs live origin:')
        lines.append(drift['skill_md_diff'])
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

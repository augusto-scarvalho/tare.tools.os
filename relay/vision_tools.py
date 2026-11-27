"""Bounded, read-only `inspect_image` tool wrapper for delegated implementers and auditors.

Strictly confines every read to admitted repositories (or the parent session's
read-only `attachments` store when provided by the host), rejects symlinks,
traversal, oversized files, and non-image bytes via magic-header verification,
enforces a per-session call cap persisted under session custody (idempotent by
`effect_id`), and delegates visual inspection to `turn_engine.describe_files()`.
Returns untrusted read-only observations (`authority='NONE'`) with zero write or
shell authority.
"""
import hashlib
from pathlib import Path, PurePosixPath
import re
import tempfile

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode, object_json


MAX_IMAGE_INSPECTIONS = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_OBSERVATION_BYTES = 4096
MAX_STORE_BYTES = 131072
INSPECTIONS_FILE = 'vision-inspections.json'
ALLOWED_ROLES = frozenset({'implementer', 'auditor'})
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
IMAGE_EXTENSIONS = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
}
EXT_BY_MEDIA = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/webp': '.webp',
    'image/gif': '.gif',
}
SECRET_SUFFIXES = ('.env', '.pem', '.key', '.p12', '.pfx', '.jks', '.kdbx', '.netrc')

INSPECT_IMAGE_SPEC = {
    'inspect_image': {
        'description': (
            'Inspect a local image file (PNG, JPEG, WebP, or GIF) inside an admitted repository '
            '(or repository="attachments" for preserved session image attachments) using the host\'s '
            'configured vision route. Read-only, capped at 6 inspections per session; returns an '
            'untrusted visual observation (authority="NONE") that must be reconciled with source code and checks.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'repository': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 128,
                    'description': 'Admitted repository alias (for example the project name, "candidate", "review", or "attachments").',
                },
                'path': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 512,
                    'description': 'Repository-relative POSIX path to an image file (or 64-hex sha256 when repository="attachments").',
                },
                'question': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 1500,
                    'description': 'Specific visual question or detail to verify in the image.',
                },
            },
            'required': ['repository', 'path', 'question'],
            'additionalProperties': False,
        },
    },
}


def detect_image_media_type(data: bytes) -> str | None:
    """Verify real image magic bytes so non-image/secret files renamed to .png are always rejected."""
    if len(data) < 8:
        return None
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    return None


def _is_secret_filename(name: str) -> bool:
    low = name.lower()
    return low == '.env' or low.startswith('.env.') or any(low.endswith(suf) for suf in SECRET_SUFFIXES)


def resolve_confined_image(base: Path, raw_path: str, *, require_sha256_path: bool = False):
    """Resolve and read a regular image file strictly inside `base`, refusing symlinks, traversal, and non-images.

    Returns `(normalized_posix_path, media_type, raw_bytes, sha256_hex)`.
    """
    if not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 512 or '\0' in raw_path:
        raise ValueError('inspect_image_invalid_path')
    if '\\' in raw_path or ':' in raw_path or raw_path.startswith('/'):
        raise ValueError('inspect_image_relative_posix_path_required')
    cleaned = raw_path.strip()
    while cleaned.startswith('./'):
        cleaned = cleaned[2:]
    posix = PurePosixPath(cleaned)
    parts = posix.parts
    if not cleaned or posix.is_absolute() or not parts or any(p in ('', '.', '..', '.git') for p in parts):
        raise ValueError('inspect_image_unsafe_path')
    if any(_is_secret_filename(part) for part in parts):
        raise ValueError('inspect_image_disallowed_file')
    if require_sha256_path and (len(parts) != 1 or not SHA256_RE.fullmatch(parts[0])):
        raise ValueError('inspect_image_attachment_sha256_required')

    base_path = Path(base)
    if base_path.is_symlink():
        raise ValueError('inspect_image_base_symlink')
    regular(base_path)
    base_resolved = base_path.resolve()

    cursor = base_path
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('inspect_image_symlink_refused')
        if not cursor.exists():
            raise ValueError(f'inspect_image_not_found:{cleaned}')
        regular(cursor)
    resolved = cursor.resolve()
    if not resolved.is_relative_to(base_resolved) or not resolved.is_file():
        raise ValueError('inspect_image_outside_root')

    suffix = resolved.suffix.lower()
    if require_sha256_path:
        expected_media = None
    else:
        expected_media = IMAGE_EXTENSIONS.get(suffix)
        if expected_media is None:
            raise ValueError('inspect_image_unsupported_extension')

    size = resolved.stat().st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise ValueError('inspect_image_size_out_of_bounds')
    with resolved.open('rb') as stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('inspect_image_size_out_of_bounds')
    detected = detect_image_media_type(raw)
    if detected is None or (expected_media is not None and detected != expected_media):
        raise ValueError('inspect_image_invalid_image_bytes')
    digest = hashlib.sha256(raw).hexdigest()
    if require_sha256_path and digest != parts[0]:
        raise ValueError('inspect_image_attachment_digest_mismatch')
    return cleaned, detected, raw, digest


def _load_records(custody: Path) -> list:
    from .native_agent import read_json
    path = regular(Path(custody) / INSPECTIONS_FILE)
    if not path.is_file():
        return []
    data = read_json(path, MAX_STORE_BYTES)
    if (not isinstance(data, dict) or data.get('schema') != 'tare.tools/vision-inspections/1'
            or not isinstance(data.get('inspections'), list)):
        raise ValueError('vision_inspections_store_invalid')
    return data['inspections']


def _save_records(custody: Path, records: list):
    save_json(
        regular(Path(custody) / INSPECTIONS_FILE),
        {'schema': 'tare.tools/vision-inspections/1', 'inspections': records},
        max_bytes=MAX_STORE_BYTES,
    )


def validate_image_inspection_config(value, *, role: str):
    """Validate optional `config['image_inspection']` in `native_agent.configuration()`."""
    if role not in ALLOWED_ROLES:
        raise ValueError('native_agent_image_inspection_role_not_allowed')
    if type(value) is bool:
        return value
    if not isinstance(value, dict) or set(value) - {'attachments_root'}:
        raise ValueError('native_agent_image_inspection_invalid')
    out = {}
    if 'attachments_root' in value and value['attachments_root'] is not None:
        raw = value['attachments_root']
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ValueError('native_agent_image_inspection_attachments_root_invalid')
        path = Path(raw)
        if path.is_symlink():
            raise ValueError('native_agent_image_inspection_attachments_root_symlink')
        regular(path)
        if not path.is_dir():
            raise ValueError('native_agent_image_inspection_attachments_root_missing')
        out['attachments_root'] = str(path.resolve())
    return out


class VisionTools:
    """Bounded read-only `inspect_image` tool wrapper for delegated implementers and auditors."""

    def __init__(self, tools, config, custody):
        role = (config or {}).get('role')
        if role not in ALLOWED_ROLES:
            raise ValueError('VisionTools is admitted only for delegated implementers and auditors.')
        self.tools = tools
        self.config = config
        self.custody = Path(custody)
        self.repositories = {name: Path(root) for name, root in (config.get('repositories') or {}).items()}
        setting = config.get('image_inspection')
        attachments_root = setting.get('attachments_root') if isinstance(setting, dict) else None
        self.attachments_root = Path(attachments_root) if attachments_root else None
        self.names = (*tools.names, 'inspect_image')
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **INSPECT_IMAGE_SPEC})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def _run_vision(self, raw: bytes, media_type: str, question: str):
        from .turn_engine import describe_files
        ext = EXT_BY_MEDIA[media_type]
        route_used = None
        succeeded = False

        def _on_vision(ev):
            nonlocal route_used, succeeded
            if ev.get('phase') == 'finish' and ev.get('ok'):
                succeeded = True
                route_used = str(ev.get('route') or 'vision')

        with tempfile.TemporaryDirectory(prefix='tare-inspect-image-') as folder:
            target = Path(folder) / f'image{ext}'
            target.write_bytes(raw)
            text = describe_files([target], lambda _p: None, question=question, on_vision=_on_vision)
        return succeeded, route_used, text.strip()

    def execute(self, request, control):
        from tare_tools_agent_runtime import EffectResult
        if request.tool_call.name != 'inspect_image':
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            args = object_json(request.tool_call.arguments_json)
            if len(encode(args).encode('utf-8')) > 8192:
                raise ValueError('inspect_image_arguments_over_budget')
            if not isinstance(args, dict) or set(args) != {'repository', 'path', 'question'}:
                raise ValueError('inspect_image_invalid_arguments')
            repo = args.get('repository')
            raw_path = args.get('path')
            question = args.get('question')
            if not isinstance(repo, str) or not repo.strip() or len(repo) > 128:
                raise ValueError('inspect_image_invalid_repository')
            if not isinstance(question, str) or not question.strip() or len(question.strip()) > 1500:
                raise ValueError('inspect_image_invalid_question')
            repo = repo.strip()
            question = question.strip()

            records = _load_records(self.custody)
            existing = next((r for r in records if isinstance(r, dict) and r.get('effect_id') == request.effect_id), None)
            if existing is not None:
                return EffectResult(request.effect_id, 'succeeded', encode(existing['output']))
            if len(records) >= MAX_IMAGE_INSPECTIONS:
                raise ValueError(f'inspect_image_limit_reached:max={MAX_IMAGE_INSPECTIONS}')

            if repo == 'attachments' and self.attachments_root is not None:
                norm_path, media_type, raw, digest = resolve_confined_image(
                    self.attachments_root, raw_path, require_sha256_path=True)
            elif repo in self.repositories:
                norm_path, media_type, raw, digest = resolve_confined_image(
                    self.repositories[repo], raw_path, require_sha256_path=False)
            else:
                admitted = sorted([*self.repositories, *(['attachments'] if self.attachments_root is not None else [])])
                raise ValueError(f'inspect_image_unknown_repository:{repo}:admitted={",".join(admitted)}')

            from .native_activity import safe as _safe_text
            from .turn_engine import clip
            succeeded, route_used, text = self._run_vision(raw, media_type, question)
            cleaned_text = _safe_text(text, len(text))
            observation = clip(cleaned_text, MAX_OBSERVATION_BYTES)
            while len(encode(observation).encode('utf-8')) > MAX_OBSERVATION_BYTES and observation:
                observation = observation[:max(0, len(observation) * 3 // 4)]
            output = {
                'status': 'OBSERVED' if succeeded else 'UNAVAILABLE',
                'authority': 'NONE',
                'basis': 'Untrusted read-only visual observation from host vision route; reconcile with source code and checks.',
                'repository': repo,
                'path': norm_path,
                'media_type': media_type,
                'sha256': digest,
                'bytes': len(raw),
                **({'route': route_used} if route_used else {}),
                'observation': observation,
                'truncated': observation != text,
                'remaining_inspections': max(0, MAX_IMAGE_INSPECTIONS - (len(records) + 1)),
            }
            records.append({
                'effect_id': request.effect_id,
                'repository': repo,
                'path': norm_path,
                'sha256': digest,
                'output': output,
            })
            _save_records(self.custody, records)
            return EffectResult(request.effect_id, 'succeeded', encode(output))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)[:300]}))

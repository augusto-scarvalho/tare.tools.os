"""Bounded, inert local skill bundle inspection, versioned snapshots, and reviewable diffs (PA-04 / SKL-02).

Captures an explicitly selected local skill directory as an immutable, hash-verified snapshot stored outside active
skill discovery paths (<storage>/serve/skills/snapshots/<bundle_id>/<snapshot_id>/). Reviewing or capturing a bundle
never loads it as active guidance, executes scripts, fetches remote dependencies, or alters any session.
"""
import argparse
import datetime
import difflib
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import tomllib
import uuid

from . import console_resources as _resources

SCHEMA = 'tare.tools/skill-bundle-snapshot/1'
DIFF_SCHEMA = 'tare.tools/skill-bundle-diff/1'

DEFAULT_MAX_FILES = 128
HARD_MAX_FILES = 512
DEFAULT_MAX_FILE_BYTES = 256 * 1024
HARD_MAX_FILE_BYTES = 1024 * 1024
MAX_SKILL_DOC_BYTES = 64 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024
HARD_MAX_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DEPTH = 4
HARD_MAX_DEPTH = 8
MAX_DIR_ENTRIES = 256
MAX_DIFF_BYTES = 64 * 1024
MAX_DIFF_LINES = 400

DEFAULT_EXCLUDED_DIRS = frozenset({
    '.git', 'node_modules', '__pycache__', '.venv', 'venv',
    '.pytest_cache', '.ruff_cache', '.mypy_cache', '.tox',
    'dist', 'build', 'out', 'output', 'private', 'target',
})

SCRIPT_EXTENSIONS = frozenset({'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd'})
SCRIPT_RUNTIMES = {
    '.py': 'python', '.js': 'node', '.mjs': 'node', '.cjs': 'node', '.ts': 'node', '.tsx': 'node',
    '.sh': 'sh', '.bash': 'bash', '.zsh': 'zsh', '.ps1': 'powershell', '.bat': 'cmd', '.cmd': 'cmd',
}
DOC_EXTENSIONS = frozenset({'.md', '.txt', '.rst'})
CONFIG_NAMES = frozenset({'package.json', 'pyproject.toml', 'requirements.txt', 'constraints.txt', 'tsconfig.json'})
CONFIG_EXTENSIONS = frozenset({'.json', '.toml', '.yaml', '.yml'})

BUNDLE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
SNAPSHOT_ID_RE = re.compile(r'^[0-9a-f]{16,64}$')
MD_LINK_RE = re.compile(r'(?<!!)\[[^\]]{0,200}\]\(([^)\r\n]{1,300})\)')
BACKTICK_PATH_RE = re.compile(r'`((?:\./|\.\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9]{1,12})`')
URL_SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')


def snapshots_root(storage_root=None) -> Path:
    """Return the inert snapshot root, kept outside active skill discovery paths."""
    if storage_root is None:
        from .native_installation import storage_root as _storage_root
        base = _storage_root()
    else:
        base = Path(storage_root)
    return base / 'serve' / 'skills' / 'snapshots'


def normalize_bundle_id(name: str) -> str:
    """Normalize a human skill or folder name into a canonical, path-safe bundle id."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Bundle id must be a non-empty string')
    slug = re.sub(r'[^a-z0-9._-]+', '-', name.strip().lower()).strip('._-')[:64]
    if not slug or not BUNDLE_ID_RE.match(slug) or '..' in slug:
        raise ValueError(f'Invalid bundle id: {name!r}')
    return slug


def _validate_bundle_id(bundle_id: str) -> str:
    if not isinstance(bundle_id, str) or not BUNDLE_ID_RE.match(bundle_id) or '..' in bundle_id:
        raise ValueError(f'Invalid bundle id: {bundle_id!r}')
    return bundle_id


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.match(snapshot_id):
        raise ValueError(f'Invalid snapshot id: {snapshot_id!r}')
    return snapshot_id


def _validate_rel_path(rel_path: str) -> tuple[str, ...]:
    if not isinstance(rel_path, str) or not rel_path or any(c in rel_path for c in ('\\', ':', '\x00')):
        raise ValueError(f'Invalid relative path: {rel_path!r}')
    parts = tuple(rel_path.split('/'))
    if any(p in ('', '.', '..') or p.rstrip(' .') != p for p in parts):
        raise ValueError(f'Invalid relative path: {rel_path!r}')
    if os.name == 'nt' and any(Path(p).is_reserved() for p in parts):
        raise ValueError(f'Reserved Windows path component: {rel_path!r}')
    return parts


def _validate_limits(*, max_files: int, max_file_bytes: int, max_total_bytes: int, max_depth: int) -> dict:
    for label, value, hard in (
        ('max_files', max_files, HARD_MAX_FILES),
        ('max_file_bytes', max_file_bytes, HARD_MAX_FILE_BYTES),
        ('max_total_bytes', max_total_bytes, HARD_MAX_TOTAL_BYTES),
        ('max_depth', max_depth, HARD_MAX_DEPTH),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > hard:
            raise ValueError(f'{label} must be an integer between 1 and {hard}')
    return {
        'max_files': max_files,
        'max_file_bytes': max_file_bytes,
        'max_total_bytes': max_total_bytes,
        'max_depth': max_depth,
        'max_skill_doc_bytes': MAX_SKILL_DOC_BYTES,
        'max_dir_entries': MAX_DIR_ENTRIES,
    }


def _normalize_include(include) -> tuple[str, ...] | None:
    if not include:
        return None
    normalized = []
    for raw in include:
        cleaned = str(raw).replace('\\', '/').strip().strip('/')
        if not cleaned:
            raise ValueError('Include filter entries must not be empty')
        parts = _validate_rel_path(cleaned)
        normalized.append('/'.join(parts))
    return tuple(sorted(set(normalized)))


def _is_link_or_reparse(path: Path) -> bool:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == 'nt' and getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return False


def _is_regular_or_dir(path: Path) -> str:
    """Inspect lstat without following links; reject symlinks, reparse points, and special files."""
    if _is_link_or_reparse(path):
        raise ValueError(f'Linked or reparse-point entry is refused inside skill bundle: {path.name}')
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        return 'dir'
    if stat.S_ISREG(st.st_mode):
        return 'file'
    raise ValueError(f'Special non-regular file is refused inside skill bundle: {path.name}')


def _classify_file(rel_path: str, utf8: bool) -> str:
    if rel_path == 'SKILL.md':
        return 'skill_doc'
    if not utf8:
        return 'opaque'
    name = rel_path.rsplit('/', 1)[-1]
    lower_name = name.lower()
    suffix = Path(lower_name).suffix
    parts = rel_path.split('/')
    if suffix in SCRIPT_EXTENSIONS or parts[0] in {'scripts', 'bin'}:
        return 'script'
    if lower_name in CONFIG_NAMES or suffix in CONFIG_EXTENSIONS:
        return 'manifest_or_config'
    if suffix in DOC_EXTENSIONS or parts[0] in {'references', 'docs', 'examples'}:
        return 'reference'
    return 'text'


def _read_pinned_bytes(path: Path, limit_bytes: int) -> tuple[bytes | None, int]:
    """Open file with pinned non-following descriptor; return (bytes or None if oversized, exact_or_lower_bound_size)."""
    with _resources._pinned(path, directory=False) as (fd, _):
        st_size = os.fstat(fd).st_size
        if st_size > limit_bytes:
            return None, st_size
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(limit_bytes + 1)
        if len(data) > limit_bytes:
            return None, len(data)
        return data, len(data)


def _compute_bundle_sha256(
    file_entries: list[dict],
    *,
    complete_directory: bool,
    truncated: bool,
    include: list[str] | None,
    skipped: list[dict],
    excluded_dirs: list[str],
    outside_filter: list[str],
) -> str:
    canonical = {
        'files': [
            {'path': item['path'], 'sha256': item['sha256'], 'size_bytes': item['size_bytes']}
            for item in sorted(file_entries, key=lambda x: x['path'])
        ],
        'complete_directory': bool(complete_directory),
        'truncated': bool(truncated),
        'include': sorted(include) if include else None,
        'skipped': [
            {'path': s['path'], 'kind': s.get('kind'), 'size_bytes': s.get('size_bytes'), 'reason': s.get('reason')}
            for s in sorted(skipped, key=lambda x: x['path'])
        ],
        'excluded_dirs': sorted(excluded_dirs),
        'outside_filter': sorted(outside_filter),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _normalize_target_ref(source_file: str, raw_target: str) -> tuple[str, str | None]:
    """Return (kind, normalized_rel_or_raw): kind in {'anchor', 'remote_url', 'outside', 'relative'}."""
    target = raw_target.strip()
    if target.startswith('<') and target.endswith('>'):
        target = target[1:-1].strip()
    # Strip optional markdown link title: path "title"
    if ' ' in target and not target.startswith(('http://', 'https://')):
        target = target.split(' ', 1)[0].strip()
    if not target or target.startswith('#'):
        return 'anchor', None
    # Strip fragment or query string for path lookup
    clean = target.split('#', 1)[0].split('?', 1)[0].strip()
    if not clean:
        return 'anchor', None
    if URL_SCHEME_RE.match(clean):
        # Single-letter Windows drive like C:\foo or C:/foo is an absolute local path, not a URL scheme.
        if len(clean) >= 2 and clean[1] == ':' and clean[0].isalpha():
            return 'outside', clean
        return 'remote_url', target
    if clean.startswith(('/', '\\')) or ':' in clean or '\x00' in clean:
        return 'outside', clean
    posix = clean.replace('\\', '/')
    # Resolve relative to source_file's directory without touching the filesystem.
    source_dir = source_file.rsplit('/', 1)[0] if '/' in source_file else ''
    base_parts = source_dir.split('/') if source_dir else []
    out_parts = list(base_parts)
    for part in posix.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not out_parts:
                return 'outside', posix
            out_parts.pop()
        else:
            out_parts.append(part)
    if not out_parts:
        return 'outside', posix
    return 'relative', '/'.join(out_parts)


def _extract_references(
    captured_texts: dict[str, str],
    captured_map: dict[str, dict],
    skipped_map: dict[str, dict],
    excluded_dirs: list[str],
    outside_filter: list[str],
) -> list[dict]:
    """Statically inspect markdown references and classify each as resolved, missing, outside_scope, or not_inspected."""
    seen: set[tuple[str, str]] = set()
    refs: list[dict] = []
    excluded_set = set(excluded_dirs)
    outside_filter_set = set(outside_filter)
    skipped_dir_map = {p: s['reason'] for p, s in skipped_map.items() if s.get('kind') == 'dir'}

    def _under_any(rel_path: str, prefixes: set[str]) -> str | None:
        for pref in prefixes:
            if rel_path == pref or rel_path.startswith(pref + '/'):
                return pref
        return None

    for source_path in sorted(captured_texts):
        if source_path != 'SKILL.md' and not source_path.lower().endswith('.md'):
            continue
        text = captured_texts[source_path]
        candidates: list[str] = []
        for match in MD_LINK_RE.finditer(text):
            candidates.append(match.group(1).strip())
        for match in BACKTICK_PATH_RE.finditer(text):
            candidates.append(match.group(1).strip())
        for raw in candidates:
            kind, norm = _normalize_target_ref(source_path, raw)
            if kind == 'anchor' or norm is None:
                continue
            key = (source_path, norm)
            if key in seen:
                continue
            seen.add(key)
            if kind == 'remote_url':
                refs.append({'source_file': source_path, 'target': raw, 'normalized': norm,
                             'status': 'outside_scope', 'reason': 'remote_url'})
                continue
            if kind == 'outside':
                refs.append({'source_file': source_path, 'target': raw, 'normalized': norm,
                             'status': 'outside_scope', 'reason': 'path_outside_bundle'})
                continue
            # Also check bundle-root interpretation if source_path is nested and relative resolution missed.
            resolved_path = norm
            if resolved_path not in captured_map and '/' in source_path:
                root_try = raw.strip().lstrip('./').replace('\\', '/').split('#', 1)[0].split('?', 1)[0]
                if root_try in captured_map:
                    resolved_path = root_try
            if resolved_path in captured_map:
                entry = captured_map[resolved_path]
                if entry['kind'] == 'opaque' or not entry['utf8']:
                    refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                                 'status': 'not_inspected', 'reason': 'opaque_captured_file'})
                else:
                    refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                                 'status': 'resolved', 'reason': 'captured_file'})
            elif resolved_path in skipped_map:
                refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                             'status': 'not_inspected', 'reason': skipped_map[resolved_path]['reason']})
            elif (skipped_dir := _under_any(resolved_path, set(skipped_dir_map))) is not None:
                refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                             'status': 'not_inspected', 'reason': skipped_dir_map[skipped_dir]})
            elif _under_any(resolved_path, excluded_set):
                refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                             'status': 'outside_scope', 'reason': 'excluded_dir'})
            elif _under_any(resolved_path, outside_filter_set):
                refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                             'status': 'outside_scope', 'reason': 'outside_include_filter'})
            elif any(p.startswith(resolved_path + '/') for p in captured_map):
                refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                             'status': 'resolved', 'reason': 'captured_directory'})
            else:
                refs.append({'source_file': source_path, 'target': raw, 'normalized': resolved_path,
                             'status': 'missing', 'reason': 'not_found_in_bundle'})
    return refs


def _extract_external_bindings(captured_files: list[dict], captured_texts: dict[str, str]) -> list[dict]:
    """Identify declared runtime/package bindings passively without executing or authorizing anything."""
    bindings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, name: str, source_file: str, detail: str = ''):
        key = (kind, name, source_file)
        if key in seen:
            return
        seen.add(key)
        entry = {'kind': kind, 'name': name, 'source_file': source_file,
                 'status': 'external_binding', 'authorized': False, 'executed': False}
        if detail:
            entry['detail'] = detail
        bindings.append(entry)

    for item in captured_files:
        rel = item['path']
        if item['kind'] == 'script':
            suffix = Path(rel.lower()).suffix
            runtime = SCRIPT_RUNTIMES.get(suffix, 'script')
            add('script_runtime', runtime, rel, 'inert captured script; not executed or authorized')

    pkg_text = captured_texts.get('package.json')
    if pkg_text is not None:
        try:
            pkg = json.loads(pkg_text)
        except ValueError:
            pkg = None
        if isinstance(pkg, dict):
            engines = pkg.get('engines')
            if isinstance(engines, dict):
                for eng, spec in sorted(engines.items()):
                    add('runtime_engine', str(eng), 'package.json', str(spec))
            deps = pkg.get('dependencies')
            if isinstance(deps, dict):
                for dep, spec in sorted(deps.items()):
                    add('npm_dependency', str(dep), 'package.json', str(spec))
            bins = pkg.get('bin')
            if isinstance(bins, dict):
                for bname, bpath in sorted(bins.items()):
                    add('cli_entrypoint', str(bname), 'package.json', str(bpath))
            elif isinstance(bins, str) and bins.strip():
                add('cli_entrypoint', str(pkg.get('name') or 'bin'), 'package.json', bins.strip())

    pyproj_text = captured_texts.get('pyproject.toml')
    if pyproj_text is not None:
        try:
            pyproj = tomllib.loads(pyproj_text)
        except ValueError:
            pyproj = None
        if isinstance(pyproj, dict):
            proj = pyproj.get('project')
            if isinstance(proj, dict):
                req_py = proj.get('requires-python')
                if isinstance(req_py, str) and req_py.strip():
                    add('runtime_engine', 'python', 'pyproject.toml', req_py.strip())
                deps = proj.get('dependencies')
                if isinstance(deps, list):
                    for dep in deps[:64]:
                        if isinstance(dep, str) and dep.strip():
                            dep_name = re.split(r'[<>=!~;\[\s]', dep.strip(), maxsplit=1)[0]
                            add('python_dependency', dep_name or dep.strip(), 'pyproject.toml', dep.strip())

    return sorted(bindings, key=lambda b: (b['kind'], b['name'], b['source_file']))


def _include_allows_dir(rel_dir: str, include_filter: tuple[str, ...] | None) -> bool:
    if include_filter is None:
        return True
    for inc in include_filter:
        if rel_dir == inc or rel_dir.startswith(inc + '/') or inc.startswith(rel_dir + '/'):
            return True
    return False


def _include_allows_file(rel_file: str, include_filter: tuple[str, ...] | None) -> bool:
    if include_filter is None or rel_file == 'SKILL.md':
        return True
    for inc in include_filter:
        if rel_file == inc or rel_file.startswith(inc + '/'):
            return True
    return False


def _scan_bundle(
    root: Path,
    *,
    name: str | None = None,
    include=None,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    excluded_dirs=DEFAULT_EXCLUDED_DIRS,
) -> tuple[dict, dict[str, bytes]]:
    """Bounded, pinned, read-only scan of a local skill directory. Never executes code or writes anything."""
    limits = _validate_limits(
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_depth=max_depth,
    )
    include_filter = _normalize_include(include)
    excluded_set = frozenset(excluded_dirs)

    raw_root = Path(root)
    if '..' in raw_root.parts:
        raise ValueError('Parent traversal is not allowed in skill bundle root')
    abs_root = raw_root.absolute()
    if abs_root == Path(abs_root.anchor):
        raise ValueError('Choose a skill bundle directory, not a filesystem root')

    # Pin the root directory and every ancestor before reading anything.
    try:
        with _resources._pinned(abs_root, directory=True):
            pass
    except (OSError, ValueError) as exc:
        raise ValueError(f'Skill bundle root is missing, linked, or invalid: {exc}') from exc

    # Read and validate mandatory SKILL.md first.
    skill_md_path = abs_root / 'SKILL.md'
    try:
        if _is_regular_or_dir(skill_md_path) != 'file':
            raise ValueError('SKILL.md must be a regular file')
        skill_doc_limit = min(MAX_SKILL_DOC_BYTES, max_file_bytes)
        skill_bytes, skill_size = _read_pinned_bytes(skill_md_path, skill_doc_limit)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Bundle requires a regular UTF-8 SKILL.md at root: {exc}') from exc
    if skill_bytes is None:
        raise ValueError(f'SKILL.md exceeds {skill_doc_limit} bytes ({skill_size} bytes)')
    try:
        skill_text = skill_bytes.decode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise ValueError('SKILL.md is not valid UTF-8') from exc
    if len(skill_bytes) > max_total_bytes:
        raise ValueError(f'SKILL.md exceeds max_total_bytes ({max_total_bytes} bytes)')

    skill_name, skill_description = _resources._metadata(skill_text, abs_root.name)
    bundle_id = normalize_bundle_id(name if name is not None else skill_name)

    captured_files: list[dict] = []
    captured_bytes: dict[str, bytes] = {}
    captured_texts: dict[str, str] = {}
    skipped: list[dict] = []
    excluded_found: list[str] = []
    outside_filter_found: list[str] = []
    coverage_warnings: list[str] = []
    truncated = False

    def record_captured(rel_path: str, data: bytes):
        digest = hashlib.sha256(data).hexdigest()
        try:
            text = data.decode('utf-8', errors='strict')
            utf8 = True
        except UnicodeError:
            text = None
            utf8 = False
        kind = _classify_file(rel_path, utf8)
        entry = {
            'path': rel_path,
            'sha256': digest,
            'size_bytes': len(data),
            'kind': kind,
            'utf8': utf8,
            'inert_data': True,
            'inspected': utf8,
        }
        captured_files.append(entry)
        captured_bytes[rel_path] = data
        if text is not None:
            captured_texts[rel_path] = text

    record_captured('SKILL.md', skill_bytes)
    total_bytes = len(skill_bytes)

    def walk(dir_path: Path, rel_prefix: str, depth: int):
        nonlocal total_bytes, truncated
        try:
            with _resources._pinned(dir_path, directory=True) as (fd, pinned_path):
                with os.scandir(pinned_path if os.name == 'nt' else fd) as stream:
                    raw_entries = list(islice(stream, MAX_DIR_ENTRIES + 1))
        except (OSError, ValueError) as exc:
            raise ValueError(f'Cannot read directory {rel_prefix or "."}: {exc}') from exc

        if len(raw_entries) > MAX_DIR_ENTRIES:
            truncated = True
            raw_entries = raw_entries[:MAX_DIR_ENTRIES]
            coverage_warnings.append(
                f'Directory {rel_prefix or "."} exceeded {MAX_DIR_ENTRIES} entries; remaining entries were not inspected.'
            )

        names = sorted(e.name for e in raw_entries)
        for entry_name in names:
            rel_path = f'{rel_prefix}/{entry_name}' if rel_prefix else entry_name
            if rel_path == 'SKILL.md':
                continue
            _validate_rel_path(rel_path)
            full_path = dir_path / entry_name
            entry_kind = _is_regular_or_dir(full_path)

            if entry_kind == 'dir':
                if entry_name in excluded_set:
                    excluded_found.append(rel_path)
                    continue
                if not _include_allows_dir(rel_path, include_filter):
                    outside_filter_found.append(rel_path)
                    continue
                if depth >= max_depth:
                    truncated = True
                    skipped.append({'path': rel_path, 'kind': 'dir', 'size_bytes': None, 'reason': 'exceeds_depth'})
                    continue
                walk(full_path, rel_path, depth + 1)
                continue

            # Regular file
            if not _include_allows_file(rel_path, include_filter):
                outside_filter_found.append(rel_path)
                continue

            if len(captured_files) >= max_files:
                truncated = True
                st_size = os.lstat(full_path).st_size
                skipped.append({'path': rel_path, 'kind': 'file', 'size_bytes': st_size, 'reason': 'exceeds_file_count'})
                continue

            data, observed_size = _read_pinned_bytes(full_path, max_file_bytes)
            if data is None:
                skipped.append({'path': rel_path, 'kind': 'file', 'size_bytes': observed_size, 'reason': 'exceeds_file_bytes'})
                continue
            if total_bytes + len(data) > max_total_bytes:
                truncated = True
                skipped.append({'path': rel_path, 'kind': 'file', 'size_bytes': len(data), 'reason': 'exceeds_total_bytes'})
                continue

            total_bytes += len(data)
            record_captured(rel_path, data)

    walk(abs_root, '', 0)

    captured_files.sort(key=lambda x: x['path'])
    skipped.sort(key=lambda x: x['path'])
    excluded_found.sort()
    outside_filter_found.sort()

    # Check if any explicit --include target was missing.
    if include_filter:
        captured_paths = {f['path'] for f in captured_files}
        skipped_paths = {s['path'] for s in skipped}
        for inc in include_filter:
            matched = (
                inc in captured_paths
                or inc in skipped_paths
                or any(p.startswith(inc + '/') for p in captured_paths)
                or any(p.startswith(inc + '/') for p in skipped_paths)
            )
            if not matched:
                coverage_warnings.append(f'Declared include path was not found in bundle: {inc}')

    captured_map = {f['path']: f for f in captured_files}
    skipped_map = {s['path']: s for s in skipped}
    references = _extract_references(captured_texts, captured_map, skipped_map, excluded_found, outside_filter_found)
    external_bindings = _extract_external_bindings(captured_files, captured_texts)

    opaque_files = [f['path'] for f in captured_files if not f['utf8']]
    if opaque_files:
        coverage_warnings.append(
            f'{len(opaque_files)} captured file(s) are opaque/binary and were not semantically inspected.'
        )
    if excluded_found:
        coverage_warnings.append(
            f'{len(excluded_found)} excluded directory(ies) remained outside captured scope: '
            + ', '.join(excluded_found[:8]) + ('...' if len(excluded_found) > 8 else '')
        )
    if outside_filter_found:
        coverage_warnings.append(
            f'{len(outside_filter_found)} path(s) remained outside the declared include filter.'
        )

    complete_directory = not (excluded_found or skipped or truncated or include_filter or outside_filter_found)
    bundle_sha256 = _compute_bundle_sha256(
        captured_files,
        complete_directory=complete_directory,
        truncated=truncated,
        include=list(include_filter) if include_filter else None,
        skipped=skipped,
        excluded_dirs=excluded_found,
        outside_filter=outside_filter_found,
    )
    snapshot_id = bundle_sha256[:16]

    manifest = {
        'schema': SCHEMA,
        'authority': 'NONE',
        'active_in_session': False,
        'effects_authorized': False,
        'bundle_id': bundle_id,
        'snapshot_id': snapshot_id,
        'bundle_sha256': bundle_sha256,
        'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds'),
        'captured_ns': time.time_ns(),
        'source': {
            'kind': 'local_directory',
            'origin_path': str(abs_root),
            'skill_name': skill_name,
            'description': skill_description,
            'skill_md_sha256': captured_map['SKILL.md']['sha256'],
        },
        'scope': {
            'kind': 'full_directory' if complete_directory else 'subset',
            'complete_directory': complete_directory,
            'include': list(include_filter) if include_filter else None,
            'limits': limits,
        },
        'summary': {
            'file_count': len(captured_files),
            'total_bytes': total_bytes,
            'opaque_count': len(opaque_files),
            'script_count': sum(1 for f in captured_files if f['kind'] == 'script'),
            'skipped_count': len(skipped),
            'excluded_dir_count': len(excluded_found),
            'reference_counts': {
                status: sum(1 for r in references if r['status'] == status)
                for status in ('resolved', 'missing', 'outside_scope', 'not_inspected')
            },
            'external_binding_count': len(external_bindings),
            'truncated': truncated,
        },
        'files': captured_files,
        'skipped': skipped,
        'excluded_dirs': excluded_found,
        'outside_filter': outside_filter_found,
        'references': references,
        'external_bindings': external_bindings,
        'coverage_warnings': coverage_warnings,
    }
    return manifest, captured_bytes


def inspect_bundle(
    root: Path,
    *,
    name: str | None = None,
    include=None,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    excluded_dirs=DEFAULT_EXCLUDED_DIRS,
) -> dict:
    """Inert read-only preview of a local skill bundle. Writes nothing and executes nothing."""
    manifest, _ = _scan_bundle(
        root,
        name=name,
        include=include,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_depth=max_depth,
        excluded_dirs=excluded_dirs,
    )
    manifest['stored'] = False
    return manifest


def _snapshot_dir(bundle_id: str, snapshot_id: str, *, storage_root=None) -> Path:
    bid = _validate_bundle_id(bundle_id)
    sid = _validate_snapshot_id(snapshot_id)
    return snapshots_root(storage_root) / bid / sid


def _ensure_confined_dir(path: Path) -> Path:
    """Create directories component by component without traversing symlinks or Windows reparse points."""
    raw = Path(path)
    if '..' in raw.parts:
        raise ValueError('Parent traversal is not allowed in snapshot storage path')
    abs_path = raw.absolute()
    if abs_path.anchor.startswith('\\\\') or abs_path.anchor.startswith('//'):
        raise ValueError('Network roots are not allowed for snapshot storage')
    current = Path(abs_path.anchor)
    for part in abs_path.parts[1:]:
        current = current / part
        if current.exists() or os.path.lexists(current):
            if _is_link_or_reparse(current):
                raise ValueError(f'Linked or reparse-point storage component is refused: {current}')
            if not current.is_dir():
                raise ValueError(f'Non-directory storage component is refused: {current}')
        else:
            current.mkdir(exist_ok=False)
    with _resources._pinned(abs_path, directory=True):
        pass
    return abs_path


def _read_verified_stored_file(files_root: Path, entry: dict, *, max_bytes: int = HARD_MAX_FILE_BYTES) -> bytes:
    """Read a stored snapshot file through pinned handles and verify its exact size and SHA-256."""
    rel = entry['path']
    parts = _validate_rel_path(rel)
    target = files_root.joinpath(*parts)
    data, _ = _read_pinned_bytes(target, max_bytes)
    if data is None:
        raise ValueError(f'Stored snapshot file exceeds limit: {rel}')
    if len(data) != entry['size_bytes']:
        raise ValueError(f'Stored snapshot size mismatch for {rel}')
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError(f'Stored snapshot hash mismatch for {rel}')
    return data


def verify_snapshot_dir(directory: Path) -> dict:
    """Verify a stored snapshot's manifest, directory identity binding, and exact stored file bytes; fail closed."""
    abs_dir = Path(directory).absolute()
    with _resources._pinned(abs_dir, directory=True):
        pass
    dir_snapshot_id = _validate_snapshot_id(abs_dir.name)
    dir_bundle_id = _validate_bundle_id(abs_dir.parent.name)
    manifest_path = abs_dir / 'manifest.json'
    manifest_bytes, _ = _read_pinned_bytes(manifest_path, 4 * 1024 * 1024)
    if manifest_bytes is None:
        raise ValueError('Snapshot manifest exceeds size limit')
    try:
        manifest = json.loads(manifest_bytes.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f'Corrupted snapshot manifest: {exc}') from exc
    if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
        raise ValueError(f'Unsupported snapshot schema in {manifest_path}')

    files_list = manifest.get('files')
    if not isinstance(files_list, list) or not files_list:
        raise ValueError('Snapshot manifest has no files')

    scope = manifest.get('scope')
    if not isinstance(scope, dict) or not isinstance(scope.get('complete_directory'), bool):
        raise ValueError('Snapshot manifest has invalid scope metadata')
    skipped_list = manifest.get('skipped', [])
    excluded_list = manifest.get('excluded_dirs', [])
    outside_list = manifest.get('outside_filter', [])
    if not all(isinstance(v, list) for v in (skipped_list, excluded_list, outside_list)):
        raise ValueError('Snapshot manifest has invalid coverage lists')

    summary = manifest.get('summary')
    if not isinstance(summary, dict) or not isinstance(summary.get('truncated'), bool):
        raise ValueError('Snapshot manifest has invalid summary metadata')
    expected_sha = _compute_bundle_sha256(
        files_list,
        complete_directory=scope['complete_directory'],
        truncated=summary['truncated'],
        include=scope.get('include'),
        skipped=skipped_list,
        excluded_dirs=excluded_list,
        outside_filter=outside_list,
    )
    if manifest.get('bundle_sha256') != expected_sha:
        raise ValueError('Snapshot bundle_sha256 does not match manifest files and coverage')
    if manifest.get('bundle_id') != dir_bundle_id:
        raise ValueError('Snapshot bundle_id does not match storage directory')
    if manifest.get('snapshot_id') != dir_snapshot_id or not expected_sha.startswith(dir_snapshot_id):
        raise ValueError('Snapshot directory identity does not match bundle_sha256')

    files_root = abs_dir / 'files'
    with _resources._pinned(files_root, directory=True):
        pass

    expected_paths: set[str] = set()
    for entry in files_list:
        rel = entry['path']
        _validate_rel_path(rel)
        expected_paths.add(rel)
        _read_verified_stored_file(files_root, entry)

    # Ensure no unexpected extra files or links exist inside files/.
    actual_paths: set[str] = set()

    def scan_stored(cur: Path, prefix: str):
        with _resources._pinned(cur, directory=True) as (fd, pinned):
            with os.scandir(pinned if os.name == 'nt' else fd) as stream:
                entries = list(stream)
        for item in entries:
            rel = f'{prefix}/{item.name}' if prefix else item.name
            full = cur / item.name
            kind = _is_regular_or_dir(full)
            if kind == 'dir':
                scan_stored(full, rel)
            else:
                actual_paths.add(rel)

    scan_stored(files_root, '')
    if actual_paths != expected_paths:
        raise ValueError('Stored snapshot file tree does not match manifest')
    return manifest


def capture_bundle(
    root: Path,
    *,
    name: str | None = None,
    include=None,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    excluded_dirs=DEFAULT_EXCLUDED_DIRS,
    storage_root=None,
) -> dict:
    """Capture a local skill directory into an immutable, inert snapshot stored outside active discovery."""
    manifest, captured_bytes = _scan_bundle(
        root,
        name=name,
        include=include,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_depth=max_depth,
        excluded_dirs=excluded_dirs,
    )
    bundle_id = manifest['bundle_id']
    bundle_sha = manifest['bundle_sha256']
    bundle_dir = _ensure_confined_dir(snapshots_root(storage_root) / bundle_id)

    # Guarantee strictly increasing capture ordering within a bundle even under coarse OS clocks.
    max_existing_ns = 0
    for existing_dir in bundle_dir.iterdir():
        if _is_link_or_reparse(existing_dir):
            continue
        if not existing_dir.is_dir() or not SNAPSHOT_ID_RE.match(existing_dir.name):
            continue
        mpath = existing_dir / 'manifest.json'
        if mpath.is_file() and not _is_link_or_reparse(mpath):
            try:
                old_meta = json.loads(mpath.read_text(encoding='utf-8'))
                if isinstance(old_meta.get('captured_ns'), int):
                    max_existing_ns = max(max_existing_ns, old_meta['captured_ns'])
            except (OSError, ValueError):
                pass
    if manifest['captured_ns'] <= max_existing_ns:
        manifest['captured_ns'] = max_existing_ns + 1

    snapshot_id = manifest['snapshot_id']
    target_dir = bundle_dir / snapshot_id
    if target_dir.exists() or os.path.lexists(target_dir):
        existing = verify_snapshot_dir(target_dir)
        if existing['bundle_sha256'] == bundle_sha:
            return {**existing, 'stored': True, 'already_captured': True, 'snapshot_dir': str(target_dir)}
        # Extremely rare 64-bit prefix collision: fall back to full 64-hex digest.
        snapshot_id = bundle_sha
        manifest['snapshot_id'] = snapshot_id
        target_dir = bundle_dir / snapshot_id
        if target_dir.exists() or os.path.lexists(target_dir):
            existing = verify_snapshot_dir(target_dir)
            if existing['bundle_sha256'] == bundle_sha:
                return {**existing, 'stored': True, 'already_captured': True, 'snapshot_dir': str(target_dir)}

    tmp_dir = bundle_dir / f'.tmp-{uuid.uuid4().hex[:12]}'
    try:
        files_dir = _ensure_confined_dir(tmp_dir / 'files')
        for rel_path, data in captured_bytes.items():
            parts = _validate_rel_path(rel_path)
            dest = files_dir.joinpath(*parts)
            _ensure_confined_dir(dest.parent)
            dest.write_bytes(data)
        (tmp_dir / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        try:
            os.replace(tmp_dir, target_dir)
        except OSError:
            if target_dir.exists():
                existing = verify_snapshot_dir(target_dir)
                if existing['bundle_sha256'] == bundle_sha:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return {**existing, 'stored': True, 'already_captured': True, 'snapshot_dir': str(target_dir)}
            raise
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    verified = verify_snapshot_dir(target_dir)
    return {**verified, 'stored': True, 'already_captured': False, 'snapshot_dir': str(target_dir)}


def list_snapshots(*, bundle_id: str | None = None, storage_root=None) -> list[dict]:
    """List captured inert skill snapshots sorted by bundle_id then captured_at."""
    root = snapshots_root(storage_root)
    if not root.is_dir():
        return []
    bundle_dirs = []
    if bundle_id is not None:
        bid = _validate_bundle_id(bundle_id)
        cand = root / bid
        if cand.is_dir():
            bundle_dirs.append(cand)
    else:
        for child in sorted(root.iterdir()):
            if child.is_dir() and BUNDLE_ID_RE.match(child.name):
                bundle_dirs.append(child)

    results: list[dict] = []
    for bdir in bundle_dirs:
        for sdir in sorted(bdir.iterdir()):
            if not sdir.is_dir() or not SNAPSHOT_ID_RE.match(sdir.name):
                continue
            manifest_path = sdir / 'manifest.json'
            if not manifest_path.is_file():
                continue
            try:
                manifest = verify_snapshot_dir(sdir)
            except (OSError, ValueError):
                continue
            results.append({
                'bundle_id': manifest['bundle_id'],
                'snapshot_id': manifest['snapshot_id'],
                'bundle_sha256': manifest['bundle_sha256'],
                'captured_at': manifest['captured_at'],
                'captured_ns': manifest.get('captured_ns', 0),
                'skill_name': manifest['source']['skill_name'],
                'description': manifest['source']['description'],
                'origin_path': manifest['source']['origin_path'],
                'complete_directory': manifest['scope']['complete_directory'],
                'file_count': manifest['summary']['file_count'],
                'total_bytes': manifest['summary']['total_bytes'],
                'snapshot_dir': str(sdir),
            })
    results.sort(key=lambda item: (item['bundle_id'], item['captured_ns'], item['captured_at'], item['snapshot_id']))
    return results


def resolve_snapshot_id(bundle_id: str, ref: str = 'latest', *, storage_root=None) -> str:
    """Resolve 'latest', 'previous', an exact snapshot_id, or a unique hex prefix (>= 6 chars)."""
    bid = _validate_bundle_id(bundle_id)
    bdir = snapshots_root(storage_root) / bid
    if ref in {'latest', 'previous'}:
        items = list_snapshots(bundle_id=bid, storage_root=storage_root)
        if not items:
            raise ValueError(f'No captured snapshots found for bundle {bid!r}')
        if ref == 'latest':
            return items[-1]['snapshot_id']
        if len(items) < 2:
            raise ValueError(f'Bundle {bid!r} has only one snapshot; no previous version to compare')
        return items[-2]['snapshot_id']
    if not isinstance(ref, str) or len(ref) < 6 or not re.match(r'^[0-9a-f]+$', ref):
        raise ValueError(f'Invalid snapshot reference: {ref!r}')
    if not bdir.is_dir():
        raise ValueError(f'No captured snapshots found for bundle {bid!r}')
    # Direct exact/prefix lookup on directory ids ensures tampered snapshots surface verification errors.
    dir_ids = sorted(child.name for child in bdir.iterdir() if child.is_dir() and SNAPSHOT_ID_RE.match(child.name))
    if ref in dir_ids:
        return ref
    matches = [sid for sid in dir_ids if sid.startswith(ref)]
    if not matches:
        # Also allow prefix match against full 64-hex bundle_sha256 in valid snapshots.
        items = list_snapshots(bundle_id=bid, storage_root=storage_root)
        matches = [it['snapshot_id'] for it in items if it['bundle_sha256'].startswith(ref)]
    if not matches:
        raise ValueError(f'Snapshot {ref!r} not found for bundle {bid!r}')
    if len(matches) > 1:
        raise ValueError(f'Snapshot prefix {ref!r} is ambiguous for bundle {bid!r}: {", ".join(matches)}')
    return matches[0]


def load_snapshot(bundle_id: str, snapshot_ref: str = 'latest', *, storage_root=None, verify: bool = True) -> dict:
    """Load a captured snapshot strictly from inert storage (never rereading origin_path)."""
    sid = resolve_snapshot_id(bundle_id, snapshot_ref, storage_root=storage_root)
    sdir = _snapshot_dir(bundle_id, sid, storage_root=storage_root)
    if verify:
        manifest = verify_snapshot_dir(sdir)
    else:
        manifest = json.loads((sdir / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('schema') != SCHEMA:
            raise ValueError(f'Unsupported snapshot schema in {sdir}')
    return {**manifest, 'stored': True, 'snapshot_dir': str(sdir)}


def read_snapshot_file(bundle_id: str, snapshot_ref: str, rel_path: str, *, storage_root=None) -> tuple[bytes, dict]:
    """Read exact verified bytes of one captured file from inert storage."""
    parts = _validate_rel_path(rel_path)
    manifest = load_snapshot(bundle_id, snapshot_ref, storage_root=storage_root, verify=True)
    entry = next((f for f in manifest['files'] if f['path'] == rel_path), None)
    if entry is None:
        raise ValueError(f'File {rel_path!r} is not in snapshot {manifest["bundle_id"]}@{manifest["snapshot_id"]}')
    target = Path(manifest['snapshot_dir']) / 'files'
    target = target.joinpath(*parts)
    data, _ = _read_pinned_bytes(target, HARD_MAX_FILE_BYTES)
    if data is None or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError(f'Stored bytes failed verification for {rel_path!r}')
    return data, entry


def _bounded_unified_diff(old_text: str, new_text: str, rel_path: str, old_label: str, new_label: str) -> tuple[str, bool]:
    lines = list(difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f'{old_label}/{rel_path}',
        tofile=f'{new_label}/{rel_path}',
        lineterm='',
    ))
    truncated = False
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES]
        truncated = True
    text = '\n'.join(lines)
    encoded = text.encode('utf-8')
    if len(encoded) > MAX_DIFF_BYTES:
        text = encoded[:MAX_DIFF_BYTES].decode('utf-8', errors='ignore')
        truncated = True
    return text, truncated


def diff_snapshots(
    bundle_id: str,
    old_ref: str = 'previous',
    new_ref: str = 'latest',
    *,
    storage_root=None,
) -> dict:
    """Produce a reviewable, bounded change report between two verified captured snapshots."""
    old_manifest = load_snapshot(bundle_id, old_ref, storage_root=storage_root, verify=True)
    new_manifest = load_snapshot(bundle_id, new_ref, storage_root=storage_root, verify=True)
    old_files = {f['path']: f for f in old_manifest['files']}
    new_files = {f['path']: f for f in new_manifest['files']}
    old_root = Path(old_manifest['snapshot_dir']) / 'files'
    new_root = Path(new_manifest['snapshot_dir']) / 'files'

    added: list[dict] = []
    removed: list[dict] = []
    modified: list[dict] = []
    unchanged_count = 0

    for rel in sorted(set(old_files) | set(new_files)):
        old_f = old_files.get(rel)
        new_f = new_files.get(rel)
        if old_f is None:
            item = {'path': rel, 'new_sha256': new_f['sha256'], 'new_size_bytes': new_f['size_bytes'],
                    'kind': new_f['kind'], 'binary': not new_f['utf8']}
            if new_f['utf8'] and new_f['size_bytes'] <= MAX_DIFF_BYTES:
                new_text = _read_verified_stored_file(new_root, new_f, max_bytes=MAX_DIFF_BYTES).decode('utf-8')
                diff_text, diff_trunc = _bounded_unified_diff('', new_text, rel, old_manifest['snapshot_id'], new_manifest['snapshot_id'])
                item['unified_diff'] = diff_text
                item['diff_truncated'] = diff_trunc
            added.append(item)
        elif new_f is None:
            item = {'path': rel, 'old_sha256': old_f['sha256'], 'old_size_bytes': old_f['size_bytes'],
                    'kind': old_f['kind'], 'binary': not old_f['utf8']}
            if old_f['utf8'] and old_f['size_bytes'] <= MAX_DIFF_BYTES:
                old_text = _read_verified_stored_file(old_root, old_f, max_bytes=MAX_DIFF_BYTES).decode('utf-8')
                diff_text, diff_trunc = _bounded_unified_diff(old_text, '', rel, old_manifest['snapshot_id'], new_manifest['snapshot_id'])
                item['unified_diff'] = diff_text
                item['diff_truncated'] = diff_trunc
            removed.append(item)
        elif old_f['sha256'] == new_f['sha256']:
            unchanged_count += 1
        else:
            binary = not (old_f['utf8'] and new_f['utf8'])
            item = {
                'path': rel,
                'old_sha256': old_f['sha256'],
                'new_sha256': new_f['sha256'],
                'old_size_bytes': old_f['size_bytes'],
                'new_size_bytes': new_f['size_bytes'],
                'kind': new_f['kind'],
                'binary': binary,
            }
            if not binary and max(old_f['size_bytes'], new_f['size_bytes']) <= MAX_DIFF_BYTES:
                old_text = _read_verified_stored_file(old_root, old_f, max_bytes=MAX_DIFF_BYTES).decode('utf-8')
                new_text = _read_verified_stored_file(new_root, new_f, max_bytes=MAX_DIFF_BYTES).decode('utf-8')
                diff_text, diff_trunc = _bounded_unified_diff(old_text, new_text, rel, old_manifest['snapshot_id'], new_manifest['snapshot_id'])
                item['unified_diff'] = diff_text
                item['diff_truncated'] = diff_trunc
            elif not binary:
                item['diff_omitted_reason'] = f'exceeds {MAX_DIFF_BYTES} bytes'
            modified.append(item)

    old_refs = {(r['source_file'], r['normalized'], r['status']) for r in old_manifest.get('references', [])}
    new_refs = {(r['source_file'], r['normalized'], r['status']) for r in new_manifest.get('references', [])}
    old_binds = {(b['kind'], b['name'], b['source_file']) for b in old_manifest.get('external_bindings', [])}
    new_binds = {(b['kind'], b['name'], b['source_file']) for b in new_manifest.get('external_bindings', [])}

    return {
        'schema': DIFF_SCHEMA,
        'authority': 'NONE',
        'bundle_id': old_manifest['bundle_id'],
        'old_snapshot_id': old_manifest['snapshot_id'],
        'new_snapshot_id': new_manifest['snapshot_id'],
        'old_bundle_sha256': old_manifest['bundle_sha256'],
        'new_bundle_sha256': new_manifest['bundle_sha256'],
        'changed': old_manifest['bundle_sha256'] != new_manifest['bundle_sha256'],
        'summary': {
            'added': len(added),
            'removed': len(removed),
            'modified': len(modified),
            'unchanged': unchanged_count,
        },
        'added': added,
        'removed': removed,
        'modified': modified,
        'coverage_delta': {
            'complete_directory': [old_manifest['scope']['complete_directory'], new_manifest['scope']['complete_directory']],
            'added_references': [
                {'source_file': s, 'normalized': n, 'status': st} for s, n, st in sorted(new_refs - old_refs)
            ],
            'removed_references': [
                {'source_file': s, 'normalized': n, 'status': st} for s, n, st in sorted(old_refs - new_refs)
            ],
            'added_external_bindings': [
                {'kind': k, 'name': n, 'source_file': s} for k, n, s in sorted(new_binds - old_binds)
            ],
            'removed_external_bindings': [
                {'kind': k, 'name': n, 'source_file': s} for k, n, s in sorted(old_binds - new_binds)
            ],
        },
    }


def format_manifest(manifest: dict) -> str:
    """Human-readable inert summary of an inspected or captured bundle."""
    src = manifest['source']
    scope = manifest['scope']
    summ = manifest['summary']
    state = 'captured (already present)' if manifest.get('already_captured') else (
        'captured' if manifest.get('stored') else 'preview only (not stored)'
    )
    lines = [
        f"Skill bundle: {manifest['bundle_id']} ({src['skill_name']}) — {state}",
        f"Snapshot: {manifest['snapshot_id']}  Bundle SHA-256: {manifest['bundle_sha256']}",
        f"Origin: {src['origin_path']}",
        f"Scope: {scope['kind']} (complete_directory={scope['complete_directory']}); "
        f"authority={manifest['authority']}, active_in_session={manifest['active_in_session']}, "
        f"effects_authorized={manifest['effects_authorized']}",
        f"Files: {summ['file_count']} captured ({summ['total_bytes']} B; "
        f"scripts={summ['script_count']}, opaque={summ['opaque_count']}, skipped={summ['skipped_count']}, "
        f"excluded_dirs={summ['excluded_dir_count']})",
    ]
    if manifest.get('snapshot_dir'):
        lines.append(f"Stored at: {manifest['snapshot_dir']}")
    ref_counts = summ['reference_counts']
    lines.append(
        f"References: resolved={ref_counts['resolved']}, missing={ref_counts['missing']}, "
        f"outside_scope={ref_counts['outside_scope']}, not_inspected={ref_counts['not_inspected']}"
    )
    if manifest.get('external_bindings'):
        lines.append(f"External bindings ({len(manifest['external_bindings'])}, none authorized):")
        for b in manifest['external_bindings'][:12]:
            detail = f" ({b['detail']})" if b.get('detail') else ''
            lines.append(f"  - [{b['kind']}] {b['name']}{detail} <- {b['source_file']}")
    if manifest.get('coverage_warnings'):
        lines.append('Coverage notices:')
        for w in manifest['coverage_warnings']:
            lines.append(f'  ! {w}')
    lines.append('Captured files:')
    for f in manifest['files'][:40]:
        lines.append(f"  - {f['path']} [{f['kind']}, {f['size_bytes']} B, {f['sha256'][:12]}]")
    if len(manifest['files']) > 40:
        lines.append(f"  ... ({len(manifest['files']) - 40} more files)")
    return '\n'.join(lines)


def format_diff(report: dict) -> str:
    """Human-readable reviewable diff between two captured snapshots."""
    s = report['summary']
    lines = [
        f"Skill bundle diff: {report['bundle_id']} ({report['old_snapshot_id']} -> {report['new_snapshot_id']})",
        f"Changed: {report['changed']} (+{s['added']} added, -{s['removed']} removed, "
        f"~{s['modified']} modified, ={s['unchanged']} unchanged)",
    ]
    for label, bucket in (('Added', report['added']), ('Removed', report['removed']), ('Modified', report['modified'])):
        for item in bucket:
            lines.append(f"\n[{label}] {item['path']} ({item['kind']})")
            if item.get('binary'):
                lines.append('  (binary/opaque file; hashes compared without text diff)')
            elif item.get('unified_diff'):
                lines.append(item['unified_diff'])
                if item.get('diff_truncated'):
                    lines.append('  ... (diff truncated to bounded size)')
            elif item.get('diff_omitted_reason'):
                lines.append(f"  (text diff omitted: {item['diff_omitted_reason']})")
    cov = report['coverage_delta']
    if cov['added_external_bindings'] or cov['removed_external_bindings']:
        lines.append('\nExternal bindings delta:')
        for b in cov['added_external_bindings']:
            lines.append(f"  + [{b['kind']}] {b['name']} <- {b['source_file']}")
        for b in cov['removed_external_bindings']:
            lines.append(f"  - [{b['kind']}] {b['name']} <- {b['source_file']}")
    return '\n'.join(lines)


def main(argv=None) -> int:
    """CLI entrypoint for `tare skills` (inert inspection, capture, listing, show, diff)."""
    parser = argparse.ArgumentParser(
        prog='tare skills',
        description='Inert local skill bundle inspection, versioned snapshots, and reviewable diffs.',
    )
    sub = parser.add_subparsers(dest='action', required=True)

    def add_scan_args(cmd):
        cmd.add_argument('path', type=Path, help='Local skill directory containing SKILL.md')
        cmd.add_argument('--name', help='Explicit bundle id slug (defaults to SKILL.md name or folder name)')
        cmd.add_argument('--include', action='append', default=None,
                         help='Limit capture to specific relative paths/subfolders (SKILL.md is always included)')
        cmd.add_argument('--max-files', type=int, default=DEFAULT_MAX_FILES)
        cmd.add_argument('--max-file-bytes', type=int, default=DEFAULT_MAX_FILE_BYTES)
        cmd.add_argument('--max-total-bytes', type=int, default=DEFAULT_MAX_TOTAL_BYTES)
        cmd.add_argument('--max-depth', type=int, default=DEFAULT_MAX_DEPTH)
        cmd.add_argument('--json', action='store_true', dest='as_json')

    p_inspect = sub.add_parser('inspect', help='Inspect a local skill directory without storing or executing anything')
    add_scan_args(p_inspect)

    p_capture = sub.add_parser('capture', help='Capture a local skill directory as an inert, versioned snapshot')
    add_scan_args(p_capture)

    p_list = sub.add_parser('list', help='List captured inert skill snapshots')
    p_list.add_argument('--bundle', help='Filter to one bundle id')
    p_list.add_argument('--json', action='store_true', dest='as_json')

    p_show = sub.add_parser('show', help='Show one verified snapshot manifest or one captured file')
    p_show.add_argument('bundle', help='Bundle id')
    p_show.add_argument('snapshot', nargs='?', default='latest', help='Snapshot id/prefix, latest, or previous')
    p_show.add_argument('--file', dest='rel_file', help='Print the exact captured UTF-8 text of one file')
    p_show.add_argument('--json', action='store_true', dest='as_json')

    p_diff = sub.add_parser('diff', help='Compare two captured snapshots of a bundle')
    p_diff.add_argument('bundle', help='Bundle id')
    p_diff.add_argument('old', nargs='?', default='previous', help='Base snapshot ref (default: previous)')
    p_diff.add_argument('new', nargs='?', default='latest', help='Target snapshot ref (default: latest)')
    p_diff.add_argument('--json', action='store_true', dest='as_json')

    p_adopt = sub.add_parser('adopt', help='Create a managed skill from a verified inert snapshot')
    p_adopt.add_argument('bundle', help='Captured bundle id')
    p_adopt.add_argument('snapshot', nargs='?', default='latest', help='Snapshot ref (default: latest)')
    p_adopt.add_argument('--as', dest='skill_id', help='Explicit managed skill id')
    p_adopt.add_argument('--roles', help='Comma-separated allowed roles (defaults to coordinator,implementer,planner,plan-auditor,auditor)')
    p_adopt.add_argument('--min-model-class', choices=('cheap', 'middling', 'frontier'),
                         help='Minimum recommended model class floor for delegated implementer units')
    p_adopt.add_argument('--json', action='store_true', dest='as_json')

    p_managed = sub.add_parser('managed', help='List managed skills with origin, roles, overlay, and update status')
    p_managed.add_argument('--project', type=Path, help='Resolve per-project overrides for this folder')
    p_managed.add_argument('--role', help='Filter to skills effectively allowed for this role')
    p_managed.add_argument('--json', action='store_true', dest='as_json')

    p_effective = sub.add_parser('effective', help='Show effective rendered skill content, roles, provenance, and diffs')
    p_effective.add_argument('skill_id', help='Managed skill id')
    p_effective.add_argument('--project', type=Path, help='Resolve per-project overrides for this folder')
    p_effective.add_argument('--role', help='Check whether this role can access this skill')
    p_effective.add_argument('--text-only', action='store_true', help='Print only the rendered effective text')
    p_effective.add_argument('--json', action='store_true', dest='as_json')

    p_roles = sub.add_parser('roles', help='Show or configure allowed roles for a managed skill')
    p_roles.add_argument('skill_id', help='Managed skill id')
    p_roles.add_argument('roles', nargs='*', help='Allowed roles (e.g. implementer planner auditor)')
    roles_mode = p_roles.add_mutually_exclusive_group()
    roles_mode.add_argument('--none', action='store_true', dest='clear_all', help='Disallow this skill for all roles')
    roles_mode.add_argument('--reset', action='store_true', help='Reset global roles to defaults or clear project override')
    p_roles.add_argument('--project', type=Path, help='Scope allowed roles to one project folder')
    p_roles.add_argument('--json', action='store_true', dest='as_json')

    p_rpol = sub.add_parser('role-policy', help='Show or configure global/project role enablement for all managed skills')
    p_rpol.add_argument('role', nargs='?', help='Role name (omit to display all roles)')
    rpol_mode = p_rpol.add_mutually_exclusive_group()
    rpol_mode.add_argument('--enable', action='store_true', help='Allow managed skills for this role')
    rpol_mode.add_argument('--disable', action='store_true', help='Disable all managed skills for this role')
    rpol_mode.add_argument('--reset', action='store_true', help='Reset this role to default (or clear project override)')
    p_rpol.add_argument('--project', type=Path, help='Scope role policy to one project folder')
    p_rpol.add_argument('--json', action='store_true', dest='as_json')

    p_edit = sub.add_parser('edit', help='Set or clear an editable overlay for a managed skill')
    p_edit.add_argument('skill_id', help='Managed skill id')
    edit_src = p_edit.add_mutually_exclusive_group(required=True)
    edit_src.add_argument('--text', help='New overlay markdown text')
    edit_src.add_argument('--file', type=Path, dest='overlay_file', help='Read overlay UTF-8 text from a local file')
    edit_src.add_argument('--clear', action='store_true', help='Clear the overlay and return to base snapshot text')
    p_edit.add_argument('--project', type=Path, help='Scope overlay to one project folder')
    p_edit.add_argument('--json', action='store_true', dest='as_json')

    p_param = sub.add_parser('param', help='Define, set, or delete a typed skill parameter')
    p_param.add_argument('skill_id', help='Managed skill id')
    p_param.add_argument('name', help='Parameter name (used as <name> in skill text)')
    p_param.add_argument('value', nargs='?', help='Parameter value (omit only with --delete)')
    p_param.add_argument('--type', dest='param_type', default='string', choices=('string', 'int', 'float', 'bool', 'choice'))
    p_param.add_argument('--default', dest='default_value', help='Explicit default value')
    p_param.add_argument('--choices', help='Comma-separated choices (for --type choice)')
    p_param.add_argument('--description', default='')
    p_param.add_argument('--project', type=Path, help='Set or delete a per-project parameter override')
    p_param.add_argument('--delete', action='store_true', help='Delete this parameter (or project override)')
    p_param.add_argument('--json', action='store_true', dest='as_json')

    p_update = sub.add_parser('update', help='Explicitly update a managed skill to a newer verified snapshot or live origin')
    p_update.add_argument('skill_id', help='Managed skill id')
    p_update.add_argument('snapshot', nargs='?', default='latest', help='Target snapshot ref (default: latest)')
    p_update.add_argument('--from-origin', action='store_true',
                          help='Re-capture the skill origin_path inertly first, then apply the update')
    p_update.add_argument('--keep-overlay', action='store_true', help='Keep existing global overlay over the new base')
    p_update.add_argument('--json', action='store_true', dest='as_json')

    p_remove = sub.add_parser('remove', help='Remove a managed skill config (inert snapshots stay intact)')
    p_remove.add_argument('skill_id', help='Managed skill id')
    p_remove.add_argument('--json', action='store_true', dest='as_json')

    p_bindings = sub.add_parser('bindings', help='Show per-runtime support and boundaries for managed skills')
    p_bindings.add_argument('--json', action='store_true', dest='as_json')

    p_catalog = sub.add_parser('catalog', help='Discover, classify, and deduplicate local Claude/Codex/AGY skills (read-only)')
    p_catalog.add_argument('--vendor', choices=('claude', 'codex', 'agy'), help='Filter to one vendor ecosystem')
    p_catalog.add_argument('--class', dest='classification',
                           choices=('portable_ready', 'local_tool_hybrid', 'needs_sanitization', 'blocked_capability', 'cli_internal'),
                           help='Filter to one static classification bucket')
    p_catalog.add_argument('--all', action='store_true', dest='include_hidden',
                           help='Include auto-hidden cli_internal and blocked_capability skills')
    p_catalog.add_argument('--json', action='store_true', dest='as_json')

    p_import = sub.add_parser('import', help='Capture and adopt one discovered vendor skill in one step')
    p_import.add_argument('target', help='Vendor catalog slug or catalog_id (from `tare skills catalog`)')
    p_import.add_argument('--vendor', choices=('claude', 'codex', 'agy'), help='Disambiguate by vendor')
    p_import.add_argument('--as', dest='skill_id', help='Explicit managed skill id')
    p_import.add_argument('--roles', help='Comma-separated allowed roles (defaults to classifier suggestion)')
    p_import.add_argument('--min-model-class', choices=('cheap', 'middling', 'frontier'),
                          help='Minimum recommended model class floor for delegated implementer units')
    p_import.add_argument('--all', action='store_true', dest='allow_hidden',
                          help='Allow importing a hidden cli_internal/blocked_capability skill explicitly')
    p_import.add_argument('--json', action='store_true', dest='as_json')

    p_mclass = sub.add_parser('model-class', help='Show or configure minimum recommended model class (cheap|middling|frontier) for a managed skill')
    p_mclass.add_argument('skill_id', help='Managed skill id')
    p_mclass.add_argument('model_class', nargs='?', choices=('cheap', 'middling', 'frontier'),
                          help='Minimum model class floor (omit to inspect current setting)')
    mc_mode = p_mclass.add_mutually_exclusive_group()
    mc_mode.add_argument('--clear', action='store_true', help='Clear minimum model class (or clear project override)')
    mc_mode.add_argument('--reset', action='store_true', help='Reset global model class to unset or clear project override')
    p_mclass.add_argument('--project', type=Path, help='Scope model class floor to one project folder')
    p_mclass.add_argument('--json', action='store_true', dest='as_json')

    p_triggers = sub.add_parser('triggers', help='Show or set explicit lexical trigger phrases for a managed skill')
    p_triggers.add_argument('skill_id', help='Managed skill id')
    p_triggers.add_argument('triggers', nargs='*', help='Trigger phrases/keywords (e.g. "pixel art" sprite)')
    trig_mode = p_triggers.add_mutually_exclusive_group()
    trig_mode.add_argument('--clear', action='store_true', help='Clear all triggers (or set empty list)')
    trig_mode.add_argument('--reset', action='store_true', help='Reset global triggers or clear project override')
    p_triggers.add_argument('--project', type=Path, help='Scope triggers to one project folder')
    p_triggers.add_argument('--json', action='store_true', dest='as_json')

    p_select = sub.add_parser('select', help='Preview deterministic lexical shortlist and abstention for a prompt')
    p_select.add_argument('prompt', help='User demand / prompt text to evaluate against eligible managed skills')
    p_select.add_argument('--role', help='Evaluate eligibility strictly for one role (omit for conversation-wide)')
    p_select.add_argument('--project', type=Path, help='Resolve per-project overrides for this folder')
    p_select.add_argument('--max-shortlist', type=int, default=5, help='Maximum shortlist size (1..5)')
    p_select.add_argument('--json', action='store_true', dest='as_json')

    args = parser.parse_args(argv)
    try:
        if args.action == 'inspect':
            result = inspect_bundle(
                args.path, name=args.name, include=args.include,
                max_files=args.max_files, max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes, max_depth=args.max_depth,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False) if args.as_json else format_manifest(result))
            return 0
        if args.action == 'capture':
            result = capture_bundle(
                args.path, name=args.name, include=args.include,
                max_files=args.max_files, max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes, max_depth=args.max_depth,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False) if args.as_json else format_manifest(result))
            return 0
        if args.action == 'list':
            items = list_snapshots(bundle_id=args.bundle)
            if args.as_json:
                print(json.dumps(items, indent=2, ensure_ascii=False))
            elif not items:
                print('No inert skill snapshots captured yet.')
            else:
                for it in items:
                    scope_tag = 'full' if it['complete_directory'] else 'subset'
                    print(f"{it['bundle_id']}@{it['snapshot_id']}  {it['captured_at']}  "
                          f"[{scope_tag}, {it['file_count']} files, {it['total_bytes']} B]  {it['skill_name']}")
            return 0
        if args.action == 'show':
            if args.rel_file:
                data, entry = read_snapshot_file(args.bundle, args.snapshot, args.rel_file)
                if args.as_json:
                    payload = {**entry, 'text': data.decode('utf-8') if entry['utf8'] else None}
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                else:
                    if not entry['utf8']:
                        raise ValueError(f"File {args.rel_file!r} is opaque/binary ({entry['size_bytes']} B, sha256={entry['sha256']})")
                    print(data.decode('utf-8'))
                return 0
            result = load_snapshot(args.bundle, args.snapshot)
            print(json.dumps(result, indent=2, ensure_ascii=False) if args.as_json else format_manifest(result))
            return 0
        if args.action == 'diff':
            report = diff_snapshots(args.bundle, args.old, args.new)
            print(json.dumps(report, indent=2, ensure_ascii=False) if args.as_json else format_diff(report))
            return 0
        from . import managed_skills as managed
        if args.action == 'adopt':
            roles = [r.strip() for r in args.roles.split(',') if r.strip()] if args.roles is not None else None
            view = managed.adopt_snapshot(
                args.bundle, args.snapshot, skill_id=args.skill_id,
                allowed_roles=roles, min_model_class=args.min_model_class,
            )
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'managed':
            rows = managed.list_managed(project=args.project, role=args.role)
            if args.as_json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            elif not rows:
                msg = (
                    f'No managed skills available for role {args.role}.'
                    if args.role else
                    'No managed skills configured yet. Capture a bundle and run `tare skills adopt <bundle>`.'
                )
                print(msg)
            else:
                for r in rows:
                    flags = [r['text_source'], f"roles={managed.format_role_tags(r['effective_roles'])}"]
                    if r.get('min_model_class'):
                        flags.append(f"tier={r['min_model_class']}")
                    if r['parameter_count']:
                        flags.append(f"{r['parameter_count']} params")
                    if r['upstream_update_available']:
                        flags.append(f"update->{r['latest_snapshot_id']}")
                    if r.get('origin_drift_available'):
                        flags.append('origin-drift')
                    print(f"{r['skill_id']}  ({r['bundle_id']}@{r['snapshot_id']})  [{', '.join(flags)}]  {r['skill_name']}")
            return 0
        if args.action == 'effective':
            view = managed.effective_view(args.skill_id, project=args.project, role=args.role)
            if args.as_json:
                print(json.dumps(view, indent=2, ensure_ascii=False))
            elif args.text_only:
                print(view['effective_text'])
            else:
                print(managed.format_effective(view))
            return 0
        if args.action == 'roles':
            if args.clear_all:
                if args.roles:
                    raise ValueError('Do not pass role names together with --none')
                view = managed.set_skill_roles(args.skill_id, [], project=args.project)
            elif args.reset:
                if args.roles:
                    raise ValueError('Do not pass role names together with --reset')
                view = managed.set_skill_roles(args.skill_id, None, project=args.project)
            elif args.roles:
                view = managed.set_skill_roles(args.skill_id, args.roles, project=args.project)
            else:
                view = managed.effective_view(args.skill_id, project=args.project)
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'role-policy':
            if args.role is None:
                if args.enable or args.disable or args.reset:
                    raise ValueError('Specify a role name with --enable, --disable, or --reset')
                policy = managed.get_role_policy(project=args.project)
            elif args.enable:
                policy = managed.set_role_policy(args.role, True, project=args.project)
            elif args.disable:
                policy = managed.set_role_policy(args.role, False, project=args.project)
            elif args.reset:
                policy = managed.set_role_policy(args.role, None, project=args.project)
            else:
                managed._validate_role(args.role)
                policy = managed.get_role_policy(project=args.project)
            print(json.dumps(policy, indent=2, ensure_ascii=False) if args.as_json else managed.format_role_policy(policy))
            return 0
        if args.action == 'edit':
            if args.clear:
                new_text = None
            elif args.overlay_file is not None:
                raw, _ = _read_pinned_bytes(args.overlay_file.absolute(), managed.MAX_SKILL_TEXT_BYTES)
                if raw is None:
                    raise ValueError(f'Overlay file exceeds {managed.MAX_SKILL_TEXT_BYTES} bytes')
                try:
                    new_text = raw.decode('utf-8', errors='strict')
                except UnicodeError as exc:
                    raise ValueError('Overlay file must be valid UTF-8') from exc
            else:
                new_text = args.text
            view = managed.set_overlay(args.skill_id, new_text, project=args.project)
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'param':
            if args.delete:
                view = managed.delete_parameter(args.skill_id, args.name, project=args.project)
            else:
                if args.value is None:
                    raise ValueError('Provide a parameter value or pass --delete')
                choices = [c.strip() for c in args.choices.split(',') if c.strip()] if args.choices else None
                view = managed.set_parameter(
                    args.skill_id, args.name, args.value,
                    param_type=args.param_type, default=args.default_value,
                    choices=choices, description=args.description, project=args.project,
                )
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'update':
            if args.from_origin:
                from . import vendor_skills as vendor_cat
                view = vendor_cat.sync_managed_from_origin(args.skill_id, keep_overlay=args.keep_overlay)
            else:
                view = managed.apply_upstream_update(args.skill_id, args.snapshot, keep_overlay=args.keep_overlay)
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'remove':
            removed = managed.remove_managed(args.skill_id)
            if args.as_json:
                print(json.dumps({'skill_id': args.skill_id, 'removed': removed}))
            else:
                print(f'Removed managed skill {args.skill_id}.' if removed else f'Managed skill {args.skill_id} was not found.')
            return 0 if removed else 2
        if args.action == 'bindings':
            rows = managed.runtime_bindings()
            print(json.dumps(rows, indent=2, ensure_ascii=False) if args.as_json else managed.format_bindings(rows))
            return 0
        if args.action == 'catalog':
            from . import vendor_skills as vendor_cat
            catalog = vendor_cat.discover_vendor_catalog(
                vendor=args.vendor,
                include_hidden=args.include_hidden,
                classification=args.classification,
            )
            print(json.dumps(catalog, indent=2, ensure_ascii=False) if args.as_json else vendor_cat.format_catalog(catalog))
            return 0
        if args.action == 'import':
            from . import vendor_skills as vendor_cat
            roles = [r.strip() for r in args.roles.split(',') if r.strip()] if args.roles is not None else None
            view = vendor_cat.import_vendor_skill(
                args.target,
                vendor=args.vendor,
                skill_id=args.skill_id,
                allowed_roles=roles,
                min_model_class=args.min_model_class,
                allow_hidden=args.allow_hidden,
            )
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'model-class':
            if args.clear or args.reset:
                if args.model_class is not None:
                    raise ValueError('Do not pass a model class together with --clear or --reset')
                view = managed.set_model_class(args.skill_id, None, project=args.project)
            elif args.model_class is not None:
                view = managed.set_model_class(args.skill_id, args.model_class, project=args.project)
            else:
                view = managed.effective_view(args.skill_id, project=args.project)
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'triggers':
            if args.clear:
                if args.triggers:
                    raise ValueError('Do not pass trigger phrases together with --clear')
                view = managed.set_triggers(args.skill_id, [], project=args.project)
            elif args.reset:
                if args.triggers:
                    raise ValueError('Do not pass trigger phrases together with --reset')
                view = managed.set_triggers(args.skill_id, None, project=args.project)
            elif args.triggers:
                view = managed.set_triggers(args.skill_id, args.triggers, project=args.project)
            else:
                view = managed.effective_view(args.skill_id, project=args.project)
            print(json.dumps(view, indent=2, ensure_ascii=False) if args.as_json else managed.format_effective(view))
            return 0
        if args.action == 'select':
            from . import skill_selector as selector
            receipt = selector.select_skills(
                args.prompt,
                project=args.project,
                role=args.role,
                max_shortlist=args.max_shortlist,
                safe=False,
            )
            print(json.dumps(receipt, indent=2, ensure_ascii=False) if args.as_json else selector.format_selection(receipt))
            return 0
    except ValueError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

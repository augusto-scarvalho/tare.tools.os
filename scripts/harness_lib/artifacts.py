'''Derived artifact inventory and confined serving paths.'''
from __future__ import annotations

import datetime as dt
import shutil
import sys
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from .ws_files import MAX_READ, _secret_like
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.ws_files import MAX_READ, _secret_like


ARTIFACT_ROOTS = (Path('.harness/handoff'), Path('.harness/runs'))
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp'})
ARTIFACT_TYPES = ('image', 'log', 'report', 'audit', 'ledger', 'evidence')


@lru_cache(maxsize=8)
def _roots(root: Path) -> tuple[Path, tuple[Path, ...]]:
    resolved = root.resolve()
    return resolved, tuple((resolved / rel).resolve() for rel in ARTIFACT_ROOTS)


def _type(path: Path) -> str | None:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return 'image'
    if name.startswith('audit-'):
        return 'audit'
    if suffix == '.jsonl':
        return 'ledger'
    if name.startswith('gate-') and suffix in {'.log', '.marker'}:
        return 'log'
    if suffix == '.json':
        return 'evidence'
    if suffix == '.md':
        return 'report'
    return None


def resolve(root: Path, rel: Any) -> tuple[Path | None, str]:
    '''Resolve a repo-relative artifact path inside the two serving roots.'''
    if not isinstance(rel, str) or not rel.strip():
        return None, 'path required'
    if '\\' in rel or ':' in rel or rel.startswith(('/', '~')):
        return None, 'repo-relative POSIX path required (no absolute paths, drives, or backslashes)'
    if '..' in rel.split('/'):
        return None, 'path traversal is not allowed'
    root, allowed_roots = _roots(Path(root))
    try:
        path = (root / rel).resolve()
        inside = path.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None, 'path escapes the repository root'
    if _secret_like(inside):
        return None, 'path matches the secrets denylist'
    for allowed in allowed_roots:
        try:
            path.relative_to(allowed)
            return path, ''
        except ValueError:
            continue
    return None, 'path is outside the artifact roots'


def list_artifacts(root: Path) -> list[dict[str, Any]]:
    '''List classified files from the two artifact roots, newest first.'''
    root = root.resolve()
    rows: list[tuple[int, dict[str, Any]]] = []
    for artifact_root in ARTIFACT_ROOTS:
        base = root / artifact_root
        if not base.is_dir():
            continue
        try:
            candidates = base.rglob('*')
            for candidate in candidates:
                kind = _type(candidate)
                if kind is None:
                    continue
                rel = candidate.relative_to(root).as_posix()
                path, _ = resolve(root, rel)
                if path is None or not path.is_file():
                    continue
                stat = path.stat()
                rows.append((stat.st_mtime_ns, {
                    'path': rel,
                    'type': kind,
                    'bytes': stat.st_size,
                    'mtime': dt.datetime.fromtimestamp(
                        stat.st_mtime, dt.timezone.utc
                    ).isoformat(timespec='seconds'),
                    'name': path.name,
                }))
        except OSError:
            continue
    rows.sort(key=lambda item: (-item[0], item[1]['path']))
    return [row for _, row in rows]


def cmd_artifacts(args) -> int:
    """CLI verb (registered thin in cli_registry, the repo idiom for read-only lists)."""
    from harness_lib import common
    wanted = getattr(args, 'type', None)
    rows = [r for r in list_artifacts(common.ROOT) if wanted in (None, r['type'])]
    common.emit(rows, tsv=True)  # TE.5: agent-facing lists stay tabular
    return 0


def _self_check() -> None:
    root = Path(tempfile.gettempdir()) / f'harness-artifacts-{uuid.uuid4().hex}'
    root.mkdir()
    try:
        handoff = root / ARTIFACT_ROOTS[0]
        runs = root / ARTIFACT_ROOTS[1]
        handoff.mkdir(parents=True)
        runs.mkdir(parents=True)
        fixtures = {
            handoff / 'result-screen.png': ('image', b'PNG\r\n'),
            handoff / 'audit-wave.md': ('audit', b'# audit\n'),
            handoff / 'result-wave.md': ('report', b'# result\n'),
            runs / 'events.jsonl': ('ledger', b'{}\n'),
            runs / 'gate-smoke.log': ('log', b'pass\n'),
            runs / 'gate-smoke.marker': ('log', b'0\n'),
            runs / 'probe.json': ('evidence', b'{}\n'),
        }
        for path, (_, data) in fixtures.items():
            path.write_bytes(data)
        (runs / 'preview.svg').write_text('<svg></svg>', encoding='utf-8')
        (runs / '.env').write_text('TOKEN=secret', encoding='utf-8')

        rows = {row['path']: row for row in list_artifacts(root)}
        for path, (kind, _) in fixtures.items():
            rel = path.relative_to(root).as_posix()
            assert rows[rel]['type'] == kind, rows
        assert all(not row['path'].endswith(('.svg', '/.env')) for row in rows.values())
        assert resolve(root, '.harness/handoff/result-screen.png')[0] is not None
        assert resolve(root, '.harness/runs/.env')[0] is None
        assert resolve(root, '.harness/handoff/../runs/events.jsonl')[0] is None
        assert resolve(root, 'outside/report.md')[0] is None
        assert '.svg' not in IMAGE_EXTENSIONS
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print('artifacts self-check ok')


if __name__ == '__main__':
    _self_check()

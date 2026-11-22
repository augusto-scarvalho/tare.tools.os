"""Offline boundary, coverage, immutability, diff, and CLI tests for inert local skill bundle snapshots (PA-04 / SKL-02)."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from relay import console_resources as resources
from relay import skill_bundles as bundles
import tare_ops


def _write(root: Path, relative: str, data: bytes | str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding='utf-8', newline='\n')
    else:
        path.write_bytes(data)
    return path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_pxl_like_bundle(root: Path) -> Path:
    _write(root, 'SKILL.md', (
        '---\n'
        'name: PXL Pixel Art\n'
        'description: Compact pixel art DSL\n'
        '---\n'
        '# PXL\n\n'
        'Read [reference](reference.md) and [missing](missing.md).\n'
        'Run `scripts/render.js` or inspect `assets/palette.bin`.\n'
        'See [upstream](https://example.invalid/docs) and [escape](../outside.md).\n'
        'Build output lives in [dist](dist/bundle.js).\n'
    ))
    _write(root, 'reference.md', '# Reference\n\nSee [guide](docs/guide.md).\n')
    _write(root, 'docs/guide.md', '# Guide\n\nUse `bin/pxl.js`.\n')
    _write(root, 'scripts/render.js', 'console.log("never executed");\n')
    _write(root, 'bin/pxl.js', '#!/usr/bin/env node\n')
    _write(root, 'assets/palette.bin', b'\x89PNG\r\n\x1a\n\xff\x00\xfe')
    _write(root, 'package.json', json.dumps({
        'name': 'pxl',
        'engines': {'node': '>=20'},
        'bin': {'pxl': 'bin/pxl.js'},
        'dependencies': {'pngjs': '^7.0.0'},
    }))
    # Excluded directory must never be traversed or labeled complete.
    _write(root, 'dist/bundle.js', 'compiled output')
    _write(root, 'node_modules/pkg/index.js', 'third party')
    return root


def test_inspect_is_read_only_and_classifies_references_bindings_and_subset_coverage(tmp_path):
    storage = tmp_path / 'storage'
    src = _make_pxl_like_bundle(tmp_path / 'pxl-repo')

    report = bundles.inspect_bundle(src)
    assert report['schema'] == bundles.SCHEMA
    assert report['authority'] == 'NONE'
    assert report['active_in_session'] is False
    assert report['effects_authorized'] is False
    assert report['stored'] is False
    assert report['bundle_id'] == 'pxl-pixel-art'
    assert report['source']['skill_name'] == 'PXL Pixel Art'
    assert report['source']['description'] == 'Compact pixel art DSL'
    assert report['scope']['kind'] == 'subset'
    assert report['scope']['complete_directory'] is False
    assert report['excluded_dirs'] == ['dist', 'node_modules']
    assert not (storage / 'serve' / 'skills' / 'snapshots').exists()

    by_path = {f['path']: f for f in report['files']}
    assert by_path['SKILL.md']['kind'] == 'skill_doc'
    assert by_path['reference.md']['kind'] == 'reference'
    assert by_path['scripts/render.js']['kind'] == 'script'
    assert by_path['scripts/render.js']['inert_data'] is True
    assert by_path['assets/palette.bin']['kind'] == 'opaque'
    assert by_path['assets/palette.bin']['utf8'] is False
    assert by_path['assets/palette.bin']['inspected'] is False
    assert by_path['package.json']['kind'] == 'manifest_or_config'

    refs = {(r['source_file'], r['normalized']): (r['status'], r['reason']) for r in report['references']}
    assert refs[('SKILL.md', 'reference.md')] == ('resolved', 'captured_file')
    assert refs[('SKILL.md', 'missing.md')] == ('missing', 'not_found_in_bundle')
    assert refs[('SKILL.md', 'scripts/render.js')] == ('resolved', 'captured_file')
    assert refs[('SKILL.md', 'assets/palette.bin')] == ('not_inspected', 'opaque_captured_file')
    assert refs[('SKILL.md', 'https://example.invalid/docs')] == ('outside_scope', 'remote_url')
    assert refs[('SKILL.md', '../outside.md')] == ('outside_scope', 'path_outside_bundle')
    assert refs[('SKILL.md', 'dist/bundle.js')] == ('outside_scope', 'excluded_dir')
    assert refs[('reference.md', 'docs/guide.md')] == ('resolved', 'captured_file')
    assert refs[('docs/guide.md', 'bin/pxl.js')] == ('resolved', 'captured_file')

    bindings = {(b['kind'], b['name'], b['source_file']): b for b in report['external_bindings']}
    assert ('runtime_engine', 'node', 'package.json') in bindings
    assert ('npm_dependency', 'pngjs', 'package.json') in bindings
    assert ('cli_entrypoint', 'pxl', 'package.json') in bindings
    assert ('script_runtime', 'node', 'scripts/render.js') in bindings
    assert all(b['authorized'] is False and b['executed'] is False for b in bindings.values())


def test_capture_stores_inert_snapshot_outside_active_discovery_and_survives_source_mutation(tmp_path):
    storage = tmp_path / 'storage'
    project = tmp_path / 'project'
    project.mkdir()
    src = tmp_path / 'local-skill'
    _write(src, 'SKILL.md', '---\nname: Clean Skill\n---\nOriginal guidance.\n')
    _write(src, 'references/notes.md', 'Reference v1.\n')

    captured = bundles.capture_bundle(src, storage_root=storage)
    assert captured['stored'] is True
    assert captured['already_captured'] is False
    assert captured['scope']['kind'] == 'full_directory'
    assert captured['scope']['complete_directory'] is True
    snap_dir = Path(captured['snapshot_dir'])
    assert snap_dir.is_dir()
    assert snap_dir.parent.parent == storage / 'serve' / 'skills' / 'snapshots'

    # Active project skill discovery must never pick up inert snapshots.
    assert resources.discover_skills(project) == []
    assert resources.discover_skills(storage) == []

    # Re-capturing identical bytes returns existing snapshot without mutating it.
    second = bundles.capture_bundle(src, storage_root=storage)
    assert second['already_captured'] is True
    assert second['snapshot_id'] == captured['snapshot_id']
    assert second['captured_at'] == captured['captured_at']

    # Mutating or deleting the source directory never changes the captured snapshot.
    _write(src, 'SKILL.md', '---\nname: Clean Skill\n---\nMUTATED AFTER CAPTURE.\n')
    (src / 'references/notes.md').unlink()
    loaded = bundles.load_snapshot('clean-skill', captured['snapshot_id'], storage_root=storage)
    assert loaded['bundle_sha256'] == captured['bundle_sha256']
    data, entry = bundles.read_snapshot_file('clean-skill', captured['snapshot_id'], 'SKILL.md', storage_root=storage)
    assert data.decode('utf-8') == '---\nname: Clean Skill\n---\nOriginal guidance.\n'
    assert entry['sha256'] == _digest(data)
    ref_bytes, _ = bundles.read_snapshot_file('clean-skill', captured['snapshot_id'], 'references/notes.md', storage_root=storage)
    assert ref_bytes == b'Reference v1.\n'


def test_tampered_stored_snapshot_fails_closed(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'demo'
    _write(src, 'SKILL.md', '---\nname: Demo\n---\nSafe body.\n')
    captured = bundles.capture_bundle(src, storage_root=storage)
    snap_dir = Path(captured['snapshot_dir'])

    # Tamper with stored file bytes -> load_snapshot must refuse.
    (snap_dir / 'files' / 'SKILL.md').write_text('---\nname: Demo\n---\nTAMPERED.\n', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch|size mismatch'):
        bundles.load_snapshot('demo', captured['snapshot_id'], storage_root=storage)

    # Restore exact bytes, then add an unexpected file -> refuse.
    (snap_dir / 'files' / 'SKILL.md').write_text('---\nname: Demo\n---\nSafe body.\n', encoding='utf-8', newline='\n')
    (snap_dir / 'files' / 'injected.txt').write_text('sneaky', encoding='utf-8')
    with pytest.raises(ValueError, match='does not match manifest'):
        bundles.load_snapshot('demo', captured['snapshot_id'], storage_root=storage)


def test_diff_between_two_captured_versions_reports_text_binary_and_binding_changes(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'skill'
    _write(src, 'SKILL.md', '---\nname: Tune\n---\nUse [guide](guide.md).\n')
    _write(src, 'guide.md', 'Line 1\nLine 2\n')
    _write(src, 'old.txt', 'removed text\n')
    _write(src, 'icon.bin', b'\xff\x01\x02')
    v1 = bundles.capture_bundle(src, storage_root=storage)

    _write(src, 'SKILL.md', '---\nname: Tune\n---\nUse [guide](guide.md) and `scripts/run.py`.\n')
    _write(src, 'guide.md', 'Line 1\nLine 2 updated\n')
    (src / 'old.txt').unlink()
    _write(src, 'icon.bin', b'\xff\x01\x09\x0a')
    _write(src, 'scripts/run.py', 'print("inert")\n')
    _write(src, 'pyproject.toml', '[project]\nname = "tune"\nrequires-python = ">=3.11"\ndependencies = ["numpy>=2"]\n')
    v2 = bundles.capture_bundle(src, storage_root=storage)

    report = bundles.diff_snapshots('tune', v1['snapshot_id'], v2['snapshot_id'], storage_root=storage)
    assert report['schema'] == bundles.DIFF_SCHEMA
    assert report['changed'] is True
    assert report['summary'] == {'added': 2, 'removed': 1, 'modified': 3, 'unchanged': 0}

    added_paths = {a['path']: a for a in report['added']}
    assert set(added_paths) == {'pyproject.toml', 'scripts/run.py'}
    assert '+print("inert")' in added_paths['scripts/run.py']['unified_diff']

    assert [r['path'] for r in report['removed']] == ['old.txt']
    assert '-removed text' in report['removed'][0]['unified_diff']

    mod_map = {m['path']: m for m in report['modified']}
    assert '-Line 2' in mod_map['guide.md']['unified_diff']
    assert '+Line 2 updated' in mod_map['guide.md']['unified_diff']
    assert mod_map['icon.bin']['binary'] is True
    assert 'unified_diff' not in mod_map['icon.bin']

    added_binds = {(b['kind'], b['name']) for b in report['coverage_delta']['added_external_bindings']}
    assert ('script_runtime', 'python') in added_binds
    assert ('python_dependency', 'numpy') in added_binds

    # Default refs (previous -> latest) resolve cleanly too.
    default_report = bundles.diff_snapshots('tune', storage_root=storage)
    assert default_report['old_snapshot_id'] == v1['snapshot_id']
    assert default_report['new_snapshot_id'] == v2['snapshot_id']
    rendered = bundles.format_diff(default_report)
    assert 'guide.md' in rendered and 'binary/opaque' in rendered


def test_include_filter_and_size_and_file_count_ceilings_stay_honest(tmp_path):
    src = tmp_path / 'big'
    _write(src, 'SKILL.md', '---\nname: Big\n---\nSee [ref](ref.md) and [huge](huge.txt) and [extra](extra/a.md).\n')
    _write(src, 'ref.md', 'small ref\n')
    _write(src, 'huge.txt', b'x' * 2048)
    _write(src, 'extra/a.md', 'outside filter\n')

    subset = bundles.inspect_bundle(src, include=['ref.md', 'huge.txt'], max_file_bytes=512)
    assert subset['scope']['complete_directory'] is False
    assert subset['scope']['include'] == ['huge.txt', 'ref.md']
    assert [f['path'] for f in subset['files']] == ['SKILL.md', 'ref.md']
    assert subset['skipped'] == [{'path': 'huge.txt', 'kind': 'file', 'size_bytes': 2048, 'reason': 'exceeds_file_bytes'}]
    assert 'extra' in subset['outside_filter']
    refs = {r['normalized']: (r['status'], r['reason']) for r in subset['references']}
    assert refs['huge.txt'] == ('not_inspected', 'exceeds_file_bytes')
    assert refs['extra/a.md'] == ('outside_scope', 'outside_include_filter')

    capped = bundles.inspect_bundle(src, max_files=2)
    assert capped['summary']['truncated'] is True
    assert capped['summary']['file_count'] == 2
    assert any(s['reason'] == 'exceeds_file_count' for s in capped['skipped'])


def test_missing_or_invalid_skill_md_and_traversal_rejected(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(ValueError, match='SKILL.md'):
        bundles.inspect_bundle(empty)

    bad_utf8 = tmp_path / 'bad-utf8'
    _write(bad_utf8, 'SKILL.md', b'\xff\xfe')
    with pytest.raises(ValueError, match='UTF-8'):
        bundles.inspect_bundle(bad_utf8)

    with pytest.raises(ValueError, match='Parent traversal'):
        bundles.inspect_bundle(tmp_path / '..' / 'empty')

    with pytest.raises(ValueError, match='Invalid bundle id'):
        bundles.load_snapshot('../escape', 'latest', storage_root=tmp_path)


@pytest.mark.parametrize('link_target', ['root', 'file', 'dir'])
def test_symlink_in_root_or_inside_bundle_is_refused(tmp_path, link_target):
    real = tmp_path / 'real'
    _write(real, 'SKILL.md', '---\nname: Sym\n---\nHello.\n')
    outside = tmp_path / 'outside'
    _write(outside, 'secret.txt', 'secret')
    try:
        if link_target == 'root':
            link = tmp_path / 'linked-root'
            link.symlink_to(real, target_is_directory=True)
            target = link
        elif link_target == 'file':
            (real / 'linked.txt').symlink_to(outside / 'secret.txt')
            target = real
        else:
            (real / 'linked-dir').symlink_to(outside, target_is_directory=True)
            target = real
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'Symlinks unavailable: {exc}')

    with pytest.raises(ValueError, match='Linked|reparse'):
        bundles.inspect_bundle(target)


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction boundary')
def test_windows_junction_inside_bundle_is_refused(tmp_path):
    bundle = tmp_path / 'bundle'
    _write(bundle, 'SKILL.md', '---\nname: Junc\n---\nHello.\n')
    outside = tmp_path / 'outside'
    _write(outside, 'secret.txt', 'secret')
    junction = bundle / 'linked'
    res = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                         capture_output=True, text=True, check=False)
    if res.returncode:
        pytest.skip(f'Junction creation unavailable: {res.stderr}')
    try:
        with pytest.raises(ValueError, match='Linked|reparse'):
            bundles.inspect_bundle(bundle)
    finally:
        junction.rmdir()


def test_cli_dispatch_through_tare_ops_inspect_capture_list_show_diff(tmp_path, monkeypatch, capsys):
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    src = tmp_path / 'my-skill'
    _write(src, 'SKILL.md', '---\nname: CLI Skill\n---\nVersion 1.\n')

    assert tare_ops.main(['skills', 'inspect', str(src), '--json']) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected['stored'] is False and inspected['bundle_id'] == 'cli-skill'

    assert tare_ops.main(['skills', 'capture', str(src), '--json']) == 0
    cap1 = json.loads(capsys.readouterr().out)
    assert cap1['stored'] is True and cap1['already_captured'] is False

    _write(src, 'SKILL.md', '---\nname: CLI Skill\n---\nVersion 2.\n')
    assert tare_ops.main(['skills', 'capture', str(src), '--json']) == 0
    cap2 = json.loads(capsys.readouterr().out)
    assert cap2['snapshot_id'] != cap1['snapshot_id']

    assert tare_ops.main(['skills', 'list', '--json']) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 2

    assert tare_ops.main(['skills', 'show', 'cli-skill', cap1['snapshot_id'][:8], '--file', 'SKILL.md']) == 0
    assert 'Version 1.' in capsys.readouterr().out

    assert tare_ops.main(['skills', 'diff', 'cli-skill', '--json']) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff['changed'] is True
    assert '-Version 1.' in diff['modified'][0]['unified_diff']
    assert '+Version 2.' in diff['modified'][0]['unified_diff']


def test_rewritten_manifest_with_matching_new_hashes_is_rejected_under_original_id(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'demo'
    _write(src, 'SKILL.md', '---\nname: Demo\n---\nOriginal.\n')
    captured = bundles.capture_bundle(src, storage_root=storage)
    snap_dir = Path(captured['snapshot_dir'])

    new_bytes = b'---\nname: Demo\n---\nRewritten content.\n'
    (snap_dir / 'files' / 'SKILL.md').write_bytes(new_bytes)
    manifest = json.loads((snap_dir / 'manifest.json').read_text(encoding='utf-8'))
    manifest['files'][0]['sha256'] = _digest(new_bytes)
    manifest['files'][0]['size_bytes'] = len(new_bytes)
    manifest['bundle_sha256'] = bundles._compute_bundle_sha256(
        manifest['files'],
        complete_directory=manifest['scope']['complete_directory'],
        truncated=manifest['summary']['truncated'],
        include=manifest['scope']['include'],
        skipped=manifest['skipped'],
        excluded_dirs=manifest['excluded_dirs'],
        outside_filter=manifest['outside_filter'],
    )
    (snap_dir / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    with pytest.raises(ValueError, match='directory identity does not match'):
        bundles.load_snapshot('demo', captured['snapshot_id'], storage_root=storage)


def test_recapture_after_adding_oversized_file_updates_coverage_honestly(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'demo'
    _write(src, 'SKILL.md', '---\nname: Demo\n---\nBody.\n')
    first = bundles.capture_bundle(src, max_file_bytes=512, storage_root=storage)
    assert first['scope']['complete_directory'] is True

    _write(src, 'large.bin', b'x' * 2048)
    second = bundles.capture_bundle(src, max_file_bytes=512, storage_root=storage)
    assert second['already_captured'] is False
    assert second['snapshot_id'] != first['snapshot_id']
    assert second['scope']['complete_directory'] is False
    assert second['skipped'] == [{'path': 'large.bin', 'kind': 'file', 'size_bytes': 2048, 'reason': 'exceeds_file_bytes'}]


def test_reference_inside_depth_skipped_directory_is_not_inspected_not_missing(tmp_path):
    src = tmp_path / 'deep'
    _write(src, 'SKILL.md', '---\nname: Deep\n---\nSee [deep guide](a/b/guide.md).\n')
    _write(src, 'a/b/guide.md', '# Deep guide\n')
    report = bundles.inspect_bundle(src, max_depth=1)
    assert any(s['path'] == 'a/b' and s['reason'] == 'exceeds_depth' for s in report['skipped'])
    refs = {r['normalized']: (r['status'], r['reason']) for r in report['references']}
    assert refs['a/b/guide.md'] == ('not_inspected', 'exceeds_depth')


def test_storage_junction_or_symlink_cannot_redirect_capture_writes(tmp_path):
    storage = tmp_path / 'storage'
    outside = tmp_path / 'outside'
    outside.mkdir()
    target_parent = storage / 'serve' / 'skills' / 'snapshots'
    target_parent.mkdir(parents=True)
    link = target_parent / 'demo'
    if os.name == 'nt':
        res = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                             capture_output=True, text=True, check=False)
        if res.returncode:
            pytest.skip(f'Junction creation unavailable: {res.stderr}')
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f'Symlinks unavailable: {exc}')
    src = tmp_path / 'demo'
    _write(src, 'SKILL.md', '---\nname: Demo\n---\nHello.\n')
    try:
        with pytest.raises(ValueError, match='Linked|reparse'):
            bundles.capture_bundle(src, storage_root=storage)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()


def test_diff_rereads_through_pinned_verified_handles(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    src = tmp_path / 'demo'
    _write(src, 'SKILL.md', '---\nname: Demo\n---\nV1.\n')
    v1 = bundles.capture_bundle(src, storage_root=storage)
    _write(src, 'SKILL.md', '---\nname: Demo\n---\nV2.\n')
    v2 = bundles.capture_bundle(src, storage_root=storage)

    calls = []
    real_reader = bundles._read_verified_stored_file

    def tracked_reader(files_root, entry, **kwargs):
        calls.append(entry['path'])
        return real_reader(files_root, entry, **kwargs)

    monkeypatch.setattr(bundles, '_read_verified_stored_file', tracked_reader)
    report = bundles.diff_snapshots('demo', v1['snapshot_id'], v2['snapshot_id'], storage_root=storage)
    assert report['changed'] is True
    assert calls.count('SKILL.md') >= 2

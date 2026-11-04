"""Destructive cleanup must protect work, changed inputs and rollback."""
import base64
import csv
import json
import os
from pathlib import Path
import sys
import time

import pytest

from relay import native_product as product, storage
from test_native_product import bundle


def release(home, source, version):
    import shutil
    b = bundle(source, version)
    identifier = product.digest(b/'bundle.json')
    dest = product.release_at(home, identifier)
    shutil.copytree(b, dest/'bundle')
    site = dest/'venv'/('Lib/site-packages' if os.name == 'nt' else 'lib/python3.12/site-packages')
    site.mkdir(parents=True)
    (site/'sample.py').write_bytes(b'# installed\n')
    record = site/'sample.dist-info/RECORD'
    record.parent.mkdir()
    encoded = base64.urlsafe_b64encode(bytes.fromhex(product.digest(site/'sample.py'))).decode().rstrip('=')
    with record.open('w', newline='') as stream:
        csv.writer(stream).writerows([['sample.py', 'sha256='+encoded, '12'], ['sample.dist-info/RECORD', '', '']])
    product.write_json(dest/'ready.json', {'bundle': identifier})
    return identifier, dest


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root, home = tmp_path/'storage', tmp_path/'product'
    root.mkdir(); home.mkdir()
    monkeypatch.setattr(storage, 'processes', lambda: [])
    monkeypatch.setattr(storage, 'system_sources', lambda: [])
    old, old_path = release(home, tmp_path/'old', 'old')
    previous, _ = release(home, tmp_path/'previous', 'previous')
    current, _ = release(home, tmp_path/'current', 'current')
    product.write_json(home/'active.json', {'current': current, 'previous': previous})
    build = root/'t/build'
    for name in ('wheels-1', 'wheels-2'):
        (build/name).mkdir(parents=True)
        (build/name/'package.whl').write_bytes(b'wheel')
    product.write_json(build/'manifest.json', dict(schema='tare.tools/native-release/1',
        status='REPRODUCIBLE_WHEELS', packages=[dict(wheel='package.whl', sha256=product.digest(build/'wheels-1/package.whl'))]))
    return root, home, old_path, build


def candidates(report, kind=None):
    return [r['id'] for r in report['rows'] if r['eligible'] and (kind is None or r['kind'] == kind)]


def test_preview_is_read_only_and_cleanup_keeps_evidence_active_versions_and_first_wheel(owned):
    root, home, old, build = owned
    evidence = root/'evidence-store'; evidence.mkdir(); (evidence/'only-copy.json').write_text('proof')
    session = root/'agent-sessions'; session.mkdir(); (session/'live.json').write_text('running')
    active = (home/'active.json').read_bytes()
    report = storage.scan(root, home)
    assert len(candidates(report)) == 2 and old.exists()
    assert sum(r['bytes'] for r in report['rows'] if r['origin'] == 'Builds and temporary files') == sum(p.stat().st_size for p in build.rglob('*') if p.is_file())
    result = storage.cleanup(report, candidates(report))
    assert all(r['status'] == 'removed' for r in result['items'])
    assert not old.exists() and not (build/'wheels-2/package.whl').exists()
    assert (build/'wheels-1/package.whl').exists()
    assert (home/'active.json').read_bytes() == active
    assert (evidence/'only-copy.json').read_text() == 'proof'
    assert (session/'live.json').read_text() == 'running'
    assert json.loads((root/'storage-maintenance/last-cleanup.json').read_text())['removed_bytes'] > 0


@pytest.mark.parametrize('change', ['user_file', 'bundle_user_file', 'environment_user_file', 'modified_package', 'git', 'active_process', 'rollback', 'partial_scan', 'modified_bundle'])
def test_preview_preserves_unsafe_or_unknown_installations(owned, monkeypatch, change):
    root, home, old, build = owned
    if change == 'user_file': (old/'unpublished.txt').write_text('work')
    if change == 'bundle_user_file': (old/'bundle/unpublished.txt').write_text('work')
    if change == 'environment_user_file': (old/'venv/activate').write_text('work')
    if change == 'modified_package': next((old/'venv').rglob('sample.py')).write_text('my fix')
    if change == 'git': (old/'.git').mkdir()
    if change == 'active_process': monkeypatch.setattr(storage, 'processes', lambda: [(123, os.path.normcase(str(old/'venv/python')))])
    if change == 'rollback': product.write_json(home/'active.json', dict(current=product.state(home)['current'], previous=product.digest(old/'bundle/bundle.json')))
    if change == 'modified_bundle': (old/'bundle/requirements.lock').write_text('changed')
    report = storage.scan(root, home, seconds=0 if change == 'partial_scan' else 90)
    row = next(r for r in report['rows'] if r['path'] == str(old))
    assert not row['eligible'] and row['reason']
    assert old.exists()


@pytest.mark.parametrize('change', ['file', 'activation', 'process', 'first_wheel'])
def test_changes_after_preview_block_removal(owned, monkeypatch, change):
    root, home, old, build = owned
    report = storage.scan(root, home)
    kind = 'wheel' if change == 'first_wheel' else 'release'
    if change == 'file': (old/'new-work.txt').write_text('preserve')
    if change == 'activation': product.write_json(home/'active.json', dict(current=product.digest(old/'bundle/bundle.json'), previous=product.state(home)['previous']))
    if change == 'process': monkeypatch.setattr(storage, 'processes', lambda: [(456, os.path.normcase(str(old)))])
    if change == 'first_wheel': (build/'wheels-1/package.whl').write_bytes(b'changed')
    result = storage.cleanup(report, candidates(report, kind))
    assert result['removed_bytes'] == 0 and result['items'][0]['status'] == 'kept_or_partial'
    assert old.exists() and (build/'wheels-2/package.whl').exists()


def test_symlink_and_escaped_metadata_are_preserved(owned):
    root, home, old, build = owned
    target = root/'outside'; target.write_text('keep')
    try: (old/'link').symlink_to(target)
    except OSError: pytest.skip('Host does not permit symlinks')
    report = storage.scan(root, home)
    assert not candidates(report, 'release')
    assert target.read_text() == 'keep'


def test_apply_rejects_tampered_preview_and_protected_selection(owned):
    root, home, old, build = owned
    report = storage.scan(root, home)
    protected = next(r['id'] for r in report['rows'] if not r['eligible'])
    with pytest.raises(ValueError, match='protected'):
        storage.cleanup(report, [protected])
    report['home'] = str(root)
    with pytest.raises(ValueError, match='Preview changed'):
        storage.cleanup(report, candidates(report))
    assert old.exists()


def test_installer_lock_serializes_cleanup(owned):
    root, home, old, build = owned
    report = storage.scan(root, home)
    with product.exclusive(home):
        with pytest.raises(OSError): storage.cleanup(report, candidates(report))
    assert old.exists()


def test_crash_during_removal_keeps_receipt_and_never_retries_unknown_directory(owned, monkeypatch):
    root, home, old, build = owned
    report = storage.scan(root, home)
    def crash(path): raise OSError('simulated interruption')
    monkeypatch.setattr(storage.shutil, 'rmtree', crash)
    result = storage.cleanup(report, candidates(report, 'release'))
    tomb = Path(result['items'][0]['retirement_path'])
    assert tomb.exists() and result['removed_bytes'] == 0
    following = storage.scan(root, home)
    assert not next(r for r in following['rows'] if r['path'] == str(tomb))['eligible']


def test_low_space_warning_is_read_only(owned, monkeypatch):
    from collections import namedtuple
    root, home, old, build = owned
    usage = namedtuple('Usage', 'total used free')
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: usage(100*storage.GIB, 95*storage.GIB, 5*storage.GIB))
    assert storage.low_space(root, home) == 5*storage.GIB
    assert old.exists()


def test_low_space_stays_in_the_header_and_only_critical_space_notifies(tmp_path, monkeypatch):
    import asyncio
    from collections import namedtuple
    from textual.widgets import Static
    from relay import relay_mesh
    from relay.console_backend import Backend
    from relay.native_console import WorkConsole
    usage, free = namedtuple('Usage', 'total used free'), [21.8]
    monkeypatch.setattr(storage.shutil, 'disk_usage',
                        lambda p: usage(500*storage.GIB, 0, int(free[0]*storage.GIB)))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'no-control-graph')
    project = tmp_path/'project'; project.mkdir()

    async def scenario():
        app = WorkConsole(project, backend=Backend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        shown = []
        app.notify = lambda message, **kwargs: shown.append(message)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            header = lambda: str(app.query_one('#session-context', Static).render())
            assert 'Low disk: 21.8 GiB free · /storage' in header() and shown == []
            free[0] = 4.0; app.check_storage_space(); app.check_storage_space(); await pilot.pause()
            assert 'Low disk: 4.0 GiB free' in header() and len(shown) == 1 and 'Disk almost full' in shown[0]
            free[0] = 80.0; app.check_storage_space(); await pilot.pause()
            assert 'Low disk' not in header() and len(shown) == 1
    asyncio.run(scenario())


def tools_tree(tmp_path, monkeypatch):
    """A home, temp and local folder holding what other tools leave behind, old and recent."""
    home, temp, local = tmp_path/'home', tmp_path/'temp', tmp_path/'local'
    old = time.time() - 30*86400

    def made(path, *, age=old, folder=False):
        target = path/'f.txt' if folder else path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'x'*100)
        os.utime(path, (age, age))
        return path
    tree = dict(old_log=made(home/'.codex/sessions/2026/09/01/rollout-a.jsonl'),
                new_log=made(home/'.codex/sessions/2026/10/03/rollout-b.jsonl', age=time.time()),
                agy_log=made(home/'.gemini/antigravity-cli/log/cli-1.log'),
                old_version=made(home/'.codex/packages/standalone/releases/0.1', folder=True),
                current_version=made(home/'.codex/packages/standalone/releases/0.2', folder=True),
                old_tests=made(temp/'pytest-of-someone', folder=True),
                new_tests=made(temp/'pytest-of-today', folder=True, age=time.time()),
                transcript=made(home/'.claude/projects/p/s.jsonl'))
    link = home/'.codex/packages/standalone/current'  # Codex points it at the version it runs
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(tree['current_version']), str(link))
    else:
        link.symlink_to(tree['current_version'])
    monkeypatch.setattr(storage, 'tool_roots', lambda: (home, temp, local))
    monkeypatch.setattr(storage.shutil, 'which', lambda name: None)  # the real npm cache stays out of tests
    return tree


def test_old_tool_leftovers_are_candidates_and_current_or_recent_files_are_kept(owned, tmp_path, monkeypatch):
    root, home, _, _ = owned
    tree = tools_tree(tmp_path, monkeypatch)
    report = storage.scan(root, home, days=7)
    rows = {r['origin']: r for r in report['rows'] if r['kind'] in storage.TOOL_KINDS}
    codex = rows['Codex CLI session logs older than 7 days']
    assert codex['eligible'] and codex['items'] == [str(tree['old_log'])]
    assert rows['Codex standalone versions other than the current one']['items'] == [str(tree['old_version'])]
    assert rows['pytest temporary folders older than 7 days']['items'] == [str(tree['old_tests'])]
    assert not any(str(tree['transcript']) in r['items'] for r in rows.values())  # they back Claude Code's /resume
    os.utime(tree['old_log'])  # written again after the preview: no longer due
    result = storage.cleanup(report, [r['id'] for r in rows.values()])
    assert tree['old_log'].exists() and tree['new_log'].exists() and tree['current_version'].exists()
    assert not tree['old_version'].exists() and not tree['old_tests'].exists() and not tree['agy_log'].exists()
    assert tree['new_tests'].exists() and tree['transcript'].exists()
    assert {i['path']: i['status'] for i in result['items']}[codex['path']] == 'kept_or_partial'


def test_a_package_cache_is_cleared_by_its_own_command(owned, tmp_path, monkeypatch):
    import shutil
    import subprocess
    root, home, _, _ = owned
    tools_tree(tmp_path, monkeypatch)
    cache = tmp_path/'local'/'pip'/'Cache'
    cache.mkdir(parents=True)
    (cache/'wheel').write_bytes(b'x'*1000)
    ran = []

    def run(command, **kw):
        ran.append(command)
        shutil.rmtree(cache)
        cache.mkdir()
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(storage.subprocess, 'run', run)
    report = storage.scan(root, home, days=7)
    row = next(r for r in report['rows'] if r['kind'] == 'cache')
    result = storage.cleanup(report, [row['id']])
    assert ran[0][-3:] == ['pip', 'cache', 'purge'] and result['removed_bytes'] == 1000


def test_automatic_cleanup_runs_only_when_opted_in_and_due(owned, tmp_path, monkeypatch):
    import subprocess
    root, home, _, _ = owned
    tree = tools_tree(tmp_path, monkeypatch)
    assert storage.auto_cleanup(root, home) == {'status': 'off'} and tree['old_log'].exists()
    storage.save_auto_settings(enabled=True, days=7)
    ran = storage.auto_cleanup(root, home)
    assert ran['status'] == 'ran' and not tree['old_log'].exists() and tree['new_log'].exists()
    assert (root/'storage-maintenance'/'last-auto-cleanup.json').is_file() and storage.auto_settings()['last_run']
    calls = []  # the product updater, every few minutes
    monkeypatch.setattr(product, 'release_command', lambda home, argv: (calls.append(argv) or ['tare'], {}))
    monkeypatch.setattr(product.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"status": "ran"}', ''))
    assert product.auto_storage(home) == 'not due' and not calls
    storage.save_auto_settings(last_run=0)
    assert product.auto_storage(home) == '{"status": "ran"}' and calls == [['storage', 'auto']]
    storage.save_auto_settings(enabled=False)
    assert product.auto_storage(home) == 'off'

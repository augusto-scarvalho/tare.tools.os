import importlib.util
from pathlib import Path
import subprocess
import json
import pytest

spec = importlib.util.spec_from_file_location('publish_product',
    Path(__file__).resolve().parents[1]/'scripts/ops/publish_product.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def test_export_rule_change_triggers_a_bundle_without_runtime_source_changes(monkeypatch):
    path=publisher.TEMPLATE+'fixture.json'
    monkeypatch.setattr(publisher, 'git', lambda *args: (path+'\nCHANGELOG.md\n').encode())
    lock={'sources':[{'id':'os','commit':'b'*40,'include':['pyproject.toml','relay/*.py']}]}
    changes=publisher.packaged_changes({'os':'fixture'}, {'os':'a'*40}, lock)
    assert changes==['os:'+path]


def test_publisher_bootstrap_uses_one_committed_tool_set_and_preserves_arguments(tmp_path,monkeypatch):
    commit='a'*40;calls=[]
    monkeypatch.setattr(publisher,'mirror',lambda state,source:('owned-os-mirror',commit))
    def git(root,*args):
        assert root=='owned-os-mirror' and args[0]=='show' and args[1].startswith(commit+':')
        return ('# committed '+args[1]).encode()
    monkeypatch.setattr(publisher,'git',git)
    monkeypatch.setattr(publisher.subprocess,'call',lambda argv:calls.append(argv) or 0)
    assert publisher.refresh_tools(tmp_path,force=True,peers=['operator@host'])==0
    assert sorted(p.name for p in (tmp_path/'tools').iterdir())==[
        'build_native_release.py','pack_native_release.py','publish_product.py']
    assert calls==[[publisher.sys.executable,str(tmp_path/'tools/publish_product.py'),
        '--state',str(tmp_path),'--tooling-commit',commit,'--force','--peer','operator@host']]
    assert all(p.read_bytes().startswith(b'# committed '+commit.encode()) for p in (tmp_path/'tools').iterdir())


def test_publisher_refuses_code_and_source_revision_drift_before_building(tmp_path,monkeypatch):
    monkeypatch.setattr(publisher,'mirror',lambda state,name:(name,'b'*40))
    monkeypatch.setattr(publisher,'lock_for',lambda *args:{'sources':[{'id':'os','commit':'b'*40}]})
    def forbidden(*args):raise AssertionError('No build starts after source drift.')
    monkeypatch.setattr(publisher,'build_python',forbidden)
    with pytest.raises(ValueError,match='source moved'):
        publisher.publish(tmp_path,tooling_commit='a'*40)


def test_release_uses_os_qualified_backlog_pin_instead_of_unvalidated_main(tmp_path):
    roots = {name: tmp_path/name for name in ('os', 'backlog')}
    def git(name, *args):
        return subprocess.check_output(['git', '-C', str(roots[name]), '-c', 'user.name=t',
            '-c', 'user.email=t@t', *args]).decode().strip()
    for root in roots.values():
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
    def commit(name):
        git(name, 'add', '-A')
        git(name, 'commit', '-qm', 'fixture')
        return git(name, 'rev-parse', 'HEAD')
    (roots['backlog']/'pyproject.toml').write_text('[project]\nversion="1.2.0"\n')
    qualified = commit('backlog')
    (roots['backlog']/'pyproject.toml').write_text('[project]\nversion="1.3.0"\n')
    newer = commit('backlog')
    (roots['os']/'pyproject.toml').write_text('[project]\nversion="0.1.71"\n')
    locks = roots['os']/'scripts/ops/release_inputs'
    locks.mkdir(parents=True)
    (locks/'PRODUCT_RELEASE_INPUTS_WIN312_fixture.json').write_text(json.dumps({
        'sources': [{'id':'os','include':['pyproject.toml']},
                    {'id':'backlog','include':['pyproject.toml','src/**']}]}))
    git('os', 'update-index', '--add', '--cacheinfo', '160000,'+qualified+',satellites/tare.tools.backlog-graph')
    # Keep the synthetic gitlink in the index; there is no submodule checkout.
    git('os', 'add', 'pyproject.toml', 'scripts')
    git('os', 'commit', '-qm', 'qualified composition')
    os_commit = git('os', 'rev-parse', 'HEAD')
    lock = publisher.lock_for(roots, {'os':os_commit,'backlog':newer})
    selected = {source['id']:source for source in lock['sources']}
    assert selected['backlog']['commit'] == qualified
    assert selected['backlog']['version'] == '1.2.0'
    assert selected['os']['commit'] == os_commit


@pytest.mark.parametrize('fault', ['missing', 'blob', 'unavailable', 'wrong-object'])
def test_release_refuses_invalid_or_unavailable_satellite_pin(monkeypatch, fault):
    os_commit, pin = 'a'*40, 'b'*40
    template_path = publisher.TEMPLATE+'fixture.json'
    def git(root, *args):
        if args[0] == 'ls-tree' and '--name-only' in args:
            return (template_path+'\n').encode()
        if args[0] == 'ls-tree':
            if fault == 'missing':
                return b''
            kind = '100644 blob' if fault == 'blob' else '160000 commit'
            return (kind+' '+pin+'\tsatellites/tare.tools.backlog-graph\n').encode()
        if args[0] == 'cat-file':
            if fault == 'unavailable':
                raise subprocess.CalledProcessError(128, ['git', *args])
            return b'blob\n'
        if args[0] == 'show' and args[1].endswith(template_path):
            return json.dumps({'sources':[{'id':'os'},{'id':'backlog'}]}).encode()
        if args[0] == 'show' and args[1].endswith(':pyproject.toml'):
            return b'[project]\nversion="0.1.71"\n'
        raise AssertionError(args)
    monkeypatch.setattr(publisher, 'git', git)
    with pytest.raises(ValueError, match='satellite_pin'):
        publisher.lock_for({'os':'os-mirror','backlog':'backlog-mirror'},
                           {'os':os_commit,'backlog':'c'*40})


def test_ignored_remote_main_does_not_hide_later_qualified_pin_change(tmp_path, monkeypatch):
    import hashlib
    selected = {key:hashlib.sha1(key.encode()).hexdigest() for key in publisher.REPOS}
    remote = {**selected, 'backlog':'b'*40}
    previous = dict(selected)
    state = tmp_path/'publisher'
    state.mkdir()
    marker = state/'published.json'
    marker.write_text(json.dumps({'heads':previous,'tag':'product-qualified'}))
    original = marker.read_bytes()
    monkeypatch.setattr(publisher, 'mirror', lambda state,key: (key+'-mirror',remote[key]))
    monkeypatch.setattr(publisher, 'lock_for', lambda mirrors,heads: {
        'sources':[{'id':key,'commit':value} for key,value in selected.items()], 'build_tools':{}})
    seen = []
    def changes(mirrors, before, lock):
        seen.append((dict(before), {row['id']:row['commit'] for row in lock['sources']}))
        return ['backlog:src/new.py']
    monkeypatch.setattr(publisher, 'packaged_changes', changes)
    def build(*args):
        raise RuntimeError('build_boundary')  # Stop before any actual build, download or publication.
    monkeypatch.setattr(publisher, 'build_python', build)
    assert publisher.publish(state) == {'status':'unchanged','tag':'product-qualified'}
    assert marker.read_bytes() == original and seen == []
    selected.update(os='c'*40, backlog=remote['backlog'])
    remote['os'] = selected['os']
    with pytest.raises(RuntimeError, match='build_boundary'):
        publisher.publish(state)
    assert seen == [(previous, selected)]
    assert marker.read_bytes() == original


def test_only_changes_to_packaged_files_make_a_release(tmp_path):
    repo = tmp_path/'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    def commit():
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'x'],
                       check=True)
        return publisher.git(repo, 'rev-parse', 'HEAD').decode().strip()
    (repo/'src').mkdir()
    (repo/'src'/'a.py').write_text('a = 1\n')
    (repo/'.githooks').mkdir()
    (repo/'.githooks'/'pre-push').write_text('#!/bin/sh\n')
    subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
    first = commit()
    (repo/'.githooks'/'pre-push').write_text('#!/bin/sh\necho hi\n')
    hook_only = commit()
    (repo/'src'/'a.py').write_text('a = 2\n')
    code = commit()
    lock = lambda head: {'sources': [{'id': 'x', 'commit': head, 'include': ['pyproject.toml', 'src/**']}]}
    assert publisher.packaged_changes({'x': repo}, {'x': first}, lock(hook_only)) == []
    assert publisher.packaged_changes({'x': repo}, {'x': hook_only}, lock(code)) == ['x:src/a.py']


def test_a_publish_starts_the_updates_at_once_here_and_on_reachable_peers(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if 'offline@host' in argv:
            raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(publisher.subprocess, 'run', run)
    started = publisher.notify(['augus@node', 'offline@host'])
    assert started == {'local': True, 'augus@node': True, 'offline@host': False}
    assert calls[0] == ['schtasks', '/Run', '/TN', 'Tare-Update']
    assert calls[1][-2:] == ['augus@node', 'schtasks /Run /TN Tare-Update'] and 'BatchMode=yes' in calls[1]


@pytest.mark.parametrize('failure', ['os', 'timeout', 'nonzero'])
@pytest.mark.parametrize('target', ['local', 'bad@host'])
def test_notification_failure_is_independent_and_bounded(monkeypatch, failure, target):
    calls = []
    def run(argv, **kw):
        name = 'local' if argv[0] == 'schtasks' else argv[-2]
        calls.append(name)
        assert 0 < kw.get('timeout', 0) <= 60
        if name == target:
            if failure == 'os':
                raise OSError('unavailable executable')
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(argv, 60)
            return subprocess.CompletedProcess(argv, 1)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(publisher.subprocess, 'run', run)
    assert publisher.notify(['bad@host', 'good@host']) == {
        'local': target != 'local', 'bad@host': target != 'bad@host', 'good@host': True}
    assert calls == ['local', 'bad@host', 'good@host']


def kernel_ripgrep_pin():
    """The kernel's tools/install_test_ripgrep.py: the satellite or a sibling checkout."""
    here = Path(__file__).resolve().parents[1]
    for root in (here/'satellites'/'tare.tools.kernel', here.parent/'tare.tools.kernel'):
        source = root/'tools'/'install_test_ripgrep.py'
        if source.is_file():
            return source.read_text(encoding='utf-8')
    return None


def test_the_bundled_ripgrep_pin_is_the_kernels_test_pin():
    import re
    import pytest
    text = kernel_ripgrep_pin()
    if text is None:
        pytest.skip('no kernel checkout (satellite or sibling)')
    pins = re.findall(r"\('(ripgrep-[^']+)',\s*'([0-9a-f]{64})',\s*'([^']+)'\)", text)
    assert sorted(pins) == sorted(publisher.RIPGREP.values())
    assert f'/releases/download/{publisher.RIPGREP_VERSION}/' in text


def ripgrep_archive(platform):
    import io
    import tarfile
    import zipfile
    asset, _, executable = publisher.RIPGREP[platform]
    folder = asset.removesuffix('.zip').removesuffix('.tar.gz')
    buffer = io.BytesIO()
    if asset.endswith('.zip'):
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr(f'{folder}/{executable}', b'rg-binary')
            archive.writestr(f'{folder}/README.md', b'docs')
    else:
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            info = tarfile.TarInfo(f'{folder}/{executable}')
            info.size = 9
            archive.addfile(info, io.BytesIO(b'rg-binary'))
    return buffer.getvalue()


def test_fetch_ripgrep_refuses_other_bytes_and_extracts_only_the_executable(tmp_path, monkeypatch):
    import hashlib
    import io
    import pytest
    for platform in ('nt', 'posix'):
        raw = ripgrep_archive(platform)
        asset, _, executable = publisher.RIPGREP[platform]
        urls = []
        opener = lambda url, timeout: urls.append(url) or io.BytesIO(raw)
        with pytest.raises(ValueError, match='digest_mismatch'):
            publisher.fetch_ripgrep(tmp_path/platform, platform, opener)  # the fixture is not the pinned archive
        assert not (tmp_path/platform).exists()
        monkeypatch.setitem(publisher.RIPGREP, platform, (asset, hashlib.sha256(raw).hexdigest(), executable))
        target = publisher.fetch_ripgrep(tmp_path/platform, platform, opener)
        assert target.read_bytes() == b'rg-binary'
        assert [p.name for p in (tmp_path/platform).iterdir()] == [executable]
        assert urls[-1].endswith(f'/{publisher.RIPGREP_VERSION}/{asset}')


def test_packed_tools_are_hashed_in_the_bundle(tmp_path, monkeypatch):
    import hashlib
    import json
    spec = importlib.util.spec_from_file_location('pack_native_release',
        Path(__file__).resolve().parents[1]/'scripts/ops/pack_native_release.py')
    packer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packer)
    monkeypatch.setattr(packer.subprocess, 'run', lambda *a, **kw: None)  # no project wheels to download here
    release = tmp_path/'release'
    release.mkdir()
    (release/'manifest.json').write_text(json.dumps({'status': 'SINGLE_BUILD_WHEELS', 'packages': []}))
    installer = tmp_path/'install.py'
    installer.write_text('# installer')
    rg = tmp_path/'rg.exe'
    rg.write_bytes(b'rg-binary')
    native_git=tmp_path/'git';(native_git/'cmd').mkdir(parents=True)
    (native_git/'cmd/git.exe').write_bytes(b'git-runtime')
    packer.pack(release, tmp_path/'bundle', installer, [rg,native_git])
    files = json.loads((tmp_path/'bundle'/'bundle.json').read_text())['files']
    assert files['tools/rg.exe'] == hashlib.sha256(b'rg-binary').hexdigest()
    assert files['tools/git/cmd/git.exe'] == hashlib.sha256(b'git-runtime').hexdigest()


@pytest.mark.parametrize('unsafe',[False,True])
def test_native_git_pin_and_safe_extraction(tmp_path,monkeypatch,unsafe):
    import hashlib,io,zipfile
    source=io.BytesIO()
    with zipfile.ZipFile(source,'w') as archive:
        archive.writestr('ucrt64/bin/ash.exe',b'native shell stub')
        archive.writestr('cmd/git.exe',b'native git')
        if unsafe:archive.writestr('../outside.exe',b'forbidden')
    raw=source.getvalue();opener=lambda *a,**kw:io.BytesIO(raw)
    target=tmp_path/'git'
    with pytest.raises(ValueError,match='digest_mismatch'):publisher.fetch_mingit(target,opener)
    assert not target.exists()
    monkeypatch.setattr(publisher,'MINGIT_SHA256',hashlib.sha256(raw).hexdigest())
    if unsafe:
        with pytest.raises(ValueError,match='unsafe_path'):publisher.fetch_mingit(target,opener)
        assert not target.exists() and not (tmp_path/'outside.exe').exists()
    else:
        assert publisher.fetch_mingit(target,opener)==target
        assert (target/'ucrt64/bin/sh.exe').read_bytes()==b'native shell stub'
        assert json.loads((target/'tare-source.json').read_text())['sha256']==hashlib.sha256(raw).hexdigest()

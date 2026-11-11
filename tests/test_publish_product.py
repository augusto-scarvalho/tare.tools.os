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
    monkeypatch.setattr(publisher, 'ci_passed', lambda commit: 'https://ci/runs/1')
    assert publisher.publish(state) == {'status':'unchanged','tag':'product-qualified'}
    assert marker.read_bytes() == original and seen == []
    selected.update(os='c'*40, backlog=remote['backlog'])
    remote['os'] = selected['os']
    with pytest.raises(RuntimeError, match='build_boundary'):
        publisher.publish(state)
    assert seen == [(previous, selected)]
    assert marker.read_bytes() == original


def test_a_release_waits_until_ci_passed_on_main_for_its_os_commit(tmp_path, monkeypatch):
    import hashlib
    from relay import native_product
    assert publisher.CI_PASSED == native_product.CI_PASSED  # the line installed copies look for in the notes
    heads = {key: hashlib.sha1(key.encode()).hexdigest() for key in publisher.REPOS}
    state = tmp_path/'publisher'
    state.mkdir()
    monkeypatch.setattr(publisher, 'mirror', lambda state, key: (key+'-mirror', heads[key]))
    monkeypatch.setattr(publisher, 'lock_for', lambda mirrors, h: {
        'sources': [{'id': key, 'commit': value} for key, value in heads.items()], 'build_tools': {}})
    monkeypatch.setattr(publisher, 'build_python', lambda *a: pytest.fail('built before CI passed'))
    runs = [{'conclusion': None, 'headBranch': 'main', 'url': 'https://ci/runs/2'},          # still running
            {'conclusion': 'success', 'headBranch': 'dev', 'url': 'https://ci/runs/1'}]      # green elsewhere
    asked = []
    def run(argv, **kw):
        asked.append(argv[argv.index('--commit') + 1])
        assert argv[argv.index('--event') + 1] == 'push'  # a pull request's branch named main is not main
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(runs))
    monkeypatch.setattr(publisher.subprocess, 'run', run)
    assert publisher.publish(state) == {'status': 'waiting for CI', 'commit': heads['os'], 'tag': None}
    assert asked == [heads['os']] and not (state/'published.json').exists()  # unrecorded: the next run asks again
    runs[0]['conclusion'] = 'success'
    assert publisher.ci_passed(heads['os']) == 'https://ci/runs/2'


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


def test_only_ci_publishes_unless_told_here(tmp_path, monkeypatch, capsys):
    # The maintainers' scheduled task runs main's publisher: once CI publishes, it must not publish too.
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)  # under CI this would write the test job's own outputs
    monkeypatch.setattr(publisher, 'publish', lambda *a, **k: pytest.fail('published outside CI'))
    assert publisher.main(['--state', str(tmp_path)]) == 0
    assert 'CI publishes releases' in capsys.readouterr().out
    monkeypatch.setattr(publisher, 'publish', lambda state, **k: {'status': 'published', **k})
    assert publisher.main(['--state', str(tmp_path), '--here', '--commit', 'c'*40]) == 0
    assert '"commit": "' + 'c'*40 in capsys.readouterr().out


def test_ci_publishes_only_a_commit_of_main(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, 'mirror', lambda state, key: (key+'-mirror', 'a'*40))
    monkeypatch.setattr(publisher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess(argv, 1))
    monkeypatch.setattr(publisher, 'lock_for', lambda *a: pytest.fail('a commit off main went further'))
    with pytest.raises(ValueError, match='not on main'):
        publisher.publish(tmp_path, commit='b'*40)


def test_ci_never_publishes_a_commit_older_than_the_newest_release(tmp_path, monkeypatch):
    # Two merges in a row: the second one's CI can finish and publish first.
    newest, older = 'n'*40, 'o'*40
    (tmp_path/'published.json').write_text(json.dumps({'heads': {'os': newest}, 'tag': 'product-newest'}))
    monkeypatch.setattr(publisher, 'mirror', lambda state, key: (key+'-mirror', newest))
    monkeypatch.setattr(publisher, 'lock_for', lambda mirrors, heads: {'sources': [{'id': 'os', 'commit': heads['os']}]})
    monkeypatch.setattr(publisher, 'ci_passed', lambda commit: pytest.fail('went on to publish'))
    on_main = lambda argv, **k: subprocess.CompletedProcess(argv, 0 if argv[-1] == 'refs/heads/main' else 1)
    monkeypatch.setattr(publisher.subprocess, 'run', on_main)
    assert publisher.publish(tmp_path, commit=older) == {
        'status': 'older than the newest release', 'commit': older, 'tag': 'product-newest'}


def test_the_newest_release_tells_ci_what_was_published_last(tmp_path, monkeypatch):
    listed = [{'tagName': 'product-old', 'publishedAt': '2026-11-04T10:00:00Z'},
              {'tagName': 'product-new', 'publishedAt': '2026-11-04T12:00:00Z'},
              {'tagName': 'v1', 'publishedAt': '2026-11-04T13:00:00Z'}]  # not a product release
    calls = []
    def run(argv, **k):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(listed))
    monkeypatch.setattr(publisher.subprocess, 'run', run)
    assert publisher.last_release(tmp_path) == 'product-new'
    download = calls[-1]
    assert download[:4] == ['gh', 'release', 'download', 'product-new']
    assert download[download.index('--pattern')+1] == 'published.json' and str(tmp_path) in download


def release_run(monkeypatch, tmp_path, listed=()):
    """publish() through to `gh release create` with every build step stubbed; returns the commands that ran."""
    heads = {key: 'a'*40 for key in publisher.REPOS}
    monkeypatch.setattr(publisher, 'mirror', lambda state, key: (key+'-mirror', heads[key]))
    monkeypatch.setattr(publisher, 'lock_for', lambda mirrors, h: {'build_tools': {}, 'sources': [
        {'id': key, 'commit': value, 'package': 'p-'+key, 'version': '1.0'} for key, value in heads.items()]})
    monkeypatch.setattr(publisher, 'ci_passed', lambda commit: 'https://ci/runs/1')
    monkeypatch.setattr(publisher, 'build_python', lambda *a: tmp_path/'python')
    monkeypatch.setattr(publisher, 'git', lambda *a: b'# installer')
    monkeypatch.setattr(publisher, 'fetch_ripgrep', lambda destination: destination/'rg')
    monkeypatch.setattr(publisher, 'fetch_mingit', lambda destination: destination)
    monkeypatch.setattr(publisher, 'notify', lambda peers: {})
    calls = []
    def run(argv, **k):
        calls.append(list(argv))
        if argv[:3] == ['gh', 'release', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(list(listed)))
        if any(str(part).endswith('pack_native_release.py') for part in argv):
            bundle = Path(argv[argv.index('--output')+1])
            bundle.mkdir(parents=True)
            (bundle/'bundle.json').write_text('{}')
        return subprocess.CompletedProcess(argv, 0, stdout='')
    monkeypatch.setattr(publisher.subprocess, 'run', run)
    return calls


def test_a_draft_release_is_created_as_a_draft_and_says_so(tmp_path, monkeypatch):
    calls = release_run(monkeypatch, tmp_path)
    record = publisher.publish(tmp_path/'state', draft=True)
    create, = (argv for argv in calls if argv[:3] == ['gh', 'release', 'create'])
    assert '--draft' in create and record['draft'] is True and record['status'] == 'published'
    calls.clear()
    record = publisher.publish(tmp_path/'state2')
    create, = (argv for argv in calls if argv[:3] == ['gh', 'release', 'create'])
    assert '--draft' not in create and 'draft' not in record
    assert not [argv for argv in calls if argv[:3] == ['gh', 'release', 'list']]  # no cleanup without --draft


def test_leftover_product_drafts_are_deleted_and_published_releases_are_not(tmp_path, monkeypatch):
    calls = release_run(monkeypatch, tmp_path, listed=[
        {'tagName': 'product-failed-signing', 'isDraft': True},
        {'tagName': 'product-published', 'isDraft': False},
        {'tagName': 'v1-draft', 'isDraft': True}])  # not a product release: not ours to delete
    publisher.publish(tmp_path/'state', draft=True)
    deleted = [argv for argv in calls if argv[:3] == ['gh', 'release', 'delete']]
    assert [argv[3] for argv in deleted] == ['product-failed-signing'] and '--yes' in deleted[0]
    listing = next(argv for argv in calls if argv[:3] == ['gh', 'release', 'list'])
    assert 'tagName,isDraft' in listing
    assert calls.index(deleted[0]) < next(i for i, argv in enumerate(calls) if argv[:3] == ['gh', 'release', 'create'])


def test_the_newest_release_is_never_a_draft(tmp_path, monkeypatch):
    calls = []
    def run(argv, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout='[]')
    monkeypatch.setattr(publisher.subprocess, 'run', run)
    assert publisher.last_release(tmp_path) is None
    assert '--exclude-drafts' in calls[0]


@pytest.mark.parametrize('result, expected', [
    ({'status': 'published', 'tag': 'product-0123'}, 'published=true\ntag=product-0123\n'),
    ({'status': 'unchanged', 'tag': 'product-old'}, 'published=false\n'),
    ({'status': 'waiting for CI', 'commit': 'c'*40, 'tag': 'product-old'}, 'published=false\n'),
    ({'status': 'no packaged change', 'tag': 'product-old'}, 'published=false\n'),
    ({'status': 'older than the newest release', 'commit': 'c'*40, 'tag': 'product-new'}, 'published=false\n')])
def test_the_workflow_learns_from_github_output_whether_a_release_was_created(
        tmp_path, monkeypatch, result, expected):
    output = tmp_path/'github_output'
    output.write_text('earlier=1\n')  # GITHUB_OUTPUT is shared by the steps: append, never replace
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    seen = {}
    monkeypatch.setattr(publisher, 'publish', lambda state, **k: seen.update(k) or result)
    assert publisher.main(['--state', str(tmp_path), '--draft']) == 0
    assert output.read_text() == 'earlier=1\n' + expected and seen['draft'] is True
    monkeypatch.delenv('GITHUB_OUTPUT')
    assert publisher.main(['--state', str(tmp_path)]) == 0 and seen['draft'] is False

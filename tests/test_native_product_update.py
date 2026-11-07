import json
import subprocess
import zipfile

import pytest

from relay import native_product as product


def write_state(home, current, previous=None):
    home.mkdir(parents=True, exist_ok=True)
    (home/'active.json').write_text(json.dumps({'current': current, 'previous': previous}), encoding='utf-8')


A, B, C = 'a' * 64, 'b' * 64, 'c' * 64


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Protocol-1 server and scheduler adapter; identical fixture on Linux/Windows."""
    home = tmp_path/'product'
    calls, health, installed = [], [], []
    monkeypatch.setattr(product.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(product, 'status', lambda home: product.state(home))
    monkeypatch.setattr(product, '_port_open', lambda: False)
    monkeypatch.setattr(product.WindowsServeTask, 'available', lambda self: True)
    monkeypatch.setattr(product.WindowsServeTask, 'start', lambda self: calls.append('start'))
    monkeypatch.setattr(product, 'serve_health', lambda: health.pop(0) if len(health) > 1 else health[0])
    def control(identity, owner, action):
        calls.append((identity, owner, action))
        return {**server(home, identity['release']), **identity, 'action': action,
                'draining': action != 'resume', 'stopping': action == 'stop'}
    monkeypatch.setattr(product, 'serve_control', control)
    return home, calls, health, installed


def server(home, release, **extra):
    return {'status': 'ok', 'pid': 7, 'release': release, 'installation': product._installation(home),
            'control_protocol': 1, 'busy': 0, 'stopping': False, **extra}


def test_idle_serve_is_drained_and_stopped_before_start(machine):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health += [server(home, A), server(home, B)]
    assert product.restart_serve(home, wait_seconds=0) == 'restarted'
    assert [c[2] for c in calls if isinstance(c, tuple)] == ['drain', 'stop']
    assert calls[-1] == 'start'
    assert calls[0][1] == calls[1][1]
    assert calls[0][0] == product._identity(server(home, A))


@pytest.mark.parametrize('foreign', [{'status': 'ok'}, {'control_protocol': 0},
                                     {'installation': 'foreign'}, {'release': C}, {'pid': None}])
def test_legacy_or_foreign_server_is_never_stopped(machine, foreign):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health.append({**server(home, A), **foreign} if foreign != {'status': 'ok'} else foreign)
    assert product.restart_serve(home, wait_seconds=0).startswith('deferred:')
    assert calls == []


def test_busy_deadline_releases_the_same_owners_lease(machine, monkeypatch):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health.append(server(home, A, busy=2))
    def control(identity, owner, action):
        calls.append((identity, owner, action))
        return {**server(home, A, busy=2), 'action': action, 'draining': action == 'drain'}
    monkeypatch.setattr(product, 'serve_control', control)
    assert 'lease released' in product.restart_serve(home, wait_seconds=0)
    assert [c[2] for c in calls] == ['drain', 'resume']
    assert calls[0][1] == calls[1][1]


@pytest.mark.parametrize('malformed', [None, [], {}, {'busy':0, 'stopping':True, 'action':'stop'}])
def test_malformed_stop_reply_defers_without_starting_another_server(machine, monkeypatch, malformed):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health.append(server(home, A))
    actual = product.serve_control
    def control(identity, owner, action):
        if action == 'stop':
            return malformed
        return actual(identity, owner, action)
    monkeypatch.setattr(product, 'serve_control', control)
    assert product.restart_serve(home, wait_seconds=0).startswith('deferred:')
    assert 'start' not in calls


def test_selected_release_reconciles_before_candidate_lookup(machine, monkeypatch):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health += [server(home, A), server(home, B)]
    monkeypatch.setattr(product, 'latest_published', lambda *a, **k: pytest.fail('replay selected another candidate'))
    assert product.auto_update(home, wait_seconds=0)['serve'] == 'restarted'


@pytest.mark.parametrize('with_receipt', [False, True])
def test_prune_keeps_current_and_previous_and_skips_a_release_in_use(tmp_path, monkeypatch, with_receipt):
    monkeypatch.setattr(product, '_release_process_in_use', lambda release: False)
    home = tmp_path/'product'
    write_state(home, A, B)
    if with_receipt:
        product.write_activation(home, product.state(home), B, 'rollback')
    before = (home/'active.json').read_bytes()
    for name in (A[:16], B[:16], C[:16], 'd' * 16):
        (home/'releases'/name).mkdir(parents=True)
    real_rename = product.os.rename
    monkeypatch.setattr(product.os, 'rename', lambda a, b: (_ for _ in ()).throw(PermissionError())
                        if str(a).endswith('d' * 16) else real_rename(a, b))  # compatibility: rename can still fail independently of registered usage
    product.prune(home)
    assert sorted(p.name for p in (home/'releases').iterdir()) == sorted([A[:16], B[:16], 'd' * 16])
    assert (home/'active.json').read_bytes() == before
    if with_receipt:
        assert product.state(home)['last_transition']['to'] == B
        assert product.state(home)['last_transition']['from'] == A


def test_prune_retries_retired_leftovers_without_touching_selected_or_unknown_folders(tmp_path,monkeypatch):
    monkeypatch.setattr(product, '_release_process_in_use', lambda release: False)
    home=tmp_path/'product';write_state(home,A,B)
    before=(home/'active.json').read_bytes()
    for name in (A[:16],B[:16],A[:16]+'.delete',C[:16],'operator-notes.delete'):
        p=home/'releases'/name;p.mkdir(parents=True);(p/'keep').write_text('bytes')
    original=product.shutil.rmtree;held=True
    def remove(path,**options):
        if held and path.name==C[:16]+'.delete':return  # a locked file remains after retirement
        return original(path,**options)
    monkeypatch.setattr(product.shutil,'rmtree',remove)
    product.prune(home)
    assert (home/'releases'/(C[:16]+'.delete')/'keep').is_file()
    held=False;product.prune(home)
    assert not (home/'releases'/(C[:16]+'.delete')).exists()
    assert sorted(p.name for p in (home/'releases').iterdir())==sorted([A[:16],B[:16],A[:16]+'.delete','operator-notes.delete'])
    assert (home/'active.json').read_bytes()==before


@pytest.mark.parametrize('tombstone', [False, True])
def test_prune_releases_usage_lock_before_recursive_delete(tmp_path, monkeypatch, tombstone):
    monkeypatch.setattr(product, '_release_process_in_use', lambda release: False)
    home = tmp_path/'product'
    write_state(home, A, B)
    release = home/'releases'/(C[:16] + ('.delete' if tombstone else ''))
    release.mkdir(parents=True)
    original = product.shutil.rmtree
    removed = []

    def remove(path, **options):
        assert path.name == C[:16]+'.delete'
        assert not (home/'releases'/C[:16]).exists()
        # A publisher can acquire the lock even while slow deletion is underway.
        with product._usage_lock(home):
            removed.append(path)
        return original(path, **options)

    monkeypatch.setattr(product.shutil, 'rmtree', remove)
    product.prune(home)
    assert removed == [home/'releases'/(C[:16]+'.delete')]
    assert not removed[0].exists()


def test_vs_code_lists_what_serve_publishes_without_touching_anything_else(tmp_path):
    user = tmp_path/'Code'/'User'
    user.mkdir(parents=True)
    models = user/'chatLanguageModels.json'
    other = {'name': 'mine', 'vendor': 'openai', 'models': [{'id': 'gpt'}]}
    mine = {'id': 'tare', 'name': 'tare (agent)', 'url': 'http://old/v1', 'toolCalling': True, 'extra': 1}
    models.write_text(json.dumps([other, {'name': 'tare', 'vendor': 'customendpoint', 'apiKey': 'tare-local',
                                          'apiType': 'chat-completions', 'models': [mine]}]), encoding='utf-8')
    published = [{'id': 'tare', 'context_window': 1000000}, {'id': 'tare-copilot', 'context_window': None},
                 {'id': 'tare-qwen38', 'context_window': 131072}]
    report = product.sync_vscode(published, appdata=tmp_path)
    data = json.loads(models.read_text(encoding='utf-8'))
    assert data[0] == other and report == ['Code: url of tare, added tare-copilot, added tare-qwen38']
    entries = {m['id']: m for m in data[1]['models']}
    assert entries['tare'] == {**mine, 'url': product.SERVE_CHAT}  # the user's values stay
    assert entries['tare-copilot']['vision'] and entries['tare-copilot']['maxInputTokens'] == 131072 - 16384
    assert not entries['tare-qwen38']['vision'] and entries['tare-qwen38']['maxInputTokens'] == 131072 - 16384
    assert len(list(user.glob('chatLanguageModels.json.bak-*'))) == 1
    assert product.sync_vscode(published, appdata=tmp_path) == []  # nothing new: no write, no backup
    assert len(list(user.glob('chatLanguageModels.json.bak-*'))) == 1


def test_files_tare_cannot_safely_edit_are_left_as_they_are(tmp_path):
    for flavour, text in (('Code', '// comments\n[]'),
                          ('Code - Insiders', '[{"name": "tare", "vendor": "someone-else", "models": []}]')):
        (tmp_path/flavour/'User').mkdir(parents=True)
        (tmp_path/flavour/'User'/'chatLanguageModels.json').write_text(text, encoding='utf-8')
    report = product.sync_vscode([{'id': 'tare'}], appdata=tmp_path)
    assert report == ['Code: not plain JSON, left as is', 'Code - Insiders: unexpected structure, left as is']
    assert (tmp_path/'Code'/'User'/'chatLanguageModels.json').read_text(encoding='utf-8') == '// comments\n[]'
    assert product.sync_vscode([{'id': 'tare'}], appdata=tmp_path/'no-vscode') == []  # VS Code not installed


def release(tag, published, passed=True):
    # The publisher writes the CI line into the notes only after CI passed on main for the release's commit.
    return {'tag_name': tag, 'created_at': '2026-10-02T00:29:51Z', 'published_at': published, 'draft': False,
            'body': 'Product bundle for Python 3.12 on Windows: ...'
                    + ('\n\n' + product.CI_PASSED + 'https://ci/runs/7' if passed else '')}


def test_the_newest_publication_wins_when_releases_share_a_commit():
    # Both releases of one OS commit carry that commit's date as their created time.
    listed = [release('product-new', '2026-10-02T00:38:21Z'), release('product-old', '2026-10-02T00:34:02Z')]
    assert product.latest_published('owner/repo', api=lambda path: listed) == 'product-new'


def test_only_a_release_ci_passed_is_installed_and_never_an_older_one():
    listed = [release('product-3', '2026-10-05T03:00:00Z', passed=False),  # no CI line: published before the gate
              release('product-2', '2026-10-05T02:00:00Z'),
              release('product-1', '2026-10-05T01:00:00Z')]
    asked = []
    def api(path):
        asked.append(path)
        return listed
    assert product.latest_published('owner/repo', api=api) == 'product-2'
    assert asked == ['/repos/owner/repo/releases?per_page=100']  # one call, no CI lookups, no login
    # Never back: with product-2 installed, the older product-1 is not a candidate (it would come forward again).
    assert product.latest_published('owner/repo', current='2' + 'f' * 63, api=api) is None
    listed[0]['body'] += '\n\n' + product.CI_PASSED + 'https://ci/runs/8'
    assert product.latest_published('owner/repo', current='2' + 'f' * 63, api=api) == 'product-3'
    assert product.latest_published('owner/repo', after='3' + 'f' * 63, api=api) is None  # a rejected candidate is a floor


def test_an_installed_release_older_than_the_listed_hundred_is_found_by_name():
    import urllib.error
    listed = [release('product-new', '2026-11-04T00:00:00Z')]
    asked = []
    def api(path):
        asked.append(path)
        if path.endswith('/releases?per_page=100'):
            return listed
        if path.endswith('/releases/tags/product-' + 'a' * 16):
            return release('product-' + 'a' * 16, '2026-09-01T00:00:00Z')
        raise urllib.error.HTTPError(path, 404, 'Not Found', {}, None)
    assert product.latest_published('owner/repo', current='a' * 64, api=api) == 'product-new'
    assert asked[-1] == '/repos/owner/repo/releases/tags/product-' + 'a' * 16
    # A release the repository no longer has: its place in the order is unknown, so nothing is installed.
    assert product.latest_published('owner/repo', current='b' * 64, api=api) is None


@pytest.mark.parametrize('suffix', ['', '.delete'])
@pytest.mark.parametrize('condition', ['alive', 'unknown', 'exception', 'mismatch', 'denied', 'malformed'])
def test_registered_usage_blocks_all_retirement(tmp_path, monkeypatch, suffix, condition):
    home = tmp_path/'product'
    write_state(home, A, B)
    release = home/'releases'/(C[:16]+suffix)
    release.mkdir(parents=True)
    dead = release.parent/('.use.'+C[:16]+'.123')
    dead.write_text('123')
    marker = release.parent/('.use.'+C[:16]+'.456')
    marker.write_text('789' if condition == 'mismatch' else '456')
    if condition == 'malformed':
        marker.rename(marker.with_name('.use.'+C[:16]+'.bad'))
    def alive(pid):
        if pid == 123:
            return False
        if condition == 'exception':
            raise RuntimeError('cannot query')
        return None if condition == 'unknown' else True
    monkeypatch.setattr(product, '_pid_alive', alive)
    if condition == 'denied':
        original = type(marker).read_text
        def read(path, *args, **kwargs):
            if path == marker:
                raise PermissionError('registry denied')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(type(marker), 'read_text', read)
    monkeypatch.setattr(product.os, 'rename', lambda *a: pytest.fail('rename while protected'))
    monkeypatch.setattr(product.shutil, 'rmtree', lambda *a, **k: pytest.fail('rmtree while protected'))
    product.prune(home)
    assert release.is_dir() and dead.is_file()


@pytest.mark.parametrize('failure', ['inventory', 'lock', 'unlink'])
def test_registry_access_failure_preserves_candidates(tmp_path, monkeypatch, failure):
    home = tmp_path/'product'
    write_state(home, A, B)
    release = home/'releases'/C[:16]
    release.mkdir(parents=True)
    marker = release.parent/('.use.'+C[:16]+'.123')
    marker.write_text('123')
    monkeypatch.setattr(product, '_pid_alive', lambda pid: False)
    def denied(*args, **kwargs):
        raise PermissionError('denied')
    if failure == 'inventory':
        monkeypatch.setattr(type(release), 'iterdir', denied)
    elif failure == 'lock':
        monkeypatch.setattr(product, '_usage_lock', denied)
    else:
        monkeypatch.setattr(type(marker), 'unlink', denied)
    calls = []
    monkeypatch.setattr(product.os, 'rename', lambda *a: calls.append('rename'))
    monkeypatch.setattr(product.shutil, 'rmtree', lambda *a, **k: calls.append('rmtree'))
    product.prune(home)
    assert calls == [] and release.is_dir() and marker.is_file()


@pytest.mark.parametrize('suffix', ['', '.delete'])
def test_dead_usage_is_cleaned_but_selected_and_unknown_are_preserved(tmp_path, monkeypatch, suffix):
    home = tmp_path/'product'
    write_state(home, A, B)
    before = (home/'active.json').read_bytes()
    names = [A[:16], B[:16], A[:16]+'.delete', B[:16]+'.delete', 'notes', C[:16]+suffix]
    for name in names:
        (home/'releases'/name).mkdir(parents=True)
    marker = home/'releases'/('.use.'+C[:16]+'.123')
    marker.write_text('123')
    monkeypatch.setattr(product, '_pid_alive', lambda pid: False)
    product.prune(home)
    assert not marker.exists()
    assert not (home/'releases'/(C[:16]+suffix)).exists()
    assert all((home/'releases'/name).is_dir() for name in names[:-1])
    assert (home/'active.json').read_bytes() == before


def test_prune_preserves_symlink_release(tmp_path):
    home = tmp_path/'product'
    write_state(home, A, B)
    releases = home/'releases'
    releases.mkdir()
    target = tmp_path/'external'
    target.mkdir()
    link = releases/C[:16]
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    product.prune(home)
    assert link.is_symlink() and target.is_dir()


@pytest.mark.parametrize('outcome, expected', [('dead', False), ('live', True), ('denied', None), ('error', None)])
def test_pid_alive_posix(monkeypatch, outcome, expected):
    from types import SimpleNamespace
    def kill(pid, signal):
        assert (pid, signal) == (123, 0)
        if outcome == 'dead':
            raise ProcessLookupError()
        if outcome == 'denied':
            raise PermissionError()
        if outcome == 'error':
            raise OSError()
    monkeypatch.setattr(product, 'os', SimpleNamespace(name='posix', kill=kill))
    assert product._pid_alive(123) is expected
    for invalid in (0, -1, True, '123', None):
        assert product._pid_alive(invalid) is None


@pytest.mark.parametrize('handle, result, error, expected', [
    (123, 0, 0, False), (123, 258, 0, True), (123, 0xffffffff, 0, None),
    (123, 'raise', 0, None), (0, 0, 87, False), (0, 0, 5, None), (0, 0, 999, None)])
def test_pid_alive_windows_closes_handles(monkeypatch, handle, result, error, expected):
    import ctypes
    from types import SimpleNamespace
    closed = []
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)
    def wait(h, timeout):
        assert (h, timeout) == (123, 0)
        if result == 'raise':
            raise OSError('query failed')
        return result
    kernel = SimpleNamespace(OpenProcess=Function(lambda *a: handle),
                             WaitForSingleObject=Function(wait),
                             CloseHandle=Function(lambda h: closed.append(h) or 1))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: error, raising=False)
    monkeypatch.setattr(product, 'os', SimpleNamespace(name='nt'))
    assert product._pid_alive(123) is expected
    assert closed == ([123] if handle else [])


@pytest.mark.parametrize('suffix', ['', '.delete'])
@pytest.mark.parametrize('outcome', ['live', 'empty', 'error'])
def test_unmarked_release_requires_successful_process_query(tmp_path, monkeypatch, suffix, outcome):
    home = tmp_path/'product'
    write_state(home, A, B)
    release = home/'releases'/(C[:16]+suffix)
    release.mkdir(parents=True)
    queried = []
    def query(path):
        queried.append(path)
        if outcome == 'error':
            raise PermissionError('process inventory denied')
        return outcome == 'live'
    monkeypatch.setattr(product, '_release_process_in_use', query)
    if outcome != 'empty':
        monkeypatch.setattr(product.os, 'rename', lambda *a: pytest.fail('rename without clearance'))
        monkeypatch.setattr(product.shutil, 'rmtree', lambda *a, **k: pytest.fail('rmtree without clearance'))
    product.prune(home)
    assert queried == [release]
    assert release.exists() is (outcome != 'empty')
    assert not release.with_name(C[:16]+'.delete').exists() if outcome == 'empty' else release.is_dir()


@pytest.mark.parametrize('payload, expected', [
    ('[]', False), ('null', False),
    ('[{"ProcessId": 123, "ExecutablePath": "C:\\\\PRODUCT\\\\RELEASES\\\\cccccccccccccccc\\\\python.exe", "CommandLine": null}]', True),
    ('{"ProcessId": 123, "ExecutablePath": null, "CommandLine": "python C:\\\\product\\\\releases\\\\cccccccccccccccc\\\\main.py"}', True),
    ('not json', None), ('[42]', None)])
def test_legacy_windows_process_inventory(monkeypatch, payload, expected):
    from pathlib import PureWindowsPath
    from types import SimpleNamespace
    calls = []
    def run(command, **options):
        calls.append(command)
        assert options == dict(capture_output=True, text=True, timeout=15, check=True)
        return SimpleNamespace(stdout=payload)
    monkeypatch.setattr(product, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(product.subprocess, 'run', run)
    release = PureWindowsPath('C:/product/releases/'+C[:16])
    if expected is None:
        with pytest.raises((ValueError, TypeError)):
            product._release_process_in_use(release)
    else:
        assert product._release_process_in_use(release) is expected
    assert calls[0][:3] == ['powershell', '-NoProfile', '-Command']


@pytest.mark.parametrize('source', ['exe', 'cmdline', 'empty', 'denied'])
def test_legacy_posix_process_inventory(tmp_path, monkeypatch, source):
    from pathlib import PurePosixPath
    from types import SimpleNamespace
    release = PurePosixPath('/releases')/C[:16]
    proc = tmp_path/'proc'
    process = proc/'123'
    process.mkdir(parents=True)
    (process/'cmdline').write_bytes(
        b'python\0'+str(release/'main.py').encode()+b'\0' if source == 'cmdline' else b'python\0')
    actual_path = product.Path
    monkeypatch.setattr(product, 'Path', lambda path: proc if path == '/proc' else actual_path(path))
    def readlink(path):
        assert path == process/'exe'
        if source == 'denied':
            raise PermissionError('process denied')
        return str(release/'python') if source == 'exe' else '/usr/bin/python'
    monkeypatch.setattr(product, 'os', SimpleNamespace(name='posix', readlink=readlink,
                       stat=lambda path: SimpleNamespace(st_uid=1000), getuid=lambda: 1000,
                       fsdecode=product.os.fsdecode, sep='/'))
    assert product._release_process_in_use(release) is (source in ('exe', 'cmdline'))


@pytest.mark.parametrize('case', ['other-user', 'both-denied', 'exe-denied', 'gone', 'listing-denied'])
@pytest.mark.parametrize('suffix', ['', '.delete'])
def test_posix_inventory_prune_permissions(tmp_path, monkeypatch, case, suffix):
    from types import SimpleNamespace
    from contextlib import nullcontext
    home = tmp_path/'product'
    write_state(home, A, B)
    release = home/'releases'/(C[:16]+suffix)
    release.mkdir(parents=True)
    proc = tmp_path/'proc'
    own = proc/'123'
    other = proc/'1'
    own.mkdir(parents=True)
    other.mkdir()
    (own/'cmdline').write_bytes(b'python\0')
    actual_path, actual_os = product.Path, product.os
    read_bytes = actual_path.read_bytes
    iterdir = actual_path.iterdir
    def read(path):
        if path == own/'cmdline' and case == 'both-denied':
            raise PermissionError('cmdline denied')
        return read_bytes(path)
    def entries(path):
        if path == proc and case == 'listing-denied':
            raise PermissionError('inventory denied')
        return iterdir(path)
    def stat(path):
        if path == own and case == 'gone':
            raise FileNotFoundError('exited')
        return SimpleNamespace(st_uid=0 if path == other else 1000)
    def readlink(path):
        assert path != other/'exe', 'other-user process must not be inspected'
        if case in ('both-denied', 'exe-denied'):
            raise PermissionError('exe denied')
        return '/usr/bin/python'
    class ProcessOS:
        name = 'posix'
        getuid = staticmethod(lambda: 1000)
        def __getattr__(self, name):
            return getattr(actual_os, name)
    fake_os = ProcessOS()
    fake_os.stat, fake_os.readlink = stat, readlink
    monkeypatch.setattr(actual_path, 'read_bytes', read)
    monkeypatch.setattr(actual_path, 'iterdir', entries)
    monkeypatch.setattr(product, 'Path', lambda path: proc if path == '/proc' else actual_path(path))
    monkeypatch.setattr(product, 'os', fake_os)
    # Exercise POSIX inventory on Windows too, without selecting a foreign lock API.
    monkeypatch.setattr(product, '_usage_lock', lambda home: nullcontext())
    uncertain = case in ('both-denied', 'listing-denied')
    if uncertain:
        monkeypatch.setattr(fake_os, 'rename', lambda *a: pytest.fail('rename under uncertainty'))
        monkeypatch.setattr(product.shutil, 'rmtree', lambda *a, **k: pytest.fail('rmtree under uncertainty'))
    product.prune(home)
    assert release.exists() is uncertain
    assert (home/'active.json').is_file()
    if not uncertain:
        assert not release.with_name(C[:16]+'.delete').exists()


@pytest.mark.skipif(product.os.name != 'posix' or not product.Path('/proc').is_dir(),
                    reason='requires real POSIX /proc')
def test_real_proc_legacy_release_cleanup(tmp_path):
    import sys
    home = tmp_path/'product'
    write_state(home, A, B)
    release = home/'releases'/C[:16]
    release.mkdir(parents=True)
    script = release/'hold.py'
    script.write_text('import time\nprint("ready", flush=True)\ntime.sleep(60)\n', encoding='utf-8')
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), 'child did not start'
        assert child.stdout.readline().strip() == 'ready'
        product.prune(home)
        assert release.is_dir()
    finally:
        child.terminate()
        child.wait(timeout=10)
        child.stdout.close()
    product.prune(home)
    assert not release.exists()
    assert not release.with_name(C[:16]+'.delete').exists()

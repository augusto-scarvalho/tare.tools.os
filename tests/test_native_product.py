"""Failed updates must not displace a working install or its user state."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from relay import native_product as product


def bundle(root, version):
    root.mkdir()
    (root/'install.py').write_bytes(Path(product.__file__).read_bytes())
    (root/'requirements.lock').write_text('# '+version)
    product.write_json(root/'release.json', {'packages': [{'name': 'tare-tools-os', 'version': version, 'commit': 'a'*40}]})
    product.write_json(root/'bundle.json', {'schema': 'tare.tools/product-bundle/1',
        'python': '.'.join(map(str, sys.version_info[:2])), 'platform': sys.platform,
        'files': {p.name: product.digest(p) for p in root.iterdir()}})
    return root


@pytest.fixture
def fake_install(monkeypatch):
    real_qualify = product.qualify_transition
    def create(self, path):
        exe = product.python_at(path.parent)
        exe.parent.mkdir(parents=True)
        exe.write_text('fixture')
    monkeypatch.setattr(product.venv.EnvBuilder, 'create', create)
    monkeypatch.setattr(product.subprocess, 'run', lambda *a, **kw: None)
    # Synthetic executables never launch a release process. Keep the legacy
    # inventory independent of the subprocess.run stub (also used by canary tests).
    monkeypatch.setattr(product, '_release_process_in_use', lambda release: False)
    def qualified(home, incumbent, candidate):
        result = {'status': 'PASS', 'incumbent': incumbent, 'candidate': candidate}
        product.write_json(product.release_at(home, candidate)/'catalog-trial.json', result)
        return result
    monkeypatch.setattr(product, 'qualify_transition', qualified)
    return real_qualify


def test_update_rollback_retains_state_and_same_bundle_is_idempotent(tmp_path, fake_install):
    home = tmp_path/'home'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    saved = home/'state/conversation.json'; saved.write_text('history')
    second_bundle = bundle(tmp_path/'two', '2')
    second = product.install(second_bundle, home)['current']
    assert product.install(second_bundle, home)['previous'] == first
    assert product.rollback(home)['current'] == first
    assert product.rollback(home)['current'] == second
    assert saved.read_text() == 'history'


def test_windows_native_git_requirement_preserves_active_install_and_legacy_rollback(
        tmp_path, fake_install, monkeypatch):
    from types import SimpleNamespace
    # Simulate bundle validation on Windows without mutating Python's global
    # platform (which pytest and the underlying filesystem also use).
    monkeypatch.setattr(product, 'sys', SimpleNamespace(**{**vars(sys), 'platform': 'win32'}))

    def windows_bundle(path, version):
        result = bundle(path, version)
        manifest = json.loads((result/'bundle.json').read_text())
        manifest['platform'] = 'win32'
        product.write_json(result/'bundle.json', manifest)
        return result

    home = tmp_path/'home'
    first = product.install(windows_bundle(tmp_path/'old', '0.1.113'), home)['current']
    saved = home/'state/conversation.json'
    saved.write_text('history')
    update = windows_bundle(tmp_path/'new', '0.1.114')
    before = (home/'active.json').read_bytes()
    with pytest.raises(ValueError, match='release-owned native Git is required'):
        product.install(update, home)
    assert (home/'active.json').read_bytes() == before
    assert len(list((home/'releases').iterdir())) == 1

    for name in ('cmd/git.exe', 'ucrt64/bin/git.exe',
                 'ucrt64/bin/busybox.exe', 'ucrt64/bin/sh.exe'):
        tool = update/'tools/git'/name
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_bytes(b'fixture native runtime')
    manifest = json.loads((update/'bundle.json').read_text())
    manifest['files'] = {p.relative_to(update).as_posix(): product.digest(p)
                         for p in update.rglob('*') if p.is_file() and p.name != 'bundle.json'}
    product.write_json(update/'bundle.json', manifest)
    second = product.install(update, home)['current']
    assert second != first
    assert product.rollback(home)['current'] == first
    assert product.rollback(home)['current'] == second
    assert saved.read_text() == 'history'


def test_failed_install_preserves_active_and_retries_unactivated_directory(tmp_path, fake_install, monkeypatch):
    home = tmp_path/'home'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    update = bundle(tmp_path/'two', '2')
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, 'pip')
    monkeypatch.setattr(product.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        product.install(update, home)
    assert product.state(home)['current'] == first
    monkeypatch.setattr(product.subprocess, 'run', lambda *a, **kw: None)
    assert product.install(update, home)['previous'] == first


def test_tampered_bundle_and_broken_rollback_cannot_activate(tmp_path, fake_install):
    home = tmp_path/'home'
    one = bundle(tmp_path/'one', '1')
    first = product.install(one, home)['current']
    two = bundle(tmp_path/'two', '2')
    (two/'requirements.lock').write_text('tamper')
    before = (home/'active.json').read_bytes()
    with pytest.raises(ValueError, match='changed'):
        product.install(two, home)
    assert (home/'active.json').read_bytes() == before
    assert product.state(home)['current'] == first
    second = product.install(bundle(tmp_path/'three', '3'), home)['current']
    before = (home/'active.json').read_bytes()
    product.python_at(product.release_at(home, first)).unlink()
    with pytest.raises(ValueError, match='incomplete'):
        product.rollback(home)
    assert (home/'active.json').read_bytes() == before
    assert product.state(home)['current'] == second


def test_invalid_paths_and_runtime_rejected(tmp_path):
    for identifier in ('../../elsewhere', 'main', None):
        with pytest.raises(ValueError):
            product.release_at(tmp_path, identifier)
    one = bundle(tmp_path/'one', '1')
    value = json.loads((one/'bundle.json').read_text())
    value['files']['../outside'] = '0'*64
    product.write_json(one/'bundle.json', value)
    with pytest.raises(ValueError, match='changed'):
        product.verify(one)
    value['python'] = '0.0'
    product.write_json(one/'bundle.json', value)
    with pytest.raises(ValueError, match='requires Python'):
        product.verify(one)


def test_managed_launch_ignores_checkout_and_legacy_descriptor(tmp_path, monkeypatch):
    from relay import native_installation
    for key in ('PYTHONPATH', 'PYTHONHOME', 'TARE_OS_ROOT'):
        monkeypatch.setenv(key, 'source-checkout')
    env = product.clean_env(tmp_path)
    assert 'PYTHONPATH' not in env and 'TARE_OS_ROOT' not in env
    assert env['TARE_MANAGED_HOME'] == str(tmp_path)
    monkeypatch.setenv('TARE_MANAGED_HOME', str(tmp_path))
    assert native_installation.settings() == {}


def test_launcher_binds_conversations_outside_version_directory(tmp_path, fake_install, monkeypatch):
    product.install(bundle(tmp_path/'one', '1'), tmp_path/'home')
    seen = []
    monkeypatch.setattr(product.subprocess, 'call', lambda argv, **kw: seen.append((argv, kw)) or 0)
    assert product.main(['--home', str(tmp_path/'home'), 'console']) == 0
    argv, options = seen[0]
    # Exercise the bootstrap itself with the command runner replaced: this must
    # work for older releases too, whose entrypoint defaults to site-packages.
    from relay import relay_mesh, native_installation
    roots = []
    monkeypatch.setattr(relay_mesh, 'rebind_root', lambda root: roots.append(root))
    monkeypatch.setattr(native_installation, 'main', lambda: 0)
    for key in ('TARE_MANAGED_HOME', 'TARE_PRODUCT_HOME', 'TARE_RELEASE_ID'):
        monkeypatch.setenv(key, options['env'][key])
    with pytest.raises(SystemExit):
        exec(argv[3])
    assert roots == [tmp_path/'home/control']


def test_install_writes_a_shell_launcher_including_for_git_bash(tmp_path, fake_install):
    home = tmp_path/'home'
    product.install(bundle(tmp_path/'one', '1'), home)
    launcher = home/'tare'
    assert b'\r' not in launcher.read_bytes()
    if sys.platform != 'win32':
        assert launcher.stat().st_mode & 0o111
    text = launcher.read_text(encoding='utf-8')
    assert text.startswith('#!/bin/sh') and '/product.py" --home' in text and '"$@"' in text


def test_same_path_command_observes_install_update_and_rollback(tmp_path, request):
    # Invoke the actual generated launcher in a child, not the imported manager.
    # Fixture installs avoid pip; status needs no running venv or model.
    run = subprocess.run
    request.getfixturevalue('fake_install')
    home = tmp_path/'managed installation'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    launcher = home/('tare.cmd' if sys.platform == 'win32' else 'tare')

    def observed():
        result = run([str(launcher), 'status'], shell=sys.platform == 'win32',
                     cwd=tmp_path, capture_output=True, text=True,
                     env=product.clean_env(home), timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert observed()['current'] == first
    second = product.install(bundle(tmp_path/'two', '2'), home)['current']
    assert observed()['current'] == second
    product.rollback(home)
    assert observed()['current'] == first
    assert launcher.parent == home  # no release-specific directory enters PATH


def test_the_launched_release_puts_its_bundled_ripgrep_first_on_path(tmp_path, fake_install, monkeypatch):
    one = bundle(tmp_path/'one', '1')
    (one/'tools').mkdir()
    (one/'tools'/'rg.exe').write_bytes(b'rg')
    manifest = json.loads((one/'bundle.json').read_text())
    manifest['files'] = {p.relative_to(one).as_posix(): product.digest(p) for p in one.rglob('*')
                         if p.is_file() and p.name != 'bundle.json'}
    product.write_json(one/'bundle.json', manifest)
    home = (tmp_path/'home').resolve()
    current = product.install(one, home)['current']
    tools = product.release_at(home, current)/'bundle'/'tools'
    if product.os.name != 'nt':
        assert (tools/'rg.exe').stat().st_mode & 0o111
    seen = []
    monkeypatch.setattr(product.subprocess, 'call', lambda argv, **kw: seen.append(kw['env']) or 0)
    monkeypatch.setenv('PATH', 'machine-path')
    assert product.main(['--home', str(home), 'console']) == 0
    assert seen[0]['PATH'] == str(tools) + product.os.pathsep + 'machine-path'
    assert product.clean_env(home)['PATH'] == 'machine-path'  # installer steps keep the machine PATH


def test_release_native_git_precedes_system_git_without_changing_machine_path(tmp_path,monkeypatch):
    tools=tmp_path/'release/bundle/tools'
    (tools/'git/cmd').mkdir(parents=True)
    (tools/'git/cmd/git.exe').write_bytes(b'owned native Git')
    monkeypatch.setenv('PATH','system-git')
    assert product.clean_env(tmp_path,tmp_path/'release')['PATH'].split(product.os.pathsep)==[
        str(tools/'git/cmd'),str(tools),'system-git']
    assert product.clean_env(tmp_path)['PATH']=='system-git'


def test_transition_receipts_and_new_launches(tmp_path, fake_install, monkeypatch):
    home = tmp_path/'home'
    monkeypatch.setattr(product.time, 'strftime', lambda *args: '2026-10-16T12:00:00Z')
    one = bundle(tmp_path/'one', '1')
    first = product.install(one, home)['current']

    def receipt(origin, destination, operation):
        return {'from': origin, 'to': destination, 'operation': operation,
                'at_utc': '2026-10-16T12:00:00Z',
                'qualification': f'releases/{destination[:16]}/ready.json'}

    assert product.state(home) == {'current': first, 'previous': None,
                                  'last_transition': receipt(None, first, 'install')}
    old_command, old_env = product.release_command(home, ['console'])
    two = bundle(tmp_path/'two', '2')
    second = product.install(two, home)['current']
    assert product.status(home)['last_transition'] == {
        **receipt(first, second, 'update'),
        'catalog_trial': {'path': f'releases/{second[:16]}/catalog-trial.json',
                          'sha256': product.digest(product.release_at(home, second)/'catalog-trial.json')}}
    before = (home/'active.json').read_bytes()
    assert product.install(two, home)['previous'] == first
    assert (home/'active.json').read_bytes() == before
    command, env = product.release_command(home, ['serve'])
    assert command[0] == str(product.python_at(product.release_at(home, second)))
    assert env['TARE_RELEASE_ID'] == second
    assert old_command[0] == str(product.python_at(product.release_at(home, first)))
    assert old_env['TARE_RELEASE_ID'] == first  # already selected launches are not rewritten
    result = product.rollback(home)
    assert result['current'] == first and result['previous'] == second
    assert result['last_transition'] == receipt(second, first, 'rollback')
    result = product.rollback(home)
    assert result['last_transition'] == receipt(first, second, 'rollback')
    qualification = home/result['last_transition']['qualification']
    assert json.loads(qualification.read_text()) == {'bundle': second}


@pytest.mark.parametrize('operation', ['update', 'rollback'])
def test_legacy_pointer_without_receipt_remains_usable(tmp_path, fake_install, operation):
    home = tmp_path/'home'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    two = bundle(tmp_path/'two', '2')
    second = product.install(two, home)['current']
    legacy = {'current': second, 'previous': first}
    (home/'active.json').write_text(json.dumps(legacy), encoding='utf-8')
    assert product.status(home)['current'] == second
    assert product.release_command(home, ['console'])[1]['TARE_RELEASE_ID'] == second
    before = (home/'active.json').read_bytes()
    product.install(two, home)
    assert (home/'active.json').read_bytes() == before  # no synthetic receipt on a no-op
    result = (product.rollback(home) if operation == 'rollback' else
              product.install(bundle(tmp_path/'three', '3'), home))
    assert result['last_transition']['from'] == second
    assert result['last_transition']['to'] == result['current']
    assert result['last_transition']['operation'] == operation


@pytest.mark.parametrize('stage', ['pip-install', 'pip-check', 'imports', 'qualification'])
def test_pre_activation_failures_preserve_pointer_bytes(tmp_path, fake_install, monkeypatch, stage):
    home = tmp_path/'home'
    product.install(bundle(tmp_path/'one', '1'), home)
    before = (home/'active.json').read_bytes()
    update = bundle(tmp_path/'two', '2')
    identifier = product.digest(update/'bundle.json')
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        target = {'pip-install': 1, 'pip-check': 2, 'imports': 3, 'qualification': 4}[stage]
        if len(calls) == target:
            assert (home/'active.json').read_bytes() == before
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(product.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        product.install(update, home)
    assert (home/'active.json').read_bytes() == before
    ready = product.release_at(home, identifier)/'ready.json'
    assert ready.exists() == (stage == 'qualification')
    monkeypatch.setattr(product.subprocess, 'run', lambda *args, **kwargs: None)
    assert product.install(update, home)['current'] == identifier


@pytest.mark.parametrize('ready', [None, {}, {'bundle': '0'*64}, [], 'not-json'])
@pytest.mark.parametrize('operation', ['update', 'rollback'])
def test_invalid_qualification_cannot_activate(tmp_path, fake_install, ready, operation):
    home = tmp_path/'home'
    one = bundle(tmp_path/'one', '1')
    first = product.install(one, home)['current']
    product.install(bundle(tmp_path/'two', '2'), home)
    path = product.release_at(home, first)/'ready.json'
    if ready is None:
        path.unlink()
    elif ready == 'not-json':
        path.write_text('{', encoding='utf-8')
    else:
        product.write_json(path, ready)
    before = (home/'active.json').read_bytes()
    if ready is None:
        diagnostic = 'Active release is incomplete' if operation == 'update' else 'Release is incomplete'
    elif ready == 'not-json':
        diagnostic = 'Expecting property name'
    else:
        diagnostic = 'Release qualification identity changed'
    with pytest.raises(ValueError, match=diagnostic):
        if operation == 'rollback':
            product.rollback(home)
        else:
            product.install(one, home)
    assert (home/'active.json').read_bytes() == before


@pytest.mark.parametrize('operation', ['update', 'rollback'])
@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_atomic_activation_failure_preserves_pointer_bytes(tmp_path, fake_install, monkeypatch, operation, failure):
    home = tmp_path/'home'
    product.install(bundle(tmp_path/'one', '1'), home)
    two = bundle(tmp_path/'two', '2')
    product.install(two, home)
    if operation == 'update':
        product.rollback(home)  # both destinations already qualified: only active.json is written
    before = (home/'active.json').read_bytes()
    before_children = {path.name for path in home.iterdir()}
    real_replace = product.os.replace
    seen = []

    def replace(source, destination):
        if Path(destination) == home/'active.json':
            value = json.loads(Path(source).read_text(encoding='utf-8'))
            assert value['last_transition']['to'] == value['current']
            assert value['last_transition']['from'] == value['previous']
            assert (home/'active.json').read_bytes() == before
            seen.append(value)
            raise OSError('fixture activation replace failed')
        return real_replace(source, destination)

    def fsync(fd):
        raise OSError('fixture activation fsync failed')

    if failure == 'replace':
        monkeypatch.setattr(product.os, 'replace', replace)
    else:
        monkeypatch.setattr(product.os, 'fsync', fsync)
    with pytest.raises(OSError, match='fixture activation '+failure+' failed'):
        if operation == 'rollback':
            product.rollback(home)
        else:
            product.install(two, home)
    assert (home/'active.json').read_bytes() == before
    assert len(seen) == (1 if failure == 'replace' else 0)
    assert {path.name for path in home.iterdir()} == before_children


def test_rollback_without_previous_preserves_pointer_bytes(tmp_path, fake_install):
    home = tmp_path/'home'
    product.install(bundle(tmp_path/'one', '1'), home)
    before = (home/'active.json').read_bytes()
    with pytest.raises(ValueError, match='No previous release'):
        product.rollback(home)
    assert (home/'active.json').read_bytes() == before


@pytest.mark.parametrize('operation', ['update', 'rollback'])
def test_activation_seals_pointer_and_receipt_in_one_replace(tmp_path, fake_install, monkeypatch, operation):
    home = tmp_path/'home'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    two = bundle(tmp_path/'two', '2')
    second = product.install(two, home)['current']
    if operation == 'update':
        product.rollback(home)
    before = (home/'active.json').read_bytes()
    origin, destination = (first, second) if operation == 'update' else (second, first)
    real_replace = product.os.replace
    replacements = []

    def replace(source, target):
        if Path(target) == home/'active.json':
            assert (home/'active.json').read_bytes() == before
            value = json.loads(Path(source).read_text(encoding='utf-8'))
            assert value['current'] == destination and value['previous'] == origin
            receipt = value['last_transition']
            assert (receipt['from'], receipt['to'], receipt['operation']) == (origin, destination, operation)
            assert json.loads((home/receipt['qualification']).read_text()) == {'bundle': destination}
            replacements.append(value)
        return real_replace(source, target)

    monkeypatch.setattr(product.os, 'replace', replace)
    result = product.rollback(home) if operation == 'rollback' else product.install(two, home)
    assert len(replacements) == 1
    assert product.state(home) == replacements[0]
    assert result['last_transition'] == replacements[0]['last_transition']


# Exercise the actual inline bootstrap with stdlib-only stand-ins for application imports.
# The selected release can be old and need not contain any new registration helper.
def _test_python():
    # Windows venv python.exe is a redirector, not the registering worker.
    try:
        base = sys._base_executable
        return base if base != sys.executable else sys.executable
    except AttributeError:
        return sys.executable


def usage_launch(home):
    identifier = 'c'*64
    release = home/'releases'/identifier[:16]
    release.mkdir(parents=True)
    (release/'ready.json').write_text('{}')
    product.write_json(home/'active.json', {'current': identifier, 'previous': None})
    command, env = product.release_command(home, ['console', 'two words', '--flag'])
    assert command[1:3] == ['-I', '-c']
    setup = '''import sys, types, os, json, time
from pathlib import Path
home = Path(os.environ['TARE_PRODUCT_HOME'])
relay = types.ModuleType('relay')
mesh = types.ModuleType('relay.relay_mesh')
mesh.rebind_root = lambda root: None
app = types.ModuleType('relay.native_installation')
def main():
    pid = os.getpid()
    marker = home/'releases'/('.use.'+os.environ['TARE_RELEASE_ID'][:16]+'.'+str(pid))
    assert marker.read_text() == str(pid)
    entered = home/'entered.json'
    pending = home/'entered.tmp'
    pending.write_text(json.dumps({'pid': pid, 'args': sys.argv[1:]}))
    os.replace(pending, entered)
    while not (home/'finish').exists():
        time.sleep(.01)
    return 0
app.main = main
relay.relay_mesh = mesh
sys.modules.update({'relay': relay, 'relay.relay_mesh': mesh, 'relay.native_installation': app})
'''
    return release, [_test_python(), *command[1:3], setup+'\nexec('+repr(command[3])+')', *command[4:]], env


def wait_for_entry(home, child):
    import time
    deadline = time.monotonic()+10
    while not (home/'entered.json').exists():
        assert child.poll() is None, child.communicate(timeout=5)
        assert time.monotonic() < deadline, 'bootstrap did not reach main'
        time.sleep(.01)
    return json.loads((home/'entered.json').read_text())


def stop_usage_child(child):
    if child.poll() is None:
        child.terminate()
    child.communicate(timeout=10)


def test_bootstrap_child_registers_before_main_and_is_removed_after_exit(tmp_path):
    home = tmp_path/'product'
    release, command, env = usage_launch(home)
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        entered = wait_for_entry(home, child)
        assert entered == {'pid': child.pid, 'args': ['console', 'two words', '--flag']}
        marker = release.parent/('.use.'+release.name+'.'+str(child.pid))
        assert marker.read_text() == str(child.pid)
        product.write_json(home/'active.json', {'current': 'a'*64, 'previous': 'b'*64})
        product.prune(home, known_free={release.name})
        assert release.is_dir() and marker.is_file()
        (home/'finish').touch()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert product._pid_alive(child.pid) is False
        product.prune(home)
        assert not release.exists() and not marker.exists()
    finally:
        stop_usage_child(child)


def test_bootstrap_publication_failure_prevents_main(tmp_path):
    home = tmp_path/'product'
    release, command, env = usage_launch(home)
    bootstrap = command[3]
    # Fail only the atomic publication; application imports must not be reached.
    command[3] = bootstrap.replace('\nexec(', '\ndef denied(*a): raise PermissionError("publication denied")\nos.replace = denied\nexec(', 1)
    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode != 0 and 'publication denied' in done.stderr
    assert not (home/'entered.json').exists()
    assert list(release.parent.iterdir()) == [release]


def test_registration_and_retirement_are_serialized_without_install_deadlock(tmp_path):
    home = tmp_path/'product'
    release, command, env = usage_launch(home)
    product.write_json(home/'active.json', {'current': 'a'*64, 'previous': 'b'*64})
    # Publisher waits for retirement, then registers before entering main.
    child = None
    try:
        with product._usage_lock(home):
            child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            import time
            time.sleep(.5)
            assert child.poll() is None
            assert not (home/'entered.json').exists()
            product.prune(home)  # nonblocking: another owner defers maintenance
            assert release.is_dir()
        wait_for_entry(home, child)
        marker = release.parent/('.use.'+release.name+'.'+str(child.pid))
        assert marker.read_text() == str(child.pid)
        (home/'finish').touch()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
    finally:
        if child is not None:
            stop_usage_child(child)
    (home/'entered.json').unlink()
    (home/'finish').unlink()
    # An installer may hold install.lock while starting a registered child and pruning.
    with product.exclusive(home):
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_for_entry(home, child)
            product.prune(home)
            assert release.is_dir()
        finally:
            stop_usage_child(child)
    product.prune(home)
    assert not release.exists()
    # Selection before retirement does not authorize publication into a removed release.
    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode != 0


@pytest.mark.parametrize('outcome', ['REJECTED', 'INCONCLUSIVE'])
def test_fake_install_refusal_cleanup_without_shutdown_confirmation(
        tmp_path, fake_install, monkeypatch, outcome):
    """A simulated refusal uses the fixture inventory, not real OS processes."""
    import zipfile

    home = tmp_path/'home'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    candidate = bundle(tmp_path/'two', '2')
    identifier = product.digest(candidate/'bundle.json')
    before = product.state(home)
    monkeypatch.setattr(product, '_restart_locked', lambda *a, **k: 'current')
    monkeypatch.setattr(product, 'sync_vscode', lambda *a: [])
    monkeypatch.setattr(product, 'auto_storage', lambda *a: 'off')
    monkeypatch.setattr(product, 'latest_published', lambda *a, **k: 'product-'+identifier[:16])

    def download(repo, tag, folder):
        with zipfile.ZipFile(Path(folder)/'bundle.zip', 'w') as archive:
            for path in candidate.iterdir():
                archive.write(path, path.name)

    def refused(home, old, new):
        # No shutdown confirmation: cleanup must still consult the fixture's
        # explicit inventory rather than treating absent confirmation as safe.
        value = {'status': outcome, 'reason': 'controlled failure',
                 'incumbent': old, 'candidate': new}
        product.write_json(home/'last-catalog-trial.json', value)
        return value

    monkeypatch.setattr(product, 'download_bundle', download)
    monkeypatch.setattr(product, 'qualify_transition', refused)
    result = product.auto_update(home)
    assert result['installed'] is None
    assert result['qualification']['status'] == outcome
    actual = product.state(home)
    assert actual['current'] == first
    assert all(actual.get(key) == before.get(key)
               for key in ('current', 'previous', 'last_transition'))
    assert not product.release_at(home, identifier).exists()
    assert (home/'last-catalog-trial.json').is_file()

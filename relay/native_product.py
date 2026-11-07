"""Offline, side-by-side product installation. Also shipped as bundle/install.py."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import venv
import zipfile


class CatalogQualificationError(ValueError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__('Catalog transition '+receipt['status']+': '+receipt['reason'])


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(value, indent=2, sort_keys=True)+'\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def product_home():
    return Path(os.environ.get('TARE_PRODUCT_HOME') or
                Path(os.environ.get('LOCALAPPDATA', Path.home()/'.local/share'))/'tare.tools/product').resolve()


def python_at(release):
    return release/'venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def release_at(home, identifier):
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier):
        raise ValueError('Invalid release identifier')
    release = home/'releases'/identifier[:16]
    if release.resolve() != home.resolve()/'releases'/identifier[:16]:
        raise ValueError('Release directory must not redirect outside the installation')
    return release


def state(home):
    path = home/'active.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'current': None, 'previous': None}


@contextmanager
def exclusive(home):
    """OS lock releases on crash; no stale lock recovery workflow."""
    home.mkdir(parents=True, exist_ok=True)
    with (home/'install.lock').open('a+b') as stream:
        stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def verify(bundle):
    manifest = json.loads((bundle/'bundle.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 'tare.tools/product-bundle/1':
        raise ValueError('Unsupported bundle')
    if manifest['python'] != '.'.join(map(str, sys.version_info[:2])) or manifest['platform'] != sys.platform:
        raise ValueError('Bundle requires Python '+manifest['python']+' on '+manifest['platform'])
    files = manifest['files']
    if not {'requirements.lock', 'release.json', 'install.py'} <= files.keys():
        raise ValueError('Incomplete bundle')
    for name, expected in files.items():
        path = bundle/name
        if (not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink()
                or not path.is_file() or digest(path) != expected):
            raise ValueError('Bundle file missing or changed: '+name)
    # Publication's trial install must also reject an old packer that omitted
    # the new native Git. Older releases remain valid for manual rollback.
    for package in json.loads((bundle/'release.json').read_text(encoding='utf-8'))['packages']:
        version=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:[.+-].*)?',package.get('version',''))
        if (sys.platform=='win32' and package['name'].replace('_','-')=='tare-tools-os' and version
                and tuple(map(int,version.groups())) >= (0,1,114)):
            required={'tools/git/cmd/git.exe', 'tools/git/ucrt64/bin/git.exe',
                      'tools/git/ucrt64/bin/busybox.exe', 'tools/git/ucrt64/bin/sh.exe'}
            if not required <= files.keys():
                raise ValueError('Incomplete Windows bundle: release-owned native Git is required')
    return manifest, digest(bundle/'bundle.json')


def clean_env(home, release=None):
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'TARE_OS_ROOT'):
        env.pop(key, None)
    env.update(TARE_PRODUCT_HOME=str(home), TARE_MANAGED_HOME=str(home),
               PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONUTF8='1')
    env.setdefault('TARE_STORAGE_ROOT', str(home/'state'))
    tools = None if release is None else Path(release)/'bundle'/'tools'
    if tools is not None and tools.is_dir():
        # Keep headless Git's native shell and the search tool release-owned.
        paths = [tools/'git/cmd', tools] if (tools/'git/cmd/git.exe').is_file() else [tools]
        env['PATH'] = os.pathsep.join(map(str,paths)) + os.pathsep + env.get('PATH', '')
    return env


def check_release(home, identifier):
    release = release_at(home, identifier)
    if not (release/'ready.json').is_file() or not python_at(release).is_file():
        raise ValueError('Release is incomplete: '+identifier)
    manifest, observed = verify(release/'bundle')
    if observed != identifier:
        raise ValueError('Release identity changed')
    ready = json.loads((release/'ready.json').read_text(encoding='utf-8'))
    if not isinstance(ready, dict) or ready.get('bundle') != identifier:
        raise ValueError('Release qualification identity changed')
    subprocess.run([str(python_at(release)), '-I', '-m', 'pip', 'check'],
                   env=clean_env(home), check=True, stdout=subprocess.DEVNULL)
    return manifest


def write_activation(home, selected, identifier, operation, *, trial=None):
    """Seal authority for new launches and its last receipt in one atomic replacement.

    Callers hold exclusive(home) and have checked the destination release.
    Running processes are not participants in this handover.
    """
    _require_pair(home, selected)
    if selected['current'] == identifier:
        return
    policy = dict(selected.get('auto_update', {'paused': False, 'after': None}))
    if operation == 'rollback':
        policy['paused'] = True
    elif operation == 'auto_rollback':
        policy['after'] = selected['current']
    write_json(home/'active.json', {
        **({'auto_update': policy} if 'auto_update' in selected or operation in ('rollback', 'auto_rollback') else {}),
        'current': identifier, 'previous': selected['current'],
        'last_transition': {
            'from': selected['current'], 'to': identifier, 'operation': operation,
            'at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'qualification': (Path('releases')/identifier[:16]/'ready.json').as_posix(),
            **({'catalog_trial': trial} if trial else {}),
        },
    })


def _require_pair(home, selected):
    observed = state(home)
    if any(observed[k] != selected[k] for k in ('current', 'previous')):
        raise ValueError('Installation pointer changed; stale transition refused')


def install(bundle, home):
    home = Path(home).resolve()
    with exclusive(home):
        return _install_locked(bundle, home)


def _install_locked(bundle, home):
    bundle, home = Path(bundle).resolve(), Path(home).resolve()
    manifest, identifier = verify(bundle)
    if os.name == 'nt':
        site = release_at(home, identifier)/'venv/Lib/site-packages'
        for wheel in (bundle/'wheels').glob('*.whl'):
            with zipfile.ZipFile(wheel) as archive:
                if any(len(str(site/name)) >= 250 for name in archive.namelist()):
                    raise ValueError('Installation path is too long for Windows. Use --home with a shorter directory.')
    # Private helper: its caller owns install.lock for the complete operation.
    def prepare():
        previous = state(home)
        release = release_at(home, identifier)
        if release.exists() and not (release/'ready.json').exists():
            if identifier in (previous['current'], previous['previous']):
                raise ValueError('Active release is incomplete; choose a different bundle')
            # Only the validated, unactivated release directory belongs to this installer.
            shutil.rmtree(release)
        if not release.exists():
            release.mkdir(parents=True)
            shutil.copytree(bundle, release/'bundle')
            for tool in (release/'bundle'/'tools').glob('*'):
                tool.chmod(0o755)  # extracting the downloaded zip (auto-update) drops the executable bit on POSIX
            verify(release/'bundle')
            venv.EnvBuilder(with_pip=True).create(release/'venv')
            env = clean_env(home)
            subprocess.run([str(python_at(release)), '-I', '-m', 'pip', 'install', '--no-index',
                '--find-links', str(release/'bundle/wheels'), '--require-hashes',
                '-r', str(release/'bundle/requirements.lock')], env=env, check=True)
            subprocess.run([str(python_at(release)), '-I', '-m', 'pip', 'check'], env=env, check=True)
            subprocess.run([str(python_at(release)), '-I', '-c',
                'import relay.native_console, compute_plane.native_models, tare_tools_agent_runtime, specgraph, graph_backlog; '
                'from relay.research_cli import REPORT, Draft7Validator; Draft7Validator.check_schema(REPORT)'],
                env=env, check=True)
            write_json(release/'ready.json', {'bundle': identifier})
        check_release(home, identifier)
        trial = None
        if previous['current'] and previous['current'] != identifier:
            qualified = qualify_transition(home, previous['current'], identifier)
            if qualified['status'] != 'PASS':
                raise CatalogQualificationError(qualified)
            write_json(release/'catalog-trial.json', qualified)
            trial = {'path': 'releases/'+identifier[:16]+'/catalog-trial.json',
                     'sha256': digest(release/'catalog-trial.json')}
        _require_pair(home, previous)
        # Bootstrap stays outside the replaceable environments and is stdlib-only.
        manager = home/'product.py'
        temporary = home/'product.py.new'
        shutil.copyfile(release/'bundle/install.py', temporary)
        os.replace(temporary, manager)
        if os.name == 'nt':
            # Percent signs are expanded even in quoted batch paths.
            base = str(Path(sys._base_executable).resolve()).replace('%', '%%')
            (home/'tare.cmd').write_text('@echo off\n"'+base+'" -I "%~dp0product.py" --home "%~dp0." %*\n', encoding='utf-8')
        import shlex
        # POSIX shells and Git Bash use the same stable manager as tare.cmd.
        # ponytail: a symlink elsewhere would need readlink -f.
        launcher = home/'tare'
        launcher.write_text('#!/bin/sh\nhere=$(dirname "$0")\nexec '+shlex.quote(Path(sys._base_executable).resolve().as_posix())
                            +' -I "$here/product.py" --home "$here" "$@"\n', encoding='utf-8', newline='\n')
        launcher.chmod(0o755)
        write_activation(home, previous, identifier, 'update' if previous['current'] else 'install', trial=trial)
        (home/'state').mkdir(exist_ok=True)
    prepare()
    return status(home)


def rollback(home):
    with exclusive(home):
        selected = state(home)
        if not selected['previous']:
            raise ValueError('No previous release is installed')
        check_release(home, selected['previous'])
        write_activation(home, selected, selected['previous'], 'rollback')
    return status(home)


UPDATE_REPO = 'augusto-scarvalho/tare.tools.os'  # product releases are tagged product-<bundle id prefix>
SERVE_TASK, SERVE_HEALTH = 'Tare-Serve', 'http://127.0.0.1:8790/health'


# The publisher releases a bundle only after tare.tools CI passed on main for its OS commit, and says so in the notes
# (scripts/ops/publish_product.py, CI_PASSED). An installed copy takes only such releases and never asks about CI.
CI_PASSED = 'CI passed on main: '


def github(path):
    """GET from GitHub's REST API over HTTPS. The repository is public: no login, no GitHub CLI (an unauthenticated
    client may make 60 calls an hour; an update check makes one, two when the installed release is old)."""
    import urllib.request
    request = urllib.request.Request('https://api.github.com' + path,
                                     headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'tare.tools'})
    with urllib.request.urlopen(request, timeout=60) as reply:
        return json.load(reply)


def latest_published(repo, current=None, *, after=None, api=github):
    """Tag of the newest product release that CI passed (its notes say so) and that is newer than the `current`
    bundle and the `after` one; None while none is. Never an older release: going back would install code that
    the next update replaces again."""
    import urllib.error
    # A release's created time is its tagged commit's date: two releases of one OS commit (a republish, a
    # satellite-only change) tie on it. The publication time orders them.
    def when(row):
        return row.get('published_at') or row['created_at']
    releases = sorted((row for row in api(f'/repos/{repo}/releases?per_page=100')
                       if row['tag_name'].startswith('product-') and not row.get('draft')), key=when)
    floor = ''
    for boundary in (value for value in (current, after) if value):
        row = next((row for row in releases if boundary.startswith(row['tag_name'].removeprefix('product-'))), None)
        if row is None:  # older than the newest hundred: ask for it by name
            try:
                row = api(f'/repos/{repo}/releases/tags/product-{boundary[:16]}')
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None  # its place in the order is unknown; never infer a downgrade
                raise
        floor = max(floor, when(row))
    passed = [row for row in releases if when(row) > floor and CI_PASSED in (row.get('body') or '')]
    return passed[-1]['tag_name'] if passed else None


def download_bundle(repo, tag, folder):
    """The release's bundle.zip into folder, from its public download link."""
    import urllib.request
    request = urllib.request.Request(f'https://github.com/{repo}/releases/download/{tag}/bundle.zip',
                                     headers={'User-Agent': 'tare.tools'})
    with urllib.request.urlopen(request, timeout=1800) as reply, open(Path(folder)/'bundle.zip', 'wb') as target:
        shutil.copyfileobj(reply, target)


def serve_health(url=SERVE_HEALTH):
    import urllib.request
    try:
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer tare-local'})
        with urllib.request.urlopen(request, timeout=5) as reply:
            payload = reply.read(65537)
            if len(payload) > 65536:
                return None
            value = json.loads(payload)
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


class WindowsServeTask:
    """The only platform-specific part of managed restart; never terminates a task."""

    def available(self):
        return os.name == 'nt' and subprocess.run(
            ['schtasks', '/Query', '/TN', SERVE_TASK], capture_output=True).returncode == 0

    def start(self):
        subprocess.run(['schtasks', '/Run', '/TN', SERVE_TASK], capture_output=True, check=True)


def _installation(home):
    return hashlib.sha256(os.path.normcase(str(Path(home).resolve())).encode('utf-8')).hexdigest()


def _identity(health):
    return {key: health.get(key) for key in ('pid', 'release', 'installation')}


def _known_server(home, selected, health):
    return (isinstance(health, dict) and health.get('status') == 'ok'
            and type(health.get('control_protocol')) is int and health['control_protocol'] == 1
            and type(health.get('pid')) is int and health['pid'] > 0
            and type(health.get('busy')) is int and health['busy'] >= 0
            and health.get('installation') == _installation(home)
            and health.get('release') is not None
            and health['release'] in (selected['current'], selected['previous']))


def _port_open():
    import socket
    from urllib.parse import urlparse
    url = urlparse(SERVE_HEALTH)
    try:
        with socket.create_connection((url.hostname, url.port), timeout=2):
            return True
    except OSError:
        return False


def serve_control(identity, owner, action):
    import urllib.request
    request = urllib.request.Request(SERVE_HEALTH.rsplit('/', 1)[0] + '/control/drain',
        data=json.dumps({**identity, 'owner': owner, 'action': action}).encode('utf-8'),
        headers={'Authorization': 'Bearer tare-local', 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=5) as reply:
        return json.loads(reply.read())


def _move_serve(home, selected, task, wait_seconds, poll):
    """Return failed only after an attempted start; all unsafe observations defer."""
    health = serve_health()
    if health is not None and not _known_server(home, selected, health):
        return 'deferred: foreign identity or legacy control protocol'
    if health and health['release'] == selected['current'] and not health.get('stopping'):
        if health.get('draining'):
            return 'deferred: selected server is draining'
        return 'current'
    if not task.available():
        if health is None and not _port_open():
            return 'no serve task'
        return 'deferred: no serve task'
    if health:
        identity, owner = _identity(health), str(uuid.uuid4())
        stopped = False
        deadline = time.monotonic() + max(0, wait_seconds)
        try:
            while True:
                _require_pair(home, selected)
                drained = serve_control(identity, owner, 'drain')  # renew the 120-second lease
                if (not _known_server(home, selected, drained) or _identity(drained) != identity
                        or drained.get('action') != 'drain' or not drained.get('draining')):
                    return 'deferred: drain identity/protocol mismatch'
                if drained['busy'] == 0:
                    _require_pair(home, selected)
                    reply = serve_control(identity, owner, 'stop')
                    if (not _known_server(home, selected, reply)
                            or _identity(reply) != identity or reply.get('action') != 'stop'
                            or reply.get('busy') != 0 or not reply.get('stopping')):
                        return 'deferred: stop not confirmed'
                    stopped = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 'deferred: busy deadline; lease released'
                time.sleep(min(max(.01, poll), 30, remaining))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return 'deferred: control failed: ' + str(exc)
        finally:
            if not stopped:
                try:
                    serve_control(identity, owner, 'resume')
                except (OSError, ValueError):
                    pass  # failed release expires within 120 seconds; never stop without the owner
        closing = time.monotonic() + 90
        while _port_open():
            if time.monotonic() >= closing:
                return 'deferred: stopped server port remains open'
            time.sleep(2)
    elif _port_open():
        return 'deferred: occupied port without managed health'
    _require_pair(home, selected)
    try:
        task.start()
    except (OSError, subprocess.CalledProcessError):
        return 'failed'
    starting = time.monotonic() + 90
    for _ in range(45):
        remaining = starting - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))
        _require_pair(home, selected)
        health = serve_health()
        if (_known_server(home, selected, health) and health['release'] == selected['current']
                and not health.get('stopping')):
            return 'restarted'
        if health is not None and not _known_server(home, selected, health):
            return 'deferred: foreign identity or legacy control protocol after start'
    return 'failed'


def restart_serve(home, *, wait_seconds, poll=30):
    home = Path(home).resolve()
    with exclusive(home):
        return _restart_locked(home, wait_seconds=wait_seconds, poll=poll)


def _restart_locked(home, *, wait_seconds, poll=30):
    selected = state(home)
    task = WindowsServeTask()
    result = _move_serve(home, selected, task, wait_seconds, poll)
    if result != 'failed':
        return result
    _require_pair(home, selected)
    if selected.get('last_transition', {}).get('operation') in ('rollback', 'auto_rollback'):
        raise ValueError('Recovery failed: rollback release remains selected; Serve is down')
    if not selected['previous']:
        raise ValueError('Serve startup failed; no previous release to recover')
    check_release(home, selected['previous'])
    _require_pair(home, selected)
    write_activation(home, selected, selected['previous'], 'auto_rollback')
    recovered = _move_serve(home, state(home), task, wait_seconds, poll)
    if recovered == 'failed':
        raise ValueError('Recovery failed: previous release remains selected; Serve is down')
    return recovered if recovered.startswith('deferred:') else 'rolled back: ' + recovered


# Kept inline in launch commands so even older releases use the same protocol.
_USAGE_LOCK_SOURCE = '''
from contextlib import contextmanager
@contextmanager
def _usage_lock(home, *, wait=False):
    import os
    import time
    with (home/'usage.lock').open('a+b') as stream:
        stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
        deadline = time.monotonic() + (5 if wait else 0)
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
'''
exec(_USAGE_LOCK_SOURCE)


def _pid_alive(pid):
    """False means proven dead; None means uncertain (and must retain usage)."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if os.name != 'nt':
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            return {0: False, 258: True}.get(result)
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return None


def _release_process_in_use(release):
    """Legacy fallback: a failed process inventory must not authorize removal.

    Markers remain authoritative when present. This only protects unregistered
    legacy processes visible to the OS inventory; it cannot close launch races.
    """
    if os.name == 'nt':
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-CimInstance Win32_Process -ErrorAction Stop | '
             'Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json'],
            capture_output=True, text=True, timeout=15, check=True)
        processes = json.loads(result.stdout)
        if processes is None:
            processes = []
        if isinstance(processes, dict):
            processes = [processes]
        if not isinstance(processes, list):
            raise ValueError('Invalid process inventory')
        target = str(release).casefold()
        found = False
        for process in processes:
            if not isinstance(process, dict) or 'ProcessId' not in process:
                raise ValueError('Invalid process entry')
            for field in ('ExecutablePath', 'CommandLine'):
                value = process.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError('Invalid process path')
                if value and target in value.casefold():
                    found = True
        return found
    target = str(release)
    try:
        processes = list(Path('/proc').iterdir())
        uid = os.getuid()
    except Exception:
        return True
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            # Other users cannot launch from this user's private installation.
            if os.stat(process).st_uid != uid:
                continue
            try:
                executable = os.readlink(process/'exe')
            except PermissionError:
                executable = ''  # cmdline can still establish usage or non-usage.
            arguments = (process/'cmdline').read_bytes().split(b'\0')
            paths = [executable, *(os.fsdecode(arg) for arg in arguments)]
            if any(path == target or path.startswith(target + os.sep) for path in paths):
                return True
        except FileNotFoundError:
            continue  # Process exited during inspection.
        except Exception:
            return True  # An unreadable current-user process is uncertain.
    return False


def prune(home):
    """Retain selected releases and registered users; uncertainty defers cleanup.

    Unmarked legacy releases also require a successful OS process inventory.
    usage.lock is independent of install.lock: launching never needs install.lock.
    Retire under the lock, then delete outside it so publishers need not wait for I/O.
    """
    try:
        with _usage_lock(home):
            retired = _prune_unused(home)
    except Exception:
        return  # Lock, state or global inventory failure must never authorize removal.
    for doomed in retired:
        shutil.rmtree(doomed, ignore_errors=True)


def _prune_unused(home):
    selected = state(home)
    entries = list((home/'releases').iterdir())
    markers = {}
    for entry in entries:
        if not entry.name.startswith('.use.'):
            continue
        match = re.fullmatch(r'\.use\.([0-9a-f]{16})\.([1-9][0-9]*)', entry.name)
        if not match:
            return []  # Cannot reliably attribute a malformed registry entry.
        markers.setdefault(match[1], []).append((entry, match[2]))
    retired = []
    for release in entries:
        match = re.fullmatch(r'([0-9a-f]{16})(\.delete)?', release.name)
        if (not match or release.is_symlink() or not release.is_dir()
                or release.resolve() != home.resolve()/'releases'/release.name
                or any(selected[k] and selected[k].startswith(match[1]) for k in ('current', 'previous'))):
            continue
        registered = markers.get(match[1], [])
        try:
            if not registered and _release_process_in_use(release):
                continue
            dead = []
            for marker, text in registered:
                if marker.is_symlink() or marker.read_text(encoding='ascii') != text:
                    raise ValueError('Invalid usage record')
                if _pid_alive(int(text)) is not False:
                    raise ValueError('Release usage is live or uncertain')
                dead.append(marker)
            # All records have been checked before any mutation, including tombstones.
            for marker in dead:
                marker.unlink()
        except Exception:
            continue
        if match[2]:
            retired.append(release)
            continue
        doomed = release.with_name(release.name + '.delete')
        try:
            os.rename(release, doomed)
        except OSError:
            continue
        retired.append(doomed)
    return retired


def auto_update(home, *, repo=UPDATE_REPO, wait_seconds=3 * 3600, resume=False):
    """One installation lock spans selection, qualification, cutover and recovery."""
    home = Path(home).resolve()
    with exclusive(home):
        if resume:
            selected = state(home)
            policy = {'paused': False, 'after': None}
            if selected.get('auto_update') != policy:
                write_json(home/'active.json', {**selected, 'auto_update': policy})
            return {'installed': None, 'serve': 'resume: policy cleared', **status(home)}
        return _auto_update_locked(home, repo=repo, wait_seconds=wait_seconds)


def _auto_update_locked(home, *, repo, wait_seconds):
    result = {'installed': None}
    result['serve'] = _restart_locked(home, wait_seconds=wait_seconds)
    # A replay finishes only the selected transition, never another candidate in the same run.
    # An idle installation without a Serve task needs no server cutover (including on Linux).
    if result['serve'] not in ('current', 'no serve task'):
        return {**result, **status(home)}
    selected = state(home)
    policy = selected.get('auto_update', {})
    if policy.get('paused'):
        return {**result, 'serve': 'paused', **status(home)}
    tag = latest_published(repo, selected['current'], after=policy.get('after'))
    if tag and not (selected['current'] or '').startswith(tag.removeprefix('product-')):
        with tempfile.TemporaryDirectory(dir=home, prefix='download-') as folder:
            download_bundle(repo, tag, folder)
            with zipfile.ZipFile(Path(folder)/'bundle.zip') as archive:
                archive.extractall(Path(folder)/'bundle')
            _require_pair(home, selected)
            try:
                _install_locked(Path(folder)/'bundle', home)
            except CatalogQualificationError as exc:
                _require_pair(home, selected)
                if exc.receipt['status'] == 'REJECTED':
                    # Reuse I2I-02's existing rejected-publication barrier, without
                    # inventing an activation or changing current/previous/receipt.
                    write_json(home/'active.json', {**selected, 'auto_update': {
                        **policy, 'paused': False, 'after': exc.receipt['candidate']}})
                prune(home)
                return {**result, 'qualification': exc.receipt, **status(home)}
        result['installed'] = tag
        result['serve'] = _restart_locked(home, wait_seconds=wait_seconds)
        if result['serve'].startswith('deferred:'):
            return {**result, **status(home)}
    try:
        result['vscode'] = sync_vscode(published_models())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['vscode'] = [f'skipped: {type(exc).__name__}: {str(exc)[:160]}']
    prune(home)
    result['storage'] = auto_storage(home)
    return {**result, **status(home)}


def release_command(home, argv):
    """`tare <argv>` in the active release: (command, environment)."""
    identifier = state(home)['current']
    if not identifier:
        raise ValueError('Install a product bundle first')
    release = release_at(home, identifier)
    if not (release/'ready.json').is_file():
        raise ValueError('Active release is incomplete')
    env = clean_env(home, release)
    env['TARE_RELEASE_ID'] = identifier
    bootstrap = '''import os, tempfile
from pathlib import Path
''' + _USAGE_LOCK_SOURCE + '''
home = Path(os.environ['TARE_PRODUCT_HOME'])
release = home/'releases'/os.environ['TARE_RELEASE_ID'][:16]
with _usage_lock(home, wait=True):
    if release.is_symlink() or not (release/'ready.json').is_file():
        raise RuntimeError('Selected release is no longer available')
    pid = str(os.getpid())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=release.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(pid.encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, release.parent/('.use.'+release.name+'.'+pid))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
from relay import relay_mesh
relay_mesh.rebind_root(Path(os.environ['TARE_MANAGED_HOME'])/'control')
from relay.native_installation import main
raise SystemExit(main())
'''
    return [str(python_at(release)), '-I', '-c', bootstrap, *argv], env


def auto_storage(home):
    """The operator's opted-in cleanup of other tools' leftovers (`tare storage auto`, relay/storage.py): daily,
    or hourly while the disk is low. Read here first, so the update every few minutes spends nothing when it is
    off or not due; it never fails an update."""
    try:
        path = Path(os.environ.get('TARE_STORAGE_AUTO') or home.parent/'storage-auto.json')
        settings = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(settings, dict) or settings.get('enabled') is not True:
            return 'off'
        usage = shutil.disk_usage(home)
        due = 3600 if usage.free < max(10 * 1024**3, usage.total * .1) else 86400
        if time.time() - float(settings.get('last_run') or 0) < due:
            return 'not due'
        command, env = release_command(home, ['storage', 'auto'])
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1800)
        return (done.stdout.strip() or done.stderr.strip())[-300:]
    except FileNotFoundError:
        return 'off'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return f'skipped: {type(exc).__name__}: {str(exc)[:160]}'


SERVE_MODELS, SERVE_CHAT = 'http://127.0.0.1:8790/v1/models', 'http://127.0.0.1:8790/v1/chat/completions'
VSCODE_FLAVOURS = ('Code', 'Code - Insiders')


def published_models():
    import urllib.request
    request = urllib.request.Request(SERVE_MODELS, headers={'Authorization': 'Bearer tare-local'})
    with urllib.request.urlopen(request, timeout=5) as reply:
        return json.loads(reply.read())['data']


def sync_vscode(models, appdata=None):
    """Keep the `tare` provider in VS Code's chatLanguageModels.json listing what tare serve publishes: add the
    models it lacks and fix their url. The block is managed (a model removed from it comes back); other providers
    and unknown values are kept. Plain JSON only: a file with comments, another kind of `tare` provider, or one
    VS Code changed meanwhile is left as is and reported."""
    appdata = Path(appdata or os.environ.get('APPDATA', ''))
    report = []
    for flavour in VSCODE_FLAVOURS:
        path = appdata/flavour/'User'/'chatLanguageModels.json'
        if not path.parent.is_dir():
            continue  # this VS Code is not installed for this user
        before = path.read_bytes() if path.exists() else b'[]'
        try:
            data = json.loads(before)
        except ValueError:
            report.append(f'{flavour}: not plain JSON, left as is')
            continue
        block = next((p for p in data if isinstance(p, dict) and p.get('name') == 'tare'), None) \
            if isinstance(data, list) else None
        if isinstance(data, list) and block is None:
            block = {'name': 'tare', 'vendor': 'customendpoint', 'apiKey': 'tare-local',
                     'apiType': 'chat-completions', 'models': []}
            data.append(block)
        if (block is None or block.get('vendor') != 'customendpoint' or block.get('apiType') != 'chat-completions'
                or not isinstance(block.get('models'), list)):
            report.append(f'{flavour}: unexpected structure, left as is')
            continue
        listed = {m.get('id'): m for m in block['models'] if isinstance(m, dict)}
        changes = []
        for model in models:
            entry = listed.get(model.get('id'))
            if entry is None:
                window = model.get('context_window')
                window = window if isinstance(window, int) and window > 0 else 131072
                block['models'].append({'id': model['id'], 'name': model['id'], 'url': SERVE_CHAT,
                                        'toolCalling': True, 'vision': model['id'] in ('tare', 'tare-copilot'),
                                        'maxInputTokens': min(window - 16384, 200000), 'maxOutputTokens': 16384})
                changes.append('added ' + model['id'])
            elif entry.get('url') != SERVE_CHAT:
                entry['url'] = SERVE_CHAT
                changes.append('url of ' + model['id'])
        if not changes:
            continue
        if (path.read_bytes() if path.exists() else b'[]') != before:
            report.append(f'{flavour}: changed while being read, next check')
            continue
        if path.exists():
            path.with_name(path.name + '.bak-' + time.strftime('%Y%m%d%H%M%S')).write_bytes(before)
        temporary = path.with_name(path.name + '.tare-tmp')
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temporary, path)
        report.append(f"{flavour}: {', '.join(changes)}")
    return report


def status(home):
    selected = state(home)
    result = {'home': str(home), **selected}
    if selected['current']:
        release = release_at(home, selected['current'])
        result['packages'] = json.loads((release/'bundle/release.json').read_text(encoding='utf-8'))['packages']
        result['packages'] = [{k: p[k] for k in ('name', 'version', 'commit')} for p in result['packages']]
        result['python'] = str(python_at(release))
    return result


_CATALOG_PROBE = r'''
import json, os, sys
from pathlib import Path
from http.server import ThreadingHTTPServer
from relay import native_serve, relay_mesh
relay_mesh.rebind_root(Path(os.environ['TARE_MANAGED_HOME'])/'control')
base = native_serve.handler({'qualification-route': 'local-qwen'}, os.environ['TARE_TRIAL_KEY'])
class ReadOnly(base):
    def do_POST(self):
        self.close_connection = True
        self.error(405, 'qualification only accepts discovery GET requests')
    def do_GET(self):
        if self.path != '/v1/models':
            return self.error(404, 'not found')
        return super().do_GET()
server = ThreadingHTTPServer(('127.0.0.1', 0), ReadOnly)
ready = Path(os.environ['TARE_TRIAL_READY'])
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid': os.getpid(), 'port': server.server_port,
                                 'bytecode_disabled': sys.dont_write_bytecode}))
temporary.replace(ready)
try:
    server.serve_forever(poll_interval=.1)
finally:
    server.server_close()
'''


@contextmanager
def catalog_peer(home, identifier, folder, deadline):
    """Own one loopback-only discovery worker; no provider, user state or tool replay."""
    import urllib.request
    folder.mkdir(parents=True)
    release = release_at(home, identifier)
    env = clean_env(folder, release)
    env.update(TARE_STORAGE_ROOT=str(folder/'state'), TARE_PROFILE_DIR=str(folder/'profiles'),
               LOCALAPPDATA=str(folder/'appdata'), CODEX_HOME=str(folder/'no-codex'),
               PYTHONDONTWRITEBYTECODE='1', PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring',
               TARE_CLI_DISCOVERY='0', TARE_STORAGE_AUTO=str(folder/'storage-auto.json'),
               TARE_RELEASE_ID=identifier,
               TARE_TRIAL_KEY=uuid.uuid4().hex, TARE_TRIAL_READY=str(folder/'ready.json'))
    for key in ('MODE', 'ENVIRONMENT', 'GPU'):
        env['TARE_'+key+'_STORE'] = str(folder/(key.lower()+'.json'))
    for key in list(env):
        if key.endswith(('API_KEY', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY', 'SESSION_TOKEN')):
            env.pop(key)
    # The worker only exposes authenticated discovery; it cannot forward POSTs.
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env[key] = 'http://127.0.0.1:9'
    argv = [str(python_at(release)), '-I', '-B', '-c', _CATALOG_PROBE]
    if os.name == 'nt':
        # Windows venv python.exe is a redirector: its PID is not the worker's.
        # Use the same base interpreter with only this verified release's packages;
        # -I -S excludes checkouts, user site hooks and environment Python paths.
        argv = [str(Path(sys._base_executable).resolve()), '-I', '-S', '-B', '-c',
                'import sys; sys.path.insert(0, sys.argv.pop(1));\n'+_CATALOG_PROBE,
                str(release/'venv/Lib/site-packages')]
    process = subprocess.Popen(argv, env=env,
        cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    try:
        ready = folder/'ready.json'
        while not ready.exists():
            if process.poll() is not None:
                raise OSError('catalog worker exited with code '+str(process.returncode))
            if time.monotonic() >= deadline:
                raise TimeoutError('catalog worker startup deadline')
            time.sleep(.05)
        identity = json.loads(ready.read_bytes())
        if (identity.get('pid') != process.pid or type(identity.get('port')) is not int
                or not 1 <= identity['port'] <= 65535 or identity.get('bytecode_disabled') is not True):
            raise ValueError('catalog worker identity mismatch')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def fetch():
            remaining = deadline-time.monotonic()
            if remaining <= 0 or process.poll() is not None:
                raise TimeoutError('catalog worker no longer available')
            request = urllib.request.Request('http://127.0.0.1:'+str(identity['port'])+'/v1/models',
                headers={'Authorization': 'Bearer '+env['TARE_TRIAL_KEY']})
            with opener.open(request, timeout=min(3, remaining)) as reply:
                raw = reply.read(65537)
            if len(raw) > 65536:
                raise ValueError('catalog response exceeds 64 KiB')
            return json.loads(raw)
        yield fetch
    finally:
        if process.poll() is None:
            process.terminate()  # only this Popen-owned, read-only worker
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def qualify_transition(home, incumbent, candidate):
    """Bounded pre-promotion shadow/canary. The caller owns the installation lock."""
    from contextlib import ExitStack
    deadline = time.monotonic()+30
    stage = 'incumbent'
    receipt = {'schema': 'tare.tools/catalog-transition/1', 'scope': 'read-only model catalog',
               'phase': 'startup', 'selected': 'incumbent'}
    try:
        with tempfile.TemporaryDirectory(dir=home, prefix='catalog-trial-') as folder, ExitStack() as workers:
            root = Path(folder)
            old = workers.enter_context(catalog_peer(home, incumbent, root/'N', deadline))
            stage = 'candidate'
            new = workers.enter_context(catalog_peer(home, candidate, root/'N1', deadline))
            stage = 'trial'
            receipt = run_catalog_trial(old, new)
            stage = 'cleanup'
    except (OSError, ValueError) as exc:
        receipt.update(status='REJECTED' if stage == 'candidate' else 'INCONCLUSIVE', selected='incumbent',
                       reason=stage+' discovery worker failed: '+type(exc).__name__)
    receipt.update(incumbent=incumbent, candidate=candidate,
                   at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    write_json(home/'last-catalog-trial.json', receipt)
    return receipt


def catalog_contract(payload):
    """Normalize only the discovery contract, never model quality or tool output."""
    if not isinstance(payload, dict) or payload.get('object') != 'list':
        raise ValueError('catalog must be a list object')
    data = payload.get('data')
    if not isinstance(data, list) or not 1 <= len(data) <= 256:
        raise ValueError('catalog must contain 1..256 models')
    models = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError('model must be an object')
        identifier = item.get('id')
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise ValueError('invalid model id')
        if identifier in models:
            raise ValueError('duplicate model id')
        if item.get('object') != 'model':
            raise ValueError('invalid model object')
        owner = item.get('owned_by')
        if not isinstance(owner, str) or not owner:
            raise ValueError('invalid model owner')
        if 'context_window' not in item:
            raise ValueError('missing context window')
        context = item['context_window']
        if context is not None and (type(context) is not int or context <= 0):
            raise ValueError('invalid context window')
        model = {'id': identifier, 'object': 'model', 'owned_by': owner,
                 'context_window': context}
        if 'tare_local' in item:
            if type(item['tare_local']) is not bool:
                raise ValueError('invalid tare_local flag')
            model['tare_local'] = item['tare_local']
        models[identifier] = model
    return {identifier: models[identifier] for identifier in sorted(models)}


def run_catalog_trial(incumbent, candidate, *, samples=10, canary_percent=20, max_errors=0):
    """Compare read-only discovery callbacks; never activate or invoke a model."""
    if type(samples) is not int or not 5 <= samples <= 50:
        raise ValueError('samples must be an integer in 5..50')
    if type(canary_percent) is not int or not 1 <= canary_percent <= 50:
        raise ValueError('canary_percent must be an integer in 1..50')
    allocated = samples * canary_percent // 100
    if allocated < 1:
        raise ValueError('allocation must include a candidate sample')
    if type(max_errors) is not int or not 0 <= max_errors <= 2 or max_errors >= allocated:
        raise ValueError('max_errors must be 0..2 and below candidate allocation')
    counts = {'incumbent': 0, 'candidate': 0}
    routed = {'incumbent': 0, 'candidate': 0}
    digests = {'incumbent': None, 'candidate': None}
    comparisons = errors = 0
    phase = 'shadow'

    def receipt(status, reason):
        return {'schema': 'tare.tools/catalog-transition/1',
                'scope': 'read-only model catalog', 'phase': phase,
                'status': status, 'selected': 'candidate' if status == 'PASS' else 'incumbent',
                'reason': reason,
                'requested': {'samples': samples, 'canary_percent': canary_percent,
                              'max_errors': max_errors, 'incumbent': samples - allocated,
                              'candidate': allocated},
                'actual': counts.copy(), 'routed': routed.copy(),
                'comparisons': comparisons, 'errors': errors, 'digests': digests.copy()}

    def fetch(name, callback):
        counts[name] += 1
        try:
            normalized = catalog_contract(callback())
        except (OSError, ValueError, TimeoutError):
            return None
        encoded = json.dumps(normalized, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True).encode('ascii')
        digests[name] = hashlib.sha256(encoded).hexdigest()
        return normalized

    def compatible(old, new):
        nonlocal comparisons
        comparisons += 1
        return all(identifier in new and
                   all(new[identifier][key] == model[key] for key in ('object', 'owned_by'))
                   for identifier, model in old.items())

    old = fetch('incumbent', incumbent)
    if old is None:
        return receipt('INCONCLUSIVE', 'incumbent catalog unavailable or malformed')
    new = fetch('candidate', candidate)
    if new is None or not compatible(old, new):
        errors += 1
        return receipt('REJECTED', 'candidate catalog unavailable, malformed or incompatible')

    phase = 'canary'
    for i in range(samples):
        candidate_slot = (i + 1) * canary_percent // 100 > i * canary_percent // 100
        if not candidate_slot:
            routed['incumbent'] += 1
            if fetch('incumbent', incumbent) is None:
                return receipt('INCONCLUSIVE', 'incumbent catalog unavailable or malformed')
            continue
        # A fresh baseline is a comparison probe, not an extra routed request.
        old = fetch('incumbent', incumbent)
        if old is None:
            return receipt('INCONCLUSIVE', 'incumbent catalog unavailable or malformed')
        routed['candidate'] += 1
        new = fetch('candidate', candidate)
        if new is None or not compatible(old, new):
            errors += 1
            if errors > max_errors:
                return receipt('REJECTED', 'candidate error threshold exceeded; route rolled back')
    return receipt('PASS', 'read-only catalog compatibility trial passed')


class _ObservationError(ValueError):
    pass


def _observation_json(path):
    """Bound every local evidence read; retain raw bytes for pointer/digest checks."""
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        raise _ObservationError('evidence_missing') from None
    except OSError:
        raise _ObservationError('evidence_io_error') from None
    if len(raw) > 65536:
        raise _ObservationError('evidence_oversized')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise _ObservationError('evidence_malformed') from None
    if not isinstance(value, dict):
        raise _ObservationError('evidence_malformed')
    return value, raw


def transition_status(home):
    """Observe existing owners only; neither reconcile nor acquire a writing lock."""
    home = Path(home).resolve()
    report = {'schema': 'tare.tools/transition-status/1', 'completed': False,
              'current': None, 'previous': None, 'serve': 'unknown', 'reasons': []}
    reasons = report['reasons']
    try:
        selected, pointer = _observation_json(home/'active.json')
    except _ObservationError as exc:
        reasons.append(str(exc))
        return report
    current, previous = selected.get('current'), selected.get('previous')
    valid_id = lambda value: isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
    if not valid_id(current) or (previous is not None and not valid_id(previous)) or current == previous:
        reasons.append('pointer_invalid')
    else:
        report.update(current=current, previous=previous)
        receipt = selected.get('last_transition')
        if not isinstance(receipt, dict):
            reasons.append('receipt_missing')
        elif receipt.get('to') != current or receipt.get('from') != previous:
            reasons.append('receipt_pair_mismatch')
        elif receipt.get('operation') in ('rollback', 'auto_rollback'):
            reasons.append(receipt['operation'])
        elif previous is None and receipt.get('operation') == 'install':
            reasons.append('initial_install')
        elif receipt.get('operation') != 'update' or previous is None:
            reasons.append('receipt_operation_invalid')
        else:
            try:
                release = release_at(home, current)
                prefix = 'releases/'+current[:16]+'/'
                trial = receipt.get('catalog_trial')
                if not isinstance(trial, dict):
                    raise _ObservationError('catalog_trial_missing')
                if (receipt.get('qualification') != prefix+'ready.json'
                        or trial.get('path') != prefix+'catalog-trial.json'):
                    raise _ObservationError('evidence_path_invalid')
                # Never dereference the receipt's supplied path, including symlink escapes.
                for name in ('ready.json', 'catalog-trial.json'):
                    if (release/name).resolve() != release.resolve()/name:
                        raise _ObservationError('evidence_path_invalid')
                catalog, raw = _observation_json(release/'catalog-trial.json')
                if hashlib.sha256(raw).hexdigest() != trial.get('sha256'):
                    raise _ObservationError('catalog_digest_mismatch')
                if (catalog.get('schema') != 'tare.tools/catalog-transition/1'
                        or catalog.get('status') != 'PASS'
                        or catalog.get('phase') != 'canary'
                        or catalog.get('selected') != 'candidate'
                        or catalog.get('incumbent') != previous
                        or catalog.get('candidate') != current):
                    raise _ObservationError('catalog_trial_mismatch')
                ready, _ = _observation_json(release/'ready.json')
                if ready.get('bundle') != current:
                    raise _ObservationError('ready_identity_mismatch')
            except _ObservationError as exc:
                reasons.append(str(exc))
            except (OSError, ValueError):
                reasons.append('evidence_path_invalid')
    try:
        health = serve_health()
    except (OSError, ValueError, TimeoutError):
        health = None
    if health is None:
        reasons.append('health_unavailable')
    elif not _known_server(home, report, health):
        report['serve'] = 'foreign_or_legacy'
        reasons.append('server_identity_mismatch')
    else:
        report['serve'] = 'selected' if health['release'] == current else 'incumbent'
        report['server'] = _identity(health)
        if health['release'] != current:
            reasons.append('incumbent_still_serving')
        if health.get('draining') is not False or health.get('stopping') is not False:
            reasons.append('server_not_settled')
    try:
        _, after = _observation_json(home/'active.json')
        if after != pointer:
            reasons[:] = ['pointer_changed']
            report.pop('server', None)
            report['serve'] = 'unknown'
    except _ObservationError:
        reasons[:] = ['pointer_recheck_failed']
        report.pop('server', None)
        report['serve'] = 'unknown'
    report['completed'] = not reasons
    if report['completed']:
        reasons.append('qualified_rotation_complete')
    return report


def console_arguments(argv):
    """The stdlib-only PATH manager forwards TUI flags before a command name."""
    values=list(sys.argv[1:] if argv is None else argv)
    options=('subscription_only','local_only','api_only','pro_planner','prefer_api',
             'prefer_subscription','prefer_local','disable_judge','prefer_cli','cli_only','coordinator',
             'planner','implementer','explorer','researcher','auditor','unlimited')
    flags={prefix+name for name in options for prefix in ('--',)} | {'--'+n.replace('_','-') for n in options}
    index=0
    while index<len(values):
        if values[index]=='--home':index+=2
        elif values[index].startswith('--home='):index+=1
        else:break
    if index<len(values) and values[index].split('=',1)[0] in flags|{'--root','--storage-root'}:
        values.insert(index,'console')
    return values


def model_help(home):
    """Ask only the selected release's read-only helper; never bootstrap/install it."""
    unavailable = ('Local catalog unavailable; no configured example proved. '
                   'Use tare models after selecting a complete release. '
                   'PROVIDER/DEPLOYMENT@REASONING is syntax, not a proved connection.')
    try:
        identifier = state(home)['current']
        if not identifier:
            return unavailable
        release = release_at(home, identifier)
        python = python_at(release)
        if not (release/'ready.json').is_file() or not python.is_file():
            return unavailable
        env = clean_env(home, release)
        env['TARE_STORAGE_ROOT'] = str(home/'state')
        env['TARE_RELEASE_ID'] = identifier
        command = [str(python), '-I', '-B', '-c',
                   'import os; from pathlib import Path; from relay import relay_mesh; '
                   'relay_mesh.rebind_root(Path(os.environ["TARE_MANAGED_HOME"])/"control"); '
                   'from relay.model_choices import help_summary; print(help_summary())']
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15, check=True)
        return result.stdout.strip() or unavailable
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return unavailable


def main(argv=None):
    values = console_arguments(argv)
    home_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    home_parser.add_argument('--home', type=Path, default=product_home())
    selected, _ = home_parser.parse_known_args(values)
    local_help = model_help(selected.home.resolve()) if any(v in ('--help', '-h') for v in values) else ''
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='TUI: tare [PROJECT] [--subscription_only | --local_only | --api_only] '
            '[--prefer_api | --prefer_subscription | --prefer_local] [--pro_planner] [--disable_judge]. '
            'These switches apply only until this TUI exits. Details: tare console --help. '
            '--unlimited [calls time money compactions stagnation] removes selected execution ceilings; '
            'no list means all five. Example: tare --unlimited calls money --prefer_subscription. '
            'Exact temporary pins: --planner, --implementer, --explorer, --researcher, --auditor '
            'PROVIDER/DEPLOYMENT@REASONING (syntax placeholders, not a proved connection); auditor also selects plan-auditor. No fallback. '
            'Author is registered model identity, not connection provider. '
            'Explicit --coordinator PROVIDER/DEPLOYMENT@REASONING uses existing coordinator policy, separate from temporary role pins; '
            'discover alternatives with tare models --role coordinator. Saved conversation selections remain preserved. '
            'Offline alternatives: tare models --role planner --via cli --include-unconfigured.\n'+local_help)
    parser.add_argument('--home', type=Path, default=product_home())
    parser.add_argument('command', nargs='?', default='console')
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args(values)
    home = args.home.resolve()
    try:
        if args.command in {'install', 'update'}:
            if len(args.args) != 1:
                raise ValueError('Usage: install.py install|update <bundle-folder>')
            result = install(Path(args.args[0]), home)
        elif args.command == 'rollback':
            result = rollback(home)
        elif args.command == 'auto-update':
            if args.args not in ([], ['--resume']):
                raise ValueError('Usage: tare auto-update [--resume]')
            result = auto_update(home, repo=os.environ.get('TARE_UPDATE_REPO', UPDATE_REPO),
                                 resume=bool(args.args))
        elif args.command == 'transition-status':
            if args.args:
                raise ValueError('Usage: install.py [--home <installation>] transition-status')
            result = transition_status(home)
        elif args.command in {'status', 'version', '--version'}:
            result = status(home)
        else:
            command, env = release_command(home, [args.command, *args.args])
            return subprocess.call(command, env=env)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print('tare installation: '+str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

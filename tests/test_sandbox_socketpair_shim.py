"""The sitecustomize that gives Python an AF_UNIX socketpair in tare's Windows sandbox (relay/sandbox-python).

Child Pythons run it outside the sandbox; the last test runs one through the real MXC launcher when the machine
supports it."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SHIM = Path(__file__).resolve().parents[1]/'relay'/'sandbox-python'
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='the shim acts only on Windows')
PROBE = r'''
import asyncio, json, socket, sys
a, b = socket.socketpair()
a.sendall(b'x')
async def work(spawn):
    loop = asyncio.get_running_loop()
    value = [await loop.run_in_executor(None, sum, (1, 2))]  # a thread wakes the loop through its self-pipe
    if spawn:
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'print(42)', stdout=asyncio.subprocess.PIPE)
        value.append((await child.communicate())[0].decode().strip())
    return value
denied = []
for host, port in json.loads(sys.argv[1]) if sys.argv[1:] else ():
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        denied.append(host)
print(json.dumps({'family': int(a.family), 'echo': b.recv(1).decode(), 'patched': socket.socketpair.__module__,
    'proactor': asyncio.run(work(True)), 'selector': asyncio.run(work(False), loop_factory=asyncio.SelectorEventLoop),
    'project': getattr(sys.modules.get('sitecustomize'), 'PROJECT', None), 'denied': denied}))
'''
SHIMMED = {'family': 1, 'echo': 'x', 'patched': 'sitecustomize', 'proactor': [3, '42'], 'selector': [3],
           'project': None, 'denied': []}


def child_env(switch, path=(), **extra):
    env = {k: v for k, v in os.environ.items() if k.upper() not in ('PYTHONPATH', 'TARE_SANDBOX_SOCKETPAIR')}
    env.update(PYTHONPATH=os.pathsep.join([str(SHIM), *map(str, path)]), PYTHONUTF8='1', **extra)
    if switch:
        env['TARE_SANDBOX_SOCKETPAIR'] = '1'
    return env


def probe(env, code=PROBE):
    done = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stderr


def test_with_the_switch_socketpair_is_an_af_unix_pair_and_asyncio_runs_on_both_loops():
    assert probe(child_env(True)) == (SHIMMED, '')


def test_without_the_switch_socketpair_is_untouched():
    value, stderr = probe(child_env(False))
    assert (value['family'], value['patched'], stderr) == (2, 'socket', '')  # AF_INET: CPython's own fallback


@pytest.mark.parametrize('switch', [True, False])
def test_a_project_sitecustomize_later_on_the_path_still_runs(tmp_path, switch):
    (tmp_path/'sitecustomize.py').write_text('PROJECT = "ran"\n')
    value, _ = probe(child_env(switch, [tmp_path]))
    assert value['project'] == 'ran' and value['family'] == (1 if switch else 2)


def test_a_temp_too_long_for_sun_path_uses_its_8_3_form(tmp_path):
    import ctypes
    folder = tmp_path/('a-long-folder-name-'+'x'*100)
    folder.mkdir()
    short = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(folder), short, 32768) or len(short.value) > 95:
        pytest.skip('this volume has no 8.3 names short enough')
    assert probe(child_env(True, TEMP=str(folder))) == (SHIMMED, '')
    assert not any(folder.iterdir())  # the socket files are gone


@pytest.mark.parametrize('case,reason', [('long', 'too long for an AF_UNIX path'),
                                         ('peer', 'listener of another process')])
def test_an_unusable_pair_falls_back_to_the_original_and_says_why_once(tmp_path, case, reason):
    # A folder that does not exist has no 8.3 form; another PID is what a process racing for the socket would show.
    extra = {'TEMP': str(tmp_path/('x'*120))} if case == 'long' else {}
    lie = 'import os; real = os.getpid; os.getpid = lambda: real() + 4\n' if case == 'peer' else ''
    value, stderr = probe(child_env(True, **extra), lie+'import socket; socket.socketpair()\n'+PROBE)
    assert value['family'] == 2 and stderr.count('tare sandbox:') == 1 and reason in stderr


def test_python_still_starts_when_the_shim_cannot_install(tmp_path):
    (tmp_path/'ctypes.py').write_text('raise ImportError("no ctypes in this fixture")\n')
    env = child_env(True)
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), str(SHIM)])
    done = subprocess.run([sys.executable, '-c', 'import socket; print(socket.socketpair.__module__)'], env=env,
                          capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stdout.strip()) == (0, 'socket') and 'shim not installed' in done.stderr


def psec_supported():
    try:
        from compute_plane.windows_mxc import MxcEnvironment, MxcPolicy
        MxcEnvironment(MxcPolicy((os.environ['SystemRoot'],), ())).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(sys.platform != 'win32' or not psec_supported(), reason='needs Windows process security (PSEC)')
def test_in_the_real_sandbox_asyncio_runs_and_loopback_stays_denied(tmp_path, monkeypatch):
    import socket
    # MXC puts the sandbox's own folders, its %TEMP% included, under this process's LOCALAPPDATA, which conftest
    # moves into the pytest tree: too long for an AF_UNIX path. The real one is what a protected command gets.
    monkeypatch.setenv('LOCALAPPDATA', str(Path(os.environ['USERPROFILE'])/'AppData'/'Local'))
    from compute_plane.managed_process import run_process
    from compute_plane.windows_mxc import MxcPolicy, WindowsMxcProfile
    from compute_plane.workspace import isolated_child_environment
    project, dest = tmp_path/'project', tmp_path/'out'
    project.mkdir(); dest.mkdir()
    (project/'probe.py').write_text(PROBE)
    listeners = [socket.create_server(('127.0.0.1', 0)), socket.create_server(('::1', 0), family=socket.AF_INET6)]
    targets = [s.getsockname()[:2] for s in listeners]
    roots = sorted({str(Path(p).resolve()) for p in (sys.base_prefix, sys.prefix)})
    policy = MxcPolicy((os.environ['SystemRoot'], *roots, str(SHIM)), (str(project),), (), registry_read=True)
    env = isolated_child_environment({'PYTHONPATH': str(SHIM), 'TARE_SANDBOX_SOCKETPAIR': '1', 'PYTHONUTF8': '1'})
    try:
        for listener, target in zip(listeners, targets):
            socket.create_connection(target, timeout=3).close()  # the host itself reaches each listener
            listener.accept()[0].close()
        process = run_process([sys.executable, str(project/'probe.py'), json.dumps(targets)], cwd=str(project),
            dest=str(dest), timeout=25, env=env, max_output=1024*1024,
            process_factory=WindowsMxcProfile(policy, cwd=str(project), environment=env))
        for listener in listeners:
            listener.settimeout(0.5)
            with pytest.raises(TimeoutError):
                listener.accept()  # nothing from inside reached the host's loopback
    finally:
        for listener in listeners:
            listener.close()
    assert process['exit_code'] == 0, (dest/'stderr.txt').read_text(errors='replace')
    value = json.loads((dest/'stdout.txt').read_text().splitlines()[-1])
    assert value == {**SHIMMED, 'denied': ['127.0.0.1', '::1']}

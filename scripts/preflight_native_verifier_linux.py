"""Linux/WSL verifier preflight using published Kernel and the existing argv seam.

No inference. The host paths in probe.py are deliberate canaries for this test;
they must never be copied into a task card or its project checkout.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import signal
import threading
import time
import uuid
import subprocess

from compute_plane.native_capabilities import NativeCapabilities
from tare_tools_agent_runtime import BindingRef, CancellationToken, EffectRequest, ExecutionControl, ToolCall


def verifier_argv(bwrap, project, command):
    """Study recipe for offline stdlib Python tests on merged-/usr Linux."""
    return [str(bwrap), '--unshare-all', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--clearenv', '--ro-bind', '/usr', '/usr',
            '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
            '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
            '--size', '16777216', '--tmpfs', '/tmp', '--ro-bind', str(project), '/work',
            '--chdir', '/work', '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--setenv', 'TMPDIR', '/tmp', '--', *command]


def live_marker(marker, *, detached_only=False):
    found = set()
    for folder in Path('/proc').iterdir():
        if not folder.name.isdecimal():
            continue
        try:
            args = (folder / 'cmdline').read_bytes().split(b'\0')
            detached = b'import time; time.sleep(60)' in args
            if detached_only:
                fields = (folder / 'stat').read_text().rsplit(')', 1)[1].split()
                detached = detached and int(fields[2]) == int(folder.name)
            if marker.encode() in args and (not detached_only or detached):
                found.add(int(folder.name))
        except (OSError, ProcessLookupError):
            pass
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--bwrap', type=Path, required=True)
    opts = parser.parse_args()
    root, bwrap = opts.workspace.resolve(), opts.bwrap.resolve()
    if platform.system() != 'Linux' or not bwrap.is_file():
        parser.error('Linux and an explicit Bubblewrap executable are required')
    bwrap_version = subprocess.check_output([str(bwrap), '--version'], text=True, timeout=5).strip()
    root.mkdir(parents=True, exist_ok=False)
    scratch = root / 'scratch'; scratch.mkdir()
    oracle = root / 'evaluator'; oracle.mkdir()
    sentinel = oracle / 'private.txt'; sentinel.write_text(uuid.uuid4().hex)
    secret_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    results = []
    binding = BindingRef('preflight', 'local', 'controller', 1, 'test-only')
    os.environ['TARE_PREFLIGHT_PRIVATE_CANARY'] = uuid.uuid4().hex

    def invoke(label, project, command, *, treatment=False, seconds=10, cancel_after=None):
        token = CancellationToken()
        tools = NativeCapabilities(session_id=label, binding=binding,
            repositories={'project': str(project)}, writable={'project': ['.']},
            verifiers={'tests': {'repository': 'project', 'argv': verifier_argv(bwrap, project, command)}},
            scratch_root=scratch, specgraph_edit_context=False,
            project_context=object() if treatment else None,
            capabilities=['read', 'list', 'search', 'edit', 'verify'] + (['context'] if treatment else []))
        control = ExecutionControl(binding, token, time.monotonic() + seconds)
        timer = threading.Timer(cancel_after, token.cancel) if cancel_after else None
        if timer: timer.start()
        try:
            result = tools.execute(EffectRequest(label, label + '-effect', ToolCall(label + '-call', 'verify', '{"name":"tests"}')), control)
        finally:
            if timer: timer.cancel(); timer.join()
        row = {'case': label, **asdict(result), 'output': json.loads(result.output_json)}
        del row['output_json']
        results.append(row)
        (root / 'results.json').write_text(json.dumps(results, indent=2))
        return row

    arms = []
    for arm in ('control', 'treatment'):
        project = root / arm; project.mkdir(); arms.append(project)
        (project / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
        (project / 'test_calc.py').write_text('import unittest\nfrom calc import add\nclass Addition(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')
        result = invoke(arm + '-ordinary-pass', project, ['/usr/bin/python3', '-B', '-m', 'unittest', 'discover'], treatment=arm == 'treatment')
        assert result['status'] == 'succeeded', result
        (project / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        result = invoke(arm + '-ordinary-fail', project, ['/usr/bin/python3', '-B', '-m', 'unittest', 'discover'], treatment=arm == 'treatment')
        assert result['status'] == 'failed' and 'AssertionError' in result['output']['output'], result
    command = ['/usr/bin/python3', '-B', '-m', 'unittest', 'discover']
    assert verifier_argv(bwrap, arms[0], command) == [str(arms[0]) if s == str(arms[1]) else s for s in verifier_argv(bwrap, arms[1], command)]

    project = arms[0]
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
    port = listener.getsockname()[1]
    socket.create_connection(('127.0.0.1', port), timeout=1).close()
    assert sentinel.read_bytes() and (arms[1] / 'calc.py').read_bytes()
    probe = '''import json, os, pathlib, socket
pathlib.Path('/tmp/escape').symlink_to(PATHS[0])
checks = {}
for value in PATHS:
    try:
        pathlib.Path(value).read_bytes()
    except OSError:
        checks[value] = True
    else:
        checks[value] = False
    try:
        pathlib.Path(value).write_text('forbidden')
    except OSError:
        checks['no_write:' + value] = True
    else:
        checks['no_write:' + value] = False
try:
    pathlib.Path('/work/changed.txt').write_text('forbidden')
except OSError:
    checks['project_readonly'] = True
else:
    checks['project_readonly'] = False
checks['env_private_absent'] = 'TARE_PREFLIGHT_PRIVATE_CANARY' not in os.environ
checks['tmp_fresh'] = not pathlib.Path('/tmp/previous.txt').exists()
pathlib.Path('/tmp/previous.txt').write_text('temporary')
checks['tmp_writable'] = pathlib.Path('/tmp/previous.txt').read_text() == 'temporary'
for location in ('/home', '/mnt/c', '/run', '/sys'):
    checks['hidden:' + location] = not pathlib.Path(location).exists()
try:
    socket.create_connection(('127.0.0.1', PORT), timeout=0.2).close()
except OSError:
    checks['host_network_unreachable'] = True
else:
    checks['host_network_unreachable'] = False
print(json.dumps(checks, sort_keys=True))
assert all(checks.values()), checks
'''
    paths = [str(sentinel), '../evaluator/private.txt', '/tmp/escape',
             str(arms[1] / 'calc.py'), '/proc/1/root' + str(sentinel),
             f'/proc/{os.getpid()}/root' + str(sentinel)]
    (project / 'probe.py').write_text('PATHS = ' + repr(paths) + '\nPORT = ' + repr(port) + '\n' + probe)
    try:
        for iteration in range(2):
            result = invoke('isolation-' + str(iteration), project, ['/usr/bin/python3', '-B', 'probe.py'])
            assert result['status'] == 'succeeded', result
    finally:
        listener.close()

    (project / 'detach.py').write_text('''import subprocess, sys, time
subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(60)', sys.argv[1]], start_new_session=True)
print('detached-started', flush=True)
time.sleep(float(sys.argv[2]))
''')
    for mode in ('normal-exit', 'deadline', 'cancellation'):
        marker = 'tare-preflight-' + uuid.uuid4().hex
        observed = set(); detached_observed = set(); stop = threading.Event()
        def watch():
            while not stop.wait(0.03):
                observed.update(live_marker(marker))
                detached_observed.update(live_marker(marker, detached_only=True))
        watcher = threading.Thread(target=watch); watcher.start()
        try:
            result = invoke(mode, project, ['/usr/bin/python3', '-B', 'detach.py', marker, '0.4' if mode == 'normal-exit' else '60'],
                seconds=2 if mode == 'deadline' else 10, cancel_after=1 if mode == 'cancellation' else None)
        finally:
            stop.set(); watcher.join()
        until = time.monotonic() + 3
        remaining = live_marker(marker)
        while remaining and time.monotonic() < until:
            time.sleep(0.03); remaining = live_marker(marker)
        result['observed_marker_pids'] = sorted(observed)
        result['observed_detached_session_leaders'] = sorted(detached_observed)
        result['remaining_marker_pids'] = sorted(remaining)
        (root / 'results.json').write_text(json.dumps(results, indent=2))
        # A failed probe must not leave its own 60-second child behind. Preserve
        # the failure above; emergency cleanup never turns it into a PASS.
        for pid in remaining:
            try:
                descriptor = os.pidfd_open(pid)
                try:
                    if pid in live_marker(marker):
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                finally:
                    os.close(descriptor)
            except ProcessLookupError:
                pass
        assert detached_observed and not remaining, result
        assert result['output']['process']['cleanup_verified'], result
        assert result['status'] == ('succeeded' if mode == 'normal-exit' else 'unknown'), result
        if mode == 'cancellation': assert result['output']['process']['cancelled'], result
        if mode == 'deadline':
            assert result['output']['process']['cancelled'] or result['output']['process']['timeout'], result
        (root / 'results.json').write_text(json.dumps(results, indent=2))
    result = invoke('command-missing', project, ['/usr/bin/nonexistent-tare-preflight'])
    assert result['status'] == 'failed' and result['output']['returncode'] != 0, result
    (project / 'noisy.py').write_text('import os, time\nos.write(1, b"x" * 1100000)\ntime.sleep(10)\n')
    result = invoke('output-limit', project, ['/usr/bin/python3', '-B', 'noisy.py'])
    assert result['status'] == 'unknown' and result['output']['process']['output_limit'], result
    assert result['output']['process']['cleanup_verified'], result
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == secret_hash
    assert not (project / 'changed.txt').exists()
    assert not any(scratch.iterdir()), 'verifier temporary files remain'
    summary = {'status': 'PASS', 'cases': len(results), 'model_calls': 0,
        'bubblewrap': bwrap_version, 'bubblewrap_executable_sha256': hashlib.sha256(bwrap.read_bytes()).hexdigest(),
        'python': platform.python_version(), 'kernel_release': platform.release(),
        'scope': 'Linux/WSL offline stdlib Python via NativeCapabilities.verify; no universal sandbox claim',
        'parity': 'identical verifier command/mount policy except selected arm root',
        'cleanup': 'Kernel process-group receipts plus independent host observation of detached descendants'}
    (root / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()

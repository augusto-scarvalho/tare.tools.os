"""Reusable training entrypoints use the real lease with a fixture gateway."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_gpu_lease_run import CLI_PATH, ROOT, launch_wrapper, requires_linux, serving, SharedGpuLease

pytestmark = requires_linux
CONTEXT = 'TARE_GPU_TRAINING_LEASE'


def test_nested_commands_reuse_one_reservation_and_preserve_exit(tmp_path):
    lock = tmp_path/'gpu.lock'
    marker = tmp_path/'ran'
    child = 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("ok"); sys.exit(7)'
    with serving(lock_path=lock) as rt:
        with launch_wrapper(['--gateway', rt.endpoint, '--gpu-lock', str(lock),
                             sys.executable, str(CLI_PATH), sys.executable, str(CLI_PATH),
                             sys.executable, '-c', child, str(marker)]) as wrapper:
            assert wrapper.wait() == 7
        assert marker.read_text() == 'ok'
        events = [row['event'] for row in wrapper.events]
        assert events.count('acquired') == 1 and events.count('lease_reused') == 2
        assert events.count('lease_released') == 1
        assert not SharedGpuLease(lock).status()['held']


def test_guarded_script_works_directly_and_inside_gpu_run(tmp_path):
    lock = tmp_path/'gpu.lock'
    marker = tmp_path/'args.json'
    with serving(lock_path=lock) as rt:
        # Relocate the installed entrypoint for this fixture, preserving HOME.
        launcher = tmp_path/'gpu-run'
        launcher.write_text('#!/bin/sh\nexec '+shlex.join([sys.executable, str(CLI_PATH),
            '--gateway', rt.endpoint, '--gpu-lock', str(lock)])+' "$@"\n')
        launcher.chmod(0o700)
        guard = tmp_path/'guard.sh'
        guard.write_text((ROOT/'tare_node/gpu_training_guard.sh').read_text().replace(
            '"$HOME/.local/bin/gpu-run"', shlex.quote(str(launcher))))
        script = tmp_path/'training with spaces.sh'
        body = 'import sys,json; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))'
        script.write_text('set -e\nsource '+shlex.quote(str(guard))+' || exit $?\n'+
            shlex.join([sys.executable, '-c', body, str(marker)])+' "$@"\n')
        for command in [['bash', str(script)], [str(launcher), str(script)]]:
            result = subprocess.run([*command, 'with spaces', '--epochs', '8'],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert json.loads(marker.read_text()) == ['with spaces', '--epochs', '8']
            rows = [json.loads(line) for line in result.stderr.splitlines()]
            assert sum(row['event'] == 'acquired' for row in rows) == 1
            assert not SharedGpuLease(lock).status()['held']


@pytest.mark.parametrize('kind', ['malformed', 'unheld', 'wrong_nonce', 'unrelated_owner', 'different_lock'])
def test_invalid_inherited_claim_never_executes_child(tmp_path, kind):
    lock = tmp_path/'gpu.lock'
    marker = tmp_path/'should-not-exist'
    lease = SharedGpuLease(lock)
    with lease.hold('image', 'fixture') as receipt:
        context = {'path': str(lock), 'gateway': 'http://127.0.0.1:8080',
                   'pid': os.getpid(), 'nonce': receipt['nonce']}
        if kind == 'wrong_nonce': context['nonce'] = 'bad'
        if kind == 'unrelated_owner': context['pid'] = 2
        if kind == 'unheld': context['path'] = str(tmp_path/'unused.lock')
        raw = 'not json' if kind == 'malformed' else json.dumps(context)
        options = ['--gpu-lock', str(tmp_path/'different.lock')] if kind == 'different_lock' else []
        result = subprocess.run([sys.executable, str(CLI_PATH), *options, sys.executable, '-c',
            'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(marker)],
            env={**os.environ, CONTEXT: raw}, capture_output=True, text=True, timeout=5)
        assert result.returncode == 125 and not marker.exists()
        assert 'Inherited GPU lease is invalid' in result.stderr
        assert lease.status()['held']


def test_check_without_parent_does_not_acquire_or_contact_gateway(tmp_path):
    lock = tmp_path/'absent.lock'
    env = {k:v for k,v in os.environ.items() if k != CONTEXT}
    result = subprocess.run([sys.executable, str(CLI_PATH), '--check-inherited',
        '--gpu-lock', str(lock), '--gateway', 'http://127.0.0.1:1'], env=env,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and not lock.exists()

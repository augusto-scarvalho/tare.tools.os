"""A normal Windows checkout stays clean when verifiers disable global Git config."""
import os
from pathlib import Path
import shutil
import subprocess

from compute_plane.workspace import isolated_child_environment


def test_isolated_verifier_preserves_checkout_normalization_and_exact_byte_exceptions(tmp_path):
    git = shutil.which('git')
    assert git
    isolated = isolated_child_environment()
    trusted_config = tmp_path/'trusted-git-config'
    trusted_config.write_text('[core]\n\tautocrlf = true\n', encoding='utf-8')
    checkout_env = {**isolated, 'GIT_CONFIG_GLOBAL': str(trusted_config)}
    origin, checkout = tmp_path/'origin', tmp_path/'checkout'
    origin.mkdir()

    def run(folder, *args, env=isolated):
        return subprocess.run([git, '-C', str(folder), *args], env=env,
                              capture_output=True, timeout=20, check=True)

    run(origin, 'init', '-q')
    attributes = Path(__file__).resolve().parents[1]/'.gitattributes'
    (origin/'.gitattributes').write_bytes(attributes.read_bytes().replace(b'\r\n', b'\n'))
    (origin/'source.py').write_bytes(b'value = 7\n')
    exact = origin/'docs/archive/integration-recovery-20260908/original.txt'
    exact.parent.mkdir(parents=True)
    exact.write_bytes(b'original\r\nsource bytes\r\n')
    launcher = b'@echo off\r\necho ready\r\n'
    (origin/'RUN_TUI.bat').write_bytes(launcher)
    run(origin, 'add', '.')
    run(origin, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'Fixture source')
    run(origin, 'worktree', 'add', '--detach', str(checkout), 'HEAD', env=checkout_env)
    assert b'\r\n' in (checkout/'source.py').read_bytes()
    assert (checkout/exact.relative_to(origin)).read_bytes() == exact.read_bytes()
    assert (checkout/'RUN_TUI.bat').read_bytes() == launcher
    # A matching checkout stat cache can hide a normalization mismatch. Rehash
    # unchanged bytes deterministically, as after a file is touched by a tool.
    for relative in ('.gitattributes', 'source.py'):
        target = checkout/relative
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    checked = run(checkout, 'diff', '--check')
    assert checked.stdout == b''
    assert run(checkout, 'status', '--porcelain').stdout == b''

    # Normalization must not suppress an actual trailing-whitespace regression.
    (checkout/'source.py').write_bytes(b'value = 7  \r\n')
    bad = subprocess.run([git, '-C', str(checkout), 'diff', '--check'],
                         env=isolated, capture_output=True, timeout=20)
    assert bad.returncode != 0 and b'trailing whitespace' in bad.stdout

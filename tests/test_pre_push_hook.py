"""The pre-push hook checks (and watches) what a push updates, whatever the local ref is called."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which('sh') or next((str(p) for p in [Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Git/usr/bin/sh.exe']
                                if p.is_file()), None)


def git(root, *args):
    return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', *args], cwd=root,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(SH is None, reason='no POSIX shell to run the hook')
@pytest.mark.parametrize('local_ref', ['refs/heads/main', 'HEAD'])  # HEAD: `git push origin HEAD:main`, detached
def test_the_changelog_guard_checks_a_push_from_a_branch_or_a_detached_head(tmp_path, local_ref):
    for path in ('.githooks/pre-push', 'tools/changelog_guard.py', 'CHANGELOG.md', 'relay/push_preflight.py'):
        (tmp_path/path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/path, tmp_path/path)
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path/'relay').mkdir(exist_ok=True)
    (tmp_path/'relay/feature.py').write_text('VALUE = 1\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'code without a changelog entry')
    head = git(tmp_path, 'rev-parse', 'HEAD')
    env = {key: value for key, value in os.environ.items() if key != 'LOCALAPPDATA'}  # no CI watcher
    env['PYTHON'] = sys.executable
    result = subprocess.run([SH, '.githooks/pre-push', 'origin', 'unused-url'], cwd=tmp_path, env=env,
                            input=f'{local_ref} {head} refs/heads/main {base}\n', capture_output=True, text=True)
    assert 'CHANGELOG_GUARD=' in result.stdout + result.stderr, result  # the pushed commits were checked


@pytest.mark.skipif(SH is None, reason='no POSIX shell to run the hook')
@pytest.mark.parametrize('email, remote_ref, refused', [
    ('t@example.org', 'refs/heads/feature', True),
    ('t@example.org', 'refs/tags/v1', True),  # a tag publishes commits too
    ('21094776+me@users.noreply.github.com', 'refs/heads/feature', False),
    ('21094776+me@users.noreply.github.com', 'refs/tags/v1', False),
])
def test_the_hook_refuses_a_pushed_commit_with_a_personal_email(tmp_path, email, remote_ref, refused):
    # The pushed commit carries a changelog entry, so the guard passes and the exit code is the identity check's.
    for path in ('.githooks/pre-push', 'tools/changelog_guard.py', 'relay/push_preflight.py', 'CHANGELOG.md'):
        (tmp_path/path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/path, tmp_path/path)
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'core.hooksPath', '/dev/null')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.email=noreply@github.com', 'commit', '-qm', 'base')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    # An entry the guard accepts (long enough to say something), added without touching the file's line endings:
    # rewriting them made every bullet look new on Windows and hid a too-short entry that Linux refused.
    changelog = (tmp_path/'CHANGELOG.md').read_bytes()
    line_end = b'\r\n' if b'\r\n' in changelog else b'\n'
    heading = b'## Unreleased' + line_end + line_end
    assert heading in changelog
    (tmp_path/'CHANGELOG.md').write_bytes(changelog.replace(
        heading, heading + b'- The notes file records what this hook test pushes.' + line_end, 1))
    (tmp_path/'notes.txt').write_text('x\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', f'user.email={email}', 'commit', '-qm', 'notes')
    head = git(tmp_path, 'rev-parse', 'HEAD')
    env = {key: value for key, value in os.environ.items() if key != 'LOCALAPPDATA'}  # no CI watcher
    env['PYTHON'] = sys.executable
    result = subprocess.run([SH, '.githooks/pre-push', 'origin', 'unused-url'], cwd=tmp_path, env=env,
                            input=f'refs/heads/feature {head} {remote_ref} {base}\n', capture_output=True, text=True)
    if refused:
        assert result.returncode != 0 and f'commit {head[:8]}: author email {email}' in result.stderr, result
    else:
        assert result.returncode == 0 and 'noreply' not in result.stderr, result


@pytest.mark.skipif(SH is None, reason='no POSIX shell to run the hook')
def test_the_hook_passes_an_empty_line_from_a_wrapper_with_nothing_to_push(tmp_path):
    # The machine's publication guard forwards its stdin with printf: an up-to-date push arrives as one empty line.
    for path in ('.githooks/pre-push', 'tools/changelog_guard.py', 'relay/push_preflight.py', 'CHANGELOG.md'):
        (tmp_path/path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/path, tmp_path/path)
    git(tmp_path, 'init', '-q')
    env = {key: value for key, value in os.environ.items() if key != 'LOCALAPPDATA'}  # no CI watcher
    env['PYTHON'] = sys.executable
    result = subprocess.run([SH, '.githooks/pre-push', 'origin', 'unused-url'], cwd=tmp_path, env=env, input='\n',
                            capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr, result

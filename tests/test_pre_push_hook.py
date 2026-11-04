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
    for path in ('.githooks/pre-push', 'tools/changelog_guard.py', 'CHANGELOG.md'):
        (tmp_path/path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/path, tmp_path/path)
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path/'relay').mkdir()
    (tmp_path/'relay/feature.py').write_text('VALUE = 1\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'code without a changelog entry')
    head = git(tmp_path, 'rev-parse', 'HEAD')
    env = {key: value for key, value in os.environ.items() if key != 'LOCALAPPDATA'}  # no CI watcher
    env['PYTHON'] = sys.executable
    result = subprocess.run([SH, '.githooks/pre-push', 'origin', 'unused-url'], cwd=tmp_path, env=env,
                            input=f'{local_ref} {head} refs/heads/main {base}\n', capture_output=True, text=True)
    assert 'CHANGELOG_GUARD=' in result.stdout + result.stderr, result  # the pushed commits were checked

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'ops'/'park_worktree.py'
spec = importlib.util.spec_from_file_location('park_worktree', SCRIPT)
park_worktree = importlib.util.module_from_spec(spec)
spec.loader.exec_module(park_worktree)


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def repo(path):
    path.mkdir(parents=True)
    git(path, 'init', '-q', '-b', 'main')
    (path/'a.py').write_text('value = 1\n')
    git(path, 'add', '-A'); git(path, 'commit', '-q', '-m', 'base')
    return path


def test_a_worktree_keeps_its_own_commit_changes_and_new_files_then_goes(tmp_path):
    main = repo(tmp_path/'main')
    wt = tmp_path/'experiment'
    git(main, 'worktree', 'add', '-q', '--detach', str(wt))
    (wt/'a.py').write_text('value = 2\n'); git(wt, 'commit', '-q', '-am', 'only here')
    (wt/'a.py').write_text('value = 3\n')
    (wt/'notes').mkdir(); (wt/'notes'/'idea.md').write_text('keep me\n')
    dest = tmp_path/'parked'

    plan = park_worktree.park(wt, dest)
    assert plan['applied'] is False and wt.exists() and not dest.exists()

    result = park_worktree.park(wt, dest, apply=True)
    assert result['commits_only_here'] == 1 and not wt.exists()
    assert str(wt) not in git(main, 'worktree', 'list')
    assert 'is okay' in subprocess.run(['git', 'bundle', 'verify', str(dest/'local.bundle')], cwd=main,
                                       capture_output=True, text=True).stderr
    git(main, 'fetch', '-q', str(dest/'local.bundle'), 'HEAD:refs/parked/head')
    git(main, 'checkout', '-q', 'refs/parked/head')
    git(main, 'apply', str(dest/'uncommitted.patch'))
    assert (main/'a.py').read_text() == 'value = 3\n'
    assert (dest/'files'/'notes'/'idea.md').read_text() == 'keep me\n'
    assert json.loads((dest/'MANIFEST.json').read_text())['kind'] == 'worktree'


def test_a_clone_without_a_remote_keeps_its_whole_history(tmp_path):
    clone = repo(tmp_path/'.reconciliation'/'copy')
    park_worktree.park(clone, tmp_path/'parked', apply=True)
    assert not clone.exists()
    out = subprocess.run(['git', 'bundle', 'list-heads', str(tmp_path/'parked'/'local.bundle')],
                         capture_output=True, text=True, check=True).stdout
    assert 'refs/heads/main' in out


def test_a_main_checkout_elsewhere_is_never_removed(tmp_path, monkeypatch):
    main = repo(tmp_path/'projects'/'tare.tools.os')
    monkeypatch.setattr(park_worktree.tempfile, 'gettempdir', lambda: str(tmp_path/'elsewhere'))
    with pytest.raises(SystemExit, match='refusing to remove'):
        park_worktree.park(main, tmp_path/'parked', apply=True)
    assert (main/'a.py').exists()

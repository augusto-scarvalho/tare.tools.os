"""The secrets a protected command's Windows sandbox denies (relay/sandbox_secrets.py); stdlib only, no Kernel."""
import os
from pathlib import Path
import sys

import pytest

from relay import sandbox_secrets


def tree(root, files, folders=()):
    for name in folders:
        (root/name).mkdir(parents=True, exist_ok=True)
    for name in files:
        (root/name).parent.mkdir(parents=True, exist_ok=True)
        (root/name).write_text('fixture')
    return root


def found(root, *, git=False, roots=None):
    return [Path(p).relative_to(root).as_posix() for p in sandbox_secrets.find(roots or [root], git=git)]


def test_secret_names_at_any_depth_and_not_their_samples(tmp_path):
    tree(tmp_path, ['.env', 'services/api/.env', 'services/api/.ENV.local', 'web/.env.production', 'pkg/.npmrc',
                    '.pypirc', 'a/.netrc', 'a/.git-credentials', 'a/.pgpass', 'deploy/.docker/config.json',
                    '.env.example', 'services/api/.env.sample', 'a/.env.template', 'a/.env.dist', 'a/.env.defaults',
                    'a/.env.local.example', 'docker/config.json', 'a/config.json', 'a/env', 'a/.environment',
                    'a/id_rsa', 'a/_netrc', 'notes.txt', 'nested/.ssh/id_ed25519'],
         ['a/.aws', 'a/.codex', 'a/.gnupg', 'a/.kube', 'a/.azure'])
    assert sorted(found(tmp_path)) == sorted(['.env', '.pypirc', 'a/.aws', 'a/.azure', 'a/.codex', 'a/.git-credentials',
        'a/.gnupg', 'a/.kube', 'a/.netrc', 'a/.pgpass', 'deploy/.docker/config.json', 'nested/.ssh', 'pkg/.npmrc',
        'services/api/.ENV.local', 'services/api/.env', 'web/.env.production'])


def test_git_folders_are_denied_only_when_asked(tmp_path):
    tree(tmp_path, ['.git', 'sub/.git', 'vendor/clone/.git/config', 'vendor/clone/.git/hooks/x'])  # a worktree's file
    assert found(tmp_path) == []
    # The root's .git whatever it is; a nested one only as a folder (a submodule's .git file points into the root's).
    assert found(tmp_path, git=True) == ['.git', 'vendor/clone/.git']


def test_dependencies_environments_and_caches_are_not_searched(tmp_path):
    tree(tmp_path, ['node_modules/pkg/.env', 'app/__pycache__/.env', 'venv/pyvenv.cfg', 'venv/Lib/.env',
                    'target/CACHEDIR.TAG', 'target/debug/.env', '.env/pyvenv.cfg', '.env/Scripts/python.exe',
                    'app/.env'])
    assert found(tmp_path) == ['app/.env']  # a virtual environment named .env is not a secret


def test_links_are_not_followed(tmp_path):
    outside = tree(tmp_path/'outside', ['.env'])
    root = tree(tmp_path/'project', ['ok.txt'])
    try:
        if sys.platform == 'win32':
            import _winapi
            _winapi.CreateJunction(str(outside), str(root/'linked'))
        else:
            os.symlink(outside, root/'linked', target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'cannot create a folder link here: {exc}')
    assert found(root) == []


def test_the_result_is_sorted_and_stable(tmp_path):
    names = [f'p{n}/.env' for n in (3, 1, 2)] + ['.npmrc', 'B/.env', 'a/.env']
    tree(tmp_path, names)
    first = sandbox_secrets.find([tmp_path], git=False)
    assert first == sorted(first) == sandbox_secrets.find([tmp_path], git=False) and len(first) == 6
    # Overlapping roots (a granted folder inside the project) count each secret once.
    assert sandbox_secrets.find([tmp_path, tmp_path/'p1'], git=False) == first


def test_more_secrets_than_the_sandbox_can_deny_refuse_the_binding(tmp_path):
    tree(tmp_path, [f'p{n:02d}/.env' for n in range(sandbox_secrets.MAX_DENIED)])
    assert len(sandbox_secrets.find([tmp_path], git=False)) == 32
    tree(tmp_path, ['.git/config'])
    with pytest.raises(ValueError, match=r'33 secret files or folders \(Git folders included\).*the 32 paths'):
        sandbox_secrets.find([tmp_path], git=True)


def test_a_walk_cut_short_refuses_rather_than_claim_protection(tmp_path, monkeypatch):
    tree(tmp_path, ['a/b/c/d/.env', *[f'f{n}.txt' for n in range(5)]])
    assert found(tmp_path) == ['a/b/c/d/.env']
    monkeypatch.setattr(sandbox_secrets, 'MAX_ENTRIES', 6)
    with pytest.raises(ValueError, match='stopped after 6 files and folders'):
        sandbox_secrets.find([tmp_path], git=False)
    monkeypatch.setattr(sandbox_secrets, 'MAX_ENTRIES', 100)
    monkeypatch.setattr(sandbox_secrets, 'MAX_DEPTH', 2)
    with pytest.raises(ValueError, match='more than 2 levels deep'):
        sandbox_secrets.find([tmp_path], git=False)


def test_only_secrets_a_command_could_move_by_renaming_a_folder_are_held(tmp_path):
    root, reference = tmp_path/'project', tmp_path/'granted'
    denied = [str(p) for p in (root/'.env', root/'.git', root/'api/.env', root/'api/deep/.ssh', reference/'x/.env')]
    # The project's own folder cannot be renamed from inside; a granted folder is read-only.
    assert sandbox_secrets.movable(denied, [str(root)]) == [str(root/'api/.env'), str(root/'api/deep/.ssh')]
    assert sandbox_secrets.movable(denied, [str(root/'api')]) == [str(root/'api/deep/.ssh')]
    assert sandbox_secrets.movable(denied, [str(root/'api'), str(root)]) == denied[2:4]  # a scope inside another
    assert sandbox_secrets.movable(denied, []) == []
    assert sandbox_secrets.Hold([]).paths == []  # nothing to hold opens nothing, on any platform


def rename(source, target):
    """Another process's attempt; the folder is put back when it moves."""
    import subprocess
    done = subprocess.run([sys.executable, '-c', 'import os, sys; os.rename(sys.argv[1], sys.argv[2])',
                           str(source), str(target)], capture_output=True, text=True)
    if done.returncode == 0:
        os.rename(target, source)
    return done.returncode == 0


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows handles')
def test_a_held_secret_keeps_every_folder_above_it_in_place_until_released(tmp_path):
    import gc
    tree(tmp_path, ['a/b/.env'], ['a/b/c/.ssh'])
    held = sandbox_secrets.Hold([str(tmp_path/'a/b/.env'), str(tmp_path/'a/b/c/.ssh')])
    assert not rename(tmp_path/'a/b', tmp_path/'a/moved') and not rename(tmp_path/'a', tmp_path/'moved')
    assert not rename(tmp_path/'a/b/c', tmp_path/'a/b/moved')  # held through the folder itself
    assert rename(tmp_path/'a/b/.env', tmp_path/'a/b/renamed')  # the secret itself stays the user's to change
    (tmp_path/'a/b/.env').write_text('edited'); (tmp_path/'a/b/new.txt').write_text('sibling')
    held.close()
    assert rename(tmp_path/'a/b', tmp_path/'a/moved')
    held = sandbox_secrets.Hold([str(tmp_path/'a/b/.env')])
    assert not rename(tmp_path/'a', tmp_path/'moved')
    del held; gc.collect()  # a binding dropped without close releases them too
    assert rename(tmp_path/'a', tmp_path/'moved')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows handles')
def test_a_secret_that_cannot_be_held_refuses_the_binding_and_releases_the_rest(tmp_path):
    tree(tmp_path, ['a/b/.env'])
    with pytest.raises(ValueError, match=r'could not hold .*missing.*protected commands are refused'):
        sandbox_secrets.Hold([str(tmp_path/'a/b/.env'), str(tmp_path/'a/missing/.env')])
    assert rename(tmp_path/'a/b', tmp_path/'a/moved')

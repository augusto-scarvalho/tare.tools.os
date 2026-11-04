"""Keep only what an experiment copy adds to git, then remove the copy.

  python scripts/ops/park_worktree.py PATH [--dest DIR]            # dry run: show what would be kept
  python scripts/ops/park_worktree.py PATH [--dest DIR] --apply    # save, verify, then remove PATH

Saved under DEST (default: <tare storage>/patches/<name>):
  local.bundle       commits reachable only from PATH (verified before anything is removed)
  uncommitted.patch  tracked changes against HEAD (git diff HEAD --binary)
  files/             untracked, non-ignored files at their paths
  MANIFEST.json      path, repository, HEAD, branch and what was saved

A linked worktree is removed with `git worktree remove` (its branch stays in the repository).
Other copies are removed only inside a .reconciliation folder or the system temp folder; a
repository's main checkout is never removed.
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def git(cwd, *args, check=True):
    r = subprocess.run(['git', '-c', 'core.longpaths=true', *args], cwd=cwd, capture_output=True)
    if check and r.returncode:
        raise SystemExit(f'git {" ".join(args)} failed in {cwd}: {r.stderr.decode(errors="replace").strip()}')
    return r.stdout.decode('utf-8', errors='replace').strip() if r.returncode == 0 else None


def removable(path, linked):
    if linked:
        return True
    resolved = path.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    return any(p.name == '.reconciliation' for p in resolved.parents) or temp in resolved.parents


def default_dest(name):
    from relay.native_installation import storage_root
    return storage_root()/'patches'/name


def park(path, dest=None, apply=False):
    path = Path(path).resolve()
    top = git(path, 'rev-parse', '--show-toplevel', check=False)
    if top is None or Path(top).resolve() != path:
        raise SystemExit(f'{path} is not the top of a git working tree')
    linked = (path/'.git').is_file()
    if not removable(path, linked):
        raise SystemExit(f'refusing to remove {path}: not a linked worktree, and not under .reconciliation or temp')
    dest = Path(dest) if dest else default_dest(path.name)
    head = git(path, 'rev-parse', 'HEAD')
    if linked:  # branches live in the shared repository; only a detached HEAD can hold commits alone
        selection = ['HEAD', '--not', '--branches', '--remotes', '--tags']
    elif git(path, 'remote').strip():
        selection = ['HEAD', '--branches', '--tags', '--not', '--remotes']
    else:  # a clone without a remote: its whole history exists only here
        selection = ['HEAD', '--branches', '--tags']
    own = git(path, 'rev-list', *selection).split()
    diff = subprocess.run(['git', 'diff', 'HEAD', '--binary'], cwd=path, capture_output=True, check=True).stdout
    untracked = [p for p in git(path, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if p]
    manifest = {'path': str(path), 'kind': 'worktree' if linked else 'clone', 'head': head,
                'branch': git(path, 'rev-parse', '--abbrev-ref', 'HEAD'),
                'repository': git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir'),
                'origin': git(path, 'remote', 'get-url', 'origin', check=False),
                'commits_only_here': len(own), 'uncommitted_bytes': len(diff), 'untracked_files': len(untracked)}
    if not apply:
        return {**manifest, 'dest': str(dest), 'applied': False}
    dest.mkdir(parents=True, exist_ok=True)
    if own:
        bundle = dest/'local.bundle'
        git(path, 'bundle', 'create', str(bundle), *selection)
        git(path, 'bundle', 'verify', str(bundle))  # nothing is removed unless the bundle verifies
    if diff.strip():
        (dest/'uncommitted.patch').write_bytes(diff)
    for rel in untracked:
        target = dest/'files'/rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path/rel, target)
    (dest/'MANIFEST.json').write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    if linked:
        owner = Path(manifest['repository']).parent
        git(owner, 'worktree', 'remove', '--force', str(path))
        git(owner, 'worktree', 'prune')
    else:
        shutil.rmtree(path, onexc=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
    return {**manifest, 'dest': str(dest), 'applied': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path')
    parser.add_argument('--dest')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(park(args.path, args.dest, args.apply), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())

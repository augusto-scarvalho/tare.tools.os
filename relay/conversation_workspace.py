"""A bounded candidate for ordinary folders; Git projects use the existing worktree."""
import os
from pathlib import Path
import shutil

from compute_plane.native_journal import regular

MAX_FILES, MAX_BYTES = 2000, 32*1024*1024
IGNORED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def ignored_targets(root, paths):
    """An ignored new output cannot silently disappear from a reviewed Git patch."""
    import subprocess
    from compute_plane.managed_process import host_executable
    root = Path(root)
    if not (root/'.git').exists() or not paths:
        return []
    result = subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false',
        '-C', str(root), 'check-ignore', '--stdin', '-z'],
        input=('\0'.join(paths)+'\0').encode('utf8'), capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot validate planned Git outputs: '+result.stderr.decode('utf8', errors='replace')[:500])
    return [p for p in result.stdout.decode('utf8').split('\0') if p]


def snapshot(root, destination):
    from .challenger import git
    from .challenger_workspace import snapshot as git_snapshot
    root, destination = regular(Path(root)), regular(Path(destination))
    if (root/'.git').exists():
        return git_snapshot(root, destination)
    if destination.resolve().is_relative_to(root.resolve()) or destination.exists():
        raise ValueError('A candidate must be a fresh directory outside the project.')
    files, size = [], 0
    # Preflight the whole bounded selection before writing. No model downloads,
    # environments or ignored caches are copied to qualify an ordinary folder.
    for folder, directories, names in os.walk(root):
        directories[:] = [n for n in directories if n not in IGNORED]
        for name in names:
            source = regular(Path(folder)/name)
            size += source.stat().st_size
            files.append(source)
            if size > MAX_BYTES or len(files) > MAX_FILES:
                raise ValueError('This folder exceeds the bounded candidate (32 MiB / 2000 files). Select the specific project folder.')
    destination.mkdir(parents=True)
    try:
        for source in files:
            target = destination/source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        git(destination, 'init')
        git(destination, 'add', '--all')
        git(destination, '-c', 'user.name=tare candidate', '-c', 'user.email=candidate@localhost',
            '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'Candidate input')
        return git(destination, 'rev-parse', 'HEAD').decode().strip()
    except BaseException:
        # This exact fresh target is owned by this call and checked above.
        shutil.rmtree(destination, ignore_errors=True)
        raise

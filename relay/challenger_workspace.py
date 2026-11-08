"""Bounded Git snapshots and explicit reuse of a reviewed challenger."""
import hashlib
import filecmp
import os
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

from compute_plane.native_journal import custody_lock, regular, save_json
from .native_agent import read_json
from .challenger import git, read_run

MAX_BYTES = 20*1024*1024


def tree(job, name, row=None):
    """Keep new workspaces shallow within the same accounted area; retain legacy custody."""
    if name not in {'candidate', 'reference'}:
        raise ValueError('Unknown comparison workspace.')
    job = Path(job)
    if row is None:
        row = read_run(job/'run.json') if (job/'run.json').exists() else {}
    if row.get('workspace_reuse'):
        previous = Path(row['workspace_reuse']).resolve()
        if previous.parent != job.resolve().parent or previous == job.resolve():
            raise ValueError('Reused workspace must belong to a prior experiment of this project.')
        saved = read_run(regular(previous/'run.json'))
        if saved.get('workspace_reuse'):
            raise ValueError('Workspace reuse must point to the original saved workspaces.')
        return tree(previous, name, saved)
    if (row.get('execution_contract') or {}).get('workspace_layout') != 1:
        return job/name
    identity = hashlib.sha256((str(job.resolve())+':'+name).encode()).hexdigest()[:16]
    return job.parents[2]/'wt'/identity


def safe(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe experiment file path.')
    return regular(Path(root)/name)


def changes(root, base):
    names = set(git(root, 'diff', '--name-only', '-z', base, '--').decode().split('\0'))
    names.update(git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0'))
    result, size = {}, 0
    for name in sorted(names - {''}):
        path = safe(root, name)
        if path.is_dir():
            raise ValueError('A changed submodule needs its own project experiment.')
        if path.exists() and path.stat().st_size > MAX_BYTES-size:
            raise ValueError('Changed files exceed the 20 MiB experiment snapshot limit.')
        raw = path.read_bytes() if path.exists() else None
        size += len(raw) if raw is not None else 0
        if size > MAX_BYTES or len(result) >= 512:
            raise ValueError('Changed files exceed the 20 MiB / 512 file experiment snapshot limit.')
        result[name] = raw
    return result


def _offline_git(root, *args, deadline, input=None):
    """Local plumbing only: never consult transports, hooks, or lazy object fetchers."""
    from compute_plane.managed_process import host_executable
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise ValueError('Submodule preparation exceeded the 300-second allowance.')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_ALLOW_PROTOCOL='', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ATTR_NOSYSTEM='1')
    result = subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false',
        '-c', 'core.hooksPath=', '-c', 'protocol.allow=never',
        '-c', 'core.autocrlf=false', '-c', 'core.attributesFile='+os.devnull,
        '-C', str(root), *args],
        env=env, input=input, capture_output=True, timeout=remaining)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[:500])
    return result.stdout


def submodule_inventory(root, *, revision='HEAD', deadline=None):
    """Validate each local pin in one batch; gitlinks belong to separate stores."""
    deadline = time.monotonic()+300 if deadline is None else deadline
    root = Path(root).resolve()
    rows = []

    def walk(parent, rev, prefix=''):
        for record in _offline_git(parent, 'ls-tree', '-rz', rev, deadline=deadline).split(b'\0'):
            if not record:
                continue
            header, raw_name = record.split(b'\t', 1)
            mode, kind, oid = header.decode().split()
            if mode != '160000':
                continue
            name = raw_name.decode('utf-8')
            row = {'path': prefix+name, 'commit': oid, 'initialized': False, 'available': False}
            rows.append(row)
            try:
                path = safe(parent, name)
                top = _offline_git(path, 'rev-parse', '--show-toplevel', deadline=deadline).decode().strip()
                row['initialized'] = Path(top).resolve() == path.resolve()
                if not row['initialized']:
                    row['reason'] = 'Submodule is not initialized.'
                    continue
                entries = _offline_git(path, 'ls-tree', '-rtz', oid, deadline=deadline)
                objects = {oid: 'commit', oid+'^{tree}': 'tree'}
                for entry in entries.split(b'\0'):
                    if not entry:
                        continue
                    nested_header, nested_name = entry.split(b'\t', 1)
                    nested_mode, nested_kind, nested_oid = nested_header.decode().split()
                    safe(path, nested_name.decode('utf-8'))
                    expected = {'040000': 'tree', '100644': 'blob', '100755': 'blob', '160000': 'commit'}
                    if nested_mode not in expected or nested_kind != expected[nested_mode]:
                        raise ValueError('Unsupported or unsafe submodule entry.')
                    if nested_mode != '160000':
                        objects[nested_oid] = nested_kind
                responses = _offline_git(path, 'cat-file', '--batch-check', deadline=deadline,
                    input=('\n'.join(objects)+'\n').encode()).decode().splitlines()
                if len(responses) != len(objects):
                    raise ValueError('Malformed local object batch response.')
                for (query, expected), response in zip(objects.items(), responses):
                    fields = response.split()
                    if (len(fields) != 3 or fields[1] != expected or not fields[2].isdigit()
                            or len(fields[0]) != len(oid)
                            or any(c not in '0123456789abcdef' for c in fields[0])
                            or ('^' not in query and fields[0] != query)):
                        raise ValueError('Missing or malformed local object: '+query)
                row['available'] = True
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                row['reason'] = str(exc)[:500]
            if row['available']:
                try:
                    walk(path, oid, prefix+name+'/')
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    row.update(available=False, reason=str(exc)[:500])
    walk(root, revision)
    return rows


def populate_submodules(root, destination, *, deadline):
    """Offline checkout with per-pin failure isolation and owned-output cleanup.

    Host info/attributes overrides even candidate-authored conversion attributes.
    Failed parents suppress descendants, but independent siblings can still succeed.
    """
    try:
        rows = submodule_inventory(root, deadline=deadline)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return [{'path': '.', 'commit': 'HEAD', 'initialized': False,
                 'available': False, 'reason': str(exc)[:500]}]
    failed = []
    for row in rows:
        owned = False
        target = None
        try:
            if any(row['path'].startswith(parent+'/') for parent in failed):
                raise ValueError('Parent submodule unavailable.')
            if not row['available']:
                failed.append(row['path'])
                continue
            source, target = safe(root, row['path']), safe(destination, row['path'])
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise ValueError('Submodule destination is not empty.')
            target.mkdir(parents=True, exist_ok=True)
            owned = True
            _offline_git(target, '-c', 'init.templateDir=', 'init', deadline=deadline)
            objects = _offline_git(source, 'rev-parse', '--path-format=absolute',
                                   '--git-path', 'objects', deadline=deadline).decode().strip()
            info = target/'.git/objects/info'
            (info/'alternates').write_bytes((str(Path(objects).resolve()).replace('\\', '/')+'\n').encode('utf-8'))
            (target/'.git/info').mkdir(exist_ok=True)
            (target/'.git/info/attributes').write_text(
                '* -filter -text -ident -working-tree-encoding\n', encoding='utf-8')
            pin = row['commit']
            _offline_git(target, 'read-tree', pin, deadline=deadline)
            _offline_git(target, 'checkout-index', '--all', '--force', deadline=deadline)
            _offline_git(target, 'update-ref', '--no-deref', 'HEAD', pin, deadline=deadline)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            row.update(available=False, reason=str(exc)[:500])
            failed.append(row['path'])
            if owned:
                shutil.rmtree(target)
    return rows


def snapshot(root, destination):
    deadline = time.monotonic()+300
    base = git(root, 'rev-parse', 'HEAD').decode().strip()
    captured = changes(root, base)
    # The worktree takes the repository's own line-ending settings, so its bytes match the operator's checkout
    # (forcing core.autocrlf=false made every text file differ under Git for Windows' default and refused any
    # repository past 512 of them). A cold checkout of a large repository can take well over 30 s.
    git(root, 'worktree', 'add', '--detach', str(destination), base,
        timeout=max(0.001, deadline-time.monotonic()))
    # Git can still hide clean/smudge or CRLF differences. The experiment receives the
    # operator's exact bytes, including clean tracked files affected by filters.
    for name in git(root, 'ls-files', '--cached', '-z').decode().split('\0'):
        if not name or name in captured:
            continue
        source, target = safe(root, name), safe(destination, name)
        if source.is_file() and (not target.is_file() or not filecmp.cmp(source, target, shallow=False)):
            size = sum(len(raw) for raw in captured.values() if raw is not None)
            if source.stat().st_size > MAX_BYTES-size or len(captured) >= 512:
                raise ValueError('Checkout filters exceed the bounded experiment snapshot.')
            captured[name] = source.read_bytes()
    for name, raw in captured.items():
        path = safe(destination, name)
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(safe(root, name).stat().st_mode)
    if captured:
        git(destination, 'add', '--all', '--', *captured)
        git(destination, '-c', 'user.name=tare challenger', '-c', 'user.email=challenger@localhost',
            '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'Experiment input snapshot')
    revision = _offline_git(destination, 'rev-parse', 'HEAD', deadline=deadline).decode().strip()
    populate_submodules(root, destination, deadline=deadline)
    return revision


def freeze(job, base):
    root = tree(job, 'candidate')
    changed = changes(root, base)
    if changed:
        git(root, 'add', '--all', '--', *changed)
    patch = git(root, 'diff', '--cached', '--binary', base, '--')
    if len(patch) > MAX_BYTES:
        raise ValueError('Challenger patch exceeds 20 MiB.')
    (job/'challenger.patch').write_bytes(patch)
    return {name: hashlib.sha256(raw).hexdigest() if raw is not None else None for name, raw in changed.items()}


def reuse(job):
    """Explicit UI action: compare every affected file, then atomically replace each.

    A retained intent makes retries idempotent after partial application. An edit
    outside the recorded reference/challenger bytes stops reuse before writing.
    """
    from contextlib import ExitStack
    from .native_agent import workspace_locks
    job = regular(job)
    row = read_run(job/'run.json')
    if row['status'] != 'COMPLETED' or row.get('challenger_verdict') != 'accepted':
        raise ValueError('Reuse requires the challenger checks and independent review to pass.')
    reference = read_json(job/'reference.json', 1024*1024)
    if 'snapshot' not in reference:
        raise ValueError('Reference snapshot is unavailable.')
    reference_root, candidate_root = tree(job, 'reference', row), tree(job, 'candidate', row)
    if changes(reference_root, reference['snapshot']):
        raise ValueError('Recorded reference snapshot changed.')
    observed = changes(candidate_root, row['base'])
    if {name: hashlib.sha256(raw).hexdigest() if raw is not None else None for name, raw in observed.items()} != row['files']:
        raise ValueError('Reviewed challenger files changed.')
    from .challenger_worker import checks
    if not checks(job/'challenger-audit', row['config']['verifiers']):
        raise ValueError('Challenger review checks are no longer current.')
    root = Path(row['project'])
    before, after = changes(reference_root, row['base']), changes(candidate_root, row['base'])
    names = sorted(set(before) | set(after))
    replacements = {}
    for name in names:
        def content(folder):
            path = safe(folder, name)
            return path.read_bytes() if path.exists() else None
        old, new = content(reference_root), content(candidate_root)
        if old != new:
            replacements[name] = (old, new)
    fingerprint = {name: [hashlib.sha256(raw).hexdigest() if raw is not None else None for raw in pair]
                   for name, pair in replacements.items()}
    with ExitStack() as stack:
        workspace_locks(stack, {'project': str(root)})
        stack.enter_context(custody_lock(job))
        intent = job/'reuse.json'
        previous = read_json(intent, 1024*1024) if intent.exists() else None
        if previous and previous['files'] != fingerprint:
            raise ValueError('The reviewed candidate changed after reuse was prepared.')
        for name, (old, new) in replacements.items():
            path = safe(root, name)
            current = path.read_bytes() if path.exists() else None
            allowed = (old, new) if previous else (old,)
            if current not in allowed:
                raise ValueError('Project changed since this comparison; preserve it and merge the patch: '+name)
        save_json(intent, {'status': 'APPLYING', 'files': fingerprint}, max_bytes=1024*1024)
        for name, (_, raw) in replacements.items():
            path = safe(root, name)
            if (path.read_bytes() if path.exists() else None) == raw:
                continue
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = safe(root, name+'.tare-challenger-'+row['id'])
                if temporary.exists():
                    if temporary.read_bytes() != raw:
                        raise ValueError('Interrupted replacement differs from the selected candidate.')
                else:
                    with temporary.open('xb') as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
        save_json(intent, {'status': 'APPLIED', 'files': fingerprint}, max_bytes=1024*1024)
    return {'files': list(replacements), 'status': 'APPLIED',
            'notice': 'Changes applied locally. Run the project checks and normal review/commit flow.'}

"""Inventory owned storage and preview a deliberately small set of safe removals.

Evidence, sessions and Git workspaces are retained. Of tare's own files, unused verified product releases and
byte-identical second-build wheels are candidates, with no age heuristic. tare's own scratch that an interrupted run
left (candidate trees, temporary folders, dead usage markers, interrupted updates) is swept when the TUI or
`tare serve` starts, once no record needs it and it is a day old: the ownership map is in
docs/guides/STORAGE_MAINTENANCE.md. Other tools' leftovers (old CLI session logs and versions, old test folders,
package caches) are candidates by age or by their own cleanup command, and are removed automatically only when the
operator opts in.
"""
import base64
from contextlib import ExitStack
import csv
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from compute_plane.native_journal import io_path

from . import native_product as product

GIB = 1024 ** 3


def read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Metadata is too large: ' + str(path))
    return json.loads(raw)


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def plain(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Linked path is preserved: ' + str(part))
    if path == Path(path.anchor):
        raise ValueError('Choose an application folder, not a whole drive.')
    return path


def measure(path, deadline, gitfiles=None):
    """Logical file bytes, bounded walk, no link traversal; incomplete is explicit. Walks the long-path form, since
    test trees pass Windows' 260 characters; `newest` is the latest modification inside, and the names of `.git`
    files (a worktree's link to its repository) are added to `gitfiles` when given."""
    result = dict(bytes=0, files=0, complete=True, linked=False, git=False, newest=0)
    digest = hashlib.sha256()
    root = io_path(path)

    def visit(p):
        if time.monotonic() >= deadline or result['files'] >= 200000:
            raise TimeoutError('Scan limit reached; shown size is a lower bound.')
        s = p.lstat()
        name = p.relative_to(root).as_posix()
        digest.update(repr((name, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ino)).encode())
        result['newest'] = max(result['newest'], s.st_mtime)
        if p.name == '.git':
            result['git'] = True
            if gitfiles is not None and stat.S_ISREG(s.st_mode):
                gitfiles.append(name)
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            result['linked'] = True
        elif stat.S_ISDIR(s.st_mode):
            for child in sorted(p.iterdir()):
                visit(child)
        elif stat.S_ISREG(s.st_mode):
            result['bytes'] += s.st_size
            result['files'] += 1
        else:
            raise ValueError('Special file is preserved.')

    try:
        plain(path)
        visit(root)
    except (OSError, ValueError) as exc:
        result.update(complete=False, error=str(exc))
    result['fingerprint'] = digest.hexdigest()
    return result


def processes():
    """Bounded process metadata; never enumerate Windows open handles."""
    import psutil
    paths = []
    for proc in psutil.process_iter(['pid', 'cwd', 'exe', 'cmdline']):
        try:
            for value in [proc.info.get('cwd'), proc.info.get('exe'), *(proc.info.get('cmdline') or [])]:
                if value and os.path.isabs(value.strip('"')):
                    paths.append((proc.pid, os.path.normcase(os.path.abspath(value.strip('"')))))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return paths


def in_use(path, observed):
    target = os.path.normcase(str(path))
    return next((pid for pid, value in observed if value == target or value.startswith(target + os.sep)), None)


def installed_unchanged(release):
    """Reject user files or modified installed packages instead of discarding them."""
    if {p.name for p in release.iterdir()} != {'bundle', 'venv', 'ready.json'}:
        raise ValueError('Unrecognized files in the installation are preserved.')
    sites = list((release/'venv').glob('Lib/site-packages')) + list((release/'venv').glob('lib/python*/site-packages'))
    if len(sites) != 1:
        raise ValueError('Installed package records are unavailable.')
    site = sites[0]
    allowed = {'Lib', 'Scripts', 'Include', 'pyvenv.cfg', '.gitignore'} if os.name == 'nt' else {
        'lib', 'lib64', 'bin', 'include', 'pyvenv.cfg', '.gitignore'}
    if any(p.name not in allowed for p in (release/'venv').iterdir()):
        raise ValueError('Unrecognized virtual environment files are preserved.')
    known = set()
    records = list(site.glob('*.dist-info/RECORD'))
    if not records:
        raise ValueError('Installed package records are unavailable.')
    for record in records:
        with record.open(encoding='utf-8', newline='') as stream:
            for name, expected, _size in csv.reader(stream):
                p = Path(os.path.abspath(site/name))
                if not p.is_relative_to(release/'venv'):
                    raise ValueError('Package record escapes its installation.')
                known.add(p)
                if expected:
                    algorithm, encoded = expected.split('=', 1)
                    if algorithm != 'sha256' or base64.urlsafe_b64encode(bytes.fromhex(product.digest(p))).decode().rstrip('=') != encoded:
                        raise ValueError('Modified installed file is preserved: ' + name)
                elif p != record and not (p.suffix == '.pyc' and p.parent.name == '__pycache__'):
                    raise ValueError('Unverified installed file is preserved: ' + name)
    for base, _dirs, files in os.walk(site):
        for name in files:
            p = Path(base)/name
            if p not in known and not (p.suffix == '.pyc' and p.parent.name == '__pycache__'):
                raise ValueError('Unrecorded installed file is preserved: ' + str(p.relative_to(site)))
    executables = {'activate', 'activate.bat', 'Activate.ps1', 'deactivate.bat',
                   'activate.fish', 'activate.csh', 'python', 'python3',
                   'python3.'+str(os.sys.version_info.minor), 'python.exe', 'pythonw.exe'}
    generated = {'pyvenv.cfg', '.gitignore'} | {
        ('Scripts/' if os.name == 'nt' else 'bin/') + name for name in executables}
    for base, _dirs, files in os.walk(release/'venv'):
        if Path(base).is_relative_to(site):
            continue
        for name in files:
            p = Path(base)/name
            if p not in known and p.relative_to(release/'venv').as_posix() not in generated:
                raise ValueError('Unrecorded environment file is preserved: ' + str(p.relative_to(release)))


def release_reason(path, home, observed):
    selected = product.state(home)
    if not selected.get('current') or not selected.get('previous'):
        raise ValueError('Current release and rollback must both be recorded.')
    for key in ('current', 'previous'):
        kept = product.release_at(home, selected[key])
        if not (kept/'ready.json').is_file() or read(kept/'ready.json').get('bundle') != selected[key]:
            raise ValueError('Current release or rollback is incomplete.')
        if path == kept:
            raise ValueError('Current installation.' if key == 'current' else 'Previous version reserved for rollback.')
    if not re.fullmatch('[0-9a-f]{16}', path.name) or path.parent != home/'releases':
        raise ValueError('Unrecognized installation is preserved.')
    if pid := in_use(path, observed):
        raise ValueError('Installation is in use by PID ' + str(pid))
    manifest, identifier = product.verify(path/'bundle')
    expected = set(manifest['files']) | {'bundle.json'}
    if any(p.relative_to(path/'bundle').as_posix() not in expected
           for p in (path/'bundle').rglob('*') if p.is_file()):
        raise ValueError('Unrecorded bundle files are preserved.')
    if product.release_at(home, identifier) != path or read(path/'ready.json') != {'bundle': identifier}:
        raise ValueError('Installation identity does not match.')
    installed_unchanged(path)
    return 'Unused verified installation; current version and rollback are kept.'


def wheel_reason(path, observed):
    build = path.parent.parent
    if any((parent/'.git').exists() for parent in (build, *build.parents)):
        raise ValueError('Git workspace is preserved.')
    if pid := in_use(build, observed):
        raise ValueError('Build is in use by PID ' + str(pid))
    manifest = read(build/'manifest.json')
    if manifest.get('schema') != 'tare.tools/native-release/1' or manifest.get('status') != 'REPRODUCIBLE_WHEELS':
        raise ValueError('Build is not complete.')
    package = next((p for p in manifest['packages'] if p['wheel'] == path.name), None)
    if package is None or path.parent.name != 'wheels-2' or path.suffix != '.whl':
        raise ValueError('Unknown build artifact is preserved.')
    original = plain(build/'wheels-1'/path.name)
    if product.digest(original) != package['sha256'] or product.digest(path) != package['sha256']:
        raise ValueError('Wheel differs from the retained first build.')
    return 'Identical second-build wheel; first wheel, sources and evidence are kept.'


def sources(storage, home):
    """Partition roots so a nested product/state directory is never counted twice."""
    rows = []
    if storage.is_dir() and storage != home and not home.is_relative_to(storage):
        groups = {'evidence-store': 'Evidence', 'agent-sessions': 'Sessions', 'console': 'Conversations',
                  'console-entries': 'Conversations', 'work-runs': 'Managed work',
                  'experiments': 'Experiments', 't': 'Builds and temporary files', 'tmp': 'Temporary files',
                  'releases': 'Release bundles', 'challengers': 'Challengers'}
        for p in sorted(storage.iterdir()):
            if p.name == 'storage-maintenance':
                rows.append((p, 'Cleanup receipts', 'keep'))
            elif p.name in {'t', 'tmp', 'experiments'} and p.is_dir() and not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)():
                rows.extend((c, groups[p.name], 'build' if p.name == 't' else 'keep') for c in sorted(p.iterdir()))
            else:
                rows.append((p, groups.get(p.name, 'Other operational files'), 'keep'))
    if home.is_dir():
        for p in sorted(home.iterdir()):
            if p.name == 'releases' and not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)():
                rows.extend((c, 'Installed versions', 'release') for c in sorted(p.iterdir()))
            elif p == storage:
                # Storage is shown above when it is the installation's state folder.
                continue
            else:
                rows.append((p, 'Product state and launcher', 'keep'))
    return rows


def system_sources():
    """Observation only. Windows, uv and Docker own their cleanup (pip and npm caches are tool rows below)."""
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return []
    root = Path(local)
    return [(p, label, 'external') for p, label in (
        (root/'Temp', 'System temporary files'), (root/'uv/cache', 'uv cache'),
        (root/'Docker/wsl', 'Docker virtual disks')) if p.exists()]


DAYS = 7  # how long other tools' logs and leftovers are kept unless the operator chooses otherwise
TOOL_KINDS = {'cli-logs', 'cli-versions', 'test-temp', 'cache'}


def tool_roots():
    """User home, temporary and local application folders, where other tools keep their files."""
    return Path.home(), Path(tempfile.gettempdir()), Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.cache')


def tool_candidates(days):
    """What other tools leave behind: Codex and AGY CLI session logs and pytest temporary folders untouched for
    `days`, Codex versions other than the current one, and the npm and pip caches (each cleared by its own
    command). Rows of (kind, label, root, items, command); Claude Code transcripts are kept, since they back /resume."""
    home, temp, local = tool_roots()
    cutoff = time.time() - days*86400

    def older(root, pattern, folders=False):
        if not root.is_dir():
            return []
        return [p for p in sorted(root.glob(pattern)) if (p.is_dir() if folders else p.is_file())
                and not p.is_symlink() and p.stat().st_mtime < cutoff]
    rows = [('cli-logs', f'Codex CLI session logs older than {days} days', home/'.codex'/'sessions',
             older(home/'.codex'/'sessions', '**/*.jsonl'), None),
            ('cli-logs', f'AGY CLI logs older than {days} days', home/'.gemini'/'antigravity-cli'/'log',
             older(home/'.gemini'/'antigravity-cli'/'log', '**/*'), None),
            ('test-temp', f'pytest temporary folders older than {days} days', temp,
             older(temp, 'pytest-of-*', folders=True), None)]
    for flavour in ('standalone', 'app-server-daemon'):
        releases = home/'.codex'/'packages'/flavour/'releases'
        if releases.is_dir() and (releases.parent/'current').exists():
            keep = (releases.parent/'current').resolve()  # a junction to the version Codex runs
            rows.append(('cli-versions', f'Codex {flavour} versions other than the current one', releases,
                         [p for p in sorted(releases.iterdir()) if p.is_dir() and p.resolve() != keep], None))
    if npm := shutil.which('npm'):
        rows.append(('cache', 'npm cache (npm cache clean)', local/'npm-cache', [local/'npm-cache'],
                     [npm, 'cache', 'clean', '--force']))
    rows.append(('cache', 'pip cache (pip cache purge)', local/'pip'/'Cache', [local/'pip'/'Cache'],
                 [sys.executable, '-m', 'pip', 'cache', 'purge']))
    return [row for row in rows if row[3] and all(p.exists() for p in row[3])]


def tool_rows(days):
    rows = []
    reasons = {'cli-logs': 'Old session logs of another tool; recent ones are kept.',
               'cli-versions': 'Versions the CLI no longer runs; the current one is kept.',
               'test-temp': 'Old test temporary folders.',
               'cache': 'Package cache, rebuilt on demand; cleared by its own command.'}
    for kind, label, root, items, command in tool_candidates(days):
        sizes = [measure(p, time.monotonic() + 15) for p in items]
        row = dict(id=identity(label)[:16], path=str(root), origin=label, kind=kind, reason=reasons[kind],
                   bytes=sum(s['bytes'] for s in sizes), files=sum(s['files'] for s in sizes),
                   complete=all(s['complete'] for s in sizes), linked=any(s['linked'] for s in sizes),
                   git=any(s['git'] for s in sizes), fingerprint=identity([s['fingerprint'] for s in sizes]),
                   items=[str(p) for p in items], command=command)
        row['eligible'] = row['complete'] and not row['linked'] and not row['git']
        if not row['eligible']:
            row['reason'] = 'Could not be fully inspected, or holds links or a Git workspace: kept.'
        rows.append(row)
    return rows


def clean_tool(row, days):
    """Remove a tool row's items that the same rule still finds now (a log written since the preview is no longer
    due); returns (removed bytes, items kept)."""
    current = {label: (kind, root, items, command) for kind, label, root, items, command in tool_candidates(days)}
    kind, root, items, command = current.get(row['origin'], (None, None, [], None))
    if kind != row['kind'] or str(root) != row['path']:
        return 0, len(row['items'])
    if command:
        cache = Path(row['items'][0])
        before = measure(cache, time.monotonic() + 30)['bytes']
        subprocess.run(command, capture_output=True, timeout=600, check=True)
        return max(0, before - measure(cache, time.monotonic() + 30)['bytes']), 0
    due, observed = {str(p) for p in items}, processes() if kind == 'cli-versions' else []
    removed = kept = 0
    for item in row['items']:
        try:
            path = plain(item)
            size = measure(path, time.monotonic() + 30)
            if (item not in due or not path.is_relative_to(root) or in_use(path, observed)
                    or not size['complete'] or size['linked'] or size['git']):
                raise ValueError('changed, in use or no longer due')
            if path.is_dir():
                tomb = path.with_name(path.name + '.storage-retiring-' + uuid.uuid4().hex)
                path.rename(tomb)
                shutil.rmtree(tomb)
            else:
                path.unlink()
            removed += size['bytes']
        except (OSError, ValueError):
            kept += 1
    return removed, kept


DAY = 86400  # an own leftover whose newest file is younger than this is kept: a run may still be using it
# Folders relay and the Kernel make in the system temporary folder: an explicit list, since another program may
# name its own folders tare-* too.
OWN_TEMP = ('tare-audit-', 'tare-blind-images-', 'tare-bwrap-', 'tare-cli-ack-', 'tare-cli-profile-', 'tare-command-',
            'tare-inference-', 'tare-native-review-', 'tare-pr-', 'tare-reading-', 'tare-round-table-seat-',
            'tare-round-table-stream-', 'tare-serve-cli-', 'tare-serve-images-', 'tare-snapshot-', 'tare-test-',
            'tare-vision-')
RANDOM = '[a-z0-9_]{8}'  # tempfile's names
TOMB = r'(\.storage-retiring-[0-9a-f]{32})?'  # a removal a sweep began and did not finish: the next one ends it
# The private folders of command bindings (command_protection): a conversation's, its checks', a unit's and a worker's.
COMMAND_TMP = ('*', '*/checks/host-checks', '*/units/*/host-checks', '*/units/*/session-*')
OWN_RECEIPT = 'last-own-sweep.json'


def own_items(storage, home, roots):
    """(kind, path) of every entry with the exact shape of one of tare's own leftovers, under its known root."""
    user, temp, _ = roots
    shapes = (('candidate', storage/'wt', r'([0-9a-f]{8}|\.[0-9a-f]{8}-pytest)'+TOMB),
              ('temp', temp, '('+'|'.join(map(re.escape, OWN_TEMP))+')'+RANDOM+TOMB),
              # work_units.managed_check
              ('check', user/'.tare'/'tmp' if os.name == 'nt' else temp, ('' if os.name == 'nt' else 'tc-')+RANDOM+TOMB),
              ('marker', home/'releases', r'\.use\.[0-9a-f]{16}\.[1-9][0-9]*'),
              ('update', home, '(download|catalog-trial)-'+RANDOM+TOMB))
    items = []
    for kind, root, shape in shapes:
        try:  # candidate trees first, so a pytest root whose tree goes in this sweep goes with it
            items += [(kind, p) for p in sorted(plain(root).iterdir(), key=lambda p: p.name.startswith('.'))
                      if re.fullmatch(shape, p.name)]
        except (OSError, ValueError):
            continue  # an absent or linked root holds nothing to sweep
    return items + [('command-tmp', p) for custody in COMMAND_TMP
                    for p in sorted((storage/'agent-sessions').glob(custody+'/command-tmp*'))
                    if re.fullmatch('command-tmp'+TOMB, p.name)]


def own_reason(kind, path, size, gitfiles, observed, records, storage, busy):
    """Why tare still needs one of its own leftovers; None when it is orphaned; a Path when it is orphaned but Git
    still registers it with that project (run_unit's `git worktree remove` comes first)."""
    if not size['complete']:
        return 'Could not be inspected: '+size.get('error', '')  # e.g. a sandbox's files the user cannot list
    if size['linked']:
        return 'Holds a link or junction: kept.'
    if kind != 'marker' and time.time()-size['newest'] < DAY:  # a marker's process is dead or not, whatever its age
        return 'Changed within the last day.'
    if pid := in_use(path, observed):
        return 'In use by PID '+str(pid)+'.'
    if busy:
        return 'An update or a launch holds the installation lock.'
    links = {}  # the .git files inside that point at a repository outside that still exists
    for name in gitfiles:
        text = io_path(path/name).read_text(encoding='utf-8').strip()
        if not text.startswith('gitdir:'):
            return 'Holds an unrecognized Git file.'
        target = Path(os.path.normpath((path/name).parent/text.removeprefix('gitdir:').strip()))
        if target.exists() and not target.is_relative_to(path):
            links[name] = target
    top = links.pop('.git', None)  # <project>/.git/worktrees/<name> while Git lists the folder as a worktree
    if top is not None and (top.parent.name != 'worktrees' or top.parents[1].name != '.git'):
        return 'Git lists it as a worktree of a repository it cannot place: '+str(top)
    if nested := next((t for t in links.values() if top is None or not t.is_relative_to(top)), None):
        return 'Git still lists a worktree inside it: '+str(nested)  # its submodules go with its own registration
    if kind == 'candidate':
        name = path.name[1:9] if path.name.startswith('.') else path.name[:8]
        if path.name.startswith('.') and (path.parent/name).exists():
            return 'Its candidate tree is still there.'
        if records is None:
            return 'A conversation record could not be read.'
        if re.search(r'wt[\\/]+'+name+'(?![0-9a-f])', '\n'.join(records.values())):
            return "A conversation's record names it (a waiting worker resumes in it)."
    elif kind == 'marker':
        pid = path.name.rsplit('.', 1)[1]
        if path.read_text(encoding='ascii') != pid:
            return 'Unrecognized usage record.'
        if product._pid_alive(int(pid)) is not False:
            return 'Its process is running, or whether it is cannot be told.'
    elif kind == 'command-tmp':
        custody = path.parent
        if custody.name.startswith('session-'):  # a worker resumes only from its conversation's record (restore)
            if records is None:
                return 'A conversation record could not be read.'
            conversation, job = path.relative_to(storage/'agent-sessions').parts[0], custody.parent.name
            if re.search(r'units[\\/]+'+re.escape(job)+r'[\\/]+'+re.escape(custody.name)+r'["\\/]',
                         records.get(conversation, '')):
                return 'A waiting worker resumes with it.'
        else:
            from .folder_access import pending
            if pending(custody) is not None:
                return "Its session waits for the user's folder answer."
    return top and top.parents[2]


def own_rows(storage, home, roots, *, seconds=60, remove=False, observed=None):
    """Each of tare's own leftovers, with why it is kept or, when orphaned, `status` 'removed' (remove=True) or
    'orphan' (a preview). Each orphan is removed as it is found: renamed first, which Windows refuses while any file
    in it is open, then checked against what was measured and deleted (read-only and long paths included)."""
    from .worker_wait import remove as remove_folder
    items = own_items(storage, home, roots)
    if not items:
        return []
    deadline = time.monotonic()+seconds
    observed = processes() if observed is None else observed
    try:
        records = {p.parent.name: p.read_text(encoding='utf-8')
                   for p in (storage/'agent-sessions').glob('*/conversation-tasks.json')}
    except (OSError, ValueError):
        records = None  # one may name a waiting worker: every candidate tree and worker folder is kept
    rows = []
    for kind, group in itertools.groupby(items, key=lambda item: item[0]):
        with ExitStack() as held:
            busy = False
            if remove and kind in ('marker', 'update'):  # a launch writes markers, an update its folders, under these
                try:
                    held.enter_context((product._usage_lock if kind == 'marker' else product.exclusive)(home))
                except OSError:
                    busy = True
            for _, path in group:
                gitfiles = []
                size = measure(path, min(deadline, time.monotonic()+15), gitfiles)
                row = dict(id=identity(str(path))[:16], path=str(path), origin="tare's own leftovers", kind='own',
                           leftover=kind, eligible=False, status='kept', **size)
                try:
                    if time.monotonic() >= deadline:
                        raise ValueError("Not inspected: the sweep's time ran out; the next one goes on.")
                    reason = own_reason(kind, path, size, gitfiles, observed, records, storage, busy)
                    if isinstance(reason, str):
                        raise ValueError(reason)
                    if not remove:
                        row.update(status='orphan', reason='Orphaned: removed when the TUI or tare serve next starts.')
                    elif kind == 'marker':
                        path.unlink()
                    else:  # long-path forms: a tomb's name can pass 260 characters
                        source = io_path(path)
                        tomb = source.with_name(path.name.split('.storage-retiring-')[0]+'.storage-retiring-'+uuid.uuid4().hex)
                        source.rename(tomb)
                        if reason is not None:  # Git's own removal, as run_unit does it, on the tree's own path
                            tomb.rename(source)
                            from .challenger import git
                            git(reason, 'worktree', 'remove', '--force', str(path), timeout=120)
                            tomb = source
                        elif measure(tomb, time.monotonic()+30)['fingerprint'] != size['fingerprint']:
                            tomb.rename(source)
                            raise ValueError('Changed during removal: kept.')
                        if not remove_folder(tomb):  # what Git left, or the whole folder
                            raise OSError('Partly removed; the next sweep ends it: '+str(tomb))
                    if remove:
                        row.update(status='removed', reason='Removed.')
                except Exception as exc:  # uncertain means kept, and one item never stops the others
                    row['reason'] = str(exc)[:300] or type(exc).__name__
                rows.append(row)
    return rows


def kill_orphans():
    """SIGKILL this user's processes whose TARE_OWNER stamp (the Kernel's) names a tare that is provably dead; each
    stamped descendant carries the stamp itself, so each is found on its own. POSIX only: on Windows a Job Object
    ends the tree with tare. A process that cannot be read, or whose owner may be alive, is left alone."""
    if os.name == 'nt':
        return []
    from compute_plane.managed_process import owner_alive
    try:
        namespace = os.readlink('/proc/self/ns/pid')  # a stamp's pid means nothing in another pid namespace
        entries = [p for p in Path('/proc').iterdir() if p.name.isdecimal()]
    except OSError:
        return []  # no Linux /proc: nothing can be told
    killed = []
    for entry in entries:
        pid = int(entry.name)
        try:
            # The pidfd pins this process: if the pid is reused after it, the signal fails instead of hitting another.
            handle = os.pidfd_open(pid)
        except (OSError, AttributeError):
            continue
        try:
            if pid == os.getpid() or entry.stat().st_uid != os.getuid() or os.readlink(entry/'ns'/'pid') != namespace:
                continue
            environ = dict(item.split(b'=', 1) for item in (entry/'environ').read_bytes().split(b'\0') if b'=' in item)
            stamp = environ.get(b'TARE_OWNER', b'').decode('ascii')
            # The same stamp as this tare's own: a shell this tare was started from, not something to end.
            if stamp == os.environ.get('TARE_OWNER') or owner_alive(stamp) is not False:
                continue
            # Only what tare itself would have ended: the process group it made for the command (TARE_GROUP). A
            # daemon the command detached on purpose (a build daemon other programs share) left that group.
            if os.getpgid(pid) != int(environ.get(b'TARE_GROUP', b'x')):
                continue
            row = dict(pid=pid, name=(entry/'comm').read_text().strip(),
                       command=(entry/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()[:200])
            signal.pidfd_send_signal(handle, signal.SIGKILL)
            killed.append(row)
        except (OSError, ValueError):
            continue  # gone or unreadable: skipped
        finally:
            os.close(handle)
    return killed


def sweep(storage, home, roots, *, seconds=60):
    """End tare's orphaned processes, then remove its own orphaned leftovers; record what was ended, removed and kept
    and why."""
    storage, home = plain(storage), plain(home)
    try:
        killed = kill_orphans()
    except Exception:
        killed = []  # never in the way of the folder sweep (an older Kernel has no owner_alive)
    rows = own_rows(storage, home, roots, seconds=seconds, remove=True)
    receipt = storage/'storage-maintenance'/OWN_RECEIPT
    if rows or killed or receipt.exists():
        receipt.parent.mkdir(parents=True, exist_ok=True)
        removed = [r for r in rows if r['status'] == 'removed']
        product.write_json(receipt, dict(finished_at=time.time(), removed_bytes=sum(r['bytes'] for r in removed),
            removed=[dict(path=r['path'], kind=r['leftover'], bytes=r['bytes']) for r in removed],
            kept=[dict(path=r['path'], kind=r['leftover'], reason=r['reason']) for r in rows if r['status'] != 'removed'],
            killed=killed))
    return rows


def sweep_in_background():
    """The sweep when the TUI or tare serve starts: on a daemon thread, so startup never waits, bounded by its
    deadline, and never raising into its caller. The roots are read here, before the caller can change them."""
    try:
        from .native_installation import storage_root
        arguments = (storage_root(), product.product_home(), tool_roots())
    except Exception:
        return
    def run():
        try:
            sweep(*arguments)
        except Exception:
            pass  # the next start tries again
    threading.Thread(target=run, name='tare-own-sweep', daemon=True).start()


def auto_settings_path():
    from .native_installation import descriptor_path
    return Path(os.environ.get('TARE_STORAGE_AUTO') or descriptor_path().parent/'storage-auto.json')


def auto_settings():
    """The operator's opt-in to clean other tools' leftovers automatically: off until chosen."""
    try:
        value = json.loads(auto_settings_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    days, last = value.get('days'), value.get('last_run')
    return {'enabled': value.get('enabled') is True,
            'days': days if type(days) is int and 1 <= days <= 365 else DAYS,
            'last_run': last if isinstance(last, (int, float)) else 0,
            'last_removed_bytes': value.get('last_removed_bytes') or 0}


def save_auto_settings(**changes):
    path = auto_settings_path()
    value = {**auto_settings(), **changes}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')  # the TUI and the updater may write at once
    temporary.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temporary, path)
    return value


def auto_cleanup(storage, home):
    """`tare storage auto`, which the product updater runs when the opt-in is on and a run is due (daily, hourly
    while the disk is low): every tool row a fresh scan finds is removed, never tare's own files."""
    settings = auto_settings()
    if not settings['enabled']:
        return {'status': 'off'}
    report = dict(storage=str(plain(storage)), home=str(plain(home)), observed_at=time.time(),
                  rows=tool_rows(settings['days']), volumes={}, days=settings['days'])
    report['revision'] = identity(report)
    selected = [r['id'] for r in report['rows'] if r['eligible']]
    result = cleanup(report, selected, receipt='last-auto-cleanup.json') if selected else {'removed_bytes': 0}
    save_auto_settings(last_run=time.time(), last_removed_bytes=result['removed_bytes'])
    return {'status': 'ran', 'removed_bytes': result['removed_bytes'], 'items': len(selected)}


def scan(storage, home, *, seconds=90, progress=None, cancelled=None, days=None):
    storage, home = plain(storage), plain(home)
    if home == storage or home.is_relative_to(storage):
        raise ValueError('Choose an operational folder separate from the product installation.')
    deadline = time.monotonic() + seconds
    observed = processes()
    rows = []
    inventory = sources(storage, home)
    for path, origin, kind in system_sources():
        if not any(path.is_relative_to(r) or r.is_relative_to(path) for r in (storage, home)):
            inventory.append((path, origin, kind))
    for path, origin, kind in sorted(inventory, key=lambda row: {'release': 0, 'build': 1}.get(row[2], 2)):
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('Scan cancelled.')
        if progress:
            progress(origin + ' · ' + path.name)
        size = measure(path, min(deadline, time.monotonic() + 15))
        row = dict(id=identity(str(path))[:16], path=str(path), origin=origin, kind=kind,
                   eligible=False, reason='Sessions, evidence and unclassified work are preserved.', **size)
        if kind == 'external':
            row['reason'] = 'Observed only; cleanup belongs to the owning application. Virtual disk size may include sparse space.'
        try:
            if not size['complete'] or size['linked'] or size['git']:
                raise ValueError(size.get('error') or ('Git workspace is preserved.' if size['git'] else 'Linked files are preserved.'))
            if kind == 'release':
                row['reason'] = 'Checking installed package records.'
            elif kind == 'build' and (path/'manifest.json').is_file():
                # Candidate files are separated from the retained build's aggregate.
                for wheel in sorted((path/'wheels-2').glob('*.whl')):
                    measured = measure(wheel, deadline)
                    candidate = dict(id=identity(str(wheel))[:16], path=str(wheel), origin=origin,
                                     kind='wheel', eligible=False, **measured)
                    try:
                        if not measured['complete'] or measured['linked']:
                            raise ValueError('Wheel could not be fully inspected.')
                        candidate['reason'] = wheel_reason(wheel, observed)
                        candidate['eligible'] = True
                    except (OSError, ValueError, KeyError) as exc:
                        candidate['reason'] = str(exc)
                    row['bytes'] -= measured['bytes']
                    rows.append(candidate)
        except (OSError, ValueError, KeyError) as exc:
            row['reason'] = str(exc)
        rows.append(row)
    # Hashing package records is separate from the bounded size inventory, so an
    # old installation cannot consume the measurement budget of every other origin.
    for row in rows:
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('Scan cancelled.')
        if row['kind'] != 'release' or not row['complete'] or row['linked'] or row['git']:
            continue
        if progress:
            progress('Checking installation · ' + Path(row['path']).name)
        try:
            with product.exclusive(home):
                row['reason'] = release_reason(Path(row['path']), home, observed)
            row['eligible'] = True
        except (OSError, ValueError, KeyError) as exc:
            row['reason'] = str(exc)
    if progress:
        progress("tare's own leftovers")
    own = own_rows(storage, home, tool_roots(), seconds=max(1, deadline-time.monotonic()), observed=observed)
    shown = {r['path'] for r in own}  # their own rows replace the folder rows they are part of
    rows = [r for r in rows if r['path'] not in shown]
    for row in rows:
        row['bytes'] -= sum(o['bytes'] for o in own if Path(o['path']).is_relative_to(row['path']))
    rows.extend(own)
    days = days or auto_settings()['days']
    if progress:
        progress('Other tools · old logs, versions and caches')
    rows.extend(tool_rows(days))
    volumes = {}
    for path in (storage, home):
        ancestor = next((p for p in (path, *path.parents) if p.exists()), None)
        if ancestor is not None:
            usage = shutil.disk_usage(ancestor)
            volumes[str(path)] = dict(free=usage.free, total=usage.total,
                                     low=usage.free < max(10*GIB, usage.total*.1))
    report = dict(storage=str(storage), home=str(home), observed_at=time.time(), rows=rows, volumes=volumes, days=days)
    try:
        report['own_sweep'] = read(storage/'storage-maintenance'/OWN_RECEIPT)
    except (OSError, ValueError):
        pass  # no sweep has run on this storage yet
    report['revision'] = identity(report)
    return report


def cleanup(report, selected, *, receipt='last-cleanup.json'):
    """Recheck the exact in-memory preview and protections immediately before removal."""
    if not selected or len(selected) != len(set(selected)):
        raise ValueError('Select cleanup candidates from the preview.')
    home, storage = plain(report['home']), plain(report['storage'])
    if identity({k: v for k, v in report.items() if k != 'revision'}) != report['revision']:
        raise ValueError('Preview changed. Scan again.')
    rows = {r['id']: r for r in report['rows']}
    if any(key not in rows or not rows[key]['eligible'] for key in selected):
        raise ValueError('Selection includes a protected or unknown item.')
    receipt_dir = plain(storage/'storage-maintenance')
    receipt_dir.mkdir(parents=True, exist_ok=True)
    result = dict(preview=report['revision'], started_at=time.time(), removed_bytes=0, items=[])
    # Same lock as install/rollback; also serializes cleanup invocations.
    with product.exclusive(home):
        for key in selected:
            row = rows[key]
            if row['kind'] in TOOL_KINDS:  # rechecked item by item against the same rule, in clean_tool
                try:
                    removed, kept = clean_tool(row, report.get('days', DAYS))
                    reason = f'{kept} item(s) changed, in use or no longer due: kept.'
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    removed, kept, reason = 0, len(row['items']), str(exc)[:300]
                result['removed_bytes'] += removed
                result['items'].append(dict(id=key, path=row['path'], status='kept_or_partial' if kept else 'removed',
                                            removed_bytes=removed, **({'reason': reason} if kept else {})))
                product.write_json(receipt_dir/receipt, result)
                continue
            outcome = dict(id=key, path=row['path'], status='kept')
            try:
                path = plain(row['path'])
                if row['kind'] == 'release':
                    if path.parent != home/'releases':
                        raise ValueError('Release is outside the installation.')
                elif row['kind'] == 'wheel':
                    if not path.is_relative_to(storage/'t') or path.parent.name != 'wheels-2':
                        raise ValueError('Wheel is outside owned build storage.')
                else:
                    raise ValueError('Unsupported cleanup item.')
                now = measure(path, time.monotonic() + 30)
                if not now['complete'] or now['linked'] or now['git'] or now['fingerprint'] != row['fingerprint']:
                    raise ValueError('Files changed since preview. Scan again.')
                observed = processes()
                if row['kind'] == 'release':
                    release_reason(path, home, observed)
                else:
                    wheel_reason(path, observed)
                tomb = path.with_name(path.name + '.storage-retiring-' + uuid.uuid4().hex)
                plain(tomb)
                outcome.update(status='removing', retirement_path=str(tomb))
                result['items'].append(outcome)
                product.write_json(receipt_dir/receipt, result)
                # The absolute target and every ancestor were checked above.
                path.rename(tomb)
                final = measure(tomb, time.monotonic() + 30)
                if not final['complete'] or final['fingerprint'] != row['fingerprint'] or in_use(path, processes()) or in_use(tomb, processes()):
                    if not path.exists():
                        tomb.rename(path)
                    raise ValueError('Item changed or became active during cleanup; preserved.')
                if row['kind'] == 'release':
                    shutil.rmtree(io_path(tomb))  # the tomb's longer name can pass 260 characters
                else:
                    tomb.unlink()
                outcome['status'] = 'removed'
                result['removed_bytes'] += row['bytes']
            except (OSError, ValueError, KeyError) as exc:
                outcome.update(status='kept_or_partial', reason=str(exc))
            if outcome not in result['items']:
                result['items'].append(outcome)
            product.write_json(receipt_dir/receipt, result)
    result['finished_at'] = time.time()
    product.write_json(receipt_dir/receipt, result)
    return result


def low_space(storage, home):
    """Free bytes of the first watched disk under 10 GiB or 10% free; None when all are fine."""
    for path in dict.fromkeys((Path(storage), Path(home))):
        try:
            usage = shutil.disk_usage(path)
            if usage.free < max(10*GIB, usage.total*.1):
                return usage.free
        except OSError:
            continue
    return None


def main(argv=None):
    """`tare storage auto`: the opted-in cleanup of other tools' leftovers; the product updater runs it when due."""
    import argparse
    parser = argparse.ArgumentParser(prog='tare storage', description=main.__doc__)
    parser.add_argument('action', choices=['auto'])
    parser.parse_args(argv)
    from .native_installation import storage_root
    from .native_product import product_home
    print(json.dumps(auto_cleanup(storage_root(), product_home())))
    return 0

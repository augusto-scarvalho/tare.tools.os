"""The secret files and folders a protected command must not read, found once when its sandbox is bound.

Stdlib only, so its tests run without the Kernel's FlatBuffers. A secret created after binding is not covered.
On Windows, MXC denies a path, not a file: a command that could rename a folder between a write scope and a nested
secret would read the secret at its new path (probed 2026-11-13), so the host holds such secrets open while the
binding lives.
"""
import ctypes
import os
from pathlib import Path
import stat
import sys
import weakref

# Named alone, these hold credentials (compared case-insensitively). `.env.*` is a secret too, except the samples.
# Only hidden names: ripgrep's file list skips them, while a listed denied file (`id_rsa`, `_netrc`) would get the
# whole `search` refused as a denied command input.
NAMES = frozenset({'.env', '.ssh', '.aws', '.codex', '.npmrc', '.pypirc', '.netrc', '.git-credentials', '.pgpass',
                   '.gnupg', '.kube', '.azure'})
SAMPLES = ('.example', '.sample', '.template', '.dist', '.defaults')
SKIPPED = frozenset({'node_modules', '__pycache__'})  # and folders holding pyvenv.cfg or CACHEDIR.TAG
MAX_ENTRIES, MAX_DEPTH, MAX_DENIED = 100_000, 32, 32  # MAX_DENIED: MxcPolicy's limit per path group
# IO_REPARSE_TAG_MOUNT_POINT, which `stat` names on Windows only: CI's Linux runners walk test trees too.
JUNCTION = 0xA0000003


def secret(name, folder):
    """Whether the entry `name`, inside a folder named `folder`, holds credentials."""
    name = name.lower()
    return (name in NAMES or name.startswith('.env.') and not name.endswith(SAMPLES)
            or name == 'config.json' and folder.lower() == '.docker')


def link(entry):
    """Whether a scandir entry is a symlink or a junction."""
    return entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), 'st_reparse_tag', 0) == JUNCTION


def linked(modules):
    """Whether the node_modules folder `modules` is a link or holds a package that is one (pnpm, npm link,
    workspaces), at its top level or in a @scope."""
    try:
        info = os.lstat(modules)
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) == JUNCTION:
            return True
        with os.scandir(modules) as listing:
            entries = list(listing)
        for entry in entries:
            if link(entry):
                return True
            if entry.name.startswith('@') and entry.is_dir():
                with os.scandir(entry.path) as scope:
                    if any(map(link, scope)):
                        return True
    except OSError:  # the host cannot read it: neither can the sandbox
        pass
    return False


def find(roots, *, git, modules=None):
    """Sorted paths of the secrets under `roots`, plus every `.git` folder (and a root `.git` file) when `git`.
    `modules`, a list, also receives each node_modules folder the walk passes (it does not search them).

    Links are never followed: a link's target is either outside every read scope, so the sandbox cannot read it
    either, or inside one, where this walk finds it. A folder the host cannot list is skipped for the same reason:
    the sandbox's token is the user's, restricted further. Raises ValueError rather than return a list that is
    incomplete (the walk's bounds) or that the sandbox cannot hold (MAX_DENIED)."""
    found, seen = set(), 0
    pending = [(str(root), 0) for root in roots]
    while pending:
        folder, depth = pending.pop()
        try:
            with os.scandir(folder) as listing:
                entries = list(listing)
        except OSError:
            continue
        seen += len(entries)
        if seen > MAX_ENTRIES:
            raise ValueError(f'Looking for secrets to deny to protected commands, tare stopped after {MAX_ENTRIES:,} '
                'files and folders, so it cannot say none is left readable; protected commands are refused. Open a '
                'smaller folder as the project or remove bulky generated folders (node_modules, __pycache__, '
                'virtual environments and folders tagged CACHEDIR.TAG are not searched).')
        if depth and any(e.name in ('pyvenv.cfg', 'CACHEDIR.TAG') for e in entries):
            continue  # a virtual environment or a tagged cache
        for entry in entries:
            name = entry.name.lower()
            directory = entry.is_dir(follow_symlinks=False) and not link(entry)
            if name == '.git':
                if git and (directory or depth == 0):
                    found.add(entry.path)
            elif secret(name, os.path.basename(folder)):
                if not (directory and os.path.isfile(os.path.join(entry.path, 'pyvenv.cfg'))):  # `.env` as venv
                    found.add(entry.path)
            elif directory and name not in SKIPPED:
                if depth == MAX_DEPTH:
                    raise ValueError(f'Looking for secrets to deny to protected commands, tare found folders more '
                        f'than {MAX_DEPTH} levels deep ({entry.path}), so it cannot say none is left readable; '
                        'protected commands are refused. Open a smaller folder as the project.')
                pending.append((entry.path, depth + 1))
            elif name == 'node_modules' and modules is not None and (directory or link(entry)):
                modules.append(entry.path)
    if len(found) > MAX_DENIED:
        shown, kind = ', '.join(sorted(found)[:3]), ' (Git folders included)' if git else ''
        raise ValueError(f'The project has {len(found)} secret files or folders{kind}, such as {shown}: '
            f'more than the {MAX_DENIED} paths the {"Windows " if sys.platform == "win32" else ""}sandbox can deny, so '
            'protected commands are refused rather than leave some readable. Move secrets out of the project (and '
            'out of the folders granted to it) or open a smaller folder as the project.')
    return sorted(found)


def movable(denied, write):
    """The denied paths a command could carry off by renaming a folder above them: those whose folder lies strictly
    inside a write scope (a scope's own folder can only be renamed from a scope above it, which this checks too)."""
    scopes = [Path(p) for p in write]
    return [p for p in denied if any(scope in Path(p).parent.parents for scope in scopes)]


class Hold:
    """Keeps `paths` open until close() or collection. Windows refuses to rename or move a folder below which something
    is open, whoever asks, so the folders above each path stay where MXC's path rule expects them (probed 2026-11-13:
    every rename and move variant failed). A handle with no access and every sharing mode neither reads the path nor
    keeps anyone from editing, renaming or deleting it. It is not inheritable and dies with this process."""

    def __init__(self, paths):
        self.paths, handles = list(paths), []
        if self.paths:  # Windows only, as MXC is
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CreateFileW.restype = ctypes.c_void_p
        self.close = weakref.finalize(self, lambda: [kernel32.CloseHandle(ctypes.c_void_p(h)) for h in handles])
        for path in self.paths:
            # No access, share read/write/delete, open existing, a folder too, the link itself rather than its target.
            handle = kernel32.CreateFileW(ctypes.c_wchar_p(path), 0, 7, None, 3, 0x02000000 | 0x00200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                reason = ctypes.FormatError(ctypes.get_last_error())
                self.close()
                raise ValueError(f'tare could not hold {path} open ({reason}) to keep the folders above it in place, '
                    'so protected commands are refused rather than leave it readable through a renamed folder. Try '
                    'again, or move the secret out of the project.')
            handles.append(handle)

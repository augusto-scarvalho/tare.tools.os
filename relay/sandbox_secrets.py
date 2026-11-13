"""The secret files and folders a protected command must not read, found once when its Windows sandbox is bound.

Stdlib only, so its tests run without the Kernel's FlatBuffers. A secret created after binding is not covered, and
MXC denies a path, not a file: a command that may edit a folder between a write scope and a nested secret can rename
that folder and read the secret at its new path (probed 2026-11-13); the secret itself cannot be renamed or deleted.
"""
import os
import stat

# Named alone, these hold credentials (compared case-insensitively). `.env.*` is a secret too, except the samples.
# Only hidden names: ripgrep's file list skips them, while a listed denied file (`id_rsa`, `_netrc`) would get the
# whole `search` refused as a denied command input.
NAMES = frozenset({'.env', '.ssh', '.aws', '.codex', '.npmrc', '.pypirc', '.netrc', '.git-credentials', '.pgpass',
                   '.gnupg', '.kube', '.azure'})
SAMPLES = ('.example', '.sample', '.template', '.dist', '.defaults')
SKIPPED = frozenset({'node_modules', '__pycache__'})  # and folders holding pyvenv.cfg or CACHEDIR.TAG
MAX_ENTRIES, MAX_DEPTH, MAX_DENIED = 100_000, 32, 32  # MAX_DENIED: MxcPolicy's limit per path group


def secret(name, folder):
    """Whether the entry `name`, inside a folder named `folder`, holds credentials."""
    name = name.lower()
    return (name in NAMES or name.startswith('.env.') and not name.endswith(SAMPLES)
            or name == 'config.json' and folder.lower() == '.docker')


def find(roots, *, git):
    """Sorted paths of the secrets under `roots`, plus every `.git` folder (and a root `.git` file) when `git`.

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
            directory = entry.is_dir(follow_symlinks=False) and not (entry.is_symlink() or getattr(
                entry.stat(follow_symlinks=False), 'st_reparse_tag', 0) == stat.IO_REPARSE_TAG_MOUNT_POINT)
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
    if len(found) > MAX_DENIED:
        shown, kind = ', '.join(sorted(found)[:3]), ' (Git folders included)' if git else ''
        raise ValueError(f'The project has {len(found)} secret files or folders{kind}, such as {shown}: '
            f'more than the {MAX_DENIED} paths the Windows sandbox can deny, so '
            'protected commands are refused rather than leave some readable. Move secrets out of the project (and '
            'out of the folders granted to it) or open a smaller folder as the project.')
    return sorted(found)

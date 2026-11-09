"""Root-only admission snapshots. No commands, recursive discovery or capability grants."""
import hashlib
import os
from pathlib import Path
import subprocess

from .console_resources import _pinned

QUARANTINE = {'mode': 'quarantine', 'root_sha256': None, 'nested': False}
REVIEW = 'Open /permissions, review the project and start a new session.'


class AdmissionRejected(ValueError):
    """Bounded recovery guidance that the CLI can show directly in the conversation."""


def preview(project):
    root = Path(project).absolute()
    raw = None
    try:
        with _pinned(root, directory=True):
            try:
                with _pinned(root/'AGENTS.md') as (fd, _):
                    with os.fdopen(os.dup(fd), 'rb') as stream:
                        raw = stream.read(16385)
            except FileNotFoundError:
                pass  # Observed absence is a pin, not permission to adopt future text.
        if raw is not None and len(raw) > 16384:
            raise AdmissionRejected('Root AGENTS.md exceeds preview budget. '+REVIEW)
        text = None if raw is None else raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise AdmissionRejected('Root preview unavailable, linked or invalid UTF-8. '+REVIEW) from exc
    return {'project': str(root), 'path': 'AGENTS.md',
            'root_sha256': None if raw is None else hashlib.sha256(raw).hexdigest(),
            'text': text}


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'mode', 'root_sha256', 'nested'}
            or not isinstance(value['mode'], str)
            or value['mode'] not in {'quarantine', 'adopted', 'trusted'} or type(value['nested']) is not bool):
        raise AdmissionRejected('Invalid project admission policy. '+REVIEW)
    pin = value['root_sha256']
    if pin is not None and (not isinstance(pin, str) or len(pin) != 64
                            or any(c not in '0123456789abcdef' for c in pin)):
        raise AdmissionRejected('Invalid root instruction pin. '+REVIEW)
    if value['mode'] == 'quarantine' and (pin is not None or value['nested']):
        raise AdmissionRejected('Quarantine cannot load instructions. '+REVIEW)
    return dict(value)


def configure(project, reviewed, *, nested=False):
    """Trust the repository's AGENTS.md from the exact preview the operator reviewed. A preview that matches HEAD
    needs no pin (later committed versions load too); an uncommitted edit, or a project outside Git, pins the
    reviewed bytes, observed absence included."""
    if reviewed != preview(project):
        raise AdmissionRejected('Project preview changed. '+REVIEW)
    pin = None if local_edit(project) is False else reviewed['root_sha256']
    return validate({'mode': 'trusted', 'root_sha256': pin, 'nested': nested})


def _git(project, *args):
    """Read-only Git observation of the project, never of an inherited GIT_DIR."""
    from compute_plane.managed_process import host_executable
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        env.pop(key, None)
    return subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false', '-C', str(project),
                           *args], capture_output=True, timeout=5, env=env)


def repository(project):
    """The repository a project belongs to: its common Git directory, shared by every worktree; None outside Git."""
    try:
        result = _git(project, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return os.path.normcase(str(Path(result.stdout.decode('utf-8', 'replace').strip()).resolve()))


def local_edit(project):
    """Whether the root AGENTS.md differs from HEAD (modified, staged, deleted or untracked): True or False; None
    outside Git or before the first commit. Git compares through its own filters, so CRLF checkouts match."""
    try:
        head = _git(project, 'rev-parse', '--verify', '-q', 'HEAD')
        status = None if head.returncode else _git(project, 'status', '--porcelain', '--', 'AGENTS.md')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if status is None or status.returncode:
        return None
    return bool(status.stdout.strip())


RECENT_CHANGE = '7 days ago'


def recent_change(project):
    """The last commit at HEAD, within RECENT_CHANGE, that changed the root AGENTS.md: {'commit', 'when', 'subject',
    'added', 'removed'}; None outside Git or when none did. Agents commit and merge here too, so a trusted
    repository's new rules are said in every new conversation's welcome while recent. No "seen" state: whoever
    drives the TUI (an agent in a dogfood run) could not consume it before the operator saw it."""
    try:
        result = _git(project, 'log', '-1', '--since='+RECENT_CHANGE, '--format=%H%x09%cr%x09%s', '--numstat', '--',
                      'AGENTS.md')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    lines = [line for line in result.stdout.decode('utf-8', 'replace').splitlines() if line] if not result.returncode else []
    if not lines:
        return None
    commit, when, subject = (lines[0].split('\t', 2)+['', ''])[:3]
    added, removed = (next((line.split('\t') for line in lines[1:] if line.count('\t') >= 2), ['?', '?']))[:2]
    return {'commit': commit, 'when': when, 'subject': subject, 'added': added, 'removed': removed}


def committed(project):
    """The root AGENTS.md as HEAD has it, in the preview's shape; text None when HEAD has none."""
    result = _git(project, 'cat-file', 'blob', 'HEAD:./AGENTS.md')
    raw = result.stdout if result.returncode == 0 else None
    if raw is not None and len(raw) > 16384:
        raise AdmissionRejected('Root AGENTS.md exceeds preview budget. '+REVIEW)
    try:
        text = None if raw is None else raw.decode('utf-8')
    except UnicodeError as exc:
        raise AdmissionRejected('Committed AGENTS.md is invalid UTF-8. '+REVIEW) from exc
    return {'project': str(Path(project).absolute()), 'path': 'AGENTS.md',
            'root_sha256': None if raw is None else hashlib.sha256(raw).hexdigest(), 'text': text}


def check(project, policy):
    """The root instructions a session loads, or None in quarantine. An adopted root must still match its pin. A
    trusted repository loads AGENTS.md as committed: a merge or pull needs no new review. An uncommitted edit
    loads only once reviewed (its pin); until then the committed version loads and `local_edit` says why."""
    policy = validate(policy)
    if policy['mode'] == 'quarantine':
        return None
    current = preview(project)
    edited = local_edit(project) if policy['mode'] == 'trusted' else None
    if edited is False or current['root_sha256'] == policy['root_sha256'] and (
            edited is None or policy['root_sha256'] is not None):
        return current  # as committed, the reviewed edit, or (adopted, outside Git) the pinned bytes
    if edited is None:
        raise AdmissionRejected('Root instruction changed. '+REVIEW)
    return {**committed(project), 'local_edit': current['root_sha256']}

"""Root-only admission snapshots. No commands, recursive discovery or capability grants."""
import hashlib
import os
from pathlib import Path

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
            or value['mode'] not in {'quarantine', 'adopted'} or type(value['nested']) is not bool):
        raise AdmissionRejected('Invalid project admission policy. '+REVIEW)
    pin = value['root_sha256']
    if pin is not None and (not isinstance(pin, str) or len(pin) != 64
                            or any(c not in '0123456789abcdef' for c in pin)):
        raise AdmissionRejected('Invalid root instruction pin. '+REVIEW)
    if value['mode'] == 'quarantine' and (pin is not None or value['nested']):
        raise AdmissionRejected('Quarantine cannot load instructions. '+REVIEW)
    return dict(value)


def configure(project, reviewed, *, nested=False):
    """Use the exact preview the operator reviewed, including observed absence."""
    if reviewed != preview(project):
        raise AdmissionRejected('Project preview changed. '+REVIEW)
    return validate({'mode': 'adopted', 'root_sha256': reviewed['root_sha256'], 'nested': nested})


def check(project, policy):
    policy = validate(policy)
    if policy['mode'] == 'adopted':
        current = preview(project)
        if current['root_sha256'] != policy['root_sha256']:
            raise AdmissionRejected('Root instruction changed. '+REVIEW)
        return current
    return None

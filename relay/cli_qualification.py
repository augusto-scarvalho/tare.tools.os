"""Which vendor CLI releases this machine already probed (compute_plane.native_cli `qualification`).

The kernel probes a CLI release it has not seen once, before the first real request; this file keeps the verdict so
each machine pays that probe once per release, not once per session. `tare setup --prove cli:NAME` forgets a failed
verdict so the next call probes again.
"""
import json
import os
import tempfile


def path():
    from .native_installation import storage_root
    return storage_root()/'cli-qualification.json'


def _read():
    try:
        value = json.loads(path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(value):
    """Best effort: a verdict that cannot be saved (storage locked by another host writing at the same moment,
    read-only) is probed again next session; it never fails the call that produced it."""
    target, temporary = path(), None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, indent=1))
        os.replace(temporary, target)
    except OSError:
        if temporary is not None and os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


class Store(dict):
    """One endpoint's records, "version model" -> record; each verdict the kernel writes is saved at once.
    # ponytail: two hosts probing the same new release at once both write; the last writer wins (same verdict)."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        records = _read().get(endpoint)
        super().__init__(records if isinstance(records, dict) else {})

    def __setitem__(self, version, record):
        super().__setitem__(version, record)
        value = _read()
        records = value.get(self.endpoint)
        value[self.endpoint] = {**(records if isinstance(records, dict) else {}), version: record}
        _write(value)


def forget_failed(endpoint):
    """Drop the failed verdicts of one CLI, so its next call probes again (an explicit `tare setup --prove`)."""
    value = _read()
    records = value.get(endpoint)
    if not isinstance(records, dict):
        return
    kept = {v: r for v, r in records.items() if not (isinstance(r, dict) and r.get('status') == 'failed')}
    if kept != records:
        value[endpoint] = kept
        _write(value)

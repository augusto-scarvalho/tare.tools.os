"""TCP-01 — Seam 1: Trusted first byte / incumbent controller root.

Deterministic command dispatch behind a fail-closed authority check and a
single-writer boundary. SHADOW module: nothing in the existing harness CLI
routes through it yet (that wiring is TCP-02/TCP-03), so shadow/parity/
rollback contracts are preserved by construction — zero regressions.

The "trusted first byte" invariant: no handler byte executes before
(1) the command resolves exactly in a frozen registry,
(2) mutating commands present a child envelope that verifies against the
    Phase-T root (bootstrap_envelope, PROCESS-REPAIR-01), and
(3) the caller holds the single-writer lock for mutating commands.
Every dispatch appends a hash-chained ledger line — the provenance anchor
TCP-02 binds EffectReceipts to.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from bootstrap_envelope import verify_child

LOCK_TTL_SECONDS = 15 * 60


class AuthorityError(Exception):
    """Raised when dispatch fails closed. The handler was never invoked."""


class SingleWriterLock:
    """Exclusive-create lockfile. One mutating controller per relay root.

    ponytail: staleness is TTL-based (a dead holder is only cleared after
    LOCK_TTL_SECONDS); upgrade path is liveness probing via OpenProcess if
    crash recovery latency ever matters.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.held = False

    def acquire(self) -> None:
        if self.held:
            return
        try:
            stale = time.time() - self.path.stat().st_mtime > LOCK_TTL_SECONDS
        except OSError:
            stale = False
        if stale:
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise AuthorityError(f"single-writer lock held: {self.path}")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": os.getpid(), "acquired_at": time.time()}))
        self.held = True

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class ControllerRoot:
    """Incumbent controller root bound to one Phase-T root envelope."""

    def __init__(self, root_envelope: dict, ledger_path: str | os.PathLike,
                 lock_path: str | os.PathLike | None = None):
        self.root = root_envelope
        self.ledger_path = Path(ledger_path)
        self.lock = SingleWriterLock(lock_path or self.ledger_path.with_suffix(".lock"))
        self._registry: dict[str, tuple] = {}
        self._frozen = False

    def register(self, name: str, handler, mutating: bool) -> None:
        if self._frozen:
            raise AuthorityError("registry frozen; no post-freeze registration")
        if name in self._registry:
            raise AuthorityError(f"duplicate command {name!r}")
        self._registry[name] = (handler, bool(mutating))

    def freeze(self) -> str:
        """Freeze the registry; returns its deterministic digest."""
        self._frozen = True
        return hashlib.sha256(json.dumps(
            sorted((n, m) for n, (_, m) in self._registry.items())
        ).encode()).hexdigest()

    def dispatch(self, command: str, *args, child_envelope: dict | None = None, **kwargs):
        if not self._frozen:
            raise AuthorityError("registry not frozen; dispatch refused")
        if command not in self._registry:
            raise AuthorityError(f"unknown command {command!r} (no fallthrough)")
        handler, mutating = self._registry[command]
        if mutating:
            if child_envelope is None or not verify_child(child_envelope, self.root):
                raise AuthorityError(f"command {command!r} requires a valid child envelope")
            if not self.lock.held:
                raise AuthorityError(f"command {command!r} requires the single-writer lock")
        result = handler(*args, **kwargs)
        self._append_ledger(command, mutating, child_envelope)
        return result

    def _append_ledger(self, command: str, mutating: bool, child: dict | None) -> None:
        prev = "genesis"
        if self.ledger_path.exists():
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
            if lines:
                prev = json.loads(lines[-1])["entry_hash"]
        entry = {
            "ts": time.time(),
            "command": command,
            "mutating": mutating,
            "child_oid": child["oid"] if child else None,
            "root_oid": self.root["oid"],
            "prev": prev,
        }
        entry["entry_hash"] = hashlib.sha256(
            json.dumps(entry, sort_keys=True).encode()).hexdigest()
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")


def verify_ledger(ledger_path: str | os.PathLike) -> bool:
    """Recompute the hash chain; False on any tamper or break."""
    prev = "genesis"
    try:
        lines = Path(ledger_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        entry = json.loads(line)
        claimed = entry.pop("entry_hash", None)
        if entry.get("prev") != prev or claimed != hashlib.sha256(
                json.dumps(entry, sort_keys=True).encode()).hexdigest():
            return False
        prev = claimed
    return True

#!/usr/bin/env python3
"""Ownership epoch (SPEC-149) -- a monotonic fencing token for state writers.

A run that writes canonical state carries an epoch from a single monotonic
counter (`.harness/state/epoch.json`). Contested targets record the owner's
epoch and refuse a writer with a LOWER epoch (fencing token, article Section
5.7): an old run never commits over a newer owner (incident a6c9af5, the
duplicated detached dispatch, and the three siblings 2026-07).

Design (regra 1/6):
- `next_epoch` increments via temp + `os.replace` with a read-back verify,
  under a process lock (threads) plus an O_EXCL file lock (cross-process), so
  local concurrency never yields a duplicate or a regression. A crashed holder
  never deadlocks: a stale lock is stolen. A corrupt/unreadable counter
  fail-opens to a wall-clock epoch (which dominates any small counter, so still
  monotonic) and flags `degraded` -- the caller then treats fencing as advisory.

Pure stdlib, EN-only, ASCII-safe. Self-check: python scripts/harness_lib/epoch.py.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from pathlib import Path

STATE_REL = ".harness/state/epoch.json"
LOCK_NAME = "epoch.lock"
ENV_EPOCH = "HARNESS_RUN_EPOCH"
OVERRIDE_EVENT = "epoch_fence_overridden"

# ponytail: one global in-process lock (not per-root) -- next_epoch is rare, so
# coarse serialization costs nothing; the O_EXCL file lock covers cross-process.
_PROC_LOCK = threading.Lock()

_STALE_STEAL_S = 8.0   # a held lock older than this is a crashed holder -> steal
_ACQUIRE_TIMEOUT_S = 10.0


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _state_path(root: Path | str) -> Path:
    return Path(root) / STATE_REL


def _read_counter(path: Path) -> tuple[int, bool]:
    """(value, corrupt). An absent file is first-use: (0, not corrupt)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0, False
    except OSError:
        return 0, True
    try:
        doc = json.loads(raw)
        val = int(doc["epoch"] if isinstance(doc, dict) else doc)
        return (val, False) if val >= 0 else (0, True)
    except (ValueError, TypeError, KeyError):
        return 0, True


def _atomic_write(path: Path, value: int) -> None:
    tmp = path.with_name(f"epoch.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"epoch": int(value), "updatedAt": _now_iso()}),
                   encoding="utf-8")
    os.replace(str(tmp), str(path))


def _acquire_lock(lock: Path) -> None:
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            now_t = time.time()
            try:
                held = now_t - lock.stat().st_mtime
            except OSError:
                held = 0.0
            if held > _STALE_STEAL_S or now_t - start > _ACQUIRE_TIMEOUT_S:
                # regra 6: a crashed holder must never deadlock the counter.
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            time.sleep(0.005)


def _release_lock(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def next_epoch(root: Path | str) -> tuple[int, bool]:
    """Atomically claim the next epoch. Returns (epoch, degraded).

    `degraded` is True when the counter was corrupt/unreadable and the value was
    derived from the wall clock (regra 6): the epoch is still monotonic but the
    caller should treat fencing as advisory for this run.
    """
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(LOCK_NAME)
    with _PROC_LOCK:
        _acquire_lock(lock)
        try:
            candidate = 1
            degraded = False
            for _ in range(3):  # retry a read-back mismatch (torn write / disk trouble)
                current, corrupt = _read_counter(path)
                degraded = corrupt
                if corrupt:
                    # wall-clock dominates any realistic counter -> no regression.
                    current = max(current, int(time.time()))
                candidate = current + 1
                _atomic_write(path, candidate)
                back, back_corrupt = _read_counter(path)
                if not back_corrupt and back == candidate:
                    return candidate, degraded
            return candidate, True  # read-backs disagreed: value stands, flagged
        finally:
            _release_lock(lock)


def current_epoch(root: Path | str) -> int | None:
    """The counter's current value, or None when absent/unreadable (pure read)."""
    path = _state_path(root)
    if not path.exists():
        return None
    val, corrupt = _read_counter(path)
    return None if corrupt else val


def caller_epoch() -> int | None:
    """This run's epoch from the env pin (HARNESS_RUN_EPOCH), or None (legacy)."""
    raw = os.environ.get(ENV_EPOCH)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def ensure_run_epoch(root: Path | str) -> tuple[int, bool]:
    """This run's epoch: inherit a parent dispatch's HARNESS_RUN_EPOCH if set,
    else acquire a fresh one and publish it to the env so caller_epoch() is
    stable for the rest of the process. Returns (epoch, degraded)."""
    ep = caller_epoch()
    if ep is not None:
        return ep, False
    ep, degraded = next_epoch(root)
    os.environ[ENV_EPOCH] = str(ep)
    return ep, degraded


def record_override(root: Path | str, **fields: object) -> None:
    """Append the loud epoch-fence override record to events.jsonl (regra 4) --
    routed through the EventLedger central chokepoint."""
    from harness_lib import event_ledger
    payload = dict(fields)
    event_ledger.append_critical(OVERRIDE_EVENT, payload, root=Path(root))



def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # first use starts at 1; current_epoch reads it back; None before any use.
        assert current_epoch(root) is None
        e1, d1 = next_epoch(root)
        assert e1 == 1 and d1 is False, (e1, d1)
        assert current_epoch(root) == 1
        assert next_epoch(root)[0] == 2

        # caller_epoch reads the env pin.
        assert caller_epoch() is None
        os.environ[ENV_EPOCH] = "7"
        try:
            assert caller_epoch() == 7
        finally:
            os.environ.pop(ENV_EPOCH, None)

        # concurrency: 8 threads x 25 -> every epoch distinct, monotonic, no dup.
        seen: list[int] = []
        guard = threading.Lock()

        def worker() -> None:
            for _ in range(25):
                ep, _deg = next_epoch(root)
                with guard:
                    seen.append(ep)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(seen) == len(set(seen)), "duplicate epoch under concurrency"
        assert min(seen) >= 3 and max(seen) == current_epoch(root), (min(seen), max(seen))

        # corruption -> degraded True, no regression (regra 6).
        prior = current_epoch(root)
        _state_path(root).write_text("{ not json", encoding="utf-8")
        ec, dc = next_epoch(root)
        assert dc is True and ec > prior, (ec, prior, dc)

        # ensure_run_epoch: acquire+publish, then inherit.
        os.environ.pop(ENV_EPOCH, None)
        ea, _ = ensure_run_epoch(root)
        assert os.environ.get(ENV_EPOCH) == str(ea)
        assert ensure_run_epoch(root)[0] == ea  # inherited, not re-acquired
        os.environ.pop(ENV_EPOCH, None)

        # record_override writes the loud event.
        record_override(root, target="test", priorEpoch=9, callerEpoch=4)
        assert OVERRIDE_EVENT in (root / ".harness" / "runs" / "events.jsonl").read_text(encoding="utf-8")
    print("epoch self-check: ok")


if __name__ == "__main__":
    _demo()

"""Cross-process GPU lease coordination for shared text and image workloads.

Coordinates admission between the text gateway and ComfyUI on a single GPU.
Uses Linux flock on a dedicated lease file with monotonic deadlines.
Stale metadata without a held kernel lock is never authoritative.
"""
from __future__ import annotations

import contextlib
import errno
import hmac
import json
import logging
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any, Callable, Iterator

LOG = logging.getLogger(__name__)
# Supervised GPU jobs (`gpu run`, `gpu hold`) describe themselves here: active-<pid>.json, runs.jsonl.
TRAINING_STATE_DIR = Path(os.environ.get("TARE_GPU_STATE") or Path.home() / ".local/state/tare-gpu-training")


def eta_seconds(state: dict[str, Any], now: float | None = None, state_dir: Path | None = None) -> int | None:
    """Seconds until text gets the GPU (contract gpu-lease/1): what the image job holding it has left of its learned
    duration plus the learned durations of the image jobs waiting; None when one of them has no estimate (ComfyUI, a
    task without history, a run already past its estimate) or no image job is in the way."""
    now, state_dir = now or time.time(), state_dir or TRAINING_STATE_DIR
    owner, ahead = state.get("owner") or {}, []
    if state.get("held") and owner.get("kind") == "image":
        try:
            pid = int(owner.get("pid"))
            job = json.loads((state_dir / f"active-{pid}.json").read_text(encoding="utf-8"))
            left = job["started_at"] + job["expected_seconds"] - now
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if left <= 0:
            return None
        ahead.append(left)
    for ticket in state.get("queue") or []:
        if ticket.get("kind") == "image":
            expected = ticket.get("expected_seconds")
            if not isinstance(expected, (int, float)) or expected <= 0:
                return None
            ahead.append(expected)
    return round(sum(ahead)) if ahead else None


class GpuBusy(TimeoutError):
    """A text request gave way to an image job holding or waiting for the GPU."""


def _get_fcntl():
    try:
        import fcntl
        return fcntl
    except ImportError:
        return None


class SharedGpuLease:
    """Exclusive cross-process GPU lease backed by Linux flock.

    Parameters:
        path: Path to the lease lock file. If None, coordination is disabled.
        timeout: Default bounded acquisition timeout in seconds.
    """

    def __init__(self, path: Path | str | None = None, timeout: float = 60.0) -> None:
        self.path: Path | None = Path(path) if path is not None else None
        self.timeout: float = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("GPU wait timeout must be finite and positive")
        self._thread_lock = threading.Lock()
        self._active_receipt: dict[str, Any] | None = None
        # A finished text lease kept during its idle grace: {"fd", "token", "idle_since"}.
        # Guarded by _thread_lock; the flock stays held on fd until the grace ends.
        self._linger: dict[str, Any] | None = None
        # The named reservation whose deadline the idle grace carries (reserve/end_reservation), or None.
        self._reservation: str | None = None

    @property
    def is_enabled(self) -> bool:
        return self.path is not None

    def _open_lock_file(self, *, create=True) -> int:
        assert self.path is not None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError(f"GPU lock path must not be a symlink: {self.path}")

        flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0)
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno == getattr(errno, "ELOOP", None):
                raise ValueError(f"GPU lock path must not be a symlink: {self.path}") from exc
            raise

        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise ValueError(f"GPU lock path must be a regular file: {self.path}")

        if os.path.islink(self.path):
            os.close(fd)
            raise ValueError(f"GPU lock path must not be a symlink: {self.path}")

        if create:
            try:
                os.fchmod(fd, 0o600)
            except BaseException:
                os.close(fd)
                raise

        return fd

    @contextlib.contextmanager
    def hold(
        self,
        kind: str,
        request_id: str,
        *,
        timeout: float | None = None,
        cancelled: Callable[[], bool] | None = None,
        reason: str = "",
        yield_to_image: float | None = None,
        linger: float = 0.0,
        image_max_wait: float | None = None,
        expected_seconds: float | None = None,
    ) -> Iterator[dict[str, Any] | None]:
        """Acquire exclusive GPU lease for 'text' or 'image'.

        Yields a receipt containing the secret nonce.
        Raises TimeoutError if deadline expires, or InterruptedError if cancelled.
        With yield_to_image, gives up with GpuBusy once an image job has held or
        waited ahead of this request for that many seconds, instead of queueing.
        With linger, the flock is kept for that many idle seconds after the request
        ends, and the next text request of this lease object reuses it without
        queueing. Once an image job has waited image_max_wait seconds, the grace
        is skipped or cut short (never during a running request).
        expected_seconds, the job's learned duration, rides on its queue ticket so status
        can estimate when text gets the GPU (contract gpu-lease/1).
        """
        linger = float(linger)
        if not math.isfinite(linger) or linger < 0:
            raise ValueError("GPU idle grace must be finite and non-negative")
        if image_max_wait is not None:
            image_max_wait = float(image_max_wait)
            if not math.isfinite(image_max_wait) or image_max_wait <= 0:
                raise ValueError("image max wait must be finite and positive")
        if not self.is_enabled:
            yield None
            return

        if kind not in ("text", "image"):
            raise ValueError(f"Invalid lease kind: {kind!r} (expected 'text' or 'image')")
        if not isinstance(request_id, str) or not request_id or len(request_id.encode()) > 128:
            raise ValueError("request_id must be a non-empty string of at most 128 UTF-8 bytes")
        if not isinstance(reason, str) or len(reason) > 200:
            raise ValueError("reason must be a string of at most 200 characters")

        fcntl_mod = _get_fcntl()
        if fcntl_mod is None:
            raise RuntimeError("fcntl is required for GPU lease coordination on POSIX systems")

        effective_timeout = self.timeout if timeout is None else float(timeout)
        if not math.isfinite(effective_timeout) or effective_timeout <= 0:
            raise ValueError("GPU wait timeout must be finite and positive")
        deadline = time.monotonic() + effective_timeout
        poll_interval = 0.05

        if cancelled is not None and cancelled():
            raise InterruptedError("GPU lease acquisition cancelled before wait")

        busy_since = None
        def give_way(ticket_name=None):
            nonlocal busy_since
            if yield_to_image is None:
                return
            if not self._image_ahead(fcntl_mod, ticket_name):
                busy_since = None
                return
            busy_since = busy_since or time.monotonic()
            if time.monotonic() - busy_since >= yield_to_image:
                raise GpuBusy("GPU busy with an image job")

        # First serialize threads in the same process
        acquired_thread_lock = False
        while True:
            if cancelled is not None and cancelled():
                raise InterruptedError("GPU lease acquisition cancelled while waiting for thread lock")
            give_way()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for GPU lease thread lock ({effective_timeout}s)")
            if self._thread_lock.acquire(timeout=min(poll_interval, max(0.001, remaining))):
                acquired_thread_lock = True
                break

        fd = None
        flock_acquired = False
        ticket = None
        inherited_until = None  # deadline of a reused grace; this request never shortens it
        inherited_reservation = None
        try:
            # A text lease still in its idle grace is reused at once: no queue, no giving way.
            fd, inherited_until, inherited_reservation = self._reuse_linger(fcntl_mod, kind, image_max_wait)
            flock_acquired = fd is not None
            if not flock_acquired:
                fd = self._open_lock_file()
                ticket = self._take_ticket(fcntl_mod, {
                    "kind": kind, "request_id": request_id, "reason": reason,
                    "pid": os.getpid(), "since": time.time(), "expected_seconds": expected_seconds})
            while ticket is not None:
                if cancelled is not None and cancelled():
                    raise InterruptedError("GPU lease acquisition cancelled while waiting for file lock")
                give_way(ticket[0])
                if self._first_in_line(fcntl_mod, ticket[0]):
                    try:
                        fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                        flock_acquired = True
                        break
                    except (BlockingIOError, OSError) as exc:
                        if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                            raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for GPU lease ({effective_timeout}s)")
                time.sleep(min(poll_interval, max(0.001, remaining)))
            if ticket is not None:
                self._drop_ticket(ticket)
                ticket = None

            nonce = secrets.token_hex(16)
            acquired_at = time.time()
            metadata = {
                "kind": kind,
                "request_id": request_id,
                "reason": reason,
                "pid": os.getpid(),
                "nonce": nonce,
                "acquired_at": acquired_at,
            }
            raw = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, raw)
            os.ftruncate(fd, len(raw))
            os.fsync(fd)

            receipt = {
                "kind": kind,
                "request_id": request_id,
                "reason": reason,
                "pid": os.getpid(),
                "nonce": nonce,
                "path": str(self.path),
                "acquired_at": acquired_at,
            }
            self._active_receipt = receipt

            yield receipt

        finally:
            if ticket is not None:
                self._drop_ticket(ticket)
            try:
                if flock_acquired and fd is not None:
                    held = self._active_receipt
                    # Keep the later of this request's own grace and the one it reused, so a
                    # request without grace (a resident-model probe) never shortens another's.
                    now = time.monotonic()
                    # A reservation's deadline goes with it: once it ended, no request inherits it.
                    carried = self._reservation if self._reservation is not None and (
                        inherited_reservation == self._reservation or request_id == self._reservation) else None
                    if inherited_reservation is not None and carried is None:
                        inherited_until = None
                    until = max((d for d in (now + linger if linger else None, inherited_until)
                                 if d is not None), default=None)
                    if (until is not None and until > now and held is not None
                            and not self._image_overdue(fcntl_mod, image_max_wait, log=True)):
                        self._start_linger(fcntl_mod, fd, until, image_max_wait, held, carried)
                        fd = None  # the grace owns the descriptor and its flock now
                    else:
                        self._unlock(fcntl_mod, fd)
            finally:
                self._active_receipt = None
                if fd is not None:
                    with contextlib.suppress(Exception):
                        os.close(fd)
                if acquired_thread_lock:
                    self._thread_lock.release()

    # Idle grace for text. Only a thread holding _thread_lock touches _linger, so a grace is
    # either reused by the next text request or released by its watcher, never both.
    def _unlock(self, fcntl_mod, fd: int) -> None:
        """Clear the owner metadata and drop the flock; the caller closes fd."""
        with contextlib.suppress(Exception):
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.fsync(fd)
        fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)

    def _close_held(self, fcntl_mod, fd: int) -> None:
        try:
            self._unlock(fcntl_mod, fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _image_overdue(self, fcntl_mod, max_wait: float | None, *, log: bool = False) -> bool:
        """An image job has waited at least max_wait seconds (never when max_wait is None)."""
        if max_wait is None:
            return False
        try:
            now = time.time()
            for _, meta in self._live_tickets(fcntl_mod):
                since = meta.get("since")
                if meta.get("kind") == "image" and isinstance(since, (int, float)) and now - since >= max_wait:
                    if log:
                        LOG.warning("image job %s (%s) waited %.0fs, over the %.0fs limit: "
                                    "text releases the GPU without idle grace",
                                    meta.get("request_id"), meta.get("reason"), now - since, max_wait)
                    return True
            return False
        except Exception:
            return True  # an unreadable queue never extends text's hold

    def _reuse_linger(self, fcntl_mod, kind: str,
                      image_max_wait: float | None) -> tuple[int | None, float | None, str | None]:
        """Take over the lingering text lease with its grace deadline (monotonic) and reservation, or
        release it when this request may not reuse it. The deadline lets the reusing request keep the grace."""
        lingering, self._linger = self._linger, None
        if lingering is None:
            return None, None, None
        if kind == "text" and not self._image_overdue(fcntl_mod, image_max_wait, log=True):
            return lingering["fd"], lingering["until"], lingering.get("reservation")
        self._close_held(fcntl_mod, lingering["fd"])
        return None, None, None

    def _start_linger(self, fcntl_mod, fd: int, until: float, image_max_wait: float | None,
                      receipt: dict[str, Any], reservation: str | None = None) -> None:
        """Keep fd's flock until the monotonic deadline until, or until an image job is overdue."""
        token = object()

        def watch() -> None:
            while True:
                time.sleep(0.1)
                if not self._thread_lock.acquire(timeout=0.1):
                    continue  # a request is running
                try:
                    current = self._linger
                    if current is None or current["token"] is not token:
                        return  # reused by the next request
                    if (time.monotonic() < current["until"]
                            and not self._image_overdue(fcntl_mod, image_max_wait, log=True)):
                        continue
                    self._linger = None
                    with contextlib.suppress(Exception):
                        self._close_held(fcntl_mod, fd)
                    return
                finally:
                    self._thread_lock.release()

        threading.Thread(target=watch, name="gpu-lease-grace", daemon=True).start()
        self._linger = {"fd": fd, "token": token, "receipt": receipt, "until": until,
                        "idle_since": time.monotonic(), "idle_since_wall": time.time(), "reservation": reservation}

    def reserve(self, reservation_id: str, seconds: float, *, wait: float, reason: str = "") -> None:
        """Hold the GPU for text up to `seconds`: a named reservation (contract gpu-lease/2). The flock stays
        in the idle grace, which only requests the caller admits reuse (the gateway checks the reservation's
        secret) and no waiting image job cuts short (no image_max_wait). Raises TimeoutError when the GPU
        stays taken `wait` seconds."""
        self._reservation = reservation_id
        try:
            with self.hold("text", reservation_id, timeout=max(.001, wait), reason=reason, linger=seconds):
                pass
        except BaseException:
            self._reservation = None
            raise

    def end_reservation(self, reservation_id: str, wait: float = 2.0) -> bool:
        """End the reservation now: its idle grace releases the GPU at once, or, when a request is running on
        it, that request keeps only its own grace when it ends. False when it is not the current one."""
        if self._reservation != reservation_id:
            return False
        self._reservation = None
        if self._thread_lock.acquire(timeout=wait):
            try:
                lingering = self._linger
                if lingering is not None and lingering.get("reservation") == reservation_id:
                    self._linger = None
                    with contextlib.suppress(Exception):
                        self._close_held(_get_fcntl(), lingering["fd"])
            finally:
                self._thread_lock.release()
        return True

    # First-come queue in front of the lease. Waiters hold an flock on their ticket, so a
    # ticket whose lock can be taken belongs to a dead process and is discarded. Tickets are
    # locked before they are renamed into the queue, so a visible ticket is never unlocked
    # by a live owner. A ticket carries its waiter's kind, request_id, reason, pid and since,
    # written before it becomes visible (tickets from older code are empty). Processes running
    # older code skip the queue but still take the same lease flock, so exclusion never
    # depends on the queue.
    @property
    def _queue_dir(self) -> Path:
        assert self.path is not None
        return self.path.with_name(self.path.name + ".queue")

    def _take_ticket(self, fcntl_mod, meta: dict[str, Any]) -> tuple[str, int]:
        queue = self._queue_dir
        queue.mkdir(mode=0o700, exist_ok=True)
        name = f"{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(4)}"
        staging = queue / ("." + name)
        fd = os.open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
            os.write(fd, json.dumps(meta, separators=(",", ":")).encode("utf-8"))
            os.rename(staging, queue / name)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(staging)
            raise
        return name, fd

    def _drop_ticket(self, ticket: tuple[str, int]) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self._queue_dir / ticket[0])
        with contextlib.suppress(OSError):
            os.close(ticket[1])

    def _live_tickets(self, fcntl_mod) -> list[tuple[str, dict[str, Any]]]:
        live = []
        if not self._queue_dir.is_dir():
            return live
        for name in sorted(n for n in os.listdir(self._queue_dir) if not n.startswith(".")):
            try:
                fd = os.open(self._queue_dir / name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                continue  # removed by its owner meanwhile
            try:
                fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
            except (BlockingIOError, OSError):
                meta: dict[str, Any] = {}
                with contextlib.suppress(Exception):
                    meta = json.loads(os.read(fd, 4096).decode("utf-8"))  # empty from older code
                live.append((name, meta if isinstance(meta, dict) else {}))
            else:
                with contextlib.suppress(OSError):
                    os.unlink(self._queue_dir / name)  # owner died while waiting
            finally:
                os.close(fd)
        return live

    def _first_in_line(self, fcntl_mod, name: str) -> bool:
        ahead = [n for n, _ in self._live_tickets(fcntl_mod) if n < name]
        return not ahead

    def _image_ahead(self, fcntl_mod, ticket_name: str | None) -> bool:
        """An image job holds the lease or waits ahead of ticket_name (anywhere when None)."""
        if any(meta.get("kind") == "image" and (ticket_name is None or name < ticket_name)
               for name, meta in self._live_tickets(fcntl_mod)):
            return True
        return (self.status().get("owner") or {}).get("kind") == "image"

    def _queue(self, fcntl_mod) -> list[dict[str, Any]]:
        fields = ("kind", "request_id", "reason", "pid", "since", "expected_seconds")
        return [{k: meta.get(k) for k in fields} for _, meta in self._live_tickets(fcntl_mod)]

    def status(self) -> dict[str, Any]:
        """Report GPU lease status and the requests waiting for it.

        Only reports held if confirmed by flock. Stale metadata without flock
        is never reported as held. Does not leak nonce.
        """
        if not self.is_enabled:
            return {"enabled": False}

        fcntl_mod = _get_fcntl()
        if fcntl_mod is None:
            return {
                "enabled": True,
                "path": str(self.path),
                "held": False,
                "owner": None,
                "error": "fcntl_unavailable",
            }

        fields = ("kind", "request_id", "reason", "pid", "acquired_at")
        free = {"enabled": True, "path": str(self.path), "held": False, "owner": None}
        queue = self._queue(fcntl_mod)
        active = self._active_receipt
        if active is not None:
            return {**free, "held": True, "owner": {k: active[k] for k in fields}, "queue": queue}
        lingering = self._linger
        if lingering is not None:
            owner = {k: lingering["receipt"][k] for k in fields}
            return {**free, "held": True, "owner": owner,
                    "idle_since": lingering["idle_since_wall"], "queue": queue}

        assert self.path is not None
        if not self.path.exists():
            return {**free, "queue": queue}

        if os.path.islink(self.path):
            raise ValueError(f"GPU lock path must not be a symlink: {self.path}")

        fd = None
        try:
            fd = self._open_lock_file(create=False)
            try:
                fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
                return {**free, "queue": queue}
            except (BlockingIOError, OSError) as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                raw = b""
                with contextlib.suppress(Exception):
                    os.lseek(fd, 0, os.SEEK_SET)
                    raw = os.read(fd, 4096)
                owner_info = None
                if raw:
                    with contextlib.suppress(Exception):
                        meta = json.loads(raw.decode("utf-8"))
                        if isinstance(meta, dict):
                            owner_info = {k: meta.get(k) for k in fields}
                return {**free, "held": True, "owner": owner_info, "queue": queue}
        finally:
            if fd is not None:
                os.close(fd)

    def validate_image_lease(self, nonce: str) -> bool:
        """Validate that an external process holds an IMAGE lease matching nonce."""
        if not self.is_enabled:
            return False
        if not isinstance(nonce, str) or not nonce:
            return False

        fcntl_mod = _get_fcntl()
        if fcntl_mod is None:
            return False

        if self._active_receipt is not None:
            return False

        assert self.path is not None
        if not self.path.exists():
            return False
        if os.path.islink(self.path):
            raise ValueError(f"GPU lock path must not be a symlink: {self.path}")

        fd = None
        try:
            fd = self._open_lock_file(create=False)
            try:
                fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
                return False
            except (BlockingIOError, OSError) as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return False

            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096)
            if not raw:
                return False
            meta = json.loads(raw.decode("utf-8"))
            if not isinstance(meta, dict):
                return False
            if meta.get("kind") != "image":
                return False
            stored_nonce = meta.get("nonce")
            if not isinstance(stored_nonce, str) or not hmac.compare_digest(stored_nonce, nonce):
                return False
            return True
        except Exception:
            return False
        finally:
            if fd is not None:
                os.close(fd)

"""Optional pty transport: the ONLY way to answer a vendor prompt that has no API.

Row env-acceptances-inbox, second half. `acceptances.py` asks the vendor what it is
skipping over the vendor's own non-interactive interface; granting has no such
interface — `codex app-server` exposes `hooks/list` and NO trust-granting method,
because the vendor deliberately keeps that decision human. So the owner's yes, once
given in the panel, has to be typed into the vendor's own TUI.

OPTIONAL BY DESIGN. POSIX drives this with the stdlib (`processes.popen_with_tty`);
Windows has no stdlib pty, so nt needs `pywinpty` — and when it is absent the caller
falls back to the visible flow instead of failing. Nothing here is a hard dependency.

This module is only the WIRE. It decides nothing: every keystroke it sends was
authorized by `screen_reader.authorize_keystroke`, which is the single authority and
is grounded in the same screen text the owner could read.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

from harness_lib import processes
# Same notion of "what is actually on the screen" the rail and the reader use, so
# the transport cannot consider a screen ready that the grounding check calls empty.
from harness_lib.screen_reader import sanitize

# What an authorized key NAME actually puts on the wire. This is the WHOLE
# vocabulary a driven key can expand into: `authorize_keystroke` lets nothing
# through but these names, one ASCII alnum character, or a bare \r \n \t.
KEYS = {
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "enter": "\r", "tab": "\t", "esc": "\x1b", "space": " ",
}


def encode(keys: str) -> str:
    """Authorized key name -> terminal bytes. Unknown names are already refused."""
    return KEYS.get(keys, keys)


def available() -> tuple[bool, str]:
    """Can this host drive a pty at all? False is a normal outcome, not an error."""
    if os.name != "nt":
        return True, "stdlib pty"
    try:
        import winpty  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — any import failure means "fall back"
        return False, f"pywinpty unavailable ({exc}); install pywinpty for the headless flow"
    return True, "pywinpty"


class PtySession:
    """A vendor TUI on a pty: read as text, typed into one authorized key at a time.

    Reading runs on a pump thread because neither backend offers a bounded read, and
    a blocked read is exactly how the headless flow would hang the owner's terminal.
    """

    def __init__(self, argv: list[str], *, cwd: Any, env: dict[str, str] | None = None,
                 size: tuple[int, int] = (40, 120)) -> None:
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        self._painted = time.monotonic()
        self._alive = True
        self._proc = None
        self._pty = None
        self._fd = -1
        if os.name == "nt":
            import winpty
            self._pty = winpty.PtyProcess.spawn(
                argv, cwd=str(cwd), env=env, dimensions=size)
        else:
            self._proc, self._fd = processes.popen_with_tty(
                argv, cwd=cwd, env=env, size=size)
        self._pump = threading.Thread(target=self._drain, daemon=True)
        self._pump.start()

    def _read_chunk(self) -> str:
        if self._pty is not None:
            return self._pty.read(4096)
        data = os.read(self._fd, 4096)
        if not data:
            raise EOFError
        return data.decode("utf-8", "replace")

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._read_chunk()
                if not chunk:
                    time.sleep(0.02)  # a backend that returns '' instead of blocking
                    continue
                with self._lock:
                    self._chunks.append(chunk)
                    self._painted = time.monotonic()
        except Exception:  # noqa: BLE001 — EOF, closed fd and vendor exit all end the pump
            pass
        finally:
            self._alive = False

    def screen(self, *, quiet_for: float = 0.5, timeout: float = 20.0) -> str:
        """Everything painted since the last `clear()`, once the TUI stops drawing.

        "Painted" means VISIBLE text, not bytes. ConPTY opens every session with a
        handshake of pure escape sequences (`\\x1b[1t\\x1b[c...`) and then delivers the
        child's real output seconds later; treating those first bytes as a settled
        screen returned a screen with nothing on it, the reader correctly said "not a
        question", and the whole headless flow fell back to visible on Windows every
        single time — silently, because falling back is also what success-with-no-
        question looks like. Proven live 2026-07-29 against a real ConPTY.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                text = "".join(self._chunks)
                settled = time.monotonic() - self._painted >= quiet_for
            if sanitize(text).strip() and (settled or not self._alive):
                return text
            if not self._alive and text:
                return text  # the vendor is gone; this is all there will ever be
            time.sleep(0.05)
        with self._lock:
            return "".join(self._chunks)

    def clear(self) -> None:
        """Drop what was already answered.

        Not housekeeping — a rail. `authorize_keystroke` grounds a verdict in the
        screen text, so carrying a previous screen forward would let text the owner
        already dismissed vouch for a question on a LATER screen.
        """
        with self._lock:
            self._chunks.clear()

    def send(self, keys: str) -> None:
        text = encode(keys)
        if self._pty is not None:
            self._pty.write(text)
        else:
            os.write(self._fd, text.encode("utf-8"))

    def alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        try:
            if self._pty is not None:
                # pywinpty's own teardown closes the ConPTY, which ends the console's
                # whole process group; there is no supported handle to signal past it.
                self._pty.terminate(force=True)
            else:
                # signal_process_tree, NOT proc.terminate(): popen_with_tty gives the
                # child its own session, so the group kill reaches anything the vendor
                # spawned (a just-trusted hook, say) instead of orphaning it. The
                # earlier H1 hazard — killpg hitting the harness's own group — cannot
                # apply here precisely BECAUSE this child is isolated.
                processes.signal_process_tree(self._proc.pid, signal.SIGTERM)
                try:
                    self._proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=5)  # reap; else a zombie lingers
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001 — a vendor that already exited is a fine outcome
            pass
        finally:
            self._alive = False
            if self._fd >= 0:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = -1

"""SPEC-114 chat bridge (pipes to `harness.py chat`) + session registry.

MF split out of ui_panel.py (self-lib-file-budget finding, 2026-07-13):
ChatBridge (one chat subprocess, stdout/stderr -> queue, REPL gate
ladder inherited) and the SPEC-114 v11 multi-session ChatSessions.
ui_panel re-exports every public name, so callers and tests are unchanged.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from .common import now
    from .processes import signal_process_tree
except ImportError:  # run directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import now
    from harness_lib.processes import signal_process_tree

# -- chat bridge (pipes to `harness.py chat`; inherits the REPL gate ladder) --

class ChatBridge:
    """One `harness.py chat` subprocess per server; stdout+stderr → a queue.

    Piped stdin puts the REPL in no-input mode (SPEC-111 R15): no wizard, no
    prompts. Plain lines behave per engine — `--engine manual` runs each line as
    `harness.py <line>`; the default engine talks to the agent. Two daemon reader
    threads (Windows pipes can't be select()ed) tag each line by stream so the
    browser can dim stderr.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.engine: str | None = None
        self.stop_file: Path | None = None  # ⏹: out-of-band turn-stop flag
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        # R24: bounded replay log. Every queued item is also kept here so a page
        # reload / second tab can rebuild the full transcript on connect (the live
        # queue has no replay). `send()` logs the user's own lines here too.
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=400)
        # Cursor-based delivery (flake root cause 2026-07-11): SSE handlers used
        # to CONSUME the shared queue, so a superseded handler could dequeue an
        # item and die without writing it — the message vanished from live view
        # forever. History + a per-connection cursor makes delivery lossless for
        # any number of (zombie) consumers; the queue stays for in-process tests.
        self.total = 0
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        # SPEC-147 inv 5: per-session full-diff ring. tool/tool-result events carry
        # an UNCAPPED diffFull; it is stripped here (never enters history/SSE) and
        # served by GET /api/chat/diff. Bounded: oldest evicted past DIFF_RING.
        self.diffs: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()

    DIFF_RING = 50

    def _emit(self, item: dict[str, Any]) -> None:
        # history first, then the live queue: an item glimpsed mid-append by an
        # SSE connect duplicates (both replay + live) rather than being lost.
        self.history.append(item)
        with self._cond:
            self.total += 1
            self._cond.notify_all()
        self.queue.put(item)

    def read_since(self, cursor: int, timeout: float = 0.5) -> tuple[list[dict[str, Any]], int]:
        """Non-destructive read: everything emitted after `cursor` (bounded by the
        history window), plus the new cursor. A cursor beyond `total` (bridge was
        restarted) resets to 0 so the new session replays in full."""
        with self._cond:
            if cursor > self.total:
                cursor = 0
            if cursor == self.total:
                self._cond.wait(timeout)
            total = self.total
        missed = total - cursor
        if missed <= 0:
            return [], total
        items = list(self.history)
        return items[-missed:] if missed <= len(items) else items, total

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, root: Path, engine: str | None = None, session: str | None = None) -> None:
        with self._lock:
            if self.alive:
                return
            self.history.clear()  # new session = fresh transcript
            self.diffs.clear()  # SPEC-147: full-diff ring dies with the transcript
            with self._cond:
                self.total = 0  # stale cursors detect the reset and replay in full
            cmd = [sys.executable, "scripts/harness.py", "chat"]
            if engine:
                cmd += ["--engine", engine]
            # PYTHONUNBUFFERED so the child flushes prompts/output line-by-line
            # into our pipes instead of block-buffering them out of sight.
            # PYTHONIOENCODING/PYTHONUTF8: force the child's stdout UTF-8 on Windows —
            # else its cp1252 plain lines decode wrong through our utf-8 pipe (U+FFFD).
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "HARNESS_CHAT_EVENTS": "1",
                   "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            if session:
                env["HARNESS_CHAT_SESSION"] = session  # cost ledger session dimension (P2)
                # ⏹ stop button: the REPL blocks in agent.send() during a turn, so a
                # piped line can't stop it — the engines watch this flag file instead
                # (chat_engines._watch_interrupt) and kill the turn's child on touch.
                self.stop_file = Path(root) / ".harness" / "runtime" / f"chat-stop-{session}.flag"
                try:
                    self.stop_file.unlink(missing_ok=True)  # stale flag must not kill turn 1
                except OSError:
                    pass
                env["HARNESS_CHAT_STOP_FILE"] = str(self.stop_file)
            self.proc = subprocess.Popen(
                cmd, cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env)
            self.engine = engine
            for tag, stream in (("out", self.proc.stdout), ("err", self.proc.stderr)):
                threading.Thread(target=self._drain, args=(tag, stream),
                                 daemon=True).start()

    def _drain(self, tag: str, stream: Any) -> None:
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip("\n")
                # The REPL's input() prompt is written without a newline, so an
                # event can arrive glued to it ("harness> \x1e{...}") — partition
                # on the sentinel anywhere, and drop the prompt echo (panel noise).
                pre, sep, evt = line.partition("\x1e")
                if sep:
                    try:
                        data = json.loads(evt)
                        # SPEC-147: strip the uncapped diffFull into the ring BEFORE
                        # the event reaches history/SSE (chips stay capped; the
                        # workspace pane fetches /api/chat/diff by tool id).
                        if isinstance(data, dict) and "diffFull" in data:
                            full = data.pop("diffFull")
                            tid = str(data.get("id") or "")
                            if tid and isinstance(full, dict):
                                self.diffs[tid] = full
                                while len(self.diffs) > self.DIFF_RING:
                                    self.diffs.popitem(last=False)
                        self._emit({"stream": "evt", "data": data})
                        if pre.strip() and pre.strip() != "harness>":
                            self._emit({"stream": tag, "line": pre})
                        continue
                    except (ValueError, json.JSONDecodeError):
                        pass  # not valid JSON — surface the whole line as-is
                if line.strip() == "harness>":
                    continue
                self._emit({"stream": tag, "line": line})
        except Exception:
            pass
        if tag == "out":  # stdout EOF ⇒ child gone; surface one exit event
            proc = self.proc  # local ref: stop() may null this concurrently
            if proc is not None:
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
                if proc.poll() is not None:
                    self._emit({"stream": "evt",
                                "data": {"event": "exit", "rc": proc.returncode}})

    def send(self, line: str) -> bool:
        with self._lock:
            if not self.alive or self.proc is None or self.proc.stdin is None:
                return False
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
                # History only (never the live queue): the front echoes the sender's
                # line locally; replay rebuilds it for a reloaded/second tab.
                self.history.append({"stream": "you", "line": line})
                return True
            except (OSError, ValueError):
                return False

    def stop_turn(self) -> bool:
        """⏹: touch the stop flag; the engine's watcher kills the turn's child
        within ~0.25s and the REPL closes the turn with the partial reply."""
        if self.stop_file is None or not self.alive:
            return False
        try:
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file.write_text("stop", encoding="utf-8")
            return True
        except OSError:
            return False

    def read(self, timeout: float = 0.5) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            items.append(self.queue.get(timeout=timeout))
        except queue.Empty:
            return items
        while True:  # drain the rest without blocking
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                return items

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
            self.proc = None
        if proc is None:
            return
        try:  # closing stdin = EOF -> the REPL's input() raises EOFError, returns 0
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            signal_process_tree(proc.pid, getattr(signal, "SIGTERM", 15))
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


# -- multi-session registry (SPEC-114 v11) -----------------------------------

class ChatSessions:
    """A dict of named ChatBridge sessions for the panel. ChatBridge is already
    per-instance clean (its own proc/queue/history/cursor), so multi-session is
    just a lock + a dict of bridges — no ChatBridge change. Every session starts
    with the engine it is handed (the server's overseer default; the owner ruled
    out per-session engine pickers). `main` is the back-compat lazy default a
    session-less request lands on. Subprocess start/stop happen OUTSIDE the lock
    (they spawn/kill a `harness.py chat` child)."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def _add_locked(self, sid: str, engine: str | None, label: str) -> ChatBridge:
        bridge = ChatBridge()
        self._sessions[sid] = {"bridge": bridge, "engine": engine,
                               "label": label, "createdAt": now()}
        return bridge

    def ensure(self, sid: str, root: Path, engine: str | None = None) -> ChatBridge:
        """Get session `sid`'s bridge, creating it if absent (the lazy `main`
        back-compat path). start() is idempotent — it no-ops a live bridge and
        restarts a dead one — so a route can call this every time."""
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                bridge, eng = self._add_locked(sid, engine, sid), engine
            else:
                bridge, eng = entry["bridge"], entry["engine"]
        bridge.start(root, engine=eng, session=sid)
        return bridge

    def new(self, root: Path, engine: str | None = None, label: str | None = None) -> str:
        """Create + start a fresh auto-id session ("s1", "s2", …). Returns its id."""
        with self._lock:
            self._seq += 1
            sid = f"s{self._seq}"
            bridge = self._add_locked(sid, engine, label or f"session {self._seq}")
        bridge.start(root, engine=engine, session=sid)
        return sid

    def get(self, sid: str) -> ChatBridge | None:
        with self._lock:
            entry = self._sessions.get(sid)
        return entry["bridge"] if entry else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._sessions.items())
        return [{"id": sid, "label": e["label"], "alive": e["bridge"].alive,
                 "engine": e["engine"], "createdAt": e["createdAt"]}
                for sid, e in entries]

    def restart(self, sid: str, root: Path) -> None:
        """Restart a session in place, reusing its stored engine (no engine swap —
        engine lives in /config/prefs now). Missing session → created."""
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                bridge, eng = self._add_locked(sid, None, sid), None
            else:
                bridge, eng = entry["bridge"], entry["engine"]
        bridge.stop()
        bridge.start(root, engine=eng, session=sid)

    def close(self, sid: str) -> bool:
        """Stop + remove a session (frees its subprocess). True if it existed."""
        with self._lock:
            entry = self._sessions.pop(sid, None)
        if entry is not None:
            entry["bridge"].stop()
        return entry is not None

    def stop_all(self) -> None:
        """Stop every session's bridge and clear the registry (atexit / shutdown)."""
        with self._lock:
            bridges = [e["bridge"] for e in self._sessions.values()]
            self._sessions.clear()
        for bridge in bridges:
            bridge.stop()



"""ide-lsp wave1 · Lane A — the pylsp WebSocket bridge (server side, Python only).

Live LSP intelligence (hover / go-to-def / completion) for the React CM6 editor,
over a WebSocket served by the SAME loopback panel process/port/token as every
other route (SPEC-114). NO second Node server, NO second port, NO new pip dep
beyond a zero-touch lazy `python-lsp-server` install.

Three pieces live here:

1. A hand-rolled RFC6455 layer: the 101 handshake (`accept_key`), a minimal frame
   codec (client-masked text frames, fragmentation reassembly, ping/pong, close,
   an 8 MB message cap), bounded and loopback-only.
2. The stdio bridge: pylsp speaks `Content-Length`-framed JSON-RPC on stdio; the WS
   side is one JSON-RPC message per text frame (the marimo CM6 client's convention).
   ONE pylsp child, lazy on the first connect, atexit-killed, last-tab-wins.
3. The URI seam + guard. The client (Lane B, commit 979dd7e) speaks a
   `file:///<repo-relative>` namespace with `rootUri = file:///` — it cannot know the
   server's absolute repo root. The bridge maps that namespace onto the REAL workspace
   root for pylsp (initialize rootUri/workspaceFolders + every textDocument URI, both
   directions) and REFUSES any client URI that escapes the confined tree — the decision
   is `ws_files.resolve_confined` (ONE function; its denylist reused, never copied).
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import re
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

try:
    from . import ws_files
    from .processes import popen_stdio_bytes, run_quiet
except ImportError:  # run directly (python scripts/harness_lib/lsp_ws.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import ws_files
    from harness_lib.processes import popen_stdio_bytes, run_quiet

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC6455 magic
MAX_MESSAGE = 8 * 1024 * 1024  # 8 MB cap: pathological frames die here

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WSProtocolError(Exception):
    """A framing violation (oversize / bad continuation) — the connection is torn down."""


# -- RFC6455 handshake + frame codec ---------------------------------------------

def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept = base64(sha1(key + GUID)) (RFC6455 §4.2.2)."""
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """One UNMASKED server->client frame (servers never mask; RFC6455 §5.1)."""
    fin_op = 0x80 | opcode
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", fin_op, n)
    elif n < (1 << 16):
        header = struct.pack("!BBH", fin_op, 126, n)
    else:
        header = struct.pack("!BBQ", fin_op, 127, n)
    return header + payload


def _unmask(payload: bytes, mask: bytes) -> bytes:
    # ponytail: per-byte XOR, O(n) Python. Bounded by MAX_MESSAGE; the 2 MB didOpen is
    # a one-shot ~ms cost. If keystroke didChange latency ever shows, switch to the
    # int.from_bytes XOR trick — not worth the obscurity today.
    return bytes(b ^ mask[i & 3] for i, b in enumerate(payload))


def _read_exact(rfile: Any, n: int) -> bytes | None:
    """Exactly `n` bytes, or None on EOF/short read (buffered read loops internally)."""
    data = rfile.read(n)
    if data is None or len(data) < n:
        return None
    return data


def _read_frame(rfile: Any) -> tuple[bool, int, bytes] | None:
    """(fin, opcode, payload) for ONE frame; None on EOF. Raises WSProtocolError on cap."""
    head = _read_exact(rfile, 2)
    if not head:
        return None
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = _read_exact(rfile, 2)
        if not ext:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _read_exact(rfile, 8)
        if not ext:
            return None
        length = struct.unpack("!Q", ext)[0]
    if length > MAX_MESSAGE:
        raise WSProtocolError(f"frame length {length} exceeds the {MAX_MESSAGE} cap")
    mask = _read_exact(rfile, 4) if masked else b""
    if masked and mask is None:
        return None
    payload = _read_exact(rfile, length) if length else b""
    if length and payload is None:
        return None
    if masked:
        payload = _unmask(payload, mask)
    return fin, opcode, payload


def read_message(rfile: Any, send: Callable[[int, bytes], None],
                 max_bytes: int = MAX_MESSAGE) -> tuple[int, bytes] | None:
    """One full (possibly fragmented) data message. Control frames are answered inline
    via `send(opcode, payload)`: ping->pong, close->echo+stop. Returns (opcode, payload)
    for a data message, or None on close/EOF. Raises WSProtocolError past the cap."""
    frags = bytearray()
    msg_opcode: int | None = None
    while True:
        frame = _read_frame(rfile)
        if frame is None:
            return None
        fin, opcode, payload = frame
        if opcode == OP_CLOSE:
            send(OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
            return None
        if opcode == OP_PING:
            send(OP_PONG, payload)
            continue
        if opcode == OP_PONG:
            continue
        if opcode == OP_CONT:
            if msg_opcode is None:
                raise WSProtocolError("continuation frame without a start frame")
        else:
            msg_opcode = opcode
        frags += payload
        if len(frags) > max_bytes:
            raise WSProtocolError(f"message exceeds the {max_bytes} cap")
        if fin:
            return msg_opcode, bytes(frags)


# -- URI seam: client `file:///<rel>` namespace <-> real workspace root ----------

def _client_rel(uri: str) -> str | None:
    """The repo-relative POSIX path inside a client `file:///<rel>` URI. "" for the root
    sentinel `file:///`; None for anything NOT in the namespace (a `file://<authority>/`
    URI or a foreign scheme) — the caller refuses those."""
    if not uri.startswith("file:///"):
        return None  # file://host/... or untitled:/... : not the client's root namespace
    return unquote(uri[len("file:///"):]).lstrip("/")


def uri_guard(root: Path, uri: str) -> tuple[str | None, str]:
    """Map a client URI onto a real `file://` URI, or refuse it.

    (server_uri, "") when the client `file:///<rel>` URI maps to a confined workspace
    path (the `file:///` sentinel -> the real repo root, for rootUri/workspaceFolders);
    (None, reason) when it escapes. The confinement decision — deny prefixes, the secrets
    denylist (`.env` & friends), protected instruction files, `..`/drive/backslash
    rejection — is `ws_files.resolve_confined`, reused whole, never re-implemented."""
    rel = _client_rel(uri)
    if rel is None:
        return None, f"uri outside the client workspace namespace: {uri}"
    root = Path(root).resolve()
    if rel == "":
        return root.as_uri(), ""  # rootUri / workspaceFolders sentinel
    path, bad = ws_files.resolve_confined(root, rel)
    if path is None:
        return None, bad
    return path.resolve().as_uri(), ""


def _uri_to_path(uri: str) -> Path | None:
    if not uri.startswith("file://"):
        return None
    path = unquote(urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:", path):  # file:///C:/x -> C:/x (Windows drive)
        path = path[1:]
    try:
        return Path(path)
    except (ValueError, OSError):
        return None


def to_client_uri(root: Path, uri: str) -> str:
    """Map a pylsp `file://` URI back into the client's `file:///<rel>` namespace when it
    is under root; pass anything else through UNCHANGED. A def target in the stdlib (jedi
    resolves into the venv/CPython) stays absolute so Lane B renders it "outside
    workspace" — the coherent counterpart of the client->server refusal."""
    p = _uri_to_path(uri)
    if p is None:
        return uri
    try:
        rel = p.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return uri
    if rel == ".":
        return "file:///"
    return "file:///" + "/".join(quote(seg, safe="") for seg in rel.split("/"))


_URI_KEYS = ("uri", "targetUri", "rootUri")  # key-aware: never touch a didOpen `text` body


def _map_uris(node: Any, fn: Callable[[str], str], key: str | None = None) -> Any:
    if isinstance(node, dict):
        return {k: _map_uris(v, fn, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_map_uris(v, fn, key) for v in node]
    if isinstance(node, str) and key in _URI_KEYS and node.startswith("file://"):
        return fn(node)
    return node


def to_server_message(root: Path, msg: Any) -> tuple[Any, str | None]:
    """Rewrite every client URI in a JSON-RPC message to its real workspace target, or
    refuse: (rewritten_msg, None) when all URIs are confined; (None, reason) when any one
    escapes (the whole message is dropped — a poisoned didOpen never reaches pylsp)."""
    errors: list[str] = []

    def fn(uri: str) -> str:
        server_uri, bad = uri_guard(root, uri)
        if server_uri is None:
            errors.append(bad)
            return uri
        return server_uri

    out = _map_uris(msg, fn)
    return (None, errors[0]) if errors else (out, None)


def to_client_message(root: Path, msg: Any) -> Any:
    """Rewrite every under-root `file://` URI in a pylsp message back to the client
    namespace (definition/references targets, etc.); out-of-tree URIs pass through."""
    return _map_uris(msg, lambda u: to_client_uri(root, u))


# -- pylsp: zero-touch lazy availability (the ws_files.lint_content pattern) ------

_PYLSP_STATE: bool | None = None  # None=unchecked, True=importable, False=absent (this process)
_PYLSP_LOCK = threading.Lock()


def _pylsp_importable() -> bool:
    try:
        res = run_quiet([sys.executable, "-c", "import pylsp"],
                        capture_output=True, text=True, timeout=30)
        return res.returncode == 0
    except Exception:  # noqa: BLE001 - probe is best-effort
        return False


def ensure_pylsp(root: Path) -> bool:
    """True when pylsp can be spawned. Missing => ONE zero-touch `uv pip install
    python-lsp-server` attempt (graceful offline), then re-probe. Cached per process:
    absent stays absent (the WS route refuses cleanly; the editor is unchanged)."""
    global _PYLSP_STATE
    with _PYLSP_LOCK:
        if _PYLSP_STATE is not None:
            return _PYLSP_STATE
        if _pylsp_importable():
            _PYLSP_STATE = True
            return True
        try:  # one attempt, into the project venv, bounded — mirrors ws_files.lint_content
            import os
            env = {**os.environ, "VIRTUAL_ENV": str(Path(root) / ".venv")}
            run_quiet(["uv", "pip", "install", "python-lsp-server"], check=True,
                      timeout=180, capture_output=True, env=env)
        except Exception:  # noqa: BLE001 - zero-touch is best-effort; offline stays graceful
            pass
        _PYLSP_STATE = _pylsp_importable()
        return _PYLSP_STATE


def _spawn_pylsp() -> subprocess.Popen:
    return popen_stdio_bytes([sys.executable, "-m", "pylsp"])


def _read_lsp_message(stream: Any) -> bytes | None:
    """One Content-Length-framed JSON-RPC body from pylsp stdout; None on EOF."""
    length = 0
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break  # blank line ends the header block
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None
    body = stream.read(length) if length else b""
    if body is None or len(body) < length:
        return None
    return body


def _write_lsp_message(stream: Any, body: bytes) -> None:
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body))
    stream.write(body)
    stream.flush()


# -- the connection: one WS client <-> one pylsp child, last-tab-wins ------------

_current_lock = threading.Lock()
_current: "_Connection | None" = None
_atexit_done = False


class _Connection:
    def __init__(self, handler: Any, root: Path) -> None:
        self.root = Path(root).resolve()
        self.rfile = handler.rfile
        self.wfile = handler.wfile
        self.sock = handler.connection
        self.wlock = threading.Lock()
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None

    def send_ws(self, opcode: int, payload: bytes) -> None:
        with self.wlock:
            try:
                self.wfile.write(encode_frame(payload, opcode))
                self.wfile.flush()
            except (OSError, ValueError):
                self.stop.set()

    def close(self) -> None:
        self.stop.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        for shut in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                shut()
            except OSError:
                pass


def _register_atexit() -> None:
    global _atexit_done
    if not _atexit_done:
        _atexit_done = True
        atexit.register(shutdown)  # belt-and-suspenders; harness_ui.serve registers it too


def shutdown() -> None:
    """Kill the live pylsp child + close its socket (atexit / panel teardown). Idempotent."""
    with _current_lock:
        conn = _current
    if conn is not None:
        conn.close()


def _install(conn: _Connection) -> None:
    """Register `conn` as the single live connection; supersede any previous one
    (last-tab-wins: close its socket, recycle its pylsp) and spawn a fresh child."""
    global _current
    _register_atexit()
    with _current_lock:
        prev, _current = _current, conn
    if prev is not None:
        prev.close()
    conn.proc = _spawn_pylsp()
    threading.Thread(target=_pump_server, args=(conn,), daemon=True).start()


def _uninstall(conn: _Connection) -> None:
    global _current
    conn.close()
    with _current_lock:
        if _current is conn:
            _current = None


def _refuse(conn: _Connection, rpc: dict, reason: str) -> None:
    """A guard-refused REQUEST gets a JSON-RPC error back; a notification is dropped."""
    rid = rpc.get("id")
    if rid is None:
        return  # didOpen/didChange with a bad uri: silently never reaches pylsp
    err = {"jsonrpc": "2.0", "id": rid,
           "error": {"code": -32600, "message": f"uri refused: {reason}"}}
    conn.send_ws(OP_TEXT, json.dumps(err).encode("utf-8"))


def _pump_client(conn: _Connection) -> None:
    """Client WS frames -> pylsp stdin (guarded + URI-translated). Runs on the handler
    thread; returns when the client disconnects or the connection is superseded."""
    while not conn.stop.is_set():
        try:
            msg = read_message(conn.rfile, conn.send_ws)
        except WSProtocolError:
            conn.send_ws(OP_CLOSE, struct.pack("!H", 1009))  # 1009 = message too big
            return
        except (OSError, ValueError):
            return
        if msg is None:
            return
        opcode, payload = msg
        if opcode != OP_TEXT:
            continue  # binary is not part of the JSON-RPC seam
        try:
            rpc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(rpc, dict):
            continue
        out, reason = to_server_message(conn.root, rpc)
        if reason is not None:
            _refuse(conn, rpc, reason)
            continue
        proc = conn.proc
        if proc is not None and proc.poll() is None and proc.stdin is not None:
            try:
                _write_lsp_message(proc.stdin, json.dumps(out).encode("utf-8"))
            except (OSError, ValueError):
                return


def _pump_server(conn: _Connection) -> None:
    """pylsp stdout -> client WS frames (URI-translated back to the client namespace)."""
    proc = conn.proc
    try:
        while not conn.stop.is_set() and proc is not None and proc.stdout is not None:
            body = _read_lsp_message(proc.stdout)
            if body is None:
                break
            try:
                rpc = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            conn.send_ws(OP_TEXT, json.dumps(to_client_message(conn.root, rpc)).encode("utf-8"))
    except (OSError, ValueError):
        pass
    finally:
        conn.stop.set()
        try:  # unblock _pump_client's read so the handler thread can unwind
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _is_upgrade(handler: Any) -> bool:
    return ("websocket" in (handler.headers.get("Upgrade") or "").lower()
            and bool(handler.headers.get("Sec-WebSocket-Key")))


def serve_ws(handler: Any, ws_root: Path | None = None) -> None:
    """GET /api/lsp entry (auth ALREADY enforced by do_GET's _authed — a bad token is a
    403 before this runs, before any upgrade byte). Validate the upgrade, ensure pylsp,
    complete the 101 handshake, then bridge frames <-> pylsp until the client leaves.
    pylsp absent => a 503 (NO 101): Lane B's probe never opens, the editor stays as-is."""
    root = handler.root
    if not _is_upgrade(handler):
        return handler.send_error(400, "expected a WebSocket upgrade")
    if not ensure_pylsp(root):
        return handler.send_error(503, "LSP server unavailable")
    handler.close_connection = True  # we own the socket for the connection's life
    accept = accept_key(handler.headers.get("Sec-WebSocket-Key", ""))
    handler.wfile.write(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n\r\n")
    handler.wfile.flush()
    conn = _Connection(handler, ws_root or root)
    _install(conn)
    try:
        _pump_client(conn)
    finally:
        _uninstall(conn)


# -- self-check (ponytail: codec + accept-key + the confinement seam fail loudly) --

def _selfcheck() -> None:
    import io

    # RFC6455 §1.3 canonical accept-key vector.
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "accept-key"

    # Frame codec: server encode -> client decode round-trip.
    dec = _decode_server_frame(encode_frame(b"hello", OP_TEXT))
    assert dec == (OP_TEXT, True, b"hello"), dec

    # Masked client frame -> read_message decode (single + fragmented + ping auto-pong).
    pongs: list[bytes] = []
    single = read_message(io.BytesIO(_mask_frame(b'{"x":1}', OP_TEXT, fin=True)),
                          lambda op, p: pongs.append(p))
    assert single == (OP_TEXT, b'{"x":1}'), single
    frag = (_mask_frame(b"ab", OP_TEXT, fin=False) + _mask_frame(b"", OP_PING, fin=True)
            + _mask_frame(b"cd", OP_CONT, fin=True))
    got = read_message(io.BytesIO(frag), lambda op, p: pongs.append(p))
    assert got == (OP_TEXT, b"abcd") and pongs and pongs[-1] == b"", (got, pongs)

    # 8 MB cap: an oversize declared length raises before allocation ruin.
    over = struct.pack("!BBQ", 0x81, 127, MAX_MESSAGE + 1) + b"\x00\x00\x00\x00"
    try:
        read_message(io.BytesIO(over), lambda op, p: None)
        raise AssertionError("cap must raise")
    except WSProtocolError:
        pass

    # URI seam + guard: confined maps, everything out-of-tree refuses. resolve_confined
    # owns the denylist — this table proves the reuse, never a copy.
    root = Path(__file__).resolve().parents[2]
    ok_uri, bad = uri_guard(root, "file:///scripts/harness_lib/ws_files.py")
    assert bad == "" and ok_uri is not None and ok_uri.startswith(root.as_uri()), ok_uri
    assert uri_guard(root, "file:///")[0] == root.as_uri(), "root sentinel -> real root"
    for refuse in ("file:///.env", "file:///C:/Windows/system32/x.py",
                   "file:///../secret.py", "file://host/share/x.py", "untitled:foo"):
        path, reason = uri_guard(root, refuse)
        assert path is None and reason, f"{refuse!r} must be refused"

    # Round-trip: a real repo URI maps out and back to the same client-namespace URI.
    server_uri, _ = uri_guard(root, "file:///scripts/harness_ui.py")
    assert to_client_uri(root, server_uri) == "file:///scripts/harness_ui.py", server_uri
    # An out-of-tree server URI passes through unchanged (Lane B => "outside workspace").
    outside = (root.parent / "elsewhere" / "os.py").as_uri()
    assert to_client_uri(root, outside) == outside, outside

    # Message-level: initialize's rootUri sentinel rewrites, a poisoned didOpen refuses.
    init = {"id": 1, "method": "initialize", "params": {"rootUri": "file:///",
            "workspaceFolders": [{"uri": "file:///", "name": "root"}]}}
    out, reason = to_server_message(root, init)
    assert reason is None and out["params"]["rootUri"] == root.as_uri(), out
    assert out["params"]["workspaceFolders"][0]["uri"] == root.as_uri(), out
    poison = {"method": "textDocument/didOpen",
              "params": {"textDocument": {"uri": "file:///.env", "text": "SECRET=1"}}}
    _, reason = to_server_message(root, poison)
    assert reason is not None, "a .env didOpen must be refused"


def _mask_frame(payload: bytes, opcode: int, fin: bool) -> bytes:
    """A MASKED client frame (test helper — real clients mask; servers never do)."""
    mask = b"\x37\xfa\x21\x3d"
    masked = _unmask(payload, mask)  # XOR is its own inverse
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, 0x80 | n)
    elif n < (1 << 16):
        head = struct.pack("!BBH", b0, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", b0, 0x80 | 127, n)
    return head + mask + masked


def _decode_server_frame(frame: bytes) -> tuple[int, bool, bytes]:
    """Decode ONE unmasked server frame (test helper): (opcode, fin, payload)."""
    import io
    fin, opcode, payload = _read_frame(io.BytesIO(frame))  # type: ignore[misc]
    return opcode, fin, payload


if __name__ == "__main__":
    _selfcheck()
    print("lsp_ws selfcheck ok")

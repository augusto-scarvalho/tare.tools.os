#!/usr/bin/env python3
"""relay_io — canonical Relay I/O primitives (TRAIN-06, Seam B/D).

One shared path for every relay read/write:
- readers tolerate UTF-8 BOM (utf-8-sig);
- writers emit UTF-8 WITHOUT BOM, ensure_ascii=False, via atomic tmp+replace;
- derived writes are digest-guarded: identical content preserves bytes+mtime;
- typed message envelopes with a durable action key (train_id, revision,
  required_action, role) — subject is display-only, never authority;
- durable action outcomes (ACK / RETRY with bounded backoff / QUARANTINE)
  so at-least-once Drive delivery yields exactly-once EFFECTS.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

ENVELOPE_VERSION = 1
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 30  # 30s, 60s, 120s

RELAY_DIR = Path(__file__).resolve().parent
OUTCOMES_DIR = RELAY_DIR / "history" / "outcomes"


def safe_subprocess_kwargs(headless: bool = True) -> dict[str, Any]:
    """Returns platform-safe subprocess kwargs to guarantee fully headless background execution on Windows without popups, window flashing, or console focus stealing."""
    import subprocess
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32" and headless:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = creationflags
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def read_text(path: Path | str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return default


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def write_text_atomic(path: Path | str, content: str) -> None:
    """UTF-8 (no BOM) atomic write via tmp + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nonce = f"{os.getpid()}_{threading.get_ident()}_{time.time_ns() % 10000000}"
    tmp = path.parent / f".{path.name}.tmp{nonce}"
    try:
        tmp.write_bytes(content.encode("utf-8"))
        # Windows readers can briefly deny replacement while a status query has
        # the destination open. Retry only sharing/access errors, with a finite
        # 310 ms budget; never retry arbitrary I/O failures or regenerate bytes.
        for delay in (0.01, 0.02, 0.04, 0.08, 0.16, None):
            try:
                os.replace(tmp, path)
                break
            except OSError as error:
                if delay is None or getattr(error, "winerror", None) not in {5, 32}:
                    raise
                time.sleep(delay)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_if_changed(path: Path | str, content: str) -> bool:
    """Digest-guarded derived write: no-op (bytes AND mtime preserved) when
    identical. Returns True iff bytes were written."""
    data = content.encode("utf-8")
    try:
        if Path(path).read_bytes() == data:
            return False
    except OSError:
        pass
    write_text_atomic(path, content)
    return True


def write_json_atomic(path: Path | str, obj: Any) -> None:
    write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False))


def write_json_if_changed(path: Path | str, obj: Any) -> bool:
    return write_if_changed(path, json.dumps(obj, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Typed envelopes — durable action key; subject is display-only
# ---------------------------------------------------------------------------

def make_envelope(*, train_id: str, revision: int, required_action: str,
                  role: str) -> dict[str, Any]:
    return {"v": ENVELOPE_VERSION, "trainId": train_id, "revision": int(revision),
            "requiredAction": required_action, "role": role}


def action_key(envelope: dict[str, Any]) -> Optional[str]:
    """Stable durable key for effect deduplication; None for non-actionable."""
    if not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION:
        return None
    train, action = envelope.get("trainId"), envelope.get("requiredAction")
    if not train or not action or action == "notify":
        return None
    return f"{train}::r{envelope.get('revision', 0)}::{action}::{envelope.get('role', '')}"


def _outcome_path(key: str) -> Path:
    return OUTCOMES_DIR / (sha256_hex(key)[:32] + ".json")


def outcome_get(key: str) -> Optional[dict[str, Any]]:
    return read_json(_outcome_path(key))


def outcome_ack(key: str, note: str = "") -> None:
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_outcome_path(key), {"key": key, "state": "ACK",
                                           "at": time.time(), "note": note})


def outcome_quarantine(key: str, error: str) -> None:
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_outcome_path(key), {"key": key, "state": "QUARANTINED",
                                           "at": time.time(), "error": error[:500]})


def outcome_reset(key: str) -> None:
    p = _outcome_path(key)
    if p.exists():
        p.unlink(missing_ok=True)


def outcome_retry(key: str, error: str) -> dict[str, Any]:
    """Record a failed attempt with bounded exponential backoff. Returns the
    outcome record; caller quarantines the SOURCE when state=QUARANTINED."""
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    record = outcome_get(key) or {"key": key, "state": "RETRY", "attempts": 0}
    if record.get("state") in ("ACK", "QUARANTINED"):
        return record
    record["attempts"] = int(record.get("attempts", 0)) + 1
    record["lastError"] = error[:500]
    if record["attempts"] >= MAX_ATTEMPTS:
        record.update({"state": "QUARANTINED", "at": time.time()})
    else:
        record["state"] = "RETRY"
        record["nextRetry"] = time.time() + BACKOFF_BASE_SECONDS * (2 ** (record["attempts"] - 1))
    write_json_atomic(_outcome_path(key), record)
    return record


def outcome_actionable(key: str) -> bool:
    """True when the effect for this key may run NOW (no ACK/quarantine, and
    not inside a backoff window)."""
    record = outcome_get(key)
    if record is None:
        return True
    if record.get("state") in ("ACK", "QUARANTINED"):
        return False
    return time.time() >= float(record.get("nextRetry", 0))

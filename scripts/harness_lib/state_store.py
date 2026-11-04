"""Pluggable state-store contract for product-grade harness persistence.

The runtime currently defaults to filesystem state. This module defines the
stable contract and provides a SQLite backend so future crash-recovery work can
move from file-only state to durable local databases without changing callers.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol


class StateStore(Protocol):
    def put_json(self, key: str, value: dict[str, Any]) -> None: ...
    def get_json(self, key: str, default: Any = None) -> Any: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str = "") -> list[str]: ...


def _safe_key(key: str) -> str:
    safe = key.strip().lstrip("/")
    if not safe or ".." in Path(safe).parts:
        raise ValueError(f"unsafe state key: {key!r}")
    return safe


class FileStateStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, key: str) -> Path:
        safe = _safe_key(key)
        if not safe.endswith(".json"):
            safe += ".json"
        return self.root / safe

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    def get_json(self, key: str, default: Any = None) -> Any:
        path = self.path_for(key)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        keys: list[str] = []
        for path in sorted(base.rglob("*.json")):
            keys.append(path.relative_to(self.root).as_posix().removesuffix(".json"))
        return keys


class SQLiteStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # sqlite3's `with conn:` commits/rolls back but never closes; the leaked
    # handles kept Windows from deleting the db file (MF.2 review finding).
    # contextlib.closing + the connection's own transaction context fix both.
    def _init(self) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        safe = _safe_key(key)
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO state(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (safe, payload),
            )

    def get_json(self, key: str, default: Any = None) -> Any:
        safe = _safe_key(key)
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (safe,)).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def delete(self, key: str) -> None:
        safe = _safe_key(key)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM state WHERE key=?", (safe,))

    def list_keys(self, prefix: str = "") -> list[str]:
        with contextlib.closing(self._connect()) as conn:
            if prefix:
                rows = conn.execute("SELECT key FROM state WHERE key LIKE ? ORDER BY key", (f"{prefix}%",)).fetchall()
            else:
                rows = conn.execute("SELECT key FROM state ORDER BY key").fetchall()
        return [str(row[0]) for row in rows]

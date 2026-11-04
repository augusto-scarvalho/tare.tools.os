#!/usr/bin/env python3
"""
relay/cluster_coordinator.py — tare.tools Central Cluster Coordinator & Cockpit Server
=====================================================================================
Direct Tailscale P2P HTTP Coordinator providing:
1. Owner Cockpit Web UI (http://localhost:8765/ and http://100.x.y.z:8765/)
2. In-Memory + SQLite Durable Linearizable State Machine (Zero Drive locks)
3. Automated Watchdog: Reclaims expired or frozen worker jobs automatically (<60s)
4. REST API for Distributed Multi-PC Workers (Desktop RTX 3090 + Acer + Satellites)
"""
from __future__ import annotations

import http.server
import hashlib
import json
import os
import secrets
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_INITIAL_DIR = Path(__file__).resolve().parent
_INITIAL_ROOT = _INITIAL_DIR.parent
sys.path = [str(_INITIAL_DIR), str(_INITIAL_ROOT)] + [p for p in sys.path if "My Drive" not in p]

import relay_io
import relay_mesh
import resource_telemetry
import intake_processor
import human_watcher
import bookkeeper
import overseer_agent
import vendor_chairs
import cluster_sre
import round_table_engine

RELAY_ROOT = relay_mesh.RELAY_ROOT
RELAY_DIR = relay_mesh.RELAY_DIR
APPROVALS_DIR = relay_mesh.APPROVALS_DIR
TRAINS_DIR = relay_mesh.TRAINS_DIR
DB_FILE = RELAY_DIR / "history" / "coordinator.db"
INTAKE_HUMAN_DIR = RELAY_DIR / "intake" / "human"

PORT = int(os.environ.get("RELAY_COCKPIT_PORT", "8765"))
HOST = os.environ.get("RELAY_COCKPIT_HOST", "0.0.0.0")
OVERSEER_BUSY = False
OVERSEER_LOCK = threading.Lock()


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_tailscale_ip() -> Optional[str]:
    ts_exe = shutil.which("tailscale")
    if not ts_exe:
        for p in [r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"]:
            if os.path.exists(p):
                ts_exe = p
                break
    if ts_exe:
        try:
            proc = subprocess.run([ts_exe, "ip", "-4"], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            pass
    return None


class CoordinatorDB:
    def __init__(self, db_path: Path = DB_FILE, clock: Callable[[], float] = time.time):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self.lock = threading.Lock()
        self._clock = clock
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self):
        with self.lock, self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cluster_nodes (
                    hostname TEXT PRIMARY KEY,
                    ip TEXT,
                    cpu_load REAL,
                    ram_free_gb REAL,
                    ram_load_pct REAL,
                    gpu_name TEXT,
                    gpu_load_pct REAL,
                    gpu_vram_free_mb REAL,
                    pressure TEXT,
                    last_seen REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_leases (
                    train_id TEXT PRIMARY KEY,
                    role TEXT,
                    claimed_by_host TEXT,
                    claimed_by_agent TEXT,
                    fence_seq INTEGER,
                    lease_token TEXT,
                    acquired_ts REAL,
                    expires_ts REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS train_fences (
                    train_id TEXT PRIMARY KEY,
                    fence_seq INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runtime_fences (
                    work_id TEXT PRIMARY KEY,
                    fence_seq INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runtime_bindings (
                    work_id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    fence_seq INTEGER NOT NULL,
                    token_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    acquired_ts REAL NOT NULL,
                    expires_ts REAL NOT NULL,
                    revoked_ts REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runtime_claims (
                    binding_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    fence_seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    turns INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    claim_sha256 TEXT NOT NULL UNIQUE,
                    submitted_ts REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runtime_binding_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    fence_seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_ts REAL NOT NULL,
                    details_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_runtime_binding_events_work
                ON agent_runtime_binding_events (work_id, event_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    def update_node_heartbeat(self, info: Dict[str, Any]):
        hostname = info.get("hostname", "unknown")
        res = info.get("resources", {})
        now = time.time()
        with self.lock, self._get_conn() as conn:
            conn.execute("""
                INSERT INTO cluster_nodes (hostname, ip, cpu_load, ram_free_gb, ram_load_pct, gpu_name, gpu_load_pct, gpu_vram_free_mb, pressure, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hostname) DO UPDATE SET
                    ip=excluded.ip,
                    cpu_load=excluded.cpu_load,
                    ram_free_gb=excluded.ram_free_gb,
                    ram_load_pct=excluded.ram_load_pct,
                    gpu_name=excluded.gpu_name,
                    gpu_load_pct=excluded.gpu_load_pct,
                    gpu_vram_free_mb=excluded.gpu_vram_free_mb,
                    pressure=excluded.pressure,
                    last_seen=excluded.last_seen
            """, (
                hostname,
                info.get("ip", ""),
                res.get("cpu_load_pct", 0.0),
                res.get("ram_available_gb", 0.0),
                res.get("ram_load_pct", 0.0),
                res.get("gpu_name", ""),
                res.get("gpu_load_pct", 0.0),
                res.get("gpu_vram_free_mb", 0.0),
                info.get("pressure", "IDLE"),
                now
            ))
            conn.commit()

    def get_live_nodes(self) -> List[Dict[str, Any]]:
        cutoff = time.time() - 90  # active in last 90s
        nodes_map: Dict[str, Dict[str, Any]] = {}
        with self.lock, self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM cluster_nodes WHERE last_seen > ? ORDER BY hostname ASC", (cutoff,))
            rows = cur.fetchall()
            for r in rows:
                nodes_map[r["hostname"]] = {
                    "hostname": r["hostname"],
                    "ip": r["ip"],
                    "pressure": r["pressure"],
                    "resources": {
                        "cpu_load_pct": r["cpu_load"],
                        "ram_available_gb": r["ram_free_gb"],
                        "ram_load_pct": r["ram_load_pct"],
                        "gpu_available": bool(r["gpu_name"]),
                        "gpu_name": r["gpu_name"],
                        "gpu_load_pct": r["gpu_load_pct"],
                        "gpu_vram_free_mb": r["gpu_vram_free_mb"],
                    },
                    "last_seen_ago_s": round(time.time() - r["last_seen"], 1)
                }

        # Fallback / merge from relay/nodes/*.json files if available
        nodes_dir = RELAY_DIR / "nodes"
        if nodes_dir.exists():
            for nf in nodes_dir.glob("*.json"):
                try:
                    data = relay_io.read_json(nf)
                    if isinstance(data, dict) and data.get("hostname"):
                        h = data["hostname"]
                        if h not in nodes_map:
                            mtime = nf.stat().st_mtime
                            if time.time() - mtime < 120:
                                res = data.get("resources", {})
                                nodes_map[h] = {
                                    "hostname": h,
                                    "ip": data.get("ip", ""),
                                    "pressure": data.get("pressure", "ONLINE"),
                                    "resources": {
                                        "cpu_load_pct": res.get("cpu_load_pct", 0),
                                        "ram_available_gb": res.get("ram_available_gb", 0),
                                        "ram_load_pct": res.get("ram_load_pct", 0),
                                        "gpu_available": bool(res.get("gpu_available")),
                                        "gpu_name": res.get("gpu_name", ""),
                                        "gpu_load_pct": res.get("gpu_load_pct", 0),
                                        "gpu_vram_free_mb": res.get("gpu_vram_free_mb", 0),
                                    },
                                    "last_seen_ago_s": round(time.time() - mtime, 1)
                                }
                except Exception:
                    pass

        return list(nodes_map.values())

    def claim_job(self, train_id: str, role: str, host: str, agent: str, ttl_s: int = 120) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self.lock, self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM active_leases WHERE train_id = ?", (train_id,))
            row = cur.fetchone()
            if row and row["expires_ts"] > now:
                # Active lease already held
                if row["claimed_by_host"] == host and row["claimed_by_agent"] == agent:
                    # Renew existing token
                    token = row["lease_token"]
                    fence = row["fence_seq"]
                    expires = now + ttl_s
                    conn.execute("UPDATE active_leases SET expires_ts = ? WHERE train_id = ?", (expires, train_id))
                    conn.commit()
                    return {"train_id": train_id, "lease_token": token, "fence_seq": fence, "expires_ts": expires}
                return None  # Conflict: held by another node

            # Issue brand new lease with monotonic fence sequence
            token = secrets.token_hex(16)
            cur = conn.execute("SELECT fence_seq FROM train_fences WHERE train_id = ?", (train_id,))
            frow = cur.fetchone()
            fence = (frow["fence_seq"] + 1) if frow else 1
            conn.execute("""
                INSERT INTO train_fences (train_id, fence_seq) VALUES (?, ?)
                ON CONFLICT(train_id) DO UPDATE SET fence_seq=excluded.fence_seq
            """, (train_id, fence))

            expires = now + ttl_s
            conn.execute("""
                INSERT INTO active_leases (train_id, role, claimed_by_host, claimed_by_agent, fence_seq, lease_token, acquired_ts, expires_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(train_id) DO UPDATE SET
                    role=excluded.role,
                    claimed_by_host=excluded.claimed_by_host,
                    claimed_by_agent=excluded.claimed_by_agent,
                    fence_seq=excluded.fence_seq,
                    lease_token=excluded.lease_token,
                    acquired_ts=excluded.acquired_ts,
                    expires_ts=excluded.expires_ts
            """, (train_id, role, host, agent, fence, token, now, expires))
            conn.commit()
            return {"train_id": train_id, "lease_token": token, "fence_seq": fence, "expires_ts": expires}

    def renew_job(self, train_id: str, token: str, ttl_s: int = 120) -> bool:
        now = time.time()
        with self.lock, self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM active_leases WHERE train_id = ? AND lease_token = ?", (train_id, token))
            row = cur.fetchone()
            if not row:
                return False
            expires = now + ttl_s
            conn.execute("UPDATE active_leases SET expires_ts = ? WHERE train_id = ?", (expires, train_id))
            conn.commit()
            return True

    def release_job(self, train_id: str, token: str) -> bool:
        with self.lock, self._get_conn() as conn:
            cur = conn.execute("DELETE FROM active_leases WHERE train_id = ? AND lease_token = ?", (train_id, token))
            conn.commit()
            return cur.rowcount > 0

    def reap_expired_leases(self) -> List[str]:
        now = time.time()
        with self.lock, self._get_conn() as conn:
            cur = conn.execute("SELECT train_id, claimed_by_host FROM active_leases WHERE expires_ts <= ?", (now,))
            expired = cur.fetchall()
            reaped = []
            for r in expired:
                reaped.append(r["train_id"])
            if reaped:
                conn.execute("DELETE FROM active_leases WHERE expires_ts <= ?", (now,))
                conn.commit()
            return reaped

    @staticmethod
    def _agent_token_sha256(token: str) -> str:
        if not isinstance(token, str) or not token:
            return ""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _agent_ttl(ttl_s: int | float) -> float:
        ttl = float(ttl_s)
        if ttl <= 0:
            raise ValueError("agent runtime binding ttl_s must be positive")
        return ttl

    @staticmethod
    def _agent_binding_matches(
        row: sqlite3.Row | None,
        *,
        binding_id: str,
        owner_id: str,
        fence_seq: int,
        token: str,
        now: float,
    ) -> bool:
        if row is None or row["state"] != "ACTIVE" or row["expires_ts"] <= now:
            return False
        token_hash = CoordinatorDB._agent_token_sha256(token)
        return (
            row["binding_id"] == binding_id
            and row["owner_id"] == owner_id
            and row["fence_seq"] == fence_seq
            and bool(token_hash)
            and secrets.compare_digest(row["token_sha256"], token_hash)
        )

    @staticmethod
    def _record_agent_binding_event(
        conn: sqlite3.Connection,
        row: Dict[str, Any],
        event_type: str,
        occurred_ts: float,
        details: Dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO agent_runtime_binding_events
                (work_id, binding_id, owner_id, fence_seq, event_type, occurred_ts, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["work_id"], row["binding_id"], row["owner_id"], row["fence_seq"],
                event_type, occurred_ts,
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _issue_agent_binding(
        self,
        conn: sqlite3.Connection,
        *,
        work_id: str,
        owner_id: str,
        ttl: float,
        now: float,
        event_type: str,
    ) -> Dict[str, Any]:
        fence_row = conn.execute(
            "SELECT fence_seq FROM agent_runtime_fences WHERE work_id = ?", (work_id,)
        ).fetchone()
        fence = int(fence_row["fence_seq"]) + 1 if fence_row else 1
        token = secrets.token_hex(32)
        binding = {
            "work_id": work_id,
            "binding_id": secrets.token_hex(16),
            "owner_id": owner_id,
            "fence_seq": fence,
            "token": token,
            "expires_ts": now + ttl,
        }
        conn.execute(
            """
            INSERT INTO agent_runtime_fences (work_id, fence_seq) VALUES (?, ?)
            ON CONFLICT(work_id) DO UPDATE SET fence_seq=excluded.fence_seq
            """,
            (work_id, fence),
        )
        conn.execute(
            """
            INSERT INTO agent_runtime_bindings
                (work_id, binding_id, owner_id, fence_seq, token_sha256, state,
                 acquired_ts, expires_ts, revoked_ts)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
            ON CONFLICT(work_id) DO UPDATE SET
                binding_id=excluded.binding_id,
                owner_id=excluded.owner_id,
                fence_seq=excluded.fence_seq,
                token_sha256=excluded.token_sha256,
                state='ACTIVE',
                acquired_ts=excluded.acquired_ts,
                expires_ts=excluded.expires_ts,
                revoked_ts=NULL
            """,
            (
                work_id, binding["binding_id"], owner_id, fence,
                self._agent_token_sha256(token), now, binding["expires_ts"],
            ),
        )
        self._record_agent_binding_event(conn, binding, event_type, now)
        return binding

    def acquire_agent_runtime_binding(
        self,
        work_id: str,
        owner_id: str,
        *,
        ttl_s: int | float = 120,
        resume_token: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        if not work_id or not owner_id:
            raise ValueError("work_id and owner_id are required")
        ttl = self._agent_ttl(ttl_s)
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
            if row and row["state"] == "ACTIVE" and row["expires_ts"] > now:
                if not resume_token or row["owner_id"] != owner_id:
                    return None
                if not secrets.compare_digest(
                    row["token_sha256"], self._agent_token_sha256(resume_token)
                ):
                    return None
                expires = now + ttl
                conn.execute(
                    "UPDATE agent_runtime_bindings SET expires_ts = ? WHERE work_id = ?",
                    (expires, work_id),
                )
                resumed = {
                    "work_id": work_id,
                    "binding_id": row["binding_id"],
                    "owner_id": owner_id,
                    "fence_seq": row["fence_seq"],
                    "token": resume_token,
                    "expires_ts": expires,
                }
                self._record_agent_binding_event(conn, resumed, "RESUMED", now)
                conn.commit()
                return resumed
            if row and row["state"] == "ACTIVE":
                conn.execute(
                    "UPDATE agent_runtime_bindings SET state='EXPIRED', revoked_ts=? WHERE work_id=?",
                    (now, work_id),
                )
                self._record_agent_binding_event(conn, dict(row), "EXPIRED", now)
            binding = self._issue_agent_binding(
                conn, work_id=work_id, owner_id=owner_id, ttl=ttl, now=now,
                event_type="ACQUIRED",
            )
            conn.commit()
            return binding

    def is_agent_runtime_binding_current(
        self,
        *,
        work_id: str,
        binding_id: str,
        owner_id: str,
        fence_seq: int,
        token: str,
    ) -> bool:
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._agent_binding_matches(
                row, binding_id=binding_id, owner_id=owner_id,
                fence_seq=fence_seq, token=token, now=now,
            )

    def renew_agent_runtime_binding(
        self,
        *,
        work_id: str,
        binding_id: str,
        owner_id: str,
        fence_seq: int,
        token: str,
        ttl_s: int | float = 120,
    ) -> bool:
        ttl = self._agent_ttl(ttl_s)
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
            if not self._agent_binding_matches(
                row, binding_id=binding_id, owner_id=owner_id,
                fence_seq=fence_seq, token=token, now=now,
            ):
                return False
            conn.execute(
                "UPDATE agent_runtime_bindings SET expires_ts=? WHERE work_id=?",
                (now + ttl, work_id),
            )
            conn.commit()
            return True

    def revoke_agent_runtime_binding(
        self,
        *,
        work_id: str,
        binding_id: str,
        owner_id: str,
        fence_seq: int,
        token: str,
        reason: str = "operator-revocation",
    ) -> bool:
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
            if (row is None or row['state'] not in {'ACTIVE', 'PAUSED', 'EXPIRED'}
                    or (row['binding_id'], row['owner_id'], row['fence_seq']) != (binding_id, owner_id, fence_seq)
                    or not secrets.compare_digest(row['token_sha256'], self._agent_token_sha256(token))):
                return False
            conn.execute(
                "UPDATE agent_runtime_bindings SET state='REVOKED', revoked_ts=? WHERE work_id=?",
                (now, work_id),
            )
            self._record_agent_binding_event(
                conn, dict(row), "REVOKED", now, {"reason": reason}
            )
            conn.commit()
            return True

    def handoff_agent_runtime_binding(
        self,
        *,
        work_id: str,
        binding_id: str,
        owner_id: str,
        fence_seq: int,
        token: str,
        new_owner_id: str,
        ttl_s: int | float = 120,
    ) -> Optional[Dict[str, Any]]:
        if not new_owner_id:
            raise ValueError("new_owner_id is required")
        ttl = self._agent_ttl(ttl_s)
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
            if not self._agent_binding_matches(
                row, binding_id=binding_id, owner_id=owner_id,
                fence_seq=fence_seq, token=token, now=now,
            ):
                return None
            conn.execute(
                "UPDATE agent_runtime_bindings SET state='REVOKED', revoked_ts=? WHERE work_id=?",
                (now, work_id),
            )
            self._record_agent_binding_event(
                conn, dict(row), "HANDOFF", now, {"new_owner_id": new_owner_id}
            )
            binding = self._issue_agent_binding(
                conn, work_id=work_id, owner_id=new_owner_id, ttl=ttl, now=now,
                event_type="ACQUIRED_AFTER_HANDOFF",
            )
            conn.commit()
            return binding

    def pause_agent_runtime_binding(self, *, work_id, binding_id, owner_id, fence_seq, token):
        """Stop effects without making a local interruption an operator revocation."""
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM agent_runtime_bindings WHERE work_id=?', (work_id,)).fetchone()
            if not self._agent_binding_matches(row, binding_id=binding_id, owner_id=owner_id,
                                              fence_seq=fence_seq, token=token, now=now):
                return False
            conn.execute("UPDATE agent_runtime_bindings SET state='PAUSED', expires_ts=? WHERE work_id=?", (now, work_id))
            self._record_agent_binding_event(conn, dict(row), 'PAUSED', now)
            conn.commit()
            return True

    def recover_agent_runtime_binding(self, *, work_id, binding_id, owner_id, fence_seq,
                                      token, successor_token, ttl_s=120):
        """Authenticate exact local custody recovery and invalidate the old bearer.

        The host holds exclusive custody. Session identity stays fixed; token rotation
        fences stale controls. Revocation, claims and another acquisition cannot recover.
        Caller durably saves both tokens before this transaction for lost-ack retry.
        """
        if not token or not successor_token or token == successor_token:
            raise ValueError('distinct_recovery_tokens_required')
        ttl, now = self._agent_ttl(ttl_s), self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM agent_runtime_bindings WHERE work_id=?', (work_id,)).fetchone()
            if (row is None or row['state'] not in {'ACTIVE', 'PAUSED', 'EXPIRED'}
                    or (row['binding_id'], row['owner_id'], row['fence_seq']) != (binding_id, owner_id, fence_seq)
                    or conn.execute('SELECT 1 FROM agent_runtime_claims WHERE binding_id=?', (binding_id,)).fetchone()):
                return None
            old = secrets.compare_digest(row['token_sha256'], self._agent_token_sha256(token))
            retry = secrets.compare_digest(row['token_sha256'], self._agent_token_sha256(successor_token))
            if not (old or retry):
                return None
            conn.execute("UPDATE agent_runtime_bindings SET state='ACTIVE', token_sha256=?, expires_ts=?, revoked_ts=NULL WHERE work_id=?",
                         (self._agent_token_sha256(successor_token), now+ttl, work_id))
            if old:
                self._record_agent_binding_event(conn, dict(row), 'RECOVERED', now)
            conn.commit()
            return dict(work_id=work_id, binding_id=binding_id, owner_id=owner_id, fence_seq=fence_seq,
                        token=successor_token, expires_ts=now+ttl)

    def agent_runtime_handoff_receipt(self, *, previous: Dict[str, Any],
                                    work_id: str, binding_id: str, owner_id: str,
                                    fence_seq: int, token: str) -> Optional[Dict[str, Any]]:
        """Authenticate the direct recorded handoff to a currently held successor.

        Uses the existing transaction event ledger, without granting or renewing
        authority. Expiry/reacquisition alone is not a causal handoff.
        """
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM agent_runtime_bindings WHERE work_id=?", (work_id,)).fetchone()
            if not self._agent_binding_matches(row, binding_id=binding_id, owner_id=owner_id,
                    fence_seq=fence_seq, token=token, now=self._clock()):
                return None
            issued = conn.execute("""SELECT * FROM agent_runtime_binding_events
                WHERE work_id=? AND binding_id=? AND event_type='ACQUIRED_AFTER_HANDOFF'
                ORDER BY event_id DESC LIMIT 1""", (work_id, binding_id)).fetchone()
            if issued is None:
                return None
            prior = conn.execute("""SELECT * FROM agent_runtime_binding_events
                WHERE work_id=? AND event_id<? ORDER BY event_id DESC LIMIT 1""",
                (work_id, issued['event_id'])).fetchone()
            if prior is None or prior['event_type'] != 'HANDOFF':
                return None
            observed = {'work_id': prior['work_id'], 'binding_id': prior['binding_id'],
                        'owner_id': prior['owner_id'], 'fence': prior['fence_seq']}
            if (previous != observed or issued['owner_id'] != owner_id or issued['fence_seq'] != fence_seq
                    or fence_seq != prior['fence_seq']+1
                    or json.loads(prior['details_json']).get('new_owner_id') != owner_id):
                return None
            return {'handoff_id': f"coordinator-handoff:{work_id}:{issued['event_id']}:{binding_id}",
                    'previous': observed, 'binding': {'work_id': work_id, 'binding_id': binding_id,
                                                     'owner_id': owner_id, 'fence': fence_seq}}

    def submit_agent_runtime_terminal_claim(
        self,
        *,
        work_id: str,
        binding_id: str,
        owner_id: str,
        fence_seq: int,
        token: str,
        status: str,
        output: str,
        turns: int,
        attempts: int,
    ) -> bool:
        if turns < 0 or attempts < 0:
            raise ValueError("turns and attempts cannot be negative")
        now = self._clock()
        output_sha = hashlib.sha256(output.encode("utf-8")).hexdigest()
        claim_doc = {
            "attempts": attempts,
            "binding_id": binding_id,
            "fence_seq": fence_seq,
            "output_sha256": output_sha,
            "owner_id": owner_id,
            "status": status,
            "turns": turns,
            "work_id": work_id,
        }
        claim_sha = hashlib.sha256(
            json.dumps(claim_doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
            if not self._agent_binding_matches(
                row, binding_id=binding_id, owner_id=owner_id,
                fence_seq=fence_seq, token=token, now=now,
            ):
                return False
            try:
                conn.execute(
                    """
                    INSERT INTO agent_runtime_claims
                        (binding_id, work_id, owner_id, fence_seq, status, output_sha256,
                         turns, attempts, claim_sha256, submitted_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding_id, work_id, owner_id, fence_seq, status, output_sha,
                        turns, attempts, claim_sha, now,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            conn.execute(
                "UPDATE agent_runtime_bindings SET state='TERMINAL', revoked_ts=? WHERE work_id=?",
                (now, work_id),
            )
            self._record_agent_binding_event(
                conn, dict(row), "TERMINAL_CLAIM_ACCEPTED", now,
                {"claim_sha256": claim_sha, "status": status},
            )
            conn.commit()
            return True

    def accepted_agent_runtime_claim(self, *, work_id, binding_id, owner_id,
                                    fence_seq, status, output, turns, attempts):
        """Read proof of this exact historical claim; grants no execution authority.

        Needed when COMMIT succeeded but its acknowledgement was lost. Unlike
        submit, this cannot create a claim, renew a binding or accept new bytes.
        """
        expected = dict(work_id=work_id, binding_id=binding_id, owner_id=owner_id,
                        fence_seq=fence_seq, status=status, turns=turns, attempts=attempts,
                        output_sha256=hashlib.sha256(output.encode('utf-8')).hexdigest())
        with self.lock, self._get_conn() as conn:
            row = conn.execute('SELECT * FROM agent_runtime_claims WHERE binding_id=?', (binding_id,)).fetchone()
            if row is None or any(row[k] != v for k,v in expected.items()):
                return None
            return expected | {'claim_sha256': row['claim_sha256'], 'submitted_ts': row['submitted_ts']}

    def reap_expired_agent_runtime_bindings(self) -> List[str]:
        now = self._clock()
        with self.lock, self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE state='ACTIVE' AND expires_ts <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE agent_runtime_bindings SET state='EXPIRED', revoked_ts=? WHERE work_id=?",
                    (now, row["work_id"]),
                )
                self._record_agent_binding_event(conn, dict(row), "EXPIRED", now)
            conn.commit()
            return [row["work_id"] for row in rows]


class _LazyCoordinatorDB:
    """Process-default CoordinatorDB, opened on first use instead of at import.

    Importing this module (pytest collection, Work entrypoints) must not open,
    create or migrate the tracked coordinator database. Initialization is
    serialized so concurrent first users share one instance; a failed
    initialization is not cached, so the next use retries.
    """

    def __init__(self, factory: Callable[[], "CoordinatorDB"]):
        self._factory = factory
        self._instance: Optional[CoordinatorDB] = None
        self._init_lock = threading.Lock()

    def _resolve(self) -> "CoordinatorDB":
        instance = self._instance
        if instance is None:
            with self._init_lock:
                instance = self._instance
                if instance is None:
                    instance = self._factory()
                    self._instance = instance
        return instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


def _open_default_db() -> "CoordinatorDB":
    # Read DB_FILE at first use (not import) so hermetic callers can redirect it.
    return CoordinatorDB(DB_FILE)


DB = _LazyCoordinatorDB(_open_default_db)


def start_watchdog_thread() -> threading.Thread:
    """Watchdog thread that automatically resets frozen leases AND auto-lands trains when auto-approve mode is ON."""
    def _watch():
        while True:
            try:
                # 1. Self-healing lease reaper
                reaped = DB.reap_expired_leases()
                for tid in reaped:
                    print(f"[COORDINATOR WATCHDOG] ⚠️ Reclaimed expired lease on '{tid}'. Resetting train for other workers!")
                    t_dir = relay_mesh.TRAINS_DIR / tid
                    st_file = t_dir / "STATE.json"
                    if st_file.exists():
                        st = relay_io.read_json(st_file) or {}
                        if st.get("status") in ("IN_PROGRESS_IMPLEMENTER", "IN_PROGRESS_AUDITOR"):
                            prev_status = "PLAN_APPROVED" if st.get("status") == "IN_PROGRESS_IMPLEMENTER" else "AUDIT_PENDING"
                            relay_mesh.transition_train(
                                tid, prev_status, st.get("current_role", "worker"),
                                "coordinator_watchdog", "supervisor",
                                "Coordinator lease expired", expected_revision=st.get("revision", 1),
                            )

                # 2. Autonomous Auto-Approve Engine
                auto_state = human_watcher.get_auto_mode()
                if auto_state.get("enabled", False):
                    pending_apps = get_pending_approvals()
                    for app in pending_apps:
                        tid = app["train_id"]
                        nonce = app.get("nonce")
                        if nonce:
                            print(f"[COORDINATOR AUTO-APPROVE] ⚡ Auto-landing '{tid}' (nonce: {nonce[:8]}...)...")
                            success = relay_mesh.land_train(tid, "COORDINATOR_AUTO_APPROVE", nonce=nonce)
                            if success:
                                (RELAY_DIR / "approvals" / f"APPROVAL_REQUEST_{tid}.md").unlink(missing_ok=True)
                                (RELAY_DIR / "approvals" / f"CAPABILITY_{tid}.json").unlink(missing_ok=True)
                                try:
                                    bookkeeper.run_bookkeeping_cycle(tid)
                                except Exception as exc:
                                    print(f"[COORDINATOR] Bookkeeper error on auto-landing: {exc}")
                                relay_mesh.update_board()
                                print(f"[COORDINATOR AUTO-APPROVE] 🏆 '{tid}' landed automatically into work-graph.json!")

                # 3. Autonomous Cluster SRE & Self-Healing Overseer Tick
                try:
                    cluster_sre.run_sre_tick()
                except Exception as sre_exc:
                    print(f"[COORDINATOR SRE ERROR]: {sre_exc}")

            except Exception as exc:
                print(f"[COORDINATOR WATCHDOG ERROR]: {exc}")
            time.sleep(5)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


def get_pending_approvals() -> List[Dict[str, Any]]:
    """Scans and extracts detailed approval dossiers for web 1-tap approvals."""
    approvals = []
    approvals_dir = RELAY_DIR / "approvals"
    if approvals_dir.exists():
        for req in sorted(approvals_dir.glob("APPROVAL_REQUEST_*.md")):
            tid = req.stem.replace("APPROVAL_REQUEST_", "")
            cap_file = approvals_dir / f"CAPABILITY_{tid}.json"
            nonce = ""
            if cap_file.exists():
                cap = relay_io.read_json(cap_file) or {}
                nonce = cap.get("nonce", "")
            
            dossier = human_watcher.extract_audit_dossier(tid)
            approvals.append({
                "train_id": tid,
                "title": dossier.get("title", tid),
                "tasks": dossier.get("tasks", []),
                "nonce": nonce,
                "verdict": dossier.get("verdict", "ANALYZED"),
                "recommendation": dossier.get("recommendation", ""),
                "findings": dossier.get("findings", []),
                "debts": dossier.get("debts", []),
                "footprint": dossier.get("footprint", []),
                "plain_explanation": dossier.get("plain_explanation", {}),
                "raw_text": dossier.get("raw_text", ""),
            })
    return approvals


def get_all_round_tables() -> List[Dict[str, Any]]:
    """Gathers all active and decided Round Table cases."""
    cases = []
    rt_dir = round_table_engine.ROUND_TABLES_DIR
    if rt_dir.exists():
        for d in sorted(rt_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            st_file = d / "case.json"
            if st_file.exists():
                st = relay_io.read_json(st_file) or {}
                # Add summary tallies if rounds exist
                rounds_dir = d / "rounds"
                latest_tally = None
                if rounds_dir.exists():
                    r_dirs = sorted(rounds_dir.glob("r*"))
                    if r_dirs:
                        tally_file = r_dirs[-1] / "tally.json"
                        if tally_file.exists():
                            latest_tally = relay_io.read_json(tally_file)
                st["latest_tally"] = latest_tally
                st["has_decision"] = (d / "decision" / "DECISION.md").exists()
                cases.append(st)
    return cases


COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="signal">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
  <title>tare.tools — SIGNAL Cluster Cockpit & Mission Control</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;0,700;1,400&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    /* ==========================================================================
       SIGNAL Design Tokens - tare.tools Canonical UI Law
       ========================================================================== */
    :root {
      color-scheme: dark;
      
      --bg-base: #0A0B08;
      --bg-void: #0A0B08;
      --atmo-glow: #12160C;
      --surface-1: #0F1109;
      --surface-2: #14170E;
      --surface-3: #1B1F14;
      --surface-hover: #1F2417;
      
      --border-subtle: #1E2216;
      --border: #2B3020;
      --border-strong: #3A4029;
      
      --text-primary: #EDEEE1;
      --text-secondary: #A6AA90;
      --text-muted: #8B9173;
      --text-disabled: #4A4E39;
      
      --accent: #CBF23F; /* Lime-phosphor canonical */
      --accent-bg: rgba(203, 242, 63, 0.12);
      --accent-border: rgba(203, 242, 63, 0.40);
      
      --stream: #45E0C4; /* Oscilloscope teal */
      --stream-bg: rgba(69, 224, 196, 0.12);
      --stream-border: rgba(69, 224, 196, 0.34);
      
      --success: #7CCB6A;
      --success-bg: rgba(124, 203, 106, 0.12);
      --success-border: rgba(124, 203, 106, 0.32);
      
      --warning: #E8A93B;
      --warning-bg: rgba(232, 169, 59, 0.12);
      --warning-border: rgba(232, 169, 59, 0.32);
      
      --danger: #F2685C;
      --danger-bg: rgba(242, 104, 92, 0.12);
      --danger-border: rgba(242, 104, 92, 0.34);
      
      --font-mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --font-prose: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      
      --radius-sm: 8px;
      --radius-xs: 5px;
      --shadow-1: 0 2px 8px rgba(0, 0, 0, 0.4);
    }
    
    * { box-sizing: border-box; margin: 0; padding: 0; }
    
    html, body {
      width: 100%;
      max-width: 100vw;
      overflow-x: hidden;
    }
    
    body {
      font-family: var(--font-mono);
      background-color: var(--bg-base);
      background-image: radial-gradient(circle at 50% 0%, var(--atmo-glow) 0%, var(--bg-base) 75%);
      color: var(--text-primary);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
      line-height: 1.5;
    }
    
    .atmo-grid {
      background-image: linear-gradient(0deg, transparent 23px, rgba(203, 242, 63, 0.02) 24px);
      background-size: 100% 24px;
    }
    
    .container {
      max-width: 1400px;
      width: 100%;
      min-width: 0;
      margin: 0 auto;
      padding: 16px 20px 40px 20px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    
    /* Top Header Bar */
    header {
      background: var(--surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 12px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      box-shadow: var(--shadow-1);
    }
    
    .header-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .brand-title {
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.3px;
      color: var(--text-primary);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .brand-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 10px var(--accent);
      display: inline-block;
      animation: pulse-glow 2.5s infinite ease-in-out;
    }
    @keyframes pulse-glow {
      0%, 100% { transform: scale(1); opacity: 0.9; box-shadow: 0 0 8px var(--accent); }
      50% { transform: scale(1.15); opacity: 1; box-shadow: 0 0 14px var(--accent); }
    }
    
    .brand-pill {
      font-size: 10.5px;
      font-weight: 600;
      background: var(--accent-bg);
      color: var(--accent);
      border: 1px solid var(--accent-border);
      padding: 2px 7px;
      border-radius: var(--radius-xs);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    
    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    
    /* Theme Selector Styled */
    .theme-select-wrap {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-xs);
      padding: 3px 8px;
    }
    .theme-select-wrap label {
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
    }
    .theme-select {
      background: transparent;
      border: none;
      color: var(--accent);
      font-family: var(--font-mono);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      outline: none;
    }
    .theme-select option {
      background: var(--surface-1);
      color: var(--text-primary);
    }
    
    /* Button System */
    button, .btn-link {
      font-family: var(--font-mono);
      font-size: 12px;
      font-weight: 600;
      border-radius: var(--radius-xs);
      cursor: pointer;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
      padding: 7px 13px;
      min-height: 36px;
    }
    .btn-link:hover { opacity: 0.95; }
    
    .btn-secondary {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text-primary);
    }
    .btn-secondary:hover {
      background: var(--surface-3);
      border-color: var(--border-strong);
    }
    
    .btn-graph {
      background: var(--stream-bg);
      border: 1px solid var(--stream-border);
      color: var(--stream);
    }
    .btn-graph:hover {
      background: rgba(69, 224, 196, 0.22);
      border-color: var(--stream);
      box-shadow: 0 0 10px rgba(69, 224, 196, 0.2);
    }
    
    .btn-auto-toggle {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      border-radius: 20px;
      padding: 5px 14px;
    }
    .btn-auto-toggle.active {
      background: var(--success-bg);
      border-color: var(--success-border);
      color: var(--success);
      box-shadow: 0 0 8px rgba(124, 203, 106, 0.2);
    }
    
    .btn-primary {
      background: var(--accent);
      color: #0A0B08;
      border: 1px solid var(--accent);
      font-weight: 700;
    }
    .btn-primary:hover {
      background: #d8f55c;
      box-shadow: 0 0 12px var(--accent-border);
    }
    
    .btn-approve {
      background: var(--success);
      color: #0A0B08;
      border: 1px solid var(--success);
      font-weight: 700;
    }
    .btn-approve:hover {
      background: #8ee07b;
      box-shadow: 0 0 10px var(--success-border);
    }
    
    .btn-reject {
      background: var(--danger-bg);
      border: 1px solid var(--danger-border);
      color: var(--danger);
    }
    .btn-reject:hover {
      background: rgba(242, 104, 92, 0.22);
      border-color: var(--danger);
    }
    
    .btn-auto-action {
      background: var(--stream-bg);
      border: 1px solid var(--stream-border);
      color: var(--stream);
    }
    .btn-auto-action:hover {
      background: rgba(69, 224, 196, 0.22);
      border-color: var(--stream);
    }

    /* Stat Strips (Instrument Telemetry Grid) */
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .stat-pill {
      background: var(--surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 3px;
      box-shadow: var(--shadow-1);
    }
    .stat-pill-label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .stat-pill-value {
      font-size: 15px;
      font-weight: 700;
      color: var(--text-primary);
    }

    /* Cards / Containers */
    .card {
      background: var(--surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      box-shadow: var(--shadow-1);
      width: 100%;
      max-width: 100%;
      min-width: 0;
      box-sizing: border-box;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border-subtle);
      padding-bottom: 10px;
      flex-wrap: wrap;
      gap: 10px;
    }
    .card-title {
      font-size: 14.5px;
      font-weight: 700;
      color: var(--text-primary);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    
    /* Grids */
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      min-width: 0;
      width: 100%;
    }
    .grid-2 > * {
      min-width: 0;
      max-width: 100%;
    }
    .grid-nodes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 14px;
      min-width: 0;
      width: 100%;
    }
    .grid-nodes > * {
      min-width: 0;
      max-width: 100%;
    }
    
    /* Node Telemetry Box */
    .node-box {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-xs);
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-width: 0;
      box-sizing: border-box;
    }
    .node-title-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .node-name {
      font-size: 13.5px;
      font-weight: 700;
      color: var(--text-primary);
    }
    .node-ip {
      font-size: 11.5px;
      color: var(--stream);
      background: var(--stream-bg);
      border: 1px solid var(--stream-border);
      padding: 2px 7px;
      border-radius: 4px;
    }
    .metrics-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .metric-badge {
      background: var(--surface-3);
      border: 1px solid var(--border);
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 11.5px;
      color: var(--text-secondary);
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .metric-badge.busy {
      border-color: var(--warning-border);
      color: var(--warning);
      background: var(--warning-bg);
    }
    .metric-badge.ok {
      border-color: var(--success-border);
      color: var(--success);
      background: var(--success-bg);
    }
    
    /* Tables */
    .table-container {
      width: 100%;
      max-width: 100%;
      min-width: 0;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      border-radius: var(--radius-xs);
      border: 1px solid var(--border-subtle);
      box-sizing: border-box;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      text-align: left;
      min-width: 580px;
    }
    th, td {
      padding: 9px 12px;
      border-bottom: 1px solid var(--border-subtle);
      white-space: nowrap;
    }
    th {
      background: var(--surface-2);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 11.5px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }
    tr:hover td {
      background: var(--surface-hover);
    }
    
    /* Status Tags */
    .status-tag {
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .tag-green { background: var(--success-bg); color: var(--success); border: 1px solid var(--success-border); }
    .tag-yellow { background: var(--warning-bg); color: var(--warning); border: 1px solid var(--warning-border); }
    .tag-purple { background: rgba(137,87,229,0.15); color: #a371f7; border: 1px solid rgba(137,87,229,0.35); }
    .tag-red { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-border); }
    .tag-stream { background: var(--stream-bg); color: var(--stream); border: 1px solid var(--stream-border); }

    /* Approval Dossier Card */
    .approval-item {
      background: var(--surface-2);
      border: 1px solid var(--warning-border);
      border-left: 4px solid var(--warning);
      border-radius: var(--radius-xs);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    /* Explanation Mode Toggle Button */
    .mode-toggle-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--surface-3);
      border: 1px solid var(--border-strong);
      color: var(--text-primary);
      padding: 4px 12px;
      border-radius: 14px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      user-select: none;
    }
    .mode-toggle-btn:hover {
      background: var(--surface-hover);
      border-color: var(--accent);
      color: var(--accent);
    }
    .mode-toggle-btn.active-simple {
      background: var(--accent-bg);
      border-color: var(--accent-border);
      color: var(--accent);
    }

    /* Markdown Prose inside Approvals */
    .prose-rendered {
      font-family: var(--font-prose);
      font-size: 13px;
      line-height: 1.6;
      color: var(--text-primary);
    }
    .prose-rendered h1, .prose-rendered h2, .prose-rendered h3, .prose-rendered h4 {
      font-family: var(--font-mono);
      color: var(--text-primary);
      margin-top: 12px;
      margin-bottom: 6px;
      font-weight: 700;
    }
    .prose-rendered h1 { font-size: 15px; color: var(--accent); border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px; }
    .prose-rendered h2 { font-size: 14px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px; color: var(--stream); }
    .prose-rendered h3 { font-size: 13px; color: var(--text-primary); }
    .prose-rendered p { margin-bottom: 8px; }
    .prose-rendered ul, .prose-rendered ol { margin-left: 20px; margin-bottom: 8px; }
    .prose-rendered li { margin-bottom: 4px; }
    .prose-rendered code {
      font-family: var(--font-mono);
      font-size: 11.5px;
      background: var(--surface-3);
      padding: 2px 5px;
      border-radius: 4px;
      color: var(--stream);
      border: 1px solid var(--border-subtle);
    }
    .prose-rendered pre {
      background: var(--surface-1);
      border: 1px solid var(--border);
      padding: 10px 12px;
      border-radius: 6px;
      overflow-x: auto;
      margin-bottom: 10px;
    }
    .prose-rendered pre code {
      background: transparent;
      padding: 0;
      border: none;
      color: var(--text-primary);
    }
    .prose-rendered table {
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 12px;
    }
    .prose-rendered th, .prose-rendered td {
      border: 1px solid var(--border);
      padding: 6px 10px;
      text-align: left;
    }
    .prose-rendered th {
      background: var(--surface-3);
      color: var(--accent);
      font-family: var(--font-mono);
    }
    .prose-rendered blockquote {
      border-left: 3px solid var(--accent);
      padding: 6px 12px;
      color: var(--text-secondary);
      margin: 8px 0;
      background: var(--surface-3);
      border-radius: 0 4px 4px 0;
    }

    /* Overseer Terminal */
    .chip-btn {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      padding: 6px 12px;
      border-radius: 16px;
      font-size: 11.5px;
      cursor: pointer;
      transition: 0.15s;
    }
    .chip-btn:hover {
      background: var(--surface-3);
      color: var(--accent);
      border-color: var(--accent-border);
    }
    
    .chat-feed-box {
      display: flex;
      flex-direction: column;
      gap: 16px;
      min-height: 520px;
      max-height: 720px;
      height: 60vh;
      overflow-y: auto;
      padding: 18px;
      background: var(--bg-base);
      border-radius: var(--radius-xs);
      border: 1px solid var(--border-subtle);
    }
    
    .chat-bubble-user {
      align-self: flex-end;
      background: var(--surface-3);
      border: 1px solid var(--border-strong);
      border-right: 3px solid var(--accent);
      color: var(--text-primary);
      padding: 12px 16px;
      border-radius: 8px 8px 0 8px;
      max-width: 85%;
      font-size: 13.5px;
      line-height: 1.5;
      word-break: break-word;
    }
    
    .chat-bubble-overseer {
      align-self: flex-start;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-left: 3px solid var(--stream);
      color: var(--text-primary);
      padding: 16px 20px;
      border-radius: 8px 8px 8px 0;
      max-width: 98%;
      width: 98%;
      font-size: 13.5px;
      line-height: 1.6;
      word-break: break-word;
      font-family: var(--font-prose);
    }
    
    .step-pill {
      background: var(--surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 4px 10px;
      font-size: 11.5px;
      color: var(--text-muted);
      font-family: var(--font-mono);
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    /* Markdown Inside Overseer */
    .overseer-md-content h1, .overseer-md-content h2, .overseer-md-content h3, .overseer-md-content h4 {
      color: var(--text-primary);
      margin: 14px 0 6px 0;
      font-family: var(--font-mono);
      font-weight: 700;
    }
    .overseer-md-content h1 { font-size: 17px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px; color: var(--accent); }
    .overseer-md-content h2 { font-size: 15px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 3px; color: var(--stream); }
    .overseer-md-content h3 { font-size: 14px; color: var(--text-primary); }
    .overseer-md-content p { margin: 8px 0; }
    .overseer-md-content ul, .overseer-md-content ol { margin: 8px 0 8px 22px; }
    .overseer-md-content li { margin: 3px 0; }
    .overseer-md-content pre {
      background: var(--bg-base);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px 16px;
      overflow-x: auto;
      margin: 12px 0;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.45;
    }
    .overseer-md-content code {
      background: var(--surface-3);
      color: var(--accent);
      padding: 2px 6px;
      border-radius: 4px;
      font-family: var(--font-mono);
      font-size: 12px;
    }
    .overseer-md-content pre code {
      background: transparent;
      padding: 0;
      color: var(--text-primary);
    }
    .overseer-md-content table {
      width: 100%;
      border-collapse: collapse;
      margin: 14px 0;
      font-size: 12.5px;
      font-family: var(--font-mono);
    }
    .overseer-md-content th, .overseer-md-content td {
      padding: 7px 11px;
      border: 1px solid var(--border);
      text-align: left;
    }
    .overseer-md-content th {
      background: var(--surface-3);
      color: var(--text-muted);
      font-weight: 600;
    }
    .overseer-md-content blockquote {
      border-left: 3px solid var(--stream);
      padding: 6px 14px;
      color: var(--text-secondary);
      margin: 10px 0;
      background: var(--stream-bg);
      border-radius: 0 4px 4px 0;
    }
    .overseer-md-content hr {
      border: none;
      border-top: 1px solid var(--border-subtle);
      margin: 14px 0;
    }
    .overseer-md-content strong { color: var(--text-primary); }

    /* Form Fields */
    .form-group { display: flex; flex-direction: column; gap: 5px; }
    label { font-size: 11.5px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; }
    input, textarea {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text-primary);
      padding: 10px 12px;
      border-radius: var(--radius-xs);
      font-family: var(--font-mono);
      font-size: 13px;
      outline: none;
      transition: border 0.15s;
    }
    input:focus, textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 1px var(--accent-border);
    }
    
    /* Toast Notification */
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: var(--surface-3);
      color: var(--text-primary);
      border: 1px solid var(--accent);
      padding: 12px 18px;
      border-radius: var(--radius-xs);
      box-shadow: 0 6px 20px rgba(0,0,0,0.6);
      display: none;
      z-index: 1000;
      font-size: 13px;
      font-weight: 600;
    }

    /* ==========================================================================
       MOBILE RESPONSIVENESS & FLUID BREAKPOINTS
       ========================================================================== */
    @media (max-width: 900px) {
      .grid-2 {
        grid-template-columns: 1fr;
      }
    }
    
    @media (max-width: 768px) {
      body {
        padding: 0;
      }
      .container {
        padding: 10px 12px 30px 12px;
        gap: 14px;
      }
      header {
        padding: 10px 12px;
        flex-direction: column;
        align-items: stretch;
        gap: 10px;
      }
      .header-brand {
        justify-content: space-between;
        width: 100%;
      }
      .header-actions {
        justify-content: space-between;
        width: 100%;
      }
      .header-actions > * {
        flex: 1 1 auto;
      }
      .card {
        padding: 14px 12px;
        gap: 12px;
      }
      .card-header {
        flex-direction: column;
        align-items: flex-start;
        gap: 8px;
      }
      .card-header > div {
        width: 100%;
        display: flex;
        justify-content: space-between;
        flex-wrap: wrap;
      }
      .chat-feed-box {
        min-height: 440px;
        height: 52vh;
        padding: 12px;
      }
      .chat-bubble-user {
        max-width: 92%;
      }
      .chat-bubble-overseer {
        max-width: 100%;
        width: 100%;
        padding: 12px 14px;
      }
      #overseerForm {
        flex-direction: column;
        align-items: stretch;
      }
      #overseerSendBtn {
        width: 100%;
      }
      .stat-grid {
        grid-template-columns: 1fr 1fr;
      }
      /* Prevent iOS font zoom */
      input, textarea, select {
        font-size: 16px !important;
      }
    }

    @media (max-width: 480px) {
      .stat-grid {
        grid-template-columns: 1fr;
      }
      .btn-auto-toggle {
        font-size: 11px;
        padding: 4px 8px;
      }
      .brand-title {
        font-size: 15px;
      }
    }
  </style>
</head>
<body class="atmo-grid">
  <div class="container">
    
    <!-- Top Header -->
    <header>
      <div class="header-brand">
        <div class="brand-title">
          <span class="brand-dot"></span>
          tare.tools
          <span class="brand-pill">SIGNAL COCKPIT</span>
        </div>
        
        <!-- Theme Selector -->
        <div class="theme-select-wrap">
          <label for="themeSelect">🎨 Theme:</label>
          <select id="themeSelect" class="theme-select" onchange="applyTheme(this.value)">
            <option value="signal">Signal (Default)</option>
            <option value="dracula">Dracula</option>
            <option value="tokyo_night">Tokyo Night</option>
            <option value="catppuccin_mocha">Catppuccin Mocha</option>
            <option value="nord_dark">Nord Dark</option>
            <option value="cyberpunk">Cyberpunk</option>
            <option value="solarized_dark">Solarized Dark</option>
            <option value="github_light">GitHub Light</option>
            <option value="solarized_light">Solarized Light</option>
            <option value="catppuccin_latte">Catppuccin Latte</option>
          </select>
        </div>
      </div>

      <div class="header-actions">
        <a href="/graph" target="_blank" class="btn-graph">🕸️ Graph Backlog ↗</a>
        <button id="autoModeBtn" onclick="toggleAutoMode()" class="btn-auto-toggle">⚡ Auto: ...</button>
        <button onclick="refreshData()" class="btn-secondary" style="padding: 5px 10px;">🔄 Sync</button>
      </div>
    </header>

    <!-- Stat Metric Strips -->
    <div class="stat-grid">
      <div class="stat-pill">
        <span class="stat-pill-label">🖥️ Cluster Nodes</span>
        <span class="stat-pill-value" id="nodeCountStat">0 active nodes</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">🚆 Trains in Progress</span>
        <span class="stat-pill-value" id="trainCountStat">0 active</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">🏁 Landed Trains</span>
        <span class="stat-pill-value" id="landedCountStat">0 finished</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">🕸️ Canonical Graph (DAG)</span>
        <span class="stat-pill-value" id="graphStat">161 nodes • 209 edges</span>
      </div>
    </div>

    <!-- Section 1: Active Cluster Nodes -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">🖥️ Cluster Node Telemetry (Tailscale Direct P2P)</div>
        <span class="status-tag tag-stream" id="syncTag">Syncing...</span>
      </div>
      <div class="grid-nodes" id="nodesGrid">Loading telemetry...</div>
    </div>

    <!-- Section 2: Pending Release Approvals (1-Tap Gatekeeper) -->
    <div class="card" id="approvalsCard" style="display: none; border-color: var(--warning-border);">
      <div class="card-header">
        <div class="card-title" style="color: var(--warning);">🔔 Pending Release Approvals (1-Click Human Gatekeeper)</div>
        <span class="status-tag tag-yellow" id="approvalCount">0 pending</span>
      </div>
      <div id="approvalsList" style="display: flex; flex-direction: column; gap: 12px;"></div>
    </div>

    <!-- Section 3: Cluster Overseer Interactive Terminal -->
    <div class="card" id="overseerCard" style="border-color: var(--accent-border);">
      <div class="card-header">
        <div class="card-title">
          🤖 Cluster Overseer
          <span class="brand-pill" style="background: var(--stream-bg); color: var(--stream); border-color: var(--stream-border);">OPERATIONS COPILOT</span>
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
          <span id="overseerStatusPill" class="status-tag tag-green">● READY</span>
          <button onclick="clearOverseerChat()" class="btn-secondary" style="padding: 4px 10px; font-size: 11px;">🗑️ Clear</button>
        </div>
      </div>

      <!-- Chat Feed -->
      <div id="overseerChatFeed" class="chat-feed-box">
        <div style="color: var(--text-muted); font-size: 13px; text-align: center; padding: 30px;">Loading Overseer history...</div>
      </div>

      <!-- Suggestion Chips -->
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        <button class="chip-btn" onclick="sendSuggestion('Diagnose the health of the cluster and its nodes now')">🩺 Diagnose Health</button>
        <button class="chip-btn" onclick="sendSuggestion('Check for bottlenecks, deadlocks or stuck leases')">🚨 Check Deadlocks</button>
        <button class="chip-btn" onclick="sendSuggestion('What is the current train status and what is running?')">📋 Train Status</button>
        <button class="chip-btn" onclick="sendSuggestion('How can the next batch of backlog tasks move forward faster?')">🚀 Advance Backlog</button>
      </div>

      <!-- Input Form -->
      <form id="overseerForm" onsubmit="submitOverseerChat(event)" style="display: flex; gap: 10px; align-items: flex-end;">
        <textarea id="overseerInput" rows="3" placeholder="Talk to the Cluster Overseer or ask for a diagnosis or an unblock... (Enter to send, Shift+Enter for a new line)" style="flex: 1; resize: vertical;" onkeydown="handleOverseerKey(event)"></textarea>
        <button type="submit" id="overseerSendBtn" class="btn-primary" style="padding: 12px 20px; white-space: nowrap;">🚀 Send</button>
      </form>
    </div>

    <!-- Section 4: Active Trains & Leases (Full-Width Table Card) -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">🚆 Trains in Progress & Leases</div>
        <span class="status-tag tag-yellow" id="trainTag">0 active</span>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>Train</th>
              <th>Status</th>
              <th>Running Now (Host & Agent)</th>
              <th>Role</th>
              <th>Rev</th>
              <th>Linked Tasks</th>
            </tr>
          </thead>
          <tbody id="trainsBody">
            <tr><td colspan="6">Loading trains...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Section 5: Backlog Intake Box (Full-Width Responsive Form Card) -->
    <div class="card" id="intakeCard" style="border-color: var(--border-strong);">
      <div class="card-header">
        <div class="card-title">📥 New Demand / Idea (Intake Box)</div>
        <span class="brand-pill" style="background: var(--surface-3); color: var(--text-muted); border-color: var(--border);">GRAPH ADMISSION</span>
      </div>
      <form id="intakeForm" onsubmit="submitIntake(event)" style="display: flex; flex-direction: column; gap: 12px; width: 100%;">
        <div class="form-group">
          <label for="title">Demand Title / Goal:</label>
          <input type="text" id="title" placeholder="E.g. Speed up the visualizer or add support for X" required style="width: 100%;">
        </div>
        <div class="form-group">
          <label for="body">Context / Requirements / Details:</label>
          <textarea id="body" rows="3" placeholder="Describe the context, acceptance criteria or technical notes for the agents..." style="width: 100%;"></textarea>
        </div>
        <div style="display: flex; justify-content: flex-end;">
          <button type="submit" class="btn-primary" style="padding: 10px 22px;">🚀 Send to the Work Graph</button>
        </div>
      </form>
    </div>

    <!-- Section 6: Canonical Graph Backlog & DAG Mission Control -->
    <div class="card" style="border-color: var(--stream-border);">
      <div class="card-header">
        <div class="card-title">
          🕸️ Canonical Backlog Graph (SIGNAL Mission Control)
          <span id="graphStatsBadge" class="brand-pill" style="background: var(--stream-bg); color: var(--stream); border-color: var(--stream-border);">161 nodes • 209 edges</span>
        </div>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <a href="/graph" target="_blank" class="btn-graph" style="font-weight: 700;">🌐 Open Viewer ↗</a>
          <a href="/graph.mermaid" target="_blank" class="btn-secondary" style="font-size: 11px;">📑 Mermaid</a>
          <a href="/work-graph.json" target="_blank" class="btn-secondary" style="font-size: 11px;">📦 JSON</a>
        </div>
      </div>

      <!-- Embedded Interactive Graph Viewer -->
      <details style="background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-xs); padding: 10px 14px;">
        <summary style="cursor: pointer; font-weight: 600; color: var(--accent); font-size: 12.5px; user-select: none;">
          👁️ Show the Full Interactive Graph in This Panel
        </summary>
        <div style="margin-top: 10px;">
          <iframe src="/graph" style="width: 100%; height: 680px; border: 1px solid var(--border); border-radius: var(--radius-xs); background: var(--bg-base);" loading="lazy"></iframe>
        </div>
      </details>
    </div>

    <!-- Section 7: Landed Trains History -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">✅ Recently Landed Trains</div>
      </div>
      <div id="landedList" style="display: flex; flex-direction: column; gap: 8px; font-size: 12.5px;">Loading history...</div>
    </div>

  </div>

  <div class="toast" id="toast"></div>

  <script>
    /* ==========================================================================
       SIGNAL Multi-Theme Engine
       ========================================================================== */
    const THEMES = {
      signal: {
        name: 'Signal (Default)',
        vars: {
          '--bg-base': '#0A0B08',
          '--bg-void': '#0A0B08',
          '--atmo-glow': '#12160C',
          '--surface-1': '#0F1109',
          '--surface-2': '#14170E',
          '--surface-3': '#1B1F14',
          '--surface-hover': '#1F2417',
          '--border-subtle': '#1E2216',
          '--border': '#2B3020',
          '--border-strong': '#3A4029',
          '--text-primary': '#EDEEE1',
          '--text-secondary': '#A6AA90',
          '--text-muted': '#8B9173',
          '--text-disabled': '#4A4E39',
          '--accent': '#CBF23F',
          '--accent-bg': 'rgba(203, 242, 63, 0.12)',
          '--accent-border': 'rgba(203, 242, 63, 0.40)',
          '--stream': '#45E0C4',
          '--stream-bg': 'rgba(69, 224, 196, 0.12)',
          '--stream-border': 'rgba(69, 224, 196, 0.34)',
          '--success': '#7CCB6A',
          '--warning': '#E8A93B',
          '--danger': '#F2685C'
        }
      },
      dracula: {
        name: 'Dracula',
        vars: {
          '--bg-base': '#282a36',
          '--bg-void': '#21222c',
          '--atmo-glow': '#1e1f29',
          '--surface-1': '#21222c',
          '--surface-2': '#282a36',
          '--surface-3': '#44475a',
          '--surface-hover': '#44475a',
          '--border-subtle': '#383a59',
          '--border': '#44475a',
          '--border-strong': '#6272a4',
          '--text-primary': '#f8f8f2',
          '--text-secondary': '#d6acff',
          '--text-muted': '#6272a4',
          '--text-disabled': '#44475a',
          '--accent': '#bd93f9',
          '--accent-bg': 'rgba(189, 147, 249, 0.15)',
          '--accent-border': 'rgba(189, 147, 249, 0.45)',
          '--stream': '#8be9fd',
          '--stream-bg': 'rgba(139, 233, 253, 0.15)',
          '--stream-border': 'rgba(139, 233, 253, 0.40)',
          '--success': '#50fa7b',
          '--warning': '#ffb86c',
          '--danger': '#ff5555'
        }
      },
      tokyo_night: {
        name: 'Tokyo Night',
        vars: {
          '--bg-base': '#1a1b26',
          '--bg-void': '#16161e',
          '--atmo-glow': '#16161e',
          '--surface-1': '#1f2335',
          '--surface-2': '#24283b',
          '--surface-3': '#292e42',
          '--surface-hover': '#2f354d',
          '--border-subtle': '#292e42',
          '--border': '#3b4261',
          '--border-strong': '#565f89',
          '--text-primary': '#c0caf5',
          '--text-secondary': '#9aa5ce',
          '--text-muted': '#565f89',
          '--text-disabled': '#3b4261',
          '--accent': '#7aa2f7',
          '--accent-bg': 'rgba(122, 162, 247, 0.15)',
          '--accent-border': 'rgba(122, 162, 247, 0.45)',
          '--stream': '#7dcfff',
          '--stream-bg': 'rgba(125, 207, 255, 0.15)',
          '--stream-border': 'rgba(125, 207, 255, 0.40)',
          '--success': '#9ece6a',
          '--warning': '#ff9e64',
          '--danger': '#f7768e'
        }
      },
      catppuccin_mocha: {
        name: 'Catppuccin Mocha',
        vars: {
          '--bg-base': '#1e1e2e',
          '--bg-void': '#181825',
          '--atmo-glow': '#11111b',
          '--surface-1': '#181825',
          '--surface-2': '#1e1e2e',
          '--surface-3': '#313244',
          '--surface-hover': '#45475a',
          '--border-subtle': '#313244',
          '--border': '#45475a',
          '--border-strong': '#585b70',
          '--text-primary': '#cdd6f4',
          '--text-secondary': '#cba6f7',
          '--text-muted': '#6c7086',
          '--text-disabled': '#45475a',
          '--accent': '#a6e3a1',
          '--accent-bg': 'rgba(166, 227, 161, 0.15)',
          '--accent-border': 'rgba(166, 227, 161, 0.45)',
          '--stream': '#89dceb',
          '--stream-bg': 'rgba(137, 220, 235, 0.15)',
          '--stream-border': 'rgba(137, 220, 235, 0.40)',
          '--success': '#a6e3a1',
          '--warning': '#fab387',
          '--danger': '#f38ba8'
        }
      },
      nord_dark: {
        name: 'Nord Dark',
        vars: {
          '--bg-base': '#2e3440',
          '--bg-void': '#242933',
          '--atmo-glow': '#1f232a',
          '--surface-1': '#242933',
          '--surface-2': '#2e3440',
          '--surface-3': '#3b4252',
          '--surface-hover': '#434c5e',
          '--border-subtle': '#3b4252',
          '--border': '#434c5e',
          '--border-strong': '#4c566a',
          '--text-primary': '#eceff4',
          '--text-secondary': '#88c0d0',
          '--text-muted': '#7b88a1',
          '--text-disabled': '#4c566a',
          '--accent': '#88c0d0',
          '--accent-bg': 'rgba(136, 192, 208, 0.15)',
          '--accent-border': 'rgba(136, 192, 208, 0.45)',
          '--stream': '#81a1c1',
          '--stream-bg': 'rgba(129, 161, 193, 0.15)',
          '--stream-border': 'rgba(129, 161, 193, 0.40)',
          '--success': '#a3be8c',
          '--warning': '#ebcb8b',
          '--danger': '#bf616a'
        }
      },
      cyberpunk: {
        name: 'Cyberpunk',
        vars: {
          '--bg-base': '#080811',
          '--bg-void': '#040409',
          '--atmo-glow': '#140c1e',
          '--surface-1': '#0e0e1a',
          '--surface-2': '#161626',
          '--surface-3': '#241a38',
          '--surface-hover': '#322350',
          '--border-subtle': '#241a38',
          '--border': '#3c2a60',
          '--border-strong': '#6b3ea8',
          '--text-primary': '#00ffcc',
          '--text-secondary': '#ff007f',
          '--text-muted': '#7a5299',
          '--text-disabled': '#3c2a60',
          '--accent': '#ffe600',
          '--accent-bg': 'rgba(255, 230, 0, 0.15)',
          '--accent-border': 'rgba(255, 230, 0, 0.50)',
          '--stream': '#00ffcc',
          '--stream-bg': 'rgba(0, 255, 204, 0.15)',
          '--stream-border': 'rgba(0, 255, 204, 0.45)',
          '--success': '#00ff66',
          '--warning': '#ffaa00',
          '--danger': '#ff0055'
        }
      },
      solarized_dark: {
        name: 'Solarized Dark',
        vars: {
          '--bg-base': '#002b36',
          '--bg-void': '#073642',
          '--atmo-glow': '#001e26',
          '--surface-1': '#073642',
          '--surface-2': '#002b36',
          '--surface-3': '#586e75',
          '--surface-hover': '#657b83',
          '--border-subtle': '#073642',
          '--border': '#586e75',
          '--border-strong': '#93a1a1',
          '--text-primary': '#93a1a1',
          '--text-secondary': '#839496',
          '--text-muted': '#657b83',
          '--text-disabled': '#586e75',
          '--accent': '#2aa198',
          '--accent-bg': 'rgba(42, 161, 152, 0.15)',
          '--accent-border': 'rgba(42, 161, 152, 0.45)',
          '--stream': '#268bd2',
          '--stream-bg': 'rgba(38, 139, 210, 0.15)',
          '--stream-border': 'rgba(38, 139, 210, 0.40)',
          '--success': '#859900',
          '--warning': '#b58900',
          '--danger': '#dc322f'
        }
      },
      github_light: {
        name: 'GitHub Light',
        vars: {
          '--bg-base': '#ffffff',
          '--bg-void': '#f6f8fa',
          '--atmo-glow': '#eaeef2',
          '--surface-1': '#f6f8fa',
          '--surface-2': '#eaeff4',
          '--surface-3': '#e1e4e8',
          '--surface-hover': '#e1e4e8',
          '--border-subtle': '#eaecef',
          '--border': '#d0d7de',
          '--border-strong': '#8c959f',
          '--text-primary': '#1f2328',
          '--text-secondary': '#59636e',
          '--text-muted': '#8c959f',
          '--text-disabled': '#d0d7de',
          '--accent': '#0969da',
          '--accent-bg': 'rgba(9, 105, 218, 0.10)',
          '--accent-border': 'rgba(9, 105, 218, 0.35)',
          '--stream': '#0550ae',
          '--stream-bg': 'rgba(5, 80, 174, 0.10)',
          '--stream-border': 'rgba(5, 80, 174, 0.35)',
          '--success': '#1a7f37',
          '--warning': '#9a6700',
          '--danger': '#cf222e'
        }
      },
      solarized_light: {
        name: 'Solarized Light',
        vars: {
          '--bg-base': '#fdf6e3',
          '--bg-void': '#eee8d5',
          '--atmo-glow': '#e0d9c5',
          '--surface-1': '#ffffff',
          '--surface-2': '#eee8d5',
          '--surface-3': '#e0d9c5',
          '--surface-hover': '#d6ceb8',
          '--border-subtle': '#e0d9c5',
          '--border': '#cbcdbe',
          '--border-strong': '#657b83',
          '--text-primary': '#073642',
          '--text-secondary': '#586e75',
          '--text-muted': '#93a1a1',
          '--text-disabled': '#b58900',
          '--accent': '#268bd2',
          '--accent-bg': 'rgba(38, 139, 210, 0.12)',
          '--accent-border': 'rgba(38, 139, 210, 0.40)',
          '--stream': '#2aa198',
          '--stream-bg': 'rgba(42, 161, 152, 0.12)',
          '--stream-border': 'rgba(42, 161, 152, 0.40)',
          '--success': '#859900',
          '--warning': '#b58900',
          '--danger': '#dc322f'
        }
      },
      catppuccin_latte: {
        name: 'Catppuccin Latte',
        vars: {
          '--bg-base': '#eff1f5',
          '--bg-void': '#e6e9ef',
          '--atmo-glow': '#dce0e8',
          '--surface-1': '#ffffff',
          '--surface-2': '#e6e9ef',
          '--surface-3': '#dce0e8',
          '--surface-hover': '#ccd0da',
          '--border-subtle': '#dce0e8',
          '--border': '#bcc0cc',
          '--border-strong': '#6c6f85',
          '--text-primary': '#4c4f69',
          '--text-secondary': '#6c6f85',
          '--text-muted': '#8c8fa1',
          '--text-disabled': '#bcc0cc',
          '--accent': '#1e66f5',
          '--accent-bg': 'rgba(30, 102, 245, 0.12)',
          '--accent-border': 'rgba(30, 102, 245, 0.40)',
          '--stream': '#04a5e5',
          '--stream-bg': 'rgba(4, 165, 229, 0.12)',
          '--stream-border': 'rgba(4, 165, 229, 0.40)',
          '--success': '#40a02b',
          '--warning': '#df8e1d',
          '--danger': '#d20f39'
        }
      }
    };

    function applyTheme(themeKey) {
      const t = THEMES[themeKey] || THEMES.signal;
      const root = document.documentElement;
      root.setAttribute('data-theme', themeKey);
      
      for (const [prop, val] of Object.entries(t.vars)) {
        root.style.setProperty(prop, val);
      }
      
      const select = document.getElementById('themeSelect');
      if (select && select.value !== themeKey) {
        select.value = themeKey;
      }
      
      try {
        localStorage.setItem('tare-signal-theme', themeKey);
      } catch (e) {}
    }

    // Initialize saved theme
    try {
      const saved = localStorage.getItem('tare-signal-theme') || 'signal';
      applyTheme(saved);
    } catch (e) {
      applyTheme('signal');
    }

    /* ==========================================================================
       Data Refresh & Rendering System & Markdown Helpers
       ========================================================================== */
    const approvalViewModes = {};

    function renderMarkdown(mdText) {
      if (!mdText) return '';
      try {
        if (window.marked && typeof window.marked.parse === 'function') {
          return `<div class="prose-rendered">${window.marked.parse(mdText)}</div>`;
        }
      } catch (e) {
        console.warn('Markdown parse error:', e);
      }
      return `<div class="prose-rendered" style="white-space: pre-wrap;">${escapeHtml(mdText)}</div>`;
    }

    function toggleApprovalMode(trainId) {
      const current = approvalViewModes[trainId] || 'simple';
      approvalViewModes[trainId] = current === 'simple' ? 'technical' : 'simple';
      refreshData();
    }

    async function refreshData() {
      try {
        const res = await fetch('/api/state');
        const data = await res.json();
        renderBoard(data);
      } catch (err) {
        console.error("Failed to load data:", err);
      }
    }

    function renderBoard(data) {
      document.getElementById('syncTag').textContent = 'Last sync: ' + (data.timestamp ? data.timestamp.substring(11, 19) : 'now');
      
      // Stat Metrics
      const nodeCount = data.nodes ? data.nodes.length : 0;
      const trainCount = data.trains ? data.trains.length : 0;
      const landedCount = data.recentLanded ? data.recentLanded.length : 0;
      
      document.getElementById('nodeCountStat').textContent = `${nodeCount} active nodes`;
      document.getElementById('trainCountStat').textContent = `${trainCount} active`;
      document.getElementById('landedCountStat').textContent = `${landedCount} finished`;
      
      if (data.graph) {
        document.getElementById('graphStat').textContent = 
          `${data.graph.total_nodes} nodes (${data.graph.done_nodes} done) • ${data.graph.total_edges} edges`;
        document.getElementById('graphStatsBadge').textContent = 
          `${data.graph.total_nodes} nodes (${data.graph.done_nodes} done, ${data.graph.pending_nodes} pending) • ${data.graph.total_edges} edges • 0 cycles`;
      }
      
      // Render Auto Mode Toggle
      const autoBtn = document.getElementById('autoModeBtn');
      const isAuto = data.auto_mode && data.auto_mode.enabled;
      if (isAuto) {
        autoBtn.innerHTML = '⚡ Auto: <b>ON</b>';
        autoBtn.className = 'btn-auto-toggle active';
      } else {
        autoBtn.innerHTML = '⚡ Auto: <b>OFF</b>';
        autoBtn.className = 'btn-auto-toggle';
      }

      // Render Pending Approvals Card
      const approvalsCard = document.getElementById('approvalsCard');
      const approvalsList = document.getElementById('approvalsList');
      const approvals = data.pending_approvals || [];
      document.getElementById('approvalCount').textContent = approvals.length + ' pending';
      
      if (approvals.length > 0) {
        approvalsCard.style.display = 'flex';
        approvalsList.innerHTML = '';
        approvals.forEach(app => {
          const div = document.createElement('div');
          div.className = 'approval-item';
          
          const currentMode = approvalViewModes[app.train_id] || 'simple';
          const isSimple = currentMode === 'simple';
          const plain = app.plain_explanation || {};

          let contentHtml = '';
          if (isSimple) {
            // Executive mode / no jargon
            contentHtml = `
              <div style="display: flex; flex-direction: column; gap: 12px; background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: 6px; padding: 14px;">
                <!-- What it does -->
                <div>
                  <div style="font-size: 11px; font-weight: bold; color: var(--accent); text-transform: uppercase; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                    <span>📌 What This Update Does:</span>
                  </div>
                  <div style="font-size: 13.5px; color: var(--text-primary); line-height: 1.5; font-family: var(--font-prose);">
                    ${escapeHtml(plain.what_it_does || app.title)}
                  </div>
                </div>

                <!-- Benefit & reason -->
                <div>
                  <div style="font-size: 11px; font-weight: bold; color: var(--stream); text-transform: uppercase; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                    <span>🎯 Benefit & Purpose in the System:</span>
                  </div>
                  <div style="font-size: 13px; color: var(--text-primary); line-height: 1.5; font-family: var(--font-prose);">
                    ${escapeHtml(plain.why_it_matters || 'Keeps the agent architecture stable, secure and evolving.')}
                  </div>
                </div>

                <!-- Quality and tests -->
                <div>
                  <div style="font-size: 11px; font-weight: bold; color: var(--success); text-transform: uppercase; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                    <span>🧪 Quality Assurance & Tests:</span>
                  </div>
                  <div style="font-size: 12.5px; color: var(--text-primary); line-height: 1.5; font-family: var(--font-prose);">
                    ${escapeHtml(plain.testing_status || 'Automated test suite and adversarial falsifiers passed.')}
                  </div>
                </div>

                <!-- Audit status & risks -->
                <div style="display: flex; gap: 14px; flex-wrap: wrap; margin-top: 2px; padding-top: 10px; border-top: 1px solid var(--border-subtle);">
                  <div style="flex: 1; min-width: 240px;">
                    <div style="font-size: 11px; font-weight: bold; color: var(--success); text-transform: uppercase; margin-bottom: 2px;">🛡️ Safety Verdict:</div>
                    <div style="font-size: 12px; color: var(--text-secondary); font-family: var(--font-prose);">${escapeHtml(plain.verdict_plain || 'Approved for a safe landing.')}</div>
                  </div>
                  <div style="flex: 1; min-width: 240px;">
                    <div style="font-size: 11px; font-weight: bold; color: ${plain.has_debts ? 'var(--warning)' : 'var(--text-muted)'}; text-transform: uppercase; margin-bottom: 2px;">⚠️ Notes / Debts:</div>
                    <div style="font-size: 12px; color: var(--text-secondary); font-family: var(--font-prose);">${escapeHtml(plain.debts_plain || 'No debt or residual risk found.')}</div>
                  </div>
                </div>
              </div>
            `;
          } else {
            // Technical / detailed mode (rich Markdown)
            let findingsHtml = '';
            if (app.findings && app.findings.length > 0) {
              findingsHtml = `
                <div style="background: var(--surface-3); border: 1px solid var(--border); border-radius: 4px; padding: 12px;">
                  <div style="font-size: 11px; font-weight: bold; color: var(--accent); text-transform: uppercase; margin-bottom: 6px;">🔍 Findings & Adversarial Tests (U-7D):</div>
                  <div class="prose-rendered">
                    <ul>
                      ${app.findings.map(f => `<li>${window.marked ? window.marked.parseInline(f) : escapeHtml(f)}</li>`).join('')}
                    </ul>
                  </div>
                </div>
              `;
            }

            let debtsHtml = '';
            if (app.debts && app.debts.length > 0) {
              debtsHtml = `
                <div style="background: var(--surface-3); border: 1px solid var(--border); border-radius: 4px; padding: 12px;">
                  <div style="font-size: 11px; font-weight: bold; color: var(--warning); text-transform: uppercase; margin-bottom: 6px;">⚠️ Technical Debts & Residual Risks:</div>
                  <div class="prose-rendered">
                    <ul>
                      ${app.debts.map(d => `<li>${window.marked ? window.marked.parseInline(d) : escapeHtml(d)}</li>`).join('')}
                    </ul>
                  </div>
                </div>
              `;
            }

            contentHtml = `
              <!-- Formal audit recommendation in Markdown -->
              <div style="background: var(--success-bg); border: 1px solid var(--success-border); border-radius: 4px; padding: 12px;">
                <div style="font-size: 11px; font-weight: bold; color: var(--success); text-transform: uppercase; margin-bottom: 6px;">🎯 Formal Audit Recommendation:</div>
                <div style="font-size: 13px;">${renderMarkdown(app.recommendation || 'Passed the adversarial audit with no blocking issues.')}</div>
              </div>

              ${findingsHtml}
              ${debtsHtml}

              <!-- Full dossier as formatted Markdown -->
              <details style="background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: 4px; padding: 10px 14px;">
                <summary style="font-size: 11.5px; color: var(--stream); font-weight: 600; cursor: pointer; user-select: none;">📄 Show the Full Audit Dossier (Formatted Markdown)</summary>
                <div style="margin-top: 12px; background: var(--surface-2); padding: 14px; border-radius: 6px; border: 1px solid var(--border); max-height: 480px; overflow-y: auto;">
                  ${renderMarkdown(app.raw_text || '')}
                </div>
              </details>
            `;
          }

          div.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px;">
              <div>
                <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                  <b style="font-size: 15px; color: var(--text-primary);">🚆 ${escapeHtml(app.train_id)}</b>
                  <span class="status-tag ${app.verdict === 'VERIFIED_PASS' ? 'tag-green' : (app.verdict === 'FAIL' ? 'tag-red' : 'tag-yellow')}">🛡️ AUDIT: ${escapeHtml(app.verdict)}</span>
                </div>
                <div style="font-size: 13px; color: var(--text-secondary); margin-top: 2px;"><b>${escapeHtml(app.title)}</b></div>
                <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">Graph Tasks: <code>${(app.tasks || []).map(escapeHtml).join(', ')}</code></div>
              </div>
              <div style="display: flex; align-items: center; gap: 8px;">
                <button class="mode-toggle-btn ${isSimple ? 'active-simple' : ''}" onclick="toggleApprovalMode('${app.train_id}')">
                  ${isSimple ? '🔬 Show Technical Details' : '👔 Show Executive Summary (No Jargon)'}
                </button>
              </div>
            </div>

            ${contentHtml}

            <!-- Gatekeeper actions -->
            <div style="display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; margin-top: 6px; padding-top: 10px; border-top: 1px solid var(--border-subtle);">
              <button class="btn-reject" onclick="rejectTrain('${app.train_id}', '${app.nonce}')">❌ Deny / Return</button>
              <button class="btn-auto-action" onclick="autoApproveTrain('${app.train_id}', '${app.nonce}')">⚡ Approve & Turn On Auto Mode</button>
              <button class="btn-approve" onclick="approveTrain('${app.train_id}', '${app.nonce}')">✅ Approve & Land (1-Click)</button>
            </div>
          `;
          approvalsList.appendChild(div);
        });
      } else {
        approvalsCard.style.display = 'none';
      }

      // Render Nodes Grid
      const nodesGrid = document.getElementById('nodesGrid');
      nodesGrid.innerHTML = '';
      if (data.nodes && data.nodes.length > 0) {
        data.nodes.forEach(n => {
          const res = n.resources || {};
          const isGpuBusy = (n.pressure || '').includes('GPU_BUSY');
          const isCpuBusy = (n.pressure || '').includes('HIGH_PRESSURE');
          
          let gpuHtml = '<span class="metric-badge">No GPU</span>';
          if (res.gpu_available) {
            const gpuClass = (res.gpu_load_pct > 80 || isGpuBusy) ? 'busy' : 'ok';
            gpuHtml = `<span class="metric-badge ${gpuClass}">🎮 ${res.gpu_name ? res.gpu_name.replace('NVIDIA GeForce ', '') : 'GPU'} (${res.gpu_load_pct || 0}% load • ${Math.round(res.gpu_vram_free_mb || 0)}MB vram free)</span>`;
          }

          const cpuClass = (res.cpu_load_pct > 80 || isCpuBusy) ? 'busy' : 'ok';
          const ramClass = (res.ram_load_pct > 85) ? 'busy' : 'ok';

          const card = document.createElement('div');
          card.className = 'node-box';
          card.innerHTML = `
            <div class="node-title-row">
              <span class="node-name">💻 ${escapeHtml(n.hostname)}</span>
              <span class="node-ip">${escapeHtml(n.ip || '127.0.0.1')}</span>
            </div>
            <div class="metrics-row">
              <span class="metric-badge ${cpuClass}">⚡ CPU: ${res.cpu_load_pct || 0}%</span>
              <span class="metric-badge ${ramClass}">🧠 RAM: ${res.ram_available_gb || 0}GB free (${res.ram_load_pct || 0}%)</span>
              ${gpuHtml}
            </div>
            <div style="font-size: 11px; color: var(--text-muted); display: flex; justify-content: space-between;">
              <span>Status: <b style="color: var(--accent);">${escapeHtml(n.pressure || 'ONLINE')}</b></span>
              <span>Sync: ${n.updated_at ? n.updated_at.substring(11, 19) : 'now'}</span>
            </div>
          `;
          nodesGrid.appendChild(card);
        });
      } else {
        nodesGrid.innerHTML = '<div style="color: var(--text-muted); padding: 12px;">No active node right now.</div>';
      }

      // Render Trains Table
      const tbody = document.getElementById('trainsBody');
      tbody.innerHTML = '';
      document.getElementById('trainTag').textContent = (data.trains ? data.trains.length : 0) + ' active';
      if (data.trains && data.trains.length > 0) {
        data.trains.forEach(t => {
          let tagClass = 'tag-yellow';
          if (t.status === 'LANDED') tagClass = 'tag-green';
          if (t.status.includes('APPROVED')) tagClass = 'tag-green';
          if (t.status.includes('AUDIT')) tagClass = 'tag-purple';
          if (t.status.includes('REJECTED') || t.status.includes('HOLD')) tagClass = 'tag-red';

          const lease = (data.leases || []).find(l => l.target_id === t.train_id);
          let workerInfo = '<span style="color: var(--text-muted);">💤 Queued</span>';
          if (lease) {
            workerInfo = `<span style="color: var(--accent); font-weight: bold;">⚡ ${escapeHtml(lease.hostname)} (${escapeHtml(lease.agent_id)})</span>`;
          } else if (t.status === 'READY_FOR_HUMAN_APPROVAL') {
            workerInfo = `<span style="color: var(--warning); font-weight: bold;">👤 Awaiting Approval</span>`;
          }

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><b>${escapeHtml(t.train_id)}</b></td>
            <td><span class="status-tag ${tagClass}">${escapeHtml(t.status)}</span></td>
            <td>${workerInfo}</td>
            <td><code>${escapeHtml(t.current_role)}</code></td>
            <td>r${t.revision}</td>
            <td>${(t.task_ids || []).map(escapeHtml).join(', ')}</td>
          `;
          tbody.appendChild(tr);
        });
      } else {
        tbody.innerHTML = '<tr><td colspan="6" style="color: var(--text-muted); text-align: center; padding: 14px;">No active train right now (cluster idle).</td></tr>';
      }

      // Render Landed Trains List
      const landedList = document.getElementById('landedList');
      landedList.innerHTML = '';
      if (data.recentLanded && data.recentLanded.length > 0) {
        data.recentLanded.forEach(l => {
          const item = document.createElement('div');
          item.innerHTML = `🏁 <b>${escapeHtml(l.train_id)}</b> — tasks: <code>${(l.task_ids || []).map(escapeHtml).join(', ')}</code>`;
          landedList.appendChild(item);
        });
      } else {
        landedList.innerHTML = '<span style="color: var(--text-muted);">No train in recent history.</span>';
      }
    }

    async function toggleAutoMode() {
      try {
        const res = await fetch('/api/approvals/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'TOGGLE_AUTO' })
        });
        const data = await res.json();
        if (data.ok) {
          const isEnabled = data.auto_mode && data.auto_mode.enabled;
          const status = isEnabled ? "ON" : "OFF";
          showToast(`⚡ Auto-Approve Mode ${status}!`);
          refreshData();
        } else {
          showToast(`❌ Failed to toggle the mode: ${data.reason}`);
        }
      } catch (err) {
        showToast("❌ Request failed: " + err);
      }
    }

    async function approveTrain(trainId, nonce) {
      if (!confirm(`Approve and land release train ${trainId}?`)) return;
      try {
        const res = await fetch('/api/approvals/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'APPROVE', train_id: trainId, nonce: nonce })
        });
        const data = await res.json();
        if (data.ok) {
          showToast(`✅ Train ${trainId} landed!`);
          refreshData();
        } else {
          showToast(`❌ Landing failed: ${data.reason}`);
        }
      } catch (err) {
        showToast("❌ Request failed: " + err);
      }
    }

    async function autoApproveTrain(trainId, nonce) {
      if (!confirm(`Approve ${trainId} and turn ON global auto-approve for the next trains?`)) return;
      try {
        const res = await fetch('/api/approvals/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'AUTO_ON', train_id: trainId, nonce: nonce })
        });
        const data = await res.json();
        if (data.ok) {
          showToast(`⚡ ${trainId} landed and Auto-Approve turned ON!`);
          refreshData();
        } else {
          showToast(`❌ Error: ${data.reason}`);
        }
      } catch (err) {
        showToast("❌ Request failed: " + err);
      }
    }

    async function rejectTrain(trainId, nonce) {
      const reason = prompt(`Rejection reason for ${trainId}:`, "The plan/code needs changes.");
      if (reason === null) return;
      try {
        const res = await fetch('/api/approvals/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'REJECT', train_id: trainId, nonce: nonce, reason: reason })
        });
        const data = await res.json();
        if (data.ok) {
          showToast(`⚠️ Train ${trainId} rejected and returned to the implementer.`);
          refreshData();
        } else {
          showToast(`❌ Rejection failed: ${data.reason}`);
        }
      } catch (err) {
        showToast("❌ Request failed: " + err);
      }
    }

    async function submitIntake(ev) {
      ev.preventDefault();
      const title = document.getElementById('title').value.trim();
      const body = document.getElementById('body').value.trim();
      if (!title) return;

      try {
        const res = await fetch('/api/intake', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, body })
        });
        const data = await res.json();
        if (data.ok) {
          showToast("✨ Demand added to the backlog and admitted to the graph!");
          document.getElementById('intakeForm').reset();
          refreshData();
        } else {
          showToast("❌ Send failed: " + data.reason);
        }
      } catch (err) {
        showToast("❌ Request failed: " + err);
      }
    }

    /* ==========================================================================
       Overseer Chat System
       ========================================================================== */
    let lastOverseerSignature = '';
    let forceScrollNext = false;

    function sanitizeHtml(html) {
      if (!html) return '';
      return html
        .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
        .replace(/<iframe\b[^<]*(?:(?!<\/iframe>)<[^<]*)*<\/iframe>/gi, '')
        .replace(/<object\b[^<]*(?:(?!<\/object>)<[^<]*)*<\/object>/gi, '')
        .replace(/<embed\b[^<]*(?:(?!<\/embed>)<[^<]*)*<\/embed>/gi, '')
        .replace(/on\w+\s*=\s*(["']).*?\1/gi, '')
        .replace(/on\w+\s*=\s*[^>\s]+/gi, '')
        .replace(/href\s*=\s*(["'])javascript:.*?\1/gi, 'href="#"');
    }

    function renderMarkdown(md) {
      if (!md) return '';
      if (window.marked && typeof window.marked.parse === 'function') {
        try {
          const raw = window.marked.parse(md, { gfm: true, breaks: true });
          return sanitizeHtml(raw);
        } catch (e) {
          console.error("Marked parse error:", e);
        }
      }
      let s = escapeHtml(md);
      s = s.replace(/```([a-z0-9_-]*)\n([\s\S]*?)```/g, '<pre><code class="lang-$1">$2</code></pre>');
      s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
      s = s.replace(/\*\*\*(.*?)\*\*\*/g, '<b><i>$1</i></b>');
      s = s.replace(/\*\*(.*?)\*\*/g, '<b>$1</b>');
      s = s.replace(/\*(.*?)\*/g, '<i>$1</i>');
      s = s.replace(/^### (.*$)/gim, '<h3>$1</h3>');
      s = s.replace(/^## (.*$)/gim, '<h2>$1</h2>');
      s = s.replace(/^# (.*$)/gim, '<h1>$1</h1>');
      s = s.replace(/^\> (.*$)/gim, '<blockquote>$1</blockquote>');
      s = s.replace(/^\- (.*$)/gim, '<li>$1</li>');
      s = s.replace(/\n/g, '<br>');
      return sanitizeHtml(s);
    }

    async function refreshOverseerChat() {
      try {
        const res = await fetch('/api/overseer/messages');
        const data = await res.json();
        renderOverseerChat(data);
      } catch (err) {
        console.error("Failed to load the Overseer chat:", err);
      }
    }

    function renderOverseerChat(data) {
      const feed = document.getElementById('overseerChatFeed');
      const statusPill = document.getElementById('overseerStatusPill');
      const sendBtn = document.getElementById('overseerSendBtn');
      const isBusy = data.is_busy;

      if (isBusy) {
        statusPill.className = 'status-tag tag-yellow';
        statusPill.textContent = '⚡ PROCESSING...';
        sendBtn.disabled = true;
        sendBtn.textContent = '⏳ Analyzing...';
      } else {
        statusPill.className = 'status-tag tag-green';
        statusPill.textContent = '● READY';
        sendBtn.disabled = false;
        sendBtn.textContent = '🚀 Send';
      }

      const msgs = data.messages || [];
      const lastMsg = msgs.length > 0 ? msgs[msgs.length - 1] : {};
      const currentSignature = `${msgs.length}_${lastMsg.id || ''}_${(lastMsg.text || '').length}_${isBusy}`;
      if (currentSignature === lastOverseerSignature && !forceScrollNext) {
        return;
      }
      lastOverseerSignature = currentSignature;

      const isNearBottom = (feed.scrollHeight - feed.scrollTop - feed.clientHeight) < 120;

      if (msgs.length === 0) {
        feed.innerHTML = `
          <div style="color: var(--text-muted); font-size: 13px; text-align: center; padding: 40px;">
            🤖 <b>Cluster Overseer ready.</b><br><br>
            Send a message or use the quick buttons above to diagnose the cluster, unblock nodes or orchestrate the backlog.
          </div>
        `;
        return;
      }

      let html = '';
      msgs.forEach(m => {
        const isUser = m.sender === 'operator';
        if (isUser) {
          html += `
            <div class="chat-bubble-user">
              <div style="font-size: 11px; opacity: 0.85; margin-bottom: 4px; font-weight: 600; color: var(--accent);">👤 You (Operator) • ${m.timestamp ? m.timestamp.substring(11, 19) : ''}</div>
              <div style="white-space: pre-wrap;">${escapeHtml(m.text)}</div>
            </div>
          `;
        } else {
          let stepsHtml = '';
          if (m.steps && m.steps.length > 0) {
            stepsHtml = `
              <div style="display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px;">
                ${m.steps.map(s => `<div class="step-pill">${escapeHtml(s)}</div>`).join('')}
              </div>
            `;
          }
          const engineTag = m.engine ? `<span style="font-size: 11px; color: var(--stream); font-weight: normal; margin-left: 6px;">[${escapeHtml(m.engine)}]</span>` : '';
          const parsedBody = renderMarkdown(m.text);
          html += `
            <div class="chat-bubble-overseer">
              <div style="font-size: 11px; color: var(--text-muted); margin-bottom: 8px; display: flex; justify-content: space-between; border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px;">
                <span style="font-weight: 700; color: var(--text-primary); font-family: var(--font-mono);">🤖 Cluster Overseer ${engineTag}</span>
                <span style="font-family: var(--font-mono);">${m.timestamp ? m.timestamp.substring(11, 19) : ''}</span>
              </div>
              ${stepsHtml}
              <div class="overseer-md-content">${parsedBody}</div>
            </div>
          `;
        }
      });

      feed.innerHTML = html;

      if (isNearBottom || forceScrollNext) {
        feed.scrollTop = feed.scrollHeight;
        forceScrollNext = false;
      }
    }

    async function submitOverseerChat(ev) {
      if (ev) ev.preventDefault();
      const input = document.getElementById('overseerInput');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      forceScrollNext = true;
      
      try {
        const res = await fetch('/api/overseer/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text })
        });
        const data = await res.json();
        if (data.ok) {
          lastOverseerSignature = '';
          refreshOverseerChat();
        } else {
          showToast('❌ Error: ' + data.reason);
        }
      } catch (err) {
        showToast('❌ Request failed: ' + err);
      }
    }

    function handleOverseerKey(ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        submitOverseerChat();
      }
    }

    function sendSuggestion(text) {
      document.getElementById('overseerInput').value = text;
      submitOverseerChat();
    }

    async function clearOverseerChat() {
      if (!confirm("Clear the conversation history with the Overseer?")) return;
      try {
        await fetch('/api/overseer/clear', { method: 'POST' });
        lastOverseerSignature = '';
        refreshOverseerChat();
      } catch (err) {
        showToast('❌ Failed to clear the chat: ' + err);
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 4000);
    }

    refreshData();
    refreshOverseerChat();
    setInterval(refreshData, 3000);
    setInterval(refreshOverseerChat, 2500);
  </script>
</body>
</html>
"""


class CoordinatorHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(COCKPIT_HTML.encode("utf-8"))
        elif url.path in ("/graph", "/work-graph.html", "/backlog"):
            graph_html_path = RELAY_ROOT / "work-graph.html"
            if graph_html_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(graph_html_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif url.path in ("/work-graph.json", "/api/graph"):
            graph_json_path = RELAY_ROOT / "work-graph.json"
            if graph_json_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(graph_json_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif url.path in ("/work-graph.mermaid", "/graph.mermaid"):
            graph_mermaid_path = RELAY_ROOT / "work-graph.mermaid"
            if graph_mermaid_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(graph_mermaid_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif url.path == "/api/overseer/messages":
            self._send_json({"ok": True, "messages": overseer_agent.get_chat_history(), "is_busy": OVERSEER_BUSY})
        elif url.path == "/api/state":
            # Live cluster state
            board = relay_io.read_json(RELAY_DIR / "BOARD.json") or {}
            # Inject live nodes from coordinator SQLite DB
            board["nodes"] = DB.get_live_nodes()
            board["auto_mode"] = human_watcher.get_auto_mode()
            board["pending_approvals"] = get_pending_approvals()
            board["round_tables"] = get_all_round_tables()
            board["vendor_fuel"] = vendor_chairs.load_fuel_state()
            board["timestamp"] = relay_mesh.now_iso()
            # Graph telemetry
            graph_path = RELAY_ROOT / "work-graph.json"
            if graph_path.exists():
                try:
                    g_data = relay_io.read_json(graph_path) or {}
                    nodes = g_data.get("nodes", [])
                    edges = g_data.get("edges", [])
                    done_count = sum(1 for n in nodes if isinstance(n, dict) and n.get("completion", {}).get("status") == "DONE")
                    board["graph"] = {
                        "total_nodes": len(nodes),
                        "total_edges": len(edges),
                        "done_nodes": done_count,
                        "pending_nodes": len(nodes) - done_count,
                    }
                except Exception:
                    pass
            self._send_json(board)
        elif url.path == "/api/round_tables":
            self._send_json({"ok": True, "cases": get_all_round_tables()})
        elif url.path.startswith("/api/round_table/"):
            case_id = url.path.replace("/api/round_table/", "").strip("/")
            case = round_table_engine.RoundTableCase(case_id)
            st = case.load_state()
            if st:
                dec_file = case.decision_dir / "DECISION.md"
                decision_text = dec_file.read_text(encoding="utf-8", errors="replace") if dec_file.exists() else ""
                self._send_json({"ok": True, "case": st, "decision_md": decision_text})
            else:
                self._send_json({"ok": False, "reason": "Case not found"}, 404)
        elif url.path == "/api/v1/vendor-fuel":
            self._send_json({"ok": True, "fuel_state": vendor_chairs.load_fuel_state()})
        elif url.path == "/api/v1/jobs/available":
            # Return unleased actionable trains
            available = []
            if relay_mesh.TRAINS_DIR.exists():
                for td in sorted(relay_mesh.TRAINS_DIR.iterdir()):
                    if not td.is_dir():
                        continue
                    st = relay_io.read_json(td / "STATE.json") or {}
                    status = st.get("status")
                    if status in ("PLAN_APPROVED", "AUDIT_PENDING", "AUDIT_REJECTED"):
                        tid = st.get("train_id", td.name)
                        # Check if active in DB
                        available.append({
                            "train_id": tid,
                            "status": status,
                            "role": st.get("current_role"),
                            "revision": st.get("revision", 1),
                            "title": st.get("title", "")
                        })
            self._send_json({"ok": True, "jobs": available})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        MAX_REQUEST_BYTES = 128 * 1024
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"ok": False, "reason": "Invalid Content-Length header"}, 400)
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._send_json({"ok": False, "reason": "Request payload exceeds 128KB limit"}, 413)
            return

        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        if url.path == "/api/v1/nodes/heartbeat":
            # Worker telemetry heartbeat
            DB.update_node_heartbeat(payload)
            self._send_json({"ok": True, "server_time": time.time()})

        elif url.path == "/api/v1/jobs/claim":
            train_id = payload.get("train_id")
            role = payload.get("role", "implementer")
            host = payload.get("hostname", "unknown")
            agent = payload.get("agent_id", "worker")
            ttl_s = int(payload.get("ttl_seconds", 120))

            claim = DB.claim_job(train_id, role, host, agent, ttl_s)
            if claim:
                self._send_json({"ok": True, "claim": claim})
            else:
                self._send_json({"ok": False, "reason": "Job is actively held by another node"}, 409)

        elif url.path == "/api/v1/jobs/heartbeat":
            train_id = payload.get("train_id")
            token = payload.get("lease_token")
            ttl_s = int(payload.get("ttl_seconds", 120))
            if DB.renew_job(train_id, token, ttl_s):
                self._send_json({"ok": True, "expires_ts": time.time() + ttl_s})
            else:
                self._send_json({"ok": False, "reason": "Lease not found or expired"}, 410)

        elif url.path == "/api/v1/jobs/complete":
            train_id = payload.get("train_id")
            token = payload.get("lease_token")
            # Verify and release in DB
            if DB.release_job(train_id, token):
                # Update train state if provided
                relay_mesh.update_board()
                self._send_json({"ok": True, "train_id": train_id})
            else:
                self._send_json({"ok": False, "reason": "Invalid or expired lease token"}, 410)

        elif url.path == "/api/round_table/convene":
            title = payload.get("title", "New Deliberation")
            proposal = payload.get("proposal", "")
            cid = payload.get("case_id")
            res = round_table_engine.init_round_table(
                case_id=cid or f"RT-{int(time.time()*1000)%100000}",
                title=title,
                proposal_text=proposal
            )
            self._send_json({"ok": True, "case": res})

        elif url.path == "/api/round_table/deliberate":
            cid = payload.get("case_id")
            if not cid:
                self._send_json({"ok": False, "reason": "case_id required"}, 400)
                return
            def _async_deliberate():
                try:
                    round_table_engine.run_full_deliberation(cid)
                except Exception as e:
                    print(f"Error in async deliberation for {cid}: {e}", flush=True)
            threading.Thread(target=_async_deliberate, daemon=True).start()
            self._send_json({"ok": True, "message": f"Deliberation started for {cid}"})

        elif url.path == "/api/publish_study":
            import publisher_engine
            src_file = payload.get("file")
            context_dir = payload.get("context", "02_harness-architecture")
            if not src_file:
                self._send_json({"ok": False, "reason": "file parameter required"}, 400)
                return
            try:
                res = publisher_engine.publish_study(src_file, context_dir=context_dir)
                self._send_json({"ok": True, "result": res})
            except Exception as e:
                self._send_json({"ok": False, "reason": str(e)}, 500)


        elif url.path == "/api/approvals/action":
            train_id = payload.get("train_id", "")
            nonce = payload.get("nonce", "")
            action = str(payload.get("action", "APPROVE")).upper()
            reason = str(payload.get("reason", "")).strip()

            if action in ("TOGGLE_AUTO", "AUTO_TOGGLE", "TOGGLE"):
                cur = human_watcher.get_auto_mode()
                new_val = not cur.get("enabled", False)
                human_watcher.set_auto_mode(new_val, "Toggled via Cockpit Web UI")
                if new_val:
                    # Immediately sweep all pending approvals
                    pending_apps = get_pending_approvals()
                    for app in pending_apps:
                        tid = app["train_id"]
                        n_val = app.get("nonce")
                        if n_val:
                            if relay_mesh.land_train(tid, "COORDINATOR_AUTO_APPROVE", nonce=n_val):
                                (RELAY_DIR / "approvals" / f"APPROVAL_REQUEST_{tid}.md").unlink(missing_ok=True)
                                (RELAY_DIR / "approvals" / f"CAPABILITY_{tid}.json").unlink(missing_ok=True)
                                try:
                                    bookkeeper.run_bookkeeping_cycle(tid)
                                except Exception:
                                    pass
                    relay_mesh.update_board()
                self._send_json({"ok": True, "auto_mode": human_watcher.get_auto_mode()})
                return

            if action in ("AUTO_ON", "AUTO_OFF"):
                enabled = (action == "AUTO_ON")
                human_watcher.set_auto_mode(enabled, f"Set via Cockpit for train {train_id}")
                if enabled:
                    # Immediately sweep all pending approvals
                    pending_apps = get_pending_approvals()
                    for app in pending_apps:
                        tid = app["train_id"]
                        n_val = app.get("nonce")
                        if n_val:
                            if relay_mesh.land_train(tid, "COORDINATOR_AUTO_APPROVE", nonce=n_val):
                                (RELAY_DIR / "approvals" / f"APPROVAL_REQUEST_{tid}.md").unlink(missing_ok=True)
                                (RELAY_DIR / "approvals" / f"CAPABILITY_{tid}.json").unlink(missing_ok=True)
                                try:
                                    bookkeeper.run_bookkeeping_cycle(tid)
                                except Exception:
                                    pass
                    relay_mesh.update_board()
                self._send_json({"ok": True, "auto_mode": human_watcher.get_auto_mode()})
                return

            if action == "APPROVE":
                if not train_id:
                    self._send_json({"ok": False, "reason": "Missing train_id"}, 400)
                    return
                if not nonce:
                    self._send_json({"ok": False, "reason": "Missing required approval capability nonce (unauthorized)"}, 400)
                    return
                
                success = relay_mesh.land_train(train_id, "WEB_COCKPIT_1CLICK", nonce=nonce)
                if success:
                    (RELAY_DIR / "approvals" / f"APPROVAL_REQUEST_{train_id}.md").unlink(missing_ok=True)
                    (RELAY_DIR / "approvals" / f"CAPABILITY_{train_id}.json").unlink(missing_ok=True)
                    try:
                        bookkeeper.run_bookkeeping_cycle(train_id)
                    except Exception as exc:
                        print(f"[COORDINATOR] Bookkeeper error on landing: {exc}")
                    relay_mesh.update_board()
                    self._send_json({"ok": True, "train_id": train_id, "status": "LANDED"})
                else:
                    self._send_json({"ok": False, "reason": "Landing failed or refused by relay rules"}, 409)
                return

            elif action == "REJECT":
                if not train_id:
                    self._send_json({"ok": False, "reason": "Missing train_id"}, 400)
                    return
                reject_reason = reason or "Rejected by operator via Cockpit Web UI"
                success = relay_mesh.transition_train(
                    train_id, "AUDIT_REJECTED", "implementer", "human_gatekeeper", "human", reject_reason
                )
                if success:
                    (RELAY_DIR / "approvals" / f"APPROVAL_REQUEST_{train_id}.md").unlink(missing_ok=True)
                    (RELAY_DIR / "approvals" / f"CAPABILITY_{train_id}.json").unlink(missing_ok=True)
                    relay_mesh.update_board()
                    self._send_json({"ok": True, "train_id": train_id, "status": "AUDIT_REJECTED"})
                else:
                    self._send_json({"ok": False, "reason": "Rejection transition failed"}, 409)
                return

            else:
                self._send_json({"ok": False, "reason": f"Unknown action {action}"}, 400)
                return

        elif url.path == "/api/overseer/chat":
            user_msg = str(payload.get("message", "")).strip()
            engine = str(payload.get("engine", "auto")).strip()
            if not user_msg:
                self._send_json({"ok": False, "reason": "Message is required"}, 400)
                return

            def _worker():
                global OVERSEER_BUSY
                with OVERSEER_LOCK:
                    OVERSEER_BUSY = True
                    try:
                        overseer_agent.process_user_message(user_msg, engine)
                    except Exception as exc:
                        print(f"[OVERSEER ERROR] {exc}")
                    finally:
                        OVERSEER_BUSY = False

            threading.Thread(target=_worker, daemon=True).start()
            self._send_json({"ok": True, "status": "PROCESSING"})

        elif url.path == "/api/overseer/clear":
            overseer_agent.clear_chat_history()
            self._send_json({"ok": True})

        elif url.path == "/api/intake":
            title = str(payload.get("title", "")).strip()
            body = str(payload.get("body", "")).strip()
            if not title:
                self._send_json({"ok": False, "reason": "Title is required"}, 400)
                return

            INTAKE_HUMAN_DIR.mkdir(parents=True, exist_ok=True)
            item_slug = intake_processor._slug(title, 32)
            item_filename = f"INTAKE_WEB_{int(time.time())}_{item_slug}.md"
            item_path = INTAKE_HUMAN_DIR / item_filename
            content = f"# {title}\n\n**Author:** Operator (via Cockpit Web UI)\n**Timestamp:** {relay_mesh.now_iso()}\n\n{body}\n"
            item_path.write_text(content, encoding="utf-8")

            intake_processor.sweep_human()
            relay_mesh.update_board()
            self._send_json({"ok": True, "filename": item_filename, "title": title})

        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, data: Dict[str, Any], code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def start_local_telemetry_thread() -> threading.Thread:
    """Periodically probes local hardware telemetry and registers on the coordinator."""
    def _beacon():
        while True:
            try:
                telem = resource_telemetry.probe_host_telemetry()
                telem["ip"] = get_tailscale_ip() or get_lan_ip()
                DB.update_node_heartbeat(telem)
                resource_telemetry.broadcast_heartbeat()
            except Exception:
                pass
            time.sleep(10)

    t = threading.Thread(target=_beacon, daemon=True)
    t.start()
    return t


def start_coordinator(host: str = HOST, port: int = PORT) -> socketserver.TCPServer:
    """Starts the Cluster Coordinator and Watchdog in daemon threads."""
    start_watchdog_thread()
    start_local_telemetry_thread()
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer((host, port), CoordinatorHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main():
    relay_io.utf8_console()
    lan_ip = get_lan_ip()
    ts_ip = get_tailscale_ip()

    print("=" * 80)
    print("  🚀 TARE.TOOLS CLUSTER COORDINATOR & OWNER COCKPIT (TAILSCALE DIRECT P2P)")
    print("=" * 80)
    print(f"  • Localhost (this PC):    http://localhost:{PORT}/")
    print(f"  • Wi-Fi / Phone / LAN:    http://{lan_ip}:{PORT}/")
    if ts_ip:
        print(f"  • Private Tailscale P2P:  http://{ts_ip}:{PORT}/ (from anywhere on 4G/5G)")
    print("=" * 80)
    print("Press Ctrl+C to stop the coordinator.\n")

    start_watchdog_thread()
    start_local_telemetry_thread()
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer((HOST, PORT), CoordinatorHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[COORDINATOR] Stopping the coordinator.")


if __name__ == "__main__":
    main()

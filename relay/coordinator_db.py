"""SQLite WAL coordinator database for work leases, monotonic fences, bindings, and terminal claims."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import relay_io, relay_mesh

RELAY_DIR = relay_mesh.RELAY_DIR
DB_FILE = RELAY_DIR / "history" / "coordinator.db"


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

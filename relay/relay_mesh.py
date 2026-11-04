#!/usr/bin/env python3
"""
tare.tools Multi-Agent Relay Mesh (MARM) Core Engine v3.0 (TRAIN-06)
Zero-dependency, file-based asynchronous coordination plane for distributed
multi-agent swarms, synchronized over Google Drive.

v3.0 (RELAY-CONTROL-PLANE-01): explicit FSM with revision CAS under a
train-scoped writer lock; role-credentialed leases; evidence-gated audit
transitions; train-scoped single-use human approval capability (no wildcard
landing); compact digest-guarded board projections; tombstoned terminal
trains; observational telemetry. Fail closed everywhere: refused operations
change no authoritative bytes.
"""

from __future__ import annotations
import json
import os
import secrets
import shutil
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay_io  # noqa: E402
import planner_role  # noqa: E402
from relay import landing_policy, train_execution  # noqa: E402

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
TRAINS_DIR = RELAY_DIR / "trains"
LEASES_DIR = RELAY_DIR / "leases"
INBOX_DIR = RELAY_DIR / "inbox"
ALERTS_DIR = RELAY_DIR / "alerts"
APPROVALS_DIR = RELAY_DIR / "approvals"
HISTORY_DIR = RELAY_DIR / "history"
BOARD_FILE = RELAY_DIR / "CURRENT_BOARD.md"
BOARD_JSON = RELAY_DIR / "BOARD.json"
WORK_GRAPH_FILE = RELAY_ROOT / "work-graph.json"
TELEMETRY_FILE = HISTORY_DIR / "telemetry.jsonl"

# Local role credentials live OUTSIDE the synchronized tree (Seam A.4): they
# authenticate a role on THIS machine; they never broaden role permissions.
CRED_DIR = Path(os.environ.get("RELAY_CRED_DIR", str(Path.home() / ".tare-relay-credentials")))

ROLES = ["planner", "plan-auditor", "implementer", "auditor", "policy", "human", "reconciler"]
PROCESS_UUID = str(uuid.uuid4())[:8]


def rebind_root(root: Path | str) -> None:
    """Point every path constant at a different relay root (tests/benchmark)."""
    global RELAY_ROOT, RELAY_DIR, TRAINS_DIR, LEASES_DIR, INBOX_DIR, ALERTS_DIR
    global APPROVALS_DIR, HISTORY_DIR, BOARD_FILE, BOARD_JSON, WORK_GRAPH_FILE, TELEMETRY_FILE
    RELAY_ROOT = Path(root)
    RELAY_DIR = RELAY_ROOT / "relay"
    TRAINS_DIR = RELAY_DIR / "trains"
    LEASES_DIR = RELAY_DIR / "leases"
    INBOX_DIR = RELAY_DIR / "inbox"
    ALERTS_DIR = RELAY_DIR / "alerts"
    APPROVALS_DIR = RELAY_DIR / "approvals"
    HISTORY_DIR = RELAY_DIR / "history"
    BOARD_FILE = RELAY_DIR / "CURRENT_BOARD.md"
    BOARD_JSON = RELAY_DIR / "BOARD.json"
    WORK_GRAPH_FILE = RELAY_ROOT / "work-graph.json"
    TELEMETRY_FILE = HISTORY_DIR / "telemetry.jsonl"
    relay_io.OUTCOMES_DIR = HISTORY_DIR / "outcomes"
    planner_role.rebind_root(RELAY_ROOT)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_hostname() -> str:
    return socket.gethostname()


def ensure_dirs() -> None:
    for d in [RELAY_DIR, TRAINS_DIR, LEASES_DIR, INBOX_DIR, ALERTS_DIR, APPROVALS_DIR, HISTORY_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    for r in ROLES:
        (INBOX_DIR / r).mkdir(parents=True, exist_ok=True)


def telemetry(op: str, **fields: Any) -> None:
    """Observational only (Seam D.2); failure can never advance or block a gate."""
    try:
        TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_iso(), "op": op, "host": get_hostname(), **fields},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_work_graph() -> Dict[str, Any]:
    data = relay_io.read_json(WORK_GRAPH_FILE)
    return data if isinstance(data, dict) else {"nodes": [], "edges": []}


# -----------------------------------------------------------------------------
# Role credentials & lease management (Seam A.4)
# -----------------------------------------------------------------------------

def role_credential_digest(role: str) -> str:
    """Computes a digest of the local role credential to prove locality (U-7D)."""
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    cred = CRED_DIR / f"{role}.key"
    if not cred.exists():
        cred.write_text(secrets.token_hex(32), encoding="utf-8")
    return relay_io.sha256_hex(cred.read_text(encoding="utf-8-sig").strip())


# -----------------------------------------------------------------------------
# TRAIN-21: Multi-host coordination immunity (opt-in admission gate)
#
# Google Drive is an eventually-consistent transport, NOT a lock service. A
# synced heartbeat/lease/hash cannot prove two disconnected hosts did not act
# concurrently. This is the smallest TRUTHFUL boundary: when multi-host safe
# mode is explicitly enabled, every effectful action requires a pre-provisioned,
# machine-local coordinator credential OUTSIDE the synced tree, bound to THIS
# host. Absence / unreadability / host mismatch is a hard refusal — no effect.
# It makes ONE operator-provisioned host the only authorized effectful writer;
# it does NOT provide automatic failover. Default (mode off) = legacy single-host
# behavior, byte-for-byte unchanged.
# -----------------------------------------------------------------------------

COORDINATOR_CRED = "coordinator.json"


def multi_host_enabled() -> bool:
    """True iff multi-host safe mode is explicitly opted in (default: off)."""
    return os.environ.get("RELAY_MULTI_HOST", "").strip().lower() in ("1", "true", "yes", "on")


def _cred_dir_outside_root() -> bool:
    """Canonical-path disjointness (plan correction #2): the credential dir must
    resolve OUTSIDE the synced relay root, so authority can never propagate via
    Drive sync. Different drives / unresolvable paths are treated as outside."""
    try:
        cred = os.path.realpath(str(CRED_DIR))
        root = os.path.realpath(str(RELAY_ROOT))
    except OSError:
        return False
    if cred == root:
        return False
    return not cred.startswith(root + os.sep) and not root.startswith(cred + os.sep)


def coordination_authorized() -> tuple[bool, str]:
    """Pure, read-only admission predicate for effectful actions. Never creates,
    copies, logs, or hashes the credential; returns (allowed, non-secret reason).
    MUST be re-evaluated at every effect boundary — callers never cache it
    (plan correction #1: no in-memory authority latching)."""
    if not multi_host_enabled():
        return True, "single-host mode (multi-host safe mode disabled)"
    if not _cred_dir_outside_root():
        return False, ("multi-host: credential dir resolves INSIDE the synced relay "
                       "root; refusing (authority must not propagate via Drive)")
    cred = Path(CRED_DIR) / COORDINATOR_CRED
    try:
        raw = cred.read_text(encoding="utf-8-sig")
    except OSError:
        return False, (f"multi-host: no local coordinator credential at "
                       f"<CRED_DIR>/{COORDINATOR_CRED}; provision it out-of-band on "
                       f"the authorized host (never auto-created)")
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return False, "multi-host: coordinator credential is malformed JSON"
    if not isinstance(doc, dict):
        return False, "multi-host: coordinator credential is not a JSON object"
    leader = str(doc.get("leaderHost", "")).strip()
    if not leader:
        return False, "multi-host: coordinator credential missing 'leaderHost'"
    if leader != get_hostname():
        return False, (f"multi-host: host mismatch — credential authorizes leader "
                       f"{leader!r}, this host is {get_hostname()!r}")
    return True, "authorized coordinator (local credential, host match)"


def _coord_ok(action: str) -> bool:
    """Gate one effectful entrypoint. Prints a non-secret refusal and telemeters
    on denial; leaves authoritative bytes untouched (caller returns its refusal
    value before any write / ensure_dirs / subprocess)."""
    ok, why = coordination_authorized()
    if not ok:
        print(f"[COORD DENIED] {action}: {why}")
        telemetry("coord.denied", action=action, why=why)
    return ok


def _lease_file(target_id: str) -> Path:
    return LEASES_DIR / f"{target_id}.lease.json"


def acquire_lease(target_id: str, agent_id: str, role: str, ttl_seconds: int = 5400) -> bool:
    if not _coord_ok("acquire_lease"):
        return False
    ensure_dirs()
    lease_file = _lease_file(target_id)
    now_ts = time.time()
    data = relay_io.read_json(lease_file)
    if isinstance(data, dict):
        if now_ts < data.get("expires_ts", 0):
            is_same_owner = (
                data.get("hostname") == get_hostname() and
                data.get("agent_id") == agent_id and
                (data.get("pid") == os.getpid() or data.get("process_uuid") == PROCESS_UUID)
            )
            if not is_same_owner:
                print(f"[LEASE CONFLICT] Target '{target_id}' is held by '{data.get('agent_id')}' "
                      f"on host '{data.get('hostname')}' (pid={data.get('pid')}) until {data.get('expires_iso')}")
                return False
    lease_data = {
        "target_id": target_id, "agent_id": agent_id, "role": role,
        "hostname": get_hostname(),
        "pid": os.getpid(),
        "process_uuid": PROCESS_UUID,
        "credentialDigest": role_credential_digest(role),
        "acquired_iso": now_iso(), "acquired_ts": now_ts,
        "expires_ts": now_ts + ttl_seconds,
        "expires_iso": datetime.fromtimestamp(now_ts + ttl_seconds, timezone.utc).isoformat(),
    }
    relay_io.write_json_atomic(lease_file, lease_data)
    print(f"[LEASE ACQUIRED] '{target_id}' claimed by '{agent_id}' ({role}) on host '{get_hostname()}' (pid={os.getpid()}).")
    telemetry("lease.acquire", target=target_id, role=role)
    update_board()
    return True


def current_lease(target_id: str) -> Optional[Dict[str, Any]]:
    data = relay_io.read_json(_lease_file(target_id))
    if isinstance(data, dict) and time.time() < data.get("expires_ts", 0):
        return data
    return None


def renew_lease(target_id: str, agent_id: str, additional_ttl: int = 5400) -> bool:
    if not _coord_ok("renew_lease"):
        return False
    data = relay_io.read_json(_lease_file(target_id))
    if not isinstance(data, dict):
        print(f"[LEASE ERROR] Cannot renew non-existent lease for '{target_id}'.")
        return False
    is_owner = (
        data.get("agent_id") == agent_id and
        data.get("hostname") in (None, get_hostname()) and
        data.get("credentialDigest") in (None, role_credential_digest(data.get("role", "")))
    )
    if not is_owner:
        print(f"[LEASE ERROR] Agent '{agent_id}' on host '{get_hostname()}' does not own lease for '{target_id}' (held by {data.get('hostname')}).")
        return False
    now_ts = time.time()
    data["expires_ts"] = now_ts + additional_ttl
    data["expires_iso"] = datetime.fromtimestamp(now_ts + additional_ttl, timezone.utc).isoformat()
    data["last_heartbeat"] = now_iso()
    relay_io.write_json_atomic(_lease_file(target_id), data)
    print(f"[LEASE RENEWED] '{target_id}' extended until {data['expires_iso']}.")
    return True


def release_lease(target_id: str, agent_id: Optional[str] = None, force: bool = False) -> bool:
    """Non-owner release REFUSES (falsifier 1) unless force (owner/terminal path)."""
    if not _coord_ok("release_lease"):
        return False
    lease_file = _lease_file(target_id)
    data = relay_io.read_json(lease_file)
    if data is None and not lease_file.exists():
        return False
    if not force and isinstance(data, dict):
        owner_ok = (
            (agent_id is None or agent_id == data.get("agent_id")) and
            data.get("hostname") in (None, get_hostname()) and
            data.get("credentialDigest") in (None, role_credential_digest(data.get("role", "")))
        )
        if not owner_ok:
            print(f"[LEASE REFUSED] '{agent_id}' on '{get_hostname()}' cannot release lease held by '{data.get('agent_id')}' on '{data.get('hostname')}'.")
            return False
    lease_file.unlink(missing_ok=True)
    print(f"[LEASE RELEASED] '{target_id}'")
    update_board()
    return True


# -----------------------------------------------------------------------------
# Seam A: FSM, revision CAS, generalized audit gates
# -----------------------------------------------------------------------------

TERMINAL_STATUS = "LANDED"
TERMINAL_STATUSES = {"LANDED", "RETIRED", "SUPERSEDED"}
MAX_TRAIN_REVISIONS = 12  # Anti-churn circuit breaker: prevents infinite agent loops

# (from, to) -> {"role": required_role, "evidence": gate or None}
FSM: Dict[tuple, Dict[str, Optional[str]]] = {
    ("PLAN_AUDIT_PENDING", "PLAN_APPROVED"): {"role": "plan-auditor", "evidence": "PLAN"},
    ("PLAN_AUDIT_PENDING", "PLAN_REJECTED"): {"role": "plan-auditor", "evidence": None},
    ("PLAN_REJECTED", "PLAN_AUDIT_PENDING"): {"role": "planner", "evidence": None},
    ("PLAN_AUDIT_PENDING", "PLAN_AUDIT_HOLD"): {"role": "plan-auditor", "evidence": None},
    ("PLAN_AUDIT_HOLD", "PLAN_AUDIT_PENDING"): {"role": ("planner", "plan-auditor", "operator", "human_gatekeeper", "auditor"), "evidence": None},
    ("PLAN_APPROVED", "IN_PROGRESS_IMPLEMENTER"): {"role": "implementer", "evidence": "LEASE"},
    ("IN_PROGRESS_IMPLEMENTER", "PLAN_APPROVED"): {"role": ("sentinel", "supervisor", "operator", "implementer"), "evidence": None},
    ("IN_PROGRESS_IMPLEMENTER", "AUDIT_PENDING"): {"role": "implementer", "evidence": "LEASE"},
    ("AUDIT_PENDING", "IN_PROGRESS_AUDITOR"): {"role": "auditor", "evidence": "LEASE"},
    ("IN_PROGRESS_AUDITOR", "AUDIT_PENDING"): {"role": ("sentinel", "supervisor", "operator", "auditor"), "evidence": None},
    ("AUDIT_PENDING", "AUDIT_HOLD"): {"role": "auditor", "evidence": None},
    ("IN_PROGRESS_AUDITOR", "AUDIT_HOLD"): {"role": "auditor", "evidence": None},
    ("AUDIT_HOLD", "AUDIT_PENDING"): {"role": ("implementer", "auditor", "operator", "human_gatekeeper"), "evidence": None},
    ("IN_PROGRESS_AUDITOR", "READY_FOR_HUMAN_APPROVAL"): {"role": "auditor", "evidence": "AUDIT"},
    ("IN_PROGRESS_AUDITOR", "AUDIT_REJECTED"): {"role": "auditor", "evidence": None},
    ("AUDIT_REJECTED", "IN_PROGRESS_IMPLEMENTER"): {"role": "implementer", "evidence": "LEASE"},
    ("AUDIT_PENDING", "READY_FOR_LANDING"): {"role": "policy", "evidence": "ADMISSION"},
    ("IN_PROGRESS_AUDITOR", "READY_FOR_LANDING"): {"role": "policy", "evidence": "ADMISSION"},
    ("READY_FOR_LANDING", "LANDED"): {"role": "policy", "evidence": "CAPABILITY"},
    ("READY_FOR_HUMAN_APPROVAL", "LANDED"): {"role": "human_gatekeeper", "evidence": "CAPABILITY"},
    # Formal Retirement transitions (U-7D closure)
    ("PLAN_AUDIT_PENDING", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("PLAN_AUDIT_HOLD", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("PLAN_APPROVED", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("IN_PROGRESS_IMPLEMENTER", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("AUDIT_PENDING", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("IN_PROGRESS_AUDITOR", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("AUDIT_HOLD", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("AUDIT_REJECTED", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("READY_FOR_HUMAN_APPROVAL", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("READY_FOR_LANDING", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("DEFERRED_BLOCKED_DIRTY_TARGET", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("DEFERRED_BLOCKED_RAW_EXPORT", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
    ("DEFERRED_BLOCKED_CI_REGRESSION_02", "RETIRED"): {"role": ("human_gatekeeper", "operator", "supervisor"), "evidence": None},
}


def train_revision(data: Dict[str, Any]) -> int:
    """Explicit monotonic revision when present, else len(history) (C2)."""
    rev = data.get("revision")
    if isinstance(rev, int):
        return rev
    return len(data.get("history", []))


@contextmanager
def _writer_lock(train_id: str):
    """Train-scoped writer lock (O_EXCL); stale locks (>60s) are broken."""
    lock = TRAINS_DIR / train_id / ".writer.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    else:
        raise RuntimeError(f"writer lock busy for {train_id}")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


_STAGE_REPORTS = {"PLAN": "PLAN_AUDIT_REPORT.md", "AUDIT": "AUDIT_REPORT.md"}
_STAGE_VERDICTS = {"PLAN": ("PLAN_VERDICT: APPROVED",),
                   "AUDIT": ("VERIFIED_PASS", "Audit Recommendation")}
# Reports saved before the markers moved to English stay valid.
_LEGACY_MARKERS = {"Audit Recommendation": "Recomendação da Auditoria"}


def _strict_candidate_context(train_dir: Path, state: Dict[str, Any]) -> tuple:
    """Read exact strict artifacts and reverify the immutable Git object."""

    from relay.candidate_change import (
        CANDIDATE_FILE,
        read_candidate_receipt,
        verify_candidate_receipt,
    )
    from relay.strict_workspace import read_workspace_ready
    from relay.train_execution import DESCRIPTOR_FILE, read_execution_descriptor

    descriptor = read_execution_descriptor(train_dir / DESCRIPTOR_FILE)
    seal = state.get("execution_descriptor")
    if not isinstance(seal, dict) or seal.get("sha256") != descriptor.sha256:
        raise ValueError("DESCRIPTOR_SEAL_MISMATCH")
    receipt = read_candidate_receipt(train_dir / CANDIDATE_FILE)
    worktree_id = receipt.payload.get("worktree_id")
    if not isinstance(worktree_id, str) or not worktree_id:
        raise ValueError("WORKTREE_ID_INVALID")
    ready_file = train_dir / f"WORKSPACE_READY.{worktree_id}.json"
    ready = read_workspace_ready(ready_file)
    ready_raw = ready_file.read_bytes()
    if (
        ready.get("descriptor_sha256") != descriptor.sha256
        or ready.get("base_commit") != descriptor.base_commit
        or ready.get("repository") != descriptor.execution_repository
        or ready.get("worktree_id") != worktree_id
        or receipt.payload.get("workspace_ready_sha256")
        != relay_io.sha256_hex(ready_raw)
    ):
        raise ValueError("WORKSPACE_READY_MISMATCH")
    repository_root = ready.get("repository_root")
    if not isinstance(repository_root, str) or not repository_root:
        raise ValueError("EXECUTION_REPOSITORY_UNAVAILABLE")
    verify_candidate_receipt(
        receipt, descriptor, repository_root=repository_root
    )
    return descriptor, receipt, repository_root, ready


def audit_valid(train_id: str, stage: str = "PLAN", revision: Optional[int] = None) -> tuple:
    """Generalized anti-rubber-stamp gate: the stage report must exist, name
    every task id, carry the stage verdict marker(s), and (AUDIT stage) bind
    the current packet digest. Mechanical or stale reports fail closed."""
    train_dir = TRAINS_DIR / train_id
    report = train_dir / _STAGE_REPORTS[stage]
    if not report.exists():
        return False, f"{report.name} missing - stage {stage} was not audited"
    data = relay_io.read_json(train_dir / "STATE.json")
    if not isinstance(data, dict):
        return False, "unreadable train state"
    text = relay_io.read_text(report)
    missing = [t for t in data.get("task_ids", []) if t not in text]
    if missing:
        return False, (f"{report.name} never mentions task(s) {missing} - "
                       "subject-mismatched/stale report (rubber stamp), fail closed")
    for marker in _STAGE_VERDICTS[stage]:
        if marker not in text and _LEGACY_MARKERS.get(marker, marker) not in text:
            return False, f"{report.name} lacks required verdict marker {marker!r}"
    if stage == "AUDIT":
        packet = train_dir / "PACKET.md"
        if packet.exists():
            digest = relay_io.sha256_hex(packet.read_bytes())
            if digest[:12] not in text.lower():
                return False, (f"{report.name} does not bind the current packet digest "
                               f"{digest[:12]} (stale or unbound audit)")
        state = relay_io.read_json(train_dir / "STATE.json") or {}
        if state.get("execution_descriptor") is not None:
            try:
                descriptor, receipt, repository_root, _ready = _strict_candidate_context(
                    train_dir, state
                )
            except Exception as exc:
                return False, f"strict candidate invalid: {exc}"
            candidate = str(receipt.payload["candidate_commit"])
            if candidate[:12] not in text.lower():
                return False, (
                    f"{report.name} does not bind strict candidate {candidate[:12]}"
                )
    return True, f"{report.name} valid for stage {stage}"


def plan_audit_valid(train_id: str) -> tuple:
    """Back-compat wrapper (implementer-claim guard, owner ruling 2026-08-16)."""
    ok, why = audit_valid(train_id, "PLAN")
    if not ok and "verdict marker" in why:
        # Claims are additionally guarded at transition time; the claim guard
        # keeps its original contract (report exists + names all tasks).
        train_dir = TRAINS_DIR / train_id
        data = relay_io.read_json(train_dir / "STATE.json") or {}
        text = relay_io.read_text(train_dir / "PLAN_AUDIT_REPORT.md")
        missing = [t for t in data.get("task_ids", []) if t not in text]
        if not missing:
            return True, "plan audit report references all train tasks"
    if ok:
        return True, "plan audit report references all train tasks"
    return ok, why


def admission_valid(train_id: str) -> tuple[bool, str]:
    """Recompute the exact smart-admission bundle for the current candidate."""

    train_dir = TRAINS_DIR / train_id
    state = relay_io.read_json(train_dir / "STATE.json")
    if not isinstance(state, dict) or state.get("execution_descriptor") is None:
        return False, "smart admission is available only for strict candidate trains"
    try:
        descriptor, receipt, _repository_root, _ready = _strict_candidate_context(
            train_dir, state
        )

        def _audit_passed() -> bool:
            return bool(audit_valid(train_id, "AUDIT")[0])

        verdict = landing_policy.verify_admission_bundle(
            train_dir,
            roster_path=RELAY_DIR / "round_table_roster.json",
            train_id=train_id,
            descriptor_sha256=descriptor.sha256,
            candidate_sha256=receipt.sha256,
            audit_passed_validator=_audit_passed,
        )
    except Exception as exc:
        return False, f"smart admission invalid: {exc}"
    return True, f"{landing_policy.VERDICT_FILE} {verdict.sha256[:12]} admitted"


def prepare_admission_verdict(
    train_id: str, *, audit_mode: str | None = None
) -> landing_policy.AdmissionVerdict:
    """Evaluate existing strict receipts; only an ADMITTED result is persisted."""

    train_dir = TRAINS_DIR / train_id
    evidence = landing_policy.read_evidence(train_dir / landing_policy.EVIDENCE_FILE)
    implementer = landing_policy.read_identity(
        train_dir / landing_policy.IMPLEMENTER_IDENTITY_FILE
    )
    mode = audit_mode or os.environ.get(
        "TARE_LANDING_AUDIT_MODE", landing_policy.DEFAULT_AUDIT_MODE
    )
    policy = landing_policy.LandingPolicy(mode, True)
    auditor = None
    audit_passed = None
    audit_sha256 = ""
    audit_identity_file = train_dir / landing_policy.AUDITOR_IDENTITY_FILE
    audit_report = train_dir / "AUDIT_REPORT.md"
    if audit_identity_file.exists() or audit_report.exists():
        if not (audit_identity_file.exists() and audit_report.exists()):
            raise landing_policy.LandingPolicyRejected("AUDIT_EVIDENCE_INCOMPLETE")
        auditor = landing_policy.read_identity(audit_identity_file)
        audit_sha256 = relay_io.sha256_hex(audit_report.read_bytes())
        audit_passed = bool(audit_valid(train_id, "AUDIT")[0])
    verdict = landing_policy.evaluate_admission(
        evidence,
        implementer,
        policy=policy,
        auditor=auditor,
        audit_passed=audit_passed,
        audit_sha256=audit_sha256,
    )
    if verdict.verdict == "ADMITTED":
        path = train_dir / landing_policy.VERDICT_FILE
        if path.exists():
            if landing_policy.read_verdict(path).canonical_bytes != verdict.canonical_bytes:
                raise landing_policy.LandingPolicyRejected("VERDICT_CREATE_ONLY_CONFLICT")
        else:
            landing_policy.write_verdict(path, verdict)
    return verdict


def materialize_admission_evidence(train_id: str) -> landing_policy.LandingEvidence:
    """Requery Work, SpecGraph, and Git for the exact current strict candidate."""

    train_dir = TRAINS_DIR / train_id
    state = relay_io.read_json(train_dir / "STATE.json")
    if not isinstance(state, dict):
        raise landing_policy.LandingPolicyRejected("TRAIN_STATE_UNAVAILABLE")
    descriptor, receipt, repository_root, _ready = _strict_candidate_context(
        train_dir, state
    )
    work_graph = Path(descriptor.work_graph)
    if not work_graph.is_absolute():
        work_graph = RELAY_ROOT / work_graph
    library_manifest = Path(descriptor.library_manifest)
    if not library_manifest.is_absolute():
        library_manifest = RELAY_ROOT / library_manifest
    from relay.landing_evidence import materialize_landing_evidence

    return materialize_landing_evidence(
        train_dir=train_dir,
        train_id=train_id,
        descriptor=descriptor,
        candidate_receipt=receipt,
        repository_root=repository_root,
        work_graph=work_graph,
        library_manifest=library_manifest,
    )


def _evidence_gate(train_id: str, gate: Optional[str], agent_id: str, role: str) -> tuple:
    if gate is None:
        return True, "no evidence gate"
    if gate == "LEASE":
        lease = current_lease(train_id)
        if not lease:
            return False, "no current lease (missing/expired)"
        if lease.get("agent_id") != agent_id or lease.get("role") != role:
            return False, (f"lease held by {lease.get('agent_id')!r} as {lease.get('role')!r}, "
                           f"not {agent_id!r} as {role!r}")
        return True, "current lease verified"
    if gate in ("PLAN", "AUDIT"):
        return audit_valid(train_id, gate)
    if gate == "ADMISSION":
        return admission_valid(train_id)
    if gate == "CAPABILITY":
        return False, "LANDED is reachable only through land_train's bound capability"
    return False, f"unknown evidence gate {gate!r}"


def transition_allowed(old: str, new: str, role: str) -> tuple:
    if old in TERMINAL_STATUSES:
        return False, f"{old} is terminal"
    rule = FSM.get((old, new))
    if rule is None:
        return False, f"illegal transition {old!r} -> {new!r} (not in FSM)"
    need = rule["role"]
    allowed_roles = need if isinstance(need, tuple) else (need,)
    if role not in allowed_roles:
        return False, f"transition {old!r}->{new!r} requires role {allowed_roles}, got {role!r}"
    return True, "allowed"


def transition_train(train_id: str, new_status: str, next_role: str, agent_id: str,
                     role: str, notes: str, expected_revision: Optional[int] = None,
                     _capability_ok: bool = False) -> bool:
    """FSM + revision-CAS transition under the train writer lock. Refusals
    change no authoritative bytes."""
    if not _coord_ok("transition_state"):
        return False
    ensure_dirs()
    state_file = TRAINS_DIR / train_id / "STATE.json"
    if not state_file.exists():
        print(f"[ERROR] Train '{train_id}' does not exist.")
        return False
    try:
        with _writer_lock(train_id):
            data = relay_io.read_json(state_file)
            if not isinstance(data, dict):
                print(f"[REFUSED] unreadable STATE.json for '{train_id}' (fails closed)")
                return False
            old_status = str(data.get("status"))
            rev = train_revision(data)
            if expected_revision is not None and rev != expected_revision:
                print(f"[CONFLICT] {train_id}: expected revision {expected_revision}, "
                      f"current is {rev} (concurrent transition won)")
                telemetry("transition.conflict", train=train_id, expected=expected_revision, actual=rev)
                return False
            ok, why = transition_allowed(old_status, new_status, role)
            if not ok:
                print(f"[REFUSED] {train_id}: {why}")
                telemetry("transition.refused", train=train_id, frm=old_status, to=new_status, why=why)
                return False
            gate = FSM[(old_status, new_status)]["evidence"]
            if gate == "CAPABILITY" and _capability_ok:
                gate = None
            ok, why = _evidence_gate(train_id, gate, agent_id, role)
            if not ok:
                print(f"[REFUSED] {train_id}: evidence gate {gate}: {why}")
                telemetry("transition.refused", train=train_id, frm=old_status, to=new_status, why=why)
                return False
            if new_status == "PLAN_APPROVED":
                descriptor_file = state_file.parent / train_execution.DESCRIPTOR_FILE
                if descriptor_file.exists():
                    try:
                        descriptor = train_execution.read_execution_descriptor(
                            descriptor_file
                        )
                        descriptor_graph = (Path(descriptor.work_graph) if Path(descriptor.work_graph).is_absolute()
                                            else Path(WORK_GRAPH_FILE))
                        if (data.get('graph_path') and
                                Path(data['graph_path']).resolve() != descriptor_graph.resolve()):
                            raise train_execution.ExecutionDescriptorRejected('WORK_GRAPH_MISMATCH')
                        train_execution.validate_descriptor_for_train(
                            descriptor,
                            train_id=train_id,
                            task_ids=data.get("task_ids", []),
                            work_graph_file=descriptor_graph,
                        )
                    except train_execution.ExecutionDescriptorRejected as exc:
                        why = str(exc)
                        print(
                            f"[REFUSED] {train_id}: execution descriptor: {why}"
                        )
                        telemetry(
                            "transition.refused",
                            train=train_id,
                            frm=old_status,
                            to=new_status,
                            why=why,
                        )
                        return False
                    report = relay_io.read_text(
                        state_file.parent / "PLAN_AUDIT_REPORT.md"
                    ).lower()
                    if descriptor.sha256[:12] not in report:
                        print(
                            f"[REFUSED] {train_id}: plan audit does not bind "
                            "the execution descriptor digest"
                        )
                        return False
                    sealed = data.get("execution_descriptor")
                    expected_seal = {
                        "schema": descriptor.schema,
                        "file": train_execution.DESCRIPTOR_FILE,
                        "sha256": descriptor.sha256,
                    }
                    if sealed is not None and sealed != expected_seal:
                        print(
                            f"[REFUSED] {train_id}: execution descriptor seal "
                            "is create-only"
                        )
                        return False
                    data["execution_descriptor"] = expected_seal
            data["status"] = new_status
            data["current_role"] = next_role
            data["updated_at"] = now_iso()
            data["revision"] = rev + 1
            data.setdefault("history", []).append({
                "timestamp": now_iso(), "from_status": old_status, "to_status": new_status,
                "agent": agent_id, "role": role, "hostname": get_hostname(), "note": notes,
            })
            relay_io.write_json_atomic(state_file, data)
    except RuntimeError as exc:
        print(f"[REFUSED] {train_id}: {exc}")
        return False

    # Notifications: display subject + typed envelope; terminal transitions
    # emit no inbox message (falsifier 12).
    if new_status != TERMINAL_STATUS and next_role in ROLES:
        send_inbox_msg(next_role, f"Train {train_id}: {old_status} -> {new_status}", {
            "train_id": train_id, "status": new_status, "agent": agent_id, "notes": notes,
        }, required_action=_action_for(new_status), train_revision=rev + 1)
    if new_status == "READY_FOR_HUMAN_APPROVAL":
        create_approval_request(train_id, relay_io.read_json(state_file) or {})
    elif new_status == "READY_FOR_LANDING":
        create_policy_capability(train_id, relay_io.read_json(state_file) or {})
    update_board()
    telemetry("transition.ok", train=train_id, frm=old_status, to=new_status, rev=rev + 1)
    print(f"[TRAIN TRANSITION] {train_id}: {old_status} -> {new_status} (Assigned: {next_role})")
    return True


def _action_for(status: str) -> str:
    return {"PLAN_AUDIT_PENDING": "plan-audit", "AUDIT_PENDING": "code-audit",
            "PLAN_APPROVED": "implement", "AUDIT_REJECTED": "remediate"}.get(status, "notify")


# -----------------------------------------------------------------------------
# Guards carried over (owner rulings 2026-08-16)
# -----------------------------------------------------------------------------

def active_task_overlap(task_ids: List[str], exclude_train: Optional[str] = None, *, graph_path=None) -> List[str]:
    ensure_dirs()
    conflicts = []
    for td in TRAINS_DIR.iterdir():
        sf = td / "STATE.json"
        if td.name == exclude_train or not sf.exists():
            continue
        data = relay_io.read_json(sf)
        if not isinstance(data, dict) or data.get("status") in TERMINAL_STATUSES or data.get("tombstone") is True:
            continue
        if Path(data.get("graph_path") or WORK_GRAPH_FILE).resolve() != Path(graph_path or WORK_GRAPH_FILE).resolve():
            continue
        for tid in data.get("task_ids", []):
            if tid in task_ids:
                conflicts.append(f"{tid} (held by {td.name}, status {data.get('status')})")
    return conflicts


def init_train(train_id: str, title: str, task_ids: List[str], planner_agent: str, *, graph_path=None) -> Path:
    if not _coord_ok("init_train"):
        raise SystemExit(1)
    ensure_dirs()
    fenced_ok, why = planner_role.enforce_fence(planner_agent)
    if not fenced_ok:
        print(f"[REFUSED] init-train: planner fence: {why}")
        raise SystemExit(1)
    conflicts = active_task_overlap(task_ids, graph_path=graph_path)
    if conflicts:
        print("[REFUSED] init-train: task(s) already owned by an active train (fail closed):")
        for c in conflicts:
            print(f"  - {c}")
        raise SystemExit(1)
    train_path = TRAINS_DIR / train_id
    train_path.mkdir(parents=True, exist_ok=True)
    (train_path / "evidence").mkdir(parents=True, exist_ok=True)

    graph = load_work_graph() if graph_path is None else relay_io.read_json(graph_path)
    if not isinstance(graph, dict):
        raise ValueError("WORK_GRAPH_UNAVAILABLE")
    nodes_map = {n["id"]: n for n in graph.get("nodes", [])}
    tasks_section = ""
    for tid in task_ids:
        n = nodes_map.get(tid, {})
        crit_md = "\n".join(f"    - [ ] {c}" for c in n.get("exit_criteria",
                            ["Complete implementation and verify tests."]))
        
        prov_md = ""
        refs = n.get("source_refs", [])
        if refs:
            prov_md += f"- **Source References:** {', '.join(map(str, refs))}\n"
        claims = n.get("source_claim_ids", [])
        if claims:
            prov_md += f"- **Source Claims:** {', '.join(map(str, claims))}\n"
        details = n.get("source_details", [])
        if details:
            prov_md += "- **Source Details:**\n"
            for sd in details:
                if isinstance(sd, dict):
                    prov_md += f"  - `{sd.get('id', 'ref')}`: {sd.get('title', '')} (path: `{sd.get('path', 'n/a')}`)\n"
                else:
                    prov_md += f"  - {sd}\n"

        tasks_section += (f"### Task `{tid}`: {n.get('title', tid)}\n"
                          f"- **Summary:** {n.get('summary', 'No summary provided.')}\n"
                          f"- **Cluster:** `{n.get('cluster', 'general')}` | "
                          f"**Priority:** `{n.get('priority', 'P1')}` | "
                          f"**Horizon:** `{n.get('horizon', 'H0')}`\n"
                          f"- **Definitions of Done (`exit_criteria`):**\n{crit_md}\n"
                          f"{prov_md}\n")

    grounding_section = "\n---\n\n## 3. Declared Repository Scopes\n"
    for tid in task_ids:
        n = nodes_map.get(tid, {})
        scopes = n.get("repository_scopes", [])
        grounding_section += f"\n### Task `{tid}`\n"
        if not scopes:
            grounding_section += "- Repository scope not declared for this task.\n"
        else:
            for scope in scopes:
                grounding_section += f"- **Repository:** `{scope.get('repository', 'unknown')}`\n"
                tps = scope.get("target_paths", [])
                if tps:
                    grounding_section += f"- **Target Paths:** {', '.join(f'`{tp}`' for tp in tps)}\n"
                grefs = scope.get("grounding_refs", [])
                if grefs:
                    grounding_section += f"- **Grounding References:** {', '.join(f'`{g}`' for g in grefs)}\n"
                tsyms = scope.get("target_symbols", [])
                if tsyms:
                    grounding_section += f"- **Target Symbols:** {', '.join(f'`{s}`' for s in tsyms)}\n"

    state_data = {
        "train_id": train_id, "graph_path": str(Path(graph_path or WORK_GRAPH_FILE).resolve()), "title": title, "task_ids": task_ids,
        "status": "PLAN_AUDIT_PENDING", "current_role": "plan-auditor",
        "revision": 1,
        "created_by": planner_agent, "created_at": now_iso(), "updated_at": now_iso(),
        "history": [{"timestamp": now_iso(), "action": "TRAIN_INITIALIZED",
                     "agent": planner_agent, "role": "planner", "hostname": get_hostname(),
                     "note": f"Initialized execution train with {len(task_ids)} tasks."}],
    }
    relay_io.write_json_atomic(train_path / "STATE.json", state_data)
    relay_io.write_text_atomic(train_path / "PACKET.md", f"""# Implementation Train: {train_id} — {title}

- **Train ID:** `{train_id}`
- **Planner Agent:** `{planner_agent}`
- **Created At:** `{now_iso()}`
- **Current Status:** `PLAN_AUDIT_PENDING` (Next role: `plan-auditor`)
- **Included Tasks:** {', '.join(f'`{t}`' for t in task_ids)}

> Process gate (owner ruling 2026-08-16): this packet starts at
> `PLAN_AUDIT_PENDING`. An EXTERNAL plan-auditor reviews it against CURRENT,
> corrects it in place (appending `## Plan audit corrections`), writes
> `PLAN_AUDIT_REPORT.md` naming every task id and containing
> `PLAN_VERDICT: APPROVED`, then dispatches to the implementer.

---

## 1. Executive Scope & Architectural Intent
This release train aggregates the following critical-path tasks from the canonical work graph for coordinated execution, adversarial audit, and atomic landing.

---

## 2. Target Tasks & Contractual Definitions of Done
{tasks_section}
{grounding_section}
""")
    send_inbox_msg("plan-auditor", f"New Train Awaiting Plan Audit: {train_id} ({title})", {
        "train_id": train_id, "graph_path": str(Path(graph_path or WORK_GRAPH_FILE).resolve()), "title": title, "tasks": task_ids,
        "packet_path": f"relay/trains/{train_id}/PACKET.md",
    }, required_action="plan-audit", train_revision=1)
    update_board()
    print(f"[TRAIN INITIALIZED] {train_id} ({len(task_ids)} tasks) -> {train_path}")
    return train_path


# -----------------------------------------------------------------------------
# Inbox, alerts, approvals (Seam B envelopes; Seam A.5 scoped capability)
# -----------------------------------------------------------------------------

def send_inbox_msg(role: str, subject: str, payload: Dict[str, Any],
                   required_action: str = "notify", train_revision: int = 0) -> None:
    ensure_dirs()
    target_box = INBOX_DIR / role
    target_box.mkdir(parents=True, exist_ok=True)
    msg_id = f"MSG_{int(time.time() * 1000)}_{role}.json"
    relay_io.write_json_atomic(target_box / msg_id, {
        "id": msg_id, "role": role, "subject": subject, "timestamp": now_iso(),
        "hostname": get_hostname(), "payload": payload,
        "envelope": relay_io.make_envelope(
            train_id=str(payload.get("train_id") or payload.get("initiative_id") or ""),
            revision=train_revision, required_action=required_action, role=role),
    })


def raise_alert(title: str, severity: str, details: str, agent_id: str,
                train_id: Optional[str] = None) -> str:
    ensure_dirs()
    alert_id = f"ALERT_{int(time.time() * 1000)}"
    relay_io.write_text_atomic(ALERTS_DIR / f"{alert_id}.md", f"""# [HUMAN INTERVENTION REQUIRED] {title}

- **Alert ID:** `{alert_id}`
- **Severity:** `{severity.upper()}`
- **Raised By:** `{agent_id}` on `{get_hostname()}`
- **Timestamp:** `{now_iso()}`
- **Associated Train:** `{train_id or 'NONE'}`

---

## [CRITICAL] Problem Summary
{details}

---

## Human Resolution Options
1. Review logs in `relay/trains/{train_id or ''}`.
2. Resolve the underlying conflict or code failure.
3. Close this alert: `python relay/relay_mesh.py resolve-alert --id {alert_id}`
""")
    send_inbox_msg("human", f"[ALERT {severity.upper()}] {title}", {
        "alert_id": alert_id, "severity": severity, "details": details, "train_id": train_id})
    update_board()
    print(f"\n[CRITICAL ALERT RAISED] {title} -> {ALERTS_DIR / (alert_id + '.md')}\n")
    return alert_id


def resolve_alert(alert_id: str, resolver_agent: str = "human", note: str = "Resolved by human") -> bool:
    ensure_dirs()
    alert_file = ALERTS_DIR / f"{alert_id}.md"
    if alert_file.exists():
        alert_file.rename(HISTORY_DIR / f"{alert_id}_RESOLVED.md")
        print(f"[ALERT RESOLVED] {alert_id}")
        update_board()
        return True
    print(f"[ERROR] Alert '{alert_id}' not found.")
    return False


def _capability_file(train_id: str) -> Path:
    return APPROVALS_DIR / f"CAPABILITY_{train_id}.json"


def create_approval_request(train_id: str, train_data: Dict[str, Any]) -> None:
    """Mint the train-scoped, single-use human approval capability (Seam A.5).

    Bound to (train_id, ready_revision, packet/implementation/audit digests,
    nonce). Single use comes from revision/status CAS: landing advances both,
    so any replay of this capability goes stale — no consumed-nonce store."""
    ensure_dirs()
    train_dir = TRAINS_DIR / train_id
    nonce = uuid.uuid4().hex

    def _digest_of(name: str) -> str:
        p = train_dir / name
        return relay_io.sha256_hex(p.read_bytes()) if p.exists() else ""

    capability = {
        "trainId": train_id,
        "authoritySource": "HUMAN_OPERATOR",
        "readyRevision": train_revision(train_data),
        "packetDigest": _digest_of("PACKET.md"),
        "implementationDigest": _digest_of("DIFF.patch"),
        "auditDigest": _digest_of("AUDIT_REPORT.md"),
        "candidateDigest": _digest_of("CANDIDATE_CHANGE.json"),
        "admissionDigest": "",
        "landingEvidenceDigest": "",
        "implementerIdentityDigest": "",
        "auditorIdentityDigest": "",
        "nonce": nonce,
        "mintedAt": now_iso(),
    }
    relay_io.write_json_atomic(_capability_file(train_id), capability)
    tasks = train_data.get("task_ids", [])
    relay_io.write_text_atomic(APPROVALS_DIR / f"APPROVAL_REQUEST_{train_id}.md", f"""# [RELEASE TRAIN APPROVAL REQUEST] {train_id} — {train_data.get('title', '')}

- **Train ID:** `{train_id}`
- **Status:** `AUDITED — READY FOR 1-CLICK HUMAN SIGN-OFF`
- **Timestamp:** `{now_iso()}`
- **Target Tasks:** {', '.join(f'`{t}`' for t in tasks)}
- **Ready Revision:** `{capability['readyRevision']}`
- **Approval Nonce:** `{nonce}`

---

## Verified Evidence
- Packet: `relay/trains/{train_id}/PACKET.md` (sha256 `{capability['packetDigest'][:16]}`)
- Audit: `relay/trains/{train_id}/AUDIT_REPORT.md` (sha256 `{capability['auditDigest'][:16]}`)

## 1-Click Human Approval (at the relay machine)
```bash
python relay/relay_mesh.py land --train {train_id}
```
Mobile path: create `relay/approvals/APPROVED_{train_id}.md` CONTAINING the
approval nonce above. Generic approval markers land nothing.
""")


def create_policy_capability(train_id: str, train_data: Dict[str, Any]) -> None:
    """Mint a revision- and evidence-bound machine capability; no human nonce."""

    ensure_dirs()
    train_dir = TRAINS_DIR / train_id

    def _digest_of(name: str) -> str:
        path = train_dir / name
        return relay_io.sha256_hex(path.read_bytes()) if path.exists() else ""

    capability = {
        "trainId": train_id,
        "authoritySource": "SMART_POLICY",
        "readyRevision": train_revision(train_data),
        "packetDigest": _digest_of("PACKET.md"),
        "implementationDigest": _digest_of("DIFF.patch"),
        "auditDigest": _digest_of("AUDIT_REPORT.md"),
        "candidateDigest": _digest_of("CANDIDATE_CHANGE.json"),
        "admissionDigest": _digest_of(landing_policy.VERDICT_FILE),
        "landingEvidenceDigest": _digest_of(landing_policy.EVIDENCE_FILE),
        "implementerIdentityDigest": _digest_of(
            landing_policy.IMPLEMENTER_IDENTITY_FILE
        ),
        "auditorIdentityDigest": _digest_of(landing_policy.AUDITOR_IDENTITY_FILE),
        "nonce": "",
        "mintedAt": now_iso(),
    }
    required = (
        "candidateDigest",
        "admissionDigest",
        "landingEvidenceDigest",
        "implementerIdentityDigest",
    )
    if any(not capability[name] for name in required):
        raise RuntimeError("smart policy capability requires complete admission receipts")
    relay_io.write_json_atomic(_capability_file(train_id), capability)


def admit_train(train_id: str, *, auto_land: bool = True) -> bool:
    """Apply the stored smart verdict and optionally settle without human latency."""

    state = relay_io.read_json(TRAINS_DIR / train_id / "STATE.json")
    if not isinstance(state, dict):
        print(f"[ERROR] Train '{train_id}' not found or unreadable.")
        return False
    old = state.get("status")
    if old not in {"AUDIT_PENDING", "IN_PROGRESS_AUDITOR"}:
        print(f"[REFUSED] admit: '{train_id}' is {old!r}, not admission-ready.")
        return False
    if not transition_train(
        train_id,
        "READY_FOR_LANDING",
        "policy",
        "smart-admission",
        "policy",
        "Immutable smart checks admitted the exact candidate.",
    ):
        return False
    return land_train(train_id, signoff="SMART_POLICY") if auto_land else True


@contextmanager
def _landing_lock():
    """Global atomic landing lock to serialize multi-train landings and prevent work-graph.json race conditions."""
    lock_file = HISTORY_DIR / ".landing.lock"
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}_{time.time()}".encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Break stale lock if older than 45s
                try:
                    if lock_file.stat().st_mtime < time.time() - 45:
                        lock_file.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                raise RuntimeError("Landing lock busy; another train is currently landing into work-graph.json.")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass


def land_train(train_id: str, signoff: str = "HUMAN_SIGN_OFF", nonce: Optional[str] = None) -> bool:
    """Scoped, capability-bound landing (Seam A.5). Refusals change nothing."""
    with _landing_lock():
        return _land_train_locked(train_id, signoff, nonce)


def _land_train_locked(train_id: str, signoff: str = "HUMAN_SIGN_OFF", nonce: Optional[str] = None) -> bool:
    if not _coord_ok("land"):
        return False
    ensure_dirs()
    train_dir = TRAINS_DIR / train_id
    state_file = train_dir / "STATE.json"
    data = relay_io.read_json(state_file)
    if not isinstance(data, dict):
        print(f"[ERROR] Train '{train_id}' not found or unreadable.")
        return False
    status = data.get("status")
    if status not in {"READY_FOR_HUMAN_APPROVAL", "READY_FOR_LANDING"}:
        print(f"[REFUSED] land: '{train_id}' is {status!r}, not ready for landing.")
        return False
    policy_authority = status == "READY_FOR_LANDING"
    capability = relay_io.read_json(_capability_file(train_id))
    if not isinstance(capability, dict):
        print(f"[REFUSED] land: no approval capability for '{train_id}'.")
        return False
    if capability.get("trainId") != train_id:
        print("[REFUSED] land: capability train mismatch (an approval for train A cannot land train B).")
        return False
    expected_source = "SMART_POLICY" if policy_authority else "HUMAN_OPERATOR"
    if capability.get("authoritySource") != expected_source:
        print("[REFUSED] land: capability authority source mismatch.")
        return False
    if capability.get("readyRevision") != train_revision(data):
        print(f"[REFUSED] land: capability bound to revision {capability.get('readyRevision')}, "
              f"current is {train_revision(data)} (stale/replayed capability).")
        return False
    if policy_authority and nonce is not None:
        print("[REFUSED] land: smart policy capability does not accept a human nonce.")
        return False
    if not policy_authority and nonce is not None and nonce != capability.get("nonce"):
        print("[REFUSED] land: approval nonce mismatch.")
        return False
    if not policy_authority and nonce is None:
        # Local 1-click: the human at this machine; require the local human
        # gatekeeper credential (outside the synced tree) to exist.
        role_credential_digest("human_gatekeeper")
    verified_artifacts = {}
    packet = train_dir / "PACKET.md"
    if capability.get("packetDigest") and not packet.exists():
        print("[REFUSED] land: PACKET.md missing after approval.")
        return False
    if packet.exists() and capability.get("packetDigest") and \
            relay_io.sha256_hex(packet.read_bytes()) != capability.get("packetDigest"):
        print("[REFUSED] land: PACKET.md changed after approval (digest mismatch).")
        return False
    if packet.exists() and capability.get("packetDigest"):
        verified_artifacts["PACKET.md"] = capability["packetDigest"]
    for artifact, digest_key in (
        ("DIFF.patch", "implementationDigest"),
        ("AUDIT_REPORT.md", "auditDigest"),
        ("CANDIDATE_CHANGE.json", "candidateDigest"),
        (landing_policy.VERDICT_FILE, "admissionDigest"),
        (landing_policy.EVIDENCE_FILE, "landingEvidenceDigest"),
        (landing_policy.IMPLEMENTER_IDENTITY_FILE, "implementerIdentityDigest"),
        (landing_policy.AUDITOR_IDENTITY_FILE, "auditorIdentityDigest"),
    ):
        expected_digest = capability.get(digest_key)
        path = train_dir / artifact
        if expected_digest and (
            not path.exists()
            or relay_io.sha256_hex(path.read_bytes()) != expected_digest
        ):
            print(
                f"[REFUSED] land: {artifact} changed after approval "
                "(digest mismatch)."
            )
            return False
        if expected_digest:
            verified_artifacts[artifact] = expected_digest

    strict_cleanup_lease = None
    if data.get("execution_descriptor") is not None:
        try:
            if policy_authority:
                admission_ok, admission_why = admission_valid(train_id)
                if not admission_ok:
                    raise ValueError(admission_why)
            else:
                audit_ok, audit_why = audit_valid(train_id, "AUDIT")
                if not audit_ok:
                    raise ValueError(audit_why)
            descriptor, receipt, repository_root, ready = _strict_candidate_context(
                train_dir, data
            )
            from relay.candidate_change import land_recorded_candidate
            from compute_plane.workspace import WorkspaceLease

            strict_cleanup_lease = WorkspaceLease(
                worktree_id=receipt.payload["worktree_id"],
                repository_root=Path(repository_root).resolve(),
                root=Path(ready["destination"]).resolve(),
                git_common_dir=Path(ready["git_common_dir"]).resolve(),
                git_admin_dir=Path(ready["git_admin_dir"]).resolve(),
                base_commit=descriptor.base_commit,
                pinned_commit=receipt.payload["candidate_commit"],
            )
        except Exception as exc:
            print(f"[REFUSED] land: strict candidate invalid: {exc}")
            return False

    graph_file = RELAY_ROOT / descriptor.work_graph if strict_cleanup_lease is not None else WORK_GRAPH_FILE
    task_ids = data.get("task_ids", [])
    if not task_ids:
        print(f"[REFUSED] land: '{train_id}' has no task_ids registered.")
        return False

    try:
        import copy
        from graph_backlog.core import WorkGraph
        from graph_backlog.jsonutil import graph_lock, compute_revision_hash, UsageError
        from graph_backlog.validation import (
            validate_completion_evidence, completion_evidence_issues,
        )

        # Share the owner's lock with backlog_record, not just other trains.
        # Keep the raw graph: legacy OS fixtures need no full normalization.
        with graph_lock(graph_file):
            before = relay_io.read_json(graph_file)
            if not isinstance(before, dict) or not isinstance(before.get("nodes"), list):
                raise UsageError("work graph unreadable (fails closed)")
            if (not isinstance(task_ids, list)
                    or any(not isinstance(tid, str) or not tid.strip() for tid in task_ids)
                    or len(set(task_ids)) != len(task_ids)):
                raise UsageError("invalid or duplicate task_ids")
            graph_raw = copy.deepcopy(before)
            nodes = {}
            for node in graph_raw["nodes"]:
                if isinstance(node, dict) and node.get("id") in task_ids:
                    tid = node["id"]
                    if tid in nodes:
                        raise UsageError(f"ambiguous graph target '{tid}'")
                    nodes[tid] = node
            binding = {
                "train": train_id, "readyRevision": capability["readyRevision"],
                "authority": expected_source,
                "capability_sha256": relay_io.sha256_hex(json.dumps(
                    capability, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")),
                "artifacts": verified_artifacts,
            }
            if strict_cleanup_lease is not None:
                binding.update(descriptor_sha256=descriptor.sha256,
                               candidate_sha256=receipt.sha256,
                               candidate_commit=receipt.payload["candidate_commit"])
            reference = "relay-signoff:" + json.dumps(binding, sort_keys=True, separators=(",", ":"))
            reason = (
                "C = manual verification or reviewer sign-off: records only the admitted "
                f"{expected_source} capability/verdict sign-off for train {train_id}, "
                f"ready revision {capability['readyRevision']}. This is not independent "
                "verification of code quality. EXECUTOR_OBSERVATION remains ungraded/"
                "NOT_VERIFIED; READY, existing files, tests-passed statements and train "
                "markers do not establish A/B evidence."
            )
            changed_targets = []
            timestamp = now_iso()
            for tid in task_ids:
                if tid not in nodes:
                    nodes[tid] = {
                        "id": tid, "title": f"Task {tid} (admitted via train {train_id})",
                        "summary": f"Admitted sign-off via release train {train_id}.",
                        "priority": "P1", "horizon": "H1",
                        "cluster": "RELAY_GOVERNANCE",
                        "admission_state": "ISSUE_INTAKE", "authority_required": "none",
                        "bounded_contexts": ["Relay Governance / Multi-Agent Mesh"],
                        "canonical_id": None, "canonical_revision": None, "canonical_system": None,
                        "confidence": {"label": "high", "raw": "high", "score": 0.95},
                        "criticality": "medium", "epistemic_status": "PROPOSED",
                        "evidence_required": [],
                        "exit_criteria": ["Complete and verify by MARM consensus and adversarial audit."],
                        "items": [], "kind": "task_candidate", "metrics": {},
                        "observed_at": None, "projection_run_id": "relay-mesh-landed",
                        "provenance": [],
                        "readiness": {
                            "max_evidence_age_days": None, "minimum_evidence_grade": None,
                            "operational_identity_required": True,
                            "required_canonical_revision": None, "required_materialization_scopes": [],
                        },
                        "source_claim_ids": [], "source_details": [], "source_refs": [],
                        "staleness_state": "FRESH",
                        "tags": ["relay", "marm", "auto_materialized"], "unlock_score": 1.0,
                        "completion": {}, "notes": [],
                    }
                    graph_raw["nodes"].append(nodes[tid])
                n = nodes[tid]
                completion = n.get("completion")
                if completion is None:
                    completion = {}
                if not isinstance(completion, dict):
                    raise UsageError(f"invalid completion for '{tid}'")
                if completion.get("status") == "DONE" or n.get("work_status") == "done":
                    # Exact replay is not a new declaration, including ungraded legacy DONE.
                    own_note = f"[RELAY_LANDED] Landed via Train {train_id} on "
                    if not (
                        completion.get("status") == "DONE"
                        and n.get("work_status") == "done"
                        and completion.get("dod_satisfied") is True
                        and completion.get("completed_at")
                        and completion.get("evidence") == f"Landed via Multi-Agent Relay Train {train_id} ({signoff})"
                        and any(isinstance(note, str) and note.startswith(own_note)
                                and note.endswith(f" with {signoff}.") for note in n.get("notes", []))
                    ):
                        raise UsageError(f"conflicting completion for '{tid}'")
                    continue
                for value, label in ((n.get("provenance", []), "provenance"),
                                     (completion.get("dod_evidence", []), "dod_evidence"),
                                     (n.get("notes", []), "notes")):
                    if not isinstance(value, list):
                        raise UsageError(f"invalid {label} for '{tid}'")
                # Preserve custom metadata and historical evidence_grade_original.
                # A new declaration never inherits the previous grade.
                completion.update(
                    status="DONE", completed_at=timestamp, dod_satisfied=True,
                    evidence=f"Landed via Multi-Agent Relay Train {train_id} ({signoff})",
                    evidence_grade="C", evidence_grade_reason=reason,
                    evidence_observed_at=timestamp,
                )
                completion.setdefault("dod_evidence", []).append(reference)
                n["completion"] = completion
                n.setdefault("provenance", []).append(reference)
                n["work_status"] = "done"
                n.setdefault("notes", []).append(
                    f"[RELAY_LANDED] Landed via Train {train_id} on {timestamp} with {signoff}.")
                changed_targets.append(tid)
            validate_completion_evidence(before, graph_raw)
            evidence_graph = WorkGraph(graph_raw)
            for tid in changed_targets:
                # Known landing targets are actionable even without a legacy kind.
                issues = completion_evidence_issues(evidence_graph, nodes[tid], actionable=True)
                if issues:
                    raise UsageError(f"completion evidence for '{tid}': {issues}")
            # Preflight the entire batch before Git CAS, consumption or cleanup.
            if changed_targets:
                if "revision" in graph_raw:
                    graph_raw["revision"] = compute_revision_hash(graph_raw)
                graph_text = json.dumps(graph_raw, indent=2, ensure_ascii=False)
                if strict_cleanup_lease is not None:
                    land_recorded_candidate(receipt, descriptor, repository_root=repository_root)
                relay_io.write_text_atomic(graph_file, graph_text)
            elif strict_cleanup_lease is not None:
                # A surviving graph write is valid only while Git retains the candidate.
                # Read only: never replay CAS or move an advanced target backwards.
                try:
                    import subprocess

                    target = subprocess.run(
                        ["git", "show-ref", "--verify", "--hash", descriptor.target_ref],
                        cwd=repository_root, check=True, capture_output=True,
                        text=True, timeout=10, **relay_io.safe_subprocess_kwargs(),
                    ).stdout.strip()
                    if not target:
                        raise ValueError("target ref has no commit")
                    candidate = receipt.payload["candidate_commit"]
                    if target != candidate:
                        subprocess.run(
                            ["git", "merge-base", "--is-ancestor", candidate, target],
                            cwd=repository_root, check=True, capture_output=True,
                            text=True, timeout=10, **relay_io.safe_subprocess_kwargs(),
                        )
                except Exception as exc:
                    raise UsageError(
                        "SURVIVING_COMPLETION_TARGET_NOT_LANDED: "
                        "target must retain the recorded candidate"
                    ) from exc
            # Valid replay needs only the terminal transition; no CAS/write.
    except Exception as exc:
        print(f"[REFUSED] land: graph/evidence preflight or landing failed: {exc}")
        return False

    terminal_agent = "smart-admission" if policy_authority else "human"
    terminal_role = "policy" if policy_authority else "human_gatekeeper"
    transitioned = transition_train(train_id, TERMINAL_STATUS, "completed", terminal_agent, terminal_role,
                                   f"Landed {len(task_ids)} tasks into work-graph.json.", _capability_ok=True)
    if not transitioned:
        print(f"[ERROR] Terminal transition failed for {train_id}; preserving approval capability.")
        return False

    if strict_cleanup_lease is not None:
        try:
            from compute_plane.workspace import cleanup_linked_worktree

            cleanup_linked_worktree(strict_cleanup_lease)
        except Exception as exc:
            telemetry(
                "strict_workspace.cleanup_failed",
                train=train_id,
                why=type(exc).__name__,
            )
            print(
                f"[WARN] strict workspace cleanup for {train_id} failed; "
                "the exact archived intent remains available for reconciliation."
            )

    release_lease(train_id, force=True)
    for tid in task_ids:
        release_lease(tid, force=True)
    _capability_file(train_id).unlink(missing_ok=True)
    (APPROVALS_DIR / f"APPROVAL_REQUEST_{train_id}.md").unlink(missing_ok=True)

    # Archive exactly once, then tombstone the active copy (Seam C.3).
    archived = any(h.name.startswith(f"{train_id}_LANDED") for h in HISTORY_DIR.iterdir()) \
        if HISTORY_DIR.exists() else False
    if not archived:
        shutil.copytree(train_dir, HISTORY_DIR / f"{train_id}_LANDED_{int(time.time())}")
    final = relay_io.read_json(state_file) or {}
    tombstone = {
        "train_id": train_id, "task_ids": task_ids, "status": TERMINAL_STATUS,
        "title": data.get("title", ""), "revision": train_revision(final),
        "landed_at": now_iso(), "tombstone": True,
        "evidenceDigests": {"packet": capability.get("packetDigest", ""),
                            "implementation": capability.get("implementationDigest", ""),
                            "audit": capability.get("auditDigest", "")},
    }
    for item in train_dir.iterdir():
        if item.name != "STATE.json":
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    relay_io.write_json_atomic(state_file, tombstone)
    update_board()
    telemetry("land.ok", train=train_id, tasks=len(task_ids))
    print(f"\n[TRAIN LANDED] {train_id}: marked {len(task_ids)} tasks DONE in work-graph.json.\n")
    return True


def land_all_pending(signoff: str = "REFUSED") -> int:
    """REMOVED (Seam A.5): wildcard landing is refused. Stale daemons calling
    this get the safe scoped path over explicit nonce-bearing files only."""
    print("[REFUSED] land_all_pending is removed: landings are train-scoped and "
          "capability-bound. Processing explicit nonce-bearing approvals only.")
    return process_drive_approvals()


def retire_train(train_id: str, reason: str = "SUPERSEDED_BY_NORTH_STAR_V2",
                 successor: Optional[str] = None, agent: str = "human_gatekeeper") -> bool:
    """Explicit FSM-governed retirement of an active/deferred train with closure report and tombstone (U-7D)."""
    if not _coord_ok("retire_train"):
        return False
    ensure_dirs()
    train_dir = TRAINS_DIR / train_id
    state_file = train_dir / "STATE.json"
    data = relay_io.read_json(state_file)
    if not isinstance(data, dict):
        print(f"[ERROR] Train '{train_id}' not found or unreadable.")
        return False
    
    old_status = data.get("status", "UNKNOWN")
    if old_status == "RETIRED" and data.get("tombstone") is True:
        print(f"[INFO] Train '{train_id}' is already retired.")
        return True
    
    # 1. Compute SHA-256 of all existing artifacts in the train folder
    digests = {}
    for p in sorted(train_dir.rglob("*")):
        if p.is_file():
            try:
                rel = p.relative_to(train_dir).as_posix()
                digests[rel] = relay_io.sha256_hex(p.read_bytes())
            except Exception:
                pass
    
    # 2. Write CLOSURE_REPORT.md
    closure_md = f"""# Release Train Closure & Retirement Report: {train_id}

- **Train ID:** `{train_id}`
- **Previous Status:** `{old_status}`
- **Terminal Status:** `RETIRED`
- **Retirement Reason:** {reason}
- **Successor Entity / Architecture:** `{successor or 'North Star 2.0 (ADR-042 Clean Core)'}`
- **Retired At:** `{now_iso()}`
- **Closed By:** `{agent}` (Host: `{get_hostname()}`)
- **Monotonic Revision at Closure:** `{train_revision(data)}`

---

## Preserved Artifact Manifest & SHA-256 Digests:
"""
    for rel_path, digest in digests.items():
        closure_md += f"- `{rel_path}`: `{digest}`\n"
    
    relay_io.write_text_atomic(train_dir / "CLOSURE_REPORT.md", closure_md)
    
    # 3. Transition to RETIRED
    transition_train(train_id, "RETIRED", "completed", agent, "human_gatekeeper",
                     f"Retired: {reason}. Successor: {successor or 'North Star 2.0'}", _capability_ok=True)
    
    # 4. Release any leases
    release_lease(train_id, force=True)
    for tid in data.get("task_ids", []):
        release_lease(tid, force=True)
    _capability_file(train_id).unlink(missing_ok=True)
    (APPROVALS_DIR / f"APPROVAL_REQUEST_{train_id}.md").unlink(missing_ok=True)
    
    # 5. Archive to HISTORY_DIR
    ts_stamp = int(time.time())
    dest_history = HISTORY_DIR / f"{train_id}_RETIRED_{ts_stamp}"
    shutil.copytree(train_dir, dest_history)
    
    # 6. Tombstone in place
    final_rev = train_revision(data) + 1
    tombstone = {
        "train_id": train_id,
        "task_ids": data.get("task_ids", []),
        "status": "RETIRED",
        "title": data.get("title", ""),
        "revision": final_rev,
        "retired_at": now_iso(),
        "tombstone": True,
        "reason": reason,
        "successor": successor,
        "evidenceDigests": digests
    }
    for item in train_dir.iterdir():
        if item.name != "STATE.json":
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    relay_io.write_json_atomic(state_file, tombstone)
    
    update_board()
    telemetry("retire.ok", train=train_id, reason=reason)
    print(f"\n[TRAIN RETIRED] {train_id} successfully retired and archived to {dest_history.name}.\n")
    return True


def unhold_train(train_id: str, agent: str = "operator") -> bool:
    """Manually unhold a train stuck in AUDIT_HOLD or PLAN_AUDIT_HOLD."""
    state_file = TRAINS_DIR / train_id / "STATE.json"
    if not state_file.exists():
        print(f"[ERROR] Train '{train_id}' does not exist.")
        return False
    data = relay_io.read_json(state_file) or {}
    status = data.get("status")
    rev = train_revision(data)
    if status == "AUDIT_HOLD":
        key = f"{train_id}::r{rev}::code-audit"
        relay_io.outcome_reset(key)
        ok = transition_train(train_id, "AUDIT_PENDING", "auditor", agent, "auditor", "Manual operator unhold")
        if ok:
            print(f"[UNHOLD] '{train_id}' returned to AUDIT_PENDING.")
        return ok
    elif status == "PLAN_AUDIT_HOLD":
        key = f"{train_id}::r{rev}::plan-audit"
        relay_io.outcome_reset(key)
        ok = transition_train(train_id, "PLAN_AUDIT_PENDING", "plan-auditor", agent, "plan-auditor", "Manual operator unhold")
        if ok:
            print(f"[UNHOLD] '{train_id}' returned to PLAN_AUDIT_PENDING.")
        return ok
    else:
        print(f"[REFUSED] Train '{train_id}' is not in a HOLD state (current: {status}).")
        return False


def process_drive_approvals() -> int:
    """Mobile path: APPROVED_<TRAIN-ID>.md must CONTAIN the capability nonce.
    Generic markers/helpers are ignored entirely (falsifier 9: no landings, no
    board writes, no deletions)."""
    if not _coord_ok("process_approval"):
        return 0
    ensure_dirs()
    landed = 0
    for f in list(APPROVALS_DIR.glob("APPROVED_*.md")):
        train_id = f.stem.replace("APPROVED_", "")
        capability = relay_io.read_json(_capability_file(train_id))
        if not isinstance(capability, dict):
            continue  # no capability -> not an approvable train; ignore file
        nonce = capability.get("nonce", "")
        if nonce and nonce in relay_io.read_text(f):
            if land_train(train_id, "MOBILE_NONCE_SIGNOFF", nonce=nonce):
                landed += 1
                f.unlink(missing_ok=True)
        else:
            telemetry("land.mobile_refused", train=train_id, reason="nonce absent from file")
    return landed


# -----------------------------------------------------------------------------
# Seam C: compact, digest-guarded board projections
# -----------------------------------------------------------------------------

def update_board() -> bool:
    ensure_dirs()
    now_ts = time.time()
    leases, trains, tombstones = [], [], []
    for lf in LEASES_DIR.glob("*.lease.json"):
        l_data = relay_io.read_json(lf)
        if isinstance(l_data, dict):
            l_data["is_active"] = l_data.get("expires_ts", 0) > now_ts
            leases.append(l_data)
    for td in sorted(TRAINS_DIR.iterdir() if TRAINS_DIR.exists() else []):
        data = relay_io.read_json(td / "STATE.json")
        if not isinstance(data, dict):
            continue
        if data.get("status") in TERMINAL_STATUSES or data.get("tombstone") is True:
            tombstones.append({"train_id": data.get("train_id", td.name),
                               "task_ids": data.get("task_ids", []),
                               "landed_at": data.get("landed_at", data.get("retired_at", data.get("updated_at", "")))})
        else:
            trains.append({"train_id": data.get("train_id", td.name),
                           "title": data.get("title", ""), "status": data.get("status"),
                           "current_role": data.get("current_role"),
                           "task_ids": data.get("task_ids", []),
                           "revision": train_revision(data),
                           "updated_at": str(data.get("updated_at", ""))[:19]})
    alerts = sorted(a.name for a in ALERTS_DIR.glob("*.md"))
    approvals = sorted(ap.name for ap in APPROVALS_DIR.glob("APPROVAL_REQUEST_*.md"))
    active_leases = [l for l in leases if l.get("is_active")]
    nodes_dir = RELAY_DIR / "nodes"
    nodes = []
    if nodes_dir.exists():
        for nf in sorted(nodes_dir.glob("*.json")):
            nd = relay_io.read_json(nf)
            if isinstance(nd, dict):
                nodes.append(nd)

    board = {
        "timestamp": "",  # excluded from digest comparison; set below only on change
        "hostname": get_hostname(),
        "counts": {"active_trains": len(trains), "landed_trains": len(tombstones),
                   "active_leases": len(active_leases), "pending_approvals": len(approvals),
                   "active_alerts": len(alerts), "active_nodes": len(nodes)},
        "trains": trains,
        "recentLanded": tombstones[-5:],
        "leases": [{k: l.get(k) for k in ("target_id", "agent_id", "role", "hostname", "expires_iso")}
                   for l in active_leases],
        "alerts": alerts,
        "approvals": approvals,
        "nodes": nodes,
    }
    semantic = json.dumps({k: v for k, v in board.items() if k != "timestamp"},
                          sort_keys=True, ensure_ascii=False)
    stale = relay_io.read_json(BOARD_JSON) or {}
    stale_semantic = json.dumps({k: v for k, v in stale.items() if k != "timestamp"},
                                sort_keys=True, ensure_ascii=False) if isinstance(stale, dict) else ""
    if semantic == stale_semantic:
        return False  # no-op render: bytes AND mtime preserved
    board["timestamp"] = now_iso()
    relay_io.write_json_atomic(BOARD_JSON, board)

    md = [f"# Multi-Agent Relay Mesh (MARM) — Live Coordination Board",
          "",
          f"- **Last Synchronization:** `{board['timestamp']}`",
          f"- **Host Reporting:** `{get_hostname()}`",
          f"- **Active Trains:** `{len(trains)}` | **Landed:** `{len(tombstones)}` | "
          f"**Active Leases:** `{len(active_leases)}` | **Pending Approvals:** `{len(approvals)}` | "
          f"**Alerts:** `{len(alerts)}` | **Active Worker Nodes:** `{len(nodes)}`", "", "---", ""]
    md.append(f"## Alerts ({len(alerts)})")
    md += [f"- [CRITICAL] `{a}`" for a in alerts] or ["_none_"]
    md += ["", f"## Pending Human Approvals ({len(approvals)})"]
    md += [f"- [READY] `{ap}`" for ap in approvals] or ["_none_"]
    md += ["", f"## Active Trains ({len(trains)})", "",
           "| Train | Status | Role | Rev | Tasks | Updated |", "|---|---|---|---|---|---|"]
    for t in trains:
        churn_tag = f" ⚠️ CHURN(r{t['revision']})" if t['revision'] >= 6 else ""
        md.append(f"| `{t['train_id']}` | `{t['status']}`{churn_tag} | `{t['current_role']}` | "
                  f"{t['revision']} | `{', '.join(t['task_ids'])}` | `{t['updated_at']}` |")
    if nodes:
        md += ["", f"## Active Swarm Worker Nodes ({len(nodes)})", "",
               "| Node / Host | Status | Pressure | CPU Cores | RAM Avail | GPU Info | Capabilities |",
               "|---|---|---|---|---|---|---|"]
        for n in nodes:
            res = n.get("resources", {})
            gpu_desc = f"{res.get('gpu_name', 'None')} ({res.get('gpu_load_pct', 0)}%)" if res.get("gpu_available") else "None"
            caps = ", ".join(f"`{c}`" for c in n.get("capabilities", [])[:3])
            md.append(f"| `{n.get('hostname')}` | `{n.get('status')}` | `{n.get('pressure', 'NORMAL')}` | "
                      f"{res.get('cpu_cores', 1)} | {res.get('ram_available_gb', 0)} GB | {gpu_desc} | {caps} |")
    md += ["", f"## Landed ({len(tombstones)}) — recent",
           *[f"- `{t['train_id']}` ({', '.join(t['task_ids'])})" for t in tombstones[-5:]]]
    relay_io.write_if_changed(BOARD_FILE, "\n".join(md) + "\n")
    telemetry("board.write", trains=len(trains))
    return True


# -----------------------------------------------------------------------------
# Seam D: role-scoped context capsule (index, never authority)
# -----------------------------------------------------------------------------

def context_capsule(train_id: str, role: str) -> Dict[str, Any]:
    train_dir = TRAINS_DIR / train_id
    data = relay_io.read_json(train_dir / "STATE.json") or {}
    pointers = {}
    for name in ("PACKET.md", "PLAN_AUDIT_REPORT.md", "AUDIT_REPORT.md", "DIFF.patch",
                 "IMPLEMENTATION_EVIDENCE.md"):
        p = train_dir / name
        if p.exists():
            pointers[name] = {"path": str(p), "sha256": relay_io.sha256_hex(p.read_bytes())}
    capsule = {"capsule": "relay-context/1", "trainId": train_id, "role": role,
               "status": data.get("status"), "revision": train_revision(data),
               "taskIds": data.get("task_ids", []),
               "requiredAction": _action_for(str(data.get("status"))),
               "sources": pointers,
               "note": "Index only. Gates re-open and verify the authoritative sources."}
    capsule["capsuleDigest"] = relay_io.sha256_hex(
        json.dumps({k: v for k, v in capsule.items() if k != "capsuleDigest"}, sort_keys=True))
    return capsule


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _arg(flag: str, default: Optional[str] = None) -> Optional[str]:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main() -> None:
    relay_io.utf8_console()
    if len(sys.argv) < 2:
        print("Usage: python relay/relay_mesh.py "
              "<board|init-train|claim|renew-lease|dispatch|audit|alert|resolve-alert|land|"
              "release-lease|capsule|intake|propose|vote|consensus|publish-study|round-table>")
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "board":
        changed = update_board()
        print(f"Board {'updated' if changed else 'unchanged (no-op render)'}:"
              f"\n  - Markdown: {BOARD_FILE}\n  - JSON: {BOARD_JSON}")
    elif cmd == "init-train":
        init_train(_arg("--id"), _arg("--title"), [t.strip() for t in _arg("--tasks").split(",")],
                   _arg("--agent", "planner"))
    elif cmd == "claim":
        train_id, agent, role = _arg("--train"), _arg("--agent"), _arg("--role")
        if role == "implementer":
            ok, why = plan_audit_valid(train_id)
            if not ok:
                print(f"[REFUSED] implementer claim on {train_id}: {why}")
                sys.exit(1)
        if acquire_lease(train_id, agent, role):
            status = {"implementer": "IN_PROGRESS_IMPLEMENTER",
                      "auditor": "IN_PROGRESS_AUDITOR",
                      "plan-auditor": "PLAN_AUDIT_PENDING"}.get(role)
            if status and status != "PLAN_AUDIT_PENDING":
                if not transition_train(train_id, status, role, agent, role,
                                        f"Claimed by {agent} on {get_hostname()}"):
                    release_lease(train_id, agent)
                    sys.exit(1)
    elif cmd == "renew-lease":
        renew_lease(_arg("--train"), _arg("--agent"))
    elif cmd == "dispatch":
        train_id, next_role, agent = _arg("--train"), _arg("--next-role"), _arg("--agent")
        notes = _arg("--notes", "Dispatched to next role")
        status = {"auditor": "AUDIT_PENDING", "plan-auditor": "PLAN_AUDIT_PENDING",
                  "implementer": "IN_PROGRESS_IMPLEMENTER"}.get(next_role)
        role = {"auditor": "implementer", "plan-auditor": "planner",
                "implementer": "auditor"}.get(next_role, "implementer")
        if status is None:
            print(f"[REFUSED] dispatch: unsupported next role {next_role!r}")
            sys.exit(1)
        if role == "planner":  # planner->plan-auditor is a fenced Planner mutation
            fenced_ok, why = planner_role.enforce_fence(agent)
            if not fenced_ok:
                print(f"[REFUSED] dispatch: planner fence: {why}")
                sys.exit(1)
        if not transition_train(train_id, status, next_role, agent, role, notes):
            sys.exit(1)
        release_lease(train_id, agent)
    elif cmd == "audit":
        train_id, status, agent = _arg("--train"), _arg("--status").upper(), _arg("--agent")
        state = relay_io.read_json(TRAINS_DIR / train_id / "STATE.json") or {}
        if state.get("status") == "AUDIT_PENDING":
            transition_train(train_id, "IN_PROGRESS_AUDITOR", "auditor", agent, "auditor",
                             "Audit claim (two-step to verdict)")
        if status == "PASS":
            ok = transition_train(train_id, "READY_FOR_HUMAN_APPROVAL", "human", agent, "auditor",
                                  "External adversarial audit passed.")
        else:
            reason = _arg("--reason", "Audit failed")
            raise_alert(f"Audit Failed for {train_id}", "high", reason, agent, train_id)
            ok = transition_train(train_id, "AUDIT_REJECTED", "implementer", agent, "auditor",
                                  f"Audit failed: {reason}")
        release_lease(train_id, agent)
        if not ok:
            sys.exit(1)
    elif cmd == "alert":
        raise_alert(_arg("--title"), "high", _arg("--details"), _arg("--agent", "unknown_agent"))
    elif cmd == "resolve-alert":
        resolve_alert(_arg("--id"))
    elif cmd == "land":
        if not land_train(_arg("--train"), nonce=_arg("--nonce")):
            sys.exit(1)
    elif cmd == "admit":
        if not admit_train(
            _arg("--train"), auto_land="--no-land" not in sys.argv
        ):
            sys.exit(1)
    elif cmd == "unhold":
        train_id = _arg("--train")
        agent = _arg("--agent", "operator")
        if not unhold_train(train_id, agent):
            sys.exit(1)
    elif cmd == "release-lease":
        release_lease(_arg("--target"), force=True)
    elif cmd == "capsule":
        print(json.dumps(context_capsule(_arg("--train"), _arg("--role", "implementer")),
                         indent=2, ensure_ascii=False))
    elif cmd == "intake":  # ingest one human demand file into the backlog
        import intake_processor
        out = (intake_processor.ingest_file(Path(_arg("--file"))) if "--file" in sys.argv
               else {"swept": intake_processor.sweep_human()})
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif cmd == "propose":  # open a multi-agent consensus proposal
        import consensus_engine
        print(json.dumps(consensus_engine.create_proposal(
            _arg("--id"), _arg("--agent", "unknown_agent"), _arg("--title", ""),
            _arg("--body", "")), indent=2, ensure_ascii=False))
    elif cmd == "vote":  # cast a typed vote on a proposal
        import consensus_engine
        print(json.dumps(consensus_engine.cast_vote(
            _arg("--id"), _arg("--agent"), _arg("--vote", "AGREE"), _arg("--comment", "")),
            indent=2, ensure_ascii=False))
    elif cmd == "consensus":  # tally, or --synthesize on consensus
        import consensus_engine
        pid = _arg("--id")
        out = consensus_engine.synthesize(pid) if "--synthesize" in sys.argv \
            else consensus_engine.tally(pid)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif cmd == "publish-study":  # 1-command publication to tare.tools.research & GitHub Pages
        import publisher_engine
        src_file = _arg("--file")
        ctx = _arg("--context", "02_harness-architecture")
        res = publisher_engine.publish_study(src_file, context_dir=ctx)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif cmd == "retire-train":  # formally retire and archive a train
        tid = _arg("--train")
        reason = _arg("--reason", "SUPERSEDED_BY_NORTH_STAR_V2")
        succ = _arg("--successor", "North Star 2.0 (ADR-042 Clean Core)")
        agent = _arg("--agent", "human_gatekeeper")
        if not retire_train(tid, reason=reason, successor=succ, agent=agent):
            sys.exit(1)
    elif cmd == "round-table":  # trigger Tripartite Dialectic Round Table
        import round_table_engine
        case_id = _arg("--case")
        doc_file = _arg("--doc")
        rounds = int(_arg("--rounds", "3"))
        if doc_file and not round_table_engine.RoundTableCase(case_id).state_file.exists():
            doc_p = Path(doc_file)
            doc_txt = doc_p.read_text(encoding="utf-8")
            title = doc_p.stem.replace("_", " ").replace("-", " ").title()
            round_table_engine.init_round_table(case_id, title, doc_txt, context_desc=str(doc_p))
        res = round_table_engine.run_full_deliberation(case_id, max_rounds=rounds)
        print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

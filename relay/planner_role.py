#!/usr/bin/env python3
"""planner_role — vendor-neutral Planner succession, epoch fencing & handoff.

RELAY-PLANNER-VENDOR-FAILOVER-01 (TRAIN-08). Makes the Relay `planner` role
transferable between vendors (Codex, ChatGPT, Claude, ...) and recoverable
after a crash WITHOUT creating two writers, losing inbox work, replaying stale
plans, or broadening role authority. A vendor is eligible because it satisfies
the capability contract and presents a valid takeover capability — never
because a brand name appears in code.

Fail closed everywhere; refused operations change no authoritative bytes.

Trust model
-----------
- The synchronized epoch record (`relay/state/planner-epoch.json`) holds only
  PUBLIC identifiers, digests, and bounded capabilities — never a reusable
  secret.
- The fencing SECRET is machine-local (`$RELAY_CRED_DIR/planner-fence.secret`),
  never in Drive. The synced record stores only sha256(token). To perform a
  fenced Planner mutation a caller must present the token PRE-IMAGE, which only
  the machine that minted the active epoch can compute. Once epoch N+1 is
  committed, every epoch-N token fails before authoritative bytes change, even
  if the old chat wakes up or Drive resurrects its messages.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay_io  # noqa: E402

CONTRACT_VERSION = "planner-role/1"
# Vendor-neutral capabilities a successor Planner MUST prove. No brand strings:
# eligibility is contract satisfaction, not a model self-assertion.
REQUIRED_CAPABILITIES = ("graph_read", "command_exec", "hashing", "envelope_routing")

LEASE_TTL_SECONDS = 180  # emergency takeover floor: silence < this is not failure

RELAY_DIR = Path(__file__).resolve().parent
STATE_DIR = RELAY_DIR / "state"
EPOCH_FILE = STATE_DIR / "planner-epoch.json"
CRED_DIR = Path(os.environ.get("RELAY_CRED_DIR", str(Path.home() / ".tare-relay-credentials")))


def rebind_root(root: Path | str) -> None:
    """Point epoch state at a different relay root (tests/benchmark)."""
    global RELAY_DIR, STATE_DIR, EPOCH_FILE
    RELAY_DIR = Path(root) / "relay"
    STATE_DIR = RELAY_DIR / "state"
    EPOCH_FILE = STATE_DIR / "planner-epoch.json"


# ---------------------------------------------------------------------------
# PlannerRoleContract (DoD 1): deterministic, read-only capability probing.
# ---------------------------------------------------------------------------

def default_probes(root: Path | str) -> Dict[str, Callable[[], bool]]:
    """Vendor-neutral probes. Each returns True iff the capability actually
    works on THIS host — a self-declaration cannot satisfy the contract."""
    root = Path(root)

    def graph_read() -> bool:
        data = relay_io.read_json(root / "work-graph.json")
        return isinstance(data, dict) and isinstance(data.get("nodes"), list)

    def command_exec() -> bool:
        import subprocess
        try:
            p = subprocess.run([sys.executable, "-c", "print(1)"],
                               capture_output=True, text=True, timeout=30)
            return p.returncode == 0 and p.stdout.strip() == "1"
        except Exception:
            return False

    def hashing() -> bool:
        return relay_io.sha256_hex("x") == relay_io.sha256_hex(b"x")

    def envelope_routing() -> bool:
        env = relay_io.make_envelope(train_id="T", revision=1,
                                     required_action="plan-audit", role="planner")
        return relay_io.action_key(env) == "T::r1::plan-audit::planner"

    return {"graph_read": graph_read, "command_exec": command_exec,
            "hashing": hashing, "envelope_routing": envelope_routing}


def probe_capabilities(probes: Dict[str, Callable[[], bool]]) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    for name, fn in probes.items():
        try:
            results[name] = bool(fn())
        except Exception:
            results[name] = False
    return results


def verify_contract(results: Dict[str, bool]) -> Tuple[bool, str]:
    """All REQUIRED_CAPABILITIES present and True, else fail closed."""
    if not isinstance(results, dict):
        return False, "capability result is not a mapping"
    missing = [c for c in REQUIRED_CAPABILITIES if c not in results]
    if missing:
        return False, f"capability probe never ran for {missing}"
    failed = [c for c in REQUIRED_CAPABILITIES if not results.get(c)]
    if failed:
        return False, f"capability(ies) not satisfied: {failed}"
    return True, "planner role contract satisfied"


# ---------------------------------------------------------------------------
# Epoch record + fencing token (DoD 2).
# ---------------------------------------------------------------------------

def _planner_secret() -> str:
    """Machine-local fencing secret (never synced). Auto-provisioned once."""
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    sec = CRED_DIR / "planner-fence.secret"
    if not sec.exists():
        import secrets as _s
        sec.write_text(_s.token_hex(32), encoding="utf-8")
    return sec.read_text(encoding="utf-8-sig").strip()


def fencing_token(epoch: int, agent_id: str, secret: Optional[str] = None) -> str:
    """Token PRE-IMAGE. Only a holder of the machine-local secret can compute
    the token whose digest the synced record commits to."""
    if secret is None:
        secret = _planner_secret()
    return relay_io.sha256_hex(f"{epoch}:{agent_id}:{secret}")


def fencing_digest(token: str) -> str:
    return relay_io.sha256_hex(token)


def read_epoch() -> Optional[Dict[str, Any]]:
    rec = relay_io.read_json(EPOCH_FILE)
    return rec if isinstance(rec, dict) else None


def _write_epoch_record(rec: Dict[str, Any]) -> None:
    relay_io.write_json_atomic(EPOCH_FILE, rec)


class _EpochLock:
    """O_EXCL lock over the epoch record; stale (>30s) locks are broken.
    Serializes CAS so a dual-writer race yields exactly one winner (falsifier)."""

    def __init__(self) -> None:
        self.lock = STATE_DIR / ".planner-epoch.lock"

    def __enter__(self) -> "_EpochLock":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(time.time()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.lock.stat().st_mtime > 30:
                        self.lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        raise RuntimeError("planner epoch lock busy")

    def __exit__(self, *exc: Any) -> None:
        self.lock.unlink(missing_ok=True)


def _record(epoch: int, agent_id: str, vendor_id: str, contract_version: str,
            *, handoff_id: str = "", context_digest: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    token = fencing_token(epoch, agent_id)
    return {
        "plannerEpoch": epoch,
        "agentId": agent_id,
        "vendorId": vendor_id,
        "contractVersion": contract_version,
        "fencingDigest": fencing_digest(token),  # PUBLIC: digest only, never the token
        "leaseIssuedAt": now, "leaseIssuedIso": iso,
        "leaseExpiresAt": now + LEASE_TTL_SECONDS,
        "heartbeatAt": now, "heartbeatIso": iso,
        "handoffId": handoff_id,
        "contextDigest": context_digest,
    }


def mint_root_epoch(agent_id: str, vendor_id: str,
                    contract_version: str = CONTRACT_VERSION,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """Establish epoch 1 (the first Planner). Refuses if an epoch already
    exists — advancement goes through cas_advance_epoch only."""
    with _EpochLock():
        if read_epoch() is not None:
            raise RuntimeError("planner epoch already established; use cas_advance_epoch")
        rec = _record(1, agent_id, vendor_id, contract_version, now=now)
        _write_epoch_record(rec)
        return rec


def heartbeat(agent_id: str, token: Optional[str] = None,
              now: Optional[float] = None) -> Tuple[bool, str]:
    """Refresh the active lease. Only the fenced active Planner may heartbeat."""
    with _EpochLock():
        ok, why = enforce_fence(agent_id, token)
        if not ok:
            return False, why
        rec = read_epoch()
        now = time.time() if now is None else now
        rec["heartbeatAt"] = now
        rec["heartbeatIso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        rec["leaseExpiresAt"] = now + LEASE_TTL_SECONDS
        _write_epoch_record(rec)
        return True, "heartbeat accepted"


def enforce_fence(agent_id: str, token: Optional[str] = None) -> Tuple[bool, str]:
    """Guard every Planner mutation. NO-OP (allow) until an epoch is minted, so
    legacy/unfenced operation and existing trains stay green. Once an epoch is
    active, a mutation must come from the active agent presenting a token whose
    digest matches the committed epoch — a stale-epoch token fails closed."""
    rec = read_epoch()
    if rec is None:
        return True, "planner fencing not activated (no epoch record)"
    if token is None:
        token = os.environ.get("RELAY_PLANNER_FENCE_TOKEN")
    active = rec.get("agentId")
    epoch = rec.get("plannerEpoch")
    if agent_id != active:
        return False, (f"planner role held by {active!r} at epoch {epoch}; "
                       f"{agent_id!r} is not the active planner (fenced)")
    if not token:
        return False, (f"active planner epoch {epoch} requires a fencing token "
                       "(set RELAY_PLANNER_FENCE_TOKEN or pass token=)")
    if fencing_digest(token) != rec.get("fencingDigest"):
        return False, f"stale/invalid fencing token for epoch {epoch} (fenced)"
    return True, f"fence ok: {agent_id} at epoch {epoch}"


def cas_advance_epoch(*, expected_epoch: int, successor_agent: str,
                      successor_vendor: str, contract_result: Dict[str, bool],
                      takeover: Dict[str, Any],
                      contract_version: str = CONTRACT_VERSION,
                      context_digest: str = "", handoff_id: str = "",
                      now: Optional[float] = None) -> Tuple[bool, Any]:
    """Atomic compare-and-swap epoch advance. Exactly one racer wins; the loser
    reads the already-advanced epoch and is refused with bytes unchanged.

    Requires: (a) expected_epoch == current, (b) successor satisfies the role
    contract, (c) a valid takeover capability bound to expected_epoch+1."""
    with _EpochLock():
        rec = read_epoch()
        current = rec.get("plannerEpoch") if rec else 0
        if current != expected_epoch:
            return False, (f"epoch CAS conflict: expected {expected_epoch}, "
                           f"current is {current} (a concurrent advance won)")
        ok, why = verify_contract(contract_result)
        if not ok:
            return False, f"successor rejected: {why}"
        target = expected_epoch + 1
        ok, why = _valid_takeover(takeover, target)
        if not ok:
            return False, f"takeover capability rejected: {why}"
        new = _record(target, successor_agent, successor_vendor, contract_version,
                      handoff_id=handoff_id, context_digest=context_digest, now=now)
        new["takeoverNonce"] = takeover.get("nonce")
        new["takeoverKind"] = takeover.get("kind", "planned")
        _write_epoch_record(new)
        return True, new


def _valid_takeover(cap: Dict[str, Any], target_epoch: int) -> Tuple[bool, str]:
    """A takeover capability must be owner/incumbent-authorized and bound to the
    SPECIFIC target epoch, so it cannot be replayed onto a later advance."""
    if not isinstance(cap, dict):
        return False, "missing takeover capability"
    if cap.get("authorizedBy") not in ("owner", "incumbent"):
        return False, "capability not authorized by owner or incumbent"
    if not cap.get("nonce"):
        return False, "capability carries no nonce"
    if cap.get("targetEpoch") != target_epoch:
        return False, (f"capability bound to epoch {cap.get('targetEpoch')}, "
                       f"not target {target_epoch} (replay/stale)")
    return True, "capability valid"


# ---------------------------------------------------------------------------
# Digest-bound handoff capsule (DoD 3): an index, never authority.
# ---------------------------------------------------------------------------

def build_capsule(*, root: Path | str, reason: str, source_epoch: int,
                  successor: str, source_paths: Dict[str, str],
                  decisions: Optional[list] = None,
                  inbox_cursor: Optional[Dict[str, Any]] = None,
                  active_trains: Optional[list] = None,
                  frontier: Optional[Dict[str, Any]] = None,
                  handoff_id: Optional[str] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Reopen every named source and record its sha256. The successor must
    reopen the same sources and refuse if any digest changed."""
    import uuid
    root = Path(root)
    now = time.time() if now is None else now
    sources: Dict[str, Dict[str, str]] = {}
    for label, rel in source_paths.items():
        p = (root / rel)
        sources[label] = {
            "path": rel,
            "sha256": relay_io.sha256_hex(p.read_bytes()) if p.exists() else "",
            "present": p.exists(),
        }
    capsule = {
        "capsule": "planner-handoff/1",
        "handoffId": handoff_id or uuid.uuid4().hex,
        "reason": reason,
        "sourceEpoch": source_epoch,
        "successor": successor,
        "createdAt": now,
        "sources": sources,
        "decisions": decisions or [],
        "inboxCursor": inbox_cursor or {},
        "activeTrains": active_trains or [],
        "frontier": frontier or {},
        "note": "Index only. The successor reopens every source and refuses on any digest change.",
    }
    capsule["capsuleDigest"] = capsule_digest(capsule)
    return capsule


def capsule_digest(capsule: Dict[str, Any]) -> str:
    body = {k: v for k, v in capsule.items() if k != "capsuleDigest"}
    return relay_io.sha256_hex(json.dumps(body, sort_keys=True, ensure_ascii=False))


def verify_capsule(capsule: Dict[str, Any], *, root: Path | str,
                   consumed_nonces: frozenset = frozenset()) -> Tuple[bool, str]:
    """Reject a tampered capsule (self-digest), a replayed handoff (nonce seen
    before), or any source whose bytes changed since the capsule was built."""
    if not isinstance(capsule, dict):
        return False, "capsule is not a mapping"
    stored = capsule.get("capsuleDigest")
    if stored != capsule_digest(capsule):
        return False, "capsule self-digest mismatch (tampered)"
    hid = capsule.get("handoffId")
    if hid in consumed_nonces:
        return False, f"handoff {hid} already consumed (replay rejected)"
    root = Path(root)
    for label, meta in capsule.get("sources", {}).items():
        p = root / meta.get("path", "")
        now_present = p.exists()
        if now_present != meta.get("present", False):
            return False, f"source {label!r} presence changed since handoff"
        if now_present:
            if relay_io.sha256_hex(p.read_bytes()) != meta.get("sha256"):
                return False, f"source {label!r} changed since handoff (stale capsule)"
    return True, "capsule verified against current sources"


# ---------------------------------------------------------------------------
# Succession (DoD 4): planned graceful handoff + emergency takeover.
# ---------------------------------------------------------------------------

def planned_handoff(*, incumbent_agent: str, incumbent_token: str,
                    successor_agent: str, successor_vendor: str,
                    contract_result: Dict[str, bool], capsule: Dict[str, Any],
                    root: Path | str, consumed_nonces: frozenset = frozenset(),
                    now: Optional[float] = None) -> Tuple[bool, Any]:
    """Graceful token handoff. The incumbent proves it holds the current fence,
    the capsule verifies against live sources, then CAS advances the epoch."""
    rec = read_epoch()
    if rec is None:
        return False, "no active planner epoch to hand off"
    ok, why = enforce_fence(incumbent_agent, incumbent_token)
    if not ok:
        return False, f"incumbent not authorized: {why}"
    ok, why = verify_capsule(capsule, root=root, consumed_nonces=consumed_nonces)
    if not ok:
        return False, f"handoff capsule invalid: {why}"
    takeover = {"authorizedBy": "incumbent", "nonce": capsule.get("handoffId"),
                "targetEpoch": rec.get("plannerEpoch") + 1, "kind": "planned"}
    return cas_advance_epoch(
        expected_epoch=rec.get("plannerEpoch"), successor_agent=successor_agent,
        successor_vendor=successor_vendor, contract_result=contract_result,
        takeover=takeover, context_digest=capsule.get("capsuleDigest", ""),
        handoff_id=capsule.get("handoffId", ""), now=now)


def emergency_takeover(*, successor_agent: str, successor_vendor: str,
                       contract_result: Dict[str, bool],
                       recovery_capability: Dict[str, Any],
                       root: Path | str, now: Optional[float] = None) -> Tuple[bool, Any]:
    """Recover after Planner crash. Silence alone before lease expiry is NOT
    proof of failure — takeover before expiry is refused even with a valid
    capability. After expiry, only an owner recovery capability may advance the
    epoch; a missing/ambiguous capability keeps the system read-only."""
    rec = read_epoch()
    if rec is None:
        return False, "no active planner epoch to recover"
    now = time.time() if now is None else now
    silence = now - float(rec.get("heartbeatAt", 0))
    if silence < LEASE_TTL_SECONDS:
        return False, (f"lease still live: {silence:.0f}s of silence < "
                       f"{LEASE_TTL_SECONDS}s floor (not proof of failure)")
    if not isinstance(recovery_capability, dict) or recovery_capability.get("authorizedBy") != "owner":
        return False, "emergency takeover requires an owner recovery capability (stays read-only)"
    cap = dict(recovery_capability)
    cap["targetEpoch"] = rec.get("plannerEpoch") + 1
    cap["kind"] = "emergency"
    return cas_advance_epoch(
        expected_epoch=rec.get("plannerEpoch"), successor_agent=successor_agent,
        successor_vendor=successor_vendor, contract_result=contract_result,
        takeover=cap, now=now)


if __name__ == "__main__":  # pragma: no cover — smoke self-check
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "relay").mkdir()
    (d / "work-graph.json").write_text('{"nodes": [], "edges": []}', encoding="utf-8")
    rebind_root(d)
    r = probe_capabilities(default_probes(d))
    assert verify_contract(r)[0], r
    assert enforce_fence("anyone")[0], "unfenced allow"
    mint_root_epoch("codex_planner", "codex")
    assert not enforce_fence("intruder")[0], "non-active must be fenced"
    ok, new = cas_advance_epoch(expected_epoch=1, successor_agent="claude_planner",
                                successor_vendor="claude", contract_result=r,
                                takeover={"authorizedBy": "owner", "nonce": "n1", "targetEpoch": 2})
    assert ok and new["plannerEpoch"] == 2, new
    ok2, _ = cas_advance_epoch(expected_epoch=1, successor_agent="x", successor_vendor="y",
                               contract_result=r, takeover={"authorizedBy": "owner", "nonce": "n2", "targetEpoch": 2})
    assert not ok2, "stale expected_epoch must lose CAS"
    print("planner_role self-check OK")

"""TCP-07 — Retire Recovery Authority (TRAIN-09-TCP-07).

The capping seam of the Trusted Control Plane. It PERMANENTLY revokes the
bootstrap recovery / emergency-override authorities so the control plane runs
fully self-governed (Genesis + Controller Root + Canonical Decision) with no
resurrectable bypass path.

Conventions mirror the other seams (genesis_activation / trusted_verifier):
- deterministic, schema-versioned receipts; verify re-opens the source and
  every authority behind it — a receipt is never a trust store;
- owner authority is a TCP-01 bootstrap child envelope that must re-derive from
  the root (`bootstrap_envelope.verify_child`) — an identity string is not
  authority;
- fail closed everywhere; refused operations change no authoritative bytes;
- irreversibility is enforced at the filesystem (O_EXCL seal) AND in policy
  (reinstatement is refused for everyone, owner included).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import sys
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib import bootstrap_envelope  # noqa: E402  — owner authority owner

RETIREMENT_SCHEMA = "tcp07-recovery-retirement-receipt/1.0"
RETIREMENT_DIR_REL = ".harness/runtime/recovery-retirement"
# The bootstrap recovery / override authorities this seam permanently revokes.
REVOKED_AUTHORITIES = ("bootstrap-root-remint", "emergency-recovery-override",
                       "recovery-bypass-key")


class RecoveryRetirementRefused(Exception):
    """Fail-closed refusal: authoritative bytes unchanged."""


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _receipt_path(root: Path | str, root_oid: str) -> Path:
    return Path(root) / RETIREMENT_DIR_REL / f"{_safe(root_oid)}.json"


def _owner_ok(owner_child_envelope: Any, root_envelope: Dict[str, Any], task_id: str) -> bool:
    return (isinstance(owner_child_envelope, dict)
            and owner_child_envelope.get("task_id") == task_id
            and bootstrap_envelope.verify_child(owner_child_envelope, root_envelope))


def retire_recovery_authority(root: Path | str, *, root_envelope: Dict[str, Any],
                              owner_child_envelope: Dict[str, Any], task_id: str,
                              retired_by: str, verifier: Optional[Dict[str, Any]] = None,
                              now: Optional[float] = None) -> Dict[str, Any]:
    """Permanently retire recovery authority for THIS root. Owner-gated; a
    non-owner (invalid/forged/out-of-scope envelope) is refused. Idempotent: a
    second call with a still-valid seal returns it rather than re-minting; a
    tampered existing seal fails closed and is NOT overwritten (irreversible)."""
    now = time.time() if now is None else now
    if not isinstance(root_envelope, dict) or not root_envelope.get("oid"):
        raise RecoveryRetirementRefused("root envelope missing/without oid")
    root_oid = str(root_envelope["oid"])
    if not _owner_ok(owner_child_envelope, root_envelope, task_id):
        raise RecoveryRetirementRefused(
            "NON_OWNER_RETIREMENT_REJECTED: owner authority envelope invalid, "
            "forged, or not bound to this task")
    receipt = {
        "schema": RETIREMENT_SCHEMA,
        "retirementId": "RET-" + _digest(
            {"root": root_oid, "task": task_id, "by": retired_by}).split(":", 1)[1][:24],
        "rootOid": root_oid, "taskId": task_id, "retiredBy": retired_by,
        "ownerEnvelope": dict(owner_child_envelope),   # re-derivable authority, not a secret
        "revokedAuthorities": list(REVOKED_AUTHORITIES),
        "sealed": True, "irreversible": True,
        "verifier": dict(verifier) if verifier else None,
        "retiredAt": now,
    }
    receipt["receiptDigest"] = _digest({k: v for k, v in receipt.items() if k != "receiptDigest"})
    path = _receipt_path(root, root_oid)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # permanence at the fs layer
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2, default=str)
        return receipt
    except FileExistsError:
        ok, why = verify_retirement_receipt(root, root_oid, root_envelope=root_envelope, now=now)
        if ok:
            return json.loads(path.read_text(encoding="utf-8"))  # already retired: idempotent
        raise RecoveryRetirementRefused(
            f"existing retirement seal invalid and cannot be overwritten (irreversible): {why}")


def verify_retirement_receipt(root: Path | str, root_oid: str, *,
                              root_envelope: Optional[Dict[str, Any]] = None,
                              verifier: Optional[Dict[str, Any]] = None,
                              now: Optional[float] = None) -> Tuple[bool, str]:
    """Re-open the sealed receipt and re-verify the authority behind it. A
    forged digest, wrong-root binding, unsealed flag, drifted owner envelope, or
    mismatched verifier all fail closed. Retirement is PERMANENT — no TTL."""
    path = _receipt_path(root, root_oid)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "retirement receipt missing/unreadable"
    claimed = receipt.get("receiptDigest")
    if claimed != _digest({k: v for k, v in receipt.items() if k != "receiptDigest"}):
        return False, "retirement receipt digest mismatch (forged/tampered)"
    if receipt.get("schema") != RETIREMENT_SCHEMA:
        return False, "unsupported retirement schema"
    if receipt.get("rootOid") != root_oid:
        return False, "retirement receipt bound to a different root (stale/replay)"
    if not (receipt.get("sealed") and receipt.get("irreversible")):
        return False, "retirement receipt not sealed"
    if root_envelope is not None:
        if str(root_envelope.get("oid")) != root_oid:
            return False, "presented root envelope does not match receipt root (stale)"
        if not _owner_ok(receipt.get("ownerEnvelope"), root_envelope, receipt.get("taskId")):
            return False, "receipt owner authority no longer re-derives from root (forged/stale)"
    if verifier is not None:
        bound = receipt.get("verifier") or {}
        if bound.get("trustedVerifierId") != verifier.get("trustedVerifierId"):
            return False, "RETIREMENT_NOT_BOUND_TO_THIS_VERIFIER"
    return True, "retirement receipt verified against reopened authority"


def is_retired(root: Path | str, root_oid: str, *,
               root_envelope: Optional[Dict[str, Any]] = None,
               verifier: Optional[Dict[str, Any]] = None) -> bool:
    return verify_retirement_receipt(root, root_oid, root_envelope=root_envelope,
                                     verifier=verifier)[0]


def authorize_recovery_override(root: Path | str, *, root_oid: str,
                                override_request: Dict[str, Any],
                                root_envelope: Optional[Dict[str, Any]] = None,
                                verifier: Optional[Dict[str, Any]] = None,
                                now: Optional[float] = None) -> Dict[str, Any]:
    """Gate a recovery/bootstrap override attempt. Once retirement is in force
    EVERY override is refused, fail closed (`RECOVERY_REVOKED_REJECTED`) — no
    key, quorum, or owner action reopens it. Before retirement the override is
    still available but remains owner-gated (never a free bypass)."""
    if is_retired(root, root_oid, root_envelope=root_envelope, verifier=verifier):
        return {"ok": False, "outcome": "RECOVERY_REVOKED_REJECTED",
                "reason": "recovery authority permanently retired (TCP-07)",
                "rootOid": root_oid}
    req = override_request or {}
    owner_env = req.get("ownerEnvelope")
    if root_envelope is None or not _owner_ok(owner_env, root_envelope, req.get("taskId")):
        return {"ok": False, "outcome": "RECOVERY_OVERRIDE_UNAUTHORIZED",
                "reason": "override lacks a valid owner authority envelope",
                "rootOid": root_oid}
    return {"ok": True, "outcome": "RECOVERY_OVERRIDE_GRANTED", "rootOid": root_oid}


def reinstate_recovery_authority(root: Path | str, *, root_oid: str,
                                 **_ignored: Any) -> Dict[str, Any]:
    """Retirement is permanent. Reinstatement is refused for EVERYONE (owner
    included) — there is no path back once the seal exists."""
    return {"ok": False, "outcome": "RECOVERY_RETIREMENT_IRREVERSIBLE",
            "reason": "recovery authority retirement is permanent and cannot be reinstated",
            "rootOid": root_oid}

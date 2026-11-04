"""TCP-06 — Genesis / strict eligibility activation (TRAIN-07-TCP-06).

Composition layer over the five landed Trusted Control Plane seams. It owns
NO validation logic of its own: every check delegates to the seam owner
(bootstrap_envelope/tcp01_controller_root, governance_receipt, trusted_verifier
+ decision_inbox, sandbox_spawn, trusted_ci) and every consumer re-opens the
canonical sources. Strict conjunction, fail closed: one absent, failed, stale,
tampered, replayed, overridden or subject-mismatched seam makes the whole
result GENESIS_INELIGIBLE — no partial mode, no caller-supplied PASS booleans.

Also owns the assignment boundary (Genesis certificate + agent capability
credential + full lease set BEFORE any handler byte) and the bounded
multi-agent lease manager with deterministic ordering + wait-for cycle
detection (deadlock prevention belt-and-suspenders).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import sys
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib import (  # noqa: E402
    bootstrap_envelope,
    governance_receipt,
    sandbox_spawn,
    tcp01_controller_root,
    trusted_ci,
    trusted_verifier,
)

CERT_SCHEMA = "tcp06-genesis-certificate/1.0"
CREDENTIAL_SCHEMA = "tcp06-agent-capability-credential/1.0"
CERT_DIR_REL = ".harness/runtime/genesis-certificates"
LEASE_DIR_REL = ".harness/runtime/genesis-leases"
MAX_EVIDENCE_AGE_SECONDS = 3600
CERT_TTL_SECONDS = 3600
SEAMS = ("controllerRoot", "provenanceBinding", "canonicalDecision",
         "filesystemConfinement", "trustedCI")


class GenesisRefused(Exception):
    """Fail-closed refusal: zero certificates, zero leases, zero handler bytes."""


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _seam(state: bool, reason: str, **refs: Any) -> Dict[str, Any]:
    row = {"state": "PASS" if state else "FAIL", "reason": reason, **refs}
    row["resultDigest"] = _digest(row)
    return row


# ---------------------------------------------------------------------------
# A. Five-seam Genesis evidence synthesis
# ---------------------------------------------------------------------------

class GenesisActivationGate:
    """Accepts identifiers/source pointers, never caller-supplied PASS booleans."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # -- individual seam re-verification (delegation only) ------------------

    def _controller_root(self, ev: Dict[str, Any], task_id: str, now: float) -> Dict[str, Any]:
        root_env, child = ev.get("rootEnvelope"), ev.get("childEnvelope")
        ledger = ev.get("ledgerPath")
        registry_digest = str(ev.get("registryDigest") or "")
        if not (isinstance(root_env, dict) and isinstance(child, dict) and ledger):
            return _seam(False, "controllerRoot evidence pointers missing")
        if now >= float(root_env.get("expires_at", 0)):
            return _seam(False, "root authority envelope expired")
        if child.get("task_id") != task_id:
            return _seam(False, f"child envelope bound to {child.get('task_id')!r}, not {task_id!r}")
        if not bootstrap_envelope.verify_child(child, root_env):
            return _seam(False, "child envelope does not re-derive from root (forged/out-of-range)")
        if not tcp01_controller_root.verify_ledger(ledger):
            return _seam(False, "controller dispatch ledger hash chain broken (tampered)")
        try:
            lines = Path(ledger).read_text(encoding="utf-8").splitlines()
            last = json.loads(lines[-1]) if lines else {}
        except (OSError, json.JSONDecodeError):
            return _seam(False, "controller ledger unreadable (fails closed)")
        if last.get("root_oid") != root_env.get("oid"):
            return _seam(False, "ledger not bound to this root authority")
        if not registry_digest:
            return _seam(False, "frozen command-registry digest missing")
        return _seam(True, "controller root re-verified", rootOid=root_env["oid"],
                     childOid=child["oid"], registryDigest=registry_digest)

    def _provenance(self, ev: Dict[str, Any], subject: str, now: float) -> Dict[str, Any]:
        ref = ev.get("reference")
        if not isinstance(ref, dict):
            return _seam(False, "provenance receipt reference missing")
        ok, why = governance_receipt.verify_receipt_reference(
            self.root, ref, expected_fingerprint=subject,
            expected_context_digest=ev.get("expectedContextDigest"))
        if not ok:
            return _seam(False, f"receipt reference refused: {why}")
        receipt = governance_receipt.load_receipt(
            governance_receipt.get_receipt_path(self.root, str(ref["kind"]), str(ref["fingerprint"])))
        ended = receipt.ended_at if receipt else None
        try:
            from datetime import datetime
            age = now - datetime.fromisoformat(str(ended)).timestamp()
        except (TypeError, ValueError):
            return _seam(False, "execution receipt has no parseable completion time (stale-unknown fails closed)")
        if age > MAX_EVIDENCE_AGE_SECONDS or age < 0:
            return _seam(False, f"execution receipt stale ({int(age)}s)")
        return _seam(True, "provenance receipt re-verified", receiptDigest=ref.get("receiptDigest"))

    def _decision(self, record: "trusted_verifier.TrustedVerifierRecord",
                  test_only: bool) -> Dict[str, Any]:
        out = trusted_verifier.validate_promotion_binding(self.root, record, test_only=test_only)
        if not out.get("ok"):
            return _seam(False, f"promotion decision binding refused: {out.get('reason')}")
        return _seam(True, "canonical owner decision re-verified",
                     authority=out.get("authority"), decisionId=out.get("decisionId"))

    def _confinement(self, ev: Dict[str, Any], subject: str) -> Dict[str, Any]:
        eid = str(ev.get("executionId") or "")
        ok, why = sandbox_spawn.verify_confinement_evidence(
            self.root, eid, expected_fingerprint=subject)
        if not ok:
            return _seam(False, f"confinement evidence refused: {why}")
        return _seam(True, "filesystem confinement re-verified", executionId=eid)

    def _trusted_ci(self, ev: Dict[str, Any], subject: str,
                    record: "trusted_verifier.TrustedVerifierRecord") -> Dict[str, Any]:
        eid = str(ev.get("executionId") or "")
        out = trusted_verifier.validate_ci_execution(
            self.root, eid, record, expected_fingerprint=subject, require_confinement=True)
        if not out.get("ok"):
            return _seam(False, f"CI receipt refused: {out.get('reason')}")
        return _seam(True, "trusted CI receipt re-verified", executionId=eid)

    # -- synthesis -----------------------------------------------------------

    def evaluate(self, *, task_id: str, agent_id: str, subject_fingerprint: str,
                 evidence: Dict[str, Any], record: "trusted_verifier.TrustedVerifierRecord",
                 test_only: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        seams = {
            "controllerRoot": self._controller_root(evidence.get("controllerRoot") or {}, task_id, now),
            "provenanceBinding": self._provenance(evidence.get("provenanceBinding") or {},
                                                  subject_fingerprint, now),
            "canonicalDecision": self._decision(record, test_only),
            "filesystemConfinement": self._confinement(evidence.get("filesystemConfinement") or {},
                                                       subject_fingerprint),
            "trustedCI": self._trusted_ci(evidence.get("trustedCI") or {}, subject_fingerprint, record),
        }
        # The FULL existing precondition registry stays mandatory (§2.A.6):
        # UNKNOWN remains ineligible for PRODUCTION; TCP-06 adds no shortcut.
        preconditions = trusted_verifier.genesis_precondition_report()
        eligible = all(row["state"] == "PASS" for row in seams.values())
        production = (eligible and not test_only
                      and preconditions.get("reportKind") == "PRODUCTION"
                      and all(r["state"] == "PASS"
                              for r in preconditions["preconditions"].values()))
        return {
            "verdict": "GENESIS_ELIGIBLE" if eligible else "GENESIS_INELIGIBLE",
            "productionEligible": production,
            "authority": "TEST_ONLY" if test_only else "PRODUCTION",
            "taskId": task_id, "agentId": agent_id,
            "subjectFingerprint": subject_fingerprint,
            "verifier": {"trustedVerifierId": record.trusted_verifier_id,
                         "verifierTreeId": record.verifier_tree_id,
                         "proofProfileDigest": record.proof_profile_digest},
            "seams": seams,
            "preconditions": preconditions["preconditions"],
            "evaluatedAt": now,
        }

    def mint_certificate(self, evaluation: Dict[str, Any],
                         evidence: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic, schema-versioned certificate; only for GENESIS_ELIGIBLE.
        Never a trust store: verify_certificate re-opens every source."""
        if evaluation.get("verdict") != "GENESIS_ELIGIBLE":
            raise GenesisRefused("cannot mint a certificate for an ineligible evaluation")
        seam_digests = {name: row["resultDigest"] for name, row in evaluation["seams"].items()}
        cert_id = "GEN-" + _digest({
            "task": evaluation["taskId"], "agent": evaluation["agentId"],
            "subject": evaluation["subjectFingerprint"], "seams": seam_digests,
            "verifier": evaluation["verifier"]}).split(":", 1)[1][:24]
        cert = {
            "schema": CERT_SCHEMA, "certificateId": cert_id,
            "taskId": evaluation["taskId"], "agentId": evaluation["agentId"],
            "subjectFingerprint": evaluation["subjectFingerprint"],
            "verifier": evaluation["verifier"],
            "authority": evaluation["authority"],
            "productionEligible": evaluation["productionEligible"],
            "seamDigests": seam_digests,
            "preconditionDigests": {n: r["resultDigest"]
                                    for n, r in evaluation["preconditions"].items()},
            "evidenceRefs": evidence,
            "issuedAt": evaluation["evaluatedAt"],
            "expiresAt": evaluation["evaluatedAt"] + CERT_TTL_SECONDS,
        }
        cert["certificateDigest"] = _digest(cert)
        path = self.root / CERT_DIR_REL / f"{cert_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cert, indent=2, default=str), encoding="utf-8")
        return cert

    def verify_certificate(self, cert_id: str, *, task_id: str, agent_id: str,
                           subject_fingerprint: str,
                           record: "trusted_verifier.TrustedVerifierRecord",
                           test_only: bool = False,
                           now: Optional[float] = None) -> Tuple[bool, str]:
        """Re-open the certificate AND every canonical source behind it."""
        now = time.time() if now is None else now
        path = self.root / CERT_DIR_REL / f"{cert_id}.json"
        try:
            cert = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, "certificate missing/unreadable"
        claimed = cert.pop("certificateDigest", None)
        if claimed != _digest(cert):
            return False, "certificate digest mismatch (tampered)"
        if cert.get("schema") != CERT_SCHEMA:
            return False, "unsupported certificate schema"
        for field, expect in (("taskId", task_id), ("agentId", agent_id),
                              ("subjectFingerprint", subject_fingerprint)):
            if cert.get(field) != expect:
                return False, f"certificate {field} mismatch (replay across {field})"
        if cert.get("verifier", {}).get("trustedVerifierId") != record.trusted_verifier_id:
            return False, "certificate bound to a different verifier (replay)"
        if now >= float(cert.get("expiresAt", 0)):
            return False, "certificate expired"
        fresh = self.evaluate(task_id=task_id, agent_id=agent_id,
                              subject_fingerprint=subject_fingerprint,
                              evidence=cert.get("evidenceRefs") or {},
                              record=record, test_only=test_only, now=now)
        if fresh["verdict"] != "GENESIS_ELIGIBLE":
            failed = [n for n, r in fresh["seams"].items() if r["state"] != "PASS"]
            return False, f"sources no longer verify: {failed}"
        if {n: r["resultDigest"] for n, r in fresh["seams"].items()} != cert.get("seamDigests"):
            return False, "seam evidence changed since minting (revision drift)"
        return True, "certificate re-verified against reopened sources"


# ---------------------------------------------------------------------------
# B. Agent capability credential (external authority; never self-minted)
# ---------------------------------------------------------------------------

def issue_capability_credential(*, agent_id: str, role: str, task_id: str,
                                subject_fingerprint: str,
                                capabilities: Dict[str, str],
                                child_envelope: Dict[str, Any],
                                issuer_identity: str, decision_ref: str,
                                ttl_seconds: int = 3600,
                                now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    cred = {
        "schema": CREDENTIAL_SCHEMA,
        "agentId": agent_id, "role": role, "taskId": task_id,
        "subjectFingerprint": subject_fingerprint,
        "capabilities": dict(capabilities),  # id -> canonical manifest digest
        "childEnvelope": dict(child_envelope),
        "issuerIdentity": issuer_identity, "decisionRef": decision_ref,
        "issuedAt": now, "expiresAt": now + ttl_seconds,
        "credentialId": "CRED-" + secrets.token_hex(8),
    }
    cred["credentialDigest"] = _digest(cred)
    return cred


_CRED_ISSUERS = ("harness-owner", "harness-governance-controller")


def verify_capability_credential(cred: Any, *, agent_id: str, role: str, task_id: str,
                                 subject_fingerprint: str, manifest_path: Path | str,
                                 root_envelope: Dict[str, Any],
                                 now: Optional[float] = None) -> Tuple[bool, str]:
    """A digest-sealed credential alone is NOT authority: it must carry the
    TCP-01 owner envelope for THIS task and match the canonical capability
    manifest re-opened from disk. No wildcards anywhere."""
    now = time.time() if now is None else now
    if not isinstance(cred, dict) or cred.get("schema") != CREDENTIAL_SCHEMA:
        return False, "credential missing or wrong schema"
    body = {k: v for k, v in cred.items() if k != "credentialDigest"}
    if cred.get("credentialDigest") != _digest(body):
        return False, "credential digest mismatch (tampered or self-edited)"
    for field, expect in (("agentId", agent_id), ("role", role), ("taskId", task_id),
                          ("subjectFingerprint", subject_fingerprint)):
        value = cred.get(field)
        if value == "*" or value != expect:
            return False, f"credential {field} mismatch/wildcard refused ({value!r})"
    if now >= float(cred.get("expiresAt", 0)):
        return False, "credential expired"
    if not cred.get("credentialId"):
        return False, "credential lacks nonce/id"
    if cred.get("issuerIdentity") not in _CRED_ISSUERS:
        return False, f"unauthorized credential issuer {cred.get('issuerIdentity')!r}"
    if not str(cred.get("decisionRef") or "").strip():
        return False, "credential lacks canonical decision/evidence reference"
    child = cred.get("childEnvelope")
    if not isinstance(child, dict):
        return False, "credential carries no owner authority envelope (manifest alone is not authority)"
    if child.get("task_id") != task_id or not bootstrap_envelope.verify_child(child, root_envelope):
        return False, "owner authority envelope invalid or bound to another task"
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False, "canonical capability manifest unreadable (fails closed)"
    caps = cred.get("capabilities") or {}
    if not caps:
        return False, "credential declares no capabilities (authority without capability evidence)"
    for cap_id, digest in caps.items():
        canonical = (manifest.get("capabilities") or {}).get(cap_id)
        if canonical is None:
            return False, f"capability {cap_id!r} absent from canonical manifest"
        if _digest(canonical) != digest:
            return False, f"capability {cap_id!r} manifest digest drift"
    return True, "capability credential verified against canonical manifest + owner envelope"


# ---------------------------------------------------------------------------
# C. Bounded multi-agent lease manager with deadlock prevention
# ---------------------------------------------------------------------------

class GenesisLeaseManager:
    def __init__(self, root: Path | str):
        self.dir = Path(root) / LEASE_DIR_REL
        self.dir.mkdir(parents=True, exist_ok=True)

    def _lease_path(self, resource: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in resource)
        return self.dir / f"{safe}.lease.json"

    def _intent_path(self, agent_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in agent_id)
        return self.dir / f"{safe}.intent.json"

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            # corrupt lease: quarantine, never silently treat as stale
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
            raise GenesisRefused(f"corrupt lease state quarantined: {path.name}")
        except OSError:
            return None

    def _would_cycle(self, agent_id: str, wanted: List[str]) -> bool:
        """Wait-for graph over live leases + pending intents (the belt)."""
        edges: Dict[str, set] = {}
        holders: Dict[str, str] = {}
        for lf in self.dir.glob("*.lease.json"):
            data = self._read(lf)
            if isinstance(data, dict) and time.time() < float(data.get("expiresAt", 0)):
                holders[data.get("resource", lf.stem)] = data.get("agentId", "")
        intents: Dict[str, List[str]] = {agent_id: wanted}
        for inf in self.dir.glob("*.intent.json"):
            data = self._read(inf)
            if isinstance(data, dict):
                intents.setdefault(data.get("agentId", ""), data.get("resources", []))
        for agent, wants in intents.items():
            for res in wants:
                holder = holders.get(res)
                if holder and holder != agent:
                    edges.setdefault(agent, set()).add(holder)
        seen: set = set()
        stack: set = set()

        def dfs(node: str) -> bool:
            seen.add(node)
            stack.add(node)
            for nxt in edges.get(node, ()):
                if nxt in stack or (nxt not in seen and dfs(nxt)):
                    return True
            stack.discard(node)
            return False

        return dfs(agent_id)

    def acquire(self, *, agent_id: str, task_id: str, subject_fingerprint: str,
                resources: List[str], certificate_digest: str, credential_digest: str,
                ttl_seconds: int = 900) -> Dict[str, Any]:
        """Atomic all-or-nothing acquisition in deterministic sorted order."""
        if not resources:
            raise GenesisRefused("empty resource set")
        ordered = sorted(set(resources))
        if self._would_cycle(agent_id, ordered):
            raise GenesisRefused("acquisition would introduce a wait-for cycle (deadlock refused)")
        intent = self._intent_path(agent_id)
        token = secrets.token_hex(16)
        intent.write_text(json.dumps({"agentId": agent_id, "resources": ordered,
                                      "token": token, "at": time.time()}), encoding="utf-8")
        acquired: List[Path] = []
        now = time.time()
        lease = {"agentId": agent_id, "taskId": task_id,
                 "subjectFingerprint": subject_fingerprint,
                 "certificateDigest": certificate_digest,
                 "credentialDigest": credential_digest,
                 "holderToken": token, "revision": 1,
                 "acquiredAt": now, "heartbeatAt": now, "expiresAt": now + ttl_seconds}
        try:
            for res in ordered:
                path = self._lease_path(res)
                row = dict(lease, resource=res)
                while True:
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(row, f)
                        acquired.append(path)
                        break
                    except FileExistsError:
                        existing = self._read(path)
                        if isinstance(existing, dict) and time.time() >= float(existing.get("expiresAt", 0)):
                            # atomic expiry/revision CAS reclamation
                            row["revision"] = int(existing.get("revision", 1)) + 1
                            tmp = path.with_suffix(".reclaim.tmp")
                            tmp.write_text(json.dumps(row), encoding="utf-8")
                            check = self._read(path)
                            if check == existing:
                                os.replace(tmp, path)
                                acquired.append(path)
                                break
                            tmp.unlink(missing_ok=True)
                            continue  # lost the CAS; re-evaluate
                        raise GenesisRefused(
                            f"resource {res!r} held by {existing.get('agentId') if isinstance(existing, dict) else '?'} "
                            "(bounded refusal, no blocking wait)")
        except GenesisRefused:
            for path in acquired:  # full rollback before returning failure
                path.unlink(missing_ok=True)
            intent.unlink(missing_ok=True)
            raise
        intent.unlink(missing_ok=True)  # full set acquired: no longer partial
        return {"agentId": agent_id, "resources": ordered, "holderToken": token,
                "leaseFiles": [str(p) for p in acquired], "expiresAt": lease["expiresAt"]}

    def _owned(self, path: Path, agent_id: str, token: str) -> Dict[str, Any]:
        data = self._read(path)
        if not isinstance(data, dict):
            raise GenesisRefused("lease missing")
        if data.get("agentId") != agent_id or data.get("holderToken") != token:
            raise GenesisRefused("non-owner cannot renew/release/steal a lease")
        return data

    def renew(self, resource: str, *, agent_id: str, token: str, revision: int,
              ttl_seconds: int = 900) -> None:
        path = self._lease_path(resource)
        data = self._owned(path, agent_id, token)
        if int(data.get("revision", 0)) != revision:
            raise GenesisRefused("stale lease revision (concurrent change)")
        data["revision"] = revision + 1
        data["heartbeatAt"] = time.time()
        data["expiresAt"] = time.time() + ttl_seconds
        path.write_text(json.dumps(data), encoding="utf-8")

    def release(self, resource: str, *, agent_id: str, token: str) -> None:
        path = self._lease_path(resource)
        self._owned(path, agent_id, token)
        path.unlink(missing_ok=True)

    def reconcile(self) -> List[str]:
        """Crash recovery: drop leases belonging to a still-pending intent
        (partial acquisition). Idempotent and bounded."""
        dropped: List[str] = []
        for inf in list(self.dir.glob("*.intent.json")):
            data = self._read(inf)
            if not isinstance(data, dict):
                inf.unlink(missing_ok=True)
                continue
            token = data.get("token")
            for res in data.get("resources", []):
                path = self._lease_path(res)
                lease = self._read(path)
                if isinstance(lease, dict) and lease.get("holderToken") == token:
                    path.unlink(missing_ok=True)
                    dropped.append(res)
            inf.unlink(missing_ok=True)
        return dropped


# ---------------------------------------------------------------------------
# D. Assignment boundary: certificate + credential + leases BEFORE any byte
# ---------------------------------------------------------------------------

def assign_task(root: Path | str, *, task_id: str, agent_id: str, role: str,
                subject_fingerprint: str, evidence: Dict[str, Any],
                record: "trusted_verifier.TrustedVerifierRecord",
                credential: Dict[str, Any], manifest_path: Path | str,
                root_envelope: Dict[str, Any], resources: List[str],
                handler: Callable[[], Any], test_only: bool = False) -> Dict[str, Any]:
    gate = GenesisActivationGate(root)
    evaluation = gate.evaluate(task_id=task_id, agent_id=agent_id,
                               subject_fingerprint=subject_fingerprint,
                               evidence=evidence, record=record, test_only=test_only)
    if evaluation["verdict"] != "GENESIS_ELIGIBLE":
        failed = [n for n, r in evaluation["seams"].items() if r["state"] != "PASS"]
        raise GenesisRefused(f"GENESIS_INELIGIBLE: {failed}")
    cert = gate.mint_certificate(evaluation, evidence)
    ok, why = verify_capability_credential(
        credential, agent_id=agent_id, role=role, task_id=task_id,
        subject_fingerprint=subject_fingerprint, manifest_path=manifest_path,
        root_envelope=root_envelope)
    if not ok:
        raise GenesisRefused(f"capability credential refused: {why}")
    leases = GenesisLeaseManager(root).acquire(
        agent_id=agent_id, task_id=task_id, subject_fingerprint=subject_fingerprint,
        resources=resources, certificate_digest=cert["certificateDigest"],
        credential_digest=credential["credentialDigest"])
    receipt = {
        "schema": "tcp06-assignment-receipt/1.0",
        "taskId": task_id, "agentId": agent_id,
        "certificateId": cert["certificateId"],
        "certificateDigest": cert["certificateDigest"],
        "credentialDigest": credential["credentialDigest"],
        "leases": leases, "assignedAt": time.time(),
    }
    receipt["receiptDigest"] = _digest(receipt)
    result = handler()  # ONLY after every gate passed
    return {"receipt": receipt, "result": result, "certificate": cert}

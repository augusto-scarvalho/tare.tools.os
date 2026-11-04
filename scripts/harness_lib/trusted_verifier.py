"""Pinned verifier authority, never inferred from the candidate or HEAD."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_lib import candidate_snapshot, gate_checks_strict_subject, validation_context
from harness_lib.common import HarnessError, now


TRUSTED_VERIFIER_RECORD_REL = ".harness/state-store/trusted-verifiers.json"
STRICT_PROOF_PROFILE = "harness-deterministic-v1"
FILESYSTEM_ISOLATION_AUTHORITY = "CONVENTION_ONLY"
FILESYSTEM_ESCAPE_BLOCKER = "OPEN"
TRUST_STORE_SCHEMA_VERSION = "1.0"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ProofProfileAuthorityManifest:
    """The authority closure owned by one proof profile, never a directory glob."""
    proof_profile_id: str
    proof_profile_version: str
    verifier_modules: tuple[str, ...]
    authority_artifacts: tuple[str, ...]
    mechanisms: tuple[str, ...]
    aggregation_semantics: str


def _profile_manifest(record: "TrustedVerifierRecord") -> ProofProfileAuthorityManifest:
    # Generic P1 verifier authority: the pinned verifier entrypoint only. A gate
    # that needs a wider authority closure (e.g. a strict-subject contract binding
    # additional modules) is an IP-2/P5 concern and is NOT expressed here.
    return ProofProfileAuthorityManifest(
        record.required_proof_profile, record.verifier_version,
        ("scripts/harness-test.py",), (), record.required_mechanisms,
        "all-required-gates-pass",
    )


def _authority_manifest(verifier_root: Path, record: "TrustedVerifierRecord") -> list[dict[str, str]]:
    """Hash only authority inputs declared by the selected proof profile."""
    manifest = _profile_manifest(record)
    rows: list[dict[str, str]] = []
    for rel in (*manifest.verifier_modules, *manifest.authority_artifacts):
        path = verifier_root / rel
        if not path.is_file():
            raise HarnessError(f"missing strict verifier authority input: {rel}")
        rows.append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return rows


def _trusted_bootstrap(validator: Path, scripts_root: Path, argv: list[str]) -> str:
    """Controller-owned `-I -B -c` runner; no env-controlled import root."""
    payload = json.dumps({"validator": str(validator), "scripts": str(scripts_root), "argv": argv})
    return (
        "import json,runpy,sys\n"
        f"_p=json.loads({payload!r})\n"
        "sys.path.insert(0,_p['scripts'])\n"
        "sys.argv=[_p['validator'],*_p['argv']]\n"
        "_code=0\n"
        "try:\n runpy.run_path(_p['validator'],run_name='__main__')\n"
        "except SystemExit as _e:\n _code=int(_e.code) if isinstance(_e.code,int) else 1\n"
        "_origins={k:getattr(v,'__file__',None) for k,v in sys.modules.items() if getattr(v,'__file__',None)}\n"
        "print('__HARNESS_VERIFIER_ORIGINS__='+json.dumps(_origins,sort_keys=True))\n"
        "raise SystemExit(_code)\n"
    )


def _observed_dependency_environment(origins: dict[str, str], verifier_root: Path,
                                     record: "TrustedVerifierRecord") -> dict[str, Any]:
    """Bind the authority modules actually imported by this strict execution."""
    manifest = _profile_manifest(record)
    modules: list[dict[str, str]] = [{"module": "harness-test", "originClass": "verifier-tree",
                                      "sha256": hashlib.sha256((verifier_root / "scripts/harness-test.py").read_bytes()).hexdigest()}]
    unexpected: list[dict[str, str]] = []
    declared = set(manifest.verifier_modules)
    for module, origin in origins.items():
        if not (module == "spec_test_gate" or module.startswith("harness_lib")):
            continue  # Declared closure inputs need not all be imported by this gate.
        actual = Path(origin).resolve()
        try:
            rel = actual.relative_to(verifier_root.resolve()).as_posix()
        except ValueError:
            unexpected.append({"module": module, "origin": str(actual)})
            continue
        modules.append({"module": module, "originClass": "verifier-tree",
                        "path": rel, "declared": str(rel in declared).lower(),
                        "sha256": hashlib.sha256(actual.read_bytes()).hexdigest()})
    payload = {"authorityModules": sorted(modules, key=lambda row: row["module"]),
               "unexpectedAuthorityOrigins": sorted(unexpected, key=lambda row: row["module"])}
    return {**payload, "ok": not unexpected, "digest": _digest(payload)}


@dataclass(frozen=True)
class TrustedVerifierRecord:
    trusted_verifier_id: str
    verifier_tree_id: str
    verifier_version: str
    promotion_epoch: str
    promoted_at: str
    promoted_by: str
    previous_trusted_verifier_id: str | None
    proof_evidence_reference: str
    status: str
    base_head: str
    required_proof_profile: str
    required_gates: tuple[str, ...]
    required_mechanisms: tuple[str, ...]
    required_policies: tuple[str, ...]
    promotion_receipt: dict[str, Any] | None = None

    @property
    def proof_profile_digest(self) -> str:
        return _digest({"id": self.required_proof_profile, "version": self.verifier_version,
                        "gates": self.required_gates, "mechanisms": self.required_mechanisms,
                        "policies": self.required_policies, "aggregation": "all-required-gates-pass"})

    @property
    def is_trusted(self) -> bool:
        return self.status == "TRUSTED"

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "TrustedVerifierRecord":
        required = ("trustedVerifierId", "verifierTreeId", "verifierVersion", "promotionEpoch",
                    "promotedAt", "promotedBy", "proofEvidenceReference", "status", "baseHead",
                    "requiredProofProfile", "requiredGates", "requiredMechanisms", "requiredPolicies")
        missing = [key for key in required if key not in row]
        if missing:
            raise HarnessError("invalid TrustedVerifierRecord missing " + ", ".join(missing))
        return cls(row["trustedVerifierId"], row["verifierTreeId"], row["verifierVersion"],
                   row["promotionEpoch"], row["promotedAt"], row["promotedBy"],
                   row.get("previousTrustedVerifierId"), row["proofEvidenceReference"], row["status"],
                   row["baseHead"], row["requiredProofProfile"], tuple(row["requiredGates"]),
                   tuple(row["requiredMechanisms"]), tuple(row["requiredPolicies"]), row.get("promotionReceipt"))

    @classmethod
    def test_only(cls, root: Path | str, base_head: str, *, required_gates: tuple[str, ...] = ("tiny",)) -> "TrustedVerifierRecord":
        """Fixture-only pin; never confers strict proof eligibility."""
        source = Path(root)
        tree = candidate_snapshot._require_git(source, "rev-parse", f"{base_head}^{{tree}}")
        return cls("TV-TEST-" + tree[:12], tree, "test-v1", "test-fixture", now(), "deterministic-fixture",
                   None, "fixture:no-historical-promotion", "TEST_ONLY", base_head, STRICT_PROOF_PROFILE,
                   required_gates, ("trusted-harness-test",), ("fixture-policy-v1",))


@dataclass(frozen=True)
class VerifierSnapshot:
    record: TrustedVerifierRecord
    verifier_path: Path
    executable_identity_digest: str


def verifier_runtime_identity(*, executable_bytes: bytes | None = None) -> dict[str, str]:
    """Stable runtime identity without making a mutable absolute path semantic."""
    executable = Path(sys.executable)
    try:
        binary = executable.read_bytes() if executable_bytes is None else executable_bytes
    except OSError as exc:
        raise HarnessError("VERIFIER_RUNTIME_IDENTITY_EXPLICIT: cannot read interpreter") from exc
    payload = {"pythonImplementation": platform.python_implementation(), "pythonVersion": platform.python_version(),
               "runtimeExecutableDigest": "sha256:" + hashlib.sha256(binary).hexdigest(),
                "platformClass": sys.platform, "architecture": platform.machine(),
                "dependencyEnvironment": "observed-at-strict-execution"}
    return {**payload, "digest": _digest(payload)}


GENESIS_PRECONDITIONS = ("subjectContract", "authorityClosure", "runtimeIdentity", "releaseFreshness",
                         "decisionBinding", "promotionReceiptValidation", "filesystemConfinement",
                         "securityReview")


def _precondition_result(name: str, state: str, reason: str, evidence_refs: list[dict[str, str]],
                         evaluator_id: str, evaluator_version: str) -> dict[str, Any]:
    payload = {"precondition": name, "state": state, "reason": reason,
               "evidenceRefs": evidence_refs, "evaluatorId": evaluator_id,
               "evaluatorVersion": evaluator_version}
    return {**payload, "resultDigest": _digest(payload), "evaluatedAt": now()}


def _registered_genesis_evaluator(name: str) -> dict[str, Any]:
    """The production registry: absence of canonical evidence is UNKNOWN, never PASS."""
    if name == "filesystemConfinement":
        # TCP-04: consult canonical, execution-bound confinement evidence
        # (sandbox_spawn is the confinement owner). PASS only for a fresh,
        # digest-verified, non-override record with real per-run application;
        # absent/stale/tampered evidence keeps the historical fail-closed
        # verdict. Root is the process CWD — harness invocations are pinned to
        # the repo root by the cwd guard.
        from harness_lib import sandbox_spawn
        root = Path.cwd()
        execution_id = sandbox_spawn.latest_confinement_evidence(root)
        if execution_id:
            ok, why = sandbox_spawn.verify_confinement_evidence(root, execution_id)
            if ok:
                return _precondition_result(
                    name, "PASS", f"verified confinement evidence for execution {execution_id}",
                    [{"kind": "confinement-evidence", "id": execution_id, "status": "verified"}],
                    "builtin-filesystem-isolation", "v2")
            return _precondition_result(
                name, "FAIL", f"FILESYSTEM_ESCAPE_BLOCKER = OPEN ({why})",
                [{"kind": "confinement-evidence", "id": execution_id, "status": "refused"}],
                "builtin-filesystem-isolation", "v2")
        return _precondition_result(
            name, "FAIL", "FILESYSTEM_ESCAPE_BLOCKER = OPEN",
            [{"kind": "filesystem-isolation", "id": FILESYSTEM_ISOLATION_AUTHORITY,
              "status": "resolved"}], "builtin-filesystem-isolation", "v1")
    if name in ("decisionBinding", "promotionReceiptValidation"):
        # TCP-06 §2.A.6: evidence-derived evaluators for the decision seams.
        # PASS only when the canonical trust store holds an active TRUSTED
        # record whose promotion binding re-verifies; absence stays UNKNOWN
        # (never PASS), errors/failed bindings are FAIL.
        try:
            record = load_trusted_verifier_record(Path.cwd())
        except HarnessError as exc:
            return _precondition_result(name, "FAIL", f"trust store fails closed: {exc}",
                                        [], "builtin-decision-evidence", "v2")
        if record is None:
            return _precondition_result(name, "UNKNOWN", "no canonical production evidence", [],
                                        "builtin-decision-evidence", "v2")
        out = validate_promotion_binding(Path.cwd(), record)
        state = "PASS" if out.get("ok") else "FAIL"
        return _precondition_result(
            name, state, str(out.get("reason") or "promotion binding re-verified"),
            [{"kind": "trusted-verifier-record", "id": record.trusted_verifier_id,
              "status": "verified" if out.get("ok") else "refused"}],
            "builtin-decision-evidence", "v2")
    return _precondition_result(name, "UNKNOWN", "no canonical production evidence", [],
                                "builtin-canonical-evidence-registry", "v1")


def genesis_activation_synthesis(root: Path | str, **kwargs: Any) -> dict[str, Any]:
    """TCP-06 final synthesis delegate: the five-seam strict conjunction lives
    in genesis_activation (composition layer); this seam only forwards. It can
    never widen eligibility — UNKNOWN preconditions keep production ineligible."""
    from harness_lib import genesis_activation
    return genesis_activation.GenesisActivationGate(root).evaluate(**kwargs)


class GenesisPreconditionEvaluator:
    """Fail-closed report derived from mechanism evidence, never caller booleans."""
    def __init__(self, mechanisms: dict[str, Any] | None = None, *, test_only: bool = False):
        if mechanisms and not test_only:
            raise HarnessError("PRODUCTION_CALLER_CANNOT_INJECT_GENESIS_PASS_MECHANISM")
        self._mechanisms = dict(mechanisms or {})
        self._test_only = test_only

    def evaluate(self) -> dict[str, Any]:
        rows: dict[str, dict[str, Any]] = {}
        for name in GENESIS_PRECONDITIONS:
            mechanism = self._mechanisms.get(name) if self._test_only else None
            if callable(mechanism):
                value = mechanism()
                if not isinstance(value, dict) or value.get("state") not in {"PASS", "FAIL", "UNKNOWN"}:
                    raise HarnessError("GENESIS_PRECONDITIONS_ARE_EVIDENCE_DERIVED")
                refs = list(value.get("evidenceRefs") or [])
                rows[name] = _precondition_result(name, value["state"], str(value.get("reason") or ""), refs,
                                                   str(value.get("evaluatorId") or "test-only-fixture"),
                                                   str(value.get("evaluatorVersion") or "v1"))
            else:
                rows[name] = _registered_genesis_evaluator(name)
        eligible = all(row["state"] == "PASS" for row in rows.values()) and not self._test_only
        return {"reportKind": "TEST_ONLY" if self._test_only else "PRODUCTION", "preconditions": rows,
                "genesisEligible": eligible}


def genesis_precondition_report() -> dict[str, Any]:
    """Compatibility reader; production callers cannot supply readiness claims."""
    return GenesisPreconditionEvaluator().evaluate()


def load_trusted_verifier_record(root: Path | str, verifier_id: str | None = None) -> TrustedVerifierRecord | None:
    path = Path(root) / TRUSTED_VERIFIER_RECORD_REL
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("invalid TrustedVerifierRecord store") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != TRUST_STORE_SCHEMA_VERSION:
        raise HarnessError("TRUST_STORE_SCHEMA_VERSIONED_AND_FAIL_CLOSED")
    rows = payload.get("records")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HarnessError("TRUST_STORE_SCHEMA_VERSIONED_AND_FAIL_CLOSED")
    records = [TrustedVerifierRecord.from_mapping(row) for row in rows]
    by_id: dict[str, TrustedVerifierRecord] = {}
    for record in records:
        prior = by_id.get(record.trusted_verifier_id)
        if prior is not None and prior != record:
            raise HarnessError("TRUST_ANCHOR_SELECTION_NOT_FILE_ORDER_DEPENDENT: conflicting duplicate id")
        by_id[record.trusted_verifier_id] = record
    for record in records:
        if record.previous_trusted_verifier_id and record.previous_trusted_verifier_id not in by_id:
            raise HarnessError("TRUST_ANCHOR_SELECTION_NOT_FILE_ORDER_DEPENDENT: unknown previous anchor")
        if record.status == "TRUSTED":
            receipt = record.promotion_receipt
            required = {"trustedVerifierId", "verifierTreeId", "verifierIdentityDigest", "proofProfileDigest",
                        "promotionEpoch", "previousTrustedVerifierId", "promotedBy", "decisionId", "decisionDigest", "promotedAt"}
            if not isinstance(receipt, dict) or not required.issubset(receipt):
                raise HarnessError("STATUS_FIELD_ALONE_CANNOT_GRANT_TRUST")
            if any(receipt.get(key) != getattr(record, {"trustedVerifierId": "trusted_verifier_id", "verifierTreeId": "verifier_tree_id", "promotionEpoch": "promotion_epoch", "previousTrustedVerifierId": "previous_trusted_verifier_id", "promotedBy": "promoted_by", "promotedAt": "promoted_at"}[key])
                   for key in ("trustedVerifierId", "verifierTreeId", "promotionEpoch", "previousTrustedVerifierId", "promotedBy", "promotedAt")):
                raise HarnessError("STATUS_FIELD_ALONE_CANNOT_GRANT_TRUST")
            if receipt.get("proofProfileDigest") != record.proof_profile_digest:
                raise HarnessError("STATUS_FIELD_ALONE_CANNOT_GRANT_TRUST")
    if verifier_id:
        return by_id.get(verifier_id)
    active = [record for record in by_id.values() if record.status == "TRUSTED"]
    if len(active) > 1:
        raise HarnessError("TRUST_ANCHOR_SELECTION_NOT_FILE_ORDER_DEPENDENT: multiple active anchors")
    return active[0] if active else None


@dataclass
class TrustedVerifier:
    """Verifier code and proof requirements pinned by an external record."""
    record: TrustedVerifierRecord
    snapshot: VerifierSnapshot

    @property
    def verifier_id(self) -> str:
        return self.record.trusted_verifier_id

    @property
    def verifier_version(self) -> str:
        return self.record.verifier_version

    @property
    def verifier_tree_id(self) -> str:
        return self.record.verifier_tree_id

    @property
    def base_head(self) -> str:
        return self.record.base_head

    @property
    def executable_identity_digest(self) -> str:
        return self.snapshot.executable_identity_digest

    @property
    def proof_profile(self) -> str:
        return self.record.required_proof_profile

    @property
    def required_gates(self) -> tuple[str, ...]:
        return self.record.required_gates

    @classmethod
    def from_record(cls, root: Path | str, record: TrustedVerifierRecord) -> "TrustedVerifier":
        source = Path(root).resolve()
        actual_tree = candidate_snapshot._require_git(source, "rev-parse", f"{record.base_head}^{{tree}}")
        if actual_tree != record.verifier_tree_id:
            raise HarnessError("TrustedVerifierRecord verifierTreeId does not match pinned baseHead")
        base = candidate_snapshot.resolve_scratch_root("trusted-verifiers", source_root=source)
        path = Path(__import__("tempfile").mkdtemp(prefix=f"TV-{actual_tree[:12]}-", dir=str(base)))
        path.rmdir()
        try:
            candidate_snapshot._materialize_execution_repository(source, actual_tree, record.base_head, path)
            validator = path / "scripts" / "harness-test.py"
            if not validator.is_file():
                raise HarnessError("TrustedVerifierRecord verifier lacks scripts/harness-test.py")
            digest = _digest({"tree": actual_tree, "authorityClosure": _authority_manifest(path, record),
                              "profile": record.proof_profile_digest,
                              "profileManifest": _profile_manifest(record).__dict__})
            if record.is_trusted:
                receipt = record.promotion_receipt or {}
                if receipt.get("verifierIdentityDigest") != digest:
                    raise HarnessError("STATUS_FIELD_ALONE_CANNOT_GRANT_TRUST")
            return cls(record, VerifierSnapshot(record, path, digest))
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise

    def cleanup(self) -> None:
        candidate_snapshot._writable(self.snapshot.verifier_path)
        shutil.rmtree(self.snapshot.verifier_path, ignore_errors=True)

    def receipt_fields(self) -> dict[str, str]:
        return {"trustedVerifierId": self.verifier_id, "trustedVerifierVersion": self.verifier_version,
                "trustedVerifierIdentityDigest": self.executable_identity_digest,
                "proofProfile": self.proof_profile, "proofProfileDigest": self.record.proof_profile_digest,
                "verifierTreeId": self.verifier_tree_id}

    def validate(self, subject: candidate_snapshot.CandidateSnapshot,
                 requested_gates: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
        """Run the verifier entrypoint from the pinned verifier tree, not subject code."""
        context = validation_context.ValidationSubjectContext.from_snapshot(subject)
        subject.verify()
        requested = None if requested_gates is None else tuple(requested_gates)
        if requested != self.required_gates:
            return {"ok": False, "reason": "CALLER_CANNOT_REDUCE_TRUSTED_PROOF_REQUIREMENTS", "gates": [],
                    "executionAuthority": "TRUSTED_VERIFIER", "proofEligibility": "INELIGIBLE", **self.receipt_fields()}
        if not self.required_gates:
            return {"ok": False, "reason": "EMPTY_CALLER_GATE_SET_CANNOT_CREATE_STRICT_PASS", "gates": [],
                    "executionAuthority": "TRUSTED_VERIFIER", "proofEligibility": "INELIGIBLE", **self.receipt_fields()}
        env = subject.execution_env()
        env.update({"HARNESS_SUBJECT_ROOT": str(subject.snapshot_path),
                    "HARNESS_VERIFIER_ROOT": str(self.snapshot.verifier_path),
                    "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"})
        validator = self.snapshot.verifier_path / "scripts" / "harness-test.py"
        def run_gate(gate: str) -> dict[str, Any]:
            subject.verify()
            process_error: BaseException | None = None
            try:
                gate_argv = gate_checks_strict_subject.gate_argv(gate, subject.snapshot_path) or [gate, "--no-project-commands"]
                proc = candidate_snapshot.processes.run_quiet(
                    [sys.executable, "-I", "-B", "-c", _trusted_bootstrap(
                        validator, self.snapshot.verifier_path / "scripts", gate_argv)], timeout=1800,
                    cwd=str(subject.snapshot_path), env=env, capture_output=True, text=True)
            except BaseException as exc:
                process_error = exc
                raise
            finally:
                try:
                    subject.verify()
                except HarnessError:
                    if process_error is not None:
                        raise
                    raise
            origins = {}
            output = proc.stdout or ""
            for line in output.splitlines():
                if line.startswith("__HARNESS_VERIFIER_ORIGINS__="):
                    origins = json.loads(line.split("=", 1)[1])
            observed = _observed_dependency_environment(origins, self.snapshot.verifier_path, self.record)
            exit_code = proc.returncode if observed["ok"] else 70
            return {"gate": gate, "argv": gate_argv, "exitCode": exit_code, "stdout": output,
                    "stderr": proc.stderr or "", "moduleOrigins": origins,
                    "observedDependencyEnvironment": observed,
                    "reason": None if observed["ok"] else "UNEXPECTED_AUTHORITY_MODULE_ORIGIN_FAILS_CLOSED"}

        rows: list[dict[str, Any]] = []
        if "parallel-dispatch" in self.record.required_mechanisms and len(self.required_gates) > 1:
            from harness_lib import gate_parallel
            by_gate: dict[str, dict[str, Any]] = {}
            def parallel_gate(gate: str) -> bool:
                by_gate[gate] = run_gate(gate)
                return by_gate[gate]["exitCode"] == 0
            try:
                verdicts = gate_parallel.verdicts_parallel(list(self.required_gates), len(self.required_gates),
                                                           parallel_gate)
            except Exception as exc:
                return {"ok": False, "reason": "PARALLEL_FAILURE_NOT_MASKED_BY_SERIAL_FALLBACK",
                        "gates": [], "executionAuthority": "TRUSTED_VERIFIER", "proofEligibility": "INELIGIBLE",
                        "parallelError": str(exc), **self.receipt_fields()}
            rows = [{**by_gate[gate], "parallelVerdict": verdicts[gate]} for gate in self.required_gates]
        else:
            for gate in self.required_gates:
                rows.append(run_gate(gate))
                if rows[-1]["exitCode"]:
                    break
        # No OS sandbox is currently available for arbitrary subject executables.
        eligible = "ELIGIBLE" if self.record.is_trusted and FILESYSTEM_ISOLATION_AUTHORITY != "CONVENTION_ONLY" else "INELIGIBLE"
        runtime = verifier_runtime_identity()
        dependency_environment = _digest([row.get("observedDependencyEnvironment", {}).get("digest", "") for row in rows])
        execution_context = _digest({**context.receipt_fields(), "trustedVerifierId": self.verifier_id,
                                      "trustedVerifierIdentityDigest": self.executable_identity_digest,
                                      "verifierRuntimeIdentityDigest": runtime["digest"],
                                      "proofProfileDigest": self.record.proof_profile_digest,
                                      "observedDependencyEnvironmentDigest": dependency_environment})
        return {"ok": all(row["exitCode"] == 0 for row in rows), "gates": rows, **context.receipt_fields(),
                "executionAuthority": "TRUSTED_VERIFIER", "proofEligibility": eligible,
                "filesystemIsolationAuthority": FILESYSTEM_ISOLATION_AUTHORITY,
                 "filesystemEscapeBlocker": FILESYSTEM_ESCAPE_BLOCKER,
                 "verifierRuntimeIdentity": runtime, "strictValidationContextDigest": execution_context,
                 "observedDependencyEnvironmentDigest": dependency_environment,
                 **self.receipt_fields()}

    @staticmethod
    def self_promotion_forbidden() -> None:
        raise HarnessError("GOVERNANCE_KERNEL_CANNOT_SELF_PROMOTE")


# ---------------------------------------------------------------------------
# TCP-03: canonical owner decision / promotion binding
# ---------------------------------------------------------------------------

PROMOTION_AUTHORIZED_EFFECT = "promote-verifier"


def validate_promotion_binding(root: Path | str, record: TrustedVerifierRecord,
                               *, test_only: bool = False) -> dict[str, Any]:
    """Fail-closed binding between a TrustedVerifierRecord's promotionReceipt
    and the CANONICAL owner decision (decision_inbox is the single decision
    seam — no second Authority or store). Structural receipt validity, or
    status=TRUSTED alone, can never mint trust here: the canonical decision is
    re-resolved and its digest recomputed on every call.

    Enforced beyond decision_inbox.validate_promotion_decision_authority:
    scope (decision subject must name this exact verifier tree), expiry
    (decision must not be expired at promotion time), authorized effect, and
    replay (a decision already consumed by another verifier fails closed).
    """
    def refuse(reason: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "reason": reason, **extra}

    receipt = record.promotion_receipt
    if not isinstance(receipt, dict):
        return refuse("PROMOTION_RECEIPT_MISSING_OR_MALFORMED")
    from harness_lib import decision_inbox  # canonical decision seam, read-only
    authority = decision_inbox.validate_promotion_decision_authority(root, receipt, test_only=test_only)
    if not authority.get("ok"):
        return refuse(str(authority.get("reason") or "DECISION_AUTHORITY_REFUSED"), authority=authority)
    binding = authority.get("decisionBinding") or {}
    if binding.get("subject") != record.verifier_tree_id:
        return refuse("DECISION_SUBJECT_DOES_NOT_BIND_THIS_VERIFIER",
                      decisionSubject=binding.get("subject"), verifierTreeId=record.verifier_tree_id)
    expires_at = binding.get("expiresAt")
    if expires_at is not None:
        import datetime
        try:
            expiry = datetime.datetime.fromisoformat(str(expires_at))
            promoted = datetime.datetime.fromisoformat(record.promoted_at)
        except ValueError:
            return refuse("DECISION_EXPIRY_UNPARSEABLE_FAILS_CLOSED", expiresAt=expires_at)
        if promoted > expiry:
            return refuse("DECISION_EXPIRED_AT_PROMOTION_TIME", expiresAt=expires_at,
                          promotedAt=record.promoted_at)
    effect = receipt.get("authorizedEffect", PROMOTION_AUTHORIZED_EFFECT)
    if effect != PROMOTION_AUTHORIZED_EFFECT:
        return refuse("DECISION_EFFECT_DOES_NOT_AUTHORIZE_PROMOTION", authorizedEffect=effect)
    consumed_by = binding.get("consumedBy")
    if consumed_by is not None and consumed_by != record.trusted_verifier_id:
        return refuse("DECISION_ALREADY_CONSUMED_BY_ANOTHER_PROMOTION", consumedBy=consumed_by)
    return {"ok": True, "authority": authority.get("authority"),
            "decisionId": receipt.get("decisionId"), "verifierTreeId": record.verifier_tree_id,
            "authorizedEffect": PROMOTION_AUTHORIZED_EFFECT}


# ---------------------------------------------------------------------------
# TCP-05: trusted CI receipt binding to the pinned verifier authority
# ---------------------------------------------------------------------------

def validate_ci_execution(root: Path | str, execution_id: str,
                          record: TrustedVerifierRecord, *,
                          expected_fingerprint: str | None = None,
                          require_confinement: bool = False) -> dict[str, Any]:
    """Fail-closed binding of a canonical TestExecutionReceipt (trusted_ci)
    to the pinned verifier authority. The CI outcome only counts under THIS
    record: a receipt minted against a different verifier tree / proof profile
    refuses, so a candidate cannot shop a green receipt across verifiers.
    Delegates receipt integrity (tamper/replay/exit/stale/confinement link)
    to the CI owner; adds only the verifier-identity binding here."""
    from harness_lib import trusted_ci  # CI evidence owner
    ok, why = trusted_ci.verify_test_receipt(
        root, execution_id, expected_fingerprint=expected_fingerprint,
        require_confinement=require_confinement)
    if not ok:
        return {"ok": False, "reason": why}
    import json as _json
    payload = _json.loads(trusted_ci._receipt_path(root, execution_id).read_text(encoding="utf-8"))
    bound = payload.get("verifier") or {}
    expected = {"trustedVerifierId": record.trusted_verifier_id,
                "verifierTreeId": record.verifier_tree_id,
                "proofProfileDigest": record.proof_profile_digest}
    mismatched = [k for k, v in expected.items() if bound.get(k) != v]
    if mismatched:
        return {"ok": False, "reason": "CI_RECEIPT_NOT_BOUND_TO_THIS_VERIFIER",
                "mismatchedFields": mismatched}
    return {"ok": True, "executionId": execution_id,
            "trustedVerifierId": record.trusted_verifier_id,
            "proofProfileDigest": record.proof_profile_digest}


# ---------------------------------------------------------------------------
# TCP-07: recovery-retirement receipt binding to the pinned verifier authority
# ---------------------------------------------------------------------------

def validate_recovery_retirement(root: Path | str, root_oid: str,
                                 record: TrustedVerifierRecord, *,
                                 root_envelope: dict[str, Any] | None = None,
                                 now: float | None = None) -> dict[str, Any]:
    """Fail-closed binding of a TCP-07 RecoveryRetirementReceipt to the pinned
    verifier authority. Seal integrity + owner-envelope re-derivation stay with
    the retirement owner; this seam adds only the verifier-identity binding, so
    a retirement receipt minted against a different verifier tree cannot be
    shopped in to satisfy THIS record. Absent/forged/unbound => not retired."""
    from harness_lib import recovery_retirement
    verifier = {"trustedVerifierId": record.trusted_verifier_id,
                "verifierTreeId": record.verifier_tree_id,
                "proofProfileDigest": record.proof_profile_digest}
    ok, why = recovery_retirement.verify_retirement_receipt(
        root, root_oid, root_envelope=root_envelope, verifier=verifier, now=now)
    if not ok:
        return {"ok": False, "reason": why}
    return {"ok": True, "rootOid": root_oid, "retired": True,
            "trustedVerifierId": record.trusted_verifier_id}

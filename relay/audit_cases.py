"""Finite audit custody. Provider execution is explicit and delegated to Kernel.

Consumes tare_ops audit-runner artifacts under explicit process trust. Hashes
detect changed bytes, not a malicious producer forging an entire evidence set.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime

from relay.landing_policy import attest_identity
from relay.relay_io import write_json_atomic, write_text_atomic


ACCEPT = {"QUALIFIED_BOUNDED", "ACCEPTED_WITH_EXPLICIT_RESIDUALS"}
TERMINAL = {"CLOSED", "CLOSED_BY_USER", "INCONCLUSIVE_WITH_REVISIT_TRIGGER"}
MAX_BYTES = 2 * 1024 * 1024
FROZEN = ("project", "stage", "objective", "non_goals", "obligations", "scope",
          "implementer", "auditor", "limits", "reopen")


def require(condition, code):
    if not condition:
        raise ValueError(code)


def validate_limits(limits):
    require(isinstance(limits, dict) and set(limits) == {'attempts', 'context_tokens'}
            and (limits['attempts'] is None or type(limits['attempts']) is int and limits['attempts'] > 0)
            and (limits['context_tokens'] is None or
                 type(limits['context_tokens']) is int and limits['context_tokens'] > 0),
            'invalid_bounded_limits')


def attempt_limit_reached(limits, number):
    return limits['attempts'] is not None and number >= limits['attempts']


def context_limit_reached(limits, observed):
    return limits['context_tokens'] is not None and observed is not None and observed > limits['context_tokens']


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value),
            "invalid_identifier")
    return value


def checksum(raw):
    return sha256(raw).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except RecursionError as error:
        raise ValueError('artifact_json_too_deep') from error


def bounded(path, limit=MAX_BYTES):
    # Atomic replacement can briefly deny readers on Windows, just as an open
    # reader can deny the writer. Match the writer's finite 310 ms retry budget;
    # never turn missing, corrupt, oversized or permanently denied state into
    # an empty/default case.
    for delay in (0.01, 0.02, 0.04, 0.08, 0.16, None):
        try:
            with Path(path).open("rb") as source:
                raw = source.read(limit + 1)
        except OSError as error:
            retryable = (getattr(error, "winerror", None) in {5, 32}
                         or (sys.platform == "win32" and isinstance(error, PermissionError)
                             and getattr(error, "winerror", None) is None))
            if delay is None or not retryable:
                raise
            time.sleep(delay)
            continue
        require(len(raw) <= limit, "artifact_over_limit")
        return raw


def strings(value, nonempty=False):
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(item, str) and item.strip() for item in value))


def digest_string(value, size=64):
    return isinstance(value, str) and re.fullmatch(f"[a-f0-9]{{{size}}}", value)


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".active.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, str(os.getpid()).encode())
        yield
    finally:
        os.close(descriptor)
        path.unlink()


class AuditCases:
    def __init__(self, store: Path, roster: Path):
        self.store, self.roster = Path(store), Path(roster)

    def path(self, case_id):
        return self.store / (identifier(case_id) + ".json")

    def load(self, case_id):
        # Never interpret a corrupt existing case as permission to start again.
        state = decode(bounded(self.path(case_id), 32 * MAX_BYTES))
        require(state["schema"] in {"tare.tools/audit-case/1", "tare.tools/audit-case/2"}
                and state["case_id"] == case_id,
                "invalid_case")
        for attempt in state["attempts"]:
            if "packet" in attempt:
                from relay.audit_packet import verify
                verify(attempt["packet"])
            for artifact in attempt.get("artifacts", {}).values():
                raw = base64.b64decode(artifact["bytes"], validate=True)
                require(checksum(raw) == artifact["sha256"], "stored_artifact_changed")
        return state

    def validate(self, request):
        identifier(request["case_id"])
        require(request["stage"] in {"plan", "code"}, "invalid_stage")
        for key in ("project", "objective"):
            require(isinstance(request[key], str) and request[key].strip(), "missing_" + key)
        require(strings(request["obligations"], True), "finite_obligations_required")
        require(strings(request["non_goals"]), "invalid_non_goals")
        require(len(set(request["obligations"])) == len(request["obligations"]), "duplicate_obligation")
        candidate = request["candidate"]
        require(set(candidate) == {"commit", "tree"} and
                all(digest_string(value, 40) for value in candidate.values()), "invalid_candidate")
        require(set(request["scope"]) == {"nodes", "trains"} and
                strings(request["scope"]["nodes"], True) and request["scope"]["trains"] == [],
                "unsupported_runner_scope")
        for node in request["scope"]["nodes"]:
            identifier(node)
        require(digest_string(request["test_execution_sha256"]), "invalid_test_receipt_digest")
        artifacts = request["candidate_artifacts"]
        require(isinstance(artifacts, list) and bool(artifacts), "missing_candidate_artifacts")
        paths = set()
        for artifact in artifacts:
            name = artifact["path"]
            require(isinstance(name, str) and name and not Path(name).is_absolute() and
                    ".." not in Path(name).parts and name not in paths, "invalid_candidate_path")
            paths.add(name)
            require(set(artifact) == {"path", "git_blob", "sha256", "size_bytes", "role", "provenance"}
                    and digest_string(artifact["git_blob"], 40) and digest_string(artifact["sha256"])
                    and type(artifact["size_bytes"]) is int and artifact["size_bytes"] >= 0
                    and artifact["role"] == "candidate-artifact"
                    and artifact["provenance"] == "git-tree:" + candidate["tree"], "invalid_candidate_artifact")
        limits = request["limits"]
        validate_limits(limits)
        return self.identities(request)

    def identities(self, request):
        identities = {}
        for role in ("implementer", "auditor"):
            identities[role] = attest_identity(roster_path=self.roster, role=role, **request[role])
        require(identities["implementer"].bias_domain != identities["auditor"].bias_domain,
                "independent_lineage_required")
        return {role: dict(identity.payload) for role, identity in identities.items()}

    def begin_packet(self, packet, request, resume=False):
        """Opt-in v2 uses the same finite store, not a v1 admission conversion."""
        from relay.audit_packet import budget, correction, recheck, seal, verify, check_symbol_continuity
        verify(packet)
        recheck(packet)
        request = decode(json_bytes(request))
        case_id = identifier(request["case_id"])
        with locked(self.store):
            state = self.load(case_id) if resume else None
            if state:
                require(state["schema"] == "tare.tools/audit-case/2", "explicit_protocol_migration_required")
                require(state["status"] not in TERMINAL and state["status"] != "RUNNING",
                        "case_terminal_or_attempt_in_flight")
                request = {**state["anchor"], **request}
            limits = request.get("limits", {"attempts": None, "context_tokens": None})
            validate_limits(limits)
            require(request["stage"] in {"plan", "code"} and isinstance(request["project"], str)
                    and request["project"].strip(), "invalid_case_stage_or_project")
            identities = self.identities(request)
            work_ids = request.get("work_ids", [])
            require(strings(work_ids) and len(work_ids) == len(set(work_ids)), "invalid_work_ids")
            for work_id in work_ids:
                identifier(work_id)
            view = packet["input"]
            anchor = {key: request[key] for key in ("project", "stage", "implementer", "auditor")}
            anchor.update(limits=limits, work_ids=work_ids, reopen=request.get("reopen"),
                intent=view["intent"], scope=view["scope"],
                non_goals=view["non_goals"], obligations=[
                    {key: item[key] for key in ("id", "claim", "falsify")} for item in view["obligations"]],
                repositories=[{"id": repo["id"], "root": repo["root"]} for repo in packet["repositories"]])
            # Existing sealed cases keep their original transport. New cases
            # independently explore by default; null explicitly selects legacy.
            if state is None or 'exploration' in state['anchor']:
                from relay.audit_independent import configuration
                default_access = state['anchor'].get('exploration') if state else {'transport': 'native-cli'}
                anchor['exploration'] = configuration(request.get('exploration', default_access))
            if state:
                require(anchor == state["anchor"] and identities == state["identities"], "case_scope_changed")
                require(not attempt_limit_reached(limits, len(state['attempts'])), 'attempt_budget_exhausted')
                previous = state["attempts"][-1]
                check_symbol_continuity(packet, previous['packet'])
                require(packet["input"]["packet_id"] not in {
                    item["packet"]["input"]["packet_id"] for item in state["attempts"]}, "packet_id_reused")
                reason = request.get("continuation", {})
                require(isinstance(reason, dict) and isinstance(reason.get("reason"), str)
                        and reason["reason"].strip(), "material_continuation_required")
                if (previous.get("result", {}).get("review") or {}).get("findings"):
                    require("correction" in request, "implementer_dispositions_required")
                    packet = correction(packet, previous["packet"], previous["result"]["review"],
                                        request["correction"], previous['result'].get('evidence_sources', ()))
                reset = request.get("session_reset")
                if reset is not None:
                    require(isinstance(reset, dict) and set(reset) == {"expected_session_id", "reason"}
                            and state["session_id"] is not None
                            and reset["expected_session_id"] == state["session_id"]
                            and isinstance(reset["reason"], str) and reset["reason"].strip(),
                            "explicit_session_reset_reason_required")
                    state.setdefault("session_history", []).append({
                        "session_id": state["session_id"], "after_attempt": len(state["attempts"]),
                        "reason": reset["reason"]})
                    state["session_id"] = None
                    if previous.get("result", {}).get("review") is not None:
                        packet = decode(json_bytes(packet))
                        if 'previous_review_archive' in packet:
                            from relay.audit_packet import review_navigation
                            packet['input']['previous_review'] = review_navigation(packet['previous_review_archive'])
                        else:
                            packet["input"]["previous_review"] = previous["result"]["review"]
                        packet["predecessor_sha256"] = previous["packet"]["sha256"]
                        packet = seal(packet)
                        verify(packet)
            else:
                require(not self.path(case_id).exists(), "case_already_exists")
                if anchor["reopen"] is not None:
                    reopen = anchor["reopen"]
                    require(isinstance(reopen, dict) and set(reopen) == {"case_id", "reason"}
                            and isinstance(reopen["reason"], str) and reopen["reason"].strip(),
                            "reopen_reason_required")
                    old = self.load(reopen["case_id"])
                    require(old["status"] in TERMINAL and old["anchor"]["project"] == anchor["project"],
                            "invalid_predecessor")
                state = {"schema": "tare.tools/audit-case/2", "case_id": case_id,
                         "anchor": anchor, "identities": identities, "attempts": [],
                         "session_id": None, "memory": None}
            state["attempts"].append({"packet": packet, "status": "RUNNING",
                                      "continuation_reason": request["continuation"]["reason"] if state['attempts'] else None})
            state["status"] = "RUNNING"
            write_json_atomic(self.path(case_id), state)
            return {**self.summary(state), "runner_action": "resume" if state["session_id"] else "run",
                    "session_id": state["session_id"], "packet_sha256": packet["sha256"],
                    **budget(packet)}

    def begin(self, request, resume=False):
        # Detach caller-owned objects. Explicit defaults are frozen with the case.
        request = decode(json_bytes(request))
        with locked(self.store):
            path = self.path(request["case_id"])
            if resume:
                state = self.load(request["case_id"])
                require(state["schema"] == "tare.tools/audit-case/1", "use_packet_protocol")
                require(state["status"] not in TERMINAL, "case_closed_create_explicit_successor")
                require(state["status"] != "RUNNING", "attempt_in_flight_or_interrupted")
                # Corrections need only the changed subject and a reason, not a
                # manually recopied anchor/roster/obligation packet.
                previous_request = state["attempts"][-1]["request"]
                request = {**state["anchor"], **{key: previous_request[key] for key in
                           ("candidate", "scope", "candidate_artifacts", "test_execution_sha256")}, **request}
            request.setdefault("limits", {"attempts": None, "context_tokens": None})
            request.setdefault("reopen", None)
            identities = self.validate(request)
            if resume:
                require(all(request[key] == state["anchor"][key] for key in FROZEN), "case_scope_changed")
                require(identities == state["identities"], "identity_or_roster_changed")
                previous = state["attempts"][-1]
                reason = request.get("continuation", {})
                expected = "correction" if previous["status"] == "REJECTED" else "execution_recovery"
                require(reason.get("kind") == expected and isinstance(reason.get("reason"), str)
                        and reason["reason"].strip(), "material_continuation_required")
                if "affected_obligations" in reason:
                    require(strings(reason["affected_obligations"], True) and
                            set(reason["affected_obligations"]) <= set(request["obligations"]),
                            "invalid_affected_obligations")
                if "witness_sha256" in reason:
                    require(digest_string(reason["witness_sha256"]), "invalid_witness_reference")
                require(not attempt_limit_reached(request['limits'], len(state['attempts'])),
                        'attempt_budget_exhausted')
            else:
                require(not path.exists(), "case_already_exists")
                if request["reopen"] is not None:
                    reopen = request["reopen"]
                    old = self.load(reopen["case_id"])
                    require(old["status"] in TERMINAL and old["anchor"]["project"] == request["project"],
                            "invalid_predecessor")
                    require(isinstance(reopen.get("reason"), str) and reopen["reason"].strip(), "reopen_reason_required")
                    if "witness_sha256" in reopen:
                        require(digest_string(reopen["witness_sha256"]), "invalid_witness_reference")
                state = {"schema": "tare.tools/audit-case/1", "case_id": request["case_id"],
                         "anchor": {key: request[key] for key in FROZEN}, "identities": identities,
                         "attempts": [], "session_id": None, "memory": None}
            feature = request["case_id"] + "." + request["stage"]
            runner_request = {key: request[key] for key in
                              ("candidate", "scope", "candidate_artifacts", "test_execution_sha256")}
            runner_request.update(feature=feature, brief=json.dumps({
                "objective": request["objective"], "non_goals": request["non_goals"],
                "obligations": request["obligations"], "stage": request["stage"],
                "continuation": request.get("continuation"), "reopen": request["reopen"],
                "rule": "Verify all obligations; memory is navigation, not approval. No new tasks. "
                        "Separate blockers, accepted residuals and suggestions. Unknown is not PASS."
            }, ensure_ascii=False))
            require(len(json_bytes(runner_request)) <= 16000, "request_context_over_limit")
            state["attempts"].append({"request": runner_request, "status": "RUNNING"})
            state["status"] = "RUNNING"
            write_json_atomic(path, state)
            return {**self.summary(state), "runner_action": "resume" if state["session_id"] else "run",
                    "runner_request": runner_request, "session_id": state["session_id"]}

    def record(self, case_id, attempt_number, run):
        """Import an explicit run directory, never follow manifest-provided paths."""
        run = Path(run)
        with locked(self.store):
            state = self.load(case_id)
            require(state["status"] == "RUNNING" and attempt_number == len(state["attempts"]),
                    "stale_or_completed_attempt")
            attempt = state["attempts"][-1]
            request = attempt["request"]
            snapshots = {}

            def take(name, limit=MAX_BYTES):
                path = run / name
                require(not path.is_symlink(), "artifact_symlink")
                raw = bounded(path, limit)
                snapshots[name] = {"sha256": checksum(raw), "bytes": base64.b64encode(raw).decode("ascii")}
                return raw

            manifest = decode(take("manifest.json"))
            launch = decode(take("launch.json"))
            prompt = take("prompt.txt", 24000).decode("utf-8")
            require(decode(prompt.rsplit("\nEXACT REQUEST:\n", 1)[1].encode()) == request,
                    "request_binding_mismatch")
            require(manifest["feature"] == request["feature"] and
                    all(launch["candidate"][key] == request["candidate"][key] for key in ("commit", "tree")),
                    "candidate_or_case_mismatch")
            session = manifest["session_id"]
            if attempt.get("execution"):
                require(session == attempt["execution"]["session_id"], "dispatched_session_mismatch")
            require(isinstance(session, str) and session and launch["session_id"] == session and
                    state["session_id"] in (None, session), "session_mismatch")
            arguments = launch["arguments"]
            model = state["identities"]["auditor"]["runtime_model"]
            require(arguments.count("--model") == 1 and arguments[arguments.index("--model") + 1] == model,
                    "requested_model_mismatch")
            require(manifest["execution_status"] in {"completed", "failed"}, "execution_not_finished")
            warnings = []
            for key, name, limit in (("result", "result.json", MAX_BYTES), ("decision", "decision.json", MAX_BYTES),
                                     ("report", "report.md", 64000), ("memory", "memory.md", 8000)):
                if key not in manifest.get("artifacts", {}):
                    continue
                try:
                    raw = take(name, limit)
                    require(checksum(raw) == manifest["artifact_sha256"][key], "artifact_hash_mismatch")
                except (OSError, ValueError, KeyError):
                    if key not in {"report", "memory"}:
                        raise
                    snapshots.pop(name, None)
                    warnings.append(name + ":not_imported")
            decision = None
            valid = manifest["execution_status"] == "completed" and manifest.get("result_valid") is True
            result = None
            if "result.json" in snapshots:
                try:
                    result = decode(base64.b64decode(snapshots["result.json"]["bytes"]))
                except (ValueError, RecursionError) as error:
                    if valid:
                        raise ValueError(str(error)) from error
            if valid:
                decision = decode(base64.b64decode(snapshots["decision.json"]["bytes"]))
                require(isinstance(result, dict) and isinstance(decision, dict),
                        "provider_result_not_object")
                require(result.get("type") == "result" and result.get("is_error") is False and
                        result.get("subtype") == "success" and result.get("session_id") == session and
                        result.get("structured_output") == decision and result.get("api_error_status") != 429
                        and manifest.get("error") is None, "original_result_mismatch")
                require(set(decision) == {"schema", "role", "candidate_tree", "candidate_artifacts",
                        "test_execution_sha256", "auditor_profile", "audit_method", "verdict", "scope",
                        "blocking_findings", "residual_findings", "issued_at"}, "invalid_decision_fields")
                require(decision.get("schema") == "tare.tools/audit-verdict/3.0" and
                        decision.get("role") == "independent-auditor" and
                        decision.get("candidate_tree") == request["candidate"]["tree"] and
                        all(decision.get(key) == request[key] for key in
                            ("scope", "candidate_artifacts", "test_execution_sha256")), "decision_subject_mismatch")
                require(decision.get("verdict") in ACCEPT | {"REJECTED", "NO_GO"} and
                        decision["verdict"] == manifest["decision"] and
                        strings(decision.get("blocking_findings")) and strings(decision.get("residual_findings")),
                        "invalid_decision")
                require(all(isinstance(decision[key], str) and decision[key].strip()
                            for key in ("auditor_profile", "audit_method", "issued_at")), "invalid_decision_metadata")
                require(datetime.fromisoformat(decision["issued_at"].replace("Z", "+00:00")).tzinfo is not None,
                        "issuance_timezone_required")
            peak = manifest.get("peak_context_tokens")
            require(peak is None or type(peak) is int and peak >= 0, "invalid_context_metric")
            if peak is None:
                warnings.append("context_usage_unknown")
            status = "EXECUTION_FAILED" if not valid else "REJECTED"
            if valid and decision["verdict"] in ACCEPT and not decision["blocking_findings"]:
                status = "CLOSED"
            attempt.update(status=status, artifacts=snapshots, decision=decision,
                           execution_error=manifest.get("error"), warnings=warnings, peak_context_tokens=peak)
            # A reservation alone does not establish a resumable vendor session.
            if isinstance(result, dict) and result.get("type") == "result" and result.get("session_id") == session:
                state["session_id"] = session
            if "memory.md" in snapshots and valid:
                state["memory"] = snapshots["memory.md"]
            state["status"] = status
            if status != "CLOSED" and (attempt_limit_reached(state['anchor']['limits'], attempt_number) or
                                        context_limit_reached(state['anchor']['limits'], peak)):
                state["status"] = "INCONCLUSIVE_WITH_REVISIT_TRIGGER"
                state["revisit_trigger"] = "Explicit successor after budget/context review; supply reason and evidence digest."
            write_json_atomic(self.path(case_id), state)
            return self.summary(state)

    def hold(self, case_id, attempt_number, reason, witness=None):
        """Explicit finite stop after interrupted execution or invalid evidence.

        This does not kill/retry a process or certify that it has exited. The
        operator must diagnose the producer; late results cannot close this case.
        """
        require(isinstance(reason, str) and reason.strip(), "hold_reason_required")
        require(witness is None or digest_string(witness), "invalid_witness_reference")
        with locked(self.store):
            state = self.load(case_id)
            require(state["status"] == "RUNNING" and attempt_number == len(state["attempts"]),
                    "stale_or_completed_attempt")
            state["attempts"][-1].update(status="INTERRUPTED_OR_INVALID", reason=reason, witness_sha256=witness)
            state["status"] = "INCONCLUSIVE_WITH_REVISIT_TRIGGER"
            state["revisit_trigger"] = "Diagnose producer and invalid evidence before an explicit successor."
            write_json_atomic(self.path(case_id), state)
            return self.summary(state)

    def cancel_packet(self, case_id, attempt_number):
        with locked(self.store):
            state = self.load(case_id)
            require(state["schema"] == "tare.tools/audit-case/2" and state["status"] == "RUNNING"
                    and len(state["attempts"]) == attempt_number, "stale_or_completed_attempt")
            execution = state["attempts"][-1].get("execution")
            require(execution is not None, "execution_not_started")
            # Cooperative cancellation: the owner kills its own child, never a PID
            # inferred from an old receipt (which could have been reused).
            write_json_atomic(Path(execution["run"]) / "cancel.request", {"attempt": attempt_number})
            return {**self.summary(state), "cancellation": "REQUESTED"}

    def reconcile_packet(self, case_id):
        """Recover a published result, or classify a proven-dead supervisor.

        No launch, no transcript scan, no stale-lock removal, no PID-based kill.
        """
        import psutil
        with locked(self.store):
            state = self.load(case_id)
            require(state["schema"] == "tare.tools/audit-case/2", "use_packet_protocol")
            if state["status"] != "RUNNING":
                return self.summary(state)
            last = state["attempts"][-1]
            execution = last.get("execution", {})
            dispatch = last.get("dispatch", {})
            pid = execution.get("supervisor_pid")
            if not pid and dispatch:
                path = Path(dispatch["spec"]).with_suffix(".spawn.json")
                if path.exists():
                    pid = decode(bounded(path, 2048)).get("pid")
            if not pid or psutil.pid_exists(pid):
                return {**self.summary(state), "reconciliation": "PROCESS_LIVE_OR_UNKNOWN"}
            run = Path(execution["run"]) if execution else None
            result = {"schema": "tare.tools/audit-result/1", "case_id": case_id,
                    "attempt": len(state["attempts"]), "run_id": execution.get("run_id", dispatch.get("run_id")),
                    "packet_id": last["packet"]["input"]["packet_id"], "packet_sha256": last["packet"]["sha256"],
                    "execution_status": "FAILED", "review_status": "MISSING", "review": None,
                    "reason": "supervisor_exited_without_result", "next_action": "DIAGNOSE_EXECUTION",
                    "authority": "NONE"}
            try:
                from relay.audit_packet import validate_review
                review = None
                if run and (run / "result.json").exists():
                    raw = bounded(run / "result.json", MAX_BYTES)
                    original = run / "result.before-reconcile.json"
                    if not original.exists():
                        original.write_bytes(raw)
                    recovered = decode(raw)
                    require(isinstance(recovered, dict)
                            and recovered.get("schema") == "tare.tools/audit-result/1"
                            and recovered.get("packet_sha256") == last["packet"]["sha256"]
                            and recovered.get("case_id") == case_id
                            and recovered.get("attempt") == len(state["attempts"])
                            and recovered.get("session_id") == execution["session_id"],
                            "reconciled_result_binding_mismatch")
                    require(recovered.get("review_status") in {"VALID", "PARTIAL", "INVALID", "MISSING"},
                            "invalid_recovered_review_status")
                    if recovered["review_status"] == "INVALID":
                        result["review_status"] = "INVALID"
                    else:
                        review = recovered.get("review")
                elif run and (run / "draft.json").exists():
                    draft = decode(bounded(run / "draft.json", 16000))
                    require(isinstance(draft, dict) and draft.get("packet_sha256") == last["packet"]["sha256"]
                            and draft.get("session_id") == execution["session_id"]
                            and draft.get("model") == state["identities"]["auditor"]["runtime_model"],
                            "reconciled_draft_binding_mismatch")
                    review = draft["review"]
                if review is not None:
                    extra_sources = []
                    manifest = run / 'exploration' / 'manifest.json' if run else None
                    if manifest is not None and manifest.exists():
                        from relay.audit_independent import collect_access
                        extra_sources, exploration = collect_access(manifest)
                        result['evidence_sources'] = extra_sources
                        result['metrics'] = {'exploration': exploration}
                    validate_review(last["packet"], review, extra_sources)
                    result.update(review=review, review_status="PARTIAL",
                                  identity_assurance="recovered_process_artifact")
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
                result.update(review_status="INVALID", review=None,
                              reason="invalid_recovery_artifact:" + str(error)[:160])
            last["result"] = result
            last["status"] = "EXECUTION_FAILED"
            # A dead supervisor is not proof that an orphan vendor child exited.
            # Hold instead of authorizing another attempt against a live session.
            state["status"] = "INCONCLUSIVE_WITH_REVISIT_TRIGGER"
            state["revisit_trigger"] = "Diagnose any orphan provider before an explicit successor."
            if run:
                write_text_atomic(run / "result.json", json_bytes(last["result"]).decode("utf-8"))
            write_json_atomic(self.path(case_id), state)
            return self.summary(state)

    def user_decision(self, case_id, attempt_number, decision):
        """Explicit operator action; preserve the real review, never manufacture PASS."""
        fields = {'decision', 'statement', 'operator_label', 'expected_result_sha256'}
        require(isinstance(decision, dict) and fields <= set(decision)
                and set(decision) <= fields | {'limits'}, 'invalid_user_decision')
        require(decision['decision'] in {'ACCEPT_RISK', 'REQUIRE_CHANGES', 'STOP'}
                and all(isinstance(decision[k], str) and decision[k].strip()
                        for k in ('statement', 'operator_label')), 'explicit_user_statement_required')
        with locked(self.store):
            state = self.load(case_id)
            require(state['schema'] == 'tare.tools/audit-case/2' and state['status'] != 'RUNNING'
                    and len(state['attempts']) == attempt_number, 'stale_or_running_user_decision')
            result = state['attempts'][-1].get('result')
            require(result is not None and checksum(json_bytes(result)) == decision['expected_result_sha256'],
                'user_decision_result_changed')
            if 'limits' in decision:
                validate_limits(decision['limits'])
                decision = {**decision, 'previous_limits': state['anchor']['limits']}
                state['anchor']['limits'] = decision['limits']
            state.setdefault('user_decisions', []).append({**decision, 'attempt': attempt_number,
                'authority': 'USER_EXPLICIT', 'recorded_at': datetime.now().astimezone().isoformat()})
            state['status'] = 'REJECTED' if decision['decision'] == 'REQUIRE_CHANGES' else 'CLOSED_BY_USER'
            write_json_atomic(self.path(case_id), state)
            return self.summary(state)

    @staticmethod
    def summary(state):
        last = state["attempts"][-1]
        if state["schema"] == "tare.tools/audit-case/2":
            result = last.get("result", {})
            return {"schema": "tare.tools/audit-status/1", "case_id": state["case_id"],
                    "attempt": len(state["attempts"]), "status": state["status"],
                    "session_id": state["session_id"], "packet_id": last["packet"]["input"]["packet_id"],
                    "run_id": last.get("execution", last.get("dispatch", {})).get("run_id"),
                    "execution_status": result.get("execution_status"),
                    "review_status": result.get("review_status"), "next_action": result.get("next_action"),
                    "finding_count": len((result.get("review") or {}).get("findings", [])), "authority": "NONE",
                    'result_sha256': checksum(json_bytes(result)) if result else None,
                    'user_decision': ({key: state['user_decisions'][-1][key]
                        for key in ('decision', 'attempt', 'authority')} if state.get('user_decisions') else None)}
        return {"case_id": state["case_id"], "status": state["status"], "attempt": len(state["attempts"]),
                "decision": (last.get("decision") or {}).get("verdict"),
                "execution_error": last.get("execution_error"), "warnings": last.get("warnings", []),
                "revisit_trigger": state.get("revisit_trigger"),
                "authority": "NONE", "identity_assurance": "request_pinned_process_trusted"}


def frugality(state):
    """Read-only bounded telemetry projection; never interpret it as a verdict."""
    rows = []
    for number, attempt in enumerate(state['attempts'], 1):
        result = attempt.get('result') or {}
        metrics = (result.get('metrics') or {}) if state['schema'] == 'tare.tools/audit-case/2' else {}
        usage = metrics.get('provider_token_usage') or {}
        counts = [counts.get(key) for counts in usage.values() if isinstance(counts, dict)
                  for key in ('inputTokens', 'outputTokens')]
        tokens = sum(counts) if counts and all(type(n) is int and n >= 0 for n in counts) else None
        duration = metrics.get('duration_ms')
        cost = metrics.get('cost_estimate_usd')
        continuation_reason = attempt.get('continuation_reason')
        continuation = attempt.get('continuation')
        if continuation is None and state['schema'] == 'tare.tools/audit-case/1':
            # Legacy requests retain the continuation inside the runner brief.
            try:
                continuation = json.loads(attempt['request']['brief']).get('continuation')
            except (KeyError, ValueError, TypeError):
                pass
        if continuation_reason is None and number > 1 and isinstance(continuation, dict):
            continuation_reason = continuation.get('reason')
        rows.append({'attempt': number, 'status': attempt.get('status'),
                     'continuation_reason': continuation_reason,
                     'duration_ms': duration.get('value') if isinstance(duration, dict) else None,
                     'token_count': tokens,
                     'cost_estimate_usd': cost.get('value') if isinstance(cost, dict) else None})
    return {'case_id': state['case_id'], 'status': state['status'], 'authority': 'NONE',
            'reopen': state['anchor'].get('reopen'), 'attempts': rows,
            'attempt_count': len(rows), 'recheck_count': max(0, len(rows) - 1),
            'false_blocks_adjudicated': None, 'reopens_without_new_information': None,
            'needs_adjudication': len(rows) > 1}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "preflight", "open", "resume", "record", "hold", "status",
                                          "result", "handoff", "execute", "launch", "cancel", "reconcile", "qualify",
                                          "user-decision", "frugality"])
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--roster", type=Path, default=Path(__file__).with_name("round_table_roster.json"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--witness-sha256")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--test-receipt", type=Path)
    parser.add_argument("--cli", nargs="+")
    parser.add_argument("--max-usd", type=float,
                        help="Optional provider soft cost limit in USD; omitted means no monetary cap.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--model")
    parser.add_argument('--transport', choices=['subscription', 'openrouter'], default='subscription',
                        help='OpenRouter reuses the Round Table fallback credentials with the same auditor model.')
    parser.add_argument("--include-artifacts", action="store_true")
    args = parser.parse_args(argv)
    cases = AuditCases(args.store, args.roster)
    try:
        if args.action == 'user-decision':
            value = cases.user_decision(args.case, args.attempt, decode(bounded(args.request, 12000)))
        elif args.action == 'preflight':
            from relay.audit_packet import verify, recheck
            from relay.audit_preflight import inspect
            require(args.packet is not None, 'packet_required')
            packet = verify(decode(bounded(args.packet)))
            recheck(packet)
            value = inspect(packet)
        elif args.action == "qualify":
            from compute_plane.audit_driver import ClaudeAuditDriver
            from relay.audit_execution import qualify_packet
            require(args.packet is not None and args.run is not None and args.cli and args.model,
                    "explicit_qualification_inputs_required")
            driver = ClaudeAuditDriver(args.cli)
            if args.transport == 'openrouter':
                from relay.audit_fallback import openrouter_driver
                driver = openrouter_driver(args.cli, args.model)
            value = qualify_packet(decode(bounded(args.packet)), run=args.run, driver=driver,
                                   model=args.model, effort=args.effort, max_usd=args.max_usd, timeout=args.timeout)
        elif args.action == "prepare":
            from relay.audit_packet import budget, prepare
            packet = prepare(decode(bounded(args.request, 64000)))
            require(args.output is not None and not args.output.exists(), "new_packet_output_required")
            require(not any(args.output.resolve().is_relative_to(Path(repo["root"]))
                            for repo in packet["repositories"]), "packet_output_inside_candidate")
            write_json_atomic(args.output, packet)
            value = {"packet": str(args.output.resolve()), "sha256": packet["sha256"],
                     **budget(packet), "authority": "NONE"}
        elif args.action in {"execute", "launch"}:
            from compute_plane.audit_driver import ClaudeAuditDriver
            from relay.audit_execution import execute, execute_packet, launch_packet
            require(args.cli, "explicit_execution_settings_required")
            if cases.load(args.case)["schema"] == "tare.tools/audit-case/2":
                driver = ClaudeAuditDriver(args.cli)
                if args.transport == 'openrouter':
                    from relay.audit_fallback import openrouter_driver
                    driver = openrouter_driver(args.cli, cases.load(args.case)['identities']['auditor']['runtime_model'])
                operation = launch_packet if args.action == "launch" else execute_packet
                value = operation(cases, args.case, args.attempt, driver=driver,
                                  max_usd=args.max_usd, timeout=args.timeout, effort=args.effort)
            else:
                require(args.transport == 'subscription', 'gateway_requires_packet_protocol')
                require(args.action == "execute", "background_requires_packet_protocol")
                require(args.root is not None and args.test_receipt is not None, "execution_inputs_required")
                value = execute(cases, args.case, args.attempt, root=args.root, receipt=args.test_receipt,
                                driver=ClaudeAuditDriver(args.cli), max_usd=args.max_usd, timeout=args.timeout)
        elif args.action in {"open", "resume"}:
            request = decode(bounded(args.request, 24000))
            value = (cases.begin_packet(decode(bounded(args.packet)), request, args.action == "resume")
                     if args.packet else cases.begin(request, args.action == "resume"))
        elif args.action == "record":
            value = cases.record(args.case, args.attempt, args.run)
        elif args.action == "hold":
            value = cases.hold(args.case, args.attempt, args.reason, args.witness_sha256)
        elif args.action == "cancel":
            value = cases.cancel_packet(args.case, args.attempt)
        elif args.action == "reconcile":
            value = cases.reconcile_packet(args.case)
        else:
            state = cases.load(args.case)
            value = frugality(state) if args.action == 'frugality' else cases.summary(state)
            if args.action in {"result", "handoff"} and state["schema"] == "tare.tools/audit-case/2":
                value = state["attempts"][-1].get("result", value)
                if state.get('user_decisions'):
                    value = {**value, 'user_decision': state['user_decisions'][-1]}
                if args.include_artifacts:
                    execution = state['attempts'][-1].get('execution', {})
                    value = {**value, 'artifact_refs': {
                        'result': str(Path(execution['run']) / 'result.json') if execution.get('run') else None,
                        'exploration_reports': [item['artifact_path'] for item in
                            value.get('metrics', {}).get('exploration', {}).get('reports', [])]}}
            elif args.action == "result":
                # Legacy projection retained explicitly; v2 never embeds base64.
                value["auditor_decision"] = state["attempts"][-1].get("decision")
                value["artifacts"] = state["attempts"][-1].get("artifacts", {})
                value["runner_request"] = state["attempts"][-1]["request"]
        print(json.dumps(value, ensure_ascii=True))
        if args.action == 'preflight':
            return 0 if value['readiness'] == 'DECLARED_INPUTS_CHECKED' else 1
        if args.action == "qualify":
            return 0 if (value["execution_status"] == "COMPLETED" and value["review_status"] == "VALID"
                         and value["identity_assurance"] == "stream_message_bound") else 1
        return 1 if args.action == "execute" and value["status"] in {
            "EXECUTION_FAILED", "INCONCLUSIVE_WITH_REVISIT_TRIGGER"} else 0
    except (ImportError, OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(json.dumps({"error": str(error), "authority": "NONE"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

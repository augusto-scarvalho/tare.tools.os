"""Opt-in, session-scoped mediation for confined commands and producer evidence."""

from pathlib import Path
import hashlib
from threading import Lock

from assurance_plane.producer_receipts import verify_receipt
from compute_plane.action_control import ActionRefused, ActionSnapshot, ExactActionGuard
from compute_plane.command_mediation import environment_digest
from relay.action_mediation import CommandActionMediator


def confined_mediation_factory(*, profile, broker, snapshot_factory, context_factory,
                               authorization_factory, guard_factory, signer, admitted_keys,
                               result_sink):
    """Build an explicit native_agent command mediation callback; own no resources."""
    for name, value in (("snapshot_factory", snapshot_factory),
                        ("context_factory", context_factory),
                        ("authorization_factory", authorization_factory),
                        ("guard_factory", guard_factory),
                        ("result_sink", result_sink)):
        if not callable(value):
            raise ValueError(f"{name}_must_be_callable")
    if not callable(getattr(broker, "assess", None)):
        raise ValueError("invalid_action_broker")
    if not callable(getattr(signer, "sign", None)):
        raise ValueError("invalid_producer_signer")
    producer_id = getattr(signer, "producer_id", None)
    public_key = getattr(signer, "public_key_bytes", None)
    if (type(public_key) is not bytes or len(public_key) != 32
            or producer_id != hashlib.sha256(public_key).hexdigest()
            or not isinstance(admitted_keys, dict)
            or admitted_keys.get(producer_id) != public_key):
        raise ValueError("producer_key_not_admitted")

    used = False  # Host-only: never claim a second session with this callback.
    lock = Lock()

    def bind_session(*, session_id, binding, control, config, custody, scratch, capabilities):
        nonlocal used
        if used:
            raise ActionRefused("confined_mediation_factory_already_used")
        if profile.validate() is not True:
            raise ActionRefused("invalid_confinement_profile")
        repositories = config.get("repositories")
        if (not isinstance(repositories, dict) or not repositories
                or any(Path(root).resolve() != Path(profile.input_root).resolve()
                       for root in repositories.values())):
            raise ActionRefused("repositories_outside_confinement")
        if (getattr(capabilities, "terminal", None) is not None
                or not callable(getattr(capabilities, "bind_command_confinement", None))
                or not callable(getattr(capabilities, "claim_action", None))):
            raise ActionRefused("invalid_confined_capabilities")
        current = getattr(control, "binding", None)
        if (current != binding or not session_id
                or not getattr(binding, "work_id", None)
                or not getattr(binding, "binding_id", None)
                or type(getattr(binding, "fence", None)) is not int
                or getattr(capabilities, "session_id", None) != session_id
                or getattr(capabilities, "binding", None) != binding):
            raise ActionRefused("invalid_confined_binding")
        if not callable(getattr(profile, "collect", None)):
            raise ActionRefused("invalid_confinement_collector")

        # Construct and check the guard before installing any host binding.
        guard = guard_factory(capabilities, control)
        if not isinstance(guard, ExactActionGuard):
            raise ActionRefused("invalid_exact_action_guard")

        def snapshot(facts, live):
            if profile.validate() is not True:
                raise ActionRefused("invalid_confinement_profile")
            if (getattr(facts, "operation", None) != "command"
                    or getattr(facts, "owner", None) != session_id):
                raise ActionRefused("invalid_confined_command_facts")
            action = snapshot_factory(facts, live)
            if not isinstance(action, ActionSnapshot):
                raise ActionRefused("invalid_confined_action_snapshot")
            expected = {
                "work_id": binding.work_id,
                "session_id": session_id,
                "binding_id": binding.binding_id,
                "fence_seq": binding.fence,
                "source_sha256": profile.source_sha256,
                "environment_sha256": environment_digest(profile.environment),
                "execution_profile": "windows-lpac/1:" + profile.profile_sha256,
                "cwd": str(profile.input_root),
            }
            if (control.binding != binding or any(
                    getattr(action, key, None) != value for key, value in expected.items())):
                raise ActionRefused("confined_snapshot_identity_mismatch")
            return action

        def result_handler(action, process, live):
            run_id = profile.run_id  # collect may reset the active run.
            if (not isinstance(process, dict)
                    or process.get("cleanup_verified") is not True
                    or any(process.get(key) for key in (
                        "cancelled", "timeout", "output_limit",
                        "isolation_violation"))):
                raise ActionRefused("confined_process_not_clean")
            artifacts = profile.collect(process)
            subject = {
                "work_id": action.work_id,
                "session_id": action.session_id,
                "effect_id": action.effect_id,
                "binding_id": action.binding_id,
                "fence_seq": action.fence_seq,
                "action_sha256": action.action_sha256,
                "source_sha256": action.source_sha256,
                "environment_sha256": action.environment_sha256,
                "runtime_sha256": profile.runtime_sha256,
                "profile_sha256": profile.profile_sha256,
                "run_id": run_id,
            }
            receipt = signer.sign(subject, artifacts)
            verify_receipt(receipt, artifacts, expected_subject=subject,
                           admitted_keys=admitted_keys)
            result_sink(receipt, artifacts, live)
            return {**process, "producer_receipt": receipt, "authority": "NONE",
                    "export_artifacts": sorted(artifacts)}

        mediator = CommandActionMediator(
            broker, snapshot, context_factory, authorization_factory, guard,
            lambda: control, result_handler=result_handler)
        capabilities.bind_command_confinement(
            profile, profile.environment, profile.seconds)
        used = True
        return mediator

    def create(*, session_id, binding, control, config, custody, scratch, capabilities):
        with lock:
            return bind_session(session_id=session_id, binding=binding, control=control, config=config,
                                custody=custody, scratch=scratch, capabilities=capabilities)

    return create

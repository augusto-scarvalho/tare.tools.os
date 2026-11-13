"""Native proposal -> exact candidate -> existing OS settlement.

No tactical loop or admission policy lives here. The coordinator accepts one
candidate claim under a live fence; its historical receipt reconciles a lost
acknowledgement. Only the existing admission/landing pipeline may settle it.
"""
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from relay.native_attempt import binding_identity, run_native_attempt
from relay.train_execution import canonical_json_bytes


def native_proposal(descriptor, result, journal, binding, identity):
    snapshot = journal.snapshot()
    return {'schema': 'tare.tools/native-candidate-proposal/1',
        'descriptor_sha256': descriptor.sha256, 'session_id': result['session_id'],
        'binding': binding_identity(binding), 'profile_sha256': result['profile_digest'],
        'implementer_identity_sha256': identity.sha256,
        'journal_revision': snapshot.revision,
        'journal_events_sha256': hashlib.sha256(b''.join(r.canonical_bytes for r in snapshot.records)).hexdigest(),
        'outcome_sha256': hashlib.sha256(canonical_json_bytes(result['outcome'])).hexdigest()}


def candidate_claim(proposal, candidate, binding, result):
    return SimpleNamespace(binding=binding, status='PROPOSED',
        output=canonical_json_bytes(proposal | {'candidate_sha256': candidate.sha256}).decode(),
        turns=result['outcome']['model_calls'], attempts=1)


def recover_accepted_candidate(train_dir, descriptor, repository_root, custody, gate):
    """Recover a historical acceptance; no live binding, model or verifier is created."""
    from compute_plane.native_journal import NativeJournal, custody_lock, regular
    from relay.candidate_change import read_candidate_receipt, verify_candidate_receipt
    from relay.executor_observation import verification_observed
    from relay.landing_policy import read_identity
    root = Path(train_dir)
    with custody_lock(Path(custody)):
        state = json.loads(regular(Path(custody)/'attempt.json').read_bytes())
        saved = state.get('proposal')
        if saved is None:
            raise ValueError('WORK_RESUME_REQUIRES_ACCEPTED_CANDIDATE')
        journal_path = regular(Path(custody)/'journal.jsonl')
        if hashlib.sha256(journal_path.read_bytes()).hexdigest() != saved['journal_sha256']:
            raise ValueError('NATIVE_ATTEMPT_IDENTITY_CHANGED')
        identity = read_identity(root/'IMPLEMENTER_IDENTITY.json')
        binding = SimpleNamespace(**saved['binding'])  # historical lookup needs no bearer token
        proposal = native_proposal(descriptor, saved['result'], NativeJournal(journal_path), binding, identity)
        if proposal != json.loads(regular(root/'NATIVE_PROPOSAL.json').read_bytes()):
            raise ValueError('NATIVE_CANDIDATE_PROPOSAL_CHANGED')
        candidate = read_candidate_receipt(root/'CANDIDATE_CHANGE.json')
        verify_candidate_receipt(candidate, descriptor, repository_root=repository_root)
        accepted = gate.accepted_claim(candidate_claim(proposal, candidate, binding, saved['result']))
        if accepted is None:
            raise ValueError('WORK_RESUME_REQUIRES_ACCEPTED_CANDIDATE')
        if not verification_observed(root, descriptor, candidate):
            raise ValueError('NATIVE_ACCEPTED_CANDIDATE_EVIDENCE_CHANGED')
        return candidate, {'session_id': proposal['session_id'], 'claim': accepted,
                           'implementer_identity_sha256': identity.sha256}


class NativeWorkRunner:
    def __init__(self, *, authority_gate, identity, settings, verification_timeout=60.0, local_producer=None):
        profile = settings['profile']
        if (identity.payload['role'] != 'implementer' or identity.payload['runtime_key'] != 'native'
                or identity.payload['runtime_model'] != profile.model):
            raise ValueError('NATIVE_IMPLEMENTER_IDENTITY_MISMATCH')
        self.authority_gate, self.identity, self.settings = authority_gate, identity, dict(settings)
        self.verification_timeout = verification_timeout
        # challenger.local of the implementer's deployment; unknown (None) counts as local: the project's saved
        # "always" then never runs its blocked checks outside the sandbox.
        self.local_producer = local_producer

    def run(self, *, train_dir, descriptor, lease, node, resume=False, rebind=False):
        from . import challenger
        custody = Path(self.settings['custody'])
        if not resume:
            try:
                challenger.start_work(self.settings, train_dir, descriptor, lease.root, node)
            except Exception:
                pass
        result = {'status': 'INTERRUPTED'}
        try:
            result = run_native_attempt(resume=resume, rebind=rebind, recover_model=resume, authority_gate=self.authority_gate,
                on_proposal=lambda result, journal, control: self.record_candidate(
                    train_dir, descriptor, lease, result, journal, control, node=node),
                **(self.settings | {'worktree': lease.root, 'descriptor': descriptor, 'node': node}))
            return result
        finally:
            if (custody/'challenger-input.json').exists():
                try:
                    challenger.finish(custody, challenger.source_state(custody), result)
                except Exception:
                    pass

    def record_candidate(self, train_dir, descriptor, lease, result, journal, control, *, node=None):
        from compute_plane.native_journal import regular, save_json
        from relay.candidate_change import (CANDIDATE_FILE, materialize_verify_and_record_candidate,
            read_candidate_receipt, verify_candidate_receipt)
        from relay.executor_observation import verification_observed

        root = Path(train_dir)
        proposal = native_proposal(descriptor, result, journal, control.binding, self.identity)
        proposal_path = regular(root / 'NATIVE_PROPOSAL.json')
        if proposal_path.exists():
            if json.loads(proposal_path.read_bytes()) != proposal:
                raise ValueError('NATIVE_CANDIDATE_PROPOSAL_CHANGED')
        else:
            self.check_control(control)
            save_json(proposal_path, proposal)

        def claim_for(candidate):
            return candidate_claim(proposal, candidate, control.binding, result)

        candidate_path = regular(root / CANDIDATE_FILE)
        # An accepted candidate belongs to host settlement now. This historical
        # check must precede ACTIVE checks: acceptance has terminated the binding.
        if candidate_path.exists():
            candidate = read_candidate_receipt(candidate_path)
            verify_candidate_receipt(candidate, descriptor, repository_root=lease.repository_root)
            accepted = self.authority_gate.accepted_claim(claim_for(candidate))
            if accepted is not None:
                if not verification_observed(root, descriptor, candidate):
                    raise ValueError('NATIVE_ACCEPTED_CANDIDATE_EVIDENCE_CHANGED')
                return self.record_acceptance(root, candidate, accepted)

        self.check_control(control)
        candidate = materialize_verify_and_record_candidate(root, descriptor, lease, recover=True,
            check_control=lambda: self.check_control(control),
            verification_timeout=self.verification_timeout,
            verification_runner=self.verification_runner(control, root, descriptor=descriptor,
                settings=self.settings | {'node': node, 'repository_root': lease.repository_root,
                                          'local_producer': self.local_producer}))
        claim = claim_for(candidate)
        save_json(regular(root / 'NATIVE_CLAIM.json'), json.loads(claim.output))
        self.check_control(control)
        if not self.authority_gate.submit_terminal_claim(claim):
            if self.authority_gate.accepted_claim(claim) is None:
                raise ValueError('NATIVE_CANDIDATE_CLAIM_REFUSED')
        accepted = self.authority_gate.accepted_claim(claim)
        if accepted is None:
            raise ValueError('NATIVE_CANDIDATE_CLAIM_UNCONFIRMED')
        return self.record_acceptance(root, candidate, accepted)

    def record_acceptance(self, root, candidate, accepted):
        from compute_plane.native_journal import regular, save_json
        value = {'status': 'CANDIDATE_ACCEPTED', 'candidate_sha256': candidate.sha256,
                 'candidate_commit': candidate.payload['candidate_commit'], 'claim': accepted,
                 'implementer_identity_sha256': self.identity.sha256, 'admission': 'NOT_EVALUATED'}
        save_json(regular(root / 'NATIVE_ACCEPTANCE.json'), value)
        return value

    def check_control(self, control):
        if control.should_stop() or not self.authority_gate.is_current(control.binding):
            raise ValueError('NATIVE_CANDIDATE_CONTROL_STOPPED')

    @staticmethod
    def verification_runner(control, train_dir, *, descriptor=None, settings=None):
        """The project's command gate when it has command protection; otherwise the sandbox where this host has it
        (no model review); otherwise, or for a sandbox-blocked check of a project whose user chose so, a process
        as the user."""
        if settings is not None and descriptor is not None:
            from .command_protection import protected_work_verifier, sandbox_available
            if settings.get('command_protection') is not None or sandbox_available():
                return protected_work_verifier(control, train_dir, descriptor=descriptor, settings=settings,
                    outside=NativeWorkRunner.verification_runner(control, train_dir))
        def run(argv, *, cwd, timeout, env, **ignored):
            from compute_plane.managed_process import run_pipe_process
            from compute_plane.native_journal import regular, save_json
            if control.should_stop():
                raise ValueError('NATIVE_CANDIDATE_CONTROL_STOPPED')
            receipt, out, err = run_pipe_process(argv, cwd=cwd, env=env,
                timeout=min(float('inf') if timeout is None else timeout, control.remaining_seconds()), cancelled=control.should_stop,
                max_output=1024*1024)
            save_json(regular(Path(train_dir)/'NATIVE_VERIFIER_PROCESS.json'), receipt)
            if (not receipt['cleanup_verified'] or receipt['cancelled'] or receipt['timeout']
                    or receipt['output_limit'] or receipt['exit_code'] is None):
                raise ValueError('NATIVE_CANDIDATE_VERIFIER_INTERRUPTED')
            return subprocess.CompletedProcess(argv, receipt['exit_code'], out, err)
        return run

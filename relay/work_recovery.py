"""Compose existing plan/workspace/native-attempt recovery before candidate acceptance."""
import json
from pathlib import Path


def origin_protection_options(origin):
    """Validate frozen origin and host identity before any recovery model call."""
    keys = {'command_protection', 'command_protection_deployment', 'command_protection_sha256'}
    if not keys.intersection(origin):
        return {}
    if not keys.issubset(origin) or origin.get('command_protection') is None:
        raise ValueError('WORK_COMMAND_PROTECTION_IDENTITY_CHANGED')
    if origin.get('prepared_implementation') or origin.get('preparation_source'):
        raise ValueError('WORK_COMMAND_PROTECTION_REQUIRES_MANAGED_IMPLEMENTATION')
    from .command_protection import saved, prepare, digest
    policy = saved(origin['command_protection'])
    deployment = origin['command_protection_deployment']
    if (policy != origin['command_protection']
            or digest({'policy': policy, 'deployment': deployment}) != origin['command_protection_sha256']
            or prepare(policy, deployment) != policy):
        raise ValueError('WORK_COMMAND_PROTECTION_IDENTITY_CHANGED')
    host_path = Path(origin['implementation_custody'])/'host.json'
    if host_path.exists():
        host = json.loads(host_path.read_bytes())
        if any(host.get(key) != origin[key] for key in keys):
            raise ValueError('WORK_COMMAND_PROTECTION_IDENTITY_CHANGED')
    return {'command_protection': policy, 'command_protection_deployment': deployment}


def resume_execution(train, descriptor, origin, receipt, stage, cancellation, *, retry_plan=False):
    from compute_plane.native_journal import save_json
    from relay import relay_mesh, work_run
    from relay.native_work_host import plan_audit, create_runner, finish_runner, renew_train_lease
    from relay.strict_workspace import recover_train_workspace
    from relay.landing_policy import write_identity, read_identity
    from relay.train_execution import validate_descriptor_for_train
    protection_options = origin_protection_options(origin)
    node = work_run.load_node((relay_mesh.RELAY_ROOT / descriptor.work_graph), descriptor.work_id)
    if node != origin.get('node') or not origin.get('native_deployment'):
        raise ValueError('WORK_RESUME_EXECUTION_CONFIGURATION_UNAVAILABLE_OR_CHANGED')
    validate_descriptor_for_train(descriptor, train_id=descriptor.train_id, task_ids=[descriptor.work_id],
                                  work_graph_file=(relay_mesh.RELAY_ROOT / descriptor.work_graph))
    work_run.eligibility_check(descriptor.work_id, (relay_mesh.RELAY_ROOT / descriptor.work_graph))
    root = Path(origin['repository_root'])
    if work_run.git(root, 'rev-parse', descriptor.target_ref) != descriptor.base_commit:
        raise ValueError('WORK_RESUME_TARGET_MOVED')
    status = json.loads((train/'STATE.json').read_bytes())['status']
    limits = origin['effective_limits']
    from . import work_control
    if status == 'PLAN_AUDIT_PENDING':
        if retry_plan:
            previous_plan = origin['plan_custody']
            new_plan_attempt(train, origin)
            stage('plan_retry', previous_custody=previous_plan, custody=origin['plan_custody'])
        plan_custody = Path(origin['plan_custody'])
        audit = plan_audit(origin['native_deployment'], train, descriptor.execution_repository, root, plan_custody,
            **({'shared_context': origin['shared_context']} if 'shared_context' in origin else {}),
            limits=limits['plan'], max_compactions=limits['max_compactions_per_stage'], cancellation=cancellation,
            resume=(plan_custody/'native-agent.json').exists(),
            **work_control.ports(train, origin, plan_custody))
        if audit.get('status') == 'PAUSED' or audit.get('verdict') == 'PAUSED':
            work_control.checkpoint(train, origin, receipt, stage, cancellation,
                                    custody=plan_custody, paused_result=audit)
            return False
        if cancellation.is_cancelled():
            receipt['status'] = 'CANCELLED'; return False
        work_run.plan_audit_report(train, [descriptor.work_id], audit['verdict'], descriptor.sha256,
                                  [*audit.get('findings', []), *['Remaining: '+str(r) for r in audit.get('remaining', [])]])
        stage('plan_audit', verdict=audit['verdict'], resumed=True, failure_code=audit.get('failure_code'),
              findings=audit.get('findings', []), remaining=audit.get('remaining', []))
        if audit['verdict'] != 'ACCEPT':
            receipt['status'] = 'PLAN_INVALID_DELIVERY' if audit.get('failure_code') == 'INVALID_DELIVERY' else 'PLAN_REJECTED'
            return False
        if not relay_mesh.transition_train(descriptor.train_id, 'PLAN_APPROVED', 'implementer', work_run.AGENT_ID,
                                           'plan-auditor', 'resume native plan audit'):
            raise ValueError('PLAN_APPROVAL_REFUSED')
        status = 'PLAN_APPROVED'
    if status not in {'PLAN_APPROVED', 'IN_PROGRESS_IMPLEMENTER'}:
        raise ValueError('WORK_RESUME_STAGE_NOT_SUPPORTED:' + status)
    if not work_control.checkpoint(train, origin, receipt, stage, cancellation,
                                   custody=origin['implementation_custody'] if status == 'IN_PROGRESS_IMPLEMENTER' else None):
        return False
    from .human_wait import checkpoint
    if not checkpoint(train, origin, receipt, stage):
        return False
    # Only the original reservation can be replaced while holding the public Work custody lock.
    current = relay_mesh.current_lease(descriptor.train_id)
    if current is not None:
        previous = origin.get('train_lease') or {}
        if any(current.get(k) != previous.get(k) for k in ('agent_id', 'hostname', 'pid', 'process_uuid', 'acquired_ts')):
            raise ValueError('WORK_RESUME_LEASE_CHANGED')
        if not relay_mesh.release_lease(descriptor.train_id, work_run.AGENT_ID):
            raise ValueError('WORK_RESUME_LEASE_UNAVAILABLE')
    custody = Path(origin['implementation_custody'])
    resuming = (custody/'host.json').exists()
    from .execution_scope import user_choice
    # A Work paused before implementing starts its implementer now: still the user's choice if it was at creation.
    with user_choice('implementer', origin['native_deployment'] if origin.get('implementer_chosen') else None):
        runner = create_runner(None if resuming else origin['native_deployment'], descriptor.work_id, custody,
            **({'shared_context': origin['shared_context']} if 'shared_context' in origin else {}),
            resume=resuming, limits=limits['implementation'], max_compactions=limits['max_compactions_per_stage'],
            cancellation=cancellation, verification_timeout=limits['candidate_verification_seconds'], graph_path=relay_mesh.RELAY_ROOT / descriptor.work_graph, **protection_options,
            **work_control.ports(train, origin, custody))
    accepted = False
    held = False
    try:
        # Candidate timeout was resolved separately from HTTP/model stage ceilings.
        if runner.verification_timeout != limits['candidate_verification_seconds']:
            raise ValueError('WORK_RESUME_VERIFICATION_POLICY_CHANGED')
        with runner.authority_gate.maintain(runner.settings['control'].binding, cancellation,
                on_renew=lambda: not held or renew_train_lease(descriptor.train_id, work_run.AGENT_ID)):
            lease = recover_train_workspace(train, descriptor, repository_root=root,
                                             workspace_parent=origin['workspace_parent'])
            if not relay_mesh.acquire_lease(descriptor.train_id, work_run.AGENT_ID, 'implementer'):
                raise ValueError('LEASE_UNAVAILABLE')
            held = True
            origin['train_lease'] = relay_mesh.current_lease(descriptor.train_id)
            save_json(train/'WORK_RUN.json', origin)
            if status == 'PLAN_APPROVED' and not relay_mesh.transition_train(descriptor.train_id,
                    'IN_PROGRESS_IMPLEMENTER', 'implementer', work_run.AGENT_ID, 'implementer', 'resume Work'):
                raise ValueError('IMPLEMENTER_TRANSITION_REFUSED')
            identity_path = train/'IMPLEMENTER_IDENTITY.json'
            if identity_path.exists():
                if read_identity(identity_path).sha256 != runner.identity.sha256:
                    raise ValueError('NATIVE_WORK_RECOVERY_IDENTITY_CHANGED')
            else:
                write_identity(identity_path, runner.identity)
            stage('workspace', worktree=str(lease.root), resumed=True)
            result = runner.run(train_dir=train, descriptor=descriptor, lease=lease, node=node,
                                resume=(custody/'attempt.json').exists())
            accepted = result.get('settlement', {}).get('status') == 'CANDIDATE_ACCEPTED'
        stage('implement', outcome=result['status'], reason=result.get('outcome', {}).get('reason'), resumed=True)
        receipt['resumed_execution'] = {'session_id': runner.settings['session_id'],
            'model_calls_total': result.get('outcome', {}).get('model_calls'), 'status': result['status']}
        if result['status'] == 'PAUSED':
            work_control.checkpoint(train, origin, receipt, stage, cancellation, custody=custody, paused_result=result)
            return False
        if not accepted:
            receipt['status'] = 'IMPLEMENTATION_' + result['status']
        return accepted
    finally:
        if held:
            relay_mesh.release_lease(descriptor.train_id, work_run.AGENT_ID)
        finish_runner(runner, accepted=accepted)


def new_plan_attempt(train, origin):
    """Explicit retry preserves the finished attempt; ordinary resume never resets it."""
    from uuid import uuid4
    from compute_plane.native_journal import regular, save_json, custody_lock
    from .native_agent import read_json
    previous = regular(origin['plan_custody'])
    with custody_lock(previous):
        state = read_json(previous/'native-agent.json', 1024*1024)
        outcome = read_json(previous/'outcome.json', 4*1024*1024)
        invocation = state.get('invocations', [])[-1:]
        finished = outcome.get('status') in {'PROPOSED', 'BLOCKED', 'INTERRUPTED', 'FAILED'}
        ended_review = outcome.get('status') in {'EXHAUSTED', 'CANCELLED'}
        if ended_review or outcome.get('status') == 'RECONCILIATION_REQUIRED':
            # An explicit new read-only review can replace a lost model response,
            # even after its deadline. Unknown tool effects cannot be discarded.
            from compute_plane.native_journal import NativeObservationJournal, NativeJournal
            from .native_model_recovery import inspect
            journal = NativeJournal(previous/'journal.jsonl')
            recovery = inspect(journal, NativeObservationJournal(previous/'observations.jsonl'), state['session_id'],
                               max_model_calls=None, deadline_utc=None, owner_interrupted=ended_review)
            config = state.get('config', {})
            pending, uncertain = set(), False
            pending_models = set()
            for record in journal.snapshot().records:
                event = record.event()
                if event['kind'] == 'model_requested':
                    pending_models.add(event['call_id'])
                if event['kind'] in {'model_returned', 'model_failed'}:
                    pending_models.discard(event.get('call_id'))
                if event['kind'] == 'effect_requested': pending.add(event['effect_id'])
                if event['kind'] == 'effect_returned':
                    uncertain |= event['result']['status'] == 'unknown'
                    pending.discard(event['result']['effect_id'])
            needs_cleanup = bool(recovery or pending_models or outcome.get('pending_id'))
            clean = bool(recovery and recovery['local_cleanup_verified']
                         and (ended_review or recovery['transport_eligible']))
            finished = bool(config.get('role') == 'plan-auditor' and not config.get('writable')
                and not config.get('terminal') and not config.get('verifiers')
                and (clean if needs_cleanup or not ended_review else True)
                and not pending and not uncertain)
        if (not invocation or invocation[0].get('ended_at') is None
                or not finished):
            raise ValueError('PLAN_RETRY_REQUIRES_FINISHED_ATTEMPT')
        next_root = regular(previous.parent/('plan-audit-'+uuid4().hex))
        updated = dict(origin)
        updated['plan_audit_attempts'] = [*origin.get('plan_audit_attempts', []), str(previous)]
        updated['plan_custody'] = str(next_root)
        save_json(regular(Path(train)/'WORK_RUN.json'), updated)
        origin.update(updated)

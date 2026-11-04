"""Resume host settlement of an already accepted native candidate."""
import json
from pathlib import Path
import subprocess
from compute_plane.managed_process import host_executable


def resume_settlement(work_id, receipt, stage, cancellation, *, repository_root=None, evidence_factory=None, retry_plan=False, retry_audit=False, graph_path=None):
    from relay import relay_mesh, work_run
    from relay.cluster_coordinator import CoordinatorDB
    from relay.runtime_authority import CoordinatorAuthorityGate
    from relay.native_settlement import recover_accepted_candidate
    from relay.train_execution import read_execution_descriptor, validate_descriptor_for_train

    _, _, graph_path = work_run._resolve_graph_scope(graph_path)
    train = work_run.train_dir_for(work_id, graph_path=graph_path)
    if not (train/'WORK_RUN.json').is_file():
        raise ValueError('WORK_RESUME_ORIGIN_UNAVAILABLE: no recoverable native Work run')
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    from .work_recovery import origin_protection_options
    origin_protection_options(origin)
    descriptor = read_execution_descriptor(train/'EXECUTION_DESCRIPTOR.json')
    if (Path(relay_mesh.RELAY_ROOT) / descriptor.work_graph).resolve() != Path(graph_path):
        raise ValueError('WORK_RESUME_GRAPH_CHANGED')
    state = json.loads((train/'STATE.json').read_bytes())
    train_id = work_run.train_id_for(work_id, graph_path=graph_path)
    if (origin.get('schema') not in {'tare.tools/work-run-origin/1', 'tare.tools/work-run-origin/2'}
            or (origin.get('schema') == 'tare.tools/work-run-origin/2') != ('shared_context' in origin)
            or origin['work_id'] != work_id
            or descriptor.work_id != work_id or descriptor.train_id != train_id
            or state['train_id'] != train_id or state.get('task_ids') != [work_id]
            or origin['descriptor_sha256'] != descriptor.sha256
            or (state['status'] != 'PLAN_AUDIT_PENDING'
                and (state.get('execution_descriptor') or {}).get('sha256') != descriptor.sha256)):
        raise ValueError('WORK_RESUME_IDENTITY_CHANGED')
    root = Path(origin['repository_root']).resolve()
    if 'shared_context' in origin:
        from .work_context_reuse import snapshot
        receipt['shared_context'] = snapshot(origin['shared_context'])
    if repository_root is not None and Path(repository_root).resolve() != root:
        raise ValueError('WORK_RESUME_REPOSITORY_CHANGED')
    if 'workflow_sha256' in origin:
        from graph_backlog.jsonutil import sha256_canonical
        saved = json.loads(work_run.receipt_path(origin['run_id']).read_bytes())
        compiled = saved.get('workflow')
        if (not isinstance(compiled, dict) or saved.get('workflow_sha256') != origin['workflow_sha256']
                or sha256_canonical(compiled) != origin['workflow_sha256']):
            raise ValueError('WORK_RESUME_WORKFLOW_CHANGED')
        # Preserve the originally compiled bytes; never recompile against today's catalog.
        receipt.update(workflow=compiled, workflow_sha256=origin['workflow_sha256'])
    receipt.update(train_id=train_id, resumed_from=origin['run_id'], execution='native', deployment=None,
                   repository_root=str(root),
                   preparation_sessions=origin.get('preparation_sessions', []),
                   effective_limits=origin.get('effective_limits'), audit_routing=origin.get('audit'))
    # Describe only the original frozen inputs, never today's catalog or Work.
    receipt.update(work_run._review_metadata(origin.get('node'), origin.get('native_deployment')))
    stage('recover', previous_train_status=state['status'])
    if cancellation.is_cancelled():
        receipt['status'] = 'CANCELLED'; return receipt
    if state['status'] not in {'PLAN_AUDIT_PENDING', 'PLAN_APPROVED', 'IN_PROGRESS_IMPLEMENTER', 'AUDIT_PENDING', 'READY_FOR_LANDING', 'LANDED'}:
        raise ValueError('WORK_RESUME_STAGE_NOT_SUPPORTED:' + state['status'])
    from . import work_control
    # A Git CAS may have completed before its backlog acknowledgment. That
    # existing effect must reach the original settlement reconciler. Controls
    # cannot grant another execution, or obstruct bookkeeping of a past landing.
    recovering_landing = (state['status'] == 'READY_FOR_LANDING'
                          and work_run.git(root, 'rev-parse', descriptor.target_ref) != descriptor.base_commit)
    if state['status'] != 'LANDED' and not recovering_landing and (train/work_control.FILE).exists():
        validate_descriptor_for_train(descriptor, train_id=train_id, task_ids=[work_id],
                                      work_graph_file=graph_path)
        if work_run.load_node(graph_path, work_id) != origin.get('node'):
            raise ValueError('WORK_RESUME_EXECUTION_CONFIGURATION_UNAVAILABLE_OR_CHANGED')
        work_run.eligibility_check(work_id, graph_path)
        if work_run.git(root, 'rev-parse', descriptor.target_ref) != descriptor.base_commit:
            raise ValueError('WORK_RESUME_TARGET_MOVED')
        current_custody = None
        if state['status'] == 'PLAN_AUDIT_PENDING':
            current_custody = origin['plan_custody']
        elif state['status'] == 'IN_PROGRESS_IMPLEMENTER' and not origin.get('prepared_implementation'):
            current_custody = origin['implementation_custody']
        elif state['status'] == 'AUDIT_PENDING':
            from .work_audit import audit_custody
            for number, route in enumerate((origin.get('audit') or {}).get('routes', [])):
                session = audit_custody(origin)/str(number)
                if route['eligible'] and (session/'native-agent.json').exists() and not (session/'audit-result.json').exists():
                    current_custody = session
                    break
        if not work_control.checkpoint(train, origin, receipt, stage, cancellation,
                                       custody=current_custody, consume_resume=True):
            return receipt
    if retry_plan and state['status'] != 'PLAN_AUDIT_PENDING':
        raise ValueError('PLAN_RETRY_NOT_PENDING')
    if retry_audit:
        if state['status'] != 'AUDIT_PENDING':
            raise ValueError('AUDIT_RETRY_NOT_PENDING')
        from .work_audit import new_attempt
        new_attempt(train, origin)
        stage('audit_retry', custody=origin['audit_custody'])
    if origin.get('prepared_implementation'):
        receipt.update(execution='prepared', prepared_implementation=origin['prepared_implementation'])
        recovered = _prepared_candidate(train, descriptor, origin, receipt, stage, cancellation, retry_plan=retry_plan)
        if recovered is None:
            return receipt
        candidate, recovery = recovered
        state = json.loads((train/'STATE.json').read_bytes())
    else:
        gate = CoordinatorAuthorityGate(CoordinatorDB(work_run.coordinator_path(graph_path)))
        from relay.work_recovery import resume_execution
        def accepted_candidate():
            if not (train/'NATIVE_PROPOSAL.json').exists() or not (train/'CANDIDATE_CHANGE.json').exists():
                raise ValueError('WORK_RESUME_REQUIRES_ACCEPTED_CANDIDATE')
            return recover_accepted_candidate(train, descriptor, root, origin['implementation_custody'], gate)
        try:
            candidate, recovery = accepted_candidate()
        except ValueError as error:
            if str(error) != 'WORK_RESUME_REQUIRES_ACCEPTED_CANDIDATE':
                raise
            if not resume_execution(train, descriptor, origin, receipt, stage, cancellation, retry_plan=retry_plan):
                return receipt
            state = json.loads((train/'STATE.json').read_bytes())
            candidate, recovery = accepted_candidate()
    receipt['recovery'] = recovery | ({} if origin.get('prepared_implementation') else
        {'implementation_resumed': True} if receipt.get('resumed_execution') else
                                      {'new_model_calls': 0, 'new_verifier_calls': 0})
    host_path = Path(origin['implementation_custody'])/'host.json'
    if host_path.exists():
        from relay.native_work_credentials import discard
        receipt['recovery']['credential_cleanup'] = discard(json.loads(host_path.read_bytes())['binding']['binding_id'])
    receipt['candidate_commit'] = commit = candidate.payload['candidate_commit']
    stage('candidate', commit=commit, sha256=candidate.sha256, recovered=True)

    if state['status'] == 'LANDED':
        # The owner has already settled the graph. Confirm Git and this train's own backlog
        # completion before returning the confirmed result; never land or execute again.
        try:
            work_run.git(root, 'merge-base', '--is-ancestor', commit, descriptor.target_ref)
        except subprocess.CalledProcessError:
            raise ValueError('WORK_RESUME_RECONCILIATION_REQUIRED: landed candidate is not in target history') from None
        try:
            graph = json.loads(Path(graph_path).read_bytes())
        except (OSError, ValueError):
            graph = None
        graph_nodes = graph.get('nodes') if isinstance(graph, dict) else None
        matches = [n for n in graph_nodes if isinstance(n, dict) and n.get('id') == work_id] if isinstance(graph_nodes, list) else []
        node = matches[0] if len(matches) == 1 else {}
        completion = node.get('completion') if isinstance(node.get('completion'), dict) else {}
        signoff = 'SMART_POLICY'  # This native Work path settles through admit_train.
        own_note = f'[RELAY_LANDED] Landed via Train {train_id} on '
        notes = node.get('notes') if isinstance(node.get('notes'), list) else []
        if not (completion.get('evidence') == f'Landed via Multi-Agent Relay Train {train_id} ({signoff})'
                and completion.get('status') == 'DONE' and node.get('work_status') == 'done'
                and completion.get('dod_satisfied') is True and completion.get('completed_at')
                and any(isinstance(note, str) and note.startswith(own_note) and note.endswith(f' with {signoff}.')
                        for note in notes)):
            raise ValueError('WORK_RESUME_RECONCILIATION_REQUIRED: backlog completion missing or not owned by ' + train_id)
        from .landing_policy import read_verdict
        verdict = read_verdict(train/'ADMISSION_VERDICT.json')
        receipt['admission'] = {'verdict': verdict.verdict, 'reason_codes': verdict.payload['reason_codes'],
                                'audit': verdict.payload['audit']}
        landed = True
    else:
        validate_descriptor_for_train(descriptor, train_id=train_id, task_ids=[work_id],
                                      work_graph_file=graph_path)
        target = work_run.git(root, 'rev-parse', descriptor.target_ref)
        reconcile_backlog = state['status'] == 'READY_FOR_LANDING' and (
            target == commit or work_run.git(root, 'merge-base', commit, target) == commit)
        if target != descriptor.base_commit and not reconcile_backlog:
            raise ValueError('WORK_RESUME_TARGET_MOVED: reconcile Git before resuming settlement')
        if not reconcile_backlog:
            work_run.load_node(graph_path, work_id)
            work_run.eligibility_check(work_id, graph_path)
        lease = relay_mesh.current_lease(train_id)
        if lease is not None:
            original = origin.get('train_lease') or {}
            # Only the reservation of the now-accepted implementation may be released.
            keys = ('agent_id', 'role', 'hostname', 'pid', 'process_uuid', 'acquired_ts')
            if any(lease.get(k) != original.get(k) for k in keys):
                raise ValueError('WORK_RESUME_LEASE_CHANGED')
            if not relay_mesh.release_lease(train_id, work_run.AGENT_ID):
                raise ValueError('WORK_RESUME_LEASE_UNAVAILABLE')
        if reconcile_backlog:
            # Git may have advanced before the graph write or terminal transition.
            # Reuse the sealed admission; the landing owner revalidates its capability
            # and exact candidate, and recognizes its own previous graph completion.
            from .landing_policy import read_verdict
            verdict = read_verdict(train/'ADMISSION_VERDICT.json')
            receipt['admission'] = {'verdict': verdict.verdict,
                                    'reason_codes': verdict.payload['reason_codes'],
                                    'audit': verdict.payload['audit']}
            if cancellation.is_cancelled():
                receipt['status'] = 'CANCELLED'; return receipt
            stage('reconcile_backlog', commit=commit)
            landed = relay_mesh.land_train(train_id, signoff='SMART_POLICY')
            receipt['recovery'].update(git_already_landed=True, backlog_reconciled=bool(landed))
        else:
            landed = _resume_admission(train, train_id, state, origin, candidate, receipt, stage,
                                       cancellation, evidence_factory)
            if landed is None:
                return receipt

    receipt['landing'] = {'landed': bool(landed), 'target_ref': descriptor.target_ref,
                          'already_landed': state['status'] == 'LANDED'}
    if landed and work_run.git(root, 'rev-parse', descriptor.target_ref) == commit:
        # Avoid labelling an already refreshed clean checkout stale on repeated resume.
        clean = all(subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false', 'diff', *args, '--quiet', descriptor.target_ref, '--',
                                   *descriptor.tracked_paths], cwd=root, check=False).returncode == 0
                    for args in ([], ['--cached']))
        receipt['landing']['checkout'] = ({'refreshed': [], 'stale': [], 'already_current': True} if clean else
            work_run.refresh_checkout(root, descriptor.target_ref, descriptor.tracked_paths, descriptor.base_commit))
    stage('landing', landed=bool(landed), recovered=True)
    receipt['status'] = 'SETTLED' if landed else 'LANDING_REFUSED'
    return receipt


def _prepared_candidate(train, descriptor, origin, receipt, stage, cancellation, *, retry_plan=False):
    """Recover deterministic incorporation, keeping its native source proposal unchanged."""
    from compute_plane.native_journal import save_json
    from . import relay_mesh, work_run, work_prepared
    from .candidate_change import read_candidate_receipt, verify_candidate_receipt
    from .executor_observation import verification_observed
    from .landing_policy import read_identity, write_identity
    from .strict_workspace import recover_train_workspace
    source = origin['prepared_implementation']
    prepared = work_prepared.inspect(source['path'])
    if (prepared['sha256'] != source['sha256'] or prepared['session'] != source['session']
            or prepared['work_id'] != descriptor.work_id
            or Path(prepared['repository_root']).resolve() != Path(origin['repository_root']).resolve()
            or prepared['deployment'] != origin['native_deployment']):
        raise ValueError('PREPARED_IMPLEMENTATION_CHANGED')
    from .native_work_host import prepared_plan_context
    prepared_plan_context(train)
    identity = work_prepared.identity(source['path'])
    identity_path = train/'IMPLEMENTER_IDENTITY.json'
    if identity_path.exists() and read_identity(identity_path).sha256 != identity.sha256:
        raise ValueError('PREPARED_IMPLEMENTER_IDENTITY_CHANGED')
    recovery = {'prepared_implementation': source, 'new_implementation_model_calls': 0,
                'new_verifier_calls': 0, 'implementer_identity_sha256': identity.sha256}
    if (train/'CANDIDATE_CHANGE.json').exists():
        if not identity_path.exists():
            raise ValueError('PREPARED_IMPLEMENTER_IDENTITY_MISSING')
        candidate = read_candidate_receipt(train/'CANDIDATE_CHANGE.json')
        verify_candidate_receipt(candidate, descriptor, repository_root=origin['repository_root'])
        if not verification_observed(train, descriptor, candidate):
            raise ValueError('PREPARED_CANDIDATE_EVIDENCE_CHANGED')
        return candidate, recovery
    graph_path = relay_mesh.RELAY_ROOT / descriptor.work_graph
    node = work_run.load_node(graph_path, descriptor.work_id)
    if node != origin['node']:
        raise ValueError('WORK_RESUME_EXECUTION_CONFIGURATION_UNAVAILABLE_OR_CHANGED')
    work_run.eligibility_check(descriptor.work_id, graph_path)
    if work_run.git(origin['repository_root'], 'rev-parse', descriptor.target_ref) != descriptor.base_commit:
        raise ValueError('WORK_RESUME_TARGET_MOVED')
    status = json.loads((train/'STATE.json').read_bytes())['status']
    limits = origin['effective_limits']
    from . import work_control
    if status == 'PLAN_AUDIT_PENDING':
        from .native_work_host import plan_audit
        if retry_plan:
            from .work_recovery import new_plan_attempt
            new_plan_attempt(train, origin)
            stage('plan_retry', custody=origin['plan_custody'])
        plan_custody = Path(origin['plan_custody'])
        audit = plan_audit(origin['native_deployment'], train, descriptor.execution_repository,
            Path(origin['repository_root']), plan_custody, limits=limits['plan'],
            **({'shared_context': origin['shared_context']} if 'shared_context' in origin else {}),
            max_compactions=limits['max_compactions_per_stage'], cancellation=cancellation,
            resume=(plan_custody/'native-agent.json').exists(),
            **work_control.ports(train, origin, plan_custody))
        if audit.get('status') == 'PAUSED' or audit.get('verdict') == 'PAUSED':
            work_control.checkpoint(train, origin, receipt, stage, cancellation,
                                    custody=plan_custody, paused_result=audit)
            return None
        if cancellation.is_cancelled():
            receipt['status'] = 'CANCELLED'; return None
        work_run.plan_audit_report(train, [descriptor.work_id], audit['verdict'], descriptor.sha256,
            [*audit.get('findings', []), *['Remaining: '+str(r) for r in audit.get('remaining', [])]])
        stage('plan_audit', verdict=audit['verdict'], resumed=True)
        if audit['verdict'] != 'ACCEPT':
            receipt['status'] = 'PLAN_INVALID_DELIVERY' if audit.get('failure_code') == 'INVALID_DELIVERY' else 'PLAN_REJECTED'
            return None
        if not relay_mesh.transition_train(descriptor.train_id, 'PLAN_APPROVED', 'implementer',
                work_run.AGENT_ID, 'plan-auditor', 'resume prepared plan audit'):
            raise ValueError('PLAN_APPROVAL_REFUSED')
        status = 'PLAN_APPROVED'
    if status not in {'PLAN_APPROVED', 'IN_PROGRESS_IMPLEMENTER'}:
        raise ValueError('WORK_RESUME_STAGE_NOT_SUPPORTED:' + status)
    if not work_control.checkpoint(train, origin, receipt, stage, cancellation):
        return None
    from .human_wait import checkpoint
    if not checkpoint(train, origin, receipt, stage):
        return None
    current = relay_mesh.current_lease(descriptor.train_id)
    if current is not None:
        previous = origin.get('train_lease') or {}
        if any(current.get(k) != previous.get(k) for k in ('agent_id', 'role', 'hostname', 'pid', 'process_uuid', 'acquired_ts')):
            raise ValueError('WORK_RESUME_LEASE_CHANGED')
        if not relay_mesh.release_lease(descriptor.train_id, work_run.AGENT_ID):
            raise ValueError('WORK_RESUME_LEASE_UNAVAILABLE')
    lease = recover_train_workspace(train, descriptor, repository_root=origin['repository_root'],
                                    workspace_parent=origin['workspace_parent'])
    if not relay_mesh.acquire_lease(descriptor.train_id, work_run.AGENT_ID, 'implementer'):
        raise ValueError('LEASE_UNAVAILABLE')
    try:
        origin['train_lease'] = relay_mesh.current_lease(descriptor.train_id)
        save_json(train/'WORK_RUN.json', origin)
        if status == 'PLAN_APPROVED' and not relay_mesh.transition_train(descriptor.train_id,
                'IN_PROGRESS_IMPLEMENTER', 'implementer', work_run.AGENT_ID, 'implementer', 'resume prepared Work'):
            raise ValueError('IMPLEMENTER_TRANSITION_REFUSED')
        if not identity_path.exists():
            write_identity(identity_path, identity)
        stage('workspace', worktree=str(lease.root), resumed=True)
        if not (train/'CANDIDATE_INTENT.json').exists():
            applied = work_prepared.apply(source['path'], descriptor, lease)
            if applied['sha256'] != source['sha256']:
                raise ValueError('PREPARED_IMPLEMENTATION_CHANGED')
            if applied['status'] != 'APPLIED':
                receipt.update(status='PREPARED_CONFLICT', prepared_conflict=applied)
                return None
        candidate = materialize_prepared(train, descriptor, lease, cancellation,
                                         limits['candidate_verification_seconds'])
        recovery['new_verifier_calls'] = len(descriptor.verification_checks)
        stage('implement', outcome='APPLIED', resumed=True, source_session=source['session'])
        return candidate, recovery
    finally:
        relay_mesh.release_lease(descriptor.train_id, work_run.AGENT_ID)


def materialize_prepared(train, descriptor, lease, cancellation, timeout):
    """Verify prepared bytes through the same candidate owner, with cancellable processes."""
    from compute_plane.managed_process import run_pipe_process
    from .candidate_change import materialize_verify_and_record_candidate
    def check():
        if cancellation.is_cancelled():
            raise ValueError('PREPARED_VERIFICATION_CANCELLED')
    def run(argv, *, cwd, timeout, env, **ignored):
        result, out, err = run_pipe_process(argv, cwd=cwd, env=env, timeout=float('inf') if timeout is None else timeout,
            cancelled=cancellation.is_cancelled, max_output=1024*1024)
        if (not result['cleanup_verified'] or result['cancelled'] or result['timeout']
                or result['output_limit'] or result['exit_code'] is None):
            raise ValueError('PREPARED_VERIFICATION_INTERRUPTED')
        return subprocess.CompletedProcess(argv, result['exit_code'], out, err)
    return materialize_verify_and_record_candidate(train, descriptor, lease, recover=True,
        verification_timeout=timeout, verification_runner=run, check_control=check)


def _resume_admission(train, train_id, state, origin, candidate, receipt, stage, cancellation, evidence_factory):
    from relay import relay_mesh, work_run
    from . import work_control
    if not work_control.checkpoint(train, origin, receipt, stage, cancellation):
        return None
    if state['status'] == 'IN_PROGRESS_IMPLEMENTER':
        if not relay_mesh.acquire_lease(train_id, work_run.AGENT_ID, 'implementer'):
            raise ValueError('LEASE_UNAVAILABLE')
        try:
            if not relay_mesh.transition_train(train_id, 'AUDIT_PENDING', 'auditor', work_run.AGENT_ID,
                                               'implementer', 'recover accepted native candidate'):
                raise ValueError('AUDIT_PENDING_TRANSITION_REFUSED')
        finally:
            relay_mesh.release_lease(train_id, work_run.AGENT_ID)
    if cancellation.is_cancelled():
        receipt['status'] = 'CANCELLED'; return None
    from .work_audit import run as run_audit
    if not run_audit(train, origin, candidate, receipt, stage, cancellation):
        return None
    # The owner re-observes Work, grounding and Git. Identical evidence is reusable;
    # a mismatch cannot overwrite the evidence on which an admission was based.
    if not work_control.checkpoint(train, origin, receipt, stage, cancellation):
        return None
    (evidence_factory or relay_mesh.materialize_admission_evidence)(train_id)
    verdict = relay_mesh.prepare_admission_verdict(train_id, **(
        {'audit_mode': origin['audit']['mode']} if origin.get('audit') else {}))
    receipt['admission'] = {'verdict': verdict.verdict, 'reason_codes': verdict.payload.get('reason_codes', []), 'audit': verdict.payload['audit']}
    stage('admission', verdict=verdict.verdict)
    if verdict.verdict != 'ADMITTED':
        receipt['status'] = 'ADMISSION_' + verdict.verdict; return None
    if cancellation.is_cancelled():
        receipt['status'] = 'CANCELLED'; return None
    if not work_control.checkpoint(train, origin, receipt, stage, cancellation):
        return None
    return (relay_mesh.land_train(train_id, signoff='SMART_POLICY') if state['status'] == 'READY_FOR_LANDING'
              else relay_mesh.admit_train(train_id))

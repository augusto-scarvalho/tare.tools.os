"""`tare run <WorkId>`: execute and settle one explicit Work through the existing train lifecycle.

Pure composition (FLOW-02, SPEC-OS-WORK-CONSOLE-001). Owners stay where they are:
relay_mesh owns the train FSM, train_execution the descriptor, strict_workspace the
worktree, candidate_change the immutable candidate, landing_policy the admission,
native_work_host the execution composition. Nothing here grants Authority: `authority` is NONE
and the final landing goes through relay_mesh.admit_train's own capability.
"""
from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib import metadata
import json
import os
import stat
from pathlib import Path
from . import execution_resources
import subprocess
import sys
import time
from uuid import uuid4
from . import native_limits

RUN_SCHEMA = "tare.tools/work-run/1"
AGENT_ID = "tare-run"


def _review_scalar(value):
    return value if (isinstance(value, str) and value.strip()
                     and len(value) <= 1024 and value.isprintable()) else None


def _review_metadata(node, deployment):
    """Descriptive declarations only; no roster lookup or served-model claim."""
    result = {}
    metrics = node.get('metrics') if isinstance(node, dict) else None
    task = _review_scalar(metrics.get('task_class')) if isinstance(metrics, dict) else None
    if task is not None:
        result['task_class'] = task
    profile = deployment.get('native_profile') if isinstance(deployment, dict) else None
    if isinstance(profile, dict):
        identity = {'deployment_id': deployment.get('id'),
                    'requested_model': profile.get('model'), 'protocol': profile.get('protocol')}
        if all(_review_scalar(value) is not None for value in identity.values()):
            value = json.dumps(identity, ensure_ascii=True, separators=(',', ':'))
            if _review_scalar(value) is not None:
                result['runtime_identity'] = value
                # This controller attempt, not provider identity or reused-call history.
                from .execution_team import reasoning
                effort = reasoning(deployment)
                try:
                    runtime_version = metadata.version('tare-tools-agent-runtime')
                except Exception:
                    # Missing or unreadable distribution metadata must not fail Work.
                    runtime_version = None
                result['runtime_observation'] = {
                    'schema': 'tare.tools/work-runtime-observation/1',
                    'runtime_version': _review_scalar(runtime_version),
                    **identity,
                    'effort': _review_scalar(effort),
                }
    return result


def _review_outcome(receipt):
    """Summarize recorded results, never quality, independence or settlement."""
    parts = {}
    for item in receipt.get('stages', []):
        if isinstance(item, dict) and item.get('stage') == 'plan_audit':
            value = _review_scalar(item.get('verdict'))
            if value is not None:
                parts['plan_verdict'] = value
    audit = receipt.get('audit')
    if isinstance(audit, dict):
        value = _review_scalar(audit.get('verdict'))
        if value is not None:
            parts['code_verdict'] = value
        elif isinstance(audit.get('result'), dict):
            value = _review_scalar(audit['result'].get('status'))
            if value is not None:
                parts['code_status'] = value
    admission = receipt.get('admission')
    if isinstance(admission, dict):
        value = _review_scalar(admission.get('verdict'))
        if value is not None:
            parts['admission_verdict'] = value
    value = '; '.join(key + '=' + value for key, value in parts.items())
    return {'recorded_audit_outcome': value} if _review_scalar(value) is not None else {}


def now():
    return datetime.now(timezone.utc).isoformat()


def receipt_path(run_id):
    from . import relay_mesh
    from compute_plane.native_journal import regular
    if (not isinstance(run_id, str) or len(run_id) != 32
            or any(c not in '0123456789abcdef' for c in run_id)):
        raise ValueError('WORK_RUN_ID_INVALID')
    return regular(relay_mesh.RELAY_ROOT.parent/'.reconciliation'/'work-runs'/run_id/'run.json')


def _resolve_graph_scope(graph_path):
    """Return control membership, normalized identity and display path separately."""
    from . import relay_mesh
    from .project_graph import graph_path as validate_path
    control_resolved = str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())
    control_canonical = os.path.normcase(control_resolved)
    if graph_path is None:
        return True, control_canonical, control_resolved

    validated = validate_path(str(graph_path) if isinstance(graph_path, Path) else graph_path)
    if not validated:
        raise ValueError('The project graph must be an absolute JSON path without parent traversal.')
    resolved = str(Path(validated).resolve())
    canonical = os.path.normcase(resolved)
    is_control = (canonical == control_canonical)
    return is_control, canonical, resolved


def _canonicalize_receipt_graph_path(receipt_gp):
    if isinstance(receipt_gp, Path):
        receipt_gp = str(receipt_gp)
    if not isinstance(receipt_gp, str) or not receipt_gp:
        return None
    try:
        from . import project_graph
        validated = project_graph.graph_path(receipt_gp)
        if not validated:
            return None
        return os.path.normcase(str(Path(validated).resolve()))
    except (ValueError, OSError, RuntimeError):
        return None


def lock_root_for(work_id, *, graph_path=None):
    import hashlib
    from . import relay_mesh
    is_control, canonical, _ = _resolve_graph_scope(graph_path)
    if is_control:
        name = hashlib.sha256(work_id.encode()).hexdigest()
    else:
        name = hashlib.sha256((canonical + '\0' + work_id).encode('utf-8')).hexdigest()
    return relay_mesh.RELAY_ROOT.parent / '.reconciliation' / 'work-runs' / 'locks' / name


def coordinator_path(graph_path=None):
    from . import relay_mesh
    import hashlib
    control, canonical, _ = _resolve_graph_scope(graph_path)
    if control:
        return relay_mesh.RELAY_DIR / 'history' / 'coordinator.db'
    return relay_mesh.RELAY_DIR / 'history' / 'project-coordinators' / (hashlib.sha256(canonical.encode()).hexdigest() + '.db')


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def load_node(work_graph, work_id):
    graph = json.loads(Path(work_graph).read_text(encoding="utf-8"))
    node = next((n for n in graph.get("nodes", []) if n.get("id") == work_id), None)
    if node is None:
        raise ValueError("WORK_NOT_FOUND:" + work_id)
    if node.get('admission_state') == 'PROPOSED_FROM_PLAN_NOT_ADMITTED':
        raise ValueError('WORK_REQUIRES_PREPARATION: select canonical criteria and verification before starting ' + work_id)
    if (node.get("completion") or {}).get("status") in {"DONE", "SUPERSEDED"}:
        raise ValueError("WORK_ALREADY_COMPLETE:" + work_id)
    scopes = node.get("repository_scopes")
    if not isinstance(scopes, list) or len(scopes) != 1 or not isinstance(scopes[0], dict)             or not isinstance(scopes[0].get("repository"), str) or not scopes[0]["repository"]:
        raise ValueError("WORK_REQUIRES_EXACTLY_ONE_REPOSITORY_SCOPE:" + work_id)
    return node


def derive_descriptor(node, *, train_id, repository_root, work_graph="work-graph.json",
                      library_manifest="SOURCES.json", checks=None):
    """Descriptor payload from the Work node (G1). Explicit inputs only; nothing inferred from prose.

    ``library_manifest`` names the run's sources file; a run passes the one it writes into its train."""
    scope = node["repository_scopes"][0]  # shape validated by load_node
    tracked = sorted(scope.get("target_paths") or node.get("target_paths") or [])
    if not tracked:
        raise ValueError("WORK_TARGET_PATHS_MISSING")
    checks = checks if checks is not None else (node.get("metrics") or {}).get("verification_checks")
    if not checks:
        raise ValueError("VERIFICATION_CHECKS_MISSING: add metrics.verification_checks to the node or pass --verify")
    criteria = node.get("exit_criteria") or []
    if set(criteria) != {c["criterion"] for c in checks} or len(set(criteria)) != len(criteria):
        # Same rule train_execution.validate_descriptor_for_train enforces at PLAN_APPROVED; fail before any session.
        raise ValueError("VERIFICATION_CHECKS_MUST_MATCH_EXIT_CRITERIA: exit_criteria are the check names, one command each")
    root = Path(repository_root).resolve()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise ValueError("REPOSITORY_DETACHED_HEAD")
    return {"schema": "tare.tools/train-execution/1", "train_id": train_id, "work_id": node["id"],
            "work_graph": str(work_graph), "execution_repository": scope["repository"],
            "base_commit": git(root, "rev-parse", "HEAD"), "target_ref": "refs/heads/" + branch,
            "library_manifest": str(library_manifest), "tracked_paths": tracked,
            "verification_checks": [{"criterion": c["criterion"], "command": list(c["command"])} for c in checks]}


RESERVED_MARKERS = ("PLAN_VERDICT: APPROVED", "PLAN_VERDICT: REJECTED", "VERIFIED_PASS", "AUDIT_VERDICT: FAIL")


def neutralize(text):
    """Free text from a model never carries a gate marker; only the structured verdict does."""
    text = str(text)
    for marker in RESERVED_MARKERS:
        text = text.replace(marker, "[marker removed]")
    return text.replace("PLAN_VERDICT", "PLAN-VERDICT").replace("AUDIT_VERDICT", "AUDIT-VERDICT")


def plan_audit_report(train_dir, task_ids, verdict, digest, findings):
    """Project a managed plan-auditor artifact into the marker file relay_mesh.audit_valid expects."""
    lines = [f"# Plan audit for {train_dir.name}", "", *[f"- task `{t}`" for t in task_ids], "",
             f"descriptor {digest[:12]}", "", "## Findings", *([f"- {neutralize(finding_text(f))}" for f in findings] or ["- none"]), ""]
    lines.append("PLAN_VERDICT: APPROVED" if verdict == "ACCEPT" else "PLAN_VERDICT: REJECTED")
    (train_dir / "PLAN_AUDIT_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def finding_text(item):
    return item.get("claim") or json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)


def implement_config(deployment_config, worktree, descriptor, os_root):
    """Session config for the implementer: root is the ephemeral worktree, writes fenced to tracked paths, absolute settings."""
    return {**deployment_config, "role": "implementer",
            "repositories": {descriptor.execution_repository: str(Path(worktree).resolve())},
            "writable": {descriptor.execution_repository: list(descriptor.tracked_paths)}, "allow_exec": False,
            "verifiers": {c.criterion: {"repository": descriptor.execution_repository, "argv": list(c.command)}
                          for c in descriptor.verification_checks},
            "graph": str((Path(os_root) / descriptor.work_graph).resolve()), "work": descriptor.work_id,
            "library_manifest": str((Path(os_root) / descriptor.library_manifest).resolve()),
            "objective": "Complete Work " + descriptor.work_id + " within the tracked paths; run the configured verifiers"}


def implement_instruction(node, descriptor):
    """Concrete files, acceptance and verifiers with qualified structural lookup."""
    checks = "; ".join(f"{c.criterion}: {' '.join(c.command)}" for c in descriptor.verification_checks)
    lines = ["WORK " + node["id"] + ": " + node.get("title", ""),
             "GOAL: " + node.get("summary", ""),
             "FILES YOU MAY EDIT (repository " + descriptor.execution_repository + "): " + ", ".join(descriptor.tracked_paths),
             "VERIFIERS THAT MUST PASS (run them with the verify capability before submitting): " + checks,
             "METHOD: use context for structural lookup when available. On MISS, STALE, TIMEOUT or UNAVAILABLE, "
             "read the listed files with read and use only bounded recovery within those paths. "
             "Make the smallest edit that satisfies the goal, add the required test, run every verifier, then deliver with "
             "tare_deliver, the verifier receipt ids as evidence. Do not touch files outside the list."]
    return chr(10).join(lines)


def identity_for(deployment_id, role):
    from relay.round_table_roster import load_role_deployments, load_roster, runtime_model
    from relay import relay_mesh
    from relay.landing_policy import attest_identity
    roster = load_roster()
    entry = next((e for e in load_role_deployments(roster=roster)["deployments"] if e["id"] == deployment_id), None)
    if entry is None:
        raise ValueError("ROLE_DEPLOYMENT_UNKNOWN:" + deployment_id)
    return attest_identity(roster_path=relay_mesh.RELAY_DIR / "round_table_roster.json", agent_id=AGENT_ID + ":" + deployment_id,
                           role=role, model_id=entry["model"], runtime_key=entry["runner"], runtime_model=runtime_model(roster, entry))


def eligibility_check(work_id, work_graph):
    """The evidence stage requeries the backlog for READY; ask first, before any session is paid for."""
    from compute_plane.backlog_work import BacklogWorkRejected, materialize_backlog_work
    try:
        materialize_backlog_work(work_id=work_id, work_graph=work_graph)
    except BacklogWorkRejected as rejected:
        raise ValueError("WORK_NOT_EXECUTABLE:" + str(rejected)) from rejected


def run_work(*args, **kwargs):
    """ground -> descriptor -> plan audit -> workspace -> implement -> candidate -> evidence -> admission -> land.

    relay_mesh logs transitions with print(); stdout stays reserved for the receipt."""
    from .flow_activity import work_stage
    from .work_inference import scope
    with scope(), contextlib.redirect_stdout(sys.stderr), work_stage(args[0] if args else kwargs['work_id'], graph_path=kwargs.get('graph_path')):
        return _run_work(*args, **kwargs)


def _run_work(work_id, *, deployment="local-qwen", repository_root=None, workspace_parent=None, checks=None,
              dry_run=False, budget=None,
              plan_auditor=None, implementer=None, evidence_factory=None, native_runner=None,
              native_deployment=None, limits=None, unlimited=False,
              max_compactions=native_limits.UNSET, cancellation=None, resume=False, retry_plan=False,
              wait_before_implement='', expected_work_sha256=None, audit_deployments=None, retry_audit=False, run_id=None, graph_path=None,
              prepared_implementation=None, preparation_source=None, command_protection=None,
              findings=None, findings_sha256=None):
    if bool(findings) != bool(findings_sha256):
        raise ValueError('Provide reviewed findings and their SHA-256 together.')
    if resume and findings is not None:
        raise ValueError('WORK_RESUME_REUSES_ORIGINAL_FINDINGS')
    if command_protection is not None:
        if resume:
            raise ValueError('WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS')
        if any(value is not None for value in
               (implementer, native_runner, prepared_implementation, preparation_source)):
            raise ValueError('WORK_COMMAND_PROTECTION_REQUIRES_MANAGED_IMPLEMENTATION')
    # Compile the one supported recipe before custody, train or provider effects.
    # Historical recovery stays with its original owner and saved contracts.
    compiled = None
    if not resume:
        from .workflow_compiler import compile_recipe, recipe, owner_capabilities, owner_effects
        compiled = compile_recipe(recipe(), capabilities=owner_capabilities(),
                                  allowed_effects=owner_effects())
    from relay import relay_mesh
    from relay.candidate_change import materialize_verify_and_record_candidate
    from relay.landing_policy import write_identity
    from relay.strict_workspace import provision_train_workspace
    from relay.train_execution import (DESCRIPTOR_FILE, ExecutionDescriptorRejected, create_execution_descriptor,
                                       read_execution_descriptor, validate_descriptor_for_train)

    _, _, selected_graph = _resolve_graph_scope(graph_path)
    graph_path = Path(selected_graph)
    started = time.monotonic()
    run_id = uuid4().hex if run_id is None else run_id
    custody = receipt_path(run_id).parent
    custody.mkdir(parents=True, exist_ok=False)  # Never reuse a caller's previous attempt.
    receipt = {"schema": RUN_SCHEMA, "run_id": run_id, "work_id": work_id, "deployment": deployment,
               "graph_path": selected_graph, "started_at": now(), "stage": "ground", "status": "RUNNING", "stages": [], "authority": "NONE",
               "train_id": None, "candidate_commit": None, "admission": None, "landing": None, "error": None,
               "repository_root": str(Path(repository_root).resolve()) if repository_root is not None else None}
    if compiled is not None:
        from graph_backlog.jsonutil import sha256_canonical
        receipt.update(workflow=compiled, workflow_sha256=sha256_canonical(compiled))
    from .work_inference import bind
    bind(receipt, custody / 'run.json')
    owned_runner = False
    protection_options = {}
    protection_origin = {}
    use_native_default = native_runner is None and implementer is None
    prepared = None
    lease_stack = contextlib.ExitStack()
    host_stack = contextlib.ExitStack()
    train_lease_active = False
    from tare_tools_agent_runtime import CancellationToken
    cancellation = cancellation or CancellationToken()
    host_options = {'limits': limits, 'unlimited': unlimited,
                    'max_compactions': max_compactions, 'cancellation': cancellation}

    def stage(name, **fields):
        receipt["stage"] = name
        receipt["stages"].append({"stage": name, "at": now(), **fields})
        receipt.update(_review_outcome(receipt))
        from compute_plane.native_journal import save_json
        save_json(custody / 'run.json', receipt)

    try:
        from compute_plane.native_journal import custody_lock, save_json
        if preparation_source is not None:
            from . import work_prepared
            if resume:
                raise ValueError('WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS')
            source = work_prepared.inspect(preparation_source)
            if source['work_id'] != work_id:
                raise ValueError('PREPARED_WORK_CHANGED')
            receipt['preparation_sessions'] = [source['session']]
        lock_root = lock_root_for(work_id, graph_path=graph_path)
        lock_root.mkdir(parents=True, exist_ok=True)
        host_stack.enter_context(custody_lock(lock_root))
        if expected_work_sha256 is not None:
            from graph_backlog.jsonutil import sha256_canonical
            selected = next((n for n in json.loads(Path(graph_path).read_bytes())['nodes']
                             if n['id'] == work_id), None)
            if selected is None or sha256_canonical(selected) != expected_work_sha256:
                raise ValueError('WORK_CHANGED_BEFORE_DISPATCH')
        receipt['train_id'] = train_id_for(work_id, graph_path=graph_path)  # Include failures before train creation in current status.
        if wait_before_implement and (not isinstance(wait_before_implement, str)
                or not wait_before_implement.strip() or not use_native_default or dry_run):
            raise ValueError('HUMAN_WAIT_REQUIRES_NATIVE_WORK_WITHOUT_DRY_RUN')
        if retry_plan and not resume:
            raise ValueError('PLAN_RETRY_REQUIRES_RESUME')
        if retry_audit and (not resume or retry_plan):
            raise ValueError('AUDIT_RETRY_REQUIRES_RESUME_WITHOUT_PLAN_RETRY')
        if prepared_implementation is not None and (not use_native_default or resume or dry_run):
            raise ValueError('PREPARED_IMPLEMENTATION_REQUIRES_NEW_NATIVE_WORK')
        if resume:
            if (audit_deployments is not None or native_deployment is not None or native_runner is not None or implementer is not None
                    or plan_auditor is not None or checks is not None or limits or unlimited or budget is not None
                    or max_compactions is not native_limits.UNSET or dry_run or workspace_parent is not None or wait_before_implement
                    or deployment != 'local-qwen'):
                raise ValueError('WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS')
            from relay.work_resume import resume_settlement
            return resume_settlement(work_id, receipt, stage, cancellation,
                                     graph_path=graph_path, repository_root=repository_root, evidence_factory=evidence_factory, retry_plan=retry_plan, retry_audit=retry_audit)
        if native_runner is not None and (implementer is not None or plan_auditor is None):
            raise ValueError('NATIVE_REQUIRES_EXPLICIT_PLAN_AUDITOR_AND_NO_LEGACY_IMPLEMENTER')
        if native_deployment is not None and not use_native_default:
            raise ValueError('NATIVE_DEPLOYMENT_CONFLICTS_WITH_INJECTED_RUNNER')
        if audit_deployments is not None and not use_native_default:
            raise ValueError('AUDIT_ROUTES_REQUIRE_NATIVE_WORK')
        if prepared_implementation is not None:
            from . import work_prepared
            prepared_implementation = str(Path(prepared_implementation).resolve())
            prepared = work_prepared.inspect(prepared_implementation)
            if prepared['work_id'] != work_id:
                raise ValueError('PREPARED_WORK_CHANGED')
            if native_deployment is not None and native_deployment != prepared['deployment']:
                raise ValueError('PREPARED_DEPLOYMENT_CHANGED')
            native_deployment = prepared['deployment']
            receipt.update(execution='prepared', deployment=None,
                           prepared_implementation={'path': prepared_implementation, 'sha256': prepared['sha256'],
                                                    'session': prepared['session']})
        if use_native_default:
            from relay.native_catalog import select
            from relay.native_work_host import stage_limits, compaction_limit
            native_deployment = native_deployment or select(deployment, role='implementer')
            if command_protection is not None:
                from copy import deepcopy
                from .command_protection import prepare as prepare_protection, digest as protection_digest
                review_deployment = deepcopy(native_deployment)
                frozen_policy = prepare_protection(command_protection, review_deployment)
                protection_options = {'command_protection': frozen_policy,
                                      'command_protection_deployment': review_deployment}
                protection_origin = {**protection_options, 'command_protection_sha256':
                    protection_digest({'policy': frozen_policy, 'deployment': review_deployment})}
            if native_deployment.get('native_selection'):
                receipt['implementer_routing'] = native_deployment['native_selection']
            from .work_audit import prepare
            audit_policy = prepare(native_deployment, audit_deployments,
                os.environ.get('TARE_LANDING_AUDIT_MODE', 'optional'))
            receipt['audit_routing'] = audit_policy
            if audit_policy['status'] == 'BLOCKED':
                raise ValueError('INDEPENDENT_AUDITOR_ROUTE_REQUIRED')
            for route in audit_policy['routes']:
                route['limits'] = stage_limits(route['deployment'], 'plan', limits=limits, unlimited=unlimited)
            receipt['effective_limits'] = {
                'plan': stage_limits(native_deployment, 'plan', limits=limits, unlimited=unlimited),
                'implementation': stage_limits(native_deployment, 'implement', limits=limits,
                                                unlimited=unlimited, budget=budget),
                'max_compactions_per_stage': compaction_limit(unlimited, max_compactions)}
            receipt['effective_limits']['candidate_verification_seconds'] = (
                None if execution_resources.selected('time', unlimited) or receipt['effective_limits']['implementation']['max_seconds'] is None else 60)
        node = load_node(graph_path, work_id)
        receipt.update(_review_metadata(node, None))
        if prepared is not None and work_prepared._node(node) != prepared['node']:
            raise ValueError('PREPARED_WORK_CHANGED')
        eligibility_check(work_id, graph_path)
        repo_name = node["repository_scopes"][0]["repository"]
        root = Path(repository_root) if repository_root else (
            relay_mesh.RELAY_ROOT if repo_name == "tare.tools.os" else relay_mesh.RELAY_ROOT / "satellites" / str(repo_name))
        receipt['repository_root'] = str(root.resolve())
        shared_context = None
        if findings is not None:
            if not use_native_default:
                raise ValueError('Reusable findings require managed native Work execution.')
            from .work_context_reuse import prepare
            shared_context = prepare(findings, findings_sha256, root)
            host_options['shared_context'] = shared_context
            receipt['shared_context'] = shared_context
        if prepared is not None and Path(prepared['repository_root']).resolve() != root.resolve():
            raise ValueError('PREPARED_REPOSITORY_CHANGED')
        if not (root / ".git").exists():
            raise ValueError("REPOSITORY_ROOT_UNAVAILABLE:" + str(root))
        train_id = train_id_for(work_id, node, graph_path=graph_path)
        train_dir = relay_mesh.TRAINS_DIR / train_id
        if train_dir.exists():
            raise ValueError("TRAIN_EXISTS_USE_RESUME:" + train_id)
        from .work_change import retire_revised_attempts
        retire_revised_attempts(node, graph_path, root)
        receipt["train_id"] = train_id
        sources_file = train_dir / "SOURCES.json"
        payload = derive_descriptor(node, train_id=train_id, repository_root=root, checks=checks,
                                    library_manifest=str(sources_file.resolve()), work_graph=selected_graph)
        stage("ground", repository_root=str(root), base_commit=payload["base_commit"])

        if use_native_default and not dry_run and prepared is None:
            from .adaptive_routes import observe_work, admitted_routes, summary as assessment_summary
            observation = observe_work(node, native_deployment, audit_policy, custody, cancellation,
                                       automatic_implementation=deployment == 'auto' and bool(native_deployment.get('native_selection')))
            if observation is not None:
                native_deployment, audit_policy = admitted_routes(observation, node,
                    native_deployment, audit_policy, custody, cancellation)
                if observation.get('applied'):
                    for route in audit_policy['routes']:
                        route['limits'] = stage_limits(route['deployment'], 'plan', limits=limits, unlimited=unlimited)
                    receipt['audit_routing'] = audit_policy
                    receipt['implementer_routing'] = {'reason': 'GUARDED_JOINT_ROUTE',
                        'deployment': native_deployment['id'], 'model': native_deployment['model']}
                    receipt['effective_limits'].update(
                        plan=stage_limits(native_deployment, 'plan', limits=limits, unlimited=unlimited),
                        implementation=stage_limits(native_deployment, 'implement', limits=limits,
                            unlimited=unlimited, budget=budget))
                    receipt['effective_limits']['candidate_verification_seconds'] = (
                        None if execution_resources.selected('time', unlimited) or receipt['effective_limits']['implementation']['max_seconds'] is None else 60)
                receipt['adaptive_routing'] = assessment_summary(observation, custody)
                stage('route_assessment', status=observation['status'], applied=observation.get('applied', False))
            if cancellation.is_cancelled():
                receipt['status'] = 'CANCELLED'; return receipt

        try:
            relay_mesh.init_train(train_id, node.get("title", work_id), [work_id], AGENT_ID, graph_path=graph_path)
        except SystemExit as refusal:  # relay_mesh refuses by exiting; the run records it instead
            raise ValueError("INIT_TRAIN_REFUSED:" + str(refusal.code)) from None
        from .sources import write as write_sources
        write_sources(sources_file, node, json.loads(graph_path.read_bytes()).get('sources', {}),
                      {repo_name: (root, payload["base_commit"])})
        digest = create_execution_descriptor(train_dir / DESCRIPTOR_FILE, payload)
        validate_descriptor_for_train(read_execution_descriptor(train_dir / DESCRIPTOR_FILE), train_id=train_id,
                                      task_ids=[work_id], work_graph_file=graph_path)
        stage("descriptor", sha256=digest)
        if use_native_default:
            receipt.update(_review_metadata(node, native_deployment))
            from .execution_scope import chosen
            save_json(train_dir/'WORK_RUN.json', {
                'schema': 'tare.tools/work-run-origin/2' if shared_context is not None else 'tare.tools/work-run-origin/1',
                'graph_path': selected_graph, 'work_id': work_id, 'run_id': run_id,
                'descriptor_sha256': digest, 'repository_root': str(root.resolve()),
                'implementation_custody': prepared['session'] if prepared is not None else str((custody/'implement').resolve()),
                'plan_custody': str((custody/'plan-audit').resolve()),
                'workspace_parent': str(Path(workspace_parent or custody/'workspaces').resolve()),
                'node': node, 'native_deployment': native_deployment, 'audit': audit_policy, 'wait_before_implement': wait_before_implement,
                'train_lease': None, 'effective_limits': receipt['effective_limits'],
                'preparation_sessions': receipt.get('preparation_sessions', []),
                'workflow_sha256': receipt['workflow_sha256'],
                **({'shared_context': shared_context} if shared_context is not None else {}),
                # The user named this implementer: a resume in a later process marks it again (routing profile).
                **({'implementer_chosen': True} if chosen('implementer', native_deployment) else {}),
                **protection_origin,
                **({'prepared_implementation': receipt['prepared_implementation'],
                    'audit_custody': str((custody/'code-audit').resolve())} if prepared is not None else {})})
        if prepared is not None:
            from .native_work_host import prepared_plan_context
            prepared_plan_context(train_dir)
        if native_runner is None and not use_native_default:
            # Only the managed host has this 8 KiB lifecycle-hook limit.
            from tare_ops import context as work_context, encode as encode_json
            projection = work_context(graph=graph_path, work_id=work_id, manifest=sources_file)
            if len(encode_json(projection)) > 8192:
                raise ValueError("SESSION_CONTEXT_OVER_BUDGET: narrow the node's grounding_refs or its text")

        from . import work_control
        if use_native_default:
            origin = json.loads((train_dir/'WORK_RUN.json').read_bytes())
            if not work_control.checkpoint(train_dir, origin, receipt, stage, cancellation):
                return receipt
        if plan_auditor is None:
            from relay.native_work_host import plan_audit
            from relay.native_catalog import select
            plan_auditor = lambda td, rr: plan_audit(native_deployment or select(deployment), td, repo_name, rr,
                                                   custody/'plan-audit', **host_options,
                                                   **(work_control.ports(train_dir, origin, custody/'plan-audit') if use_native_default else {}))
        audit = plan_auditor(train_dir, root)
        if use_native_default and (audit.get('status') == 'PAUSED' or audit.get('verdict') == 'PAUSED'):
            work_control.checkpoint(train_dir, origin, receipt, stage, cancellation,
                                    custody=origin['plan_custody'], paused_result=audit)
            return receipt
        if cancellation.is_cancelled():
            receipt['status'] = 'CANCELLED'; return receipt
        plan_audit_report(train_dir, [work_id], audit["verdict"], digest,
                          [*audit.get('findings', []), *['Remaining: '+str(r) for r in audit.get('remaining', [])]])
        if audit["verdict"] != "ACCEPT":
            stage("plan_audit", verdict=audit["verdict"], failure_code=audit.get('failure_code'),
                  findings=audit.get('findings', []), remaining=audit.get('remaining', []))
            receipt["status"] = "PLAN_INVALID_DELIVERY" if audit.get('failure_code') == 'INVALID_DELIVERY' else "PLAN_REJECTED"
            return receipt
        if not relay_mesh.transition_train(train_id, "PLAN_APPROVED", "implementer", AGENT_ID, "plan-auditor", "tare run plan audit"):
            raise ValueError("PLAN_APPROVAL_REFUSED")
        stage("plan_audit", verdict="ACCEPT")
        if dry_run:
            receipt["status"] = "DRY_RUN"; return receipt

        if use_native_default:
            if not work_control.checkpoint(train_dir, origin, receipt, stage, cancellation):
                return receipt
            from .human_wait import checkpoint
            if not checkpoint(train_dir, json.loads((train_dir/'WORK_RUN.json').read_bytes()), receipt, stage):
                return receipt

        if use_native_default and prepared is None:
            from relay.native_work_host import create_runner, renew_train_lease
            native_runner = create_runner(native_deployment, work_id, custody/'implement', budget=budget, graph_path=graph_path, **host_options, **protection_options,
                                          **work_control.ports(train_dir, origin, custody/'implement'))
            owned_runner = True
            lease_stack.enter_context(native_runner.authority_gate.maintain(
                native_runner.settings['control'].binding, cancellation,
                on_renew=lambda: not train_lease_active or renew_train_lease(train_id, AGENT_ID)))

        descriptor = read_execution_descriptor(train_dir / DESCRIPTOR_FILE)
        parent = Path(workspace_parent) if workspace_parent else custody / "workspaces"
        parent.mkdir(parents=True, exist_ok=True)
        artifacts = provision_train_workspace(train_dir, descriptor, repository_root=root, workspace_parent=parent)
        if not relay_mesh.acquire_lease(train_id, AGENT_ID, "implementer"):
            raise ValueError("LEASE_UNAVAILABLE")
        train_lease_active = True
        if not relay_mesh.transition_train(train_id, "IN_PROGRESS_IMPLEMENTER", "implementer", AGENT_ID, "implementer", "tare run"):
            raise ValueError("IMPLEMENTER_TRANSITION_REFUSED")
        stage("workspace", worktree=str(artifacts.lease.root))

        if prepared is not None:
            from . import work_prepared
            if work_prepared.inspect(prepared_implementation)['sha256'] != prepared['sha256']:
                raise ValueError('PREPARED_IMPLEMENTATION_CHANGED')
            origin = json.loads((train_dir/'WORK_RUN.json').read_bytes())
            origin['train_lease'] = relay_mesh.current_lease(train_id)
            save_json(train_dir/'WORK_RUN.json', origin)
            write_identity(train_dir/'IMPLEMENTER_IDENTITY.json', work_prepared.identity(prepared_implementation))
            outcome = work_prepared.apply(prepared_implementation, descriptor, artifacts.lease)
            if outcome['sha256'] != prepared['sha256']:
                raise ValueError('PREPARED_IMPLEMENTATION_CHANGED')
            if outcome['status'] != 'APPLIED':
                receipt['status'] = 'PREPARED_CONFLICT'
                receipt['prepared_conflict'] = outcome
                return receipt
            from .work_resume import materialize_prepared
            candidate = materialize_prepared(train_dir, descriptor, artifacts.lease, cancellation,
                                             receipt['effective_limits']['candidate_verification_seconds'])
        elif native_runner is not None:
            receipt['execution'] = 'native'
            receipt['deployment'] = None
            # Keep host-selected custody and identity before the first possible candidate effect.
            write_identity(train_dir / 'IMPLEMENTER_IDENTITY.json', native_runner.identity)
            origin = json.loads((train_dir/'WORK_RUN.json').read_bytes()) if (train_dir/'WORK_RUN.json').exists() else {}
            save_json(train_dir/'WORK_RUN.json', origin | {
                'schema': origin.get('schema', 'tare.tools/work-run-origin/1'),
                'graph_path': selected_graph, 'work_id': work_id, 'run_id': run_id,
                'descriptor_sha256': descriptor.sha256, 'repository_root': str(root.resolve()),
                'implementation_custody': str(Path(native_runner.settings['custody']).resolve()),
                'train_lease': relay_mesh.current_lease(train_id),
                'effective_limits': receipt.get('effective_limits')})
            outcome = native_runner.run(train_dir=train_dir, descriptor=descriptor, lease=artifacts.lease, node=node)
            lease_stack.close()  # candidate claim is recorded; stop renewal before releasing the train lease
        else:
            outcome = implementer(artifacts.lease.root, descriptor)
        stage("implement", outcome=(outcome or {}).get("status") if isinstance(outcome, dict) else str(outcome),
              reason=(outcome.get('outcome') or {}).get('reason') if isinstance(outcome, dict) else None)
        if use_native_default and isinstance(outcome, dict) and outcome.get('status') == 'PAUSED':
            origin = json.loads((train_dir/'WORK_RUN.json').read_bytes())
            work_control.checkpoint(train_dir, origin, receipt, stage, cancellation,
                                    custody=origin['implementation_custody'], paused_result=outcome)
            return receipt
        if prepared is not None:
            pass  # The host verified the prepared bytes; no native terminal claim was fabricated.
        elif native_runner is not None:
            from relay.candidate_change import read_candidate_receipt, verify_candidate_receipt
            settlement = outcome.get('settlement', {})
            if outcome.get('status') != 'PROPOSED' or settlement.get('status') != 'CANDIDATE_ACCEPTED':
                receipt['status'] = 'IMPLEMENTATION_' + str(outcome.get('status')); return receipt
            candidate = read_candidate_receipt(train_dir / 'CANDIDATE_CHANGE.json')
            verify_candidate_receipt(candidate, descriptor, repository_root=root)
            if candidate.sha256 != settlement['candidate_sha256']:
                raise ValueError('NATIVE_SETTLEMENT_CANDIDATE_CHANGED')
        elif isinstance(outcome, dict) and outcome.get("status") not in {"SUCCEEDED", "PASS"}:
            receipt["status"] = "IMPLEMENTATION_" + str(outcome.get("status")); return receipt
        else:
            candidate = materialize_verify_and_record_candidate(train_dir, descriptor, artifacts.lease)
        receipt["candidate_commit"] = candidate.payload["candidate_commit"]
        stage("candidate", commit=candidate.payload["candidate_commit"], sha256=candidate.sha256)
        if use_native_default and not work_control.checkpoint(train_dir, origin, receipt, stage, cancellation):
            return receipt
        if not relay_mesh.transition_train(train_id, "AUDIT_PENDING", "auditor", AGENT_ID, "implementer", "candidate recorded"):
            raise ValueError("AUDIT_PENDING_TRANSITION_REFUSED")
        relay_mesh.release_lease(train_id, AGENT_ID)

        if cancellation.is_cancelled():
            receipt['status'] = 'CANCELLED'; return receipt

        if native_runner is None and prepared is None:
            write_identity(train_dir / "IMPLEMENTER_IDENTITY.json", identity_for(deployment, "implementer"))
        if use_native_default:
            from .work_audit import run as run_audit
            origin = json.loads((train_dir/'WORK_RUN.json').read_bytes())
            if not run_audit(train_dir, origin, candidate, receipt, stage, cancellation):
                return receipt
        if use_native_default and not work_control.checkpoint(train_dir, origin, receipt, stage, cancellation):
            return receipt
        (evidence_factory or (lambda tid: relay_mesh.materialize_admission_evidence(tid)))(train_id)
        verdict = relay_mesh.prepare_admission_verdict(train_id, **(
            {'audit_mode': audit_policy['mode']} if use_native_default else {}))
        receipt["admission"] = {"verdict": verdict.verdict, "reason_codes": verdict.payload.get("reason_codes", []), "audit": verdict.payload["audit"]}
        stage("admission", verdict=verdict.verdict)
        if verdict.verdict != "ADMITTED":
            receipt["status"] = "ADMISSION_" + verdict.verdict; return receipt
        if cancellation.is_cancelled():
            receipt['status'] = 'CANCELLED'; return receipt
        if use_native_default and not work_control.checkpoint(train_dir, origin, receipt, stage, cancellation):
            return receipt
        landed = relay_mesh.admit_train(train_id)
        receipt["landing"] = {"landed": bool(landed), "target_ref": descriptor.target_ref}
        if landed:
            receipt["landing"]["checkout"] = refresh_checkout(root, descriptor.target_ref, descriptor.tracked_paths, payload["base_commit"])
        stage("landing", landed=bool(landed))
        receipt["status"] = "SETTLED" if landed else "LANDING_REFUSED"
        return receipt
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:  # ExecutionDescriptorRejected is a ValueError
        receipt['status'] = ('CANCELLED' if receipt.get('execution') == 'prepared'
                             and cancellation.is_cancelled() else 'FAILED')
        receipt["error"] = type(error).__name__ + ":" + str(error)[:300]
        return receipt
    finally:
        lease_stack.close()
        if train_lease_active:
            relay_mesh.release_lease(receipt['train_id'], AGENT_ID)
        if owned_runner:
            from relay.native_work_host import finish_runner
            finish_runner(native_runner, accepted=receipt['candidate_commit'] is not None,
                          paused=receipt.get('status') == 'PAUSED')
        receipt.update(_review_outcome(receipt))
        receipt["ended_at"] = now(); receipt["duration_ms"] = round((time.monotonic() - started) * 1000)
        try:
            from compute_plane.native_journal import save_json
            save_json(custody / 'run.json', receipt)
        finally:
            host_stack.close()


def refresh_checkout(root, target_ref, tracked_paths, base_commit):
    """A CAS landing moves the branch ref only: the operator index and worktree keep the old bytes, and git
    then shows the landed change as a staged reversal. Refresh a tracked path only when its index equals the
    base commit, every base file under it still has the base bytes on disk (hashed directly, so
    assume-unchanged, skip-worktree or a stale stat cache cannot hide an edit), and no local entry sits where
    the landing adds a file. Anything else is reported as stale and never overwritten."""
    def git(*args, binary=False):
        # listings travel as bytes: text mode would rewrite CR/CRLF inside file names
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=not binary, check=False)

    def base_bytes_intact(path):
        listed = git("ls-tree", "-r", "-z", base_commit, "--", path, binary=True)
        if listed.returncode:
            return False
        filemode = git("config", "--get", "core.filemode").stdout.strip().lower() != "false"  # git default is true
        for entry in filter(None, listed.stdout.split(b"\0")):
            meta, raw_name = entry.split(b"\t", 1)
            mode, blob = meta.split()[0].decode(), meta.split()[2].decode()
            name = os.fsdecode(raw_name)
            local = Path(root) / name
            # the entry type is checked without following links: a file swapped for a symlink (or the reverse,
            # or a directory) is a change git add would keep, so it is stale whatever the pointed-to bytes say
            if os.path.islink(local) != (mode == "120000"):
                return False
            if mode == "120000":
                target = git("cat-file", "-p", blob, binary=True)
                if target.returncode or os.fsencode(os.readlink(local)).replace(os.sep.encode(), b"/") != target.stdout:
                    return False
                continue
            if not local.is_file():
                return False
            # with core.filemode the executable bit is content git add keeps: a chmod is an edit
            if filemode and bool(os.stat(local).st_mode & stat.S_IXUSR) != (mode == "100755"):
                return False
            # ponytail: filtered hash = the content git would commit; a line-ending-only difference that the
            # eol/autocrlf filters normalize away is not an edit git can preserve, so it is not protected here
            hashed = git("hash-object", "--", name)
            if hashed.returncode or hashed.stdout.strip() != blob:
                return False
        return True

    def collides(name):
        """A local entry at the added name, or a non-directory at any ancestor the checkout would have to create."""
        local = Path(root) / name
        if os.path.lexists(local):  # a dangling symlink is a local entry too
            return True
        for parent in local.parents:
            if parent == Path(root):
                break
            if os.path.lexists(parent):
                return not (parent.is_dir() and not os.path.islink(parent))
        return False

    if git("symbolic-ref", "-q", "HEAD").stdout.strip() != target_ref:
        return {"refreshed": [], "stale": list(tracked_paths), "reason": "HEAD_NOT_ON_TARGET_REF"}
    refreshed, stale = [], []
    for path in tracked_paths:
        clean = git("diff", "--cached", "--quiet", base_commit, "--", path).returncode == 0 and base_bytes_intact(path)
        # --no-renames: a renamed or copied target is an addition here, whatever diff.renames says
        listed = git("diff", "--name-only", "-z", "--no-renames", "--diff-filter=A", base_commit, target_ref, "--", path, binary=True)
        added = [os.fsdecode(name) for name in listed.stdout.split(b"\0") if name]
        clean = clean and listed.returncode == 0 and not any(collides(name) for name in added)
        (refreshed if clean else stale).append(path)
    if refreshed:
        done = git("checkout", target_ref, "--", *refreshed)
        if done.returncode:
            return {"refreshed": [], "stale": list(tracked_paths), "reason": "CHECKOUT_FAILED:" + done.stderr.strip()[:200]}
    return {"refreshed": refreshed, "stale": stale}


def train_id_for(work_id, node=None, *, graph_path=None):
    import hashlib
    from . import relay_mesh
    is_control, canonical, resolved = _resolve_graph_scope(graph_path)
    if is_control:
        if node is None:
            try:
                graph = json.loads(Path(relay_mesh.WORK_GRAPH_FILE).read_text(encoding='utf-8'))
            except FileNotFoundError:
                graph = {'nodes': []}  # Legacy receipts can still be inspected without a graph.
            if (not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list)
                    or any(not isinstance(n, dict) or not isinstance(n.get('id'), str) for n in graph['nodes'])):
                raise ValueError('WORK_GRAPH_INVALID: expected a node list with string IDs')
            node = next((n for n in graph['nodes'] if n['id'] == work_id), {})
        prefix = 'RUN-' + work_id
    else:
        if node is None:
            from tare_ops import read_graph
            graph = read_graph(Path(resolved))
            if (not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list)
                    or any(not isinstance(n, dict) or not isinstance(n.get('id'), str) for n in graph['nodes'])):
                raise ValueError('GRAPH_MALFORMED')
            matches = [n for n in graph['nodes'] if n['id'] == work_id]
            if len(matches) != 1:
                raise ValueError('WORK_NOT_UNIQUE_IN_GRAPH:' + work_id)
            node = matches[0]
        digest = hashlib.sha256((canonical + '\0' + work_id).encode('utf-8')).hexdigest()
        prefix = 'GRAPH-RUN-' + digest[:32]  # Disjoint from every legacy RUN-<WorkId>.

    revision = node.get('metrics', {}).get('execution_revision')
    if revision is not None and (not isinstance(revision, str) or len(revision) != 24
            or any(c not in '0123456789abcdef' for c in revision)):
        raise ValueError('WORK_EXECUTION_REVISION_INVALID')
    return prefix + ('-r' + revision if revision else '')


def train_dir_for(work_id, *, graph_path=None):
    """Active train, or its archived copy after landing (relay_mesh moves landed trains to history)."""
    return _train_dir_for_id(train_id_for(work_id, graph_path=graph_path))


def _train_dir_for_id(train_id):
    from . import relay_mesh
    active = relay_mesh.TRAINS_DIR / train_id
    # relay_mesh archives a landed train's artifacts under history/ and may leave a state stub behind.
    archived = sorted(relay_mesh.HISTORY_DIR.glob(train_id + "_*"), key=lambda p: p.stat().st_mtime)
    return archived[-1] if archived else active


def status(work_id, *, graph_path=None):
    """Train state and last run receipt, from custody; no live session required."""
    from . import relay_mesh
    is_control, selected_canonical, selected_resolved = _resolve_graph_scope(graph_path)
    train_id = train_id_for(work_id, graph_path=graph_path)
    train_dir = _train_dir_for_id(train_id)
    state = json.loads((train_dir / "STATE.json").read_text(encoding="utf-8")) if (train_dir / "STATE.json").exists() else None
    runs_dir = relay_mesh.RELAY_ROOT.parent / ".reconciliation" / "work-runs"
    receipt_errors = []
    # Collect stat inside per-file try so a disappearing receipt cannot abort the query.
    statted = []
    for p in runs_dir.glob("*/run.json"):
        try:
            statted.append((p, p.stat()))
        except OSError as e:
            receipt_errors.append({"path": str(p), "reason": "stat_error:" + type(e).__name__})
    statted.sort(key=lambda item: item[1].st_mtime)
    candidates = []
    for p, st in statted:
        if st.st_size > 1048576:
            receipt_errors.append({"path": str(p), "reason": "oversized"})
            continue
        try:
            with p.open("rb") as stream:
                raw = stream.read(1048577)
        except OSError as e:
            receipt_errors.append({"path": str(p), "reason": "unreadable:" + type(e).__name__})
            continue
        if len(raw) > 1048576:
            receipt_errors.append({"path": str(p), "reason": "oversized"})
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            receipt_errors.append({"path": str(p), "reason": "invalid_utf8"})
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            receipt_errors.append({"path": str(p), "reason": "malformed_json"})
            continue
        if not isinstance(data, dict):
            receipt_errors.append({"path": str(p), "reason": "non_object"})
            continue
        candidates.append((p, data))

    def _receipt_matches(data):
        if data.get("work_id") != work_id:
            return False
        receipt_tid = data.get("train_id")
        if is_control:
            base = 'RUN-' + work_id
            if train_id != base and receipt_tid != train_id:
                return False
            if train_id == base and receipt_tid not in (None, base):
                suffix = receipt_tid[len(base)+2:] if isinstance(receipt_tid, str) and receipt_tid.startswith(base+'-r') else ''
                if len(suffix) != 24 or any(c not in '0123456789abcdef' for c in suffix):
                    return False
            if 'graph_path' not in data:
                return True  # Legacy control receipts predate explicit graph identity.
        elif receipt_tid != train_id:
            return False
        return _canonicalize_receipt_graph_path(data.get('graph_path')) == selected_canonical

    last = next((data for p, data in reversed(candidates) if _receipt_matches(data)), None)
    from .human_wait import inspect as inspect_wait
    operational_control = None
    if state and state.get('status') in {'PLAN_AUDIT_PENDING', 'PLAN_APPROVED', 'IN_PROGRESS_IMPLEMENTER', 'AUDIT_PENDING', 'READY_FOR_LANDING'} and (train_dir/'WORK_CONTROL.json').exists():
        try:
            from .work_control import inspect as inspect_control
            origin = json.loads((train_dir/'WORK_RUN.json').read_bytes())
            operational_control = inspect_control(work_id, graph_path=selected_resolved,
                                                  repository_root=origin['repository_root'])
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            receipt_errors.append({'path': str(train_dir/'WORK_RUN.json'),
                                   'reason': 'operational_control:' + str(error)[:300]})
    return {"operational_control": operational_control, "work_id": work_id, "graph_path": selected_resolved, "human_wait": inspect_wait(train_dir), "train": {"status": state.get("status"), "revision": state.get("revision"), "dir": str(train_dir)} if state else None,
            "last_run": last, "authority": "NONE", "receipt_errors": receipt_errors}

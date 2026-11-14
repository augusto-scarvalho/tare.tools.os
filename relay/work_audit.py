"""Native code review of the recorded candidate, composed before existing admission."""
import hashlib
import json
from pathlib import Path
import subprocess
from compute_plane.managed_process import host_executable

from compute_plane.native_journal import regular, save_json
from . import native_agent, relay_mesh
from .blocker_rubric import BLOCKER_RUBRIC
from .landing_policy import attest_identity, read_identity, write_identity

# Only operational failures may advance the explicitly selected read-only routes.
FALLBACK_CODES = {'HTTP_429', 'HTTP_502', 'HTTP_503', 'HTTP_504', 'PLAN_USAGE_LIMIT'}


def identity(deployment, role):
    from .native_catalog import roster_path
    return attest_identity(roster_path=roster_path(),
        agent_id='tare-run:'+deployment['id'], role=role, model_id=deployment['model'],
        runtime_key='native', runtime_model=deployment['native_profile']['model'],
        endpoint=deployment['native_profile'].get('endpoint'))


def independence(implementer, auditor):
    """landing_policy.independence of two deployments; None when either identity is unknown."""
    from .landing_policy import independence as rule
    try:
        return rule(identity(implementer, 'implementer'), identity(auditor, 'auditor'))
    except (ValueError, KeyError, TypeError, OSError):
        return None


def prepare(implementer, deployments, mode):
    from .landing_policy import AUDIT_MODES
    if mode not in AUDIT_MODES:
        raise ValueError('POLICY_INVALID')
    if deployments and mode == 'off':
        raise ValueError('AUDIT_SELECTION_CONFLICTS_WITH_OFF_POLICY')
    selection = None
    if deployments == 'auto':
        from .native_routes import independent_auditors
        deployments, selection = independent_auditors(implementer)
    from .landing_policy import independence as rule
    producer = identity(implementer, 'implementer')
    routes = []
    for deployment in deployments or []:
        auditor = identity(deployment, 'auditor')
        kind = rule(producer, auditor)
        routes.append({'deployment': deployment, 'identity_sha256': auditor.sha256,
            'eligible': kind != 'AUDITOR_NOT_INDEPENDENT',
            'reason': ('AUTO_SELECTED_INDEPENDENT' if selection else 'OPERATOR_SELECTED_INDEPENDENT') if kind == 'INDEPENDENT' else kind,
            'cross_vendor': auditor.vendor != producer.vendor})
    return {'mode': 'required' if deployments or selection else mode, 'producer_sha256': producer.sha256,
            'routes': routes, 'status': 'READY' if any(r['eligible'] for r in routes) else
            'BLOCKED' if mode == 'required' or deployments or selection else 'SKIPPED',
            **({'selection': selection} if selection else {}),
            'reason': selection['reason'] if selection else 'EXPLICIT_ORDER' if deployments else 'NO_AUDITOR_SELECTED_'+mode.upper()}


def run(train, origin, candidate, receipt, stage, cancellation):
    policy = origin.get('audit')
    if not policy or policy['status'] == 'SKIPPED':
        return True  # Legacy policy remains owned by admission.
    if policy['status'] != 'READY':
        raise ValueError('INDEPENDENT_AUDITOR_ROUTE_REQUIRED')
    train = Path(train)
    from .train_execution import read_execution_descriptor
    from .candidate_change import verify_candidate_receipt
    from compute_plane.workspace import qualify_ephemeral_git_worktree, WorkspaceQualificationRejected
    descriptor = read_execution_descriptor(train/'EXECUTION_DESCRIPTOR.json')
    producer = read_identity(train/'IMPLEMENTER_IDENTITY.json')
    if producer.sha256 != policy['producer_sha256']:
        raise ValueError('AUDIT_PRODUCER_IDENTITY_CHANGED')
    custody = audit_custody(origin)
    custody.mkdir(exist_ok=True)
    ready = native_agent.read_json(train/('WORKSPACE_READY.'+candidate.payload['worktree_id']+'.json'))
    worktree = Path(ready['destination'])
    def check_candidate():
        verify_candidate_receipt(candidate, descriptor, repository_root=origin['repository_root'])
        try:
            observed = qualify_ephemeral_git_worktree(worktree)
        except WorkspaceQualificationRejected:
            raise ValueError('AUDIT_CANDIDATE_WORKTREE_CHANGED') from None
        if observed.head != candidate.payload['candidate_commit'] or not observed.clean:
            raise ValueError('AUDIT_CANDIDATE_WORKTREE_CHANGED')
    check_candidate()
    inputs = custody/'input'; inputs.mkdir(exist_ok=True)
    # Existing verification observations and exact Git diff, not implementer conversation.
    sources = {name: regular(train/name).read_bytes() for name in
        ('PACKET.md','EXECUTION_DESCRIPTOR.json','CANDIDATE_CHANGE.json','EXECUTOR_OBSERVATION.json')}
    sources['diff.patch'] = subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false','-C',origin['repository_root'],'diff','--no-ext-diff','--no-textconv',
        descriptor.base_commit,candidate.payload['candidate_commit'],'--'],capture_output=True,check=True).stdout
    for name, raw in sources.items():
        target = regular(inputs/name)
        if target.exists() and target.read_bytes() != raw:
            raise ValueError('AUDIT_INPUT_CHANGED:'+name)
        if not target.exists(): target.write_bytes(raw)
    for number, route in enumerate(policy['routes']):
        if not route['eligible']:
            continue
        if cancellation.is_cancelled():
            receipt['status'] = 'CANCELLED'; return False
        deployment = route['deployment']
        auditor = identity(deployment, 'auditor')
        if auditor.sha256 != route['identity_sha256']:
            raise ValueError('AUDITOR_IDENTITY_CHANGED')
        from . import work_control
        session = custody/str(number)
        if not work_control.checkpoint(train, origin, receipt, stage, cancellation,
                                       custody=session if (session/'native-agent.json').exists() else None):
            return False
        stage('code_audit', deployment=deployment['id'], reason=route['reason'], session=str(session))
        config = {'schema':'tare.tools/native-agent-config/1','role':'auditor','typed_output':True,
            'repositories':{'candidate':str(worktree.resolve()),'review':str(inputs.resolve())},
            'writable':{},'capabilities':['read','list','search'],'specgraph_edit_context':False,
            'context_policy':{'max_compactions':origin['effective_limits']['max_compactions_per_stage']}}
        result_file = session/'audit-result.json'
        if result_file.exists():
            result = native_agent.read_json(result_file, 4*1024*1024)
        else:
            result = native_agent.run_session(config,deployment,session,
                'Independently review the exact code candidate against review:PACKET.md and review:EXECUTION_DESCRIPTOR.json. '
                'Read review:diff.patch, review:CANDIDATE_CHANGE.json and review:EXECUTOR_OBSERVATION.json. '
                'Inspect relevant code in repository candidate. Recorded verification is evidence from the executor, '
                'not a test you ran. Do not expand the Work scope or trust implementer conclusions. '
                'Cite all five review files and candidate code supporting your verdict, using observed SHA-256 values. '
                + BLOCKER_RUBRIC + 'Return ACCEPT only without findings or remaining obligations; otherwise REVISE or INCONCLUSIVE. '
                'Keep the review concise. You cannot edit, run commands or grant admission.',
                limits=route['limits'],
                cancellation=cancellation,resume=(session/'native-agent.json').exists(),
                **work_control.ports(train, origin, session))
            if result['status'] == 'PAUSED':
                work_control.checkpoint(train, origin, receipt, stage, cancellation,
                                        custody=session, paused_result=result)
                return False
            if result['status'] != 'PAUSED' and (result['status'] == 'PROPOSED' or result.get('failure_code')):
                save_json(result_file,result)
        # An eligible route is independent exactly when it is of another vendor (landing_policy.independence).
        receipt['audit'] = {'deployment':deployment['id'],'session':str(session),'result':result,
                            'independent':route['cross_vendor'],'cross_vendor':route['cross_vendor']}
        stage('code_audit_result', deployment=deployment['id'], status=result['status'], failure_code=result.get('failure_code'))
        if cancellation.is_cancelled():
            receipt['status']='CANCELLED'; return False
        check_candidate()
        if result['status'] != 'PROPOSED':
            if result.get('failure_code') in FALLBACK_CODES and any(r['eligible'] for r in policy['routes'][number+1:]):
                stage('audit_fallback', from_deployment=deployment['id'], reason=result['failure_code'])
                continue
            receipt['status']='AUDIT_'+result['status']; return False
        from .native_artifacts import read_input
        handoff = read_input(result['artifact_file'])
        artifact = handoff['artifact']
        if artifact['role'] != 'auditor' or artifact['session_id'] != result['session_id'] or artifact['profile_digest'] != result['profile_digest']:
            raise ValueError('AUDIT_ARTIFACT_IDENTITY_CHANGED')
        review = artifact['payload']
        cited = {(s['repository'],s['path']) for s in review['sources']}
        if not {('review',name) for name in sources} <= cited or not any(repo=='candidate' for repo,path in cited):
            receipt['status']='AUDIT_INCONCLUSIVE'
            receipt['audit']['reason']='AUDIT_SOURCE_COVERAGE_MISSING'; return False
        receipt['audit']['verdict']=review['verdict']
        # Keep the validated finding evidence in the existing owner receipt.
        # Readers must not follow the result's arbitrary artifact_file path.
        receipt['audit']['observation'] = {
            'schema': 'tare.tools/work-audit-observation/1',
            'artifact_sha256': handoff['sha256'],
            **{key: review[key] for key in ('verdict', 'summary', 'findings', 'sources')},
        }
        if review['verdict'] != 'ACCEPT':
            receipt['status']='AUDIT_'+review['verdict']; return False
        from .work_run import neutralize
        text = '\n'.join(['# Native code audit',descriptor.work_id,
            'packet '+hashlib.sha256(sources['PACKET.md']).hexdigest(),
            'candidate '+candidate.payload['candidate_commit'],
            'artifact '+handoff['sha256'],
            neutralize(review['summary']), '## Audit Recommendation', 'AUDIT_VERDICT: VERIFIED_PASS'])+'\n'
        path=regular(train/'AUDITOR_IDENTITY.json')
        if path.exists():
            if read_identity(path).sha256 != auditor.sha256:raise ValueError('AUDITOR_IDENTITY_CHANGED')
        else:write_identity(path,auditor)
        path=regular(train/'AUDIT_REPORT.md')
        # A report saved before the marker moved to English is the same report.
        legacy = text.replace('## Audit Recommendation', '## Recomendação da Auditoria')
        if path.exists() and path.read_text(encoding='utf-8') not in (text, legacy):raise ValueError('AUDIT_REPORT_CHANGED')
        if not path.exists():
            from .relay_io import write_text_atomic
            write_text_atomic(path,text)
        stage('code_audit_accepted',deployment=deployment['id'],candidate=candidate.sha256)
        return True
    raise ValueError('INDEPENDENT_AUDITOR_ROUTE_REQUIRED')


def audit_custody(origin):
    return Path(origin.get('audit_custody') or Path(origin['implementation_custody']).parent/'code-audit')


def new_attempt(train, origin):
    """Explicit retry after operational failure; never retry a semantic review."""
    previous = audit_custody(origin)
    results = []
    for number, route in enumerate((origin.get('audit') or {}).get('routes', [])):
        session = previous/str(number)
        if (session/'native-agent.json').exists() and not (session/'audit-result.json').exists():
            raise ValueError('AUDIT_RETRY_REQUIRES_FINISHED_OPERATIONAL_FAILURE')
        if (session/'audit-result.json').exists():
            results.append(native_agent.read_json(session/'audit-result.json',4*1024*1024))
    if not results or any(r.get('failure_code') not in FALLBACK_CODES | {'INVALID_DELIVERY','INVALID_PROVIDER_RESPONSE','AUTH_FAILED'} for r in results):
        raise ValueError('AUDIT_RETRY_REQUIRES_FINISHED_OPERATIONAL_FAILURE')
    next_root = previous.parent/('audit-'+str(len(origin.get('audit_attempts', []))+1))
    if next_root.exists():
        raise ValueError('AUDIT_RETRY_CUSTODY_EXISTS')
    updated = dict(origin, audit_custody=str(next_root),
                   audit_attempts=[*origin.get('audit_attempts', []),str(previous)])
    save_json(Path(train)/'WORK_RUN.json',updated)
    origin.update(updated)


def select_deployments(names=None, paths=None):
    from .native_catalog import select
    if names and 'auto' in names:
        if names != ['auto'] or paths:
            raise ValueError('AUTO_AUDITOR_CANNOT_MIX_EXPLICIT_ROUTES')
        return 'auto'
    return [select(name) for name in names] if names else [select(path=p) for p in paths] if paths else None

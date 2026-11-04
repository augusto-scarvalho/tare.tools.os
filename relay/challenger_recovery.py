"""An explicit failed-arm retry keeps the real finished reference and creates fresh bounded custody."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
from uuid import uuid4

from compute_plane.native_journal import custody_lock, regular, save_json
from . import challenger
from .native_agent import read_json, workspace_locks
from .challenger_workspace import changes, snapshot, tree


def selected(root, job):
    job = regular(Path(job).resolve())
    if job.parent != challenger.directory(root)/'runs':
        raise ValueError('This experiment belongs to another project. Select a result from this project.')
    return job, challenger.read_run(job/'run.json')


def retry(root, job):
    root = regular(Path(root).resolve())
    job, row = selected(root, job)
    if row['status'] in challenger.ACTIVE:
        raise ValueError('This experiment is still running. Stop it and wait for its receipt before retrying.')
    if row.get('managed_train') or row['config'].get('project_graph'):
        raise ValueError('This saved input needs its original Work or graph recovery path; no model was called.')
    if row.get('checks_passed') and row.get('challenger_verdict') in {'accepted', 'rejected'}:
        raise ValueError('This challenger already has a qualified verdict. Its result is preserved; no retry was started.')
    source = regular(Path(row['source']).resolve())
    reference_root = tree(job, 'reference', row)
    if not (job/'reference.json').is_file() or not (source/'native-agent.json').is_file() or not reference_root.is_dir():
        raise ValueError('The finished reference is not available. Wait for it or recover its saved evidence; no model was called.')
    with ExitStack() as stack:
        stack.enter_context(custody_lock(job))
        stack.enter_context(custody_lock(source))
        workspace_locks(stack, {'project': str(root), 'reference': str(reference_root)})
        reference = read_json(job/'reference.json', 1024*1024)
        state = read_json(source/'native-agent.json', 1024*1024)
        invocation = (state.get('invocations') or [{}])[-1]
        if (reference.get('outcome', {}).get('status') != 'PROPOSED' or invocation.get('status') != 'PROPOSED'
                or not invocation.get('ended_at') or reference.get('invocation') != invocation.get('id')
                or state.get('config', {}).get('role') != 'implementer'
                or state.get('deployment') != row['baseline'] or state.get('instruction') != row['instruction']):
            raise ValueError('The reference did not finish with matching producer evidence. Recover it first; no model was called.')
        if not reference.get('snapshot') or changes(reference_root, reference['snapshot']):
            raise ValueError('The saved reference changed. Preserve its receipts and inspect the change before retrying.')
        if changes(root, row['base']):
            raise ValueError('The project no longer matches the saved input. Start a new comparison for the current files.')
        if not challenger.policy(root)['enabled']:
            raise ValueError('Experiments are disabled. Enable them before requesting a bounded retry.')
        from .challenger_worker import session_config
        original = {**row, 'config': state['config'],
                    'project': next(iter(state['config']['repositories'].values()))}
        execution = session_config(original, root, 'implementer')
        if execution != session_config(row, root, 'implementer'):
            raise ValueError('Saved execution settings differ from the real producer. Recover that evidence before retrying.')
        request = {'id': uuid4().hex, 'job': str(job), 'base': row['base'], 'reference': reference,
            'reference_root': str(reference_root),
            'source_state_sha256': hashlib.sha256((source/'native-agent.json').read_bytes()).hexdigest(),
            'reference_receipt_sha256': hashlib.sha256((job/'reference.json').read_bytes()).hexdigest(),
            'config': execution}
        if row.get('checks_passed') and row.get('challenger_outcome') == 'PROPOSED':
            candidate = tree(job, 'candidate', row)
            workspace_locks(stack, {'candidate': str(candidate)})
            producer = regular(Path((row.get('implementation_reuse') or {}).get('source', job/'implementation')))
            stack.enter_context(custody_lock(producer))
            completed = read_json(producer/'native-agent.json', 1024*1024)
            outcome = read_json(producer/'outcome.json', 1024*1024)
            invocation = (completed.get('invocations') or [{}])[-1]
            if (invocation.get('status') != 'PROPOSED' or not invocation.get('ended_at')
                    or outcome.get('status') != 'PROPOSED' or completed.get('deployment') != row['challenger']
                    or completed.get('instruction') != row['instruction']
                    or completed.get('config', {}).get('role') != 'implementer'
                    or challenger.git(candidate, 'diff', '--binary', row['base'], '--') != (job/'challenger.patch').read_bytes()):
                raise ValueError('The completed challenger or its producer changed; no review was dispatched.')
            request['implementation'] = dict(source=str(producer), deployment=completed['deployment'],
                state_sha256=hashlib.sha256((producer/'native-agent.json').read_bytes()).hexdigest(),
                outcome_sha256=hashlib.sha256((producer/'outcome.json').read_bytes()).hexdigest(),
                patch_sha256=hashlib.sha256((job/'challenger.patch').read_bytes()).hexdigest(),
                workspace_job=row.get('workspace_reuse', str(job)))
        challenger.start(source, state, retry=request)
        marker = source/'challenger-retries'/(request['id']+'.json')
        result = read_json(marker) if marker.exists() else {'status': 'SKIPPED', 'reason': 'No eligible challenger was admitted.'}
        if result.get('status') != 'RUNNING':
            raise ValueError(result.get('reason', 'The retry was not admitted; no model was called.'))
        return result


def publish_reference(job, row, request):
    # Admission holds the original project and reference locks. A new tree must
    # still have the same Git content as the captured input before any dispatch.
    if challenger.git(row['project'], 'rev-parse', row['base']+'^{tree}') != challenger.git(
            row['project'], 'rev-parse', request['base']+'^{tree}'):
        raise ValueError('The captured input changed during retry preparation; no model was called.')
    row['reference_reuse'] = {key: request[key] for key in
        ('job', 'source_state_sha256', 'reference_receipt_sha256')}
    row['execution_contract']['reference_reuse'] = 1
    root = tree(job, 'reference', row)
    captured = request['reference']['snapshot'] if row.get('workspace_reuse') else snapshot(Path(request['reference_root']), root)
    save_json(job/'reference.json', {**request['reference'], 'snapshot': captured,
        'reused_from': request['job']}, max_bytes=1024*1024)


def validate_reference(row):
    if not row.get('reference_reuse'):
        return
    state = regular(Path(row['source'])/'native-agent.json')
    if state.stat().st_size > 1024*1024 or hashlib.sha256(state.read_bytes()).hexdigest() != row['reference_reuse']['source_state_sha256']:
        raise ValueError('The saved reference producer changed. Results are preserved without further model dispatch.')
    reused = row.get('implementation_reuse')
    if reused:
        source = regular(Path(reused['source']))
        for name, key in [('native-agent.json', 'state_sha256'), ('outcome.json', 'outcome_sha256')]:
            path = source/name
            if path.stat().st_size > 1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest() != reused[key]:
                raise ValueError('The completed challenger producer changed; no review was dispatched.')
        patch = challenger.git(tree(Path(row['path']), 'candidate', row), 'diff', '--binary', row['base'], '--')
        if hashlib.sha256(patch).hexdigest() != reused['patch_sha256']:
            raise ValueError('The completed challenger changed; no review was dispatched.')


def stop(root, job):
    job, row = selected(root, job)
    if row['status'] not in challenger.ACTIVE:
        return 'This experiment has already stopped. Its receipts are preserved.'
    try:
        with regular(job/'cancel').open('xb'):
            pass
    except FileExistsError:
        pass
    return 'Stop requested. An in-flight call may still finish; saved receipts are preserved.'

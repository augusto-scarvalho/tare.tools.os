"""Isolated advisory implementations; import never substitutes for Work admission."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from compute_plane.managed_process import host_executable
from uuid import uuid4

from compute_plane.native_journal import custody_lock, regular, save_json
from compute_plane.workspace import (isolated_child_environment, provision_linked_worktree,
                                     qualify_ephemeral_git_worktree)
from .native_agent import read_json

MAX_PATCH = 20 * 1024 * 1024
SCHEMA = 'tare.tools/prepared-implementation/1'
SHARED_SCHEMA = 'tare.tools/prepared-implementation/2'


def _git(root, *args, input=None, check=True):
    # Match Kernel provisioning/qualification when Git rehashes a worktree file;
    # otherwise CRLF can appear as an index conflict depending on cached stat data.
    return subprocess.run([host_executable('git', required=True), '-c', 'core.hooksPath=', '-c', 'core.longpaths=true',
        '-c', f"core.autocrlf={'true' if os.name == 'nt' else 'false'}", *args],
        cwd=root, env=isolated_child_environment(), input=input, capture_output=True, check=check)


def _hash(path, limit=MAX_PATCH):
    path = regular(path)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('PREPARED_ARTIFACT_OVER_BUDGET:' + path.name)
    return hashlib.sha256(raw).hexdigest()


def _node(node):
    return {key: node.get(key) for key in ('id', 'title', 'summary', 'repository_scopes',
        'target_paths', 'exit_criteria', 'source_refs', 'spec_ref')} | {
        'verification_checks': (node.get('metrics') or {}).get('verification_checks')}


def _attest(deployment):
    from . import relay_mesh
    from .landing_policy import attest_identity
    from .native_generation import deployment_profile
    profile = deployment_profile(deployment, deployment['native_profile']['model'])
    if not deployment.get('model'):
        raise ValueError('native_work_requires_registered_model_identity')
    return attest_identity(roster_path=relay_mesh.RELAY_DIR/'round_table_roster.json',
        agent_id='tare-run:' + deployment['id'], role='implementer', model_id=deployment['model'],
        runtime_key='native', runtime_model=profile.model)


def _entries(root, tree, paths):
    result = {name: None for name in paths}
    if not result:
        return result
    raw = _git(root, 'ls-tree', '-r', '-z', tree, '--', *paths).stdout
    for row in raw.split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            mode, kind, digest = metadata.decode().split()
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise ValueError('PREPARED_UNSUPPORTED_FILE_MODE')
            result[name.decode('utf-8')] = {'mode': mode, 'blob': digest}
    return result


def _scope(paths, tracked):
    for name in paths:
        parts = PurePosixPath(name)
        if (parts.is_absolute() or '..' in parts.parts or '.git' in parts.parts
                or '\\' in name or ':' in name or not name):
            raise ValueError('PREPARED_UNSAFE_PATH')
        if not any(name == p.rstrip('/') or name.startswith(p.rstrip('/') + '/') for p in tracked):
            raise ValueError('PREPARED_PATH_OUTSIDE_SCOPE:' + name)


def _changed_paths(root, base):
    """Use the same Git configuration as checkout, staging and incorporation."""
    from .challenger_workspace import safe
    names = set(_git(root, 'diff', '--name-only', '--no-ext-diff', '--no-renames', '-z', base, '--').stdout.decode().split('\0'))
    names.update(_git(root, 'ls-files', '--others', '--exclude-standard', '-z').stdout.decode().split('\0'))
    names.discard('')
    size = 0
    for name in names:
        path = safe(root, name)
        if path.is_dir():
            raise ValueError('PREPARED_UNSUPPORTED_FILE_MODE')
        size += path.stat().st_size if path.exists() else 0
        if size > MAX_PATCH or len(names) > 512:
            raise ValueError('PREPARED_PATCH_OVER_BUDGET')
    return sorted(names)


def prepare(directory, node, root, deployment, *, cancellation=None, unlimited=False, limits=None,
            findings=None, findings_sha256=None):
    """Resume the same Runtime journal; a proposal remains advisory and unadmitted."""
    from . import native_agent
    from .native_work_host import stage_limits, compaction_limit
    from .work_run import derive_descriptor, implement_instruction
    from .train_execution import canonical_json_bytes, parse_execution_descriptor
    directory, root = regular(directory).resolve(), Path(root).resolve()
    if bool(findings) != bool(findings_sha256):
        raise ValueError('Provide reviewed findings and their SHA-256 together.')
    directory.mkdir(parents=True, exist_ok=True)
    with custody_lock(directory):
        if (directory/'prepared.json').exists():
            value = inspect(directory)
            if value['node'] != _node(node) or value['repository_root'] != str(root) or value['deployment'] != deployment:
                raise ValueError('PREPARED_INPUT_CHANGED')
            if findings is not None:
                selected = read_json(directory/'intent.json', 1024*1024).get('shared_context', {}).get('origin', {})
                if str(Path(findings).resolve()) != selected.get('path') or findings_sha256 != selected.get('sha256'):
                    raise ValueError('PREPARED_FINDINGS_CHANGED')
            return value
        intent_path = directory/'intent.json'
        if intent_path.exists():
            intent = read_json(intent_path, 1024*1024)
            if (intent.get('schema') not in {SCHEMA, SHARED_SCHEMA}
                    or (intent.get('schema') == SHARED_SCHEMA) != ('shared_context' in intent)):
                raise ValueError('PREPARED_CONTRACT_INVALID')
            if intent['node'] != _node(node) or intent['repository_root'] != str(root) or intent['deployment'] != deployment:
                raise ValueError('PREPARED_INPUT_CHANGED')
        else:
            descriptor = derive_descriptor(node, train_id='prepared-' + uuid4().hex, repository_root=root)
            intent = {'schema': SCHEMA, 'node': _node(node), 'repository_root': str(root),
                'deployment': deployment, 'identity_sha256': _attest(deployment).sha256,
                'descriptor': descriptor, 'worktree_id': 'prepare-' + uuid4().hex,
                'workspace': str(directory/'workspace'),
                'limits': stage_limits(deployment, 'implement', unlimited=unlimited, limits=limits),
                'compactions': compaction_limit(unlimited)}
            save_json(intent_path, intent, max_bytes=1024*1024)
        if findings is not None and 'shared_context' not in intent:
            if (directory/'session/native-agent.json').exists():
                raise ValueError('PREPARED_FINDINGS_REQUIRE_A_NEW_SESSION')
            from .work_context_reuse import prepare as prepare_context
            intent['shared_context'] = prepare_context(findings, findings_sha256, root)
            intent['schema'] = SHARED_SCHEMA
            save_json(intent_path, intent, max_bytes=1024*1024)
        elif findings is not None:
            selected = intent['shared_context']['origin']
            if str(Path(findings).resolve()) != selected['path'] or findings_sha256 != selected['sha256']:
                raise ValueError('PREPARED_FINDINGS_CHANGED')
        descriptor = parse_execution_descriptor(canonical_json_bytes(intent['descriptor']))
        workspace = Path(intent['workspace'])
        if not workspace.exists():
            if (directory/'session').exists():
                raise ValueError('PREPARED_WORKSPACE_MISSING')
            from .strict_workspace import adding_worktree
            with adding_worktree(root):  # two Works are prepared at once on the same repository
                provision_linked_worktree(root, workspace, base_commit=descriptor.base_commit,
                                         worktree_id=intent['worktree_id'])
        qualified = qualify_ephemeral_git_worktree(workspace, require_clean=False)
        common = Path(_git(root, 'rev-parse', '--git-common-dir').stdout.decode().strip())
        common = (root/common).resolve()
        if qualified.head != descriptor.base_commit or qualified.git_common_dir != common:
            raise ValueError('PREPARED_WORKSPACE_CHANGED')
        config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
            'repositories': {descriptor.execution_repository: str(workspace)},
            'writable': {descriptor.execution_repository: list(descriptor.tracked_paths)},
            'specgraph_edit_context': False,
            'verifiers': {c.criterion: {'repository': descriptor.execution_repository, 'argv': list(c.command)}
                          for c in descriptor.verification_checks}, 'typed_output': True,
            'context_policy': {'max_compactions': intent['compactions']}}
        if intent.get('shared_context') is not None:
            config['shared_context'] = intent['shared_context']
        session = directory/'session'
        outcome = read_json(session/'outcome.json', 4*1024*1024) if (session/'outcome.json').exists() else None
        if not outcome or outcome['status'] != 'PROPOSED':
            resume = (session/'native-agent.json').exists()
            outcome = native_agent.run_session(config, deployment, session, implement_instruction(node, descriptor),
                resume=resume, resume_interrupted_model=resume,
                limits=intent['limits'], cancellation=cancellation)
        if outcome['status'] != 'PROPOSED':
            return {**outcome, 'artifact': str(directory)}
        from .native_artifacts import read_input
        proposal_path = regular(outcome['artifact_file'])
        if not proposal_path.resolve().is_relative_to(session.resolve()):
            raise ValueError('PREPARED_PROPOSAL_OUTSIDE_SESSION')
        proposal = read_input(proposal_path)['artifact']
        changed = _changed_paths(workspace, descriptor.base_commit)
        _scope(changed, descriptor.tracked_paths)
        if changed:
            _git(workspace, 'add', '--all', '--', *changed)
        patch = _git(workspace, 'diff', '--cached', '--binary', '--no-ext-diff', '--no-renames', descriptor.base_commit, '--').stdout
        if len(patch) > MAX_PATCH:
            raise ValueError('PREPARED_PATCH_OVER_BUDGET')
        (directory/'implementation.patch').write_bytes(patch)
        tree = _git(workspace, 'write-tree').stdout.decode().strip()
        files = _entries(workspace, tree, changed)
        evidence = [intent_path, session/'native-agent.json', session/'journal.jsonl',
                    session/'outcome.json', proposal_path, directory/'implementation.patch']
        value = {'schema': SHARED_SCHEMA if intent.get('shared_context') is not None else SCHEMA, 'status': 'PREPARED', 'authority': 'NONE',
            'work_id': node['id'], 'node': intent['node'], 'repository_root': str(root),
            'repository': descriptor.execution_repository, 'base_commit': descriptor.base_commit,
            'tracked_paths': list(descriptor.tracked_paths), 'deployment': deployment,
            'identity_sha256': intent['identity_sha256'], 'session': str(session),
            'session_id': outcome['session_id'], 'profile_digest': outcome['profile_digest'],
            'proposal': proposal_path.relative_to(directory).as_posix(), 'files': files,
            'proposal_payload': proposal['payload'],
            'base_files': _entries(workspace, descriptor.base_commit, changed),
            'hashes': {p.relative_to(directory).as_posix(): _hash(p) for p in evidence}}
        save_json(directory/'prepared.json', value, max_bytes=1024*1024)
        return inspect(directory)


def inspect(directory):
    """Verify frozen custody, not merely a mutable status label."""
    from .native_artifacts import read_input
    directory = regular(directory).resolve()
    value = read_json(directory/'prepared.json', 1024*1024)
    if value.get('schema') not in {SCHEMA, SHARED_SCHEMA} or value.get('status') != 'PREPARED' or value.get('authority') != 'NONE':
        raise ValueError('PREPARED_CONTRACT_INVALID')
    for name, digest in value['hashes'].items():
        path = regular(directory/name)
        if not path.resolve().is_relative_to(directory.resolve()) or _hash(path) != digest:
            raise ValueError('PREPARED_EVIDENCE_CHANGED:' + name)
    session = directory/'session'
    outcome = read_json(session/'outcome.json', 4*1024*1024)
    proposal = read_input(directory/value['proposal'])['artifact']
    saved = read_json(session/'native-agent.json', 1024*1024)
    intent = read_json(directory/'intent.json', 1024*1024)
    expected_deployment = value['deployment']
    if value['schema'] == SHARED_SCHEMA:
        from .work_context_reuse import deployment_for_context, snapshot
        context = snapshot(intent.get('shared_context'))
        if saved['config'].get('shared_context') != context or intent.get('schema') != SHARED_SCHEMA:
            raise ValueError('PREPARED_SHARED_CONTEXT_CHANGED')
        expected_deployment = deployment_for_context(expected_deployment, context, 'implementer')
    required = {'intent.json', 'implementation.patch', 'session/native-agent.json',
                'session/journal.jsonl', 'session/outcome.json', value['proposal']}
    # Earlier preparations already hash the native proposal; expose its payload
    # without rewriting custody or invalidating the caller's recorded digest.
    payload = value.get('proposal_payload', proposal['payload'])
    if (outcome['status'] != 'PROPOSED' or proposal['role'] != 'implementer'
            or payload != proposal['payload'] or saved['deployment'] != expected_deployment
            or not required.issubset(value['hashes'])
            or intent['node'] != value['node'] or intent['deployment'] != value['deployment']
            or intent['repository_root'] != value['repository_root']
            or intent['identity_sha256'] != value['identity_sha256']
            or intent['descriptor']['base_commit'] != value['base_commit']
            or intent['descriptor']['work_id'] != value['work_id']
            or intent['descriptor']['execution_repository'] != value['repository']
            or intent['descriptor']['tracked_paths'] != value['tracked_paths']
            or set(value['base_files']) != set(value['files'])
            or any(proposal[k] != value[k] or outcome[k] != value[k] for k in ('session_id', 'profile_digest'))):
        raise ValueError('PREPARED_SOURCE_IDENTITY_CHANGED')
    _scope(value['files'], value['tracked_paths'])
    return {**value, 'proposal_payload': payload,
            'artifact': str(directory), 'sha256': _hash(directory/'prepared.json', 1024*1024)}


def identity(directory):
    value = inspect(directory)
    result = _attest(value['deployment'])
    if result.sha256 != value['identity_sha256']:
        raise ValueError('PREPARED_MODEL_IDENTITY_CHANGED')
    return result


def check(directory, root, base, tracked_paths):
    value = inspect(directory)
    if Path(root).resolve() != Path(value['repository_root']).resolve():
        raise ValueError('PREPARED_REPOSITORY_CHANGED')
    _scope(value['files'], tracked_paths)
    current = _entries(root, base, value['files']) if value['files'] else {}
    overlap = sorted(name for name in value['files'] if current.get(name) != value['base_files'].get(name))
    return {'status': 'CONFLICT' if overlap else 'READY', 'reason': 'PREPARED_BASE_OVERLAP' if overlap else None,
            'files': overlap, 'artifact': value['artifact'], 'sha256': value['sha256'], 'session': value['session']}


def apply(directory, descriptor, lease):
    """Import once into the current strict workspace; conflicts leave it untouched."""
    directory = regular(directory).resolve()
    with custody_lock(directory):
        value = inspect(directory)
        if (value['work_id'] != descriptor.work_id or value['repository'] != descriptor.execution_repository
                or sorted(value['tracked_paths']) != sorted(descriptor.tracked_paths)
                or value['node']['verification_checks'] != [
                    {'criterion': c.criterion, 'command': list(c.command)} for c in descriptor.verification_checks]):
            raise ValueError('PREPARED_DESCRIPTOR_CHANGED')
        result = check(directory, lease.repository_root, descriptor.base_commit, descriptor.tracked_paths)
        if result['status'] == 'CONFLICT':
            return result
        observed = qualify_ephemeral_git_worktree(lease.root, require_clean=False)
        if observed.head != descriptor.base_commit or observed.git_admin_dir != lease.git_admin_dir:
            raise ValueError('PREPARED_TARGET_WORKSPACE_CHANGED')
        patch = (directory/'implementation.patch').read_bytes()
        marker = directory/('import-' + hashlib.sha256(str(lease.root).encode()).hexdigest() + '.json')
        intent = {'source': value['sha256'], 'descriptor': descriptor.sha256, 'workspace': str(lease.root)}
        previous = read_json(marker) if marker.exists() else None
        if previous is not None and previous != intent:
            raise ValueError('PREPARED_IMPORT_CHANGED')
        if not observed.clean:
            if previous is None or _git(lease.root, 'apply', '--reverse', '--check', '--index', input=patch, check=False).returncode:
                return {**result, 'status': 'CONFLICT', 'reason': 'PREPARED_TARGET_DIRTY'}
            names = set(_git(lease.root, 'diff', '--name-only', '-z', descriptor.base_commit).stdout.decode().split('\0')) - {''}
            tree = _git(lease.root, 'write-tree').stdout.decode().strip()
            if (names != set(value['files']) or _entries(lease.root, tree, names) != value['files']
                    or _git(lease.root, 'diff', '--quiet', check=False).returncode
                    or _git(lease.root, 'ls-files', '--others', '--exclude-standard').stdout):
                return {**result, 'status': 'CONFLICT', 'reason': 'PREPARED_TARGET_DIRTY'}
        else:
            # --index requires current index stat data, even when re-reading the
            # file proves its bytes unchanged. Refresh metadata without staging edits.
            if _git(lease.root, 'update-index', '--refresh', check=False).returncode:
                return {**result, 'status': 'CONFLICT', 'reason': 'PREPARED_TARGET_DIRTY'}
            if patch and _git(lease.root, 'apply', '--check', '--index', input=patch, check=False).returncode:
                return {**result, 'status': 'CONFLICT', 'reason': 'PREPARED_PATCH_CONFLICT'}
            if previous is None:
                save_json(marker, intent)
            if patch:
                _git(lease.root, 'apply', '--index', input=patch)
        return {**result, 'status': 'APPLIED', 'files': sorted(value['files'])}

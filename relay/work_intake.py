"""Materialize reviewed Work intent; reuse pinned-source acquisition and preparation.

Source bytes live in the explicit owner Git repo, in a retained ref; preparation
reads them at that commit. No shared catalog is written.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from compute_plane.managed_process import host_executable

from compute_plane.native_journal import custody_lock, regular, save_json
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.validation import validate_work_graph

from . import work_prepare
from .native_agent import read_json
from .train_execution import canonical_json_bytes, parse_execution_descriptor
from .work_run import derive_descriptor, git


def draft(graph_path, work_id):
    graph = read_json(graph_path, 32*1024*1024)
    if validate_work_graph(graph)['status'] != 'PASS':
        raise ValueError('Invalid Work graph.')
    node = next((n for n in graph['nodes'] if n['id'] == work_id), None)
    if (node is None or node.get('admission_state') != work_prepare.PROPOSED
            or node.get('completion', {}).get('status') != 'NOT_DONE'):
        raise ValueError('Select an unstarted PLAN proposal.')
    if len(node.get('repository_scopes', [])) != 1:
        raise ValueError('Intake supports one explicit repository per Work.')
    return graph, node


def preview(graph_path, work_id, root, criteria, argv):
    graph_path, root = Path(graph_path).resolve(), Path(root).resolve()
    graph, node = draft(graph_path, work_id)
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Select the repository root, not a child directory.')
    try:
        git(root, 'var', 'GIT_AUTHOR_IDENT')
        git(root, 'var', 'GIT_COMMITTER_IDENT')
    except subprocess.CalledProcessError:
        raise ValueError('Configure Git user.name and user.email in the owner repository before recording intent.') from None
    owner = node['repository_scopes'][0]['repository']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?', owner):
        raise ValueError('Invalid repository identity in the Work.')
    if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 64
            or any(not isinstance(c, str) or not c.strip() or len(c.encode()) > 4096 for c in criteria)
            or len(criteria) != len(set(criteria))):
        raise ValueError('Supply distinct nonempty criteria, one per line (at most 64).')
    # Exercise the same command/scope contract before writing any source objects.
    prepared = copy.deepcopy(node)
    acs = {f'AC-{i:02}': text for i, text in enumerate(criteria, 1)}
    prepared['exit_criteria'] = [key+': '+text for key, text in acs.items()]
    prepared['metrics']['verification_checks'] = [
        {'criterion': c, 'command': argv} for c in prepared['exit_criteria']]
    descriptor = parse_execution_descriptor(canonical_json_bytes(derive_descriptor(
        prepared, train_id='RUN-'+work_id, repository_root=root)))
    body = ('---\n'+json.dumps({'id': 'WORK-INTENT-'+work_id, 'status': 'OPERATOR_CONFIRMED',
        'acceptance_criteria': acs}, ensure_ascii=False, indent=2)+'\n---\n# '+node['title']+
        '\n\n'+node['summary']+'\n\nScope: '+json.dumps(node['repository_scopes'], ensure_ascii=False)+
        '\n\nVerification argv: '+json.dumps(argv, ensure_ascii=False)+
        '\n\nConfirmed Work intent; this is not evidence of passing verification.\n')
    if len(body.encode()) > 32768:
        raise ValueError('Source exceeds 32768 UTF-8 bytes; narrow the Work.')
    value = {'schema': 'tare.tools/work-intake/1', 'graph_path': str(graph_path),
        'work_id': work_id, 'root': str(root), 'repository': owner,
        'base_revision': compute_revision_hash(graph), 'base_commit': descriptor.base_commit,
        'target_ref': descriptor.target_ref, 'before': node, 'criteria': criteria, 'argv': argv,
        'body': body, 'source_sha256': hashlib.sha256(body.encode()).hexdigest()}
    return {'status': 'PREVIEW', 'intake': value, 'sha256': sha256_canonical(value)}


def _git(root, *args, data=None, env=None):
    return subprocess.run([host_executable('git', required=True), '-C', str(root), *args], input=data, env=env,
        capture_output=True, check=True).stdout.decode('utf-8').strip()


def _pin(value, directory, digest):
    """A retained source ref, without changing HEAD, the operator index or worktree."""
    root = value['root']; ref = 'refs/tare/work-intents/'+digest
    path = 'docs/work-intents/'+digest+'.md'
    try:
        commit = _git(root, 'rev-parse', '--verify', ref)
    except subprocess.CalledProcessError:
        index = directory/'source.index'
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        try:
            _git(root, 'read-tree', value['base_commit'], env=env)
            blob = _git(root, 'hash-object', '-w', '--stdin', data=value['body'].encode())
            _git(root, 'update-index', '--add', '--cacheinfo', '100644,'+blob+','+path, env=env)
            tree = _git(root, 'write-tree', env=env)
            commit = _git(root, '-c', 'commit.gpgsign=false', 'commit-tree', tree,
                '-p', value['base_commit'], data=('Record reviewed Work intent '+value['work_id']+'\n').encode())
            _git(root, 'update-ref', ref, commit, '0'*len(commit))
        finally:
            index.unlink(missing_ok=True)
    from specgraph.federation import read_pinned_document
    if read_pinned_document(root, commit, path, value['source_sha256']).decode() != value['body']:
        raise ValueError('Pinned intent differs from the reviewed source.')
    return commit, path, ref


def apply(view):
    value = view['intake']; digest = view['sha256']
    if digest != sha256_canonical(value):
        raise ValueError('Intake changed since preview.')
    control = Path(value['graph_path']).parent
    directory = regular(control/'.tare/work-intake'/digest)
    directory.mkdir(parents=True, exist_ok=True)
    with custody_lock(directory):
        saved = directory/'preparation.json'
        graph = read_json(value['graph_path'], 32*1024*1024)
        revision = compute_revision_hash(graph)
        prior = read_json(saved) if saved.exists() else None
        already_applied = prior is not None and revision == prior['preparation']['result_revision']
        if not already_applied:
            current = preview(value['graph_path'], value['work_id'], value['root'], value['criteria'], value['argv'])
            if current != view:
                raise ValueError('Work or repository changed; preview again.')
        save_json(directory/'intent.json', view)
        commit, path, ref = _pin(value, directory, digest)
        if prior is None:
            prior = work_prepare.prepare(value['graph_path'], value['work_id'], value['root'],
                value['repository']+':'+path, [f'AC-{i:02}' for i in range(1, len(value['criteria'])+1)], value['argv'],
                commit=commit)
            observed = prior['preparation']
            if (observed['base_revision'] != value['base_revision'] or observed['before'] != value['before']
                    or observed['repository_head'] != value['base_commit'] or observed['target_ref'] != value['target_ref']):
                raise ValueError('Work or repository changed while recording the source; preview again.')
            save_json(saved, prior)
        # Preparation owns graph mutation, source validation, readiness and replay.
        result = work_prepare.apply(prior)
        result.update(source_commit=commit, source_ref=ref, source_path=path,
            intake_sha256=digest, generation_calls=0,
            source_publication='LOCAL_OWNER_REF')
        save_json(directory/'result.json', result)
        return result


def render(view):
    value = view['intake']
    return '\n'.join(['Create source and prepare '+value['work_id'], 'Project: '+value['root'],
        'Objective: '+value['before']['title']+' — '+value['before']['summary'],
        'Scope: '+json.dumps(value['before']['repository_scopes'], ensure_ascii=False),
        'Proposed criteria: '+json.dumps(value['before']['exit_criteria'], ensure_ascii=False),
        'Exact source to record after confirmation:', value['body'],
        'Confirm records a local source revision and prepares this Work. Start executes it.',
        'The source is retained in the owner Git repository; HEAD and working files stay in place.',
        'No remote publication or shared Library catalog update. No tests have run.',
        'Review SHA-256: '+view['sha256']])

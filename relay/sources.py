"""Specs and ADRs read where they live, from Git blobs at an explicit commit; no catalog.

A reference is ``repo:path``. The Work's execution repository is read from its own checkout at the
run's base commit, ``tare.tools.os`` from the OS tree, and any other repository from
``satellites/<repo>`` at the commit the OS gitlink pins. A spec is admissible when its frontmatter
declares ``acceptance_criteria``. Sources are written in the v3 manifest format that SpecGraph
``ground`` and ``acquire_federated_documents`` already read.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

PINNED_LOCATOR = re.compile(r'[^@:]+@([0-9a-f]{40}|[0-9a-f]{64}):.+')


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Source unavailable in '+str(root)+': '+args[-1])
    return result.stdout


def entry(root, commit, ref, targets):
    """One manifest entry for the blob at commit:path; its id is the content SHA-256."""
    repository, _, path = ref.partition(':')
    commit = _git(root, 'rev-parse', '--verify', commit+'^{commit}').decode().strip()
    sha = hashlib.sha256(_git(root, 'cat-file', 'blob', commit+':'+path)).hexdigest()
    locator = repository+'@'+commit+':'+path
    return {'id': sha, 'semantic_document_id': ref, 'doc_type': 'spec', 'title': ref, 'relative_path': locator,
            'sha256': sha, 'status': 'REPOSITORY_OWNED', 'authority_state': 'DECLARED_ACTIVE',
            'target_repositories': sorted(targets), 'acceptance_criteria': [],
            'source_paths': [{'relative_path': locator, 'semantic_document_id': ref,
                              'authority_state': 'DECLARED_ACTIVE', 'editorial_status': 'REPOSITORY_OWNED'}]}


def save(path, entries):
    Path(path).write_text(json.dumps({'version': '3.0.0', 'authority_vocabulary': 'authority_state/v1',
                                      'specs': entries}, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    return Path(path)


def acquire(root, ref, commit='HEAD'):
    """The document's verified body and criteria from its Git blob, never the working file."""
    from specgraph.federation import acquire_federated_documents
    repository, separator, _ = ref.partition(':')
    if not separator or not repository:
        raise ValueError('Select a repository-qualified source.')
    with tempfile.TemporaryDirectory() as directory:
        manifest = save(Path(directory)/'SOURCES.json', [entry(root, commit, ref, [repository])])
        result = acquire_federated_documents(manifest, repository, {repository: root},
                                             include_ids=[ref], max_bytes=65536)
    if result['status'] != 'READY':
        raise ValueError('Source unavailable: '+result['status']+' '+result.get('reason', ''))
    return result['documents'][0]


def list_specs(root, repository):
    """Admissible specs at HEAD of one checkout, qualified with the repository name."""
    # ponytail: a line-start match, not a frontmatter parse; preparation refuses a false positive.
    result = subprocess.run(['git', '-C', str(root), 'grep', '-l', '-z', '-E', '^acceptance_criteria:',
                             'HEAD', '--', '*.md'], capture_output=True, timeout=30)
    if result.returncode not in (0, 1):  # 1: no match
        raise ValueError('Sources unavailable in '+str(root))
    return sorted(repository+':'+name.split(':', 1)[1] for name in result.stdout.decode().split('\0') if name)


def _owner(repository, roots):
    if repository in roots:
        return roots[repository]
    from . import relay_mesh
    if repository == 'tare.tools.os':
        return relay_mesh.RELAY_ROOT, 'HEAD'
    gitlink = _git(relay_mesh.RELAY_ROOT, 'rev-parse', 'HEAD:satellites/'+repository).decode().strip()
    return relay_mesh.RELAY_ROOT/'satellites'/repository, gitlink


def retained_commit(node, graph_sources):
    """The commit an intake intent is kept at: a hidden ref, not the branch, so its locator keeps it."""
    for key in node.get('source_refs', []):
        record = (graph_sources or {}).get(key) or {}
        match = PINNED_LOCATOR.fullmatch(record.get('locator') or '')
        if match and node.get('spec_ref') and record.get('repository_ref') == node['spec_ref']:
            return match.group(1)
    return None


def write(path, node, graph_sources=None, roots=None):
    """SOURCES.json for one Work: its spec_ref and grounding_refs, read where they live.

    ``roots`` maps the execution repository to its checkout and the run's base commit. A reference
    that cannot be read is left out, so grounding reports it missing instead of this file hiding it.
    """
    roots, scopes, spec = roots or {}, node.get('repository_scopes') or [], node.get('spec_ref')
    refs = {ref for scope in scopes for ref in scope.get('grounding_refs', [])} | ({spec} if spec else set())
    retained = retained_commit(node, graph_sources)
    entries = []
    for ref in sorted(refs):
        repository, separator, _ = ref.partition(':')
        if not separator:
            continue
        targets = {repository, *(scope['repository'] for scope in scopes if scope.get('repository'))}
        try:
            root, commit = _owner(repository, roots)
            try:
                entries.append(entry(root, commit, ref, targets))
            except ValueError:
                if ref != spec or retained is None:
                    raise
                entries.append(entry(root, retained, ref, targets))
        except ValueError:
            continue
    return save(path, entries)

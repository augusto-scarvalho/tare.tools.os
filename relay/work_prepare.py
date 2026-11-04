"""Explicit preparation of PLAN proposals using existing source and Work contracts."""
import argparse
import copy
import json
from pathlib import Path

from graph_backlog.algorithms import readiness
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.mutations import execute_graph_transaction
from graph_backlog.validation import validate_work_graph
from specgraph.federation import check_work_source_binding

from . import sources
from .native_agent import read_json
from .train_execution import canonical_json_bytes, parse_execution_descriptor
from .work_run import derive_descriptor, git

PROPOSED = 'PROPOSED_FROM_PLAN_NOT_ADMITTED'


def _work(graph_path, work_id):
    graph = read_json(graph_path, 32*1024*1024)
    node = next((n for n in graph['nodes'] if n['id'] == work_id), None)
    if node is None or len(node.get('repository_scopes', [])) != 1:
        raise ValueError('Select a Work with one repository scope first.')
    return graph, node


def _read(root, source_ref, commit, node, graph):
    """The source at commit; the Work's own intake intent, absent at HEAD, at the commit its record keeps."""
    try:
        return sources.acquire(root, source_ref, commit), commit
    except ValueError:
        retained = sources.retained_commit(node, graph.get('sources', {}))
        if commit != 'HEAD' or source_ref != node.get('spec_ref') or retained is None:
            raise
        return sources.acquire(root, source_ref, retained), retained


def source_options(graph_path, work_id, root):
    """Admissible specs in the selected checkout, named as the Work names its repository, plus its kept intent."""
    graph, node = _work(graph_path, work_id)
    options = sources.list_specs(root, node['repository_scopes'][0]['repository'])
    if sources.retained_commit(node, graph.get('sources', {})):
        options = sorted({*options, node['spec_ref']})
    return options


def source_details(graph_path, work_id, root, source_ref):
    graph, node = _work(graph_path, work_id)
    return _read(Path(root).resolve(), source_ref, 'HEAD', node, graph)[0]


def prepare(graph_path, work_id, root, source_ref, criteria, argv, *, graph_value=None, commit='HEAD'):
    """Read-only preview. Criteria replacement and command are visible before mutation.

    The source is read from the owner's Git blob at ``commit``: HEAD, or the retained intake commit."""
    graph_path, root = Path(graph_path).resolve(), Path(root).resolve()
    graph = read_json(graph_path, 32*1024*1024) if graph_value is None else graph_value
    if validate_work_graph(graph)['status'] != 'PASS':
        raise ValueError('Invalid Work graph.')
    node = next((n for n in graph['nodes'] if n['id'] == work_id), None)
    if node is None or node.get('admission_state') != PROPOSED:
        raise ValueError('Select an unprepared PLAN proposal.')
    if (node.get('completion') or {}).get('status') != 'NOT_DONE':
        raise ValueError('Only an unstarted proposal can be prepared.')
    scopes = node.get('repository_scopes', [])
    if len(scopes) != 1:
        raise ValueError('Preparation currently supports one repository per Work.')
    source, commit = _read(root, source_ref, commit, node, graph)
    if (not isinstance(criteria, list) or not criteria or len(criteria) != len(set(criteria))
            or any(c not in source['acceptance_criteria'] for c in criteria)):
        raise ValueError('Select distinct criterion IDs present in the pinned source.')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise ValueError('Provide verification as a nonempty JSON argv array.')
    prepared = copy.deepcopy(node)
    prepared['repository_scopes'][0].update(repository=source['repository'], grounding_refs=[source_ref])
    prepared['repository_scopes'][0]['target_paths'] = sorted(scopes[0]['target_paths'])
    prepared['exit_criteria'] = [c+': '+source['acceptance_criteria'][c] for c in criteria]
    prepared['spec_ref'] = source_ref
    source_key = 'plan-source-'+sha256_canonical([source_ref, source['sha256']])[:24]
    prepared['source_refs'] = [source_key]
    prepared['admission_state'] = 'ADMITTED'
    prepared['metrics'] = {**prepared.get('metrics', {}),
        'prepared_from_plan': node['projection_run_id'],
        'verification_checks': [{'criterion': c, 'command': list(argv)} for c in prepared['exit_criteria']]}
    prepared['notes'] = [*node.get('notes', []),
        'Operator preparation binds canonical criteria and verification; no test pass or landing is claimed.']
    prepared['canonical_revision'] = 'prepared-'+sha256_canonical(prepared)[:24]
    binding = check_work_source_binding(prepared, source, criteria, expected_source_sha256=source['sha256'])
    if binding['status'] != 'MATCHED':
        raise ValueError('Source binding refused: '+str(binding['reason_codes']))
    payload = derive_descriptor(prepared, train_id='RUN-'+work_id, repository_root=root, work_graph=str(graph_path))
    descriptor = parse_execution_descriptor(canonical_json_bytes(payload))
    source_record = {'kind': 'repo_file', 'title': source_ref, 'recoverability': 'CONTENT_ADDRESSED',
        'repository_ref': source_ref, 'sha256': source['sha256'],
        'locator': source_ref if commit == 'HEAD' else source['repository']+'@'+source['revision']+':'+source['path']}
    if source_key in graph['sources'] and graph['sources'][source_key] != source_record:
        raise ValueError('Source identity collision.')
    updated = copy.deepcopy(graph)
    updated['nodes'] = [prepared if n['id'] == work_id else n for n in updated['nodes']]
    updated['sources'][source_key] = source_record
    updated['revision'] = compute_revision_hash(updated)
    validation = validate_work_graph(updated)
    if validation['status'] != 'PASS':
        raise ValueError('Prepared Work violates graph contract: '+str(validation['errors']))
    feasibility = readiness(WorkGraph(updated), prepared, 'operational')
    value = {'schema': 'tare.tools/work-preparation/1', 'graph_path': str(graph_path),
        'base_revision': compute_revision_hash(graph), 'root': str(root), 'work_id': work_id,
        'commit': commit, 'source_ref': source_ref,
        'criteria': criteria, 'argv': argv, 'source': source, 'source_key': source_key,
        'source_record': source_record, 'before': node, 'after': prepared,
        'repository_head': descriptor.base_commit, 'target_ref': descriptor.target_ref,
        'readiness': feasibility, 'result_revision': updated['revision']}
    return {'status': 'PREVIEW', 'sha256': sha256_canonical(value), 'preparation': value}


def apply(view):
    value = view['preparation']
    if view['sha256'] != sha256_canonical(value):
        raise ValueError('Preparation changed since preview.')
    applied = False
    def mutation(wg):
        nonlocal applied
        graph = wg.to_dict()
        revision = compute_revision_hash(graph)
        if revision == value['result_revision']:
            return graph
        if revision != value['base_revision']:
            raise ValueError('Graph changed; preview the preparation again.')
        current = prepare(value['graph_path'], value['work_id'], value['root'], value['source_ref'],
                          value['criteria'], value['argv'], commit=value['commit'])
        if current['sha256'] != view['sha256']:
            raise ValueError('Source or repository changed; preview the preparation again.')
        graph = copy.deepcopy(graph)
        graph['nodes'] = [value['after'] if n['id'] == value['work_id'] else n for n in graph['nodes']]
        graph['sources'][value['source_key']] = value['source_record']
        graph['revision'] = compute_revision_hash(graph)
        applied = True
        return graph
    result = execute_graph_transaction(value['graph_path'], mutation)
    return {'status': 'PREPARED' if applied else 'ALREADY_PREPARED', 'work_id': value['work_id'],
        'revision': result['revision'], 'readiness': value['readiness'],
        'note': 'Start runs the existing Work flow; verification and final landing admission are still required.'}


def render(view):
    value = view['preparation']
    return '\n'.join(['Prepare '+value['work_id'], 'Project: '+value['root'],
        'Source: '+value['source_ref']+' @ '+value['source']['revision'],
        'Proposed criteria:', *['  '+c for c in value['before']['exit_criteria']],
        'Selected canonical criteria (replace proposed wording):', *['  '+c for c in value['after']['exit_criteria']],
        'Scope: '+json.dumps(value['after']['repository_scopes'], ensure_ascii=False),
        'Verification argv for each selected criterion: '+json.dumps(value['argv'], ensure_ascii=False),
        'Dependency readiness: '+json.dumps(value['readiness'], ensure_ascii=False),
        'Preparation SHA-256: '+view['sha256'],
        'Confirm preparation records these choices and admits the Work for execution. Tests have not run.'])


def main(argv=None):
    from . import relay_mesh
    parser = argparse.ArgumentParser(description='Bind a PLAN Work to pinned criteria and verification; preview before applying.')
    parser.add_argument('work_id')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--graph', type=Path, default=relay_mesh.WORK_GRAPH_FILE)
    parser.add_argument('--source', required=True)
    parser.add_argument('--criteria', required=True, help='Comma-separated canonical criterion IDs')
    parser.add_argument('--argv-json', required=True)
    parser.add_argument('--apply', metavar='PREVIEW_SHA256')
    parser.add_argument('--format', choices=['json', 'text'], default='text')
    args = parser.parse_args(argv)
    try:
        view = prepare(args.graph, args.work_id, args.root, args.source,
                       [c.strip() for c in args.criteria.split(',')], json.loads(args.argv_json))
        if args.apply:
            if args.apply != view['sha256']:
                raise ValueError('Preparation differs from the reviewed digest.')
            result = apply(view)
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(json.dumps(view, ensure_ascii=False) if args.format == 'json' else render(view))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)}))
        return 2

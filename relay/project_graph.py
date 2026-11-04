"""Explicit project graph references, saved defaults and Backlog observations."""
import hashlib
import json
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular, save_json
from tare_tools_agent_runtime import ConversationRejected


def graph_path(value):
    if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in '\0\r\n'):
        raise ValueError('Enter an absolute JSON graph path, or leave it blank.')
    if not value:
        return ''
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != '.json' or '..' in path.parts:
        raise ValueError('The project graph must be an absolute JSON path without parent traversal.')
    return str(regular(path))


def binding(value, repositories):
    if (not isinstance(value, dict) or set(value) != {'repository', 'path'}
            or not isinstance(value['repository'], str) or value['repository'] not in repositories):
        raise ValueError('The project graph must name a selected repository and an absolute JSON path.')
    path = graph_path(value['path'])
    if not path:
        raise ValueError('Omit the project graph configuration to start without a graph.')
    return {'repository': value['repository'], 'path': path}


def preference_path(directory, project, control_root):
    from .project_permissions import preference_path as scoped_path
    path, project, control = scoped_path(directory, project, control_root)
    return regular(path.parent.parent.parent/'project-graphs'/path.parent.name/'preferences.json'), project, control


def read_defaults(directory, project, control_root):
    from .native_agent import read_json
    path, project, control = preference_path(directory, project, control_root)
    if not path.exists():
        return '', 'missing'
    try:
        value = read_json(path, 16384)
        if (set(value) != {'schema', 'project', 'control_root', 'path'}
                or value['schema'] != 'tare.tools/project-graph/1'
                or value['project'] != project or value['control_root'] != control):
            raise ValueError('Scope mismatch')
        selected = graph_path(value['path'])
    except (ValueError, TypeError, KeyError, ConversationRejected) as exc:
        raise ValueError('Saved graph preferences are invalid; the file was preserved. Restore it before saving.') from exc
    return selected, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def observe(path):
    """Read a bounded snapshot; no global root rebinding, mutations or dispatch."""
    from tare_ops import read_graph
    from graph_backlog.core import WorkGraph
    from graph_backlog.validation import validate_work_graph
    from graph_backlog.algorithms import ranked_next, readiness
    path = graph_path(path)
    if not path:
        return {'path': '', 'summary': 'No project graph configured. Ordinary conversations need no graph.', 'authority': 'NONE'}
    if not Path(path).is_file():
        raise ValueError('The selected graph is not an available regular file: '+path)
    try:
        raw = read_graph(Path(path))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError('The selected graph contains invalid JSON: '+path) from exc
    if validate_work_graph(raw)['status'] != 'PASS':
        raise ValueError('Invalid Backlog graph. Run backlog validate for details.')
    graph = WorkGraph(raw)
    revision = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    from collections import Counter
    counts = dict(sorted(Counter(node.get('completion', {}).get('status', 'UNRECORDED')
                                 for node in raw['nodes']).items()))
    lines = ['Recorded completion · '+str(len(raw['nodes']))+' nodes',
             ' · '.join(str(count)+' '+status for status, count in counts.items()),
             'Graph records only; admission labels and documented implementation are separate evidence.',
             '', 'Suggested work · '+Path(path).name, 'Backlog ordering; execution not checked.', '']
    suggestions_start = len(lines)
    for row in ranked_next(graph, 'planning', 10):
        node = graph.by_id[row['id']]
        ready = readiness(graph, node, 'operational')
        lines.append(str(row['id'])+' · '+str(row['title'])[:400]+' · '+('Ready' if ready['ready'] else 'Blocked'))
        lines += ['  '+str(reason)[:400] for reason in ready['reasons'][:3]]
    if len(lines) == suggestions_start:
        lines.append('No work meets the planning profile.')
    lines += ['', 'Read-only snapshot · '+revision[:12]]
    deliveries = [n for n in raw['nodes'] if isinstance(n.get('metrics', {}).get('delivery_latest'), dict)]
    if deliveries:
        def excerpt(value):
            text = ' '.join(str(value).split())
            return text[:237]+'…' if len(text) > 240 else text
        extra = ['', 'Recorded delivery/progress · bounded excerpts']
        shown = 0
        for node in sorted(deliveries, key=lambda n: n.get('observed_at', ''), reverse=True)[:12]:
            latest = node['metrics']['delivery_latest']
            row = [str(node['id'])[:120]+' · '+node['completion']['status']+' · '+excerpt(latest.get('summary', ''))]
            remaining = latest.get('remaining', [])
            if isinstance(remaining, list):
                row += ['  remaining: '+excerpt(item) for item in remaining[:3]]
                if len(remaining) > 3:
                    row.append('  More obligations: inspect this item in the source graph.')
            if len('\n'.join(lines+extra+row).encode()) > 16000:
                break
            extra += row
            shown += 1
        extra.append(f'{shown}/{len(deliveries)} delivery records shown; full evidence and obligations remain in the graph.')
        lines += extra
    if len('\n'.join(lines).encode()) > 32768:
        raise ValueError('The graph summary exceeds the 32 KiB conversation context budget.')
    result = {'path': path, 'revision': revision, 'completion_counts': counts,
            'revision_basis': 'SHA-256 of parsed JSON serialized with sorted keys. This snapshot digest '
                'is distinct from the source file byte hash and the embedded graph revision. '
                'Differences between these identifiers do not imply different source versions.',
            'node_count': len(raw['nodes']), 'summary': '\n'.join(lines), 'authority': 'NONE'}
    inventory = []
    result.update(completion_inventory=inventory, omitted_inventory_nodes=len(raw['nodes']),
                  inventory_notice='Recorded completion for node IDs, not implementation or admission evidence. '
                  'Use this inventory for subset counts when omitted_inventory_nodes is zero; '
                  'do not reread the entire graph just to reconstruct these same facts. '
                  'For details, read selected JSON fields from the source. Counts describe this snapshot.')
    if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > 32768:
        raise ValueError('The graph snapshot exceeds the 32 KiB conversation context budget.')
    for node in raw['nodes']:
        inventory.append({'id': node['id'], 'status': node.get('completion', {}).get('status', 'UNRECORDED')})
        result['omitted_inventory_nodes'] -= 1
        if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > 32768:
            inventory.pop()
            result['omitted_inventory_nodes'] += 1
            break
    return result


def save_defaults(directory, project, control_root, selected, expected_revision):
    selected = graph_path(selected)
    observe(selected)  # Validate before any preferences are changed.
    path, project, control = preference_path(directory, project, control_root)
    if not Path(project).is_dir():
        raise ValueError('Project directory is unavailable.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with custody_lock(path.parent):
        _, revision = read_defaults(directory, project, control)
        if not expected_revision or revision != expected_revision:
            raise ValueError('Project graph defaults changed. Refresh before saving again.')
        save_json(path, {'schema': 'tare.tools/project-graph/1', 'project': project,
                         'control_root': control, 'path': selected})
        return read_defaults(directory, project, control)


def graph_view(directory, project, control_root, *, session=None, pending=''):
    selected, revision = read_defaults(directory, project, control_root)
    effective = pending
    if session:
        from .native_review_actions import review_custody
        with review_custody(session) as (_, state, config, records):
            roots = config['repositories']
            names = [name for name, root in roots.items() if Path(root).resolve() == Path(project).resolve()]
            if len(names) != 1:
                raise ValueError('The saved conversation belongs to another project.')
            saved = config.get('project_graph')
            if saved and binding(saved, roots)['repository'] != names[0]:
                raise ValueError('The saved graph belongs to another repository in this conversation.')
            effective = saved['path'] if saved else ''
    try:
        summary = observe(effective)['summary']
    except (ValueError, OSError) as exc:
        summary = 'The bound graph is unavailable: '+str(exc)+'. The recorded binding was preserved.'
    return {'revision': revision, 'path': selected, 'effective_path': effective, 'summary': summary}

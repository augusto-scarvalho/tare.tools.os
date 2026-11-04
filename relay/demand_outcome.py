"""Reviewed demand results over existing owner evidence and conversation custody."""
import copy
import hashlib
from pathlib import Path

from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import sha256_canonical
from graph_backlog.validation import validate_work_graph
from compute_plane.native_journal import regular
from compute_plane.native_models import object_json

from . import console_demand, console_recovery, demand_conduction, relay_mesh, work_run
from .native_agent import read_json


def parse_resolutions(text):
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, separator, reason = line.partition(':')
        key = key.strip()
        if not separator or not key.isdecimal() or int(key) < 1 or not reason.strip() or key in result:
            raise ValueError('Explain each addressed attempt as number: reason, once per attempt.')
        result[key] = reason.strip()
    return result


def inspect(value, *, resolutions=None):
    """Collect current evidence; terminal execution alone never closes a demand."""
    root = Path(value['root']).resolve()
    if (Path(value['control_root']).resolve() != Path(relay_mesh.RELAY_ROOT).resolve()
            or Path(value['snapshot']['selection']['root']).resolve() != root):
        raise ValueError('Demand project/control root changed.')
    resolutions = resolutions or {}
    if not isinstance(resolutions, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               or not v.strip() for k, v in resolutions.items()):
        raise ValueError('Addressed attempts require an explicit reason.')
    ids, applied, plans = demand_conduction.linkage(value)
    blockers, works, stages = [], [], []
    if console_recovery.pending(value):
        blockers.append('An operation remains unconfirmed. Reconcile its owner receipts.')
    raw = read_json(relay_mesh.WORK_GRAPH_FILE, 32*1024*1024) if ids else None
    if raw is not None and validate_work_graph(raw)['status'] != 'PASS':
        raise ValueError('Work graph is invalid.')
    wg = WorkGraph(raw) if raw is not None else None
    for key in ids:
        node = wg.by_id.get(key)
        if node is None:
            blockers.append('Linked Work is missing: '+key)
            works.append({'work_id': key, 'status': 'MISSING'})
            continue
        state = work_run.status(key)
        result = state.get('last_run')
        row = {'work_id': key, 'title': node['title'], 'status': wg.status_of(node),
               'criteria': node.get('exit_criteria', []), 'completion': node.get('completion'),
               'node_sha256': sha256_canonical(node), 'receipt': None}
        if not wg.is_satisfied(key) or wg.status_of(node) != 'DONE':
            blockers.append('Work is not completed: '+key)
        if state.get('receipt_errors') or state.get('human_wait'):
            blockers.append('Work evidence needs inspection: '+key)
        if (not result or result.get('status') != 'SETTLED' or not result.get('ended_at')
                or Path(result.get('repository_root', '')).resolve() != root
                or result.get('work_id') != key
                or result.get('train_id') != work_run.train_id_for(key, node)):
            blockers.append('No matching settled execution receipt: '+key)
        else:
            file = work_run.receipt_path(result['run_id'])
            with regular(file).open('rb') as stream:
                payload = stream.read(4*1024*1024+1)
            if len(payload) > 4*1024*1024 or object_json(payload) != result:
                raise ValueError('Work receipt changed during observation: '+key)
            row['receipt'] = {'path': str(file), 'sha256': hashlib.sha256(payload).hexdigest(),
                              'run_id': result['run_id'], 'candidate_commit': result.get('candidate_commit')}
        works.append(row)
    # The latest invocation of each owner supersedes that owner's earlier attempts.
    # Other owners remain visible, including failures; no blanket success inference.
    latest = {}
    for i, event in enumerate(value['events']):
        if event['kind'] != 'execution_requested':
            continue
        selected = event['selection']; mode = selected['mode']
        field = 'batch' if mode == 'parallel' else mode
        latest[(mode, selected.get(field))] = i
    success = {'session': {'PROPOSED'}, 'plan': {'REVIEWED'}, 'work': {'SETTLED'},
               'sequence': {'COMPLETED'}, 'research': {'COMPLETED'}, 'parallel': {'COMPLETED'},
               'synthesis': {'COMPLETED'}}
    addressed = set()
    for (mode, owner), index in latest.items():
        attempt = str(index+1)
        if demand_conduction.sequence_request_refused(value, index):
            stages.append({'attempt': attempt, 'mode': mode, 'owner': owner, 'state': 'REFUSED_BEFORE_CUSTODY',
                           'status': 'REFUSED', 'receipts': [], 'reason': 'Explicit rejection before sequence creation.'})
            continue
        observation = console_recovery.observe({**value, 'events': value['events'][:index+1]})
        result = observation.get('result') or {}
        stage = {'attempt': attempt, 'mode': mode, 'owner': owner, 'state': observation['state'],
                 'status': result.get('status', 'UNKNOWN'), 'receipts': observation.get('receipts', []),
                 'reason': observation['reason']}
        children = result.get('items') or []
        if not isinstance(children, list) or any(not isinstance(item, dict) for item in children):
            blockers.append('Malformed report items in attempt '+attempt)
            children = []
        reports = [result.get('report') or {}, *[item.get('report') or {} for item in children]]
        if mode == 'plan':
            # PLAN's top-level remaining is a standard preparation instruction.
            # Actual obligations belong to the proposal/reviewer, not that hint.
            reports.extend([(result.get('proposal') or {}).get('plan') or {},
                            (result.get('review') or {}).get('review') or {}])
        elif 'remaining' in result:
            reports.append({'remaining': result['remaining']})
        if any(not isinstance(report, dict) or not isinstance(report.get('remaining', []), list) for report in reports):
            blockers.append('Malformed report details in attempt '+attempt)
            reports = []
        stage['reported_summary'] = str(result.get('output') or '\n'.join(str(r.get('summary') or '') for r in reports).strip())
        stage['reported_remaining'] = [item for report in reports for item in report.get('remaining', [])]
        can_resolve = (observation['state'] == 'RECOVERED'
                       and (stage['status'] in {'REFUSED', 'FAILED', 'CANCELLED', 'REJECTED', 'REVIEW_REQUIRED', 'PARTIAL'}
                            or bool(stage['reported_remaining']) and stage['status'] in success.get(mode, set())))
        if mode == 'work':
            can_resolve = can_resolve and owner not in ids and wg is not None and owner in wg.by_id and wg.status_of(wg.by_id[owner]) == 'SUPERSEDED'
        stage['can_resolve'] = can_resolve
        if attempt in resolutions:
            if not can_resolve:
                raise ValueError('Attempt '+attempt+' needs owner reconciliation or has no failed result to address.')
            stage['resolution'] = resolutions[attempt].strip()
            addressed.add(attempt)
        stages.append(stage)
        if stage['reported_remaining'] and not stage.get('resolution'):
            blockers.append('Resolve the reported obligations in attempt '+attempt+'.')
        if not stage.get('resolution') and (observation['state'] != 'RECOVERED' or stage['status'] not in success.get(mode, set())):
            blockers.append('Inspect '+mode+' result: '+str(owner)+' ('+stage['status']+').')
    if set(resolutions) != addressed:
        raise ValueError('An addressed attempt changed or is no longer the latest attempt of that flow. Review again.')
    if plans and plans[-1] not in applied and not any(s['mode'] == 'plan' and s['owner'] == plans[-1]
                                                     and s.get('resolution') for s in stages):
        blockers.append('The latest PLAN has not been applied or explicitly resolved.')
    if not works and not any(s['mode'] in {'session', 'research', 'parallel', 'synthesis'} and s['state'] == 'RECOVERED'
                              and s['status'] in success[s['mode']] for s in stages):
        blockers.append('No delivered Work, session or knowledge result is linked to this demand.')
    request = next(((e.get('detail') or {}).get('operator') or (e.get('selection') or {}).get('prompt')
                    for e in value['events'] if (e.get('detail') or {}).get('operator')
                    or (e.get('selection') or {}).get('prompt')), '')
    return {'demand_id': value['id'], 'root': str(root), 'request': request,
            'works': works, 'stages': stages, 'blockers': list(dict.fromkeys(blockers)),
            'graph_sha256': sha256_canonical(raw) if raw is not None else None,
            'authority': 'NONE'}


def preview(value, summary, remaining='', *, disposition='FULFILLED', resolutions=None):
    """Bind a human-reviewed answer and its limitations to currently observed evidence."""
    if disposition not in {'FULFILLED', 'PARTIAL'}:
        raise ValueError('Choose FULFILLED or PARTIAL.')
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('Explain what was delivered against the original request.')
    if not isinstance(remaining, str):
        raise ValueError('Remaining obligations must be text.')
    if disposition == 'PARTIAL' and not remaining.strip():
        raise ValueError('Describe the obligations that remain.')
    evidence = inspect(value, resolutions=resolutions)
    if disposition == 'FULFILLED' and (evidence['blockers'] or remaining.strip()):
        raise ValueError('Resolve the remaining obligations or record a partial result: '+
                         '; '.join(evidence['blockers']))
    payload = {'schema': 'tare.tools/demand-outcome/1', 'demand_id': value['id'],
               'demand_revision': value['revision'], 'demand_sha256': sha256_canonical(value),
               'disposition': disposition, 'summary': summary.strip(), 'remaining': remaining.strip(),
               'resolutions': resolutions or {},
               'evidence': evidence}
    return {**payload, 'sha256': sha256_canonical(payload)}


def confirm(identity, root, control_root, view):
    """Append the reviewed observation atomically; never change Work or flow state."""
    value = console_demand.read(identity, root, control_root)
    current = preview(value, view['summary'], view['remaining'], disposition=view['disposition'], resolutions=view.get('resolutions'))
    if current != view:
        raise ValueError('Demand or evidence changed. Review the result again.')
    # save checks the conversation revision under its writer lock. Evidence is an
    # observation at confirmation, not permanent proof about mutable external state.
    return console_demand.save(identity, value['revision'], root, control_root,
                               'demand_result_confirmed', copy.deepcopy(value['snapshot']), current)


def latest(value):
    return next((e['detail'] for e in reversed(value['events'])
                 if e['kind'] == 'demand_result_confirmed'), None)


def render(view):
    evidence = view.get('evidence', view)
    lines = ['Demand result', '', 'Original request: '+(evidence['request'] or '(not recorded)')]
    if 'summary' in view:
        lines += ['', view['disposition'], view['summary'], 'Remaining: '+(view['remaining'] or 'None declared.')]
    for row in evidence['works']:
        lines += ['', row['work_id']+' · '+row['status'], *['  '+c for c in row.get('criteria', [])]]
        if row.get('receipt'):
            lines.append('  Receipt: '+row['receipt']['path'])
    for stage in evidence['stages']:
        lines.append('Attempt '+stage['attempt']+' · '+stage['mode']+' · '+stage['status']+' · '+str(stage['owner']))
        if stage.get('reported_summary'):
            lines.append(stage['reported_summary'])
        if stage.get('reported_remaining'):
            lines += ['Reported limitations:', *['  '+str(r) for r in stage['reported_remaining']]]
        if stage.get('resolution'):
            lines.append('Addressed: '+stage['resolution'])
        elif stage.get('can_resolve'):
            lines.append('Explain how this earlier attempt was addressed before marking the request fulfilled.')
    if evidence['blockers']:
        lines += ['', 'Requires attention:', *['  '+b for b in evidence['blockers']]]
    return '\n'.join(lines)

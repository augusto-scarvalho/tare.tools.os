"""Reviewed research snapshots for PLAN; reports remain attributed claims."""
import argparse
import hashlib
import json
from pathlib import Path

from compute_plane.native_journal import custody_lock, io_path, regular, save_json
from graph_backlog.jsonutil import sha256_canonical
from . import native_artifacts, parallel_explore, research_flow
from .native_agent import read_json


def promotion_template():
    """The packaged intake guidance; reading it never starts a model or admits Work."""
    # relay is a namespace package; editable installs may add virtual finder paths
    # that importlib.resources cannot open. Read beside this executing module.
    text = (Path(__file__).parent/'templates/research_promotion.md').read_text(encoding='utf-8')
    if not text.strip() or len(text.encode('utf-8')) > 8192:
        raise ValueError('Promotion template must be nonempty and at most 8 KiB.')
    return text


def install_promotion_template(target):
    """Freeze one small template beside a new PLAN's existing findings snapshot."""
    raw = promotion_template().encode('utf-8')
    regular(io_path(Path(target)/'intake.md')).write_bytes(raw)
    return {'path': 'findings/intake.md', 'sha256': hashlib.sha256(raw).hexdigest()}


def text_source(path, digest):
    with regular(io_path(path)).open('rb') as stream:
        raw = stream.read(2*1024*1024+1)
    if len(raw) > 2*1024*1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Finding source changed or exceeds the native source limit.')
    return raw.decode('utf-8')


def session_preview(directory):
    """A session's checked plan (its ledger, work.json) as one finding for /plan: units, paths, criteria and checks."""
    from .work_forms import LEDGER, LEDGER_BYTES
    directory = Path(directory).resolve()
    ledger = read_json(directory/LEDGER, LEDGER_BYTES)
    plan = ledger.get('plan') or {}
    if ledger.get('problems') or not plan.get('units'):
        raise ValueError('The session has no checked plan.')
    config = read_json(directory/'native-agent.json', 4*1024*1024)['config']
    text = json.dumps(plan, indent=1, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    report = {'id': 'session-plan', 'kind': 'session-plan', 'question': 'The plan session '+directory.name+' wrote',
              'session': str(directory), 'plan': plan, 'web_citations': [],
              'sources': [{'repository': 'session', 'path': LEDGER, 'sha256': digest, 'snapshot': 'sources/'+digest+'.txt'}]}
    value = {'schema': 'tare.tools/planning-findings-preview/1',
             'root': str(Path(next(iter(config['repositories'].values()))).resolve()),
             'origin': {'kind': 'session', 'directory': str(directory)}, 'reports': [report], 'omitted': [],
             'documents': {digest: text}, 'authority': 'NONE'}
    return {**value, 'sha256': sha256_canonical(value)}


def preview(kind, directory, items=None, *, max_bytes=None):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError('Finding snapshot budget must be a positive byte count.')
    if kind == 'session':
        return session_preview(directory)
    directory = Path(directory).resolve()
    if kind == 'research':
        view = research_flow.inspect(directory)
        rows = [{'id': 'report', 'question': view['question'], 'report': view.get('report'),
                 'session': view['session'], 'status': view['status']}]
        roots = {'sources': directory/'sources'}
    elif kind == 'parallel':
        view = parallel_explore.inspect(directory)
        rows = view['items']; roots = {'project': Path(view['root'])}
    else:
        raise ValueError('Select research, parallel exploration or a session.')
    if view['status'] == 'NEEDS_REVIEW':
        raise ValueError('Sources changed; review the original flow first.')
    ids = items if items is not None else [r['id'] for r in rows if r.get('report')]
    if not ids or len(ids) != len(set(ids)) or set(ids)-{r['id'] for r in rows}:
        raise ValueError('Select distinct completed report IDs.')
    reports, documents, document_bytes = [], {}, 0
    for row in rows:
        if row['id'] not in ids:
            continue
        if not row.get('report'):
            raise ValueError('Selected report is incomplete.')
        source_artifact = native_artifacts.read_input(Path(row['session'])/'artifact.json')
        artifact = source_artifact['artifact']
        if artifact['role'] != 'explorer':
            raise ValueError('An explorer report is required.')
        citations = []
        for source in artifact['payload']['sources']:
            root = roots[source['repository']].resolve()
            path = (root/source['path']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('Finding source escapes its owner.')
            new_document = source['sha256'] not in documents
            if new_document:
                if max_bytes is not None and path.stat().st_size > max_bytes - document_bytes:
                    raise ValueError('Aggregate source storage exceeds synthesis limit.')
            content = text_source(path, source['sha256'])
            if new_document:
                document_bytes += len(content.encode('utf-8'))
                if max_bytes is not None and document_bytes > max_bytes:
                    raise ValueError('Aggregate source storage exceeds synthesis limit.')
                documents[source['sha256']] = content
            citations.append({**source, 'snapshot': 'sources/'+source['sha256']+'.txt'})
        if not citations:
            raise ValueError('A finding must cite observed sources.')
        reports.append({'id': row['id'], 'question': row['question'],
            'session': row['session'], 'status': row['status'],
            'coverage': view.get('sources', []),
            'artifact_sha256': source_artifact['sha256'], 'artifact': artifact,
            'sources': citations, 'web_citations': row['report'].get('citations', [])})
    value = {'schema': 'tare.tools/planning-findings-preview/1', 'root': str(Path(view['root']).resolve()),
        'origin': {'kind': kind, 'directory': str(directory)}, 'reports': reports,
        'omitted': [{'id': r['id'], 'question': r['question'], 'status': r['status']} for r in rows if r['id'] not in ids],
        'documents': documents, 'authority': 'NONE'}
    return {**value, 'sha256': sha256_canonical(value)}


def review_packet(view, note='', *, purpose='planning'):
    statuses = {'planning': 'OPERATOR_SELECTED_FOR_PLANNING', 'synthesis': 'OPERATOR_SELECTED_FOR_SYNTHESIS'}
    if purpose not in statuses:
        raise ValueError('Select planning or synthesis as the findings destination.')
    packet = {k: v for k, v in view.items() if k not in {'sha256', 'documents'}}
    packet.update(schema='tare.tools/planning-findings/1',
        review={'status': statuses[purpose], 'note': note,
                'meaning': 'Reviewed input selection; not independent approval or proof of current correctness.'})
    return packet


def save(view, expected_digest, note=''):
    fresh = preview(view['origin']['kind'], view['origin']['directory'], [r['id'] for r in view['reports']])
    if fresh['sha256'] != expected_digest or view != fresh:
        raise ValueError('Findings changed since preview; review again.')
    from .native_installation import storage_root
    packet = review_packet(fresh, note)
    digest = sha256_canonical(packet)
    directory = storage_root()/'planning-findings'/digest
    directory.mkdir(parents=True, exist_ok=True)
    with custody_lock(directory):
        if not (directory/'manifest.json').exists():
            (directory/'sources').mkdir(exist_ok=True)
            for key, content in fresh['documents'].items():
                regular(io_path(directory/'sources'/(key+'.txt'))).write_bytes(content.encode('utf-8'))
            save_json(directory/'manifest.json', packet)
        load(directory/'manifest.json', digest, fresh['root'])
    return {'path': str(directory/'manifest.json'), 'sha256': digest, 'root': fresh['root']}


def load(path, digest, root):
    path = Path(path).resolve(); packet = read_json(path, 4*1024*1024)
    if (packet.get('schema') != 'tare.tools/planning-findings/1' or packet.get('authority') != 'NONE'
            or sha256_canonical(packet) != digest or packet['root'] != str(Path(root).resolve())
            or packet['review']['status'] != 'OPERATOR_SELECTED_FOR_PLANNING'):
        raise ValueError('Reviewed findings identity changed or belongs to another project.')
    for report in packet['reports']:
        if report.get('kind') == 'session-plan':
            from .work_forms import MAX_UNITS
            units = report['plan'].get('units')
            if not isinstance(units, list) or not 1 <= len(units) <= MAX_UNITS or any(
                    not isinstance(u, dict) or not {'key', 'title', 'target_paths', 'criteria'} <= set(u) for u in units):
                raise ValueError('Invalid session plan.')
        else:
            native_artifacts.validate('explorer', report['artifact']['payload'])
        for source in report['sources']:
            expected = 'sources/'+source['sha256']+'.txt'
            if source['snapshot'] != expected:
                raise ValueError('Invalid finding snapshot path.')
            text_source(path.parent/expected, source['sha256'])
    return packet


def install(path, digest, root, target):
    packet = load(path, digest, root)
    target = Path(target); (target/'sources').mkdir(parents=True)
    for report in packet['reports']:
        for source in report['sources']:
            content = text_source(Path(path).parent/source['snapshot'], source['sha256'])
            regular(io_path(target/source['snapshot'])).write_bytes(content.encode('utf-8'))
    save_json(target/'manifest.json', packet)
    load(target/'manifest.json', digest, root)
    return {'path': 'findings/manifest.json', 'sha256': digest}


def render(view, *, purpose='planning'):
    lines = ['Research findings for '+purpose+' — attributed claims, not approval.']
    for report in view['reports']:
        if report.get('kind') == 'session-plan':
            lines += ['', report['id']+' · '+report['question'], 'Author session: '+report['session']]
            for unit in report['plan']['units']:
                checks = [*unit.get('verifiers', []), *unit.get('test_paths', [])]
                lines += [f"- {unit['key']}: {unit['title']} [{', '.join(unit['target_paths'])}]"
                          + (f" checked by {', '.join(checks)}" if checks else '')]
            lines += ['Remaining: '+json.dumps(report['plan'].get('remaining', []), ensure_ascii=False)]
            continue
        artifact = report['artifact']; payload = artifact['payload']
        lines += ['', report['id']+' · '+report['question'], payload['summary'],
                  'Author session: '+artifact['session_id'], 'Profile: '+artifact['profile_digest'],
                  'Remaining: '+json.dumps(payload['remaining'], ensure_ascii=False)]
        lines += [s['repository']+':'+s['path']+' · '+s['sha256'] for s in report['sources']]
        lines += ['URL: '+str(s.get('url')) for s in report['web_citations']]
    lines += ['', 'Omitted reports: '+json.dumps(view['omitted'], ensure_ascii=False)]
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', required=True, choices=['research', 'parallel', 'session'])
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--item', action='append')
    parser.add_argument('--save', metavar='PREVIEW_SHA256')
    parser.add_argument('--note', default='')
    parser.add_argument('--format', choices=['json', 'text'], default='json')
    args = parser.parse_args(argv)
    try:
        view = preview(args.kind, args.source, args.item)
        if args.save:
            result = save(view, args.save, args.note)
            if args.format == 'text':
                print('path: ' + result['path'])
                print('sha256: ' + result['sha256'])
            else:
                print(json.dumps(result, ensure_ascii=False))
        else:
            if args.format == 'text':
                print(render(view))
                print(view['sha256'])
            else:
                print(json.dumps({**view, 'documents': 'Snapshot text omitted from display.'}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)})); return 2

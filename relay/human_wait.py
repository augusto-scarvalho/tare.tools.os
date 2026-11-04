"""Optional durable decision at the existing plan-to-implementation boundary."""
import argparse
import json
from pathlib import Path

from graph_backlog.jsonutil import sha256_canonical
from .native_agent import read_json

FILE = 'HUMAN_WAIT.json'


def request_for(origin):
    return {'schema': 'tare.tools/human-wait/1', 'work_id': origin['work_id'],
        'descriptor_sha256': origin['descriptor_sha256'],
        'question': origin['wait_before_implement'],
        'criteria': origin['node']['exit_criteria'],
        'repository_scopes': origin['node']['repository_scopes'],
        'repository_root': origin['repository_root'],
        'work_sha256': sha256_canonical(origin['node'])}


def _origin_request(request):
    return {k: v for k, v in request.items() if k != 'request_round'}


def _save(path, value):
    from compute_plane.native_journal import save_json
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 1024*1024:
        raise ValueError('HUMAN_WAIT_RECORD_TOO_LARGE')
    save_json(path, value)


def inspect(train):
    path = Path(train)/FILE
    if not path.exists():
        return None
    value = read_json(path, 1024*1024)
    if value.get('request_sha256') != sha256_canonical(value['request']):
        raise ValueError('HUMAN_WAIT_REQUEST_CHANGED')
    history = value.get('prior_decisions', [])
    round_number = value['request'].get('request_round', 0)
    if (type(round_number) is not int or round_number < 0 or not isinstance(history, list)
            or len(history) != round_number):
        raise ValueError('HUMAN_WAIT_HISTORY_INVALID')
    for index, prior in enumerate(history):
        request = dict(value['request'])
        if index == 0:
            request.pop('request_round', None)
        else:
            request['request_round'] = index
        if (not isinstance(prior, dict) or prior.get('decision') != 'decline'
                or prior.get('request_sha256') != sha256_canonical(request)):
            raise ValueError('HUMAN_WAIT_HISTORY_INVALID')
    decision = value.get('answer')
    if decision is not None and (decision.get('decision') not in {'approve', 'decline'}
            or decision.get('request_sha256') != value['request_sha256']):
        raise ValueError('HUMAN_WAIT_ANSWER_INVALID')
    return {**value, 'status': 'WAITING_FOR_HUMAN' if decision is None else
            'HUMAN_APPROVED' if decision['decision'] == 'approve' else 'HUMAN_DECLINED'}


def checkpoint(train, origin, receipt, stage):
    """Called under the Work lock, before acquiring a model or workspace."""
    if not origin.get('wait_before_implement'):
        return True
    from compute_plane.native_journal import save_json
    from .work_run import now
    request = request_for(origin)
    if not (Path(train)/FILE).exists():
        _save(Path(train)/FILE, {'request': request, 'request_sha256': sha256_canonical(request),
                                    'requested_at': now(), 'answer': None})
    value = inspect(train)
    if _origin_request(value['request']) != request:
        raise ValueError('HUMAN_WAIT_ORIGIN_CHANGED')
    receipt['human_wait'] = value
    if value['status'] == 'HUMAN_APPROVED':
        return True
    receipt['status'] = value['status']
    stage('human_wait', request_sha256=value['request_sha256'], status=value['status'])
    return False


def answer(work_id, expected_digest, decision, note='', *, graph_path=None):
    from compute_plane.native_journal import save_json
    from . import relay_mesh, work_run
    from .work_change import work_locks
    from .train_execution import read_execution_descriptor
    if decision not in {'approve', 'decline', 'reconsider'}:
        raise ValueError('Select approve, decline or reconsider.')
    with work_locks([work_id], graph_path=graph_path):
        train = work_run.train_dir_for(work_id, graph_path=graph_path)
        value = inspect(train)
        if value is None:
            raise ValueError('HUMAN_WAIT_CHANGED: inspect the pending decision again.')
        replay = (decision == 'reconsider' and value.get('last_reconsider') ==
                  {'expected': expected_digest, 'note': note} and value['answer'] is None)
        if value['request_sha256'] != expected_digest and not replay:
            raise ValueError('HUMAN_WAIT_CHANGED: inspect the pending decision again.')
        origin = read_json(train/'WORK_RUN.json', 1024*1024)
        if _origin_request(value['request']) != request_for(origin):
            raise ValueError('HUMAN_WAIT_ORIGIN_CHANGED')
        if value['answer'] is not None and decision != 'reconsider':
            if (value['answer']['decision'], value['answer']['note']) != (decision, note):
                raise ValueError('HUMAN_WAIT_ALREADY_ANSWERED')
            return value  # Lost acknowledgement: never execute as a side effect of retrying an answer.
        descriptor = read_execution_descriptor(train/'EXECUTION_DESCRIPTOR.json')
        state = read_json(train/'STATE.json')
        if (descriptor.train_id != work_run.train_id_for(work_id, graph_path=graph_path) or descriptor.work_id != work_id
                or descriptor.sha256 != origin['descriptor_sha256'] or state['status'] != 'PLAN_APPROVED'):
            raise ValueError('HUMAN_WAIT_EXECUTION_CHANGED')
        if work_run.load_node(relay_mesh.RELAY_ROOT / descriptor.work_graph, work_id) != origin['node']:
            raise ValueError('HUMAN_WAIT_WORK_CHANGED')
        if work_run.git(Path(origin['repository_root']), 'rev-parse', descriptor.target_ref) != descriptor.base_commit:
            raise ValueError('HUMAN_WAIT_TARGET_MOVED')
        if replay:
            return value
        value.pop('status')
        if decision == 'reconsider':
            if value['answer'] is None or value['answer']['decision'] != 'decline':
                raise ValueError('HUMAN_WAIT_RECONSIDER_REQUIRES_DECLINED')
            value['prior_decisions'] = [*value.get('prior_decisions', []), value['answer']]
            value['request']['request_round'] = value['request'].get('request_round', 0) + 1
            value['request_sha256'] = sha256_canonical(value['request'])
            value['answer'] = None
            value['last_reconsider'] = {'expected': expected_digest, 'note': note}
            _save(train/FILE, value)
            return inspect(train)
        value['answer'] = {'decision': decision, 'note': note, 'answered_at': work_run.now(),
                           'request_sha256': expected_digest}
        _save(train/FILE, value)
        return inspect(train)


def render(value):
    if value is None:
        return 'No human decision recorded for this execution.'
    request = value['request']
    return '\n'.join([value['status'], request['question'], 'Work: '+request['work_id'],
        'Project: '+request['repository_root'], 'Criteria:', *request['criteria'],
        'Scope: '+json.dumps(request['repository_scopes'], ensure_ascii=False),
        'Request SHA-256: '+value['request_sha256'],
        'Answer: '+json.dumps(value['answer'], ensure_ascii=False),
        'Approval permits Resume; it does not attest implementation, tests or landing.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect or answer a saved Work decision; never starts execution.')
    parser.add_argument('work_id'); parser.add_argument('--decision', choices=['approve', 'decline', 'reconsider'])
    parser.add_argument('--expected'); parser.add_argument('--note', default='')
    parser.add_argument('--format', choices=['json', 'text'], default='text')
    args = parser.parse_args(argv)
    try:
        from .work_run import train_dir_for
        value = answer(args.work_id, args.expected, args.decision, args.note) if args.decision else inspect(train_dir_for(args.work_id))
        print(json.dumps(value, ensure_ascii=False) if args.format == 'json' else render(value))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)})); return 2

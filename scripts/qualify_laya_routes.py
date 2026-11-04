"""Run a frozen, CPU-only route-choice screen. Never changes routing policy."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import time


def grade(protocol, results):
    """A stable abstainer still has to pass every evidence-response control."""
    if protocol.get('schema') == 'tare.tools/laya-strategy-screen/1':
        return grade_strategies(protocol, results)
    if protocol.get('schema') == 'tare.tools/laya-fewshot-screen/1':
        return grade_factorial(protocol, results)
    cases = {c['id']: c for c in protocol['cases']}
    valid = len(cases) == protocol['case_count'] == protocol['max_calls']
    seen = set()
    scores = defaultdict(lambda: {'passed': 0, 'total': 0})
    groups = defaultdict(list)
    positions = Counter()
    for result in results:
        case = cases.get(result['id'])
        if case is None or result['id'] in seen:
            valid = False
            continue
        seen.add(result['id'])
        observed = result.get('status') == 'OBSERVED'
        choice = result.get('answer', {}).get('choice')
        resolved = ('insufficient' if choice == 'insufficient'
                    else case['choice_identity'].get(choice))
        valid = valid and observed and resolved is not None
        if observed:
            positions[choice] += 1
        if case['category'] == 'real-order-control':
            groups[case['group']].append(resolved)
        else:
            score = scores[case['category']]
            score['total'] += 1
            score['passed'] += int(observed and resolved == case['expected_identity'])
    expected_groups = {c['group'] for c in cases.values() if c['category'] == 'real-order-control'}
    stable = {g: len(groups[g]) == 6 and None not in groups[g] and len(set(groups[g])) == 1
              for g in expected_groups}
    gates = {'complete_valid_untruncated_responses': valid and seen == set(cases),
             'real_order_invariance': len(stable) == 2 and all(stable.values())}
    for category in ('quality', 'cost', 'effort', 'abstention'):
        gates[category] = scores[category] == {'passed': 4, 'total': 4}
    return {'status': 'SCREEN_PASSED' if all(gates.values()) else 'NOT_QUALIFIED',
            'gates': gates, 'controls': dict(scores), 'stable_groups': stable,
            'group_choices': dict(groups), 'choice_positions': dict(positions),
            'automatic_routing_authorized': False}


def grade_factorial(protocol, results):
    """Score four paired conditions without pooling away position failures."""
    cases = {c['id']: c for c in protocol['cases']}
    complete = len(cases) == protocol['case_count'] == protocol['max_calls'] == 32
    seen = set()
    conditions = {}
    for name in ('generic_zero', 'descriptive_zero', 'generic_few', 'descriptive_few'):
        conditions[name] = {'correct': 0, 'total': 0, 'categories': {},
                            'pairs': defaultdict(list), 'positions': Counter()}
    for row in results:
        case = cases.get(row['id'])
        if case is None or row['id'] in seen:
            complete = False
            continue
        seen.add(row['id'])
        answer = row.get('answer', {}).get('choice')
        identity = 'insufficient' if answer == 'insufficient' else case['choice_identity'].get(answer)
        valid = row.get('status') == 'OBSERVED' and identity is not None
        complete = complete and valid
        score = conditions[case['condition']]
        correct = valid and identity == case['expected_identity']
        score['correct'] += int(correct); score['total'] += 1
        category = score['categories'].setdefault(case['category'], {'correct': 0, 'total': 0})
        category['correct'] += int(correct); category['total'] += 1
        score['pairs'][case['group']].append(identity if valid else None)
        score['positions'][answer or 'FAILED'] += 1
    complete = complete and seen == set(cases)
    for score in conditions.values():
        pairs = score.pop('pairs')
        score['stable_pairs'] = sum(len(v) == 2 and None not in v and len(set(v)) == 1
                                    for v in pairs.values())
        score['pairs_total'] = len(pairs)
        score['status'] = 'SCREEN_PASSED' if (complete and score['correct'] == score['total'] == 8
            and score['stable_pairs'] == score['pairs_total'] == 4) else 'NOT_QUALIFIED'
        score['positions'] = dict(score['positions'])
    return {'status': 'SCREEN_PASSED' if any(s['status'] == 'SCREEN_PASSED'
             for s in conditions.values()) else 'NOT_QUALIFIED',
            'complete': complete, 'conditions': conditions,
            'automatic_routing_authorized': False}


def grade_strategies(protocol, results):
    """A small joint-cost diagnostic, never vendor or production qualification."""
    cases = {c['id']: c for c in protocol['cases']}
    complete = len(cases) == protocol['case_count'] == protocol['max_calls'] == 6
    seen, groups, correct = set(), defaultdict(list), 0
    for row in results:
        case = cases.get(row['id'])
        if case is None or row['id'] in seen:
            complete = False
            continue
        seen.add(row['id'])
        identity = case['choice_identity'].get(row.get('answer', {}).get('choice'))
        valid = row.get('status') == 'OBSERVED' and identity is not None
        complete = complete and valid
        correct += int(valid and identity == case['expected_identity'])
        groups[case['group']].append(identity if valid else None)
    complete = complete and seen == set(cases)
    stable = {key: len(rows) == 2 and None not in rows and len(set(rows)) == 1
              for key, rows in groups.items()}
    passed = complete and correct == 6 and len(stable) == 3 and all(stable.values())
    return {'status': 'SCREEN_PASSED' if passed else 'NOT_QUALIFIED', 'complete': complete,
            'correct': correct, 'total': len(cases), 'stable_groups': stable,
            'group_choices': dict(groups), 'automatic_routing_authorized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Qualification output already exists; never overwrite a run.')
    raw = args.protocol.read_bytes()
    protocol = json.loads(raw)
    if not 1 <= len(protocol['cases']) == protocol['max_calls'] <= 32:
        raise ValueError('Qualification call budget invalid.')
    # Optional assessment dependencies are imported only by an explicit run.
    from compute_plane.laya_choice_worker import LayaChoiceModel, check_envelope
    from compute_plane.choice_assessment import request_digest
    started = time.monotonic()
    model = LayaChoiceModel(args.manifest)
    import torch
    torch.manual_seed(0)
    report = {'schema': 'tare.tools/laya-routing-qualification-result/1',
              'protocol_sha256': hashlib.sha256(raw).hexdigest(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'model_identity': model.identity, 'load_seconds': time.monotonic()-started,
              'mode': 'resident-cpu-evaluation-not-production-worker-latency',
              'status': 'PREFLIGHT', 'envelopes': [], 'results': []}
    def save():
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(args.output)
    save()
    try:
        for case in protocol['cases']:
            request = case['request']
            check_envelope(model.agent, request)
            def count(text):
                return len(model.agent.tok(text, add_special_tokens=False)['input_ids'])
            head = count('choice question: '+request['question']) + sum(
                count(' '+k+': '+v)+1 for k, v in request['options'].items())
            report['envelopes'].append({'id': case['id'], 'tokens': head+count(request['state'])+4})
    except ValueError as exc:
        report.update(status='NOT_QUALIFIED', error=str(exc)); save()
        return 2
    report['status'] = 'RUNNING'; save()
    for case in protocol['cases']:
        before = time.monotonic()
        row = {'id': case['id'], 'request_sha256': request_digest(case['request'])}
        try:
            row.update(status='OBSERVED', answer=model.assess(case['request']))
        except (ValueError, RuntimeError) as exc:
            row.update(status='FAILED', error=str(exc))
        row['seconds'] = time.monotonic()-before
        report['results'].append(row); save()
        print(json.dumps({'completed': len(report['results']), 'of': protocol['max_calls'],
                          'id': row['id'], 'choice': row.get('answer', {}).get('choice'),
                          'seconds': round(row['seconds'], 3)}), flush=True)
    report['analysis'] = grade(protocol, report['results'])
    report['status'] = report['analysis']['status']
    report['total_seconds'] = time.monotonic()-started
    save()
    print(json.dumps(report['analysis']), flush=True)
    return 0 if report['status'] == 'SCREEN_PASSED' else 2


if __name__ == '__main__':
    raise SystemExit(main())

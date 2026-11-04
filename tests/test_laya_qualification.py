import copy
import json
from pathlib import Path

from scripts.qualify_laya_routes import grade


def corpus():
    root = Path(__file__).parent/'fixtures'/'laya'
    return json.loads((root/'LAYA_QUALIFICATION_PROTOCOL_2026-09-21.json').read_bytes())


def perfect(protocol):
    results = []
    for case in protocol['cases']:
        wanted = case['expected_identity'] or 'insufficient'
        choice = 'insufficient' if wanted == 'insufficient' else next(
            k for k, v in case['choice_identity'].items() if v == wanted)
        results.append({'id': case['id'], 'status': 'OBSERVED', 'answer': {'choice': choice}})
    return results


def test_complete_oracle_screen_pass_does_not_authorize_routing():
    protocol = corpus()
    result = grade(protocol, perfect(protocol))
    assert result['status'] == 'SCREEN_PASSED'
    assert not result['automatic_routing_authorized']


def test_missing_duplicate_or_failed_response_cannot_pass():
    protocol = corpus()
    rows = perfect(protocol)
    failed = copy.deepcopy(rows); failed[0]['status'] = 'FAILED'
    for results in (rows[:-1], rows+[rows[0]], failed):
        assert grade(protocol, results)['status'] == 'NOT_QUALIFIED'


def test_stable_abstention_does_not_masquerade_as_useful_recommendations():
    protocol = corpus()
    rows = perfect(protocol)
    for row in rows: row['answer']['choice'] = 'insufficient'
    result = grade(protocol, rows)
    assert result['gates']['real_order_invariance']
    assert result['gates']['abstention']
    assert not result['gates']['quality']
    assert result['status'] == 'NOT_QUALIFIED'


def test_fixed_position_cannot_pass_permuted_real_candidates():
    protocol = corpus()
    rows = perfect(protocol)
    for row in rows: row['answer']['choice'] = 'candidate_2'
    result = grade(protocol, rows)
    assert not result['gates']['real_order_invariance']
    assert result['status'] == 'NOT_QUALIFIED'


def factorial_corpus():
    return json.loads((Path(__file__).parent/'fixtures'/'laya'/
                       'LAYA_FEWSHOT_PROTOCOL_2026-09-21.json').read_bytes())


def test_factorial_preserves_controls_and_balances_answer_positions():
    protocol = factorial_corpus()
    cases = {c['id']: c for c in protocol['cases']}
    for category in ('quality', 'cost', 'effort', 'abstention'):
        for order in (0, 1):
            generic = cases[f'generic_zero-{category}-{order}']['request']
            descriptive = cases[f'descriptive_zero-{category}-{order}']['request']
            assert generic['state'] == descriptive['state']
            assert generic['question'] == descriptive['question']
            assert generic['options'] != descriptive['options']
            for condition in ('generic', 'descriptive'):
                zero = cases[f'{condition}_zero-{category}-{order}']['request']
                few = cases[f'{condition}_few-{category}-{order}']['request']
                assert few['state'] == protocol['demonstrations'] + zero['state']
                assert few['question'] == zero['question'] and few['options'] == zero['options']
    result = grade(protocol, perfect(protocol))
    assert result['status'] == 'SCREEN_PASSED'
    assert not result['automatic_routing_authorized']
    assert all(s['positions'] == {'candidate_1': 3, 'candidate_2': 3, 'insufficient': 2}
               for s in result['conditions'].values())


def test_factorial_reports_each_arm_and_rejects_partial_runs():
    protocol = factorial_corpus()
    rows = perfect(protocol)
    cases = {c['id']: c for c in protocol['cases']}
    for row in rows:
        if cases[row['id']]['condition'] == 'generic_zero':
            row['answer']['choice'] = 'candidate_2'
    result = grade(protocol, rows)
    assert result['conditions']['generic_zero']['correct'] == 3
    assert result['conditions']['generic_zero']['status'] == 'NOT_QUALIFIED'
    assert result['conditions']['descriptive_few']['correct'] == 8
    assert grade(protocol, rows[:-1])['status'] == 'NOT_QUALIFIED'
    assert grade(protocol, rows+[rows[0]])['status'] == 'NOT_QUALIFIED'


def test_joint_screen_rejects_position_bias_partial_and_abstaining_results():
    protocol = json.loads((Path(__file__).parent/'fixtures'/'laya'/
                           'LAYA_STRATEGY_PROTOCOL_2026-09-21.json').read_bytes())
    rows = perfect(protocol)
    assert grade(protocol, rows)['status'] == 'SCREEN_PASSED'
    assert not grade(protocol, rows)['automatic_routing_authorized']
    assert grade(protocol, rows[:-1])['status'] == 'NOT_QUALIFIED'
    assert grade(protocol, rows+[rows[0]])['status'] == 'NOT_QUALIFIED'
    for constant in ('strategy_1', 'strategy_2', 'insufficient'):
        fixed = copy.deepcopy(rows)
        for row in fixed: row['answer']['choice'] = constant
        assert grade(protocol, fixed)['status'] == 'NOT_QUALIFIED'

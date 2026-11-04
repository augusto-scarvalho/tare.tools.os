import json
from pathlib import Path

import pytest
from compute_plane.choice_assessment import digest
from relay import delivery_comparison as comparison
from relay import native_catalog, native_review, native_profiles


@pytest.fixture
def study(tmp_path, monkeypatch):
    profiles = {name: {'id': name, 'model': name, 'native_profile': {'model': name}}
                for name in ('cheap', 'reliable', 'reviewer')}
    monkeypatch.setattr(native_catalog, 'select', lambda name: profiles[name])
    monkeypatch.setattr(native_review, 'review', lambda *a, **kw: {'unresolved_effects': [], 'checks': [
        {'name': 'check', 'status': 'succeeded', 'returncode': 0, 'freshness': 'OBSERVED_FILES_MATCH'}]})
    cases = {name: {'baseline': 'a'*40, 'instruction': 'Fix '+name, 'checks': {'check': ['python', 'checks.py']}}
             for name in ('one', 'two')}
    value = {'schema': comparison.SCHEMA, 'cohort': 'small-python-v1', 'baseline': 'cheap', 'cases': cases,
        'candidates': [{'id': name, 'implementer': {'deployment': name},
                        'auditor': {'deployment': 'reviewer'}} for name in ('cheap', 'reliable')], 'trials': []}
    for name in ('cheap', 'reliable'):
        for case, contract in cases.items():
            stages = []
            for stage, model in (('implement', name), ('audit', 'reviewer')):
                root = tmp_path/name/case/stage; root.mkdir(parents=True)
                identity = name+'-'+case+'-'+stage
                count = (3 if stage == 'implement' else 2) if name == 'cheap' else 1
                cost = '.10' if model == 'cheap' else '.20'
                state = {'schema': 'tare.tools/native-agent/2', 'session_id': identity,
                    'deployment': profiles[model], 'instruction': contract['instruction'],
                    'config': {'verifiers': {'check': {'argv': ['python', 'checks.py']}}},
                    'invocations': [{'id': str(n), 'started_at': n*10, 'ended_at': n*10+5, 'status': 'PROPOSED'} for n in range(count)]}
                (root/'native-agent.json').write_text(json.dumps(state))
                (root/'outcome.json').write_text('{"status":"PROPOSED"}')
                (root/'journal.jsonl').write_text('fixture journal signature')
                (root/'usage.json').write_text(json.dumps({'schema': 'tare-native-usage/1', 'session_id': identity,
                    'calls': [{'call_id': str(n), 'purpose': 'model', 'execution': 'possibly_executed',
                               'cost': {'currency': 'USD', 'known_amount': cost, 'complete': True}} for n in range(count)]}))
                stages.append({'stage': stage, 'path': str(root)})
            value['trials'].append({'candidate': name, 'case': case, 'contract_sha256': digest(contract),
                'sessions': stages, 'verdict': 'accepted', 'verdict_source': 'external test review', 'additional_cost_usd': '0'})
    path = tmp_path/'study.json'
    def save():
        path.write_text(json.dumps(value)); return path
    save()
    return value, path, save


def test_full_path_can_reverse_per_call_price_and_exports_bound_policy(study, tmp_path):
    value, path, save = study
    result = comparison.compare(path)
    assert result['winner'] == 'reliable' and result['generation_calls'] == 0
    assert [r['usd_per_accepted_delivery'] for r in result['candidates']] == ['0.70', '0.40']
    assert result['candidates'][0]['trials'][0]['generation_intents'] == 5
    output = tmp_path/'policy.json'
    comparison.write_policy(path, output)
    policy = json.loads(output.read_text())
    comparison.validate_evidence(policy)
    assert policy['selection'] == 'measured_delivery' and 'command' not in policy
    session = Path(value['trials'][0]['sessions'][0]['path'])
    with (session/'journal.jsonl').open('a') as f: f.write('later execution')
    with pytest.raises(ValueError, match='CHANGED'):
        comparison.validate_evidence(policy)


def test_selection_observation_does_not_change_effective_model_identity(study):
    value, path, save = study
    root = Path(value['trials'][0]['sessions'][0]['path'])
    state = json.loads((root/'native-agent.json').read_text())
    state['deployment']['native_selection'] = {'observed_at': 'new-dispatch', 'reason': 'AUTO'}
    (root/'native-agent.json').write_text(json.dumps(state))
    assert comparison.compare(path)['winner'] == 'reliable'
    state['deployment']['native_profile']['parameters_json'] = '{"temperature":0.9}'
    (root/'native-agent.json').write_text(json.dumps(state))
    report = comparison.compare(path)
    assert report['winner'] is None
    assert any('PROFILE_CHANGED' in issue for issue in report['candidates'][0]['issues'])


def test_undispatched_trial_is_visible_and_prevents_promotion(study, tmp_path):
    value, path, save = study
    value['trials'][2]['sessions'] = [{'stage': 'implement', 'path': str(tmp_path/'never-dispatched')}]
    report = comparison.compare(save())
    assert report['winner'] is None
    candidate = report['candidates'][1]
    assert candidate['attempted'] == 2 and candidate['accepted'] == 1
    assert any(issue.startswith('IMPLEMENTATION_MISSING:') for issue in candidate['issues'])


@pytest.mark.parametrize('fault,issue', [('price', 'COST_INCOMPLETE'), ('verdict', 'VERDICT_UNKNOWN'),
    ('audit', 'AUDIT_MISSING'), ('verifier', 'VERIFIER_CONTRACT_MISMATCH'), ('extra', 'ADDITIONAL_COST_UNKNOWN'),
    ('live', 'INCOMPLETE_EXECUTION'), ('clock', 'INVALID_EXECUTION_TIMESTAMPS'), ('profile', 'PROFILE_CHANGED_OR_MIXED')])
def test_unknown_or_changed_evidence_never_wins(study, tmp_path, fault, issue):
    value, path, save = study
    trial = value['trials'][2]
    root = Path(trial['sessions'][0]['path'])
    if fault == 'price':
        report = json.loads((root/'usage.json').read_text()); report['calls'][0].pop('cost')
        (root/'usage.json').write_text(json.dumps(report))
    elif fault == 'verdict': trial.pop('verdict')
    elif fault == 'audit': trial['sessions'] = trial['sessions'][:1]
    elif fault == 'extra': trial.pop('additional_cost_usd')
    else:
        if fault == 'verifier': root = Path(trial['sessions'][1]['path'])
        state = json.loads((root/'native-agent.json').read_text())
        if fault == 'live': state['invocations'][0].pop('ended_at')
        elif fault == 'clock': state['invocations'][0]['ended_at'] = state['invocations'][0]['started_at'] - 1
        elif fault == 'profile': state['deployment']['native_profile']['model'] = 'different'
        else: state['config']['verifiers']['check']['argv'] = ['always-pass']
        (root/'native-agent.json').write_text(json.dumps(state))
    result = comparison.compare(save())
    assert result['winner'] is None
    assert any(i.startswith(issue) for i in result['candidates'][1]['issues'])
    if fault == 'clock':
        measured = result['candidates'][1]
        assert measured['known_cost_usd'] == '0.80' and measured['accepted'] == 2
        assert measured['total_seconds'] is None and measured['trials'][0]['seconds'] is None
        assert measured['trials'][0]['generation_intents'] == 2
    with pytest.raises(ValueError, match='unchanged'):
        comparison.write_policy(path, tmp_path/'policy.json')
    assert not (tmp_path/'policy.json').exists()


def test_same_case_set_contract_and_independent_trials_required(study):
    value, path, save = study
    trial = value['trials'].pop()
    with pytest.raises(ValueError, match='Every candidate'):
        comparison.compare(save())
    value['trials'].append(trial)
    trial['contract_sha256'] = 'wrong'
    with pytest.raises(ValueError, match='contract'):
        comparison.compare(save())
    trial['contract_sha256'] = digest(value['cases'][trial['case']])
    trial['sessions'] = value['trials'][0]['sessions']
    with pytest.raises(ValueError, match='different comparison trials'):
        comparison.compare(save())


def test_failed_delivery_charges_still_count_and_cli_does_not_infer_acceptance(study, capsys):
    value, path, save = study
    value['trials'][0]['verdict'] = 'rejected'
    result = comparison.compare(save())
    assert result['candidates'][0]['known_cost_usd'] == '1.40'
    assert result['candidates'][0]['accepted'] == 1
    assert result['candidates'][0]['usd_per_accepted_delivery'] == '1.40'
    assert native_profiles.main(['compare', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['winner'] == 'reliable'


def test_duplicate_reference_not_double_counted_and_stale_check_abstains(study, monkeypatch):
    value, path, save = study
    value['trials'][0]['sessions'].insert(1, value['trials'][0]['sessions'][0])
    assert comparison.compare(save())['candidates'][0]['known_cost_usd'] == '1.40'
    monkeypatch.setattr(native_review, 'review', lambda *a, **kw: {'unresolved_effects': [], 'checks': [
        {'name': 'check', 'status': 'succeeded', 'returncode': 0, 'freshness': 'STALE'}]})
    assert comparison.compare(path)['winner'] is None


def test_zero_success_costs_are_retained_without_dividing_by_zero(study):
    value, path, save = study
    for trial in value['trials']:
        trial.pop('contract_sha256')  # Cases are defined once; no hand-written receipt hashes needed.
        if trial['candidate'] == 'cheap':
            trial['verdict'] = 'rejected'
    report = comparison.compare(save())
    assert report['winner'] == 'reliable'
    assert report['candidates'][0]['known_cost_usd'] == '1.40'
    assert report['candidates'][0]['usd_per_accepted_delivery'] is None


def test_provider_allocation_snapshot_is_bound_to_preference_and_preserves_receipts(study, tmp_path):
    value, path, save = study
    costs = {'schema': 'tare.tools/delivery-costs/1', 'basis': 'estimated_delivery_cost', 'profiles': {}}
    originals = {}
    for trial in value['trials']:
        for ref in trial['sessions']:
            root = Path(ref['path']); state = json.loads((root/'native-agent.json').read_text())
            dep = state['deployment']
            costs['profiles'][dep['id']] = {'profile_sha256': digest(dep), 'mode': 'hourly',
                'currency': 'USD', 'source': 'Declared subscription allocation', 'observed_at': '2026-09-23',
                'usd_per_hour': '3.6'}
            originals[str(root)] = (root/'usage.json').read_bytes()
    snapshot = tmp_path/'costs.json'; snapshot.write_text(json.dumps(costs))
    value['costs'] = str(snapshot)
    result = comparison.compare(save())
    assert result['cost_basis'] == 'estimated_delivery_cost' and result['winner'] == 'reliable'
    assert [r['known_cost_usd'] for r in result['candidates']] == ['0.050', '0.020']
    output = tmp_path/'policy.json'; comparison.write_policy(path, output)
    policy = json.loads(output.read_text()); comparison.validate_evidence(policy)
    for root, original in originals.items():
        assert (Path(root)/'usage.json').read_bytes() == original
    costs['profiles']['cheap']['usd_per_hour'] = '4'
    snapshot.write_text(json.dumps(costs))
    with pytest.raises(ValueError, match='CHANGED'):
        comparison.validate_evidence(policy)


def test_new_experiments_do_not_invalidate_completed_preference(study, tmp_path):
    value, path, save = study
    output = tmp_path/'preference.json'
    comparison.write_policy(path, output)
    policy = json.loads(output.read_text())
    frozen = Path(policy['delivery_study']['path'])
    assert frozen == output.with_suffix('.study.json')
    assert json.loads(frozen.read_text()) == value
    value['cohort'] = 'next-round-still-in-progress'
    value['trials'].pop()
    save()
    comparison.validate_evidence(policy)
    receipt = Path(value['trials'][0]['sessions'][0]['path'])/'journal.jsonl'
    receipt.write_text(receipt.read_text()+'new owner activity')
    with pytest.raises(ValueError, match='CHANGED'):
        comparison.validate_evidence(policy)

def test_existing_snapshot_is_preserved(study, tmp_path):
    value, path, save = study
    output = tmp_path/'preference.json'
    frozen = output.with_suffix('.study.json')
    frozen.write_text('preserve me')
    with pytest.raises(ValueError):
        comparison.write_policy(path, output)
    assert frozen.read_text() == 'preserve me'
    assert not output.exists()


def test_study_changed_between_compare_and_snapshot_is_not_exported(study, tmp_path, monkeypatch):
    value, path, save = study
    original = comparison.compare

    def changed(source):
        result = original(source)
        value['cohort'] = 'new-experiment'
        save()
        return result

    monkeypatch.setattr(comparison, 'compare', changed)
    with pytest.raises(ValueError, match='Study changed'):
        comparison.write_policy(path, tmp_path/'preference.json')
    assert not (tmp_path/'preference.json').exists()
    assert not (tmp_path/'preference.study.json').exists()

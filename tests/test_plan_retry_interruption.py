"""An explicit new review preserves a lost response without hiding uncertain tools."""
import json

import pytest

from compute_plane.native_journal import NativeJournal, save_json
from relay.work_recovery import new_plan_attempt


@pytest.mark.parametrize('fault', ['none', 'cleanup', 'pending', 'unknown', 'running', 'writable'])
def test_new_review_after_lost_model_response(tmp_path, fault):
    train = tmp_path/'train'; train.mkdir()
    custody = tmp_path/'review'; custody.mkdir()
    state = {'session_id': 'review', 'config': {'role': 'plan-auditor', 'writable': {}},
             'invocations': [{'ended_at': None if fault == 'running' else 1}]}
    if fault == 'writable': state['config']['writable'] = {'repo': ['.']}
    save_json(custody/'native-agent.json', state)
    save_json(custody/'outcome.json', {'status': 'RECONCILIATION_REQUIRED'})
    journal = NativeJournal(custody/'journal.jsonl')
    journal.append({'kind': 'effect_requested', 'effect_id': 'read'})
    if fault != 'pending':
        journal.append({'kind': 'effect_returned', 'result': {
            'effect_id': 'read', 'status': 'unknown' if fault == 'unknown' else 'succeeded'}})
    journal.append({'kind': 'model_requested', 'call_id': 'review/model/2'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 'review/model/2'})
    NativeJournal(custody/'observations.jsonl').append({'call_id': 'review/model/2', 'purpose': 'model',
        'error': 'HTTP_INCOMPLETE_STREAM', 'transport_calls': [{'cleanup_verified': fault != 'cleanup'}]})
    origin = {'plan_custody': str(custody)}
    save_json(train/'WORK_RUN.json', origin)
    before = {p: p.read_bytes() for p in custody.iterdir()}
    if fault == 'none':
        new_plan_attempt(train, origin)
        assert origin['plan_custody'] != str(custody)
        assert origin['plan_audit_attempts'] == [str(custody)]
        assert json.loads((train/'WORK_RUN.json').read_bytes()) == origin
    else:
        with pytest.raises(ValueError, match='PLAN_RETRY_REQUIRES_FINISHED_ATTEMPT'):
            new_plan_attempt(train, origin)
        assert origin == {'plan_custody': str(custody)}
    assert all(p.read_bytes() == raw for p, raw in before.items())

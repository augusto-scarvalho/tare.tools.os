import hashlib
import json

import pytest

from compute_plane.native_journal import NativeJournal
from relay.native_agent import run_session
from tare_tools_agent_runtime import CancellationToken
from test_native_agent import Client, deployment, setup


def prepare(setup):
    config, custody, root = setup
    (root/'PACKET.md').write_text('Bounded task: inspect this plan.\n')
    (root/'EXECUTION_DESCRIPTOR.json').write_text('{"criteria": ["inspect the plan"]}\n')
    config.update(role='plan-auditor', typed_output=True, repositories={'train': str(root)},
                  writable={}, verifiers={}, capabilities=['read', 'list'])
    sources = [{'repository': 'train', 'path': name,
                'sha256': hashlib.sha256((root/name).read_bytes()).hexdigest()}
               for name in ('PACKET.md', 'EXECUTION_DESCRIPTOR.json')]
    review = {'summary': 'Plan reviewed', 'sources': sources[:1], 'remaining': [], 'verdict': 'ACCEPT', 'findings': []}
    steps = [('read', {'repository': 'train', 'path': 'PACKET.md'}), ('tare_deliver', review)]
    complete = dict(review, sources=sources)
    return config, custody, root, steps, complete


def invoke(config, custody, client, **options):
    return run_session(config, deployment(client.protocol), custody, 'Review the bounded plan.',
                       client=client, local_counter=client, recover_plan_source=True, **options)


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_missing_citation_reads_once_continues_and_replays_without_inference(setup, protocol):
    config, custody, root, steps, complete = prepare(setup)
    client = Client(protocol, steps+[('tare_deliver', complete)])
    result = invoke(config, custody, client)
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 3
    assert result['source_recovery']['state'] == 'applied'
    events = [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    assert len([e for e in events if e['kind'] == 'host_observation']) == 1
    assert not any(e['kind'] == 'user_message' for e in events)
    assert 'not a user message' in json.dumps(client.calls[-1])
    reads = NativeJournal(custody/'source-recovery-read.jsonl').snapshot().records
    assert len(reads) == 2
    assert json.loads((custody/'artifact.json').read_text())['payload'] == complete
    replay = Client(protocol)
    again = run_session(None, None, custody, resume=True, client=replay, local_counter=replay, recover_plan_source=True)
    assert again['model_calls'] == 3 and not replay.calls
    assert len(NativeJournal(custody/'source-recovery-read.jsonl').snapshot().records) == 2


@pytest.mark.parametrize('mode', ['disabled', 'exhausted', 'second-missing'])
def test_disabled_budget_and_second_missing_never_dispatch_another_read(setup, mode):
    config, custody, root, steps, complete = prepare(setup)
    client = Client(steps=steps + ([steps[-1]] if mode == 'second-missing' else []))
    result = run_session(config, deployment(), custody, 'Review the plan.', client=client, local_counter=client,
        recover_plan_source=mode != 'disabled', limits={'max_model_calls': 2} if mode == 'exhausted' else {})
    assert result['model_calls'] == (3 if mode == 'second-missing' else 2)
    assert (custody/'source-recovery-read.jsonl').exists() == (mode == 'second-missing')
    if mode == 'second-missing':
        assert len(NativeJournal(custody/'source-recovery-read.jsonl').snapshot().records) == 2


def test_cancel_after_read_preserves_receipt_and_resumes_without_second_read(setup, monkeypatch):
    from compute_plane.native_capabilities import NativeCapabilities
    config, custody, root, steps, complete = prepare(setup)
    cancellation = CancellationToken()
    original = NativeCapabilities.execute
    reads = []
    def execute(self, request, control):
        result = original(self, request, control)
        if request.effect_id.endswith('/source-recovery/1'):
            reads.append(request.effect_id); cancellation.cancel()
        return result
    monkeypatch.setattr(NativeCapabilities, 'execute', execute)
    result = invoke(config, custody, Client(steps=steps), cancellation=cancellation)
    assert result['status'] == 'CANCELLED'
    deadline = json.loads((custody/'native-agent.json').read_text())['deadline_utc']
    client = Client(steps=[('tare_deliver', complete)])
    # Disabling new decisions still consumes a read that was already dispatched.
    again = run_session(None, None, custody, resume=True, client=client, local_counter=client, recover_plan_source=False)
    assert again['status'] == 'PROPOSED' and again['model_calls'] == 3
    assert len(reads) == 1
    assert json.loads((custody/'native-agent.json').read_text())['deadline_utc'] == deadline


@pytest.mark.parametrize('fault', ['interrupted', 'changed'])
def test_interrupted_or_stale_read_is_not_retried_or_consumed(setup, monkeypatch, fault):
    from compute_plane.native_capabilities import NativeCapabilities
    config, custody, root, steps, complete = prepare(setup)
    original = NativeCapabilities.execute
    def execute(self, request, control):
        result = original(self, request, control)
        if request.effect_id.endswith('/source-recovery/1'):
            if fault == 'interrupted':
                raise RuntimeError('crash after read before receipt')
            (root/'EXECUTION_DESCRIPTOR.json').write_text('changed')
        return result
    monkeypatch.setattr(NativeCapabilities, 'execute', execute)
    with pytest.raises((ValueError, RuntimeError)):
        invoke(config, custody, Client(steps=steps))
    replay = Client()
    with pytest.raises(ValueError):
        run_session(None, None, custody, resume=True, client=replay, local_counter=replay, recover_plan_source=True)
    assert not replay.calls


def test_source_context_overflow_stops_without_another_model_call(setup):
    config, custody, root, steps, complete = prepare(setup)
    (root/'EXECUTION_DESCRIPTOR.json').write_text('x' * 18000)
    result = invoke(config, custody, Client(steps=steps))
    assert result['model_calls'] == 2
    assert result['source_recovery']['state'] == 'insufficient'
    saved = json.loads((custody/'source-recovery.json').read_text())
    assert saved['failure'] == 'SOURCE_CONTEXT_OVER_BUDGET'
    assert 'text' not in saved

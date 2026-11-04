"""OS facade qualification using installed Runtime/Kernel, never a provider.

Only the inference/counting ports are fixtures. In particular, progressed is
produced by the real repository capabilities and consumed by NativeSession.
"""
import json

import pytest

from compute_plane.native_journal import NativeJournal
from relay import native_agent
from tare_tools_agent_runtime import NativeBudget
from test_native_agent import Client, run, setup  # explicit shared fixture/helpers


def events(custody):
    path = custody/'journal.jsonl'
    return ([record.event() for record in NativeJournal(path).snapshot().records]
            if path.exists() else [])


def effects(custody):
    return [row['result'] for row in events(custody) if row['kind'] == 'effect_returned']


def read(path='a.py'):
    return 'read', {'repository': 'repo', 'path': path}


def saved_state(custody):
    return json.loads((custody/'native-agent.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
@pytest.mark.parametrize('reconstruct', [False, True])
def test_identical_reads_stop_before_another_call_even_after_reconstruction(setup, protocol, reconstruct):
    config, custody, root = setup
    config['verifiers'] = {}
    # One novel read followed by three stagnant turns. Any fifth generation is
    # a test failure (Client's script is exhausted), not a manufactured outcome.
    first = Client(protocol, [read()] * (3 if reconstruct else 4))
    options = ({'pause_requested': lambda: len(effects(custody)) >= 3}
               if reconstruct else {})
    initial = run(config, custody, first,
                  budget=NativeBudget(max_model_calls=8, max_stagnant_turns=3), **options)
    if reconstruct:
        assert initial['status'] == 'PAUSED', initial
        assert initial['model_calls'] == first.generations == 3
        assert [row['progressed'] for row in effects(custody)] == [True, False, False]
        state_before = saved_state(custody)
        journal_before = (custody/'journal.jsonl').read_bytes()
        observations_before = (custody/'observations.jsonl').read_bytes()
        second = Client(protocol, [read()])
        result = native_agent.run_session(None, None, custody, resume=True,
                                          client=second, local_counter=second)
        # A lost stagnant counter would try a second generation; lost tool
        # observation memory would incorrectly mark this identical read novel.
        assert second.generations == 1
        assert (custody/'journal.jsonl').read_bytes().startswith(journal_before)
        assert (custody/'observations.jsonl').read_bytes().startswith(observations_before)
        after = saved_state(custody)
        for key in ('session_id', 'instruction', 'deployment', 'config', 'budget',
                    'limits', 'timeout', 'deadline_utc'):
            assert after[key] == state_before[key], key
        assert result['session_id'] == initial['session_id']
        assert result['profile_digest'] == initial['profile_digest']
        assert result['effective_limits'] == initial['effective_limits']
        # Rebuilt adapter retains the original system prefix and every earlier
        # assistant message (including reasoning/signature), not a fresh chat.
        generation_path = '/chat/completions' if protocol == 'openai-chat' else '/converse'
        before_wire = [body for path, body in first.calls if path.endswith(generation_path)][-1]
        after_wire = [body for path, body in second.calls if path.endswith(generation_path)][0]
        assert after_wire['messages'][0] == before_wire['messages'][0]
        for message in before_wire['messages']:
            if message['role'] == 'assistant':
                assert message in after_wire['messages']
    else:
        result = initial
        assert first.generations == 4
    assert result['status'] == 'BLOCKED' and result['reason'] == 'NO_PROGRESS', result
    assert result['model_calls'] == 4
    assert [row['progressed'] for row in effects(custody)] == [True, False, False, False]
    assert all(row['status'] == 'succeeded' for row in effects(custody))
    assert (root/'a.py').read_bytes() == b'value = 1\n'
    # Inspect/resume of the same terminal session must not request even counting
    # or template work, much less buy another generation or renew its allowance.
    replay = Client(protocol)
    repeated = native_agent.run_session(None, None, custody, resume=True,
                                        client=replay, local_counter=replay)
    assert repeated['status'] == 'BLOCKED' and repeated['reason'] == 'NO_PROGRESS'
    assert repeated['model_calls'] == 4 and replay.calls == []


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_new_observation_between_repeats_allows_continuation(setup, protocol):
    config, custody, root = setup
    config['verifiers'] = {}
    (root/'b.py').write_bytes(b'other = 2\n')
    client = Client(protocol, [read(), read(), read('b.py'), read(),
                              ('tare_deliver', {'summary': 'Inspected both fixture files; no edits.'})])
    result = run(config, custody, client,
                 budget=NativeBudget(max_model_calls=6, max_stagnant_turns=2))
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 5, result
    assert client.generations == 5
    assert [row['progressed'] for row in effects(custody)] == [True, False, True, False]
    # Novelty resets the consecutive-stagnation window, not the seen set: a.py
    # after b.py remains a repeat. This is NOT proof of semantic task progress.
    assert (root/'a.py').read_bytes() == b'value = 1\n'

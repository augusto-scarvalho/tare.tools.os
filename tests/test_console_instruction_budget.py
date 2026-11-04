"""Offline checks for public input versus trusted initial-instruction framing."""
from types import SimpleNamespace

import pytest

from relay import console_entry, native_agent
from relay.console_entry import ASSISTANT_CONTEXT
from test_native_agent import Client, deployment, setup


class BootstrapReached(Exception):
    """Stop immediately after the real initial-instruction validation."""


@pytest.fixture
def bootstrap(setup, monkeypatch):
    config, custody, _ = setup
    client = Client()
    reached = []
    # Keep this boundary check independent of optional deployment capsules.
    monkeypatch.setattr(native_agent, 'prompt_capsule', lambda *args: None)

    def next_stage(*args):
        reached.append(args)
        raise BootstrapReached

    monkeypatch.setattr(native_agent, 'deployment_profile', next_stage)

    def start(instruction):
        return native_agent.run_session(
            config, deployment(), custody, instruction,
            client=client, local_counter=client,
            conversation=True, accept_messages=True)

    return start, reached, client, custody


def test_trusted_prefix_fits_reserved_bytes():
    prefix_bytes = len(ASSISTANT_CONTEXT.encode('utf-8'))
    assert 0 < prefix_bytes <= 2048
    assert len((ASSISTANT_CONTEXT + 'x' * 16384).encode('utf-8')) <= 18432


@pytest.mark.parametrize('prompt', [
    'x' * 16205,
    'é' * 8102 + 'x',
    'x' * 16384,
    'é' * 8192,
], ids=['ascii-near', 'utf8-near', 'ascii-public-limit', 'utf8-public-limit'])
def test_public_prompt_with_guidance_reaches_next_bootstrap_stage(bootstrap, prompt):
    start, reached, client, _ = bootstrap
    instruction = ASSISTANT_CONTEXT + prompt
    assert len(prompt.encode('utf-8')) <= 16384
    assert 16384 < len(instruction.encode('utf-8')) <= 18432
    # This exercises run_session, not a duplicate of its validation expression.
    # On the old ceiling it raises ValueError instead of reaching this sentinel.
    with pytest.raises(BootstrapReached):
        start(instruction)
    assert len(reached) == 1
    assert client.calls == []
    assert client.generations == 0


@pytest.mark.parametrize('instruction', ['x' * 18432, 'é' * 9216],
                         ids=['ascii-internal-limit', 'utf8-internal-limit'])
def test_exact_internal_byte_ceiling_is_accepted(bootstrap, instruction):
    start, reached, client, _ = bootstrap
    assert len(instruction.encode('utf-8')) == 18432
    with pytest.raises(BootstrapReached):
        start(instruction)
    assert len(reached) == 1
    assert client.calls == []


@pytest.mark.parametrize('instruction', ['x' * 18433, 'é' * 9216 + 'x'],
                         ids=['ascii-over', 'utf8-over'])
def test_one_byte_over_internal_ceiling_is_refused_before_generation(bootstrap, instruction):
    start, reached, client, custody = bootstrap
    assert len(instruction.encode('utf-8')) == 18433
    with pytest.raises(ValueError, match='native_agent_bounded_instruction_required') as error:
        start(instruction)
    assert '18433 UTF-8 bytes' in str(error.value)
    assert 'limit is 18432' in str(error.value)
    assert 'refer to project files' in str(error.value)
    assert reached == []
    assert client.calls == []
    assert client.generations == 0
    assert not (custody / 'native-agent.json').exists()
    assert not (custody / 'journal.jsonl').exists()


@pytest.mark.parametrize('prompt', ['x' * 16385, 'é' * 8192 + 'x'],
                         ids=['ascii-over', 'utf8-over'])
def test_public_initial_message_still_refuses_over_16kib(tmp_path, prompt):
    assert len(prompt.encode('utf-8')) == 16385
    calls = []

    def forbidden_generation(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail('Oversized public input must not generate')

    custody = tmp_path / 'entry'
    selection = SimpleNamespace(prompt=prompt, root=str(tmp_path))
    with pytest.raises(ValueError, match='at most 16384 UTF-8 bytes'):
        console_entry.propose(selection, None, directory=custody, generate=forbidden_generation)
    assert calls == []
    assert not custody.exists()

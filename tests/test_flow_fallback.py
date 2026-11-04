import json
from pathlib import Path

import pytest
from tare_tools_agent_runtime import CancellationToken

from relay import console_entry, flow_assessment as flows, flow_fallback as fallback
from relay.console_backend import Selection, command


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path/'project'; project.mkdir()
    config = {'schema': 'tare.tools/flow-assessment-policy/1', 'mode': 'suggest',
              'command': ['fixture'], 'timeout_seconds': 10,
              'fallback': {'deployment': 'vendor-fixture', 'timeout_seconds': 15, 'output_tokens': 1024}}
    policy = tmp_path/'policy.json'; policy.write_text(json.dumps(config))
    monkeypatch.setenv('TARE_FLOW_POLICY', str(policy))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(flows, 'run_assessment', lambda *a, **k: {
        'status': 'FAILED', 'reason': 'ASSESSMENT_CAPACITY_EXCEEDED', 'inference_attempts': 1})
    monkeypatch.setattr(fallback, 'select', lambda name: {
        'id': name, 'native_profile': {'model': 'fixture', 'protocol': 'openai-codex-responses',
                                     'output_tokens': 32768}})
    return project, config


def model(monkeypatch, choice='session', *, fail=False):
    calls = []
    def generate(dep, name, prompt, timeout, **kwargs):
        calls.append((dep, json.loads(prompt), timeout, kwargs))
        return not fail, json.dumps({'choice': choice}), {
            'generation_calls': 1, 'usage': None, 'failure_class': 'AUTH_FAILED'}
    monkeypatch.setattr(fallback, 'generate_text', generate)
    return calls


@pytest.mark.parametrize('choice', ['session', 'explore', 'plan', 'research', 'clarify', 'insufficient'])
def test_capacity_automatically_uses_one_bounded_choice_and_keeps_intent(setup, tmp_path, monkeypatch, choice):
    calls = model(monkeypatch, choice)
    text = 'Investigate https://example.org/docs without editing.'
    selected = Selection(str(setup[0]), prompt=text, unlimited=True)
    def forbidden(*a, **k): pytest.fail('Legacy interpreter called as third evaluator')
    result = console_entry.propose(selected, CancellationToken(), directory=tmp_path/'entry', generate=forbidden)
    assert len(calls) == 1
    dep, request, timeout, kwargs = calls[0]
    assert json.loads(request['state'])['operator_messages'] == [text]
    assert timeout == 15 and dep['native_profile']['output_tokens'] == 1024
    assert 'no tools' in kwargs['system'] and result['demand'] == text
    assert result['status'] == ('UNAVAILABLE' if choice == 'insufficient' else 'NEEDS_INPUT' if choice == 'clarify' else 'PROPOSED')
    assert result['generation_calls'] == 2 and result['tokens'] is None
    assert result['flow_assessment']['fallback']['status'] == 'SUGGESTED'
    assert not list(setup[0].iterdir())
    if choice == 'explore':
        args = command(console_entry.realize(selected, result))
        assert args[args.index('--role')+1] == 'explorer'
    saved = json.loads((tmp_path/'entry/flow-assessment/decision.json').read_text())
    replay = flows.decide(request, tmp_path/'entry/flow-assessment', CancellationToken(), setup[1])
    assert replay == saved and len(calls) == 1


@pytest.mark.parametrize('reason', ['ASSESSMENT_RESOURCE_UNAVAILABLE', 'ASSESSMENT_DISTRIBUTION_INVALID',
                                   'ASSESSMENT_CHECKPOINT_CHANGED', 'ASSESSMENT_TIMEOUT', 'CANCELLED'])
def test_ambiguous_or_invalid_local_result_does_not_start_fallback(setup, tmp_path, monkeypatch, reason):
    monkeypatch.setattr(flows, 'run_assessment', lambda *a, **k: {
        'status': 'FAILED', 'reason': reason, 'inference_attempts': 1})
    monkeypatch.setattr(fallback, 'generate_text', lambda *a, **k: pytest.fail('Unsafe fallback'))
    result = console_entry.propose(Selection(str(setup[0]), prompt='Help'), CancellationToken(), directory=tmp_path/'entry')
    assert result['status'] == 'UNAVAILABLE' and 'fallback' not in result['flow_assessment']


def test_known_interactive_cpu_admission_refusal_uses_vendor_once(setup, tmp_path, monkeypatch):
    calls = model(monkeypatch, 'explore')
    monkeypatch.setattr(flows, 'run_assessment', lambda *a, **k: {
        'status': 'FAILED', 'reason': 'ASSESSMENT_CPU_NOT_ADMITTED', 'inference_attempts': 1,
        'elapsed_ms': 120, 'worker_exit_code': 0})
    result = console_entry.propose(Selection(str(setup[0]), prompt='Inspect without editing'),
        CancellationToken(), directory=tmp_path/'entry')
    assert result['status'] == 'PROPOSED' and result['flow'] == 'explore'
    assert len(calls) == 1
    assert result['flow_assessment']['fallback']['trigger'] == 'ASSESSMENT_CPU_NOT_ADMITTED'
    saved = json.loads((tmp_path/'entry/flow-assessment/decision.json').read_text())
    assert saved['assessment']['worker_exit_code'] == 0


def test_recorded_cpu_exit_allows_fallback_but_cancellation_does_not(setup, tmp_path, monkeypatch):
    calls = model(monkeypatch)
    cancellation = CancellationToken()
    monkeypatch.setattr(flows, 'run_assessment', lambda *a, **k: {
        'status': 'FAILED', 'reason': 'ASSESSMENT_TIMEOUT', 'inference_attempts': 1,
        'worker_exit_code': 124, 'worker_stage': 'CPU_SCORING'})
    selected = Selection(str(setup[0]), prompt='Fix the bug')
    assert console_entry.propose(selected, cancellation, directory=tmp_path/'one')['status'] == 'PROPOSED'
    cancellation.cancel()
    assert console_entry.propose(selected, cancellation, directory=tmp_path/'two')['status'] == 'CANCELLED'
    assert len(calls) == 1


@pytest.mark.parametrize('choice,fail', [('shell', False), ('session', True)])
def test_bad_fallback_is_retained_without_retry(setup, tmp_path, monkeypatch, choice, fail):
    calls = model(monkeypatch, choice, fail=fail)
    result = console_entry.propose(Selection(str(setup[0]), prompt='Help'), CancellationToken(), directory=tmp_path/'entry')
    assert result['status'] == 'UNAVAILABLE' and len(calls) == 1
    assert result['flow_assessment']['fallback']['status'] == 'UNAVAILABLE'
    assert not list(setup[0].iterdir())


def test_cancelled_fallback_cannot_supply_choice(setup, tmp_path, monkeypatch):
    cancel = CancellationToken()
    def generate(*a, **k):
        cancel.cancel()
        return True, '{"choice":"session"}', {'generation_calls': 1, 'usage': None}
    monkeypatch.setattr(fallback, 'generate_text', generate)
    result = console_entry.propose(Selection(str(setup[0]), prompt='Fix bug'), cancel, directory=tmp_path/'entry')
    assert result['status'] == 'CANCELLED' and not result.get('flow')


def test_interrupted_fallback_is_not_replayed(setup, tmp_path, monkeypatch):
    class Interrupted(BaseException): pass
    monkeypatch.setattr(fallback, 'generate_text', lambda *a, **k: (_ for _ in ()).throw(Interrupted()))
    request = {'state': 'Fix bug', 'question': 'Which flow?', 'options': flows.ENTRY_OPTIONS}
    root = tmp_path/'decision'
    with pytest.raises(Interrupted):
        flows.decide(request, root, CancellationToken(), setup[1])
    monkeypatch.setattr(fallback, 'generate_text', lambda *a, **k: pytest.fail('Replayed interruption'))
    result = flows.decide(request, root, CancellationToken(), setup[1])
    assert result['fallback']['status'] == 'UNKNOWN' and result['choice'] is None
    assert result['fallback']['generation_calls'] is None
    monkeypatch.setattr(flows, 'decide', lambda *a, **k: result)
    entry = flows.entry(None, ['Fix bug'], [], root, CancellationToken())
    assert entry['generation_calls'] is None and entry['tokens'] is None
    assert entry['generation_calls_lower_bound'] == 1


def test_receipt_write_failure_does_not_report_zero_vendor_calls(setup, tmp_path, monkeypatch):
    def generate(*args, **kwargs): raise OSError('receipt write failed after dispatch')
    monkeypatch.setattr(fallback, 'generate_text', generate)
    result = console_entry.propose(Selection(str(setup[0]), prompt='Fix bug'), CancellationToken(), directory=tmp_path/'entry')
    assert result['status'] == 'UNAVAILABLE'
    assert result['flow_assessment']['fallback']['generation_calls'] is None
    assert result['generation_calls'] is None and result['tokens'] is None


def test_explicit_commands_and_gpu_profiles_cannot_call_fallback(setup, tmp_path, monkeypatch):
    calls = model(monkeypatch)
    selected = Selection(str(setup[0]), prompt='/session Fix bug')
    assert console_entry.propose(selected, CancellationToken(), directory=tmp_path/'one')['generation_calls'] == 0
    monkeypatch.setattr(fallback, 'select', lambda *a: {'native_profile': {'protocol': 'openai-chat'}})
    selected.prompt = 'Fix bug'
    result = console_entry.propose(selected, CancellationToken(), directory=tmp_path/'two')
    assert result['status'] == 'UNAVAILABLE' and not calls


@pytest.mark.parametrize('field,value', [('timeout_seconds', None), ('timeout_seconds', True),
    ('timeout_seconds', 61), ('output_tokens', 0), ('deployment', 'auto')])
def test_fallback_configuration_is_explicit_and_finite(setup, field, value):
    with pytest.raises(ValueError, match='CONFIG_INVALID'):
        fallback.validate_config({**setup[1]['fallback'], field: value})

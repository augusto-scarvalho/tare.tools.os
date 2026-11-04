"""Derived usage retains full evidence beyond the ordinary metadata budget."""
import json

import pytest

from compute_plane.native_journal import save_json, read_json
from compute_plane.native_usage import MAX_USAGE_BYTES
from relay import native_agent
from test_native_agent import setup, Client, run


def test_direct_outcome_keeps_attempt_metadata_out_of_model_prompts(setup, monkeypatch):
    from relay import native_profiles, work_run
    config, custody, _ = setup
    monkeypatch.setattr(work_run.metadata, 'version', lambda _: 'controller-fixture/1')
    client = Client(steps=[('tare_deliver', {'summary': 'Fixture delivery'})])
    result = run(config, custody, client)
    observation = result['runtime_observation']
    assert observation['runtime_version'] == 'controller-fixture/1'
    assert observation['effort'] is None
    assert read_json(custody/'outcome.json')['runtime_observation'] == observation
    assert read_json(custody/'native-agent.json')['invocations'][-1]['runtime_observation'] == observation
    assert native_profiles.usage(custody)['last_runtime_observation'] == observation
    assert 'controller-fixture' not in json.dumps(client.calls)


def test_large_report_finalizes_and_all_readers_agree(setup, monkeypatch):
    from compute_plane import native_usage
    from relay.native_profiles import usage
    from relay.execution_usage_sources import session_entry
    config, custody, _ = setup
    original = native_usage.native_usage_report
    def expanded(*args, **kwargs):
        report = original(*args, **kwargs)
        report['calls'][0]['raw_usage']['attribution'] = 'evidence' * (700 * 1024)
        return report
    monkeypatch.setattr(native_usage, 'native_usage_report', expanded)
    client = Client(steps=[('tare_deliver', {'summary': 'Fixture delivery'})])
    result = run(config, custody, client)
    assert result['status'] == 'PROPOSED'
    assert (custody/'usage.json').stat().st_size > 4 * 1024 * 1024
    saved = read_json(custody/'usage.json', max_bytes=MAX_USAGE_BYTES)
    assert len(saved['calls'][0]['raw_usage']['attribution']) == 8 * 700 * 1024
    assert read_json(custody/'outcome.json')['status'] == 'PROPOSED'
    assert usage(custody)['tokens'] == saved['tokens']
    assert session_entry(custody)['usage'] == saved
    assert native_agent.read_json(custody/'usage.json', MAX_USAGE_BYTES) == saved


def test_report_budget_is_explicit_and_oversize_does_not_replace_existing_file(tmp_path):
    target = tmp_path/'usage.json'
    save_json(target, {'old': True})
    with pytest.raises(ValueError, match='over_budget'):
        save_json(target, {'large': 'x' * (4 * 1024 * 1024)})
    assert read_json(target) == {'old': True}
    with pytest.raises(ValueError, match='over_budget'):
        save_json(target, {'large': 'x' * 100}, max_bytes=32)
    assert read_json(target) == {'old': True}
    with pytest.raises(ValueError, match='over_budget'):
        read_json(target, max_bytes=2)


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', '{"x":NaN}', '[1]', '{"x":"\ud800"}'])
def test_file_decoder_remains_strict(tmp_path, raw):
    target = tmp_path/'invalid.json'
    target.write_bytes(raw.encode('utf-8', errors='surrogatepass'))
    with pytest.raises(ValueError): read_json(target, max_bytes=MAX_USAGE_BYTES)

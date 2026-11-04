"""The public usage reader/formatter includes planners without an explorer."""
import json

import pytest

from compute_plane.native_journal import save_json
from relay.console_workbench import usage_rows
from relay.native_profiles import usage
from test_execution_usage_sources import direct


@pytest.mark.parametrize('stage,relative', [('planning','planning/p/session'), ('implementation','units/u/session-1')])
def test_native_usage_reader_includes_each_child_without_exploration(tmp_path, stage, relative):
    parent = tmp_path/'parent'
    child = parent/relative
    direct(parent, 'parent'); direct(child, 'child')
    state = json.loads((parent/'native-agent.json').read_bytes())
    state.update(limits={'max_model_calls':8}, deadline_utc=None)
    save_json(parent/'native-agent.json',state)
    if stage == 'planning': save_json(parent/'planning/p/receipt.json', {'calls_reserved':6,'calls_used':1})
    report = usage(parent)
    assert report['model_calls'] == report['provider_sessions'] == 2
    assert report[stage+'_sessions'] == 1 and report['exploration_sessions'] == 0
    body = usage_rows(report)[0]['body']
    assert 'Model calls: 2' in body and stage.capitalize()+' sessions included: 1' in body
    assert 'Unpriced or partially priced calls: 2' in body


def test_usage_panel_does_not_hide_a_session_with_missing_usage(tmp_path):
    parent = tmp_path/'parent'
    child = parent/'planning/p/session'
    direct(parent, 'parent'); direct(child, 'child')
    (child/'usage.json').unlink()
    state = json.loads((parent/'native-agent.json').read_bytes())
    state.update(limits={'max_model_calls':8}, deadline_utc=None)
    save_json(parent/'native-agent.json',state)
    save_json(parent/'planning/p/receipt.json', {'calls_reserved':6,'calls_used':1})
    report = usage(parent)
    assert report['unknown_session_ids'] == ['child']
    assert 'Sessions missing usage reports: 1' in usage_rows(report)[0]['body']

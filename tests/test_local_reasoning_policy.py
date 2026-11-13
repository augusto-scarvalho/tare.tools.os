"""Local effort is a template level, plus a token budget only where the model declares one."""
from copy import deepcopy
import json
import pytest
from relay import model_choices, native_catalog


@pytest.mark.parametrize('name',['qwen38-gsq','swift-next'])
def test_local_reasoning_preserves_other_wire_fields(name):
    value=native_catalog.select(name)
    original=deepcopy(value)
    params=json.loads(value['native_profile']['parameters_json'])
    params['chat_template_kwargs']['preserve_thinking']=True
    value['native_profile']['parameters_json']=json.dumps(params)
    for effort in ('low','medium','xhigh'):
        actual=model_choices.reasoning_parameters(value,effort)
        expected=deepcopy(params)
        expected['chat_template_kwargs']['reasoning_effort']=effort
        if effort in value.get('native_reasoning_budgets',{}):
            expected['thinking_budget']=value['native_reasoning_budgets'][effort]
        assert actual==expected
    for effort in ('high','max'):
        with pytest.raises(ValueError):model_choices.reasoning_parameters(value,effort)
    route=model_choices.describe(name,original)['route']
    assert route['efforts']==['low','medium','xhigh']
    assert route['default_effort']=='low'


def test_qwen38_levels_send_their_thinking_budget_and_a_switch_drops_the_old_one():
    value=native_catalog.select('qwen38-gsq')
    budget=lambda v,effort:model_choices.reasoning_parameters(v,effort).get('thinking_budget')
    assert [budget(value,e) for e in ('low','medium','xhigh')]==[None,4096,8192]
    saved=deepcopy(value)  # a session realized at xhigh, then switched back to low
    saved['native_profile']['parameters_json']=json.dumps(model_choices.reasoning_parameters(value,'xhigh'))
    assert budget(saved,'low') is None
    for name in ('swift-next','swift27b'):
        other=native_catalog.select(name)
        for effort in model_choices.describe(name,other)['route']['efforts']:
            assert 'thinking_budget' not in model_choices.reasoning_parameters(other,effort)
    assert model_choices.reasoning_parameters(native_catalog.select('swift-next'),'xhigh')['reasoning_budget_tokens']==2048

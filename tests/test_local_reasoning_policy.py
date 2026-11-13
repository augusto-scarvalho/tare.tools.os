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
        if 'thinking_budget' in value.get('native_reasoning_budgets',{}).get(effort,{}):
            expected['thinking_budget']=value['native_reasoning_budgets'][effort]['thinking_budget']
        assert actual==expected
    for effort in ('high','max'):
        with pytest.raises(ValueError):model_choices.reasoning_parameters(value,effort)
    route=model_choices.describe(name,original)['route']
    assert route['efforts']==['low','medium','xhigh']
    assert route['default_effort']=='low'


def test_qwen38_levels_send_their_thinking_budget_and_output_cap_and_a_switch_drops_the_old_ones():
    def limits(value,effort):  # (budget sent, output cap) after applying a level to a copy, as every caller does
        value=deepcopy(value)
        params=model_choices.reasoning_parameters(value,effort)
        return params.get('thinking_budget'),value['native_profile']['output_tokens']
    value=native_catalog.select('qwen38-gsq')
    assert set(value['native_reasoning_budgets'])==set(value['native_reasoning_levels'])  # every level states its cap
    # Thinking and the answer share the cap: a level that thinks 2048 more gets 2048 more, so the answer keeps its room.
    assert [limits(value,e) for e in ('low','medium','xhigh')]==[(None,16384),(4096,18432),(4096,18432)]
    saved=deepcopy(value)  # a session realized at xhigh, then switched back to low
    saved['native_profile']['parameters_json']=json.dumps(model_choices.reasoning_parameters(saved,'xhigh'))
    assert saved['native_profile']['output_tokens']==18432 and limits(saved,'low')==(None,16384)
    for name in ('swift-next','swift27b'):
        other=native_catalog.select(name)
        for effort in model_choices.describe(name,other)['route']['efforts']:
            before=other['native_profile']['output_tokens']
            assert limits(other,effort)==(None,before)
    assert model_choices.reasoning_parameters(native_catalog.select('swift-next'),'xhigh')['reasoning_budget_tokens']==2048

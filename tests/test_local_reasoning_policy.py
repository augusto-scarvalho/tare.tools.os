"""Local effort is not a token budget or a universal high switch."""
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
        assert actual==expected
    for effort in ('high','max'):
        with pytest.raises(ValueError):model_choices.reasoning_parameters(value,effort)
    route=model_choices.describe(name,original)['route']
    assert route['efforts']==['low','medium','xhigh']
    assert route['default_effort']=='low'

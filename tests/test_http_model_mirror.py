"""Pro execution is a distinct operator choice, not a fabricated provider model."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay import model_choices, native_catalog


@pytest.mark.parametrize('model,standard,pro',[
    ('gpt-6.1-sol','openai-api','openai-api-gpt-6-1-sol-pro'),
    ('gpt-6-astra','openai-api-gpt-6-astra','openai-api-gpt-6-astra-pro'),
])
def test_pro_is_separate_and_keeps_mode_when_effort_changes(tmp_path,monkeypatch,model,standard,pro):
    values={name:native_catalog.select(name) for name in (standard,pro)}
    original=deepcopy(values)
    monkeypatch.setattr(native_catalog,'available_models',lambda:[{'deployment':name} for name in values])
    monkeypatch.setattr(native_catalog,'select',lambda name:deepcopy(values[name]))
    choices=model_choices.inventory()
    assert len(choices)==2
    regular=next(row for row in choices if row['key']==model)
    advanced=next(row for row in choices if row['key']==model+'::pro')
    assert advanced['label']==regular['label']+' Pro'
    assert advanced['routes'][0]['label']=='OpenAI API · Pro mode'
    normal={'key':model,'connection':standard,'reasoning':'high'}
    selected={'key':model+'::pro','connection':pro,'reasoning':'high'}
    paths=[model_choices.materialize(choices,choice,tmp_path) for choice in (normal,selected)]
    assert paths[0]!=paths[1]
    standard_value,pro_value=[json.loads(Path(path).read_bytes()) for path in paths]
    assert standard_value['native_profile']['model']==pro_value['native_profile']['model']==model
    assert json.loads(standard_value['native_profile']['parameters_json'])['reasoning']=={'effort':'high'}
    assert json.loads(pro_value['native_profile']['parameters_json'])['reasoning']=={'effort':'high','mode':'pro'}
    assert pro_value['native_credential_env']==standard_value['native_credential_env']=='OPENAI_API_KEY'
    for choice,path in zip((normal,selected),paths):model_choices.remember(tmp_path,choice,path)
    preferences=model_choices.read_preferences(tmp_path)
    assert preferences['models'][model]['connection']==standard
    assert preferences['models'][model+'::pro']['connection']==pro
    assert values==original


def test_gemini_flash_api_and_cli_are_one_model_with_separate_connections(monkeypatch):
    api=native_catalog.select('gemini-api-3-8-flash')
    cli={'native_profile':{'protocol':'vendor-cli','model':'gemini-3.8-flash-high',
         'endpoint':'cli://agy','parameters_json':'{"effort":"high"}'},
         'native_reasoning_models':{'low':'gemini-3.8-flash-low','high':'gemini-3.8-flash-high'}}
    values={'gemini-api-3-8-flash':api,'agy-cli':cli}
    monkeypatch.setattr(native_catalog,'available_models',lambda:[{'deployment':name} for name in values])
    monkeypatch.setattr(native_catalog,'select',lambda name:deepcopy(values[name]))
    choices=model_choices.inventory()
    assert len(choices)==1 and choices[0]['label']=='Gemini 3.8 Flash'
    assert {route['label'] for route in choices[0]['routes']}=={'AGY CLI','Gemini API'}
    value=model_choices.realization(choices,{'key':'gemini-3.8-flash','connection':'gemini-api-3-8-flash','reasoning':'low'})
    assert value['native_profile']['model']=='gemini-3.8-flash'
    assert json.loads(value['native_profile']['parameters_json'])=={'reasoning_effort':'low'}

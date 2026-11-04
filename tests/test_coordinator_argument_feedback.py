"""Schema refusals stay refusals and offer a bounded repair contract."""
import json

import pytest

from compute_plane.native_models import encode
from tare_tools_agent_runtime import EffectResult
from test_conversation_tasks import effect, host  # noqa: F401


@pytest.fixture
def search_host(host,monkeypatch):
    tools,control,root,seen=host
    schema={'type':'object','properties':{'repository':{'type':'string','enum':['repo']},
        'query':{'type':'string','minLength':1}},'required':['repository','query'],
        'additionalProperties':False}
    specs=json.loads(tools.tool_specs_json)
    specs['search']={'description':'Search repository content','parameters':schema}
    tools.names=(*tools.names,'search')
    tools.tool_specs_json=encode(specs)
    def execute(request,_):
        seen.append(request.tool_call.name)
        return EffectResult(request.effect_id,'refused',encode({
            'reason':'capability_arguments_mismatch: missing query, repository',
            'error_type':'ValueError'}))
    monkeypatch.setattr(tools.tools,'execute',execute)
    return tools,control,schema,seen


def test_missing_search_fields_return_the_saved_schema_without_fabricated_arguments(search_host):
    tools,control,schema,seen=search_host
    before=tools.tool_specs_json
    result=tools.execute(effect('search',{}),control)
    body=json.loads(result.output_json)
    assert result.status=='refused' and not result.progressed and seen==['search']
    assert body['reason'].startswith('capability_arguments_mismatch:')
    assert body['correction']=={'tool':'search','parameters':schema}
    assert 'Do not repeat' in body['next'] and 'delegate' in body['next']
    assert 'arguments' not in body['correction']
    assert tools.tool_specs_json==before


@pytest.mark.parametrize('status,body',[
    ('succeeded',{'status':'PARTIAL','reason':'capability_arguments_mismatch: historical'}),
    ('refused',{'reason':'permission denied'}),
    ('failed',{'reason':'capability_arguments_mismatch: not a schema refusal'}),
])
def test_unrelated_results_are_unchanged(search_host,monkeypatch,status,body):
    tools,control,_,_=search_host
    original=EffectResult('effect',status,encode(body))
    monkeypatch.setattr(tools.tools,'execute',lambda *_:original)
    assert tools.execute(effect('search',{}),control) is original


def test_oversized_schema_does_not_expand_a_refusal(search_host):
    tools,control,_,_=search_host
    specs=json.loads(tools.tool_specs_json)
    specs['search']['parameters']['description']='x'*5000
    tools.tool_specs_json=encode(specs)
    result=tools.execute(effect('search',{}),control)
    assert result.status=='refused'
    assert 'correction' not in json.loads(result.output_json)

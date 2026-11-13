"""Delegated API effort uses the same protocol mapping as the public model picker."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from compute_plane.native_models import NativeModelAdapter
from relay.native_generation import deployment_profile
from relay.turn_engine import with_effort
from tare_tools_agent_runtime import ConversationLog,ModelRequest,ProjectionSpec


@pytest.mark.parametrize('protocol,endpoint,credential,params,expected',[
    ('openai-responses','https://api.openai.com/v1','OPENAI_API_KEY',
     {'reasoning':{'effort':'medium','summary':'auto'},'prompt_cache_key':'same-affinity'},
     {'reasoning':{'effort':'high','summary':'auto'},'prompt_cache_key':'same-affinity'}),
    ('openai-chat','https://openrouter.ai/api/v1','OPENROUTER_API_KEY',
     {'reasoning':{'effort':'medium'},'session_id':'same-affinity'},
     {'reasoning':{'effort':'high'},'session_id':'same-affinity'}),
    ('openai-chat','https://generativelanguage.googleapis.com/v1beta/openai','GEMINI_API_KEY',
     {'reasoning_effort':'medium'},{'reasoning_effort':'high'}),
    ('anthropic-messages','https://api.anthropic.com','ANTHROPIC_API_KEY',
     {'output_config':{'effort':'medium'}},{'output_config':{'effort':'high'}}),
])
def test_requested_high_reaches_actual_api_wire_without_changing_saved_profile_or_cache(
        protocol,endpoint,credential,params,expected):
    value={'id':'fixture','runner':'native-http','native_credential_env':credential,
        'native_counter_protocol':'utf8-envelope-estimate-v1','native_profile':{
            'protocol':protocol,'endpoint':endpoint,'model':'fixture','source':'offline fixture',
            'context_window':32768,'output_tokens':2048,'parameters_json':json.dumps(params)}}
    original=deepcopy(value)
    changed=with_effort(value,'high')
    assert value==original and changed is not value
    assert json.loads(changed['native_profile']['parameters_json'])==expected
    adapter=NativeModelAdapter(deployment_profile(changed,'fixture'),SimpleNamespace(endpoint=endpoint),
        system='Stable system',tool_specs_json='{}',counter_protocol='utf8-envelope-estimate-v1')
    log=ConversationLog();log.append({'kind':'user_message','message_id':'u','text':'Do the requested task.'})
    request=ModelRequest('s','s/model/1','Do the requested task.',(),log.project(ProjectionSpec(1,'native-full-v1')))
    wire=adapter.wire(request)
    for key,expected_value in expected.items():assert wire[key]==expected_value


def test_a_policy_effort_maps_to_a_level_the_local_profile_declares():
    from relay.execution_team import reasoning
    from relay.native_catalog import select
    local = select('qwen38-gsq')  # declares low, medium and xhigh
    with pytest.raises(ValueError, match='Unsupported local reasoning level: high'):
        with_effort(local, 'high')  # an explicit choice is not moved
    assert [reasoning(with_effort(local, e, at_least=True)) for e in ('medium', 'high', 'max')] == ['medium', 'xhigh', 'xhigh']
    hosted = select('claude-api-opus-5-5')
    assert with_effort(hosted, 'high', at_least=True) == with_effort(hosted, 'high')


def test_unsupported_connection_keeps_its_exact_parameters_without_adding_a_frontier_call():
    value={'id':'custom','native_profile':{'protocol':'openai-chat','endpoint':'http://localhost:8080/v1',
        'parameters_json':'{"chat_template_kwargs":{"reasoning_effort":"low"}}'}}
    assert with_effort(value,'high')==value

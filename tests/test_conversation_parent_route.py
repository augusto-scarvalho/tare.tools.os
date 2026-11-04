"""An explicitly selected conversation model remains selectable for a separate child."""
from copy import deepcopy
import json

import pytest

from relay import execution_scope,turn_engine,work_units


@pytest.fixture
def api_route(monkeypatch):
    parent={'id':'openai-api','native_credential_env':'OPENAI_API_KEY','native_profile':{
        'protocol':'openai-responses','endpoint':'https://api.openai.com/v1','model':'gpt-6.1-sol',
        'parameters_json':'{"reasoning":{"effort":"high"}}'}}
    seen=[]
    monkeypatch.setattr(turn_engine,'policy',lambda:{'implementer':[['legacy-unavailable']]})
    def available(name,probe=True):
        seen.append(name)
        return (deepcopy(parent),None) if name==parent['id'] else (None,'unavailable')
    monkeypatch.setattr(turn_engine,'available',available)
    return parent,seen


def test_explicit_parent_api_uses_the_normal_availability_and_billing_path(api_route):
    parent,seen=api_route
    assert list(work_units.routes(parent,'openai-api',allow_parent=True))==[('openai-api',parent)]
    assert seen==['openai-api']


@pytest.mark.parametrize('chosen,allow_parent',[('openai-api',False),('other-api',True)])
def test_leaf_or_unadmitted_route_cannot_expand_the_policy(api_route,chosen,allow_parent):
    parent,seen=api_route
    with pytest.raises(ValueError,match='implementer routes'):
        list(work_units.routes(parent,chosen,allow_parent=allow_parent))
    assert seen==[]


def test_unavailable_explicit_parent_does_not_silently_fall_back(api_route,monkeypatch):
    parent,_=api_route
    seen=[]
    monkeypatch.setattr(turn_engine,'available',lambda name,probe=True:seen.append(name) or (None,'credential missing'))
    assert list(work_units.routes(parent,'openai-api',allow_parent=True))==[]
    assert seen==['openai-api']


@pytest.mark.parametrize('restriction',['subscription','pro'])
def test_temporary_restrictions_and_planner_only_pro_still_apply(api_route,monkeypatch,restriction):
    parent,seen=api_route
    if restriction=='subscription':
        monkeypatch.setenv(execution_scope.ENV,json.dumps({'only':'subscription'}))
    else:
        parent['native_profile']['parameters_json']='{"reasoning":{"mode":"pro"}}'
    with pytest.raises(ValueError,match='implementer routes'):
        list(work_units.routes(parent,'openai-api',allow_parent=True))
    assert seen==[]


def test_parent_fallback_never_implicitly_delegates_pro_to_an_implementer(api_route):
    parent,seen=api_route
    parent['native_profile']['parameters_json']='{"reasoning":{"mode":"pro"}}'
    assert list(work_units.routes(parent,allow_parent=True))==[]
    assert seen==['legacy-unavailable']

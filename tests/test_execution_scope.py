"""Launch flags constrain real route admission, without changing stored preferences."""
import json,os
from dataclasses import asdict
import pytest
from relay import execution_scope as scope,native_console,native_installation,native_product
from test_terminal_project_entry import opened  # noqa:F401


def deployment(kind,pro=False):
    value={'id':kind,'native_profile':{'protocol':'openai-chat','endpoint':'http://127.0.0.1:8080/v1',
        'model':'weights','parameters_json':'{}'}}
    if kind=='subscription':value['native_profile'].update(protocol='vendor-cli',endpoint='cli://codex')
    elif kind=='subscription_direct':value['native_profile'].update(protocol='openai-codex-responses',endpoint='https://chatgpt.com/backend-api/codex')
    elif kind=='api':
        value['native_credential_env']='OPENAI_API_KEY'
        value['native_profile'].update(protocol='openai-responses',model='gpt-6.1-sol',
            parameters_json=json.dumps({'reasoning':{'effort':'high',**({'mode':'pro'} if pro else {})}}))
    return value


@pytest.mark.parametrize('only',scope.KINDS)
@pytest.mark.parametrize('kind',(*scope.KINDS,'subscription_direct'))
def test_only_modes_never_admit_another_transport(only,kind):
    assert scope.Scope(only=only).allows(deployment(kind))[0] is (only==kind)


@pytest.mark.parametrize('role',['coordinator','explorer','researcher','implementer','planner','auditor','plan-auditor'])
def test_prefer_local_reserves_external_models_for_planning_and_auditing(role):
    value=scope.Scope(prefer='local')
    assert value.allows(deployment('local'),role)[0] is (role not in scope.REVIEW_ROLES)
    assert value.allows(deployment('api'),role)[0] is (role in scope.REVIEW_ROLES)


@pytest.mark.parametrize('only,prefer,pro',[('local','api',False),('api','subscription',False),
    ('subscription','local',False),('local','',True),('subscription','',True)])
def test_conflicts_are_rejected_before_launch(only,prefer,pro):
    with pytest.raises(ValueError):scope.Scope(only,prefer,pro).validate()


def test_pro_mode_is_limited_to_the_separate_planner():
    value=scope.Scope(pro_planner=True)
    assert value.allows(deployment('api',True),'planner')[0]
    assert not value.allows(deployment('api'),'planner')[0]
    for role in ('coordinator','implementer','auditor','explorer'):
        assert not value.allows(deployment('api',True),role)[0]
        assert value.allows(deployment('api'),role)[0]


def test_scope_lifetime_restores_the_parent_environment(monkeypatch):
    monkeypatch.delenv(scope.ENV,raising=False)
    with scope.activated(scope.Scope(only='local')):
        assert scope.current().only=='local'
        with scope.activated(scope.Scope(only='api')):assert scope.current().only=='api'
        assert scope.current().only=='local'
    assert scope.ENV not in os.environ


@pytest.mark.parametrize('flag',scope.FLAGS)
@pytest.mark.parametrize('alias',[False,True])
def test_public_tare_flags_reach_only_this_console(opened,monkeypatch,flag,alias):
    project,calls=opened
    observed=[]
    class Console:
        def __init__(self,root,**kwargs):observed.append((root,asdict(scope.current())))
        def run(self):pass
    monkeypatch.setattr(native_console,'WorkConsole',Console)
    option='--'+(flag.replace('_','-') if alias else flag)
    assert native_installation.main([option,str(project)])==0
    assert observed[0][0]==project.resolve() and any(observed[0][1].values())
    assert not scope.current().active and not list(project.iterdir())
    assert native_product.console_arguments(['--home','C:/managed',option,str(project)])==[
        '--home','C:/managed','console',option,str(project)]


def test_only_flags_leave_search_and_helper_policy_separate():
    assert scope.Scope(only='subscription').judge('jev')
    assert not scope.Scope(only='subscription').judge('local_judge')
    assert scope.Scope(only='api').judge('jev')
    assert not scope.Scope(only='api').judge('local_judge')
    assert not scope.Scope(only='local').judge('jev')
    assert scope.Scope(only='local').judge('local_judge')
    assert scope.Scope(prefer='local').judge('jev')
    assert all(not scope.Scope(disable_judge=True).judge(name) for name in ('jev','local_judge'))


def test_restore_preserves_parent_payload_verbatim_even_on_error(monkeypatch):
    parent = '{ "only": "local", "disable_judge": true }'
    monkeypatch.setenv(scope.ENV, parent)
    with pytest.raises(RuntimeError):
        with scope.activated(scope.Scope(only='api')):
            assert scope.current().only == 'api'
            raise RuntimeError('fixture')
    assert os.environ[scope.ENV] == parent
    import argparse
    parser = argparse.ArgumentParser()
    scope.add_arguments(parser)
    with pytest.raises(ValueError, match='conflict'):
        scope.from_arguments(parser.parse_args(['--api_only']))
    assert os.environ[scope.ENV] == parent

"""Judge overrides retain exact-command consent, cancellation and sandbox gates."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import time

import pytest
from compute_plane.action_control import ActionRefused
from relay import command_permission,execution_scope as scope,turn_engine
from relay.action_model_assessment import TareJevActionClassifier
from test_command_protection import host,jev_composition  # noqa:F401
from test_action_model_assessment import assessment,jev_host  # noqa:F401


@pytest.mark.parametrize('choice',['once','late','deny','cancel','source_changed'])
def test_disabled_judges_ask_for_the_exact_command_and_run_no_model(jev_composition,tmp_path,monkeypatch,choice):
    h=jev_composition;root=tmp_path/'approvals'
    monkeypatch.setenv(command_permission.ROOT_ENV,str(root))
    monkeypatch.setenv('TARE_SESSION_OPERATOR_MODE','edits')
    def respond():
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            question=command_permission.pending(root)
            if question:
                assert question['argv']==list(h.host.facts().argv)
                if choice=='cancel':h.host.control.cancellation.cancel()
                else:
                    if choice=='source_changed':h.host.executable.write_bytes(b'changed while waiting')
                    if choice=='late':time.sleep(1.5)  # past the action's one-second review window
                    command_permission.answer(root,question['id'],'once' if choice!='deny' else 'deny')
                return
            time.sleep(.01)
        raise AssertionError('No command question arrived.')
    with scope.activated(scope.Scope(disable_judge=True)),ThreadPoolExecutor(1) as executor:
        mediation=h.select('tare',maximum=1)
        if choice=='late':
            h.host.config['command_protection']['review_seconds']=1;mediation=h.host.build()
        reply=executor.submit(respond)
        if choice in ('once','late'):
            result=mediation.execute(h.host.facts(),h.host.effect)
            assert result['command_protection']['review_status']=='human_approved'
            assert result['command_protection']['reserved_calls']==0
        else:
            with pytest.raises(ActionRefused):mediation.execute(h.host.facts(),h.host.effect)
        reply.result(timeout=4)
    assert not h.calls and not h.host.native
    assert h.host.effects==(['effect'] if choice in ('once','late') else [])
    assert not root.exists()


def test_disabled_judges_in_yolo_run_no_judge_and_never_ask(jev_composition,monkeypatch):
    h=jev_composition
    monkeypatch.setenv('TARE_SESSION_OPERATOR_MODE','commands')
    monkeypatch.setattr(command_permission,'approve',lambda *a:pytest.fail('YOLO must not ask'))
    with scope.activated(scope.Scope(disable_judge=True)):
        result=h.select('tare',maximum=1).execute(h.host.facts(),h.host.effect)
    assert not h.calls and result['command_protection']['review_status']=='operator_yolo'
    assert h.host.effects==['effect']


def test_disabled_judges_leave_native_codex_review_in_charge(jev_composition):
    h=jev_composition
    with scope.activated(scope.Scope(disable_judge=True)):
        result=h.select('codex',maximum=1).execute(h.host.facts(),h.host.effect)
    assert not h.calls and len(h.host.native)==1 and h.host.effects==['effect']
    assert result['command_protection']['classifier']=='disabled'


def test_prefer_local_keeps_the_hosted_jev_classifier(jev_composition):
    h=jev_composition
    with scope.activated(scope.Scope(prefer='local')):
        result=h.select('tare',maximum=1).execute(h.host.facts(),h.host.effect)
    assert h.calls==['jev'] and result['command_protection']['classifier']=='jev'


def test_local_only_uses_local_classification_and_no_native_review(jev_composition,monkeypatch):
    h=jev_composition
    monkeypatch.setattr(turn_engine,'local_judge',lambda:'http://local-judge.invalid')
    with scope.activated(scope.Scope(only='local')):
        result=h.select('tare',maximum=1).execute(h.host.facts(),h.host.effect)
    assert h.calls==['jev']  # fake Choice port; the receipt identifies its selected local backend
    assert result['command_protection']['classifier']=='local' and not h.host.native


def test_unavailable_local_judge_asks_instead_of_loading_a_model_or_calling_hosted_jev(jev_composition,monkeypatch):
    h=jev_composition;questions=[]
    monkeypatch.setattr(turn_engine,'local_judge',lambda:None)
    monkeypatch.setattr(command_permission,'approve',lambda *a:questions.append(a[0].action_sha256) or True)
    monkeypatch.setenv('TARE_SESSION_OPERATOR_MODE','edits')
    with scope.activated(scope.Scope(only='local')):
        result=h.select('tare',maximum=1).execute(h.host.facts(),h.host.effect)
    assert not h.calls and len(questions)==1 and h.host.effects==['effect']
    assert result['command_protection']['classifier']=='local_unavailable'


def test_local_classifier_uses_the_pinned_local_model_without_reading_a_hosted_key(jev_host):
    h=jev_host;h.reply['model']=turn_engine.LOCAL_JUDGE
    def forbidden():pytest.fail('No hosted Jev key may be read.')
    classifier=TareJevActionClassifier(journal_factory=lambda:h.journal,custody_factory=nullcontext,
        disclosure_check=lambda r:True,eligibility_check=lambda:True,key_factory=forbidden,
        assess_choice=h.transport,model=turn_engine.LOCAL_JUDGE,provider='local',requires_key=False)
    result=classifier.assess(h.assessment,h.control)
    assert result.status=='OBSERVED' and h.calls[0][1]['model']==turn_engine.LOCAL_JUDGE
    assert classifier.backend_version.startswith(turn_engine.LOCAL_JUDGE)


@pytest.mark.parametrize('temporary',[scope.Scope(only='local'),scope.Scope(disable_judge=True),scope.Scope(prefer='local')])
@pytest.mark.parametrize('model',[None,'jev-1.14.0'])
def test_semantic_judges_obey_the_same_launch_override(monkeypatch,temporary,model):
    import compute_plane.jev_choice_worker as hosted
    import compute_plane.ninfer_litjev as local
    calls=[]
    monkeypatch.setattr(turn_engine,'jev_key',lambda:'offline fixture')
    monkeypatch.setattr('relay.environment.decides',lambda name:scope.current().judge(name))
    monkeypatch.setattr(turn_engine,'local_judge',lambda:'http://fixture' if scope.current().judge('local_judge') else None)
    monkeypatch.setattr(hosted,'assess',lambda *a,**kw:calls.append('jev') or {})
    monkeypatch.setattr(local,'assess',lambda *a,**kw:calls.append('local') or {})
    request={'state':'fixture','question':'Pick?','options':{'a':'A','b':'B'}}
    with scope.activated(temporary):turn_engine.judges(request,local=False,model=model)
    assert calls==([] if temporary.disable_judge else ['local'] if temporary.only=='local' else ['jev'])


@pytest.mark.parametrize('observed',['jev-1.14.0','jev-1.13.0'])
def test_pinned_hosted_judge_observes_its_requested_model_without_changing_other_routes(monkeypatch,observed):
    import compute_plane.jev_choice_worker as hosted
    calls=[]
    monkeypatch.setenv('TARE_JEV_SHADOW','on')
    monkeypatch.setattr(turn_engine,'jev_key',lambda:'offline fixture')
    monkeypatch.setattr('relay.environment.decides',lambda name:True)
    monkeypatch.setattr(turn_engine,'local_judge',lambda:pytest.fail('Hosted-only intent check.'))
    def answer(request,**kwargs):
        calls.append(kwargs)
        return {'choice':'deliver','probabilities':{'deliver':1.0},'usage':{'input_tokens':10},
                'model_identity':{'model':observed}}
    monkeypatch.setattr(hosted,'assess',answer)
    result=turn_engine.judges({'state':'fixture','question':'Outcome?',
        'options':{'deliver':'Deliver','plan':'Plan'}},local=False,model='jev-1.14.0')['jev']
    assert calls==[{'key':'offline fixture','timeout':10,'model':'jev-1.14.0'}]
    if observed=='jev-1.14.0':assert result['model']==observed and result['choice']=='deliver'
    else:assert result['failure']=='ASSESSMENT_CHECKPOINT_CHANGED' and 'choice' not in result

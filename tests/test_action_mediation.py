"""OS assessments are advisory; only the owner permit and exact Kernel guard execute."""
from dataclasses import replace
import hashlib
import json

import pytest
from compute_plane.action_control import ActionAuthorization, ActionRefused, ActionSnapshot, ExactActionGuard
from compute_plane.command_mediation import CommandFacts
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
from relay.action_control import ActionDecision
from relay.action_mediation import CommandActionMediator


def setup(allowed=True):
    context='One disposable fixture effect.'
    facts=CommandFacts('command','s','r','/fixture','a'*64,'b'*64,('python','fixture.py'),'{"argv":["python","fixture.py"]}')
    control=ExecutionControl(BindingRef('w','binding','operator',3,'opaque'),CancellationToken(),200,
        monotonic=lambda:100)
    current=['c'*64]; claims=set(); effects=[]; issued=[]
    def snapshot(facts,live):
        return ActionSnapshot('w','s','e','binding',3,facts.operation,facts.repository,facts.cwd,
            facts.environment_sha256,hashlib.sha256(context.encode()).hexdigest(),current[0],
            'd'*64,facts.grant_sha256,'mediation-only',200,argv=facts.argv,payload_json=facts.payload_json)
    class Broker:
        def assess(self,action,public,live):
            assert public==context and live is control
            return ActionDecision(allowed,'fixture')
    def authorize(action,decision,live):
        issued.append(action.action_sha256)
        return ActionAuthorization('operator','nonce',action.action_sha256,'s','e','binding',3,190)
    def claim(action,permit):
        if action.effect_id in claims:return False
        claims.add(action.effect_id);return True
    guard=ExactActionGuard(lambda a,p:p.issuer=='operator' and p.nonce=='nonce',claim,clock=lambda:100)
    def mediator(**overrides):
        return CommandActionMediator(**{'broker':Broker(),'snapshot_factory':snapshot,
            'context_factory':lambda a,c:context,'authorization_factory':authorize,'guard':guard,
            'control_factory':lambda:control,**overrides})
    def effect():effects.append('once');return {'exit_code':0}
    return facts,control,current,claims,effects,issued,mediator,effect


def test_owner_authorization_and_exact_guard_are_required_even_for_a_positive_assessment():
    facts,_,_,claims,effects,issued,mediator,effect=setup()
    assert mediator().execute(facts,effect)=={'exit_code':0}
    with pytest.raises(ActionRefused):mediator().execute(facts,effect)
    assert effects==['once'] and claims=={'e'} and len(issued)==2


def test_assessment_denial_cannot_mint_or_execute():
    facts,_,_,claims,effects,issued,mediator,effect=setup(False)
    with pytest.raises(ActionRefused):mediator().execute(facts,effect)
    assert not claims and not effects and not issued


def test_snapshot_factory_cannot_approve_different_command_bytes():
    facts,_,_,claims,effects,issued,mediator,effect=setup()
    good=mediator()
    factory=good.snapshot_factory
    bad=mediator(snapshot_factory=lambda facts,c:replace(factory(facts,c),argv=('different',)))
    with pytest.raises(ActionRefused):bad.execute(facts,effect)
    assert not claims and not effects and not issued


def test_source_changed_while_the_owner_mints_is_refused():
    facts,_,current,claims,effects,_,mediator,effect=setup()
    good=mediator();factory=good.authorization_factory
    def changed(*args):
        permit=factory(*args);current[0]='e'*64;return permit
    with pytest.raises(ActionRefused):mediator(authorization_factory=changed).execute(facts,effect)
    assert not claims and not effects


class FiniteReviewedExecution:
    """A finite gate fake: no provider, command runner, or approval inference."""

    def __init__(self, issued, claims, *, outcome='approved', waiting=None):
        self.issued = issued
        self.claims = claims
        self.outcome = outcome
        self.waiting = waiting
        self.starts = []
        self.order = []

    def run(self, action, authorization, control, effect, *, public_context, before_effect):
        assert self.issued[-1] == action.action_sha256 == authorization.action_sha256
        assert self.claims == {action.effect_id}
        assert public_context == 'One disposable fixture effect.'
        self.starts.append((action, authorization, control))
        if self.waiting is not None:
            self.waiting()
        if self.outcome != 'approved':
            raise ActionRefused(self.outcome)
        self.order.append('before_effect')
        before_effect()
        if control.should_stop():
            raise ActionRefused('execution control is stopped')
        self.order.append('effect')
        return effect()


def test_reviewed_execution_passes_the_real_host_result_to_the_handler(monkeypatch):
    facts, control, _, claims, effects, issued, mediator, _ = setup()
    host = mediator()
    claim = host.guard._claim
    claim_calls = []
    def counted_claim(action, permit):
        claim_calls.append(action.effect_id)
        return claim(action, permit)
    monkeypatch.setattr(host.guard, '_claim', counted_claim)
    native = FiniteReviewedExecution(issued, claims)
    real_result = {'exit_code': 7, 'stdout': 'host output', 'stderr': 'host error'}
    handled = []
    delivered = object()
    def effect():
        assert native.order == ['before_effect', 'effect']
        effects.append('once')
        return real_result
    def handler(action, result, live):
        assert result is real_result and live is control
        assert native.starts[0][0] is action
        handled.append(result)
        return delivered
    host = mediator(reviewed_execution=native, result_handler=handler)
    assert host.execute(facts, effect) is delivered
    assert handled == [real_result] and effects == ['once']
    assert claim_calls == ['e'] and len(native.starts) == 1
    assert native.starts[0][2] is control
    with pytest.raises(ActionRefused, match='claim was not granted'):
        host.execute(facts, effect)
    assert claim_calls == ['e', 'e'] and len(issued) == 2
    assert len(native.starts) == 1 and effects == ['once'] and handled == [real_result]


@pytest.mark.parametrize('outcome', ['native denial', 'no correlated native event'])
def test_native_denial_or_missing_event_never_falls_back(outcome):
    facts, _, _, claims, effects, issued, mediator, effect = setup()
    native = FiniteReviewedExecution(issued, claims, outcome=outcome)
    handled = []
    host = mediator(reviewed_execution=native, result_handler=lambda *args: handled.append(args))
    with pytest.raises(ActionRefused, match=outcome):
        host.execute(facts, effect)
    assert len(native.starts) == 1 and claims == {'e'} and len(issued) == 1
    assert native.order == [] and not effects and not handled
    with pytest.raises(ActionRefused, match='claim was not granted'):
        host.execute(facts, effect)
    assert len(native.starts) == 1 and not effects


@pytest.mark.parametrize('change', [
    'source', 'fence', 'permission', 'grant', 'authorization_expiry', 'action_expiry',
])
def test_waiting_changes_are_refused_by_before_effect_without_another_claim(change):
    facts, _, current, claims, effects, issued, mediator, effect = setup()
    original = mediator()
    clock = [100]
    permission = [True]
    changed_fields = {}
    claim_calls = []
    def claim(action, permit):
        claim_calls.append(action.effect_id)
        return original.guard._claim(action, permit)
    guard = ExactActionGuard(
        lambda a, p: permission[0] and original.guard._verify(a, p),
        claim, clock=lambda: clock[0])
    def waiting():
        if change == 'source':
            current[0] = 'e' * 64
        elif change == 'fence':
            changed_fields['fence_seq'] = 4
        elif change == 'permission':
            permission[0] = False
        elif change == 'grant':
            changed_fields['grant_sha256'] = 'e' * 64
        else:
            clock[0] = 190 if change == 'authorization_expiry' else 200
    native = FiniteReviewedExecution(issued, claims, waiting=waiting)
    handled = []
    host = mediator(
        guard=guard, reviewed_execution=native,
        snapshot_factory=lambda f, c: replace(original.snapshot_factory(f, c), **changed_fields),
        result_handler=lambda *args: handled.append(args))
    with pytest.raises(ActionRefused):
        host.execute(facts, effect)
    assert len(native.starts) == 1 and native.order == ['before_effect']
    assert claim_calls == ['e'] and claims == {'e'}
    assert not effects and not handled


def test_cancellation_while_waiting_prevents_the_effect():
    facts, control, _, claims, effects, issued, mediator, effect = setup()
    native = FiniteReviewedExecution(issued, claims, waiting=control.cancellation.cancel)
    with pytest.raises(ActionRefused, match='execution control is stopped'):
        mediator(reviewed_execution=native).execute(facts, effect)
    assert len(native.starts) == 1 and native.order == ['before_effect']
    assert claims == {'e'} and not effects


@pytest.mark.parametrize('stage', ['assessment', 'permit', 'claim'])
def test_native_never_starts_before_host_admission(stage):
    facts, _, _, claims, effects, issued, mediator, effect = setup(stage != 'assessment')
    original = mediator()
    native = FiniteReviewedExecution(issued, claims)
    overrides = {'reviewed_execution': native}
    if stage == 'permit':
        overrides['authorization_factory'] = lambda *args: replace(
            original.authorization_factory(*args), nonce='invalid')
    elif stage == 'claim':
        overrides['guard'] = ExactActionGuard(original.guard._verify, lambda a, p: False, clock=lambda: 100)
    with pytest.raises(ActionRefused):
        mediator(**overrides).execute(facts, effect)
    assert not native.starts and not effects and not claims
    assert len(issued) == (0 if stage == 'assessment' else 1)


@pytest.mark.parametrize('port', [
    object(), type('MissingRun', (), {'run': None})(),
    type('NonCallableRun', (), {'run': 42})(), lambda: None,
])
def test_invalid_reviewed_execution_is_rejected_before_work(port):
    _, _, _, claims, effects, issued, mediator, _ = setup()
    with pytest.raises(ValueError, match='command_reviewed_execution_run_must_be_callable'):
        mediator(reviewed_execution=port)
    assert not claims and not effects and not issued


def test_no_adapter_preserves_the_ordinary_result_handler_path():
    facts, control, _, claims, effects, issued, mediator, _ = setup()
    real_result = object()
    handled = []
    def effect():
        effects.append('once')
        return real_result
    def handler(action, result, live):
        assert result is real_result and live is control
        handled.append(action.action_sha256)
        return 'handled'
    assert mediator(reviewed_execution=None, result_handler=handler).execute(facts, effect) == 'handled'
    assert effects == ['once'] and claims == {'e'} and handled == issued



def test_mandatory_external_review_cannot_be_lost_before_an_effect():
    facts,control,current,claims,effects,issued,mediator,effect=setup()
    class RequiresReview:
        def assess(self,*args):return ActionDecision(True,'escalated',execution_review_required=True)
    with pytest.raises(ActionRefused,match='mandatory execution review'):
        mediator(broker=RequiresReview()).execute(facts,effect)
    assert not claims and not effects and not issued

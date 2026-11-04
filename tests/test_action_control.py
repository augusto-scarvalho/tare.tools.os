"""Finite policy falsifiers using explicit assessment ports; no inference/authority."""
from dataclasses import replace
import hashlib

import pytest
from compute_plane.action_control import ActionSnapshot
from compute_plane.action_assessment import AssessmentObservation
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
from relay.action_control import ActionPolicy, ActionReviewBroker


def setup(classification='REVIEW_REQUIRED', *, mode='tare', required=True, routine=False, fallback=False, max_calls=3):
    context = 'Operator requested one fixture command; no publication or network.'
    action = ActionSnapshot('w','s','e','b',3,'verify','r','/fixture','a'*64,
        hashlib.sha256(context.encode()).hexdigest(),'a'*64,'a'*64,'a'*64,'mediation-only',200.0,
        argv=('python','fixture.py'))
    clock = [100.0]
    control = ExecutionControl(BindingRef('w','b','operator',3,'opaque'), CancellationToken(),199.0,
        monotonic=lambda:clock[0])
    calls, states = [], {'classifier': ('OBSERVED',classification),
                         'tare': ('OBSERVED','APPROVE_RECOMMENDED'),
                         'codex_native': ('OBSERVED','APPROVE_RECOMMENDED')}
    changes = {}
    class Port:
        def __init__(self, name): self.name=name
        def assess(self, request, live):
            calls.append(self.name)
            status, verdict = states[self.name]
            observation = AssessmentObservation(request.stage,request.backend,'fixture',request.request_sha256,
                request.action.action_sha256,request.action.context_sha256,request.action.policy_sha256,
                status,verdict,('FIXTURE',),0.0,'fixture')
            if self.name in changes: return changes[self.name](observation,live)
            return observation
    budget = []
    def reserve(stage, backend, live):
        budget.append((stage,backend))
        return True
    def broker(check=lambda a,c:True, reserve=reserve, reviewers=None):
        policy=ActionPolicy(mode=mode,review_required=required,allow_routine=routine,
                            fallback_on_unavailable=fallback,max_assessments=max_calls)
        return ActionReviewBroker(policy=policy,classifier=Port('classifier'),
            reviewers=reviewers if reviewers is not None else {'tare':Port('tare'),'codex_native':Port('codex_native')},
            deterministic_check=check,reserve_assessment=reserve,clock=lambda:clock[0])
    return action, context, control, clock, calls, states, changes, budget, broker


def test_deterministic_denial_never_spends_an_assessment():
    a,ctx,c,_,calls,_,_,budget,broker=setup()
    result=broker(check=lambda a,c:False).assess(a,ctx,c)
    assert not result.allowed and not calls and not budget and result.authority=='NONE'


@pytest.mark.parametrize('required,routine,expected', [(True,True,['classifier','tare']), (False,False,['classifier','tare']), (False,True,['classifier'])])
def test_routine_triage_only_skips_review_with_host_permission(required,routine,expected):
    a,ctx,c,_,calls,_,_,_,broker=setup('ROUTINE_ELIGIBLE',required=required,routine=routine)
    result=broker().assess(a,ctx,c)
    assert result.allowed and result.authority=='NONE' and calls==expected


def test_classifier_denial_is_terminal():
    a,ctx,c,_,calls,_,_,_,broker=setup('DENY_RECOMMENDED')
    assert not broker().assess(a,ctx,c).allowed and calls==['classifier']


@pytest.mark.parametrize('mode', ['codex_native','both_required'])
def test_denial_does_not_probe_an_alternate_reviewer(mode):
    a,ctx,c,_,calls,states,_,_,broker=setup(mode=mode,fallback=True)
    states['codex_native']=('OBSERVED','DENY_RECOMMENDED')
    result=broker().assess(a,ctx,c)
    assert not result.allowed
    if mode=='codex_native': assert calls==['classifier','codex_native']
    assert len(calls)==len(set(calls))


@pytest.mark.parametrize('mode,fallback,allowed', [('codex_native',False,False),('codex_native',True,True),('both_required',True,False)])
def test_unavailability_fallback_is_explicit_and_both_required_never_downgrades(mode,fallback,allowed):
    a,ctx,c,_,calls,states,_,_,broker=setup(mode=mode,fallback=fallback)
    states['codex_native']=('UNAVAILABLE',None)
    assert broker().assess(a,ctx,c).allowed is allowed
    if mode=='codex_native': assert ('tare' in calls) is fallback


@pytest.mark.parametrize('status', ['ABSTAINED','TIMEOUT','MALFORMED','CANCELLED'])
def test_nonverdict_failure_does_not_trigger_fallback(status):
    a,ctx,c,_,calls,states,_,_,broker=setup(mode='codex_native',fallback=True)
    states['codex_native']=(status,None)
    assert not broker().assess(a,ctx,c).allowed and 'tare' not in calls


def test_observation_for_another_request_never_allows():
    a,ctx,c,_,_,_,changes,_,broker=setup()
    changes['tare']=lambda o,c:replace(o,request_sha256='b'*64)
    assert not broker().assess(a,ctx,c).allowed


def test_late_approval_and_cancellation_never_allow():
    for cancel in (False,True):
        a,ctx,c,clock,_,_,changes,_,broker=setup()
        def late(o,live):
            if cancel: live.cancellation.cancel()
            else: clock[0]=201.0
            return o
        changes['tare']=late
        assert not broker().assess(a,ctx,c).allowed


def test_outer_budget_refusal_prevents_model_calls():
    a,ctx,c,_,calls,_,_,_,broker=setup()
    assert not broker(reserve=lambda *args:False).assess(a,ctx,c).allowed and not calls


def test_cancellation_during_budget_reservation_does_not_dispatch_a_model():
    a,ctx,c,_,calls,_,_,_,broker=setup()
    def reserve(*args):
        c.cancellation.cancel()
        return True
    assert not broker(reserve=reserve).assess(a,ctx,c).allowed and not calls


def test_missing_native_backend_is_unavailable_without_implicit_substitution():
    a,ctx,c,_,calls,_,_,_,broker=setup(mode='codex_native')
    result=broker(reviewers={}).assess(a,ctx,c)
    assert not result.allowed and calls==['classifier']


def test_fallback_respects_the_same_per_action_budget():
    a,ctx,c,_,calls,states,_,_,broker=setup(mode='codex_native',fallback=True,max_calls=2)
    states['codex_native']=('UNAVAILABLE',None)
    assert not broker().assess(a,ctx,c).allowed and calls==['classifier','codex_native']



@pytest.mark.parametrize('classification,allowed,review_needed',[
    ('ROUTINE_ELIGIBLE',True,False),('REVIEW_REQUIRED',True,True),
    ('DENY_RECOMMENDED',False,False),('INSUFFICIENT_EVIDENCE',False,False)])
def test_external_native_review_is_required_only_for_escalation(classification,allowed,review_needed):
    a,ctx,c,clock,calls,states,changes,budget,old=setup(classification)
    original=old();policy=replace(original.policy,mode='codex_native',allow_routine=True,
        review_required=False,external_execution_review=True,pause_on_insufficient=True)
    broker=ActionReviewBroker(policy,original.classifier,{},original.deterministic_check,
        original.reserve_assessment,clock=lambda:clock[0])
    result=broker.assess(a,ctx,c)
    assert result.allowed is allowed and result.execution_review_required is review_needed
    assert calls==['classifier'] and result.authority=='NONE'


def test_native_always_review_policy_cannot_be_skipped_by_routine_classification():
    a,ctx,c,clock,calls,states,changes,budget,old=setup('ROUTINE_ELIGIBLE')
    original=old();policy=replace(original.policy,mode='codex_native',external_execution_review=True,
        review_required=True,pause_on_insufficient=True)
    result=ActionReviewBroker(policy,original.classifier,{},original.deterministic_check,
        original.reserve_assessment,clock=lambda:clock[0]).assess(a,ctx,c)
    assert result.allowed and result.execution_review_required and calls==['classifier']

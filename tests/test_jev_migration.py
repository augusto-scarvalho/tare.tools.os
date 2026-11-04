"""Offline request-construction regressions for the qualified hosted checkpoint."""
import json
import pytest
from relay import turn_engine as engine,work_forms,command_protection

@pytest.fixture
def hosted(monkeypatch):
    from compute_plane import jev_choice_worker as worker
    calls=[];logs=[]
    monkeypatch.delenv('TARE_JEV_SHADOW',raising=False)
    monkeypatch.setattr(engine,'jev_key',lambda:'offline-key')
    monkeypatch.setattr('relay.environment.decides',lambda name:True)
    monkeypatch.setattr(engine,'local_judge',lambda:None)
    monkeypatch.setattr(engine,'judge_log',lambda name,row:logs.append(row))
    def assess(request,**kwargs):
        calls.append((request,kwargs));key=next(iter(request['options']))
        return {'choice':key,'probabilities':{k:float(k==key) for k in request['options']},
                'model_identity':{'model':kwargs['model']},'usage':{'input_tokens':12}}
    monkeypatch.setattr(worker,'assess',assess)
    return worker,calls,logs

def test_long_final_requirement_and_complete_reply_reach_the_judge(hosted):
    _,calls,_=hosted
    request='Contexto irrelevante. '*95+' Não execute; responda apenas sim.'
    engine.unanswered(request,'sim')
    assert json.loads(calls[0][0]['state'])=={'user_request':request,'assistant_final_reply':'sim'}
    assert calls[0][1]['model']=='jev-1.14.0'

def test_oversized_answer_assessment_cannot_escalate_or_spend(hosted):
    _,calls,logs=hosted
    assert engine.unanswered('á'*5000+' preserve a instrução final','Vou fazer depois.') is None
    assert calls==[] and logs[-1]['jev']['failure']=='ASSESSMENT_INPUT_OVERFLOW'
    assert logs[-1]['escalate'] is False

def test_work_form_overflow_never_silently_judges_a_prefix(hosted,monkeypatch):
    _,calls,_=hosted
    monkeypatch.delenv('TARE_WORK_FORM_SHADOW',raising=False)
    verdict=work_forms.judge_form('é'*5000+' somente planejar')()
    assert verdict['failure']=='ASSESSMENT_INPUT_OVERFLOW' and calls==[]

def test_novelty_keeps_the_whole_finding_or_abstains(hosted):
    _,calls,_=hosted
    claim='a'*8500+' REQUEST.md:1: keep this final restriction'
    row={'findings':[claim],'remaining':[],'changed_lines':1,'route':'fixture'}
    engine.judge_fix(row,'Fix only addition.','- return a-b\n+ return a+b',lambda text:None)
    assert row['judges']['novelty'][0]['finding']==claim
    assert row['judges']['novelty'][0]['jev']['failure']=='ASSESSMENT_INPUT_OVERFLOW'
    assert len(calls)==1  # Only the independent proportion question fits.

def test_cited_finding_tail_and_pinned_batch_are_preserved(hosted,monkeypatch,tmp_path):
    worker,_,_=hosted;seen=[]
    (tmp_path/'math.py').write_text('def add(a,b): return a+b\n')
    def many(state,questions,**kwargs):
        seen.append((state,questions,kwargs));return {'f0':0.02},120
    monkeypatch.setattr(worker,'assess_many',many)
    claim='Narrative '*90+' The cited function uploads secrets. math.py:1'
    scores,_=engine.verify_findings([claim],'Only fix addition.','',tmp_path)
    assert scores==[0.02] and claim in seen[0][0] and seen[0][2]['model']=='jev-1.14.0'
    scores,result=engine.verify_findings(['字'*25000+' math.py:1'],'Only fix addition.','',tmp_path)
    assert scores==[None] and result['failure']=='ASSESSMENT_INPUT_OVERFLOW' and len(seen)==1

def test_new_sessions_use_qualified_default_and_old_pins_still_resume():
    new=command_protection.prepare({'review':'tare','classifier':'jev'}, {})
    assert new['jev_model']=='jev-1.14.0'
    old={**new,'jev_model':'jev-1.13.0'}
    assert command_protection.saved(old)['jev_model']=='jev-1.13.0'
    assert command_protection.prepare(old,{})['jev_model']=='jev-1.13.0'
    with pytest.raises(ValueError):command_protection.saved({**old,'jev_model':'jev-preview'})

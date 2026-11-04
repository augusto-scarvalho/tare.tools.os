"""Spend admission, durable uncertainty and actual native host integration."""
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from compute_plane.native_models import ModelProfile
from compute_plane.native_usage import UsageContract, UsageTariff
from relay.native_spend import SpendModel, SpendPolicy, settings
from tare_tools_agent_runtime import (BindingRef, CancellationToken, ContextPolicy, ConversationLog,
    EffectResult, ExecutionControl, ModelFailure, ModelReply, ModelRequest, NativeContext, NativeSession,
    ProjectionSpec, ToolCall)
from test_native_generation import endpoint, deployment
from test_native_agent import setup as agent_setup, Client as AgentClient, deployment as agent_deployment, steps


def tariff(**updates):
    return UsageTariff('USD', 'synthetic fixture, not vendor pricing', 'fixture', json.dumps({
        'uncached_input_tokens':'1', 'cache_read_tokens':'0.1', 'cache_write_tokens':'3',
        'output_tokens':'5', **updates}))


def control():
    return ExecutionControl(BindingRef('w','b','owner',1,'token'),CancellationToken(),100,lambda:0)


@pytest.mark.parametrize('amount', ['invalid', '', 'NaN', 'Infinity', '-1', True, 1])
def test_invalid_money_is_a_configuration_error(amount):
    with pytest.raises(ValueError): SpendPolicy(amount, 'USD')
    with pytest.raises(ValueError): tariff(output_tokens=amount)


class Adapter:
    profile=ModelProfile('bedrock-converse','fixture',1000,100,'fixture','http://fixture')
    profile_digest='a'*64
    counter_protocol='server-template-tokenize'
    def __init__(self, usage=None):
        self.calls=0; self.usage=usage; self.failure=None
    def count(self, request, control):
        return {'status':'observed','method':'fixture','input_tokens':100,'request_sha256':'b'*64,
            'call_id':request.call_id,'context_sha256':request.context.sha256,'request_purpose':request.purpose,
            'profile_digest':self.profile_digest,'context_window':1000,'output_tokens':100}
    def generate(self, request, control):
        self.calls+=1
        if self.failure: raise self.failure
        return ModelReply(text='done',proposes_delivery=True,metadata_json=json.dumps({'usage':self.usage}))


def guard(adapter=None, journal=None, policy=None, rates=None):
    return SpendModel(adapter or Adapter(),journal=journal if journal is not None else ConversationLog(),
        policy=policy or SpendPolicy('0.001','USD',input_margin_tokens=0),tariff=rates or tariff(),
        contract=UsageContract('converse-exclusive','fixture'))


def request(model, number=1):
    req=ModelRequest('s',f's/model/{number}','task',(),ConversationLog().project(ProjectionSpec(1,'test')))
    count=model.count(req,control())
    return replace(req,admitted_request_sha256=count['request_sha256'])


def test_missing_usage_retains_reservation_across_reopen_and_blocks_next_call():
    model=guard();model.generate(request(model),control())
    report=model.report()
    assert report['uncertain_calls']==1 and Decimal(report['committed_amount'])==Decimal('.0008')
    resumed=guard(Adapter(),journal=model.journal)
    with pytest.raises(ModelFailure,match='SPEND_LIMIT') as error:
        resumed.generate(request(resumed,2),control())
    assert error.value.not_started and resumed.adapter.calls==0
    assert resumed.report()['uncertain_calls']==1


@pytest.mark.parametrize('layout', ['responses-inclusive', 'anthropic-exclusive', 'openrouter-inclusive', 'openrouter-responses-inclusive'])
def test_api_admission_reserves_the_higher_cache_write_rate_before_dispatch(layout):
    adapter = Adapter()
    model = SpendModel(adapter, journal=ConversationLog(),
        policy=SpendPolicy('0.0007', 'USD', input_margin_tokens=0), tariff=tariff(),
        contract=UsageContract(layout, 'explicit write-rate fixture'))
    with pytest.raises(ModelFailure, match='SPEND_LIMIT'):
        model.generate(request(model), control())
    assert adapter.calls == 0


def test_reported_provider_charge_settles_and_detects_reservation_overrun():
    adapter = Adapter({'cost': .0009})
    adapter.profile = replace(adapter.profile, protocol='openai-chat')
    model = SpendModel(adapter, journal=ConversationLog(), policy=SpendPolicy('0.001', 'USD', input_margin_tokens=0),
                       tariff=tariff(), contract=UsageContract('openrouter-inclusive', 'provider fixture'))
    model.generate(request(model), control())
    report = model.report()
    assert Decimal(report['committed_amount']) == Decimal('.0009')
    assert report['uncertain_calls'] == 0 and report['reservation_overruns']
    with pytest.raises(ModelFailure, match='SPEND_RESERVATION_OVERRUN'):
        model.generate(request(model, 2), control())
    assert adapter.calls == 1


def test_actual_cost_refunds_unused_prediction_and_duplicate_dispatch_is_refused():
    usage={'inputTokens':100,'outputTokens':10,'cacheReadInputTokens':0,'cacheWriteInputTokens':0}
    model=guard(Adapter(usage));req=request(model);model.generate(req,control())
    assert Decimal(model.report()['committed_amount'])==Decimal('.00015')
    with pytest.raises(ModelFailure,match='ALREADY_RESERVED'):
        model.generate(req,control())
    model.generate(request(model,2),control())
    assert model.adapter.calls==2 and Decimal(model.report()['known_amount'])==Decimal('.00030')


def test_partial_usage_keeps_the_larger_known_cost_and_remains_uncertain():
    model=guard(Adapter({'inputTokens':1000,'outputTokens':10}),
                policy=SpendPolicy('1','USD',input_margin_tokens=0))
    model.generate(request(model),control())
    report=model.report()
    assert report['uncertain_calls']==1 and Decimal(report['known_amount'])==Decimal('.00105')
    assert report['committed_amount']==report['known_amount']
    assert report['reservation_overruns']==['s/model/1']


@pytest.mark.parametrize('failure',[TimeoutError(),ModelFailure('HTTP_429'),KeyboardInterrupt()])
def test_uncertain_provider_failure_keeps_full_reservation(failure):
    adapter=Adapter();adapter.failure=failure;model=guard(adapter)
    with pytest.raises(type(failure)): model.generate(request(model),control())
    assert Decimal(model.report()['committed_amount'])==Decimal('.0008')
    assert model.report()['uncertain_calls']==1


def test_proven_not_started_refunds_reservation():
    adapter=Adapter();adapter.failure=ModelFailure('HTTP_NOT_DISPATCHED',not_started=True);model=guard(adapter)
    with pytest.raises(ModelFailure):model.generate(request(model),control())
    assert model.report()['committed_amount']=='0' and model.report()['uncertain_calls']==0


def test_uncertain_settlement_write_never_refunds_and_reservation_failure_never_dispatches():
    class FailingLog(ConversationLog):
        fail='spend_reserved'
        def append(self, event):
            if event['kind']==self.fail:raise OSError('storage unavailable')
            return super().append(event)
    log=FailingLog();adapter=Adapter({'inputTokens':1,'outputTokens':1,'cacheReadInputTokens':0,'cacheWriteInputTokens':0})
    model=guard(adapter,log)
    with pytest.raises(OSError):model.generate(request(model),control())
    assert adapter.calls==0
    log.fail='spend_settled'
    with pytest.raises(OSError):model.generate(request(model),control())
    assert adapter.calls==1 and model.report()['uncertain_calls']==1


def test_tariff_or_policy_change_cannot_reset_existing_budget():
    model=guard();model.generate(request(model),control())
    with pytest.raises(ValueError,match='identity_changed'):
        guard(journal=model.journal,policy=SpendPolicy('1','USD',input_margin_tokens=0))
    with pytest.raises(ValueError,match='identity_changed'):
        guard(journal=model.journal,rates=tariff(output_tokens='4'))


def test_observed_reservation_overrun_is_visible_and_stops_further_requests():
    model=guard(Adapter({'inputTokens':999,'outputTokens':999,'cacheReadInputTokens':0,'cacheWriteInputTokens':0}),
                policy=SpendPolicy('1','USD',input_margin_tokens=0))
    model.generate(request(model),control())
    assert model.report()['reservation_overruns']==['s/model/1']
    with pytest.raises(ModelFailure,match='OVERRUN'):model.generate(request(model,2),control())
    assert model.adapter.calls==1


@pytest.mark.parametrize('allow',[False,True])
def test_estimated_input_requires_separate_spend_opt_in(allow):
    model=guard(policy=SpendPolicy('.001','USD',input_margin_tokens=0,allow_estimated_counts=allow))
    req=request(model);model.last_count.update(status='estimated',input_tokens=None,estimated_input_tokens=100)
    if allow:model.generate(req,control())
    else:
        with pytest.raises(ModelFailure,match='COUNT_UNAVAILABLE'):model.generate(req,control())
    assert model.adapter.calls==int(allow)


def test_unknown_tariff_or_changed_wire_blocks_without_dispatch():
    model=guard(rates=tariff(cache_write_tokens=None))
    with pytest.raises(ModelFailure,match='TARIFF_INCOMPLETE'):model.generate(request(model),control())
    model=guard();req=request(model)
    with pytest.raises(ModelFailure,match='IDENTITY_MISMATCH'):
        model.generate(replace(req,admitted_request_sha256='c'*64),control())
    assert model.adapter.calls==0


@pytest.mark.parametrize('limit,expected_calls,reason',[('.0008',2,'SPEND_LIMIT'),('.002',4,'DELIVERY_PROPOSED')])
def test_compaction_uses_the_same_budget_and_denied_summary_is_not_dispatched(limit,expected_calls,reason):
    class LoopAdapter(Adapter):
        def count(self, req, ctl):
            row=super().count(req,ctl);events=req.context.events()
            row['input_tokens']=(400 if req.purpose=='summary' else 300 if any(e['kind']=='context_activated' for e in events)
                else 850 if sum(e['kind']=='model_returned' for e in events)>=2 else 100)
            return row
        def generate(self, req, ctl):
            self.calls+=1
            if req.purpose=='summary':return ModelReply(text='earlier evidence')
            if self.calls<=2:return ModelReply(tool_calls=(ToolCall(str(self.calls),'read','{}'),))
            return ModelReply(text='done',proposes_delivery=True)
    class Tools:
        def execute(self, req, ctl):return EffectResult(req.effect_id,'succeeded','{}',True)
    model=guard(LoopAdapter(),policy=SpendPolicy(limit,'USD',input_margin_tokens=0),
        rates=tariff(cache_write_tokens='1',cache_read_tokens='1',output_tokens='1'))
    log=ConversationLog()
    result=NativeSession(session_id='s',instruction='task',capabilities=('read',),journal=log,
        tools=Tools(),model=model,context=NativeContext(ContextPolicy(1000,100,20),counter=model.count,summarizer=model)).run(control())
    assert result.reason==reason and model.adapter.calls==expected_calls
    if expected_calls==2:
        failed=[r.event() for r in log.snapshot().records if r.event()['kind']=='context_summary_failed']
        assert failed[0]['not_started'] is True


def configured(endpoint, limit='1'):
    from dataclasses import asdict
    dep=deployment(endpoint,'bedrock-converse')
    dep['native_usage_contract']={'layout':'converse-exclusive','source':'fixture'}
    dep['native_usage_tariff']=asdict(tariff())
    dep['native_spend_policy']={'max_amount':limit,'currency':'USD'}
    return dep


def test_text_host_refuses_before_generation_and_reports_known_zero_charge(endpoint,tmp_path):
    from relay.native_generation import generate_text
    dep=configured(endpoint,'0')
    ok,_,receipt=generate_text(dep,'fixture-model','task',10,attempt_dir=tmp_path)
    assert not ok and receipt['failure_class']=='SPEND_LIMIT' and receipt['generation_calls']==0
    assert all(path.endswith('/count-tokens') for path,_,_ in endpoint['calls'])
    assert receipt['cost']['complete'] and receipt['cost']['known_amount']=='0'


def test_text_stages_share_reservations_despite_different_system_prompts(endpoint,tmp_path):
    from relay.native_generation import generate_text
    from compute_plane.native_journal import NativeJournal
    dep=configured(endpoint,'.004');journal=NativeJournal(tmp_path/'spend.jsonl')
    first=generate_text(dep,'fixture-model','task',10,system='Map sources.',spend_journal=journal)
    second=generate_text(dep,'fixture-model','task',10,system='Reduce findings.',spend_journal=journal)
    assert first[0] and not second[0] and second[2]['failure_class']=='SPEND_LIMIT'
    assert sum(path.endswith('/converse') for path,_,_ in endpoint['calls'])==1
    assert second[2]['spend']['uncertain_calls']==1


def test_text_transport_proven_not_started_has_zero_cost_and_releases_reserve(endpoint,monkeypatch):
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    from relay.native_generation import generate_text
    original = NativeHttpClient.post_observed
    def post(self, path, payload, ctl):
        if path.endswith('/converse'):
            raise NativeTransportInterrupted('not dispatched', {'started':False,'cleanup_verified':True})
        return original(self, path, payload, ctl)
    monkeypatch.setattr(NativeHttpClient,'post_observed',post)
    ok,_,receipt=generate_text(configured(endpoint),'fixture-model','task',10)
    assert not ok and receipt['failure_class']=='HTTP_NOT_DISPATCHED'
    assert receipt['generation_not_started'] and receipt['spend']['uncertain_calls']==0
    assert receipt['cost']['complete'] and receipt['cost']['known_amount']=='0'
    assert all(path.endswith('/count-tokens') for path,_,_ in endpoint['calls'])


def test_wrong_served_model_cannot_be_priced_with_the_requested_tariff(endpoint):
    from dataclasses import asdict
    from relay.native_generation import generate_text
    dep=deployment(endpoint,'openai-chat')
    dep.update(native_usage_contract={'layout':'chat-inclusive','source':'fixture'},
               native_usage_tariff=asdict(tariff()), native_spend_policy={'max_amount':'1','currency':'USD'})
    endpoint.update(model='wrong-model', usage={'prompt_tokens':10,'completion_tokens':5,
        'prompt_tokens_details':{'cached_tokens':0}})
    ok,_,receipt=generate_text(dep,'fixture-model','task',10)
    assert not ok and receipt['failure_class']=='SERVED_MODEL_MISMATCH'
    assert not receipt['cost']['complete'] and receipt['cost']['currency'] is None
    assert receipt['spend']['uncertain_calls']==1


def test_spend_settings_require_explicit_compatible_tariff(endpoint):
    dep=configured(endpoint);dep.pop('native_usage_tariff')
    with pytest.raises(ValueError):settings(dep)
    dep=configured(endpoint);dep['native_spend_policy']['currency']='BRL'
    with pytest.raises(ValueError):settings(dep)


def test_research_map_reduce_shares_a_durable_spend_scope(endpoint,tmp_path):
    import time
    from relay.research import native_completion, MAP_PROMPT
    from relay.native_generation import NativeGenerationError
    receipts=[]
    complete=native_completion(configured(endpoint,'.004'),receipts,job=tmp_path,
                               started=time.monotonic(),timeout=10,max_calls=3)
    assert complete([{'content':MAP_PROMPT},{'content':'source'}])=='{"answer":42}'
    with pytest.raises(NativeGenerationError,match='SPEND_LIMIT'):
        complete([{'content':'Reduce findings.'},{'content':'findings'}])
    assert [r['generation_calls'] for r in receipts]==[1,0] and (tmp_path/'spend.jsonl').exists()


def test_native_follow_up_reuses_spend_after_reopen_and_does_not_charge_denied_generation(agent_setup):
    from dataclasses import asdict
    from relay import native_agent
    config,custody,root=agent_setup
    dep=agent_deployment('bedrock-converse')
    dep['native_usage_tariff']=asdict(tariff(cache_write_tokens='1',cache_read_tokens='1',output_tokens='1'))
    dep['native_spend_policy']={'max_amount':'.0016','currency':'USD'}
    first=AgentClient('bedrock-converse',steps())
    result=native_agent.run_session(config,dep,custody,'Change and verify a.py',client=first)
    assert result['status']=='PROPOSED' and first.generations==4
    assert Decimal(result['spend']['known_amount'])==Decimal('.0004')
    second=AgentClient('bedrock-converse',[])
    result=native_agent.run_session(None,None,custody,None,resume=True,
        follow_up='Review it again.',message_id='review-1',client=second)
    assert result['status']=='BLOCKED' and result['reason']=='SPEND_LIMIT'
    assert second.generations==0 and Decimal(result['spend']['committed_amount'])==Decimal('.0004')
    assert result['usage_coverage']['not_started']==1
    assert result['cost']['unpriced_or_partial_calls']==0
    assert result['tokens']['input_tokens']['unknown_calls']==0

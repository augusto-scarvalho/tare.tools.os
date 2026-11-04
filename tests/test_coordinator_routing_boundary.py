"""Bound pre-planning discovery by journaled effects, including ordinary resume."""
from dataclasses import asdict
import json

import pytest

from compute_plane.native_journal import NativeJournal
from relay import conversation_tasks as tasks
from test_conversation_tasks import effect, host, intake, planned, user_reply  # noqa: F401


def reads(host, count=3):
    tools=host[0]
    journal=NativeJournal(tools.root/'journal.jsonl')
    if not journal.snapshot().records:
        journal.append({'kind':'session_started','session_id':tools.session_id})
    for i in range(count):
        request=effect('read',{'repository':'repo','path':'a.py'},key='read-'+str(i))
        journal.append({'kind':'effect_requested','effect_id':request.effect_id,
                        'tool_call':asdict(request.tool_call)})
    return journal


def test_a_long_discovery_requires_intake_before_dispatch_and_survives_resume(host):
    tools,control,_,seen=host
    reads(host,tasks.QUESTION_LOOKUPS)
    tools=tasks.ConversationTools(tools.tools,tools.root,tools.state,'resumed')
    result=tools.execute(effect('read',{'repository':'repo','path':'a.py'},key='one-more'),control)
    assert result.status=='refused' and seen==[]
    body=json.loads(result.output_json)
    assert body['required_tool']=='demand' and 'intended outcome' in body['next']
    assert tasks.current(tasks.load(tools.root)) is None


def test_a_question_reads_like_a_chat_and_product_help_needs_no_intake(host):
    tools,control,_,seen=host
    reads(host,tasks.QUESTION_LOOKUPS-1)  # list, README, two parts of a file, tests... without demand
    result=tools.execute(effect('read',{'repository':'repo','path':'a.py'},key='last'),control)
    assert result.status=='succeeded' and seen==['read']
    reads(host,3)
    assert tools.execute(effect('product_help',{'topic':'workflow'}),control).status=='succeeded'


@pytest.mark.parametrize('intent',['plan','deliver'])
def test_recorded_actionable_intent_routes_to_planner_instead_of_unbounded_reads(host,intent):
    tools,control,_,seen=host
    intake(host,intent=intent);reads(host)
    result=tools.execute(effect('read',{'repository':'repo','path':'a.py'},key='fourth'),control)
    assert result.status=='refused' and seen==[]
    assert json.loads(result.output_json)['required_tool']=='delegate_plan'
    assert tools.execute(effect('delegate_plan',{'instruction':'Produce the requested plan.'},key='planner'),control).status=='succeeded'
    assert seen==['delegate_plan']


@pytest.mark.parametrize('intent',['answer','investigate'])
def test_readonly_questions_keep_discovery_after_recording_their_intent(host,intent):
    tools,control,_,seen=host
    intake(host,intent=intent);reads(host,5)
    assert tools.execute(effect('read',{'repository':'repo','path':'a.py'},key='sixth'),control).status=='succeeded'
    assert seen==['read']


def test_saved_plan_allows_targeted_fact_reads_and_new_user_input_has_own_allowance(host):
    tools,control,_,seen=host
    intake(host);planned(host);reads(host,5)
    assert tools.execute(effect('read',{'repository':'repo','path':'a.py'},key='sixth'),control).status=='succeeded'
    user_reply(host,'Explain the relevant source without editing.')
    assert tools.execute(effect('read',{'repository':'repo','path':'a.py'},key='new-read'),control).status=='succeeded'
    assert seen==['read','read']


def test_journaled_current_and_later_parallel_requests_do_not_consume_earlier_allowance(host):
    tools,control,_,seen=host
    journal=reads(host,tasks.QUESTION_LOOKUPS-1)
    third=effect('read',{'repository':'repo','path':'a.py'},key='third')
    fourth=effect('read',{'repository':'repo','path':'a.py'},key='fourth')
    for request in (third,fourth):
        journal.append({'kind':'effect_requested','effect_id':request.effect_id,'tool_call':asdict(request.tool_call)})
    assert tools.execute(third,control).status=='succeeded'
    assert tools.execute(fourth,control).status=='refused'
    assert seen==['read']

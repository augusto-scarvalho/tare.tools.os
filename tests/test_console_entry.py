import asyncio
import json
from pathlib import Path
from threading import Event

import pytest
from tare_tools_agent_runtime import CancellationToken
from textual.widgets import Button, Input, Select, TextArea

from relay import console_entry
from relay.console_backend import Backend, Selection, command
from relay.native_console import WorkConsole
from test_native_console import FakeBackend
from test_native_generation import endpoint, deployment


@pytest.mark.parametrize('flow', ['session', 'explore', 'plan', 'research', 'work'])
def test_explicit_flow_has_no_model_and_preserves_operator_text(tmp_path, flow):
    project=tmp_path/'project';project.mkdir()
    body='work-one' if flow=='work' else 'Inspect café https://example.org/docs'
    selected=Selection(str(project),prompt='/'+flow+' '+body)
    def forbidden(*a, **k):pytest.fail('Explicit command called a model')
    result=console_entry.propose(selected,CancellationToken(),directory=tmp_path/'entry',generate=forbidden)
    assert result['status']=='PROPOSED' and result['generation_calls']==0
    assert result['demand']==body
    choice=console_entry.realize(selected,result)
    assert choice.prompt==('' if flow=='work' else body)
    if flow=='explore':
        assert choice.mode=='session' and choice.role=='explorer'
        args=command(choice)
        assert args[args.index('--role')+1]=='explorer'
    assert not list(project.iterdir())


@pytest.mark.parametrize('protocol',['openai-chat','bedrock-converse'])
def test_interpreter_uses_real_codec_once_and_retains_usage(tmp_path,endpoint,protocol):
    project=tmp_path/'project';project.mkdir()
    profile=tmp_path/'profile.json';profile.write_text(json.dumps(deployment(endpoint,protocol)))
    endpoint['text']=json.dumps({'flow':'explore','reason':'Read-only investigation.','question':''})
    selected=Selection(str(project),profile=str(profile),prompt='Explain the failure without editing.')
    result=console_entry.propose(selected,CancellationToken(),directory=tmp_path/'entry')
    assert result['status']=='PROPOSED' and result['flow']=='explore'
    assert result['generation_calls']==1 and result['tokens']=={'input':None,'output':None}
    calls=[c for c in endpoint['calls'] if c[0].endswith(('/completions','/converse'))]
    assert len(calls)==1
    assert not calls[0][1].get('tools') and 'toolConfig' not in calls[0][1]
    assert result['demand']==selected.prompt
    assert (tmp_path/'entry/proposal.json').exists() and not list(project.iterdir())


@pytest.mark.parametrize('content',[
    '{"flow":"shell","reason":"Run this","question":""}',
    '{"flow":"work","reason":"Invented ID","question":""}',
    '{"flow":"session","reason":"ok","question":"","command":"rm -rf /"}',
    '{"flow":"session","reason":"ok","question":"Are you sure?"}',
    'not JSON'])
def test_invalid_model_output_never_starts_or_silently_retries(tmp_path,content):
    project=tmp_path/'project';project.mkdir();calls=[]
    def generate(*a,**k):
        calls.append(1);return True,content,{'generation_calls':1,'usage':None}
    result=console_entry.propose(Selection(str(project),prompt='Help with this.'),CancellationToken(),
                                  directory=tmp_path/'entry',generate=generate)
    assert result['status']=='UNAVAILABLE' and len(calls)==1
    assert result['generation_calls']==1
    assert not list(project.iterdir())


def test_missing_research_urls_asks_and_followup_preserves_original_demand(tmp_path):
    project=tmp_path/'project';project.mkdir()
    def generate(*a,**k):
        return True,json.dumps({'flow':'research','reason':'Web research.','question':''}),{'generation_calls':1,'usage':None}
    first=console_entry.propose(Selection(str(project),prompt='/research Compare caching approaches.'),
                               CancellationToken(),directory=tmp_path/'first',generate=generate)
    assert first['status']=='NEEDS_INPUT'
    second=console_entry.propose(Selection(str(project),prompt='https://example.org/doc'),CancellationToken(),
              history=first['messages'],questions=(first['question'],),directory=tmp_path/'second',generate=generate)
    assert second['status']=='PROPOSED' and 'Compare caching' in second['demand']
    assert second['urls']==['https://example.org/doc']


async def until(pilot,condition,seconds=10):
    """Pilot.click can return before the button's Pressed message reaches the app (it did on a loaded Windows
    runner): `busy` is still False then, so wait for the click's effect itself."""
    deadline=asyncio.get_running_loop().time()+seconds
    while not condition() and asyncio.get_running_loop().time()<deadline:await pilot.pause(.02)
    assert condition()


def test_conversation_default_entry_preview_and_stale_choice(tmp_path,monkeypatch):
    monkeypatch.setattr(console_entry,'storage_root',lambda:tmp_path/'storage')
    project=tmp_path/'project';project.mkdir()
    class EntryBackend(FakeBackend):
        propose_entry=Backend.propose_entry
    async def scenario():
        backend=EntryBackend();app=WorkConsole(project,backend=backend,initial_mode='conversation')
        async with app.run_test(size=(130,46)) as pilot:
            await pilot.pause();app.query_one('#prompt',TextArea).load_text('/explore Explain a.py without editing.')
            await pilot.click('#start')
            await until(pilot,lambda:not app.busy and app.entry_proposal)
            assert not backend.launches
            app.query_one('#prompt',TextArea).load_text('/session Different task')
            await pilot.click('#start-suggested')
            await until(pilot,lambda:app.entry_proposal is None)
            assert not backend.launches
            app.query_one('#prompt',TextArea).load_text('/explore Explain a.py without editing.')
            await pilot.click('#start')
            await until(pilot,lambda:not app.busy and app.entry_proposal)
            await pilot.click('#start-suggested')
            await until(pilot,lambda:backend.launches)
            assert len(backend.launches)==1
            args=backend.launches[0];assert args[args.index('--role')+1]=='explorer'
            assert args[args.index('--instruction')+1]=='Explain a.py without editing.'
            app.action_cancel()
            while app.busy:await pilot.pause(.02)
    asyncio.run(scenario())


def test_cancel_late_interpretation_cannot_enable_execution(tmp_path):
    ready,release=Event(),Event()
    class EntryBackend(FakeBackend):
        def propose_entry(self,selection,cancellation,history,questions=()):
            ready.set();release.wait(5)
            return {'status':'PROPOSED','flow':'session','reason':'late','directory':'saved','tokens':None}
    async def scenario():
        backend=EntryBackend();app=WorkConsole(tmp_path,backend=backend,initial_mode='conversation')
        async with app.run_test(size=(120,40)) as pilot:
            await pilot.pause();app.query_one('#prompt',TextArea).load_text('Do something.')
            await pilot.click('#start')
            while not ready.is_set():await pilot.pause(.01)
            app.action_cancel();release.set()
            while app.busy:await pilot.pause(.02)
            assert app.entry_proposal is None and not backend.launches
            assert app.query_one('#start-suggested',Button).disabled
    asyncio.run(scenario())


def test_clarification_keeps_question_and_answer_roles(tmp_path):
    project=tmp_path/'project';project.mkdir();prompts=[]
    def generate(dep,model,prompt,*args,**kwargs):
        prompts.append(json.loads(prompt))
        return True,json.dumps({'flow':'plan','reason':'Operator confirmed planning.','question':''}),{'generation_calls':1,'usage':None}
    result=console_entry.propose(Selection(str(project),prompt='Sim, só planejar.'),CancellationToken(),
        history=('Quero organizar as melhorias.',),questions=('Você quer um plano antes de implementar?',),
        directory=tmp_path/'entry',generate=generate)
    assert result['status']=='PROPOSED'
    assert prompts[0]['clarification_questions']==['Você quer um plano antes de implementar?']
    assert 'operator_answer' in result['demand'] and 'Sim, só planejar.' in result['demand']
    assert 'questions are not additional requirements' in result['demand']

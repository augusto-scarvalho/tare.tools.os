"""Keyboard and inline decisions through the actual Textual event path."""
import asyncio

import pytest
from textual.app import App
from textual.widgets import TextArea

from relay.console_output import Transcript, StreamedReply, ExecutionResult
from relay.execution_permission_panel import ExecutionPermissionPanel
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def test_approval_frontend_is_available_on_new_and_resumed_public_chat(tmp_path):
    from relay.console_backend import Selection,command
    grants=tmp_path/'console/folder-grants/project/grants.json'
    selected=Selection(str(tmp_path/'project'),mode='session',prompt='Implement the change.',folder_grants=str(grants))
    (tmp_path/'project').mkdir()
    args=command(selected,assistant=True)
    assert args[args.index('--execution-permissions')+1]==str(tmp_path/'console')
    session=tmp_path/'session';session.mkdir()
    import json
    (session/'native-agent.json').write_text(json.dumps({'config':{'repositories':{'project':selected.root}}}))
    selected.session=str(session)
    resumed=command(selected,True,assistant=True)
    assert '--resume' in resumed and resumed[resumed.index('--execution-permissions')+1]==str(tmp_path/'console')


@pytest.mark.parametrize('key',['enter','ctrl+enter'])
def test_composer_send_key_starts_exactly_once_and_not_a_newline(tmp_path,key):
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(120,40)) as pilot:
            await app.workers.wait_for_complete();await pilot.pause()
            prompt=app.query_one('#prompt',TextArea)
            prompt.focus();prompt.load_text('Explain this project.');await pilot.pause()
            await pilot.press(key);await pilot.pause()
            backend.cancel(backend.process)
            await app.workers.wait_for_complete()
            assert len(backend.launches)==1
            sent=backend.launches[0][backend.launches[0].index('--instruction')+1]
            assert sent.endswith('Operator message:\nExplain this project.') and not prompt.text
    asyncio.run(scenario())


def test_explicit_newlines_stay_in_draft_and_modal_enter_does_not_send(tmp_path):
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(120,40)) as pilot:
            await app.workers.wait_for_complete();await pilot.pause()
            prompt=app.query_one('#prompt',TextArea)
            prompt.focus();await pilot.press('a','shift+enter','b','ctrl+j','c');await pilot.pause()
            assert prompt.text=='a\nb\nc' and not backend.launches
            answers=[]
            app.push_screen(ExecutionPermissionPanel({'objective':'Implement the saved task.',
                'project':str(tmp_path),'paths':['new.py'],'checks':{}}),answers.append)
            await pilot.pause();await pilot.press('enter');await pilot.pause()
            assert answers==['once'] and not backend.launches and prompt.text=='a\nb\nc'
    asyncio.run(scenario())


@pytest.mark.parametrize('final',['Hello.','The final answer differs.'])
def test_final_answer_replaces_only_its_identical_stream_preview(final):
    class Console(App):
        def compose(self): yield Transcript(id='output')
    async def scenario():
        app=Console()
        async with app.run_test() as pilot:
            transcript=app.query_one(Transcript)
            transcript.stream({'channel':'text','call_id':'one','text':'Hello.'})
            await pilot.pause()
            transcript.finish_stream()
            transcript.write({'kind':'execution-result','status':'PROPOSED','body':final,
                'notice':'No new check.','attention':False,'details':''})
            await pilot.pause()
            assert len(transcript.query(ExecutionResult))==1
            assert len(transcript.query(StreamedReply))==(0 if final=='Hello.' else 1)
            transcript.write('You: Another greeting.')
            transcript.stream({'channel':'text','call_id':'two','text':'Hello.'})
            await pilot.pause()
            transcript.write({'kind':'execution-result','status':'PROPOSED','body':'Hello.',
                'notice':'','attention':False,'details':''})
            await pilot.pause()
            assert len(transcript.query(ExecutionResult))==2
    asyncio.run(scenario())

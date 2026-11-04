import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
from threading import Event, Thread

from PIL import Image
import pytest
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from relay.console_attachments import AttachmentPicker, draft_store
from relay.console_backend import Backend, Selection
from relay.native_console import WorkConsole
from test_native_console import FakeBackend
from test_native_agent import Client, deployment
from test_console_slash import edit as edit_slash, settle as settle_slash, shown as shown_slash


def png(color='red'):
    output=io.BytesIO()
    Image.new('RGB',(40,20),color).save(output,format='PNG')
    return output.getvalue()


async def pick(app,pilot,path):
    await pilot.click('#attach');await pilot.pause()
    assert isinstance(app.screen,AttachmentPicker)
    field=app.screen.query_one('#attachment-location',Input)
    field.focus();field.value=str(path)
    await pilot.press('enter')
    return await preview_ready(app,pilot)


async def preview_ready(app,pilot):
    for _ in range(100):
        await pilot.pause(.05)
        if app.screen.selected is not None:break
    assert app.screen.selected is not None, app.screen.query_one('#attachment-picker-status',Static).render()
    return app.screen.selected


@pytest.mark.parametrize('size',[(80,24),(130,44)])
def test_snapshot_picker_preview_draft_reopen_and_workspace_isolation(tmp_path,monkeypatch,size):
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'screen shot.png';source.write_bytes(png())
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    async def scenario():
        app=WorkConsole(project,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt',TextArea).load_text('Explain this screenshot.')
            ref=await pick(app,pilot,source)
            assert ref.sha256==hashlib.sha256(png()).hexdigest()
            assert '40×20' in str(app.screen.query_one('#attachment-preview-info',Static).render())
            assert app.screen.query_one('#attachment-confirm').region.bottom<=size[1]
            await pilot.press('ctrl+enter','f2','f3')
            assert isinstance(app.screen,AttachmentPicker) and not app.backend.launches
            source.write_bytes(png('blue'))
            await pilot.click('#attachment-confirm');await app.workers.wait_for_complete();await pilot.pause()
            assert app.attachments[0]['sha256']==ref.sha256
            assert app.query_one('#attachment-chips').display
            await pilot.click('#attachment-preview-0');await app.workers.wait_for_complete();await pilot.pause()
            await preview_ready(app,pilot)
            assert isinstance(app.screen,AttachmentPicker) and app.screen.selected==ref
            await pilot.press('escape');await pilot.pause()
            app.save_demand();identity=app.demand_id
            assert app.demand_state['snapshot']['selection']['attachments']==app.attachments
            source.unlink()
            app.new_conversation();await app.workers.wait_for_complete();await pilot.pause()
            assert not app.attachments
        reopened=WorkConsole(project,backend=FakeBackend(),initial_mode='assistant')
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete()
            await reopened.list_demands().wait()
            reopened.query_one('#saved-demand',Select).value=identity
            reopened.open_demand();await reopened.workers.wait_for_complete();await pilot.pause()
            assert reopened.attachments[0]['sha256']==ref.sha256
            await pilot.click('#attachment-preview-0');await reopened.workers.wait_for_complete();await pilot.pause()
            await preview_ready(reopened,pilot)
            assert reopened.screen.selected==ref
            await pilot.press('escape');await pilot.pause()
            reopened.query_one('#root',Input).value=str(tmp_path)
            reopened.prepare_workspace();await reopened.workers.wait_for_complete()
            assert not reopened.attachments and not reopened.backend.launches
    asyncio.run(scenario())


def test_an_image_for_a_model_that_cannot_see_is_sent_for_the_agent_to_describe(tmp_path,monkeypatch):
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'screen.png';source.write_bytes(png())
    profile=tmp_path/'profile.json';profile.write_text(json.dumps(deployment()))
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    async def scenario():
        backend=FakeBackend();app=WorkConsole(project,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#profile',Input).value=str(profile)
            ref=await pick(app,pilot,source)
            await pilot.click('#attachment-confirm');await pilot.pause()
            app.query_one('#prompt',TextArea).load_text('Explain this image.')
            await pilot.click('#start');await pilot.pause()
            # The agent describes it through the turn engine's vision routes (relay/native_attachments.py).
            assert len(backend.launches)==1 and '--attachment-input' in backend.launches[0]
            await pilot.click('#cancel')
            for _ in range(40):
                if not app.busy:break
                await pilot.pause(0.05)
    asyncio.run(scenario())


def test_tui_snapshot_reaches_real_cli_and_followup_after_source_deletion(tmp_path,monkeypatch):
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'screen.png';source.write_bytes(png())
    fixture=Client('bedrock-converse', [('tare_deliver',{'summary':'The screenshot is red.'}),
                                       ('tare_deliver',{'summary':'The document explains the failure.'})])
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,body))
            raw=json.dumps(fixture.post(self.path,body,None)).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    model=deployment('bedrock-converse',f'http://127.0.0.1:{server.server_port}')
    model['native_image_media_types']=['image/png']
    profile=tmp_path/'model.json';profile.write_text(json.dumps(model))
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    async def scenario():
        app=WorkConsole(project,backend=Backend(),initial_mode='assistant',storage_preferred=tmp_path/'state')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#profile',Input).value=str(profile)
            app.query_one('#streaming',Checkbox).value=False
            ref=await pick(app,pilot,source)
            await pilot.click('#attachment-confirm');await app.workers.wait_for_complete();await pilot.pause()
            source.unlink()
            await pilot.click('#start');await app.workers.wait_for_complete()
            # The run's worker can start after the click returns, as the follow-up's below (Windows CI, 2026-10-07).
            for _ in range(400):
                if app.last_result is not None and not app.busy:break
                await pilot.pause(0.05)
            # No result means the run never started (status says why) or its process exited without one.
            assert app.last_result and app.last_result['status']=='PROPOSED',(
                app.last_result,'status',str(app.query_one('#status').render()),
                'process',getattr(app.process,'returncode','not started'))
            assert base64.b64encode(png()).decode() in json.dumps(calls[-1][1])
            session=app.assistant_session
            assert not app.attachments and fixture.generations==1
            document=tmp_path/'error.log';document.write_text('Error E312: unmatched separator')
            await pick(app,pilot,document)
            await pilot.click('#attachment-confirm');await app.workers.wait_for_complete();await pilot.pause()
            document.unlink()
            app.query_one('#prompt',TextArea).load_text('Explain the error too.')
            await pilot.click('#start');await app.workers.wait_for_complete()
            # The follow-up's worker can start after the click returns (Windows CI): wait for its result.
            from test_review_checks_panel import until
            await until(pilot,lambda:fixture.generations==2 and not app.busy,max_ticks=400)
            assert app.last_result['status']=='PROPOSED' and fixture.generations==2,(
                app.last_result,'status',str(app.query_one('#status').render()),'busy',app.busy)
            assert 'Error E312: unmatched separator' in json.dumps(calls[-1][1])
            assert base64.b64encode(png()).decode() in json.dumps(calls[-1][1])
            assert app.assistant_session==session and not app.attachments
            from compute_plane.native_journal import io_path
            assert io_path(Path(session)/'attachments'/ref.sha256).read_bytes()==png()
    try:asyncio.run(scenario())
    finally:server.shutdown();server.server_close();thread.join(5)


def test_slash_attach_and_queued_snapshot_use_current_draft_not_previous_turn(tmp_path,monkeypatch):
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'screen.png';source.write_bytes(png())
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    async def scenario():
        class Messages(FakeBackend):
            def __init__(self):super().__init__();self.messages=[];self.fail=True
            def send_message(self,selection,text,message_id):
                self.messages.append((selection,text,message_id))
                if self.fail:raise ValueError('fixture save failed')
            def message_status(self,*args):return 'QUEUED'
        backend=Messages()
        app=WorkConsole(project,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            await settle_slash(pilot)
            prompt=app.query_one('#prompt',TextArea)
            await edit_slash(pilot,prompt,'/att',(0,4))
            assert shown_slash(app)==['/attach']
            await pilot.press('tab','ctrl+enter');await pilot.pause()
            assert isinstance(app.screen,AttachmentPicker) and not backend.launches
            await pilot.press('escape');await pilot.pause()
            app.assistant_session=str(tmp_path/'session')
            app.active=Selection(str(project),session=app.assistant_session)
            app.controls(True)
            ref=await pick(app,pilot,source)
            await pilot.click('#attachment-confirm');await pilot.pause()
            prompt.load_text('Use this image.')
            app.send_operator_message();await app.workers.wait_for_complete()
            assert backend.messages[0][0].attachments[0]['sha256']==ref.sha256
            assert app.attachments and prompt.text=='Use this image.'
            first_id=backend.messages[0][2]
            app.send_operator_message();await app.workers.wait_for_complete()
            assert backend.messages[-1][2]==first_id
            backend.fail=False
            await pilot.click('#attachment-remove-0');await pilot.pause()
            source.write_bytes(png('blue'))
            changed=await pick(app,pilot,source)
            await pilot.click('#attachment-confirm');await pilot.pause()
            app.send_operator_message();await app.workers.wait_for_complete();await pilot.pause()
            assert backend.messages[-1][2]!=first_id
            assert backend.messages[-1][0].attachments[0]['sha256']==changed.sha256
            assert not app.attachments and prompt.text==''
            assert app.demand_state['snapshot']['selection']['attachments']==[]
            assert app.demand_state['events'][-1]['detail']['attachments'][0]['sha256']==changed.sha256
            app.controls(False)
    asyncio.run(scenario())


def test_delayed_directory_listing_preserves_typed_path_and_focus(tmp_path,monkeypatch):
    from relay import console_attachments
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'screen.png';source.write_bytes(png())
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    entered,release=Event(),Event()
    original=console_attachments.directory_entries
    def delayed(path):
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(console_attachments,'directory_entries',delayed)
    async def scenario():
        app=WorkConsole(project,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_attach()
            for _ in range(60):
                await pilot.pause(.03)
                if entered.is_set():break
            assert entered.is_set()
            field=app.screen.query_one('#attachment-location',Input)
            field.focus();field.value=str(source)
            release.set()
            await app.workers.wait_for_complete();await pilot.pause()
            assert field.value==str(source) and app.focused is field
            await pilot.press('enter')
            ref=await preview_ready(app,pilot)
            await pilot.resize_terminal(80,24)
            await pilot.pause();await app.workers.wait_for_complete()
            assert await preview_ready(app,pilot)==ref
            await pilot.press('escape')
            assert not app.backend.launches
    try:asyncio.run(scenario())
    finally:release.set()


def test_missing_file_explains_recovery_and_picker_accepts_a_replacement(tmp_path,monkeypatch):
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'notes.txt';source.write_text('A readable replacement.')
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    async def scenario():
        app=WorkConsole(project,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            app.action_attach(str(tmp_path/'missing.txt'))
            await pilot.pause();await app.workers.wait_for_complete()
            picker=app.screen
            text=str(picker.query_one('#attachment-preview',Static).render())
            assert 'Cannot read this file' in text and 'ATTACHMENT_' not in text
            assert picker.selected is None and picker.query_one('#attachment-confirm',Button).disabled
            picker.open_path(source)
            ref=await preview_ready(app,pilot)
            await pilot.click('#attachment-confirm');await pilot.pause()
            assert app.attachments[0]['sha256']==ref.sha256 and not app.backend.launches
    asyncio.run(scenario())


def test_documented_image_profiles_require_explicit_selection(tmp_path):
    from relay.console_attachments import check_image_profile
    from compute_plane.native_attachments import NativeAttachmentStore
    ref=NativeAttachmentStore(tmp_path/'assets').put_bytes('image.png',png())
    from relay.native_attachments import records
    selection=Selection(str(tmp_path/'project'),attachments=records((ref,)),
                        attachment_root=str(tmp_path/'assets'),attachment_project=str(tmp_path/'project'))
    selection.deployment='local-qwen'
    with pytest.raises(ValueError,match='cannot see images'):  # a queued message goes to the model as is
        check_image_profile(selection)
    for name in ('bedrock-opus','bedrock-sonnet'):
        selection.deployment=name
        check_image_profile(selection)


def test_a_model_that_cannot_see_gets_the_images_described_instead(tmp_path, monkeypatch):
    from compute_plane.native_attachments import NativeAttachmentStore
    from relay import turn_engine
    from relay.native_attachments import describe_for_blind
    store = NativeAttachmentStore(tmp_path/'assets')
    image, notes = store.put_bytes('screen.png', png()), store.put_bytes('notes.txt', b'hello', 'text/plain')
    described = []
    monkeypatch.setattr(turn_engine, 'describe_files', lambda paths, emit: described.append(
        [p.read_bytes() for p in paths]) or '\n\nAttached image(s), described by luna: a red button')
    refs, text = describe_for_blind((image, notes), tmp_path/'assets', {'id': 'local-qwen'}, 'why is it red?')
    assert refs == (notes,) and text == 'why is it red?\n\nAttached image(s), described by luna: a red button'
    assert described == [[png()]]
    seeing = {'native_image_media_types': ['image/png']}
    assert describe_for_blind((image, notes), tmp_path/'assets', seeing, 'why?') == ((image, notes), 'why?')

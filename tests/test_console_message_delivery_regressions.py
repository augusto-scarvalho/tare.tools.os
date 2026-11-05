"""Parent counterexamples independent of the delegated implementation tests."""
import asyncio
import pytest
from textual.app import App, ComposeResult
from relay.console_output import Transcript, MessageDelivery


def item(mid='one',state='SAVED',text='Keep the operator note.'):
    return {'message_id':mid,'text':text,'status':'QUEUED' if state=='SAVED' else 'IN_CONTEXT',
            'delivery':{'status':state,'receipt_sequence':None if state=='SAVED' else 2,
                        'call_id':None,'request_sequence':None,'response_sequence':None,'terminal_sequence':None}}


class View(App):
    def compose(self)->ComposeResult:
        yield Transcript(id='output')


@pytest.mark.parametrize('failed_read', [False, True])
def test_pending_observation_ignores_partially_unmounted_view(tmp_path, failed_read):
    from threading import Event
    from textual.widgets import Static
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    started, release = Event(), Event()
    class Backend(FakeBackend):
        calls = 0
        def message_observations(self, selection):
            self.calls += 1
            started.set()
            assert release.wait(8)
            if failed_read:
                raise OSError('Read completed during shutdown')
            return [item()]
    async def scenario():
        backend = Backend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.assistant_session = str(tmp_path/'session')
            refresh = asyncio.create_task(app.refresh_messages())
            try:
                assert await asyncio.to_thread(started.wait, 3)
                # Textual removes child widgets before the app's unmount handler.
                await app.query_one('#message-status', Static).remove()
            finally:
                release.set()
                await refresh
            assert backend.calls == 1
            assert not app._message_proofs
            assert not app.query_one(Transcript).message_rows
    asyncio.run(scenario())


def test_receipt_does_not_split_the_current_stream():
    async def scenario():
        app=View()
        async with app.run_test(size=(80,24)) as pilot:
            output=app.query_one(Transcript)
            output.stream({'channel':'text','call_id':'call-1','text':'Beginning '})
            live=output.live
            output.message_delivery(item())
            assert output.live is live, 'A receipt interrupted the visible streaming response'
            output.stream({'channel':'text','call_id':'call-1','text':'and ending.'})
            assert output.live is live and output.live_text=='Beginning and ending.'
    asyncio.run(scenario())


def test_message_receipt_title_removes_terminal_control_sequences():
    async def scenario():
        app=View()
        async with app.run_test(size=(80,24)) as pilot:
            row=app.query_one(Transcript).message_delivery(item(text='\x1b[2Jclear screen\x07'))
            await pilot.pause()
            assert '\x1b' not in str(row.title) and '\x07' not in str(row.title)
    asyncio.run(scenario())


def test_receipt_update_preserves_scroll_and_expansion():
    async def scenario():
        app=View()
        async with app.run_test(size=(80,24)) as pilot:
            output=app.query_one(Transcript)
            row=output.message_delivery(item())
            row.collapsed=False
            for n in range(25):output.write('Answer '+str(n)+'\n\nMore detail.')
            await pilot.pause()
            output.scroll_home(animate=False, immediate=True)  # not after a refresh a slow runner may not reach
            await pilot.pause()
            old=output.scroll_y
            same=output.message_delivery(item(state='RECEIVED'))
            await pilot.pause()
            assert same is row and not row.collapsed
            assert output.scroll_y==old
            assert len(list(output.query(MessageDelivery)))==1
    asyncio.run(scenario())


def test_full_receipt_retained_without_mutating_backend_snapshot(tmp_path):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    class Backend(FakeBackend):
        rows=[item(state='RESPONSE_READY')]
        def message_observations(self,selection):return self.rows
    async def scenario():
        backend=Backend()
        backend.rows[0]['delivery'].update(call_id='proof-call',request_sequence=3,response_sequence=4,terminal_sequence=5)
        app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete();await pilot.pause()
            app.assistant_session=str(tmp_path/'session')
            await app.refresh_messages()
            backend.rows=[item(state='SAVED')]
            await app.refresh_messages()
            row=app.query_one(Transcript).message_rows['one']
            assert row.delivery_status=='RESPONSE_READY'
            assert row.item['delivery']['call_id']=='proof-call'
            assert row.item['delivery']['terminal_sequence']==5
            assert backend.rows[0]['delivery']['status']=='SAVED'
    asyncio.run(scenario())


def test_old_receipts_do_not_churn_out_conversation_messages(tmp_path):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    class Backend(FakeBackend):
        def message_observations(self,selection):return [item(str(n)) for n in range(140)]
    async def scenario():
        app=WorkConsole(tmp_path,backend=Backend(),initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete();await pilot.pause()
            app.assistant_session=str(tmp_path/'session')
            output=app.query_one(Transcript)
            output.write('You: Keep this visible conversation.')
            operator=output.items[-1]
            await app.refresh_messages();await pilot.pause()
            rows=dict(output.message_rows)
            for _ in range(3):await app.refresh_messages()
            await pilot.pause()
            assert 0<len(rows)<=50
            assert output.message_rows==rows
            assert operator in output.items and operator.is_mounted
    asyncio.run(scenario())


def test_late_failure_after_transcript_reset_is_silent(tmp_path):
    from threading import Event
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from textual.widgets import TextArea
    started,release=Event(),Event()
    class Backend(FakeBackend):
        def message_observations(self,selection):
            started.set()
            assert release.wait(8)
            raise OSError('old view read failure')
    async def scenario():
        app=WorkConsole(tmp_path,backend=Backend(),initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete();await pilot.pause()
            app.assistant_session=str(tmp_path/'session')
            read=asyncio.create_task(app.refresh_messages())
            assert await asyncio.to_thread(started.wait,5)
            output=app.query_one(Transcript)
            output.clear()
            app.query_one('#prompt',TextArea).load_text('A new unsent draft')
            release.set();await read
            assert not output.message_rows and output.delivery_note is None
            assert app.query_one('#prompt',TextArea).text=='A new unsent draft'
    try:asyncio.run(scenario())
    finally:release.set()


def test_receipt_timer_tolerates_partially_unmounted_controls(tmp_path):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from textual.css.query import NoMatches
    async def scenario():
        app=WorkConsole(tmp_path,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete();await pilot.pause()
            app.assistant_session=str(tmp_path/'session')
            original=app.query_one
            def unmounting(selector,*args,**kwargs):
                if selector=='#root':raise NoMatches('Root unmounted before mode')
                return original(selector,*args,**kwargs)
            app.query_one=unmounting
            try:
                assert app.message_observation_scope() is None
                app.poll_message_observations()
            finally:app.query_one=original
    asyncio.run(scenario())

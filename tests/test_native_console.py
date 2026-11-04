import asyncio
import json
from pathlib import Path
import pytest
from textual.widgets import Input, Select, TextArea, Button
from relay.console_backend import Selection, command
from relay.native_console import WorkConsole, plain


def test_resume_preserves_saved_model_and_limits_and_explicit_followup(tmp_path):
    session=tmp_path/'session';session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config':{'repositories':{'repo':str(tmp_path)}}}))
    selected=Selection(str(tmp_path), session=str(session), deployment='different-model', unlimited=True,
                       prompt='Continue with Unicode café', refresh=True)
    args=command(selected,True)
    assert '--deployment' not in args and '--unlimited' not in args and '--root' not in args
    assert args[args.index('--follow-up')+1]=='Continue with Unicode café'
    assert '--message-id' in args and '--refresh' in args
    elsewhere=tmp_path/'elsewhere';elsewhere.mkdir();selected.root=str(elsewhere)
    with pytest.raises(ValueError,match='another project'):command(selected,True)


def test_saved_selection_from_before_the_manifest_input_was_removed_still_reopens(tmp_path):
    from dataclasses import asdict
    from relay.console_backend import saved_selection
    selected=Selection(str(tmp_path),mode='work',work='Work-1')
    assert saved_selection({**asdict(selected),'manifest':'old.json'})==selected


def test_work_resume_does_not_override_descriptor_and_retry_is_explicit(tmp_path):
    selected=Selection(str(tmp_path),mode='work',work='Work-1',deployment='different',retry_plan=True)
    args=command(selected,True)
    assert '--retry-plan' in args and '--resume' in args
    assert not any(x in args for x in ('--deployment','--unlimited','--repo-root'))


@pytest.mark.parametrize('mode', ['session', 'work', 'sequence'])
def test_reviewed_findings_are_forwarded_only_for_new_execution(tmp_path, monkeypatch, mode):
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')
    selected = Selection(str(tmp_path), mode=mode, work='work-one', sequence_works='work-one',
        findings=str(tmp_path/'reviewed.json'), findings_sha256='a'*64, prompt='Inspect')
    args = command(selected)
    assert args[args.index('--findings')+1] == selected.findings
    assert args[args.index('--findings-sha256')+1] == 'a'*64
    if mode == 'session':
        Path(selected.session).mkdir(parents=True, exist_ok=True)
        (Path(selected.session)/'native-agent.json').write_text(json.dumps({'config': {'repositories': {'repo': str(tmp_path)}}}))
    if mode == 'sequence':
        monkeypatch.setattr('relay.console_backend.Backend.sequence_view', lambda *a: {})
    args = command(selected, True)
    assert '--findings' not in args and '--findings-sha256' not in args


def test_partial_findings_selection_is_refused_before_dispatch(tmp_path):
    with pytest.raises(ValueError, match='findings with their identity'):
        command(Selection(str(tmp_path), findings='reviewed.json', prompt='Inspect'))


def test_custom_http_verifier_argv_and_optional_limits(tmp_path,monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    selected=Selection(str(tmp_path),profile=str(tmp_path/'endpoint.json'),prompt='Implement',
        role='implementer', write_paths=['.'],
        verifier=json.dumps(['python','a file.py','literal; no shell']),unlimited=False)
    args=command(selected)
    assert '--native-deployment' in args and '--deployment' not in args and '--unlimited' not in args
    assert args[-3:]==['python','a file.py','literal; no shell']
    assert Path(selected.session).parent==tmp_path/'custody'


def test_terminal_content_cannot_inject_escapes_or_markup():
    assert plain('\x1b[2J[red]café\x00\n')=='[red]café\n'


class Process:
    returncode=None
    def __init__(self):self.done=asyncio.Event()
    async def communicate(self):
        await self.done.wait();self.returncode=2
        return json.dumps({'status':'CANCELLED','reason':'OPERATOR_CANCELLED'}).encode(),b''


class FakeBackend:
    def __init__(self):self.launches=[];self.process=Process();self.cancelled=0
    def catalog(self,root):return {'models':[{'deployment':'local-qwen','model':'Qwen'}],'sessions':[],'works':[],'errors':[]}
    async def launch(self,args,root):self.launches.append(args);return self.process
    def progress(self,selection):return 'Model working'
    def cancel(self,process):self.cancelled+=1;process.done.set()
    def review(self,selection):return 'Recorded diff; no new model call.'


@pytest.mark.parametrize('deployments', [[], ['only-configured-route']])
def test_filtered_model_catalog_does_not_hide_sessions_or_require_an_unlisted_default(tmp_path, deployments):
    class FilteredBackend(FakeBackend):
        def catalog(self, root):
            return {'models': [{'deployment': value, 'model': 'Configured model'} for value in deployments],
                    'sessions': [], 'works': [{'id': 'visible-work', 'title': 'Still available'}], 'errors': []}
    async def scenario():
        backend = FilteredBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert 'Catalog unavailable' not in str(app.query_one('#status').render())
            model = app.query_one('#model', Select)
            assert model.value == (deployments[0] if deployments else '')
            work = app.query_one('#work', Select)
            work.value = 'visible-work'
            assert work.value == 'visible-work'
            assert not backend.launches
            app.action_reload()
            await app.workers.wait_for_complete()
            assert model.value == (deployments[0] if deployments else '')
    asyncio.run(scenario())


@pytest.mark.parametrize('status,audited',[('PROPOSED',True),('RECONCILIATION_REQUIRED',False)])
def test_only_a_finished_turn_is_audited(tmp_path,monkeypatch,status,audited):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    seen=[]
    monkeypatch.setattr('relay.turn_engine.resumed_turn',lambda session,invocation:(['stopped',invocation],'Opened it'))
    monkeypatch.setattr('relay.turn_engine.turn_changes',
                        lambda runs,root:seen.append([r[1]['invocation_id'] for r in runs]) or ['a.py'])
    monkeypatch.setattr('relay.turn_engine.turn_checks',lambda runs,root:[])
    audits=[]
    monkeypatch.setattr(WorkConsole,'audit_changes',lambda self,*a:audits.append(a))
    class Finished(Process):
        async def communicate(self):
            self.returncode=0
            return json.dumps({'status':status,'reason':'X'}).encode(),b''
    async def scenario():
        backend=FakeBackend();backend.process=Finished()
        app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt',TextArea).load_text('Change a.py')
            app.begin_assistant()
            await app.workers.wait_for_complete();await pilot.pause()
            assert app.last_result['status']==status
    asyncio.run(scenario())
    assert bool(audits)==audited
    if audited:  # the stopped invocation before it is audited too, against the message the operator sent
        assert seen[0][0]=='stopped' and len(seen[0])==2 and audits[0][3]=='Change a.py'


def test_a_short_reply_is_audited_with_the_messages_before_it(tmp_path,monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    monkeypatch.setattr('relay.turn_engine.resumed_turn',lambda session,invocation:([invocation],''))
    monkeypatch.setattr('relay.turn_engine.turn_changes',lambda runs,root:['a.py'])
    monkeypatch.setattr('relay.turn_engine.turn_checks',lambda runs,root:[])
    audits=[]
    monkeypatch.setattr(WorkConsole,'audit_changes',lambda self,*a:audits.append(a))
    class Finished(Process):
        async def communicate(self):
            self.returncode=0
            return json.dumps({'status':'PROPOSED','reason':'X'}).encode(),b''
    async def scenario():
        backend=FakeBackend();backend.process=Finished()
        app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_history=['You: Classify provider refusals as generation failures','Tare: May I read the receipts?']
            app.query_one('#prompt',TextArea).load_text('sim')
            app.begin_assistant()
            await app.workers.wait_for_complete();await pilot.pause()
    asyncio.run(scenario())
    request=audits[0][3]
    assert request.startswith('sim') and 'Classify provider refusals' in request and 'May I read' not in request


def test_a_new_conversation_drops_the_previous_ones_folder_request(tmp_path):
    async def scenario():
        app=WorkConsole(tmp_path,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.folder_request={'path':str(tmp_path),'reason':'x','session':str(tmp_path/'old')}
            app.new_conversation();await pilot.pause()
            assert app.folder_request is None
    asyncio.run(scenario())


def test_a_resumed_turn_covers_the_invocations_that_stopped_before_it(tmp_path):
    from relay.turn_engine import resumed_turn
    session=tmp_path/'s';session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'invocations':[
        {'id':'first','status':'PROPOSED','journal_start':0,'journal_end':2},
        {'id':'blocked','status':'BLOCKED','journal_start':2,'journal_end':4},
        {'id':'refused','status':'BLOCKED','journal_start':4,'journal_end':4},
        {'id':'resumed','status':'PROPOSED','journal_start':4,'journal_end':5}]}))
    events=[{'kind':'session_started','instruction':'First request'},{'kind':'terminal'},
            {'kind':'user_message','text':'Second request'},{'kind':'model_requested'},{'kind':'terminal'}]
    (session/'journal.jsonl').write_text(''.join(json.dumps({'event':e})+'\n' for e in events))
    assert resumed_turn(session,'resumed')==(['blocked','refused','resumed'],'Second request')
    assert resumed_turn(session,'first')==(['first'],'First request')


@pytest.mark.parametrize('status',['CANCELLED','BLOCKED'])
def test_enter_after_a_stopped_turn_sends_a_correction_to_the_same_session(tmp_path,status):
    session=tmp_path/'s';session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config':{'repositories':{'repo':str(tmp_path)}},'conversation':True,'invocations':[{'id':'a','status':status}]}))
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session=str(session)
            app.query_one('#prompt',TextArea).load_text('Next step')
            app.query_one('#prompt',TextArea).focus();await pilot.press('enter');await pilot.pause()
            assert len(backend.launches)==1
            args=backend.launches[0]
            assert args[args.index('--session')+1]==str(session)
            assert args[args.index('--follow-up')+1]=='Next step'
            assert app.query_one('#prompt',TextArea).text==''
            app.action_cancel();await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_enter_after_a_first_turn_that_never_started_its_session_starts_one(tmp_path):
    never=tmp_path/'never'  # the turn failed (an unknown pin) before native-agent.json existed
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session=str(never)
            app.query_one('#prompt',TextArea).load_text('e como eu rodo os testes?')
            app.query_one('#prompt',TextArea).focus();await pilot.press('enter');await pilot.pause()
            [args]=backend.launches
            assert args[args.index('--instruction')+1].endswith('e como eu rodo os testes?')
            assert args[args.index('--session')+1]!=str(never) and '--resume' not in args
            assert app.query_one('#prompt',TextArea).text==''
            app.action_cancel();await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_tui_stays_responsive_prevents_duplicate_start_and_records_cancel(tmp_path,monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend)
        async with app.run_test(size=(130,44)) as pilot:
            await pilot.pause()
            app.query_one('#prompt',TextArea).load_text('A small real task')
            await pilot.click('#start');await pilot.pause()
            assert app.busy and app.query_one('#start',Button).disabled
            app.execute(False)
            assert len(backend.launches)==1
            await pilot.click('#cancel')
            for _ in range(30):
                if not app.busy:break
                await pilot.pause(0.05)
            assert not app.busy and backend.cancelled==1
            assert app.last_result['status']=='CANCELLED'
            assert not app.query_one('#start',Button).disabled
    asyncio.run(scenario())


def test_tui_quit_waits_for_runner_cancellation(tmp_path,monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend)
        async with app.run_test(size=(100,36)) as pilot:
            await pilot.pause();app.query_one('#prompt',TextArea).load_text('Task')
            app.execute(False);await pilot.pause()
            app.action_quit()
            await pilot.pause()
            assert app.close_after_run and backend.cancelled==1
    asyncio.run(scenario())


def test_controls_wait_for_saved_session_selection_refresh(tmp_path,monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend)
        refreshing=asyncio.Event();release=asyncio.Event()
        async def delayed(path):
            refreshing.set();await release.wait()
        app.refresh_session=delayed
        async with app.run_test(size=(130,44)) as pilot:
            await pilot.pause();app.query_one('#prompt',TextArea).load_text('Task')
            app.execute(False);await pilot.pause();app.action_cancel()
            await refreshing.wait()
            assert app.busy and app.query_one('#resume',Button).disabled
            release.set();await pilot.pause()
            assert not app.busy
    asyncio.run(scenario())


def test_quit_during_launch_still_cancels_child(tmp_path,monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    async def scenario():
        backend=FakeBackend();gate=asyncio.Event();original=backend.launch
        async def launch(*args):
            await gate.wait();return await original(*args)
        backend.launch=launch
        app=WorkConsole(tmp_path,backend=backend)
        async with app.run_test(size=(130,44)) as pilot:
            await pilot.pause();app.query_one('#prompt',TextArea).load_text('Task')
            app.execute(False);await pilot.pause();app.action_quit()
            gate.set();await pilot.pause()
            assert backend.cancelled==1
    asyncio.run(scenario())


def test_work_start_from_ui_dispatches_work_and_keeps_failure_visible(tmp_path):
    async def scenario():
        backend=FakeBackend();app=WorkConsole(tmp_path,backend=backend)
        async with app.run_test(size=(130,44)) as pilot:
            await pilot.pause()
            app.query_one('#mode',Select).value='work'
            app.query_one('#work-id',Input).value='WORK-EXPLICIT'
            app.execute(False);await pilot.pause()
            assert backend.launches[0][:2]==['run','WORK-EXPLICIT']
            app.action_cancel();await pilot.pause()
            assert app.last_result['status']=='CANCELLED'
    asyncio.run(scenario())


def test_live_message_uses_active_session_keeps_new_draft_and_does_not_cancel(tmp_path, monkeypatch):
    from threading import Event
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')
    async def scenario():
        backend=FakeBackend();sent=[];entered=Event();release=Event()
        def send(selection, text, message_id):
            sent.append((selection.session, text, message_id));entered.set()
            assert release.wait(5)
            return {'status':'SAVED'}
        backend.send_message=send
        backend.message_status=lambda selection, message_id:'QUEUED'
        app=WorkConsole(tmp_path,backend=backend)
        async with app.run_test(size=(130,50)) as pilot:
            await pilot.pause();app.query_one('#prompt',TextArea).load_text('Original task')
            app.execute(False);await pilot.pause()
            app.query_one('#message',TextArea).load_text('First correction')
            app.send_operator_message();await asyncio.to_thread(entered.wait, 5)
            app.query_one('#message',TextArea).load_text('Next draft')
            app.send_operator_message()  # Double-click cannot send twice.
            release.set()
            for _ in range(20):
                await pilot.pause(.05)
                if not app.sending_message:break
            assert len(sent)==1 and sent[0][0]==app.active.session
            assert app.query_one('#message',TextArea).text=='Next draft'
            assert backend.cancelled==0 and app.busy
            app.action_cancel();await pilot.pause()
    asyncio.run(scenario())


def test_the_agent_process_never_imports_tare_from_the_project(tmp_path,monkeypatch):
    import os
    from relay.console_backend import Backend
    project=tmp_path/'project';(project/'relay').mkdir(parents=True)
    (project/'relay'/'__init__.py').write_text('')
    (project/'relay'/'native_installation.py').write_text('def main():\n    print("PROJECT CODE RAN")\n    return 0\n')
    # The trailing empty entry means the current directory.
    monkeypatch.setenv('PYTHONPATH',os.pathsep.join([str(Path(__file__).resolve().parents[1]),os.environ.get('PYTHONPATH',''),'']))
    async def run():
        process=await Backend().launch(['--help'],str(project))
        out,_=await process.communicate()
        return out.decode()
    out=asyncio.run(run())
    assert 'PROJECT CODE RAN' not in out and 'usage' in out.lower()


@pytest.mark.parametrize('changed',[[],['a.py']])
def test_a_finished_turn_that_changed_nothing_has_its_reply_judged_and_one_that_changed_files_is_audited(
        tmp_path,monkeypatch,changed):
    monkeypatch.setattr('relay.native_agent.session_root',lambda:tmp_path/'custody')
    monkeypatch.setattr('relay.native_console.session_has_terminal',lambda selection:False)  # no snapshot diff
    monkeypatch.setattr('relay.turn_engine.resumed_turn',lambda session,invocation:([invocation],'Opened it'))
    monkeypatch.setattr('relay.turn_engine.turn_changes',lambda runs,root:list(changed))
    monkeypatch.setattr('relay.turn_engine.turn_checks',lambda runs,root:[])
    audits,judged=[],[]
    monkeypatch.setattr(WorkConsole,'audit_changes',lambda self,*a:audits.append(a))
    monkeypatch.setattr(WorkConsole,'judge_answer',lambda self,*a:judged.append(a))
    reply='Vou rastrear a regra que define IsDeleted e o fluxo usado para propostas antigas...'
    class Finished(Process):
        async def communicate(self):
            self.returncode=0
            return json.dumps({'status':'PROPOSED','reason':'X','output':reply}).encode(),b''
    async def scenario():
        backend=FakeBackend();backend.process=Finished()
        app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt',TextArea).load_text('Como IsDeleted é definido?')
            app.begin_assistant()
            await app.workers.wait_for_complete();await pilot.pause()
            assert app.last_result['status']=='PROPOSED'
    asyncio.run(scenario())
    if changed:  # a change goes to the audit; the judge is not asked
        assert len(audits)==1 and audits[0][1]==['a.py'] and judged==[]
    else:  # nothing changed: the reply is judged against the message the operator sent
        assert audits==[] and len(judged)==1
        source,request,output=judged[0]
        assert source and request=='Como IsDeleted é definido?' and output==reply

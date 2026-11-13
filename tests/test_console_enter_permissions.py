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
            await pilot.pause();assert str(app.screen.query_one('#permission-once').label)=='Allow for this task'
            await pilot.press('enter');await pilot.pause()
            assert answers==['once'] and not backend.launches and prompt.text=='a\nb\nc'
    asyncio.run(scenario())


@pytest.mark.parametrize('files_allowed', [False, True])
def test_plan_dialog_shows_the_whole_plan_and_sends_the_requested_changes(tmp_path, files_allowed):
    import json
    from types import SimpleNamespace
    from textual.widgets import Button, Input, Static
    from relay import conversation_permissions as permissions
    project, session = tmp_path/'project', tmp_path/'session'
    project.mkdir()
    artifact = session/'planning'/'p'/'session'/'artifact.json'; artifact.parent.mkdir(parents=True)
    steps = [f'Step number {n}.' for n in range(1, 41)]  # longer than the screen: it scrolls
    permissions.save_json(artifact, {'payload': {'summary': 'Change a.py.', 'steps': steps, 'criteria': ['value is two'],
        'sources': [], 'remaining': ['Whether callers rely on one.'], 'tasks': [{'title': 'Change value',
        'summary': 'Set two.', 'target_paths': ['a.py'], 'criteria': ['value is two'], 'test_paths': ['tests/test_a.py']}]}})
    import hashlib
    request = {'schema': permissions.SCHEMA, 'session': 'parent', 'effect': 'e', 'task': 't',
               'plan': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'project': str(project.resolve()),
               'paths': ['a.py'], 'checks': {}, 'tests': [], 'objective': 'Change value to two.', 'status': 'WAITING',
               'artifact': str(artifact), 'files_allowed': files_allowed}
    request['id'] = permissions.identity(request)
    (session/'execution-permission').mkdir()
    permissions.save_json(session/'execution-permission'/'request.json', request)
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.refresh_execution_permission(SimpleNamespace(session=str(session), root=str(project)))
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, ExecutionPermissionPanel)
            assert str(panel.query_one('.panel-title', Static).content) == 'Implement this plan?'
            shown = '\n'.join(str(s.content) for s in panel.query(Static))
            for part in ('40. Step number 40.', 'Files: a.py', 'Checks: tests/test_a.py', 'Left uncertain by the planner:',
                         'Whether callers rely on one.'):
                assert part in shown
            buttons = [str(b.label) for b in panel.query(Button)]
            assert buttons == ['Implement this plan', *([] if files_allowed else ['Implement, always allow edits here']),
                               'Change the plan', 'Deny']
            assert app.focused.id == 'permission-once' and panel.query_one('.permission-detail').max_scroll_y > 0
            changes = panel.query_one('#permission-changes', Input)
            changes.focus(); changes.value = 'Keep a.py short'
            await pilot.press('enter'); await pilot.pause()
            assert not isinstance(app.screen, ExecutionPermissionPanel)
            assert json.loads((session/'execution-permission'/'answer.json').read_text(encoding='utf-8')) == {
                'id': request['id'], 'choice': 'change', 'changes': 'Keep a.py short'}
    asyncio.run(scenario())


@pytest.mark.parametrize('key,choice', [('enter', 'once'), ('escape', 'deny')])
def test_local_only_apply_dialog_says_the_audit_is_not_independent_and_sends_the_answer(tmp_path, key, choice):
    import json
    from types import SimpleNamespace
    from textual.widgets import Button, Static
    from relay import conversation_permissions as permissions
    project, session = tmp_path/'project', tmp_path/'session'
    project.mkdir(); (session/'execution-permission').mkdir(parents=True)
    request = {'schema': permissions.SCHEMA, 'kind': 'audit', 'session': 'parent', 'effect': 'e', 'task': 't',
               'plan': 'a'*64, 'project': str(project.resolve()), 'paths': ['a.py'], 'checks': {}, 'tests': [],
               'objective': permissions.APPLY_QUESTION, 'status': 'WAITING',
               'audit': {'label': 'same-family audit, not independent', 'verdict': 'ACCEPT', 'route': 'local-qwen',
                         'summary': 'The change sets value to two.'}}
    request['id'] = permissions.identity(request)
    permissions.save_json(session/'execution-permission'/'request.json', request)
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.refresh_execution_permission(SimpleNamespace(session=str(session), root=str(project)))
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, ExecutionPermissionPanel)
            assert str(panel.query_one('.panel-title', Static).content) == 'Apply this change?'
            shown = '\n'.join(str(s.content) for s in panel.query(Static))
            for part in ('same model family as the implementer, so the audit is not independent',
                         'Audit (same-family audit, not independent): ACCEPT by local-qwen', 'a.py'):
                assert part in shown
            assert 'independently audited' not in shown
            assert [str(b.label) for b in panel.query(Button)] == ['Apply the change', 'Do not apply']
            assert app.focused.id == 'permission-once'
            await pilot.press(key); await pilot.pause()
            assert not isinstance(app.screen, ExecutionPermissionPanel)
            assert json.loads((session/'execution-permission'/'answer.json').read_text(encoding='utf-8')) == {
                'id': request['id'], 'choice': choice}
    asyncio.run(scenario())


@pytest.mark.parametrize('keys,choice', [(('enter',), 'deny'), (('shift+tab', 'enter'), 'once')])
def test_local_only_dialog_for_an_audit_that_did_not_accept_starts_on_do_not_apply(tmp_path, keys, choice):
    import json
    from types import SimpleNamespace
    from textual.widgets import Button, Static
    from relay import conversation_permissions as permissions
    project, session = tmp_path/'project', tmp_path/'session'
    project.mkdir(); (session/'execution-permission').mkdir(parents=True)
    items = ['Running hello.py was not evidenced [bold]x[/bold].'] + [f'Item {n}.' for n in range(2, 12)]
    request = {'schema': permissions.SCHEMA, 'kind': 'audit', 'session': 'parent', 'effect': 'e', 'task': 't',
               'plan': 'a'*64, 'project': str(project.resolve()), 'paths': ['hello.py'], 'checks': {}, 'tests': [],
               'objective': permissions.APPLY_UNACCEPTED_QUESTION, 'status': 'WAITING',
               'audit': {'label': 'same-family audit, not independent', 'verdict': 'INCONCLUSIVE', 'route': 'local-qwen',
                         'summary': 'Criteria 1 and 2 hold.', 'open_items': items[:10], 'open_items_total': 11}}
    request['id'] = permissions.identity(request)
    permissions.save_json(session/'execution-permission'/'request.json', request)
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 40)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.refresh_execution_permission(SimpleNamespace(session=str(session), root=str(project)))
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, ExecutionPermissionPanel)
            assert str(panel.query_one('.panel-title', Static).content) == 'Apply this change?'
            shown = '\n'.join(str(s.content) for s in panel.query(Static))
            for part in ('the auditor did NOT accept this change', 'so its audit is not independent either',
                         'Audit (same-family audit, not independent): INCONCLUSIVE by local-qwen (not accepted)',
                         'Open items the auditor left:\n- Running hello.py was not evidenced [bold]x[/bold].',
                         '- Item 10.', '(1 more in the audit receipt)', 'Criteria 1 and 2 hold.'):
                assert part in shown  # items are plain text: no markup is interpreted
            assert 'Item 11.' not in shown
            assert [str(b.label) for b in panel.query(Button)] == ['Apply anyway', 'Do not apply']
            assert app.focused.id == 'permission-deny'  # the safe answer is the default
            await pilot.press(*keys); await pilot.pause()
            assert not isinstance(app.screen, ExecutionPermissionPanel)
            assert json.loads((session/'execution-permission'/'answer.json').read_text(encoding='utf-8')) == {
                'id': request['id'], 'choice': choice}
    asyncio.run(scenario())


@pytest.mark.parametrize('keys,choice', [(('enter',), 'deny'), (('shift+tab', 'shift+tab', 'enter'), 'once'),
                                         (('shift+tab', 'enter'), 'project')])
def test_a_check_the_sandbox_blocked_asks_before_running_outside_it_and_starts_on_do_not_run(tmp_path, keys, choice):
    import json
    from types import SimpleNamespace
    from textual.widgets import Button, Static
    from relay import conversation_permissions as permissions
    from relay.project_permissions import permissions_view
    from relay.turn_engine import checks_outside_sandbox
    project, session = tmp_path/'project', tmp_path/'session'
    project.mkdir(); (session/'execution-permission').mkdir(parents=True)
    argv = ['C:/venv/python.exe', '-m', 'pytest', '-q']
    limitation = 'ISOLATION_NETWORK_DENIED: every failing test of this check failed because the sandbox denied...'
    request = {'schema': permissions.SCHEMA, 'kind': 'sandbox', 'session': 'parent', 'effect': 'e', 'task': 't',
               'plan': 'a'*64, 'project': str(project.resolve()), 'paths': [], 'checks': {'tests': argv}, 'tests': [],
               'objective': permissions.sandbox_question(limitation), 'status': 'WAITING',
               'sandbox': {'code': 'ISOLATION_NETWORK_DENIED', 'check': 'tests'}}
    request['id'] = permissions.identity(request)
    permissions.save_json(session/'execution-permission'/'request.json', request)
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(110, 40)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.refresh_execution_permission(SimpleNamespace(session=str(session), root=str(project)))
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, ExecutionPermissionPanel)
            assert str(panel.query_one('.panel-title', Static).content) == 'Run this check outside the sandbox?'
            shown = '\n'.join(str(s.content) for s in panel.query(Static))
            for part in ('The sandbox blocked a network connection on this machine', 'cannot judge this check',
                         'Check: tests', repr(argv), 'it can use the network and read your files'):
                assert part in shown
            assert 'Checks:' not in shown and 'independently audited' not in shown
            assert [str(b.label) for b in panel.query(Button)] == [
                'Run it outside the sandbox this turn', "Always run this project's blocked checks outside it",
                'Do not run it']
            assert app.focused.id == 'permission-deny'  # the safe answer is the default
            await pilot.press(*keys); await pilot.pause()
            assert not isinstance(app.screen, ExecutionPermissionPanel)
            assert json.loads((session/'execution-permission'/'answer.json').read_text(encoding='utf-8')) == {
                'id': request['id'], 'choice': choice}
    asyncio.run(scenario())
    # Only "always" is saved, beside the project's mode, shown in /permissions and removable there.
    assert checks_outside_sandbox(project) is (choice == 'project')
    directory = tmp_path/'console'
    from relay import relay_mesh
    view = permissions_view(directory, project, relay_mesh.RELAY_ROOT)
    assert view['defaults'].get('checks_outside_sandbox', False) is (choice == 'project')
    if choice == 'project':
        from relay.project_permissions import save_defaults
        save_defaults(directory, project, relay_mesh.RELAY_ROOT, {**view['defaults'], 'checks_outside_sandbox': False},
                      view['revision'])
        assert not checks_outside_sandbox(project)


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

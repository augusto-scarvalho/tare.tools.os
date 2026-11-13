"""Launch banner and real keyboard answers reuse the existing permission surface."""
import asyncio
import time

import pytest
from compute_plane.native_journal import save_json
from relay import command_permission,execution_scope as scope
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@pytest.mark.parametrize('key,expected',[('enter','once'),('escape','deny')])
def test_command_consent_is_answerable_with_a_keyboard_on_a_small_terminal(tmp_path,key,expected):
    approvals=tmp_path/'approvals';identity='a'*64
    (approvals/identity).mkdir(parents=True)
    save_json(approvals/identity/'request.json',{'schema':command_permission.SCHEMA,'kind':'command',
        'id':identity,'status':'WAITING','expires':time.time()+60,'project':str(tmp_path),'cwd':str(tmp_path),
        'argv':['python','check.py'],'objective':'Review this command.'})
    async def scenario():
        backend=FakeBackend();backend.action_approval_root=str(approvals)
        app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            assert 'Jev/local judge: off' in str(app.query_one('#execution-scope').render())
            app.refresh_command_permission()
            await pilot.pause()
            assert app.screen.query_one('#permission-once').has_focus
            assert not app.screen.query('#permission-project')
            assert str(app.screen.query_one('#permission-once').label)=='Allow this command'
            await pilot.press(key)
            await pilot.pause()
            assert command_permission.read(approvals/identity/'answer.json')['choice']==expected
            assert not backend.launches
    with scope.activated(scope.Scope(disable_judge=True)):asyncio.run(scenario())


def test_one_yes_to_running_without_review_lasts_the_turn_and_a_no_is_not_kept(tmp_path,monkeypatch):
    asked,replies=[],[False,True]
    def approve(action,public,control,*,turn=False):
        asked.append((action,turn));return replies.pop(0)
    monkeypatch.setattr(command_permission,'approve',approve)
    monkeypatch.setattr(command_permission,'allowed_turn',None)
    monkeypatch.setenv(command_permission.ROOT_ENV,str(tmp_path/'turn-1'))
    assert command_permission.unreviewed('first','context',None) is False  # refuses this command only
    assert command_permission.unreviewed('second','context',None) is True  # asks again
    assert command_permission.unreviewed('third','context',None) is True  # the yes covers the turn
    assert asked==[('first',True),('second',True)]
    monkeypatch.setenv(command_permission.ROOT_ENV,str(tmp_path/'turn-2'))  # the next launch: a new approval root
    replies.append(False)
    assert command_permission.unreviewed('fourth','context',None) is False and asked[-1]==('fourth',True)
    monkeypatch.delenv(command_permission.ROOT_ENV)  # no console: nothing is kept
    replies.extend([True,True])
    command_permission.unreviewed('fifth','context',None);command_permission.unreviewed('sixth','context',None)
    assert [a for a,_ in asked[-2:]]==['fifth','sixth']


@pytest.mark.parametrize('turn',[False,True])
def test_the_turn_question_says_what_a_yes_covers_and_the_command_question_is_unchanged(tmp_path,monkeypatch,turn):
    import threading
    from types import SimpleNamespace
    from concurrent.futures import ThreadPoolExecutor
    approvals=tmp_path/'approvals';monkeypatch.setenv(command_permission.ROOT_ENV,str(approvals))
    action=SimpleNamespace(cwd=str(tmp_path/'project'),argv=('python','check.py'),action_sha256='b'*64,effect_id='e1')
    stop=threading.Event();control=SimpleNamespace(should_stop=stop.is_set,monotonic=time.monotonic)
    async def scenario():
        backend=FakeBackend();backend.action_approval_root=str(approvals)
        app=WorkConsole(tmp_path,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            app.refresh_command_permission();await pilot.pause()
            texts=[str(w.render()) for w in app.screen.query('Static')]
            assert ('Run commands without review this turn?' if turn else 'Run this command?') in texts
            assert any("['python', 'check.py']" in t for t in texts)
            assert str(app.screen.query_one('#permission-once').label)==(
                'Run without review this turn' if turn else 'Allow this command')
            await pilot.press('enter');await pilot.pause()
    with scope.activated(scope.Scope(only='local')),ThreadPoolExecutor(1) as executor:
        reply=executor.submit(command_permission.approve,action,'context',control,turn=turn)
        try:
            deadline=time.monotonic()+3
            while not (request:=command_permission.pending(approvals)) and time.monotonic()<deadline:time.sleep(.01)
            assert request['argv']==['python','check.py']
            if turn:assert request['scope']=='turn' and request['objective']==command_permission.TURN_QUESTION
            else:assert 'scope' not in request and request['objective']==(
                'Automatic judges are off or unavailable. Review this command before it runs.')
            asyncio.run(scenario())
            assert reply.result(timeout=3) is True
        finally:stop.set()  # never leave the asker waiting
    assert not approvals.exists()


def test_the_launch_banner_and_header_name_the_pinned_coordinator(tmp_path, monkeypatch):
    from relay import coordinator_policy
    from relay.console_connection import deployment_for
    pins = scope.Scope(pins=(('explorer', 'openrouter/deepseek@high'), ('implementer', 'openrouter/deepseek@high'),
                             ('auditor', 'openai/codex-cli@high')))
    assert pins.label() == 'explorer, implementer: openrouter/deepseek@high · auditor: openai/codex-cli@high'
    monkeypatch.setattr(coordinator_policy, 'select', lambda raw: {'pinned': raw})
    # Before the first turn the header reads the coordinator a new conversation runs, not the saved preference.
    assert deployment_for((str(tmp_path), '', '', 'google/agy-cli@high', False)) == {'pinned': 'google/agy-cli@high'}
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant',
                          default_coordinator='google/agy-cli@high')
        async with app.run_test(size=(120, 30)):
            await app.workers.wait_for_complete()
            assert 'Coordinator: google/agy-cli@high' in str(app.query_one('#execution-scope').render())
            assert app.header_key()[1:] == ('', '', 'google/agy-cli@high', False)
    with scope.activated(pins):
        asyncio.run(scenario())

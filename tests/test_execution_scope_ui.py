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
            await pilot.press(key)
            await pilot.pause()
            assert command_permission.read(approvals/identity/'answer.json')['choice']==expected
            assert not backend.launches
    with scope.activated(scope.Scope(disable_judge=True)):asyncio.run(scenario())


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

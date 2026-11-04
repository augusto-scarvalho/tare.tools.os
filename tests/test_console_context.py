import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest
from textual.widgets import Button, Input, OptionList, Select, TextArea

from relay.console_backend import Selection, command
from relay.console_context import context_prompt, path_choices, path_token
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def test_path_completion_is_nonrecursive_and_references_are_project_bound(tmp_path):
    root=tmp_path/'one'; root.mkdir()
    (root/'src').mkdir(); (root/'src'/'a.py').write_text('private body')
    (root/'space name.txt').write_text('private body')
    (root/'.git').mkdir()
    assert path_choices(root,path_token('@',(0,1)))[0]==[('src',True),('space name.txt',False)]
    assert path_choices(root,path_token('./src/',(0,6)))[0]==[('src/a.py',False)]
    assert path_choices(root,path_token('@"space n',(0,9)))[0]==[('space name.txt',False)]
    absolute='@'+str(root/'src')+'/'
    assert path_choices(root,path_token(absolute,(0,len(absolute))))[0]==[('src/a.py',False)]
    assert path_token('mail@example.org',(0,16)) is None
    assert path_token('https://example.org/a',(0,21)) is None
    selected=Selection(str(root),prompt='Explain it',context_root=str(root),context_paths=['src/a.py'])
    result=context_prompt(selected)
    assert 'src/a.py' in result and 'private body' not in result
    other=tmp_path/'two';other.mkdir()
    with pytest.raises(ValueError,match='another project'):
        context_prompt(replace(selected,root=str(other)))
    with pytest.raises(ValueError,match='inside this project'):
        context_prompt(replace(selected,context_paths=['../two']))
    (root/'src'/'a.py').unlink()
    with pytest.raises(ValueError,match='unavailable'):
        command(selected)


async def edit(pilot,prompt,text):
    prompt.focus();prompt.load_text(text)
    lines=text.split('\n');prompt.move_cursor((len(lines)-1,len(lines[-1])))
    await pilot.pause(.3)


async def choices(app,pilot):
    for _ in range(60):
        await pilot.pause(.05)
        if app.query_one('#context-box').display:
            return [row[0] for row in app.context_choices]
    pytest.fail('Context suggestions did not appear')


@pytest.mark.parametrize('size',[(80,24),(130,44)])
def test_picker_selects_file_and_folder_without_sending_then_restores_draft(tmp_path,monkeypatch,size):
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    root=tmp_path/'project';root.mkdir()
    (root/'src').mkdir();(root/'src'/'main.py').write_text('value = 1\n')
    (root/'space name.md').write_text('# notes')
    async def scenario():
        app=WorkConsole(root,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt=app.query_one('#prompt',TextArea)
            await edit(pilot,prompt,'Explain @s')
            assert await choices(app,pilot)==['src','space name.md']
            await pilot.press('tab')
            assert prompt.text=='Explain @src/'
            assert await choices(app,pilot)==['src/main.py']
            await pilot.press('enter');await pilot.pause()
            assert prompt.text=='Explain ' and app.context_paths==['src/main.py']
            assert app.query_one('#context-chips').display
            await edit(pilot,prompt,'Explain @"space n')
            assert await choices(app,pilot)==['space name.md']
            await pilot.press('tab');await pilot.pause()
            assert app.context_paths==['src/main.py','space name.md']
            assert not app.backend.launches and not app.demand_id
            await edit(pilot,prompt,'Explain ./sr')
            assert await choices(app,pilot)==['src']
            await pilot.press('enter');await pilot.pause()
            assert app.context_paths[-1]=='src'
            app.save_demand()
            saved_id=app.demand_id
            saved=app.demand_state['snapshot']['selection']
            assert saved['context_paths']==['src/main.py','space name.md','src']
            app.clear_context();await app.workers.wait_for_complete()
            app.restore_selection(saved);await app.workers.wait_for_complete();await pilot.pause()
            assert app.context_paths==saved['context_paths']
            buttons=list(app.query('#context-chips Button'))
            await pilot.click(buttons[0]);await pilot.pause()
            assert app.context_paths==['space name.md','src']
            assert app.query_one('#start').region.bottom<=size[1]
            app.query_one('#root',Input).value=str(tmp_path)
            app.prepare_workspace();await app.workers.wait_for_complete()
            assert app.context_paths==[] and app.selection().context_paths==[]
            assert not app.backend.launches
        reopened=WorkConsole(root,backend=FakeBackend(),initial_mode='assistant')
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete()
            await reopened.list_demands().wait()
            reopened.query_one('#saved-demand',Select).value=saved_id
            reopened.open_demand();await reopened.workers.wait_for_complete()
            assert reopened.context_paths==saved['context_paths']
            assert Path(reopened.context_root)==root
            assert not reopened.backend.launches
    asyncio.run(scenario())


def test_escape_stale_results_and_mouse_selection_do_not_submit(tmp_path):
    (tmp_path/'a.py').write_text('value=1')
    async def scenario():
        app=WorkConsole(tmp_path,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(100,32)) as pilot:
            await app.workers.wait_for_complete()
            prompt=app.query_one('#prompt',TextArea)
            await edit(pilot,prompt,'@a');assert await choices(app,pilot)==['a.py']
            await pilot.press('escape','left');await pilot.pause(.2)
            assert not app.query_one('#context-box').display
            await pilot.press('right');await pilot.pause(.2)
            assert not app.query_one('#context-box').display
            await edit(pilot,prompt,'@a.')
            assert await choices(app,pilot)==['a.py']
            await pilot.click('#context-menu',offset=(1,0));await pilot.pause()
            assert app.context_paths==['a.py'] and not app.backend.launches
            await edit(pilot,prompt,'@')
            prompt.load_text('normal text');await pilot.pause(.3)
            assert not app.query_one('#context-box').display
            assert app.context_key('enter') is False
    asyncio.run(scenario())


def test_queued_context_uses_saved_paths_and_preserves_a_new_attachment(tmp_path,monkeypatch):
    from threading import Event
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    root=tmp_path/'project';root.mkdir()
    for name in ('a.py','b.py'): (root/name).write_text('content')
    received,release=Event(),Event()
    calls=[]
    def send(selection,text,identity):
        calls.append(text);received.set()
        assert release.wait(10)
    async def scenario():
        backend=FakeBackend();backend.send_message=send;backend.message_status=lambda *a:'IN_CONTEXT'
        app=WorkConsole(root,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(100,32)) as pilot:
            await app.workers.wait_for_complete()
            app.active=Selection(str(root),session='session')
            app.controls(True)
            prompt=app.query_one('#prompt',TextArea);prompt.load_text('Inspect this')
            app.context_root=str(root);app.context_paths=['a.py']
            app.send_operator_message()
            for _ in range(80):
                await pilot.pause(.025)
                if received.is_set(): break
            assert received.is_set()
            app.context_paths=['b.py']
            release.set()
            await app.workers.wait_for_complete()
            assert '"path": "a.py"' in calls[0] and 'b.py' not in calls[0]
            assert prompt.text=='Inspect this' and app.context_paths==['b.py']
            assert not backend.launches
            app.controls(False)
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_late_directory_result_cannot_reappear_in_another_workspace(tmp_path,monkeypatch):
    from threading import Event
    import relay.console_context as context
    first,second=tmp_path/'one',tmp_path/'two'
    first.mkdir();second.mkdir()
    (first/'a.py').write_text('one');(second/'b.py').write_text('two')
    started,release=Event(),Event()
    original=context.path_choices
    def delayed(root,token):
        if Path(root)==first:
            started.set();assert release.wait(10)
        return original(root,token)
    monkeypatch.setattr(context,'path_choices',delayed)
    async def scenario():
        app=WorkConsole(first,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(100,32)) as pilot:
            await app.workers.wait_for_complete()
            prompt=app.query_one('#prompt',TextArea)
            await edit(pilot,prompt,'@a')
            for _ in range(80):
                await pilot.pause(.025)
                if started.is_set():break
            assert started.is_set()
            app.query_one('#root',Input).value=str(second)
            app.prepare_workspace()
            await edit(pilot,prompt,'@b')
            assert await choices(app,pilot)==['b.py']
            release.set();await pilot.pause(.2)
            assert app.context_choices==[('b.py',False)]
            await pilot.press('enter');await pilot.pause()
            assert Path(app.context_root)==second and app.context_paths==['b.py']
            assert not app.backend.launches
    try:
        asyncio.run(scenario())
    finally:
        release.set()

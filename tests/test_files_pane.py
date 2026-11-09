import asyncio
import json

import pytest
from textual.widgets import OptionList, Select, TextArea

from relay.files_pane import FilesPane, read_open, save_open, touches
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def row(operation, target, state='succeeded', identity=None, detail='', **extra):
    return dict(kind='tool-activity', id=identity or f's/effect/{operation}/{target}/{state}', operation=operation,
                target=target, state=state, detail=detail, receipt='1 · '+'a'*64, duration_ms=None, **extra)


def source(tmp_path, stage='Implement'):
    from pathlib import Path
    from relay import relay_mesh
    return {'session': str(tmp_path/'c'/'units'/'u'/'session-1'), 'kind': 'session', 'mode': 'conversation',
            'owner': str(tmp_path/'c'), 'project': str(tmp_path.resolve()),
            'control_root': str(Path(relay_mesh.RELAY_ROOT).resolve()), 'stage': stage, 'work': ''}


def test_touches_group_by_turn_and_keep_the_last_state(tmp_path):
    history = ['You: first', row('read', 'a.py'), row('edit', 'a.py'), row('read', 'a.py'), row('search', 'value'),
               'reply', 'You: second', row('read', 'b.py', 'running'),
               row('edit', 'c.py', source=source(tmp_path)), row('edit', 'd.py', detail='Deleted file.'),
               row('folder_read', 'docs: guide.md'), row('read', 'e.py', 'refused', reason='COORDINATOR_ROUTING_REQUIRED')]
    session = {touch.path: touch for touch in touches(history, turn_only=False)}
    assert list(session) == ['a.py', 'b.py', 'c.py', 'd.py', 'docs: guide.md', 'e.py']  # a search is no file
    assert session['a.py'].edited and not session['a.py'].proposed  # an edit outranks the later read
    assert session['b.py'].running and session['c.py'].proposed and session['c.py'].role == 'impl'
    assert session['d.py'].deleted and session['docs: guide.md'].outside
    assert session['e.py'].failed and session['e.py'].reason == 'COORDINATOR_ROUTING_REQUIRED'
    assert [touch.path for touch in touches(history, turn_only=True)] == ['b.py', 'c.py', 'd.py', 'docs: guide.md', 'e.py']


def test_open_state_is_saved_per_machine_and_a_bad_file_is_refused(tmp_path):
    assert read_open(tmp_path) is False
    save_open(tmp_path, True)
    assert read_open(tmp_path) is True
    (tmp_path/'files-pane.json').write_text(json.dumps({'schema': 'other', 'open': True}))
    with pytest.raises(ValueError):
        read_open(tmp_path)


@pytest.mark.parametrize('size', [(150, 52), (80, 24)])
def test_f8_and_slash_files_open_the_pane_and_compact_hides_the_chat(tmp_path, size):
    project = tmp_path/'project'; project.mkdir()
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            pane, detail = app.query_one(FilesPane), app.query_one('#detail')
            assert not pane.display  # closed on a new machine
            await pilot.press('f8'); await pilot.pause()
            assert pane.display and app.focused is app.query_one('#files-touched', OptionList)
            assert detail.display is (size[0] >= 100)
            await pilot.press('f8'); await pilot.pause()
            assert not pane.display and detail.display and app.focused is app.query_one('#prompt', TextArea)
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('/files'); app.execute(False); await pilot.pause()
            assert pane.display and not prompt.text
            app.controls(True)  # a running turn leaves the pane usable
            assert not any(widget.disabled for widget in pane.query('*'))
            app.controls(False)
            await pilot.press('f1'); await pilot.pause()
            assert pane.display is (size[0] >= 140)  # F1 covers it unless there is room for both
            await pilot.press('f1'); await pilot.pause()
            assert pane.display
            assert not backend.launches
        reopened = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant',
                               storage_preferred=tmp_path/'state')
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete(); await pilot.pause()
            assert reopened.query_one(FilesPane).display  # the last state is remembered
    asyncio.run(scenario())


def test_touched_rows_follow_activity_for_every_role_and_survive_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    project = tmp_path/'project'; project.mkdir()
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('f8'); await pilot.pause()
            app.assistant_say('You: change the parser')
            app.assistant_activity({'channel': 'activity', 'activity': row('read', 'parser.py')})
            app.assistant_activity({'channel': 'activity', 'activity': row(
                'edit', 'parser.py', identity='child/effect/1', source=source(project))})
            await pilot.pause(.6)
            listing = app.query_one('#files-touched', OptionList)
            labels = [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]
            assert labels == ['p parser.py · impl']
            assert 'This turn · read 0 · edited 1' in str(app.query_one('#files-summary').render())
            app.checkpoint('draft_saved'); saved_id = app.demand_id
            app.new_demand(); await pilot.pause(.6)
            assert listing.option_count == 0
            await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved_id
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause(.6)
            assert [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)] == labels
            assert not app.backend.launches
    asyncio.run(scenario())

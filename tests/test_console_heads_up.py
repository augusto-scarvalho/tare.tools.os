"""The heads-up note's buttons in the real console (headless Textual): fill the composer, record a label."""
import asyncio
import json

from textual.widgets import Button, TextArea

from relay import heads_up
from relay.console_output import HeadsUpNote, Transcript
from relay.native_console import WorkConsole
from test_native_console import FakeBackend

NOTE = 'The verifier failed, so the fix is untested.'


def test_the_note_buttons_fill_the_composer_and_record_labels(tmp_path, monkeypatch):
    monkeypatch.delenv('TARE_HEADS_UP', raising=False)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    session = tmp_path/'session'; session.mkdir()
    heads_up.keep(session, [NOTE], {'request': 'Fix it', 'reply': 'Done.'}, {})
    log = tmp_path/'state'/'serve'/'heads-up.jsonl'

    def rows():
        return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = session
            output, prompt = app.query_one('#output', Transcript), app.query_one('#prompt', TextArea)
            app.show_heads_up([NOTE], str(tmp_path/'elsewhere')); await pilot.pause()
            assert not output.query(HeadsUpNote)  # another conversation: a notification only
            app.show_heads_up([NOTE], str(session)); await pilot.pause(.3)  # the note's Markdown settles its height
            note = output.query_one(HeadsUpNote)
            buttons = {b.id: b for b in note.query(Button)}
            assert [str(b.label) for b in buttons.values()] == ['Ask the agent', 'Useful', 'Not useful']
            assert {b.region.y for b in buttons.values()} == {buttons['heads-up-ask'].region.y}  # one row
            assert max(b.region.right for b in buttons.values()) <= 80

            prompt.load_text('My draft')
            await pilot.click('#heads-up-ask'); await pilot.pause()
            assert prompt.text == 'My draft\n\n' + heads_up.ask([NOTE]) and app.focused is prompt
            assert not app.busy and rows() == []  # nothing sent, nothing logged: only a sent note counts
            assert heads_up.shown(session)['noted'][0]['loaded']  # kept, so a rewritten message still finds its note
            prompt.load_text('/heads-up'); app.execute(False); await pilot.pause(.3)  # the composer resized the transcript
            assert prompt.text == heads_up.ask([NOTE]) and rows() == []

            await pilot.click('#heads-up-useful'); await pilot.pause()
            assert [str(buttons[i].label) for i in buttons] == ['Ask the agent', 'Useful ✓', 'Not useful']
            await pilot.click('#heads-up-not-useful'); await pilot.pause()
            assert [str(buttons[i].label) for i in buttons] == ['Ask the agent', 'Useful', 'Not useful ✓']
            assert heads_up.shown(session)['noted'][0]['label'] == 'not_useful'
            assert [(r['event'], r['value']) for r in rows()] == [('label', 'useful'), ('label', 'not_useful')]

            # keyboard only: Tab to a button and Enter
            buttons['heads-up-useful'].focus(); await pilot.press('enter'); await pilot.pause()
            assert heads_up.shown(session)['noted'][0]['label'] == 'useful'
    asyncio.run(scenario())


def test_missed_records_the_last_reply_without_sending_anything(tmp_path, monkeypatch):
    from test_heads_up import session as journal  # a conversation whose journal holds a finished turn
    monkeypatch.delenv('TARE_HEADS_UP', raising=False)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    folder = journal(tmp_path)
    log = tmp_path/'state'/'serve'/'heads-up.jsonl'
    words = 'the reply hid the failed audit, "really"!  (twice)'

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(root, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(.3)
            prompt, said = app.query_one('#prompt', TextArea), []
            app.set_status = said.append
            prompt.load_text('/missed ' + words)  # no conversation yet: nothing to flag, the draft stays
            app.execute(False); await pilot.pause()
            assert prompt.text == '/missed ' + words and 'no finished reply' in said[-1] and not log.exists()
            app.assistant_session = folder
            app.execute(False); await pilot.pause()
            assert prompt.text == '' and 'Recorded' in said[-1] and 'on this machine' in said[-1]
            assert not app.busy and not backend.launches  # nothing was sent, no model was called
            [entry] = heads_up.shown(folder)['missed']
            assert entry['text'] == words and entry['turn'] == heads_up._digest('Done: all tests pass.')
            [row] = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
            assert (row['event'], row['said'], row['reply_sha256']) == ('miss', True, entry['turn'])
            assert 'audit' not in json.dumps(row)

            app.busy = True  # about the last finished reply: allowed during a turn (as /team is), with no words too
            prompt.load_text('/missed'); app.execute(False); await pilot.pause()
            assert prompt.text == '' and 'Recorded' in said[-1] and not backend.launches
            assert [m['text'] for m in heads_up.shown(folder)['missed']] == [words, '']
            app.busy = False
            heads_up.set_enabled(True)
    asyncio.run(scenario())

"""User journeys for bounded review and discovery, without provider calls."""
import asyncio

import pytest
from textual.widgets import Button, Input, TextArea

from relay.console_workbench import WorkbenchPanel, artifact, correction_draft, observed_tasks
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@pytest.mark.parametrize('area', ['preview', 'comment'])
def test_preview_ctrl_end_reaches_last_line_without_scrolling_conversation(tmp_path, monkeypatch, area):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            latest = []
            monkeypatch.setattr(app.query_one('#output'), 'scroll_end', lambda **kwargs: latest.append(True))
            body = '\n'.join(f'Observation {i}' for i in range(200))+'\nEnd of evidence'
            panel = WorkbenchPanel('Usage', [{'id':'evidence', 'label':'Evidence', 'body':body}],
                                   kind='artifacts' if area == 'comment' else 'usage')
            app.push_screen(panel)
            await pilot.pause()
            preview = panel.query_one('#workbench-'+area, TextArea)
            if area == 'comment':
                preview.load_text(body)
            await pilot.click('#workbench-'+area)
            assert app.focused is preview
            await pilot.press('ctrl+end')
            assert preview.cursor_location == (200, len('End of evidence'))
            await pilot.press('ctrl+home')
            assert preview.cursor_location == (0, 0)
            assert not latest and not app.backend.launches
            await pilot.press('escape', 'ctrl+end')
            assert latest == [True]
    asyncio.run(scenario())


def test_feedback_is_bounded_and_references_exact_captured_content():
    windows = artifact('Windows response', '\x1b[31mfirst\r\nsecond\x1b[0m')
    assert windows['body'] == 'first\nsecond'
    assert 'first\\nsecond' in correction_draft('', windows, 'first\nsecond', 'Review both lines')
    row = artifact('Response', 'original passage\nother text', history_index=4)
    value = correction_draft('Keep my draft', row, 'original passage', 'Explain this')
    assert value.startswith('Keep my draft\n\n')
    assert row['id'] in value and '"history_index": 4' in value
    assert 'other text' not in value
    for excerpt, comment in [('absent', 'fix'), ('original passage', ''),
                             ('original passage', 'x'*4097)]:
        with pytest.raises(ValueError):
            correction_draft('', row, excerpt, comment)
    with pytest.raises(ValueError, match='16 KiB'):
        correction_draft('x'*16384, row, 'original passage', 'fix')


def test_tasks_do_not_promote_tool_success_or_stopped_observation_to_task_outcome():
    source = {'session': '/owned/child', 'stage': 'Plan review'}
    history = [{'kind': 'tool-activity', 'operation': 'read', 'state': 'succeeded',
                'target': 'plan.md', 'source': source},
               {'kind': 'tool-activity', 'operation': 'verify', 'state': 'running',
                'target': 'unit', 'source': source}]
    rows = observed_tasks(history)
    assert len(rows) == 2 and 'unconfirmed' in rows[1]['label']
    assert 'Overall task outcome is not inferred' in rows[1]['body']
    live = observed_tasks(history, busy=True)
    assert 'last operation running' in live[1]['label']
    assert history[1]['state'] == 'running'


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_feedback_and_skill_drafts_survive_without_launching_models(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    skill = root/'skills'/'review'/'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: Review\ndescription: Check changes\n---\nRead the diff.', encoding='utf-8')

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Preserve this draft')
            app.assistant_history = [{'kind': 'execution-result', 'body': 'Inspect the parser.', 'status': 'PROPOSED'}]
            app.action_artifacts(); await pilot.pause()
            panel = app.screen
            assert isinstance(panel, WorkbenchPanel)
            assert panel.region.contains_region(panel.query_one('#workbench-add').region)
            panel.query_one('#workbench-comment', TextArea).load_text('Also check empty input.')
            await pilot.press('ctrl+enter', 'f2', 'f3', 'f4')
            assert app.screen is panel and not app.backend.launches
            await pilot.click('#workbench-add'); await pilot.pause()
            assert app.screen is not panel
            assert prompt.text.startswith('Preserve this draft\n\nReview feedback')
            assert 'Also check empty input.' in prompt.text
            assert app.demand_state['snapshot']['selection']['prompt'] == prompt.text
            app.action_skills(); await pilot.pause()
            panel = app.screen
            assert 'Read the diff.' in panel.query_one('#workbench-preview', TextArea).text
            await pilot.click('#workbench-add'); await pilot.pause()
            assert 'skills/review/SKILL.md' in prompt.text
            assert 'Preserve this draft' in prompt.text
            assert app.demand_state['snapshot']['selection']['prompt'] == prompt.text
            assert not app.backend.launches
            # Reopening the catalog verifies content again at Add, not just at Preview.
            app.action_skills(); await pilot.pause()
            skill.write_text('Changed after preview.', encoding='utf-8')
            panel = app.screen
            before = prompt.text
            await pilot.click('#workbench-add'); await pilot.pause()
            assert app.screen is panel and prompt.text == before
            await pilot.press('escape'); await pilot.pause()
            app.action_artifacts(); await pilot.pause()
            panel = app.screen
            panel.query_one('#workbench-comment', TextArea).load_text('Do not misroute this.')
            # The visible root field changing invalidates the captured destination.
            app.query_one('#root', Input).value = str(tmp_path/'other')
            await pilot.click('#workbench-add'); await pilot.pause()
            assert app.screen is panel and prompt.text == before
            assert not app.backend.launches
    asyncio.run(scenario())


def test_commands_open_views_and_tasks_update_while_busy(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            for command in ('/artifacts', '/skills', '/tools', '/tasks'):
                prompt.load_text(command)
                app.execute(False); await pilot.pause()
                assert isinstance(app.screen, WorkbenchPanel), command
                assert not prompt.text
                assert not app.backend.launches
                await pilot.press('escape'); await pilot.pause()
            prompt.load_text('/skills unexpected')
            app.execute(False); await pilot.pause()
            assert prompt.text == '/skills unexpected'
            app.controls(True)
            prompt.load_text('/tasks'); app.execute(False); await pilot.pause()
            panel = app.screen
            assert isinstance(panel, WorkbenchPanel)
            app.assistant_history.append({'kind': 'tool-activity', 'id': 'call1', 'operation': 'read',
                                          'state': 'succeeded', 'target': 'notes.md'})
            panel.refresh_observations(); await pilot.pause()
            assert len(panel.rows) == 2 and 'last operation succeeded' in panel.rows[1]['label']
            assert not app.backend.launches
            app.controls(False)
    asyncio.run(scenario())


def test_failed_checkpoint_keeps_original_draft_and_comment(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Original')
            app.open_artifact(artifact('Diff', '+ new line', effect_id='edit1'))
            await pilot.pause()
            panel = app.screen
            panel.query_one('#workbench-comment', TextArea).load_text('Change this.')
            def fail(*args, **kwargs):
                raise OSError('Disk full')
            monkeypatch.setattr(app, 'checkpoint', fail)
            await pilot.click('#workbench-add'); await pilot.pause()
            assert app.screen is panel and prompt.text == 'Original'
            assert panel.query_one('#workbench-comment', TextArea).text == 'Change this.'
            assert not app.backend.launches
    asyncio.run(scenario())


from relay.console_workbench import usage_rows

def test_estimate_is_visible_but_not_a_charge():
    body = usage_rows({'model_calls': 2, 'known_cost_by_currency': {'USD': '0.12'},
                      'provider_estimates_by_currency': {'USD': '0.87', 'EUR': '0.50'},
                      'unpriced_or_partial_calls': 1})[0]['body']
    assert 'Known model cost: USD 0.12' in body
    assert 'Provider estimate (not billed): EUR 0.50, USD 0.87' in body
    assert 'Unpriced or partially priced calls: 1' in body

def test_missing_price_remains_unknown():
    body = usage_rows({'model_calls': 1, 'provider_estimates_by_currency': {'USD': '0.09'}})[0]['body']
    assert 'Known model cost: Unknown' in body
    assert 'Provider estimate (not billed): USD 0.09' in body
    assert 'Provider estimate' not in usage_rows({'model_calls': 0})[0]['body']


@pytest.mark.parametrize('size',[(80,24),(130,44)])
def test_explicit_verifier_is_available_in_ordinary_conversations(tmp_path,size):
    from textual.widgets import Select
    async def scenario():
        app=WorkConsole(tmp_path,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=size) as gui:
            await app.workers.wait_for_complete()
            verifier=app.query_one('#verifier',Input)
            verifier.value='["python","-m","pytest","tests/test_one.py"]'
            for mode in ('assistant','session','plan'):
                app.query_one('#mode',Select).value=mode
                await gui.pause()
                assert app.query_one('#direct-check-options').display == (mode!='plan')
                if mode!='plan':
                    assert app.selection().verifier==verifier.value
            assert not app.backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('unavailable',[False,True])
def test_usage_exposes_global_pilot_and_keeps_existing_rows_and_draft(tmp_path,monkeypatch,unavailable):
    import sys
    from relay import flow_usage,test_selection_pilot as collection
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'storage'))
    project=tmp_path/'other-project';project.mkdir()
    result=collection.start(str(project),[sys.executable,'-m','pytest','tests'])
    collection.stop(result['wrapper_argv'][-1])
    monkeypatch.setattr(flow_usage,'completed_work_usage_rows',lambda *_,**kwargs:[
        {'id':'completed-known','label':'Saved Work data','body':'Preserved existing rows.'}])
    if unavailable:
        def broken():
            raise OSError('collector unavailable')
        monkeypatch.setattr(collection,'usage_rows',broken)
    async def scenario():
        workspace=tmp_path/'current-project';workspace.mkdir()
        app=WorkConsole(workspace,backend=FakeBackend(),initial_mode='assistant')
        async with app.run_test(size=(130,44)) as gui:
            await app.workers.wait_for_complete()
            draft=app.query_one('#prompt',TextArea);draft.load_text('Keep this draft')
            app.action_usage();await gui.pause()
            panel=app.screen
            assert isinstance(panel,WorkbenchPanel)
            assert any(r['id']=='completed-known' for r in panel.rows)
            assert any(r['id']=='usage' for r in panel.rows)
            if unavailable:
                assert any(r['id']=='test-selection-unavailable' for r in panel.rows)
            else:
                row=next(r for r in panel.rows if r['id'].startswith('test-selection-'))
                assert str(project) in row['body']
                assert 'REVIEW_REQUIRED' in row['body']
                assert 'review --decision keep-full' in row['body']
            await gui.press('escape')
            assert draft.text=='Keep this draft' and not app.backend.launches
    asyncio.run(scenario())


def test_public_test_selection_dispatch_does_not_execute_tests(monkeypatch):
    import tare_ops
    from relay import test_selection_pilot as collection
    calls=[]
    monkeypatch.setattr(collection,'main',lambda argv:calls.append(argv) or 0)
    assert tare_ops.main(['test-selection','status'])==0
    assert calls==[['status']]

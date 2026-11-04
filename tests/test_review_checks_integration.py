"""Checks UI against saved native sessions and actual Kernel-managed processes."""
import asyncio
import json

import pytest
from textual.app import App
from textual.widgets import Button,Select,Static

from relay.changes_panel import ChangesPanel
from relay.review_checks_panel import ReviewChecksPanel,CheckRunConfirmDialog
from test_native_review_checks import setup,checked,set_command
from test_review_checks_panel import until,content,sample_checks_inventory


@pytest.mark.parametrize('size',[(80,24),(130,44)])
@pytest.mark.parametrize('outcome',['pass','fail','cancel'])
def test_real_check_from_changes_and_reopened_history(checked,size,outcome):
    _,custody,root,client=checked
    if outcome=='fail':set_command(checked,"print('failing output'); raise SystemExit(7)")
    if outcome=='cancel':set_command(checked,"import time; print('live output',flush=True); time.sleep(30)")
    journal=(custody/'journal.jsonl').read_bytes();calls=len(client.calls)
    async def scenario():
        app=App()
        async with app.run_test(size=size) as pilot:
            changes=ChangesPanel(custody,root);app.push_screen(changes)
            await until(pilot,lambda:changes.diff_text is not None)
            changes.query_one('#changes-checks',Button).press()
            await until(pilot,lambda:isinstance(app.screen,ReviewChecksPanel) and bool(app.screen.checks_by_id))
            panel=app.screen
            panel.request_run();await until(pilot,lambda:isinstance(app.screen,CheckRunConfirmDialog))
            dialog=app.screen
            assert json.loads(content(dialog.query_one('#check-confirm-command',Static)))[0]
            assert dialog.focused.id=='check-confirm-cancel'
            button=dialog.query_one('#check-confirm-confirm',Button)
            assert button.region.bottom<=size[1] and str(button.label) in button.render_line(0).text
            button.press()
            if outcome=='cancel':
                await until(pilot,lambda:'live output' in content(panel.query_one('#review-checks-output',Static)))
                assert panel.running
                panel.query_one('#review-checks-cancel',Button).press()
            expected={'pass':'PASS','fail':'FAIL','cancel':'CANCELLED'}[outcome]
            await until(pilot,lambda:not panel.running and bool(panel.runs_by_id),max_ticks=200)
            assert panel.runs[0]['status']==expected
            await until(pilot,lambda:bool(panel.current_output))
            identity=panel.runs[0]['run_id'];output=panel.current_output
            await pilot.press('escape')
            await until(pilot,lambda:app.screen is changes)
            changes.open_checks()
            await until(pilot,lambda:isinstance(app.screen,ReviewChecksPanel) and bool(app.screen.current_output))
            assert app.screen.runs[0]['run_id']==identity and app.screen.current_output==output
            assert not app.screen.running
    asyncio.run(scenario())
    assert (custody/'journal.jsonl').read_bytes()==journal and len(client.calls)==calls


def test_refresh_error_discards_old_run_plan(tmp_path):
    fail=False
    def reader(*a,**k):
        if fail:raise ValueError('inventory disconnected')
        return sample_checks_inventory()
    async def scenario():
        nonlocal fail
        app=App()
        async with app.run_test(size=(80,24)) as pilot:
            panel=ReviewChecksPanel(tmp_path,tmp_path,reader=reader);app.push_screen(panel)
            await until(pilot,lambda:bool(panel.checks_by_id))
            fail=True;panel.action_reload()
            await until(pilot,lambda:'disconnected' in content(panel.query_one('#review-checks-details',Static)))
            assert panel.query_one('#review-checks-run',Button).disabled
            panel.request_run();assert panel.active_confirmation is None
    asyncio.run(scenario())


def test_long_confirmation_keeps_exact_arguments_and_keyboard_access():
    from textual.containers import VerticalScroll
    async def scenario():
        app=App()
        async with app.run_test(size=(80,24)) as pilot:
            argv=['program with spaces','argument "quoted"',*['long argument '+str(i) for i in range(30)]]
            dialog=CheckRunConfirmDialog(check_id='c',argv=argv,repository='long-project-'*12,timeout_seconds=300,plan='p')
            app.push_screen(dialog);await until(pilot,lambda:bool(dialog.query('#check-confirm-confirm')))
            assert json.loads(content(dialog.query_one('#check-confirm-command',Static)))==argv
            assert dialog.focused.id=='check-confirm-cancel'
            scroll=dialog.query_one('#check-confirm-scroll',VerticalScroll);scroll.focus()
            await pilot.pause();await pilot.press('end')
            await until(pilot,lambda:scroll.scroll_y==scroll.max_scroll_y)  # End animates the scroll
            assert scroll.scroll_y>0
            for selector in ['#check-confirm-cancel','#check-confirm-confirm']:
                b=dialog.query_one(selector,Button)
                assert b.region.bottom<=24 and str(b.label) in b.render_line(0).text
            await pilot.press('escape')
            assert app.screen is not dialog
    asyncio.run(scenario())


def test_first_run_preflight_failure_stays_visible_after_refresh(checked):
    _,custody,root,_=checked
    path=custody/'native-agent.json';state=json.loads(path.read_text())
    state['config']['verifiers']['unit']['argv']=[str(root/'missing-command-82379')]
    path.write_text(json.dumps(state))
    async def scenario():
        app=App()
        async with app.run_test(size=(80,24)) as pilot:
            panel=ReviewChecksPanel(custody,root);app.push_screen(panel)
            await until(pilot,lambda:bool(panel.checks_by_id))
            panel.request_run();await until(pilot,lambda:isinstance(app.screen,CheckRunConfirmDialog))
            app.screen.query_one('#check-confirm-confirm',Button).press()
            await until(pilot,lambda:panel.mutation_result is not None and not panel.running)
            await until(pilot,lambda:bool(panel.checks_by_id))
            assert 'Executable not found' in content(panel.query_one('#review-checks-status',Static))
            assert panel.runs==[] and not (custody/'review'/'check-runs').exists()
    asyncio.run(scenario())

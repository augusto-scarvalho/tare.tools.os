import asyncio

import pytest
from textual.widgets import Button, Checkbox, Input, SelectionList, TextArea

from relay.console_storage import StoragePanel
from relay.native_console import WorkConsole
from test_native_console import FakeBackend
from test_storage import owned


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_user_can_preview_select_and_clean_without_losing_rollback(owned, size, monkeypatch):
    root, home, old, build = owned
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root))
    monkeypatch.setenv('TARE_PRODUCT_HOME', str(home))

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/storage', '')
            await pilot.pause()
            await app.workers.wait_for_complete()
            panel = app.screen
            assert isinstance(panel, StoragePanel) and panel.report is not None
            assert old.exists() and panel.query_one('#storage-clean', Button).disabled
            for name in ('scan', 'clean', 'confirm', 'items'):
                assert panel.region.contains_region(panel.query_one('#storage-'+name).region)
            await pilot.press('ctrl+enter', 'f2', 'f3', 'f4')
            assert app.screen is panel and not app.backend.launches
            choices = panel.query_one('#storage-items', SelectionList)
            row = next(r for r in panel.report['rows'] if r['kind'] == 'wheel' and r['eligible'])
            choices.select(row['id'])
            await pilot.pause()
            assert panel.query_one('#storage-clean', Button).disabled
            await pilot.click('#storage-confirm')
            await pilot.pause()
            assert not panel.query_one('#storage-clean', Button).disabled
            await pilot.click('#storage-clean')
            await app.workers.wait_for_complete()
            assert old.exists()  # Not selected.
            assert (build/'wheels-1/package.whl').exists()
            assert not (build/'wheels-2/package.whl').exists()
            assert panel.report is None and panel.query_one('#storage-clean', Button).disabled
            await pilot.press('escape')
            assert app.screen is not panel
    asyncio.run(scenario())


def test_root_edit_invalidates_preview(owned, monkeypatch):
    root, home, old, build = owned
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root))
    monkeypatch.setenv('TARE_PRODUCT_HOME', str(home))
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.dispatch_conversation_command('/storage', '')
            await pilot.pause(); await app.workers.wait_for_complete()
            panel = app.screen
            panel.query_one('#storage-items', SelectionList).select_all()
            await pilot.pause()
            panel.query_one('#storage-confirm', Checkbox).value = True
            await pilot.pause()
            panel.query_one('#storage-root', Input).value = str(root.parent)
            await pilot.pause()
            assert panel.report is None and panel.query_one('#storage-clean', Button).disabled
            assert old.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('children_pruned', [False, True])
def test_user_can_leave_a_running_scan_without_cleanup(owned, monkeypatch, children_pruned):
    import threading
    from relay import storage
    root, home, old, build = owned
    entered, stopped = threading.Event(), threading.Event()
    def slow_scan(*args, cancelled, **kwargs):
        entered.set()
        if cancelled.wait(5):
            stopped.set()
        raise InterruptedError('cancelled')
    monkeypatch.setattr(storage, 'scan', slow_scan)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root))
    monkeypatch.setenv('TARE_PRODUCT_HOME', str(home))
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.dispatch_conversation_command('/storage', '')
            await pilot.pause()
            assert entered.is_set()
            if children_pruned:
                await app.screen.query_one('#storage-root', Input).remove()
            await pilot.press('escape'); await pilot.pause()
            assert not isinstance(app.screen, StoragePanel)
            assert await asyncio.to_thread(stopped.wait, 2)
            assert old.exists()
    asyncio.run(scenario())


def test_the_operator_opts_in_to_automatic_cleanup_from_storage(owned, monkeypatch):
    from relay import storage
    from test_review_checks_panel import until
    root, home, old, build = owned
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root))
    monkeypatch.setenv('TARE_PRODUCT_HOME', str(home))

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/storage', '')
            await pilot.pause()
            await app.workers.wait_for_complete()
            panel = app.screen
            assert not storage.auto_settings()['enabled']  # off until chosen
            panel.query_one('#storage-days', Input).value = '14'
            panel.query_one('#storage-auto', Checkbox).value = True
            await until(pilot, lambda: storage.auto_settings()['enabled'])
            assert storage.auto_settings()['days'] == 14
            assert panel.report is None  # what is due depends on the days kept: scan again
            assert 'Automatic cleanup on' in str(panel.query_one('#storage-status').render())
    asyncio.run(scenario())

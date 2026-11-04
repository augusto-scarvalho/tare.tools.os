"""Tests for ReviewChecksPanel, CheckRunConfirmDialog, and ChangesPanel/TurnChangesPanel integration."""
import asyncio
import json
from pathlib import Path
import threading

import pytest
from textual.app import App
from textual.widgets import Button, Select, Static

from relay.changes_panel import ChangesPanel
from relay.turn_changes_panel import TurnChangesPanel
from relay.review_checks_panel import CheckRunConfirmDialog, ReviewChecksPanel


def content(widget):
    return str(getattr(widget, 'content', widget.render()))


async def until(pilot, condition, max_ticks=80, pause=0.05):
    for _ in range(max_ticks):
        await pilot.pause(pause)
        if condition():
            return
    assert condition()


def sample_checks_inventory(project_name='test-project', invocation_id=None):
    return {
        'session_id': 'sess-123',
        'invocation_id': invocation_id,
        'checks': [
            {
                'id': 'check-lint',
                'label': 'Run Linter',
                'kind': 'lint',
                'repository': 'repo-core',
                'argv': ['flake8', 'pkg/'],
                'timeout_seconds': 30,
                'plan': 'plan-lint-v1',
                'disabled_reason': None,
            },
            {
                'id': 'check-test',
                'label': 'Run Unit Tests',
                'kind': 'test',
                'repository': 'repo-core',
                'argv': ['pytest', '-q', 'tests/test_core.py'],
                'timeout_seconds': 60,
                'plan': 'plan-test-v1',
                'disabled_reason': None,
            },
            {
                'id': 'check-disabled',
                'label': 'Run Integration Tests',
                'kind': 'test',
                'repository': 'repo-core',
                'argv': ['pytest', 'tests/integration/'],
                'timeout_seconds': 120,
                'plan': 'plan-integ-v1',
                'disabled_reason': 'Integration database unavailable',
            },
        ],
        'runs': [
            {
                'run_id': 'run-101',
                'check_id': 'check-lint',
                'label': 'Run Linter (historical)',
                'status': 'PASS',
                'freshness': 'OBSERVED_FILES_MATCH',
                'started_at': '2026-09-19T12:00:00Z',
                'elapsed_ms': 150,
                'returncode': 0,
                'reason': None,
            },
            {
                'run_id': 'run-102',
                'check_id': 'check-lint',
                'label': 'Run Linter (stale)',
                'status': 'PASS',
                'freshness': 'STALE',
                'started_at': '2026-09-19T11:00:00Z',
                'elapsed_ms': 145,
                'returncode': 0,
                'reason': None,
            },
            {
                'run_id': 'run-201',
                'check_id': 'check-test',
                'label': 'Run Unit Tests (failed)',
                'status': 'FAIL',
                'freshness': 'OBSERVED_FILES_MATCH',
                'started_at': '2026-09-19T12:05:00Z',
                'elapsed_ms': 350,
                'returncode': 1,
                'reason': '1 test failed',
            },
        ],
        'reason': None,
    }


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_geometry_layout_and_check_details(tmp_path, size):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {
            'run_id': run_id,
            'check_id': 'check-lint',
            'label': 'Linter',
            'status': 'PASS',
            'returncode': 0,
            'elapsed_ms': 150,
            'output': 'flake8 pkg/ passed.\nAll files clean.\n',
            'output_truncated': False,
        }

    async def scenario():
        app = App()
        async with app.run_test(size=size) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()

            await until(pilot, lambda: 'Repository: repo-core' in content(panel.query_one('#review-checks-details', Static)))

            # Verify geometry fits within bounds
            assert panel.query_one('#review-checks-close').region.bottom <= size[1]
            assert panel.query_one('#review-checks-actions').region.bottom <= size[1]
            assert panel.query_one('#review-checks-output-scroll').region.bottom <= size[1]

            # Verify exact JSON argv and details formatting
            details_text = content(panel.query_one('#review-checks-details', Static))
            assert 'Repository: repo-core' in details_text
            assert 'Timeout: 30s' in details_text
            assert json.loads(details_text.split('Command: ',1)[1]) == ['flake8','pkg/']

            # Verify raw custody paths are not exposed
            assert str(session) not in details_text
            status_text = content(panel.query_one('#review-checks-status', Static))
            assert str(session) not in status_text

            # Switch check to check-test
            check_sel = panel.query_one('#review-checks-check', Select)
            check_sel.value = 'check-test'
            await pilot.pause()
            await until(pilot, lambda: 'pytest' in content(panel.query_one('#review-checks-details', Static)))

            details_text2 = content(panel.query_one('#review-checks-details', Static))
            assert 'Timeout: 60s' in details_text2
            assert json.loads(details_text2.split('Command: ',1)[1]) == ['pytest','-q','tests/test_core.py']

            # Close panel
            panel.action_close()
            await pilot.pause()
            assert app.screen is not panel

    asyncio.run(scenario())


def test_disabled_check_displays_reason_and_disables_run(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: bool(panel.checks))

            run_btn = panel.query_one('#review-checks-run', Button)
            assert not run_btn.disabled

            # Select disabled check
            check_sel = panel.query_one('#review-checks-check', Select)
            check_sel.value = 'check-disabled'
            await pilot.pause()
            await until(pilot, lambda: 'Disabled: Integration database unavailable' in content(panel.query_one('#review-checks-details', Static)))

            # Run button must be disabled for disabled check
            assert run_btn.disabled

            # Attempting request_run must not open confirmation dialog
            panel.request_run()
            await pilot.pause()
            assert panel.active_confirmation is None
            assert app.screen is panel

    asyncio.run(scenario())


def test_empty_configuration_actionable_message(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'

    def empty_reader(sess, *, project=None, invocation_id=None):
        return {
            'session_id': 'sess-empty',
            'invocation_id': None,
            'checks': [],
            'runs': [],
            'reason': None,
        }

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=empty_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: 'No review checks configured' in content(panel.query_one('#review-checks-details', Static)))

            assert panel.query_one('#review-checks-run', Button).disabled
            assert panel.query_one('#review-checks-cancel', Button).disabled
            assert panel.query_one('#review-checks-copy', Button).disabled
            assert not panel.query_one('#review-checks-refresh', Button).disabled
            assert not panel.query_one('#review-checks-close', Button).disabled

            # Check actionable status message
            status_text = content(panel.query_one('#review-checks-status', Static))
            assert 'No runs recorded' in status_text

    asyncio.run(scenario())


def test_confirm_dialog_autofocus_default_enter_and_escape_cancel(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    runner_calls = []

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def fake_runner(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        runner_calls.append((check_id, expected_plan, run_id))
        return {
            'run_id': run_id,
            'check_id': check_id,
            'label': 'Run Linter',
            'status': 'PASS',
            'returncode': 0,
            'elapsed_ms': 120,
        }

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {'run_id': run_id, 'output': 'clean', 'output_truncated': False}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=fake_runner, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#review-checks-run', Button).disabled)

            # Click Run check -> opens CheckRunConfirmDialog
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            dialog = app.screen

            # Initial autofocus must be on Cancel button
            assert dialog.focused.id == 'check-confirm-cancel'
            cmd_text = content(dialog.query_one('#check-confirm-command', Static))
            assert json.loads(cmd_text) == ['flake8','pkg/']
            details_text = content(dialog.query_one('#check-confirm-details', Static))
            assert 'Repository: repo-core' in details_text
            assert 'Timeout: 30s' in details_text
            warn_text = content(dialog.query_one('#check-confirm-warning', Static))
            assert 'This check executes again' in warn_text

            # Press Enter while focused on Cancel -> cancels execution
            await pilot.press('enter')
            await pilot.pause()
            await until(pilot, lambda: app.screen is panel)
            assert len(runner_calls) == 0

            # Open again and press Escape -> cancels execution
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            await pilot.press('escape')
            await pilot.pause()
            await until(pilot, lambda: app.screen is panel)
            assert len(runner_calls) == 0

            # Open again and click Confirm run
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            app.screen.query_one('#check-confirm-confirm', Button).press()
            await pilot.pause()

            # Execution occurs
            await until(pilot, lambda: len(runner_calls) == 1)
            check_id, expected_plan, run_id = runner_calls[0]
            assert check_id == 'check-lint'
            assert expected_plan == 'plan-lint-v1'
            assert len(run_id) == 32  # uuid4 hex

    asyncio.run(scenario())


def test_stale_confirmation_rejected_after_refresh_or_selection(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    runner_calls = []

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def fake_runner(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        runner_calls.append(check_id)
        return {'run_id': run_id, 'check_id': check_id, 'status': 'PASS'}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=fake_runner)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#review-checks-run', Button).disabled)

            old_gen = panel.revision
            # Open confirmation dialog
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            assert panel.active_confirmation is not None

            # Refresh panel -> dismisses and invalidates active confirmation
            panel.action_reload()
            await pilot.pause()
            await until(pilot, lambda: panel.active_confirmation is None)

            # Manual call with old generation must be rejected
            panel.perform_run('check-lint', 'plan-lint-v1', old_gen)
            await pilot.pause()
            assert len(runner_calls) == 0

            # Change check selection -> invalidates confirmation
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            panel.query_one('#review-checks-check', Select).value = 'check-test'
            await pilot.pause()
            await until(pilot, lambda: panel.active_confirmation is None)

            panel.perform_run('check-lint', 'plan-lint-v1', old_gen)
            await pilot.pause()
            assert len(runner_calls) == 0

    asyncio.run(scenario())


def test_duplicate_run_prevention_and_control_locking(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    runner_calls = []
    entered_event = threading.Event()
    release_event = threading.Event()

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def blocking_runner(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        runner_calls.append(run_id)
        entered_event.set()
        release_event.wait(timeout=10.0)
        return {
            'run_id': run_id,
            'check_id': check_id,
            'status': 'PASS',
            'elapsed_ms': 500,
            'returncode': 0,
        }

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {'run_id': run_id, 'output': 'running...', 'output_truncated': False}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=blocking_runner, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#review-checks-run', Button).disabled)

            # Start run
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            app.screen.query_one('#check-confirm-confirm', Button).press()
            await pilot.pause()

            # Wait for runner to enter
            await until(pilot, lambda: entered_event.is_set())
            assert panel.running

            # Controls locked during run
            assert panel.query_one('#review-checks-run', Button).disabled
            assert panel.query_one('#review-checks-refresh', Button).disabled
            assert panel.query_one('#review-checks-close', Button).disabled
            assert panel.query_one('#review-checks-check', Select).disabled
            assert panel.query_one('#review-checks-history', Select).disabled
            assert not panel.query_one('#review-checks-cancel', Button).disabled

            # Attempt duplicate run / reload / close while running
            panel.request_run()
            panel.action_reload()
            panel.action_close()
            await pilot.pause()
            assert len(runner_calls) == 1
            assert app.screen is panel

            # Release blocking runner
            release_event.set()
            await pilot.pause()
            await until(pilot, lambda: not panel.running)

            assert len(runner_calls) == 1
            assert not panel.query_one('#review-checks-run', Button).disabled

    asyncio.run(scenario())


def test_live_output_polling_and_copy(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    output_stage = {'count': 0}
    entered_event = threading.Event()
    release_event = threading.Event()
    copies = []

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def runner_with_lock(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        entered_event.set()
        release_event.wait(timeout=10.0)
        return {'run_id': run_id, 'check_id': check_id, 'status': 'PASS', 'elapsed_ms': 200, 'returncode': 0}

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        # Must not wait on read_lock
        output_stage['count'] += 1
        lines = [f'Step {i} completed' for i in range(1, output_stage['count'] + 1)]
        return {
            'run_id': run_id,
            'output': '\n'.join(lines),
            'output_truncated': False,
        }

    async def scenario():
        app = App()
        app.copy_to_clipboard = copies.append
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=runner_with_lock, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#review-checks-run', Button).disabled)

            # Start run
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            app.screen.query_one('#check-confirm-confirm', Button).press()
            await pilot.pause()

            # Wait for execution and timer polling
            await until(pilot, lambda: entered_event.is_set())
            await until(pilot, lambda: 'Step 1 completed' in content(panel.query_one('#review-checks-output', Static)))

            # Test Copy output button
            copy_btn = panel.query_one('#review-checks-copy', Button)
            assert not copy_btn.disabled
            copy_btn.press()
            await pilot.pause()
            assert len(copies) == 1
            assert 'Step 1 completed' in copies[0]
            assert copy_btn.label == 'Copied'

            release_event.set()
            await pilot.pause()
            await until(pilot, lambda: not panel.running)

    asyncio.run(scenario())


def test_cancellation_flow_and_no_premature_completion(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    cancel_seen = threading.Event()
    release_worker = threading.Event()

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def cancellable_runner(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        while not cancelled():
            pass
        cancel_seen.set()
        release_worker.wait(timeout=10.0)
        return {
            'run_id': run_id,
            'check_id': check_id,
            'status': 'CANCELLED',
            'elapsed_ms': 100,
            'reason': 'Cancelled by user',
        }

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {'run_id': run_id, 'output': 'Process cancelling...', 'output_truncated': False}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=cancellable_runner, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#review-checks-run', Button).disabled)

            # Start run
            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            app.screen.query_one('#check-confirm-confirm', Button).press()
            await pilot.pause()

            await until(pilot, lambda: panel.running)
            cancel_btn = panel.query_one('#review-checks-cancel', Button)
            assert not cancel_btn.disabled

            # Request cancel
            cancel_btn.press()
            await pilot.pause()

            # Must NOT claim completion yet
            await until(pilot, lambda: cancel_seen.is_set())
            assert panel.running
            status_text = content(panel.query_one('#review-checks-status', Static))
            assert 'Cancellation requested' in status_text
            assert cancel_btn.disabled

            # Release worker
            release_worker.set()
            await pilot.pause()
            await until(pilot, lambda: not panel.running)

            # Now recorded as CANCELLED
            final_status = content(panel.query_one('#review-checks-status', Static))
            assert 'CANCELLED' in final_status
            assert 'Cancelled by user' in final_status

    asyncio.run(scenario())


def test_no_automatic_retry_on_unknown_or_failure(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    runner_calls = []

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def failing_runner(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        runner_calls.append(run_id)
        return {
            'run_id': run_id,
            'check_id': check_id,
            'status': 'UNKNOWN',
            'elapsed_ms': 50,
            'reason': 'Orphaned process failure',
        }

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {'run_id': run_id, 'output': 'fatal error', 'output_truncated': False}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=failing_runner, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#review-checks-run', Button).disabled)

            panel.query_one('#review-checks-run', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, CheckRunConfirmDialog))
            app.screen.query_one('#check-confirm-confirm', Button).press()
            await pilot.pause()

            await until(pilot, lambda: not panel.running)
            # Exactly one execution, never retry
            assert len(runner_calls) == 1
            status_text = content(panel.query_one('#review-checks-status', Static))
            assert 'UNKNOWN' in status_text
            assert 'Orphaned process failure' in status_text

    asyncio.run(scenario())


def test_history_selector_reopened_without_execution(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    runner_calls = []
    run_reader_calls = []

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def fake_runner(sess, *, project=None, invocation_id=None, check_id=None, expected_plan=None, run_id=None, cancelled=None):
        runner_calls.append(run_id)
        return {'run_id': run_id, 'status': 'PASS'}

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        run_reader_calls.append(run_id)
        return {
            'run_id': run_id,
            'output': f'Output for {run_id}\n',
            'output_truncated': False,
        }

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, runner=fake_runner, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()

            # First run in history (run-101) selected automatically
            await until(pilot, lambda: 'Output for run-101' in content(panel.query_one('#review-checks-output', Static)))
            assert len(runner_calls) == 0
            assert 'run-101' in run_reader_calls

            # Switch history to run-102
            hist_sel = panel.query_one('#review-checks-history', Select)
            hist_sel.value = 'run-102'
            await pilot.pause()
            await until(pilot, lambda: 'Output for run-102' in content(panel.query_one('#review-checks-output', Static)))
            assert len(runner_calls) == 0
            assert 'run-102' in run_reader_calls

    asyncio.run(scenario())


def test_orphaned_running_mapped_to_unknown(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'

    def reader_with_orphaned_running(sess, *, project=None, invocation_id=None):
        inv = sample_checks_inventory()
        inv['runs'].insert(0, {
            'run_id': 'run-orphaned',
            'check_id': 'check-lint',
            'label': 'Linter (crashed previously)',
            'status': 'RUNNING',  # No local worker running this!
            'started_at': '2026-09-19T10:00:00Z',
            'elapsed_ms': 9999,
            'returncode': None,
            'reason': 'Interrupted by daemon restart',
        })
        return inv

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {'run_id': run_id, 'output': 'partial output before crash', 'output_truncated': False}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=reader_with_orphaned_running, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()

            # Status must be mapped to UNKNOWN, not RUNNING
            await until(pilot, lambda: 'UNKNOWN' in content(panel.query_one('#review-checks-status', Static)))
            status_text = content(panel.query_one('#review-checks-status', Static))
            assert 'RUNNING' not in status_text
            assert 'UNKNOWN' in status_text and 'RUNNING' not in status_text

    asyncio.run(scenario())


def test_old_pass_freshness_display(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'

    def fake_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory()

    def fake_run_reader(sess, *, run_id, max_bytes=65536):
        return {'run_id': run_id, 'output': 'clean', 'output_truncated': False}

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ReviewChecksPanel(session, project, reader=fake_reader, run_reader=fake_run_reader)
            app.push_screen(panel)
            await pilot.pause()

            # run-101 has OBSERVED_FILES_MATCH
            await until(pilot, lambda: 'observed files match' in content(panel.query_one('#review-checks-status', Static)))

            # switch to run-102 which has STALE
            panel.query_one('#review-checks-history', Select).value = 'run-102'
            await pilot.pause()
            await until(pilot, lambda: 'STALE · later changes' in content(panel.query_one('#review-checks-status', Static)))

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_changes_panel_integration_and_actions_geometry(tmp_path, size):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    changes_reloads = []

    def fake_changes_reader(sess, *, project=None, invocation_id=None, **kwargs):
        changes_reloads.append(invocation_id)
        return {
            'turns': [{'id': 'turn-1', 'status': 'succeeded'}],
            'edits': [{'effect_id': 'e1', 'repository': 'repo', 'path': 'a.py', 'status': 'succeeded'}],
            'checks': [],
        }

    def fake_checks_reader(sess, *, project=None, invocation_id=None):
        return sample_checks_inventory(invocation_id=invocation_id)

    async def scenario():
        app = App()
        async with app.run_test(size=size) as pilot:
            panel = ChangesPanel(session, project, reader=fake_changes_reader, checks_reader=fake_checks_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: bool(panel.rows))

            # Verify 6 actions do not clip and fit within bounds
            assert panel.query_one('#changes-close').region.bottom <= size[1]
            checks_btn = panel.query_one('#changes-checks', Button)
            assert not checks_btn.disabled
            assert checks_btn.region.bottom <= size[1]

            # Click Checks button
            reloads_before = len(changes_reloads)
            checks_btn.press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, ReviewChecksPanel))

            checks_panel = app.screen
            assert checks_panel.session == session
            assert checks_panel.project == project
            assert checks_panel.read_lock is panel.read_lock

            # Returning from checks refreshes parent without automatic execution
            checks_panel.action_close()
            await pilot.pause()
            await until(pilot, lambda: app.screen is panel)
            assert len(changes_reloads) > reloads_before

    asyncio.run(scenario())


def test_turn_changes_panel_integration(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'
    turn_reloads = []

    def fake_turn_reader(sess, *, project=None, invocation_id=None, **kwargs):
        turn_reloads.append(invocation_id)
        return {
            'invocation_id': turn_id,
            'files': [],
            'undo': {'status': 'UNAVAILABLE'},
        }

    def fake_checks_reader(sess, *, project=None, invocation_id=None):
        assert invocation_id == turn_id
        return sample_checks_inventory(invocation_id=turn_id)

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_turn_reader, checks_reader=fake_checks_reader)
            app.push_screen(panel)
            await pilot.pause()
            await until(pilot, lambda: not panel.query_one('#turn-changes-checks', Button).disabled)

            # Click Checks
            reloads_before = len(turn_reloads)
            panel.query_one('#turn-changes-checks', Button).press()
            await pilot.pause()
            await until(pilot, lambda: isinstance(app.screen, ReviewChecksPanel))

            checks_panel = app.screen
            assert checks_panel.invocation_id == turn_id
            assert checks_panel.read_lock is panel.read_lock

            # Return to TurnChangesPanel
            checks_panel.action_close()
            await pilot.pause()
            await until(pilot, lambda: app.screen is panel)
            assert len(turn_reloads) > reloads_before

    asyncio.run(scenario())

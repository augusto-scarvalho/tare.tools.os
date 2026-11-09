"""Focused presentation tests for compact model row and Settings Connection details."""
import asyncio
import json
from pathlib import Path
import time

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea

from relay.console_connection import Observation
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def descriptor(directory, root, model='saved-model'):
    directory.mkdir(exist_ok=True)
    (directory/'native-agent.json').write_text(json.dumps({
        'schema': 'tare.tools/native-agent/2', 'config': {'repositories': {'repo': str(root)}},
        'deployment': {'native_profile': {'model': model}}}))


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('saved', [False, True])
def test_compact_model_row_omits_source_phrase_and_keeps_check_visible(tmp_path, monkeypatch, size, saved):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    session = tmp_path/'session'
    if saved:
        descriptor(session, tmp_path, 'actual-saved-model')

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            if saved:
                app.assistant_session = str(session)
                app.update_mode()
            await app.workers.wait_for_complete()
            await pilot.pause()

            context = app.query_one('#session-context', Static)
            button = app.query_one('#connection-check', Button)
            rendered = str(context.render())

            expected_model = 'actual-saved-model' if saved else app.header_details.model
            assert expected_model in rendered
            assert 'Saved model' not in rendered
            assert 'Next execution' not in rendered
            assert 'Connection unchecked' in rendered

            # Check button is visible and on-screen
            assert button.display is True
            assert 'Check' in str(button.label)
            assert app.screen.region.contains_region(button.region)

            # Tooltip retains full model, source distinction, and explanation
            expected_source = 'Saved model' if saved else 'Next execution'
            tooltip_text = str(context.tooltip)
            assert expected_model in tooltip_text
            assert expected_source in tooltip_text
            assert 'No connection check yet' in tooltip_text

            # Loading state shows checking in row and updates button
            app.connection_loading = True
            app.render_header()
            rendered_loading = str(context.render())
            assert 'Checking connection…' in rendered_loading
            assert 'Saved model' not in rendered_loading
            assert 'Next execution' not in rendered_loading
            assert 'Cancel check' in str(button.label)
            assert button.display is True

            # Observation summary with age and status
            obs = Observation(
                identity='ident-1',
                status='Model healthy',
                detail='Endpoint reports resident model healthy.',
                observed_at='2026-09-19T14:30:00Z',
                monotonic=time.monotonic(),
                duration_ms=45,
            )
            app.connection_loading = False
            app.connection_observation = obs
            app.render_header()
            rendered_obs = str(context.render())
            assert 'Model healthy' in rendered_obs
            assert '0s ago' in rendered_obs
            assert 'Saved model' not in rendered_obs
            assert 'Next execution' not in rendered_obs

            # Tooltip with observation retains explanation
            tooltip_obs = str(context.tooltip)
            assert expected_model in tooltip_obs
            assert expected_source in tooltip_obs
            assert 'Endpoint reports resident model healthy.' in tooltip_obs
            assert 'Observed 2026-09-19T14:30:00Z · 45 ms' in tooltip_obs

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('saved', [False, True])
def test_settings_connection_details_collapsed_and_expanded(tmp_path, monkeypatch, size, saved):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    session = tmp_path/'session'
    if saved:
        descriptor(session, tmp_path, 'saved-prod-model')

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            if saved:
                app.assistant_session = str(session)
                app.update_mode()
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Open Settings
            app.action_workspace()
            await pilot.pause()

            # Verify saved model guidance is preserved
            guidance = [str(note.render()) for note in app.query('.settings-note')]
            assert 'Use Choose model → Continue task to change a saved conversation.' in guidance

            # Verify Connection details section exists and is collapsed by default
            collapsible = app.query_one('#connection-section', Collapsible)
            assert collapsible.title == 'Connection details'
            assert collapsible.collapsed is True
            assert collapsible.display is True

            # Back button is available in Settings heading
            back_btn = app.query_one('#settings-close', Button)
            assert back_btn.display is True

            # Expand the section
            collapsible.collapsed = False
            await pilot.pause()

            details_widget = app.query_one('#connection-details', Static)
            content = str(details_widget.render())

            expected_model = 'saved-prod-model' if saved else app.header_details.model
            expected_source = 'Saved model' if saved else 'Next execution'

            # Must show exact model, source distinction, check result, and explanation
            assert expected_model in content
            assert expected_source in content
            assert 'Connection unchecked' in content
            assert 'No connection check yet' in content

            # Now add an observation and re-render
            obs = Observation(
                identity='ident-2',
                status='Model listed',
                detail='Endpoint returned listing matching requested model.',
                observed_at='2026-09-19T14:35:00Z',
                monotonic=time.monotonic(),
                duration_ms=120,
            )
            app.connection_observation = obs
            app.render_header()
            content_obs = str(details_widget.render())

            assert expected_model in content_obs
            assert expected_source in content_obs
            assert 'Model listed' in content_obs
            assert 'Endpoint returned listing matching requested model.' in content_obs
            assert 'Observed 2026-09-19T14:35:00Z · 120 ms' in content_obs

            # Settings still scrolls and Back stays available
            selection = app.query_one('#selection', VerticalScroll)
            assert selection is not None
            assert back_btn.display is True

    asyncio.run(scenario())


def test_connection_section_hidden_in_optional_flow_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            conn_section = app.query_one('#connection-section', Collapsible)
            conn_details = app.query_one('#connection-details', Static)
            conn_row = app.query_one('#connection-row')

            # In assistant mode, connection section and row are displayed
            assert conn_section.display is True
            assert conn_details.display is True

            # Switch to non-assistant flow (e.g. session / conversation / plan)
            app.query_one('#mode', Select).value = 'session'
            app.update_mode()
            await pilot.pause()

            assert conn_section.display is False
            assert conn_details.display is False
            assert conn_row.display is False

            # Switch to plan mode
            app.query_one('#mode', Select).value = 'plan'
            app.update_mode()
            await pilot.pause()

            assert conn_section.display is False
            assert conn_details.display is False

            # Switch back to assistant mode
            app.query_one('#mode', Select).value = 'assistant'
            app.update_mode()
            await pilot.pause()

            assert conn_section.display is True
            assert conn_details.display is True
            assert conn_row.display is True

    asyncio.run(scenario())


def test_draft_and_status_preserved_during_presentation_updates(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Set user draft
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Draft of an important implementation step')

            # Set status
            app.controls(True)
            app.set_status('Running test battery · 12s')
            status = app.query_one('#status', Static)
            assert 'Running test battery · 12s' in str(status.render())

            # Update header connection observation
            app.connection_loading = True
            app.render_header()
            assert prompt.text == 'Draft of an important implementation step'
            assert 'Running test battery · 12s' in str(status.render())

            # Expand connection section in settings
            app.action_workspace()
            await pilot.pause()
            collapsible = app.query_one('#connection-section', Collapsible)
            collapsible.collapsed = False
            await pilot.pause()

            # Close settings
            app.action_workspace()
            await pilot.pause()

            assert prompt.text == 'Draft of an important implementation step'
            assert 'Running test battery · 12s' in str(status.render())

    asyncio.run(scenario())


def test_a_new_conversation_says_when_the_projects_agents_md_is_not_loaded(tmp_path, monkeypatch):
    # 2026-11-09: AGENTS.md was quarantined by default and nothing said so; the planner wrote a criterion it forbids.
    # The header line cut the notice beside a low-disk warning, so the welcome of a new conversation says it.
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    (tmp_path/'AGENTS.md').write_text('Never run the whole suite on a laptop.\n')

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            welcome = lambda: str(app.query_one('#welcome', Static).render())
            assert 'AGENTS.md is not loaded' in welcome() and '/permissions' in welcome()
            assert 'AGENTS.md' not in str(app.query_one('#session-context', Static).render())
            app.project_admission = {'mode': 'adopted', 'root_sha256': 'a'*64, 'nested': False}
            app.render_header(); await pilot.pause()
            assert 'AGENTS.md' not in welcome()
            (tmp_path/'AGENTS.md').unlink()
            app.project_admission = {'mode': 'quarantine', 'root_sha256': None, 'nested': False}
            app.render_header(); await pilot.pause()
            assert 'AGENTS.md' not in welcome()  # nothing to load, nothing to say
    asyncio.run(scenario())

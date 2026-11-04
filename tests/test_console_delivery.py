"""Delivery comparison and independent verification through the TUI."""
import asyncio
import json

import pytest
from textual.widgets import Button, Input, Select, TextArea

from relay.console_backend import Selection, command
from relay.console_workbench import DeliveryComparisonPanel
from relay.native_console import WorkConsole
from test_delivery_comparison import study
from test_native_console import FakeBackend


@pytest.mark.parametrize('assistant', [True, False])
@pytest.mark.parametrize('terminal, protected', [(False, False), (True, False), (False, True)],
                         ids=['edit-only', 'terminal', 'command-protection'])
@pytest.mark.parametrize('explicit', [False, True])
def test_command_conversation_checks_are_explicit_and_structured_session_gates_remain(tmp_path, monkeypatch, assistant, terminal, protected, explicit):
    detected = []
    def config(root):
        detected.append(root)
        return {'verifiers': {'tests': {'argv': ['python', 'all_checks.py']}}}
    monkeypatch.setattr('relay.turn_engine.config_for', config)
    protection = {'review': 'reviewer', 'backend': 'sandbox',
                  'max_reviews': 1, 'review_seconds': 30} if protected else {}
    monkeypatch.setattr('relay.command_protection.validate', lambda value: value)
    argv = ['C:/Program Files/Python/python.exe', 'checks/focused check.py', '--label', 'a b']
    expected = argv if explicit else None
    selected = Selection(str(tmp_path), mode='session', role='implementer', terminal=terminal,
                         command_protection=protection, prompt='Implement the focused change.',
                         write_paths=['.'], verifier=json.dumps(argv) if explicit else '')
    args = command(selected, assistant=assistant)
    if expected is None:
        assert '--verify-command' not in args
        assert not detected
    else:
        assert args[args.index('--verify-command') + 1:] == expected
        assert not detected


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_compare_save_and_invalid_study_without_model_calls(study, tmp_path, monkeypatch, size):
    value, path, save = study
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/delivery', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, DeliveryComparisonPanel)
            assert panel.region.contains_region(panel.query_one('#delivery-save').region)
            await pilot.press('ctrl+enter', 'f2', 'f3', 'f4')
            assert app.screen is panel and not app.backend.launches
            panel.query_one('#delivery-study', Input).value = str(path)
            await pilot.click('#delivery-compare'); await pilot.pause()
            body = panel.query_one('#delivery-result', TextArea).text
            assert 'Lower-cost candidate: reliable' in body
            assert 'USD 0.70 per accepted delivery' in body
            assert not panel.query_one('#delivery-save', Button).disabled
            output = tmp_path/'preference.json'
            panel.query_one('#delivery-policy', Input).value = str(output)
            await pilot.click('#delivery-save'); await pilot.pause()
            assert json.loads(output.read_text())['delivery_study']['winner'] == 'reliable'
            assert 'Saved preference:' in panel.query_one('#delivery-result', TextArea).text
            before = output.read_bytes()
            await pilot.click('#delivery-save'); await pilot.pause()
            assert output.read_bytes() == before
            panel.query_one('#delivery-study', Input).value = str(tmp_path/'missing.json')
            await pilot.pause()
            assert panel.query_one('#delivery-save', Button).disabled
            await pilot.click('#delivery-compare'); await pilot.pause()
            assert panel.query_one('#delivery-save', Button).disabled
            await pilot.press('escape')
            assert app.screen is not panel
            assert not app.backend.launches
    asyncio.run(scenario())


def test_tui_auditor_has_checks_without_edit_or_terminal_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend())
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#session-role', Select).value = 'auditor'
            app.query_one('#verifier', Input).value = '["python", "-m", "pytest", "-q"]'
            app.query_one('#prompt', TextArea).load_text('Review the completed change and run check.')
            selected = app.selection()
            args = command(selected)
            assert args[args.index('--role')+1] == 'auditor'
            assert '--verify-command' in args
            assert '--write-path' not in args and '--terminal' not in args
    asyncio.run(scenario())

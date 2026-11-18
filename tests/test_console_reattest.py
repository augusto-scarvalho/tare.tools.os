"""The TUI's Work view offers the identity re-attestation (`tare run <work> --resume --reattest`) for a Work refused
at landing only because the roster no longer matches a saved identity. Offline fixtures: no model, no network."""
import asyncio
import json
from dataclasses import replace

import pytest
from textual.widgets import Button, Static

from relay import console_demand, relay_mesh
from relay.console_backend import Backend, Selection, command, saved_selection
from relay.native_console import ReattestPanel, WorkConsole
from relay.work_console import reattest_copy
from test_console_work_control import ControlBackend
from test_identity_reattestation import TRAIN, edit_roster, files, local_before_262, mesh, strict_train  # noqa: F401
from test_native_console import FakeBackend, Process

STALE, MISMATCH = 'IDENTITY_ROSTER_STALE', 'IDENTITY_ROSTER_MISMATCH'


class OfferBackend(FakeBackend):
    def __init__(self, offer):
        super().__init__()
        self.offer = offer  # (code, why) as relay_mesh.reattestable gives it, or None
        self.process = Finished()

    def inspect(self, selection):
        return 'Work recipe'

    def reattest_offer(self, selection):
        return self.offer


class Finished(Process):
    async def communicate(self):
        self.returncode = 1
        return json.dumps({'status': 'BLOCKED', 'reason': 'FIXTURE'}).encode(), b''


def shown(app):
    return app.query_one('#reattest-work', Button).display and app.query_one('#reattest-note').display


async def opened(app, pilot, work='work-one'):
    await app.workers.wait_for_complete()
    app.query_one('#work-id').value = work
    await app.refresh_reattest(app.selection())
    await pilot.pause()


@pytest.mark.parametrize('code,words', [(STALE, 'model list changed'), (MISMATCH, 'read differently')])
def test_an_eligible_work_shows_why_it_cannot_land_and_one_button(tmp_path, code, words):
    async def scenario():
        app = WorkConsole(tmp_path, backend=OfferBackend((code, '')), initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await opened(app, pilot)
            assert shown(app)
            text = str(app.query_one('#reattest-note', Static).render())
            assert 'cannot land' in text and words in text and 'old verdict cannot be reused' in text
            assert text == reattest_copy(code)
            assert not app.backend.launches
            app.query_one('#work-id').value = 'another-work'  # the offer belongs to the Work it was computed for
            await pilot.pause()
            assert not shown(app)
    asyncio.run(scenario())


def test_a_work_that_is_not_eligible_shows_nothing_new(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=OfferBackend(None), initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await opened(app, pilot)
            assert not shown(app) and app.reattest_offer is None
            assert not app.query_one('#reattest-note').display and not app.query_one('#reattest-work').display
    asyncio.run(scenario())


def test_a_saved_auditor_that_a_new_audit_would_refuse_shows_the_note_without_the_button(tmp_path):
    async def scenario():
        backend = OfferBackend((MISMATCH, 'attesting again would not help'))
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await opened(app, pilot)
            text = str(app.query_one('#reattest-note', Static).render())
            assert app.query_one('#reattest-note').display and not app.query_one('#reattest-work').display
            assert text == reattest_copy(MISMATCH, True) and 'read differently' in text
            assert ('Attesting again would not help: its saved auditor is now read as the same model family as the '
                    'model that wrote the change, so a new audit would be refused. '
                    'Run the Work again to choose another auditor.') in text
            assert 'old verdict' not in text
            app.ask_reattest()  # even called directly, nothing is asked or started
            await pilot.pause()
            assert not isinstance(app.screen, ReattestPanel) and not backend.launches
    asyncio.run(scenario())


def test_the_button_asks_and_cancel_or_escape_change_nothing(tmp_path):
    async def scenario():
        backend = OfferBackend((STALE, ''))
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await opened(app, pilot)
            app.query_one('#reattest-work', Button).focus()
            for leave in ('escape', 'enter'):  # Escape, then the focused Cancel
                await pilot.press('enter')  # keyboard only: the focused button asks
                await pilot.pause()
                assert isinstance(app.screen, ReattestPanel)
                assert app.focused.id == 'reattest-cancel'
                assert 'kept aside' in str(app.screen.query(Static).first().render())
                assert 'new model calls' in str(app.screen.query(Static).first().render())
                await pilot.press(leave)
                await pilot.pause()
                assert not isinstance(app.screen, ReattestPanel)
                assert not backend.launches and not app.busy
                assert shown(app)
    asyncio.run(scenario())


def test_confirm_runs_the_ordinary_resume_with_reattest_and_drops_the_retry_options(tmp_path):
    async def scenario():
        backend = OfferBackend((MISMATCH, ''))
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await opened(app, pilot)
            app.query_one('#retry-audit').value = True  # the CLI refuses it together with --reattest
            app.query_one('#reattest-work', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            await pilot.press('tab', 'enter')  # Cancel -> the confirming button
            await app.workers.wait_for_complete()
            await pilot.pause()
            [args] = backend.launches
            assert args[0] == 'run' and 'work-one' in args and '--resume' in args and '--reattest' in args
            assert '--retry-audit' not in args and '--retry-plan' not in args
    asyncio.run(scenario())


def test_a_pause_during_a_confirmed_reattest_saves_the_demand_without_the_confirmation(tmp_path, monkeypatch):
    project, control = tmp_path/'project', tmp_path/'control'
    project.mkdir(); control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    async def scenario():
        app = WorkConsole(project, backend=ControlBackend('RUNNING'), initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await app.workers.wait_for_complete()
            confirmed = Selection(str(project), mode='work', work='work-one', reattest=True)
            app.selection = lambda: replace(confirmed, reattest=False)
            app.active = confirmed
            app.controls(True)
            await pilot.pause()
            app.request_work_control('pause')
            await app.workers.wait_for_complete()
            assert app.backend.writes, str(app.query_one('#status').render())
            assert app.backend.writes[0][0].reattest  # the running selection is untouched
            saved = console_demand.read(app.demand_id, project, control)['events']
            assert saved and all(event['selection']['reattest'] is False for event in saved)
            assert '--reattest' not in command(saved_selection(saved[-1]['selection']), True)
            app.controls(False)
    asyncio.run(scenario())


def test_only_the_confirmed_selection_carries_reattest(tmp_path):
    ordinary = command(Selection(str(tmp_path), mode='work', work='Work-1'), True)
    assert ordinary[ordinary.index('--resume'):] == ['--resume']
    chosen = command(Selection(str(tmp_path), mode='work', work='Work-1', reattest=True), True)
    assert chosen[-2:] == ['--resume', '--reattest']
    assert '--reattest' not in command(Selection(str(tmp_path), mode='work', work='Work-1', reattest=True))


def test_the_backend_asks_the_train_of_the_selected_work(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr('relay.work_run.train_id_for', lambda work, graph_path=None: seen.append((work, graph_path)) or 'T')
    monkeypatch.setattr(relay_mesh, 'reattestable', lambda train: (STALE, '') if train == 'T' else None)
    assert Backend().reattest_offer(Selection(str(tmp_path), mode='work', work='w', graph_path='g.json')) == (STALE, '')
    assert seen == [('w', 'g.json')]


def test_the_eligibility_check_names_the_two_codes_and_changes_nothing(mesh):  # noqa: F811
    train = strict_train(mesh)
    assert relay_mesh.reattestable(TRAIN) is None  # admitted
    edit_roster(mesh, lambda value: value.setdefault('notes', 'edited in a release'))
    before = files(mesh.parent)
    assert relay_mesh.reattestable(TRAIN) == (STALE, '')
    assert relay_mesh.reattestable(TRAIN) == (STALE, '')
    assert files(mesh.parent) == before
    state = json.loads((train/'STATE.json').read_bytes())
    assert state['status'] == 'READY_FOR_LANDING' and state['revision'] == 7


def test_a_relay_mesh_train_without_a_saved_work_run_is_offered_as_before(mesh, capsys):  # noqa: F811
    train = strict_train(mesh, implementer=local_before_262(mesh))
    assert not (train/'WORK_RUN.json').exists()
    assert relay_mesh.reattestable(TRAIN) == (MISMATCH, '')
    assert relay_mesh.reattest_train(TRAIN) is True
    capsys.readouterr()
    assert relay_mesh.reattestable(TRAIN) is None  # now AUDIT_PENDING
    assert relay_mesh.reattestable('TRAIN-UNKNOWN') is None
    assert json.loads((train/'STATE.json').read_bytes())['status'] == 'AUDIT_PENDING'


@pytest.mark.parametrize('case', ['other_code', 'status', 'not_strict'])
def test_other_states_are_not_offered(mesh, case):  # noqa: F811
    train = strict_train(mesh, status='AUDIT_PENDING' if case == 'status' else 'READY_FOR_LANDING',
                         strict=case != 'not_strict')
    if case == 'other_code':
        (train/'AUDIT_REPORT.md').write_text('another report\n', encoding='utf-8')
    before = files(train)
    assert relay_mesh.reattestable(TRAIN) is None and files(train) == before

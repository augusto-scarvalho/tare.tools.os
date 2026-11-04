"""Owner custody paths remain copyable through completion and recovery."""
import asyncio
import json
import pytest

from textual.selection import Selection as TextSelection
from textual.widgets import Select, TextArea

from relay import console_demand, relay_mesh
from relay.native_console import WorkConsole
from test_native_agent import setup
from test_native_console import FakeBackend
from test_console_recovery import Backend, finish, request


def copied_output(app):
    return '\n'.join(widget.get_selection(TextSelection(None, None))[0]
                     for widget in app.query('#output .message'))


def test_completed_direct_session_keeps_exact_custody_path_and_resume_command(tmp_path, monkeypatch):
    path = r'C:\projects\_archives\session_[1]'
    project = tmp_path/'project'
    project.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    result = {'status': 'PROPOSED', 'output': 'Recorded delivery.', 'session': path,
              'capability_observations': {}, 'model_calls': 1}

    async def scenario():
        class RecordedBackend(FakeBackend):
            def catalog(self, root):
                return {'models': [{'deployment': 'recorded-remote', 'model': 'Recorded'}],
                        'sessions': [], 'works': [], 'errors': []}

        backend = RecordedBackend()

        async def communicate():
            backend.process.returncode = 0
            return json.dumps(result).encode(), b''

        backend.process.communicate = communicate
        app = WorkConsole(project, backend=backend, initial_mode='session')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#mode', Select).value = 'session'
            app.query_one('#model', Select).value = 'recorded-remote'
            await pilot.pause()
            assert app.selection().mode == 'session'
            app.query_one('#prompt', TextArea).load_text('Read the project.')
            app.execute(False)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.last_result == result, str(app.query_one('#status').render())
            output = copied_output(app)
            assert 'Session: ' + path in output
            assert '--session' in output and path in output.split('--session', 1)[1]
            assert len(backend.launches) == 1

    asyncio.run(scenario())


def test_recovered_owner_receipt_keeps_path_without_dispatch(setup, monkeypatch):
    config, original, root = setup
    # Both platforms have an actual backslash before underscore: a separator on
    # Windows and a valid filename character on POSIX. No Windows-only skip.
    custody = original.parent/r'saved\_archives'/'session_[1]'
    _, result = finish(config, custody)
    control = root.parent/'control'
    control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root.parent/'presentation'))
    value = request(root, custody)
    saved = console_demand.save(None, 0, root, control, 'execution_requested',
                               value['snapshot'], value['events'][0]['detail'])

    async def scenario():
        backend = Backend()
        app = WorkConsole(root, backend=backend)
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved['id']
            app.open_demand()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.last_result == result
            assert app.demand_state['events'][-1]['kind'] == 'session_reconciled'
            assert str(custody) in copied_output(app)
            assert not backend.calls and app.process is None

    asyncio.run(scenario())


@pytest.mark.parametrize('entry', ['review', 'inspect'])
def test_review_keeps_exact_owner_report_without_dispatch(tmp_path, monkeypatch, entry):
    path = r'C:\projects\_archives\session_[1]'
    report = ('Session: ' + path + '\nIdentifier: work_*literal*_[red]\n'
              "Continue (PowerShell): tare-ops agent --native --resume --session '" + path + "'")
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))

    async def scenario():
        requests = []

        class ReviewBackend(FakeBackend):
            def review(self, selection):
                requests.append(('review', selection.session))
                return report

            def inspect(self, selection):
                requests.append(('inspect', selection.session))
                return report

        backend = ReviewBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='session')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            # This test invokes the requested report explicitly. Fixture setup
            # must not queue an automatic inspection that can later cancel it
            # in the exclusive inspect group, depending on event-loop timing.
            with app.prevent(Select.Changed):
                app.query_one('#mode', Select).value = 'session'
                app.query_one('#session', Select).set_options([('Recorded session', path)])
                app.query_one('#session', Select).value = path
            await pilot.pause()
            await getattr(app, entry + '_selection')().wait()
            await pilot.pause()
            assert requests == [(entry, path)]
            assert report in copied_output(app)
            assert not backend.launches and app.process is None

    asyncio.run(scenario())

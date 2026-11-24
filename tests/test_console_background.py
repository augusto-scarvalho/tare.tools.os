"""Supervised TUI background execution: detach via Ctrl+B / /bg / stall dialog, answer, complete, quit cleanly."""
import asyncio
import json
from pathlib import Path

from textual.widgets import Button, TextArea

from relay import conversation_permissions as permissions
from relay.console_background import BackgroundPanel
from relay.execution_permission_panel import ExecutionPermissionPanel
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


class ControlledProcess:
    def __init__(self, payload=None):
        self.returncode = None
        self.done = asyncio.Event()
        self.payload = payload or {'status': 'PROPOSED', 'output': 'Done in background.', 'model_calls': 1}
        self.cancelled = False

    async def communicate(self):
        await self.done.wait()
        if self.cancelled:
            self.returncode = 2
            return json.dumps({'status': 'CANCELLED', 'reason': 'OPERATOR_CANCELLED'}).encode(), b''
        self.returncode = 0
        return json.dumps(self.payload).encode(), b''


class ControlledBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.processes = []

    async def launch(self, args, root):
        self.launches.append(args)
        proc = ControlledProcess()
        self.processes.append(proc)
        return proc

    def cancel(self, process):
        self.cancelled += 1
        process.cancelled = True
        process.done.set()


def _write_stall_request(session_dir, project_root):
    folder = Path(session_dir) / 'execution-permission'
    folder.mkdir(parents=True, exist_ok=True)
    req = {
        'schema': permissions.SCHEMA, 'kind': 'stall', 'session': 's-bg', 'effect': 'e-bg',
        'task': 't1', 'plan': 'p1', 'project': str(Path(project_root).resolve()),
        'paths': [], 'checks': {'tests': ['godot', '-s', 'tests/smoke.gd']}, 'tests': [],
        'status': 'WAITING',
        'objective': 'The check tests has run for 10 min and has produced no new output for the last 10 min.',
        'stall': {'check': 'tests', 'outside': False, 'reason': 'quiet_output', 'output_tail': 'round 0 ok'},
    }
    req['id'] = permissions.identity(req)
    (folder / 'request.json').write_text(json.dumps(req), encoding='utf-8')
    return req


def test_ctrl_b_moves_turn_to_background_unlocks_tui_notifies_on_permission_and_completion(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path / 'sessions')
    backend = ControlledBackend()

    async def scenario():
        app = WorkConsole(project, backend=backend, initial_mode='assistant',
                          storage_preferred=tmp_path / 'storage', storage_fallback=tmp_path / 'storage')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(.2)
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Run slow tests')
            app.action_start()
            for _ in range(30):
                await pilot.pause(.05)
                if app.busy and app.process is not None:
                    break
            assert app.busy and len(backend.processes) == 1
            session_path = app.assistant_session
            assert session_path

            # Detach via Ctrl+B: TUI unlocks immediately while process stays alive.
            app.action_background()
            for _ in range(20):
                await pilot.pause(.05)
                if not app.busy:
                    break
            assert not app.busy and app.process is None
            assert session_path in app.background_runs
            run = app.background_runs[session_path]
            assert run.is_active and run.process.returncode is None

            # Sending to the SAME running background conversation is cleanly refused (no writer.lock race).
            prompt.load_text('second message')
            app.action_start()
            await pilot.pause(.1)
            assert len(backend.launches) == 1 and 'running in the background' in app.status_text

            # Starting a new conversation works immediately while the first stays in background!
            assert app.new_conversation()
            await pilot.pause(.1)
            assert app.assistant_session is None and run.is_active

            # Pending stall/permission request on the backgrounded session transitions status -> needs_answer.
            req = _write_stall_request(session_path, project)
            for _ in range(30):
                await pilot.pause(.1)
                if run.status == 'needs_answer':
                    break
            assert run.status == 'needs_answer' and run.pending_request_id == req['id']

            # Open /bg panel, click Answer -> opens ExecutionPermissionPanel -> click Keep waiting.
            app.action_background()
            await pilot.pause(.1)
            assert isinstance(app.screen, BackgroundPanel)
            assert not app.screen.query_one('#bg-answer', Button).disabled
            await pilot.click('#bg-answer')
            await pilot.pause(.1)
            assert isinstance(app.screen, ExecutionPermissionPanel)
            await pilot.click('#permission-once')
            await pilot.pause(.1)
            answer_data = json.loads((Path(session_path) / 'execution-permission' / 'answer.json').read_text())
            assert answer_data == {'id': req['id'], 'choice': 'once'}
            (Path(session_path) / 'execution-permission' / 'request.json').write_text(
                json.dumps({**req, 'status': 'CLOSED'}), encoding='utf-8')

            # Complete the background process -> status becomes 'done'.
            backend.processes[0].done.set()
            for _ in range(30):
                await pilot.pause(.05)
                if run.status == 'done':
                    break
            assert run.status == 'done' and run.result['output'] == 'Done in background.'

    asyncio.run(scenario())


def test_stall_dialog_continue_in_background_answers_and_detaches_and_quit_cancels_active_bg(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path / 'sessions')
    backend = ControlledBackend()

    async def scenario():
        app = WorkConsole(project, backend=backend, initial_mode='assistant',
                          storage_preferred=tmp_path / 'storage', storage_fallback=tmp_path / 'storage')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(.2)
            app.query_one('#prompt', TextArea).load_text('Long check')
            app.action_start()
            for _ in range(30):
                await pilot.pause(.05)
                if app.busy and app.process is not None:
                    break
            session_path = app.assistant_session
            req = _write_stall_request(session_path, project)
            for _ in range(30):
                await pilot.pause(.1)
                if isinstance(app.screen, ExecutionPermissionPanel):
                    break
            assert isinstance(app.screen, ExecutionPermissionPanel)
            await pilot.click('#permission-background')
            for _ in range(20):
                await pilot.pause(.05)
                if not app.busy and session_path in app.background_runs:
                    break
            assert not app.busy and session_path in app.background_runs
            answer_data = json.loads((Path(session_path) / 'execution-permission' / 'answer.json').read_text())
            assert answer_data == {'id': req['id'], 'choice': 'background'}

            # Quitting while a background run is active cancels it cleanly (no orphan child).
            app.action_quit()
            await pilot.pause(.2)
            assert backend.cancelled >= 1 and backend.processes[0].cancelled is True

    asyncio.run(scenario())


def test_background_guards_resume_command_approval_direct_session_and_draft_preservation(tmp_path, monkeypatch):
    import hashlib
    import time as time_mod
    from relay import command_permission

    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path / 'sessions')

    class ApprovalBackend(ControlledBackend):
        def __init__(self):
            super().__init__()
            self.action_approval_root = ''

        async def launch(self, args, root):
            self.action_approval_root = str(tmp_path / f'approvals-{len(self.launches)}')
            Path(self.action_approval_root).mkdir(parents=True, exist_ok=True)
            return await super().launch(args, root)

    backend = ApprovalBackend()

    async def scenario():
        app = WorkConsole(project, backend=backend, initial_mode='assistant',
                          storage_preferred=tmp_path / 'storage', storage_fallback=tmp_path / 'storage')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(.2)
            app.query_one('#prompt', TextArea).load_text('First task')
            app.action_start()
            for _ in range(30):
                await pilot.pause(.05)
                if app.busy and app.process is not None:
                    break
            session_path = app.assistant_session
            first_approval_root = backend.action_approval_root
            app.action_background()
            for _ in range(20):
                await pilot.pause(.05)
                if not app.busy:
                    break
            run = app.background_runs[session_path]
            assert run.command_approval_root == first_approval_root

            # /resume (execute(True, assistant=True)) must also refuse launching against an active bg session.
            app.execute(True, assistant=True, resume_only=True)
            await pilot.pause(.05)
            assert len(backend.launches) == 1 and 'running in the background' in app.status_text

            # Simulate another launch overwriting backend.action_approval_root: detached run keeps its own root.
            backend.action_approval_root = str(tmp_path / 'other-approvals')
            req_id = hashlib.sha256(b'cmd-req-1').hexdigest()
            req_dir = Path(first_approval_root) / req_id
            req_dir.mkdir(parents=True, exist_ok=True)
            cmd_req = {
                'schema': command_permission.SCHEMA, 'id': req_id, 'kind': 'command',
                'project': str(project.resolve()), 'cwd': str(project.resolve()),
                'argv': ['echo', 'hi'], 'objective': 'Approve echo hi', 'status': 'WAITING',
                'expires': time_mod.time() + 60,
            }
            (req_dir / 'request.json').write_text(json.dumps(cmd_req), encoding='utf-8')
            for _ in range(30):
                await pilot.pause(.1)
                if run.status == 'needs_answer' and run.pending_kind == 'command':
                    break
            assert run.status == 'needs_answer' and run.pending_kind == 'command'
            app.answer_background_permission(run)
            await pilot.pause(.1)
            assert isinstance(app.screen, ExecutionPermissionPanel)
            await pilot.click('#permission-once')
            await pilot.pause(.1)
            cmd_ans = json.loads((req_dir / 'answer.json').read_text(encoding='utf-8'))
            assert cmd_ans == {'id': req_id, 'choice': 'once'}

            # Switching back via open_background_run preserves any unsent draft via draft_saved checkpoint.
            assert app.new_conversation()
            await pilot.pause(.1)
            app.query_one('#prompt', TextArea).load_text('Unsent important draft')
            saved_kinds = []
            orig_checkpoint = app.checkpoint

            def spy_checkpoint(kind, *a, **kw):
                saved_kinds.append(kind)
                return orig_checkpoint(kind, *a, **kw)

            app.checkpoint = spy_checkpoint
            app.open_background_run(run)
            await pilot.pause(.1)
            assert 'draft_saved' in saved_kinds

            # Cleanly finish background run before exiting.
            backend.processes[0].done.set()
            for _ in range(20):
                await pilot.pause(.05)
                if not run.is_active:
                    break

    asyncio.run(scenario())


def test_direct_non_conversation_session_cannot_detach_to_background(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path / 'sessions')
    backend = ControlledBackend()

    async def scenario():
        app = WorkConsole(project, backend=backend, initial_mode='session',
                          storage_preferred=tmp_path / 'storage', storage_fallback=tmp_path / 'storage')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(.2)
            app.query_one('#prompt', TextArea).load_text('Direct edit task')
            app.action_start()
            for _ in range(30):
                await pilot.pause(.05)
                if app.busy and app.process is not None:
                    break
            assert app.busy and app.process is not None
            assert app.detach_active_to_background() is False
            assert app.busy and not app.background_runs
            assert 'post-turn audit' in app.status_text
            backend.processes[0].done.set()
            for _ in range(20):
                await pilot.pause(.05)
                if not app.busy:
                    break

    asyncio.run(scenario())

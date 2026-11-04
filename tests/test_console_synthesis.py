import asyncio
import json
import pytest
from relay import research_synthesis as flow, native_installation
from relay.console_backend import Selection, Backend, command
from test_research_synthesis import synthesis, make
from test_research_flow import study
from test_native_agent import setup


def test_public_arguments_reopen_and_project_boundary(synthesis, capsys):
    target, root, inputs, view, calls = synthesis
    selected = Selection(str(root), mode='synthesis', prompt=view['question'],
                         synthesis_inputs=json.dumps(inputs), synthesis_sha256=view['sha256'])
    args = command(selected)
    assert args[0] == 'research-synthesis' and '--unlimited' not in args
    assert '--reviewed-sha256' in args
    make(synthesis)
    selected.synthesis = str(target)
    assert command(selected, True) == ['research-synthesis', '--synthesis', str(target), '--resume', '--format', 'json']
    assert native_installation.main(['research-synthesis', '--synthesis', str(target), '--show', '--format', 'json']) == 0
    assert json.loads(capsys.readouterr().out)['authority'] == 'NONE'
    assert flow.main(['--synthesis', str(target), '--resume', '--deployment', 'other']) == 2
    selected.root = str(root.parent)
    with pytest.raises(ValueError, match='another project'): command(selected, True)
    assert not calls


def test_tui_review_and_reopen_real_snapshots(synthesis):
    from relay.native_console import WorkConsole
    from textual.widgets import Select, Input, TextArea
    target, root, inputs, view, calls = synthesis
    class TestBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}], 'sessions': [], 'works': [], 'errors': []}
    make(synthesis)
    flow.execute(target)
    async def check():
        app = WorkConsole(root, backend=TestBackend()); shown = []
        async with app.run_test(size=(120, 70)) as pilot:
            app.say = shown.append
            app.query_one('#mode', Select).value = 'synthesis'
            app.query_one('#prompt', TextArea).load_text(view['question'])
            app.query_one('#synthesis-inputs', TextArea).load_text(json.dumps(inputs))
            await pilot.pause()
            await app.preview_synthesis().wait()
            assert app.query_one('#synthesis-sha256', Input).value == view['sha256']
            assert any('TTL is five minutes.' in s and 'Profile:' in s for s in shown)
            app.query_one('#synthesis', Input).value = str(target)
            assert 'disputed' in app.backend.inspect(app.selection())
            assert app.query_one('#synthesis-options').display
    asyncio.run(check())
    assert len(calls) == 1


def test_tui_invocation_argument_records_a_bound_owner_result(synthesis, capsys):
    from dataclasses import asdict
    from relay import console_recovery, flow_invocation, relay_mesh
    target, root, inputs, view, calls = synthesis
    make(synthesis)
    identity = '4' * 32
    assert native_installation.main(['research-synthesis', '--invocation-id', identity,
        '--synthesis', str(target), '--resume', '--format', 'json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['invocation_id'] == identity
    recovered, evidence = flow_invocation.observe(target, identity, 'synthesis', root, relay_mesh.RELAY_ROOT)
    assert recovered == result and evidence
    selected = Selection(str(root), mode='synthesis', synthesis=str(target))
    pending = {'root': str(root), 'control_root': str(relay_mesh.RELAY_ROOT),
        'events': [{'kind': 'execution_requested', 'selection': asdict(selected),
                    'detail': {'invocation_id': identity}}]}
    observed = console_recovery.observe(pending)
    assert observed['state'] == 'RECOVERED' and observed['result'] == result
    assert 'Advisory synthesis' in Backend().review(selected)
    assert len(calls) == 1


def test_paused_generation_is_not_a_successful_cli_exit(synthesis, monkeypatch, capsys):
    from relay import native_agent
    target, root, inputs, view, calls = synthesis
    make(synthesis)
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw:
                        {'status': 'EXHAUSTED', 'reason': 'MODEL_CALL_BUDGET'})
    assert flow.main(['--synthesis', str(target), '--resume', '--format', 'json']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'PAUSED'


def test_saved_demand_restores_the_synthesis_and_reviewed_inputs(synthesis):
    from dataclasses import asdict
    from relay.native_console import WorkConsole
    from textual.widgets import Select, Input, TextArea
    target, root, inputs, view, calls = synthesis
    class TestBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}], 'sessions': [], 'works': [], 'errors': []}
    selected = Selection(str(root), mode='synthesis', prompt=view['question'], synthesis=str(target),
                         synthesis_inputs=json.dumps(inputs), synthesis_sha256=view['sha256'])
    async def check():
        app = WorkConsole(root, backend=TestBackend())
        async with app.run_test(size=(120, 70)) as pilot:
            app.restore_selection(asdict(selected))
            await pilot.pause()
            assert app.query_one('#mode', Select).value == 'synthesis'
            assert app.query_one('#synthesis', Input).value == str(target)
            assert app.query_one('#synthesis-inputs', TextArea).text == selected.synthesis_inputs
            assert app.query_one('#synthesis-sha256', Input).value == view['sha256']
    asyncio.run(check())
    assert not calls


def test_first_input_can_be_added_after_an_ordinary_demand_is_restored(synthesis):
    from dataclasses import asdict
    from relay.native_console import WorkConsole
    from textual.widgets import Select, TextArea
    target, root, inputs, view, calls = synthesis
    class TestBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}], 'sessions': [], 'works': [], 'errors': []}
    async def check():
        app = WorkConsole(root, backend=TestBackend())
        async with app.run_test(size=(120, 70)) as pilot:
            app.restore_selection(asdict(Selection(str(root))))
            app.query_one('#mode', Select).value = 'synthesis'
            choice = json.dumps(inputs[0])
            widget = app.query_one('#synthesis-choice', Select)
            widget.set_options([('First saved research', choice)])
            widget.value = choice
            await pilot.pause()
            app.add_synthesis_input()
            assert json.loads(app.query_one('#synthesis-inputs', TextArea).text) == [inputs[0]]
    asyncio.run(check())
    assert not calls


@pytest.mark.parametrize('filled', [False, True])
def test_quit_saves_an_unstarted_draft_but_not_an_empty_conversation(synthesis, filled):
    from dataclasses import asdict
    from relay import console_demand, relay_mesh
    from relay.native_console import WorkConsole
    target, root, inputs, view, calls = synthesis
    class TestBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}], 'sessions': [], 'works': [], 'errors': []}
    selected = Selection(str(root), mode='synthesis', prompt=view['question'] if filled else '',
                         synthesis_inputs=json.dumps(inputs) if filled else '[]',
                         synthesis_sha256=view['sha256'] if filled else '')
    async def check():
        app = WorkConsole(root, backend=TestBackend())
        async with app.run_test(size=(120, 70)) as pilot:
            app.restore_selection(asdict(selected))
            await pilot.pause()
            assert app.demand_id is None
            app.action_quit()
            if filled:
                assert app.demand_id is not None
                saved = console_demand.read(app.demand_id, root, relay_mesh.RELAY_ROOT)
                assert saved['snapshot']['selection']['prompt'] == selected.prompt
                assert saved['snapshot']['selection']['synthesis_inputs'] == selected.synthesis_inputs
                assert saved['snapshot']['selection']['synthesis_sha256'] == view['sha256']
                assert all(e['kind'] != 'execution_requested' for e in saved['events'])
            else:
                assert app.demand_id is None
    asyncio.run(check())
    assert not calls


def test_failed_draft_save_keeps_the_input_open(synthesis, monkeypatch):
    from dataclasses import asdict
    from relay import console_demand
    from relay.native_console import WorkConsole
    target, root, inputs, view, calls = synthesis
    class TestBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}], 'sessions': [], 'works': [], 'errors': []}
    selected = Selection(str(root), mode='synthesis', prompt=view['question'], synthesis_inputs=json.dumps(inputs))
    async def check():
        app = WorkConsole(root, backend=TestBackend())
        async with app.run_test(size=(120, 70)) as pilot:
            app.restore_selection(asdict(selected))
            await pilot.pause()
            def failed(*args, **kwargs):
                raise OSError('fixture disk unavailable')
            monkeypatch.setattr(console_demand, 'save', failed)
            exited = []
            with monkeypatch.context() as patch:
                patch.setattr(app, 'exit', lambda *a, **kw: exited.append(True))
                app.action_quit()
            assert not exited and app.selection().prompt == selected.prompt
    asyncio.run(check())
    assert not calls

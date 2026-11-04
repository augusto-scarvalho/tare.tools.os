"""Leave the local GPU to others: while the machine's choice is on, tare skips local models everywhere."""
import asyncio
import os

import pytest

from relay import challenger_variants, delegation, turn_engine
from test_native_agent import deployment

LOCAL, CLOUD = 'http://127.0.0.1:8080/v1', 'https://api.example.test/v1'


def profile(endpoint, name):
    value = deployment(endpoint=endpoint)
    value['id'] = name
    return value


@pytest.fixture
def avoiding(monkeypatch):
    monkeypatch.setenv(turn_engine.AVOID_LOCAL_GPU, '1')


def test_routing_skips_local_routes_only_while_the_option_is_on(monkeypatch, avoiding):
    monkeypatch.setattr('relay.native_catalog.select', lambda name: profile(LOCAL if name == 'local' else CLOUD, name))
    monkeypatch.setattr('relay.native_catalog.resolve_host_mode', lambda value: value)
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda value: None)
    tier, name, chosen, skipped = turn_engine.choose([['local'], ['cloud']])
    assert name == 'cloud' and len(skipped) == 1
    assert skipped[0]['route'] == 'local' and skipped[0]['reason'] == 'local GPU left to others'
    assert skipped[0]['diagnostic']['code'] == 'LOCAL_GPU_DISABLED'
    monkeypatch.delenv(turn_engine.AVOID_LOCAL_GPU)
    assert turn_engine.choose([['local'], ['cloud']])[1] == 'local'


def test_the_choice_is_saved_for_the_machine_and_the_environment_forces_it(monkeypatch):
    monkeypatch.delenv(turn_engine.AVOID_LOCAL_GPU, raising=False)
    assert not turn_engine.avoid_local_gpu()
    turn_engine.leave_local_gpu(True)  # what the TUI's option and tare serve's /local-gpu off save
    assert turn_engine.avoid_local_gpu()
    turn_engine.leave_local_gpu(False)
    assert not turn_engine.avoid_local_gpu()
    monkeypatch.setenv(turn_engine.AVOID_LOCAL_GPU, '1')
    assert turn_engine.avoid_local_gpu()
    monkeypatch.delenv(turn_engine.AVOID_LOCAL_GPU)
    turn_engine.gpu_store().write_text('{not json', encoding='utf-8')
    assert not turn_engine.avoid_local_gpu()  # an unreadable choice uses the GPU, as before the option existed


def test_the_local_judge_is_not_even_asked(monkeypatch, avoiding):
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('the gateway was queried'))
    assert turn_engine.local_judge() is None


def test_delegation_explores_with_the_hosted_explorer_instead_of_a_local_one(monkeypatch, tmp_path, avoiding):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_DELEGATION_POLICY', raising=False)
    value = dict(delegation.DEFAULT, enabled=True, profile='local-explorer')
    monkeypatch.setattr(delegation, 'policy', lambda: dict(value))
    monkeypatch.setattr(challenger_variants, 'realize', lambda name: dict(
        deployment=profile(LOCAL if name == 'local-explorer' else CLOUD, name), identity={}, key=name, label=name,
        accounting='local' if name == 'local-explorer' else 'quota', task_types=['exploration'], resource_pool=name))
    # Nothing but the local GPU to explore with: no delegation, and never the session's own model.
    assert delegation.snapshot('session-a', 'implementer', profile(CLOUD, 'cloud-parent')) is None
    value['overflow'] = 'hosted-explorer'
    chosen = delegation.snapshot('session-b', 'implementer', profile(CLOUD, 'cloud-parent'))
    assert chosen['variant']['deployment']['id'] == 'hosted-explorer' and chosen['selection'] == 'OVERFLOW_EXPLORER'
    assert 'overflow' not in chosen


def test_the_tui_option_moves_to_tare_auto_and_refuses_a_local_model(tmp_path, monkeypatch):
    from textual.widgets import Checkbox, Select, TextArea
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from test_review_checks_panel import until  # the Changed event can take more than one pause on a busy runner
    monkeypatch.delenv(turn_engine.AVOID_LOCAL_GPU, raising=False)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(WorkConsole, 'local_model', staticmethod(
        lambda value: (value if isinstance(value, str) else value.get('id')) == 'local-qwen'))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            model = app.query_one('#model', Select)
            model.set_options([('local-qwen', 'local-qwen'), ('tare-auto', 'tare-auto')])
            model.value = 'local-qwen'
            app.query_one('#avoid-local-gpu', Checkbox).value = True
            await until(pilot, lambda: turn_engine.avoid_local_gpu() and model.value == 'tare-auto')
            model.value = 'local-qwen'  # chosen again on purpose
            app.query_one('#prompt', TextArea).load_text('hello')
            app.begin_assistant()
            await pilot.pause()
            assert 'local GPU' in str(app.query_one('#status').render()) and not app.backend.launches
            app.query_one('#avoid-local-gpu', Checkbox).value = False
            await until(pilot, lambda: not turn_engine.avoid_local_gpu())
    try:
        asyncio.run(scenario())
    finally:
        os.environ.pop(turn_engine.AVOID_LOCAL_GPU, None)


def test_a_conversation_leaves_local_models_out_for_itself_only(monkeypatch):
    monkeypatch.setattr('relay.native_catalog.select', lambda name: profile(LOCAL if name == 'local' else CLOUD, name))
    monkeypatch.setattr('relay.native_catalog.resolve_host_mode', lambda value: value)
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda value: None)
    with turn_engine.local_models(False):  # what a tare serve chat or a TUI escalation sets for its conversation
        tier, name, chosen, skipped = turn_engine.choose([['local'], ['cloud']])
        assert name == 'cloud' and len(skipped) == 1
        assert skipped[0]['route'] == 'local' and skipped[0]['reason'] == 'no local models in this chat'
        assert skipped[0]['diagnostic']['code'] == 'LOCAL_GPU_DISABLED'
        assert turn_engine.local_judge() is None and not turn_engine.leaves_gpu_to_others()
    assert turn_engine.choose([['local'], ['cloud']])[1] == 'local'  # the choice ended with the conversation


def test_the_agent_cli_runs_a_conversation_without_local_models(monkeypatch, capsys):
    from relay import native_agent
    monkeypatch.setenv(turn_engine.AVOID_LOCAL_GPU, '0')  # restored after the test
    native_agent.main(['--no-local-models', '--list-models'])
    assert os.environ[turn_engine.AVOID_LOCAL_GPU] == '1'


def test_a_tui_conversation_starts_without_local_models_and_keeps_the_choice(tmp_path, monkeypatch):
    from textual.widgets import Checkbox, Select, TextArea
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from test_review_checks_panel import until
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(WorkConsole, 'local_model', staticmethod(
        lambda value: (value if isinstance(value, str) else value.get('id')) == 'local-qwen'))

    project = tmp_path/'project'  # project preferences live outside the project
    project.mkdir()

    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            model, prompt = app.query_one('#model', Select), app.query_one('#prompt', TextArea)
            model.set_options([('local-qwen', 'local-qwen'), ('tare-auto', 'tare-auto')])
            model.value = 'local-qwen'
            app.query_one('#local-models', Checkbox).value = False
            await until(pilot, lambda: model.value == 'tare-auto')  # a local pick moves to tare-auto
            assert not turn_engine.leaves_gpu_to_others()  # the machine is untouched
            model.value = 'local-qwen'  # chosen again on purpose
            prompt.load_text('hello')
            app.begin_assistant()
            await pilot.pause()
            assert 'this conversation uses no local models' in str(app.query_one('#status').render())
            assert not app.backend.launches
            model.value = 'tare-auto'
            prompt.load_text('hello')
            app.begin_assistant()
            await until(pilot, lambda: app.backend.launches)
            assert '--no-local-models' in app.backend.launches[-1]  # every launch of this conversation
            app.backend.cancel(app.backend.process)
            await app.workers.wait_for_complete()
            app.new_demand()
            assert app.selection().local_models is False  # the next conversation starts the same way
    asyncio.run(scenario())

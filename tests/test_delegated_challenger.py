"""The ordinary delegated implementer must reach the same isolated trial contract."""
from relay import native_agent
from test_challenger import project
from test_challenger import test_native_session_automatically_runs_one_isolated_challenger as _exercise_trial


def test_delegated_implementer_runs_one_isolated_challenger(project, tmp_path, monkeypatch):
    run = native_agent.run_session

    def delegated(*args, **kwargs):
        return run(*args, **kwargs, delegated_leaf=True)

    monkeypatch.setattr(native_agent, 'run_session', delegated)
    _exercise_trial(project, tmp_path, monkeypatch)
    from pathlib import Path
    marker=native_agent.read_json(tmp_path/'primary'/'challenger.json')
    job=Path(marker['job']); row=native_agent.read_json(job/'run.json')
    assert row['execution_contract']['delegated_leaf'] is True
    from relay.command_protection import probe_sandbox
    if not probe_sandbox():  # the local challenger is refused there, as _exercise_trial asserts: no session to read
        assert not (job/'implementation'/'native-agent.json').exists()
        return
    saved=native_agent.read_json(job/'implementation'/'native-agent.json')
    assert saved['delegated_leaf'] is True
    assert saved['instruction']==row['instruction']

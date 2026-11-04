"""Public Resume composes Kernel proof, Runtime reconciliation and TUI activity."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
from compute_plane.native_journal import NativeJournal
from relay import native_agent
from test_native_agent import Client, deployment, setup, steps


@pytest.mark.parametrize('legacy', [False, True])
def test_resume_after_edit_checkpoint_crash_preserves_effect_and_updates_activity(setup, monkeypatch, legacy):
    config, custody, root = setup
    client = Client(steps=steps())
    original = NativeJournal.append
    def die(self, event):
        if event['kind'] == 'effect_returned' and event['result']['effect_id'].endswith('/effect/2'):
            raise SystemExit('crash after edit')
        return original(self, event)
    with monkeypatch.context() as fault:
        fault.setattr(NativeJournal, 'append', die)
        with pytest.raises(SystemExit):
            native_agent.run_session(config, deployment(), custody, 'Implement.', client=client, local_counter=client)
    if legacy:
        # Exact snapshots existed before effect receipts were introduced.
        for path in (custody/'scratch/effect-receipts').glob('*.json'):
            path.unlink()
    before = (root/'a.py').read_bytes()
    result = native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED' and client.generations == 4
    assert before == (root/'a.py').read_bytes() == b'value = 2\n'
    events = [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    assert sum(e['kind'] == 'effect_requested' and e['tool_call']['name'] == 'edit' for e in events) == 1
    row = next(r for r in result['tool_activity']['items'] if r['operation'] == 'edit')
    assert row['state'] == 'succeeded' and row['duration_ms'] is None


def test_hard_process_crash_during_model_request_resumes_with_original_limits(setup):
    config, custody, root = setup
    script = root.parent/'crash.py'
    script.write_text('import os,sys\n'
        + f'sys.path.insert(0, {str(Path(__file__).parent)!r})\n'
        + 'from test_native_agent import Client, deployment, steps\n'
        + 'from relay.native_agent import run_session\n'
        + 'client=Client(steps=steps(), after_model=lambda:os._exit(86))\n'
        + f'run_session({config!r},deployment(),{str(custody)!r},"Implement.",client=client,local_counter=client)\n')
    child = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=20)
    assert child.returncode == 86, child.stderr.decode()
    manifest = json.loads((custody/'native-agent.json').read_bytes())
    assert 'ended_at' not in manifest['invocations'][-1]
    client = Client(steps=steps())
    result = native_agent.run_session(None, None, custody, resume=True, resume_interrupted_model=True,
        client=client, local_counter=client)
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 5
    after = json.loads((custody/'native-agent.json').read_bytes())
    assert after['deadline_utc'] == manifest['deadline_utc']
    events = [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    abandoned = next(e for e in events if e['kind'] == 'model_abandoned')
    assert abandoned['owner_interrupted'] and abandoned['usage'] is None

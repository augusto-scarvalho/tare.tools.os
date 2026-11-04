"""CLI regression: native session output through restricted Windows-style pipes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


TASK = 'Correção de sessão — ação 漢字 🔎'


@pytest.fixture
def saved(tmp_path):
    repo = tmp_path/'project'
    repo.mkdir()
    storage = tmp_path/'storage'
    session = storage/'agent-sessions'/'example'
    session.mkdir(parents=True)
    state = {'schema': 'tare.tools/native-agent/2', 'session_id': 'example',
        'instruction': TASK, 'config': {'repositories': {'project': str(repo)}, 'role': 'implementer'},
        'deployment': {'id': 'route', 'native_profile': {'model': 'model'}},
        'invocations': [{'started_at': 100, 'ended_at': 101, 'status': 'PROPOSED'}]}
    (session/'native-agent.json').write_text(json.dumps(state), encoding='utf-8')
    (session/'journal.jsonl').write_bytes(b'')
    (session/'writer.lock').write_bytes(b'0')
    return repo, storage, session


def invoke(saved, command, mode, encoding):
    repo, storage, session = saved
    env = dict(os.environ, TARE_STORAGE_ROOT=str(storage), PYTHONIOENCODING=encoding+':strict',
               PYTHONDONTWRITEBYTECODE='1')
    # Explicit candidate owner imports also propagate to the actual subprocess.
    source = str(Path(__file__).resolve().parents[1])
    script = "import sys; sys.path.insert(0, " + repr(source) + "); from tare_ops import main; raise SystemExit(main(sys.argv[1:]))"
    args = [command, '--root', str(repo), '--format', mode]
    if command == 'review':
        args.insert(1, '--last')
    return subprocess.run([sys.executable, '-B', '-c', script, *args], env=env,
                          capture_output=True, timeout=15)


@pytest.mark.parametrize('encoding', ['cp1252', 'ascii', 'utf-8'])
@pytest.mark.parametrize('command', ['sessions', 'review'])
@pytest.mark.parametrize('mode', ['json', 'text'])
def test_cli_output_preserves_unicode_or_escapes_unrepresentable_text(saved, encoding, command, mode):
    repo, storage, session = saved
    before = {p.name: p.read_bytes() for p in session.iterdir()}
    result = invoke(saved, command, mode, encoding)
    assert result.returncode == 0, result.stderr.decode('ascii', errors='replace')
    if mode == 'json':
        # JSON must survive UTF-8 machine consumption with exact Unicode values.
        payload = json.loads(result.stdout.decode('utf-8'))
        assert (payload['sessions'][0]['task'] if command == 'sessions' else payload['task']) == TASK
    else:
        text = result.stdout.decode(encoding)
        expected = TASK.encode(encoding, errors='backslashreplace').decode(encoding)
        assert expected in text
    assert before == {p.name: p.read_bytes() for p in session.iterdir()}

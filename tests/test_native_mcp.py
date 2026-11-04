"""Public MCP transport, real Runtime effects and bounded session custody."""
import json
import os
import sys
import threading
import time

import anyio
import pytest

pytest.importorskip('mcp')
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from relay import native_agent, native_profiles
from relay.native_catalog import select, available_models
from relay.native_mcp import Service
from test_native_agent import Client, deployment, steps


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path/'repo'
    root.mkdir()
    (root/'a.py').write_bytes(b'value = 1\n')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path/'profiles'))
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', None)
    source = tmp_path/'profile.json'
    value = deployment()
    source.write_text(json.dumps(value))
    native_profiles.install('fixture', source)
    return root, source


def test_named_profiles_validate_redact_and_do_not_shadow(setup):
    root, source = setup
    assert select('fixture') == deployment()
    assert any(row['deployment'] == 'fixture' for row in available_models())
    summary = native_profiles.describe('fixture')
    assert summary['effective_limits']['max_model_calls'] == 16
    assert 'endpoint' not in json.dumps(summary)
    assert summary['subscription_quota'] == 'unknown'
    with pytest.raises(FileExistsError):
        native_profiles.install('fixture', source)
    for name in ('../outside', 'auto', 'local-qwen', 'UPPER', 'bad/name'):
        with pytest.raises(ValueError):
            native_profiles.install(name, source)
    bad = native_profiles.directory()/'broken.json'
    bad.write_text('{')
    assert any(row.get('availability') == 'invalid_profile' for row in native_profiles.inventory())
    assert any(row['deployment'] == 'fixture' for row in available_models())
    source.write_text(json.dumps(deployment())+' '*65536)
    native_profiles.install('larger-profile', source)
    assert select('larger-profile') == deployment()
    source.write_text(' '*262145)
    with pytest.raises(ValueError, match='over_budget'):
        native_profiles.install('oversized', source)
    source.write_text('[]')
    with pytest.raises(ValueError, match='object_and_profile'):
        native_profiles.install('array', source)


@pytest.mark.anyio
async def test_real_runtime_edit_verify_retry_and_continuation(setup, monkeypatch):
    root, _ = setup
    client = Client(steps=[*steps(), ('tare_deliver', {'summary': 'Follow-up complete.'})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient', lambda *a, **k: client)
    service = Service(root, ['fixture'], write_paths=['a.py'], context=False,
                      verifiers={'unit': [sys.executable, '-B', '-c',
                                         "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]})
    result = await service.start('edit-1', 'Change and verify a.py', 'fixture')
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 4
    assert (root/'a.py').read_bytes() == b'value = 2\n'
    assert result['tokens']['output_tokens']['known_sum'] == 80
    usage = native_profiles.usage(service.path('edit-1'))
    assert usage['model_calls'] == 4 and usage['tokens'] == result['tokens']
    assert usage['subscription_quota'] == 'unknown'
    saved = native_agent.read_json(service.path('edit-1')/'native-agent.json', 1024*1024)
    await service.start('edit-1', 'Change and verify a.py', 'fixture')
    assert client.generations == 4
    result = await service.resume('edit-1', 'message-1', 'Confirm completion.')
    assert result['status'] == 'PROPOSED' and client.generations == 5
    await service.resume('edit-1', 'message-1', 'Confirm completion.')
    assert client.generations == 5
    assert result['deadline_utc'] == saved['deadline_utc']
    assert result['effective_limits'] == saved['limits']
    with pytest.raises(ValueError, match='different input'):
        await service.resume('edit-1', 'message-1', 'Different input')
    with pytest.raises(ValueError, match='different input'):
        await service.start('edit-1', 'Different task', 'fixture')
    readonly = Service(root, ['fixture'], context=False)
    with pytest.raises(ValueError, match='scope/profile changed'):
        readonly.inspect('edit-1')


@pytest.mark.anyio
async def test_readonly_and_call_ceiling_are_enforced_by_runtime(setup, monkeypatch):
    root, _ = setup
    client = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}),
                          ('tare_deliver', {'summary': 'Read the file.'})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient', lambda *a, **k: client)
    service = Service(root, ['fixture'], context=False, max_model_calls=1)
    assert service.catalog()['writable'] == {}
    result = await service.start('read-1', 'Read a.py', 'fixture')
    assert result['status'] == 'EXHAUSTED' and client.generations == 1
    result = await service.resume('read-1', 'resume-1')
    assert result['status'] == 'EXHAUSTED' and client.generations == 1
    requests = [body for path, body in client.calls if path.endswith('/completions')]
    assert not {'edit', 'verify', 'terminal_start'} & {t['function']['name'] for t in requests[0]['tools']}
    assert (root/'a.py').read_bytes() == b'value = 1\n'
    with pytest.raises(ValueError, match='ceiling'):
        await service.start('read-2', 'Read', 'fixture', calls=2)
    assert not service.path('read-2').exists()


@pytest.mark.anyio
async def test_cancellation_joins_worker_and_busy_server_refuses_overlap(setup, monkeypatch):
    root, _ = setup
    service = Service(root, ['fixture'], context=False)
    entered, cleaned = threading.Event(), threading.Event()
    def operation(cancel):
        entered.set()
        while not cancel.is_cancelled():
            time.sleep(.01)
        time.sleep(.03)
        cleaned.set()
    async with anyio.create_task_group() as group:
        group.start_soon(service.execute, 'cancel-1', operation)
        while not entered.is_set():
            await anyio.sleep(.01)
        with pytest.raises(ValueError, match='busy'):
            await service.start('second', 'Must not run', 'fixture')
        group.cancel_scope.cancel()
    assert cleaned.is_set() and not service.active


def test_startup_and_unlimited_profile_are_bounded(setup):
    root, source = setup
    value = deployment()
    value['native_execution_limits'] = {key: None for key in
        ('max_model_calls', 'max_model_retries', 'max_stagnant_turns', 'max_seconds', 'request_seconds')}
    source.write_text(json.dumps(value))
    native_profiles.install('unlimited', source)
    service = Service(root, ['unlimited'], context=False, max_model_calls=4, max_seconds=60)
    assert service.limits('unlimited') == {'max_model_calls': 4, 'max_model_retries': 1,  # one, never unlimited
        'max_stagnant_turns': 3, 'max_seconds': 60, 'request_seconds': 60, 'connect_seconds': 60, 'read_seconds': 60}
    with pytest.raises(ValueError):
        Service(root, ['fixture'], max_seconds=float('inf'))
    with pytest.raises(ValueError):
        Service(root, ['fixture'], write_paths=['../outside'])
    with pytest.raises(ValueError):
        service.path('../escape')
    with pytest.raises(ValueError, match='outside'):
        Service(root.parent, ['fixture'])


@pytest.mark.anyio
@pytest.mark.timeout(60)
async def test_actual_stdio_handshake_and_tools_without_model_call(setup, tmp_path):
    root, _ = setup
    # The real entrypoint, SDK transport and schema negotiation run in a child process.
    command, args = sys.executable, ['-B', '-c',
        'from relay.native_installation import main; raise SystemExit(main())',
        'mcp', '--root', str(root), '--deployment', 'fixture', '--no-context']
    parameters = StdioServerParameters(command=command, args=args,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
    with anyio.fail_after(45):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info.serverInfo.name == 'tare.tools'
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {
                    'tare_profiles', 'tare_run', 'tare_inspect', 'tare_continue', 'tare_cancel',
                    'tare_review', 'tare_usage'}
                result = await session.call_tool('tare_profiles', {})
                assert not result.isError
                value = json.loads(result.content[0].text)
                assert value['profiles'][0]['deployment'] == 'fixture'
                assert value['writable'] == {}
                denied = await session.call_tool('tare_run', {'request_id': 'bad',
                    'instruction': 'No model should run', 'profile': 'fixture', 'max_model_calls': 999})
                assert denied.isError
    assert not (tmp_path/'storage'/'agent-sessions').exists()


@pytest.mark.anyio
@pytest.mark.timeout(60)
async def test_sdk_cancellation_notification_waits_for_cleanup(setup):
    from mcp import types
    root, _ = setup
    entered, cleaned = root/'entered', root/'cleaned'
    # A blocking host fixture proves wire cancellation reaches and joins the worker.
    script = '''
import time
from pathlib import Path
from relay import native_agent
from relay.native_mcp import main
def run(config, deployment, custody, instruction=None, **options):
    root = Path(next(iter(config['repositories'].values())))
    (root/'entered').touch()
    while not options['cancellation'].is_cancelled():
        time.sleep(.01)
    time.sleep(.1)
    (root/'cleaned').touch()
native_agent.run_session = run
raise SystemExit(main())
'''
    parameters = StdioServerParameters(command=sys.executable, args=['-B', '-c', script,
        '--root', str(root), '--deployment', 'fixture', '--no-context'],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
    with anyio.fail_after(45):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                rpc_id = session._request_id  # SDK request correlation, not Tare's durable ID.
                async def invoke():
                    try:
                        await session.call_tool('tare_run', {'request_id': 'wire-cancel',
                            'instruction': 'Wait for cancellation', 'profile': 'fixture'})
                    except Exception:
                        pass  # A cancelled MCP request need not yield a tool result.
                async with anyio.create_task_group() as group:
                    group.start_soon(invoke)
                    while not entered.exists():
                        await anyio.sleep(.02)
                    await session.send_notification(types.ClientNotification(types.CancelledNotification(
                        params=types.CancelledNotificationParams(requestId=rpc_id, reason='test'))))
                    while not cleaned.exists():
                        await anyio.sleep(.02)
                    group.cancel_scope.cancel()
                while True:
                    result = await session.call_tool('tare_cancel', {'request_id': 'wire-cancel'})
                    assert not result.isError
                    if json.loads(result.content[0].text)['status'] == 'NOT_ACTIVE':
                        break
                    await anyio.sleep(.05)
    assert cleaned.exists()

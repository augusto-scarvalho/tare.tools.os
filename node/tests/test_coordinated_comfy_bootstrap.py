"""Exercise the real launcher against a small Comfy-compatible module tree."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('comfy_bootstrap_fixture', ROOT/'tare_node/coordinated_comfy.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path/'comfy-root'; root.mkdir()
    (root/'comfy').mkdir()
    (root/'comfy/__init__.py').write_text('')
    (root/'comfy/model_management.py').write_text('')
    (root/'execution.py').write_text('import comfy.model_management\nclass PromptExecutor:\n def execute(self,*args): pass\n')
    (root/'server.py').write_text('''class Routes:
 def get(self, path):
  assert path == '/tare/gpu/status'
  return lambda function: function
class PromptServer:
 def __init__(self):
  import threading
  from types import SimpleNamespace
  self.routes=Routes()
  self.prompt_queue=SimpleNamespace(mutex=threading.RLock(),history={})
 async def start_multi_address(self):
  import builtins
  builtins.comfy_bootstrap_events.append('listening')
''')
    (root/'main.py').write_text('''import asyncio,execution,server
assert hasattr(execution.PromptExecutor.execute, '__wrapped__')
asyncio.run(server.PromptServer().start_multi_address())
''')
    manifest = tmp_path/'sources.json'

    def seal():
        manifest.write_text(json.dumps({name:hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in ('main.py','server.py','execution.py','comfy/model_management.py')}))

    seal()
    events = []
    import builtins
    monkeypatch.setattr(builtins, 'comfy_bootstrap_events', events, raising=False)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(sys, 'argv', list(sys.argv))
    for name in ('execution','server','comfy','comfy.model_management'):
        monkeypatch.delitem(sys.modules, name, raising=False)

    class Lease:
        def __init__(self, *args, **kwargs): pass
        @contextmanager
        def hold(self, kind, request_id):
            events.append('acquired')
            try: yield {'nonce':'test'}
            finally: events.append('released')

    monkeypatch.setattr(bootstrap, 'SharedGpuLease', Lease)
    monkeypatch.setattr(bootstrap, 'yield_text_backend', lambda *args: events.append('yield_text'))
    monkeypatch.setattr(bootstrap, 'release_models', lambda *args: events.append('cleanup'))
    argv=['--comfy-root',str(root),'--gpu-lock',str(tmp_path/'gpu.lock'),
          '--source-manifest',str(manifest),'--','--port','8188']
    yield root, argv, events, seal
    for name in ('execution','server','comfy','comfy.model_management'):
        sys.modules.pop(name, None)
    for coordinator in bootstrap._STARTUP_QUARANTINE:
        for context in coordinator.quarantine:
            context.__exit__(None, None, None)
    bootstrap._STARTUP_QUARANTINE.clear()


def test_bootstrap_coordinates_startup_before_listening(installation):
    _, argv, events, _ = installation
    bootstrap.main(argv)
    assert events == ['acquired','yield_text','cleanup','released','listening']


def test_changed_upstream_source_refuses_before_gpu_acquisition(installation):
    root, argv, events, _ = installation
    (root/'execution.py').write_text('raise AssertionError("must not import")')
    with pytest.raises(ValueError, match='ComfyUI changed'):
        bootstrap.main(argv)
    assert not events


def test_custom_node_cannot_replace_executor_and_start_unmanaged(installation):
    root, argv, events, seal = installation
    (root/'main.py').write_text('''import asyncio,execution,server
execution.PromptExecutor.execute=lambda *args: None
asyncio.run(server.PromptServer().start_multi_address())
''')
    seal()
    with pytest.raises(RuntimeError, match='replaced'):
        bootstrap.main(argv)
    assert events == ['acquired','yield_text']
    assert bootstrap._STARTUP_QUARANTINE


def test_completed_history_is_imported_once_after_successful_bind(installation):
    root, argv, events, seal = installation
    snapshot=root.parent/'history.json'
    snapshot.write_text(json.dumps({'prior-job':{'outputs':{'saved':True}}}))
    (root/'main.py').write_text("import asyncio,execution,server\ns=server.PromptServer()\nassert s.prompt_queue.history['prior-job']['outputs']['saved'] is True\nasyncio.run(s.start_multi_address())\n")
    seal()
    i=argv.index('--'); argv[i:i]=['--history-snapshot',str(snapshot)]
    bootstrap.main(argv)
    assert not snapshot.exists()
    assert json.loads(snapshot.with_suffix('.imported.json').read_text())['prior-job']['outputs']['saved']
    assert events[-1]=='listening'

"""Local installations must not turn fixture tests into live model calls."""
import os
from pathlib import Path
import tempfile

import pytest

# The kernel's CodexHttpClient subclasses compute_plane.native_models.NativeHttpClient, and relay imports its module
# on first use. Tests replace NativeHttpClient with stand-in functions: imported for the first time under such a
# patch, the subclass inherits from the function and the import fails. Import it once, before any test patches.
try:
    import compute_plane.native_codex_auth  # noqa: F401
except ImportError:  # no kernel installed: the tests that need it skip themselves
    pass


@pytest.fixture(autouse=True)
def disable_installed_flow_assessor(monkeypatch, tmp_path_factory):
    # Numbered mktemp scans all earlier entries. Five scans per test became
    # quadratic on Windows as the suite accumulated thousands of directories.
    # mkdtemp atomically chooses one unique root without enumerating basetemp.
    state = Path(tempfile.mkdtemp(prefix='op-', dir=tmp_path_factory.getbasetemp()))

    def state_dir(name):
        path = state/name
        path.mkdir()
        return path

    # Workspace locks are per machine; tests keep theirs out of the operator's.
    monkeypatch.setenv('TARE_LOCK_ROOT', str(tmp_path_factory.getbasetemp()/'locks'))
    # Project permission modes are per machine too.
    monkeypatch.setenv('TARE_MODE_STORE', str(tmp_path_factory.getbasetemp()/'project-modes.json'))
    # So is leaving the local GPU to others: one file per test, since a test that leaves it would skip local
    # routes in every later one (and the operator's choice must not reach a test).
    monkeypatch.setenv('TARE_GPU_STORE', str(state_dir('gpu')/'local-gpu.json'))
    # Heads-up notes would add a judge and a writer call to every fixture turn; tests of them turn them on.
    monkeypatch.setenv('TARE_HEADS_UP', 'off')
    monkeypatch.setenv('TARE_HEADS_UP_STORE', str(state_dir('heads-up')/'heads-up.json'))
    monkeypatch.delenv('TARE_AVOID_LOCAL_GPU', raising=False)
    # Launch flags constrain real workers, not the fixture's authored route policy.
    monkeypatch.delenv('TARE_SESSION_SCOPE', raising=False)
    # Storage cleanup removes other tools' files (Codex logs, caches): a test sees only an empty home, and the
    # operator's automatic-cleanup choice never reaches it.
    tools = state_dir('tools')
    monkeypatch.setattr('relay.storage.tool_roots', lambda: (tools, tools, tools))
    monkeypatch.setenv('TARE_STORAGE_AUTO', str(tools/'storage-auto.json'))
    # Auto-update syncs the operator's VS Code model list from the running serve: never from a test.
    monkeypatch.setattr('relay.native_product.published_models', lambda: [])
    # Tests exercising activation explicitly supply their own policy/worker.
    monkeypatch.setenv('TARE_FLOW_POLICY', 'off')
    monkeypatch.setenv('TARE_DELEGATION_POLICY', 'off')
    # An operator's hosted Jev key would turn delivery gates and routing shadows into paid calls.
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    # nor a key in the operator's vault: tests that set TYPESAFE_API_KEY themselves still reach Jev
    monkeypatch.setattr('relay.turn_engine.jev_key', lambda: os.environ.get('TYPESAFE_API_KEY', '').strip())
    # Fixture sessions must not add rows to the operator's shadow of session turns.
    monkeypatch.setenv('TARE_WORK_FORM_SHADOW', 'off')
    # Nor see what the operator chose to use on this machine: no file is today's behaviour.
    monkeypatch.setenv('TARE_ENVIRONMENT_STORE', str(state_dir('environment')/'environment.json'))
    # Nor the operator's gateway or Codex catalogue: the picker's default and the older models it leaves out would
    # depend on this machine's files.
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://127.0.0.1:8080')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path_factory.getbasetemp()/'no-codex-home'))
    # Nor the operator's installation: its descriptor names their storage, and a workspace that holds its history
    # folder falls back to LOCALAPPDATA. Tests wrote demands into the operator's TUI history through both.
    appdata = state_dir('appdata')
    monkeypatch.setenv('LOCALAPPDATA', str(appdata))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(appdata/'storage'))
    # Optional /usage Work history must never discover the operator's control custody.
    # Direct reader tests pass their own owner; console fixture journeys see an empty owner.
    from relay import flow_usage
    saved_work_view = flow_usage.completed_work_usage_rows
    fixture_control = state_dir('work-usage-owner')/'control'
    fixture_control.mkdir()
    monkeypatch.setattr(flow_usage, 'completed_work_usage_rows',
                        lambda control_root, **kwargs: saved_work_view(fixture_control, **kwargs))
    # Nor any provider credential of the operator's: a fixture test that reached a provider would spend real money.
    for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY', 'DEEPSEEK_API_KEY',
                 'SERPAPI_API_KEY', 'FIRECRAWL_API_KEY', 'AWS_ACCESS_KEY_ID',
                 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE'):
        monkeypatch.delenv(name, raising=False)
    # New search fallback must not discover a runner/operator key in the vault.
    # A test can explicitly replace this resolver to exercise simulated keys.
    from relay import native_generation
    vault = native_generation.vault_secret
    monkeypatch.setattr(native_generation, 'vault_secret', lambda name:
        None if name in {'DEEPSEEK_API_KEY', 'SERPAPI_API_KEY', 'FIRECRAWL_API_KEY'} else vault(name))
    # The processes a test starts (the HTTP worker of every model call, tare's CLI and server, a vendor CLI) are out
    # of the socket guard's reach: they inherit a proxy that refuses, and a vault with none of the operator's keys.
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9')
    for name in ('NO_PROXY', 'no_proxy'):
        monkeypatch.setenv(name, 'localhost,127.0.0.1,::1')
    monkeypatch.setenv('PYTHON_KEYRING_BACKEND', 'keyring.backends.null.Keyring')
    # The quota readings taken at startup must not run the operator's vendor CLIs; a test replaces these to read.
    from relay import provider_quota

    def no_vendor_cli(*args, **kwargs):
        raise OSError('tests do not run vendor CLIs')
    monkeypatch.setattr(provider_quota, 'run', no_vendor_cli)
    monkeypatch.setattr(provider_quota, 'codex_limits', no_vendor_cli)


LOOPBACK = ('127.', '::1', 'localhost')


@pytest.fixture(autouse=True)
def offline(request, monkeypatch):
    """The suite never leaves this machine: a socket to any address but loopback is refused, so a test that would
    reach a real provider (and the operator's quota) fails here instead. `@pytest.mark.live` opts a test out."""
    if request.node.get_closest_marker('live'):
        return
    import socket
    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else ''
        if isinstance(host, str) and host and not host.startswith(LOOPBACK) and self.family != socket.AF_UNIX:
            raise ConnectionRefusedError(f'tests are offline: {host} (mark the test live to reach it)')
        return real_connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)

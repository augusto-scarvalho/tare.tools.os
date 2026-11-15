"""A model call is judged by the silence of its stream, not by a fixed clock (owner, 2026-11-14)."""
from types import SimpleNamespace

import pytest

from relay import native_generation, native_limits
from relay.native_limits import SILENCE_SECONDS, http_options, resolve

HTTP = {'native_profile': {'protocol': 'openai-chat', 'endpoint': 'https://models.example/v1'},
        'native_execution_limits': {'request_seconds': 300}}
STREAMING = {**HTTP, 'native_streaming': True}
CLAUDE = {'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://claude'},
          'native_execution_limits': {'request_seconds': 300}}
CODEX = {**CLAUDE, 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://codex'}}
AGY = {**CLAUDE, 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://agy'}}
UNLIMITED = {name: None for name in ('max_seconds', 'request_seconds', 'connect_seconds', 'read_seconds')}
# What each caller passes today: a limited session, the unlimited conversation, a planner, an auditor, a work unit.
CALLERS = {'limited': {'max_seconds': 300}, 'unlimited': UNLIMITED,
           'planner': {'max_seconds': 600, 'request_seconds': 600},
           'auditor': {'max_seconds': 1200, 'request_seconds': 600},
           'unit': {'max_model_calls': 8, 'max_seconds': 900, 'request_seconds': 900}}


@pytest.mark.parametrize('caller', CALLERS)
@pytest.mark.parametrize('route', [STREAMING, CLAUDE, CODEX, AGY], ids=['streaming-http', 'claude-cli', 'codex-cli', 'agy-cli'])
def test_a_streaming_route_has_no_total_and_waits_one_silence_window(route, caller):
    limits = resolve(route, CALLERS[caller])
    assert limits['request_seconds'] is None
    assert limits['read_seconds'] == limits['connect_seconds'] == SILENCE_SECONDS == 600
    assert limits['max_seconds'] == CALLERS[caller]['max_seconds']  # the session's own budget is untouched
    assert http_options(limits)['timeout_seconds'] is None


@pytest.mark.parametrize('caller', CALLERS)
@pytest.mark.parametrize('route', [
    HTTP,  # not streaming: the call cannot show life while it works
    {**STREAMING, 'native_profile': {'protocol': 'anthropic-messages', 'endpoint': 'https://models.example/v1'}}],
    ids=['non-streaming-http', 'anthropic-messages'])
def test_a_route_that_cannot_show_life_keeps_the_total_it_had(route, caller):
    limits = resolve(route, CALLERS[caller])
    expected = {'limited': 300, 'unlimited': None, 'planner': 600, 'auditor': 600, 'unit': 900}[caller]
    assert limits['request_seconds'] == expected
    assert limits['read_seconds'] == expected


def test_a_number_the_caller_names_for_connecting_or_reading_is_kept():
    limits = resolve(STREAMING, {'connect_seconds': 20, 'read_seconds': 45})
    assert (limits['connect_seconds'], limits['read_seconds'], limits['request_seconds']) == (20, 45, None)


def test_the_codex_route_keeps_its_short_connection_wait_and_streams_without_a_total():
    deployment = {'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'https://codex.example/v1'},
                  'native_streaming': True}
    limits = resolve(deployment, {'max_seconds': 60, 'request_seconds': 60})
    assert (limits['request_seconds'], limits['connect_seconds'], limits['read_seconds']) == (None, 5, 600)


def test_saved_sessions_and_calls_without_a_route_keep_what_they_resolved():
    assert resolve({}, {'max_seconds': 300})['request_seconds'] == 300
    assert resolve({}, UNLIMITED)['read_seconds'] is None


@pytest.mark.parametrize('route,window', [(CLAUDE, SILENCE_SECONDS), (CODEX, SILENCE_SECONDS), (AGY, SILENCE_SECONDS)])
def test_every_cli_client_receives_the_window(monkeypatch, route, window):
    import compute_plane.native_cli as native_cli
    seen = {}
    monkeypatch.setattr(native_cli, 'VendorCliClient', lambda endpoint, **kwargs: seen.update(kwargs))
    deployment = {**route, 'native_cli': {'executable': 'C:/x/cli.exe', 'version': '1.0.0'}}
    profile = SimpleNamespace(protocol='vendor-cli', endpoint=route['native_profile']['endpoint'])
    native_generation.inference_client(deployment, profile, credential=lambda: None, metadata_only=True,
                                       **http_options(resolve(deployment, {'max_seconds': None, 'request_seconds': None})))
    assert seen['silence_seconds'] == window
    assert seen['timeout_seconds'] is None  # no total from the OS


def test_a_call_stopped_on_silence_is_recovered_like_one_stopped_by_a_timeout():
    from relay.native_model_recovery import RECOVERABLE_ERRORS
    assert {'CLI_INCOMPLETE_EXCHANGE', 'CLI_SILENT_EXCHANGE'} <= RECOVERABLE_ERRORS


@pytest.mark.parametrize('stage', ['plan', 'implement'])
def test_a_work_runs_stage_on_a_streaming_route_has_no_request_total(stage):
    from relay.native_work_host import stage_limits
    limits = stage_limits(CLAUDE, stage)
    assert limits['request_seconds'] is None and limits['read_seconds'] == SILENCE_SECONDS
    assert limits['max_seconds'] == (240 if stage == 'plan' else 600)  # the stage's own budget is untouched
    assert stage_limits(HTTP, stage)['request_seconds'] == 300  # not streamed: it cannot show life

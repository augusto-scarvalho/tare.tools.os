"""The coordinator reads links itself; internet research remains a separate evidence-producing role."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from compute_plane.native_journal import save_json
from compute_plane.native_models import encode
from relay import research_cli, research_tools, web_research as web
from relay.conversation_tasks import ConversationTools
from tare_tools_agent_runtime import BindingRef, CancellationToken, EffectRequest, ExecutionControl, ToolCall

ADDRESS = 'https://example.org/docs'
TEXT = 'A documented claim.\nThe cache TTL is five minutes.\n'


def effect(name, args, identity='effect'):
    return EffectRequest('parent', identity, ToolCall('tool-'+identity, name, encode(args)))


def report():
    return {'status': 'COMPLETED', 'summary': 'The cache TTL is five minutes.',
        'sources': [{'url': ADDRESS, 'title': 'Documentation'}], 'remaining': [],
        'findings': [{'claim': 'TTL is five minutes.', 'url': ADDRESS,
                      'anchor': 'cache TTL', 'kind': 'observed'}]}


def test_researcher_prefers_luna_6_without_redirecting_other_installed_roles():
    from relay.turn_engine import DEFAULT_POLICY, HELPER_ROUTES
    assert DEFAULT_POLICY['researcher'][:3] == ['codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli', 'agy-cli']
    from relay.openrouter import RESEARCH_ROUTES
    assert tuple(DEFAULT_POLICY['researcher'][3:]) == RESEARCH_ROUTES
    assert web.DEFAULT_ROUTES == tuple(DEFAULT_POLICY['researcher'])
    assert HELPER_ROUTES == ('codex-cli-gpt-6-luna',)


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path/'session'; root.mkdir()
    execution = ExecutionControl(BindingRef('parent', 'binding', 'owner', 1, 'opaque'),
        CancellationToken(), 100000, monotonic=lambda: 0)
    inner = SimpleNamespace(names=('read', 'search', 'edit'), tool_specs_json=encode({'read': {}, 'search': {}, 'edit': {}}),
        session_id='parent', binding=execution.binding, observations=lambda: {})
    deployment = {'id': 'luna', 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://codex', 'model': 'gpt-6.1-luna'}}
    state = {'conversation_flow': True, 'research': {'routes': {'luna': deployment}, 'route_order': ['luna']},
             'limits': {'max_model_calls': 30}, 'config': {'repositories': {}}}
    fetched = []
    def fetch(address, control, **kwargs):
        fetched.append(address)
        return {'url': address, 'status': 'OK', 'text': TEXT, 'title': 'Documentation',
                'fetched_at': __import__('time').time(), 'sha256': 'fixture', 'method': 'trafilatura_local'}
    monkeypatch.setattr(web, 'read_page', fetch)
    return research_tools.ResearchTools(inner, root, state), execution, fetched


def test_direct_url_is_admitted_to_coordinator_without_a_researcher_or_extra_model(host, monkeypatch):
    tools, execution, fetched = host
    monkeypatch.setattr(research_cli, 'research', lambda *a, **k: pytest.fail('URL reading spawned a researcher'))
    coordinator = ConversationTools(tools, tools.root, tools.state, 'turn')
    assert {'read_url', 'research', 'search'} <= set(coordinator.names) and 'edit' not in coordinator.names
    first = json.loads(coordinator.execute(effect('read_url', {'url': ADDRESS, 'limit': 1}), execution).output_json)
    second = json.loads(coordinator.execute(effect('read_url', {'url': ADDRESS, 'start_line': 2}), execution).output_json)
    assert first['text'] == 'A documented claim.' and second['text'] == 'The cache TTL is five minutes.'
    assert second['reused'] and fetched == [ADDRESS] and web.committed(tools.root) == 0
    assert first['authority'] == 'NONE' and first['method'] == 'trafilatura_local'


@pytest.mark.parametrize('address', ['file:///secret', 'https://user:password@example.org', 'not a URL'])
def test_invalid_urls_never_reach_reader(host, address):
    tools, execution, fetched = host
    assert tools.execute(effect('read_url', {'url': address}), execution).status == 'refused'
    assert fetched == []


def test_real_access_failure_is_returned_without_claiming_the_machine_is_offline(host, monkeypatch):
    tools, execution, _ = host
    monkeypatch.setattr(web, 'read_page', lambda *a, **k: {'status': 'FAILED', 'error': 'HTTP 403', 'url': ADDRESS})
    value = json.loads(tools.execute(effect('read_url', {'url': ADDRESS}), execution).output_json)
    assert value['error'] == 'HTTP 403' and value['status'] == 'FAILED'


def test_missing_research_dependency_refuses_only_the_tool_without_reserving_or_repeating(host, monkeypatch):
    import builtins
    tools, execution, fetched = host
    original = builtins.__import__
    def missing(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'research_cli' and level == 1:
            raise ModuleNotFoundError("No module named 'jsonschema'")
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', missing)
    result = tools.execute(effect('research', {'question': 'Find documentation.'}), execution)
    value = json.loads(result.output_json)
    assert result.status == 'refused' and value['status'] == 'UNAVAILABLE'
    assert value['capability'] == 'research' and 'jsonschema' in value['reason']
    assert web.committed(tools.root) == 0 and not list(tools.root.glob('web-research/*/receipt.json'))
    assert tools.execute(effect('read_url', {'url': ADDRESS}), execution).status == 'succeeded'
    assert fetched == [ADDRESS]


def test_research_returns_checked_sources_and_reuses_identical_question_without_another_call(host, monkeypatch):
    tools, execution, fetched = host
    calls = []
    def worker(*args, **kwargs):
        calls.append(kwargs)
        return {'status': 'COMPLETED', 'report': report(), 'calls_used': 2, 'usage': {'cache_read_tokens': 17}}
    monkeypatch.setattr(research_cli, 'research', worker)
    first = json.loads(tools.execute(effect('research', {'question': 'What is the TTL?'}), execution).output_json)
    second = json.loads(tools.execute(effect('research', {'question': 'What is the TTL?'}, 'next'), execution).output_json)
    assert first['status'] == 'COMPLETED' and first['sources'][0]['sha256'] == 'fixture'
    assert second['reused'] and len(calls) == 1 and fetched == [ADDRESS]
    assert web.committed(tools.root) == 2 and second['usage']['cache_read_tokens'] == 17


def test_missing_source_anchor_never_becomes_evidence(host):
    tools, execution, _ = host
    value = report(); value['findings'][0]['anchor'] = 'A claim that does not appear on the page.'
    checked = web.verified_report(tools.root, value, execution)
    assert checked['status'] == 'PARTIAL' and checked['findings'] == [] and checked['remaining']
    assert checked['summary'] != value['summary']


def test_anchor_lookup_normalizes_formatting_but_requires_literal_content(host, monkeypatch):
    tools, execution, _ = host
    monkeypatch.setattr(web, 'read_page', lambda address, *a, **k: {
        'url': address, 'status': 'OK', 'text': 'Set `include_links=True` to keep [links](https://example.org).',
        'fetched_at': __import__('time').time()})
    value = report(); value['findings'][0]['anchor'] = 'include_links=True'
    value['findings'][0]['claim'] = 'Use include_links=True to keep links.'
    assert web.verified_report(tools.root, value, execution)['status'] == 'COMPLETED'
    value['findings'][0]['anchor'] = 'include_links=False'
    rejected = web.verified_report(tools.root, value, execution)
    assert rejected['status'] == 'PARTIAL' and rejected['rejected_excerpts'][0]['reason'] == 'anchor_not_found'


def test_literal_identifier_anchors_are_not_reinterpreted_as_markdown(host, monkeypatch):
    tools, execution, _ = host
    monkeypatch.setattr(web, 'read_page', lambda address, *a, **k: {
        'url': address, 'status': 'OK', 'text': 'Use the `__init__` method, not an arbitrary word init.',
        'fetched_at': __import__('time').time()})
    value = report(); value['findings'][0]['anchor'] = '__init__'
    assert web.verified_report(tools.root, value, execution)['status'] == 'COMPLETED'
    value['findings'][0]['anchor'] = '__missing__'
    assert web.verified_report(tools.root, value, execution)['status'] == 'PARTIAL'


def test_a_failed_paid_attempt_does_not_silently_spawn_a_second_provider(host, monkeypatch):
    tools, execution, _ = host
    tools.state['research']['routes']['other'] = deepcopy(tools.state['research']['routes']['luna'])
    tools.state['research']['route_order'].append('other')
    called = []
    def fail(deployment, *a, **k):
        called.append(deployment)
        return {'status': 'FAILED', 'error': 'Search denied by provider.'}
    monkeypatch.setattr(research_cli, 'research', fail)
    value = json.loads(tools.execute(effect('research', {'question': 'Search this.'}), execution).output_json)
    assert len(called) == 1 and value['status'] == 'FAILED' and 'Search denied' in value['reason']
    assert value['usage'] is None and web.committed(tools.root) == 6


def test_research_receipt_names_the_actual_mapped_model_and_effort(host, monkeypatch):
    tools, execution, _ = host
    monkeypatch.setattr(research_cli, 'research', lambda *a, **k: {'status': 'COMPLETED',
        'report': report(), 'calls_used': 2, 'model': 'gemini-3.8-flash-medium'})
    result = json.loads(tools.execute(effect('research', {'question': 'Search.'}), execution).output_json)
    assert result['model'] == 'gemini-3.8-flash-medium' and result['effort'] == 'medium'
    receipt = web.records(tools.root)[0]
    assert receipt['model'] == result['model'] and receipt['effort'] == 'medium'


def test_authoritative_model_rejection_can_fall_back_without_replacing_luna_with_an_older_model(host, monkeypatch):
    tools, execution, _ = host
    tools.state['research']['routes']['haiku'] = {'id': 'haiku', 'native_profile': {
        'protocol': 'vendor-cli', 'endpoint': 'cli://claude', 'model': 'claude-haiku-5-5'}}
    tools.state['research']['route_order'].append('haiku')
    seen = []
    def worker(deployment, *a, **kwargs):
        seen.append((deployment['native_profile']['model'], kwargs['max_calls']))
        if len(seen) == 1:
            return {'status': 'FAILED', 'not_started': True, 'calls_used': 1, 'error': 'model_not_found'}
        return {'status': 'COMPLETED', 'report': report(), 'calls_used': 2}
    monkeypatch.setattr(research_cli, 'research', worker)
    result = json.loads(tools.execute(effect('research', {'question': 'Search.'}), execution).output_json)
    assert result['status'] == 'COMPLETED' and result['route'] == 'haiku'
    assert seen == [('gpt-6.1-luna', 6), ('claude-haiku-5-5', 5)]
    assert web.committed(tools.root) == 3 and result['skipped'][0]['not_started']


def test_cancelled_control_never_dispatches(host, monkeypatch):
    tools, execution, fetched = host
    execution.cancellation.cancel()
    assert tools.execute(effect('read_url', {'url': ADDRESS}), execution).status == 'refused'
    assert fetched == []


def test_report_retention_keeps_accounting_and_limits_full_report_copies(host):
    tools, _, _ = host
    for i in range(10):
        target = tools.root/'web-research'/str(i)/'receipt.json'
        target.parent.mkdir(parents=True)
        save_json(target, {'status': 'COMPLETED', 'calls_used': 2,
                          'result': {'summary': 'A full report.'}})
    web.trim_reports(tools.root)
    assert web.committed(tools.root) == 20
    assert sum('result' in r for r in web.records(tools.root)) == 8


def test_codex_task_tools_are_rejected_even_alongside_a_valid_web_result():
    rows = [{'type': 'item.completed', 'item': {'type': 'web_search'}},
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'unwanted'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(report())}}]
    with pytest.raises(ValueError, match='Unexpected researcher tool'):
        research_cli.decode(rows, 'cli://codex')


def test_json_stdout_failure_preserves_the_actual_provider_diagnostic():
    message = 'The requested model is not available for this account.'
    rows = [{'type': 'thread.started', 'thread_id': 'test'},
            {'type': 'error', 'message': message}, {'type': 'turn.failed', 'error': {'message': message}}]
    assert message in research_cli.failure_message(rows, b'', {'exit_code': 1})


def test_agy_response_json_is_accepted_when_the_structured_output_field_is_absent():
    rows = [{'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'search_web'}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'response': json.dumps(report())}}]
    assert research_cli.decode(rows, 'cli://agy')['report'] == report()


def test_weekly_cli_limit_is_an_authoritative_pre_generation_rejection(host, monkeypatch):
    tools, execution, _ = host
    profile = {'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://claude', 'model': 'claude-haiku-5-5'}}
    monkeypatch.setattr(research_cli, 'command', lambda *a: (['fixture'], {}, 'claude-haiku-5-5'))
    failure = {'type': 'result', 'is_error': True,
               'result': "You've hit your weekly limit · resets tomorrow"}
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', lambda *a, **k:
        ({'exit_code': 1, 'cleanup_verified': True}, encode(failure).encode(), b''))
    noted = []
    monkeypatch.setattr('relay.turn_engine.note_usage_limit', lambda deployment: noted.append(deployment))
    result = research_cli.research(profile, 'Search the docs.', root=tools.root, control=execution, max_calls=6)
    assert result['not_started'] and result['calls_used'] == 1 and noted == [profile]
    assert 'weekly limit' in result['error']


def test_agy_call_ceiling_allows_final_serialization_but_blocks_another_generation(tmp_path, monkeypatch, capsys):
    import io
    import sys
    from relay import research_cli_hook
    target = tmp_path/'boundary.jsonl'
    target.write_text('\n'.join(json.dumps({'phase': 'pre'}) for _ in range(4))+'\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['hook.py', str(target), 'post', '4'])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{}')))
    research_cli_hook.main()
    assert json.loads(capsys.readouterr().out) == {}
    monkeypatch.setattr(sys, 'argv', ['hook.py', str(target), 'pre', '4'])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{}')))
    research_cli_hook.main()
    assert json.loads(capsys.readouterr().out)['terminationBehavior'] == 'terminate'


def test_agy_hook_denies_task_commands_even_under_web_permission_bypass(tmp_path, monkeypatch, capsys):
    import io
    import sys
    from relay import research_cli_hook
    target = tmp_path/'boundary.jsonl'
    monkeypatch.setattr(sys, 'argv', ['hook.py', str(target), 'tool', '6'])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(
        json.dumps({'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'unwanted'}}}).encode())))
    research_cli_hook.main()
    assert json.loads(capsys.readouterr().out)['decision'] == 'deny'


@pytest.mark.parametrize('endpoint', ['cli://codex', 'cli://claude', 'cli://agy'])
def test_cli_structured_report_requires_an_observed_web_tool(endpoint):
    value = report()
    if endpoint == 'cli://codex':
        rows = [{'type': 'item.completed', 'item': {'type': 'web_search'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(value)}}]
    elif endpoint == 'cli://claude':
        rows = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'WebSearch'}]}},
                {'type': 'result', 'structured_output': value, 'usage': {'cache_read_input_tokens': 3}}]
    else:
        rows = [{'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'search_web'}},
                {'event': 'result', 'result': {'structured_output': value, 'usage': {'cache_read_tokens': 3}}}]
    assert research_cli.decode(rows, endpoint)['report'] == value
    with pytest.raises(ValueError, match='without an observed web tool'):
        research_cli.decode(rows[1:], endpoint)


@pytest.mark.parametrize('endpoint', ['cli://codex', 'cli://claude', 'cli://agy'])
def test_researcher_command_is_read_only_medium_and_reuses_connection(tmp_path, host, endpoint):
    from relay import cli_discovery
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    name = {'cli://codex': 'codex-cli-gpt-6-1-luna', 'cli://claude': 'claude-haiku-5-5-cli', 'cli://agy': 'agy-cli'}[endpoint]
    value = cli_discovery.deployment(templates[name], '/fixture/cli')
    directory = tmp_path/'connection'; directory.mkdir()
    save_json(directory/'connection.json', {'features': [], 'session_id': 'saved-session'})
    args, _, model = research_cli.command(value, directory, 'Stable researcher role.', host[1], 6)
    assert '--ephemeral' not in args
    assert ('--dangerously-skip-permissions' in args) is (endpoint == 'cli://agy')
    if endpoint == 'cli://codex':
        assert 'gpt-6.1-luna' == model and 'resume' in args and 'saved-session' in args
        assert args.index('--ignore-user-config') > args.index('resume')
        assert 'web_search="live"' in args and 'model_reasoning_effort="medium"' in args
        record = json.loads((directory/'models.json').read_text(encoding='utf-8'))['models'][0]
        assert record['shell_type'] == 'disabled' and record['apply_patch_tool_type'] is None
    elif endpoint == 'cli://claude':
        assert args[args.index('--tools')+1] == 'WebSearch,WebFetch' and '--resume' in args
        assert args[args.index('--effort')+1] == 'medium'
    else:
        assert model == 'gemini-3.8-flash-medium' and '--conversation' in args
        agent = (directory/'.agents/agents/tare-researcher/agent.md').read_text(encoding='utf-8')
        assert 'tools: [search_web, read_url_content, finish]' in agent
        assert 'commandExecutionPolicy: "off"' in agent


def test_researcher_inherits_unlimited_calls_and_keeps_verified_sources(host, monkeypatch):
    from relay import execution_scope as scope
    tools, execution, _ = host
    tools.state['limits']['max_model_calls'] = None
    calls = []
    def worker(*args, **kwargs):
        calls.append(kwargs['max_calls'])
        return {'status': 'COMPLETED', 'report': report(), 'calls_used': 9}
    monkeypatch.setattr(research_cli, 'research', worker)
    with scope.activated(scope.Scope(unlimited=('calls',))):
        result = json.loads(tools.execute(effect('research', {'question': 'What is the TTL?'}), execution).output_json)
    assert calls == [None] and result['status'] == 'COMPLETED'
    assert result['sources'] and web.committed(tools.root) == 9


def test_unlimited_claude_research_does_not_pass_an_internal_turn_cap(tmp_path, host):
    from relay import cli_discovery
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf8'))
    value = cli_discovery.deployment(templates['claude-haiku-5-5-cli'], '/fixture/cli')
    directory = tmp_path/'connection'; directory.mkdir()
    save_json(directory/'connection.json', {'features': []})
    args, _, _ = research_cli.command(value, directory, 'Research only.', host[1], None)
    assert '--max-turns' not in args
    assert args[args.index('--allowedTools')+1] == 'WebSearch,WebFetch'


def test_unlimited_agy_hook_still_denies_commands(tmp_path):
    import subprocess
    import sys
    from relay import research_cli_hook
    boundary = tmp_path/'boundary.jsonl'
    boundary.write_text('\n'.join(json.dumps({'phase': 'pre'}) for _ in range(10))+'\n')
    base = [sys.executable, '-I', '-B', research_cli_hook.__file__, str(boundary)]
    result = subprocess.run([*base, 'pre', 'unlimited'], input='{}', text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {}
    result = subprocess.run([*base, 'tool', 'unlimited'],
        input=json.dumps({'toolCall': {'name': 'run_command'}}), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['decision'] == 'deny'


from test_role_pin_dispatch import pin_catalog  # noqa: F401
from test_role_pins import parse


@pytest.fixture
def offline_cli(host, monkeypatch):
    from relay import cli_discovery, native_agent
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    monkeypatch.setattr(native_agent, 'prompt_capsule', lambda *a: {'system': 'PRIVATE SYSTEM'})
    monkeypatch.setattr('compute_plane.native_cli_codex.discover', lambda *a, **k: [])
    def value(endpoint):
        name = {'cli://codex': 'codex-cli-gpt-6-1-luna',
                'cli://claude': 'claude-haiku-5-5-cli', 'cli://agy': 'agy-cli'}[endpoint]
        deployment = cli_discovery.deployment(templates[name], '/fixture/cli')
        deployment['native_profile']['parameters_json'] = '{"effort":"high"}'
        return deployment
    return value


@pytest.fixture
def installed_research_defaults(host, offline_cli, tmp_path, monkeypatch):
    from relay import cli_discovery, environment, execution_scope as scope, turn_engine
    profiles = tmp_path/'profiles'; profiles.mkdir()
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    for name in ('codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli', 'agy-cli'):
        save_json(profiles/(name+'.json'), cli_discovery.deployment(templates[name], str(tmp_path/'cli')))
    monkeypatch.setenv('TARE_PROFILE_DIR', str(profiles))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    routing = tmp_path/'routing.json'
    monkeypatch.setattr(turn_engine, 'policy_path', lambda: routing)
    monkeypatch.setattr(environment, 'read', lambda: {})
    monkeypatch.setattr(turn_engine, 'usage_limited', lambda value: None)
    monkeypatch.setattr(host[0], 'search_fallback', lambda *a, **k: pytest.fail('Unexpected API fallback'))
    processes = []
    def process(argv, **kwargs):
        assert '-m' in argv and 'model_reasoning_effort="medium"' in argv
        processes.append(list(argv))
        rows = [{'type': 'thread.started', 'thread_id': 'offline-session'},
                {'type': 'item.completed', 'item': {'type': 'web_search'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': encode(report())}}]
        return {'exit_code': 0, 'cleanup_verified': True}, ('\n'.join(map(encode, rows))).encode(), b''
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', process)
    with scope.activated(scope.Scope()):
        yield routing, processes


def test_default_snapshot_resolves_installed_profile_and_dispatches_luna_offline(host, installed_research_defaults):
    from relay import native_catalog, turn_engine
    tools, execution, fetched = host
    routing, processes = installed_research_defaults
    assert not routing.exists()
    assert turn_engine.policy()['researcher'] == list(web.DEFAULT_ROUTES)
    frozen = web.snapshot()
    assert frozen['route_order'][:3] == ['codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli', 'agy-cli'], frozen
    assert list(frozen['routes']) == frozen['route_order']
    assert frozen['origin'] == 'CONFIGURED'
    assert set(frozen['unavailable']) == {'openrouter-muse-spark-1-3-contributor'}
    assert 'codex-cli-gpt-6-1-luna' not in frozen['routes']
    assert frozen['routes']['codex-cli-gpt-6-luna'] == native_catalog.select('codex-cli-gpt-6-luna')
    tools.state['research'] = frozen
    before = encode(frozen)
    args = {'question': 'What is the documented TTL?'}
    result = tools.investigate(effect('research', args), args, execution)
    assert result['status'] == 'COMPLETED', result
    assert len(processes) == 1
    argv = processes[0]
    assert argv[argv.index('-m')+1] == 'gpt-6-luna'
    assert 'model_reasoning_effort="medium"' in argv
    receipt, = web.records(tools.root)
    attempt, = receipt['attempts']
    assert attempt['model'] == 'gpt-6-luna' and attempt['reasoning'] == 'medium'
    assert attempt['route'] == receipt['route'] == result['route'] == 'codex-cli-gpt-6-luna'
    assert receipt['model'] == result['model'] == 'gpt-6-luna'
    assert receipt['executed_decision']['mode'] == 'CONFIGURED'
    assert receipt['executed_decision']['executed_deployment'] == 'codex-cli-gpt-6-luna'
    assert encode(tools.state['research']) == before
    assert fetched == [ADDRESS] and not routing.exists()


def test_saved_old_routing_and_frozen_research_are_not_migrated(host, offline_cli, installed_research_defaults):
    from relay import native_catalog, turn_engine
    tools, execution, fetched = host
    routing, processes = installed_research_defaults
    old = 'codex-cli-gpt-6-1-luna'
    saved_order = [old, 'claude-haiku-5-5-cli', 'agy-cli']
    raw = (json.dumps({'researcher': saved_order}, indent=2)+'\r\n').encode()
    routing.write_bytes(raw)
    assert turn_engine.policy()['researcher'] == saved_order
    with pytest.raises(ValueError, match='native_deployment_unknown:'):
        native_catalog.select(old)
    configured = web.snapshot()
    assert configured['origin'] == 'CONFIGURED'
    assert configured['route_order'] == saved_order[1:], configured
    assert list(configured['routes']) == saved_order[1:]
    assert list(configured['unavailable']) == [old]
    assert old not in configured['routes'] and routing.read_bytes() == raw
    # Existing sessions retain the full saved profile even if its selector is no longer installed.
    tools.state['research'] = {'version': 1, 'routes': {old: offline_cli('cli://codex')},
        'route_order': [old], 'unavailable': {}, 'origin': 'CONFIGURED', 'search_api': 1}
    before = encode(tools.state['research'])
    args = {'question': 'What is the saved session TTL?'}
    result = tools.investigate(effect('research', args), args, execution)
    assert result['status'] == 'COMPLETED', result
    assert len(processes) == 1
    argv = processes[0]
    assert argv[argv.index('-m')+1] == 'gpt-6.1-luna'
    receipt, = web.records(tools.root)
    attempt, = receipt['attempts']
    assert attempt['model'] == 'gpt-6.1-luna'
    assert attempt['route'] == receipt['route'] == result['route'] == old
    assert receipt['executed_decision']['executed_deployment'] == old
    assert encode(tools.state['research']) == before
    assert routing.read_bytes() == raw and turn_engine.policy()['researcher'] == saved_order
    assert web.snapshot() == configured and fetched == [ADDRESS]


@pytest.mark.parametrize('endpoint', ['cli://codex', 'cli://claude', 'cli://agy'])
@pytest.mark.parametrize('resumed', [False, True])
def test_real_research_cli_launch_matches_actual_command(host, offline_cli, monkeypatch, endpoint, resumed):
    tools, execution, _ = host
    value = offline_cli(endpoint)
    directory = tools.root/'research-connections'/web.key(value['native_profile'])
    directory.mkdir(parents=True)
    if resumed:
        save_json(directory/'connection.json', {'session_id': 'saved-session', 'features': []})
    launches, processes = [], []
    def process(argv, **kwargs):
        assert len(launches) == 1 and not processes
        launch = launches[0]
        flag = '-m' if endpoint == 'cli://codex' else '--model'
        assert launch == {'endpoint': endpoint, 'model': argv[argv.index(flag)+1],
                          'effort': 'medium', 'resume': resumed}
        if endpoint == 'cli://codex':
            assert 'model_reasoning_effort="medium"' in argv
            assert ('resume' in argv) is resumed
            rows = [{'type': 'thread.started', 'thread_id': 'session'},
                    {'type': 'item.completed', 'item': {'type': 'web_search'}},
                    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': encode(report())}}]
        elif endpoint == 'cli://claude':
            assert argv[argv.index('--effort')+1] == 'medium'
            assert ('--resume' in argv) is resumed
            rows = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'WebSearch'}]}},
                    {'type': 'result', 'structured_output': report(), 'session_id': 'session', 'num_turns': 2}]
        else:
            assert launch['model'] == value['native_reasoning_models']['medium']
            assert argv[argv.index('--effort')+1] == 'medium'
            assert ('--conversation' in argv) is resumed
            (directory/'boundary.jsonl').write_text(encode({'phase': 'pre', 'model': launch['model']})+'\n', encoding='utf-8')
            rows = [{'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'search_web'}},
                    {'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': report()}}]
        processes.append(argv)
        return {'exit_code': 0, 'cleanup_verified': True}, ('\n'.join(map(encode, rows))).encode(), b''
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', process)
    result = research_cli.research(value, 'PRIVATE QUESTION', root=tools.root, control=execution,
                                   max_calls=6, on_dispatch=launches.append)
    assert result['status'] == 'COMPLETED', result
    assert result['dispatched'] == launches[0] and len(processes) == 1
    assert 'PRIVATE' not in encode(launches) and 'argv' not in launches[0]


@pytest.mark.parametrize('outcome', ['failed', 'not_started', 'cancelled', 'timeout', 'decode', 'exception'])
def test_real_research_cli_post_launch_failures_keep_dispatch(host, offline_cli, monkeypatch, outcome):
    tools, execution, _ = host
    launches = []
    def process(*a, **k):
        assert len(launches) == 1
        if outcome == 'exception':
            raise OSError('process failed')
        physical = {'exit_code': 0 if outcome == 'decode' else 1, 'cleanup_verified': True}
        if outcome in {'cancelled', 'timeout'}:
            physical[outcome] = True
        if outcome == 'not_started':
            physical['exit_code'] = 2
            return physical, b'', b'unexpected argument'
        return physical, b'not JSON' if outcome == 'decode' else b'', b''
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', process)
    result = research_cli.research(offline_cli('cli://claude'), 'Search.', root=tools.root,
        control=execution, max_calls=6, on_dispatch=launches.append)
    assert result['status'] == 'FAILED' and result['dispatched'] == launches[0] and len(launches) == 1
    assert bool(result.get('not_started')) is (outcome == 'not_started')


@pytest.mark.parametrize('stage', ['capsule', 'command', 'discover', 'prepare', 'unsupported'])
def test_real_research_cli_preparation_failure_has_no_dispatch(host, offline_cli, monkeypatch, stage):
    from relay import native_agent
    tools, execution, _ = host
    value = offline_cli('cli://codex')
    def fail(*a, **k):
        raise ValueError('preparation refused')
    if stage == 'capsule':
        monkeypatch.setattr(native_agent, 'prompt_capsule', fail)
    elif stage == 'command':
        monkeypatch.setattr(research_cli, 'command', fail)
    elif stage in {'discover', 'prepare'}:
        monkeypatch.setattr('compute_plane.native_cli_codex.'+stage, fail)
    else:
        value['native_profile']['endpoint'] = 'cli://unsupported'
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', lambda *a, **k: pytest.fail('unexpected process'))
    launches = []
    result = research_cli.research(value, 'Search.', root=tools.root, control=execution,
                                   max_calls=6, on_dispatch=launches.append)
    assert result['status'] == 'FAILED' and 'dispatched' not in result and launches == []


def test_researcher_pin_records_effective_reasoning_not_profile_default(host, pin_catalog, monkeypatch):
    from relay import execution_scope as scope
    tools, execution, _ = host
    launches = []
    def process(argv, **kwargs):
        assert argv[argv.index('--effort')+1] == 'max'
        assert launches[0]['effort'] == 'max'
        return {'exit_code': 1, 'cleanup_verified': True}, b'', b'offline'
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', process)
    monkeypatch.setattr('relay.native_agent.prompt_capsule', lambda *a: {'system': 'PRIVATE'})
    with scope.activated(parse(['--researcher', 'anthropic/test-cli@max'])):
        with scope.for_role('researcher'):
            value = scope.pinned('researcher')
            assert json.loads(pin_catalog['test-cli']['native_profile']['parameters_json'])['effort'] == 'medium'
            assert json.loads(value['native_profile']['parameters_json'])['effort'] == 'max'
            result = research_cli.research(value, 'Search.', root=tools.root, control=execution,
                                           max_calls=6, on_dispatch=launches.append)
    assert result['dispatched']['effort'] == 'max' and len(launches) == 1


@pytest.mark.parametrize('outcome', ['COMPLETED', 'FAILED', 'NOT_STARTED', 'CANCELLED', 'TIMEOUT', 'INTERRUPTED'])
def test_research_attempt_is_durable_before_process_and_settled_only_after(host, offline_cli, monkeypatch, outcome):
    tools, execution, _ = host
    value = offline_cli('cli://claude')
    tools.state['research'] = {'routes': {'actual': value}, 'route_order': ['actual'], 'origin': 'CONFIGURED'}
    def process(argv, **kwargs):
        receipt = web.records(tools.root)[0]
        assert receipt['executed_decision']['mode'] == 'CONFIGURED'
        attempt = receipt['attempts'][0]
        assert attempt['status'] == 'DISPATCHING' and attempt['calls_used'] == 'UNKNOWN'
        assert attempt['money'] == 'UNKNOWN' and attempt['reasoning'] == 'medium'
        assert attempt['model'] == argv[argv.index('--model')+1]
        if outcome == 'INTERRUPTED':
            raise KeyboardInterrupt('host stopped')
        if outcome == 'COMPLETED':
            rows = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'WebSearch'}]}},
                    {'type': 'result', 'structured_output': report(), 'session_id': 'paid-session', 'num_turns': 2}]
            return {'exit_code': 0, 'cleanup_verified': True}, ('\n'.join(map(encode, rows))).encode(), b''
        physical = {'exit_code': 2 if outcome == 'NOT_STARTED' else 1, 'cleanup_verified': True}
        if outcome in {'CANCELLED', 'TIMEOUT'}:
            physical[outcome.lower()] = True
        return physical, b'', b'unexpected argument' if outcome == 'NOT_STARTED' else b'failed'
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', process)
    request = effect('research', {'question': 'PRIVATE QUESTION', 'route': 'actual'})
    if outcome == 'INTERRUPTED':
        with pytest.raises(KeyboardInterrupt):
            tools.investigate(request, {'question': 'PRIVATE QUESTION', 'route': 'actual'}, execution)
    else:
        tools.investigate(request, {'question': 'PRIVATE QUESTION', 'route': 'actual'}, execution)
    saved = web.records(tools.root)[0]
    expected = 'DISPATCHING' if outcome == 'INTERRUPTED' else outcome
    assert saved['attempts'][0]['status'] == expected
    assert saved['attempts'][0]['calls_used'] == (2 if outcome == 'COMPLETED' else 0 if outcome == 'NOT_STARTED' else 'UNKNOWN')
    before = encode(saved)
    if outcome == 'COMPLETED':
        assert tools.investigate(effect('research', {}, 'cache'), {'question': 'PRIVATE QUESTION', 'route': 'actual'}, execution)['reused']
    else:
        tools.investigate(request, {'question': 'PRIVATE QUESTION', 'route': 'actual'}, execution)
    assert encode(web.records(tools.root)[0]) == before and 'PRIVATE' not in encode(saved['attempts'])


def test_research_fallback_and_resume_preserve_each_prior_attempt(host, monkeypatch):
    tools, execution, _ = host
    second = deepcopy(tools.state['research']['routes']['luna']); second['id'] = 'second'
    tools.state['research']['routes']['second'] = second
    tools.state['research']['route_order'] = ['disabled', 'luna', 'second']
    disabled = deepcopy(second); disabled['id'] = 'disabled'
    tools.state['research']['routes']['disabled'] = disabled
    monkeypatch.setattr('relay.environment.allows', lambda d: (d['id'] != 'disabled', None))
    previous = []
    def worker(deployment, *a, **kwargs):
        name = deployment['id']
        kwargs['on_dispatch']({'endpoint': 'cli://codex', 'model': 'actual-'+name,
                               'effort': 'medium', 'resume': bool(previous)})
        saved = web.records(tools.root)[-1]
        assert saved['attempts'][-1]['status'] == 'DISPATCHING'
        if name == 'luna':
            return {'status': 'FAILED', 'not_started': True, 'calls_used': 1}
        assert saved['attempts'][0]['status'] == 'NOT_STARTED'
        return {'status': 'COMPLETED', 'report': report(), 'calls_used': 2}
    monkeypatch.setattr(research_cli, 'research', worker)
    tools.investigate(effect('research', {}, 'first'), {'question': 'Question.'}, execution)
    first = deepcopy(web.records(tools.root)[0]); previous.append(first)
    assert [a['status'] for a in first['attempts']] == ['NOT_STARTED', 'COMPLETED']
    assert first['executed_decision']['mode'] == 'UNKNOWN'
    assert first['executed_decision']['fallback']['skipped'] == [
        {'deployment': 'disabled', 'reason': 'DISABLED_OR_USAGE_LIMITED'},
        {'deployment': 'luna', 'reason': 'NOT_STARTED'}]
    tools.investigate(effect('research', {}, 'refresh'), {'question': 'Question.', 'refresh': True}, execution)
    records = web.records(tools.root)
    assert first in records and all(a['resume'] for r in records if r != first for a in r['attempts'])


def test_research_preparation_refusal_and_limit_create_no_fictional_dispatch(host, offline_cli, monkeypatch):
    tools, execution, _ = host
    value = offline_cli('cli://claude')
    tools.state['research'] = {'routes': {'actual': value}, 'route_order': ['actual']}
    monkeypatch.setattr(research_cli, 'command', lambda *a: (_ for _ in ()).throw(ValueError('prepare refused')))
    tools.investigate(effect('research', {}), {'question': 'Search.'}, execution)
    saved = web.records(tools.root)[0]
    assert saved['attempts'] == [] and 'executed_decision' not in saved
    tools.state['limits']['max_model_calls'] = 0
    assert tools.investigate(effect('research', {}, 'limited'), {'question': 'Other.'}, execution)['status'] == 'LIMIT_REACHED'
    assert web.records(tools.root) == [saved]


@pytest.mark.parametrize('refusal', ['startup', 'model_refused'])
def test_api_search_fallback_keeps_real_cli_attempts_and_decision(host, offline_cli, monkeypatch, refusal):
    tools, execution, _ = host
    first = offline_cli('cli://claude')
    second = deepcopy(first); second['id'] = 'second-cli'
    tools.state['research'] = {'routes': {'first': first, 'second': second},
                               'route_order': ['first', 'second'], 'origin': 'CONFIGURED'}
    launches, before_fallback = [], {}
    def process(argv, **kwargs):
        saved = web.records(tools.root)[0]
        assert len(saved['attempts']) == len(launches)+1
        assert saved['attempts'][-1]['status'] == 'DISPATCHING'
        assert saved['attempts'][-1]['model'] == argv[argv.index('--model')+1]
        assert saved['attempts'][-1]['reasoning'] == 'medium'
        if launches:
            assert saved['attempts'][0] == launches[0]
        settled = deepcopy(saved['attempts'][-1])
        from relay.native_routes import settle_attempt
        settle_attempt(settled, {'status': 'NOT_STARTED'})
        settled['calls_used'] = 0 if refusal == 'startup' else 1
        launches.append(settled)
        if refusal == 'startup':
            return {'exit_code': 2, 'cleanup_verified': True}, b'', b'unexpected argument'
        return {'exit_code': 1, 'cleanup_verified': True}, b'', b'model_not_found'
    def search(*args):
        saved = web.records(tools.root)[0]
        before_fallback.update(deepcopy(saved))
        assert saved['attempts'] == launches and len(launches) == 2
        return {'status': 'COMPLETED', 'results': []}
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', process)
    monkeypatch.setattr(research_tools.search_api, 'selection', lambda: ('fixture', None))
    monkeypatch.setattr(research_tools.search_api, 'search', search)
    args = {'question': 'Find the docs.'}
    request = effect('research', args)
    result = tools.investigate(request, args, execution)
    saved = web.records(tools.root)[0]
    assert result['status'] == 'PARTIAL' and saved['kind'] == 'api-search-fallback'
    assert saved['attempts'] == before_fallback['attempts'] == launches
    assert all(a['money'] == 'UNKNOWN' for a in saved['attempts'])
    assert saved['executed_decision'] == before_fallback['executed_decision']
    assert saved['executed_decision']['executed_deployment'] == 'second'
    assert saved['executed_decision']['fallback']['skipped'] == [
        {'deployment': 'first', 'reason': 'NOT_STARTED'},
        {'deployment': 'second', 'reason': 'NOT_STARTED'}]
    assert saved['calls_used'] == (0 if refusal == 'startup' else 2)
    assert len(saved['native_attempts']) == 2
    assert 'PRIVATE' not in encode(saved['attempts'])
    frozen = encode(saved)
    tools.investigate(request, args, execution)
    assert encode(web.records(tools.root)[0]) == frozen and len(launches) == 2


@pytest.mark.parametrize('preparation', [False, True])
def test_api_search_fallback_without_process_has_no_execution(host, offline_cli, monkeypatch, preparation):
    tools, execution, _ = host
    if preparation:
        tools.state['research'] = {'routes': {'cli': offline_cli('cli://claude')}, 'route_order': ['cli']}
        monkeypatch.setattr(research_cli, 'command', lambda *a: (_ for _ in ()).throw(ValueError('prepare refused')))
    else:
        tools.state['research'] = {'routes': {}, 'route_order': []}
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', lambda *a, **k: pytest.fail('unexpected process'))
    monkeypatch.setattr(research_tools.search_api, 'selection', lambda: ('fixture', None))
    monkeypatch.setattr(research_tools.search_api, 'search', lambda *a: {'status': 'COMPLETED', 'results': []})
    result = tools.investigate(effect('research', {}), {'question': 'Find docs.'}, execution)
    saved = web.records(tools.root)[0]
    assert result['status'] == ('FAILED' if preparation else 'PARTIAL')
    assert not saved.get('attempts') and 'executed_decision' not in saved

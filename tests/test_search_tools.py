"""Search belongs to the coordinator's read-only tools, with frozen legacy toolsets."""
import json

import pytest

from relay import research_cli, research_tools, web_research, web_search
from relay.conversation_tasks import ConversationTools
from test_research_tools import effect, host
from test_native_agent import Client, deployment, setup


def test_coordinator_can_search_without_a_researcher_and_read_only_the_relevant_url(host, monkeypatch):
    original, control, fetched = host
    original.state['research'].update(search_api=1, routes={}, route_order=[])
    tools = research_tools.ResearchTools(original.tools, original.root, original.state)
    coordinator = ConversationTools(tools, tools.root, tools.state, 'turn')
    monkeypatch.setattr(web_search, 'secret', lambda name: 'fixture-search-key')
    calls = []
    def request(provider, query, limit, execution, directory):
        calls.append((provider, query, limit))
        return {'status': 'COMPLETED', 'results': [{'url': 'https://docs.example.org/cache', 'title': 'Docs',
                 'snippet': 'A discovery snippet.'}], 'untrusted': True, 'safe_fallback': False}
    monkeypatch.setattr(web_search, 'request', request)
    monkeypatch.setattr(research_cli, 'research', lambda *a, **kw: pytest.fail('Direct search spawned a researcher'))
    assert 'search_web' in coordinator.names and 'edit' not in coordinator.names
    frozen = coordinator.tool_specs_json
    result = coordinator.execute(effect('search_web', {'query': 'cache docs'}), control)
    value = json.loads(result.output_json)
    assert result.status == 'succeeded' and value['provider'] == 'deepseek' and value['authority'] == 'NONE'
    assert fetched == [] and calls == [('deepseek', 'cache docs', 5)]
    coordinator.execute(effect('read_url', {'url': value['results'][0]['url']}, 'read-source'), control)
    assert fetched == ['https://docs.example.org/cache'] and web_research.committed(tools.root) == 0
    assert coordinator.tool_specs_json == frozen
    missing = json.loads(coordinator.execute(effect('research', {'question': 'cache docs'}, 'research'), control).output_json)
    assert missing['status'] == 'PARTIAL' and missing['discovery']['reused'] and 'read_url' in missing['next']
    contract = json.loads(coordinator.execute(effect('product_help', {'topic': 'web'}, 'help'), control).output_json)
    assert contract['search_web'] and contract['search_services'] == list(web_search.PROVIDERS)
    assert len(calls) == 1  # Product help observes settings without running another search.


def test_legacy_research_toolsets_gain_api_fallback_without_expansion_or_prefix_rewrite(host, monkeypatch):
    tools, control, _ = host
    assert 'search_web' not in tools.names and 'search_web' not in tools.tool_specs_json
    assert tools.state['research'].get('search_api') is None
    tools.state['research'].update(routes={}, route_order=[])
    original = tools.tool_specs_json
    monkeypatch.setattr(web_search, 'secret', lambda name: 'fixture-search-key')
    calls = []
    def search(provider, *args):
        calls.append(provider)
        return {'status': 'COMPLETED', 'results': [{'url': 'https://docs.example.org/guide'}], 'safe_fallback': False}
    monkeypatch.setattr(web_search, 'request', search)
    value = json.loads(tools.execute(effect('research', {'question': 'public API docs'}), control).output_json)
    assert value['status'] == 'PARTIAL' and value['findings'] == value['sources'] == []
    assert value['discovery']['results'] and 'read_url' in value['next']
    assert calls == ['deepseek'] and web_research.committed(tools.root) == 0
    assert tools.tool_specs_json == original and 'search_web' not in tools.names


def test_recovery_reads_the_search_receipt_and_never_repeats_an_unknown_http_effect(host, monkeypatch):
    from compute_plane.native_journal import save_json
    original, control, _ = host
    original.state['research']['search_api'] = 1
    tools = research_tools.ResearchTools(original.tools, original.root, original.state)
    request = effect('search_web', {'query': 'docs'})
    monkeypatch.setattr('compute_plane.native_effect_recovery.pending', lambda *a: request)
    monkeypatch.setattr(web_search, 'request', lambda *a: pytest.fail('Recovery repeated HTTP'))
    target = tools.root/'web-search'/web_research.key(request.effect_id)/'receipt.json'
    target.parent.mkdir(parents=True)
    save_json(target, {'status': 'RUNNING', 'requests_used': 1,
                      'request_sha256': web_research.key({'query': 'docs'})})
    recovered, result = tools.recover_effect(None)
    assert recovered == request and result.status == 'refused'
    assert json.loads(result.output_json)['status'] == 'INTERRUPTED'
    final = {'status': 'COMPLETED', 'results': [], 'provider': 'serpapi'}
    save_json(target, {'request_sha256': web_research.key({'query': 'docs'}), 'result': final})
    assert json.loads(tools.recover_effect(None)[1].output_json) == final


def test_native_open_weight_conversation_can_search_and_resume_without_researcher_or_prefix_rewrite(setup, monkeypatch):
    from relay import native_agent
    config, custody, project = setup
    monkeypatch.setattr(web_search, 'secret', lambda name: 'fixture-search-key')
    monkeypatch.setattr(research_cli, 'research', lambda *a, **kw: pytest.fail('Unexpected researcher model'))
    calls = []
    def request(provider, query, limit, control, directory):
        calls.append((provider, query))
        return {'status': 'COMPLETED', 'results': [], 'untrusted': True, 'safe_fallback': False}
    monkeypatch.setattr(web_search, 'request', request)
    client = Client(steps=[('search_web', {'query': 'public API docs'}),
                           ('tare_deliver', {'summary': 'No sources found in this search.'})])
    first = native_agent.run_session(config, deployment(), custody, 'Search for public API docs.',
        conversation=True, client=client, local_counter=client)
    assert first['status'] == 'PROPOSED' and calls == [('deepseek', 'public API docs')]
    follow = Client(steps=[('search_web', {'query': 'public API docs'}),
                           ('tare_deliver', {'summary': 'The saved search still has no sources.'})])
    second = native_agent.run_session(None, None, custody, resume=True, follow_up='Reuse that search.',
        message_id='follow-up', client=follow, local_counter=follow)
    assert second['status'] == 'PROPOSED' and len(calls) == 1
    before = next(b for p, b in client.calls if p == '/chat/completions')
    after = next(b for p, b in follow.calls if p == '/chat/completions')
    assert before['tools'] == after['tools'] and before['messages'][0] == after['messages'][0]
    assert 'search_web' in {t['function']['name'] for t in before['tools']}
    assert (project/'a.py').read_text() == 'value = 1\n'


def test_a_saved_toolset_can_gain_research_api_fallback_without_changing_its_cached_prefix(setup, monkeypatch):
    from relay import native_agent
    config, custody, _ = setup
    monkeypatch.setattr(web_research, 'snapshot', lambda: {'version': 1, 'routes': {}, 'route_order': [], 'unavailable': {}})
    first_client = Client(steps=[('tare_deliver', {'summary': 'The conversation is ready.'})])
    native_agent.run_session(config, deployment(), custody, 'Hello.', conversation=True,
        client=first_client, local_counter=first_client)
    monkeypatch.setattr(web_search, 'secret', lambda name: 'fixture-search-key')
    calls = []
    def request(provider, *args):
        calls.append(provider)
        return {'status': 'COMPLETED', 'results': [], 'safe_fallback': False}
    monkeypatch.setattr(web_search, 'request', request)
    follow = Client(steps=[('research', {'question': 'public documentation'}),
                           ('tare_deliver', {'summary': 'No sources were found.'})])
    result = native_agent.run_session(None, None, custody, resume=True, follow_up='Search for public documentation.',
        message_id='next', client=follow, local_counter=follow)
    assert result['status'] == 'PROPOSED' and calls == ['deepseek']
    before = next(b for p, b in first_client.calls if p == '/chat/completions')
    after = next(b for p, b in follow.calls if p == '/chat/completions')
    assert before['tools'] == after['tools'] and before['messages'][0] == after['messages'][0]
    assert 'search_web' not in {t['function']['name'] for t in after['tools']}
    assert web_research.committed(custody) == 0


@pytest.mark.parametrize('not_started', [True, False])
def test_api_fallback_preserves_native_attempt_count_and_does_not_resample_paid_research(host, monkeypatch, not_started):
    tools, control, _ = host
    monkeypatch.setattr(web_search, 'secret', lambda name: 'fixture-search-key')
    monkeypatch.setattr(research_cli, 'research', lambda *a, **k: {
        'status': 'UNAVAILABLE' if not_started else 'FAILED', 'not_started': not_started,
        'calls_used': 1, 'error': 'Fixture research could not complete.'})
    calls = []
    def request(provider, *args):
        calls.append(provider)
        return {'status': 'COMPLETED', 'results': [], 'safe_fallback': False}
    monkeypatch.setattr(web_search, 'request', request)
    result = json.loads(tools.execute(effect('research', {'question': 'docs'}), control).output_json)
    assert web_research.committed(tools.root) == 1
    assert calls == (['deepseek'] if not_started else [])
    assert result['status'] == ('PARTIAL' if not_started else 'FAILED')


def test_research_recovery_uses_completed_api_evidence_from_before_wrapper_receipt(host, monkeypatch):
    from compute_plane.native_journal import save_json
    tools, _, _ = host
    pending = effect('research', {'question': '  public\n\tdocs  '})
    monkeypatch.setattr('compute_plane.native_effect_recovery.pending', lambda *a: pending)
    monkeypatch.setattr(web_search, 'request', lambda *a: pytest.fail('Recovery must not send HTTP'))
    target = tools.root/'web-search'/web_research.key(pending.effect_id)/'receipt.json'
    target.parent.mkdir(parents=True)
    save_json(target, {'request_sha256': web_research.key({'query': 'public docs', 'refresh': False}),
        'result': {'status': 'COMPLETED', 'results': [{'url': 'https://docs.example.org'}], 'requests_used': 1}})
    result = tools.recover_effect(None)[1]
    assert result.status == 'succeeded'
    value = json.loads(result.output_json)
    assert value['status'] == 'PARTIAL' and value['discovery']['results']

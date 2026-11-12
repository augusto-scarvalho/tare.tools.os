import asyncio
import json
from pathlib import Path

import pytest
from tare_tools_agent_runtime import CancellationToken
from relay import native_agent, native_limits, research_flow as flow
from relay.console_backend import Backend, Selection, command
from test_native_agent import setup, Client, deployment
from test_research import fetch
from test_role_pin_dispatch import pin_catalog  # noqa: F401
from test_role_pins import parse
from relay.native_catalog import select as NATIVE_SELECT

NATIVE_RUN = native_agent.run_session

@pytest.fixture
def study(setup, monkeypatch):
    _, directory, root = setup
    calls, fetched = [], []
    original = native_agent.run_session
    def run(config, selected, session, instruction, **kwargs):
        p = next(p for p in flow.pages(directory) if p.get('sha256'))
        path = 'corpus/'+p['sha256']+'.md'
        client = Client(steps=[('read', {'repository': 'sources', 'path': path}),
            ('tare_deliver', {'summary': 'TTL is five minutes.', 'remaining': [],
                'sources': [{'repository': 'sources', 'path': path, 'sha256': p['sha256']}]})])
        calls.append(client)
        return original(config, selected, session, instruction, client=client, local_counter=client, **kwargs)
    def collect(url):
        fetched.append(url)
        return fetch(url)
    monkeypatch.setattr(native_agent, 'run_session', run)
    flow.create(directory, root, 'What is the TTL?', ['https://docs.example.com/guide'], deployment(), unlimited=True)
    return directory, root, calls, fetched, collect


def test_collected_sources_real_native_delivery_and_idempotent_resume(study):
    directory, root, calls, fetched, collect = study
    result = flow.execute(directory, fetch=collect)
    assert result['status'] == 'COMPLETED', result
    assert result['report']['citations'][0]['url'].endswith('/guide')
    assert result['result']['status'] == 'PROPOSED'
    ref = result['sources'][0]['ref']
    assert '5 minutes' in flow.source(directory, ref)['text']
    artifact = (directory/'analyst'/'artifact.json').read_bytes()
    assert flow.execute(directory, fetch=collect)['status'] == 'COMPLETED'
    assert len(calls) == 1 and len(fetched) == 1
    assert (directory/'analyst'/'artifact.json').read_bytes() == artifact
    state = native_agent.read_json(directory/'analyst'/'native-agent.json')
    assert state['limits']['max_stagnant_turns'] == native_limits.UNLIMITED_STAGNANT_TURNS
    assert all(v is None for k,v in state['limits'].items() if k not in ('max_model_retries', 'max_stagnant_turns'))
    assert state['config']['capabilities'] == ['read','list','search']
    assert state['config']['writable'] == {}
    assert (root/'a.py').read_bytes() == b'value = 1\n'


def test_cancel_collection_resume_keeps_snapshot_and_saved_model(study):
    directory, _, calls, fetched, collect = study
    cancel = CancellationToken()
    def cancelled(url):
        result = collect(url); cancel.cancel(); return result
    first = flow.execute(directory, fetch=cancelled, cancellation=cancel)
    assert first['status'] == 'CANCELLED' and not calls
    assert flow.execute(directory, fetch=collect)['status'] == 'COMPLETED'
    assert len(fetched) == 1 and len(calls) == 1


def test_changed_snapshot_blocks_generation_and_source_read(study):
    directory, _, calls, _, collect = study
    assert flow.execute(directory, fetch=collect)['status'] == 'COMPLETED'
    p = flow.pages(directory)[0]
    (directory/'sources'/'corpus'/(p['sha256']+'.md')).write_bytes(b'altered')
    assert flow.inspect(directory)['status'] == 'NEEDS_REVIEW'
    assert flow.execute(directory, fetch=collect)['status'] == 'NEEDS_REVIEW'
    with pytest.raises(ValueError, match='hash_mismatch'): flow.source(directory, p['ref'])
    assert len(calls) == 1


def test_failed_url_remains_visible_beside_successful_report(study):
    directory, _, _, _, collect = study
    from compute_plane.native_journal import save_json
    state=flow.read(directory);state['urls'].append('https://docs.example.com/missing')
    save_json(directory/'research.json', state)
    result=flow.execute(directory, fetch=collect)
    assert result['status']=='PARTIAL' and result['report']
    assert any(p['status']=='ERROR' for p in result['sources'])
    assert 'ERROR' in flow.render(result)


def test_lost_native_result_recovers_without_generation_or_collection(study, monkeypatch):
    directory, _, calls, fetched, collect = study
    original=native_agent.run_session
    def lost(*a, **kw):
        original(*a, **kw);raise OSError('lost response after durable delivery')
    monkeypatch.setattr(native_agent,'run_session',lost)
    assert flow.execute(directory,fetch=collect)['status']=='PAUSED'
    monkeypatch.setattr(native_agent,'run_session',original)
    assert flow.execute(directory,fetch=collect)['status']=='COMPLETED'
    assert sum(c.generations for c in calls)==2 and len(fetched)==1


def test_manifest_only_delivery_is_not_page_evidence(study, monkeypatch):
    directory, _, _, _, collect = study
    import hashlib
    def run(*a, **kw):
        digest=hashlib.sha256((directory/'sources'/'manifest.jsonl').read_bytes()).hexdigest()
        client=Client(steps=[('read',{'repository':'sources','path':'manifest.jsonl'}),
            ('tare_deliver',{'summary':'Unsupported by a page read','remaining':[],
                'sources':[{'repository':'sources','path':'manifest.jsonl','sha256':digest}]})])
        return NATIVE_RUN(*a,client=client,local_counter=client,**kw)
    monkeypatch.setattr(native_agent,'run_session',run)
    assert flow.execute(directory,fetch=collect)['status']=='NEEDS_REVIEW'


def test_observed_but_uncollected_file_is_not_an_explicit_url_citation(study, monkeypatch):
    import hashlib
    directory, root, calls, fetched, collect = study
    extra = directory/'sources'/'uncollected.md'
    extra.write_bytes(b'Outside the explicitly collected URL set.')
    def run(*args, **kwargs):
        page = next(p for p in flow.pages(directory) if p.get('sha256'))
        path = 'corpus/'+page['sha256']+'.md'
        client = Client(steps=[
            ('read', {'repository': 'sources', 'path': path}),
            ('read', {'repository': 'sources', 'path': 'uncollected.md'}),
            ('tare_deliver', {'summary': 'Mixing a collected page with an uncollected source.', 'remaining': [],
                'sources': [{'repository': 'sources', 'path': path, 'sha256': page['sha256']},
                            {'repository': 'sources', 'path': 'uncollected.md',
                             'sha256': hashlib.sha256(extra.read_bytes()).hexdigest()}]})])
        return NATIVE_RUN(*args, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert flow.execute(directory, fetch=collect)['status'] == 'NEEDS_REVIEW'


def test_saved_research_is_filtered_by_project(study, monkeypatch):
    directory,root,_,_,_=study
    monkeypatch.setattr(flow,'flow_root',lambda:directory.parent)
    assert flow.inventory(root)[0]['directory']==str(directory.resolve())
    assert flow.inventory(root.parent)==[]


@pytest.mark.parametrize('effective,expected', [('https://docs.example.com/guide','OK'),
                                               ('https://outside.example.net/guide','OFF_DOMAIN_REDIRECT')])
def test_relative_trafilatura_url_uses_verified_http_effective_url(tmp_path, monkeypatch, effective, expected):
    from unittest.mock import MagicMock
    from relay import research
    from relay.web_reader import WebReader
    html=b'<html><title>Guide</title><article><p>The cache TTL defaults to five minutes. This is the documented cache behavior.</p></article></html>'
    response=MagicMock(html=html.decode(),url='/guide')
    monkeypatch.setattr('trafilatura.fetch_response',lambda *a,**kw:response)
    monkeypatch.setattr('trafilatura.extract',lambda *a,**kw:'The cache TTL defaults to five minutes. This is the documented cache behavior.')
    monkeypatch.delenv('FIRECRAWL_API_KEY',raising=False)
    http=MagicMock();http.__enter__.return_value=http
    http.read.return_value=html;http.geturl.return_value=effective
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**kw:http)
    entries=research.collect(tmp_path,['https://docs.example.com/guide'],domains=['docs.example.com'],
        max_pages=1,max_depth=0,max_bytes=200000,fetch=WebReader.fetch_clean_markdown)
    assert entries[0]['status']==expected
    assert entries[0]['final_url']==effective


def test_cli_tui_commands_and_root_identity(study, monkeypatch, capsys):
    directory, root, _, _, _=study
    selected=Selection(str(root),mode='research',research=str(directory),prompt='q',urls='https://docs.example.com/')
    assert command(selected,True)==['research-flow','--research',str(directory),'--resume','--format','json']
    monkeypatch.setattr(flow,'flow_root',lambda:directory.parent/'flows')
    assert '--unlimited' in command(selected)
    selected.research=str(directory);selected.root=str(root.parent)
    with pytest.raises(ValueError,match='another project'): Backend().research_view(selected)
    assert flow.main(['--research',str(directory),'--resume','--deployment','other'])==2
    capsys.readouterr()


def test_tui_can_reopen_and_read_snapshot_without_calling_model(study):
    from relay.native_console import WorkConsole
    from textual.selection import Selection as TextSelection
    from textual.widgets import Select, Input, TextArea
    directory,root,calls,_,collect=study
    flow.execute(directory,fetch=collect)
    class TestBackend(Backend):
        def catalog(self,root):
            return {'models':[{'deployment':'local-qwen','model':'fixture'}],'sessions':[],'works':[],'errors':[]}
    async def check():
        app=WorkConsole(root,backend=TestBackend())
        async with app.run_test(size=(120,70)) as pilot:
            app.query_one('#mode',Select).value='research'
            app.query_one('#research',Input).value=str(directory)
            await pilot.pause()
            await app.review_selection().wait()
            app.query_one('#research-ref',Select).value=flow.pages(directory)[0]['ref']
            await app.read_research_source().wait()
            await pilot.pause()
            shown = '\n'.join(widget.get_selection(TextSelection(None, None))[0]
                              for widget in app.query('#output .message'))
            assert 'Review unavailable' not in shown
            assert '5 minutes' in shown and 'SHA256' in shown
            assert app.query_one('#research-options').display
            assert not app.query_one('#prompt',TextArea).disabled
    asyncio.run(check())
    assert len(calls)==1


@pytest.mark.parametrize('origin', [None, 'CONFIGURED', 'AUTOMATIC', 'EXACT_TEMPORARY_PIN'])
def test_snapshot_dispatch_uses_saved_provenance_and_shared_helper(study, monkeypatch, origin):
    from compute_plane.native_journal import save_json
    directory, _, _, _, collect = study
    state = flow.read(directory)
    if origin == 'CONFIGURED':
        state['origin'] = origin
    elif origin == 'AUTOMATIC':
        from test_delegate_dispatch_trace import native_selection_record
        state['deployment']['native_selection'] = native_selection_record(state['deployment']['id'])
    elif origin == 'EXACT_TEMPORARY_PIN':
        state['deployment']['native_selection'] = {'reason': origin, 'candidates': [
            {'eligible': True, 'selected_deployment': state['deployment']['id']}]}
    selected = json.loads(json.dumps(state['deployment']))
    save_json(directory/'research.json', state)
    original = native_agent.run_session
    def run(config, deployment, session, instruction, **kwargs):
        saved = flow.read(directory)
        attempt = saved['attempts'][-1]
        assert attempt['executed_decision']['mode'] == (origin or 'UNKNOWN')
        assert attempt['executed_decision']['fallback'] is None
        assert attempt['dispatch']['status'] == 'DISPATCHING'
        assert deployment == selected
        return original(config, deployment, session, instruction, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert flow.execute(directory, fetch=collect)['status'] == 'COMPLETED'


def test_snapshot_resume_appends_without_rewriting_interrupted_dispatch(study, monkeypatch):
    directory, _, _, fetched, collect = study
    original = native_agent.run_session
    def interrupted(*a, **kw):
        original(*a, **kw)
        raise OSError('lost response')
    monkeypatch.setattr(native_agent, 'run_session', interrupted)
    assert flow.execute(directory, fetch=collect)['status'] == 'PAUSED'
    first = flow.read(directory)['attempts'][0]
    assert first['dispatch']['status'] == 'DISPATCHING'
    def resumed(*a, **kw):
        saved = flow.read(directory)
        assert saved['attempts'][0] == first
        assert len(saved['attempts']) == 2
        assert saved['attempts'][1]['executed_decision']['mode'] == 'RESUME_SAVED_ROUTE'
        assert saved['attempts'][1]['dispatch']['status'] == 'DISPATCHING' and kw['resume']
        return original(*a, **kw)
    monkeypatch.setattr(native_agent, 'run_session', resumed)
    assert flow.execute(directory, fetch=collect)['status'] == 'COMPLETED'
    assert flow.read(directory)['attempts'][0] == first and len(fetched) == 1


def test_snapshot_cancelled_before_analysis_has_no_dispatch(study):
    directory, _, calls, _, collect = study
    token = CancellationToken(); token.cancel()
    assert flow.execute(directory, fetch=collect, cancellation=token)['status'] == 'CANCELLED'
    attempt = flow.read(directory)['attempts'][-1]
    assert 'executed_decision' not in attempt and 'dispatch' not in attempt and not calls


def test_snapshot_real_pinned_selection_survives_scope_and_reaches_dispatch(study, pin_catalog, monkeypatch):
    from compute_plane.native_journal import save_json
    from relay import execution_scope as scope, native_routes
    directory, _, _, _, collect = study
    with scope.activated(parse(['--explorer', 'anthropic/test-cli@max'])):
        with scope.for_role('explorer'):
            selected = NATIVE_SELECT('auto', role='explorer')
    selection = selected['native_selection']
    assert selection['reason'] == 'EXACT_TEMPORARY_PIN'
    assert selection['candidates'] == [{'eligible': True, 'catalog': 'test-cli',
                                        'selected_deployment': selected['id']}]
    assert native_routes.selected_decision(selected)['mode'] == 'EXACT_TEMPORARY_PIN'
    state = flow.read(directory)
    state['deployment'] = selected
    save_json(directory/'research.json', state)
    dispatched = []
    def run(config, deployment, session, instruction, **kwargs):
        saved = flow.read(directory)
        assert deployment == selected == saved['deployment']
        assert saved['attempts'][-1]['executed_decision'] == native_routes.selected_decision(selected)
        assert saved['attempts'][-1]['dispatch']['status'] == 'DISPATCHING'
        assert not kwargs['resume']
        dispatched.append(deployment)
        return {'status': 'FAILED', 'model_calls': 0}
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert flow.execute(directory, fetch=collect)['status'] == 'PAUSED'
    saved = flow.read(directory)
    assert dispatched == [selected]
    assert saved['attempts'][-1]['executed_decision']['mode'] == 'EXACT_TEMPORARY_PIN'
    assert saved['attempts'][-1]['dispatch']['status'] == 'FAILED'

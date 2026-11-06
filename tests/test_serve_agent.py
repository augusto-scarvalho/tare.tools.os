import json
import sys
from pathlib import Path

import pytest

from relay import native_agent, serve_agent, turn_engine


def copilot(root, *requests):
    messages = [{'role': 'system', 'content': 'You are Copilot.'},
                {'role': 'user', 'content': '<workspace_info>\nI am working in a workspace with the following '
                                            f'folders:\n- {root} \n</workspace_info>'}]
    for text in requests:
        messages.append({'role': 'user', 'content': f'<context>\nx\n</context>\n<userRequest>\n{text}\n</userRequest>'})
    return messages


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)  # no live Jev shadow calls from tests
    monkeypatch.setattr(turn_engine, 'local_judge', lambda: None)  # nor the node's gateway
    monkeypatch.setattr(native_agent, 'session_root', lambda: tmp_path/'sessions')
    project = tmp_path/'project'
    project.mkdir()
    state = {'down': set(), 'outcomes': [], 'calls': [], 'real_run_session': native_agent.run_session}
    tiers = [['local-a', 'local-b'], ['cheap'], ['middle']]
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': tiers, 'planner': ['astra', 'opus'],
                                                     'planner_effort': 'high'})
    monkeypatch.setattr(turn_engine, 'with_effort', lambda d, effort: {**d, 'effort': effort})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'busy') if name in state['down'] else (
        {'id': name, 'native_profile': {'protocol': 'openai-chat'}}, None))

    def run_session(config, deployment, custody, instruction=None, **kw):
        state['calls'].append({'config': config, 'route': deployment and deployment['id'], 'custody': Path(custody),
                               'instruction': instruction, **kw})
        status = state['outcomes'].pop(0) if state['outcomes'] else 'PROPOSED'
        kw.get('on_activity', lambda e: None)({'activity': {'kind': 'tool-activity', 'operation': 'read',
                                                            'target': 'a.py', 'state': 'succeeded'}})
        return {'status': status, 'reason': 'X' if status != 'PROPOSED' else 'DELIVERY_PROPOSED',
                'output': f'answer from {deployment["id"] if deployment else "resumed"}'}
    monkeypatch.setattr(native_agent, 'run_session', run_session)
    saved = []
    monkeypatch.setattr('relay.native_continuity.preview', lambda source, project: {'text': 'BRIEF', 'project': project})
    monkeypatch.setattr('relay.native_continuity.save_request',
                        lambda target, brief, text, deployment: saved.append((Path(target), text, deployment['id'])))
    return project, state, saved


def test_workspace_and_requests_come_from_copilots_context(tmp_path):
    messages = copilot(tmp_path, 'first', 'second')
    assert serve_agent.workspace_root(messages) == tmp_path.resolve()
    assert serve_agent.user_requests(messages) == ['first', 'second']
    assert serve_agent.workspace_root([{'role': 'user', 'content': 'hi'}]) is None


def test_a_chat_starts_on_the_first_available_tier_and_follow_ups_resume_it(runtime):
    project, state, _ = runtime
    state['down'].add('local-a')
    out = []
    answer = serve_agent.run_turn(copilot(project, 'explain main'), out.append)
    first = state['calls'][0]
    assert first['route'] == 'local-b' and first['instruction'] == 'explain main' and first['conversation'] is True
    assert first['config']['repositories'] == {'workspace': str(project.resolve())}
    assert answer == 'answer from local-b' and any('`read` a.py' in line for line in out)
    serve_agent.run_turn(copilot(project, 'explain main', 'now add a docstring'), out.append)
    second = state['calls'][1]
    assert second['resume'] is True and second['follow_up'] == 'now add a docstring'
    assert second['custody'] == first['custody']


def test_a_blocked_turn_continues_on_the_next_tier_with_the_brief(runtime):
    project, state, saved = runtime
    state['outcomes'] = ['BLOCKED']
    out = []
    answer = serve_agent.run_turn(copilot(project, 'hard refactor'), out.append)
    assert [c['route'] for c in state['calls']] == ['local-a', None]  # successor runs from continuation.json
    target, text, route = saved[0]
    assert route == 'cheap' and text.startswith('BRIEF') and 'Continue the task' in text
    assert state['calls'][1]['custody'] == target and answer == 'answer from resumed'
    store = json.loads(next((Path(serve_agent.chat_store('x')).parent).glob('*.json')).read_text())
    assert [s['route'] for s in store['sessions']] == ['local-a', 'cheap']


def test_a_route_that_went_away_moves_the_next_message_to_an_available_one(runtime):
    project, state, saved = runtime
    serve_agent.run_turn(copilot(project, 'task'), lambda text: None)
    state['down'].add('local-a')
    serve_agent.run_turn(copilot(project, 'task', 'follow'), lambda text: None)
    assert saved and saved[0][2] == 'local-b' and 'New user instruction:\nfollow' in saved[0][1]


def test_a_chat_past_its_session_budget_continues_in_a_successor_on_the_same_tier(runtime, monkeypatch):
    project, state, saved = runtime
    serve_agent.run_turn(copilot(project, 'task'), lambda text: None)
    fake = native_agent.run_session

    def exhausted(config, deployment, custody, instruction=None, **kw):
        if kw.get('follow_up'):
            raise ValueError('native continuation execution budget exhausted')
        return fake(config, deployment, custody, instruction, **kw)
    monkeypatch.setattr(native_agent, 'run_session', exhausted)
    answer = serve_agent.run_turn(copilot(project, 'task', 'one more thing'), lambda text: None)
    fresh = state['calls'][-1]  # a new session with fresh limits, not a continuation that inherits the spent ones
    assert not saved and fresh['route'] == 'local-a' and fresh['conversation'] is True and fresh['limits']
    assert fresh['instruction'].startswith('BRIEF') and fresh['instruction'].endswith('New user instruction:\none more thing')
    assert answer == 'answer from local-a'


def test_no_open_folder_asks_for_one(runtime):
    answer = serve_agent.run_turn([{'role': 'user', 'content': '<userRequest>hi</userRequest>'}], lambda t: None)
    assert 'open a project folder' in answer


def test_plan_and_explore_run_read_only_flows_without_touching_the_chat_session(runtime):
    project, state, _ = runtime
    out = []
    answer = serve_agent.run_turn(copilot(project, '/plan migrate the storage layer'), out.append)
    plan = state['calls'][0]
    assert plan['route'] == 'astra' and plan['config']['role'] == 'planner' and plan['instruction'] == 'migrate the storage layer'
    assert plan['config']['writable'] == {} and 'edit' not in plan['config']['capabilities']
    assert answer == 'answer from astra' and any('/plan' in line and 'astra' in line for line in out)
    serve_agent.run_turn(copilot(project, '/plan migrate the storage layer', '/explore where is the cache?'), out.append)
    assert state['calls'][1]['config']['role'] == 'explorer' and state['calls'][1]['route'] == 'local-a'
    serve_agent.run_turn(copilot(project, '/plan migrate the storage layer', '/explore where is the cache?', 'implement it'),
                         out.append)
    instruction = state['calls'][2]['instruction']
    assert state['calls'][2]['conversation'] is True and instruction.endswith('User request:\nimplement it')
    assert 'answer from astra' in instruction and 'answer from local-a' in instruction  # the flows' results


def test_the_auditor_is_independent_and_another_vendor_goes_first(monkeypatch):
    from types import SimpleNamespace
    ids = {'qwen': ('qwen-3.8', 'Qwen', 'qwen/qwen-family'), 'sol': ('gpt-6.1-sol', 'OpenAI', 'openai/gpt'),
           'opus': ('claude-opus-5.5', 'Anthropic', 'anthropic/opus'), 'sonnet': ('claude-sonnet-5.5', 'Anthropic', 'anthropic/sonnet')}
    monkeypatch.setattr('relay.work_audit.identity', lambda d, role: SimpleNamespace(
        **dict(zip(('model_id', 'vendor', 'bias_domain'), ids[d['id']]))))
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'id': name}, None))
    names = lambda implementer, routes: [name for name, _ in turn_engine.pick_auditors({'id': implementer}, routes)]
    assert names('qwen', ['sol', 'opus']) == ['sol', 'opus']
    assert names('sonnet', ['opus', 'sol']) == ['sol', 'opus']  # cross vendor preferred
    assert names('sonnet', ['opus']) == ['opus']  # independent, same vendor allowed
    assert names('sol', ['sol']) == []  # never its own auditor


def test_only_a_turn_that_changed_code_is_audited_and_revise_gets_one_fix(runtime, monkeypatch):
    project, state, _ = runtime
    changes = []
    monkeypatch.setattr(turn_engine, 'turn_changes', lambda runs, root: list(changes))
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda implementer, names: [('sol', {'id': 'sol', 'native_profile': {}})])
    fake = native_agent.run_session
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: {**fake(*a, **kw), 'artifact_file': 'x'})
    monkeypatch.setattr('relay.native_artifacts.read_input', lambda path: {'artifact': {'payload': {
        'verdict': 'REVISE', 'summary': 'off by one', 'findings': [{'claim': 'range skips the last item'}], 'remaining': ['could cache pages']}}})
    serve_agent.run_turn(copilot(project, 'explain main'), lambda text: None)
    assert len(state['calls']) == 1  # nothing changed: no audit
    changes.append({'path': 'todo.py', 'diff': '--- before/todo.py\n+++ after/todo.py\n'})
    out = []
    serve_agent.run_turn(copilot(project, 'explain main', 'add paging'), out.append)
    audit, fix = state['calls'][2], state['calls'][3]
    assert audit['config']['role'] == 'auditor' and audit['config']['writable'] == {} and audit['route'] == 'sol'
    assert 'edit' not in audit['config']['capabilities'] and audit['config']['typed_output'] is True
    review = Path(audit['config']['repositories']['review'])
    assert (review/'diff.patch').read_text().startswith('--- before/todo.py')
    asked = (review/'REQUEST.md').read_text()  # the turn's message first, the chat's earlier ones as context
    assert asked.startswith('add paging\n\nEarlier messages') and asked.endswith('explain main')
    assert fix['resume'] is True and fix['custody'] == state['calls'][0]['custody']
    assert 'range skips the last item' in fix['follow_up'] and 'smallest change' in fix['follow_up']
    assert 'could cache pages' not in fix['follow_up'] and any('residual' in line and 'cache' in line for line in out)
    assert 'Check only whether each finding was resolved' in state['calls'][4]['instruction']
    # The fix is audited too, and its REVISE gets no second automatic fix.
    assert len(state['calls']) == 5 and state['calls'][4]['config']['role'] == 'auditor'
    assert any('REVISE' in line for line in out)


def test_judges_may_only_demote_a_finding_at_high_confidence_and_fail_open(runtime, monkeypatch):
    def answer(p_residual, model='qwen38-gsq'):
        return {'choice': 'residual' if p_residual >= .5 else 'material', 'model': model,
                'probabilities': {'residual': p_residual, 'material': 1 - p_residual}}
    odds = {'cache the pages': .95, 'range skips the last item': .1, 'rename a variable': .8}
    asked = []

    def judges(request):
        asked.append(request)
        claim = json.loads(request['state'])['audit_finding']
        return {'local': answer(odds[claim]), 'jev': answer(.99, 'jev-1.13.0')}  # the loaded local judge decides
    monkeypatch.setattr(turn_engine, 'judges', judges)
    record, out = {'route': 'sol', 'changed_lines': 12, 'findings': list(odds)}, []
    assert turn_engine.triage(record, 'add paging', '+x\n', out.append) == ['range skips the last item', 'rename a variable']
    assert len(out) == 1 and 'cache the pages' in out[0] and '0.95' in out[0]
    assert all(r['question'] == turn_engine.MATERIALITY_QUESTION and '+x' in r['state'] for r in asked)
    log = (Path(serve_agent.chat_store('x')).parents[1]/'audit-judge.jsonl').read_text().splitlines()
    assert json.loads(log[-1])['kind'] == 'materiality' and [i['demoted'] for i in json.loads(log[-1])['items']] == [True, False, False]
    monkeypatch.setattr(turn_engine, 'judges', lambda request: {})  # no judge available: every finding stands
    assert turn_engine.triage(record, 'add paging', '', out.append) == list(odds)


def test_the_fix_review_is_measured_for_novelty_and_proportion_without_acting(runtime, monkeypatch):
    def judges(request):
        choice = 'new' if request['options'] is turn_engine.NOVELTY else 'excessive'
        return {'jev': {'choice': choice, 'model': 'jev-1.13.0', 'probabilities': {choice: .9, 'x': .1}}}
    monkeypatch.setattr(turn_engine, 'judges', judges)
    record, out = {'route': 'sol', 'changed_lines': 80, 'findings': ['a'], 'remaining': ['b']}, []
    turn_engine.judge_fix(record, 'fix request', '+x\n', out.append)
    assert len(record['judges']['novelty']) == 2 and 'excessive' in out[0] and '2 of 2' in out[0]


def test_audit_triage_preserves_the_whole_finding_after_a_long_request(runtime, monkeypatch):
    goal = 'Preserve every original delivery obligation. ' * 100
    claim = 'The implementation drops the final required row.'
    patch = '+ implementation\n'
    asked = []

    def judges(packet):
        asked.append(packet)
        assert claim in packet['state']
        subject = json.loads(packet['state'])
        assert subject == {'user_request': goal, 'audit_finding': claim, 'change': patch}
        return {'jev': {'choice': 'material', 'model': 'jev-1.13.0',
                        'probabilities': {'material': .99, 'residual': .01}}}

    monkeypatch.setattr(turn_engine, 'judges', judges)
    record = {'route': 'sol', 'changed_lines': 1, 'findings': [claim]}
    assert turn_engine.triage(record, goal, patch, lambda text: None) == [claim]
    assert len(asked) == 1


@pytest.mark.parametrize('oversized_part', ['user_request', 'audit_finding', 'change'])
def test_incomplete_audit_context_cannot_pay_for_or_demote_a_finding(runtime, monkeypatch, oversized_part):
    subject = {'user_request': 'Fix the export.', 'audit_finding': 'The final row is missing.',
               'change': '+ implementation\n'}
    subject[oversized_part] = 'obrigação "não omitir"\n' * 1000
    monkeypatch.setattr(turn_engine, 'judges', lambda packet: pytest.fail('Incomplete context reached a judge'))
    record = {'route': 'sol', 'changed_lines': 1, 'findings': [subject['audit_finding']]}
    emitted = []
    assert turn_engine.triage(record, subject['user_request'], subject['change'], emitted.append) == record['findings']
    assert not emitted
    assert record['triage'][0]['demoted'] is False
    assert record['triage'][0]['input']['failure'] == 'ASSESSMENT_INPUT_OVERFLOW'


def test_a_judge_request_is_complete_and_overflow_is_explicit():
    from compute_plane.choice_assessment import validate_request
    head,patch='é"\n'*3000,'"\\\n'*9000
    request = turn_engine.judge_request('Q?', turn_engine.MATERIALITY, head, patch)
    assert request['state']==head+'\n\nChange (unified diff):\n'+patch
    with pytest.raises(ValueError,match='ASSESSMENT_INPUT_OVERFLOW'):validate_request(request)


def test_an_audit_request_leads_with_the_latest_message_and_keeps_earlier_ones_as_context():
    request = turn_engine.conversation_request(['Fix the export bug', 'Use the Windows path rule', '  ', 'sim'])
    latest, context = request.split('\n\n', 1)
    assert latest == 'sim' and context.index('Use the Windows') < context.index('Fix the export')
    assert turn_engine.conversation_request(['only one']) == 'only one'
    assert turn_engine.conversation_request([]) == ''
    long = turn_engine.conversation_request(['x' * 9000, 'ok'])  # earlier context is clipped, never the latest
    assert long.startswith('ok\n\n') and len(long.encode()) < 3300


def test_the_default_policy_names_single_routes_per_tier():
    rules = turn_engine.DEFAULT_POLICY
    assert all(isinstance(name, str) for tier in rules['implementer'] for name in tier)
    assert all(isinstance(name, str) for name in rules['planner']) and rules['planner_effort'] == 'high'


def test_a_route_whose_executable_cannot_be_checked_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr('relay.native_catalog.select', lambda name: {
        'id': name, 'native_cli': {'executable': 'C:/junction/codex.exe'},
        'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://codex'}})
    monkeypatch.setattr('relay.native_catalog.resolve_host_mode', lambda d: d)

    def refuse(self):
        raise OSError(448, 'The path cannot be traversed because it contains an untrusted mount point')
    monkeypatch.setattr(turn_engine.Path, 'is_file', refuse)
    deployment, reason = turn_engine.available('codex-cli')
    assert deployment is None and reason.startswith('CLI not reachable')


def test_images_are_described_by_the_first_vision_cli_that_answers(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['local-a']],
                                                     'vision': ['luna', 'sonnet', 'gemini']})
    tried = []

    def see(deployment, prompt, paths, timeout=240):
        tried.append((deployment['id'], [Path(p).read_bytes() for p in paths]))
        if deployment['id'] == 'luna':
            raise RuntimeError('quota exhausted')  # Codex out of quota: the next vendor describes it
        return 'A red square and the text ERROR 404.'
    monkeypatch.setattr(turn_engine, 'see', see)
    messages = copilot(project, 'why does this fail?')
    messages[-1]['content'] = [{'type': 'text', 'text': messages[-1]['content']},
                               {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,UE5H'}}]
    out = []
    serve_agent.run_turn(messages, out.append)
    assert tried == [('luna', [b'PNG']), ('sonnet', [b'PNG'])]
    instruction = state['calls'][0]['instruction']
    assert instruction.startswith('why does this fail?') and 'ERROR 404' in instruction and 'described by sonnet' in instruction
    assert any('`image`' in line for line in out) and any('sonnet' in line for line in out)


def test_linked_pages_are_read_and_never_crowd_out_the_request(runtime, monkeypatch):
    from relay.web_reader import WebReader
    project, state, _ = runtime
    page = type('Doc', (), {'status': 'OK', 'markdown': 'x' * 50000})
    monkeypatch.setattr(WebReader, 'fetch_clean_markdown', classmethod(lambda cls, url: page))
    request = 'summarize https://example.com/guide. ' + 'y' * 9000
    serve_agent.run_turn(copilot(project, request), lambda text: None)
    instruction = state['calls'][0]['instruction']
    assert instruction.startswith(request) and 'Content of https://example.com/guide ' in instruction
    assert len(instruction.encode()) <= serve_agent.INSTRUCTION_BYTES


def test_free_search_results_are_read_locally_and_cli_search_is_only_the_fallback(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'search_links', lambda question: [
        {'href': 'https://a.example/1', 'title': 'A', 'body': 'a'}, {'href': 'https://b.example/2', 'title': 'B', 'body': 'b'}])
    reads = []
    def read(question, urls, emit, **kw):
        reads.append((urls, kw))
        return ({'flow': 'research', 'mode': kw['mode']}, 'local report') if len(reads) == 1 else (None, 'tare: failed')
    monkeypatch.setattr(turn_engine, 'read_links', read)
    monkeypatch.setattr(turn_engine, 'ask_cli', lambda prompt, **kw: ('cli report', 'luna'))
    assert turn_engine.run_research('latest trafilatura', lambda t: None) == (
        {'flow': 'research', 'mode': 'free-search'}, 'local report')
    urls, options = reads[0]
    assert urls == ['https://a.example/1', 'https://b.example/2'] and callable(options.pop('fetch'))
    assert options == {'mode': 'free-search', 'max_pages': 2, 'max_depth': 0}
    assert turn_engine.run_research('latest trafilatura', lambda t: None)[1] == 'cli report'


def test_search_snippets_lead_the_page_and_survive_a_failed_fetch(monkeypatch):
    from relay.web_reader import WebDocument, WebReader
    def fetch(cls, url):
        ok = url.endswith('/ok')
        return WebDocument(url, 't', 'page text' if ok else '', 1, 1, 0.0, 'm', 'OK' if ok else 'FAILED')
    monkeypatch.setattr(WebReader, 'fetch_clean_markdown', classmethod(fetch))
    read = turn_engine.with_snippets([{'href': 'https://PyPI.org/ok', 'title': 'pkg 2.2.0', 'body': 'Released'},
                                      {'href': 'https://x.org/down', 'title': 'Down', 'body': 'only snippet'}])
    assert read('https://pypi.org/ok').markdown == 'Search result: pkg 2.2.0\nReleased\n\npage text'
    down = read('https://x.org/down')
    assert down.status == 'OK' and down.markdown.startswith('Search result: Down\nonly snippet')
    assert read('https://other.org/ok').markdown == 'page text'


def test_research_without_links_searches_and_the_chat_session_receives_the_result(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'search_links', lambda question: [])  # free search blocked
    monkeypatch.setattr(turn_engine, 'ask_cli', lambda prompt, **kw: (
        f"search={kw.get('search')} {prompt.rsplit('Question: ', 1)[1]}\nSources: https://a.example", 'luna'))
    answer = serve_agent.run_turn(copilot(project, '/research latest trafilatura'), lambda text: None)
    assert answer.startswith('search=True latest trafilatura') and not state['calls']
    serve_agent.run_turn(copilot(project, '/research latest trafilatura', 'upgrade it'), lambda text: None)
    instruction = state['calls'][0]['instruction']
    assert instruction.startswith('Earlier in this chat:\n/research latest trafilatura') and instruction.endswith('upgrade it')
    serve_agent.run_turn(copilot(project, '/research latest trafilatura', 'upgrade it', 'thanks'), lambda text: None)
    assert state['calls'][1]['follow_up'] == 'thanks'  # delivered once


def test_research_with_links_reads_them_locally_and_cites_urls(runtime, monkeypatch, tmp_path):
    project, state, _ = runtime
    import relay.research as research
    def fake(jobs, question, seeds, **kw):
        job = Path(jobs)/('research-' + '0' * 32)
        job.mkdir(parents=True)
        (job/'manifest.jsonl').write_text(json.dumps({'ref': 'W-1', 'url': seeds[0]}) + '\n', encoding='utf-8')
        return {'job_id': job.name, 'status': 'COMPLETED', 'pages': 1, 'envelope': {
            'summary': 'Short answer.', 'evidence': [{'ref': 'W-1', 'claim': 'A fact'}], 'remaining': []}}
    monkeypatch.setattr(research, 'research', fake)
    answer = serve_agent.run_turn(copilot(project, '/research what changed? https://docs.example/x'), lambda t: None)
    assert answer == 'Short answer.\n\nEvidence:\n- A fact (https://docs.example/x)'
    assert not any((Path(serve_agent.chat_store('x')).parents[1]/'research').glob('research-*'))


def test_the_helper_cli_is_read_only_and_gets_search_and_images_as_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_engine, 'available', lambda name: (
        {'native_cli': {'executable': 'codex.exe'}, 'native_profile': {'model': 'gpt-6-luna'}}, None))
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw['input']))
        Path(argv[argv.index('-o') + 1]).write_text(' answer \n', encoding='utf-8')
    monkeypatch.setattr(turn_engine.subprocess, 'run', run)
    assert turn_engine.ask_cli('q', images=['a.png'], search=True) == ('answer', 'codex-cli-gpt-6-luna')
    argv, prompt = calls[0]
    assert argv[:3] == ['codex.exe', '--search', 'exec'] and prompt == b'q'
    assert argv.index('-') < argv.index('-i')  # -i takes many values: a trailing '-' was read as an image
    assert argv[argv.index('-s') + 1] == 'read-only' and argv[argv.index('-i') + 1] == 'a.png'
    # The user's ~/.codex/config.toml (effort max, features) stays out; the tier is the default one.
    assert '--ignore-user-config' in argv and 'service_tier="default"' in argv and 'project_doc_max_bytes=0' in argv
    assert not any(a.startswith('model_reasoning_effort') for a in argv)  # this route names no effort



def test_the_implementer_verifies_with_the_projects_own_tests(tmp_path, monkeypatch):
    pytest_argv = [sys.executable, '-m', 'pytest', '-q']
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: 'npm.cmd' if name == 'npm' else None)

    def verifier(**files):
        root = tmp_path/str(len(list(tmp_path.iterdir())))
        root.mkdir()
        for name, text in files.items():
            (root/name).parent.mkdir(parents=True, exist_ok=True)
            (root/name).write_text(text, encoding='utf-8')
        config = turn_engine.config_for(root)
        assert ('verify' in config['capabilities']) == bool(config['verifiers'])
        return config['verifiers'].get('tests', {}).get('argv')
    assert verifier(**{'tests/test_a.py': ''}) == pytest_argv
    assert verifier(**{'pytest.ini': '[pytest]'}) == verifier(**{'setup.cfg': '[tool:pytest]'}) == pytest_argv
    assert verifier(**{'pyproject.toml': '[tool.pytest.ini_options]'}) == pytest_argv
    # A JavaScript project with a tests/ folder is not a Python one.
    assert verifier(**{'package.json': '{"scripts": {"test": "jest"}}', 'tests/a.test.js': ''}) == ['npm.cmd', 'test']
    assert verifier(**{'package.json': '{"scripts": {"lint": "eslint"}}'}) is None
    assert verifier(**{'README.md': ''}) is None
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: None)
    assert verifier(**{'package.json': '{"scripts": {"test": "jest"}}'}) is None


def test_the_tests_run_with_the_projects_python_not_tares_own(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: r'C:\Python312\python.exe')
    assert turn_engine.project_python(tmp_path) == r'C:\Python312\python.exe'  # an installed release has no pytest
    venv_python = tmp_path/'.venv'/'Scripts'/'python.exe'
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b'')
    assert turn_engine.project_python(tmp_path) == str(venv_python)


def test_a_large_suite_gets_no_automatic_verifier(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_engine, 'VERIFY_MAX_TEST_FILES', 2)
    (tmp_path/'tests').mkdir()
    for n in range(3):
        (tmp_path/'tests'/f'test_{n}.py').write_text('', encoding='utf-8')
    assert turn_engine.config_for(tmp_path)['verifiers'] == {}


def test_terminal_commands_the_host_recorded_reach_the_auditor_beside_the_checks():
    text = turn_engine.verification_of([
        {'name': 'tests', 'returncode': 0, 'freshness': 'OBSERVED_FILES_MATCH', 'output': ''},
        {'argv': ['git', 'log', '-3'], 'status': 'exited', 'exit_code': 0},
        {'argv': ['make'], 'status': 'running', 'exit_code': None}])
    assert '## tests' in text and 'status: PASS' in text and '## git' not in text
    assert '- git log -3 -> exited (exit 0)' in text and '- make -> running' in text


def test_the_auditor_sees_host_recorded_checks_and_a_failed_auditor_hands_over_once(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda implementer, names: [
        (name, {'id': name, 'native_profile': {}}) for name in ('sol', 'opus', 'third')])
    state['outcomes'] = ['RECONCILIATION_REQUIRED', 'RECONCILIATION_REQUIRED']
    checks = [{'name': 'tests', 'returncode': 0, 'freshness': 'OBSERVED_FILES_MATCH', 'output': '45 passed'}]
    out = []
    record = turn_engine.run_audit('add stats', [{'path': 'a.py', 'diff': '+x\n'}], project, {}, out.append, checks=checks)
    audits = [c for c in state['calls'] if c['config']['role'] == 'auditor']
    assert [c['route'] for c in audits] == ['sol', 'opus'] and 'verdict' not in record  # two tries, then unaudited
    assert 'OBSERVED_FILES_MATCH' in audits[0]['instruction'] and any('unaudited' in line for line in out)
    verification = (Path(audits[0]['config']['repositories']['review'])/'VERIFICATION.md').read_text(encoding='utf-8')
    assert '## tests' in verification and 'PASS' in verification and '45 passed' in verification


def test_chat_modes_set_permissions_and_a_mode_change_starts_a_fresh_session(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'terminal_for', lambda session, config: 'TERMINAL' if config.get('terminal') else None)
    monkeypatch.setattr(turn_engine, 'session_terminal', lambda session: None)
    monkeypatch.setattr(turn_engine, 'snapshot', lambda root: (None, 'test'))
    monkeypatch.setattr('relay.native_continuity.preview', lambda source, project: {'text': 'BRIEF'})
    answer = serve_agent.run_turn(copilot(project, '/read-only default'), lambda text: None)
    assert 'read-only mode' in answer and 'new chats' in answer and not state['calls']
    serve_agent.run_turn(copilot(project, 'look around'), lambda text: None)  # a new chat: the project default
    config = state['calls'][-1]['config']
    assert 'edit' not in config['capabilities'] and config['writable'] == {} and not config.get('terminal')
    out = []
    serve_agent.run_turn(copilot(project, 'look around', '/yolo run the build'), out.append)
    yolo = state['calls'][-1]
    assert yolo['config']['terminal'] is True and 'terminal_start' in yolo['config']['capabilities']
    assert yolo['terminal_client'] == 'TERMINAL' and yolo['custody'] != state['calls'][0]['custody']
    assert yolo['instruction'].startswith('BRIEF') and yolo['instruction'].endswith('run the build')
    assert any('yolo mode' in line for line in out) and any('cannot be audited' in line for line in out)
    serve_agent.run_turn(copilot(project, 'look around', '/yolo run the build', 'again'), lambda text: None)
    assert state['calls'][-1]['resume'] is True and state['calls'][-1]['custody'] == yolo['custody']
    assert serve_agent.project_mode(project) == 'read-only'


def test_a_yolo_turn_is_audited_on_the_project_snapshot_diff(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'terminal_for', lambda session, config: None)
    trees = iter(['T0', 'T1', 'T1'])  # start, after the turn, and the turn record
    monkeypatch.setattr(turn_engine, 'snapshot', lambda root: (next(trees), None))
    monkeypatch.setattr(turn_engine, 'tree_changes', lambda root, a, b: [{'path': 'built.txt', 'diff': f'{a}->{b}'}])
    audited = []
    monkeypatch.setattr(turn_engine, 'audit_turn', lambda files, *a, **kw: audited.append(files) or (None, None))
    serve_agent.run_turn(copilot(project, '/yolo make the build'), lambda text: None)
    assert audited == [[{'path': 'built.txt', 'diff': 'T0->T1'}]]


def test_the_shadow_snapshot_sees_shell_changes_and_never_touches_the_project(runtime, monkeypatch):
    project, _, _ = runtime
    (project/'.gitignore').write_text('*.log\n', encoding='utf-8')
    (project/'a.txt').write_text('a\n', encoding='utf-8')
    before, why = turn_engine.snapshot(project)
    assert before and why is None
    (project/'a.txt').write_text('a\nb\n', encoding='utf-8')
    (project/'new.txt').write_text('new\n', encoding='utf-8')
    (project/'x.log').write_text('ignored\n', encoding='utf-8')
    monkeypatch.setattr(turn_engine, 'SNAPSHOT_FILE_BYTES', 10)
    (project/'big.bin').write_bytes(b'0' * 64)  # left out, not a reason to fail
    after, _ = turn_engine.snapshot(project)
    changed = {f['path']: f['diff'] for f in turn_engine.tree_changes(project, before, after)}
    assert set(changed) == {'a.txt', 'new.txt'} and '+b' in changed['a.txt']
    assert not (project/'.git').exists()
    monkeypatch.setattr(turn_engine, 'SNAPSHOT_NEW_BYTES', 1)
    (project/'more.txt').write_text('mm', encoding='utf-8')  # small (under the large-file bound), but new
    assert turn_engine.snapshot(project) == (None, 'more than 0 MB of new files')


def scripted_effects(monkeypatch, project, effects):
    """Each implementer call (new or resumed) applies the next scripted file change, as tare's edits would."""
    fake = native_agent.run_session

    def run(config, deployment, custody, instruction=None, **kw):
        if (config or {}).get('role', 'implementer') == 'implementer' and effects:
            for name, text in effects.pop(0).items():
                (project/name).write_text(text, encoding='utf-8')
        return fake(config, deployment, custody, instruction, **kw)
    monkeypatch.setattr(native_agent, 'run_session', run)


def test_undo_restores_only_what_tare_changed_and_keeps_the_users_own_edits(runtime, monkeypatch):
    project, state, _ = runtime
    (project/'b.txt').write_text('b0\n', encoding='utf-8')
    scripted_effects(monkeypatch, project, [{'a.txt': 'a1\n', 'b.txt': 'b1\n'}])
    serve_agent.run_turn(copilot(project, 'make a'), lambda text: None)
    (project/'b.txt').write_text('mine\n', encoding='utf-8')  # the user edits b.txt after the turn
    answer = serve_agent.run_turn(copilot(project, 'make a', '/undo'), lambda text: None)
    assert not (project/'a.txt').exists() and (project/'b.txt').read_text(encoding='utf-8') == 'mine\n'
    assert 'restored 1 file(s): a.txt' in answer and 'kept your own changes to b.txt' in answer
    answer = serve_agent.run_turn(copilot(project, 'make a', '/undo', '/undo'), lambda text: None)
    assert 'no turn of this chat changed files' in answer
    serve_agent.run_turn(copilot(project, 'make a', '/undo', '/undo', 'go on'), lambda text: None)
    assert 'the user undid the last turn' in state['calls'][-1]['follow_up']  # tare is told


def test_editing_an_earlier_message_rolls_back_later_turns_and_forks_from_the_visible_history(runtime, monkeypatch):
    project, state, _ = runtime
    scripted_effects(monkeypatch, project, [{'x.txt': 'x\n'}, {'y.txt': 'y\n'}, {}])
    serve_agent.run_turn(copilot(project, 'first'), lambda text: None)
    serve_agent.run_turn(copilot(project, 'first', 'second'), lambda text: None)
    assert (project/'y.txt').exists()
    out = []
    serve_agent.run_turn(copilot(project, 'first', 'second, but differently'), out.append)
    assert not (project/'y.txt').exists() and (project/'x.txt').exists()  # only the discarded turn is undone
    fork = state['calls'][-1]
    assert fork['config'] is not None and fork['custody'] != state['calls'][0]['custody']
    assert fork['instruction'].startswith('Conversation so far') and 'User: first' in fork['instruction']
    assert 'User: second\n' not in fork['instruction'] and fork['instruction'].endswith('second, but differently')
    assert any('went back to an earlier message' in line and 'y.txt' in line for line in out)


def test_a_spent_budget_moves_on_in_a_fresh_session_since_continuation_inherits_the_spend(runtime):
    project, state, saved = runtime
    assert [turn_engine.successor(s, per_message_limits=True) for s in ('BLOCKED', 'REJECTED', 'EXHAUSTED', 'PROPOSED')] \
        == ['continue', 'continue', 'fresh', None]
    assert turn_engine.successor('EXHAUSTED', per_message_limits=False) == 'stop'  # the TUI: the operator's cap
    state['outcomes'] = ['EXHAUSTED']
    serve_agent.run_turn(copilot(project, 'big task'), lambda text: None)
    successor = state['calls'][-1]
    assert saved == [] and successor['route'] == 'cheap' and successor['config'] is not None  # not a continuation
    assert successor['instruction'].startswith('BRIEF') and successor['limits']


def test_a_completed_reply_that_only_announced_the_work_moves_to_the_next_tier_once(runtime, monkeypatch):
    project, state, saved = runtime
    judged = []

    def unanswered(request, reply, observed=None):
        judged.append(reply)
        return {'model': 'qwen38-gsq', 'probabilities': {'announced': .95}} if reply == 'answer from local-a' else None
    monkeypatch.setattr(turn_engine, 'unanswered', unanswered)
    out = []
    answer = serve_agent.run_turn(copilot(project, 'explain IsDeleted'), out.append)
    assert [c['route'] for c in state['calls']] == ['local-a', None]  # the successor runs from continuation.json
    target, text, route = saved[0]
    assert route == 'cheap' and text.startswith('BRIEF') and turn_engine.UNANSWERED_CONTINUE in text
    assert answer == 'answer from resumed' and any('only announced the work' in line for line in out)
    assert judged == ['answer from local-a']  # once per message: the stronger tier's reply is not judged again
    store = json.loads(next((Path(serve_agent.chat_store('x')).parent).glob('*.json')).read_text())
    assert [s['route'] for s in store['sessions']] == ['local-a', 'cheap']


def test_the_answer_judge_is_not_asked_when_no_stronger_route_exists(runtime, monkeypatch):
    project, state, _ = runtime
    state['down'].update({'cheap', 'middle'})
    monkeypatch.setattr(turn_engine, 'unanswered', lambda *a, **kw: pytest.fail('judged with nowhere to go'))
    assert serve_agent.run_turn(copilot(project, 'hi'), lambda text: None) == 'answer from local-a'


def test_a_folder_outside_the_project_is_asked_in_the_chat_and_only_a_yes_grants_it(runtime, monkeypatch, tmp_path):
    from relay import folder_access
    project, state, _ = runtime
    other, notes = tmp_path/'other-repo', tmp_path/'notes'
    other.mkdir(), notes.mkdir()
    fake = native_agent.run_session
    asks = {1: other, 3: notes}  # the first and third turns ask for a folder

    def asking(config, deployment, custody, instruction=None, **kw):
        result = fake(config, deployment, custody, instruction, **kw)
        folder = asks.get(len(state['calls']))
        return {**result, 'folder_access_request': {'path': str(folder), 'reason': 'its schema'}} if folder else result
    monkeypatch.setattr(native_agent, 'run_session', asking)
    chat = ['compare with the other repo']
    answer = serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    store = folder_access.project_store(project)  # the grants file the TUI keeps for this project
    assert state['calls'][0]['config']['folder_access'] == {'grants': str(store)}
    assert f'asks to read ` {other} `: its schema' in answer and answer.startswith('answer from local-a')
    chat.append('sim')
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert state['calls'][1]['follow_up'] == 'sim'
    assert folder_access.granted(store) == []  # submission cannot grant
    chat.append('and the notes folder?')
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert '[tare]' not in state['calls'][2]['follow_up']  # answered once
    chat.append('why do you need it?')  # not a yes: nothing granted, and the agent is told
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert state['calls'][3]['follow_up'] == 'why do you need it?'
    assert folder_access.granted(store) == []


def test_a_chat_starts_without_local_models_and_other_chats_and_the_machine_keep_them(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'no local models in this chat') if (
        name.startswith('local') and turn_engine.avoid_local_gpu()) else (
        {'id': name, 'native_profile': {'protocol': 'openai-chat'}}, None))
    cloud, out = ['/local-gpu off explain main'], []  # a first message can start the chat that way
    serve_agent.run_turn(copilot(project, *cloud), out.append)
    assert state['calls'][0]['route'] == 'cheap' and state['calls'][0]['instruction'] == 'explain main'
    assert any('this chat uses no local models' in line for line in out)
    serve_agent.run_turn(copilot(project, 'explain the parser'), lambda text: None)  # another chat, same process
    assert state['calls'][1]['route'] == 'local-a'
    assert not turn_engine.avoid_local_gpu() and not turn_engine.leaves_gpu_to_others()  # nothing leaked
    cloud.append('/local-gpu')  # a recorded command is history, not an edited message that forks the chat
    out = serve_agent.run_turn(copilot(project, *cloud), lambda text: None)
    assert 'this chat uses no local models' in out and len(state['calls']) == 2
    cloud.append('/local-gpu off default')  # the machine: every chat and the TUI
    out = serve_agent.run_turn(copilot(project, *cloud), lambda text: None)
    assert turn_engine.leaves_gpu_to_others() and 'leaves the local GPU to others for every chat' in out
    cloud.append('/local-gpu on default')
    serve_agent.run_turn(copilot(project, *cloud), lambda text: None)
    assert not turn_engine.leaves_gpu_to_others()


def test_standalone_flow_resumes_only_after_cited_coordinator_approval(runtime, monkeypatch, tmp_path):
    from relay import folder_access, consent_decisions as consent
    from compute_plane.native_journal import NativeJournal
    project, state, _ = runtime
    other = tmp_path/'other-repo'
    other.mkdir()
    fake = native_agent.run_session

    def asking(config, deployment, custody, instruction=None, **kw):
        result = fake(config, deployment, custody, instruction, **kw)
        custody = Path(custody)
        custody.mkdir(parents=True, exist_ok=True)
        log = NativeJournal(custody/'journal.jsonl')
        if config and config['role'] == 'explorer' and len(state['calls']) == 1:
            log.append({'kind':'session_started', 'session_id':'flow'})
            q = consent.register(custody, 'flow', 'folder',
                {'path':str(other), 'store':str(folder_access.project_store(project).resolve())}, presented=False,
                question=folder_access.question({'path': str(other), 'reason': 'its cache'},
                                                folder_access.project_store(project)))
            return {**result, 'folder_access_request': {'path':str(other), 'reason':'its cache', 'question_id':q}}
        request = kw.get('folder_consent_request')
        if request:
            q = consent.adopt_folder(custody, 'coordinator', request['session'],
                request['question_id'], folder_access.project_store(project))
            assert not folder_access.granted(folder_access.project_store(project))
            message = str(len(state['calls']))
            log.append({'kind':'user_message', 'message_id':message, 'text':request['reply']})
            from test_consent_decisions import verdict
            choice = 'unclear' if request['reply'] == 'Por que?' else 'allow'
            monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict(choice)})
            receipt = consent.record(custody, 'coordinator', q, message)
            if receipt.get('notice'):
                kw['on_progress']({'kind':'delta','channel':'text','text':receipt['notice']})
        return result
    monkeypatch.setattr(native_agent, 'run_session', asking)
    chat = ['/explore where is the cache in both repos?']
    answer = serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert f'asks to read ` {other} `: its cache' in answer
    source = state['calls'][0]['custody']
    assert consent.pending(source, 'flow')[0]['messages'] == []
    chat.append('Por que?')
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert len(state['calls']) == 2
    coordinator = state['calls'][1]['custody']
    before = consent.load(coordinator)['questions'][0].copy()
    chat.append('pode')
    output = []
    serve_agent.run_turn(copilot(project, *chat), output.append)
    assert len(state['calls']) == 4
    assert state['calls'][-1]['config']['role'] == 'explorer'
    assert folder_access.granted(folder_access.project_store(project)) == [str(other)]
    after = consent.load(coordinator)['questions'][0]
    assert after['status'] == 'CONSUMED'
    assert after['boundary'] == before['boundary'] and after['prefix'] == before['prefix']
    assert any(str(other) in text and 'Revoke' in text and 'saved for this project' in text for text in output)
    chat.append('continue')
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert len(state['calls']) == 5  # no second flow dispatch


def test_a_request_choice_rules_out_local_models_for_that_turn_without_changing_the_chat(runtime, monkeypatch):
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'no local models in this chat') if (
        name.startswith('local') and turn_engine.avoid_local_gpu()) else (
        {'id': name, 'native_profile': {'protocol': 'openai-chat'}}, None))
    serve_agent.run_turn(copilot(project, 'explain main'), lambda text: None, local_models=False)
    assert state['calls'][0]['route'] == 'cheap'
    assert 'local_models' not in json.loads(next(Path(serve_agent.chat_store('x')).parent.glob('*.json')).read_text())
    serve_agent.run_turn(copilot(project, 'explain the parser'), lambda text: None)
    assert state['calls'][1]['route'] == 'local-a' and not turn_engine.avoid_local_gpu()


def test_first_serve_turn_shares_initial_choice_and_followup_never_reclassifies(runtime, monkeypatch):
    import compute_plane.jev_choice_worker as worker
    project, state, saved = runtime
    monkeypatch.setattr(turn_engine, 'jev_key', lambda: 'fixture-key')
    monkeypatch.setattr('relay.environment.decides', lambda name: True)
    monkeypatch.delenv('TARE_JEV_SHADOW', raising=False)
    monkeypatch.setattr(turn_engine, 'unanswered', lambda *a: None)
    asked, shadows, deployments = [], [], []
    def assess(request, **kw):
        asked.append(request)
        return {'choice': 'tier_1', 'probabilities': {'tier_0': 0, 'tier_1': 1, 'tier_2': 0},
                'model_identity': {'model': turn_engine.HOSTED_JEV_MODEL}, 'usage': {'input_tokens': 100}}
    monkeypatch.setattr(worker, 'assess', assess)
    monkeypatch.setattr(turn_engine, 'jev_shadow', lambda *a: shadows.append(a))
    run = native_agent.run_session
    def observe(config, deployment, custody, instruction=None, **kw):
        deployments.append(deployment)
        return run(config, deployment, custody, instruction, **kw)
    monkeypatch.setattr(native_agent, 'run_session', observe)
    serve_agent.run_turn(copilot(project, 'complex full requirement é'), lambda text: None)
    assert len(asked) == 1 and asked[0]['state'] == 'complex full requirement é'
    assert state['calls'][0]['route'] == 'cheap' and shadows == []
    selection = deployments[0]['native_auto_selection']
    assert selection['decision']['source'] == 'hosted_jev'
    stored = json.loads(next(serve_agent.chat_store('x').parent.glob('*.json')).read_text())
    assert stored['sessions'][0]['selection'] == selection
    monkeypatch.setattr(turn_engine, 'initial_choice', lambda *a: pytest.fail('followup reclassified'))
    serve_agent.run_turn(copilot(project, 'complex full requirement é', 'more'), lambda text: None)
    assert len(asked) == 1 and deployments[-1] is None
    assert state['calls'][-1]['custody'] == state['calls'][0]['custody']
    assert state['calls'][-1]['resume'] is True and len(shadows) == 1  # existing followup shadow retained


@pytest.mark.parametrize('kind', ['folder', 'retry'])
def test_serve_consent_judges_raw_reply_not_enriched_context(runtime, monkeypatch, tmp_path, kind):
    from compute_plane.native_journal import NativeJournal
    from relay import consent_decisions as consent, folder_access
    from test_native_agent import Client, deployment
    from test_consent_decisions import verdict
    project, state, _ = runtime
    (project/'a.py').write_text('value = 1\n')
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
              'repositories': {'workspace': str(project)}, 'writable': {'workspace': ['a.py']},
              'specgraph_edit_context': False, 'verifiers': {}}
    sessions = []
    clients = []
    def run(config_arg, route, custody, instruction=None, **kwargs):
        client = Client(steps=[('tare_deliver', {'summary': 'Please clarify.'})])
        clients.append(client)
        result = state['real_run_session'](config if config_arg else None,
            deployment() if config_arg else None, custody, instruction,
            client=client, local_counter=client, **kwargs)
        sessions.append((Path(custody), result['session_id']))
        return result
    monkeypatch.setattr(native_agent, 'run_session', run)
    monkeypatch.setattr(turn_engine, 'unanswered', lambda *a, **k: False)
    monkeypatch.setattr(turn_engine, 'jev_shadow', lambda *a, **k: None)
    chat = ['Inspect the project']
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    custody, session = sessions[0]
    question = 'Allow reading the external folder?' if kind == 'folder' else 'Allow another attempt?'
    other = tmp_path/'external'; other.mkdir()
    scope = ({'path': str(other), 'store': str(folder_access.project_store(project).resolve())}
             if kind == 'folder' else {'task': 'example'})
    consent.register(custody, session, kind, scope, question=question)
    packets = []
    def judges(packet, **kwargs):
        packets.append(packet)
        return {'jev': verdict('unclear')}
    monkeypatch.setattr(turn_engine, 'judges', judges)
    monkeypatch.setattr(turn_engine, 'read_pages', lambda *a: '\nSECRET LINKED PAGE')
    monkeypatch.setattr(turn_engine, 'describe_images', lambda *a: '\nSECRET IMAGE DESCRIPTION')
    store = next(Path(serve_agent.chat_store('x')).parent.glob('*.json'))
    saved = json.loads(store.read_text())
    saved['notes'] = ['SECRET FLOW HISTORY']
    store.write_text(json.dumps(saved))
    chat.append('pode ler, claro')
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert len(packets) == 1
    assert set(packets[0]) == {'question', 'options', 'state'}
    assert packets[0]['options'] == consent.OPTIONS
    judge_question = ('Allow read access to the entire folder for this project? The grant is saved for this project on this machine. Path: ' + json.dumps(str(other))
                      if kind == 'folder' else question)
    assert json.loads(packets[0]['state']) == {'question': judge_question, 'user_words': chat[-1]}
    assert 'SECRET' not in json.dumps(packets)
    body = next(b for p, b in clients[-1].calls if p == '/chat/completions')
    assert all(text in json.dumps(body['messages'], ensure_ascii=False) for text in (
        'SECRET LINKED PAGE', 'SECRET IMAGE DESCRIPTION', 'SECRET FLOW HISTORY', consent.REASK))
    assert consent.pending(custody, session)
    assert consent.process(custody, session) == []
    assert len(packets) == 1
    rows = [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    assert any(r.get('text') == chat[-1] for r in rows if r['kind'] == 'user_message')


def test_real_serve_folder_notice_is_a_delta_delivered_once(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr('relay.consent_decisions.select_rule', lambda: 'judge')
    from relay import consent_decisions as consent, folder_access
    from test_native_agent import Client, deployment
    from test_consent_decisions import verdict
    project, state, _ = runtime
    outside = tmp_path/'reference'; outside.mkdir()
    (project/'a.py').write_text('value = 1\n')
    results, events, sessions = [], [], []

    def run(config_arg, route, custody, instruction=None, **kwargs):
        steps = ([('request_folder_access', {'path': str(outside), 'reason': 'Read reference.'})]
                 if config_arg else [])
        client = Client(steps=steps + [('tare_deliver', {'summary': 'Turn finished.'})])
        config = ({'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
                   'repositories': {'workspace': str(project)}, 'writable': {'workspace': ['a.py']},
                   'specgraph_edit_context': False, 'verifiers': {},
                   'folder_access': config_arg['folder_access']} if config_arg else None)
        callback = kwargs['on_progress']
        def progress(event):
            assert isinstance(event, dict)
            events.append(event)
            callback(event)
        kwargs['on_progress'] = progress
        result = state['real_run_session'](config, deployment() if config_arg else None,
            custody, instruction, client=client, local_counter=client, **kwargs)
        results.append(result)
        sessions.append(Path(custody))
        return result

    monkeypatch.setattr(native_agent, 'run_session', run)
    monkeypatch.setattr(turn_engine, 'unanswered', lambda *a, **k: False)
    monkeypatch.setattr(turn_engine, 'jev_shadow', lambda *a, **k: None)
    monkeypatch.setattr(turn_engine, 'judges', lambda *a, **k: {'jev': verdict('allow')})
    chat = ['Inspect the reference folder']
    serve_agent.run_turn(copilot(project, *chat), lambda text: None)
    assert results[-1]['folder_access_request']['path'] == str(outside)
    chat.append('pode ler, claro')
    output = []
    answer = serve_agent.run_turn(copilot(project, *chat), output.append)
    assert results[-1]['status'] == 'PROPOSED'
    q = consent.load(sessions[-1])['questions'][0]
    notice = q['notice']
    assert 'Read access granted' in notice and q['notice_delivered'] is True
    assert results[-1]['output'].count(notice) == 1
    assert ''.join(output).count(notice) == 1
    assert notice not in answer  # serve returns only unstreamed text
    assert [e for e in events if notice in e.get('text', '')] == [
        {'kind': 'delta', 'channel': 'text', 'text': '\n\n' + notice}]
    assert folder_access.granted(folder_access.project_store(project)) == [str(outside)]
    events.clear(); output.clear()
    chat.append('Continue explaining')
    answer = serve_agent.run_turn(copilot(project, *chat), output.append)
    assert results[-1]['status'] == 'PROPOSED'
    assert notice not in results[-1]['output']
    assert notice not in answer and notice not in ''.join(output)
    assert not any(notice in e.get('text', '') for e in events)

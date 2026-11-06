"""The agent asks to read a folder outside its repositories; only the user's reply grants it, and the grant stays."""
import json
from pathlib import Path

import pytest

from relay import folder_access, native_agent
from relay.console_backend import Selection, command
from test_native_agent import Client, deployment, setup


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path/'other-project'
    (folder/'docs').mkdir(parents=True)
    (folder/'docs/schema.md').write_text('backlog schema v2\n', encoding='utf-8')
    (tmp_path/'secret.txt').write_text('never read\n', encoding='utf-8')
    return folder.resolve()


def granting(setup, tmp_path):
    config, custody, root = setup
    grants = tmp_path/'grants'/'grants.json'
    return {**config, 'folder_access': {'grants': str(grants)}}, custody, grants


@pytest.mark.parametrize('path,store', [
    (r'C:\projects\_archives\[evidence]', r'C:\profiles\_folder_grants\grants.json'),
    (r'C:\projects\`evidence`', r'C:\profiles\``grants``.json'),
])
def test_folder_consent_markdown_preserves_literal_windows_paths(path, store):
    from io import StringIO
    from rich.console import Console
    from rich.markdown import Markdown
    text = folder_access.question({'path': path, 'reason': 'Read the recorded evidence.'}, store)
    output = StringIO()
    Console(file=output, width=4096, force_terminal=False).print(Markdown(text))
    rendered = output.getvalue()
    assert path in rendered and store in rendered
    assert 'Reply naturally' in rendered and 'read' in rendered


def replies(client):
    """What each tool answered the model, in order (the last request carries the whole conversation)."""
    last = [body for path, body in client.calls if isinstance(body, dict) and 'messages' in body][-1]
    def parsed(text):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return [parsed(m['content']) for m in last['messages'] if m.get('role') == 'tool']


@pytest.mark.parametrize('conversation', [False, True])
def test_the_user_grants_a_folder_mid_conversation_and_later_sessions_keep_it(setup, tmp_path, outside, conversation, monkeypatch):
    from test_consent_decisions import verdict
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict()})
    config, custody, grants = granting(setup, tmp_path)
    asking = Client('openai-chat', [
        ('folder_read', {'folder': str(outside), 'path': 'docs/schema.md'}),  # not granted yet
        ('request_folder_access', {'path': str(outside), 'reason': 'The backlog schema lives there.'}),
        ('tare_deliver', {'summary': 'May I read the backlog project?'})])
    first = native_agent.run_session(config, deployment(), custody, 'Convert the backlog.',
        conversation=conversation, client=asking, local_counter=asking)
    assert first['status'] == 'PROPOSED'
    assert [r.get('status') or 'refused' for r in replies(asking)] == ['refused', 'ASKED']
    assert first['folder_access_request']['path'] == str(outside)
    assert first['folder_access_request']['reason'] == 'The backlog schema lives there.'
    assert folder_access.granted(grants) == []  # asking grants nothing

    from relay import consent_decisions as consent
    from compute_plane.native_journal import NativeJournal
    state = json.loads((custody/'native-agent.json').read_text(encoding='utf8'))
    question_id = first['folder_access_request']['question_id']
    consent.present(custody, question_id)
    log = NativeJournal(custody/'journal.jsonl')
    log.append({'kind':'user_message','message_id':'folder-answer',
        'text':'pode ler, claro', 'after_sequence':len(log.snapshot().records),
        **({'interrupts_turn': True} if conversation else {})})
    note = consent.record(custody, state['session_id'], question_id, 'folder-answer')['notice']
    assert folder_access.granted(grants) == [str(outside)] and 'granted' in note
    reading = Client('openai-chat', [
        ('folder_list', {'folder': str(outside), 'path': 'docs'}),
        ('folder_read', {'folder': str(outside), 'path': 'docs/schema.md'}),
        ('folder_read', {'folder': str(outside), 'path': '../secret.txt'}),  # still confined to the folder
        ('tare_deliver', {'summary': 'Read the schema.'})])
    second = native_agent.run_session(None, None, custody, resume=True, follow_up='pode ler, claro',
                                      message_id='folder-answer', client=reading, local_counter=reading)
    assert second['status'] == 'PROPOSED', (second['reason'], second.get('output'))
    original = next(body for path,body in asking.calls if path == '/chat/completions')
    resumed = next(body for path,body in reading.calls if path == '/chat/completions')
    assert original['tools'] == resumed['tools']
    assert 'folder_access_request' not in second
    shown = [(item['operation'], item['target'], item['state']) for item in second['tool_activity']['items']]
    assert shown[0] == ('folder_list', str(outside)+': docs', 'succeeded') and shown[2][2] == 'refused'
    listed, read, escaped = replies(reading)[-3:]
    assert listed['entries'] == ['schema.md'] and 'backlog schema v2' in json.dumps(read)
    assert escaped['reason'] == 'invalid_repository_path' and 'never read' not in json.dumps(reading.calls)

    later = Client('openai-chat', [('folder_read', {'folder': str(outside), 'path': 'docs/schema.md'}),
                                   ('tare_deliver', {'summary': 'Read it again.'})])
    third = native_agent.run_session(config, deployment(), tmp_path/'later-session', 'Check the schema.',
                                     client=later, local_counter=later)
    assert third['status'] == 'PROPOSED' and 'backlog schema v2' in json.dumps(replies(later))


def test_a_folder_of_the_project_needs_no_grant_and_writes_no_request(setup, tmp_path):
    config, custody, grants = granting(setup, tmp_path)
    client = Client('openai-chat', [
        ('request_folder_access', {'path': str(Path(config['repositories']['repo'])), 'reason': 'Need it.'}),
        ('tare_deliver', {'summary': 'Already readable.'})])
    result = native_agent.run_session(config, deployment(), custody, 'Look.', client=client, local_counter=client)
    assert 'ALREADY_READABLE' in json.dumps(client.calls) and 'folder_access_request' not in result


def test_granted_large_graph_is_searchable_and_readable_in_an_existing_session(setup, tmp_path, outside):
    import hashlib
    config, custody, grants = granting(setup, tmp_path)
    raw = ('{\n"padding":"'+'x'*(3*1024*1024)+'",\n"id":"i2i01"\n}\n').encode()
    (outside/'work-graph.json').write_bytes(raw)
    first = Client('openai-chat', [('tare_deliver', {'summary': 'Waiting for comparison access.'})])
    native_agent.run_session(config, deployment(), custody, 'Compare project backlogs.', client=first, local_counter=first)
    folder_access.grant(grants, str(outside))
    reading = Client('openai-chat', [
        ('folder_search', {'folder': str(outside), 'path': 'work-graph.json', 'query': 'i2i01'}),
        ('folder_read', {'folder': str(outside), 'path': 'work-graph.json', 'start': 3, 'end': 3}),
        ('tare_deliver', {'summary': 'Read the actual node.'})])
    result = native_agent.run_session(None, None, custody, resume=True, follow_up='Compare now.',
        message_id='large-grant', client=reading, local_counter=reading)
    found, page = replies(reading)[-2:]
    assert result['status'] == 'PROPOSED' and found['status'] == 'HIT'
    assert found['coverage']['files_searched'] == 1 and page['text'] == '3|"id":"i2i01"'
    assert page['sha256'] == hashlib.sha256(raw).hexdigest() and len(page['text'].encode()) <= 24000
    original = next(body for path,body in first.calls if path == '/chat/completions')
    resumed = next(body for path,body in reading.calls if path == '/chat/completions')
    assert original['tools'] == resumed['tools'] and original['messages'][0] == resumed['messages'][0]


def test_protected_worker_uses_complete_unit_contract_instead_of_large_planner_brief(setup, monkeypatch):
    from types import SimpleNamespace
    config, custody, _ = setup
    contract = json.dumps({'title': 'Fix backlog writes', 'criteria': ['Preserve proven closures'],
                           'target_paths': ['work-graph.json']})
    config = {**config, 'command_protection': {'review': 'auto', 'backend': 'mxc'},
              'command_context': contract}
    observed = []
    def factory(config, deployment, root, context, *args, **kwargs):
        observed.append(context())
        return lambda **kwargs: SimpleNamespace(execute=lambda *args: None)
    monkeypatch.setattr('relay.command_protection.factory', factory)
    client = Client('openai-chat', [('tare_deliver', {'summary': 'No command or inference review needed.'})])
    result = native_agent.run_session(config, deployment(), custody, 'Planner advice: '+'x'*18000,
        client=client, local_counter=client)
    assert result['status'] == 'PROPOSED' and observed == [contract]
    saved = json.loads((custody/'native-agent.json').read_text(encoding='utf8'))
    assert saved['config']['command_context'] == contract


@pytest.mark.parametrize('reply,decision', [('Claro, pode ler', 'yes'), ('pode sim, manda bala', 'yes'),
    ('manda ver', 'yes'), ('não', 'no'), ('não, obrigado', 'no'), ('deixa pra depois', 'no'),
    ('Por que?', 'pending'), ('espera, me explica antes', 'pending')])
def test_coordinator_interprets_natural_folder_answers(tmp_path, outside, reply, decision, monkeypatch):
    from test_consent_decisions import verdict
    choice = {'yes': 'allow', 'no': 'deny', 'pending': 'unclear'}[decision]
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict(choice)})
    store = tmp_path/'grants.json'
    from relay import consent_decisions as consent
    from compute_plane.native_journal import NativeJournal
    log = NativeJournal(tmp_path/'journal.jsonl')
    log.append({'kind':'session_started','session_id':'folder-test'})
    question = consent.register(tmp_path, 'folder-test', 'folder', {'path':str(outside),'store':str(store)}, question='Allow reading this folder?')
    log.append({'kind':'user_message','message_id':'answer','text':reply})
    receipt = consent.record(tmp_path, 'folder-test', question, 'answer')
    assert (folder_access.granted(store) == [str(outside)]) is (decision == 'yes')
    assert folder_access.declined(tmp_path, str(outside)) is (decision == 'no')
    if decision == 'yes':
        assert str(outside) in receipt['notice'] and str(store) in receipt['notice']
    if decision == 'pending':
        assert consent.pending(tmp_path, 'folder-test')[0]['question_id'] == question
        log.append({'kind':'user_message','message_id':'later','text':'Claro, pode ler'})
        monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict()})
        consent.record(tmp_path, 'folder-test', question, 'later')
        assert folder_access.granted(store) == [str(outside)]


def test_a_whole_drive_relative_or_missing_folder_is_refused(tmp_path):
    for value in [Path(tmp_path.anchor), 'relative/folder', str(tmp_path/'missing')]:
        with pytest.raises(ValueError):
            folder_access.folder(str(value))


def test_the_user_folder_and_credential_folders_are_never_granted_nor_read(tmp_path, outside):
    keys = tmp_path/'repo'/'.SSH'
    keys.mkdir(parents=True)
    for value in (Path.home(), Path.home().parent, keys):
        with pytest.raises(ValueError, match='credentials'):
            folder_access.folder(str(value))
    store = tmp_path/'grants.json'
    with pytest.raises(ValueError, match='Raw-text consent'):
        folder_access.answer('sim', {'path': str(Path.home()), 'reason': 'x'}, str(store))
    assert folder_access.granted(store) == []


def test_the_tui_starts_sessions_with_the_grants_file_and_sends_the_reply_note(tmp_path):
    project = tmp_path/'project'; project.mkdir()
    selection = Selection(root=str(project), mode='session', role='implementer', write_paths=['.'], prompt='sim',
                          folder_grants=str(tmp_path/'grants.json'), folder_note='[tare] granted')
    args = command(selection, assistant=True)
    assert args[args.index('--folder-grants')+1] == str(tmp_path/'grants.json')
    assert args[args.index('--instruction')+1].endswith('sim\n\n[tare] granted')


@pytest.mark.parametrize('reply,allowed', [('sim', True), ('por que precisa?', False)])
@pytest.mark.parametrize('reopened',[False,True])
def test_the_tui_turns_the_reply_to_a_pending_request_into_a_grant_or_not(tmp_path, outside, monkeypatch, reply, allowed,reopened):
    import asyncio
    from textual.widgets import TextArea
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    project = tmp_path/'project'; project.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    session = tmp_path/'state'/'session-one'; session.mkdir(parents=True)
    (session/folder_access.REQUEST).write_text(json.dumps({'path': str(outside), 'reason': 'Schema there.'}))
    sent = []
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session)
            app.folder_request = {'path': str(outside), 'reason': 'Schema there.', 'session': str(session)}
            if reopened:
                app.checkpoint('draft_saved')
                identity=app.demand_id
                app.new_demand()
                assert app.folder_request is None
                app.action_conversations();await app.workers.wait_for_complete()
                app.open_conversation(next(i for i,r in enumerate(app.navigation_shown) if r['id']==identity))
                await app.workers.wait_for_complete()
                assert app.folder_request=={'path':str(outside),'reason':'Schema there.','session':str(session)}
            app.execute = lambda *a, **k: sent.append(app.selection())
            app.query_one('#prompt', TextArea).load_text(reply)
            app.begin_assistant()
            assert app.folder_request is not None and app.folder_note == ''
            return app.folder_grants(str(project))
    store = asyncio.run(scenario())
    [selection] = sent
    assert selection.folder_note == '' and selection.prompt == reply
    assert folder_access.granted(store) == []
    assert (session/folder_access.REQUEST).exists()  # submission cannot consume the question


@pytest.mark.parametrize('reply', ['pode', 'Claro, pode ler', 'manda ver'])
def test_initial_coordinator_journals_flow_answer_before_deciding(setup, tmp_path, outside, reply, monkeypatch):
    from test_consent_decisions import verdict
    packets = []
    def judge(packet, **kwargs):
        packets.append(packet)
        return {'jev': verdict()}
    monkeypatch.setattr('relay.turn_engine.judges', judge)
    from relay import consent_decisions as consent
    from compute_plane.native_journal import NativeJournal
    config, custody, grants = granting(setup, tmp_path)
    source = tmp_path/'flow'
    source.mkdir()
    NativeJournal(source/'journal.jsonl').append({'kind':'session_started','session_id':'flow'})
    q = consent.register(source, 'flow', 'folder',
        {'path':str(outside), 'store':str(grants.resolve())}, presented=False, question='Allow reading this folder?')
    consent.present(source, q)

    client = Client(steps=[('folder_list', {'folder':str(outside), 'path':'.'}),
                           ('tare_deliver', {'summary':'Access granted.'})])
    result = native_agent.run_session(config, deployment(), custody, reply, conversation=True,
        client=client, local_counter=client,
        folder_consent_request={'session':str(source), 'question_id':q, 'reply':reply})
    assert result['status'] == 'PROPOSED', (result, (custody/'journal.jsonl').read_text(), client.generations)
    assert folder_access.granted(grants) == [str(outside)]


def test_folder_denial_can_be_followed_by_a_fresh_request(tmp_path, outside, monkeypatch):
    from types import SimpleNamespace
    from compute_plane.native_journal import NativeJournal
    from relay import consent_decisions as consent
    from test_consent_decisions import verdict
    store = tmp_path/'grants.json'
    project = tmp_path/'project'; project.mkdir()
    tools = folder_access.FolderTools(
        SimpleNamespace(names=(), tool_specs_json='{}', session_id='folder-test'),
        {'folder_access': {'grants': str(store)}, 'repositories': {'project': str(project)}}, tmp_path)
    log = NativeJournal(tmp_path/'journal.jsonl')
    log.append({'kind': 'session_started', 'session_id': 'folder-test'})
    args = {'path': str(outside), 'reason': 'Read the schema.'}
    assert tools._request(args)['status'] == 'ASKED'
    first = json.loads((tmp_path/folder_access.REQUEST).read_text())['question_id']
    consent.present(tmp_path, first)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict('deny', .95)})
    log.append({'kind': 'user_message', 'message_id': 'deny', 'text': 'não, obrigado'})
    assert consent.record(tmp_path, 'folder-test', first, 'deny')['decision'] == 'no'
    assert folder_access.declined(tmp_path, str(outside))
    assert folder_access.granted(store) == []
    assert tools._request(args)['status'] == 'ASKED'
    second = json.loads((tmp_path/folder_access.REQUEST).read_text())['question_id']
    assert second != first
    consent.present(tmp_path, second)
    pending = consent.pending(tmp_path, 'folder-test')
    assert len(pending) == 1 and pending[0]['question_id'] == second
    assert pending[0]['messages'] == []
    with pytest.raises(ValueError):
        consent.record(tmp_path, 'folder-test', first, choice='allow')
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict('allow', .95)})
    log.append({'kind': 'user_message', 'message_id': 'allow', 'text': 'pode ler, claro'})
    assert consent.record(tmp_path, 'folder-test', second, 'allow')['decision'] == 'yes'
    assert folder_access.granted(store) == [str(outside)]
    assert consent.pending(tmp_path, 'folder-test') == []
    with pytest.raises(ValueError):
        consent.record(tmp_path, 'folder-test', second, choice='allow')

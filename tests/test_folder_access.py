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
    assert 'Reply yes' in rendered and 'read' in rendered


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
def test_the_user_grants_a_folder_mid_conversation_and_later_sessions_keep_it(setup, tmp_path, outside, conversation):
    config, custody, grants = granting(setup, tmp_path)
    asking = Client('openai-chat', [
        ('folder_read', {'folder': str(outside), 'path': 'docs/schema.md'}),  # not granted yet
        ('request_folder_access', {'path': str(outside), 'reason': 'The backlog schema lives there.'}),
        ('tare_deliver', {'summary': 'May I read the backlog project?'})])
    first = native_agent.run_session(config, deployment(), custody, 'Convert the backlog.',
        conversation=conversation, client=asking, local_counter=asking)
    assert first['status'] == 'PROPOSED'
    assert [r.get('status') or 'refused' for r in replies(asking)] == ['refused', 'ASKED']
    assert first['folder_access_request'] == {'path': str(outside), 'reason': 'The backlog schema lives there.'}
    assert folder_access.granted(grants) == []  # asking grants nothing

    note = folder_access.answer('sim, pode', first['folder_access_request'], str(grants))
    folder_access.settle(custody)
    assert folder_access.granted(grants) == [str(outside)] and 'granted' in note
    reading = Client('openai-chat', [
        ('folder_list', {'folder': str(outside), 'path': 'docs'}),
        ('folder_read', {'folder': str(outside), 'path': 'docs/schema.md'}),
        ('folder_read', {'folder': str(outside), 'path': '../secret.txt'}),  # still confined to the folder
        ('tare_deliver', {'summary': 'Read the schema.'})])
    second = native_agent.run_session(None, None, custody, resume=True, follow_up='sim, pode\n\n'+note,
                                      message_id='grant', client=reading, local_counter=reading)
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


@pytest.mark.parametrize('reply,grants', [('sim', True), ('Pode!', True), ('ok', True), ('yes please', True),
                                          ('pode uai', True), ('claro que pode', True), ('uai', False),
                                          ('pode uai, mas antes explique', False),
                                          ('não', False), ('por que você precisa disso?', False),
                                          ('sim, mas antes me explique o motivo', False), ('sim, por favor', True),
                                          ('ok, mas rode os testes', False), ('vai que eu confio', False)])
def test_only_a_short_yes_grants(tmp_path, outside, reply, grants):
    store = tmp_path/'grants.json'
    note = folder_access.answer(reply, {'path': str(outside), 'reason': 'x'}, str(store))
    assert (folder_access.granted(store) == [str(outside)]) is grants
    assert ('granted read access' in note) is grants


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
    with pytest.raises(ValueError, match='credentials'):  # a yes to it saves nothing (the caller reports why)
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
            assert app.folder_request is None and app.folder_note == ''
            return app.folder_grants(str(project))
    store = asyncio.run(scenario())
    [selection] = sent
    assert ('granted read access' in selection.folder_note) is allowed and selection.prompt == reply
    assert (folder_access.granted(store) == [str(outside)]) is allowed
    assert not (session/folder_access.REQUEST).exists()  # answered either way

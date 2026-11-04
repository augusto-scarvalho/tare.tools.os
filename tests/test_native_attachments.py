import base64
import io
import json

from PIL import Image
import pytest

from compute_plane.native_attachments import AttachmentError, NativeAttachmentStore
from relay import native_agent, native_messages
from test_native_agent import setup, Client, deployment


def screenshot(store, color='red'):
    output = io.BytesIO()
    Image.new('RGB', (16, 8), color).save(output, format='PNG')
    return store.put_bytes('screen.png', output.getvalue())


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_initial_document_uses_session_snapshot_after_draft_deletion(setup, tmp_path, protocol):
    config, directory, _ = setup
    drafts = NativeAttachmentStore(tmp_path/'draft-assets')
    ref = drafts.put_bytes('error.log', b'Error E312: unmatched separator')
    client = Client(protocol, [('tare_deliver', {'summary': 'Explained the error.'}),
                               ('tare_deliver', {'summary': 'More details.'})])
    first = native_agent.run_session(config, deployment(protocol), directory, '',
        attachments=(ref,), attachment_root=drafts.root, client=client, local_counter=client)
    assert first['status'] == 'PROPOSED'
    (drafts.root/ref.sha256).unlink()
    second = native_agent.run_session(None, None, directory, resume=True,
        follow_up='Explain the cause.', message_id='next', client=client, local_counter=client)
    assert second['status'] == 'PROPOSED'
    assert 'Error E312: unmatched separator' in json.dumps(client.calls[-1][1])
    assert NativeAttachmentStore(directory/'attachments').load(ref) == b'Error E312: unmatched separator'
    native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert client.generations == 2


def test_queued_image_reaches_next_round_and_identity_includes_content(setup, tmp_path):
    config, directory, project = setup
    drafts = NativeAttachmentStore(tmp_path/'draft-assets')
    ref = screenshot(drafts)
    raw = drafts.load(ref)
    replacement = screenshot(drafts, 'blue')
    def incoming():
        if client.generations == 1:
            native_messages.enqueue(directory, '', message_id='image', project=project,
                attachments=(ref,), attachment_root=drafts.root)
            assert native_messages.inspect(directory)['items'][0]['status'] == 'QUEUED'
            with pytest.raises(ValueError, match='different text or attachments'):
                native_messages.enqueue(directory, '', message_id='image', attachments=(replacement,),
                    attachment_root=drafts.root)
            (drafts.root/ref.sha256).unlink()
    client = Client('bedrock-converse', [('read', {'repository':'repo', 'path':'a.py'}),
                                       ('tare_deliver', {'summary':'Explained the screenshot.'})], incoming)
    model = deployment('bedrock-converse') | {'native_image_media_types':['image/png']}
    result = native_agent.run_session(config, model, directory, 'Read the source.',
        client=client, local_counter=client, accept_messages=True, accept_attachments=True)
    assert result['status'] == 'PROPOSED' and client.generations == 2
    assert base64.b64encode(raw).decode() in json.dumps(client.calls[-1][1])
    item = native_messages.inspect(directory)['items'][0]
    assert item['status'] == 'IN_CONTEXT' and item['attachments'][0]['sha256'] == ref.sha256
    native_messages.enqueue(directory, '', message_id='image', attachments=(ref,))
    with pytest.raises(ValueError, match='different text or attachments'):
        native_messages.enqueue(directory, '', message_id='image', attachments=(replacement,),
            attachment_root=drafts.root)
    native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert client.generations == 2


def test_followup_image_survives_source_refresh_and_retry(setup, tmp_path):
    config, directory, project = setup
    drafts = NativeAttachmentStore(tmp_path/'draft-assets')
    ref = screenshot(drafts)
    client = Client('bedrock-converse', [('read', {'repository':'repo','path':'a.py'}),
        ('tare_deliver', {'summary':'Read.'}), ('tare_deliver', {'summary':'Reviewed the changed source.'})])
    model = deployment('bedrock-converse') | {'native_image_media_types':['image/png']}
    native_agent.run_session(config, model, directory, 'Read a.py.', client=client,
        local_counter=client, accept_attachments=True)
    (project/'a.py').write_bytes(b'value = 3\n')
    result = native_agent.run_session(None, None, directory, resume=True, refresh=True,
        follow_up='Compare this screenshot.', message_id='follow', attachments=(ref,),
        attachment_root=drafts.root, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'
    assert base64.b64encode(drafts.load(ref)).decode() in json.dumps(client.calls[-1][1])
    native_agent.run_session(None, None, directory, resume=True, refresh=True,
        follow_up='Compare this screenshot.', message_id='follow', attachments=(ref,),
        client=client, local_counter=client)
    assert client.generations == 3


def test_missing_image_or_unconfigured_vision_blocks_without_generation(setup, tmp_path):
    config, directory, _ = setup
    drafts = NativeAttachmentStore(tmp_path/'draft-assets')
    ref = screenshot(drafts)
    client = Client()
    result = native_agent.run_session(config, deployment(), directory, 'Explain the image.',
        attachments=(ref,), attachment_root=drafts.root, client=client, local_counter=client)
    assert result['status'] == 'BLOCKED' and result['reason'] == 'CONTEXT_COUNT_ERROR'
    assert not client.calls
    (directory/'attachments'/ref.sha256).unlink()
    result = native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert result['status'] == 'BLOCKED' and not client.calls


def test_old_session_refuses_attachment_queue_and_missing_snapshot_is_not_saved(setup, tmp_path):
    config, directory, _ = setup
    drafts = NativeAttachmentStore(tmp_path/'draft-assets')
    ref = screenshot(drafts)
    client = Client(steps=[('tare_deliver',{'summary':'Explained.'})])
    native_agent.run_session(config, deployment(), directory, 'Explain.',
        client=client, local_counter=client, accept_messages=True)
    with pytest.raises(ValueError, match='does not accept attachments'):
        native_messages.enqueue(directory, 'Look.', attachments=(ref,), attachment_root=drafts.root)
    assert not (directory/'attachments').exists()
    other = tmp_path/'attachment-session'
    native_agent.run_session(config, deployment(), other, 'Explain.', client=Client(steps=[
        ('tare_deliver',{'summary':'Explained.'})]), local_counter=Client(),
        accept_messages=True, accept_attachments=True)
    (drafts.root/ref.sha256).unlink()
    with pytest.raises(AttachmentError, match='SNAPSHOT_UNAVAILABLE'):
        native_messages.enqueue(other, 'Look.', attachments=(ref,), attachment_root=drafts.root)
    assert native_messages.inspect(other)['items'] == []


def test_attachment_custody_cannot_be_created_inside_selected_project(setup, tmp_path):
    config, _, project = setup
    drafts = NativeAttachmentStore(tmp_path/'draft-assets')
    ref = screenshot(drafts)
    with pytest.raises(ValueError, match='custody_inside_repository'):
        native_agent.run_session(config, deployment(), project/'history', 'Look.',
            attachments=(ref,), attachment_root=drafts.root)
    assert not (project/'history').exists()


@pytest.mark.parametrize('invalid', [{}, [], None, [{'sha256':'missing metadata'}]])
def test_malformed_queue_attachment_metadata_is_not_delivered_as_plain_text(setup, invalid, capsys):
    config, directory, _ = setup
    client = Client(steps=[('tare_deliver',{'summary':'Explained.'})])
    native_agent.run_session(config, deployment(), directory, 'Explain.', client=client,
        local_counter=client, accept_messages=True, accept_attachments=True)
    native_messages.enqueue(directory, 'Look.', message_id='bad')
    queue = directory/'messages'/'queue.json'
    value = json.loads(queue.read_text())
    value['items'][0]['attachments'] = invalid
    queue.write_text(json.dumps(value))
    before = queue.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        native_messages.inspect(directory)
    assert native_messages.main(['--session', str(directory), '--show']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'REFUSED'
    assert queue.read_bytes() == before and client.generations == 1


def test_cli_snapshots_files_on_first_input_and_explicit_followup(setup, tmp_path, monkeypatch, capsys):
    config, directory, _ = setup
    config_file=tmp_path/'config.json';config_file.write_text(json.dumps(config))
    profile_file=tmp_path/'profile.json';profile_file.write_text(json.dumps(deployment()))
    source=tmp_path/'operator notes.txt';source.write_text('First document')
    client=Client(steps=[('tare_deliver',{'summary':'First.'}),('tare_deliver',{'summary':'Second.'})])
    class Http:
        def __new__(cls,*args,**kwargs):return client
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient',Http)
    assert native_agent.main(['--config',str(config_file),'--native-deployment',str(profile_file),
        '--session',str(directory),'--attach',str(source),'--format','json'])==0
    assert json.loads(capsys.readouterr().out)['status']=='PROPOSED'
    source.write_text('Second document')
    assert native_agent.main(['--session',str(directory),'--resume','--attach',str(source),
        '--follow-up','','--message-id','second','--format','json'])==0
    assert json.loads(capsys.readouterr().out)['status']=='PROPOSED'
    assert 'First document' in json.dumps(client.calls[-1]) and 'Second document' in json.dumps(client.calls[-1])
    assert client.generations==2


def test_missing_cli_file_reports_refusal_without_starting_generation(setup, tmp_path, monkeypatch, capsys):
    config, directory, _ = setup
    config_file=tmp_path/'config.json';config_file.write_text(json.dumps(config))
    profile_file=tmp_path/'profile.json';profile_file.write_text(json.dumps(deployment()))
    client=Client()
    class Http:
        def __new__(cls,*args,**kwargs):return client
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient',Http)
    assert native_agent.main(['--config',str(config_file),'--native-deployment',str(profile_file),
        '--session',str(directory),'--attach',str(tmp_path/'missing.txt'),'--format','json'])==2
    assert 'SOURCE_UNAVAILABLE' in json.loads(capsys.readouterr().out)['reason']
    assert not client.calls


def test_large_document_uses_same_request_allowance_for_counting_and_generation(setup, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    config,directory,_=setup
    source=tmp_path/'large.log';source.write_bytes(b'fixture log line\n'*70000)
    assert source.stat().st_size>1024*1024
    client=Client(steps=[('tare_deliver',{'summary':'Read the fixture document.'})])
    sizes={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            size=int(self.headers['Content-Length']);sizes[self.path]=size
            body=json.loads(self.rfile.read(size))
            raw=json.dumps(client.post(self.path,body,None)).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        result=native_agent.run_session(config,deployment(endpoint=f'http://127.0.0.1:{server.server_port}'),
            directory,'Read the document.',attachment_files=(source,))
        assert result['status']=='PROPOSED' and client.generations==1
        assert sizes['/apply-template']>1024*1024 and sizes['/chat/completions']>1024*1024
    finally:server.shutdown();server.server_close();thread.join(5)

"""Physical gateway HTTP and backend fixtures; no GPU or model inference."""
from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
from threading import RLock, Thread, Event
from urllib import request, error

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tare_node.fleet_count import BINDING_FIELD, digest
spec = importlib.util.spec_from_file_location('fleet_gateway_fixture_module', ROOT/'tare_node/qualified_model_gateway.py')
gateway = importlib.util.module_from_spec(spec); spec.loader.exec_module(gateway)


@contextmanager
def serving():
    class Runtime:
        request_lock = RLock()
        model_id = 'one'
        template = 'template-v1'
        windows = [4096, 4096]
        identity_wrong = False
        calls = []
        generations = []
        config = {'fleet': {'default_model': 'one'}, 'aliases': {'coding': 'one'}, 'models': {
            name: {'artifact': {'path': '/models/'+name, 'sha256': name[0]*64},
                   'runtime': {'binary': '/bin/llama-server', 'args': ['--parallel', '2']}}
            for name in ('one', 'two')}}
        def ensure_model(self, requested):
            name, _ = gateway.resolve_model(self.config, requested)
            self.model_id = name
            return name, 0
        def backend_url(self, path):
            return f'http://127.0.0.1:{self.backend_port}{path}'
    runtime = Runtime()
    runtime.stream_continue = Event()
    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value):
            raw=json.dumps(value).encode(); self.send_response(200)
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            runtime.calls.append((self.path, runtime.model_id))
            if self.path == '/props':
                return self.reply({'model_path': '/models/'+runtime.model_id, 'total_slots': len(runtime.windows),
                    'model_alias': 'wrong' if runtime.identity_wrong else runtime.model_id,
                    'chat_template': runtime.template, 'build_info': 'fixture-build'})
            assert self.path == '/slots'
            self.reply([{'id': i, 'n_ctx': value} for i,value in enumerate(runtime.windows)])
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            runtime.calls.append((self.path, runtime.model_id))
            if self.path == '/apply-template':
                assert value['model'] == runtime.model_id
                return self.reply({'prompt': json.dumps(value['messages'])+runtime.template})
            if self.path == '/tokenize':
                assert value['add_special'] is True and value['parse_special'] is True
                return self.reply({'tokens': list(range(len(value['content'].split())))})
            assert self.path == '/v1/chat/completions' and BINDING_FIELD not in value
            runtime.generations.append(value)
            if value.get('stream'):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                def emit(data):self.wfile.write(b'data: '+json.dumps(data).encode()+b'\n\n');self.wfile.flush()
                emit({'model':runtime.model_id,'choices':[{'index':0,'delta':{'role':'assistant','content':'OK'},'finish_reason':None}]})
                runtime.stream_continue.wait(3)
                emit({'model':runtime.model_id,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})
                emit({'model':runtime.model_id,'choices':[],'usage':{'prompt_tokens':8,'completion_tokens':1}})
                self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush();return
            self.reply({'model': runtime.model_id, 'usage': {'prompt_tokens': 8, 'completion_tokens': 1},
                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'OK'}}]})
    backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    runtime.backend_host, runtime.backend_port = '127.0.0.1', backend.server_port
    old = getattr(gateway, 'RUNTIME', None); gateway.RUNTIME = runtime
    public = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
    threads = [Thread(target=server.serve_forever,daemon=True) for server in (backend,public)]
    for thread in threads:thread.start()
    runtime.endpoint=f'http://127.0.0.1:{public.server_port}/v1'
    try: yield runtime
    finally:
        runtime.stream_continue.set()
        for server in (public,backend):server.shutdown();server.server_close()
        for thread in threads:thread.join(timeout=2)
        gateway.RUNTIME=old


def post(runtime, path, body):
    req=request.Request(runtime.endpoint+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    try:
        with request.urlopen(req,timeout=5) as response:return response.status,json.load(response)
    except error.HTTPError as exc:
        return exc.code,json.load(exc)


def chat(model='coding'):
    return {'model':model,'messages':[{'role':'user','content':'Answer OK.'}],
            'max_tokens':8,'stream':False,'chat_template_kwargs':{'enable_thinking':True}}


def test_bound_stream_forwards_before_completion_and_keeps_count_identity():
    import time
    with serving() as runtime:
        wire=dict(chat(),stream=True,stream_options={'include_usage':True})
        _,count=post(runtime,'/fleet/count',{'model':'coding','request':wire})
        started=time.monotonic()
        req=request.Request(runtime.endpoint+'/chat/completions',data=json.dumps(wire|{BINDING_FIELD:count['binding']}).encode(),
                            headers={'Content-Type':'application/json'})
        with request.urlopen(req,timeout=5) as response:
            proof=json.loads(response.readline()[6:]);assert response.readline()==b'\n'
            delta=json.loads(response.readline()[6:]);assert response.readline()==b'\n'
            assert time.monotonic()-started<2
            runtime.stream_continue.set();rest=response.read()
        assert proof['tare_fleet_observation']['binding']==count['binding']
        assert delta['choices'][0]['delta']['content']=='OK' and b'[DONE]' in rest
        assert runtime.generations[0]['stream'] and runtime.generations[0]['model']=='one'


def test_bound_stream_changed_after_count_is_rejected_without_generation():
    with serving() as runtime:
        wire=dict(chat(),stream=True)
        _,count=post(runtime,'/fleet/count',{'model':'coding','request':wire})
        wire['max_tokens']=9
        status,_=post(runtime,'/chat/completions',wire|{BINDING_FIELD:count['binding']})
        assert status==409 and not runtime.generations


def test_count_selects_alias_and_generation_rechecks_same_binding():
    with serving() as runtime:
        wire=chat(); code,count=post(runtime,'/fleet/count',{'model':'coding','request':wire})
        assert code==200 and count['input_tokens']>0
        assert count['profile']['model']=='one' and count['profile']['context_window']==4096
        assert count['binding']['request_sha256']==digest(wire)
        assert len(count['backend_operations'])==4 and not runtime.generations
        runtime.model_id='two'  # Another client may switch away between requests.
        code,reply=post(runtime,'/chat/completions',wire|{BINDING_FIELD:count['binding']})
        assert code==200 and reply['model']=='one'
        assert reply['tare_fleet_observation']['binding']==count['binding']
        assert len(reply['tare_fleet_observation']['backend_operations'])==2
        assert runtime.generations[0]['model']=='one' and runtime.generations[0]['chat_template_kwargs']['enable_thinking'] is True


@pytest.mark.parametrize('change',['template','slots','body','alias','identity','build-config'])
def test_changed_binding_is_rejected_before_inference(change):
    with serving() as runtime:
        wire=chat();_,count=post(runtime,'/fleet/count',{'model':'coding','request':wire})
        if change=='template':runtime.template='changed'
        if change=='slots':runtime.windows=[2048,4096]
        if change=='body':wire['messages'][0]['content']='changed'
        if change=='alias':runtime.config=copy.deepcopy(runtime.config);runtime.config['aliases']['coding']='two'
        if change=='identity':runtime.identity_wrong=True
        if change=='build-config':runtime.config=copy.deepcopy(runtime.config);runtime.config['models']['one']['runtime']['args']=['--other']
        code,reply=post(runtime,'/chat/completions',wire|{BINDING_FIELD:count['binding']})
        assert code==409 and not runtime.generations


@pytest.mark.parametrize('body,status',[
    ({'request':chat()},400),({'model':'missing','request':chat('missing')},404),
    ({'model':'one','request':chat('two')},400),
    ({'model':'one','request':dict(chat('one'),messages=[{'role':'user','content':[{'type':'image_url'}]}])},400),
])
def test_invalid_count_does_not_use_default_model_or_inference(body,status):
    with serving() as runtime:
        code,_=post(runtime,'/fleet/count',body)
        assert code==status and not runtime.generations


def test_profile_is_live_per_slot_and_legacy_chat_still_works():
    with serving() as runtime:
        runtime.windows=[2048,4096]
        code,result=post(runtime,'/fleet/profile',{'model':'two'})
        assert code==200 and result['profile']['context_window']==2048
        code,result=post(runtime,'/chat/completions',chat())
        assert code==200 and result['model']=='one' and 'tare_fleet_observation' not in result

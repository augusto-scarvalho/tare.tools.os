"""Loopback wire fixture for OS streaming composition; no sibling test imports.

Adapted from the qualified Kernel streaming fixture. Only stdlib server/wire
construction lives here; the real installed Kernel client remains under test.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import json
import struct
import zlib


def aws(kind,value):
    headers=b''
    for key,text in [(':message-type','event'),(':event-type',kind),(':content-type','application/json')]:
        k,v=key.encode(),text.encode();headers+=bytes([len(k)])+k+b'\x07'+struct.pack('!H',len(v))+v
    payload=json.dumps(value).encode();pre=struct.pack('!II',16+len(headers)+len(payload),len(headers))
    frame=pre+struct.pack('!I',zlib.crc32(pre))+headers+payload
    return frame+struct.pack('!I',zlib.crc32(frame))


def sse(value):return b'data: '+json.dumps(value,ensure_ascii=False).encode()+b'\n\n'


def chat_parts(tool=False):
    if tool:
        deltas=[{'role':'assistant','tool_calls':[{'index':0,'id':'t1','type':'function','function':{'name':'tare_deliver','arguments':'{"summary":'}}]},
                {'tool_calls':[{'index':0,'function':{'arguments':'"olá"}'}}]}]
    else:deltas=[{'role':'assistant','content':'olá '},{'content':'mundo'}]
    return [*[sse({'model':'fixture','choices':[{'index':0,'delta':d,'finish_reason':None}]}) for d in deltas],
        sse({'model':'fixture','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if tool else 'stop'}]}),
        sse({'model':'fixture','choices':[],'usage':{'prompt_tokens':20,'completion_tokens':5}}),b'data: [DONE]\n\n']


def converse_parts(tool=False):
    parts=[aws('messageStart',{'role':'assistant'}),
        aws('contentBlockDelta',{'contentBlockIndex':0,'delta':{'reasoningContent':{'text':'retained'}}}),
        aws('contentBlockDelta',{'contentBlockIndex':0,'delta':{'reasoningContent':{'signature':'signature'}}}),
        aws('contentBlockStop',{'contentBlockIndex':0})]
    if tool:
        parts += [aws('contentBlockStart',{'contentBlockIndex':1,'start':{'toolUse':{'toolUseId':'t1','name':'tare_deliver'}}}),
            aws('contentBlockDelta',{'contentBlockIndex':1,'delta':{'toolUse':{'input':'{"summary":'}}}),
            aws('contentBlockDelta',{'contentBlockIndex':1,'delta':{'toolUse':{'input':'"olá"}'}}})]
    else:
        parts += [aws('contentBlockDelta',{'contentBlockIndex':1,'delta':{'text':t}}) for t in ('olá ','mundo')]
    return parts+[aws('contentBlockStop',{'contentBlockIndex':1}),
        aws('messageStop',{'stopReason':'tool_use' if tool else 'end_turn'}),
        aws('metadata',{'usage':{'inputTokens':20,'outputTokens':5,'cacheReadInputTokens':0,'cacheWriteInputTokens':0}})]


@contextmanager
def serving(protocol='openai-chat',tool=False,gate=False,cut=False):
    seen,release,first= [],Event(),Event()
    parts=chat_parts(tool) if protocol=='openai-chat' else converse_parts(tool)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append((self.path,body))
            if self.path.endswith('/count-tokens'):
                raw=json.dumps({'inputTokens':20}).encode();self.send_response(200);self.end_headers();self.wfile.write(raw);return
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream' if protocol=='openai-chat' else 'application/vnd.amazon.eventstream')
            self.end_headers()
            try:
                for i,part in enumerate(parts):
                    # Network write boundaries deliberately split UTF-8 and AWS frames.
                    for start in range(0,len(part),7):self.wfile.write(part[start:start+7]);self.wfile.flush()
                    if i==0:
                        first.set()
                        if gate:release.wait(5)
                    if cut and i==1:return
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port),seen,release,first
    finally:release.set();server.shutdown();server.server_close();thread.join(2)


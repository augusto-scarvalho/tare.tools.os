"""tare as a local OpenAI-compatible endpoint (Chat Completions) for editors such as VS Code Copilot.

  tare serve [--host 127.0.0.1] [--port 8790] [--model NAME=PROFILE ...] [--api-key-env VAR]

Each published model name maps to a tare profile. The client keeps its conversation and tools
(Copilot executes the tool calls in the editor); tare chooses the route, applies the profile's
qualified request settings and its coding guidance, and records usage in
<storage>/serve/usage.jsonl. Only OpenAI-chat profiles can pass client tools through.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

DEFAULT_MODELS = {'tare-qwen38': 'qwen38-gsq', 'tare-swift': 'swift27b'}
# Published names that run tare's own agent runtime instead of passing the client's tools through.
AGENT_MODELS = ('tare',)
# tare's routing and audit over the client's own tools (relay/copilot_mode.py).
COPILOT_MODELS = ('tare-copilot',)
# Client fields forwarded as sent; sampling and thinking come from the tare profile.
PASSTHROUGH = ('messages', 'tools', 'tool_choice', 'parallel_tool_calls', 'stream', 'stop',
               'response_format', 'max_tokens', 'max_completion_tokens', 'seed')
MAX_BODY = 32 * 1024 * 1024
# A request's own choice of local models (`tare` and `tare-copilot` routing, judges and explorers; a single local
# route is refused when false): atomic with the inference, unlike a chat's `/local-gpu off`. Never sent upstream.
LOCAL_MODELS_FIELD = 'tare_local_models'
_log_lock = threading.Lock()
CONTROL_PROTOCOL = 1
CONTROL_MAX_BODY = 4096
DRAIN_LEASE_SECONDS = 120


class Admission:
    """Volatile, per-server admission fence; never shares the usage-log lock.

    Control identity is (pid, release, installation); installation is SHA-256 of
    the normalized resolved managed home, or null for an unmanaged endpoint.
    A ticket counts once until BOTH its handler and any agent worker finish.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.pid = os.getpid()
        self.release = os.environ.get('TARE_RELEASE_ID')
        home = os.environ.get('TARE_MANAGED_HOME')
        self.home = Path(home).resolve() if home else None
        self.installation = (hashlib.sha256(os.path.normcase(str(self.home)).encode('utf-8')).hexdigest()
                             if self.home else None)
        self.busy = 0
        self.owner = None
        self.deadline = 0
        self.stopping = False

    def selected(self):
        if self.home is None:
            return True
        try:
            active = json.loads((self.home/'active.json').read_text(encoding='utf-8'))
            return bool(self.release) and active.get('current') == self.release
        except (OSError, ValueError, AttributeError):
            return False

    def require_selected(self):
        if not self.selected():
            raise ValueError('managed Serve release is no longer selected by active.json')

    def expire(self):
        # Caller holds lock. Stop is terminal even after the lease expires.
        if self.owner is not None and time.monotonic() >= self.deadline:
            self.owner = None
            self.deadline = 0

    def snapshot(self):
        with self.lock:
            self.expire()
            return self.snapshot_locked()

    def snapshot_locked(self):
        return {'status': 'ok', 'pid': self.pid, 'release': self.release,
                'installation': self.installation, 'busy': self.busy,
                'control_protocol': CONTROL_PROTOCOL,
                'draining': self.owner is not None or self.stopping, 'stopping': self.stopping}

    def admit(self):
        with self.lock:
            self.expire()
            if self.owner is not None or self.stopping or not self.selected():
                return None
            self.busy += 1
            return WorkTicket(self)

    def control(self, body):
        if not isinstance(body, dict):
            return 400, {'error': 'control body must be an object'}
        try:
            owner = str(uuid.UUID(body['owner']))
        except (KeyError, ValueError, TypeError, AttributeError):
            return 400, {'error': 'owner must be a UUID'}
        action = body.get('action')
        if action not in ('drain', 'resume', 'stop'):
            return 400, {'error': 'unknown control action'}
        with self.lock:
            if (type(body.get('pid')) is not int or body['pid'] != self.pid
                    or 'release' not in body or body['release'] != self.release
                    or 'installation' not in body or body['installation'] != self.installation):
                return 409, {'error': 'Serve identity mismatch'}
            self.expire()
            if self.stopping or (self.owner is not None and self.owner != owner):
                return 409, {'error': 'Serve is owned by another transition or stopping'}
            if action == 'drain':
                self.owner = owner
                self.deadline = time.monotonic() + DRAIN_LEASE_SECONDS
            elif self.owner != owner:
                return 409, {'error': 'a valid lease owned by this attempt is required'}
            elif action == 'resume':
                self.owner = None
                self.deadline = 0
            elif self.busy:
                return 409, {'error': 'Serve is busy'}
            else:
                self.stopping = True
            return 200, {**self.snapshot_locked(), 'action': action}


class WorkTicket:
    def __init__(self, admission):
        self.admission = admission
        self.refs = 1

    def retain(self):
        with self.admission.lock:
            self.refs += 1

    def done(self):
        with self.admission.lock:
            self.refs -= 1
            if not self.refs:
                self.admission.busy -= 1


def resolve(models, name):
    """The concrete tare deployment behind a published model name."""
    from .native_catalog import select, resolve_host_mode
    if name not in models:
        raise KeyError(name)
    deployment = resolve_host_mode(select(models[name]))
    profile = deployment['native_profile']
    if profile['protocol'] != 'openai-chat':
        raise ValueError(f'{name} uses {profile["protocol"]}; only OpenAI-chat routes can carry client tools')
    return deployment


def guidance(deployment):
    """tare's working rules for a client's conversation: the serve capsule (relay/prompt_capsules.json)."""
    from .guidance import fill, values
    from .native_agent import prompt_capsule
    return fill(prompt_capsule(deployment, '*', 'serve').get('system', ''),  # variables (/guidance)
                values(model_id=(deployment.get('native_profile') or {}).get('model'), model_name=deployment.get('model')))


def with_guidance(messages, text):
    """Add tare's guidance `text` to the first system message (or insert one); never mutates the input."""
    if not text:
        return messages
    messages = [dict(m) for m in messages]
    if messages and messages[0].get('role') in ('system', 'developer'):
        content = messages[0].get('content')
        if isinstance(content, list):
            messages[0]['content'] = content + [{'type': 'text', 'text': text}]
        else:
            messages[0]['content'] = (content or '') + '\n\n' + text
    else:
        messages.insert(0, {'role': 'system', 'content': text})
    return messages


def upstream_request(deployment, body):
    profile = deployment['native_profile']
    request = json.loads(profile.get('parameters_json') or '{}')
    request.update({k: body[k] for k in PASSTHROUGH if k in body})
    request['model'] = profile['model']
    request['messages'] = with_guidance(body['messages'], guidance(deployment))
    cap = profile.get('output_tokens')
    for key in ('max_tokens', 'max_completion_tokens'):
        if cap and isinstance(request.get(key), int):
            request[key] = min(request[key], cap)
    if request.get('stream'):
        request['stream_options'] = {'include_usage': True}
    return request


def credential(deployment):
    name = deployment.get('native_credential_env')
    if not name:
        return None
    from .native_generation import vault_secret
    value = os.environ.get(name) or vault_secret(name)
    if not value:
        raise PermissionError(f'credential {name} is not available')
    return value


def record(row):
    from .native_installation import storage_root
    path = storage_root()/'serve'/'usage.jsonl'
    with _log_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def handler(models, api_key, default_root=None, admission=None):
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        @property
        def admission(self):
            # The same Handler class may be used by multiple HTTP servers.
            with state_lock:
                if not hasattr(self.server, '_tare_admission'):
                    self.server._tare_admission = admission if admission is not None else Admission()
                return self.server._tare_admission

        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            pass

        def reply(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            if self.close_connection:
                self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def error(self, status, message, kind='invalid_request_error'):
            self.reply(status, {'error': {'message': message, 'type': kind}})

        def discard_body(self):
            """Read a request body before refusing it: unread bytes become the next keep-alive request, and on
            Windows closing a socket with unread bytes resets it, so the client never sees the refusal."""
            if self.headers.get('Transfer-Encoding'):
                self.close_connection = True
                return
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) > 1:
                self.close_connection = True
                return
            try:
                length = int(lengths[0]) if lengths else 0
            except ValueError:
                length = -1
            limit = CONTROL_MAX_BODY if self.path.rstrip('/') == '/control/drain' else MAX_BODY
            if 0 < length <= limit:
                self.rfile.read(length)
            elif length:
                self.close_connection = True

        def authorized(self):
            if not api_key:
                return True
            if self.headers.get('Authorization', '') == 'Bearer ' + api_key:
                return True
            self.discard_body()
            self.error(401, 'invalid api key', 'authentication_error')
            return False

        def do_GET(self):  # noqa: N802
            if not self.authorized():
                return
            if self.path.rstrip('/') in ('/v1/models', '/models'):
                from .native_catalog import select
                data = [{'id': name, 'object': 'model', 'owned_by': 'tare', 'context_window': 1000000}
                        for name in (*AGENT_MODELS, *COPILOT_MODELS)]
                for name, target in models.items():
                    try:
                        deployment = select(target)
                        window = deployment['native_profile'].get('context_window')
                    except (ValueError, OSError, KeyError):
                        continue
                    from .challenger import local
                    data.append({'id': name, 'object': 'model', 'owned_by': 'tare', 'context_window': window,
                                 'tare_local': local(deployment)})
                return self.reply(200, {'object': 'list', 'data': data})
            if self.path.rstrip('/') in ('/health', '/v1/health'):
                return self.reply(200, self.admission.snapshot())
            self.error(404, 'not found', 'not_found')

        def do_POST(self):  # noqa: N802
            if not self.authorized():
                return
            if self.path.rstrip('/') == '/control/drain':
                return self.control()
            if self.path.rstrip('/') not in ('/v1/chat/completions', '/chat/completions'):
                self.discard_body()
                return self.error(404, 'not found', 'not_found')
            ticket = self.admission.admit()
            if ticket is None:
                self.discard_body()
                return self.reply(503, {'error': {'message': 'Serve is draining or no longer selected; retry',
                                                 'type': 'serve_unavailable', 'retryable': True}})
            self._work = ticket
            try:
                return self.chat()
            finally:
                ticket.done()

        def control(self):
            try:
                loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                self.discard_body()
                return self.error(403, 'control requires a loopback peer')
            lengths = self.headers.get_all('Content-Length', [])
            try:
                length = int(lengths[0]) if len(lengths) == 1 else -1
            except ValueError:
                length = -1
            if self.headers.get('Transfer-Encoding') or not 0 < length <= CONTROL_MAX_BODY:
                self.close_connection = True
                return self.error(413 if length > CONTROL_MAX_BODY else 400, 'invalid control body framing')
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                return self.error(400, 'invalid control JSON')
            status, payload = self.admission.control(body)
            stopping = status == 200 and payload['action'] == 'stop'
            try:
                self.reply(status, payload)
                self.wfile.flush()
            finally:
                if stopping:
                    self.close_connection = True
                    # shutdown must not run on the serve_forever thread; no process killing.
                    threading.Thread(target=self.server.shutdown, daemon=True).start()

        def chat(self):
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > MAX_BODY:
                return self.error(413 if length > MAX_BODY else 400, 'request body size is not accepted')
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
                    raise ValueError('messages must be a list')
                if not isinstance(body.get(LOCAL_MODELS_FIELD, False), bool):
                    raise ValueError(LOCAL_MODELS_FIELD + ' must be true or false')
                if os.environ.get('TARE_SERVE_CAPTURE'):
                    # Opt-in diagnostics: keep the last request as sent by the client.
                    from .native_installation import storage_root
                    capture = storage_root()/'serve'/'last-request.json'
                    capture.parent.mkdir(parents=True, exist_ok=True)
                    capture.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding='utf-8')
                if body.get('model') in AGENT_MODELS:
                    return self.agent(body)
                if body.get('model') in COPILOT_MODELS:
                    from .copilot_mode import serve
                    return serve(self, body, default_root)
                deployment = resolve(models, body.get('model'))
                from .challenger import local
                if body.get(LOCAL_MODELS_FIELD) is False and local(deployment):
                    return self.error(409, f'{body.get("model")} runs on the local GPU and this request allows no '
                                           'local models', 'local_models_refused')
                request = upstream_request(deployment, body)
                key = credential(deployment)
            except KeyError:
                return self.error(404, f'unknown model {body.get("model")!r}; see /v1/models', 'model_not_found')
            except (ValueError, TypeError, PermissionError) as exc:
                return self.error(400, str(exc)[:500])
            self.forward(body, deployment, request, key)

        def agent(self, body):
            """Run the turn in tare's runtime; stream its progress as chat.completion chunks."""
            from .serve_agent import run_turn
            events, started, name = queue.Queue(), time.monotonic(), body['model']
            reply = {'answer': ''}

            ticket = self._work

            def work():
                try:
                    try:
                        reply['answer'] = run_turn(body['messages'], events.put, default_root,
                                                   local_models=body.get(LOCAL_MODELS_FIELD))
                    except Exception as exc:  # the client must get an answer, not a dropped stream
                        reply['answer'] = f'tare error: {type(exc).__name__}: {str(exc)[:800]}'
                finally:
                    events.put(None)
                    ticket.done()
            ticket.retain()
            try:
                threading.Thread(target=work, daemon=True).start()
            except BaseException:
                ticket.done()
                raise
            ident, created = 'chatcmpl-tare-' + uuid.uuid4().hex, int(time.time())

            def chunk(delta, finish=None):
                return ('data: ' + json.dumps({'id': ident, 'object': 'chat.completion.chunk', 'created': created,
                        'model': name, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})
                        + '\n\n').encode()
            parts = []
            if body.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                if isinstance(body.get(LOCAL_MODELS_FIELD), bool):  # the policy this request runs under
                    from .turn_engine import leaves_gpu_to_others
                    allowed = body[LOCAL_MODELS_FIELD] and not leaves_gpu_to_others()
                    self.send_header('X-Tare-Local-Models', 'on' if allowed else 'off')
                self.end_headers()
                self.wfile.write(chunk({'role': 'assistant', 'content': ''}))
            while True:
                try:
                    item = events.get(timeout=15)
                except queue.Empty:
                    if body.get('stream'):
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                    continue
                if item is None:
                    break
                parts.append(item)
                if body.get('stream'):
                    self.wfile.write(chunk({'content': item}))
                    self.wfile.flush()
            if reply['answer']:
                parts.append(reply['answer'])
                if body.get('stream'):
                    self.wfile.write(chunk({'content': reply['answer']}))
            if body.get('stream'):
                self.wfile.write(chunk({}, 'stop') + b'data: [DONE]\n\n')
                self.close_connection = True
            else:
                self.reply(200, {'id': ident, 'object': 'chat.completion', 'created': created, 'model': name,
                                 'choices': [{'index': 0, 'finish_reason': 'stop',
                                              'message': {'role': 'assistant', 'content': ''.join(parts)}}]})
            record({'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'model': name, 'agent': True,
                    'seconds': round(time.monotonic() - started, 2), 'stream': bool(body.get('stream'))})

        def forward(self, body, deployment, request, key):
            from .challenger import client_headers
            endpoint = urlparse(deployment['native_profile']['endpoint'].rstrip('/') + '/chat/completions')
            connection = (http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection)(
                endpoint.hostname, endpoint.port, timeout=900)
            headers = {'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + key} if key else {}),
                       **client_headers(deployment)}
            started, usage, status = time.monotonic(), None, None
            try:
                connection.request('POST', endpoint.path, body=json.dumps(request).encode(), headers=headers)
                response = connection.getresponse()
                status = response.status
                streaming = response.getheader('Content-Type', '').startswith('text/event-stream')
                self.send_response(status)
                self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                if streaming:
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    while line := response.readline():
                        self.wfile.write(line)
                        self.wfile.flush()
                        if line.startswith(b'data: {') and b'"usage"' in line:
                            try:
                                usage = json.loads(line[6:]).get('usage') or usage
                            except ValueError:
                                pass
                    self.close_connection = True
                else:
                    raw = response.read()
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    try:
                        usage = json.loads(raw).get('usage')
                    except ValueError:
                        pass
            except OSError as exc:
                if status is None:
                    self.error(502, f'model route unavailable: {type(exc).__name__}', 'upstream_error')
            finally:
                connection.close()
                record({'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'model': body.get('model'),
                        'deployment': deployment['id'], 'backend_model': deployment['native_profile']['model'],
                        'status': status, 'stream': bool(request.get('stream')), 'tools': len(body.get('tools') or []),
                        'seconds': round(time.monotonic() - started, 2), 'usage': usage,
                        **({'host_selection': deployment['native_host_selection']}
                           if 'native_host_selection' in deployment else {})})

    return Handler


def discover_clis():
    """Offer the installed, signed-in vendor CLIs as routes (the same discovery the TUI runs)."""
    try:
        from .cli_discovery import discover, summary
        line = summary(discover())
        print('tare serve: ' + (line or 'vendor CLIs unchanged'), flush=True)
    except Exception as exc:  # discovery is optional; the local and API routes keep working
        print(f'tare serve: CLI discovery failed: {type(exc).__name__}: {str(exc)[:200]}', flush=True)
    try:  # the models.dev reference prices, at most once a day; no model call, no key sent
        from .provider_prices import refresh_reading
        refresh_reading()
    except Exception:  # optional: the last good reading stays, or none (no reference price, as before)
        pass
    try:  # what each provider has left, so a spent plan is not chosen; no model call
        from . import provider_quota
        provider_quota.refresh()
    except Exception:  # optional: without a reading a spent plan is learned from the failed call, as before
        pass


def default_models():
    """The local routes published by default: the shipped ones this machine's setup offers, and its generic local
    profiles (a model its gateway publishes that no shipped profile covers) as tare-<profile>."""
    from .environment import listed, read
    from .native_catalog import select
    setup = read()
    models = {}
    for name, target in DEFAULT_MODELS.items():
        try:
            if listed(target, select(target), setup):
                models[name] = target
        except (ValueError, OSError, KeyError, TypeError):
            continue
    if 'error' not in setup:  # the generic profiles tare setup wrote
        models.update({'tare-' + name[len('local-'):]: name for name in setup.get('managed') or {}})
    return models


def parse_models(specs):
    models = default_models() if not specs else {}
    for spec in specs or ():
        name, _, target = spec.partition('=')
        if not name or not target:
            raise ValueError('use --model NAME=PROFILE')
        models[name] = target
    return models


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare serve', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--model', action='append', help='NAME=PROFILE; repeat. Default: '
                        + ', '.join(f'{k}={v}' for k, v in DEFAULT_MODELS.items()))
    parser.add_argument('--api-key-env', help='Require this environment variable as the bearer key')
    parser.add_argument('--root', help='Folder the tare agent uses when the client names no open workspace')
    parser.add_argument('--no-discovery', action='store_true', help='Do not look for Claude/Codex/AGY CLIs at startup')
    args = parser.parse_args(argv)
    os.environ.setdefault('TARE_CLIENT', 'tare.tools serve')  # who calls a local gateway
    models = parse_models(args.model)
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    if args.api_key_env and not api_key:
        print(f'{args.api_key_env} is not set', file=sys.stderr)
        return 2
    admission = Admission()
    try:
        admission.require_selected()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.no_discovery:
        threading.Thread(target=discover_clis, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), handler(models, api_key, args.root, admission))
    print(f'tare serve on http://{args.host}:{args.port}/v1 models: {", ".join((*AGENT_MODELS, *COPILOT_MODELS, *models))}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""tare (copilot): the editor runs the tools; tare picks the route and audits each user message.

VS Code Copilot sends its own tools and the route answers with tool calls that VS Code applies: Keep/Undo on
edits, the user's approval on every command, its own checkpoints. tare chooses the route by its policy once per
user message (only OpenAI-chat routes can carry client tools) and audits the net change of that message from
shadow-git snapshots taken when it arrives and when the model answers without tool calls. A REVISE gets one fix
round, asked of the model before the answer ends; the fix is audited against those findings, with no second
round. Side requests (titles, summaries: not streamed) go to the first route unaudited.

Escalation keeps the prompt cache: a chat has a floor tier that only rises (a route error before anything was
streamed retries the call one tier up; a fix that fails its audit again moves the next message up), so it never
flips back to a cheaper route, and the context tare adds to a user message is kept on that same message in every
later request, so the history a route receives only grows at its end.
"""
import contextvars
import hashlib
import http.client
import json
import os
import re
import queue
import threading
import time
import uuid
from urllib.parse import urlparse

from . import turn_engine as engine
from .serve_agent import chat_key, text_of, user_requests, workspace_root  # VS Code chat protocol

NAME = 'tare-copilot'
_turns, _chats, _guard = {}, {}, threading.Lock()


def routes(start=0):
    """(tier, name, deployment) of the policy's available routes that carry client tools, from tier `start`."""
    for tier, names in enumerate(engine.policy()['implementer']):
        if tier < start:
            continue
        for name in names:
            deployment, _ = engine.available(name)
            if deployment is not None and deployment['native_profile']['protocol'] == 'openai-chat':
                yield tier, name, deployment


def route_from(floor):
    """(tier, name, deployment) of the first route at or above the chat's floor, else the strongest one left."""
    found = next(routes(floor), None)
    if found is None:
        found = next(iter(reversed(list(routes()))), None)
    return found or (floor, None, None)


def request_key(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def turn_for(messages, root):
    """The user message a request serves (route, start snapshot, fix round), kept across its tool calls."""
    requests = user_requests(messages)
    key = chat_key(root, requests[0] if requests else '')
    with _guard:
        chat = _chats.setdefault(key, {'floor': 0, 'contexts': {}})
        turn = _turns.get(key)
        if turn is None or turn['requests'] != len(requests):
            tier, name, _ = route_from(chat['floor'])
            start, why = engine.snapshot(root)
            turn = _turns[key] = {'requests': len(requests), 'route': name, 'tier': tier, 'start': start, 'why': why,
                                  'request': requests[-1] if requests else '',
                                  'conversation': engine.conversation_request(requests), 'fix': None, 'fix_start': None,
                                  'chat': chat}
    return turn


CLIENT_RUNS = re.compile(r'terminal|test|task|command|shell', re.I)


def client_evidence(messages):
    """Commands and test runs VS Code executed for the current user message, with what it returned: evidence the
    client observed, for the audit (tare did not run them, so they are not host receipts)."""
    start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'
                 and '<userRequest>' in text_of(m)), default=0)
    results = {m.get('tool_call_id'): text_of(m) for m in messages[start:] if m.get('role') == 'tool'}
    rows = []
    for message in messages[start:]:
        for call in message.get('tool_calls') or []:
            function = call.get('function') or {}
            if not CLIENT_RUNS.search(function.get('name') or ''):
                continue
            try:
                args = json.loads(function.get('arguments') or '{}')
            except ValueError:
                args = {}
            command = args.get('command') if isinstance(args, dict) else None
            rows.append({'argv': [command or f"{function.get('name')} {json.dumps(args)[:200]}"],
                         'status': 'ran in VS Code', 'exit_code': None,
                         'output': engine.clip(results.get(call.get('id'), ''), 2000)})
    return rows


def rounds_of(messages):
    """The rounds of the current user message: each assistant reply with tool calls and what VS Code returned."""
    start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'
                 and '<userRequest>' in text_of(m)), default=0)
    results = {m.get('tool_call_id'): text_of(m) for m in messages[start:] if m.get('role') == 'tool'}
    return [[((call.get('function') or {}).get('name'), (call.get('function') or {}).get('arguments'),
              results.get(call.get('id'))) for call in m['tool_calls']]
            for m in messages[start:] if m.get('role') == 'assistant' and m.get('tool_calls')]


def gather(turn, messages, deployment):
    """Linked pages and, for a route that cannot see, image descriptions: gathered once per user message."""
    contexts = turn['chat']['contexts']
    if request_key(turn['request']) in contexts:
        return []
    progress = []
    context = engine.read_pages(turn['request'], progress.append)
    if not deployment.get('native_image_media_types'):
        context += engine.describe_images(engine.images_in(messages), progress.append)
    contexts[request_key(turn['request'])] = context
    return progress


def with_context(body, turn, deployment):
    """The request as the route should get it: the gathered context on the user message, and no image parts
    for a route that cannot see them (their descriptions are in that context)."""
    blind = not deployment.get('native_image_media_types')
    contexts = turn['chat']['contexts']
    shaped = []
    for message in body['messages']:
        content = message.get('content')
        if blind and isinstance(content, list):
            content = [p for p in content if not (isinstance(p, dict) and p.get('type') == 'image_url')]
        found = message.get('role') == 'user' and re.search(r'<userRequest>\s*(.*?)\s*</userRequest>',
                                                             text_of(message), re.S)
        context = contexts.get(request_key(found.group(1))) if found else None
        if context:  # on its own message in every request: the history only grows at its end
            content = (content + [{'type': 'text', 'text': context}] if isinstance(content, list)
                       else (content or '') + context)
        shaped.append({**message, 'content': content} if content is not message.get('content') else message)
    return {**body, 'messages': shaped}


def upstream_lines(deployment, request):
    from .challenger import client_headers
    from .native_serve import credential
    endpoint = urlparse(deployment['native_profile']['endpoint'].rstrip('/') + '/chat/completions')
    key = credential(deployment)
    connection = (http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection)(
        endpoint.hostname, endpoint.port, timeout=900)
    try:
        connection.request('POST', endpoint.path, body=json.dumps(request).encode(),
                           headers={'Content-Type': 'application/json',
                                    **({'Authorization': 'Bearer ' + key} if key else {}),
                                    **client_headers(deployment)})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f'route answered HTTP {response.status}: {response.read(300)!r}')
        while line := response.readline():
            yield line
    finally:
        connection.close()


class RouteFailed(Exception):
    """A route failed before anything reached the client: the same call can go to another route."""


def relay(deployment, body, write):
    """Stream one model call to the client, holding back its end: (text, tool calls seen, finish reason)."""
    from .native_serve import upstream_request
    text, calls, finish = [], False, None
    try:
        lines = upstream_lines(deployment, upstream_request(deployment, body))
        first = next(lines, None)  # the request is sent here: nothing has reached the client yet
    except (OSError, ValueError) as exc:
        raise RouteFailed(f'{type(exc).__name__}: {str(exc)[:200]}') from exc
    for line in _chain(first, lines):
        if not line.startswith(b'data: {'):
            continue  # [DONE] and blank separators: tare ends the stream itself
        chunk = json.loads(line[6:])
        for choice in chunk.get('choices') or []:
            delta = choice.get('delta') or {}
            text.append(delta.get('content') or '')
            calls = calls or bool(delta.get('tool_calls'))
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
                if finish == 'stop':  # the answer may still get an audit and a fix round
                    choice['finish_reason'] = None
        write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
    return ''.join(text), calls, finish


def _chain(first, rest):
    if first is not None:
        yield first
    yield from rest


def audit(turn, root, deployment, say, keepalive, evidence=()):
    """Audit what the message (or its fix round) changed; the fix instruction when one is due. A fix that fails
    its audit again moves the chat's next message one tier up (engine.successor's cheap signal for Copilot)."""
    start = turn['fix_start'] or turn['start']
    after, _ = engine.snapshot(root)
    if not (start and after):
        return None
    files = engine.tree_changes(root, start, after)
    if not files:
        return None
    events, outcome = queue.Queue(), {}

    def work():
        try:
            outcome['record'], outcome['fix'] = engine.audit_turn(
                files, root, turn['fix'] or turn['conversation'], deployment, events.put, checks=list(evidence))
        finally:
            events.put(None)
    threading.Thread(target=work, daemon=True).start()
    while True:
        try:
            item = events.get(timeout=15)
        except queue.Empty:
            keepalive()
            continue
        if item is None:
            break
        say(item)
    fix = outcome.get('fix')
    if turn['fix'] and (outcome.get('record') or {}).get('verdict') == 'REVISE':
        turn['chat']['floor'] = max(turn['chat']['floor'], turn['tier'] + 1)
        say('\n_tare-copilot: the fix did not pass its audit; the next message starts on a stronger route._\n')
    if fix and not turn['fix']:
        turn['fix'], turn['fix_start'] = fix, after
        return fix
    return None


def heads_up(turn, messages, text, say, keepalive):
    """The message's heads-up notes (relay/heads_up.py), said in the stream: VS Code keeps them in the history the
    model reads next, so the user's "check that" reaches it. Kept alive like the audit while the funnel runs."""
    from . import heads_up as funnel
    if not funnel.enabled():
        return
    chat, done = turn['chat'], {}
    worker = threading.Thread(target=lambda: done.update(new=funnel.notes(  # the user's words, not VS Code's wrapper
        {**funnel.from_messages(messages, text), 'request': turn['request']}, seen=chat.get('heads_up', []),
        observed={'source': 'copilot', 'route': turn['route']})), daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(15)
        if worker.is_alive():
            keepalive()
    new = done.get('new') or []
    if new:
        chat['heads_up'] = chat.get('heads_up', []) + new
        say(funnel.shown_text(new))


def stream(handler, local_models=None):
    """Open the client's event stream: write(raw) and chunk(delta, finish). `local_models`: the policy the
    request runs under, echoed as X-Tare-Local-Models."""
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Connection', 'close')
    if local_models is not None:
        handler.send_header('X-Tare-Local-Models', 'on' if local_models else 'off')
    handler.end_headers()
    ident, created = 'chatcmpl-tare-' + uuid.uuid4().hex, int(time.time())

    def write(raw):
        handler.wfile.write(raw)
        handler.wfile.flush()

    def chunk(delta, finish=None):
        write(b'data: ' + json.dumps({'id': ident, 'object': 'chat.completion.chunk', 'created': created,
                                      'model': NAME, 'choices': [{'index': 0, 'delta': delta,
                                                                  'finish_reason': finish}]}).encode() + b'\n\n')
    return write, chunk


def serve(handler, body, default_root):
    """One request, in its own context: a chat's choice about local models (engine.LOCAL_MODELS) stays in it."""
    return contextvars.copy_context().run(_serve, handler, body, default_root)


def chat_choices():
    """Chats' `/local-gpu` choices, kept on disk so tare serve's restart after each update does not drop them."""
    from .native_installation import storage_root
    return storage_root()/'serve'/'copilot-chats.json'


def saved_choice(key):
    try:
        value = json.loads(chat_choices().read_text(encoding='utf-8')).get(key)
    except (OSError, ValueError, AttributeError):
        return {}
    return {'local_models': value} if isinstance(value, bool) else {}


def save_choice(key, allowed):
    """Under _guard. ponytail: one entry per chat that used /local-gpu, never pruned; prune by age if it grows."""
    path = chat_choices()
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    saved = {**(saved if isinstance(saved, dict) else {}), key: allowed}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(saved), encoding='utf-8')
    os.replace(temporary, path)


def _serve(handler, body, default_root):
    from .native_serve import LOCAL_MODELS_FIELD, credential, record, upstream_request
    if isinstance(body.get(LOCAL_MODELS_FIELD), bool):  # the request's own choice, before any route is picked
        engine.LOCAL_MODELS.set(body[LOCAL_MODELS_FIELD])
    root = workspace_root(body['messages'], default_root)
    started = time.monotonic()
    if root is None or not body.get('stream'):
        _, name, deployment = next(routes(), (None, None, None))
        if deployment is None:
            return handler.error(503, 'tare-copilot: no route that carries client tools is available')
        return handler.forward(body, deployment, upstream_request(deployment, body), credential(deployment))
    requests = user_requests(body['messages'])
    key = chat_key(root, requests[0] if requests else '')
    with _guard:
        chat = _chats.get(key)
        if chat is None:  # a chat this serve has not seen yet: its saved choice, if it made one
            chat = _chats[key] = {'floor': 0, 'contexts': {}, **saved_choice(key)}
    local = (engine.local_gpu_command(requests[-1], chat)
             if requests and body['messages'][-1].get('role') == 'user' else None)
    if local is not None:  # `/local-gpu`: tare answers it, no route is called
        if 'local_models' in chat:
            with _guard:
                save_choice(key, chat['local_models'])
        write, chunk = stream(handler)
        # ponytail: VS Code sends the message as typed, so a request after the command waits for the next message
        chunk({'content': local[0] + ('_Send the request in a new message._\n' if local[1] else '')})
        chunk({}, 'stop')
        write(b'data: [DONE]\n\n')
        handler.close_connection = True
        return
    allowed = body.get(LOCAL_MODELS_FIELD) if isinstance(body.get(LOCAL_MODELS_FIELD), bool) else chat.get(
        'local_models', True)
    engine.LOCAL_MODELS.set(allowed)
    turn = turn_for(body['messages'], root)
    deployment = engine.available(turn['route'])[0] if turn['route'] else None
    if deployment is None:  # the route went away mid-message: the next one serves the rest
        turn['tier'], turn['route'], deployment = route_from(turn['chat']['floor'])
    if deployment is None:
        return handler.error(503, 'tare-copilot: no route that carries client tools is available')
    progress = gather(turn, body['messages'], deployment)
    write, chunk = stream(handler, allowed and not engine.leaves_gpu_to_others())
    said = []
    say = lambda text: (said.append(text), chunk({'content': text}))
    for line in progress:
        chunk({'content': line})

    def call(request):
        """One model call; a route that fails before anything reached the client hands it one tier up, and that
        tier becomes the chat's floor (the chat never flips back, which would throw its prompt cache away)."""
        nonlocal deployment
        while True:
            try:
                return relay(deployment, with_context(request, turn, deployment), write)
            except RouteFailed as exc:
                failed = turn['route']
                tier, name, successor = next(routes(turn['tier'] + 1), (None, None, None))
                if successor is None:
                    raise ValueError(f'{failed} failed ({exc}) and no stronger route is available') from exc
                turn.update(route=name, tier=tier)
                turn['chat']['floor'] = max(turn['chat']['floor'], tier)
                deployment = successor
                chunk({'content': f'\n_tare-copilot: {failed} failed ({exc}); continuing on {name}._\n'})
    # The runtime's no-progress rule over the rounds VS Code ran for this message: stuck moves the chat up once;
    # stuck on the strongest route ends VS Code's loop with an answer instead of another model call.
    flags = engine.round_progress(rounds_of(body['messages']))
    stuck = engine.stagnant_rounds(flags[turn.get('stuck_at', 0):]) >= engine.MAX_STAGNANT_ROUNDS
    if stuck:
        turn['stuck_at'] = len(flags)
        tier, name, successor = next(routes(turn['tier'] + 1), (None, None, None))
        if successor is not None:
            chunk({'content': f"\n_tare-copilot: no progress in {engine.MAX_STAGNANT_ROUNDS} rounds on "
                              f"{turn['route']} (the same calls returned the same results); continuing on {name}._\n"})
            turn.update(route=name, tier=tier)
            turn['chat']['floor'] = max(turn['chat']['floor'], tier)
            deployment = successor
    try:
        if stuck and successor is None:
            say(f"\n_tare-copilot: no progress in {engine.MAX_STAGNANT_ROUNDS} rounds, and {turn['route']} is the "
                f"strongest route available: stopping here instead of looping. Rephrase the request, narrow it, "
                f"or say what to try next._\n")
            text, calls, finish = '', False, 'stop'
        else:
            text, calls, finish = call(body)
        if (not calls and finish == 'stop' and not (stuck and successor is None) and not turn.get('judged')
                and text.strip()):
            # A reply that only announced the work moves the chat one tier up, once per message; the history is
            # the continuity, as in the fix round.
            turn['judged'] = True
            up = next(routes(turn['tier'] + 1), None)
            if up is not None and engine.unanswered(turn['request'], text, {'source': 'copilot', 'route': turn['route']}):
                failed = turn['route']
                turn['tier'], turn['route'], deployment = up
                turn['chat']['floor'] = max(turn['chat']['floor'], turn['tier'])
                chunk({'content': f"\n_tare-copilot: the reply on {failed} only announced the work; "
                                  f"continuing on {turn['route']}._\n"})
                messages = body['messages'] + [{'role': 'assistant', 'content': text},
                                               {'role': 'user', 'content': engine.UNANSWERED_CONTINUE}]
                text, calls, finish = call({**body, 'messages': messages})
        if not calls and finish == 'stop' and not (stuck and successor is None):
            if turn['start'] is None and not turn['fix']:
                say(f"\n\n_tare-copilot: this message's changes cannot be audited ({turn['why']})._\n")
            fix = audit(turn, root, deployment, say, lambda: write(b': keepalive\n\n'),
                        client_evidence(body['messages']))
            if fix:  # one fix round: the model continues; VS Code runs its tool calls as usual
                messages = body['messages'] + [{'role': 'assistant', 'content': text + ''.join(said)},
                                               {'role': 'user', 'content': fix}]
                text, calls, finish = call({**body, 'messages': messages})
            else:  # the message is finished (a fix round ends in a later request, with its tool calls in history)
                heads_up(turn, body['messages'], text, lambda note: chunk({'content': note}),
                         lambda: write(b': keepalive\n\n'))
    except (OSError, ValueError) as exc:
        say(f'\n\n_tare-copilot: {type(exc).__name__}: {str(exc)[:300]}_\n')
        finish = 'stop'
    if finish != 'tool_calls':
        chunk({}, 'stop')
    write(b'data: [DONE]\n\n')
    handler.close_connection = True
    record({'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'model': NAME, 'route': turn['route'],
            'seconds': round(time.monotonic() - started, 2), 'finish': finish, 'audited': bool(said)})

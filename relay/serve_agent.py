"""tare's agent runtime behind an OpenAI-compatible model name (used by `tare serve`).

The client (VS Code Copilot) supplies the chat; tare runs its own session on the open workspace with
its tools, streams progress and returns the answer. Each client chat maps to a tare conversation
session; a new user message is a follow-up. Routes follow the operator policy: local first, then
cheap, then intermediate API price (subscription quota counts at API price). A turn that ends
blocked, exhausted or failed, or a route that stops being available, moves the conversation to the
next tier through provider continuation (a successor session with the prior history).
"""
from datetime import datetime, timezone
import contextvars
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from . import turn_engine as engine  # the shared turn engine; this module adapts VS Code chats
from . import ci_watch



INSTRUCTION_BYTES = 16384  # native_agent's bound; the user's own words are never the part that is cut


_locks = {}


_locks_guard = threading.Lock()
_ci_seen = set()  # CI alerts this serve already showed


def text_of(message):
    content = message.get('content')
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict))
    return content or ''


def user_requests(messages):
    """The user's own words per turn (Copilot wraps them in <userRequest>)."""
    requests = []
    for message in messages:
        if message.get('role') != 'user':
            continue
        text = text_of(message)
        found = re.search(r'<userRequest>\s*(.*?)\s*</userRequest>', text, re.S)
        if found:
            requests.append(found.group(1))
        elif not re.match(r'\s*<(environment_info|workspace_info|context)>', text):
            requests.append(text.strip())
    return [r for r in requests if r]


def workspace_root(messages, default=None):
    """The first existing folder listed in Copilot's <workspace_info>, else the default."""
    for message in messages:
        found = re.search(r'<workspace_info>(.*?)</workspace_info>', text_of(message), re.S)
        if not found:
            continue
        for line in found.group(1).splitlines():
            candidate = line.strip().lstrip('-*').strip().strip('`')
            if re.match(r'^([A-Za-z]:[\\/]|/)', candidate) and Path(candidate).is_dir():
                return Path(candidate).resolve()
    return Path(default).resolve() if default else None


def chat_key(root, first_request):
    return hashlib.sha256(json.dumps([str(root), first_request]).encode()).hexdigest()[:32]


def chat_store(key):
    from .native_installation import storage_root
    return storage_root()/'serve'/'chats'/(key + '.json')


MODES = {'read-only': 'read, search and run the detected tests',
         'accept-edits': 'also edit files',
         'yolo': 'also run any command in a terminal'}


MODE = re.compile(r'/(read-only|accept-edits|yolo)\b(\s+default\b)?\s*(.*)', re.S)


PERMISSION = {'read-only': 'read', 'accept-edits': 'edit', 'yolo': 'commands'}  # chat names, engine modes


def with_folder_access(config, root):
    """A session's config plus folders outside the project the user allows in the chat (relay/folder_access.py),
    in the grants file the TUI keeps for the project; unchanged without storage outside the project."""
    try:
        from .folder_access import project_store
        return {**config, 'folder_access': {'grants': str(project_store(root))}}
    except (OSError, ValueError):
        return config


def folder_question(request, root, custody=None):
    from .folder_access import project_store, question, presented_question
    text = (presented_question(request, project_store(root), custody) if custody
            else question(request, project_store(root)))
    return '\n\n_tare: ' + text + '_\n'


def project_mode(root):
    """The project's default chat mode (`/<mode> default` here, or the TUI's project permissions); accept-edits
    when never chosen."""
    chosen = engine.project_permission_mode(root)
    return next((name for name, mode in PERMISSION.items() if mode == chosen), 'accept-edits')


def save_project_mode(root, mode):
    engine.set_project_permission_mode(root, PERMISSION[mode])


def request_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def remember(state, digest, before=None, after=None):
    """The chat's turn record: request digests (to see a rewound or edited history) and project snapshots."""
    state.setdefault('requests', []).append(digest)
    state.setdefault('trees', []).append({'before': before, 'after': after})


def visible_history(messages, turns):
    """The conversation the client still shows, up to its `turns`-th user request (a fork's starting point)."""
    lines, seen = [], 0
    for message in messages:
        text = text_of(message)
        if message.get('role') == 'user':
            found = re.search(r'<userRequest>\s*(.*?)\s*</userRequest>', text, re.S)
            if not found:
                continue
            if seen == turns:
                break
            seen += 1
            lines.append('User: ' + engine.clip(found.group(1), 1500))
        elif message.get('role') == 'assistant' and seen and text.strip():
            lines.append('Assistant: ' + engine.clip(text.strip(), 1500))
    return 'Conversation so far (the user went back to this point; later turns were discarded):\n' + \
        engine.clip('\n\n'.join(lines), 8000)


def undo_to(state, root, base, latest, emit, why):
    """Restore what tare changed since `base`; the result is reported and noted for the conversation."""
    try:
        restored, kept = engine.restore(root, base, latest)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        emit(f'_tare: files could not be restored ({str(exc)[:120]})._\n\n')
        return
    text = (f"_tare: {why}; restored {len(restored)} file(s)" + (': ' + ', '.join(restored[:12]) if restored else '') +
            (f"; kept your own changes to {', '.join(kept[:12])}" if kept else '') + '._\n\n')
    emit(text)
    state.setdefault('notes', []).append(f'{why}: the files you changed since then were restored '
                                         f'({", ".join(restored[:20]) or "none"}).')


def activity_line(event):
    item = event.get('activity') or {}
    if item.get('kind') != 'tool-activity' or item.get('state') == 'running':
        return None
    mark = {'succeeded': '✓', 'failed': '✗', 'refused': '⊘'}.get(item.get('state'), '·')
    target = item.get('target') or ''
    return f"\n> `{item.get('operation')}` {target} {mark}\n"


def instruction_for(request, messages, notes, emit):
    """The user's request, with earlier flow results in this chat, linked pages and image descriptions."""
    extra = engine.read_pages(request, emit) + engine.describe_images(engine.images_in(messages), emit)
    head = engine.clip('Earlier in this chat:\n' + '\n\n'.join(notes), 4000) + '\n\nUser request:\n' if notes else ''
    room = INSTRUCTION_BYTES - len(request.encode('utf-8'))
    return engine.clip(head, room) + request + engine.clip(extra, room - len(engine.clip(head, room).encode('utf-8')))


FLOWS = {'plan': 'planner', 'explore': 'explorer'}


def run_flow(kind, text, root, emit, activity):
    """/plan (planner route at the policy effort) or /explore (explorer on the implementer tiers): read-only."""
    from . import native_agent
    rules = engine.policy()
    role = FLOWS[kind]
    from .execution_scope import current, pinned, for_role
    with for_role(role):
        if current().pin(role):
            deployment=pinned(role)
            name=deployment['native_model_choice']['connection']
        else:
            tiers = [rules['planner']] if role == 'planner' else rules['implementer']
            tier, name, deployment, skipped = engine.choose(tiers)
            if deployment is None:
                return None, 'tare: no route is available for /' + kind + ': ' + json.dumps(skipped)
            if role == 'planner':
                deployment = engine.with_effort(deployment, rules.get('planner_effort', 'high'))
    emit(f'_tare /{kind} · {name}_\n\n')
    config = with_folder_access({'schema': 'tare.tools/native-agent-config/1', 'role': role,
                                 'repositories': {'workspace': str(root)}, 'writable': {}, 'verifiers': {},
                                 'capabilities': ['read', 'list', 'search']}, root)
    session = native_agent.session_root()/('serve-' + kind + '-' + uuid.uuid4().hex)
    result = native_agent.run_session(config, engine.streaming(deployment), session, text, limits=dict(engine.LIMITS),
                                      on_activity=activity)
    record = {'flow': kind, 'route': name, 'session': str(session), 'status': result.get('status')}
    if result.get('folder_access_request'):
        record['folder_access_request'] = result['folder_access_request']
    return record, result.get('output') or f"tare: {result.get('status')} {result.get('reason') or ''}".strip()


def run_turn(messages, emit, default_root=None, *, local_models=None):
    """Run one client turn through tare; emit(text) streams progress; returns the final answer text. Each turn runs
    in its own context, so a chat's choice about local models (engine.LOCAL_MODELS) never reaches another chat;
    `local_models` (the request's tare_local_models) overrides the chat's choice for this turn."""
    return contextvars.copy_context().run(_run_turn, messages, emit, default_root, local_models)


def _run_turn(messages, emit, default_root=None, local_models=None):
    from . import native_agent
    started = time.time()
    requests = user_requests(messages)
    if not requests:
        return 'tare: no user request found in the conversation.'
    root = workspace_root(messages, default_root)
    if root is None:
        return ('tare needs an open folder: open a project folder in VS Code (File > Open Folder) '
                'and ask again.')
    try:  # a push from this machine left CI red: say so once, before the turn
        alerts = ci_watch.fresh_alerts(_ci_seen)
        if alerts:
            emit(ci_watch.notice(alerts) + '\n\n')
    except (OSError, ValueError):
        pass
    key = chat_key(root, requests[0])
    digests = [request_digest(r) for r in requests]
    mode_command = MODE.match(requests[-1])
    if mode_command:  # `/<mode> [default] [request]`: the rest, if any, is this turn's request
        requests[-1] = mode_command.group(3).strip()
    command = re.match(r'/(plan|explore|research)\b\s*(.*)', requests[-1], re.S)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        store = chat_store(key)
        state = json.loads(store.read_text(encoding='utf-8')) if store.exists() else None
        tiers = engine.policy()['implementer']
        progress = lambda event: emit(event['text']) if (event.get('kind') == 'delta' and event.get('channel') == 'text'
                                                        and event.get('purpose') != 'summary') else None
        activity = lambda event: (lambda line: emit(line) if line else None)(activity_line(event))
        streamed = {'text': False}

        def emit_text(event):
            if event.get('kind') == 'delta' and event.get('channel') == 'text' and event.get('purpose') != 'summary':
                streamed['text'] = True
            progress(event)

        def save(answer):
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(state, indent=1), encoding='utf-8')
            return answer

        if state is not None:
            if 'requests' not in state:  # a chat from before turn records: its history is taken as it is
                state['requests'] = digests[:-1]
                state['trees'] = [{'before': None, 'after': None} for _ in digests[:-1]]
            known = state['requests']
            same = next((i for i, (a, b) in enumerate(zip(digests[:-1], known)) if a != b),
                        min(len(digests) - 1, len(known)))
            if same < len(known):  # the client rewound or edited an earlier message: later turns are discarded
                trees = state['trees'][same:]
                base = next((t['before'] for t in trees if t['before']), None)
                latest = next((t['after'] for t in reversed(trees) if t['after']), None)
                if base and latest:
                    undo_to(state, root, base, latest, emit, 'the conversation went back to an earlier message')
                del state['requests'][same:], state['trees'][same:]
                state.pop('folder_request', None)  # asked in a discarded turn
                if state.get('sessions'):
                    state['fork'] = visible_history(messages, same)
        if requests[-1].strip() == '/undo':
            if state is None:
                return 'tare: there is nothing to undo in this chat.'
            turn = next((t for t in reversed(state['trees']) if t['before'] and t['after']
                         and t['before'] != t['after']), None)
            out = []
            if turn is None:
                out.append('_tare: no turn of this chat changed files that can be restored._\n\n')
            else:
                undo_to(state, root, turn['before'], turn['after'], out.append, 'the user undid the last turn that '
                        'changed files')
                turn['after'] = turn['before']  # a second /undo reaches the turn before it
            remember(state, digests[-1])
            return save(''.join(out))
        if requests[-1].strip() == '/setup':  # what tare uses on this machine; changing it takes a terminal
            from .environment import detect, read, show
            text = ('```\n' + show(read(), detect()) + '\n```\n\nChange it with `tare setup` in a terminal on this '
                    'machine, or /setup in the tare TUI.')
            if state is None:
                return text
            remember(state, digests[-1])
            return save(text)
        chat = state if state is not None else {}
        local = engine.local_gpu_command(requests[-1], chat)
        if local is not None:  # `/local-gpu off|on [default] [request]`: the rest, if any, is this turn's request
            state = {'workspace': str(root), 'first_request': requests[0][:2000], 'turns': 0, 'sessions': [], **chat}
            note, requests[-1] = local
            if not requests[-1]:
                remember(state, digests[-1])
                return save(note)
            emit(note)
        engine.LOCAL_MODELS.set((state or {}).get('local_models', True) if local_models is None else local_models)
        if mode_command:
            chosen = mode_command.group(1)
            state = state or {'workspace': str(root), 'first_request': requests[0][:2000], 'turns': 0, 'sessions': []}
            state['mode'] = chosen
            if mode_command.group(2):
                save_project_mode(root, chosen)
            note = (f"_tare: this chat is in {chosen} mode ({MODES[chosen]})"
                    f"{'; new chats in this project start in it' if mode_command.group(2) else ''}._\n\n")
            if not requests[-1]:
                remember(state, digests[-1])
                return save(note)
            emit(note)
        mode = (state or {}).get('mode') or project_mode(root)
        flow = (command.group(1), command.group(2).strip(), '') if command and command.group(2).strip() else None
        note = ''
        asked = (state or {}).get('folder_request')
        # Forward the original reply. Submission alone cannot grant or decline.
        consent_options = {}
        if asked and asked.get('flow'):
            if asked.get('question_id'):
                consent_options = {'folder_consent_request': {**asked, 'reply': requests[-1]}}
            note = ('\n\n[tare] A standalone flow is waiting for folder consent. '
                    'The host classifies the original user reply in isolation. '
                    'If this is a legacy request without a question ID, re-present it in a compatible coordinator.')
        if flow:
            kind, text, extra = flow
            record, answer = (engine.run_research(text, emit) if kind == 'research' else
                              run_flow(kind, text + extra, root, emit, activity))
            if record:
                state = state or {'workspace': str(root), 'first_request': requests[0][:2000], 'turns': 0, 'sessions': []}
                asked = record.pop('folder_access_request', None)
                if asked:
                    state['folder_request'] = {**asked, 'session': record['session'], 'flow': kind, 'text': text}
                    answer += folder_question(asked, root, record['session'])
                    if asked.get('question_id'):
                        from .consent_decisions import present
                        present(record['session'], asked['question_id'])
                state.setdefault('flows', []).append(record)
                # The conversation session did not see the flow; its next turn receives the result.
                state.setdefault('notes', []).append(f'/{kind} {engine.clip(text, 500)}\n{engine.clip(answer, 3000)}')
                remember(state, digests[-1])
                save(None)
            return answer
        instruction = instruction_for(requests[-1] + note, messages, (state or {}).pop('notes', []), emit)
        before = len((state or {}).get('sessions', []))
        # Every turn is snapshotted (/undo and rewinds restore from it); in yolo, commands leave no edit
        # receipts, so the audit also diffs these snapshots of the whole project.
        start_tree, why = engine.snapshot(root)
        before_tree = start_tree if mode == 'yolo' else None
        if mode == 'yolo' and start_tree is None:
            emit(f'_tare: shell changes in this turn cannot be audited ({why})._\n\n')
        if state is None or not state.get('sessions'):
            from .environment import decided
            if not decided():  # once per chat, on its first turn
                emit('_tare: this machine is not set up yet, so tare uses every route it finds (local first). '
                     '`tare setup` in a terminal, or /setup here, shows what it found._\n\n')
            tier, name, deployment, skipped = engine.initial_choice(tiers, requests[-1])
            if deployment is None:
                return 'tare: no route is available right now: ' + json.dumps(skipped)
            emit(f'_tare · {name}_\n\n')
            session = native_agent.session_root()/('serve-' + uuid.uuid4().hex)
            config = with_folder_access(engine.config_for(root, mode), root)
            result = native_agent.run_session(config, engine.streaming(deployment), session, instruction,
                                              limits=dict(engine.LIMITS), conversation=True, on_progress=emit_text,
                                              on_activity=activity, terminal_client=engine.terminal_for(session, config),
                                              **consent_options)
            runs = [(session, result)]
            state = {**(state or {}), 'workspace': str(root), 'first_request': requests[0][:2000], 'turns': 1,
                     'sessions': [{'session': str(session), 'route': name, 'tier': tier, 'skipped': skipped,
                                   'mode': mode, 'selection': deployment['native_auto_selection']}]}
        else:
            current = state['sessions'][-1]
            deployment, reason = engine.available(current['route'])
            session = Path(current['session'])
            if deployment is None:  # the route went away: continue on the next available one
                session, result = escalate(state, root, current['tier'], instruction, emit, emit_text, activity,
                                           why=reason, same_tier_ok=True, mode=mode)
            elif state.get('fork'):  # the session remembers discarded turns: fork from what the client shows
                session, result = fresh(state, root, instruction, deployment, emit, emit_text, activity, mode=mode,
                                        why='the conversation went back to an earlier message',
                                        brief=state.pop('fork'))
            elif current.get('mode', 'accept-edits') != mode:  # a session's permissions are fixed at creation
                session, result = fresh(state, root, instruction, deployment, emit, emit_text, activity,
                                        mode=mode, why=f'mode changed to {mode}')
            else:
                try:
                    result = native_agent.run_session(None, None, session, resume=True, follow_up=instruction,
                                                      raw_user_reply=requests[-1],
                                                      message_id=uuid.uuid4().hex, on_progress=emit_text,
                                                      on_activity=activity, terminal_client=engine.session_terminal(session),
                                                      **consent_options)
                except ValueError as exc:
                    if 'budget exhausted' not in str(exc):
                        raise
                    session, result = fresh(state, root, instruction, deployment, emit, emit_text, activity,
                                            mode=mode, why='the previous one spent its budget')
            runs = [(session, result)]
            state['turns'] += 1
        step = engine.successor((result or {}).get('status'), per_message_limits=True)
        if step:
            tier = state['sessions'][-1]['tier']
            # An account out of usage: another route of the same tier first (available() skips that account).
            limited = result.get('failure_code') == 'CLI_USAGE_LIMIT'
            why = gpu_cause(result.get('gpu_busy')) or result.get('reason')
            emit(f"\n_tare: {result.get('status')} ({result.get('failure_code') if limited else why});"
                 f" moving to the next route._\n")
            target, next_result = escalate(state, root, tier if limited else tier + 1,
                                           'Continue the task from the brief and history.', emit, emit_text, activity,
                                           why=why, mode=mode, renew=step == 'fresh')
            runs.append((target, next_result))
            result = next_result or result
        if (result or {}).get('status') == 'PROPOSED':  # a completed turn that only announced the work goes up once
            current = state['sessions'][-1]
            if engine.choose(tiers, current['tier'] + 1)[2] is not None:
                verdict = engine.unanswered(requests[-1], (result or {}).get('output'),
                                            {'source': 'serve', 'chat': key, 'turn': state['turns'],
                                             'route': current['route']})
                if verdict:
                    emit(f"\n_tare: the reply only announced the work (judge {verdict['model']}, "
                         f"{verdict['probabilities']['announced']:.2f}); moving to the next route._\n")
                    target, next_result = escalate(state, root, current['tier'] + 1, engine.UNANSWERED_CONTINUE,
                                                   emit, emit_text, activity, why='the reply only announced the work',
                                                   mode=mode)
                    runs.append((target, next_result))
                    result = next_result or result
        waiting = state.get('folder_request')
        if waiting and waiting.get('flow') and waiting.get('question_id'):
            from . import consent_decisions as consent, folder_access
            source = next((q for q in consent.load(waiting['session'])['questions']
                           if q['id'] == waiting['question_id']), None)
            target = source.get('target') if source else None
            if target and target['root'] == str(Path(session).resolve()):
                q = next((q for q in consent.load(session)['questions']
                          if q['id'] == source['target_question']), None)
                if q and q['status'] == 'DECLINED':
                    state.pop('folder_request', None)
                elif (q and waiting['path'] in folder_access.granted(q['scope']['store'])
                      and consent.consume(session, target['session'], 'folder', q['scope'])):
                    # Consume and save before dispatch: interruption cannot replay a flow.
                    state.pop('folder_request', None)
                    save(None)
                    record, resumed = run_flow(waiting['flow'], waiting['text'], root, emit, activity)
                    if record:
                        state.setdefault('flows', []).append(record)
                    emit('\n\n' + resumed)
        files = engine.turn_changes(runs, root)
        if before_tree:
            after_tree, _ = engine.snapshot(root)
            if after_tree:
                files = engine.tree_changes(root, before_tree, after_tree)
                state['last_turn'] = {'before': before_tree, 'after': after_tree}
        checks = engine.turn_checks(runs, root)
        fix_runs = []
        if files and (result or {}).get('status') == 'PROPOSED':  # an unfinished turn has no answer to audit or fix
            if not streamed['text'] and (result or {}).get('output'):
                emit(result['output'])  # the answer comes before its audit
                streamed['text'] = True
            current = state['sessions'][-1]
            implementer, _ = engine.available(current['route'])
            record, fix = engine.audit_turn(files, root, engine.conversation_request(requests), implementer,
                                            emit, checks=checks)
            if record:
                state.setdefault('audits', []).append({**record, 'turn': state['turns']})
            if fix and implementer is not None:
                emit(f"\n_tare · {current['route']} (fixing audit findings)_\n\n")
                fix_tree = engine.snapshot(root)[0] if before_tree else None
                fixed = native_agent.run_session(None, None, Path(current['session']), resume=True, follow_up=fix,
                                                 message_id=uuid.uuid4().hex, on_progress=emit_text,
                                                 on_activity=activity,
                                                 terminal_client=engine.session_terminal(current['session']))
                result = fixed or result
                fix_runs = [(Path(current['session']), fixed)]
                fix_files = engine.turn_changes(fix_runs, root)
                if fix_tree:
                    after_tree, _ = engine.snapshot(root)
                    if after_tree:
                        fix_files = engine.tree_changes(root, fix_tree, after_tree)
                        state['last_turn']['after'] = after_tree
                record, _ = engine.audit_turn(fix_files, root, fix, implementer, emit, checks=engine.turn_checks(fix_runs, root))
                if record:
                    state.setdefault('audits', []).append({**record, 'turn': state['turns'], 'fix': True})
        from . import heads_up
        if (result or {}).get('status') == 'PROPOSED' and heads_up.enabled():  # what the reply left unsaid
            try:
                turn = heads_up.from_journal([(s, r['invocation_id']) for s, r in runs + fix_runs
                                              if r and r.get('invocation_id')])
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                turn = None
            new = heads_up.notes({**turn, 'request': requests[-1]},  # the user's words; earlier turns: the journal's
                                 seen=state.get('heads_up', []), pace=state.setdefault('heads_up_pace', {}),
                                 observed={'source': 'serve', 'chat': key, 'turn': state['turns'],
                                           'route': state['sessions'][-1]['route']}) if turn else []
            if new:
                if not streamed['text'] and result.get('output'):
                    emit(result['output'])  # the answer comes before its notes
                    streamed['text'] = True
                emit(heads_up.shown_text(new))
                state['heads_up'] = state.get('heads_up', []) + new
                # The session did not see them; its next turn does, so "check that" has a referent.
                state.setdefault('notes', []).append('tare showed the user these heads-up notes after your reply:\n'
                                                     + '\n'.join('- ' + n for n in new))
        first, last = state['sessions'][max(before - 1, 0)], state['sessions'][-1]
        if before:  # no duplicate paid shadow assessment for the initial routing decision
            engine.jev_shadow(requests[-1], {'source': 'serve', 'chat': key, 'turn': state['turns'], 'start_tier': first['tier'],
                                      'start_route': first['route'], 'final_tier': last['tier'], 'final_route': last['route'],
                                      'status': (result or {}).get('status')})
        asked = (result or {}).get('folder_access_request')
        if asked:  # the user's next message answers it
            state['folder_request'] = {**asked, 'session': state['sessions'][-1]['session']}
        remember(state, digests[-1], start_tree, engine.snapshot(root)[0] if start_tree else None)
        state['updated'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        save(None)
        answer = (result or {}).get('output') or ''
        ask = folder_question(asked, root, state['sessions'][-1]['session']) if asked else ''
        if asked and asked.get('question_id'):
            from .consent_decisions import present
            present(state['sessions'][-1]['session'], asked['question_id'])
        from .work_units import kept_patches
        kept = '\n'.join(f'_tare: unit {key} was not applied ({outcome}); its patch is kept at {patch}_'
                         for session, _ in runs for key, outcome, patch in kept_patches(session, started))
        if kept:
            ask = '\n\n' + kept + ask
        if streamed['text'] and answer:
            return ask  # the answer already streamed
        return (answer or f"tare: {(result or {}).get('status')} {(result or {}).get('reason') or ''}".strip()) + ask


def fresh(state, root, text, deployment, emit, emit_text, activity, *, mode, why, route=None, tier=None,
          skipped=(), brief=None):
    """A new session with the continuity brief, fresh limits and the chat's mode: after a spent budget
    (provider continuation inherits what was spent by design; engine.LIMITS are meant per message), a mode change (a
    session's permissions are fixed at creation), or a route change of a chat whose sessions hold a terminal."""
    from . import native_agent
    from .native_continuity import preview
    current = state['sessions'][-1]
    route, tier = route or current['route'], current['tier'] if tier is None else tier
    brief = brief or preview(current['session'], str(root))['text']
    tail = '\n\nNew user instruction:\n' + text
    instruction = engine.clip(brief, INSTRUCTION_BYTES - len(tail.encode('utf-8'))) + tail
    target = native_agent.session_root()/('serve-' + uuid.uuid4().hex)
    emit(f"\n_tare · {route} (new session: {why})_\n\n")
    config = with_folder_access(engine.config_for(root, mode), root)
    result = native_agent.run_session(config, engine.streaming(deployment), target, instruction,
                                      limits=dict(engine.LIMITS), conversation=True, on_progress=emit_text,
                                      on_activity=activity, terminal_client=engine.terminal_for(target, config))
    state['sessions'].append({'session': str(target), 'route': route, 'tier': tier, 'skipped': list(skipped),
                              'reason': why, 'mode': mode})
    return target, result


def gpu_cause(busy):
    """Why a turn left the local model when the node's GPU was taken (result['gpu_busy']), with how long until text
    gets it back when the node says; None otherwise. Recorded as the next session's reason (tare gpu report)."""
    if not busy:
        return None
    what = engine.GPU_RESERVED + ' by another holder' if busy.get('reason') == 'gpu_reserved' else engine.GPU_BUSY
    after = busy.get('retry_after')
    if not after:
        return what
    minutes = max(1, round(after / 60))
    hours, rest = divmod(minutes, 60)  # a training run can hold the GPU for 12 h
    return what + ', back in about ' + (f'{hours} h {rest} min' if hours else f'{minutes} min')


def escalate(state, root, start_tier, text, emit, emit_text, activity, *, why, same_tier_ok=False,
             mode='accept-edits', renew=False):
    """Provider continuation onto the first available route from `start_tier`; a fresh session when the budget
    is spent (`renew`, engine.successor) or the chat holds a terminal."""
    from . import native_agent
    from .native_continuity import preview, save_request
    tiers = engine.policy()['implementer']
    current = state['sessions'][-1]
    tier, name, deployment, skipped = engine.choose(tiers, start_tier)
    if deployment is None or (name == current['route'] and not same_tier_ok):
        emit(f'\n_tare: no further route available ({why})._\n')
        return Path(current['session']), None
    if renew or mode != 'accept-edits' or current.get('mode', 'accept-edits') != 'accept-edits':
        # ponytail: continuation carries no terminal grant or mode; such chats move on in a fresh session
        return fresh(state, root, text, deployment, emit, emit_text, activity, mode=mode, why=why, route=name,
                     tier=tier, skipped=skipped)
    brief = preview(current['session'], str(root))
    target = native_agent.session_root()/('serve-' + uuid.uuid4().hex)
    # The successor's instruction is the continuity brief followed by the user's new request.
    instruction = (brief['text'] + '\n\nNew user instruction:\n' + text).encode('utf-8')[:8192].decode('utf-8', 'ignore')
    save_request(target, brief, instruction, engine.streaming(deployment))
    emit(f'\n_tare · {name} (continuing: {why})_\n\n')
    result = native_agent.run_session(None, None, target, on_progress=emit_text, on_activity=activity)
    state['sessions'].append({'session': str(target), 'route': name, 'tier': tier, 'skipped': skipped,
                              'reason': why})
    return target, result

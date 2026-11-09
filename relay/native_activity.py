"""Bounded tool presentation, emitted only after the owning journal append succeeds."""
import json
import math
import re
import time


OPERATIONS = {'read': 'Read', 'list': 'List', 'search': 'Search', 'context': 'Find context',
              'explore': 'Delegate exploration', 'plan': 'Plan units', 'implement': 'Delegate implementation',
              'delegate_plan': 'Delegate planning', 'agent_run': 'Model execution',
              'backlog_read': 'Read backlog item', 'backlog_record': 'Record backlog progress',
              'conversation_history': 'Read previous conversation',
              'request_folder_access': 'Ask to read a folder', 'folder_list': 'List granted folder',
              'folder_read': 'Read granted folder', 'folder_search': 'Search granted folder',
              'edit': 'Edit', 'verify': 'Check', 'command': 'Protected command', 'terminal_start': 'Start command',
              'terminal_read': 'Read process output', 'terminal_input': 'Send process input',
              'terminal_stop': 'Stop process', 'terminal_list': 'List processes',
              'terminal_resize': 'Resize terminal', 'terminal_eof': 'Close process input'}
STATES = {'running', 'succeeded', 'failed', 'refused', 'unknown', 'unconfirmed', 'not_started'}


def saved_history(values):
    """Keep display checkpoints readable by the previous installed product.

    Tool identities and receipts remain exact in the owning execution journal.
    New backlog operations use existing display categories only on disk.
    """
    return [dict(value, operation=ALIASES[value['operation']],
                 target=safe(value['operation'] + ': ' + value.get('target', ''), 2048))
            if isinstance(value, dict) and value.get('kind') == 'tool-activity'
            and value.get('operation') in ALIASES else value for value in values]


ALIASES = {'backlog_read': 'read', 'backlog_record': 'edit', 'explore': 'search', 'plan': 'edit', 'implement': 'edit',
           'delegate_plan': 'edit', 'agent_run': 'read', 'request_folder_access': 'read',
           'folder_list': 'list', 'folder_read': 'read', 'folder_search': 'search'}


def restored(value):
    """The richer operation a saved checkpoint stored under an older category ("Edit implement: ...")."""
    target = value.get('target', '')
    for name, alias in ALIASES.items():
        if value.get('operation') == alias and isinstance(target, str) and target.startswith(name + ': '):
            return {**value, 'operation': name, 'target': target[len(name) + 2:]}
    return value


def retained_history(values):
    """Tool chatter must not consume the existing 50-message history allowance."""
    remaining = {'message': 50, 'tool': 100}
    kept = []
    for value in reversed(values):
        kind = 'tool' if isinstance(value, dict) and value.get('kind') == 'tool-activity' else 'message'
        if remaining[kind]:
            kept.append(value)
            remaining[kind] -= 1
    return list(reversed(kept))


def safe(value, limit):
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(value))
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)[:limit]


def activity_view(value):
    """Accept a small presentation record, never a model or raw journal event."""
    if (not isinstance(value, dict) or value.get('kind') != 'tool-activity'
            or value.get('operation') not in OPERATIONS or value.get('state') not in STATES
            or not isinstance(value.get('id'), str) or not 0 < len(value['id']) <= 256):
        raise ValueError('invalid_tool_activity')
    duration = value.get('duration_ms')
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
        raise ValueError('invalid_tool_activity_duration')
    source = value.get('source')
    extra = {}
    actor = value.get('actor')
    if actor is not None:
        fields = {'role', 'model', 'reasoning', 'connection', 'route'}
        if (not isinstance(actor, dict) or set(actor) != fields or
                any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in actor.values())):
            raise ValueError('invalid_tool_activity_actor')
        extra['actor'] = {k: ' '.join(safe(v, 256).split()) for k, v in actor.items()}
    value = restored(value)  # previous releases read the checkpoint; this one restores its labels
    if 'agent_status' in value:
        if value['operation'] != 'agent_run' or value['agent_status'] not in {
                'RUNNING', 'PROPOSED', 'PAUSED', 'CANCELLED', 'BLOCKED', 'EXHAUSTED', 'UNCONFIRMED'}:
            raise ValueError('invalid_agent_activity_status')
        extra['agent_status'] = value['agent_status']
    if source is not None:
        fields = {'session', 'kind', 'mode', 'owner', 'project', 'control_root', 'stage', 'work'}
        if (not isinstance(source, dict) or set(source) not in (fields, fields | {'graph_path'})
                or any(not isinstance(source[key], str) or len(source[key]) > 4096 for key in fields)
                or source['kind'] not in {'session', 'attempt'}
                or source['mode'] not in {'parallel', 'research', 'plan', 'work', 'sequence'}):
            raise ValueError('invalid_tool_activity_source')
        from pathlib import Path
        if any(not Path(source[key]).is_absolute() for key in ('session', 'project', 'control_root')):
            raise ValueError('invalid_tool_activity_source_path')
        if 'graph_path' in source:
            from .project_graph import graph_path
            if not graph_path(source['graph_path']):
                raise ValueError('invalid_tool_activity_graph_path')
        if source['mode'] != 'work' and not Path(source['owner']).is_absolute():
            raise ValueError('invalid_tool_activity_owner_path')
        extra['source'] = {**source, 'stage': safe(source['stage'], 80)}
    if value['state'] == 'refused' and isinstance(value.get('reason'), str) and value['reason'].strip():
        extra['reason'] = ' '.join(safe(value['reason'], 256).split())  # why the host refused, for the row's title
    return {'kind': 'tool-activity', 'id': value['id'], 'operation': value['operation'],
            'state': value['state'], 'target': safe(value.get('target', ''), 2048),
            'detail': safe(value.get('detail', ''), 8000), 'duration_ms': duration,
            'receipt': safe(value.get('receipt', ''), 256),
            'truncated': value.get('truncated') is True, **extra}


def deletion_requested(args):
    """Use the kernel's boolean/argument contract, never string truthiness."""
    from compute_plane.native_review import edit_args
    try:
        return edit_args(args).get('delete') is True
    except (ValueError, TypeError, KeyError):
        return False


def deletion_detail(status, output):
    if status == 'succeeded' and 'sha256' in output and output['sha256'] is None:
        return 'Deleted file.'
    return 'File deletion requested; completion not confirmed.'


def activity_key(value):
    source = value.get('source')
    if source is None:
        return value['id']
    import hashlib
    return hashlib.sha256(source['session'].encode()).hexdigest()+':'+value['id']


def stopped_activity(value):
    value = activity_view(value)
    if value['state'] == 'running':
        value.update(state='unconfirmed', detail='Observation ended before a tool result was received. '
                     'Check the saved session before retrying; effects may be unknown.')
        if value['operation'] == 'agent_run':
            value['agent_status'] = 'UNCONFIRMED'
    return value


def output_preview(output):
    """Readable output first; the exact structured result remains available separately.

    Scoped project instructions are tool metadata already delivered to the model.
    They are omitted from this display only; journals and Full output stay exact.
    """
    if isinstance(output, dict) and 'scoped_project_instructions' in output:
        retained = output['scoped_project_instructions']
        paths = ([str(row.get('path')) for row in retained if isinstance(row, dict) and row.get('text') is not None]
                 if isinstance(retained, list) else [])
        rest = {key: value for key, value in output.items() if key != 'scoped_project_instructions'}
        if not paths:
            return readable_output(rest)
        body = readable_output(rest)
        shown = [safe(path, 120) for path in paths[:3]]
        suffix = f', plus {len(paths)-3} more' if len(paths) > 3 else ''
        notice = safe('Scoped project instructions omitted from this preview ('
                      + ', '.join(shown) + suffix + '); Full output retains them.', 512)
        return body + ('\n\n' if body else '') + notice
    return readable_output(output)


def readable_output(output):
    if not isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False, indent=2)
    lines = []
    for key, label in [('status', 'Process/result state'), ('returncode', 'Exit code'),
                       ('exit_code', 'Exit code'), ('reason', 'Reason'), ('error_type', 'Error')]:
        if key in output:
            lines.append(f'{label}: {output[key]}')
    body = output.get('text', output.get('output'))
    if isinstance(body, str):
        return '\n'.join(lines+([''] if lines else [])+[body])
    if isinstance(body, dict) and any(name in body for name in ('stdout', 'stderr')):
        for name in ('stdout', 'stderr'):
            page = body.get(name, {})
            if isinstance(page, dict):
                if page.get('truncated'):
                    lines.append(name+': earlier output was truncated.')
                if page.get('text'):
                    lines += [name+':', str(page['text'])]
        return '\n'.join(lines)
    return json.dumps(output, ensure_ascii=False, indent=2)


def full_output(custody, project, value):
    """Read one exact saved result under custody; no tool execution or journal dump."""
    from pathlib import Path
    from compute_plane.native_journal import NativeJournal, custody_lock, regular
    from .native_agent import read_json
    value = activity_view(value)
    if project is None:
        raise ValueError('The current project is required to read saved output.')
    root = regular(value['source']['session'] if value.get('source') else custody)
    with custody_lock(root):
        if value.get('source'):
            from .flow_activity import validate_source
            _, project = validate_source(value['source'], project)
        if project is not None:
            state = read_json(root/'native-agent.json', 1024*1024)
            if Path(project).resolve() not in [Path(p).resolve() for p in state['config']['repositories'].values()]:
                raise ValueError('The saved tool result belongs to another project.')
        for record in NativeJournal(root/'journal.jsonl').snapshot().records:
            if value['receipt'] != f'{record.sequence} · {record.sha256}':
                continue
            event = record.event()
            if event['kind'] in {'invocation_not_started', 'invocation_interrupted'} and event['invocation_id'] == value['id']:
                return json.dumps({'status': 'not_started' if event['kind'] == 'invocation_not_started' else 'interrupted',
                                   **({'error_type': event['error_type']} if 'error_type' in event else {})}, indent=2)
            if event['kind'] != 'effect_returned' or event['result']['effect_id'] != value['id']:
                break
            return json.dumps(json.loads(event['result']['output_json']), ensure_ascii=False, indent=2)
    raise ValueError('The recorded tool result could not be verified.')


class ActivityJournal:
    """Journal port decorator. Presentation failures cannot turn an effect into a retry."""

    def __init__(self, journal, callback=None, *, clock=time.monotonic, source=None, narrate=False):
        self.journal, self.callback, self.clock = journal, callback, clock
        self.source, self.narrate = source, narrate
        self.rows, self.started = {}, {}
        self.deletions = set()
        self.omitted = self.observation_failures = 0

    def snapshot(self):
        return self.journal.snapshot()

    def project(self, spec):
        return self.journal.project(spec)

    def append(self, event):
        record = self.journal.append(event)
        try:
            self.observe(record)
        except Exception:
            self.observation_failures += 1
        return record

    def publish(self, row):
        if self.source is not None:
            row = {**row, 'source': self.source}
        row = activity_view(row)
        self.rows[row['id']] = row
        if len(self.rows) > 100:
            first = next(iter(self.rows))
            del self.rows[first]
            self.started.pop(first, None)
            self.deletions.discard(first)
            self.omitted += 1
        if self.callback is not None:
            try:
                self.callback({'channel': 'activity', 'activity': dict(row)})
            except Exception:
                self.observation_failures += 1

    def observe(self, record):
        event = record.event()
        if event['kind'] == 'model_returned':
            self.narration(event)
        elif event['kind'] == 'effect_requested':
            call = event['tool_call']
            if call['name'] not in OPERATIONS:
                return
            args = json.loads(call['arguments_json'])
            target = args.get('path', args.get('name', args.get('query', args.get('handle', ''))))
            if call['name'] in {'backlog_read', 'backlog_record'}:
                target = args.get('work_id', args.get('record', {}).get('work_id', ''))
            if call['name'] == 'terminal_start':
                target = ' '.join(str(arg) for arg in args.get('argv', []))
            if call['name'] == 'explore' and isinstance(args.get('investigations'), list):
                asked = [str(item.get('question', ''))[:80] for item in args['investigations'] if isinstance(item, dict)]
                target = (f'{len(asked)} in parallel: ' if len(asked) > 1 else '') + ' | '.join(asked)
            if call['name'] in {'folder_list', 'folder_read', 'folder_search'}:  # outside the project: say where
                target = str(args.get('folder', '')) + ': ' + str(args.get('path', args.get('query', '')))
            deleting = call['name'] == 'edit' and deletion_requested(args)
            if deleting:
                self.deletions.add(event['effect_id'])
            self.started[event['effect_id']] = self.clock()
            self.publish(dict(kind='tool-activity', id=event['effect_id'], operation=call['name'],
                              target=target, state='running', duration_ms=None,
                              detail='File deletion requested; awaiting its result.' if deleting else
                                     'Tool request recorded; awaiting its result.',
                              receipt=f'{record.sequence} · {record.sha256}'))
        elif event['kind'] == 'effect_returned':
            result = event['result']
            previous = self.rows.get(result['effect_id'])
            if previous is None:
                # A reconciled result belongs to a request saved by an earlier
                # process. Recover its identity from the same owning journal.
                for saved in reversed(self.snapshot().records):
                    intent = saved.event()
                    if intent['kind'] == 'effect_requested' and intent['effect_id'] == result['effect_id']:
                        self.observe(saved)
                        self.started.pop(result['effect_id'], None)
                        previous = self.rows.get(result['effect_id'])
                        break
                if previous is None:
                    return
            output = json.loads(result['output_json'])
            # Tool output only. Arguments such as environment overrides and process
            # input are deliberately absent; model messages/reasoning are never projected.
            detail = output_preview(output)
            if result['effect_id'] in self.deletions:
                detail = deletion_detail(result['status'], output) + '\n' + detail
            protection = output.get('process', {}).get('command_protection') if isinstance(output, dict) else None
            if isinstance(protection, dict):
                summary = ('Command review: '+str(protection.get('review', 'unknown'))+'  '
                           +str(protection.get('review_status', 'unknown'))+'\nReview requests: '
                           +str(protection.get('reserved_calls', '?'))+'/'+str(protection.get('max_reviews', '?')))
                if protection.get('classifier')=='jev':
                    summary = 'Classifier: Jev  '+str(protection.get('classification','unknown'))+'\n'+summary
                for stage, usage in protection.get('usage', {}).items():
                    summary += '\n'+stage+' usage: '+('not reported' if usage is None else json.dumps(usage, ensure_ascii=False))
                detail = summary+('\n\n'+detail if detail else '')
            began = self.started.pop(result['effect_id'], None)
            if result['status'] == 'refused' and isinstance(output, dict) and isinstance(output.get('reason'), str):
                previous = {**previous, 'reason': output['reason']}
            self.publish({**previous, 'state': result['status'], 'detail': detail,
                          'duration_ms': round((self.clock()-began)*1000, 3) if began is not None else None,
                          'receipt': f'{record.sequence} · {record.sha256}', 'truncated': len(detail) > 8000})
        elif event['kind'] in {'invocation_not_started', 'invocation_interrupted'}:
            identity = event['invocation_id']
            previous = self.rows.get(identity)
            if previous is None:
                return  # Model invocation failures are outside this tool-only projection.
            began = self.started.pop(identity, None)
            not_started = event['kind'] == 'invocation_not_started'
            self.publish({**previous, 'state': 'not_started' if not_started else 'unknown',
                          'detail': 'The tool was not dispatched.' if not_started else
                                    'The tool invocation was interrupted. Effects may be unknown; inspect the saved session before retrying.',
                          'duration_ms': round((self.clock()-began)*1000, 3) if began is not None else None,
                          'receipt': f'{record.sequence} · {record.sha256}'})

    def narration(self, event):
        """What the coordinator tells the user while it works ("I'll map the storage first"): the reply text
        that accompanies tool calls. Reasoning stays private; the final answer arrives as the result."""
        reply = event.get('reply') or {}
        calls, text = reply.get('tool_calls') or [], reply.get('text')
        if (not self.narrate or self.callback is None or not isinstance(text, str) or not text.strip() or not calls
                or reply.get('proposes_delivery') or any(c.get('name') == 'tare_deliver' for c in calls)):
            return
        try:
            self.callback({'channel': 'narration', 'call_id': str(event.get('call_id', ''))[:256],
                           'text': safe(text.strip(), 4000)})
        except Exception:
            self.observation_failures += 1

    def finish(self):
        for row in list(self.rows.values()):
            if row['state'] == 'running':
                self.publish(stopped_activity(row))

    def presentation(self):
        return {'items': list(self.rows.values()), 'omitted': self.omitted,
                'observation_failures': self.observation_failures}

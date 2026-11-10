"""Read-only execution attribution. No model, roster, credential or prompt mutation."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
import json
from pathlib import Path
from threading import Lock
import time


_sink = ContextVar('execution_team_sink', default=None)
_owner = ContextVar('execution_team_owner', default=None)
ROLES = {'coordinator': 'Coordinator', 'planner': 'Planner', 'plan-auditor': 'Plan auditor',
         'implementer': 'Implementer', 'explorer': 'Explorer', 'researcher': 'Researcher', 'auditor': 'Auditor'}


def reasoning(deployment):
    """The requested level, including local chat templates; absence stays unknown."""
    try:
        params = deployment['native_profile'].get('parameters_json') or {}
        if isinstance(params, str):
            params = json.loads(params)
        if not isinstance(params, Mapping):
            return None
        if params.get('chat_template_kwargs', {}).get('enable_thinking') is False:
            return 'off'
        for value in (params.get('effort'), params.get('reasoning_effort'),
                      params.get('reasoning', {}).get('effort'),
                      params.get('chat_template_kwargs', {}).get('reasoning_effort'),
                      params.get('output_config', {}).get('effort'),
                      params.get('thinking_config', {}).get('thinking_level'),
                      params.get('thinkingConfig', {}).get('thinkingLevel'),  # Gemini generateContent
                      params.get('additionalModelRequestFields', {}).get('reasoning_config')):
            if isinstance(value, str) and value.strip():
                return value
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        pass
    return None


def actor(state):
    from .native_activity import safe
    deployment = state.get('deployment') or {}
    profile = deployment.get('native_profile') or {}
    protocol, endpoint = profile.get('protocol'), profile.get('endpoint')
    connection = {'cli://codex': 'Codex CLI', 'cli://claude': 'Claude CLI', 'cli://agy': 'AGY CLI'}.get(endpoint)
    if connection is None:
        connection = {'OPENAI_API_KEY': 'OpenAI API', 'ANTHROPIC_API_KEY': 'Claude API',
                      'GEMINI_API_KEY': 'Gemini API', 'OPENROUTER_API_KEY': 'OpenRouter API'}.get(deployment.get('native_credential_env'))
    if deployment.get('native_opencode'):
        from .opencode import connection as opencode_connection
        connection = opencode_connection(deployment)
    if connection is None:
        connection = {'openai-codex-responses': 'Codex Direct', 'openai-chat': 'HTTP · OpenAI-compatible',
                      'openai-responses': 'HTTP · Responses', 'anthropic-messages': 'HTTP · Anthropic',
                      'gemini-generate-content': 'HTTP · Gemini', 'bedrock-converse': 'AWS Bedrock'}.get(protocol, 'Connection not recorded')
    role = state.get('conversation_role') or ('explorer' if state.get('exploration_leaf') else
                                           state.get('config', {}).get('role'))
    return {k: ' '.join(safe(v or 'Not recorded', 256).split()) for k, v in {
        'role': role, 'model': profile.get('model'), 'reasoning': reasoning(deployment),
        'connection': connection, 'route': deployment.get('id')}.items()}


def actor_label(value):
    from .model_choices import LABELS
    model = value['model']
    key = model.rsplit('-', 1)[0] if model.endswith(('-low', '-medium', '-high')) else model
    return ' · '.join((ROLES.get(value['role'], value['role']), LABELS.get(key, model), value['reasoning']))


@contextmanager
def observe_session(state, invocation, callback=None, *, custody=None, source=None, host_action=None):
    """One bounded row per invocation; children inherit observation, never authority."""
    from .native_activity import activity_view
    inherited = _sink.get()
    callback = callback if callback is not None else inherited[0] if inherited else None
    lock = inherited[1] if inherited else Lock()
    token = _sink.set((callback, lock))
    owner = Path(custody) if custody and state.get('conversation_flow') else _owner.get()
    owner_token = _owner.set(owner)
    mission = str(state.get('instruction', '')).split('\n', 1)[0][:512]
    if owner is not None:
        try:
            from .native_agent import read_json
            tasks = read_json(owner/'conversation-tasks.json', 1024*1024)
            active = next((t for t in tasks['tasks'] if t['id'] == tasks.get('current')), None)
            if active and active.get('objective'):
                mission = str(active['objective'])[:512]
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Missing presentation metadata never changes dispatch.
    result, started = {}, time.monotonic()
    row = {'kind': 'tool-activity', 'operation': 'agent_run',
           'id': state['session_id']+'/invocation/'+invocation['id'],
           'actor': (actor(state) if host_action is None else {
               'role':'host','model':'Saved delivery','reasoning':'No coordinator call',
               'connection':'Local host','route':host_action}), 'target': mission,
           'state': 'running', 'agent_status': 'RUNNING', 'duration_ms': None, 'receipt': 'Native invocation: '+invocation['id'],
           'detail': ('Model execution started. No final result is recorded yet.' if host_action is None
                      else 'Saved delivery recovery started. Checks and independent audit still apply.')}
    if source is not None:
        row['source'] = source

    def emit(value):
        if callback is not None:
            try:
                with lock:
                    callback({'channel': 'activity', 'activity': activity_view(value)})
            except Exception:
                pass  # Presentation cannot fail, retry or grant an execution.

    emit(row)
    try:
        yield result
    finally:
        _sink.reset(token)
        _owner.reset(owner_token)
        status = result.get('status', 'UNCONFIRMED').upper()
        detail = 'Native result: '+status
        if result.get('reason'):
            detail += '\nReason: '+str(result['reason'])
        if 'model_calls' in result:
            detail += '\nModel calls (session total): '+str(result['model_calls'])
        detail += '\nA returned proposal is not evidence of application or audit acceptance.'
        emit({**row, 'state': 'succeeded' if status == 'PROPOSED' else
              'unconfirmed' if status in {'PAUSED', 'UNCONFIRMED'} else 'failed',
              'agent_status': status, 'detail': detail,
              'duration_ms': round((time.monotonic()-started)*1000, 3)})


def saved_rows(custody, project, history=()):
    """Project current and legacy custody without backfilling execution history."""
    from compute_plane.native_journal import regular
    from .native_agent import read_json
    from .native_activity import safe
    rows = {}

    def add(row):
        value = row['actor']
        label = actor_label(value)+' · '+value['connection']+' · '+row.get('status', row.get('state', 'Not recorded'))
        if value['role'] == 'planner' and value['reasoning'] in {'low', 'medium', 'minimal', 'none', 'off'}:
            label += ' · Below minimum high'
        rows[row['id']] = {'id': row['id'], 'label': label,
            'body': (label+'\n\nMission: '+safe(row.get('target', ''), 512)+'\n'+safe(row.get('detail', ''), 8000)+
                     '\n\nRequested model: '+value['model']+'\nRequested reasoning: '+value['reasoning']+
                     '\nRoute: '+value['route']+'\nExecution: '+row['id'])}

    note = ('Recorded executions only, showing the latest saved attempt per participant. '
            'Reasoning is the requested level, not private model reasoning. ')
    if custody:
        try:
            root = regular(Path(custody)).resolve()
            state = read_json(regular(root/'native-agent.json'), 1024*1024)
            if Path(project).resolve() not in [Path(p).resolve() for p in state['config']['repositories'].values()]:
                raise ValueError('This saved session belongs to another project.')
            task_names = {}
            if (root/'conversation-tasks.json').is_file():
                try:
                    tasks = read_json(regular(root/'conversation-tasks.json'), 1024*1024)
                    task_names = {t['id']: t.get('objective') for t in tasks['tasks'] if isinstance(t.get('objective'), str)}
                except (OSError, ValueError, KeyError, TypeError):
                    note += 'Saved task labels unavailable. '
            queue, seen = [root], set()
            while queue and len(seen) < 100:
                session = queue.pop(0)
                if session in seen:
                    continue
                seen.add(session)
                try:
                    if not session.resolve().is_relative_to(root):
                        raise ValueError('Child session outside the conversation custody.')
                    child = read_json(regular(session/'native-agent.json'), 1024*1024)
                    person = actor(child)
                    latest = (child.get('invocations') or [{}])[-1]
                    status = latest.get('status', 'NOT_STARTED')
                    result = 'Native result: '+status
                    mission = child.get('instruction', '').split('\n', 1)[0]
                    receipt = session.parent/'receipt.json'
                    if receipt.is_file():
                        record = read_json(regular(receipt), 1024*1024)
                        task_id = record.get('task') or record.get('demand_id') or (record.get('unit') or {}).get('key')
                        mission = task_names.get(task_id) or mission
                        review = record.get('result') or {}
                        if person['role'] in {'auditor', 'plan-auditor'} and review.get('verdict'):
                            status += ' · Audit '+str(review['verdict'])
                            result += '\nRecorded audit: '+str(review['verdict'])
                            result += '\nReason: '+str(review.get('reason') or review.get('summary') or 'Not recorded')
                    result += '\nA proposal alone does not establish application or approval.'
                    add({'id': child['session_id']+'/invocation/'+latest.get('id', 'not-started'), 'status': status,
                         'actor': person, 'target': mission, 'detail': result})
                    for pattern in ('planning/*/session', 'delegations/*/session', 'units/*/session-*',
                                    'reviews/*/session', 'web-research/*/session'):
                        for path in session.glob(pattern):
                            if len(queue)+len(seen) >= 100:
                                break
                            if (path/'native-agent.json').is_file():
                                queue.append(path)
                except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
                    note += 'Some saved execution metadata could not be read. '
            if queue or len(seen) == 100:
                note += 'Display limited to 100 saved sessions. '
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            note += safe(str(exc), 200)+' '
    # Live rows include sessions whose final outcome has not reached the TUI yet.
    for row in history:
        if isinstance(row, dict) and row.get('operation') == 'agent_run' and row.get('actor'):
            if row['id'] not in rows:
                add(row)
    summary = {'id': 'team-summary', 'label': 'Execution team · '+str(len(rows))+' recorded execution(s)',
               'body': note+'No model calls. No files changed.\n\n'+
               ('\n'.join(row['label'] for row in rows.values()) or
                'No execution identity recorded yet. Configured role preferences are not proof of execution.')}
    return [summary, *rows.values()]

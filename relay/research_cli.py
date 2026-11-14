"""Native CLI web tools behind one read-only research contract and managed process."""
from copy import deepcopy
import json
import math
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from uuid import uuid4

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError
from tare_tools_agent_runtime import ModelFailure
from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode

from .web_research import key, url


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STRING = {'type': 'string'}
REPORT = obj({'status': {'type': 'string', 'enum': ['COMPLETED', 'PARTIAL']}, 'summary': STRING,
    'findings': {'type': 'array', 'maxItems': 8, 'items': obj({'claim': STRING, 'url': STRING,
        'anchor': {'type': 'string', 'description': 'A short literal identifier or phrase present in the source, '
            'such as include_links. This locates evidence; Tare extracts the actual quotation.'},
        'kind': {'type': 'string', 'enum': ['observed', 'inference']}})},
    'sources': {'type': 'array', 'maxItems': 5, 'items': obj({'url': STRING, 'title': STRING})},
    'remaining': {'type': 'array', 'maxItems': 8, 'items': STRING}})
WEB_TOOLS = {'cli://codex': {'web_search'}, 'cli://claude': {'WebSearch', 'WebFetch'},
             'cli://agy': {'search_web', 'read_url_content'}}


def failure_message(rows, errors, physical):
    """Vendor failures often live in stdout JSON while stderr is empty."""
    messages = []
    for row in rows:
        if row.get('type') in {'error', 'turn.failed'}:
            error = row.get('error', row)
            messages.append(error.get('message', encode(error)) if isinstance(error, dict) else str(error))
        if row.get('type') == 'result' and row.get('is_error'):
            messages.append(str(row.get('result', row.get('subtype', 'Research failed.'))))
        if row.get('event') == 'result' and row.get('result', {}).get('status') not in {None, 'SUCCESS'}:
            messages.append(str(row['result'].get('error', row['result'].get('response', 'Research failed.'))))
    return '\n'.join(messages)[-600:] or errors.decode('utf-8', 'replace')[-600:] or encode(physical)


def environment():
    env = dict(os.environ)
    # Reuse the CLI's login, never accidentally spend an API key or load project code.
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
            'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
            'CLAUDE_CODE_USE_FOUNDRY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_GEMINI_BASE_URL',
            'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY', 'PYTHONPATH', 'PYTHONHOME',
            'CLAUDE_CODE_EFFORT_LEVEL', 'MAX_THINKING_TOKENS'):
        env.pop(name, None)
    return env


def effort(deployment):
    if deployment['native_profile']['protocol'] != 'vendor-cli':
        from .execution_team import reasoning
        return reasoning(deployment) or 'profile-defined'
    from .execution_scope import current, require
    if current().pin('researcher'):
        require(deployment,'researcher')
        from .execution_team import reasoning
        return reasoning(deployment)
    return 'medium'


def command(deployment, directory, system, control, max_calls):
    """Stable role/tools/working directory; subsequent questions resume the same researcher connection."""
    from .native_agent import read_json
    profile = deployment['native_profile']
    endpoint, model = profile['endpoint'], profile['model']
    executable = deployment['native_cli']['executable']
    selected_effort=effort(deployment)
    connection = directory/'connection.json'
    saved = read_json(connection, 65536) if connection.exists() else {}
    saved.setdefault('system', system)
    system = saved['system']  # Role updates must not rewrite a running researcher's cached prefix.
    schema = directory/'response-schema.json'
    schema.write_text(encode(REPORT), encoding='utf-8')
    if endpoint == 'cli://codex':
        from compute_plane.native_cli_codex import discover, prepare
        from compute_plane.managed_process import run_pipe_process
        features = saved.get('features')
        if features is None:
            features = list(discover(executable, directory, control, run_pipe_process, {}))
            saved['features'] = features
        prepared = prepare(directory, {'model': model, 'effort': selected_effort, 'system': system,
            'response_schema': REPORT}, deployment['native_cli']['model_catalog'], features=features)
        # Reuse Kernel's disabled shell/plugins/apps/agents controls. Only the
        # built-in web tool changes; no execution/patch/MCP tool is admitted.
        catalog = json.loads((directory/'models.json').read_text(encoding='utf-8'))
        catalog['models'][0]['supports_search_tool'] = True
        save_json(directory/'models.json', catalog)
        configs = [part for i, value in enumerate(prepared[:-1]) if value == '-c'
                   for part in ('-c', prepared[i+1])]
        argv = [executable, '-s', 'read-only', '-a', 'never', *configs,
                '-c', 'web_search="live"', 'exec']
        if saved.get('session_id'):
            argv += ['resume', saved['session_id']]
        argv += ['--ignore-user-config', '--skip-git-repo-check', '--json', '-m', model, '--output-schema', str(schema), '-']
    elif endpoint == 'cli://claude':
        argv = [executable, '-p', '--model', model, '--effort', selected_effort,
            '--tools', 'WebSearch,WebFetch', '--allowedTools', 'WebSearch,WebFetch',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
            '--settings', '{"disableAllHooks":true,"enabledPlugins":{}}',
            '--disable-slash-commands', '--no-chrome',
            '--output-format', 'stream-json', '--verbose', '--system-prompt', system,
            '--json-schema', encode(REPORT)]
        if max_calls is not None:
            argv += ['--max-turns', str(max_calls)]
        if saved.get('session_id'):
            argv += ['--resume', saved['session_id']]
        else:
            saved['session_id'] = str(uuid4())
            argv += ['--session-id', saved['session_id']]
    elif endpoint == 'cli://agy':
        model = deployment.get('native_reasoning_models', {}).get(selected_effort, model)
        agent = directory/'.agents/agents/tare-researcher/agent.md'
        agent.parent.mkdir(parents=True, exist_ok=True)
        plugin = directory/'.agents/plugins/tare-researcher'
        plugin.mkdir(parents=True, exist_ok=True)
        save_json(plugin/'plugin.json', {'name': 'tare-researcher', 'description': 'Bounded read-only web research.'})
        launcher = plugin/'hook.py'
        hook_source = Path(__file__).with_name('research_cli_hook.py')
        launcher.write_bytes(hook_source.read_bytes())
        boundary = directory/'boundary.jsonl'
        boundary.write_text('', encoding='utf-8')
        def hook(phase):
            args = [sys.executable, '-I', '-B', str(launcher), str(boundary), phase,
                    'unlimited' if max_calls is None else str(max_calls)]
            return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)
        save_json(plugin/'hooks.json', {'tare-researcher': {
            'PreInvocation': [{'command': hook('pre')}], 'PostInvocation': [{'command': hook('post')}],
            'PreToolUse': [{'matcher': '*', 'hooks': [{'command': hook('tool')}]}]}})
        agent.write_text('---\nname: tare-researcher\ndescription: Internet research for Tare.\n'
            'tools: [search_web, read_url_content, finish]\nexcludeDefaultComponents: true\n'
            'mainAgent: true\nsubagent: false\ncommandExecutionPolicy: "off"\nplugins:\n  - '
            +json.dumps(plugin.as_posix())+'\n---\n'+system, encoding='utf-8')
        argv = [executable, '--add-dir', str(directory), '--agent', 'tare-researcher', '--model', model,
            '--effort', selected_effort, '--disable-slash-commands', '--output-format', 'stream-json',
            '--input-format', 'stream-json', '--json-schema', encode(REPORT),
            # The agent exposes only search/read/finish and its hook denies all
            # other tools. Approve that closed read-only surface in headless
            # mode; never modify the operator's global permission settings.
            '--dangerously-skip-permissions',
            '--print-timeout', str(max(1, math.ceil(min(150, control.remaining_seconds()))))+'s']
        if saved.get('session_id'):
            argv += ['--conversation', saved['session_id']]
    else:
        raise ValueError('Research requires a supported web-capable CLI.')
    save_json(connection, saved)
    return argv, saved, model


def decode(rows, endpoint, *, prior_web=False):
    """Normalize only final structured output; text/claims of having searched are insufficient."""
    result, text, session_id, usage, calls = None, None, None, None, None
    observed = set()
    for row in rows:
        if row.get('type') == 'thread.started':
            session_id = row.get('thread_id')
        if row.get('type') == 'item.completed':
            item = row.get('item', {})
            if item.get('type') in WEB_TOOLS['cli://codex']:
                observed.add(item['type'])
            if item.get('type') in {'command_execution', 'mcp_tool_call', 'file_change'}:
                observed.add(item['type'])
            if item.get('type') == 'agent_message':
                text = item.get('text')
        if row.get('type') == 'turn.completed':
            usage = row.get('usage')
        if row.get('type') == 'assistant':
            for item in row.get('message', {}).get('content', []):
                if item.get('type') == 'tool_use':
                    observed.add(item.get('name'))
        if row.get('type') == 'result':
            if row.get('is_error'):
                raise ValueError('The research CLI ended with an error: '+str(row.get('result', ''))[:200])
            result = row.get('structured_output')
            usage, calls = row.get('usage'), row.get('num_turns')
            session_id = row.get('session_id', session_id)
        if row.get('event') == 'init':
            session_id = row.get('conversation_id')
        if row.get('event') == 'step_update':
            step = row.get('step_update', {})
            if step.get('step_type') == 'tool':
                observed.add(step.get('tool_name'))
        if row.get('event') == 'result':
            final = row.get('result', {})
            if final.get('status') not in {None, 'SUCCESS'}:
                raise ValueError('The research CLI ended with '+str(final.get('status'))+': '+str(final.get('error', ''))[:200])
            result, usage, calls = final.get('structured_output'), final.get('usage'), final.get('num_turns')
            if result is None:
                text = final.get('response')
            session_id = row.get('conversation_id', session_id)
    if result is None and text:
        try:
            result = json.loads(text) if isinstance(text, str) else text
        except ValueError:
            raise ValueError('Research ended without structured evidence: '+str(text)[:240]) from None
    Draft7Validator(REPORT).validate(result)
    if not observed.intersection(WEB_TOOLS[endpoint]) and not prior_web:
        raise ValueError('The CLI returned text without an observed web tool call; internet research is unconfirmed.')
    extra = observed-WEB_TOOLS[endpoint]-{'StructuredOutput', 'finish'}
    if extra:
        raise ValueError('Unexpected researcher tool: '+','.join(sorted(map(str, extra))))
    for source in result['sources']:
        url(source['url'])
    if len(encode(result).encode()) > 32768:
        raise ValueError('The research report exceeds 32 KiB.')
    return {'status': 'COMPLETED', 'report': result, 'session_id': session_id, 'usage': usage,
            'web_findings_reused': not bool(observed.intersection(WEB_TOOLS[endpoint])),
            **({'calls_used': calls} if type(calls) is int and calls >= 1 else {})}


def research(deployment, question, *, root, control, max_calls, refresh=False, on_dispatch=None):
    from .execution_scope import admit, require
    require(deployment,'researcher')
    admit(deployment, 'researcher')  # the routing profile's backstop: a research connection is not a session
    if deployment['native_profile']['protocol'] != 'vendor-cli':
        from .research_api import research as api_research
        return api_research(deployment, question, root=root, control=control, max_calls=max_calls,
                            refresh=refresh, on_dispatch=on_dispatch)
    from compute_plane.managed_process import run_pipe_process
    from .native_agent import prompt_capsule, read_json
    profile = deployment['native_profile']
    directory = regular(Path(root)/'research-connections'/key(profile))
    directory.mkdir(parents=True, exist_ok=True)
    prior = directory/'connection.json'
    existed = prior.exists() and bool(read_json(prior, 65536).get('session_id'))
    dispatched = None
    physical = None
    try:
        capsule = prompt_capsule(deployment, 'researcher', 'leaf')
        from .guidance import fill, values
        system = fill(capsule['system'], values(model_id=(deployment.get('native_profile') or {}).get('model'),
                                                model_name=deployment.get('model')))  # variables (/guidance)
        argv, saved, actual_model = command(deployment, directory, system, control, max_calls)
        prompt = ('Research this question using web tools. Return the specified JSON report. '
                  'For each finding, copy only a short literal source locator into anchor; '
                  'Tare retrieves the surrounding source excerpt. Do not compose quotations.\n'+question)
        if refresh:
            prompt += '\nRefresh requested: use a web tool now to check the current sources; do not rely only on earlier findings.'
        source = encode({'event': 'user', 'message': {'content': prompt}})+'\n' \
            if profile['endpoint'] == 'cli://agy' else prompt
        launch = {'endpoint': profile['endpoint'], 'model': actual_model,
                  'effort': effort(deployment), 'resume': existed}
        if on_dispatch is not None:
            on_dispatch(dict(launch))
        dispatched = launch
        physical, raw, errors = run_pipe_process(argv, cwd=directory, env=environment(), stdin=source.encode(),
            timeout=min(150, control.remaining_seconds()), cancelled=control.should_stop, max_output=1024*1024)
        rows = []
        for line in raw.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        # Keep a paid connection even when its final report or exit is invalid.
        for row in rows:
            identity = row.get('thread_id') if row.get('type') == 'thread.started' else \
                row.get('session_id') if row.get('type') in {'system', 'result'} else \
                row.get('conversation_id') if row.get('event') == 'init' else None
            if identity:
                saved['session_id'] = identity
                save_json(prior, saved)
        used_web = any(
            r.get('type') == 'item.completed' and r.get('item', {}).get('type') == 'web_search'
            or r.get('type') == 'assistant' and any(c.get('type') == 'tool_use' and c.get('name') in WEB_TOOLS[profile['endpoint']]
                for c in r.get('message', {}).get('content', []))
            or r.get('event') == 'step_update' and r.get('step_update', {}).get('tool_name') in WEB_TOOLS[profile['endpoint']]
            for r in rows)
        prior_web = saved.get('web_tool_observed', False)
        if used_web:
            saved['web_tool_observed'] = True
            save_json(prior, saved)
        if (not physical.get('cleanup_verified') or physical.get('exit_code') != 0
                or any(physical.get(k) for k in ('cancelled', 'timeout', 'output_limit'))):
            startup_error = (physical.get('cleanup_verified') and physical.get('exit_code') == 2
                             and b'unexpected argument' in errors and not raw.strip())
            diagnostic = failure_message(rows, errors, physical)
            model_refused = physical.get('cleanup_verified') and 'model_not_found' in diagnostic
            from compute_plane.native_cli import USAGE_LIMIT_TEXT
            quota_refused = physical.get('cleanup_verified') and bool(USAGE_LIMIT_TEXT.search(diagnostic) or re.search(
                r"you(?:'ve| have) hit your (?:weekly|daily|monthly|session) limit", diagnostic, re.I))
            if quota_refused:
                from .turn_engine import note_usage_limit
                note_usage_limit(deployment)
            return {'status': 'FAILED', 'error': 'Research CLI interrupted or failed: '
                +diagnostic, 'process': physical, 'dispatched': dispatched,
                **({'calls_used': 0, 'not_started': True} if startup_error else
                   {'calls_used': 1, 'not_started': True} if model_refused or quota_refused else {})}
        answer = decode(rows, profile['endpoint'], prior_web=prior_web and not refresh)
        if answer.get('session_id'):
            saved['session_id'] = answer['session_id']
            save_json(prior, saved)
        if profile['endpoint'] == 'cli://agy':
            hooks = [json.loads(line) for line in (directory/'boundary.jsonl').read_bytes().splitlines()]
            if (not hooks or any(h.get('model') != actual_model for h in hooks)
                    or any(h.get('phase') == 'tool' and not h.get('allowed') for h in hooks)):
                raise ValueError('The AGY researcher tool/model boundary could not be confirmed.')
            answer['calls_used'] = sum(h['phase'] == 'pre' for h in hooks)
            # Per-step receipts belong to this invocation; resumed result totals
            # may describe the entire conversation and must not be counted twice.
            steps = {r['step_update']['step_index']: r['step_update']['usage'] for r in rows
                if r.get('event') == 'step_update' and r.get('step_update', {}).get('state') == 'DONE'
                and isinstance(r.get('step_update', {}).get('usage'), dict)}
            answer['usage'] = {'reported_steps': list(steps.values()),
                'scope': 'Current invocation step usage; missing counters are unknown.'} if steps else None
        answer.update(model=actual_model, session_reused=existed, dispatched=dispatched,
            cache_note='Same researcher session, stable role/tools; cache hits depend on the provider usage report.')
        return answer
    except (ImportError, OSError, ValueError, KeyError, TypeError, ValidationError, ModelFailure) as exc:
        # Includes schema/CLI feature failures; no implicit paid retry or fallback.
        return {'status': 'FAILED', 'error': type(exc).__name__+': '+str(exc).splitlines()[0][:350],
                **({'dispatched': dispatched} if dispatched is not None else {}),
                **({'process': physical} if physical is not None else {})}

"""Conversation web reading and a separate, bounded internet researcher."""
from copy import deepcopy
import json
from pathlib import Path
import time

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode, object_json
from tare_tools_agent_runtime import EffectResult

from . import web_research as web
from . import web_search as search_api

SPEC = {
    'read_url': {'description': 'Read an HTTP(S) URL directly with the local Trafilatura reader. No researcher '
        'or extra model call. Use this when the user gives a link; repository search cannot open websites. '
        'Returns bounded, untrusted page text, title, source URL, hash and any real access error. '
        'Follow-up ranges reuse the page for one hour; refresh explicitly when current content is required.',
        'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'},
            'start_line': {'type': 'integer', 'minimum': 1},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 160},
            'refresh': {'type': 'boolean'}}, 'required': ['url'], 'additionalProperties': False}},
    'research': {'description': 'Delegate a bounded internet investigation to a separate researcher. '
        'Use to discover sources or compare information from several pages; for one given URL use read_url. '
        'Give one self-contained question, without repository contents or full chat history. Returns findings '
        'with host-checked source excerpts, URLs, uncertainties and usage when reported. '
        'The researcher cannot inspect/edit the project, run task commands, plan delivery or delegate again. '
        'Identical recent questions reuse the report; related questions reuse the researcher session.',
        'parameters': {'type': 'object', 'properties': {'question': {'type': 'string'},
            'route': {'type': 'string'}, 'refresh': {'type': 'boolean', 'description':
                'Use true when the user asks to retry or recheck current sources. Bypasses report/page caches '
                'while keeping the researcher session and prompt history.'}},
            'required': ['question'], 'additionalProperties': False}},
}


class ResearchTools:
    def __init__(self, tools, root, state):
        self.tools, self.root, self.state = tools, Path(root), state
        specs = deepcopy(SPEC)
        if state['research'].get('search_api') == 1:
            specs['search_web'] = deepcopy(search_api.SPEC)
        self.specs = specs
        self.names = (*tools.names, *specs)
        order = state['research']['route_order']
        specs['research']['description'] += ' Configured researcher routes: '+encode(order)+'.'
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **specs})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def observations(self):
        return {**self.tools.observations(), 'research': web.summary(self.root),
                'web_search': search_api.summary(self.root)}

    def read(self, args, control):
        if set(args)-{'url', 'start_line', 'limit', 'refresh'} or type(args.get('refresh', False)) is not bool:
            raise ValueError('Use the declared URL reading fields.')
        start, limit = args.get('start_line', 1), args.get('limit', 80)
        if type(start) is not int or start < 1 or type(limit) is not int or not 1 <= limit <= 160:
            raise ValueError('Use a positive start_line and a limit from 1 to 160.')
        value = web.page(self.root, args.get('url'), control, refresh=args.get('refresh', False))
        if value.get('status') == 'REFUSED':
            raise ValueError(value['error'])
        text = value.pop('text', '')
        lines = text.splitlines()
        excerpt = '\n'.join(lines[start-1:start-1+limit])
        value.update(start_line=start, total_lines=len(lines), text=excerpt[:12000], untrusted=True,
            excerpt_truncated=len(excerpt) > 12000, authority='NONE')
        return value

    @staticmethod
    def search_args(args):
        return {'query': ' '.join(str(args.get('question', '')).split()), 'refresh': args.get('refresh', False)}

    @staticmethod
    def discovery(value):
        return {'schema': web.SCHEMA, 'status': 'PARTIAL' if value.get('status') == 'COMPLETED' else value.get('status', 'UNAVAILABLE'),
                'summary': ('Native research was unavailable. Direct API search completed; page reading and synthesis remain pending.'
                            if value.get('status') == 'COMPLETED' else
                            'Native research was unavailable; API search did not complete. Check discovery for its actual status.'),
                'findings': [], 'sources': [], 'discovery': value, 'authority': 'NONE',
                'remaining': ['Read relevant result pages and synthesize the answer; snippets are not page-reading proof.'],
                'next': 'Use read_url on discovery.results URLs before source-backed claims. Do not automatically repeat an uncertain search.'}

    def search_fallback(self, request, args, control, *, calls_used=0, failures=None):
        # Existing chats keep their cached tool definitions. The existing
        # research effect can still discover URLs without a researcher CLI.
        if args.get('route') or not search_api.selection()[0]:
            return None
        search_args = self.search_args(args)
        if len(search_args['query']) > 128:
            return {'status': 'UNAVAILABLE', 'reason': 'Native research could not start. Search APIs require a query of at most 128 characters.',
                    'next': 'Call research with a concise search question, or use search_web if offered.', 'authority': 'NONE'}
        value = self.discovery(search_api.search(self.root, search_args, request.effect_id, self.session_id, control))
        job = regular(self.root/'web-research'/web.key(request.effect_id))
        job.mkdir(parents=True, exist_ok=True)
        # Keep launched-process provenance without changing the fallback's legacy
        # fields or cache identity; catalog/preparation failures add no dispatch.
        from .native_agent import read_json
        target = job/'receipt.json'
        previous = read_json(target, 128*1024) if target.exists() else {}
        provenance = {name: previous[name] for name in ('attempts', 'executed_decision') if name in previous}
        save_json(target, {'schema': web.SCHEMA, 'kind': 'api-search-fallback',
            'request_sha256': web.key(args), 'status': value['status'], 'calls_used': calls_used,
            'native_attempts': failures or [],
            'finished_at': time.time(), 'result': value, **provenance})
        web.trim_reports(self.root)
        return value

    def investigate(self, request, args, control):
        if (set(args)-{'question', 'route', 'refresh'} or not isinstance(args.get('question'), str)
                or not args['question'].strip() or len(encode(args).encode()) > 8192
                or type(args.get('refresh', False)) is not bool):
            raise ValueError('Give one concrete internet question within 8 KiB.')
        frozen = self.state['research']
        order = [args['route']] if args.get('route') else frozen['route_order']
        if not order or any(n not in frozen['routes'] for n in order):
            fallback = self.search_fallback(request, args, control) if not args.get('route') else None
            if fallback is not None:
                return fallback
            return {'status': 'UNAVAILABLE', 'reason': 'No configured researcher CLI is available.',
                    'routes': frozen['route_order'], 'details': frozen.get('unavailable'), 'authority': 'NONE',
                    'next': 'Use search_web for direct API search, then read_url on relevant results.'
                            if 'search_web' in self.names else 'Configure an available researcher connection.'}
        earlier = sorted(web.records(self.root), key=lambda r: r.get('finished_at', 0))
        from .execution_scope import current
        identity_input={'question': args['question'].strip(), 'routes': order}
        if current().pin('researcher'):
            identity_input['deployments']={n:frozen['routes'][n] for n in order}
        identity = web.key(identity_input)
        if not args.get('refresh'):
            match = next((r for r in reversed(earlier) if r.get('result') and r.get('question_sha256') == identity
                and r.get('status') in {'COMPLETED', 'PARTIAL'}
                and time.time()-r.get('finished_at', 0) < web.CACHE_SECONDS), None)
            if match:
                return {**match['result'], 'reused': True}
        from compute_plane.native_journal import NativeJournal
        messages = [r.event() for r in NativeJournal(self.root/'journal.jsonl').snapshot().records
                    if r.event()['kind'] == 'user_message']
        input_id = messages[-1]['message_id'] if messages else 'initial-'+self.session_id
        from . import execution_resources as resources
        if not resources.selected('calls') and sum(r.get('input_id') == input_id for r in earlier) >= web.MAX_REQUESTS:
            return {'status': 'LIMIT_REACHED', 'reason': 'This user request already used eight research attempts.', 'authority': 'NONE'}
        from .delegation import child_calls
        from .delegation_tools import count_calls
        maximum = self.state['limits']['max_model_calls']
        left = None if maximum is None else maximum-count_calls(self.root)-resources.known_calls(child_calls(self.root))-2
        if left is not None and left < 2 or control.remaining_seconds() < 5:
            return {'status': 'LIMIT_REACHED', 'reason': 'Keep budget for the coordinator to inspect findings and answer.', 'authority': 'NONE'}
        calls = resources.ceiling('calls', resources.child_calls(6), left)
        from .research_cli import research  # Check the installed dependency before reserving a job.
        job = regular(self.root/'web-research'/web.key(request.effect_id))
        job.mkdir(parents=True, exist_ok=True)
        from .native_agent import read_json
        target = job/'receipt.json'
        if target.exists():
            saved = read_json(target, 128*1024)
            if saved.get('request_sha256') != web.key(args):
                raise ValueError('Research effect identity changed.')
            return saved.get('result') or {'status': 'INTERRUPTED',
                'reason': 'Research outcome is unknown; no automatic repeat.', 'authority': 'NONE'}
        receipt = {'schema': web.SCHEMA, 'request_sha256': web.key(args), 'question_sha256': identity, 'input_id': input_id,
            'status': 'RUNNING', 'calls_reserved': calls, 'calls_used': calls, 'usage': None, 'attempts': []}
        save_json(job/'receipt.json', receipt)
        failures, spent = [], 0
        # Fallback is allowed only before generation. A paid/uncertain result is
        # returned to the coordinator instead of silently starting another CLI.
        from .turn_engine import usage_limited
        from .environment import allows, switched_off
        from .execution_scope import pins_route
        from .native_routes import executed_attempt, executed_decision, settle_attempt
        visited, skipped = [], []
        launched = None
        mode = frozen.get('origin')
        if mode not in {'CONFIGURED', 'EXACT_TEMPORARY_PIN'}:
            mode = 'UNKNOWN'
        def decision(name):
            receipt['executed_decision'] = executed_decision(mode, name, visited, skipped)
        for name in order:
            visited.append(name)
            deployment = frozen['routes'][name]
            if not allows(deployment)[0] or usage_limited(deployment):
                blocked = 'DISABLED_OR_USAGE_LIMITED', 'Disabled or quota-limited.'
            elif (off := switched_off('researcher', deployment, name)) and not pins_route(current().pin('researcher'),
                                                                                         deployment):
                blocked = 'SWITCHED_OFF_FOR_ROLE', 'SWITCHED_OFF_FOR_ROLE: ' + off  # a route the coordinator names too
            else:
                blocked = None
            if blocked:
                skipped.append((name, blocked[0]))
                failures.append({'route': name, 'reason': blocked[1]})
                if launched is not None:
                    decision(launched)
                    save_json(job/'receipt.json', receipt)
                continue
            from .research_cli import effort
            selected_effort=effort(deployment)
            receipt.update(route=name, model=deployment['native_profile']['model'], effort=selected_effort)
            save_json(job/'receipt.json', receipt)
            allowance = None if calls is None else calls-spent
            attempt = None
            def on_dispatch(dispatched):
                nonlocal attempt, launched, mode
                if attempt is not None:
                    raise ValueError('Duplicate research dispatch notification.')
                if current().pin('researcher'):
                    mode = 'EXACT_TEMPORARY_PIN'  # effort() already required the applied pin.
                attempt = executed_attempt(deployment, {'status': 'DISPATCHING'})
                attempt.update(route=name, model=dispatched['model'], runtime_model=dispatched['model'],
                    reasoning=dispatched['effort'], endpoint=dispatched['endpoint'],
                    resume=dispatched['resume'], calls_used='UNKNOWN', money='UNKNOWN')
                receipt['attempts'].append(attempt)
                launched = name
                decision(name)
                save_json(job/'receipt.json', receipt)
            answer = research(deployment, args['question'], root=self.root, control=control, max_calls=allowance,
                              refresh=args.get('refresh', False), on_dispatch=on_dispatch)
            if attempt is not None:
                physical = answer.get('process') or {}
                status = ('CANCELLED' if physical.get('cancelled') else 'TIMEOUT' if physical.get('timeout')
                          else 'NOT_STARTED' if answer.get('not_started') else answer.get('status', 'FAILED'))
                settle_attempt(attempt, {'status': status})
                attempt['calls_used'] = answer['calls_used'] if type(answer.get('calls_used')) is int else 'UNKNOWN'
            if answer.get('not_started'):
                skipped.append((name, 'NOT_STARTED'))
            if launched is not None:
                decision(launched)
            observed = answer.get('calls_used', allowance)
            spent = None if observed is None else spent+observed
            receipt.update(usage=answer.get('usage'), calls_used=spent,
                           model=answer.get('model', receipt['model']), effort=selected_effort,
                           error=answer.get('error'), session_reused=answer.get('session_reused'))
            save_json(job/'receipt.json', receipt)
            if answer.get('not_started') and not args.get('route') and spent is not None and (calls is None or calls-spent >= 2):
                failures.append({'route': name, 'reason': answer.get('error'), 'not_started': True})
                save_json(job/'receipt.json', receipt)
                continue
            if answer.get('status') == 'COMPLETED':
                receipt['proposal'] = answer['report']
                result = web.verified_report(self.root, answer['report'], control, refresh=args.get('refresh', False))
                if result['status'] == 'COMPLETED':
                    receipt.pop('proposal', None)
            else:
                result = {'schema': web.SCHEMA, 'status': answer.get('status', 'FAILED'),
                    'reason': answer.get('error', 'Research did not produce verified evidence.'),
                    'findings': [], 'sources': [], 'remaining': ['Internet research is incomplete.'], 'authority': 'NONE'}
            result.update(route=name, model=receipt['model'], effort=selected_effort, usage=receipt['usage'], reused=False, skipped=failures)
            receipt.update(status=result['status'], result=result, finished_at=time.time())
            save_json(job/'receipt.json', receipt)
            web.trim_reports(self.root)
            return result
        fallback = self.search_fallback(request, args, control, calls_used=spent, failures=failures)
        if fallback is not None:
            return fallback
        result = {'status': 'UNAVAILABLE', 'reason': 'No configured researcher connection could start this research.',
                  'details': failures, 'authority': 'NONE',
                  'next': 'Use search_web for direct API search, then read_url on relevant results.'
                          if 'search_web' in self.names else 'Configure an available researcher connection.'}
        receipt.update(status='UNAVAILABLE', calls_used=spent, result=result, finished_at=time.time())
        save_json(job/'receipt.json', receipt)
        return result

    def execute(self, request, control):
        name = request.tool_call.name
        if name not in self.specs:
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            args = object_json(request.tool_call.arguments_json)
            if name == 'search_web':
                result = search_api.search(self.root, args, request.effect_id, self.session_id, control)
            else:
                result = self.read(args, control) if name == 'read_url' else self.investigate(request, args, control)
            return EffectResult(request.effect_id, 'succeeded', encode(result), True)
        except ImportError as exc:
            return EffectResult(request.effect_id, 'refused', encode({'status': 'UNAVAILABLE', 'capability': name,
                'reason': 'This tool is missing an installed dependency: '+str(exc)[:240],
                'next': 'Explain this tool failure; the conversation can continue. No automatic retry.'}))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)[:400]}))

    def execute_many(self, requests, control):
        # Sequential effects keep one owner of the finite researcher budget.
        return [self.execute(r, control) for r in requests]

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending
        from .native_agent import read_json
        request = pending(journal, self.session_id)
        if request is None or request.tool_call.name not in self.specs:
            return self.tools.recover_effect(journal)
        folder = 'web-search' if request.tool_call.name == 'search_web' else 'web-research'
        target = regular(self.root/folder/web.key(request.effect_id)/'receipt.json')
        row = read_json(target, 128*1024) if target.exists() else {}
        args = object_json(request.tool_call.arguments_json)
        result = row.get('result') if row.get('request_sha256') == web.key(args) else None
        if result is None and request.tool_call.name == 'research' and not args.get('route'):
            # A crash after HTTP but before the wrapper receipt can settle from
            # the exact saved API result. Unknown HTTP is never dispatched again.
            api = regular(self.root/'web-search'/web.key(request.effect_id)/'receipt.json')
            saved = read_json(api, 64*1024) if api.exists() else {}
            search_args = self.search_args(args)
            if saved.get('request_sha256') == web.key(search_args) and saved.get('result'):
                result = self.discovery(saved['result'])
        return request, EffectResult(request.effect_id, 'succeeded' if result else 'refused',
            encode(result or {'status': 'INTERRUPTED', 'reason': 'Research outcome is unknown; no automatic repeat.'}), bool(result))

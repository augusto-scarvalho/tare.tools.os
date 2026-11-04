"""The existing native loop, restricted to search APIs and URL reading; one reusable connection per profile."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from jsonschema import Draft7Validator
from compute_plane.native_journal import NativeJournal, custody_lock, regular, save_json
from compute_plane.native_models import NativeModelAdapter, encode
from tare_tools_agent_runtime import ContextPolicy, ModelFailure, NativeBudget, NativeContext, NativeSession

from . import native_generation, web_research as web


def research(deployment, question, *, root, control, max_calls, refresh=False, on_dispatch=None):
    from .research_cli import REPORT, effort
    from .research_tools import ResearchTools
    from .native_agent import prompt_capsule, read_json
    from .native_spend import compose
    from .openrouter import is_route, observer
    if not is_route(deployment):
        return {'status': 'FAILED', 'error': 'Choose an admitted OpenRouter research connection.',
                'not_started': True, 'calls_used': 0}
    directory = regular(Path(root)/'research-connections'/web.key(deployment['native_profile']))
    directory.mkdir(parents=True, exist_ok=True)
    observations = []
    dispatched = None
    def generations():
        return [row for row in observations if row.get('purpose') in {'model', 'summary'}]
    def usage():
        return {'reported_calls': [r.get('usage') for r in generations()],
                'routing': [{k: r.get(k) for k in ('purpose', 'status', 'error', 'openrouter')} for r in generations()],
                'scope': 'Current invocation; absent fields remain unknown.'}
    with custody_lock(directory):
        path = directory/'api-connection.json'
        saved = read_json(path, 65536) if path.exists() else {}
        existed = bool(saved)
        budget_note = ('No per-question model-call ceiling; the enclosing execution limits still apply.' if max_calls is None else
                       f'This question has at most {max_calls} model calls, including the final report. Reserve a call to deliver.')
        if not saved:
            saved = {'session_id': uuid4().hex, 'system': prompt_capsule(deployment, 'researcher', 'leaf')['system'],
                     'instruction': 'Research this question using the admitted web tools. Finish by calling tare_deliver with the report JSON.\n'+question+'\n'+budget_note,
                     'profile': deployment['native_profile']}
            save_json(path, saved)
        session_id = saved['session_id']
        try:
            profile = native_generation.deployment_profile(deployment, deployment['native_profile']['model'])
            def credential():
                import os
                name = deployment['native_credential_env']
                value = os.environ.get(name) or native_generation.vault_secret(name)
                if not value:
                    raise ModelFailure('CREDENTIAL_UNAVAILABLE', not_started=True)
                return value
            # Resolve only custody status, not a paid request, before reserving a dispatch.
            credential()
            from .native_limits import http_options, resolve
            client = native_generation.inference_client(deployment, profile, credential=credential,
                                                        **http_options(resolve(deployment)))
            inner = SimpleNamespace(names=(), tool_specs_json='{}', session_id=session_id, binding=control.binding,
                                    recover_effect=lambda journal: None)
            tools = ResearchTools(inner, directory, {'research': {'search_api': 1, 'routes': {}, 'route_order': []}})
            tools.specs = {n: s for n, s in tools.specs.items() if n in {'read_url', 'search_web'}}
            tools.names = tuple(tools.specs)
            tools.tool_specs_json = encode(tools.specs)
            original_execute = tools.execute
            used_web = []
            def execute(request, execution):
                result = original_execute(request, execution)
                if result.status == 'succeeded':
                    used_web.append(request.tool_call.name)
                if max_calls is not None:
                    output = json.loads(result.output_json)
                    output['_research_model_calls_remaining'] = max(0, max_calls-len(generations()))
                    result = replace(result, output_json=encode(output))
                return result
            tools.execute = execute
            delivered = []
            def validate_report(value):
                Draft7Validator(REPORT).validate(value)
                delivered.append(value)
                return value
            adapter = NativeModelAdapter(profile, client, system=saved['system']+'\nReport schema:\n'+encode(REPORT),
                tool_specs_json=tools.tool_specs_json, counter_protocol='utf8-envelope-estimate-v1',
                delivery_schema_json=encode(REPORT), validate_delivery=validate_report,
                observe=observer(deployment, client, observations.append))
            paid = compose(adapter, deployment, NativeJournal(directory/'spend.jsonl'))
            limit_hit = False
            class BoundedModel:
                def __getattr__(self, name):
                    return getattr(paid, name)
                def generate(self, request, execution):
                    nonlocal limit_hit
                    if max_calls is not None and len(generations()) >= max_calls:
                        limit_hit = True
                        raise ModelFailure('RESEARCH_MODEL_CALL_LIMIT')
                    return paid.generate(request, execution)
            model = BoundedModel()
            journal = NativeJournal(directory/'journal.jsonl')
            from .execution_resources import selected
            session = NativeSession(session_id=session_id, instruction=saved['instruction'], capabilities=tools.names,
                model=model, tools=tools, journal=journal, budget=NativeBudget(max_model_calls=None,
                    max_stagnant_turns=None if selected('stagnation') else 3),
                context=NativeContext(ContextPolicy(window=profile.context_window, output_reserve=profile.output_tokens,
                    margin=4096, allow_estimated_counts=True, max_compactions=None), counter=model.count, summarizer=model))
            if existed:
                # A new question authorizes a new call, never replay of an uncertain earlier one.
                # Exclusive custody proves the prior local invocation ended; remote usage stays unknown.
                events = [r.event() for r in journal.snapshot().records]
                last = events[-1] if events else {}
                if last.get('kind') in {'model_requested', 'invocation_interrupted'}:
                    call_id = last.get('call_id') if last['kind'] == 'model_requested' else last.get('invocation_id')
                    session.abandon_interrupted_model(control, call_id=call_id, resolution_id=uuid4().hex,
                                                       owner_interrupted=last['kind'] == 'model_requested')
                recovered = tools.recover_effect(journal)
                if recovered is not None:
                    session.reconcile_effect(control, *recovered)
                session.continue_with(control, message_id=uuid4().hex,
                    text=question+'\n'+budget_note+('\nRefresh the sources with web tools now.' if refresh else ''), interrupts_turn=True)
            dispatched = {'endpoint': profile.endpoint, 'model': profile.model, 'effort': effort(deployment), 'resume': existed}
            if on_dispatch:
                on_dispatch(dict(dispatched))
            outcome = session.run(control)
            if limit_hit or outcome.status != 'proposed' or not delivered:
                return {'status': 'FAILED', 'error': 'RESEARCH_MODEL_CALL_LIMIT' if limit_hit else outcome.reason,
                        'calls_used': len(generations()), 'usage': usage(), 'session_reused': existed, 'dispatched': dispatched}
            report = delivered[-1]
            Draft7Validator(REPORT).validate(report)
            if not used_web and (refresh or not saved.get('web_tool_observed')):
                raise ValueError('Research returned no observed web tool use.')
            saved['web_tool_observed'] = bool(used_web) or saved.get('web_tool_observed', False)
            save_json(path, saved)
            return {'status': 'COMPLETED', 'report': report, 'model': profile.model, 'calls_used': len(generations()),
                    'usage': usage(),
                    'session_reused': existed, 'dispatched': dispatched,
                    'cache_note': 'Stable native researcher session and tool prefix; hits require provider usage evidence.'}
        except Exception as error:
            # Never include provider bodies, request text or credentials in a diagnostic.
            return {'status': 'FAILED', 'error': getattr(error, 'code', type(error).__name__),
                    'calls_used': len(generations()), 'usage': usage(), 'session_reused': existed,
                    **({'dispatched': dispatched} if dispatched else {'not_started': True})}

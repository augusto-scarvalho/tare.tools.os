"""Small demand records and role boundaries for a persistent conversation.

The coordinator interprets intent; this host owns execution preconditions. Records
contain references and short decisions, not another copy of the conversation.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode, object_json
from .candidate_checks import ScopeRepair


FILE = 'conversation-tasks.json'
MAX_BYTES, MAX_TASKS = 65536, 16
BACKLOG_CHECK = 'tare-backlog-validate'
READ_TOOLS = {'read', 'list', 'search', 'context', 'explore', 'folder_access', 'history', 'backlog_read', 'read_url', 'research', 'search_web'}
FOLDER_READS = {'folder_list', 'folder_read', 'folder_search'}
PROJECT_LOOKUPS = {'read', 'list', 'search', 'context', *FOLDER_READS}
ROUTING_LOOKUPS = 3
# The capsule answers a question by reading the project, without demand; its fourth read was refused, and
# coordinators then answered from half the files (2026-11-02/03). Only a longer discovery asks for intent.
QUESTION_LOOKUPS = 8
INTENT_JEV_MODEL = 'jev-1.14.0'
INTENT_QUESTION = (
    'Classify the final outcome the USER requested, not the next tool or workflow stage. '
    'A delivery can include exploration, planning, checks and audit. A versioned/written report is a delivery; '
    'a report only in chat is investigation. Preserve explicit plan-only/read-only/no-edit limits. '
    'A folder-read consent or host annotation does not change the original requested outcome. '
    'Recent explicit USER changes override the earlier request; model suggestions are not user authorization.')
INTENT_OPTIONS = {
    'answer': 'Explain an existing fact, concept or status. No investigation or saved file change is requested.',
    'investigate': 'Inspect/read/research sources and explain findings in the conversation. No repository file or versioned artifact is requested.',
    'plan': 'Produce a plan or design and stop there. The user explicitly wants planning/advice only, without implementing or writing the proposed files.',
    'deliver': 'Complete a requested file change. Includes creating/editing code, tests, documentation, reports, JSON/CSV/data files, even when based only on existing evidence. Planning/research are intermediate stages of this outcome.',
    'uncertain': 'The final requested outcome cannot be established from the user request and available user context.'}
GUIDANCE = (
    'You coordinate one conversation, with separate planner, implementer and auditor roles. '
    'For a requested change or an explicit formal plan, call demand to record the goal and intended outcome. '
    'Understand the current conversation: a reply to a pending question, a request to continue, '
    'or a correction normally belongs to the current demand, not a new task. '
    'Read project instructions and only enough facts to route the work; delegate multi-file discovery '
    'to explore and reuse its findings. Start unfamiliar-project discovery with one focused explorer, without '
    'a whole-scope split; expand only for distinct remaining questions. Ask the user only '
    'for consequential choices the project cannot answer. Ordinary explanations need no demand. '
    'Use read_url directly for a supplied URL, without spawning a researcher. Use research for internet '
    'discovery/comparison. When the user asks to use configured search APIs, call search_web with that provider; '
    'do not implement API clients or research their contracts. Consult product_help topic=web for availability. '
    'When researcher/native search is unavailable, use search_web directly through '
    'a configured search API, then read_url on relevant results. Search snippets are discovery, not read '
    'page evidence. Do not repeat an uncertain paid search automatically. Explore investigates project files. '
    'Cite the consulted sources, distinguish '
    'observations from document claims and proposals, and report actual unavailable tools/access failures. '
    'Consult product_help for the installed Tare contract; project tools inspect the current project. '
    'Never infer Tare features or schemas from system prompts. '
    "Answer simple questions, explanations, quick comparisons and informal advice such as 'como você faria?' "
    'directly in the conversation, reading the project if needed, without demand, plan or delegate_plan. '
    'Require the separate planner only for a requested change or an explicit formal plan. '
    'For these requests record demand with intent=deliver or intent=plan, then call delegate_plan; '
    'a formal plan alone does not authorize implementation. '
    'Summarize a returned plan in a few lines and ask at most two decision questions. '
    'Hand off the user objective and confirmed choices as instructions; '
    'put observed facts and source locations in context, label your suggestions as unvalidated hypotheses '
    'and ask the planner to compare alternatives. Do not turn your own recommendation into a user requirement. '
    'Simple explanations need no planner. State assumptions and use reproducible calculations for numerical '
    'claims; code inspection alone does not establish measured gameplay, cost or quality. '
    'For delivery, then use implement; Tare verifies '
    'and independently audits its candidate before applying it. You cannot edit or execute commands '
    'yourself. A small change needs a small plan, not a bypass. Reuse a valid plan for corrections; '
    'revise the demand when its goal or criteria change. A plan-only request never authorizes edits. '
    'When the user explicitly requests implementer reasoning, pass that supported level as implement.effort. '
    'The coordinator reasoning does not configure its implementer; omitting effort uses the implementer route default. '
    'Otherwise omit effort overrides: configured models already have settings. '
    'A refused delegation produced no plan; correct its arguments or use an available configured planner, '
    'never substitute your own plan. Read source lines with start/end or limit; offset is for JSON pointers. '
    'Before file changes, describe the action in one sentence; the existing inline permission dialog is the '
    'approval, without another confirmation request in chat. Do not send the user to settings '
    'or ask them to restart. The coordinator stays read-only while the authorized implementer works. '
    'Report pending checks, missing routes and audit findings honestly. Never describe an unaccepted '
    'candidate as delivered. Ask questions and answer progress queries naturally. Write every message, progress '
    'notes and the final summary included, in the language of the user latest message, even when tool results are '
    'in English. '
    'Never use internal jargon in operator-facing replies (host, auditor, worker, demanda, receipts, pipeline). '
    'On failure cite the recorded reason and the next step, never invent a cause.'
)
SPEC = {
    'product_help': {
        'description': 'Consult the installed Tare product contract, not the current project. Use for Tare workflow, '
            'permission, web search or backlog setup questions before guessing commands or searching the internet. '
            'Read-only, local, no model call; includes the installed version and owner references.',
        'parameters': {'type': 'object', 'properties': {'topic': {
            'type': 'string', 'enum': ['workflow', 'permissions', 'backlog', 'web']}},
            'required': ['topic'], 'additionalProperties': False}},
    'demand': {
        'description': 'Record or continue the user demand, then delegate technical discovery and planning as needed; '
            'ask only for unresolved user decisions. Preserve the current demand on replies and corrections. '
            'A changed goal/criteria requires relation=revise and a new plan. A new intent may use the configured cheap Jev check.',
        'parameters': {'type': 'object', 'properties': {
            'intent': {'type': 'string', 'enum': ['investigate', 'plan', 'deliver', 'answer']},
            'relation': {'type': 'string', 'enum': ['new', 'continue', 'correct', 'revise'],
                'description': 'continue/correct reuses the recorded goal, criteria and plan: omit objective and criteria.'},
            'objective': {'type': 'string', 'description': 'For new/revise only; omit when continuing or correcting. The result the user wants; how they '
                'asked you to work (explore or research first) guides your own steps.'},
            'criteria': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 16,
                'description': 'For new/revise only; omit when continuing or correcting. Each one a property of the delivered '
                'result that a reviewer can check in the changed files, tests or command output; requested working steps '
                '(explore, research or plan first) are not criteria. Never weaken criteria to fix tool errors.'},
            'question': {'type': 'string', 'description': 'Empty unless project inspection leaves an essential user '
                'decision. When nonempty, ask it and wait; planning/execution cannot proceed until the reply.'},
        }, 'required': ['intent', 'relation'], 'additionalProperties': False}},
    'implement': {
        'description': 'Execute the saved separate planner\'s plan with an implementer. Tare runs checks '
            'and an independent auditor before applying the candidate. Use the same plan to address audit '
            'findings. Set recheck_candidate=true after fixing a failure of the Tare checks or environment to reverify and '
            'independently audit the preserved candidate without another implementer call. Does not run when '
            'the demand is only to investigate or plan.',
        'parameters': {'type': 'object', 'properties': {
            'note': {'type': 'string', 'description': 'Relevant new findings or audit corrections, at most 2000 characters.'},
            'route': {'type': 'string'},
            'recheck_candidate': {'type': 'boolean', 'description': 'Reuse an unapplied retained candidate; checks and independent audit still must pass. No implementer inference or producer change.'},
            'effort': {'type': 'string', 'enum': ['low', 'medium', 'high', 'xhigh'],
                       'description': 'Required when the user explicitly requests implementer reasoning: pass that supported level. The coordinator reasoning does not set this level. Otherwise omit to preserve the configured implementer route settings.'},
        }, 'additionalProperties': False}},
    'check': {
        'description': 'Run configured project checks or the Tare backlog graph check from the saved plan in a private candidate. '
            'A verification-only request needs no new demand, plan, implementation or auditor and authorizes no edits. '
            'Use check names, not test file paths. The saved plan exposes scoped test checks; '
            'a refused name returns their exact commands and any equivalent retry. No arbitrary command arguments.',
        'parameters': {'type': 'object', 'properties': {
            'names': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 16},
        }, 'required': ['names'], 'additionalProperties': False}},
}


def product_help(state, topic):
    from importlib.metadata import version
    common = {'product': 'tare.tools', 'version': version('tare-tools-os'), 'topic': topic,
              'basis': 'Installed product contract; project contents are separate.', 'authority': 'NONE'}
    if topic == 'workflow':
        return {**common, 'source': 'relay/conversation_tasks.py', 'steps': [
            'Record an actionable request with demand. Explore only missing project facts.',
            'Use delegate_plan for the configured separate planner.',
            'Use implement for a delivery request after an executable plan; Tare verifies and independently audits before applying.'],
            'intents': {'deliver': 'A request such as "podemos criar o backlog?" asks to start the work.',
                       'answer': 'A how-to question asks for an explanation; it does not authorize edits.',
                       'plan': 'An explicit plan-only request ends with the plan.'},
            'permissions': 'Tare asks inline for the implementer permission when needed. '
                'Read-only coordinator capabilities are compatible with starting this workflow.',
            'conversation': 'Natural-language requests use this workflow; slash commands are optional.',
            'available_checks': list({**state['config']['verifiers'], **detected_checks(state['config'])})}
    if topic == 'permissions':
        return {**common, 'source': 'relay/conversation_permissions.py',
            'coordinator': 'Reads facts and delegates; never edits or runs project commands directly.',
            'implementer': 'implement requests a scoped grant for the saved plan, inline: once, remember for the project, or deny.',
            'plan': 'In the TUI, /permissions > Before implementing: Show me the plan and ask (default) makes implement first '
                'show the user the whole plan: Implement this plan, Change the plan or Deny. Go straight on skips it. '
                'A project without its own choice uses My default, set once for every project; '
                'runs without the TUI never show it.',
            'settings': '/permissions inspects project defaults. It is not a prerequisite for planning or delegation.',
            'writable': 'An empty coordinator write scope is expected; implementer grants do not add coordinator edit tools.'}
    if topic == 'web':
        from .web_search import PROVIDERS, selection
        configured, skipped = selection()
        return {**common, 'source': ['relay/research_tools.py', 'relay/web_search.py'],
            'read_url': 'Reads a supplied URL with the local reader without another model.',
            'research': 'A separate CLI researcher for multi-source investigation when configured and available.',
            'search_web': 'search_web' in state.get('conversation_tool_names', []) or
                          (state.get('research') or {}).get('search_api') == 1,
            'search_services': list(PROVIDERS), 'configured_services': configured, 'unavailable': skipped,
            'setup': 'Set up Tare > Web search. Add one API key to the secure vault; choose which service to try first.',
            'evidence': 'Search returns untrusted snippets and source links. Read relevant URLs before page-backed claims.',
            'cost': 'Search API requests use provider credit, not another model call. Opening setup sends no search.',
            'saved_conversations': 'Saved toolsets stay frozen. Existing research can fall back to direct API discovery when no native researcher can start; no prompt or toolset rewrite is needed.'}
    if topic != 'backlog':
        raise ValueError('Choose workflow, permissions, web or backlog.')
    from graph_backlog.adapters import MarkdownAdapter
    from graph_backlog.validation import validate_work_graph
    starter = MarkdownAdapter.from_markdown('')
    if validate_work_graph(starter)['status'] != 'PASS':
        raise ValueError('The installed Backlog Graph starter did not validate.')
    return {**common, 'source': ['relay/project_graph.py', 'graph_backlog.adapters.MarkdownAdapter',
                               'graph_backlog.validation.validate_work_graph'],
        'graph': state['config'].get('project_graph') or None,
        'format': 'A Backlog Graph JSON document. work-graph.json is a conventional filename, not an automatic binding.',
        'create': 'For a creation request, inspect existing project requirements and pass this contract to delegate_plan, '
            'then implement. Reuse recorded tasks. If there are none, start with the empty valid graph as a reversible '
            'baseline; do not ask for confirmation merely to choose empty over invented tasks. Preserve existing files. '
            'An empty starter records no invented tasks.',
        'starter': starter,
        'authoring': backlog_authoring_contract(),
        'verification': {'name': BACKLOG_CHECK, 'scope': 'Planned JSON graph files; Tare runs the installed Backlog Graph validator in the private candidate.'},
        'binding': 'Creating a file does not link it automatically. /backlog selects an existing valid graph for new '
            'conversations; saved conversations retain their binding. Report creation and binding separately.',
        'plan': '/plan produces a plan; it does not automatically create a graph or authorize implementation.'}


def backlog_authoring_contract():
    """Deterministic owner-generated example; no project scan or model call."""
    from graph_backlog.adapters import MarkdownAdapter
    from graph_backlog.core import VALID_STATUSES, VALID_STALENESS
    from graph_backlog.validation import validate_work_graph
    example = MarkdownAdapter.from_markdown('- [ ] EXAMPLE-001: Documented pending task')
    # Examples are stable across calls/processes, not fabricated project observations.
    example['meta']['generated_at'] = '2000-01-01T00:00:00Z'
    example['nodes'][0].update(observed_at='2000-01-01T00:00:00Z', source_refs=['example-doc'])
    example['sources']['example-doc'] = {'kind': 'document', 'title': 'Example evidence document',
        'recoverability': 'RECOVERABLE_REFERENCE', 'locator': 'docs/example.md'}
    if validate_work_graph(example)['status'] != 'PASS':
        raise ValueError('The installed owner-generated nonempty Backlog Graph example did not validate.')
    return {'schema': 'tare.tools/backlog-authoring/1',
        'source': ['graph_backlog.adapters.MarkdownAdapter', 'graph_backlog.validation.validate_work_graph'],
        'completion_status': sorted(VALID_STATUSES), 'staleness_state': sorted(VALID_STALENESS),
        'completion_rules': 'DONE requires completion.dod_satisfied=true. If real obligations remain, '
            'record PARTIAL with dod_satisfied=false and preserve those obligations. Use DONE only when '
            'all existing criteria have evidence. Keep work_status and completion consistent; never mark '
            'DoD satisfied merely to silence validation.',
        'derived_revision': 'The embedded top-level revision is optional and owned by Backlog transactions. '
            'Prefer backlog_read/backlog_record when available for progress on an existing node. When '
            'repairing an existing graph with file edits, remove a stale revision field; the owner validator '
            'accepts its absence. Never guess, copy or hand-compute a replacement content hash. '
            'Omitting the revision does not relax any semantic validation.',
        'existing_graph': 'Edit the requested existing items and preserve other nodes, edges, source records '
            'and real evidence. The example below demonstrates schema; never replace an existing graph with it.',
        'example': example, 'use': 'Replace example IDs, titles and sources with observed project facts. '
            'Use NOT_DONE for pending work and FRESH for newly observed facts. Repository documents use '
            'RECOVERABLE_REFERENCE with their real locator. Example timestamps are placeholders: use the host UTC '
            'time for newly created observations. Do not copy example tasks, claim completed work, or search '
            'project files or the installation to rediscover this supplied product contract.'}


def detected_checks(config):
    """Reuse the host's bounded test detector without changing saved chat identity."""
    if config.get('delegated_verifiers'):
        return deepcopy(config['delegated_verifiers'])
    if len(config['repositories']) != 1 or any(n != 'conversation-diff' for n in config['verifiers']):
        return {}  # Explicit scoped checks take precedence over automatic detection.
    from .turn_engine import config_for
    repository, root = next(iter(config['repositories'].items()))
    return {name: {**check, 'repository': repository}
            for name, check in config_for(Path(root), mode='read-only')['verifiers'].items()}


def host_checks(config, unit):
    """Closed host check, scoped to the unit; no project code or model-supplied argv."""
    if BACKLOG_CHECK not in unit['verifiers']:
        return {}
    import graph_backlog
    import sys
    paths = [p for p in unit['target_paths'] if isinstance(p, str) and p.endswith('.json')]
    if not paths:
        raise ValueError(BACKLOG_CHECK+' needs a planned JSON graph target.')
    site = str(Path(graph_backlog.__file__).resolve().parent.parent)
    program = ('import sys,json; from pathlib import Path; sys.path.insert(0,'+repr(site)+'); '
        'from graph_backlog.validation import validate_work_graph; '
        'results=[validate_work_graph(json.loads(Path(p).read_text(encoding="utf-8"))) for p in sys.argv[1:]]; '
        'print(json.dumps(results)); raise SystemExit(any(r["status"]!="PASS" for r in results))')
    return {BACKLOG_CHECK: {'repository': unit['repository'], 'argv': [sys.executable, '-I', '-c', program, *paths]}}


def delivery_view(value, *, config=None, unit=None):
    """Actionable facts for the coordinator; custody paths remain in the host records."""
    result = {k: value[k] for k in ('verdict', 'applied', 'application', 'application_files',
                                  'reason', 'files', 'route', 'checks', 'suite',
                                  'checks_before', 'checks_after', 'review_edits', 'source_state', 'implementer_report',
                                  'verification', 'summary', 'remaining', 'next', 'folder_access_request','last_attempt','implementation_stop') if k in value}
    if value.get('audit'):
        result['audit'] = {k: v for k, v in value['audit'].items() if k != 'session'}
    if result.get('last_attempt'):
        result['last_attempt']={k:v for k,v in result['last_attempt'].items() if k in {'verdict','reason','route','files'}}
    result['candidate_preserved'] = bool(value.get('candidate') or value.get('worker'))
    if value.get('worker'):
        result['next']='Ask for the requested folder through request_folder_access. After consent, continue this demand and implement; the host resumes the same worker and saved budget.'
    result['files_scope'] = 'project' if value.get('applied') else 'unapplied candidate, not project files'
    if unit and BACKLOG_CHECK in unit.get('verifiers', []):
        result['project_graph_binding'] = graph_binding(config or {})
    if (value.get('audit') or {}).get('verdict') == 'INCONCLUSIVE' and not value.get('applied'):  # applied: the user's decision
        audit = value['audit']
        result['reason'] = ('Audit ('+audit['audit_label']+') incomplete: ' if audit.get('audit_label') else 'Independent audit incomplete: ')+str(
            audit.get('failure_code') or audit.get('reason') or 'No accepted verdict')
        result['next'] = 'Explain the concrete audit failure and stop. The candidate was not applied. '
        result['next'] += 'Name audit.actor.model as the selected auditor. A local call/context/time limit is not evidence of exhausted provider quota. '
        result['next'] += 'Do not repeat implementation or try reading host receipts with project tools.'
        from .conversation_review import CONTEXT_FAILURES, uncovered_review
        if uncovered_review(audit):
            result['audit_state']='COVERAGE_PENDING'
            result['reason']='The auditor returned a completed partial review with unread source obligations.'
            result['audit_remaining']=deepcopy(audit['remaining'])
            result['next']=('Report the exact unread evidence and retain the candidate and completed observations. '
                'This is a coverage gap, not an execution failure or quota diagnostic. '
                'A fresh continuation can complete the saved review coverage; do not repeat implementation.')
        if audit.get('failure_code') in CONTEXT_FAILURES or audit.get('reason') in CONTEXT_FAILURES:
            result['next'] += ' These are host-owned context-admission limits, not proof of exhausted provider quota. '
            result['next'] += 'A new message does not itself change or renew them. Distinguish estimated from measured '
            result['next'] += 'tokens; preserve this candidate and plan rather than inventing a smaller delivery.'
        elif audit.get('failure_code') == 'CLI_REQUIRED_ACTION':
            result['next'] += ' The auditor requested other actions instead of the required final decision. '
            result['next'] += 'Preserve the goal, plan and candidate. Changing token/time limits or splitting the '
            result['next'] += 'delivery does not qualify this transport choice; identical paid reviews are stopped.'
        if audit.get('failure_code')=='REVIEW_PACKET_OVERSIZED' and audit.get('requires_candidate_revision'):
            result['reason']='Review preparation requires a candidate correction; no auditor started.'
            result['next']='The host stopped review preparation before any auditor ran. Preserve this candidate and all criteria; '
            result['next']+='make one bounded correction now using demand relation=correct and implement, removing only unnecessary copied review data. '
            result['next']+='Use diff_projection sizes and largest_changed_values. Do not drop required evidence or retry the unchanged packet with another model.'
        elif audit.get('requires_candidate_revision') and audit.get('summary') and audit.get('findings'):
            result['reason']='The completed '+('same-family (not independent)' if audit.get('audit_label') else 'independent')+' review identified required candidate corrections.'
            result['next']='Preserve the verdict, findings, remaining obligations and saved plan. Make one bounded correction now with demand relation=correct and implement. The host carries the full review to the implementer. An unchanged candidate does not authorize another audit.'
    elif value.get('verdict') == 'NOT_VERIFIED':
        result['next'] = 'Preserve this demand and plan. Diagnose checks_after, then use relation=correct and implement '
        result['next'] += 'to repair only the failed part; the host supplies the candidate and verifier feedback. '
        result['next'] += 'For Backlog fields use product_help(backlog). Never guess enum values, open a replacement '
        result['next'] += 'task, request installation access, or suggest direct edits to escape a failure guard.'
    elif (value.get('verdict')=='INCONCLUSIVE' and not value.get('files')
            and (value.get('implementation_stop') or {}).get('provider_stop_reason')=='error'):
        result['reason']='The inference provider ended the implementer response with an error; no files changed.'
        result['next']='Do not repeat the same failed route unchanged. Preserve the same plan, prior calls and '
        result['next']+='charges. Explain the provider failure; a fresh authorized continuation can select another '
        result['next']+='available implementer route within the current execution policy. This is not an output-token limit.'
    from .conversation_permissions import paths_allowed
    missing = [p for p in value.get('scope_needed') or []
               if not value.get('applied') and not (unit and paths_allowed([p], unit['target_paths']))]
    if missing:
        result.update(missing_target_paths=missing,
            required_action={'tool': 'delegate_plan', 'arguments': {'instruction': SCOPE_REPAIR+needed_scope(missing)}},
            next='Ask the separate planner now to add these files to the saved plan, then implement on the preserved '
                 'candidate; the user approves the new files when implementation starts. Do not open another task.')
    return result


def graph_binding(config):
    return {'changed': False, 'current_conversation': config.get('project_graph'),
        'new_conversations': 'No binding was configured by this file operation. File creation does not establish a binding.',
        'next': 'Report file creation separately. /backlog selects the graph for new conversations; saved conversations keep their binding.'}


def recovery_view(root, task):
    """Fresh host facts in tool results, without rewriting a saved prompt or historical verdict."""
    from .conversation_review import context_recovery_available, coverage_recovery_available, recorded_failure
    if not task or not task.get('delivery'):
        return None
    audit = task['delivery'].get('audit') or {}
    record = recorded_failure(root, audit)
    view = delivery_view({**task['delivery'], **({'audit': record[2]} if record else {})})
    if (task.get('verification') or {}).get('status') in {'red', 'inconclusive'}:
        view.update(verdict='NOT_VERIFIED', verification=task['verification'],
            reason='The earlier patch remains applied, but later project checks did not pass.',
            next='Report the latest diagnostic. A new delivery request can use demand relation=correct '
                 'and the same plan; do not apply the old patch again or treat its earlier checks as current.')
        return view
    if context_recovery_available(root, task['delivery'].get('audit') or {}):
        view['context_requalification_available'] = True
        view['next'] = ('The host audit execution contract changed after this recorded failure. For an unchanged '
            'user goal, record demand intent='+task['intent']+' relation=continue, then use implement with the '
            'saved plan. The host can requalify the preserved candidate audit; no replacement demand, '
            'split delivery or new technical plan is required by this host failure. '
            'A changed goal still needs explicit revised scope. Dispatch rechecks identity, budget and prior decisions.')
    elif task.get('intent')=='deliver' and coverage_recovery_available(root,task):
        view['coverage_continuation_available']=True
        view['next']='For the fresh human request to continue this delivery, use demand intent=deliver relation=continue '
        view['next']+='and then implement now. Despite the operation name, the host runs only one continuation of '
        view['next']+='the same auditor on the unchanged candidate, not another implementer. Preserve the plan and '
        view['next']+='previous review; inspect its unread material evidence. Dispatch rechecks the shared budget and '
        view['next']+='candidate identity. Do not merely describe this next action or start a new plan.'
    return view


def intent_agrees(intent, choice):
    # All three phases are read-only. A semantic label must not veto advice or authorize implementation.
    readonly = {'answer', 'investigate', 'plan'}
    return choice in {None, intent} or intent in readonly and choice in readonly


def assess_intent(root, state, intent):
    """One cheap semantic check at a new/revised demand, never at every tool call.

    Missing Jev keeps the coordinator interpretation and all execution gates.
    Disagreement asks for clarification; it never expands the user's authority.
    """
    from compute_plane.native_journal import NativeJournal
    from .turn_engine import judges
    texts = [r.event()['text'] for r in NativeJournal(Path(root)/'journal.jsonl').snapshot().records
             if r.event()['kind'] == 'user_message'] if (Path(root)/'journal.jsonl').exists() else []
    initial = state.get('instruction', '')
    from .console_entry import ASSISTANT_CONTEXT
    if initial.startswith(ASSISTANT_CONTEXT): initial = initial[len(ASSISTANT_CONTEXT):]
    # Preserve the human request rather than judging a suffix of mixed history.
    # Host consent annotations are context, never another user request.
    task = current(load(root))
    recent = []
    for text in texts[-3:]:
        human, separator, note = text.partition('\n\n[tare] ')
        if separator and note.startswith(('The user granted read access to ', 'This reply did not grant read access to ')):
            recent.append({'text': human, 'runtime_note': '[tare] '+note})
        else:
            recent.append({'text': text})
    message = encode({'original_user_request': initial,
        'recorded_goal': task['objective'] if task else None,
        'pending_question': task.get('question') or None if task else None,
        'recent_user_messages': recent})
    request = {'state': message, 'question': INTENT_QUESTION, 'options': INTENT_OPTIONS}
    from compute_plane.choice_assessment import validate_request
    try:
        validate_request(request)
    except ValueError as error:
        return {'status': 'UNDERSTOOD', 'assessment': {'source': 'coordinator', 'failure': str(error)}}
    from .turn_engine import deciding
    verdicts = judges(request, local=False, timeout=2, model=INTENT_JEV_MODEL)
    answer = deciding(verdicts) or {'source': 'coordinator'}
    if 'choice' not in answer:
        failures = {name: value['failure'] for name, value in verdicts.items() if value.get('failure')}
        if failures: answer['unavailable'] = failures
    if not intent_agrees(intent, answer.get('choice')):
        return {'status': 'NEEDS_CLARIFICATION', 'assessment': answer,
                'next': 'Clarify the user\'s intended outcome in ordinary language before requesting implementation.'}
    return {'status': 'UNDERSTOOD', 'assessment': answer}


def load(root):
    from .native_agent import read_json
    path = regular(Path(root)/FILE)
    return read_json(path, MAX_BYTES) if path.exists() else {'schema': 'tare.tools/conversation-tasks/1',
                                                          'current': None, 'tasks': []}


def current(value):
    return next((t for t in value['tasks'] if t['id'] == value['current']), None)


def delivery_notice(root, invocation):
    """The host's line under a turn whose delivery was not applied: no reply reads as a finished change (a local
    coordinator summarized an unapplied candidate as "what changed")."""
    task = current(load(root)) or {}
    delivery = task.get('delivery') or {}
    if task.get('delivery_invocation') != invocation or not delivery or delivery.get('applied'):
        return None
    return '[tare] Not applied: the project files are unchanged' + (
        '; the candidate is preserved.' if delivery.get('candidate') or delivery.get('worker') else '.')


def retained_delivery(output):
    result={k:output[k] for k in ('verdict','applied','files','reason','route','receipt','targets',
        'source_state','candidate','worker','folder_access_request','implementer_report','implementation_stop',
        'scope_needed') if k in output}
    if output.get('audit'):
        result['audit']={k:output['audit'][k] for k in ('verdict','route','session','reason','failure_code','budget','actor',
            'requires_candidate_revision','diff_projection','delivery_validation','provider_error')
                         if k in output['audit']}
    return result


def recover_unapplied_delivery(root,state,value):
    """Recover a completed host receipt after a task-record save failed; never infer or repeat application."""
    from datetime import datetime
    from .work_units import producer_reference, candidate_producer
    from .native_agent import read_json
    task=current(value)
    if not task:return False
    delivery=task.get('delivery') or {}
    lost=(task.get('stage')=='PENDING' and not delivery.get('candidate')
          and delivery.get('verdict')=='INCONCLUSIVE')
    if task.get('stage') not in {'IMPLEMENTING','AUDITING'} and not lost:return False
    attempt=next((i for i in state.get('invocations',[]) if i['id']==task.get('attempt_invocation')),None)
    if not attempt or not attempt.get('ended_at'):return False
    preceding={}
    if lost:
        latest=regular(Path(task['delivery'].get('receipt','')))
        if not latest.resolve().is_relative_to(Path(root).resolve()/'units') or not latest.is_file():return False
        latest_record=read_json(latest,1024*1024)
        if (latest_record.get('effect_id')!=task.get('attempt_effect')
                or latest_record.get('unit',{}).get('key')!=task['id']
                or latest_record.get('result',{}).get('applied')
                or latest_record.get('result',{}).get('candidate')):return False
        # Legacy cancellation overwrote the previous delivery. Bind recovery to
        # earlier completed implement effects in this same Runtime invocation,
        # never to a guessed newest file or an unrelated older conversation.
        from compute_plane.native_journal import NativeJournal
        records=NativeJournal(Path(root)/'journal.jsonl').snapshot().records
        for record in records[attempt.get('journal_start',0):attempt.get('journal_end',len(records))]:
            event=record.event()
            if event['kind']=='effect_requested' and event['effect_id']==task.get('attempt_effect'):break
            if event['kind']=='effect_returned' and event['result']['status']=='succeeded':
                output=object_json(event['result']['output_json'])
                if output.get('candidate_preserved') and not output.get('applied'):
                    preceding[event['result']['effect_id']]=record.sequence
        if not preceding:return False
    matches=[]
    for path in (Path(root)/'units').glob(task['id']+'-*/receipt.json'):
        receipt=read_json(regular(path),1024*1024)
        if not receipt.get('finished') or not receipt.get('result'):continue
        if lost:
            if receipt.get('effect_id') not in preceding:continue
        elif task.get('attempt_effect'):
            if receipt.get('effect_id')!=task['attempt_effect']:continue
        else:
            # Older writers lacked effect IDs. Recover only one receipt contained
            # in this exact ended invocation, with the same immutable plan scope.
            started=datetime.fromisoformat(receipt['started'].replace('Z','+00:00')).timestamp()
            finished=datetime.fromisoformat(receipt['finished'].replace('Z','+00:00')).timestamp()
            if not attempt['started_at']-1<=started<=finished<=attempt['ended_at']+1:continue
        plan=task.get('plan',{}).get('unit',{})
        if any(receipt.get('unit',{}).get(k)!=plan.get(k) for k in ('key','repository','target_paths','criteria','test_paths')):continue
        result=receipt['result'];candidate=result.get('candidate')
        if result.get('applied') or not candidate:continue
        patch=regular(Path(candidate['patch']))
        if (not patch.resolve().is_relative_to(Path(root).resolve()/'units')
                or hashlib.sha256(patch.read_bytes()).hexdigest()!=candidate['sha256']
                or source_state(state['config'],candidate['targets'])!=candidate['basis']):continue
        matches.append((path,result,preceding.get(receipt.get('effect_id'),0)))
    if lost and matches:
        latest=max(row[2] for row in matches)
        matches=[row for row in matches if row[2]==latest]
    if len(matches)!=1:return False
    path,result,_=matches[0]
    candidate={**result['candidate'],'producer':producer_reference(path.parent,candidate_producer(root,result['candidate']['producer']))}
    # The receipt keeps the whole audit: files it requires outside the plan still gate the next plan.
    needed=audit_scope(result,plan)
    delivery=retained_delivery({**result,'candidate':candidate,'receipt':str(path),
                                **({'scope_needed':needed} if needed else {})})
    if lost:delivery['last_attempt']=task['delivery']
    task.update(stage='PENDING',delivery=delivery)
    # Only the matching interrupted attempt can supply provenance. Lost-candidate
    # recovery restores older work, not the identity of the latest attempt.
    if not lost:
        task['delivery_plan_sha256'] = (task.get('attempt_plan_sha256')
            if task.get('attempt_effect') else None)
        task['delivery_invocation'] = task.get('attempt_invocation')
    save(root,value)
    return True


def save(root, value):
    while len(encode(value).encode()) > MAX_BYTES:
        old = next((t for t in value['tasks'] if t['id'] != value['current'] and (
            t['stage'] == 'DELIVERED' or t['intent'] == 'plan' and t['stage'] == 'PLANNED')), None)
        if old is None:
            raise ValueError('The bounded demand record is full. Keep goals, criteria and pending decisions concise.')
        value['tasks'].remove(old)  # full history remains in the existing journal and role artifacts
    save_json(regular(Path(root)/FILE), value, max_bytes=MAX_BYTES)


def source_state(config, paths):
    """Only the plan's literal targets, including absence; never scan the whole project."""
    result = {}
    for repository, names in paths.items():
        base = Path(config['repositories'][repository]).resolve()
        for name in names:
            path = regular(base/name)
            if not path.resolve().is_relative_to(base):
                raise ValueError('Planned source redirects outside the project.')
            if path.is_dir():
                raise ValueError('Plan literal files, not entire directories.')
            result[repository+':'+name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def proof_paths(config, unit, sources=()):
    """The files this bounded delivery depends on, rather than a project-wide scan."""
    from .work_units import test_file
    repository = unit['repository']
    paths = {repository: list(dict.fromkeys([*unit['target_paths'],
                                           *(test_file(t) for t in unit.get('test_paths', []))]))}
    for source in sources:
        repo = source['repository']
        if repo in config['repositories']:
            paths.setdefault(repo, [])
            if source['path'] not in paths[repo]: paths[repo].append(source['path'])
    return paths


def retained_candidate(task):
    """Select current private work in order; an applied record is a history barrier."""
    for key in ('replan_candidate', 'delivery', 'prior_delivery'):
        retained = task.get(key) or {}
        if retained.get('applied'):
            return None
        if retained.get('candidate'):
            return retained
    return None


SCOPE_REPAIR = ('Repair only this executable-scope omission; reuse the previous plan and preserve all criteria '
                'and the candidate. ')


def audit_scope(output, unit):
    """Files a REVISE audit says must change that the plan cannot write. The host never writes them; the planner
    adds them (ScopeRepair) and the user approves the new files when implementation starts."""
    from .conversation_permissions import paths_allowed
    audit = output.get('audit') or {}
    if output.get('applied') or audit.get('verdict') != 'REVISE':
        return []
    return [path for path in dict.fromkeys(p for f in audit.get('findings') or [] for p in f.get('paths') or [])
            if not paths_allowed([path], unit['target_paths'])]


def needed_scope(paths):
    return 'The independent audit requires changing files outside the plan: '+', '.join(paths)+'.'


def require_retained_scope(plan, previous):
    """Ask the planner to reconcile omitted outputs and audit-required files; never widen its scope here."""
    from .conversation_permissions import paths_allowed
    missing = lambda paths: [p for p in paths or [] if not paths_allowed([p], plan['unit']['target_paths'])]
    omitted = missing(previous.get('files'))
    needed = [p for p in missing(previous.get('scope_needed')) if p not in omitted]
    if omitted or needed:
        error = ScopeRepair(' '.join(filter(None, [
            omitted and 'The revised plan omits retained candidate outputs: '+', '.join(omitted)+'.',
            needed and needed_scope(needed)]))+
            ' Preserve the candidate and reconcile these paths with the recorded goal before implementation.')
        error.missing_target_paths = omitted + needed
        raise error


def replanned_delivery(config, plan, previous):
    """Keep an unverified patch; only a fresh plan can reconcile changed read dependencies."""
    require_retained_scope(plan, previous)
    candidate = previous['candidate']
    unit = plan['unit']
    observed = source_state(config, candidate['targets'])
    changed = {key for key in observed if observed[key] != candidate['basis'].get(key)}
    writes = {unit['repository']+':'+name for name in unit['target_paths']}
    writes.update(unit['repository']+':'+name for name in previous.get('files') or [])
    sources = {p['repository']+':'+p['path']: p['sha256'] for p in plan.get('sources') or []}
    if changed & writes or any(key not in sources or observed[key] is None
                              or sources[key] != observed[key] for key in changed):
        raise ValueError('The retained candidate has changed outputs or unreconciled read dependencies; '
                         'preserve it and ask the planner to inspect the affected facts.')
    result = deepcopy(previous)
    if changed:
        proof = result['candidate'].setdefault('replanned_dependencies', {})
        proof.update({key: {'before': candidate['basis'][key], 'after': observed[key]} for key in changed})
        result['candidate']['basis'] = observed
    return result  # No approval, check result, patch byte or producer identity is manufactured.


def task_plan(root, config, task, result):
    """The executable unit is derived from the actual planner artifact, never coordinator edits."""
    from .native_agent import read_json
    from .work_forms import lint, record_plan
    artifact = regular(Path(result['artifact']))
    if not artifact.resolve().is_relative_to((Path(root)/'planning').resolve()):
        raise ValueError('Planner artifact is outside this conversation.')
    plan = read_json(artifact, 65536)['payload']
    tasks = plan.get('tasks') or []
    if not tasks:
        if task['intent'] == 'deliver':
            raise ValueError('The planner must name executable tasks, literal target paths and checks. '
                'Read-only planning can inspect sources and propose implementation in the same call; '
                'it does not require another user request. Preserve its findings and repair only the '
                'missing executable plan with delegate_plan, rather than asking for exploration alone.')
        task.update(stage='PLANNED', plan={'artifact': str(artifact), 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()})
        return
    if len(config['repositories']) != 1:
        raise ValueError('Plan one project at a time; select the project before implementation.')
    repository = next(iter(config['repositories']))
    unit = {'key': task['id'], 'title': '; '.join(t['title'] for t in tasks), 'repository': repository,
            'target_paths': list(dict.fromkeys(p for t in tasks for p in t['target_paths'])),
            # A discovery/documentation subtask cannot silently replace the requested delivery.
            'criteria': list(dict.fromkeys([*task['criteria'], *task.get('inherited_criteria',[]),
                                           *(c for t in tasks for c in t['criteria'])])), 'depends_on': [],
            'test_paths': list(dict.fromkeys(p for t in tasks for p in t.get('test_paths', []))),
            'verifiers': list(dict.fromkeys(p for t in tasks for p in t.get('verifiers', [])))}
    # A promised Python test module is part of the executable check selection,
    # even when the planner listed only existing tests in test_paths. Its missing
    # baseline is red; it must exist and pass after implementation.
    from .work_units import test_file
    for path in unit['target_paths']:
        name = Path(path).name
        if (path.startswith('tests/') and name.endswith('.py')
                and (name.startswith('test_') or name.endswith('_test.py'))
                and not any(test_file(t) == path for t in unit['test_paths'])):
            unit['test_paths'].append(path)
    from .candidate_checks import admit
    admit(config['repositories'][repository],unit)
    proposal = {'units': [unit], 'criteria': plan['criteria']}
    executable = {**config, 'conversation_flow': True,
                  'verifiers': {**config['verifiers'], **detected_checks(config), **host_checks(config, unit)}}
    problems = lint(executable, proposal)
    for path in unit['target_paths']:
        if (Path(config['repositories'][repository])/path).is_dir():
            problems.append('Name literal target files, not entire directories: '+path)
    if not problems:
        from .conversation_workspace import ignored_targets
        ignored = ignored_targets(config['repositories'][repository], unit['target_paths'])
        if ignored:
            problems.append('Git ignores these new outputs, so they would be absent from the reviewed patch: '
                +', '.join(ignored)+'. Select versioned target paths in the plan; do not change ignore rules or force-track private outputs.')
    if problems:
        raise ValueError('The planner must correct its executable scope: '+'; '.join(problems))
    record_plan(root, executable, proposal)
    task.update(stage='PLANNED', plan={'artifact': str(artifact),
        'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'unit': unit,
        'sources': plan.get('sources', [])})


def recover_completed_plan(root, config, value):
    """Rebind an unchanged, completed same-revision proposal after host admission was repaired."""
    from .native_agent import read_json
    from .native_artifacts import read_input
    task = current(value)
    if (not task or task.get('intent') not in {'deliver', 'plan'} or task.get('plan')
            or task.get('stage') not in {'DISCOVERY', 'PENDING'}):
        return False
    paths = list((Path(root)/'planning').glob('*/receipt.json'))
    for receipt in sorted(paths, key=lambda p:p.stat().st_mtime_ns, reverse=True):
        try:
            from .planning_tools import record
            row = record(receipt)
            result = row.get('result') or {}
            if (row.get('status') != 'FINISHED' or row.get('demand_id') != task['id']
                    or row.get('planning_revision', 0) != task.get('planning_revision', 0)
                    or result.get('status') not in {'PLANNED','HANDOFF_TOO_LARGE'}):
                continue
            artifact = regular(Path(result['artifact']))
            if artifact.resolve() != (receipt.parent/'session/artifact.json').resolve():
                continue
            recorded = read_input(artifact)
            payload=recorded['artifact']['payload']
            if recorded['artifact']['role'] != 'planner' or not payload.get('tasks'):
                continue
            if result.get('status')=='HANDOFF_TOO_LARGE':
                # Historical oversized replies have no inline copy or digest.
                # Bind them to the actual completed model delivery, not mutable file bytes alone.
                from compute_plane.native_journal import NativeJournal
                from .native_artifacts import validate
                delivered=[r.event() for r in NativeJournal(artifact.parent/'journal.jsonl').snapshot().records
                    if r.event().get('kind')=='model_returned'
                    and r.event().get('call_id')==recorded['artifact']['call_id']]
                if (len(delivered)!=1 or validate('planner',object_json(delivered[0]['reply']['metadata_json'])['delivery_payload'])!=payload):
                    continue
            elif result.get('plan_reference'):
                if result['plan_reference']!={'sha256':recorded['sha256'],'bytes':artifact.stat().st_size}:
                    continue
            elif payload != result.get('plan'):
                continue
            sources = [p for p in payload.get('sources', []) if p['repository'] in config['repositories']]
            if any(source_state(config, {p['repository']: [p['path']]})[p['repository']+':'+p['path']] != p['sha256']
                   for p in sources):
                continue
            recovered=deepcopy(task)
            task_plan(root, config, recovered, result)
            preserved=recovered.pop('replan_candidate',None)
            if preserved is not None:
                recovered['delivery']=replanned_delivery(config,recovered['plan'],preserved)
            recovered.pop('replan_from',None)
            recovered['planning_recovered_from'] = str(receipt)
            if row.get('explicit_resume'):
                recovered['planning_resume']=deepcopy(row['explicit_resume'])
            task.clear();task.update(recovered)
            save(root, value)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False




class ConversationTools:
    def __init__(self, tools, root, state, invocation_id, *, permission_directory=None):
        self.tools, self.root, self.state = tools, Path(root), state
        self.invocation_id, self.parent_model = invocation_id, None
        self.permission_directory = permission_directory
        value=load(self.root)
        recover_unapplied_delivery(self.root,state,value)
        recover_completed_plan(self.root, state['config'], value)
        specs = object_json(tools.tool_specs_json)
        # Default deny: adding an effectful tool elsewhere cannot silently give
        # the coordinator edits, shell access or graph mutation authority.
        allowed = {n for n in tools.names if n in READ_TOOLS or n.startswith('history_')
                   or state.get('coordinator_folder_reads') and n in FOLDER_READS
                   or n in {'request_folder_access', 'delegate_plan'}}
        frozen = state.get('conversation_specs', SPEC)
        self.names = tuple(n for n in tools.names if n in allowed) + tuple(frozen)
        self.tool_specs_json = encode({**{n: specs[n] for n in allowed if n in specs}, **frozen})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def spend_committed(self):
        from decimal import Decimal
        from .conversation_review import records
        from .work_units import unit_spend
        inner = getattr(self.tools, 'spend_committed', None)
        from .native_spend import total_amounts
        return total_amounts([unit_spend(self.root), *(r.get('amount_committed', '0') for r in records(self.root)),
                              inner() if callable(inner) else Decimal(0)])

    def input_id(self):
        if self.__dict__.get('_host_input_id'):
            return self.__dict__['_host_input_id']
        from compute_plane.native_journal import NativeJournal
        path = self.root/'journal.jsonl'
        if path.exists():
            messages = [r.event() for r in NativeJournal(path).snapshot().records if r.event()['kind'] == 'user_message']
            if messages: return messages[-1]['message_id']
        return 'initial-'+self.session_id  # Resume is not another user instruction.

    def understand(self, args, effect_id=None):
        if set(args)-{'intent', 'relation', 'objective', 'criteria', 'question'}:
            raise ValueError('Use the declared demand fields.')
        intent, relation = args.get('intent'), args.get('relation')
        if intent not in {'answer', 'investigate', 'plan', 'deliver'} or relation not in {'new', 'continue', 'correct', 'revise'}:
            raise ValueError('Choose the intended outcome and whether it continues the current demand.')
        criteria = args.get('criteria', [])
        if not isinstance(criteria, list) or len(criteria) > 16 or any(not isinstance(c, str) or not c.strip() for c in criteria):
            raise ValueError('Give up to 16 concrete outcome criteria.')
        value = load(self.root); task = current(value)
        if (relation == 'new' and task and task.get('delivery') and task['stage'] != 'DELIVERED'
                and task.get('intent_input') == self.input_id()):
            raise ValueError('The current demand has an unresolved execution under this user input. '
                'Preserve its goal and plan; use relation=continue/correct with the recorded intent, omitting '
                'objective and criteria. A replacement task cannot bypass a failed verifier or retry limit.')
        boundary = value.get('input_scope') or {}
        if (boundary.get('input') == self.input_id() and boundary.get('intent') in {'answer', 'investigate', 'plan'}
                and intent == 'deliver'):
            raise ValueError('This user input was scoped to investigation or planning. Wait for a later user request to implement; opening another demand cannot expand it.')
        if (task and task.get('question') and task.get('question_input', task.get('intent_input')) == self.input_id()
                and not args.get('question') and intent != 'answer'):
            raise ValueError('Wait for the user reply before clearing this pending decision.')
        identity = hashlib.sha256(encode(args).encode()).hexdigest()
        earlier = next((t for t in value['tasks'] if (t.get('intake') or {}).get('effect') == effect_id), None) if effect_id else None
        if earlier:
            if earlier['intake']['sha256'] != identity:
                raise ValueError('Demand effect arguments changed.')
            return {'task': earlier, 'reused': True}
        if intent != 'answer' and task is None and (not isinstance(args.get('objective'), str) or not args['objective'].strip()):
            raise ValueError('No demand is recorded. Use demand relation=new with the user objective and criteria; '
                             'a rejected intake created no task to continue.')
        assessment = None
        if intent != 'answer' and (relation in {'new', 'revise'} or task is None or task['intent'] != intent):
            # Retrying an identical boundary reuses its decision, even when a
            # clarification was needed; a continuation does not pay again.
            assessment_boundary = self.input_id()
            saved = value.get('assessment') or {}
            assessment = saved.get('result') if saved.get('boundary') == assessment_boundary else assess_intent(self.root, self.state, intent)
            choice = assessment.get('assessment', {}).get('choice')
            assessment = {**assessment, 'status': 'UNDERSTOOD' if intent_agrees(intent, choice) else 'NEEDS_CLARIFICATION'}
            if assessment['status'] == 'UNDERSTOOD': assessment.pop('next', None)
            value['assessment'] = {'boundary': assessment_boundary, 'result': assessment}
            save(self.root, value)
            if assessment['status'] == 'NEEDS_CLARIFICATION':
                return assessment
        if intent == 'answer':
            # A progress question does not cancel or close the active delivery.
            value['turn'] = {'invocation': self.invocation_id, 'intent': 'answer'}
            value['input_scope'] = {'input': self.input_id(), 'intent': 'answer'}
            save(self.root, value)
            view = recovery_view(self.root, task)
            return {'stage': 'ANSWER', 'current': task, **({'delivery':view} if view else {})}
        objective = args.get('objective', '')
        question = args.get('question', '')
        if not isinstance(objective, str) or not isinstance(question, str):
            raise ValueError('Use short text for the objective and any essential question.')
        if relation == 'new' or task is None:
            if not objective.strip():
                raise ValueError('State what the user wants to achieve, or ask the essential missing question.')
            if len(value['tasks']) >= MAX_TASKS:
                completed = next((t for t in value['tasks'] if t['id'] != value['current'] and (
                    t['stage'] == 'DELIVERED' or t['intent'] == 'plan' and t['stage'] == 'PLANNED')), None)
                if completed is None:
                    raise ValueError('There are 16 unfinished demands. Resolve or review an existing one before opening another.')
                value['tasks'].remove(completed)  # the existing journal/artifacts retain its complete history
            task = {'id': 'task-'+uuid4().hex[:12], 'objective': objective, 'criteria': criteria,
                    'intent': intent, 'intent_input': self.input_id(), 'stage': 'DISCOVERY', 'question': question}
            if assessment: task['interpretation'] = assessment
            value['tasks'].append(task); value['current'] = task['id']
        elif relation == 'revise':
            if task.get('delivery') and task.get('intent_input') == self.input_id():
                raise ValueError('Execution already started under this user request. Preserve its goal and criteria; '
                    'use continue/correct for operational failures. Revise only after a new user instruction changes the goal.')
            old_objective=task['objective'];old_criteria=task['criteria']
            new_objective=objective if objective.strip() else old_objective
            new_criteria=criteria or old_criteria
            changed=new_objective!=old_objective or set(new_criteria)!=set(old_criteria)
            if changed and task.get('planning_revision_input')==self.input_id():
                raise ValueError('This user input already revised the planning scope. Preserve its criteria and remaining shared budget.')
            if changed:
                task['planning_revision']=task.get('planning_revision',0)+1
                task['planning_revision_input']=self.input_id()
            extending=new_objective==old_objective and set(old_criteria)<=set(new_criteria)
            # Rewording a goal must not erase the old planner's observed facts.
            # It remains read-only advice even when changed criteria require a
            # fresh candidate rather than inheriting the previous patch.
            if task.get('plan'):
                task['replan_from']={k:task['plan'][k] for k in ('artifact','sha256')}
            if extending and task.get('plan'):
                task['inherited_criteria']=(task['plan'].get('unit') or {}).get('criteria',task.get('inherited_criteria',[]))
            if not extending:task.pop('inherited_criteria',None)
            if (task.get('delivery') or {}).get('applied'):
                task['prior_delivery']=task['delivery']
                task.pop('replan_candidate',None)
            elif (task.get('delivery') or {}).get('candidate'):
                task['prior_delivery']=task['delivery']
                if extending:task['replan_candidate']=task['delivery']
                else:task.pop('replan_candidate',None)
            else:task.pop('replan_candidate',None)
            task.update(objective=new_objective,criteria=new_criteria,intent=intent,
                        intent_input=self.input_id(),stage='DISCOVERY',question=question)
            task.pop('plan', None); task.pop('delivery', None)
        else:
            promoting = task['intent'] == 'plan' and intent == 'deliver'
            if promoting and task.get('intent_input') == self.input_id():
                raise ValueError('The user requested only a plan. Wait for a later user request before implementing it.')
            if objective and objective != task['objective'] and not promoting or criteria and criteria != task['criteria']:
                raise ValueError('continue/correct keeps the recorded goal and criteria. Omit objective and criteria '
                    'to continue; use revise only if the user actually requested a changed outcome.')
            if task['intent'] != intent and not (task['intent'] == 'plan' and intent == 'deliver'):
                raise ValueError('Changing the requested outcome requires relation=revise.')
            task.update(intent=intent, question=question)
            task['intent_input'] = self.input_id()
            if promoting and objective:
                task['objective'] = objective  # the same immutable plan is now requested for execution
            if relation == 'correct':
                # A rejected candidate stays available for the implementer's
                # correction. An already applied patch must not be applied twice.
                if (task.get('delivery') or {}).get('applied'):
                    task['prior_delivery'] = task['delivery']
                    task.pop('replan_candidate', None)
                    task['delivery'] = None
                    # The user's correction of an applied change is a new request: it plans in a round of its
                    # own, since the three delegations guard one request against loops, not a demand's later
                    # corrections (dogfooding, a CI fix outside the saved plan's scope found none left).
                    if task.get('planning_revision_input') != self.input_id():
                        task['planning_revision'] = task.get('planning_revision', 0)+1
                        task['planning_revision_input'] = self.input_id()
                task['stage'] = 'PLANNED' if task.get('plan') else 'DISCOVERY'
        if question:
            if task['stage'] != 'WAITING_INPUT': task['waiting_from'] = task['stage']
            task.update(stage='WAITING_INPUT', question_input=self.input_id())
        elif task['stage'] == 'WAITING_INPUT':
            task['stage'] = task.pop('waiting_from', 'DISCOVERY')
        value['turn'] = {'invocation': self.invocation_id, 'intent': intent, 'relation': relation}
        if boundary.get('input') != self.input_id():
            value['input_scope'] = {'input': self.input_id(), 'intent': intent}
        if effect_id:
            task['intake'] = {'effect': effect_id, 'sha256': identity}
        save(self.root, value)
        recovery = recovery_view(self.root, task)
        planning_recovery = None
        if not question and task.get('plan'):
            from .planning_tools import MAX_REQUESTS, records, source_requalification
            prior = records(self.root)
            if sum(r.get('demand_id')==task['id'] and r.get('planning_revision',0)==task.get('planning_revision',0)
                    for r in prior) >= MAX_REQUESTS:
                repair = source_requalification(self.root, self.state['config'], task, prior)
                if repair:
                    planning_recovery = {'status': 'SOURCE_CHANGED', 'changed_sources': repair['changed_sources'],
                        'next': 'The host proved cited plan facts changed after a fresh user continuation. '
                            'Use delegate_plan for one bounded repair with the attached prior plan and complete original goal. '
                            'Earlier calls remain charged and dispatch still checks shared budget; no automatic allowance reset.'}
        return {'task': task, 'continue_arguments': {'intent': task['intent'], 'relation': 'continue'},
                **({'delivery': recovery} if recovery else {}),
                **({'planning_requalification': planning_recovery} if planning_recovery else {}),
                'next': 'Ask the pending question.' if question else
                (planning_recovery or recovery or {}).get('next',
                    'Reuse the plan if still valid; otherwise ask the separate planner. Read or explore only missing facts.')}

    def active(self, *, delivery=False):
        value = load(self.root); task = current(value)
        if task is None or value.get('turn', {}).get('invocation') != self.invocation_id:
            raise ValueError('First understand this message with demand; reuse the current demand on continuation.')
        if value['turn']['intent'] == 'answer' or task.get('question'):
            raise ValueError('Answer the question first; no implementation is authorized by this turn.')
        if delivery and (task['intent'] != 'deliver' or value['turn']['intent'] != 'deliver'):
            raise ValueError('The user asked only for investigation or planning. Implementation is not admitted.')
        return value, task

    def argument_feedback(self, name, result):
        """Append repair facts to a refusal; keep the frozen tool/cache prefix."""
        if name not in {'read', 'list', 'search'} or result.status != 'refused':
            return result
        try:
            body = object_json(result.output_json)
            reason = body.get('reason')
            if not isinstance(reason, str) or not reason.startswith('capability_arguments_mismatch:'):
                return result
            parameters = object_json(self.tool_specs_json)[name]['parameters']
            if not isinstance(parameters, dict) or len(encode(parameters).encode()) > 4096:
                return result
            body.update(correction={'tool': name, 'parameters': parameters},
                next='Do not repeat this call unchanged or issue placeholder calls. Supply actual values '
                     'for every required field using this saved schema. Missing arguments are not a '
                     'permission problem. If the user requested a delivery, record demand and delegate '
                     'the plan and missing project exploration instead of reconstructing the patch yourself.')
            return replace(result, output_json=encode(body))
        except (ValueError, KeyError, TypeError):
            return result  # Optional guidance cannot replace the original refusal.

    def routing_boundary(self, request):
        """Keep initial routing from turning into an unbounded inline exploration.

        Count already requested lookups in the durable journal, not a fresh
        in-memory allowance on resume. Intent remains the coordinator's decision;
        no text classifier or permission is inferred from the user's wording.
        """
        if request.tool_call.name not in PROJECT_LOOKUPS:
            return None
        from compute_plane.native_journal import NativeJournal
        path = self.root/'journal.jsonl'
        if not path.exists():
            return None
        reads = set()
        input_id = 'initial-'+self.session_id
        for row in NativeJournal(path).snapshot().records:
            event = row.event()
            if event['kind'] == 'user_message':
                reads.clear(); input_id = event['message_id']
            elif event['kind'] == 'effect_requested':
                if event['effect_id'] == request.effect_id:
                    break  # Later calls in the same batch cannot consume this one's allowance.
                if event['tool_call']['name'] in PROJECT_LOOKUPS:
                    reads.add(event['effect_id'])
        if len(reads) < ROUTING_LOOKUPS:
            return None
        value = load(self.root); task = current(value)
        scope = value.get('input_scope') or {}
        intent = scope.get('intent') if scope.get('input') == input_id else None
        if intent in {'answer', 'investigate'} or intent in {'plan', 'deliver'} and task and task.get('plan'):
            return None
        if intent in {'plan', 'deliver'}:
            return {'required_tool': 'delegate_plan', 'next':
                'The initial routing lookups are complete. Ask the separate planner to inspect '
                'the necessary source and produce the plan for the recorded goal. Use explore '
                'only for distinct missing facts; do not reconstruct the implementation inline.'}
        if len(reads) < QUESTION_LOOKUPS:
            return None
        return {'required_tool': 'demand', 'next':
            'Record the user goal and intended outcome with demand before further project discovery. '
            'Use answer/investigate for read-only questions, plan for plan-only requests, and deliver '
            'only when the user requested a change. Then delegate planning or exploration as needed. '
            'This routing boundary does not require more user permissions.'}

    def execute(self, request, control):
        from tare_tools_agent_runtime import EffectResult
        def refused(reason, *, context=False, **details):
            result = {'reason': str(reason)[:700], **details}
            if str(reason) in {'Use the declared demand fields.', 'Use bounded demand and context fields, within 8 KiB.',
                    'Use an implementation note within 2000 characters.',
                    'Implement the saved plan; only note, route, effort and boolean recheck_candidate may be supplied.'}:
                try:
                    name=request.tool_call.name
                    parameters=object_json(self.tool_specs_json)[name]['parameters']
                    arguments=object_json(request.tool_call.arguments_json)
                    if len(encode(parameters).encode())<=4096:
                        result.update(correction={'tool':name,'parameters':parameters,
                            'unknown_fields':sorted(set(arguments)-set(parameters.get('properties',{}))),
                            'arguments_bytes':len(encode(arguments).encode()),'max_arguments_bytes':8192},
                            next='Use only the named fields in the saved schema; correct the arguments without '
                                'repeating this call unchanged. Keep instruction/context concise: the planner '
                                'already receives the recorded objective and criteria. Do not duplicate the '
                                'entire user request in every field. No operation was dispatched.')
                        if name=='implement':
                            note=arguments.get('note','')
                            result['correction'].update(note_characters=len(note) if isinstance(note,str) else None,
                                max_note_characters=2000)
                            result['next']=('No worker, check or auditor started. Correct the named arguments and '
                                'keep note within 2000 characters; the worker already receives the saved plan. '
                                'Do not repeat the same refused call or report a background job.')
                except (ValueError,KeyError,TypeError):pass
            if context:
                try:
                    value = load(self.root)
                    task = current(value)
                    if task and value.get('turn', {}).get('invocation') != self.invocation_id:
                        result.update(required_action={'tool': 'demand', 'arguments': {
                            'intent': task['intent'], 'relation': 'continue'}},
                            next='Record this follow-up with demand first, including permission replies. '
                                 'Then retry the requested stage with the saved goal and criteria. '
                                 'This is an intake gate, not an unavailable planner/provider.')
                    view = recovery_view(self.root, task)
                    if view:
                        result.update(current_demand=task['id'], saved_plan=bool(task.get('plan')), delivery=view)
                except (ValueError, OSError, KeyError, TypeError):
                    pass  # Diagnostic failure cannot relax the original refusal.
            return EffectResult(request.effect_id, 'refused', encode(result))
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return refused('CONTROL_OR_SESSION_MISMATCH')
        name = request.tool_call.name
        if name not in self.names:
            return refused('The coordinator cannot execute this effect. Use the separate planner and implementer.')
        try:
            args = object_json(request.tool_call.arguments_json)
            if len(encode(args).encode()) > 8192:
                raise ValueError('Use bounded demand and context fields, within 8 KiB.')
            from . import consent_decisions
            receipts = ([] if getattr(self, '_host_retry_delivery', False)
                        else consent_decisions.process(self.root, self.session_id))
            if name == 'implement' and any(r['decision'] == 'pending' for r in receipts):
                return refused(consent_decisions.REASK)
            routing = self.routing_boundary(request)
            if routing:
                return refused('COORDINATOR_ROUTING_REQUIRED', **routing)
            if name == 'product_help':
                if set(args) != {'topic'}:
                    raise ValueError('Give only the product-help topic.')
                output = product_help(self.state, args['topic'])
            elif name == 'request_folder_access' and not FOLDER_READS <= set(self.names):
                return refused('This saved coordinator does not expose folder readers; no permission request was created. '
                    'Use product_help for the Tare schema. A new conversation can use approved-folder readers; '
                    'project read/list/search never accept absolute external paths.')
            elif name == 'demand':
                output = self.understand(args, request.effect_id)
            elif name == 'check':
                # A closed verification request needs no planning ceremony. It
                # never authorizes implementation or changes the saved goal.
                value = load(self.root); task = current(value)
                from .work_units import CHECK_PORT, run_checks, checks as planned_checks, test_file
                from .conversation_workspace import snapshot
                from .challenger import git
                from .native_installation import storage_root
                import shutil
                config = deepcopy(self.state['config'])
                config['verifiers'].update(detected_checks(config))
                unit = (task or {}).get('plan', {}).get('unit')
                unit_checks = {}
                if unit:
                    config['verifiers'].update(host_checks(config, unit))
                    owner=unit['repository']
                    unit_checks = planned_checks(config,unit,owner,Path(config['repositories'][owner]))
                    config['verifiers'].update({n:{'repository':owner,'argv':a} for n,a in unit_checks.items()})
                names = args.get('names')
                if (set(args) != {'names'} or not isinstance(names, list) or not 1 <= len(names) <= 16
                        or any(not isinstance(n,str) or n not in config['verifiers'] for n in names)):
                    catalog={n:{'repository':v['repository'],'argv':v['argv']}
                             for n,v in config['verifiers'].items()}
                    result={'reason':'Choose a registered check name, not test paths. No arbitrary commands.',
                            'available_checks':catalog}
                    # Only suggest an exact equivalent, never broaden a subset to
                    # the whole plan or silently execute it on a refused request.
                    tests=(unit or {}).get('test_paths') or []
                    python_tests=[t for t in tests if test_file(t).endswith('.py')]
                    if (set(args)=={'names'} and isinstance(names,list) and 1 <= len(names) <= 16
                            and all(isinstance(n,str) for n in names)
                            and set(names)==set(python_tests)):
                        # Use generated names, including collision suffixes, but
                        # never a custom verifier that occupied the original name.
                        retry_names = []
                        custom = set(unit.get('verifiers') or [])
                        for path in sorted({test_file(t) for t in python_tests}):
                            base = 'pytest-tests:' + path
                            matches = []
                            for candidate in unit_checks:
                                suffix = candidate.removeprefix(base + '#')
                                if (candidate in catalog and candidate not in custom
                                        and (candidate == base or (candidate.startswith(base + '#')
                                             and suffix.isdecimal() and int(suffix) >= 2))):
                                    matches.append(candidate)
                            if len(matches) != 1:
                                break  # Missing or ambiguous coverage: no partial advice.
                            retry_names.append(matches[0])
                        else:
                            if 1 <= len(retry_names) <= 16:
                                result.update(required_action={'tool':'check','arguments':{'names':retry_names}},
                                    next=('This registered check runs exactly the requested test targets, not a broader suite. Retry it.'
                                          if len(retry_names) == 1 else
                                          'These registered checks run exactly the requested test targets, not a broader suite. Retry them.'))
                    return refused(result.pop('reason'),**result)
                if 'tests' in names and 'godot-import' in config['verifiers']:
                    names = ['godot-import', *[n for n in names if n != 'godot-import']]
                if value.get('turn', {}).get('invocation') != self.invocation_id:
                    value['turn'] = {'invocation': self.invocation_id, 'intent': 'investigate'}
                    value['input_scope'] = {'input': self.input_id(), 'intent': 'investigate'}
                    save(self.root, value)
                repositories = {config['verifiers'][n]['repository'] for n in names}
                if len(repositories) != 1:
                    raise ValueError('Verify one project at a time.')
                repository = repositories.pop()
                from .conversation_permissions import with_review_policy
                from . import relay_mesh
                config = with_review_policy(config, self.permission_directory,
                    config['repositories'][repository], relay_mesh.RELAY_ROOT)
                commands = {n: config['verifiers'][n]['argv'] for n in names}
                tree = storage_root()/'wt'/uuid4().hex[:8]
                token = None
                try:
                    snapshot(config['repositories'][repository], tree)
                    if unit and unit['repository']==repository:
                        scoped=planned_checks({**config,'conversation_flow':True},unit,repository,tree)
                        commands.update({n:a for n,a in scoped.items() if n in names})
                    # Protected: the project's command gate. Otherwise the sandbox, no model review; a check it blocks
                    # (and, with no sandbox here, any check of a read-only project) runs outside only if the user says.
                    from .command_protection import conversation_verifier
                    from .conversation_permissions import outside_sandbox
                    from .turn_engine import project_permission_mode
                    check_unit = {'repository': repository, 'target_paths': [],
                        'title': task['objective'] if task else 'Verify configured project checks',
                        'criteria': task['criteria'] if task else []}
                    project = config['repositories'][repository]
                    token = CHECK_PORT.set(conversation_verifier(control, self.root/'checks', config,
                        self.state['deployment'], tree, check_unit, commands, **({} if config.get('command_protection')
                        else {'outside': outside_sandbox(self.root, self.state, task, request.effect_id, project,
                                                         control, self.permission_directory, self.invocation_id),
                              'read_only': (project_permission_mode(project) or 'read') == 'read'})))
                    status, details = run_checks(tree, commands)
                    output = {'status': status, 'checks': details, 'applied': False}
                    if BACKLOG_CHECK in names:
                        output['project_graph_binding'] = graph_binding(config)
                    (task if task else value)['verification'] = output
                    if task and status != 'green':
                        task['stage'] = 'PENDING'
                    save(self.root, value)
                finally:
                    if token is not None:
                        check_runner=CHECK_PORT.get()
                        CHECK_PORT.reset(token)
                        if callable(getattr(check_runner,'close',None)):check_runner.close()
                    try: git(config['repositories'][repository], 'worktree', 'remove', '--force', str(tree), timeout=120)
                    except (ValueError, OSError): pass
                    shutil.rmtree(tree, ignore_errors=True)
            elif name == 'delegate_plan':
                value, task = self.active()
                if task['intent'] not in {'plan', 'deliver'}:
                    raise ValueError('This demand is still an investigation, not a request to plan or implement.')
                if task.get('plan') and (task.get('verification') or {}).get('status') in {'red', 'inconclusive'}:
                    raise ValueError('The current goal already has a saved plan. Failed checks are correction '
                        'feedback, not permission to spend on planning again. Use demand relation=correct and '
                        'implement with the saved plan. A genuinely revised demand must be recorded first.')
                # Said before planning, so nobody approves a plan that cannot be implemented; a plan-only demand plans.
                from .execution_scope import local_only_unsandboxed
                if task['intent'] == 'deliver' and (refusal := local_only_unsandboxed()):
                    raise ValueError(refusal)
                result = self.tools.execute(request, control)
                if result.status != 'succeeded': return result
                output = object_json(result.output_json)
                if output.get('status') == 'PLANNED':
                    task_plan(self.root, self.state['config'], task, output)
                    # Planner prose can name a validation step without registering
                    # a tool. Return the compiled host identities in the result,
                    # leaving the cached tool schema and planner artifact intact.
                    names = list(task['plan']['unit']['verifiers'])
                    output['execution_checks'] = {'names': names,
                        'source': 'Host-compiled saved plan; descriptive planner labels are not check names.'}
                    if names:
                        output['execution_checks']['action'] = {'tool': 'check', 'arguments': {'names': names}}
                    preserved = retained_candidate({key: task.get(key)
                        for key in ('replan_candidate', 'delivery')})
                    task.pop('replan_candidate', None)
                    # Keep an unapplied attempt's provenance for the same-turn plan comparison;
                    # an applied record never stands in for the new plan's delivery.
                    kept = task.get('delivery') if not (task.get('delivery') or {}).get('applied') else None
                    task['delivery'] = deepcopy(preserved or kept)
                    task.pop('replan_from',None)
                    save(self.root, value)
                    output['next'] = 'Return this plan to the user.' if task['intent'] == 'plan' else 'Use implement to execute this saved plan.'
            elif name == 'implement':
                value, task = self.active(delivery=True)
                plan = task.get('plan') or {}
                if not plan.get('unit'):
                    raise ValueError('A separate planner must produce an executable plan before implementation.')
                if task.get('permission_denied_input') == self.input_id():
                    raise ValueError('The user did not authorize this execution. Do not repeat the request in this turn.')
                if task.get('plan_change') == {'input': self.input_id(), 'plan': plan['sha256']}:
                    raise ValueError('The user asked to change this plan. Ask the planner for a revised plan; do not implement this one.')
                if (set(args)-{'note', 'route', 'effort', 'recheck_candidate'}
                        or type(args.get('recheck_candidate',False)) is not bool):
                    raise ValueError('Implement the saved plan; only note, route, effort and boolean recheck_candidate may be supplied.')
                if not isinstance(args.get('note',''),str) or len(args.get('note',''))>2000:
                    raise ValueError('Use an implementation note within 2000 characters.')
                if args.get('recheck_candidate') and (args.get('route') or args.get('effort')):
                    raise ValueError('Rechecking preserves the candidate producer; omit route and effort.')
                artifact = regular(Path(plan['artifact']))
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != plan['sha256']:
                    raise ValueError('The saved planner artifact changed. Replan before execution.')
                for source in plan.get('sources', []):
                    repo, name = source['repository'], source['path']
                    if repo in self.state['config']['repositories'] and not (
                            repo == plan['unit']['repository'] and name in plan['unit']['target_paths']):
                        if source_state(self.state['config'], {repo: [name]})[repo+':'+name] != source['sha256']:
                            raise ValueError('A cited plan dependency changed. Inspect the changed facts and revise the affected plan before implementation.')
                if (task.get('delivery') or {}).get('applied'):
                    if (task.get('verification') or {}).get('status') in {'red', 'inconclusive'}:
                        raise ValueError('Later project checks did not pass. The earlier patch remains applied, '
                            'but its old green checks are not current verification. Use demand relation=correct '
                            'to repair the same plan after a delivery request; do not apply the old patch again.')
                    if source_state(self.state['config'], task['delivery']['targets']) != task['delivery']['source_state']:
                        task['stage'] = 'PENDING'; save(self.root, value)
                        raise ValueError('Files changed since review. Inspect the change and continue with relation=correct or revise.')
                    return EffectResult(request.effect_id, 'succeeded', encode(delivery_view(
                        task['delivery'], config=self.state['config'], unit=plan['unit'])), True)
                # Every implementer start below: a new attempt, recheck_candidate, /retry_delivery, a waiting worker.
                from .execution_scope import local_only_unsandboxed
                if refusal := local_only_unsandboxed():
                    raise ValueError(refusal)
                previous = task.get('delivery') or {}
                preserved = retained_candidate(task)
                if preserved:
                    require_retained_scope(plan, preserved)
                prior_audit=previous.get('audit') or {}
                if (preserved is not None and preserved is not previous
                        and (preserved is task.get('replan_candidate')
                             or not previous.get('candidate') and not previous.get('worker'))):
                    preserved = replanned_delivery(self.state['config'], plan, preserved)
                    if previous:
                        preserved['last_attempt'] = previous
                    task['delivery'] = previous = preserved
                    prior_audit = previous.get('audit') or {}
                    save(self.root, value)
                from .conversation_review import recorded_failure
                if record:=recorded_failure(self.root,prior_audit):prior_audit=record[2]
                packet_revision=(prior_audit.get('failure_code')=='REVIEW_PACKET_OVERSIZED'
                    and prior_audit.get('requires_candidate_revision') is True
                    and not prior_audit.get('session') and not prior_audit.get('actor'))
                # A fresh authorized revision executes its separate saved plan
                # on the retained patch; an unchanged continuation only rechecks.
                revised_plan=(value['turn'].get('relation')=='revise'
                    and task.get('attempt_input')!=self.input_id())
                candidate_revision=revised_plan or packet_revision or bool(prior_audit.get('requires_candidate_revision')
                    and not (prior_audit.get('failure_code') or prior_audit.get('reason'))
                    and prior_audit.get('summary') and prior_audit.get('findings'))
                candidate_revision=candidate_revision or (
                    value['turn'].get('relation')=='correct' and task.get('attempt_input')!=self.input_id()
                    and prior_audit.get('failure_code') in {'PREPARATION_FAILED','INVALID_DELIVERY'})
                candidate_revision=candidate_revision or bool(record and record[1].get('status')=='FINISHED'
                    and value['turn'].get('relation')=='correct' and task.get('attempt_input')!=self.input_id()
                    and prior_audit.get('verdict')=='INCONCLUSIVE' and prior_audit.get('summary')
                    and prior_audit.get('remaining') and not prior_audit.get('failure_code')
                    and not prior_audit.get('reason'))
                last_attempt = previous.get('last_attempt') or previous
                inconclusive = (last_attempt.get('verdict') == 'INCONCLUSIVE'
                    or (last_attempt.get('audit') or {}).get('verdict') == 'INCONCLUSIVE'
                    or prior_audit.get('verdict') == 'INCONCLUSIVE')
                # Compare attempt provenance, never the artifact path or a newly loaded plan.
                plan_revision = bool(inconclusive and task.get('delivery_plan_sha256')
                    and plan.get('sha256') and plan['sha256'] != task['delivery_plan_sha256'])
                candidate_revision=candidate_revision or plan_revision
                if task.get('delivery_invocation') == self.invocation_id and inconclusive and (
                        not candidate_revision or task.get('candidate_correction_invocation')==self.invocation_id):
                    return EffectResult(request.effect_id, 'succeeded', encode({**delivery_view(
                        previous, config=self.state['config'], unit=plan['unit']),
                        'dispatch': {'implementer':'NOT_STARTED','auditor':'NOT_STARTED'},
                        'next': 'Stop and explain the concrete failure. The saved candidate and inconclusive audit were returned unchanged; '
                            'no implementer or auditor was rerun. An unchanged inconclusive implementation is not repeated in the same user turn.'}), True)
                # Rehydrate the executable projection from the same immutable
                # artifact. Earlier plan-only summary/phase limits are history,
                # never implementation criteria, and require no paid replanning.
                task_plan(self.root, self.state['config'], task, {'artifact': str(artifact)})
                plan = task['plan']
                from .work_units import run_unit, check_failures, child_limits, GuardRefusal
                from .conversation_recheck import retry_decision
                from .conversation_review import review
                from .conversation_permissions import authorize, PlanChangeRequested
                execution_unit = deepcopy(plan['unit'])
                discovered = detected_checks(self.state['config'])
                execution_unit['verifiers'] = list(dict.fromkeys([*discovered, *execution_unit['verifiers']]))
                if (task.get('verification') or {}).get('status') in {'red', 'inconclusive'}:
                    execution_unit['verification_feedback'] = task['verification']['checks'][:12000]
                if BACKLOG_CHECK in execution_unit['verifiers']:
                    contract = backlog_authoring_contract()
                    execution_unit['verification_contract'] = contract
                    execution_unit['verification_contract_digest'] = hashlib.sha256(encode(contract).encode()).hexdigest()
                candidate = previous.get('candidate')
                if args.get('recheck_candidate') and (not candidate or candidate_revision or previous.get('applied')):
                    raise ValueError('Rechecking requires an unapplied retained candidate with no required candidate revision.')
                if candidate:
                    candidate = {**candidate, 'review_only': bool(args.get('recheck_candidate')) or (
                        prior_audit.get('verdict') == 'INCONCLUSIVE' and not candidate_revision)}
                # Diagnose a stopped/exhausted unit before asking for consent.
                # Dispatch still rechecks the shared budget after human waiting.
                operator_retry = False
                try:
                    if not candidate or not candidate['review_only']:
                        explicit_retry = bool(getattr(self, '_host_retry_delivery', False))
                        operator_retry = retry_decision(self.root, task, execution_unit, explicit=explicit_retry) or explicit_retry
                        check_failures(self.root, execution_unit, conversation=True,
                            **({'operator_retry': True} if operator_retry else {}))
                except GuardRefusal:
                    retry_decision(self.root, task, execution_unit, register=True)
                    task['stage'] = 'PENDING'; save(self.root, value)
                    raise
                try:
                    if not candidate or not candidate['review_only']:
                        child_limits(self.root, self.state)
                except ValueError:
                    task['stage'] = 'PENDING'; save(self.root, value)
                    raise
                authorization_state = {**self.state, 'config': {**self.state['config'],
                    'verifiers': {**self.state['config']['verifiers'], **discovered, **host_checks(self.state['config'], plan['unit'])}}}
                try:
                    config = authorize(self.root, authorization_state, task, request.effect_id, control, self.permission_directory)
                except PlanChangeRequested:
                    # The planner gets the shown plan as advice, in a planning round of its own (a user decision).
                    task['plan_change'] = {'input': self.input_id(), 'plan': plan['sha256']}
                    task['replan_from'] = {k: plan[k] for k in ('artifact', 'sha256')}
                    task['planning_revision'] = task.get('planning_revision', 0)+1
                    save(self.root, value)
                    raise
                except PermissionError:
                    task['permission_denied_input'] = self.input_id()
                    save(self.root, value)
                    raise
                # Host-defined baseline check is available even for documentation.
                state = deepcopy(self.state)
                state.pop('operator_delivery_retry', None)
                if operator_retry: state['operator_delivery_retry'] = True
                state['config'] = {**config, 'conversation_flow': True}  # worker grant, never coordinator capabilities
                if config.get('command_protection'):
                    from .command_protection import encode_context
                    # Command triage needs the complete operator authorization,
                    # not every planner-derived acceptance/test obligation. The
                    # implementation and independent audit still receive them all.
                    state['config']['command_context']=encode_context({'title':task['objective'],
                        'criteria':task['criteria'],'target_paths':plan['unit']['target_paths']})
                repository = plan['unit']['repository']
                state['config']['verifiers']['conversation-diff'] = {'repository': repository,
                    'argv': ['git', '-c', 'core.fsmonitor=false', '-c',
                             'core.whitespace=trailing-space,space-before-tab,cr-at-eol',
                             'diff', '--check', 'HEAD', '--', *plan['unit']['target_paths']]}
                execution_unit['proof_paths'] = proof_paths(config, plan['unit'], plan.get('sources', []))
                execution_unit['verifiers'] = list(dict.fromkeys([*execution_unit['verifiers'], 'conversation-diff']))
                from .native_agent import read_json
                payload = read_json(artifact, 65536)['payload']
                advice=deepcopy({k: payload[k] for k in ('steps', 'tasks', 'sources')})
                for planned_task in advice['tasks']:
                    if all(c in execution_unit['criteria'] for c in planned_task.get('criteria',[])):
                        planned_task.pop('criteria',None)  # already present in the complete Done-when list
                execution_unit['planner_advice'] = encode(advice)
                execution_unit['planner_input'] = {'path': str(artifact), 'sha256': plan['sha256']}
                previous = task.get('delivery') or {}
                if previous.get('receipt'):
                    receipt = regular(Path(previous['receipt']))
                    if not receipt.resolve().is_relative_to(self.root.resolve()/'units'):
                        raise ValueError('Previous implementation receipt is outside this conversation.')
                    previous_receipt=read_json(receipt,1024*1024)
                    previous_result=previous_receipt.get('result',{})
                    rungs=previous_receipt.get('rungs') or []
                    if rungs and not previous_result.get('applied'):
                        from .work_units import interruption_feedback
                        execution_unit['implementation_feedback']=interruption_feedback(
                            receipt.parent/f'session-{len(rungs)}',rungs[-1])
                    if previous_result.get('verdict') == 'NOT_VERIFIED':
                        execution_unit['verification_feedback'] = previous_result.get('checks_after', '')[:4096]
                        # Old receipts have a patch but no captured application basis. Reuse its
                        # contents as advice, never silently assert a base and apply it.
                        if not previous.get('candidate') and previous_result.get('patch'):
                            patch = regular(Path(previous_result['patch']))
                            if patch.resolve().is_relative_to(self.root.resolve()/'units') and patch.stat().st_size <= 8192:
                                execution_unit['previous_candidate_changes'] = patch.read_text(encoding='utf-8', errors='replace')
                    # Saved delivery projections may omit the actionable summary/remaining fields.
                    # The bound review receipt is the source for correction feedback too.
                    feedback = prior_audit if record else previous_result.get('audit') or {}
                    if feedback.get('verdict') in {'REVISE','INCONCLUSIVE'}:
                        execution_unit['review_feedback'] = encode({k: feedback[k] for k in (
                            'verdict','reason','failure_code','requires_candidate_revision','diff_projection',
                            'summary', 'findings', 'remaining') if k in feedback})
                from .work_forms import record_plan
                record_plan(self.root, state['config'], {'units': [execution_unit], 'criteria': execution_unit['criteria']})
                task.update(stage='IMPLEMENTING', attempt_invocation=self.invocation_id, attempt_effect=request.effect_id,
                            attempt_plan_sha256=execution_unit['planner_input']['sha256'])
                # A cached-review lookup did not attempt an implementation. It must not consume
                # the new human input before the coordinator corrects its relation in this turn.
                if not candidate or not candidate['review_only']:task['attempt_input']=self.input_id()
                if candidate_revision:task['candidate_correction_invocation']=self.invocation_id
                save(self.root, value)
                def audit(tree, producer, unit, changed, checks):
                    task['stage'] = 'AUDITING'; save(self.root, value)
                    result = review(self, task, tree, producer, unit, changed, checks, control)
                    # The checks passed (run_unit reviews only then). A same-family ACCEPT, or a completed one that
                    # left only open items, is the user's decision; findings take the ordinary correction path.
                    if result.get('audit_label') and (result.get('verdict') == 'ACCEPT' or uncovered_review(result)):
                        from .conversation_permissions import confirm_apply
                        result = confirm_apply(self.root, self.state, task, changed, result, request.effect_id,
                                               control, self.permission_directory)
                    return result
                from .conversation_review import available, packet_preflight, resume_tree, uncovered_review
                audit.preflight = lambda producer: available(producer, self)
                audit.packet_preflight = packet_preflight
                audit.resume_tree = lambda: resume_tree(self, task)
                try:
                    from .conversation_permissions import outside_sandbox
                    output = run_unit(self.root, state, {'unit': task['id'], **args}, control.should_stop, review=audit,
                                      candidate=candidate, worker=(task.get('delivery') or {}).get('worker'),
                                      control=control, spend_model=self.parent_model, review_effect=request.effect_id,
                                      outside=outside_sandbox(self.root, self.state, task, request.effect_id,
                                          config['repositories'][plan['unit']['repository']], control,
                                          self.permission_directory, self.invocation_id))
                except (ValueError, OSError, KeyError, TypeError):
                    task['stage'] = 'PENDING'; save(self.root, value)
                    raise
                if output.get('applied'):
                    task['verification'] = {'status': 'green', 'checks': output.get('checks_after', ''), 'applied': True}
                    output['targets'] = proof_paths(config, plan['unit'], [*plan.get('sources', []),
                        *(output.get('audit') or {}).get('sources', [])])
                    output['source_state'] = source_state(self.state['config'], output['targets'])
                if needed := audit_scope(output, plan['unit']):
                    output['scope_needed'] = needed
                retained = retained_delivery(output)
                if (not output.get('applied') and not output.get('candidate') and not output.get('worker')
                        and previous.get('candidate')):
                    retained={**previous,'last_attempt':retained}
                    output.update(candidate=previous['candidate'],last_attempt={k:output[k] for k in
                        ('verdict','reason','route') if k in output})
                task.update(stage='DELIVERED' if output.get('applied') else 'PENDING', delivery=retained,
                            delivery_invocation=self.invocation_id,
                            delivery_plan_sha256=execution_unit['planner_input']['sha256'])
                save(self.root, value)
                output = delivery_view(output, config=self.state['config'], unit=plan['unit'])
                if not output.get('applied') and (not candidate or not candidate['review_only']):
                    try:
                        check_failures(self.root, execution_unit, conversation=True)
                    except GuardRefusal as stop:
                        # This run reached the failure limit: ask now, so the reply to the coordinator's question
                        # is the consent, instead of a second question when the next correction is refused.
                        retry_decision(self.root, task, execution_unit, register=True)
                        output['next'] = str(stop)
            else:
                return self.argument_feedback(name, self.tools.execute(request, control))
            return EffectResult(request.effect_id, 'succeeded', encode(output), True)
        except ScopeRepair as exc:
            if task.get('scope_repair_input')==self.input_id():
                return refused('The planner still omitted required output scope. '
                    'The candidate and criteria are preserved; do not repeat planning in this user turn.')
            old=task.get('plan') or {}
            task['inherited_criteria']=list(dict.fromkeys([
                *task.get('inherited_criteria',[]),*(old.get('unit') or {}).get('criteria',[])]))
            artifact=old.get('artifact') or (output or {}).get('artifact')
            if artifact:
                task['replan_from']={'artifact':artifact,
                    'sha256':hashlib.sha256(regular(Path(artifact)).read_bytes()).hexdigest()}
            preserved = retained_candidate(task)
            if preserved:
                task['replan_candidate']=deepcopy(preserved)
            task.pop('plan',None)
            task['scope_repair_input']=self.input_id()
            task['planning_revision']=task.get('planning_revision',0)+1
            task['stage']='DISCOVERY'
            save(self.root,value)
            return EffectResult(request.effect_id,'refused',encode({'reason':str(exc),
                'missing_target_paths':getattr(exc,'missing_target_paths',[]),
                'required_action':{'tool':'delegate_plan','arguments':{'instruction':SCOPE_REPAIR+str(exc)}},
                'next':'Use the separate planner to repair scope; no implementation was dispatched.'}),False)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return refused(exc, context=True)

    def execute_many(self, requests, control):
        # Stage mutations are serialized, including a model's parallel tool batch.
        return [self.execute(request, control) for request in requests]

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending
        from tare_tools_agent_runtime import EffectResult
        request = pending(journal, self.session_id)
        if request and request.tool_call.name in SPEC:
            if request.tool_call.name == 'implement':
                from .conversation_permissions import close_interrupted
                close_interrupted(self.root, request.effect_id)
            return request, EffectResult(request.effect_id, 'refused', encode({'reason':
                'This operation was interrupted. Inspect demand and child receipts before continuing; it was not repeated.'}))
        return self.tools.recover_effect(journal)

    def summary(self):
        value = load(self.root); task = current(value)
        if task is None: return None
        delivery = task.get('delivery') or {}
        if task['stage'] == 'DELIVERED' and source_state(self.state['config'], delivery['targets']) != delivery['source_state']:
            task['stage'] = 'PENDING'; save(self.root, value)
        return {k: task[k] for k in ('id', 'objective', 'intent', 'stage', 'question')}

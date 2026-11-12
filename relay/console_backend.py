"""Terminal presentation adapter over public CLI and read-only native services."""
import asyncio
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import signal
import subprocess
from compute_plane.managed_process import host_executable
import sys
from uuid import uuid4


def default_deployment():
    from .execution_scope import current
    if current().changes('coordinator'):return 'tare-auto'
    from .environment import default_route  # tare auto once tare setup has run
    return default_route()


@dataclass
class Selection:
    root: str
    mode: str = 'session'
    deployment: str = field(default_factory=lambda: default_deployment())
    profile: str = ''
    session: str = ''
    work: str = ''
    prompt: str = ''
    verifier: str = ''
    unlimited: bool = True
    max_model_calls: str = ''
    max_seconds: str = ''
    execution_policy: dict = field(default_factory=dict)
    resume_coordinator_seconds: int = 0
    resume_coordinator_calls: int = 0
    refresh: bool = False
    retry_plan: bool = False
    plan: str = ''
    source_ref: str = ''
    criterion_ids: str = ''
    work_verifier: str = ''
    revision_reason: str = ''
    revision_paths: str = ''
    wait_before_implement: str = ''
    sequence: str = ''
    sequence_works: str = ''
    sequence_parallel: bool = False
    sequence_serial: bool = False
    sequence_revision_reason: str = ''
    sequence_revision_works: str = ''
    batch: str = ''
    workers: str = '1'
    synthesis: str = ''
    synthesis_inputs: str = '[]'
    synthesis_sha256: str = ''
    research: str = ''
    urls: str = ''
    auditor: str = ''
    retry_audit: bool = False
    recheck_candidate: bool = False
    retry_delivery: bool = False
    repair_plan: bool = False
    auto_planner: bool = False
    auto_execution: bool = False
    findings: str = ''
    findings_sha256: str = ''
    finding_items: str = ''
    finding_note: str = ''
    plan_reviewer: str = ''
    plan_reviewer_profile: str = ''
    role: str = 'explorer'
    intake_criteria: str = ''
    intake_argv: str = ''
    outcome_summary: str = ''
    outcome_remaining: str = ''
    outcome_disposition: str = 'FULFILLED'
    outcome_resolutions: str = ''
    guide_next: bool = False
    streaming: bool = True
    local_models: bool = True  # False: this conversation's sessions run on cloud or CLI models only
    specgraph_context: bool = False  # advisory lookup only; never enables the edit gate
    terminal: bool = False
    command_protection: dict = field(default_factory=dict)
    project_admission: dict = field(default_factory=lambda: {'mode': 'quarantine', 'root_sha256': None, 'nested': False})
    write_paths: list[str] = field(default_factory=list)
    graph_path: str = ''
    folder_grants: str = ''  # the project's grants file: new sessions may ask to read outside folders
    folder_note: str = ''  # what the user's reply did to the agent's pending folder request
    context_root: str = ''
    context_paths: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    attachment_root: str = ''
    attachment_project: str = ''
    attachments_enabled: bool = False


def saved_selection(payload):
    """A recorded selection; demands saved before the Library manifest input was removed still reopen."""
    return Selection(**{key: value for key, value in payload.items() if key != 'manifest'})


def require_control_graph(selection):
    """Sequences retain their control-graph contract; individual Work is graph-scoped."""
    from . import relay_mesh
    if selection.graph_path and Path(selection.graph_path).resolve() != Path(relay_mesh.WORK_GRAPH_FILE).resolve():
        raise ValueError('Sequence execution for this project graph is not connected yet. Select an individual Work.')


def command(selection, resume=False, *, replan=False, assistant=False):
    """Saved session/Work options win on resume; argv never goes through a shell."""
    from .native_agent import session_root
    root = Path(selection.root).resolve()
    if not root.is_dir():
        raise ValueError('Project directory is unavailable.')
    budget_args = []
    from .execution_scope import current
    unlimited = current().unlimited
    if not resume and selection.mode in {'session', 'work', 'sequence', 'parallel'} and (unlimited or not selection.unlimited):
        for name, label, parse in (('max_model_calls', 'Model call limit', int),
                                   ('max_seconds', 'Time limit', float)):
            if ('calls' if name == 'max_model_calls' else 'time') in unlimited:
                continue
            text = getattr(selection, name).strip()
            if not text:
                continue
            try:
                value = parse(text)
                if value <= 0 or not math.isfinite(value):
                    raise ValueError()
            except (ValueError, OverflowError):
                raise ValueError(label+' must be a positive '+('integer.' if parse is int else 'number of seconds.')) from None
            budget_args += ['--'+name.replace('_', '-'), str(value)]
    if selection.mode == 'sequence':
        require_control_graph(selection)
    if (selection.context_paths or selection.attachments) and selection.mode != 'session':
        raise ValueError('Selected file context is for direct conversations. Remove it before starting a flow.')
    if replan and (resume or selection.mode != 'plan'):
        raise ValueError('Replan requires PLAN mode and a new execution.')
    if selection.mode == 'synthesis':
        from . import research_synthesis as synthesis
        if resume:
            Backend().synthesis_view(selection)
            return ['research-synthesis', '--synthesis', selection.synthesis, '--resume', '--format', 'json']
        view = Backend().synthesis_preview(selection)
        if view['sha256'] != selection.synthesis_sha256:
            raise ValueError('Selections changed since preview; review again.')
        selection.synthesis = str(synthesis.flow_root()/uuid4().hex)
        args = ['research-synthesis', '--synthesis', selection.synthesis, '--root', str(root),
                '--question', selection.prompt, '--inputs', selection.synthesis_inputs,
                '--reviewed-sha256', selection.synthesis_sha256, '--format', 'json']
        return args + (['--native-deployment', selection.profile] if selection.profile else ['--deployment', selection.deployment])
    if selection.mode == 'research':
        if resume:
            Backend().research_view(selection)
            args = ['research-flow', '--research', selection.research, '--resume', '--format', 'json']
        else:
            from .research_flow import flow_root
            from .research import normalize
            urls = [u.strip() for u in selection.urls.splitlines() if u.strip()]
            if not selection.prompt.strip() or not urls or any(not normalize(u) for u in urls):
                raise ValueError('Enter a research question and HTTP(S) source URLs, one per line.')
            selection.research = str(flow_root()/uuid4().hex)
            args = ['research-flow', '--research', selection.research, '--root', str(root),
                    '--question', selection.prompt, '--format', 'json']
            for url in urls:
                args += ['--url', url]
    elif selection.mode == 'parallel':
        if resume:
            Backend().batch_view(selection)
            args = ['parallel-explore', '--batch', selection.batch, '--resume', '--format', 'json']
        else:
            questions = [q.strip() for q in selection.prompt.splitlines() if q.strip()]
            if not questions or not selection.workers.isdecimal() or int(selection.workers) < 1:
                raise ValueError('Enter independent questions, one per line, and a positive worker count.')
            from .parallel_explore import batch_root
            selection.batch = str(batch_root()/uuid4().hex)
            args = ['parallel-explore', '--batch', selection.batch, '--root', str(root),
                    '--workers', selection.workers, '--format', 'json']
            for question in questions:
                args += ['--question', question]
    elif selection.mode == 'sequence':
        if resume:
            if not selection.sequence:
                raise ValueError('Select a saved sequence first.')
            Backend().sequence_view(selection)
            args = ['sequence', '--sequence', selection.sequence, '--resume', '--format', 'json']
            if selection.sequence_serial:
                args += ['--workers', '1']
        else:
            ids = [s.strip() for s in selection.sequence_works.split(',') if s.strip()]
            if not ids or len(ids) != len(set(ids)):
                raise ValueError('Select distinct Work IDs separated by commas.')
            from .work_sequence import sequence_root
            selection.sequence = str(sequence_root()/uuid4().hex)
            args = ['sequence', '--sequence', selection.sequence, '--works', *ids, '--root', str(root), '--format', 'json']
            if selection.sequence_parallel:
                args += ['--workers', '2', '--isolated-effects']
            if selection.wait_before_implement:
                args += ['--wait-before-implement', selection.wait_before_implement]
    elif selection.mode == 'plan':
        if resume:
            if not selection.plan:
                raise ValueError('Select a saved plan first.')
            saved = Backend().plan_preview(selection)  # Saved graph wins over current defaults.
            selection.graph_path = saved['proposal']['graph_path']
            args = ['plan', '--plan', selection.plan, '--retry-review', '--format', 'json']
        else:
            if not selection.prompt.strip():
                raise ValueError('Describe the planning demand first.')
            from .native_plan import plan_root
            parent = selection.plan
            if replan:
                if not parent:
                    raise ValueError('Select a saved plan before replanning.')
                saved = Backend().plan_preview(selection)
                selection.graph_path = saved['proposal']['graph_path']
            selection.plan = str(plan_root()/uuid4().hex)
            args = ['plan', '--root', str(root), '--plan', selection.plan,
                    '--instruction', selection.prompt, '--format', 'json']
            if not replan:
                from . import relay_mesh
                from .project_graph import graph_path
                selection.graph_path = graph_path(selection.graph_path or str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()))
                args += ['--graph', selection.graph_path]
            if not replan and (selection.findings or selection.findings_sha256):
                if not selection.findings or not selection.findings_sha256:
                    raise ValueError('Select reviewed findings with their identity.')
                args += ['--findings', selection.findings, '--findings-sha256', selection.findings_sha256]
            if replan:
                args += ['--replan', parent]
            if selection.plan_reviewer_profile:
                if selection.plan_reviewer:
                    raise ValueError('Choose a plan reviewer name or a custom profile, not both.')
                args += ['--reviewer-profile', str(Path(selection.plan_reviewer_profile).resolve())]
            elif selection.plan_reviewer:
                args += ['--reviewer-deployment', selection.plan_reviewer]
    elif selection.mode == 'work':
        if not selection.work.strip():
            raise ValueError('Select a Work first.')
        args = ['run', selection.work, '--format', 'json', '--max-bytes', '65536']
        if selection.graph_path:
            args += ['--project-graph', selection.graph_path]
        if resume:
            args += ['--resume']
            if selection.retry_plan:
                args += ['--retry-plan']
            if selection.retry_audit:
                args += ['--retry-audit']
        else:
            args += ['--repo-root', str(root)]
            if selection.auditor:
                args += ['--auditor-deployment', selection.auditor]
            if selection.wait_before_implement:
                args += ['--wait-before-implement', selection.wait_before_implement]
            if selection.command_protection:
                from .command_protection import validate
                protection = validate(selection.command_protection)
                if selection.terminal:
                    raise ValueError('Protected Work cannot use an interactive terminal.')
                args += ['--command-review', protection['review'], '--command-isolation', protection['backend'],
                         '--max-command-reviews', str(protection['max_reviews']),
                         '--command-review-seconds', str(protection['review_seconds'])]
                if 'classifier' in protection:
                    args += ['--command-classifier', protection['classifier'],
                             '--command-review-policy', protection['review_policy']]
    elif selection.mode == 'session':
        from .console_context import context_prompt
        prompt = context_prompt(selection)
        if selection.folder_note:
            prompt += '\n\n' + selection.folder_note
        args = ['agent', '--format', 'json', '--activity-events']
        if assistant and selection.folder_grants:
            args += ['--execution-permissions', str(Path(selection.folder_grants).parent.parent.parent)]
        if not selection.local_models:  # on every launch of the conversation, new or resumed
            args += ['--no-local-models']
        saved = None
        if resume:
            if not selection.session:
                raise ValueError('Select a saved session first.')
            if ((Path(selection.session)/'continuation.json').is_file()
                    and not (Path(selection.session)/'native-agent.json').exists()):
                from .native_agent import read_json
                request = read_json(Path(selection.session)/'continuation.json', 1024*1024)
                if Path(request['project']).resolve() != root:
                    raise ValueError('The prepared provider switch belongs to another project.')
                if prompt.strip() or selection.attachments:
                    raise ValueError('Resume the prepared provider switch before sending this new draft.')
                return args + ['--continue-provider', '--session', selection.session]
            from .native_agent import read_json
            saved = read_json(Path(selection.session)/'native-agent.json', 1024*1024)
            if root not in [Path(p).resolve() for p in saved['config']['repositories'].values()]:
                raise ValueError('The saved session belongs to another project. Reload the selection.')
            args += ['--resume', '--session', selection.session]
            if selection.recheck_candidate or selection.retry_delivery or selection.repair_plan:
                if not assistant or prompt.strip() or selection.attachments:
                    raise ValueError('Recheck uses the saved candidate. Clear the message and attachments first.')
                args += ['--repair-plan' if selection.repair_plan else '--retry-delivery' if selection.retry_delivery else '--recheck-candidate']
                if selection.resume_coordinator_seconds:
                    args += ['--active-time-budget',str(selection.resume_coordinator_seconds)]
                if selection.resume_coordinator_calls:
                    args += ['--active-call-budget',str(selection.resume_coordinator_calls)]
            if selection.attachments and not saved.get('attachments_enabled'):
                raise ValueError('Start a new conversation to use attachments; this saved session does not support them.')
            if prompt.strip() or selection.attachments:
                if saved.get('messages_enabled') and not saved.get('conversation'):
                    from .native_messages import inspect
                    if any(row['status'] == 'QUEUED' for row in inspect(selection.session, root)['items']):
                        raise ValueError('Queued messages are waiting. Choose Resume before sending this draft.')
                args += ['--follow-up', prompt, '--message-id', uuid4().hex]
                if assistant and selection.resume_coordinator_seconds:
                    args += ['--active-time-budget',str(selection.resume_coordinator_seconds)]
                if assistant and selection.resume_coordinator_calls:
                    args += ['--active-call-budget',str(selection.resume_coordinator_calls)]
            if selection.refresh:
                args += ['--refresh']
        else:
            if not prompt.strip() and not selection.attachments:
                raise ValueError('Describe the task first.')
            if selection.terminal and selection.role != 'implementer':
                raise ValueError('System commands require edit access in project permissions (/permissions).')
            from .project_admission import check
            policy = selection.project_admission
            check(root, policy)
            admission_args = ['--project-admission', policy['mode']]
            if policy['mode'] == 'adopted' or policy['mode'] == 'trusted' and policy['root_sha256']:
                admission_args += ['--root-instruction-pin', policy['root_sha256'] or 'absent']
            if policy['mode'] != 'quarantine' and policy['nested']:
                admission_args += ['--nested-instructions']
            selection.session = str(session_root()/uuid4().hex)
            args += admission_args
            from .console_entry import ASSISTANT_CONTEXT
            instruction = (ASSISTANT_CONTEXT if assistant else '')+prompt
            args += ['--root', str(root), '--session', selection.session,
                     '--instruction', instruction, '--role', selection.role]
            if not assistant and selection.role=='auditor':
                args += ['--typed-output']
            args += ['--specgraph-context' if selection.specgraph_context else '--no-context']
            if selection.role == 'implementer':
                from .project_permissions import validate_defaults
                scopes = validate_defaults({'mode': 'edit', 'write_paths': selection.write_paths})['write_paths']
                for path in scopes:
                    args += ['--write-path', path]
            if assistant and selection.streaming:
                args += ['--stream']
            if selection.command_protection:
                from .command_protection import validate
                protection = validate(selection.command_protection)
                if selection.terminal or selection.role != 'implementer':
                    raise ValueError('Protected commands require an implementer without an interactive terminal.')
                args += ['--command-review', protection['review'], '--command-isolation', protection['backend'],
                         '--max-command-reviews', str(protection['max_reviews']),
                         '--command-review-seconds', str(protection['review_seconds'])]
                if 'classifier' in protection:
                    args += ['--command-classifier', protection['classifier'],
                             '--command-review-policy', protection['review_policy']]
            if selection.terminal:
                args += ['--terminal']
            if selection.attachments_enabled:
                args += ['--allow-attachments']
            if selection.graph_path:
                args += ['--project-graph', selection.graph_path]
            if selection.folder_grants:
                args += ['--folder-grants', selection.folder_grants]
        if assistant:
            args += ['--conversation']
        if selection.attachments:
            from .console_attachments import input_file  # images a blind model cannot see are described by the agent
            args += ['--attachment-input', str(input_file(selection))]
    else:
        raise ValueError('Unknown execution mode.')
    if not resume:
        if selection.mode in {'session', 'work', 'sequence'} and (selection.findings or selection.findings_sha256):
            if not selection.findings or not selection.findings_sha256:
                raise ValueError('Select reviewed findings with their identity.')
            args += ['--findings', selection.findings, '--findings-sha256', selection.findings_sha256]
        if selection.mode == 'plan' and selection.auto_planner and selection.profile.strip():
            raise ValueError('Choose automatic planner selection or a custom profile, not both.')
        auto = (selection.auto_planner if selection.mode == 'plan' else selection.auto_execution)
        if auto and selection.profile.strip():
            raise ValueError('Choose automatic selection or a custom profile, not both.')
        args += (['--native-deployment', str(Path(selection.profile).resolve())] if selection.profile.strip()
                 else ['--deployment', 'auto' if auto else selection.deployment])
        from .execution_scope import current
        from .execution_resources import LEGACY
        resources = current().unlimited or (LEGACY if selection.unlimited else ())
        if resources:
            args += ['--unlimited', ','.join(resources)]
        args += budget_args
        if selection.mode == 'session' and selection.verifier.strip():
            argv = json.loads(selection.verifier)
            if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
                raise ValueError('Verification must be a JSON array of command arguments.')
            args += ['--verify-command', *argv]
    return args


class Backend:
    def continuity_terminal(self, source):
        from .console_terminal import session_owner
        terminals = getattr(self, '_terminals', None)
        return terminals._connections.get(session_owner(source)) if terminals else None

    def propose_entry(self, selection, cancellation, history, questions=()):
        from .console_entry import propose
        return propose(selection, cancellation, history=history, questions=questions)

    def send_message(self, selection, text, message_id):
        from .native_messages import enqueue
        if selection.mode != 'session' or not selection.session:
            raise ValueError('Select a direct session first.')
        options = {}
        if selection.attachments:
            from .console_attachments import selected_content, check_image_profile
            from .native_agent import read_json
            saved = read_json(Path(selection.session)/'native-agent.json',1024*1024)
            check_image_profile(selection, saved)
            options = {'attachments':selected_content(selection), 'attachment_root':selection.attachment_root}
        return enqueue(selection.session, text, message_id=message_id, project=selection.root, **options)

    def message_status(self, selection, message_id):
        from .native_messages import inspect
        view = inspect(selection.session, selection.root)
        return next(item['status'] for item in view['items'] if item['message_id'] == message_id)

    def message_observations(self, selection):
        from .native_messages import inspect
        if selection.mode != 'session' or not selection.session:
            raise ValueError('Select a direct session first.')
        return inspect(selection.session, selection.root)['items']

    def synthesis_preview(self, selection):
        from .research_synthesis import preview
        return preview(selection.root, selection.prompt, json.loads(selection.synthesis_inputs))

    def synthesis_view(self, selection):
        from .research_synthesis import inspect
        if not selection.synthesis:
            raise ValueError('Select a saved synthesis first.')
        value = inspect(selection.synthesis)
        if Path(value['root']).resolve() != Path(selection.root).resolve():
            raise ValueError('The synthesis belongs to another project.')
        return value

    def synthesis_inventory(self, root):
        from . import research_flow, parallel_explore
        rows = [{'kind': 'research', **r} for r in research_flow.inventory(root)]
        for path in parallel_explore.batch_root().glob('*/batch.json'):
            try:
                view = parallel_explore.inspect(path.parent)
                if Path(view['root']).resolve() == Path(root).resolve():
                    rows.append({'kind': 'parallel', 'directory': view['batch'],
                                 'question': ' / '.join(item['question'] for item in view['items']),
                                 'status': view['status']})
            except (OSError, ValueError, KeyError):
                continue
        return rows

    def research_view(self, selection):
        from .research_flow import inspect
        if not selection.research:
            raise ValueError('Select saved research first.')
        value = inspect(selection.research)
        if Path(value['root']).resolve() != Path(selection.root).resolve():
            raise ValueError('The research belongs to another project.')
        return value

    def research_inventory(self, root):
        from .research_flow import inventory
        return inventory(root)

    def research_source(self, selection, ref, start, end):
        self.research_view(selection)
        from .research_flow import source
        return source(selection.research, ref, start, end)

    def batch_view(self, selection):
        from .parallel_explore import inspect
        if not selection.batch:
            raise ValueError('Select a saved exploration batch first.')
        value = inspect(selection.batch)
        if Path(value['root']).resolve() != Path(selection.root).resolve():
            raise ValueError('The exploration belongs to another project.')
        return value

    def sequence_view(self, selection):
        require_control_graph(selection)
        from .work_sequence import inspect
        value = inspect(selection.sequence)
        if Path(value['root']).resolve() != Path(selection.root).resolve():
            raise ValueError('The sequence belongs to another project.')
        return value

    def work_control(self, selection):
        from .work_control import inspect
        if selection.mode != 'work' or not selection.work:
            raise ValueError('Select a saved Work first.')
        return inspect(selection.work, graph_path=selection.graph_path or None,
                       repository_root=selection.root)

    def control_work(self, selection, operation, token, note=''):
        from .work_control import command
        if selection.mode != 'work' or not selection.work:
            raise ValueError('Select a saved Work first.')
        return command(selection.work, operation, token, note,
                       graph_path=selection.graph_path or None, repository_root=selection.root)

    def decision(self, selection):
        from .work_run import train_dir_for
        from .human_wait import inspect
        view = inspect(train_dir_for(selection.work, graph_path=selection.graph_path or None))
        if view and Path(view['request']['repository_root']).resolve() != Path(selection.root).resolve():
            raise ValueError('The pending decision belongs to another project.')
        return view

    def answer_decision(self, selection, digest, decision, note):
        self.decision(selection)
        from .human_wait import answer
        return answer(selection.work, digest, decision, note, graph_path=selection.graph_path or None)

    def catalog(self, root):
        from .native_catalog import available_models
        from .native_sessions import inventory
        from . import relay_mesh
        from tare_ops import read_graph
        sessions = inventory(root)
        errors = [str(item) for item in sessions['unavailable']]
        try:
            works = read_graph(relay_mesh.WORK_GRAPH_FILE)['nodes']
        except FileNotFoundError:
            works = []  # Standalone installs need no OS development backlog.
        except (ValueError, OSError, KeyError) as exc:
            works = []; errors.append('Work graph unavailable: '+str(exc))
        return {'models': available_models(), 'sessions': sessions['sessions'],
                'works': works, 'errors': errors}

    async def launch(self, args, root):
        from . import relay_mesh
        env = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                   TARE_OS_ROOT=str(relay_mesh.RELAY_ROOT))
        env.pop('TARE_TERMINAL_CONNECTION',None)
        if args:
            env['TARE_FLOW'] = args[0]  # the flow its sessions ran in, for the shadow of session turns (work_forms)
        from .execution_scope import current, ENV
        policy = current()
        if policy.active:
            env[ENV] = json.dumps(asdict(policy), sort_keys=True, separators=(',', ':'))
            from .turn_engine import project_permission_mode
            from .native_installation import storage_root
            self.action_approval_root=str(storage_root()/'tui-approvals'/uuid4().hex)
            env.update(TARE_ACTION_APPROVAL_ROOT=self.action_approval_root,
                TARE_ACTION_APPROVAL_PROJECT=str(Path(root).resolve()),
                TARE_SESSION_OPERATOR_MODE=project_permission_mode(root) or 'read')
        # An empty PYTHONPATH entry means the current directory, the project: the same shadowing -P prevents.
        env['PYTHONPATH'] = os.pathsep.join(p for p in env.get('PYTHONPATH', '').split(os.pathsep) if p)
        if args and args[0] == 'agent' and '--session' in args:
            custody = Path(args[args.index('--session')+1])
            if '--resume' in args or '--continue-provider' in args:
                from .native_agent import read_json
                config_path = custody/'native-agent.json'
                if not config_path.exists():
                    config_path = Path(read_json(custody/'continuation.json', 1024*1024)['source'])/'native-agent.json'
                config = read_json(config_path,1024*1024)['config']
                terminal = config.get('terminal',False)
                repositories = config['repositories']
            else:
                terminal = '--terminal' in args
                repositories = {Path(root).name:str(Path(root).resolve())}
            if terminal:
                from .console_terminal import ConsoleTerminals
                if getattr(self,'_terminals',None) is None:
                    self._terminals = ConsoleTerminals()
                descriptor = await asyncio.to_thread(self._terminals.connection,custody,repositories)
                env['TARE_TERMINAL_CONNECTION'] = json.dumps(descriptor)
        options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
                   else {'start_new_session': True})
        from .flow_activity import MODES
        entry = 'flow_activity' if args and args[0] in MODES else 'native_installation'
        # -P: the project directory (cwd) must not shadow tare's own packages; a project with its own relay/ (a
        # tare.tools.os checkout, or any repository) would otherwise run its code as the agent.
        process = await asyncio.create_subprocess_exec(sys.executable, '-P', '-u', '-c',
            f'from relay.{entry} import main; raise SystemExit(main())', *args,
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **options)
        self.contain(process)
        return process

    def contain(self, process):
        """Tie a launched child to this TUI process: one kill-on-close Job, closed by Windows when the TUI ends."""
        # ponytail: POSIX children are not tied to the TUI yet; it needs PR_SET_PDEATHSIG or a supervising pipe.
        if os.name != 'nt':
            return
        try:
            from compute_plane.managed_process import WindowsJob
            job = getattr(self, '_job', None)
            if job is None:
                job = self._job = WindowsJob()  # never closed: the handle dies with this process
            popen = process._transport.get_extra_info('subprocess')
            if not job.k.AssignProcessToJobObject(job.handle, int(popen._handle)):
                raise job.c.WinError(job.c.get_last_error())
            self.containment = 'job'
        except Exception as exc:  # a turn must still run without containment
            self.containment = f'unavailable: {exc}'

    def close(self):
        if getattr(self,'_terminals',None) is not None:
            self._terminals.close()

    def terminals(self, custody):
        from .console_terminal import SessionTerminal
        if getattr(self,'_terminals',None) is None:
            raise ValueError('This conversation has not opened a terminal in this TUI yet.')
        return SessionTerminal(self._terminals,custody)

    def cancel(self, process):
        if process.returncode is None:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)

    def progress(self, selection):
        if selection.mode == 'synthesis':
            return self.synthesis_view(selection)['status']+' · advisory synthesis'
        if selection.mode == 'research':
            value = self.research_view(selection)
            if value['status'] == 'ANALYZING':
                return 'Research · analyzing collected sources'
            return value['status']+f" · {len(value['sources'])}/{len(value['urls'])} collected URLs"
        if selection.mode == 'parallel':
            value = self.batch_view(selection)
            return value['status']+' · '+', '.join(r['id']+': '+r['status'] for r in value['items'])
        if selection.mode == 'sequence':
            value = self.sequence_view(selection)
            active = ' · Work '+value['active_work'] if value['active_work'] else ''
            return f"Sequence · {value['completed']}/{value['total']} completed"+active
        if selection.mode == 'plan':
            directory = Path(selection.plan)
            stage = 'auditor' if (directory/'auditor').exists() else 'planner'
            from .native_agent import read_json
            if (directory/'review-attempts.json').exists():
                attempts = read_json(directory/'review-attempts.json')['attempts']
                if attempts:
                    stage = attempts[-1]['directory']
            return 'Plan · '+('drafting proposal' if stage == 'planner' else 'reviewing proposal')
        if selection.mode == 'work':
            from .work_run import status
            value = status(selection.work, graph_path=selection.graph_path or None)
            from .work_console import control_copy
            message = control_copy(value.get('operational_control'))
            if message:
                return message
            stage = value['train']['status'] if value['train'] else 'preparing'
            return 'Work '+selection.work+' · '+stage.replace('_', ' ').lower()
        # Observational tail only; never interpret partial journal data as a receipt.
        path = Path(selection.session)/'journal.jsonl'
        if not path.exists():
            return 'Preparing session…'
        with path.open('rb') as stream:
            stream.seek(0, 2); size = stream.tell(); stream.seek(max(0, size-16384))
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)['event']
                kind = event['kind']
                if kind == 'model_requested':
                    return 'Model responding · '+event['call_id'].rsplit('/', 1)[-1]
                if kind == 'effect_requested':
                    call = event['tool_call']; args = json.loads(call['arguments_json'])
                    return 'Tool: '+call['name']+' '+str(args.get('path', args.get('name', '')))
                if kind == 'terminal':
                    return 'Recorded outcome: '+str(event.get('status', '?'))
            except (ValueError, KeyError, TypeError):
                continue
        return 'Session running; waiting for the next observation.'

    def inspect(self, selection):
        if selection.mode == 'synthesis':
            from .research_synthesis import render
            return render(self.synthesis_view(selection))
        if selection.mode == 'research':
            from .research_flow import render
            return render(self.research_view(selection))
        if selection.mode == 'parallel':
            from .parallel_explore import render
            return render(self.batch_view(selection))
        if selection.mode == 'sequence':
            from .work_sequence import render
            return render(self.sequence_view(selection))
        if selection.mode == 'work':
            from .work_console import inspect_work, render
            return render('explain', inspect_work(work_id=selection.work, graph_path=selection.graph_path or None))
        from .native_agent import read_json
        state = read_json(Path(selection.session)/'native-agent.json', 1024*1024)
        return '\n'.join([state['instruction'], '', 'Saved model: '+state['deployment']['native_profile']['model'],
            'Saved limits: '+json.dumps(state.get('limits', {})),
            'Repositories: '+json.dumps(state['config']['repositories'])])

    def review(self, selection):
        if selection.mode in {'research', 'parallel', 'synthesis'}:
            return self.inspect(selection)
        if selection.mode == 'plan':
            from .native_plan import render
            return render(self.plan_preview(selection))
        if selection.mode == 'session':
            from .native_review import review, render
            return render(review(selection.session))
        from .work_run import status
        from .work_console import render
        from .native_agent import read_json
        from .candidate_change import read_candidate_receipt, verify_candidate_receipt
        from .train_execution import read_execution_descriptor
        state = status(selection.work, graph_path=selection.graph_path or None)
        if not state['train']:
            return render('status', state)
        train = Path(state['train']['dir'])
        lines = [render('status', state)]
        if (train/'CANDIDATE_CHANGE.json').exists():
            origin = read_json(train/'WORK_RUN.json', 1024*1024)
            descriptor = read_execution_descriptor(train/'EXECUTION_DESCRIPTOR.json')
            receipt = read_candidate_receipt(train/'CANDIDATE_CHANGE.json')
            # The existing owner validates the exact recorded candidate before presentation.
            verify_candidate_receipt(receipt, descriptor, repository_root=origin['repository_root'])
            diff = subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false', '-C', origin['repository_root'], 'diff', '--no-ext-diff',
                '--no-textconv', receipt.payload['base_commit'], receipt.payload['candidate_commit'], '--'],
                capture_output=True, check=True).stdout.decode('utf-8', errors='replace')
            lines += ['\nExact recorded candidate diff:', diff]
        for name in ('NATIVE_VERIFIER_PROCESS.json', 'ADMISSION_VERDICT.json', 'PLAN_AUDIT_REPORT.md', 'AUDIT_REPORT.md'):
            path = train/name
            if path.exists():
                lines += ['\n'+name, path.read_text(encoding='utf-8')]
        lines.append('Recorded evidence; this view does not rerun tests or grant approval.')
        return '\n'.join(lines)

    def plan_preview(self, selection):
        from .native_plan import preview
        result = preview(selection.plan)
        if Path(result['proposal']['roots'][result['proposal']['repository']]).resolve() != Path(selection.root).resolve():
            raise ValueError('This proposal belongs to another project.')
        return result

    def apply_plan(self, directory, digest):
        from .native_plan import apply
        return apply(directory, digest)

    def work_sources(self, selection):
        from . import relay_mesh, work_prepare
        return work_prepare.source_options(selection.graph_path or relay_mesh.WORK_GRAPH_FILE,
            selection.work, selection.root)

    def work_source_details(self, selection):
        from . import relay_mesh, work_prepare
        source = work_prepare.source_details(selection.graph_path or relay_mesh.WORK_GRAPH_FILE, selection.work,
            selection.root, selection.source_ref)
        return '\n'.join(['Pinned source: '+source['semantic_document_id']+' @ '+source['revision'],
            *[key+': '+text for key, text in source['acceptance_criteria'].items()],
            'Choose criterion IDs below; the preparation preview shows the exact replacement.'])

    def prepare_work(self, selection):
        from . import relay_mesh, work_prepare
        return work_prepare.prepare(selection.graph_path or relay_mesh.WORK_GRAPH_FILE, selection.work, selection.root,
            selection.source_ref,
            [c.strip() for c in selection.criterion_ids.split(',')], json.loads(selection.work_verifier))

    def apply_preparation(self, view):
        if 'intake' in view:
            from .work_intake import apply
            return apply(view)
        from .work_prepare import apply
        return apply(view)

    def work_intake_draft(self, selection):
        from . import relay_mesh, work_intake
        _, node = work_intake.draft(selection.graph_path or relay_mesh.WORK_GRAPH_FILE, selection.work)
        return node

    def preview_work_intake(self, selection):
        from . import relay_mesh, work_intake
        return work_intake.preview(selection.graph_path or relay_mesh.WORK_GRAPH_FILE, selection.work, selection.root,
            [line.strip() for line in selection.intake_criteria.splitlines() if line.strip()],
            selection.intake_argv.splitlines())

    def preview_revision(self, selection):
        from . import relay_mesh, work_change
        return work_change.preview(selection.graph_path or relay_mesh.WORK_GRAPH_FILE, selection.work, selection.root,
            selection.source_ref,
            [c.strip() for c in selection.criterion_ids.split(',')], json.loads(selection.work_verifier),
            selection.revision_reason, json.loads(selection.revision_paths) if selection.revision_paths else None)

    def apply_revision(self, view):
        from .work_change import apply
        return apply(view)

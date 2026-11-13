"""Local session composition of existing review, exact guard and Windows isolation."""
import ast
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import ntpath
import os
from pathlib import Path
import secrets
import shlex
import re
import sys
import time

from compute_plane.action_control import ActionAuthorization, ActionRefused, ActionSnapshot, ExactActionGuard
from compute_plane.native_journal import NativeJournal
from compute_plane.windows_mxc import MxcPolicy, policy_sha256
from compute_plane.workspace import isolated_child_environment
from .action_control import ActionDecision, ActionPolicy, ActionReviewBroker
from .action_mediation import CommandActionMediator
from .action_model_assessment import TareActionClassifier, TareActionReviewer, TareJevActionClassifier, JEV_MODEL
from .command_permission import UNREVIEWED, unreviewed as allowed_this_turn, yolo
from . import sandbox_secrets
from .windows_isolation import windows_isolation_factory
from .worker_wait import steady

REVIEWS = ('auto', 'tare', 'codex', 'both')
BACKENDS = ('mxc', 'codex')
DEFAULTS = {'review': 'codex', 'backend': 'mxc', 'max_reviews': 6, 'review_seconds': 60}
CLASSIFIERS = ('model', 'jev')
REVIEW_POLICIES = ('always', 'on_demand')
CONFIG_KEYS = set(DEFAULTS) | {'classifier', 'review_policy'}
SANDBOX_PYTHON = Path(__file__).resolve().parent/'sandbox-python'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def file_digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def prepared_checks(commands):
    """Resolve only host-registered checks; never discover arbitrary model commands."""
    from compute_plane.managed_process import host_executable
    return {name:[str(Path(argv[0]).resolve(strict=True)) if Path(argv[0]).is_absolute()
                  else host_executable(argv[0],required=True),*argv[1:]] for name,argv in commands.items()}


def verifier_runtime(executable):
    path=Path(executable).resolve(strict=True)
    folder=path.parent
    # Git for Windows' cmd launcher loads its runtime from the same installation.
    if (path.name.lower()=='git.exe' and folder.name.lower() in ('cmd','bin')
            and any((folder.parent/p).is_dir() for p in ('mingw64','ucrt64'))):
        return folder.parent
    if folder.name.lower() in ('scripts','bin') and (folder.parent/'pyvenv.cfg').is_file():
        return folder.parent  # the interpreter needs its own Lib/site-packages as well
    return folder


def validate(value):
    if (not isinstance(value,dict) or set(value)-CONFIG_KEYS
            or value.get('review',DEFAULTS['review']) not in REVIEWS
            or value.get('backend',DEFAULTS['backend']) not in BACKENDS):
        raise ValueError('Choose automatic review, Tare, Codex or both and an available isolation backend.')
    if value.get('review') == 'auto' and any(k in value for k in ('classifier', 'review_policy')):
        raise ValueError('Automatic review chooses the classifier from the execution connection.')
    if value.get('classifier','model') not in CLASSIFIERS or value.get('review_policy','always') not in REVIEW_POLICIES:
        raise ValueError('Choose the Jev classifier and on-demand or always review.')
    if value.get('review_policy')=='on_demand' and value.get('classifier')!='jev':
        raise ValueError('On-demand review requires the independent Jev classifier.')
    result={**DEFAULTS,**value}
    if 'classifier' in value or 'review_policy' in value:
        result.setdefault('classifier','model');result.setdefault('review_policy','always')
    for key,upper in (('max_reviews',24),('review_seconds',120)):
        if type(result[key]) is not int or not 1<=result[key]<=upper:
            raise ValueError(f'{key} must be between 1 and {upper}.')
    return result


def prepare(value,deployment):
    """Freeze the selected installed CLI before session persistence; no CLI discovery."""
    result=validate({k:v for k,v in value.items() if k in CONFIG_KEYS})
    if result['review']=='auto':
        if any(k in value for k in ('codex_sha256','jev_model')):
            raise ValueError('Automatic routing is resolved when an action executes; it cannot pin another route.')
        return result
    if result.get('classifier')=='jev':
        if value.get('jev_model') not in (None,'jev-1.13.0',JEV_MODEL):
            raise ValueError('The saved Jev model changed; start a new conversation.')
        result['jev_model']=value.get('jev_model') or JEV_MODEL
    cli=deployment.get('native_cli',{})
    needs_cli=result['review'] in ('codex','both') or result['backend']=='codex'
    if needs_cli:
        if (deployment.get('native_profile',{}).get('endpoint')!='cli://codex'
                or not cli.get('executable') or not Path(cli['executable']).is_absolute()):
            raise ValueError('Native Codex review requires the selected Codex CLI connection.')
        path=Path(cli['executable']).resolve(strict=True)
        expected=value.get('codex_sha256')
        actual=file_digest(path)
        if expected is not None and expected!=actual:
            raise ValueError('The saved Codex executable changed; start a new conversation.')
        result['codex_sha256']=actual
    return result


def saved(value):
    if not isinstance(value,dict) or set(value)-CONFIG_KEYS-{'codex_sha256','jev_model'}:
        raise ValueError('Invalid command protection configuration.')
    result=validate({k:v for k,v in value.items() if k in CONFIG_KEYS})
    if result['review']=='auto' and any(k in value for k in ('codex_sha256','jev_model')):
        raise ValueError('Automatic routing cannot carry another route identity.')
    if value.get('jev_model') is not None:
        if result.get('classifier')!='jev' or value['jev_model'] not in ('jev-1.13.0',JEV_MODEL):
            raise ValueError('The saved Jev model changed; start a new conversation.')
        result['jev_model']=value['jev_model']
    pin=value.get('codex_sha256')
    if pin is not None:
        if not isinstance(pin,str) or len(pin)!=64 or any(c not in '0123456789abcdef' for c in pin):
            raise ValueError('Invalid saved Codex executable digest.')
        result['codex_sha256']=pin
    return result


def factory(config,deployment,custody,public_context,invocation_id,activity=None,*,host_checks=False):
    options=prepare(config['command_protection'],deployment)
    if options!=config['command_protection']:
        raise ValueError('Command protection identity was not frozen before saving the session.')
    automatic=options['review']=='auto'
    if automatic:
        native=deployment.get('native_profile',{}).get('endpoint')=='cli://codex'
        options=prepare({**options,'review':'codex' if native else 'tare',
            **({} if native else {'classifier':'jev','review_policy':'on_demand'})},deployment)
    from .execution_scope import current
    temporary=current()
    disabled=temporary.disable_judge
    local_classification=temporary.only=='local' and options['review'] in ('tare','both') and not disabled
    if local_classification:
        from .turn_engine import LOCAL_JUDGE
        options={**options,'classifier':'local','review_policy':'on_demand','local_judge_model':LOCAL_JUDGE}
        options.pop('jev_model',None)
    if disabled:options={**options,'judge_override':'disabled'}
    jev=not disabled and options.get('classifier') in ('jev','local')
    if sys.platform!='win32':
        raise ValueError('Protected commands currently require Windows; no unisolated fallback.')
    if config.get('terminal') or len(config['repositories'])!=1:
        raise ValueError('Protected commands require one project and no interactive terminal.')
    def public_request():
        value=public_context() if callable(public_context) else public_context
        if not isinstance(value,str) or len(value.encode())>8192:
            raise ActionRefused('Command review context exceeds 8 KiB; use a smaller demand.')
        return value
    root=Path(next(iter(config['repositories'].values()))).resolve(strict=True)
    scratch=Path(custody)/'command-tmp';scratch.mkdir(exist_ok=True)
    journal=NativeJournal(Path(custody)/'command-reviews.jsonl')
    policy_text=json.dumps({'project':str(root),'writable':config['writable'],'protection':options},sort_keys=True)
    context='';context_sha=''
    pinned_cli=deployment.get('native_cli',{})
    policy_identity=digest(options)

    def append(event):
        if journal.append(dict(event,invocation_id=invocation_id,authority='NONE')) is False:
            raise ActionRefused('command_review_custody_unavailable')

    def announce(action,detail,state='running'):
        if activity is not None:
            activity({'kind':'tool-activity','id':action.effect_id,'operation':'command',
                      'state':state,'target':' '.join(action.argv),'detail':detail,
                      'duration_ms':None,'receipt':'','truncated':False})

    def reserved_count():
        return sum(r.event().get('kind')=='command_review_reserved'
                   and r.event().get('invocation_id')==invocation_id for r in journal.snapshot().records)

    def reserve(stage,backend,control,action=None):
        if control.should_stop() or reserved_count()>=options['max_reviews']:
            return False
        append({'kind':'command_review_reserved','stage':stage,'backend':backend,
                'action_sha256':None if action is None else action.action_sha256})
        return True

    def bind(**session):
        import tempfile
        import weakref
        capabilities,control,binding=session['capabilities'],session['control'],session['binding']
        if host_checks and set(capabilities.names)-{'read','verify'}:
            raise ValueError('Host check metadata requires read/verify-only capabilities.')
        environment=isolated_child_environment({'TEMP':str(scratch),'TMP':str(scratch),
            'USERPROFILE':str(scratch),'HOME':str(scratch),'PYTHONDONTWRITEBYTECODE':'1',
            'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
        # pytest's Windows tempfile retry loop treats sandbox-denied mkdir as a
        # name collision. Keep its cache in the admitted scratch directory.
        # Deep session custody plus pytest fixture/lock names can exceed MAX_PATH.
        # Own a short, unique host temp directory; never scan shared pytest roots.
        test_directory=tempfile.TemporaryDirectory(prefix='tare-test-')
        test_scratch=Path(test_directory.name)
        # AppContainer replaces TEMP/TMP with its own directory, whose delete
        # access can be denied. Python honors TMPDIR first; keep ordinary
        # TemporaryDirectory calls in our admitted, short, owned scratch.
        environment['TMPDIR']=str(test_scratch)
        environment['PYTEST_ADDOPTS']=shlex.join(['-o','cache_dir='+str(test_scratch/'cache'),
                                               '--basetemp='+str(test_scratch/'tests')])
        # Both Windows backends deny loopback, where socket.socketpair (and so asyncio) would wait forever; this
        # sitecustomize gives Python an AF_UNIX pair instead. Codex's launcher passes the same environment on.
        environment.update(PYTHONPATH=str(SANDBOX_PYTHON),TARE_SANDBOX_SOCKETPAIR='1')
        runtime_roots={str(Path(sys.base_prefix).resolve()),str(Path(sys.prefix).resolve())}
        if not any(SANDBOX_PYTHON.is_relative_to(p) for p in runtime_roots):
            runtime_roots.add(str(SANDBOX_PYTHON))  # a source checkout; a release has it inside its venv
        runtime_roots.update(str(verifier_runtime(row['argv'][0])) for row in config.get('verifiers',{}).values()
            if Path(row['argv'][0]).is_absolute())
        runtime_roots.update(str(verifier_runtime(p)) for p in config.get('command_runtimes', []))
        from compute_plane.managed_process import host_executable
        search_executable=host_executable('rg') if 'search' in capabilities.names else None
        if search_executable:
            runtime_roots.add(str(verifier_runtime(search_executable)))
        read=[os.environ['SystemRoot'],*sorted(runtime_roots)]
        references=[]
        if config.get('folder_access'):
            from .folder_access import folder, granted
            references=[str(folder(p)) for p in granted(config['folder_access']['grants'])]
            read.extend(references)
        write=[]
        for scopes in config['writable'].values():
            write.extend(str((root/p).resolve()) for p in scopes)
        read.append(str(root));write.extend((str(scratch),str(test_scratch)))
        from .native_installation import storage_root
        staged_effects=bool(root.parent==storage_root().resolve()/'wt' and re.fullmatch('[0-9a-f]{8}',root.name))
        if staged_effects:
            # Build artifacts belong to the host-owned candidate. The original
            # project remains outside the command write boundary; application
            # still rejects unplanned source changes after independent review.
            write.append(str(root))
        # Every classifier: secrets at any depth of the project and its granted folders, found once per binding.
        denied=sandbox_secrets.find([root,*references],git=not (host_checks or staged_effects))
        if (host_checks or staged_effects) and (root/'.git').exists():
            # Exact host verifiers and private staged workers can inspect Git
            # metadata, with no metadata write grant. This admits status/diff
            # while preventing commits/index/ref writes to the real repository.
            metadata=root/'.git'
            if metadata.is_file():
                reference=metadata.read_text(encoding='utf-8').strip()
                if not reference.startswith('gitdir: '):raise ValueError('Invalid candidate Git metadata.')
                metadata=(root/reference.removeprefix('gitdir: ')).resolve(strict=True)
            read.extend((str(root/'.git'),str(metadata)))
            if (metadata/'commondir').is_file():
                read.append(str((metadata/(metadata/'commondir').read_text().strip()).resolve(strict=True)))
            # Git discovers this worktree from cwd. A global GIT_WORK_TREE also
            # reaches test fixtures' init/clone commands and changes their roots.
            environment['GIT_OPTIONAL_LOCKS']='0'
        # Runtime and project read scopes are explicit; nested scopes retain their configured write grants.
        read=[p for p in dict.fromkeys(read) if p not in write]
        # No enumerate-only scope: PSEC's fs_enumerate admits listing of the whole subtree, so the volume
        # root's lets a command list every folder on the drive (probed 2026-11-13). Ancestors stay
        # stat-able without it; only the volume root's own metadata is then denied.
        policy=MxcPolicy(tuple(read),tuple(dict.fromkeys(write)),tuple(denied),registry_read=True)
        # MXC denies a path: renaming a writable folder above a nested secret would carry the secret out of the rule.
        held=sandbox_secrets.Hold(sandbox_secrets.movable(denied,write))
        deadline_cache={};issued={};observed={};routine_ports={}

        def sources(argv):
            executable=Path(argv[0])
            if not executable.is_absolute() or not executable.is_file():
                raise ActionRefused('Use an absolute admitted executable path for a protected command.')
            admitted=[Path(p) for p in (*policy.read_only,*policy.read_write)]
            if not any(executable.resolve().is_relative_to(p.resolve()) for p in admitted):
                raise ActionRefused('Command executable is outside the configured runtime and project.')
            files=[executable]
            for arg in argv[1:]:
                candidate=Path(arg)
                candidate=candidate if candidate.is_absolute() else root/candidate
                if candidate.is_file() and any(candidate.resolve().is_relative_to(p.resolve()) for p in admitted):
                    files.append(candidate)
            if any(path.resolve().is_relative_to(Path(p).resolve()) for path in files for p in policy.denied):
                raise ActionRefused('Command input is denied by the selected Windows profile.')
            rows=[];total=0
            for path in dict.fromkeys(files):
                size=path.stat().st_size;total+=size
                limit=512*1024*1024 if path==executable else 32*1024*1024
                if path.is_symlink() or size>limit or total>544*1024*1024:
                    raise ActionRefused('Command input identity is unavailable or oversized.')
                rows.append((str(path.resolve()),file_digest(path)))
            return digest(rows)

        def snapshot(facts,live):
            nonlocal context,context_sha
            if live.should_stop():raise ActionRefused('Command was cancelled or expired.')
            context=public_request()+'\nOperator-selected command scope:\n'+policy_text
            context_sha=hashlib.sha256(context.encode()).hexdigest()
            source=sources(facts.argv)
            deadline=deadline_cache.setdefault(facts.effect_id,min(live.deadline_monotonic,
                time.monotonic()+options['review_seconds']))
            return ActionSnapshot(binding.work_id,session['session_id'],facts.effect_id,binding.binding_id,
                binding.fence,facts.operation,facts.repository,facts.cwd,facts.environment_sha256,
                context_sha,source,policy_identity,facts.grant_sha256,
                'windows-mxc/1:'+policy_sha256(policy),deadline,argv=facts.argv,payload_json=facts.payload_json)

        def classifier_facts(action):
            if sources(action.argv)!=action.source_sha256:
                raise ActionRefused('Command input changed before Jev classification.')
            facts={'network':'denied','writable':config['writable'],
                'command_write_roots':list(policy.read_write),
                'read_only_references':references,
                'denied':list(policy.denied),'referenced_code':[],
                'unknowns':['Runtime internals and transitive dependencies are not inspected.']}
            for arg in action.argv[1:]:
                candidate=Path(arg);candidate=candidate if candidate.is_absolute() else root/candidate
                if candidate.suffix.lower() not in ('.py','.ps1','.sh','.bat','.cmd','.js') or not candidate.is_file():
                    continue
                if not candidate.resolve().is_relative_to(root):
                    facts['unknowns'].append('Executable source is outside the admitted project.');continue
                if candidate.stat().st_size>4096:
                    facts['unknowns'].append('Executable source exceeds the bounded context; do not assume benignity.');continue
                try:code=candidate.read_bytes().decode('utf-8',errors='strict')
                except UnicodeError:
                    facts['unknowns'].append('Executable source is not UTF-8; do not assume benignity.');continue
                facts['referenced_code'].append({'path':candidate.relative_to(root).as_posix(),'code':code})
            if sources(action.argv)!=action.source_sha256:
                raise ActionRefused('Command input changed while preparing Jev context.')
            return facts

        def port(kind):
            from .model_choices import declared_effort, reasoning_parameters
            selected=deepcopy(deployment)
            selected['native_profile']['parameters_json']=json.dumps(
                reasoning_parameters(selected,declared_effort(selected,'low' if kind=='classifier' else 'high')))
            cls=TareActionClassifier if kind=='classifier' else TareActionReviewer
            return cls(selected,journal_factory=lambda:journal,custody_factory=nullcontext,
                       disclosure_check=lambda request:request.public_context==context,
                       facts_factory=classifier_facts)

        classifier=reviewer=None
        local_endpoint={'url':None}
        def local_choice(request,*,key,model,timeout):
            from compute_plane.ninfer_litjev import assess
            return assess(request,url=local_endpoint['url'],model=model,timeout=timeout)
        if not disabled and (jev or options['review'] in ('tare','both')):
            classifier=(TareJevActionClassifier(journal_factory=lambda:journal,custody_factory=nullcontext,
                disclosure_check=lambda request:request.public_context==context,
                facts_factory=classifier_facts,**({'assess_choice':local_choice,'provider':'local',
                    'model':options['local_judge_model'],'requires_key':False,
                    'eligibility_check':lambda:True} if local_classification else
                    {'model':options.get('jev_model',JEV_MODEL)})) if jev else port('classifier'))
            reviewer=port('reviewer') if options['review'] in ('tare','both') else None
            assessment=ActionReviewBroker(ActionPolicy(
                mode='codex_native' if jev and options['review']=='codex' else 'tare',
                review_required=not (jev and options.get('review_policy')=='on_demand'),
                allow_routine=jev,external_execution_review=jev and options['review'] in ('codex','both'),
                pause_on_insufficient=jev,max_assessments=2,
                assessment_seconds=options['review_seconds'],output_tokens=256),classifier,
                {} if reviewer is None else {'tare':reviewer},
                lambda action,live:not live.should_stop(),reserve)

        def ask(action,public):
            """The user's answer about this exact command. It waits on the session's own control, whose clock stops
            meanwhile, and forgets the review window: the mediator then takes the action again with a fresh one."""
            from .command_permission import approve
            allowed=approve(action,public,control)
            deadline_cache.pop(action.effect_id,None)
            return allowed

        class Broker:
            def assess(self,action,public,live):
                try:
                    native_search=(search_executable and action.argv
                        and Path(action.argv[0]).resolve()==Path(search_executable).resolve()
                        and json.loads(action.payload_json).get('read_only') is True)
                except (ValueError,TypeError):
                    native_search=False
                native_check=False
                if staged_effects and Path(action.cwd).resolve()==root:
                    declared={tuple(v['argv']) for v in config.get('verifiers',{}).values()}
                    if tuple(action.argv) in declared:
                        if host_checks:
                            native_check=True  # this binding exposes read/verify only
                        else:
                            # The Runtime journal binds this effect to a named
                            # verifier. Raw argv cannot supply this provenance.
                            path=Path(custody)/'journal.jsonl'
                            if path.exists():
                                for record in reversed(NativeJournal(path).snapshot().records):
                                    event=record.event()
                                    if event['kind']=='effect_requested' and event['effect_id']==action.effect_id:
                                        call=event['tool_call']
                                        name=json.loads(call['arguments_json']).get('name')
                                        native_check=(call['name']=='verify' and name in config['verifiers']
                                            and tuple(config['verifiers'][name]['argv'])==tuple(action.argv))
                                        break
                if (native_search or native_check) and (options.get('review_policy')=='on_demand'
                        or automatic and options.get('review_policy')!='always'):
                    decision=ActionDecision(True,'Host-owned registered check in a private candidate; no model assessment required.'
                        if native_check else 'Host-owned read-only source search; no model assessment required.')
                    observed[action.effect_id]=decision
                    routine_ports[action.effect_id]='host_named_verifier' if native_check else 'host_read_only_port'
                    append({'kind':'command_review_decision','effect_id':action.effect_id,
                            'allowed':True,'reason':decision.reason,'assessments':[]})
                    return decision
                manual=disabled and options['review'] in ('tare','both')
                if local_classification:
                    from .turn_engine import local_judge
                    local_endpoint['url']=local_judge()
                    manual=not local_endpoint['url']
                turn_question=manual and local_classification and yolo()  # YOLO; the local judge is on but not available
                needed=(int(options['review'] in ('codex','both')) if manual else
                        1 if jev else {'codex':1,'tare':2,'both':3}[options['review']])
                if reserved_count()+needed>options['max_reviews']:
                    return ActionDecision(False,'Command review budget exhausted; no request sent.')
                announce(action,'Classifying command with local judge.' if local_classification and not manual else
                         'Local judge not available; no model can review this command.' if turn_question else
                         'Waiting for operator command approval.' if manual else 'Classifying command with Jev.' if jev else
                         'Reviewing command with '+options['review']+'.')
                if turn_question:
                    # Ask once per turn before running commands nobody reviews (owner, 2026-11-12).
                    allowed=allowed_this_turn(action,public,control)
                    deadline_cache.pop(action.effect_id,None)  # as in ask(): a wait forgets the review window
                    if allowed:
                        announce(action,'Not reviewed: local judge not available; you allowed commands without review this turn.')
                    decision=ActionDecision(allowed,UNREVIEWED if allowed else 'Human denied running this command without review.')
                elif manual:
                    unattended=yolo()
                    allowed=unattended or ask(action,public)
                    decision=ActionDecision(allowed,'Operator YOLO: deterministic gates and sandbox remain.' if unattended else
                        'Human approved this exact command.' if allowed else 'Human denied this command.')
                else:
                    decision=(assessment.assess(action,public,live) if classifier is not None else
                              ActionDecision(True,'Host-selected native execution review is mandatory.',execution_review_required=True))
                    failed_classification = any(o.stage=='classifier' and o.status in {
                        'UNAVAILABLE','TIMEOUT','ABSTAINED','MALFORMED'} for o in decision.observations)
                    if (not decision.allowed and jev and options['review']=='tare'
                            and options.get('review_policy')=='on_demand' and failed_classification):
                        # No classifier verdict is approval. Reuse the exact-command human gate;
                        # never buy an unconfigured frontier fallback or bypass an observed denial.
                        announce(action,'Classifier unavailable; waiting for approval of this exact command.')
                        allowed=ask(action,public)
                        decision=ActionDecision(allowed,'Human approved this exact command after classifier failure.'
                            if allowed else 'Human denied this command after classifier failure.',decision.observations)
                if decision.allowed and decision.execution_review_required and reserved_count()>=options['max_reviews']:
                    decision=ActionDecision(False,'Native review budget exhausted; no command claim or effect.')
                observed[action.effect_id]=decision
                # The assessed action; after the user's yes the executed one differs only in its deadline.
                append({'kind':'command_review_decision','effect_id':action.effect_id,
                        'action_sha256':action.action_sha256,'allowed':decision.allowed,'reason':decision.reason,
                        'assessments':[{'stage':o.stage,'status':o.status,'reason_codes':o.reason_codes,
                                        'result_origin':o.result_origin} for o in decision.observations]})
                return decision

        def authorize(action,decision,live):
            permit=ActionAuthorization('local-session:'+session['session_id'],secrets.token_urlsafe(24),
                action.action_sha256,action.session_id,action.effect_id,action.binding_id,action.fence_seq,action.deadline)
            issued[action.effect_id]=permit;return permit
        guard=ExactActionGuard(lambda action,permit:issued.get(action.effect_id)==permit,capabilities.claim_action)
        gate=None
        if options['review'] in ('codex','both'):
            from compute_plane.codex_auto_review import CodexNativeCommandReview
            def record(receipt):
                append({'kind':'command_native_review','receipt':receipt})
            gate=CodexNativeCommandReview(pinned_cli['executable'],options['codex_sha256'],
                deployment['native_profile']['model'],dict(os.environ),model_catalog=pinned_cli['model_catalog'],
                disclosure_check=lambda action,public:public==context,
                reserve_review=lambda action,live:reserve('execution','codex',live,action),record_receipt=record)
        def result(action,process,live):
            report={'review':options['review'],'backend':options['backend'],
                    'reserved_calls':reserved_count(),'max_reviews':options['max_reviews'],
                    'billing':'UNKNOWN','authority':'NONE',
                    'review_status':'approved', 'usage':{}}
            if automatic:report['routing']='automatic_by_execution_connection'
            decision=observed[action.effect_id]
            if action.effect_id in routine_ports:
                report.update(review_status='not_required',review_basis=routine_ports[action.effect_id])
            if jev and decision.observations:
                triage=decision.observations[0]
                report['classifier']=options['classifier'];report['classification']=triage.verdict
                if triage.verdict=='ROUTINE_ELIGIBLE' and options.get('review_policy')=='on_demand':
                    report['review_status']='not_required'
            if disabled:report['classifier']='disabled'
            if disabled and options['review'] in ('tare','both') or local_classification and not decision.observations:
                report.update(classifier='disabled' if disabled else 'local_unavailable',
                    review_status='not_reviewed_this_turn' if decision.reason==UNREVIEWED else
                                  'operator_yolo' if 'YOLO' in decision.reason else 'human_approved')
            assessed={action.action_sha256,*(o.action_sha256 for o in decision.observations)}
            for record in journal.snapshot().records:
                event=record.event()
                if event.get('action_sha256') in assessed and event.get('kind')=='action_assessment_result':
                    report['usage'][event['stage']]=json.loads(event.get('usage_json') or 'null')
                elif event.get('kind')=='command_native_review':
                    native=event.get('receipt',{})
                    if native.get('action_sha256')==action.action_sha256:
                        report['usage']['codex']=native.get('usage')
            if (options['review'] in ('codex','both') and action.effect_id not in routine_ports
                    and (not jev or decision.execution_review_required)):
                report['usage'].setdefault('codex',None)
            append({'kind':'command_protected_effect','effect_id':action.effect_id,
                    'action_sha256':action.action_sha256,'process':process,'protection':report})
            return {**process,'command_protection':report}
        if (jev or automatic) and gate is not None:
            native_gate=gate
            class ConditionalExecutionReview:
                def run(self,action,authorization,live,effect,*,public_context,before_effect):
                    decision=observed.get(action.effect_id)
                    if decision is None or not decision.allowed:
                        raise ActionRefused('No matching command decision for this command.')
                    if decision.execution_review_required:
                        announce(action,'Reviewing command with Codex.')
                        return native_gate.run(action,authorization,live,effect,
                            public_context=public_context,before_effect=before_effect)
                    before_effect()
                    return effect()
            gate=ConditionalExecutionReview()
        # Reviews and the Kernel gate read time.monotonic: each command gets the session's control on it, built
        # after any wait for the user (the session's own clock may have stopped meanwhile).
        mediator=CommandActionMediator(Broker(),snapshot,lambda a,c:context,authorize,guard,
            lambda:steady(control),result_handler=result,reviewed_execution=gate)
        mediator.close=weakref.finalize(mediator,lambda:(held.close(),test_directory.cleanup()))
        cli_options=({'codex_executable':pinned_cli['executable'],'codex_sha256':options['codex_sha256']}
                     if options['backend']=='codex' else {})
        from .work_units import VERIFY_SECONDS
        from .execution_resources import selected
        windows_isolation_factory(backend=options['backend'],policy=policy,cwd=str(root),
            environment=environment,seconds=30,verification_seconds=VERIFY_SECONDS,staged_effects=staged_effects,
            unbounded_verification=selected('time'),
            mediation_factory=lambda **kw:mediator,
            **cli_options)(**session)
        if 'command' in capabilities.names:
            capabilities._specs['command']['description']+=' Python executable: '+str(Path(sys.executable).resolve())+'.'
        return mediator
    return bind


def conversation_verifier(control, custody, config, deployment, tree, unit, commands):
    """Use the existing command gate for a conversation candidate's host checks."""
    import subprocess
    from uuid import uuid4
    from compute_plane.native_capabilities import NativeCapabilities
    from compute_plane.native_models import object_json
    from tare_tools_agent_runtime import EffectRequest, ToolCall
    # The turn's own control: a command question stops its clock; the mediator steadies it for each command.
    repository = unit.get('repository') or next(iter(config['repositories']))
    session_id = 'candidate-checks-'+uuid4().hex
    child = {'repositories': {repository: str(Path(tree).resolve())},
             'writable': {repository: list(unit['target_paths'])},
             'command_protection': config['command_protection']}
    admitted=prepared_checks(commands)
    child['verifiers']={name:{'repository':repository,'argv':argv} for name,argv in admitted.items()}
    scratch = Path(custody)/'host-check-scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    (Path(custody)/'host-checks').mkdir(parents=True, exist_ok=True)
    caps = NativeCapabilities(session_id=session_id, binding=control.binding,
        repositories=child['repositories'], writable=child['writable'],
        verifiers=child['verifiers'],
        scratch_root=scratch, role='implementer',
        capabilities=['read', 'verify'], specgraph_edit_context=False)
    # A verification-only request can differ from the saved implementation goal.
    # The reviewer needs the host's exact selected checks and their execution
    # authority, not just that old goal (whose source-write scope may be empty).
    context=json.dumps({'work':config.get('command_context') or json.loads(encode_context(unit)),
        'verification':{'source':'Host-selected registered checks for the requested verification or delivery.',
            'commands':admitted,'scope':'Execute only these checks in this private candidate. '
                'Test artifacts may use the owned scratch; no project application or network is authorized.'}},ensure_ascii=False)
    port = factory(child, deployment, Path(custody)/'host-checks',
                   context, session_id,host_checks=True)(session_id=session_id, binding=control.binding,
                   control=control, config=child, custody=custody, scratch=scratch,
                   capabilities=caps)
    caps.bind_command_mediation(port)
    def run(argv, **kwargs):
        name = next((name for name, command in commands.items() if list(argv) == list(command)), None)
        if name is None:
            raise ValueError('The host check is not declared in this candidate.')
        result = caps.execute(EffectRequest(session_id, 'verify-'+uuid4().hex,
            ToolCall('verify', 'verify', json.dumps({'name': name}))), control)
        value = object_json(result.output_json)
        if result.status not in {'succeeded', 'failed'} or 'returncode' not in value:
            process = value.get('process') or {}
            stopped = next((label for key, label in (('cancelled', 'cancelled'),
                ('timeout', 'timed out'), ('output_limit', 'exceeded its output limit'))
                if process.get(key)), None)
            reason = stopped or value.get('reason') or value.get('error_type') or result.status
            raise ValueError(f'Protected verification was refused: {name}: {reason}')
        completed = subprocess.CompletedProcess(argv, value.get('returncode', 1),
                                                value.get('output', ''), '')
        completed.execution_limitation = verification_limitation(value)
        return completed
    if callable(getattr(port,'close',None)):run.close=port.close
    return run


def verification_limitation(value):
    """A check the MXC sandbox stopped (the loopback socket pair hang, or pytest tests that each failed on a
    connection or a secret file the sandbox denied) is inconclusive, never a failing-code verdict."""
    output = str(value.get('output') or '')
    backend = (value.get('process') or {}).get('confinement', {}).get('backend')
    if (value.get('returncode') and backend == 'windows-mxc'
            and all(part in output for part in ('asyncio', 'socket.py',
                'in _fallback_socketpair', 'in accept', 'Timeout'))):
        return ('ISOLATION_INTERNAL_IPC_UNAVAILABLE: the MXC check waited forever for a loopback socket pair. '
            'The sandbox denies loopback. tare gives Python an AF_UNIX socket.socketpair there, so asyncio runs, '
            'but tests that open real TCP or UDP servers on 127.0.0.1, Python started with -I, -E or -S or with '
            'a scrubbed environment or under a profile folder name too long for the socket path (its stderr says '
            'so), and libraries that build their own loopback connections still fail. '
            'This does not establish failing test assertions. Inspect the required check scope or use a '
            'compatible execution policy; do not remove required checks, change tests to hide this limitation, '
            'or silently enable host networking.')
    def denied(section):
        # The failure raised (pytest's E lines, not captured output) is a connect the sandbox refused: WSAEACCES,
        # a socket error only, or ERROR_ACCESS_DENIED completing asyncio's overlapped connect (windows_events.py,
        # finish_connect). A bare "Access is denied" can be the test's own file error, so it never counts.
        raised = '\n'.join(line for line in section.splitlines() if line.startswith('E '))
        return '[WinError 10013]' in raised or ('[WinError 5]' in raised and 'windows_events.py' in section
                                                and 'finish_connect' in section)
    def secret(line):
        # A permission error on a path whose own name the sandbox denies (`.env`, `.npmrc`...), read back from the
        # message's repr. Another exception, wording or name, or a path that does not parse, is not this.
        found = re.fullmatch(r'PermissionError: \[(?:Errno 13|WinError 5)\] (?:Permission|Access is) denied: '
                             r'''(['"].*['"])''', line)
        try:
            path = ast.literal_eval(found[1]) if found else None
        except (ValueError, SyntaxError):
            return False
        return isinstance(path, str) and sandbox_secrets.secret(ntpath.basename(path),
                                                                ntpath.basename(ntpath.dirname(path)))
    def kind(section):
        raised = [line[1:].strip() for line in section.splitlines() if line.startswith('E ') and line[1:].strip()]
        return 'network' if denied(section) else 'secret' if raised and all(map(secret, raised)) else 'failure'
    sections = [] if value.get('output_truncated') else pytest_failures(output)
    kinds = {kind(section) for section in sections}
    if not (value.get('returncode') and backend == 'windows-mxc' and sections and 'failure' not in kinds):
        return None
    if kinds == {'network'}:
        return ('ISOLATION_NETWORK_DENIED: every failing test of this check failed because the sandbox denied a '
            'network connection on this machine (loopback included), so these tests cannot be judged inside the '
            'sandbox. This does not establish failing test assertions. Do not remove required checks, change '
            'tests to hide this limitation, or silently enable host networking.')
    both = 'network' in kinds
    return ('ISOLATION_SECRET_DENIED: every failing test of this check failed because the sandbox denied it a secret '
        'file' + (' or a network connection' if both else '') + ': the sandbox does not let commands read secret '
        'files such as .env, so these tests cannot be judged inside the sandbox. This does not establish failing '
        'test assertions. Do not copy secrets into other files, rename them or change tests to hide this'
        + (', or silently enable host networking' if both else '') + '; the user decides whether such a check may '
        'run outside the sandbox.')


def pytest_failures(output):
    """The text of each failure and error section of pytest's reports; [] when one cannot be delimited."""
    sections, current = [], None
    for line in output.splitlines():
        text = line.strip()
        if re.fullmatch(r'=+ (FAILURES|ERRORS) =+', text):
            current = []  # inside a report, before its first section
        elif re.fullmatch(r'=+ .+ =+', text):
            current = None
        elif re.fullmatch(r'_{3,} \S.* _{3,}', text):
            if current is None:
                return []  # a section outside FAILURES and ERRORS: unknown, so nothing is attributed
            current = [line]
            sections.append(current)
        elif current is not None:
            current.append(line)
    return [] if current is not None else ['\n'.join(s) for s in sections]  # a report that never ended


def encode_context(unit):
    return json.dumps({k: unit[k] for k in ('title', 'criteria', 'target_paths')}, ensure_ascii=False)


def protected_work_verifier(control, train_dir, *, descriptor, settings):
    """Compose the existing command gate for host verification of a pinned candidate."""
    import subprocess
    import tempfile
    from compute_plane.command_mediation import CommandFacts, environment_digest
    from compute_plane.managed_process import run_process
    from compute_plane.native_effect_recovery import EffectReceipts
    from compute_plane.native_journal import regular, save_json
    from tare_tools_agent_runtime import EffectRequest, ToolCall
    from .native_work_context import encode, work_packet

    policy = saved(settings['command_protection'])
    deployment = settings['command_protection_deployment']
    if policy != settings['command_protection'] or prepare(policy, deployment) != policy:
        raise ValueError('NATIVE_WORK_COMMAND_PROTECTION_IDENTITY_CHANGED')
    session_id = settings['session_id']
    scratch = regular(Path(settings['custody']) / 'scratch')
    scratch.mkdir(exist_ok=True)
    from .flow_activity import observer
    on_activity, activity_source = observer(settings['custody'], 'Candidate verification', kind='attempt')
    review_activity = on_activity
    if on_activity is not None and activity_source is not None:
        def review_activity(event):
            on_activity({'channel': 'activity',
                         'activity': {**event, 'source': activity_source}})

    def run(argv, *, cwd, timeout, env, **ignored):
        if control.should_stop():
            raise ValueError('NATIVE_CANDIDATE_CONTROL_STOPPED')
        argv = tuple(argv)
        if argv not in {tuple(check.command) for check in descriptor.verification_checks}:
            raise ValueError('NATIVE_CANDIDATE_VERIFIER_NOT_DECLARED')
        root = Path(cwd).resolve(strict=True)
        repository = descriptor.execution_repository
        config = {'repositories': {repository: str(root)},
                  'writable': {repository: list(descriptor.tracked_paths)},
                  'command_protection': policy}
        packet, _ = work_packet(descriptor, settings['node'], config['repositories'])
        context = encode(packet)
        if len(context.encode()) > 8192:
            raise ValueError('NATIVE_WORK_COMMAND_CONTEXT_OVER_BUDGET')
        effect_id = 'host-verify-' + digest({'cwd': str(root), 'argv': argv,
            'descriptor': descriptor.sha256, 'session': session_id})
        request = EffectRequest(session_id, effect_id,
            ToolCall(effect_id, 'verify', encode({'argv': argv, 'cwd': str(root)})))
        receipts = EffectReceipts(scratch, session_id, config)

        class HostCapabilities:
            names = ()
            terminal = None
            launcher = None

            def bind_command_confinement(self, launcher, environment, seconds, *, verification_seconds=None,
                                         staged_effects=False,unbounded_verification=False):
                from compute_plane.mediated_tools import validate_command_confinement
                if self.launcher is not None:
                    raise ValueError('command_confinement_binding_invalid')
                if type(unbounded_verification) is not bool:
                    raise ValueError('unbounded_verification_must_be_bool')
                self.environment, self.seconds = validate_command_confinement(launcher, environment, verification_seconds or seconds)
                if unbounded_verification:
                    self.seconds=float('inf')
                self.launcher = launcher

            def claim_action(self, action, authorization):
                if control.should_stop():
                    raise ValueError('NATIVE_CANDIDATE_CONTROL_STOPPED')
                return receipts.claim_action(request, action, authorization)

        capabilities = HostCapabilities()
        mediator = factory(config, deployment, scratch, context, session_id, review_activity)(
            session_id=session_id, binding=control.binding, control=control,
            config=config, custody=scratch, scratch=scratch, capabilities=capabilities)
        if capabilities.launcher is None or not callable(getattr(mediator, 'execute', None)):
            raise ValueError('NATIVE_WORK_COMMAND_MEDIATION_UNAVAILABLE')
        wall = min(capabilities.seconds, control.remaining_seconds(),
                   float('inf') if timeout is None else timeout)
        facts = CommandFacts('command', session_id, repository, str(root),
            environment_digest(capabilities.environment), digest(config['writable']), argv,
            encode({'argv': argv, 'timeout': None if wall==float('inf') else wall,
                    'output_bytes': 1024*1024, 'read_only': False}),
            session_id=session_id, effect_id=effect_id, work_id=control.binding.work_id,
            binding_id=control.binding.binding_id, fence_seq=control.binding.fence)
        with tempfile.TemporaryDirectory(prefix='host-verify-', dir=scratch) as dest:
            def effect():
                if control.should_stop():
                    raise ValueError('NATIVE_CANDIDATE_CONTROL_STOPPED')
                return run_process(list(argv), cwd=str(root), dest=dest,
                    timeout=min(wall, control.remaining_seconds()), env=capabilities.environment,
                    cancelled=control.should_stop, max_output=1024*1024,
                    process_factory=capabilities.launcher)
            try:
                process = mediator.execute(facts, effect)
            finally:
                if callable(getattr(mediator,'close',None)):mediator.close()
            save_json(regular(Path(train_dir) / 'NATIVE_VERIFIER_PROCESS.json'), process)
            if (not process['cleanup_verified'] or process['cancelled'] or process['timeout']
                    or process['output_limit'] or process.get('isolation_violation')
                    or process['exit_code'] is None):
                raise ValueError('NATIVE_CANDIDATE_VERIFIER_INTERRUPTED')
            out = (Path(dest) / 'stdout.txt').read_bytes()
            err = (Path(dest) / 'stderr.txt').read_bytes()
            if len(out) + len(err) > 1024*1024:
                raise ValueError('NATIVE_CANDIDATE_VERIFIER_OUTPUT_LIMIT')
            return subprocess.CompletedProcess(argv, process['exit_code'], out, err)
    return run

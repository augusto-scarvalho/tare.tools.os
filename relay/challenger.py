"""Project-scoped, bounded challengers over native sessions and their receipts."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
from compute_plane.managed_process import host_executable
import sys
import time
from urllib.parse import urlparse

from compute_plane.native_journal import custody_lock, regular, save_json
from .native_agent import read_json

DEFAULT = dict(enabled=False, models=['local-qwen'], reviewer='', local_percent=20, external_percent=0,
               daily_usd='0', trial_usd='0.25', daily_local_seconds=1800, daily_quota_calls=0,
               allow_unknown_quota=False, quota_floor=25, task_type='implementation', seconds=180, calls=8,
               storage_mib=512)
ACTIVE = {'PREPARING', 'RUNNING', 'WAITING_REFERENCE', 'REVIEWING'}


def git(root, *args, timeout=30):
    result = subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false', '-C', str(root), *args], capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError(result.stderr.decode('utf-8', errors='replace')[:800])
    return result.stdout


def directory(root):
    from .native_installation import storage_root
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()).resolve()
    key = hashlib.sha256(os.path.normcase(str(common)).encode()).hexdigest()[:24]
    # Validate links before canonicalizing. Windows service accounts often use
    # an 8.3 TEMP alias; persisted jobs and resolved recovery paths must agree.
    return regular(storage_root()/'challengers'/key).resolve()


def policy(root):
    path = directory(root)/'policy.json'
    return read_json(path) if path.exists() else deepcopy(DEFAULT)


def local(deployment):
    """Served from this machine or its network, decided without a DNS query: a private, loopback or Tailscale
    address, a name without a dot or under a local suffix, or the configured local gateway's own host.
    native_challenger_accounting, when present, decides instead."""
    if deployment.get('native_challenger_accounting'):
        return deployment['native_challenger_accounting'] == 'local'
    url = urlparse(deployment['native_profile']['endpoint'])
    host = (url.hostname or '').rstrip('.')
    try:
        address = ipaddress.ip_address(host)
        private = address.is_private or address in ipaddress.ip_network('100.64.0.0/10')
    except ValueError:
        from .web_research import LOCAL_NAMES
        private = bool(host) and ('.' not in host or host.endswith(LOCAL_NAMES))
        if host and not private:
            from .native_catalog import local_gateway
            try:  # the operator's gateway, however it is named (http://models.example.com:8080)
                private = host == urlparse(local_gateway()).hostname.rstrip('.')
            except ValueError:  # no storage or a malformed gateway: the name rules alone decide
                pass
    return url.scheme in {'http', 'https'} and private


def client_headers(deployment):
    """Who calls a local route, for the gateway's log and text queue: the frontend that set TARE_CLIENT (the TUI,
    tare serve; their sessions inherit it), else tare.tools. A process running under a GPU reservation (`tare gpu
    run`, TARE_GPU_RESERVATION) also shows its secret, so the node lets it use the reserved GPU (contract
    gpu-lease/2). Other routes get nothing."""
    if not local(deployment):
        return {}
    headers = {'User-Agent': os.environ.get('TARE_CLIENT', 'tare.tools')[:120]}
    if os.environ.get('TARE_GPU_RESERVATION'):
        from compute_plane.gpu_lease_client import RESERVATION_HEADER
        headers[RESERVATION_HEADER] = os.environ['TARE_GPU_RESERVATION']
    return headers


def bounded(deployment, config, amount):
    from .challenger_variants import accounting
    result = deepcopy(deployment)
    result['native_execution_limits'] = dict(max_model_calls=config['calls'], max_seconds=config['seconds'],
        request_seconds=config['seconds'], connect_seconds=min(15, config['seconds']), read_seconds=config['seconds'],
        max_model_retries=0, max_stagnant_turns=3)
    if accounting(result) == 'usd':
        if amount <= 0:
            raise ValueError('Commercial challengers need a positive daily budget.')
        existing = result.get('native_spend_policy', {})
        ceiling = min(amount, Decimal(existing.get('max_amount', str(amount))))
        result['native_spend_policy'] = {**existing, 'max_amount': str(ceiling), 'currency': 'USD',
            'allow_estimated_counts': existing.get('allow_estimated_counts', True)}
        # The worker resolves a provider price snapshot before dispatch. Network
        # catalog lookup must not delay the primary task's first model call.
    return result


def configure(root, value):
    from .challenger_variants import realize
    from .delivery_comparison import money
    if set(value) != set(DEFAULT) or type(value['enabled']) is not bool:
        raise ValueError('Invalid challenger settings.')
    if not isinstance(value['models'], list) or not 1 <= len(value['models']) <= 8:
        raise ValueError('Choose one to eight explicit experimental variants.')
    for key, minimum, maximum in [('local_percent', 0, 100), ('external_percent', 0, 100),
            ('seconds', 1, 3600), ('calls', 1, 64), ('daily_local_seconds', 0, 86400),
            ('daily_quota_calls', 0, 1000), ('quota_floor', 0, 100), ('storage_mib', 1, 32768)]:
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError(f'{key} must be between {minimum} and {maximum}.')
    if type(value['allow_unknown_quota']) is not bool or not isinstance(value['task_type'], str) or not value['task_type'].strip():
        raise ValueError('Choose a task type and an explicit unknown-quota policy.')
    daily, trial = money(value['daily_usd']), money(value['trial_usd'])
    if trial <= 0:
        raise ValueError('The per-trial ceiling must be positive.')
    if value['enabled']:
        variants = [realize(v) for v in value['models']]
        if len({v['key'] for v in variants}) != len(variants):
            raise ValueError('Duplicate experimental configuration.')
        if value['reviewer']:
            realize(value['reviewer'])
    folder = directory(root); folder.mkdir(parents=True, exist_ok=True)
    with custody_lock(folder):
        save_json(folder/'policy.json', value)
    return value


def read_run(path):
    """An atomic replacement can briefly deny a Windows reader; never omit a ledger row."""
    for attempt in range(3):
        try:
            return read_json(path, 1024*1024)
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(.05)


def records(folder):
    rows = [read_run(p) for p in sorted((folder/'runs').glob('*/run.json'))]
    import psutil
    for row in rows:
        if row['status'] in ACTIVE and not alive(row) and ('pid' in row or not psutil.pid_exists(row.get('primary_pid', -1))):
            row.update(status='INTERRUPTED', reason='Worker stopped; receipts and reservation retained. No automatic replay.')
    return sorted(rows, key=lambda r: (r.get('started_at', 0), r['id']))


def alive(row):
    import psutil
    try:
        return abs(psutil.Process(row['pid']).create_time()-row['pid_created']) < .1
    except (KeyError, psutil.Error):
        return False


def inspect(root):
    folder = directory(root)
    rows = records(folder)
    for row in rows:
        if row.get('managed_train'):
            try:
                from .execution_usage_sources import execution_usage
                train = Path(row['managed_train'])
                candidates = [train, *sorted((train.parent.parent/'history').glob(train.name+'_*'))]
                selected = next((p for p in candidates if (p/'WORK_RUN.json').exists() and
                    read_json(p/'WORK_RUN.json', 4*1024*1024).get('implementation_custody') == row['source']), train)
                row['managed_usage'] = execution_usage(train=selected)
            except (ValueError, OSError, KeyError) as exc:
                row['managed_usage'] = {'status': 'UNAVAILABLE', 'reason': str(exc)[:200]}
    today = datetime.now(timezone.utc).date().isoformat()
    all_rows = [r for project in folder.parent.iterdir() if project.is_dir() for r in records(project)] if folder.parent.exists() else []
    committed = sum((Decimal(r.get('committed_usd', r['reserved_usd'])) for r in all_rows if r['day'] == today), Decimal(0))
    return {'policy': policy(root), 'committed_usd': str(committed), 'runs': rows,
            'scope': 'Across projects, UTC day. Hardware and subscription allocation remain unpriced.'}


def study(job):
    """Refresh a study from the complete paired history, including failures."""
    from compute_plane.choice_assessment import digest
    from .native_profiles import directory as profile_directory
    job = Path(job)
    current = read_run(job/'run.json')
    if not current['reviewer']:
        return None
    def cohort(row):
        from .delivery_comparison import strategy_profile
        parts = [strategy_profile(row['baseline']), row.get('variant'), row.get('review_variant'),
                       row['settings']['task_type'], row['config']['verifiers'],
                       row['settings']['calls'], row['settings']['seconds'], row['reserved_usd']]
        if row.get('execution_contract') is not None:
            parts.append({'execution_contract': row['execution_contract']})
        if row.get('reference_reuse'):
            parts.append({'descriptive_retry': row['id']})  # Reuse cannot manufacture independent deliveries.
        return digest(parts)
    key = cohort(current)
    rows = [r for r in records(job.parent.parent) if cohort(r) == key and r['status'] not in ACTIVE][-100:]
    serving = {p['fingerprint'] for r in rows for p in r.get('observed_serving_configurations', [])}
    def profile(value):
        name = 'challenger-'+digest(value)[:24]
        folder = profile_directory(); folder.mkdir(parents=True, exist_ok=True)
        path = folder/(name+'.json')
        if path.exists():
            if read_json(path) != value:
                raise ValueError('Frozen experiment profile changed.')
        else:
            save_json(path, value)
        return {'deployment': name}
    value = {'schema': 'tare.tools/delivery-study/1', 'cohort': 'challenger-'+key[:16],
             'baseline': 'reference', 'cases': {}, 'trials': [], 'candidates': [
                 {'id': variant, 'implementer': profile(current[field]), 'auditor': profile(current['reviewer'])}
                 for variant, field in [('reference', 'baseline'), ('challenger', 'challenger')]]}
    for row in rows:
        folder = Path(row['path'])
        case = {'instruction': row['instruction'], 'baseline': row.get('base', 'snapshot-unavailable'),
                'checks': {k: v['argv'] for k, v in row['config']['verifiers'].items()}}
        value['cases'][row['id']] = case
        for variant in ('reference', 'challenger'):
            implementation = Path(row['source']) if variant == 'reference' else folder/'implementation'
            sessions = [{'stage': 'implement', 'path': str(implementation)}]
            review = folder/(variant+'-audit')
            if (review/'native-agent.json').exists():
                sessions.append({'stage': 'audit', 'path': str(review)})
            verdict = row.get(variant+'_verdict', 'unknown')
            original = source_state(Path(row['source']))
            reference = read_json(folder/'reference.json', 1024*1024) if (folder/'reference.json').exists() else {}
            # Subsequent conversation work has a broader contract than this paired trial.
            if (len(original['invocations']) != 1 or original.get('continued_to') or len(serving) > 1
                    or reference.get('outcome', {}).get('last_message_id') or reference.get('outcome', {}).get('source_changes')):
                verdict = 'unknown'
            value['trials'].append({'candidate': variant, 'case': row['id'], 'contract_sha256': digest(case),
                'sessions': sessions, 'verdict': verdict, 'verdict_source': str(folder/'run.json'),
                **({'additional_cost_usd': '0'} if not row.get('managed_train') and not local(current['reviewer']) and not local(
                    current['baseline'] if variant == 'reference' else current['challenger']) else {})})
    destination = job.parent.parent/('study-'+key[:16]+'.json')
    # Bound the active measurement window, keeping every attempt within it and
    # retaining older receipts and frozen preferences outside the live study.
    while len(json.dumps(value, ensure_ascii=False, indent=2).encode()) > 250*1024 and len(value['cases']) > 2:
        oldest = next(iter(value['cases']))
        del value['cases'][oldest]
        value['trials'] = [t for t in value['trials'] if t['case'] != oldest]
    save_json(destination, value, max_bytes=256*1024)
    return str(destination)


def spawn(job):
    import psutil
    log = (job/'worker.log').open('ab')
    try:
        env = dict(os.environ)
        owner = str(Path(__file__).resolve().parents[1])
        env['PYTHONPATH'] = os.pathsep.join([owner, *([env['PYTHONPATH']] if env.get('PYTHONPATH') else [])])
        process = subprocess.Popen([sys.executable, '-P', '-m', 'relay.challenger_worker', str(job)],  # -P: cwd is not tare
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return process.pid, psutil.Process(process.pid).create_time()
    finally:
        log.close()


def require_worktree_compatible(config):
    """Reject only the managed X5 argv, without running or resolving commands."""
    def absolute(value):
        return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()

    for check in config.get('verifiers', {}).values():
        argv = check.get('argv')
        if (isinstance(argv, (list, tuple)) and len(argv) == 8
                and all(isinstance(token, str) for token in argv)
                and list(argv[1:5]) == ['-m', 'relay.test_selection_pilot', 'run', '--store']
                and argv[6] == '--pilot-id' and re.fullmatch('[0-9a-f]{32}', argv[7])
                and absolute(argv[0]) and absolute(argv[5])):
            raise ValueError('Managed X5 wrapper is incompatible with challenger worktrees: '
                             'the pilot is bound to the original project root; comparison skipped.')


def start(custody, state, *, retry=None):
    """Called under the reference workspace lock, before its first model call."""
    config = retry['config'] if retry else state['config']
    if config['role'] != 'implementer' or len(config['repositories']) != 1:
        return
    if (custody/'challenger-child.json').exists():
        return
    root = Path(next(iter(config['repositories'].values())))
    try:
        settings = policy(root)
    except ValueError:
        return  # Ordinary folders do not need Git or challenger configuration.
    if not settings['enabled']:
        return
    marker = custody/'challenger.json' if retry is None else custody/'challenger-retries'/(retry['id']+'.json')
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not config['verifiers']:
            raise ValueError('No named checks: conversation recorded without a challenger.')
        require_worktree_compatible(config)
        if state.get('attachments'):
            raise ValueError('Attachment tasks require an equivalent challenger input; skipped.')
        from .challenger_variants import realize, accounting
        from .challenger_workspace import snapshot
        folder = directory(root); folder.mkdir(parents=True, exist_ok=True)
        # One ledger across projects prevents multiplying GPU/quota/budget allowances.
        with custody_lock(folder.parent):
            variants = [realize(v) for v in settings['models']]
            variants = [v for v in variants if settings['task_type'] in v['task_types']]
            def arm(deployment):
                return deployment['native_profile'], deployment.get('native_context_projection', {})
            variants = [v for v in variants if arm(v['deployment']) != arm(state['deployment'])]
            from .turn_engine import avoid_local_gpu
            if avoid_local_gpu():  # the operator left the shared local GPU to others
                variants = [v for v in variants if v['accounting'] != 'local']
            from .environment import allows
            variants = [v for v in variants if allows(v['deployment'])[0]]  # nor one this machine's setup declines
            variants = [v for v in variants if int(hashlib.sha256(
                (state['session_id']+('local' if v['accounting'] == 'local' else 'external')).encode()).hexdigest()[:8], 16) % 100 <
                settings['local_percent' if v['accounting'] == 'local' else 'external_percent']]
            if not variants:
                raise ValueError('Outside the configured task/sample; no extra model calls.')
            # A sampled variant that changes only the context policy of the primary's own model
            # isolates that policy; prefer it over pairs that would also change the model.
            same_model = [v for v in variants if v['deployment']['native_profile'] == state['deployment']['native_profile']]
            variants = same_model or variants
            rows = [r for project in folder.parent.iterdir() if project.is_dir() for r in records(project)]
            if any(r['status'] in ACTIVE and alive(r) for r in rows):
                raise ValueError('A challenger is already active; normal work continues.')
            cooling = {r['variant']['resource_pool'] for r in rows if r.get('quota_cooldown_until', 0) > time.time()}
            cooling.update(r['review_variant']['resource_pool'] for r in rows if r.get('review_quota_cooldown_until', 0) > time.time())
            variants = [v for v in variants if v['resource_pool'] not in cooling]
            if not variants:
                raise ValueError('Experimental quota pool is cooling down after a rate limit; normal work continues.')
            variant = min(variants, key=lambda v: sum(r.get('variant', {}).get('key') == v['key'] for r in rows))
            candidate = variant['deployment']; name = variant['label']
            reused = retry.get('implementation') if retry else None
            if reused and arm(candidate) != arm(reused['deployment']):
                raise ValueError('The completed challenger has another model configuration; its implementation cannot be relabelled.')
            review_variant = realize(settings['reviewer']) if settings['reviewer'] else None
            if review_variant and review_variant['resource_pool'] in cooling:
                raise ValueError('Reviewer quota pool is cooling down; no challenger dispatched.')
            if review_variant and not allows(review_variant['deployment'])[0]:
                raise ValueError('The challenger reviewer is not used on this machine (tare setup); no challenger dispatched.')
            reviewer = review_variant['deployment'] if review_variant else None
            paid = (not reused and accounting(candidate) == 'usd') or reviewer is not None and accounting(reviewer) == 'usd'
            reserved = Decimal(settings['trial_usd']) if paid else Decimal(0)
            today = datetime.now(timezone.utc).date().isoformat()
            committed = sum((Decimal(r.get('committed_usd', r['reserved_usd']))
                             for r in rows if r['day'] == today), Decimal(0))
            if paid and committed+reserved > Decimal(settings['daily_usd']):
                raise ValueError('Daily experimental budget reached; the main task continues.')
            units = ([] if reused else [variant]) + ([review_variant, review_variant] if review_variant else [])
            local_seconds = sum(v['accounting'] == 'local' for v in units)*settings['seconds']
            quota_calls = sum(v['accounting'] == 'quota' for v in units)*settings['calls']
            today_rows = [r for r in rows if r['day'] == today]
            if sum(r.get('local_seconds_reserved', 0) for r in today_rows)+local_seconds > settings['daily_local_seconds']:
                raise ValueError('Daily local compute allowance reached.')
            if sum(r.get('quota_calls_reserved', 0) for r in today_rows)+quota_calls > settings['daily_quota_calls']:
                raise ValueError('Daily experimental quota-call allowance reached.')
            used = sum(p.stat().st_size for p in folder.parent.rglob('*') if p.is_file() and not p.is_symlink())
            tracked = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
            required = 0 if reused else 2*sum((root/p).stat().st_size for p in set(tracked)-{''} if (root/p).is_file())
            if used+required > settings['storage_mib']*1024*1024:
                raise ValueError('Experiment storage allowance reached; retained evidence was preserved.')
            candidate = bounded(candidate, settings, reserved/(2 if reviewer else 1))
            reviewer = bounded(reviewer, settings, reserved/4) if reviewer else None
            identifier = retry['id'] if retry else state['session_id']
            job = folder/'runs'/identifier; job.mkdir(parents=True)
            row = dict(id=identifier, source=str(custody), project=str(root),
                repository=next(iter(config['repositories'])), config=config, settings=settings,
                instruction=state['instruction'], baseline=state['deployment'], challenger=candidate,
                name=name, reviewer=reviewer, day=today, reserved_usd=str(reserved),
                variant=variant, review_variant=review_variant, local_seconds_reserved=local_seconds,
                quota_calls_reserved=quota_calls,
                status='PREPARING', path=str(job), primary_pid=os.getpid(), started_at=time.time())
            row['execution_contract'] = {'delegated_leaf': bool(state.get('delegated_leaf')), 'variant_inbox': 1,
                                         'workspace_layout': 1, 'request_seconds': settings['seconds']}
            if state.get('managed_train'):
                row['managed_train'] = state['managed_train']
            if reused:
                row.update(base=retry['base'], workspace_reuse=reused['workspace_job'],
                           implementation_reuse={k:v for k,v in reused.items() if k != 'deployment'},
                           challenger=reused['deployment'])
            save_json(job/'run.json', row, max_bytes=1024*1024)
            from .challenger_workspace import tree
            candidate_tree = tree(job, 'candidate', row)
            if not reused:
                candidate_tree.parent.mkdir(parents=True, exist_ok=True)
                row['base'] = snapshot(root, candidate_tree)
            if retry:
                from .challenger_recovery import publish_reference
                publish_reference(job, row, retry)
            if config.get('project_graph'):
                graph = regular(config['project_graph']['path'])
                if not graph.resolve().is_relative_to(root.resolve()):
                    if graph.stat().st_size > 2*1024*1024:
                        raise ValueError('External graph exceeds the experiment snapshot limit.')
                    (job/'graph-input.json').write_bytes(graph.read_bytes())
            row['status'] = 'RUNNING'
            save_json(job/'run.json', row, max_bytes=1024*1024)
            # Worker takes the same global ledger lock before opening run.json.
            row['pid'], row['pid_created'] = spawn(job)
            save_json(job/'run.json', row, max_bytes=1024*1024)
            save_json(marker, {'status': 'RUNNING', 'job': str(job)})
    except Exception as exc:
        if 'row' in locals() and 'job' in locals() and row['status'] == 'PREPARING':
            row.update(status='FAILED', reason=str(exc)[:800], committed_usd='0')
            save_json(job/'run.json', row, max_bytes=1024*1024)
        save_json(marker, {'status': 'SKIPPED', 'reason': str(exc)[:800]})


def finish(custody, state, result):
    """Request linked stop before releasing the reference; retain existing evidence."""
    marker = custody/'challenger.json'
    if not marker.exists():
        return
    record = read_json(marker)
    if not record.get('job'):
        return
    job = regular(record['job'])
    stop_error = None
    if result.get('status') in {'CANCELLED', 'INTERRUPTED'}:
        try:
            # The worker already consumes this cooperative, job-local signal.
            # Exclusive creation preserves the first notification on repeated finish.
            with (job/'cancel').open('x'):
                pass
        except FileExistsError:
            pass
        except Exception as exc:
            stop_error = exc
    try:
        if not (job/'reference.json').exists():
            from contextlib import ExitStack
            publication = job/'reference-publication'
            publication.mkdir(exist_ok=True)
            with ExitStack() as stack:
                try:
                    # Separate from the worker's job lock and the owner's native
                    # lock: reentry may notify stop, but cannot repeat a snapshot.
                    stack.enter_context(custody_lock(publication))
                except ValueError:
                    return  # Another finish owns reference publication.
                if (job/'reference.json').exists():
                    return
                try:
                    from .challenger_workspace import snapshot
                    root = Path(next(iter(state['config']['repositories'].values())))
                    from .challenger_workspace import tree
                    reference = snapshot(root, tree(job, 'reference'))
                    save_json(job/'reference.json', {'snapshot': reference, 'outcome': result,
                        'invocation': state['invocations'][-1]['id']}, max_bytes=1024*1024)
                except Exception as exc:
                    save_json(job/'reference.json', {'error': str(exc)[:800]})
    finally:
        if stop_error is not None:
            # Native callers are best-effort: retain a diagnostic even if they
            # swallow the exception. A later finish may retry without resnapshotting.
            try:
                if not (job/'stop-request-error.json').exists():
                    save_json(job/'stop-request-error.json', {
                        'error_type': type(stop_error).__name__, 'error': str(stop_error)[:800]})
            finally:
                raise stop_error


def source_state(root):
    """Read either owner's original receipts; never fabricate a native session."""
    if (root/'native-agent.json').exists():
        return read_json(root/'native-agent.json', 1024*1024)
    inputs = read_json(root/'challenger-input.json', 1024*1024)
    host = read_json(root/'host.json', 1024*1024)
    attempt = read_json(root/'attempt.json', 4*1024*1024)
    if host['session_id'] != inputs['session_id'] or attempt['identity']['descriptor_sha256'] != inputs['descriptor_sha256']:
        raise ValueError('Managed challenger source identity changed.')
    return {**inputs, 'deployment': host['deployment'], 'invocations': attempt['attempts']}


def start_work(settings, train, descriptor, workspace, node):
    """Bridge managed Work inputs into the same optional experiment scheduler."""
    root, custody = Path(workspace), Path(settings['custody'])
    try:
        if not policy(root)['enabled']:
            return
        if settings.get('read_only_repositories') or settings.get('source_refs'):
            raise ValueError('Multi-repository/pinned-source Work requires an equivalent isolated input; skipped.')
        from .native_work_context import work_packet, encode
        repo = descriptor.execution_repository
        config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
            'repositories': {repo: str(root)}, 'writable': {repo: list(descriptor.tracked_paths)},
            'capabilities': ['read', 'list', 'search', 'context', 'edit', 'verify'],
            'specgraph_edit_context': settings.get('specgraph_edit_context', True),
            'verifiers': {check.criterion: {'repository': repo, 'argv': list(check.command)}
                          for check in descriptor.verification_checks}}
        require_worktree_compatible(config)
        packet, _ = work_packet(descriptor, node, config['repositories'])
        host = read_json(custody/'host.json', 1024*1024)
        state = {'config': config, 'session_id': host['session_id'], 'deployment': host['deployment'],
            'instruction': 'Implement this bounded Work, run its named verifiers, then propose delivery.\n'+encode(packet),
            'descriptor_sha256': descriptor.sha256, 'managed_train': str(Path(train).resolve())}
        save_json(custody/'challenger-input.json', state, max_bytes=1024*1024)
        start(custody, state)
    except Exception as exc:
        # The production Work retains its own authority and execution lifecycle.
        save_json(custody/'challenger.json', {'status': 'SKIPPED', 'reason': str(exc)[:800]})

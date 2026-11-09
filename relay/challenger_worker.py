"""One isolated challenger; optional paired review; no retry or automatic landing."""
from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
import sys
import time

from compute_plane.native_journal import custody_lock, save_json
from . import challenger
from .native_agent import read_json, run_session


def session_config(row, root, role):
    challenger.require_worktree_compatible(row['config'])
    value = deepcopy(row['config'])
    value['repositories'] = {row['repository']: str(root)}
    value['index_states'] = {}
    from .work_units import pytest_scratch
    for check in value.get('verifiers', {}).values():
        argv = check['argv']
        if argv:
            check['argv'] = pytest_scratch(argv, argv[0], root, previous_tree=row['project'])
    if value.get('project_graph'):
        source = Path(value['project_graph']['path'])
        try:
            relative = source.relative_to(Path(row['project']))
            value['project_graph']['path'] = str(root/relative)
        except ValueError:
            value['project_graph']['path'] = str(Path(row['path'])/'graph-input.json')
    value['terminal'] = False
    allowed = {'read', 'list', 'search', 'context', 'verify'} | ({'edit'} if role == 'implementer' else set())
    value['capabilities'] = [name for name in value.get('capabilities', sorted(allowed)) if name in allowed]
    if role == 'auditor' and 'verify' not in value['capabilities']:
        value['capabilities'].append('verify')
    value['typed_output'] = role == 'auditor'
    value['role'] = role
    if role == 'auditor':
        value['writable'] = {}
    return value


def checks(session, configured):
    from .native_review import review
    observed = review(session, include_diffs=False)
    latest = {row['name']: row for row in observed['checks']}
    return bool(configured) and not observed['unresolved_effects'] and all(
        name in latest and latest[name]['status'] == 'succeeded' and latest[name]['returncode'] == 0
        and latest[name]['freshness'] == 'OBSERVED_FILES_MATCH' for name in configured)


def audit(row, job, variant, cancellation):
    challenger.require_worktree_compatible(row['config'])
    from .work_audit import prepare
    producer = row['challenger'] if variant == 'challenger' else row['baseline']
    if prepare(producer, [row['reviewer']], 'required')['status'] != 'READY':
        return 'unknown', 'Independent reviewer identity is not qualified for this candidate.'
    from .challenger_workspace import tree
    root = tree(job, 'candidate' if variant == 'challenger' else 'reference', row)
    session = job/(variant+'-audit'); session.mkdir()
    save_json(session/'challenger-child.json', {'job': str(job)})
    config = session_config(row, root, 'auditor')
    instruction = ('Review this implementation against the original task below. Read the changed source, '
        'run every configured verifier, and return a typed review with observed sources. '
        'ACCEPT requires no unresolved findings and every named check passing. '
        'Original task:\n'+row['instruction'])
    outcome = execute(config, row['reviewer'], row['review_variant'], session, instruction, row, cancellation)
    if outcome['status'] != 'PROPOSED' or not (session/'artifact.json').exists():
        public = {'CLI_UNEXPECTED_TOOL', 'CLI_UNEXPECTED_INVOCATION', 'CLI_BOUNDARY_UNCONFIRMED',
                  'CLI_USAGE_LIMIT', 'PLAN_USAGE_LIMIT', 'CLI_INCOMPLETE_EXCHANGE', 'CLI_TURN_LIMIT_REACHED',
                  'CLI_GENERATION_FAILED', 'CLI_INVALID_RESPONSE', 'CLI_ACTION_LIMIT_EXCEEDED'}
        code = outcome.get('failure_code')
        return 'unknown', 'Review did not complete'+(' ('+code+')' if code in public else '')+'. Inspect its execution receipt before retrying.'
    payload = read_json(session/'artifact.json')['payload']
    if payload['verdict'] == 'ACCEPT' and payload['sources'] and checks(session, config['verifiers']):
        return 'accepted', payload['summary']
    return ('rejected' if payload['verdict'] == 'REVISE' else 'unknown'), payload['summary']


def execute(config, deployment, variant, session, instruction, row, cancellation):
    challenger.require_worktree_compatible(row['config'])
    challenger.require_worktree_compatible(config)
    from .challenger_variants import preflight, IdentityClient, price
    from .native_generation import inference_client, deployment_profile, vault_secret
    from .native_limits import resolve, http_options
    from tare_tools_agent_runtime import ModelFailure
    facts = preflight(variant, allow_unknown_quota=row['settings']['allow_unknown_quota'],
                      quota_floor=row['settings']['quota_floor'])
    save_json(session/'resource-before.json', facts, max_bytes=1024*1024)
    price(deployment)
    def credential():
        key = deployment.get('native_credential_env')
        if key is None:
            return None
        value = os.environ.get(key) or vault_secret(key)
        if not value:
            raise ModelFailure('AUTH_FAILED')
        return value
    client = inference_client(deployment, deployment_profile(deployment, deployment['native_profile']['model']),
        credential=credential, **http_options(resolve(deployment)))
    if variant['identity']:
        client = IdentityClient(client, variant['identity'])
    options = {}
    contract = row.get('execution_contract') or {}
    if contract:
        options['delegated_leaf'] = bool(contract.get('delegated_leaf'))
    if contract.get('variant_inbox') == 1:
        from tare_tools_agent_runtime import NativeMessage
        from .execution_team import actor
        import json
        facts = {'basis': 'HOST_SELECTED_CONFIGURATION',
            'reference': actor({'deployment': row['baseline'], 'config': {'role': 'implementer'}}),
            'current': actor({'deployment': deployment, 'config': {'role': config['role']}})}
        message = NativeMessage('host-comparison-routing-v1',
            'This is a host-admitted isolated comparison, with the model below explicitly selected by its policy. '
            'Executor model/route clauses in the captured task describe the ordinary reference; this admitted '
            'alternative and its paired reviewer are authorized exceptions to those routing clauses. Do not refuse '
            'or reroute merely because this model differs from the reference. Preserve every functional criterion, '
            'file scope, named check and permission. Evaluate code under this admitted variant; reference routing '
            'identities and experimental bookkeeping are host concerns, not code defects. Do not change provider '
            'settings, create nested trials or claim that unrecorded stages happened. Configuration is not provider '
            'attestation. The original captured work instruction is unchanged.\n'+json.dumps(facts,sort_keys=True))
        options['receive_message'] = lambda consumed: message if message.message_id not in consumed else None
    return run_session(config, deployment, session, instruction, cancellation=cancellation, client=client, **options)


def costs(job, row):
    from .native_profiles import usage
    known, committed, unknown = Decimal(0), Decimal(0), 0
    measured = []
    for name in ('implementation', 'challenger-audit', 'reference-audit'):
        session = job/name
        if not (session/'native-agent.json').exists():
            continue
        state = read_json(session/'native-agent.json', 1024*1024)
        if not (session/'outcome.json').exists():
            unknown += 1
            continue
        result = read_json(session/'outcome.json', 1024*1024)
        report = usage(session)
        measured.append({'stage': name, 'model_calls': report.get('model_calls'),
            'tokens': report.get('tokens'), 'provider_estimates_by_currency': report.get('provider_estimates_by_currency'),
            'seconds': sum(max(0, i['ended_at']-i['started_at']) for i in state.get('invocations', [])
                           if i.get('ended_at') is not None), 'accounting': row['variant']['accounting']
                           if name == 'implementation' else row['review_variant']['accounting']})
        known += Decimal((report.get('known_cost_by_currency') or {}).get('USD', '0'))
        from .challenger_variants import accounting
        if accounting(state['deployment']) != 'usd':
            continue  # No provider fee reserved; hardware remains unpriced.
        spend = result.get('spend')
        if spend is None or spend.get('reservation_overruns'):
            unknown += 1
        else:
            committed += Decimal(spend['committed_amount'])
            unknown += spend['uncertain_calls']
    # Unfinished dispatch never releases the enclosing reservation.
    row.update(known_usd=str(known), uncertain_calls=unknown,
               measured=measured,
               committed_usd=str(max(committed, Decimal(row['reserved_usd'])) if unknown else committed))


def run(job):
    from threading import Event, Thread
    from tare_tools_agent_runtime import CancellationToken
    from .challenger_workspace import freeze
    job = Path(job).resolve(); folder = job.parent.parent
    # The parent publishes worker identity under this lock before releasing it.
    for _ in range(100):
        try:
            with custody_lock(folder.parent):
                row = read_json(job/'run.json', 1024*1024)
            break
        except ValueError:
            time.sleep(.05)
    else:
        return
    if row['status'] != 'RUNNING':
        return
    cancellation, stop = CancellationToken(), Event()
    def cancelled():
        # Also poll at dispatch boundaries: do not wait for the monitor's next
        # tick to prevent a new implementation or paired review.
        if not cancellation.is_cancelled():
            try:
                if (job/'cancel').exists() or not read_json(folder/'policy.json')['enabled']:
                    cancellation.cancel()
            except Exception:
                cancellation.cancel()
        return cancellation.is_cancelled()
    def watch():
        while not stop.wait(1):
            if cancelled():
                return
    monitor = Thread(target=watch, daemon=True); monitor.start()
    with custody_lock(job):
        # Legacy rows have no compatibility version. Revalidate their original
        # argv before reading reused implementations or creating any session.
        try:
            challenger.require_worktree_compatible(row['config'])
        except ValueError as exc:
            stop.set()
            row.update(status='FAILED', reason=str(exc)[:800])
            # No dispatch occurred: keep historical reservations and receipts,
            # rather than recomputing costs for this rejected launch.
            save_json(job/'run.json', row, max_bytes=1024*1024)
            return
        # A repeated worker launch cannot replay a paid or mutating session.
        if (job/'implementation'/'native-agent.json').exists() or row['status'] != 'RUNNING':
            stop.set()
            return
        row.setdefault('reference_verdict', 'unknown')
        row.setdefault('challenger_verdict', 'unknown')
        try:
            if cancelled():
                raise ValueError('Experiment stop requested; implementation was not dispatched.')
            from .challenger_recovery import validate_reference
            validate_reference(row)
            session = Path(row['implementation_reuse']['source']) if row.get('implementation_reuse') else job/'implementation'
            if not row.get('implementation_reuse'):
                session.mkdir()
                save_json(session/'challenger-child.json', {'job': str(job)})
            from .challenger_workspace import tree
            config = session_config(row, tree(job, 'candidate', row), 'implementer')
            # A shared local GPU is not a parallel race. Wait for the useful task,
            # preserving the exact input snapshot, then use the owner's live admission.
            if challenger.local(row['baseline']) and row['variant']['accounting'] == 'local':
                row['status'] = 'WAITING_REFERENCE'; save_json(job/'run.json', row, max_bytes=1024*1024)
                until = time.monotonic()+3600
                while not (job/'reference.json').exists() and time.monotonic() < until and not cancelled():
                    import psutil
                    if not psutil.pid_exists(row['primary_pid']):
                        break
                    time.sleep(.5)
                if not (job/'reference.json').exists() or cancelled():
                    raise ValueError('Reference did not release local compute; experiment was not dispatched.')
            if cancelled():
                raise ValueError('Experiment stop requested; implementation was not dispatched.')
            outcome = (read_json(session/'outcome.json', 1024*1024) if row.get('implementation_reuse') else
                       execute(config, row['challenger'], row['variant'], session, row['instruction'], row, cancellation))
            row['challenger_outcome'] = outcome['status']
            row['challenger_reason'] = outcome.get('failure_code') or outcome.get('reason')
            if (outcome.get('provider_rate_limit_observed') or
                    any(code in str(row['challenger_reason']) for code in ('QUOTA', 'HTTP_429', 'RATE_LIMIT'))):
                row['quota_cooldown_until'] = time.time()+900
            row['files'] = freeze(job, row['base'])
            scope = config['writable'].get(row['repository'], [])
            if any(not any(p == '.' or name == p or name.startswith(p.rstrip('/')+'/') for p in scope) for name in row['files']):
                raise ValueError('Challenger changed files outside the original write scope.')
            row['checks_passed'] = checks(session, config['verifiers'])
            from compute_plane.native_journal import NativeObservationJournal
            effective = {}
            for record in NativeObservationJournal(session/'observations.jsonl').snapshot().records:
                observed = record.event().get('fleet_count', {}).get('profile')
                if observed:
                    effective[observed['fingerprint']] = observed
            row['observed_serving_configurations'] = list(effective.values())
            if len(effective) > 1:
                raise ValueError('Serving configuration changed during the trial; evidence retained without qualification.')
            if not cancelled() and (outcome['status'] != 'PROPOSED' or not row['checks_passed']):
                raise ValueError('Challenger did not produce a proposal with passing checks; '
                                 'results retained without spending on paired reviews.')
            row['status'] = 'WAITING_REFERENCE'
            save_json(job/'run.json', row, max_bytes=1024*1024)
            until = time.monotonic()+3600
            while not (job/'reference.json').exists() and time.monotonic() < until and not cancelled():
                import psutil
                if not psutil.pid_exists(row['primary_pid']):
                    break
                time.sleep(.5)
            row['reference_verdict'] = row['challenger_verdict'] = 'unknown'
            reference = read_json(job/'reference.json', 1024*1024) if (job/'reference.json').exists() else {}
            if row['reviewer'] and 'snapshot' in reference and not cancelled():
                validate_reference(row)
                row['status'] = 'REVIEWING'; save_json(job/'run.json', row, max_bytes=1024*1024)
                for variant in ('challenger', 'reference'):
                    if cancelled():
                        break
                    validate_reference(row)
                    try:
                        verdict, reason = audit(row, job, variant, cancellation)
                    except Exception as exc:
                        verdict, reason = 'unknown', str(exc)[:800]
                    if any(code in reason for code in ('QUOTA', 'HTTP_429', 'RATE_LIMIT')):
                        row['review_quota_cooldown_until'] = time.time()+900
                    row[variant+'_verdict'], row[variant+'_review'] = verdict, reason
                    if verdict == 'unknown' and not cancelled():
                        raise ValueError('Paired review did not complete; its receipt is retained and later review was not dispatched.')
            row['status'] = 'CANCELLED' if cancelled() else 'COMPLETED'
            if freeze(job, row['base']) != row['files']:
                raise ValueError('Candidate changed during review; prior checks cannot qualify it.')
            row['reason'] = ('Experiment cancelled; prior results and receipts are retained.' if cancellation.is_cancelled() else
                            'Paired review recorded; compare receipts before changing preferences.' if row['reviewer']
                             else 'Checks recorded. Select an independent reviewer to qualify acceptance.')
        except Exception as exc:
            row.update(status='CANCELLED' if cancelled() else 'FAILED', reason=str(exc)[:800])
            if 'QUOTA_EXHAUSTED' in str(exc):
                row['quota_cooldown_until'] = time.time()+900
        finally:
            stop.set()
            try:
                costs(job, row)
            except Exception:
                row['committed_usd'] = row['reserved_usd']
            save_json(job/'run.json', row, max_bytes=1024*1024)
            try:
                row['study'] = challenger.study(job)
            except Exception as exc:
                row['study_error'] = str(exc)[:800]
            save_json(job/'run.json', row, max_bytes=1024*1024)


if __name__ == '__main__':
    run(Path(sys.argv[1]))

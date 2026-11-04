"""Exclusive durable native attempt. Authority remains supplied by the OS host."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import time
import math

from relay.native_work_context import encode


def binding_identity(binding):
    return {k: getattr(binding, k) for k in ('work_id', 'binding_id', 'owner_id', 'fence')}


def run_native_attempt(*, resume=False, rebind=False, authority_gate=None, on_proposal=None, recover_model=False, **settings):
    """Resume under existing authority, or explicitly consume a recorded handoff.

    Caller supplies its CoordinatorAuthorityGate-backed ExecutionControl. The
    custody directory must be the same host-selected directory for this attempt.
    No identity is inferred from branch, cwd, provider name or a prior Aider run.
    """
    from compute_plane.native_journal import NativeObservationJournal, NativeJournal, custody_lock, save_json, regular
    from relay.native_work import implement_native
    if rebind and (not resume or authority_gate is None):
        raise ValueError('NATIVE_REBIND_REQUIRES_RESUME_AND_AUTHORITY')
    if recover_model and not resume:
        raise ValueError('NATIVE_MODEL_RECOVERY_REQUIRES_RESUME')
    if 'journal' in settings or 'resume' in settings:
        raise ValueError('NATIVE_ATTEMPT_OWNS_JOURNAL')
    root = regular(settings['custody'])
    workspace = Path(settings['worktree']).resolve()
    if root.resolve().is_relative_to(workspace):
        raise ValueError('NATIVE_ATTEMPT_CUSTODY_INSIDE_WORKSPACE')
    control = settings['control']
    if authority_gate is not None:
        control = replace(control, binding_is_current=lambda binding=control.binding: authority_gate.is_current(binding))
    if settings.get('command_protection') is not None:
        from .command_protection import saved, prepare
        policy = saved(settings['command_protection'])
        review_deployment = settings.get('command_protection_deployment')
        if review_deployment is None:
            raise ValueError('NATIVE_WORK_COMMAND_REVIEW_DEPLOYMENT_REQUIRED')
        if policy != settings['command_protection'] or prepare(policy, review_deployment) != policy:
            raise ValueError('NATIVE_WORK_COMMAND_PROTECTION_IDENTITY_CHANGED')
    identity = {'schema': 'tare.tools/native-attempt/1', 'session_id': settings['session_id'],
        'worktree': str(workspace), 'descriptor_sha256': settings['descriptor'].sha256,
        'binding': binding_identity(control.binding),
        'configuration_sha256': hashlib.sha256(encode({
            'profile': asdict(settings['profile']), 'node': settings['node'],
            'context_policy': asdict(settings['context_policy']), 'budget': asdict(settings['budget']),
            'source_refs': settings.get('source_refs', ()),
            **({'shared_context': settings['shared_context']} if settings.get('shared_context') is not None else {}),
            'read_only_repositories': {k: str(Path(v).resolve()) for k,v in (settings.get('read_only_repositories') or {}).items()},
            'index_state_dirs': {k: str(Path(v).resolve()) for k,v in (settings.get('index_state_dirs') or {}).items()},
            'specgraph_edit_context': settings.get('specgraph_edit_context', True),
            **({'command_protection': settings['command_protection'],
                'command_protection_deployment': settings['command_protection_deployment']}
               if settings.get('command_protection') is not None else {}),
            **({'streaming':True} if settings.get('streaming',False) else {}),
            **({'counter_protocol': settings['counter_protocol']} if settings.get('counter_protocol', 'server-template-tokenize') != 'server-template-tokenize' else {}),
            **({'spend_configuration': settings['spend_configuration']} if any(
                (settings.get('spend_configuration') or {}).get(k) is not None
                for k in ('native_spend_policy', 'native_usage_tariff')) else {}),
        }).encode()).hexdigest()}
    with custody_lock(root):
        manifest = regular(root/'attempt.json')
        if resume:
            with manifest.open('rb') as stream:
                raw = stream.read(4*1024*1024+1)
            if len(raw) > 4*1024*1024:
                raise ValueError('NATIVE_ATTEMPT_METADATA_OVER_BUDGET')
            state = json.loads(raw)
            # Initial identity is immutable. Active binding is reduced from the
            # journal; an identity difference never silently authorizes rebind.
            if ({k:v for k,v in state['identity'].items() if k != 'binding'} !=
                    {k:v for k,v in identity.items() if k != 'binding'}):
                raise ValueError('NATIVE_ATTEMPT_IDENTITY_CHANGED')
        else:
            if manifest.exists() or (root/'journal.jsonl').exists():
                raise ValueError('NATIVE_ATTEMPT_EXISTS_USE_RESUME')
            remaining = control.remaining_seconds()
            state = {'identity': identity, 'deadline_utc': None if math.isinf(remaining) else time.time()+remaining,
                     'attempts': [], 'authority': 'NONE'}
            from .delegation import snapshot as exploration_snapshot
            try:
                selected_explorer = exploration_snapshot(settings['session_id'], 'implementer',
                                                     {'native_profile': asdict(settings['profile'])})
                if selected_explorer:
                    state['delegation'] = selected_explorer
            except (ValueError, OSError, KeyError) as exc:
                state['delegation_unavailable'] = str(exc)[:500]
            # The implementer's own guidance, as any implementer session gets it, frozen with the attempt: an
            # attempt saved before has none and keeps its text.
            from .native_agent import prompt_capsule
            capsule = prompt_capsule({'native_profile': asdict(settings['profile'])}, 'implementer')
            if capsule:
                state['prompt_capsule'] = capsule
            save_json(manifest, state)
        # Resume consumes the original wall budget; it cannot grant another session budget.
        remaining = max(0, min(control.remaining_seconds(),
                              math.inf if state['deadline_utc'] is None else state['deadline_utc']-time.time()))
        control = replace(control, deadline_monotonic=control.monotonic()+remaining)
        settings = dict(settings, control=control)
        if len(state['attempts']) >= 64:
            raise ValueError('NATIVE_ATTEMPT_REOPEN_BUDGET')
        row = {'number': len(state['attempts'])+1, 'started_at': time.time(), 'status': 'PREPARING',
               'binding': binding_identity(control.binding), 'rebind_requested': rebind,
               'preparation_counters': 'UNKNOWN_UNTIL_COMPLETED'}
        state['attempts'].append(row)
        save_json(manifest, state)
        try:
            journal = NativeJournal(root/'journal.jsonl')
            if resume and not journal.snapshot().records and identity['binding'] != state['identity']['binding']:
                raise ValueError('NATIVE_ATTEMPT_IDENTITY_CHANGED')
            previous = state.get('proposal')
            if previous is not None and on_proposal is not None:
                if (identity['binding'] != previous['binding'] or
                        hashlib.sha256((root/'journal.jsonl').read_bytes()).hexdigest() != previous['journal_sha256']):
                    raise ValueError('NATIVE_ATTEMPT_IDENTITY_CHANGED')
                result = previous['result']
            else:
                scratch = regular(root/'scratch'); scratch.mkdir(exist_ok=True)
                if (root/'observations.jsonl').exists():
                    settings = dict(settings, count_observations=tuple(record.event() for record in
                        NativeObservationJournal(root/'observations.jsonl').snapshot().records))
                from .flow_activity import observer
                callback, source = observer(root, 'implementer', kind='attempt')
                presentation = {'on_activity': callback, 'activity_source': source} if callback is not None else {}
                if state.get('prompt_capsule'):
                    presentation['prompt_capsule'] = state['prompt_capsule']
                if state.get('delegation'):
                    presentation['delegation_state'] = {'delegation': state['delegation'],
                        'deadline_utc': state['deadline_utc'], 'custody': root}
                if recover_model:
                    from .native_model_recovery import inspect
                    recovery = inspect(journal, NativeObservationJournal(root/'observations.jsonl'), settings['session_id'],
                        max_model_calls=settings['budget'].max_model_calls, deadline_utc=state['deadline_utc'],
                        owner_interrupted=any(r.get('ended_at') is None for r in state['attempts'][:-1]))
                    if recovery and recovery['available']:
                        presentation['model_recovery'] = (recovery['call_id'], f"work-resume-{row['number']}", recovery['owner_interrupted'])
                result = implement_native(**dict(settings, custody=scratch), **presentation, journal=journal, resume=resume,
                                          rebind_gate=authority_gate if rebind else None)
                if result['status'] == 'PROPOSED':
                    state['proposal'] = {'result': result, 'binding': identity['binding'],
                        'journal_sha256': hashlib.sha256((root/'journal.jsonl').read_bytes()).hexdigest()}
                    save_json(manifest, state)
            row.update(status=result['status'], outcome=result, preparation_counters='OBSERVED_IN_OUTCOME')
            save_json(manifest, state)
            if result['status'] == 'PROPOSED' and on_proposal is not None:
                # OS candidate capture remains inside the same exclusive custody.
                settlement = on_proposal(result, journal, control)
                row['settlement'] = settlement
                result = result | {'settlement': settlement}
            return result | {'custody': str(root)}
        except BaseException as exc:
            row.update(status='INTERRUPTED', error_type=type(exc).__name__)
            if isinstance(exc, ValueError):
                row['reason'] = str(exc)[:256]
            raise
        finally:
            row['ended_at'] = time.time()
            row['elapsed_seconds'] = max(0, row['ended_at']-row['started_at'])
            save_json(manifest, state)

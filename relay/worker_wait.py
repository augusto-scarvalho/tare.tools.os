"""Keep one private worker while its requested folder awaits the operator's consent."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil

from compute_plane.native_journal import regular, read_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()


def contents(tree, base):
    from .work_units import changed_files
    return digest({p: hashlib.sha256(raw).hexdigest() if raw is not None else None
                   for p, raw in changed_files(tree, base).items()})


def unit_digest(unit):
    return digest({k:v for k,v in unit.items() if k not in {
        'review_feedback','verification_feedback','previous_candidate_changes','implementation_feedback'}})


def retain(tree, base, session, unit, producer, route, targets, basis, outcome):
    from .work_units import producer_reference
    return {'tree': str(tree), 'base': base, 'session': str(session), 'producer': producer_reference(session.parent,producer),
            'route': route, 'targets': targets, 'basis': basis, 'unit_sha256': unit_digest(unit),
            'contents_sha256': contents(tree, base), 'model_calls': outcome.get('model_calls', 0),
            'folder_access_request': outcome['folder_access_request']}


def restore(worker, custody, config, unit):
    """The waiting worker to resume, as (tree, session, saved state, whether its folder was granted), or None when a
    new plan replaced the one it waited under (the caller releases it and starts a fresh implementer). Until the
    user answers the folder request it is refused; once answered without a grant it resumes without the folder."""
    from .challenger import git
    from .conversation_tasks import source_state
    from .folder_access import declined, granted, same
    from .native_installation import storage_root
    tree, session = regular(Path(worker['tree'])), regular(Path(worker['session']))
    if (tree.resolve().parent != storage_root().resolve()/'wt' or not re.fullmatch('[0-9a-f]{8}', tree.name)
            or not session.resolve().is_relative_to(Path(custody).resolve()/'units')):
        raise ValueError('The waiting worker is outside this conversation\'s private custody.')
    if unit_digest(unit) != worker['unit_sha256']:
        return None
    saved = read_json(session/'native-agent.json', max_bytes=1024*1024)
    if (not tree.is_dir() or saved['config']['repositories'] != {unit['repository']: str(tree)}
            or source_state(config, worker['targets']) != worker['basis']
            or git(tree, 'rev-parse', 'HEAD').decode().strip() != worker['base']
            or contents(tree, worker['base']) != worker['contents_sha256']):
        raise ValueError('The waiting worker or project changed; inspect the retained evidence before continuing.')
    request = worker['folder_access_request']
    store = (config.get('folder_access') or {}).get('grants')
    allowed = bool(store) and any(same(p, request['path']) for p in granted(store))
    if not allowed and not declined(custody, request['path']):  # not answered yet, or never asked
        raise ValueError('The waiting worker still needs the user\'s read permission for '+request['path']
                         +'. Ask it with request_folder_access; only the user\'s reply grants or declines it.')
    if saved['config'].get('folder_access') != config.get('folder_access'):
        raise ValueError('The waiting worker belongs to another folder-grant store.')
    return tree, session, saved, allowed


def release(root, worker):
    """Remove a waiting worker that a new plan replaced: its Git worktree entry and private tree."""
    import subprocess
    from .challenger import git
    tree = Path(worker['tree'])
    try:
        git(root, 'worktree', 'remove', '--force', str(tree), timeout=120)
    except (ValueError, subprocess.SubprocessError):
        pass
    if tree.exists():
        discard(tree)


def _conversation_clock(state, follow_up, instant, message_id):
    """Recover completed idle gaps, including legacy chats; incomplete custody grants no time."""
    def number(value):
        return type(value) in (int,float) and math.isfinite(value)
    rows=state.get('invocations') or []
    deadline=state.get('deadline_utc')
    if not (follow_up and isinstance(message_id,str) and message_id and rows
            and number(instant) and number(deadline)):
        return
    clock=state.get('conversation_idle_clock')
    covered=0
    excluded=0
    if clock is not None:
        if (not isinstance(clock,dict) or type(clock.get('through_invocations')) is not int
                or not 0<=clock['through_invocations']<=len(rows)
                or not number(clock.get('excluded_seconds')) or clock['excluded_seconds']<0):return
        covered=clock['through_invocations'];excluded=clock['excluded_seconds']
        if covered==len(rows) or clock.get('message_id')==message_id:return
    for index,row in enumerate(rows):
        start,end=row.get('started_at'),row.get('ended_at')
        if (not number(start) or not number(end) or end<start
                or index and start<rows[index-1]['ended_at']):return
    if instant<rows[-1]['ended_at']:return
    waiting=instant-rows[-1]['ended_at']+sum(
        rows[i]['started_at']-rows[i-1]['ended_at'] for i in range(max(1,covered+1),len(rows)))
    state['deadline_utc']=deadline+waiting
    state['conversation_idle_clock']={'through_invocations':len(rows),
        'excluded_seconds':excluded+waiting,'message_id':message_id}


def grant_conversation_time(state, total, message_id):
    """An explicit operator cap increase; repeated delivery cannot replenish it again."""
    if (not state.get('conversation_flow') or state.get('delegated_leaf')
            or type(total) is not int or not 1<=total<=86400 or not message_id):
        raise ValueError('Active-time override requires an operator conversation follow-up and 1..86400 seconds.')
    previous=state.get('operator_active_time_budget',{}).get('total_seconds',state['limits']['max_seconds'])
    if previous is None or state.get('deadline_utc') is None:
        raise ValueError('This conversation already has unlimited time.')
    if total<previous:
        raise ValueError('The operator time cap cannot reduce or reset a saved budget.')
    state['deadline_utc']+=total-previous
    state['operator_active_time_budget']={'total_seconds':total,'message_id':message_id,'source':'operator_cli'}


def grant_conversation_calls(state,total,message_id,*,native_budget=None):
    """Raise the OS conversation pool, preserving the runtime's immutable tactical identity."""
    if (not state.get('conversation_flow') or state.get('delegated_leaf') or not message_id
            or type(total) is not int or not 1<=total<=10000):
        raise ValueError('Call-cap override requires an operator conversation follow-up and 1..10000 calls.')
    previous=state['limits']['max_model_calls']
    if previous is None or total<previous:
        raise ValueError('A call override cannot reduce or reset the saved budget.')
    if native_budget is not None and state['budget']!=native_budget:
        prior=state.get('operator_call_budget',{})
        if (prior.get('source')!='operator_cli' or prior.get('original_limit')!=native_budget.get('max_model_calls')
                or {k:v for k,v in state['budget'].items() if k!='max_model_calls'}!=
                   {k:v for k,v in native_budget.items() if k!='max_model_calls'}):
            raise ValueError('The saved tactical budget does not match its original journal.')
        state['budget']=dict(native_budget)
    if total==previous:return
    original=state.get('operator_call_budget',{}).get('original_limit',previous)
    state['limits']['max_model_calls']=total
    state['operator_call_budget']={'total_calls':total,'original_limit':original,
        'message_id':message_id,'source':'operator_cli'}


def resume_clock(root, state, follow_up, instant, *, message_id=None):
    """Exclude completed human waits; never renew calls or active-time allowance."""
    if state.get('conversation_flow') and not state.get('delegated_leaf'):
        _conversation_clock(state,follow_up,instant,message_id)
        return
    from .folder_access import granted, pending, same, settle
    request = pending(root)
    if not (follow_up and state.get('delegated_leaf') and request and state.get('invocations')):
        return
    ended = state['invocations'][-1].get('ended_at')
    store = (state['config'].get('folder_access') or {}).get('grants')
    if not ended or not store or not any(same(p, request['path']) for p in granted(store)):
        settle(root)  # resumed after a reply that granted nothing: go on without the folder, crediting no wait
        return
    waiting = max(0, instant-ended)
    if state.get('deadline_utc') is not None:
        state['deadline_utc'] += waiting
    state['folder_wait_seconds'] = state.get('folder_wait_seconds', 0)+waiting
    settle(root)  # this exact wait can be excluded only once


def discard(tree):
    """Delete only the validated private candidate, including Git's read-only object files on Windows."""
    from .native_installation import storage_root
    if tree.resolve().parent != storage_root().resolve()/'wt' or not re.fullmatch('[0-9a-f]{8}',tree.name):
        raise ValueError('Refuse to remove a directory outside the private candidate root.')
    def readonly(function,path,error):
        if not isinstance(error,PermissionError) or not Path(path).resolve().is_relative_to(tree.resolve()):
            raise error
        os.chmod(path,0o700)
        function(path)
    shutil.rmtree(tree,onexc=readonly)

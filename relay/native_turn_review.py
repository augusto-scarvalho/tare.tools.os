"""Historical per-file turn diffs and explicit, preflighted turn restoration."""
import difflib
import json
import os
from pathlib import Path

from compute_plane.mediated_tools import MediatedTools
from compute_plane.native_journal import regular, save_json
from compute_plane.native_review import capture, digest, receipt_path, restore_bytes
from .native_review import edit_snapshots
from .native_review_actions import review_custody, undo_observation, undo_path

MAX_EDITS = 256
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


def turn_receipt_path(root, invocation_id):
    return receipt_path(root/'review'/'turn-undos', invocation_id)


def _validate_batch(batch,state,turn,records,pairs,root):
    if (batch.get('schema') != 'tare.tools/turn-undo/1' or batch.get('session_id') != state['session_id']
            or batch.get('invocation_id') != turn['id'] or type(batch.get('journal_sequence')) is not int
            or not turn['journal_end'] <= batch['journal_sequence'] <= len(records)):
        raise ValueError('Turn undo receipt identity mismatch.')
    token=batch.get('plan')
    if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Turn undo receipt plan missing.')
    files=batch.get('files')
    if not isinstance(files,list) or not files:raise ValueError('Turn undo file results missing.')
    expected={r['effect_id']:json.loads(r['tool_call']['arguments_json']) for r,_ in pairs}
    seen=set()
    for row in files:
        effects=row.get('effect_ids')
        if not isinstance(effects,list) or not effects or row.get('status') not in {'PENDING','UNKNOWN','UNDONE'}:
            raise ValueError('Turn undo file result invalid.')
        for effect in effects:
            if (effect in seen or effect not in expected or expected[effect]['repository']!=row.get('repository')
                    or expected[effect]['path']!=row.get('path')):
                raise ValueError('Turn undo file membership mismatch.')
            seen.add(effect)
    if batch.get('status')=='UNDONE' and (any(r['status']!='UNDONE' for r in files) or
            any(undo_observation(root,state['session_id'],effect)!='UNDONE' for effect in expected)):
        raise ValueError('Turn undo completion is not confirmed by all edit receipts.')


def _diff(before, after, path):
    if before is None and after == b'':
        return 'Created empty file: '+path+'\n'
    if before == b'' and after is None:
        return 'Deleted empty file: '+path+'\n'
    lines = difflib.unified_diff((before or b'').decode('utf-8').splitlines(keepends=True),
                                (after or b'').decode('utf-8').splitlines(keepends=True),
                                fromfile='before/'+path, tofile='after/'+path)
    return ''.join(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n' for line in lines)


def _selected(state, records, invocation_id):
    if not isinstance(invocation_id, str) or not invocation_id:
        raise ValueError('Select a specific recorded turn.')
    turn = next((r for r in state.get('invocations', []) if r['id'] == invocation_id), None)
    if (turn is None or type(turn.get('journal_start')) is not int or type(turn.get('journal_end')) is not int
            or not 0 <= turn['journal_start'] <= turn['journal_end'] <= len(records)):
        raise ValueError('This turn has no complete recorded review boundaries.')
    pending, unresolved, pairs = {}, set(), []
    for sequence, record in enumerate(records):
        event = record.event()
        if event['kind'] == 'effect_requested':
            pending[event['effect_id']] = event
            unresolved.add(event['effect_id'])
        elif event['kind'] == 'effect_returned':
            result = event['result']
            if result['status'] != 'unknown': unresolved.discard(result['effect_id'])
            request = pending.get(result['effect_id'])
            if request is not None:
                from .native_review import confirmed_edits
                for edit_request, edit_result in confirmed_edits(request, result):
                    pending[edit_request['effect_id']] = edit_request
                    if edit_result['status'] == 'succeeded' and turn['journal_start'] <= sequence < turn['journal_end']:
                        pairs.append((edit_request, edit_result))
    return turn, pairs, pending, unresolved


def _build(root, state, config, records, project, invocation_id, max_diff_bytes):
    from .native_agent import read_json
    if type(max_diff_bytes) is not int or not 1 <= max_diff_bytes <= 512*1024:
        raise ValueError('Invalid turn preview byte limit.')
    if Path(project).resolve() not in [Path(p).resolve() for p in config['repositories'].values()]:
        raise ValueError('This conversation belongs to another project.')
    turn, pairs, pending, unresolved = _selected(state, records, invocation_id)
    if len(pairs) > MAX_EDITS:
        raise ValueError('This turn exceeds the 256-edit review limit; use individual review.')
    grouped = {}
    for request, result in pairs:
        args = json.loads(request['tool_call']['arguments_json'])
        grouped.setdefault((args['repository'], args['path']), []).append((request, result))
    files, changes, reasons = [], [], []
    if unresolved: reasons.append('Reconcile unresolved operations before undoing a turn.')
    if any(undo_observation(root,state['session_id'],effect) in {'UNKNOWN','UNAVAILABLE'} for effect in pending):
        reasons.append('A previous undo is uncertain; inspect it before further restoration.')
    snapshot_bytes, preview_bytes, targets = 0, 0, set()
    for (repository, name), entries in grouped.items():
        file_id = digest(json.dumps([repository,name],ensure_ascii=True).encode())
        row = {'file_id':file_id, 'repository':repository, 'path':name, 'effect_id':entries[-1][0]['effect_id'],
               'edit_count':len(entries), 'segments':0, 'diff':None}
        files.append(row)
        try:
            snapshots, segments, statuses = [], [], []
            for request, result in entries:
                if snapshot_bytes >= MAX_SNAPSHOT_BYTES:
                    raise ValueError('Turn snapshots exceed the 16 MiB review budget; use individual review.')
                before, after = edit_snapshots(root,request,result)
                row['deleted'] = after is None
                snapshot_bytes += sum(len(raw) for raw in (before,after) if raw is not None)
                if snapshot_bytes > MAX_SNAPSHOT_BYTES:
                    raise ValueError('Turn snapshots exceed the 16 MiB review budget; use individual review.')
                snapshots.append((before,after))
                statuses.append(undo_observation(root,state['session_id'],request['effect_id']))
                if segments and segments[-1][1] == before:
                    segments[-1] = (segments[-1][0],after)
                else:
                    segments.append((before,after))
            row['segments'] = len(segments)
            cost = sum(len(raw) for segment in segments for raw in segment if raw is not None)
            preview_bytes += cost
            if preview_bytes <= max_diff_bytes and sum((raw or b'').count(b'\n') for segment in segments for raw in segment) <= 20000:
                diffs = [_diff(before,after,name) for before,after in segments]
                row['diff'] = ('\nRecorded segment boundary: intervening changes excluded.\n'.join(diffs)
                               if len(diffs)>1 else diffs[0])
            else:
                row['diff_unavailable'] = 'Turn exceeds the interactive diff preview budget.'
            if all(status == 'UNDONE' for status in statuses):
                row['undo_status'] = 'UNDONE'
                continue
            if any(status in {'UNKNOWN','UNAVAILABLE'} for status in statuses):
                row['undo_status'] = 'UNKNOWN'
                raise ValueError('An edit in this file has an uncertain undo receipt.')
            # Only a completed suffix can be omitted without reintroducing a
            # previously undone edit when restoring the remaining prefix.
            active_count = next((i for i,status in enumerate(statuses) if status=='UNDONE'),len(statuses))
            if any(status != 'UNDONE' for status in statuses[active_count:]):
                raise ValueError('This file has nonsequential undo history; review individual edits.')
            active = snapshots[:active_count]
            if any(a[1] != b[0] for a,b in zip(active,active[1:])):
                raise ValueError('Intervening changes split this file history; review individual edits.')
            if Path(project).resolve() != Path(config['repositories'][repository]).resolve():
                raise ValueError('An edit belongs to another project.')
            capabilities = records[0].event().get('capabilities', [])
            if 'edit' not in config.get('capabilities',['edit']) or not (
                    'edit' in capabilities or state.get('conversation_flow') and 'implement' in capabilities):
                raise ValueError('This conversation does not permit file edits.')
            target = regular(MediatedTools(config).path(repository,name,write=True))
            identity = os.path.normcase(str(target.resolve()))
            if identity in targets:
                raise ValueError('Multiple recorded paths name the same file; review individual edits.')
            targets.add(identity)
            if capture(target) != active[-1][1]:
                raise ValueError('File changed since the recorded edits; newer changes were preserved.')
            changes.append({'repository':repository,'path':name,'target':target,'before':active[0][0],
                            'after':active[-1][1],'entries':entries[:active_count]})
        except (ValueError,OSError,KeyError,TypeError) as exc:
            message = str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            row['undo_status'] = 'UNAVAILABLE'
            row['undo_unavailable'] = message
            if not row['segments']: row.setdefault('diff_unavailable',message)
            reasons.append(name+': '+message)
    undo = {'status':'UNAVAILABLE','plan':None,'reason':'; '.join(reasons) or None,
            'files':len(changes),'edits':sum(len(change['entries']) for change in changes),
            'targets':[{'repository':r['repository'],'path':r['path'],'edit_count':len(r['entries'])} for r in changes]}
    if not reasons and changes:
        proof = {'session_id':state['session_id'],'invocation_id':invocation_id,'config':config,
                 'journal_head':records[-1].sha256,'turn':turn,
                 'files':[{'repository':r['repository'],'path':r['path'],'before':digest(r['before']),
                           'after':digest(r['after']),'effects':[e[0]['effect_id'] for e in r['entries']]} for r in changes]}
        undo.update(status='READY',plan=digest(json.dumps(proof,sort_keys=True,ensure_ascii=True).encode()))
    elif not reasons:
        undo.update(status='UNDONE' if files else 'UNAVAILABLE',reason=None if files else 'No confirmed edits in this turn.')
    batch_path = turn_receipt_path(root,invocation_id)
    if batch_path.exists():
        try:
            batch = read_json(batch_path,1024*1024)
            _validate_batch(batch,state,turn,records,pairs,root)
            status = batch.get('status')
            undo.update(status=status if status in {'UNDONE','PARTIAL'} else 'UNKNOWN',plan=None,
                        reason=batch.get('reason') or ('Inspect the previous turn undo result before further restoration.' if status != 'UNDONE' else None))
        except (ValueError,OSError,TypeError,AttributeError,KeyError):
            undo.update(status='UNKNOWN',plan=None,reason='Turn undo receipt unavailable; inspect it before further restoration.')
    return {'session_id':state['session_id'],'invocation_id':invocation_id,'turn_number':state['invocations'].index(turn)+1,'files':files,'undo':undo,
            'scope':'Confirmed file edits in this turn, grouped by file. Discontinuous snapshots stay separate. Shell effects are not captured.'}, changes


def review_turn(root, *, project, invocation_id, max_diff_bytes=256*1024):
    with review_custody(root) as (root,state,config,records):
        result,_ = _build(root,state,config,records,project,invocation_id,max_diff_bytes)
        return result


def undo_turn(root, *, project, invocation_id, expected_plan):
    with review_custody(root,write=True) as (root,state,config,records):
        view,changes = _build(root,state,config,records,project,invocation_id,256*1024)
        if view['undo']['status'] != 'READY' or not expected_plan or expected_plan != view['undo']['plan']:
            raise ValueError(view['undo']['reason'] or 'The turn undo preview changed; refresh and confirm the current plan.')
        receipt = {'schema':'tare.tools/turn-undo/1','session_id':state['session_id'],'invocation_id':invocation_id,
                   'plan':expected_plan,'journal_sequence':len(records),'status':'IN_PROGRESS',
                   'files':[{'repository':r['repository'],'path':r['path'],'status':'PENDING',
                             'effect_ids':[e[0]['effect_id'] for e in r['entries']]} for r in changes]}
        path = turn_receipt_path(root,invocation_id)
        regular(path.parent).mkdir(exist_ok=True)
        save_json(path,receipt)
        for change,observation in zip(changes,receipt['files']):
            intents = []
            for request,result in change['entries']:
                args = json.loads(request['tool_call']['arguments_json'])
                output = json.loads(result['output_json'])
                intents.append((undo_path(root,request['effect_id']),{
                    'schema':'tare.tools/edit-undo/1','session_id':state['session_id'],'effect_id':request['effect_id'],
                    'repository':change['repository'],'path':change['path'],'before':args['sha256'],
                    'after':output['sha256'],'status':'UNKNOWN','journal_sequence':len(records),'turn_undo':invocation_id}))
            def intent():
                observation['status'] = 'UNKNOWN'
                save_json(path,receipt)
                for destination,value in intents:
                    regular(destination.parent).mkdir(exist_ok=True)
                    save_json(destination,value)
            try:
                restore_bytes(change['target'],change['before'],change['after'],before_write=intent)
                for destination,value in intents:
                    save_json(destination,{**value,'status':'UNDONE'})
                observation['status'] = 'UNDONE'
                save_json(path,receipt)
            except (ValueError,OSError) as exc:
                receipt['status'] = ('PARTIAL' if any(r['status']=='UNDONE' for r in receipt['files']) else
                                     'UNKNOWN' if any(r['status']=='UNKNOWN' for r in receipt['files']) else 'REFUSED')
                receipt['reason'] = 'Turn restoration stopped. Inspect the recorded file results before continuing. '+str(exc)
                try: save_json(path,receipt)
                except OSError:
                    receipt.update(status='UNKNOWN',reason='Turn result could not be persisted. Files may have been restored; inspect them before continuing.')
                return receipt
        receipt['status'] = 'UNDONE'
        try: save_json(path,receipt)
        except OSError:
            receipt.update(status='UNKNOWN',reason='Files were restored, but the turn result could not be persisted. Refresh and inspect the receipts.')
        return receipt

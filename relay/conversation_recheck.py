"""Explicit host recovery of a retained candidate without coordinator inference."""
import json
from pathlib import Path

from compute_plane.native_journal import NativeJournal
from tare_tools_agent_runtime import EffectRequest, NativeOutcome, ToolCall


def recheck(tools, control, root, invocation_id, journal, *, recheck_only=True, repair_plan=False):
    from .conversation_tasks import ConversationTools, current, load
    if not isinstance(tools, ConversationTools) or not tools.state.get('conversation_flow'):
        raise ValueError('Open a saved assistant conversation to recheck its candidate.')
    task=current(load(root))
    if not task or not (task.get('delivery') or {}).get('candidate'):
        raise ValueError('No saved candidate is available to recheck. Open the conversation that produced it.')
    tools.__dict__['_host_input_id']='host-'+invocation_id
    # Only the explicit user command admits a further delivery attempt;
    # model notes and ordinary continue/recheck calls cannot grant it.
    tools.__dict__['_host_retry_delivery']=not recheck_only and not repair_plan
    calls=sum(r.event()['kind'] in {'model_requested','context_summary_requested'}
              for r in journal.snapshot().records)
    log=NativeJournal(root/'host-actions.jsonl')
    log.append({'kind':'host_recheck_requested','invocation_id':invocation_id,'task':task['id'],
                'candidate_sha256':task['delivery']['candidate'].get('sha256'),
                'recheck_only':recheck_only,'authority':'NONE'})
    operations=[('demand',{'intent':'deliver','relation':'continue'}),
                ('implement',{'recheck_candidate':True} if recheck_only else {})]
    if repair_plan:
        if (task.get('delivery') or {}).get('audit',{}).get('verdict')!='REVISE':
            raise ValueError('Plan repair requires recorded audit findings; keep the current plan for ordinary retries.')
        from compute_plane.native_journal import regular
        from .native_agent import read_json
        receipt=regular(Path(task['delivery']['receipt']))
        if not receipt.resolve().is_relative_to((root/'units').resolve()):
            raise ValueError('The recorded audit receipt is outside this conversation.')
        audit=read_json(receipt,1024*1024)['result']['audit']
        context=json.dumps({k:audit.get(k) for k in ('summary','findings','remaining')},ensure_ascii=False)
        if len(context.encode())>16384:raise ValueError('Audit feedback exceeds the bounded scope-repair context.')
        operations=[('demand',{'intent':'deliver','relation':'revise',
            'objective':task['objective'],'criteria':task['criteria']}),
            ('delegate_plan',{'instruction':'Repair only the executable scope of the retained plan after the '
                'recorded audit finding. Preserve its technical approach, every criterion and the unapplied candidate. '
                'Include every required generated output in literal target_paths and retain the repository checks. '
                'Reuse the verified previous plan and findings; do not repeat general exploration.',
                'context':context,'effort':'high'})]
    for name,args in operations:
        if control.should_stop():
            status,reason=('cancelled','CANCEL_REQUESTED') if control.cancellation.is_cancelled() else (
                ('exhausted','DEADLINE') if control.remaining_seconds()<=0 else ('rejected','BINDING_REVOKED'))
            return NativeOutcome(status,reason,calls,'Candidate recheck stopped; inspect its saved receipt.')
        result=tools.execute(EffectRequest(tools.session_id,'host-recheck-'+invocation_id+'-'+name,
            ToolCall('host-'+name,name,json.dumps(args))),control)
        log.append({'kind':'host_recheck_result','invocation_id':invocation_id,'operation':name,
                    'status':result.status,'output_json':result.output_json,'authority':'NONE'})
        if result.status!='succeeded':
            return NativeOutcome('blocked','HOST_RECHECK_REFUSED',calls,result.output_json)
    output=json.loads(result.output_json)
    if repair_plan:
        if output.get('status')!='PLANNED':
            return NativeOutcome('blocked','HOST_PLAN_NOT_REPAIRED',calls,
                'Plan repair did not produce an executable plan. The candidate is retained.\n'+result.output_json)
        return NativeOutcome('proposed','HOST_PLAN_REPAIRED',calls,
            'Saved plan scope repaired. The candidate is retained and not applied.\n'+result.output_json)
    if not output.get('applied'):
        return NativeOutcome('blocked','HOST_RECHECK_NOT_DELIVERED',calls,
            'Candidate not applied. Inspect the check or audit finding below.\n'+result.output_json)
    return NativeOutcome('proposed','HOST_RECHECK_COMPLETED',calls,
        'Saved candidate passed checks and independent audit and was applied.\n'+result.output_json)

"""Explicit coordinator realization; no role reassignment or global preferences."""
from copy import deepcopy
from pathlib import Path

APPROVED = {'claude-opus-5-5', 'gpt-6.1-sol', 'gemini-3.8-flash', 'qwen38-gsq', 'swift-next'}


def eligible(value):
    from .execution_scope import current, is_pro
    from .environment import allows
    from .native_routes import credential_available
    from .native_generation import codex_login_available
    profile=value['native_profile']
    from .model_choices import describe
    identity=describe(value.get('id',''),value)['key']
    if identity not in APPROVED or is_pro(value):
        raise ValueError('Model is not an approved coordinator.')
    allowed,reason=current().allows(value,'coordinator')
    if not allowed:raise ValueError(reason)
    allowed,reason=allows(value)
    if not allowed:raise ValueError(reason)
    if profile['protocol']=='vendor-cli':
        executable=value.get('native_cli',{}).get('executable')
        if not executable or not Path(executable).is_file():raise ValueError('CLI executable unavailable.')
    if profile['protocol']=='openai-codex-responses' and not codex_login_available(value):
        raise ValueError('Coordinator login unavailable.')
    key=value.get('native_credential_env')
    if key and not credential_available(key):raise ValueError('Coordinator credential unavailable: '+key)
    from .route_facts import collect, admission_reason
    reason=admission_reason(collect(value,live=True))
    if reason:raise ValueError('Coordinator unavailable: '+reason)
    return value


def select(raw):
    from . import native_catalog, model_choices
    from .inference_allowlist import parse, matches
    entries=parse(raw)
    if len(entries)!=1 or not entries[0][1] or len(entries[0][2])!=1:
        raise ValueError('--coordinator requires exactly one model and one reasoning level.')
    level=entries[0][2][0]
    candidates=[]; reasons=[]
    for row in native_catalog.available_models():
        name=row['deployment']
        value=native_catalog.select(name)
        if not matches(raw,name,value,effort=level):continue
        item=model_choices.describe(name,value)
        choices=[{k:item[k] for k in ('key','label','group')} | {'routes':[item['route']]}]
        try:
            chosen=model_choices.realization(choices,{'key':item['key'],'connection':name,'reasoning':level},deployment=value)
            local=chosen['native_profile']['model'] in {'qwen38-gsq','swift-next'}
            if not local and level!='high':raise ValueError('Hosted coordinators require high reasoning.')
            eligible(chosen)
        except (ValueError,OSError,KeyError,TypeError) as error:
            reasons.append(str(error));continue
        if not any(c['native_profile']==chosen['native_profile'] for c in candidates):candidates.append(chosen)
    if len(candidates)!=1:
        raise ValueError('Coordinator selection '+raw+': '+('ambiguous connection; use a deployment name' if len(candidates)>1 else '; '.join(reasons) or 'no eligible connection'))
    chosen=deepcopy(candidates[0])
    chosen['native_coordinator_pin']=raw
    return chosen

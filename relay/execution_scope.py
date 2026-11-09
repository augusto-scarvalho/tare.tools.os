"""Temporary routing restrictions inherited only by this TUI and its children."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps
import json
import os

ENV='TARE_SESSION_SCOPE'
ROLE=ContextVar('tare_execution_role',default='coordinator')
COORDINATOR=ContextVar('tare_conversation_coordinator',default='')
POLICY_VIEW=ContextVar('tare_policy_view',default=None)
KINDS=('subscription','local','api')
REVIEW_ROLES={'planner','plan-auditor','auditor'}
PIN_ROLES=('planner','implementer','explorer','researcher','auditor')
FLAGS=('subscription_only','local_only','api_only','pro_planner','prefer_api',
       'prefer_subscription','prefer_local','disable_judge')
HELP={
    'subscription_only':'Use only subscriptions: Codex, Claude and AGY CLIs and the OpenCode subscription; keep the Jev helper.',
    'local_only':'Use only local models and the local judge; keep search APIs.',
    'api_only':'Use only inference APIs; keep search APIs and Jev.',
    'pro_planner':'Use OpenAI PRO via API for the separate planner only.',
    'prefer_api':'Prefer available inference APIs for planner and auditors.',
    'prefer_subscription':'Prefer available subscriptions (CLIs, the OpenCode subscription) for planner and auditors.',
    'prefer_local':'Use local models; delegate planner and auditors to external providers.',
    'disable_judge':'Disable Jev and the local judge; use human approval unless the project is in YOLO.',
}


@dataclass(frozen=True)
class Scope:
    only:str=''
    prefer:str=''
    pro_planner:bool=False
    disable_judge:bool=False
    restrictions:tuple=()
    coordinator:str=''
    pins:tuple=()
    unlimited:tuple=()

    def pin(self,role):
        return dict(self.pins).get('auditor' if role=='plan-auditor' else role,'')

    @property
    def active(self):return any(asdict(self).values())

    def validate(self):
        from .execution_resources import parse
        object.__setattr__(self,'unlimited',parse(self.unlimited))
        if self.only not in ('',*KINDS) or self.prefer not in ('',*KINDS):
            raise ValueError('Unknown temporary execution mode.')
        if type(self.pro_planner) is not bool or type(self.disable_judge) is not bool:
            raise ValueError('Temporary execution switches must be booleans.')
        if self.only and self.prefer and (self.only!=self.prefer or self.prefer=='local'):
            raise ValueError('These execution flags conflict. Use one *_only mode, or --prefer_local for local execution with external planning/auditing.')
        if self.pro_planner and self.only in ('local','subscription'):
            raise ValueError('--pro_planner requires an inference API. Combine it with --prefer_local or --prefer_subscription instead of a conflicting *_only flag.')
        from .inference_allowlist import canonical, parse
        if not isinstance(self.restrictions,(tuple,list)):
            raise ValueError('Invalid inference restrictions.')
        normalized=[]
        for entry in self.restrictions:
            if not isinstance(entry,(tuple,list)) or len(entry)!=2 or entry[0] not in KINDS:
                raise ValueError('Invalid inference restriction.')
            normalized.append((entry[0],canonical(entry[1])))
        object.__setattr__(self,'restrictions',tuple(sorted(set(normalized))))
        if not isinstance(self.coordinator,str):raise ValueError('Invalid coordinator selection.')
        if self.coordinator:
            entries=parse(self.coordinator)
            if len(entries)!=1 or not entries[0][1] or len(entries[0][2])!=1:
                raise ValueError('--coordinator requires exactly one model and reasoning level.')
            object.__setattr__(self,'coordinator',canonical(self.coordinator))
        from .inference_allowlist import exact
        if not isinstance(self.pins,(tuple,list)):
            raise ValueError('Invalid role pins; consult tare models.')
        pins={}
        for entry in self.pins:
            if not isinstance(entry,(tuple,list)) or len(entry)!=2 or entry[0] not in PIN_ROLES:
                raise ValueError('Invalid role pin; consult tare models.')
            role,raw=entry;raw=exact(raw)
            if role in pins and pins[role]!=raw:
                raise ValueError('Conflicting --'+role+' pins; consult tare models --role '+role+'.')
            if role=='planner' and raw.rsplit('@',1)[1] not in ('high','xhigh','max'):
                raise ValueError('Planner minimum reasoning is high; consult tare models --role planner.')
            pins[role]=raw
        object.__setattr__(self,'pins',tuple(sorted(pins.items())))
        return self

    def intersect(self,other):
        if self.only and other.only and self.only!=other.only:
            raise ValueError('Inherited *_only policies conflict.')
        if self.prefer and other.prefer and self.prefer!=other.prefer:
            raise ValueError('Inherited preference policies conflict; they cannot be widened.')
        if self.coordinator and other.coordinator and self.coordinator!=other.coordinator:
            raise ValueError('The coordinator is pinned; start a new conversation to change it.')
        return Scope(self.only or other.only,other.prefer or self.prefer,
                     self.pro_planner or other.pro_planner,self.disable_judge or other.disable_judge,
                     (*self.restrictions,*other.restrictions),self.coordinator or other.coordinator,
                     (*self.pins,*other.pins),(*self.unlimited,*other.unlimited)).validate()

    def allows(self,deployment,role=None):
        if not self.active:return True,None
        if 'native_auto_routes' in deployment:return True,None
        role=role or ROLE.get()
        pin=self.pin(role)
        if pin and not pin_matches(pin,deployment):
            return False,'Excluded by --'+role+' exact pin; consult tare models --role '+role+'.'
        category=kind(deployment)
        if self.restrictions:
            from .inference_allowlist import matches
            for family,allowed in self.restrictions:
                if category==family and not matches(allowed,deployment.get('id',''),deployment):
                    return False,'Excluded by temporary '+family+' allowlist: '+allowed+'.'
        if self.only and category!=self.only:
            return False,'Excluded by --'+self.only+'_only for this TUI instance.'
        if self.prefer=='local':
            if role in REVIEW_ROLES and category=='local':
                return False,'--prefer_local requires an external planner and auditor.'
            if role not in REVIEW_ROLES and category!='local':
                return False,'--prefer_local keeps this execution role on local models.'
        if self.pro_planner:
            if role=='planner' and not is_pro(deployment):
                return False,'--pro_planner requires a qualified OpenAI PRO API planner.'
            if role!='planner' and is_pro(deployment):
                return False,'--pro_planner applies PRO only to the separate planner.'
        return True,None

    def preference(self,deployment,role=None):
        role=role or ROLE.get()
        return int(bool(role in REVIEW_ROLES and self.prefer in ('api','subscription')
                        and kind(deployment)!=self.prefer))

    def judge(self,name):
        if self.disable_judge:return False
        if name=='jev':return self.only!='local'
        if name=='local_judge':return self.only not in ('api','subscription')
        return True

    def changes(self,role):
        return bool(self.pin(role) or self.restrictions or self.only or self.prefer=='local' or
                    role in REVIEW_ROLES and (self.prefer or self.pro_planner and role=='planner'))

    def label(self):
        parts=[]
        if self.only:parts.append({'subscription':'Subscriptions only','local':'Local only','api':'API only'}[self.only])
        if self.prefer:parts.append({'subscription':'Planner/audit: prefer subscriptions','api':'Planner/audit: prefer API',
                                    'local':'Local execution; external planner/audit'}[self.prefer])
        if self.pro_planner:parts.append('Planner: OpenAI PRO')
        if self.disable_judge:parts.append('Jev/local judge: off')
        if self.coordinator:parts.append('Coordinator: '+self.coordinator)
        grouped={}  # roles sharing one pin are named once: 'explorer, implementer: provider/route@level'
        for role,pin in self.pins:grouped.setdefault(pin,[]).append(role)
        parts.extend(', '.join(roles)+': '+pin for pin,roles in grouped.items())
        for family,allowed in self.restrictions:parts.append(family+': '+allowed)
        if self.unlimited:parts.append('Unlimited: '+', '.join(self.unlimited))
        return ' · '.join(parts)


def pin_matches(raw,deployment):
    from .inference_allowlist import parse,identities
    provider,name,levels=parse(raw)[0]
    connection=(deployment.get('native_model_choice') or {}).get('connection',deployment.get('id',''))
    actual,_,route=identities(connection,deployment)
    return provider==actual and name==connection.lower() and route['default_effort']==levels[0] and levels[0] in route['efforts']


def pinned(role,producer=None):
    """Admit exactly one live route through existing availability/independence gates."""
    scope=current();raw=scope.pin(role)
    if not raw:return None
    from .model_choices import select_exact
    value=select_exact(raw,role,scope=scope)
    from .turn_engine import available
    name=value['native_model_choice']['connection']
    with for_role(role):live,reason=available(name)
    if live is None or not pin_matches(raw,live):
        raise ValueError('Pinned '+role+' unavailable: '+str(reason)+'. No fallback; consult tare models --role '+role+'.')
    if role in ('auditor','plan-auditor'):
        from .work_audit import identity
        if producer is None:raise ValueError('Pinned auditor requires the actual producer.')
        author=identity(producer,'implementer');auditor=identity(live,'auditor')
        if author.model_id==auditor.model_id or author.bias_domain==auditor.bias_domain:
            raise ValueError('Pinned auditor is not independent; consult tare models --role auditor.')
    return live


def pin_selection(role, chosen=None, effort=None, producer=None):
    """Resolve a new exact selection and reject only explicit caller overrides."""
    value=pinned(role,producer)
    if value is None:return None
    from .execution_team import reasoning
    connection=value['native_model_choice']['connection']
    if chosen not in (None,connection) or effort not in (None,'default',reasoning(value)):
        raise ValueError('Requested route or effort conflicts with --'+role+
                         '; consult tare models --role '+role+'. No fallback.')
    return value


def kind(deployment):
    profile=deployment.get('native_profile') or {}
    if profile.get('protocol')=='vendor-cli':return 'subscription'
    if profile.get('protocol')=='openai-codex-responses':return 'subscription_direct'
    # OpenCode's subscription profiles draw on a plan; its API profiles are paid per token and stay 'api'.
    if (deployment.get('native_opencode') or {}).get('plan')=='sub':return 'subscription'
    if deployment.get('native_credential_env') or str(profile.get('protocol','')).startswith('bedrock'):
        return 'api'
    from .challenger import local
    return 'local' if local(deployment) else 'api'


def is_pro(deployment):
    profile=deployment.get('native_profile') or {}
    try:parameters=json.loads(profile.get('parameters_json','{}'))
    except (ValueError,TypeError):return False
    return (kind(deployment)=='api' and deployment.get('native_credential_env')=='OPENAI_API_KEY'
            and profile.get('protocol')=='openai-responses'
            and profile.get('model') in ('gpt-6.1-sol','gpt-6-astra')
            and isinstance(parameters.get('reasoning'),dict)
            and parameters['reasoning'].get('mode')=='pro')


def current():
    if POLICY_VIEW.get() is not None:return POLICY_VIEW.get()
    raw=os.environ.get(ENV)
    try:
        value=json.loads(raw) if raw else {}
        if not isinstance(value,dict) or set(value)-set(Scope.__dataclass_fields__):raise ValueError()
        return Scope(**value).validate().intersect(Scope(coordinator=COORDINATOR.get()))
    except (TypeError,ValueError) as error:
        raise ValueError('Invalid temporary execution policy; reopen tare with the intended flags.') from error


def require(deployment,role=None):
    allowed,reason=current().allows(deployment,role)
    if not allowed:raise ValueError(reason+' Select an allowed connection; no inference was started.')


@contextmanager
def for_role(role):
    token=ROLE.set(role)
    try:yield
    finally:ROLE.reset(token)


@contextmanager
def for_coordinator(pin):
    """Bind a conversation pin to this task, never to the process environment."""
    policy = Scope(coordinator=pin).validate().intersect(current())
    token = COORDINATOR.set(policy.coordinator)
    try:yield
    finally:COORDINATOR.reset(token)


@contextmanager
def activated(scope):
    scope.validate();previous=os.environ.get(ENV)
    if scope.active:
        payload=json.dumps(asdict(scope),sort_keys=True,separators=(',',':'))
        try:inherited=json.loads(previous) if previous else None
        except (TypeError,ValueError):inherited=None
        # Preserve the parent's exact bytes when the policy is unchanged.
        if inherited!=json.loads(payload):os.environ[ENV]=payload
    else:os.environ.pop(ENV,None)
    try:yield
    finally:
        if previous is None:os.environ.pop(ENV,None)
        else:os.environ[ENV]=previous


def add_arguments(parser):
    from .execution_resources import add_argument
    add_argument(parser)
    groups={key:parser.add_mutually_exclusive_group() for key in ('only','prefer')}
    for name in FLAGS:
        target=groups['only'] if name.endswith('_only') else groups['prefer'] if name.startswith('prefer_') else parser
        aliases={'subscription_only':('--cli_only','--cli-only'),
                 'prefer_subscription':('--prefer_cli','--prefer-cli')}.get(name,())
        options=('--'+name,'--'+name.replace('_','-'),*aliases)
        if name.endswith('_only') or name.startswith('prefer_'):
            target.add_argument(*options,nargs='?',const='__legacy__',default=False,help=HELP[name])
        else:target.add_argument(*options,action='store_true',help=HELP[name])
    parser.add_argument('--coordinator',default='')
    for role in PIN_ROLES:
        parser.add_argument('--'+role,action='append',default=[],metavar='PROVIDER/DEPLOYMENT@REASONING',
                            help='Temporary exact '+role+' connection; no fallback. Consult tare models --role '+role+'.')


def from_arguments(args, *, discovery=False):
    """Build/intersect policy without activation; discovery validates the offline catalog."""
    restrictions=[]
    for family in KINDS:
        for key in (family+'_only','prefer_'+family):
            value=getattr(args,key,False)
            if value is not False and value!='__legacy__':restrictions.append((family,value))
    scope=Scope(only=next((k for k in KINDS if getattr(args,k+'_only',False)),''),
        prefer=next((k for k in KINDS if getattr(args,'prefer_'+k,False)),''),
        pro_planner=bool(args.pro_planner),disable_judge=bool(args.disable_judge),
        restrictions=tuple(restrictions),coordinator=getattr(args,'coordinator',''),
        pins=tuple((role,raw) for role in PIN_ROLES for raw in getattr(args,role,[])),
        unlimited=getattr(args,'unlimited',())).validate()
    if scope.restrictions or scope.coordinator:
        from .inference_allowlist import validate_catalog
        from .native_catalog import available_models,select,offline_deployments
        deployments=([(name,value) for name,value,_ in offline_deployments(include_unconfigured=True)]
                     if discovery else [(r['deployment'],select(r['deployment'])) for r in available_models()])
        for _,raw in scope.restrictions:validate_catalog(raw,deployments)
        if scope.coordinator:validate_catalog(scope.coordinator,deployments)
    merged=current().intersect(scope)
    if merged.pins:
        from .model_choices import select_exact
        for role,raw in merged.pins:select_exact(raw,role,scope=merged)
    return merged


def console_option(value):
    return value.split('=',1)[0]=='--unlimited' or value.split('=',1)[0] in {option for name in (*FLAGS,*PIN_ROLES,'coordinator','cli_only','prefer_cli') for option in ('--'+name,'--'+name.replace('_','-'))}


def normalize_arguments(values):
    """Fold adjacent list values without consuming a legacy positional PROJECT."""
    from .inference_allowlist import PROVIDERS
    from .execution_resources import normalize_arguments as resource_arguments
    values=resource_arguments(list(values))
    normalized=[]
    index=0
    switches=tuple('--'+r for r in PIN_ROLES)+('--coordinator','--pro_planner','--pro-planner','--disable_judge','--disable-judge')
    while index<len(values):
        value=values[index]
        index+=1
        if not console_option(value) or '=' in value or value in switches:
            normalized.append(value)
            continue
        entries=[]
        while index<len(values) and not values[index].startswith('-'):
            following=values[index]
            provider=following.lower().split('/',1)[0].split('@',1)[0].split(',',1)[0].split(';',1)[0]
            if provider not in PROVIDERS:break
            entries.append(following)
            index+=1
        normalized.append(value+'='+(';'.join(entries) if entries else '__legacy__'))
    return normalized


def session_role(function):
    @wraps(function)
    def run(config,deployment,custody,*args,**kwargs):
        saved={}
        if kwargs.get('resume'):
            from pathlib import Path
            from .native_agent import read_json
            path=Path(custody)/'native-agent.json'
            if path.is_file():saved=read_json(path,1024*1024)
        selected=config or saved.get('config') or {}
        role='coordinator' if kwargs.get('conversation') or saved.get('conversation_flow') else selected.get('role','implementer')
        with for_role(role):
            candidate=deployment or saved.get('deployment')
            # Saved policy is evidence of an earlier invocation, not authorization
            # for this instance. Only its concrete coordinator deployment persists.
            scope=current().intersect(Scope(unlimited=kwargs.get('unlimited_resources',())))
            if scope.pin(role) and not saved:
                if role in ('auditor','plan-auditor'):
                    require(candidate,role)  # Independence was checked with the producer by the routing owner.
                else:deployment=candidate=pinned(role)
            with activated(scope):
                if role=='coordinator' and scope.coordinator:
                    from .coordinator_policy import select,eligible
                    if saved:
                        from .inference_allowlist import matches
                        if not matches(scope.coordinator,candidate.get('id',''),candidate):
                            raise ValueError('Saved coordinator conflicts with the temporary selection; history was preserved.')
                        eligible(candidate)
                    else:
                        if candidate and candidate.get('native_coordinator_pin')==scope.coordinator:
                            eligible(candidate)
                        else:
                            deployment=candidate=select(scope.coordinator)
                if candidate and candidate.get('native_profile'):
                    require(candidate,role)
                    if role=='coordinator' and candidate.get('native_coordinator_pin'):
                        from .coordinator_policy import eligible
                        eligible(candidate)
                return function(config,deployment,custody,*args,**kwargs)
    return run


def constrained(deployment, role=None):
    """Realize a supported allowed effort for a new route, never a saved session."""
    scope=current()
    role=role or ROLE.get()
    raw=scope.pin(role)
    if raw:
        from .inference_allowlist import parse,identities
        provider,name,levels=parse(raw)[0]
        connection=(deployment.get('native_model_choice') or {}).get('connection',deployment.get('id',''))
        actual,_,_=identities(connection,deployment)
        if actual==provider and connection.lower()==name:
            from .model_choices import describe,realization
            item=describe(connection,deployment)
            choices=[{k:item[k] for k in ('key','label','group')} | {'routes':[item['route']]}]
            return realization(choices,{'key':item['key'],'connection':connection,'reasoning':levels[0]},deployment=deployment)
        return deployment  # The admission guard rejects any other connection.
    if not scope.restrictions or 'native_auto_routes' in deployment or 'native_host_modes' in deployment or scope.allows(deployment,role)[0]:return deployment
    from .model_choices import describe,realization
    item=describe(deployment['id'],deployment)
    route=item['route']
    choices=[{k:item[k] for k in ('key','label','group')} | {'routes':[route]}]
    levels=route['efforts']
    if (role or ROLE.get())=='planner':
        from .planner_policy import LEVELS
        levels=[level for level in levels if level in LEVELS]
    for level in levels:
        value=realization(choices,{'key':item['key'],'connection':route['id'],
                                   'reasoning':level},deployment=deployment)
        if scope.allows(value,role)[0]:return value
    return deployment  # The existing admission gate supplies the exclusion reason.


def planner_candidates(planning):
    """Resolve a transient role override; never replace the saved planner snapshot/schema."""
    scope=current()
    original=[(name,planning['routes'][name]) for name in planning.get('route_order',planning['routes'])]
    if scope.pin('planner'):
        value=pinned('planner')
        return [(value['native_model_choice']['connection'],value)]
    if not scope.changes('planner'):return original
    if scope.pro_planner:
        from .native_catalog import select
        values=[]
        for name in ('openai-api-gpt-6-1-sol-pro','openai-api-gpt-6-astra-pro'):
            try:values.append((name,select(name)))
            except (ValueError,OSError,KeyError,TypeError):continue
    else:
        from .native_routes import ranked
        values=list(original)
        candidates,selection=ranked('planner')
        for deployment in candidates:
            row=next((r for r in selection['candidates'] if r.get('eligible')
                      and r.get('selected_deployment')==deployment['id']),{})
            values.append((row.get('catalog',deployment['id']),deployment))
    values=[(n,constrained(d,'planner')) for n,d in values]
    values=[(n,d) for n,d in values if scope.allows(d,'planner')[0]]
    values.sort(key=lambda item:scope.preference(item[1],'planner'))
    return values


def execution_tiers(tiers):
    """Keep normal routing; supplement an explicitly restricted launch from admitted roles."""
    scope=current()
    if not scope.changes('implementer'):return tiers
    if scope.pin('implementer'):
        value=pinned('implementer')
        return [[value['native_model_choice']['connection']]]
    from .native_routes import ranked
    deployments,selection=ranked('implementer')
    seen={name for tier in tiers for name in tier}
    extra=[]
    for deployment in deployments:
        row=next(r for r in selection['candidates'] if r.get('eligible')
                 and r.get('selected_deployment')==deployment['id'])
        name=row.get('catalog',deployment['id'])
        if name not in seen:extra.append(name);seen.add(name)
    return [*tiers,*([[name] for name in extra])]

"""One exact host-owned action question; no inference or persistent command grants."""
import hashlib,json,os,time
from dataclasses import replace
from pathlib import Path
from compute_plane.action_control import ActionRefused
from compute_plane.native_journal import custody_lock,regular,save_json
from .worker_wait import waiting_for_user

ROOT_ENV='TARE_ACTION_APPROVAL_ROOT'
SCHEMA='tare.tools/command-permission/1'
LEASE=10  # seconds: 'expires' is renewed while the asker waits, so a question whose asker died leaves the console


def read(path):
    path=regular(Path(path))
    if path.stat().st_size>32768:raise ValueError('Command question is oversized.')
    return json.loads(path.read_text(encoding='utf-8'))


def pending(root):
    if not root:return None
    root=regular(Path(root))
    if not root.is_dir():return None
    for folder in sorted(root.iterdir()):
        path=regular(folder/'request.json')
        if not path.is_file():continue
        value=read(path)
        if (value.get('schema')==SCHEMA and value.get('id')==folder.name
                and value.get('status')=='WAITING' and value.get('expires',0)>time.time()
                and not regular(folder/'answer.json').exists()):
            return value
    return None


def answer(root,identifier,choice):
    if choice not in ('once','deny'):raise ValueError('Approve this exact command once or deny it.')
    if len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('Invalid command question.')
    folder=regular(Path(root)/identifier)
    from .conversation_permissions import decision_lock
    with decision_lock(folder):  # the asker briefly holds it to renew the lease
        value=read(folder/'request.json')
        if (value.get('schema')!=SCHEMA or value.get('id')!=identifier or value.get('status')!='WAITING'
                or value.get('expires',0)<=time.time() or regular(folder/'answer.json').exists()):
            raise ValueError('This command question expired or was already answered.')
        save_json(folder/'answer.json',{'id':identifier,'choice':choice})


def same_command(action,writable=()):
    """What one yes covers when the command comes up again in the same session: everything the action hash binds
    (session, binding, folder, argv, payload, environment, the files the argv names, context, policy, grant, sandbox)
    except what each run has of its own, its effect id and its deadline; and the bytes of the files the session may
    write, so a run after an edit to code the command imports asks again. None (ask every time) when a folder is
    writable. ponytail: a folder scope is not hashed on each question; walk it, bounded, if such plans need repeats."""
    state=[]
    try:
        for path in map(Path,writable):
            if path.exists() and not path.is_file():return None
            if not path.exists():state.append((str(path),None));continue
            with path.open('rb') as stream:state.append((str(path),hashlib.file_digest(stream,'sha256').hexdigest()))
    except OSError:return None  # unreadable now: ask
    exact=replace(action,effect_id='repeat',deadline=0.0).action_sha256
    return hashlib.sha256(json.dumps([exact,state]).encode()).hexdigest()


def approve(action,public_context,control):
    """Wait for the user's answer about this exact action. Only the answer, cancellation (or a stale binding) or this
    process ending closes the question; the action's review window does not, so the caller takes the action again
    after a yes. On the session's control (a TurnClock) the wait spends none of the turn's time."""
    location=os.environ.get(ROOT_ENV)
    if not location:raise ActionRefused('Human command approval requires the connected TUI; no command ran.')
    root=regular(Path(location))
    if not root.is_absolute() or root.resolve().is_relative_to(Path(action.cwd).resolve()):
        raise ActionRefused('Human approval custody must be outside the command workspace.')
    identifier=hashlib.sha256((action.action_sha256+'\n'+action.effect_id).encode()).hexdigest()
    folder=regular(root/identifier)
    request={'schema':SCHEMA,'kind':'command','id':identifier,'status':'WAITING',
        'objective':'Automatic judges are off or unavailable. Review this command before it runs.',
        'project':os.environ.get('TARE_ACTION_APPROVAL_PROJECT',action.cwd),'cwd':action.cwd,
        'argv':list(action.argv),'action_sha256':action.action_sha256,
        'context':public_context,'expires':time.time()+LEASE}
    if len(json.dumps(request,ensure_ascii=False).encode('utf-8'))>32768:
        raise ActionRefused('Command approval question exceeds 32 KiB; use a smaller exact command.')
    root.mkdir(parents=True,exist_ok=True)
    if folder.exists():raise ActionRefused('This exact command question is already pending; it is not replayed.')
    folder.mkdir()
    try:
        save_json(folder/'request.json',request)
        with waiting_for_user(control):  # the turn's time stops while the user reads and decides
            while not control.should_stop():
                path=regular(folder/'answer.json')
                if path.is_file():
                    from .conversation_permissions import decision_lock
                    with decision_lock(folder):reply=read(path)
                    return reply.get('id')==identifier and reply.get('choice')=='once'
                if request['expires']-time.time()<LEASE/2:
                    request['expires']=time.time()+LEASE
                    try:
                        with custody_lock(folder):save_json(folder/'request.json',request)
                    except (ValueError,OSError):pass  # an answer is being written or a reader holds the file
                time.sleep(.05)
        raise ActionRefused('Human command approval was cancelled or expired; no command ran.')
    finally:
        # Only files created by this exact question; no recursive deletion.
        for name in ('answer.json','request.json','request.json.pending','writer.lock'):
            try:regular(folder/name).unlink(missing_ok=True)
            except OSError:pass
        try:folder.rmdir();root.rmdir()
        except OSError:pass


def yolo():
    return os.environ.get('TARE_SESSION_OPERATOR_MODE')=='commands'

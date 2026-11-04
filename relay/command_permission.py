"""One exact host-owned action question; no inference or persistent command grants."""
import hashlib,json,os,time
from pathlib import Path
from compute_plane.action_control import ActionRefused
from compute_plane.native_journal import custody_lock,regular,save_json

ROOT_ENV='TARE_ACTION_APPROVAL_ROOT'
SCHEMA='tare.tools/command-permission/1'


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
    with custody_lock(folder):
        value=read(folder/'request.json')
        if (value.get('schema')!=SCHEMA or value.get('id')!=identifier or value.get('status')!='WAITING'
                or value.get('expires',0)<=time.time() or regular(folder/'answer.json').exists()):
            raise ValueError('This command question expired or was already answered.')
        save_json(folder/'answer.json',{'id':identifier,'choice':choice})


def approve(action,public_context,control):
    location=os.environ.get(ROOT_ENV)
    if not location:raise ActionRefused('Human command approval requires the connected TUI; no command ran.')
    root=regular(Path(location))
    if not root.is_absolute() or root.resolve().is_relative_to(Path(action.cwd).resolve()):
        raise ActionRefused('Human approval custody must be outside the command workspace.')
    identifier=hashlib.sha256((action.action_sha256+'\n'+action.effect_id).encode()).hexdigest()
    folder=regular(root/identifier)
    deadline=min(action.deadline,control.deadline_monotonic)
    request={'schema':SCHEMA,'kind':'command','id':identifier,'status':'WAITING',
        'objective':'Automatic judges are off or unavailable. Review this command before it runs.',
        'project':os.environ.get('TARE_ACTION_APPROVAL_PROJECT',action.cwd),'cwd':action.cwd,
        'argv':list(action.argv),'action_sha256':action.action_sha256,
        'context':public_context,'expires':time.time()+max(0,deadline-control.monotonic())}
    if len(json.dumps(request,ensure_ascii=False).encode('utf-8'))>32768:
        raise ActionRefused('Command approval question exceeds 32 KiB; use a smaller exact command.')
    root.mkdir(parents=True,exist_ok=True)
    if folder.exists():raise ActionRefused('This exact command question is already pending; it is not replayed.')
    folder.mkdir()
    try:
        save_json(folder/'request.json',request)
        while not control.should_stop() and control.monotonic()<deadline:
            path=regular(folder/'answer.json')
            if path.is_file():
                from .conversation_permissions import decision_lock
                with decision_lock(folder):reply=read(path)
                return reply.get('id')==identifier and reply.get('choice')=='once'
            time.sleep(.05)
        raise ActionRefused('Human command approval was cancelled or expired; no command ran.')
    finally:
        # Only files created by this exact question; no recursive deletion.
        for name in ('answer.json','request.json','writer.lock'):
            try:regular(folder/name).unlink(missing_ok=True)
            except OSError:pass
        try:folder.rmdir();root.rmdir()
        except OSError:pass


def yolo():
    return os.environ.get('TARE_SESSION_OPERATOR_MODE')=='commands'

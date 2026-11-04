"""A current project execution policy reaches workers without rewriting the coordinator."""
from copy import deepcopy
from relay import conversation_permissions as permissions, project_permissions, relay_mesh
from test_conversation_tasks import host, intake, planned, effect  # noqa: F401


def test_review_policy_is_carried_even_when_file_scope_was_already_granted(host,tmp_path):
    tools,control,project,_=host
    intake(host);task=planned(host)
    before=deepcopy(tools.state)
    directory=tmp_path/'preferences'
    defaults,revision=project_permissions.read_defaults(directory,project,relay_mesh.RELAY_ROOT)
    project_permissions.save_defaults(directory,project,relay_mesh.RELAY_ROOT,
        {**defaults,'mode':'commands','write_paths':['.'],'command_protection':{'review':'auto'}},revision)
    worker=permissions.authorize(tools.root,tools.state,task,'effect',control,directory)
    assert worker['command_protection']['review']=='auto'
    assert worker['writable']==before['config']['writable']
    assert tools.state==before


def test_saved_conversation_named_checks_use_the_current_project_policy(host,tmp_path,monkeypatch):
    import subprocess,sys
    from relay import command_protection
    tools,control,project,_=host
    tools.permission_directory=tmp_path/'preferences'
    defaults,revision=project_permissions.read_defaults(tools.permission_directory,project,relay_mesh.RELAY_ROOT)
    project_permissions.save_defaults(tools.permission_directory,project,relay_mesh.RELAY_ROOT,
        {**defaults,'mode':'commands','write_paths':['.'],'command_protection':{'review':'auto'}},revision)
    tools.state['config']['verifiers']={'tests':{'repository':'repo','argv':[sys.executable,'-c','print(1)']}}
    before=deepcopy(tools.state)
    selected=[]
    def verifier(control,custody,config,deployment,tree,unit,commands):
        selected.append(config['command_protection'])
        return lambda argv,**kwargs:subprocess.CompletedProcess(argv,0,'verified','')
    monkeypatch.setattr(command_protection,'conversation_verifier',verifier)
    result=tools.execute(effect('check',{'names':['tests']}),control)
    assert result.status=='succeeded' and selected[0]['review']=='auto'
    assert tools.state==before

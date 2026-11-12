"""Project defaults, CLI scope enforcement, and saved conversation identity."""
import hashlib
import json
from pathlib import Path

import pytest

from relay import native_agent
from relay.console_backend import Selection, command
from relay.project_permissions import (
    permissions_view, preference_path, read_defaults, save_defaults, validate_defaults,
)
from test_native_agent import Client, deployment, run, setup


@pytest.mark.parametrize('path', ['../x', ' ../x ', ' /tmp ', 'C:/x', 'x\\y',
    'src/../x', '.git/config', ' .GIT/config ', 'src/*.py', '', 'x\nq',
    '.git./config', 'src/.. /x', 'src /x'])
def test_invalid_edit_paths(path):
    with pytest.raises(ValueError):
        validate_defaults({'mode': 'edit', 'write_paths': [path]})


def test_preferences_scope_revision_and_corruption(tmp_path):
    project = tmp_path/'project'; project.mkdir()
    directory, control = tmp_path/'preferences', tmp_path/'control'
    defaults, revision = read_defaults(directory, project, control)
    assert defaults == {'mode': 'read', 'write_paths': [],
                        'project_admission': {'mode': 'quarantine', 'root_sha256': None, 'nested': False}}
    value, saved = save_defaults(directory, project, control,
        {'mode': 'edit', 'write_paths': [' src ', 'src', 'docs']}, revision)
    assert value['write_paths'] == ['src', 'docs'] and saved != revision
    with pytest.raises(ValueError, match='Refresh'):
        save_defaults(directory, project, control, defaults, revision)
    assert read_defaults(directory, project, control)[0] == value
    assert read_defaults(directory, project, tmp_path/'other-control')[1] == 'missing'
    assert read_defaults(directory, tmp_path/'other-project', control)[1] == 'missing'
    with pytest.raises(ValueError, match='outside'):
        read_defaults(project/'preferences', project, control)
    path, _, _ = preference_path(directory, project, control)
    path.write_text('{broken')
    with pytest.raises(ValueError):
        save_defaults(directory, project, control, defaults, saved)
    assert path.read_text() == '{broken'


def test_plan_preference_is_kept_when_another_frontend_changes_the_mode(tmp_path):
    from relay.turn_engine import set_project_permission_mode
    project = tmp_path/'project'; project.mkdir()
    directory, control = tmp_path/'preferences', tmp_path/'control'
    defaults, revision = read_defaults(directory, project, control)
    assert 'show_plan' not in defaults  # absent: show me the plan and ask
    save_defaults(directory, project, control, {**defaults, 'show_plan': False}, revision)
    set_project_permission_mode(project, 'edit')
    assert read_defaults(directory, project, control)[0]['show_plan'] is False
    with pytest.raises(ValueError, match='plan'):
        validate_defaults({'mode': 'read', 'write_paths': [], 'show_plan': 'no'})
    from relay.project_permissions import default_path, save_show_plan_default, show_plan_default
    assert show_plan_default(directory) is True  # My default, never chosen: show me the plan and ask
    save_show_plan_default(directory, False)
    assert show_plan_default(directory) is False
    default_path(directory).write_text('{"schema": "other", "show_plan": true}')
    with pytest.raises(ValueError, match='preserved'):
        show_plan_default(directory)
    assert 'other' in default_path(directory).read_text()


@pytest.mark.parametrize('read_only', [False, True])
def test_cli_scopes_are_enforced_by_real_tools_and_resume_keeps_them(setup, monkeypatch, read_only):
    _, custody, root = setup
    directory, control = root.parent/'preferences', root.parent/'control'
    clients = []
    real_run = native_agent.run_session
    def invoke(config, dep, path, prompt, **kwargs):
        plan = [('read', {'repository': 'repo', 'path': 'a.py'}),
            ('edit', {'repository': 'repo', 'path': 'a.py',
                      'sha256': hashlib.sha256(b'value = 1\n').hexdigest(),
                      'search': 'value = 1', 'replace': 'value = 2'})]
        if not read_only:
            plan += [('read', {'repository': 'repo', 'path': 'other.py'}),
                ('edit', {'repository': 'repo', 'path': 'other.py',
                    'sha256': hashlib.sha256(b'other = 1\n').hexdigest(),
                    'search': 'other = 1', 'replace': 'other = 2'})]
        plan += [('tare_deliver', {'summary': 'Permissions checked.'})]
        client = Client(steps=plan); clients.append(client)
        return real_run(config, dep, path, prompt, client=client, local_counter=client, **kwargs)
    (root/'other.py').write_bytes(b'other = 1\n')
    monkeypatch.setattr(native_agent, 'run_session', invoke)
    monkeypatch.setattr('relay.native_catalog.select', lambda *a, **k: deployment())
    args = ['--root', str(root), '--session', str(custody), '--instruction', 'Check permissions',
            '--no-context', '--role', 'explorer' if read_only else 'implementer']
    if not read_only:
        args += ['--write-path', 'a.py']
    # This test exercises adopted-root resume; CLI omission now intentionally means quarantine.
    args += ['--project-admission', 'adopted', '--root-instruction-pin',
             hashlib.sha256((root/'AGENTS.md').read_bytes()).hexdigest()]
    native_agent.main(args)
    assert clients[0].generations >= 2
    assert (root/'a.py').read_bytes() == (b'value = 1\n' if read_only else b'value = 2\n')
    assert (root/'other.py').read_bytes() == b'other = 1\n'
    state = json.loads((custody/'native-agent.json').read_text())
    assert state['config']['writable'] == ({} if read_only else {'repo': ['a.py']})
    tools = clients[0].calls[2][1]['tools']
    assert ('edit' in [t['function']['name'] for t in tools]) != read_only
    view = permissions_view(directory, root, control, session=custody)
    assert 'Match the saved snapshot.' in view['instructions_text']
    assert ('Editable paths: None' if read_only else 'Editable paths: a.py') in view['effective_text']
    save_defaults(directory, root, control, {'mode': 'commands', 'write_paths': ['.']}, 'missing')
    later = permissions_view(directory, root, control, session=custody)
    assert later['effective_text'] == view['effective_text']
    assert later['defaults']['mode'] == 'commands'
    client = Client(steps=[('tare_deliver', {'summary': 'Continue with saved scope.'})])
    real_run(None, None, custody, resume=True, client=client, local_counter=client)
    resumed = json.loads((custody/'native-agent.json').read_text())
    assert resumed['config'] == state['config']
    assert resumed['project_instructions'] == state['project_instructions']
    (root/'AGENTS.md').write_text('Changed project instructions')
    assert 'Changed since' in permissions_view(directory, root, control, session=custody)['instructions_text']
    with pytest.raises(ValueError, match='changed|CHANGED'):
        real_run(None, None, custody, resume=True, client=Client())
    with pytest.raises(ValueError, match='another project'):
        permissions_view(directory, root.parent/'another-project', control, session=custody)


@pytest.mark.parametrize('extra', [['--resume'], ['--role', 'explorer'], ['--config', 'unused.json']])
def test_cli_rejects_write_scope_on_resume_read_role_or_config(tmp_path, monkeypatch, extra):
    def forbidden(*a, **k):
        pytest.fail('Invalid startup arguments reached a model session')
    monkeypatch.setattr(native_agent, 'run_session', forbidden)
    assert native_agent.main(['--session', str(tmp_path/'session'), '--write-path', 'src', *extra]) == 2


def test_backend_argv_uses_scopes_only_for_new_implementer(tmp_path, monkeypatch):
    monkeypatch.setattr(native_agent, 'session_root', lambda: tmp_path/'sessions')
    selected = Selection(str(tmp_path), role='implementer', prompt='Task', write_paths=['src', 'docs'])
    argv = command(selected)
    assert [argv[i+1] for i, item in enumerate(argv) if item == '--write-path'] == ['src', 'docs']
    selected.role = 'explorer'
    assert '--write-path' not in command(selected)
    session = tmp_path/'saved'; session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config': {'repositories': {'repo': str(tmp_path)}}}))
    selected.session = str(session)
    argv = command(selected, True)
    assert not {'--role', '--terminal', '--write-path'} & set(argv)


def test_pending_draft_permissions_are_distinct_from_saved_defaults(tmp_path):
    project=tmp_path/'project'; project.mkdir()
    directory,control=tmp_path/'state',tmp_path/'control'
    save_defaults(directory,project,control,{'mode':'read','write_paths':[]},'missing')
    pending=Selection(str(project),role='implementer',write_paths=['src'],terminal=True,
                      verifier='["python", "-m", "pytest"]')
    view=permissions_view(directory,project,control,pending=pending)
    assert view['defaults']=={'mode':'read','write_paths':[]}
    assert 'Next message' in view['effective_text']
    assert 'Editable paths: src' in view['effective_text']
    assert 'System commands: Enabled' in view['effective_text']
    assert 'check (configured; validated at session start)' in view['effective_text']


def test_protected_preferences_roundtrip_and_legacy_terminal_is_not_enabled(tmp_path):
    from relay.project_permissions import profile_values
    project=tmp_path/'project';project.mkdir()
    directory,control=tmp_path/'preferences',tmp_path/'control'
    value={'mode':'commands','write_paths':['.'],
           'command_protection':{'review':'codex','backend':'mxc','max_reviews':2}}
    saved,revision=save_defaults(directory,project,control,value,'missing')
    assert read_defaults(directory,project,control)==(saved,revision)
    assert saved['command_protection']['max_reviews']==2
    assert profile_values(saved)==('implementer',False,['.'])
    assert profile_values({'mode':'commands','write_paths':['.']})==('implementer',True,['.'])


def test_protected_commands_require_project_command_permissions():
    with pytest.raises(ValueError,match='system commands'):
        validate_defaults({'mode':'edit','write_paths':['.'],'command_protection':{'review':'codex'}})
    with pytest.raises(ValueError):
        validate_defaults({'mode':'commands','write_paths':['.'],'command_protection':{'review':'off'}})


def test_console_passes_explicit_protection_and_its_budget_before_verifier(tmp_path):
    selection=Selection(str(tmp_path),role='implementer',write_paths=['.'],prompt='Run the check',verifier='["python","check.py"]',
                        command_protection={'review':'both','backend':'codex','max_reviews':3})
    args=command(selection)
    assert args[args.index('--command-review')+1]=='both'
    assert args[args.index('--command-isolation')+1]=='codex'
    assert args[args.index('--max-command-reviews')+1]=='3'
    assert args.index('--command-review')<args.index('--verify-command')
    assert '--terminal' not in args
    selection.terminal=True
    with pytest.raises(ValueError,match='interactive terminal'):command(selection)


def test_pending_protection_displays_actual_tools_and_limits(tmp_path):
    root=tmp_path/'project';root.mkdir()
    selected=Selection(str(root),role='implementer',write_paths=['.'],command_protection={'review':'codex','backend':'mxc','max_reviews':2})
    view=permissions_view(tmp_path/'preferences',root,tmp_path/'control',pending=selected)
    assert 'Protected noninteractive commands' in view['effective_text']
    assert 'command' in view['effective_text'] and 'up to 2 per request' in view['effective_text']
    assert view['defaults']=={'mode':'read','write_paths':[],
                             'project_admission':{'mode':'quarantine','root_sha256':None,'nested':False}}



def test_jev_auto_preferences_are_independent_and_resume_does_not_override(tmp_path):
    root=tmp_path/'project';root.mkdir()
    defaults={'mode':'commands','write_paths':['.'],'command_protection':
        {'review':'codex','backend':'mxc','classifier':'jev','review_policy':'on_demand','max_reviews':2}}
    saved,revision=save_defaults(tmp_path/'preferences',root,tmp_path/'control',defaults,'missing')
    assert saved['command_protection']['classifier']=='jev'
    selected=Selection(str(root),role='implementer',write_paths=['.'],prompt='Run the admitted check',command_protection=saved['command_protection'])
    args=command(selected)
    assert args[args.index('--command-classifier')+1]=='jev'
    assert args[args.index('--command-review-policy')+1]=='on_demand'
    session=tmp_path/'session';session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config':{'repositories':{'project':str(root)},
        'command_protection':saved['command_protection']}}))
    selected.session=str(session)
    selected.command_protection={'review':'both','classifier':'model','review_policy':'always'}
    resumed=command(selected,True)
    assert '--resume' in resumed and '--command-classifier' not in resumed and '--command-review' not in resumed
    view=permissions_view(tmp_path/'preferences',root,tmp_path/'control',pending=Selection(str(root),role='implementer',write_paths=['.'],
        command_protection=saved['command_protection']))
    assert 'Classifier: Jev' in view['effective_text'] and 'on_demand' in view['effective_text']


def test_on_demand_review_cannot_silently_use_the_selected_frontier_model():
    with pytest.raises(ValueError,match='independent Jev'):
        validate_defaults({'mode':'commands','write_paths':['.'],
            'command_protection':{'review':'codex','classifier':'model','review_policy':'on_demand'}})

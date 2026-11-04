"""Project permission defaults and observations of immutable session scope."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from compute_plane.native_journal import custody_lock, regular, save_json
from tare_tools_agent_runtime import ConversationRejected
from .native_agent import read_json, project_instructions

from . import project_admission

DEFAULTS = {'mode': 'read', 'write_paths': [], 'project_admission': dict(project_admission.QUARANTINE)}


def validate_defaults(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'write_paths', 'command_protection', 'project_admission'} or not {'mode', 'write_paths'} <= set(value) or not isinstance(value['mode'], str) or value['mode'] not in {'read', 'edit', 'commands'}:
        raise ValueError('Choose read only, edit files, or edit with system commands.')
    paths = value['write_paths']
    if not isinstance(paths, list) or len(paths) > 32 or not paths and value['mode'] != 'read':
        raise ValueError('Enter 1–32 project-relative edit paths, or . for the whole project.')
    cleaned = []
    for path in paths:
        if isinstance(path, str):
            path = path.strip()
        if (not isinstance(path, str) or not path.strip() or len(path) > 512
                or any(c in path for c in '\\:*?[]\0\r\n') or PurePosixPath(path).is_absolute()
                or '..' in PurePosixPath(path).parts
                or any(p.endswith((' ', '.')) and p != '.' for p in PurePosixPath(path).parts)
                or '.git' in [p.casefold() for p in PurePosixPath(path).parts]):
            raise ValueError('Use relative edit paths without globs, parent traversal, .git, or trailing dots/spaces.')
        name = PurePosixPath(path.strip()).as_posix()
        if name not in cleaned:
            cleaned.append(name)
    result = {'mode': value['mode'], 'write_paths': cleaned}
    if 'project_admission' in value:
        result['project_admission'] = project_admission.validate(value['project_admission'])
    if value.get('command_protection') is not None:
        if value['mode'] != 'commands':
            raise ValueError('Choose Edit + system commands before enabling command protection.')
        from .command_protection import validate
        result['command_protection'] = validate(value['command_protection'])
    if len(json.dumps(result).encode()) > 16384:
        raise ValueError('Edit paths exceed the 16 KiB preferences budget.')
    return result


def preference_path(directory, project, control_root):
    project, control = str(Path(project).resolve()), str(Path(control_root).resolve())
    directory = regular(Path(directory))
    if directory.resolve().is_relative_to(Path(project)):
        raise ValueError('Project preferences must be stored outside the project.')
    key = hashlib.sha256((os.path.normcase(project)+'\0'+os.path.normcase(control)).encode()).hexdigest()
    return regular(directory/'project-permissions'/key/'preferences.json'), project, control


def read_defaults(directory, project, control_root):
    """The TUI's defaults for a project; the permission mode itself is the one every frontend shares
    (turn_engine.project_permission_mode), while the edit paths stay in this scope's file."""
    defaults, revision = stored_defaults(directory, project, control_root)
    # Inspection preserves stale consent; save and session use validate source pins.
    from .turn_engine import project_permission_mode
    mode = project_permission_mode(preference_path(directory, project, control_root)[1])
    if mode and mode != defaults['mode']:
        defaults = validate_defaults({'mode': mode, 'write_paths': defaults['write_paths'] or ['.'],
            **({'project_admission': defaults['project_admission']} if 'project_admission' in defaults else {}),
            **({'command_protection': defaults['command_protection']}
               if mode == 'commands' and 'command_protection' in defaults else {})})
    return defaults, revision


def stored_defaults(directory, project, control_root):
    path, project, control = preference_path(directory, project, control_root)
    if not path.exists():
        return {**DEFAULTS, 'write_paths': [], 'project_admission': dict(project_admission.QUARANTINE)}, 'missing'
    try:
        value = read_json(path, 32768)
    except ConversationRejected as exc:
        raise ValueError('Saved project preferences contain invalid JSON; the file was preserved.') from exc
    if (set(value) != {'schema', 'project', 'control_root', 'defaults'}
            or value['schema'] != 'tare.tools/project-permissions/1'
            or Path(value['project']) != Path(project) or Path(value['control_root']) != Path(control)):
        raise ValueError('Saved project preferences have a different scope; the file was preserved.')
    defaults = validate_defaults(value['defaults'])
    return defaults, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save_defaults(directory, project, control_root, defaults, expected_revision):
    defaults = validate_defaults(defaults)
    path, project, control = preference_path(directory, project, control_root)
    if not Path(project).is_dir():
        raise ValueError('Project directory is unavailable.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with custody_lock(path.parent):
        _, revision = stored_defaults(directory, project, control)
        if not expected_revision or revision != expected_revision:
            raise ValueError('Project defaults changed. Refresh before saving again.')
        if 'project_admission' in defaults:
            project_admission.check(project, defaults['project_admission'])
        save_json(path, {'schema': 'tare.tools/project-permissions/1', 'project': project,
                         'control_root': control, 'defaults': defaults})
        from .turn_engine import set_project_permission_mode
        set_project_permission_mode(project, defaults['mode'])  # tare serve's /mode reads the same choice
        return read_defaults(directory, project, control)


def admission_status(defaults):
    """Absent policy is legacy saved consent, not retroactive project adoption."""
    return defaults.get('project_admission', {}).get('mode', 'legacy')


def preview_admission(project):
    return project_admission.preview(project)


def configure_admission(defaults, project, reviewed=None, *, load_instructions=False, nested=False):
    """Permissions are an independent explicit choice, including edit in quarantine."""
    if nested and not load_instructions:
        raise ValueError('Nested instructions require explicit instruction adoption.')
    policy = (project_admission.configure(project, reviewed, nested=nested) if load_instructions
              else dict(project_admission.QUARANTINE))
    return validate_defaults({**defaults, 'project_admission': policy})


def profile_values(defaults):
    defaults = validate_defaults(defaults)
    return ('explorer' if defaults['mode'] == 'read' else 'implementer',
            defaults['mode'] == 'commands' and 'command_protection' not in defaults, list(defaults['write_paths']))


def permissions_view(directory, project, control_root, *, session=None, pending=None):
    project = str(Path(project).resolve())
    defaults, revision = read_defaults(directory, project, control_root)
    nested, nested_enabled, nested_error = {}, defaults.get('project_admission', {}).get('nested', True), None
    if session:
        from .native_review_actions import review_custody
        with review_custody(session) as (_, state, config, records):
            names = [name for name, root in config['repositories'].items() if Path(root).resolve() == Path(project)]
            if len(names) != 1:
                raise ValueError('The saved conversation belongs to another project.')
            name = names[0]
            original = records[0].event().get('capabilities', [])
            tools = sorted(set(original) & set(config.get('capabilities', original)))
            role, terminal = config['role'], bool(config.get('terminal'))
            protection = config.get('command_protection', {})
            paths = config['writable'].get(name, []) if 'edit' in tools else []
            checks = [key for key, row in config['verifiers'].items() if row['repository'] == name and 'verify' in tools]
            saved = [row for row in state.get('project_instructions', []) if row['repository'] == name]
            nested_enabled = state.get('scoped_instructions', False)
            if nested_enabled:
                for record in records:
                    event = record.event()
                    if event['kind'] == 'effect_returned':
                        output = json.loads(event['result']['output_json'])
                        for row in output.get('scoped_project_instructions', []):
                            if row['repository'] == name:
                                nested[row['path']] = row
                if nested:
                    from compute_plane.mediated_tools import MediatedTools
                    from compute_plane.native_instructions import ScopedInstructions
                    try:
                        resolver = MediatedTools({'role': 'explorer', 'repositories': {name: project},
                            'writable': {}, 'verifiers': {}, 'specgraph_edit_context': False})
                        ScopedInstructions(resolver.roots, resolver.path).restore(list(nested.values()))
                    except (ValueError, OSError, UnicodeError) as exc:
                        reason, _, location = str(exc).partition(':')
                        if location.startswith(name+':'):
                            location = location[len(name)+1:]
                        description = {'instruction_source_changed': 'changed',
                            'instruction_pinned_row_changed': 'changed',
                            'present_file_missing_on_disk': 'was removed',
                            'absent_file_now_exists': 'was added after this conversation started'}.get(reason)
                        nested_error = (location.strip()+' '+description if description and location else
                                        'The saved instruction files could not be validated')
            title = ('Coordinator · workers request edit access when needed' if state.get('conversation_flow') else
                     'Saved conversation · permissions remain unchanged')
    else:
        role, terminal, paths = profile_values(defaults)
        protection = defaults.get('command_protection', {})
        if pending is not None:
            role, terminal, paths = pending.role, pending.terminal, list(pending.write_paths)
            protection = getattr(pending, 'command_protection', {})
        paths = paths if role == 'implementer' else []
        tools = ['read', 'list', 'search'] + (['edit'] if role == 'implementer' else [])
        if terminal and role == 'implementer':
            from compute_plane.native_terminal import TERMINAL_SPECS
            tools += list(TERMINAL_SPECS)
        if protection:
            tools.append('command')
        checks, saved, name = [], None, Path(project).name
        if pending is not None and pending.verifier.strip():
            checks = ['check (configured; validated at session start)']
            tools += ['verify'] if role == 'implementer' else []
        title = ('Next message · current unstarted conversation settings' if pending is not None else
                 'Stored defaults · applied to new conversations')
    effective = [title, 'Project: '+project, 'Role: '+role, 'Tools: '+(', '.join(tools) or 'None'),
                 'Editable paths: '+(', '.join(paths) or 'None'),
                 'System commands: '+('Protected noninteractive commands' if protection else
                     'Enabled' if terminal and any(t.startswith('terminal_') for t in tools) else 'Disabled'),
                 'Configured checks: '+(', '.join(checks) or 'None in this scope')]
    if protection:
        if protection.get('classifier')=='jev':
            effective += ['Classifier: Jev  Review policy: '+protection.get('review_policy','always')]
        effective += ['Command review: '+protection['review']+'  Isolation: '+protection['backend'],
                      'Review calls: up to '+str(protection['max_reviews'])+' per request; native usage may be unknown.',
                      'Protected commands use the declared Windows profile. Interactive terminal is unavailable in this mode.']
    else:
        effective.append('Configured checks run programs. Edit paths do not restrict command filesystem or network access.')
    if not session:
        effective.append('The new-session verifier field is separate; saved conversations keep their original checks.')
    instruction_policy = (config.get('project_admission') if session else
                          pending.project_admission if pending is not None else defaults.get('project_admission'))
    if not session and instruction_policy is not None:
        nested_enabled = instruction_policy['nested']
    current_preview = None
    try:
        current_preview = project_admission.preview(project)
        current = ([] if current_preview['text'] is None else [{
            'repository': name, 'path': 'AGENTS.md',
            'sha256': current_preview['root_sha256'], 'text': current_preview['text']}])
        current_error = None
    except (ValueError, OSError, UnicodeError) as exc:
        current, current_error = [], str(exc)
    instructions = ([('Project instructions quarantined; the preview below is data, not adopted guidance.'
                      if instruction_policy['mode'] == 'quarantine' else
                      'Root AGENTS.md adopted; nested discovery requires separate explicit authorization.')]
                    if instruction_policy is not None else
                    [('Legacy instruction policy: root and scoped discovery.' if nested_enabled else
                      'Legacy conversation: root AGENTS.md only.')])
    if instruction_policy is not None and instruction_policy['mode'] == 'adopted':
        stale = (current_preview is None or
                 current_preview['root_sha256'] != instruction_policy['root_sha256'])
        instructions.append('Root approval stale; review the current preview and approve again. '
                            'The stored policy remains unchanged.' if stale else
                            'Root approval matches the current preview.')
    if nested_enabled:
        instructions.append('Nested rules apply only inside their directory subtree. Deeper rules take precedence there. '
                            'Shell commands and checks can access other folders; their arbitrary paths are not inspected.')
    if saved is not None:
        instructions.append('Saved conversation instructions:')
        instructions += [row['text'] for row in saved] or ['No root AGENTS.md was loaded.']
        instructions.append('Current root instructions: '+(
                            'Not adopted; changing the preview does not block resume.'
                            if instruction_policy is not None and instruction_policy['mode'] == 'quarantine' else
                            'Unavailable: '+current_error if current_error else
                            'Match the saved snapshot.' if current == saved else
                            'Changed since this conversation started. Resume requires the original instructions.'))
        if current != saved and current:
            instructions += ['Current AGENTS.md:', *[row['text'] for row in current]]
    else:
        instructions += ['Current root instructions (read again at session start):']
        instructions += (['Unavailable: '+current_error] if current_error else
                         [row['text'] for row in current] or ['No root AGENTS.md found.'])
    if nested_enabled and session:
        instructions.append('Saved nested instruction observations:')
        for key in sorted(nested):
            row = nested[key]
            instructions.append(row['path'] + (' · absent when accessed' if row['text'] is None else '\n'+row['text']))
        if not nested:
            instructions.append('No subfolders have been observed yet.')
        else:
            instructions.append('Current nested instructions: ' + ('Changed or unavailable: '+nested_error+
                '. Restore the saved instructions to resume, or start a new conversation.' if nested_error else
                'Match the saved observations, including absent files.'))
    if not session:
        instructions.insert(0, 'Instruction policy: '+
                            (instruction_policy['mode'] if instruction_policy is not None else 'legacy')+
                            ' (stored defaults: '+admission_status(defaults)+'). '+
                            'Permissions and instruction adoption are separate choices.')
    elif 'project_admission' in config:
        instructions.insert(0, 'Instruction policy: '+config['project_admission']['mode']+
                            '. Nested discovery: '+str(nested_enabled)+'.')
    instructions.insert(0, 'Preview project: '+project+'\nPath: '+str(Path(project)/'AGENTS.md')+
                        '\nSHA-256: '+(current_preview['root_sha256'] or 'absent')
                        if current_preview is not None else 'Root preview unavailable; ignoring instructions is still allowed.')
    return {'project': project, 'revision': revision, 'defaults': defaults,
            'admission_preview': current_preview,
            'effective_text': '\n'.join(effective), 'instructions_text': '\n\n'.join(instructions)}

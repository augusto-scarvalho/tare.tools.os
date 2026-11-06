"""Read access to folders outside a session's repositories: the agent asks, the user grants, the grant stays.

A session started with `--folder-grants FILE` has four tools whose schemas never change, so its identity holds
across turns. request_folder_access records which folder the agent wants and why; the TUI asks the user when
the turn ends. folder_list, folder_read and folder_search read a folder the user granted. The grants live in
FILE, one per project and outside the session's configuration, so a grant works in the same conversation and
in every later one. Reads go through MediatedTools' path checks (no '..', '.git', links or writes).
"""
import json
import os
from pathlib import Path

from compute_plane.mediated_tools import MediatedTools
from compute_plane.native_capabilities import TOOL_SPECS
from compute_plane.native_models import encode, object_json
from tare_tools_agent_runtime import EffectResult

SCHEMA = 'tare.tools/folder-grants/1'
REQUEST = 'folder-access-request.json'  # in the session's custody: the turn's pending request
DECLINED = 'folder-access-declined.json'  # in the session's custody: folders a reply of the user did not grant
MAX_FOLDERS = 64


def _granted_variant(name):
    spec = json.loads(json.dumps(TOOL_SPECS[name]))
    properties = {key: value for key, value in spec['parameters']['properties'].items()
                  if key not in {'repository', 'sha256'}}
    spec['parameters']['properties'] = {'folder': {
        'type': 'string', 'description': 'Absolute path of a folder the user granted (request_folder_access).'},
        **properties}
    spec['parameters']['required'] = ['folder', *[r for r in spec['parameters']['required'] if r != 'repository']]
    spec['description'] = 'In a folder the user granted, outside this session\'s repositories: ' + spec['description']
    return spec


SPECS = {
    'request_folder_access': {
        'description': 'Ask the user for read access to a folder outside this session\'s repositories (another '
            'project, a reference). Give its absolute path and why the task needs it, then end your turn by asking '
            'the user, in their language. Once they allow it, read it with folder_list, folder_read and '
            'folder_search; the grant stays for this project. Do not ask the user to copy files instead.',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': 'Absolute folder path.'},
            'reason': {'type': 'string', 'description': 'Why the task needs it, in one sentence.'}},
            'required': ['path', 'reason'], 'additionalProperties': False}},
    'folder_list': _granted_variant('list'),
    'folder_read': _granted_variant('read'),
    'folder_search': _granted_variant('search'),
}


def store_path(directory, project, control_root):
    """The project's grants file, next to its TUI permission preferences (outside the project)."""
    from .project_permissions import preference_path
    path, _, _ = preference_path(directory, project, control_root)
    return path.parent.parent.parent/'folder-grants'/path.parent.name/'grants.json'


def project_store(project):
    """The grants file the TUI keeps for this project in its default storage: tare serve uses the same one, so a
    folder allowed in either is allowed in both."""
    from . import relay_mesh
    from .native_installation import default_storage, workspace_storage
    return store_path(workspace_storage(project, default_storage())/'console', project, relay_mesh.RELAY_ROOT)


CREDENTIALS = {'.ssh', '.aws', '.gnupg', '.azure', '.kube', '.docker', '.config', '.tare-relay-credentials'}


def folder(value):
    """An existing absolute folder, resolved. A drive, the user's home or a folder above it would grant their
    credentials too, and credential folders themselves are refused; every grant and read goes through here, so a
    grant saved before a refusal existed is refused as well."""
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError('Give the absolute path of an existing folder.')
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError('Not an existing folder: ' + value)
    if path.parent == path:
        raise ValueError('A whole drive is too broad; ask for the project folder you need.')
    if Path.home().resolve().is_relative_to(path):
        raise ValueError('The user folder (or one above it) holds credentials; ask for the project folder you need.')
    if CREDENTIALS & {part.lower() for part in path.parts}:
        raise ValueError('That folder holds credentials; it is never read.')
    return path


def same(first, second):
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def granted(store):
    try:
        value = json.loads(Path(store).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or not isinstance(value.get('folders'), list):
        return []
    return [item for item in value['folders'] if isinstance(item, str)]


def grant(store, path):
    """Remember a folder the user allowed; returns it as stored."""
    path = str(folder(path))
    store = Path(store)
    folders = [item for item in granted(store) if not same(item, path)] + [path]
    store.parent.mkdir(parents=True, exist_ok=True)
    temporary = store.with_name(store.name + '.tmp')
    temporary.write_text(json.dumps({'schema': SCHEMA, 'folders': folders[-MAX_FOLDERS:]}, indent=1), encoding='utf-8')
    os.replace(temporary, store)  # ponytail: last writer wins; two TUIs granting at once lose one grant
    return path


def pending(custody):
    """The request the session's last turn left for the user, if any."""
    try:
        value = json.loads((Path(custody)/REQUEST).read_text(encoding='utf-8'))
        return {**value, 'path': value['path'], 'reason': value['reason']}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def settle(custody):
    (Path(custody)/REQUEST).unlink(missing_ok=True)


def allows(text):
    """Whether the user's reply is a plain yes: every word of a short reply agrees ('sim', 'pode sim', 'ok!').
    'ok, mas antes...' answers something else and grants nothing."""
    words = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in text.lower()).split()
    if words in (['pode', 'uai'], ['pode', 'sim', 'uai'], ['sim', 'uai'], ['claro', 'que', 'pode']):
        return True
    return 0 < len(words) <= 3 and all(word in {
        'sim', 's', 'pode', 'ok', 'okay', 'claro', 'yes', 'y', 'libera', 'liberado', 'autorizo', 'permito', 'manda',
        'vai', 'beleza', 'please', 'por', 'favor'} for word in words)


def literal(value):
    """A Markdown code span that preserves literal paths, including backticks."""
    text = str(value)
    fence = '`'
    while fence in text:
        fence += '`'
    return f'{fence} {text} {fence}'


def question(request, store, *, rule='judge'):
    """What the user is asked, in the TUI and in tare serve's chats."""
    from .consent_decisions import guidance
    return (f"The agent asks to read {literal(request['path'])}: {request['reason'].rstrip('.')}. {guidance(rule)}. What it reads goes to the conversation's model, and the folder "
            f"stays allowed for this project on this machine (remove it from {literal(store)} to take it back).")


def presented_question(request, store, custody):
    """Render the saved contract; transports never recompute availability."""
    from . import consent_decisions as consent
    key = request.get('question_id')
    if key:
        q = next((q for q in consent.load(custody)['questions'] if q['id'] == key), None)
        if not q or not q.get('question'):
            raise ValueError('Re-present this invalid folder question.')
        return q['question']
    return question(request, store)


def declined(custody, path):
    """Whether a reply of the user to this session's request for `path` granted nothing."""
    try:
        folders = json.loads((Path(custody)/DECLINED).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(folders, list) and any(isinstance(item, str) and same(item, path) for item in folders)


def _decline(custody, path):
    if declined(custody, path):
        return
    try:
        folders = json.loads((Path(custody)/DECLINED).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        folders = []
    temporary = Path(custody)/(DECLINED + '.tmp')
    temporary.write_text(json.dumps([*[f for f in folders if isinstance(f, str)], str(path)]), encoding='utf-8')
    os.replace(temporary, Path(custody)/DECLINED)


def answer(text, request, store):
    """The note sent to the agent with the user's reply. A short yes grants (and saves); anything else does not,
    and is recorded in the asking session, so a waiting worker is told of a refusal only the user gave."""
    raise ValueError('Raw-text consent is disabled. Re-present the folder question in a compatible conversation.')


class FolderTools:
    def __init__(self, tools, config, custody):
        self.tools, self.custody = tools, Path(custody)
        self.store = Path(config['folder_access']['grants'])
        self.roots = [Path(root).resolve() for root in config['repositories'].values()]
        self.names = (*tools.names, *SPECS)
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **SPECS})
        self._seen = set()  # like NativeCapabilities: only a new observation counts as progress

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def execute(self, request, control):
        name = request.tool_call.name
        if name not in SPECS:
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            args = object_json(request.tool_call.arguments_json)
            if len(encode(args).encode()) > 16384:
                raise ValueError('Use the declared arguments, within 16 KiB.')
            if name == 'request_folder_access':
                output = self._request(args)
            else:
                path = folder(args.pop('folder', None))
                if not any(same(path, item) for item in granted(self.store)):
                    raise ValueError('The user has not granted this folder. Call request_folder_access first.')
                reader = MediatedTools({'role': 'explorer', 'repositories': {'folder': str(path)}, 'writable': {},
                                        'verifiers': {}, 'admitted_capabilities': ['list', 'read', 'search'],
                                        'specgraph_edit_context': False}, cancelled=control.should_stop)
                output = {'folder': str(path), **reader.perform(name.removeprefix('folder_'), {**args, 'repository': 'folder'})}
            observation = encode({'name': name, 'args': args, 'output': output})
            progressed = observation not in self._seen and not (name == 'folder_search' and not output.get('matches'))
            self._seen.add(observation)
            return EffectResult(request.effect_id, 'succeeded', encode(output), progressed)
        except (ValueError, OSError, KeyError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)}))

    def _request(self, args):
        if set(args) != {'path', 'reason'} or not isinstance(args['reason'], str) or not args['reason'].strip():
            raise ValueError('Give path and reason.')
        path = folder(args['path'])
        if any(path == root or path.is_relative_to(root) for root in self.roots):
            return {'status': 'ALREADY_READABLE', 'next': 'Use read, list and search with this session\'s repository.'}
        if any(same(path, item) for item in granted(self.store)):
            return {'status': 'GRANTED', 'folder': str(path), 'next': 'Read it with folder_list, folder_read and folder_search.'}
        from . import consent_decisions as consent
        scope = {'path': str(path), 'store': str(self.store.resolve())}
        request = {'path': str(path), 'reason': args['reason'].strip()[:500]}
        rule = consent.select_rule()
        question_id = consent.register(self.custody, self.session_id, 'folder', scope, presented=False,
                                       question=question(request, self.store, rule=rule), rule=rule)
        temporary = self.custody/(REQUEST + '.tmp')
        temporary.write_text(json.dumps({'path': str(path), 'reason': args['reason'].strip()[:500],
            'question_id': question_id}), encoding='utf-8')
        os.replace(temporary, self.custody/REQUEST)
        return {'status': 'ASKED', 'folder': str(path),
                'next': 'End your turn now: ask the user, in their language, to allow reading this folder and say why.'}

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending as pending_effect
        request = pending_effect(journal, self.session_id)
        if request is None or request.tool_call.name not in SPECS:
            return self.tools.recover_effect(journal)
        # Reads change nothing and a request only asks again: repeating either is safe.
        return request, EffectResult(request.effect_id, 'refused', encode({
            'reason': 'Interrupted before a result was recorded. Run it again.'}))

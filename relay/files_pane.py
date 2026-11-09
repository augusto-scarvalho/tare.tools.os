"""Read-only files pane of the TUI (F8): what the agents read and edit, this turn or this session.

The rows come from the transcript's own retained tool activity (`assistant_history`), which every route and role
reaches through the same journal projection (relay/native_activity.py), so the pane has no hook per vendor and
always agrees with the transcript, also when a conversation is reopened. It sends nothing to a model.
"""
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, Static, Tree
from textual.widgets.option_list import Option


SCHEMA = 'tare.tools/console-files-pane/1'
FILE = 'files-pane.json'
FILE_OPERATIONS = {'read', 'edit', 'folder_read'}
FAILED = {'failed', 'refused', 'unknown', 'unconfirmed', 'not_started'}
LIST_LIMIT = 100_000
STATUS_LIMIT = 5000
POLL_TICKS = 6  # git status every 3 s while the pane shows (0.5 s ticks)
SLOW_SECONDS = 1  # a read slower than this polls every 10 s instead
VIEW_BYTES = 256*1024  # the interactive preview bound of read_edit_file
GIT_SECONDS = 30  # off the UI thread, so a cold status of a large repository may take its time
CONFLICTS = {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}
ROLES = {'Implement': 'impl', 'Explore': 'expl', 'Code review': 'audit', 'Plan draft': 'plan',
         'Plan review': 'plan audit', 'Research': 'research', 'Candidate verification': 'check'}


def read_open(root):
    """Whether the pane was left open on this machine; closed when never saved."""
    from compute_plane.native_journal import regular
    try:
        raw = regular(Path(root)/FILE).read_bytes()[:4097]
    except FileNotFoundError:
        return False
    value = json.loads(raw) if len(raw) <= 4096 else None
    if not isinstance(value, dict) or set(value) != {'schema', 'open'} or value['schema'] != SCHEMA \
            or not isinstance(value['open'], bool):
        raise ValueError('Files pane preference uses an unsupported format.')
    return value['open']


def save_open(root, flag):
    from compute_plane.native_journal import custody_lock, regular, save_json
    root = regular(root)
    root.mkdir(parents=True, exist_ok=True)
    with custody_lock(root):
        read_open(root)  # never replace an unreadable or newer format implicitly
        save_json(root/FILE, {'schema': SCHEMA, 'open': bool(flag)})


@dataclass(frozen=True)
class Touch:
    path: str
    edited: bool = False
    proposed: bool = False  # edited by a delegated session, in its candidate tree
    deleted: bool = False
    running: bool = False
    failed: bool = False
    reason: str = ''
    role: str = ''
    outside: bool = False  # a granted folder outside the project

    @property
    def project_path(self):
        """The path as the tree and git name it ('/'-separated, no leading './')."""
        return self.path.replace('\\', '/').removeprefix('./')


def role_label(row):
    stage = (row.get('source') or {}).get('stage', '')
    phase = stage.rsplit(' · ', 1)[-1]  # "Work W-12 · Implement"
    return ROLES.get(phase, 'expl' if phase.startswith('Explore ') else phase)


def touches(history, *, turn_only):
    """One entry per file the agents read or edited, first touch first; an edit outranks a read and the latest row
    gives the state. A turn starts at the operator's message ('You: ...'): a resume without a message stays in the
    previous turn, and once the history no longer holds the last message, "this turn" is all it kept."""
    from .native_activity import restored
    rows = [restored(row) if isinstance(row, dict) else row for row in history]  # a reopened history is saved_history()
    if turn_only:
        starts = [index for index, value in enumerate(rows) if isinstance(value, str) and value.startswith('You: ')]
        rows = rows[starts[-1]+1:] if starts else rows
    found = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') != 'tool-activity' or row.get('operation') not in FILE_OPERATIONS:
            continue
        path = ' '.join(str(row.get('target', '')).split())
        if not path:
            continue
        edit = row['operation'] == 'edit'
        before = found.get(path, Touch(path))
        found[path] = Touch(
            path, edited=before.edited or edit,
            proposed=(edit and bool(row.get('source'))) or (before.proposed and not edit),
            deleted=edit and 'Deleted file.' in str(row.get('detail', '')) or (before.deleted and not edit),
            running=row.get('state') == 'running', failed=row.get('state') in FAILED,
            reason=str(row.get('reason') or ''),
            role=before.role if before.edited and not edit else role_label(row),  # who edited it, else who read it
            outside=row['operation'] == 'folder_read')
    return list(found.values())


def list_paths(root, limit=LIST_LIMIT):
    """Project-relative files, '/'-separated: git's tracked and untracked files that are not ignored, else a walk
    that skips conversation_workspace.IGNORED. Returns (sorted paths, truncated)."""
    from .console_header import run_git
    try:
        listed = run_git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', timeout=30, text=False)
        if listed.returncode == 0:
            paths = sorted({p for p in listed.stdout.decode('utf-8', 'replace').split('\0') if p})
            return paths[:limit], len(paths) > limit
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    from .conversation_workspace import IGNORED
    paths = []
    for directory, folders, files in os.walk(root):
        folders[:] = [name for name in folders if name not in IGNORED]
        base = Path(directory).relative_to(root).as_posix()
        paths += [name if base == '.' else base+'/'+name for name in files]
        if len(paths) > limit:
            return sorted(paths)[:limit], True
    return sorted(paths), False


def git_status(root, limit=STATUS_LIMIT):
    """{'marks': {path: M A D U !}, 'dirs': (untracked folders 'a/',), 'count': n, 'numstat': {path: (added,
    removed)}}, or None outside a repository. Read-only; `count` counts as `git status` does. Paths are relative
    to `root`, also when it is a folder inside the repository (git status names them from the repository's top)."""
    from .console_header import run_git
    top = run_git(root, 'rev-parse', '--show-prefix', timeout=GIT_SECONDS)
    if top.returncode != 0:
        return None
    prefix = top.stdout.strip()
    status = run_git(root, 'status', '--porcelain=v1', '-z', '--no-renames', '--untracked-files=normal', '--', '.',
                     timeout=GIT_SECONDS)
    if status.returncode != 0:
        return None
    entries = [(entry[:2], entry[3+len(prefix):]) for entry in status.stdout.split('\0')
               if len(entry) > 3 and entry[3:].startswith(prefix)]
    marks, dirs = {}, []
    for code, path in entries[:limit]:
        if code == '??' and (path == '' or path.endswith('/')):  # '' is the opened folder itself, wholly untracked
            dirs.append(path)
        else:
            marks[path] = ('U' if code == '??' else '!' if code in CONFLICTS else
                           'D' if 'D' in code else 'A' if 'A' in code else 'M')
    numstat = {}
    if entries:  # -z keeps unusual names unquoted; --relative names them from root
        diff = run_git(root, 'diff', '--numstat', '-z', '--no-renames', '--no-textconv', '--relative', 'HEAD',
                       timeout=GIT_SECONDS)
        for record in diff.stdout.split('\0') if diff.returncode == 0 else ():
            added, removed, path = (record.split('\t', 2)+['', ''])[:3]
            if added.isdigit() and removed.isdigit():
                numstat[path] = (int(added), int(removed))
    return {'marks': marks, 'dirs': tuple(dirs), 'count': len(entries), 'numstat': numstat}


def read_text(root, path):
    """A project file's text for the viewer: linked or outside paths refused (console_context.context_path),
    bounded, CRLF shown as LF, a binary file named as such."""
    from .console_context import context_path
    target, _ = context_path(root, path)
    if not target.is_file():
        raise ValueError('Choose a file, not a folder.')
    with target.open('rb') as stream:
        raw = stream.read(VIEW_BYTES+1)
    if len(raw) > VIEW_BYTES:
        return 'File exceeds the interactive preview limit.'
    if b'\0' in raw[:4096]:
        return 'Binary file; no text preview.'
    return raw.decode('utf-8', 'replace').replace('\r\n', '\n')


def diff_text(root, path):
    """What changed in a project file since the last commit (staged and not), or the whole new file when Git
    does not track it yet."""
    from .console_context import context_path
    from .console_header import run_git
    try:
        _, name = context_path(root, path)
    except ValueError:
        # A deleted file is gone from disk, and its deletion is the diff worth seeing: a missing path cannot be a
        # link, so a project-relative name without '..' or '.git' is enough.
        name = path.replace('\\', '/').removeprefix('./')
        parts = name.split('/')
        if Path(name).is_absolute() or '..' in parts or '.git' in parts or (Path(root)/name).exists():
            raise
    def git(*args):  # names are literal (a[1].py is not a glob) and no textconv program runs
        return run_git(root, '--literal-pathspecs', *args, timeout=GIT_SECONDS)
    if git('rev-parse', '--show-prefix').returncode != 0:
        return 'Not a Git repository: no diff.'
    if git('ls-files', '--error-unmatch', '--', name).returncode != 0:
        return 'New file, not in Git yet:\n\n'+read_text(root, name)
    if git('rev-parse', '--verify', '--quiet', 'HEAD').returncode != 0:
        return 'No commit yet:\n\n'+read_text(root, name)
    diff = git('diff', '--no-ext-diff', '--no-textconv', '--no-color', 'HEAD', '--', name)
    text = diff.stdout.replace('\r\n', '\n')
    if len(text.encode('utf-8')) > VIEW_BYTES:
        return 'Diff exceeds the interactive preview limit.'
    return text or 'No change since the last commit.'


OPERATIONS = (('rebase-merge', 'rebase'), ('rebase-apply', 'rebase'), ('MERGE_HEAD', 'merge'),
              ('CHERRY_PICK_HEAD', 'cherry-pick'), ('REVERT_HEAD', 'revert'), ('BISECT_LOG', 'bisect'))


def git_state(root):
    """{'ahead', 'behind'} against the branch's upstream (None without one or on a detached HEAD) and the operation
    in progress ('' when none): read-only, from rev-list and the markers git leaves in its folder."""
    from .console_header import run_git
    counts = run_git(root, 'rev-list', '--left-right', '--count', '@{upstream}...HEAD', '--', timeout=GIT_SECONDS)
    behind, ahead = ([int(n) for n in counts.stdout.split()] if counts.returncode == 0 and len(counts.stdout.split()) == 2
                     else (None, None))
    marks = run_git(root, 'rev-parse', *[arg for name, _ in OPERATIONS for arg in ('--git-path', name)],
                    timeout=GIT_SECONDS)
    found = marks.stdout.splitlines() if marks.returncode == 0 else []
    operation = next((label for (_, label), path in zip(OPERATIONS, found) if (Path(root)/path).exists()), '')
    return {'ahead': ahead, 'behind': behind, 'operation': operation}


def git_line(status, truncated=False):
    """The pane's Git line: ↑ahead ↓behind, changes, an operation in progress and its conflicts."""
    if status is None:
        return 'Not a Git repository'
    state = status.get('state') or {}
    parts = [f"↑{state['ahead']} ↓{state['behind']}"] if state.get('ahead') or state.get('behind') else []
    parts.append(f"{status['count']}{'+' if status['count'] >= STATUS_LIMIT else ''} changed"
                 if status['count'] else 'No changes')
    if state.get('operation'):
        parts.append(state['operation']+' in progress')
    conflicts = sum(mark == '!' for mark in status['marks'].values())
    if conflicts:
        parts.append(f'!{conflicts} conflict'+('s' if conflicts > 1 else ''))
    if truncated:
        parts.append('list cut at 100 000')
    return ' · '.join(parts)


def listing(root):
    """list_paths with its index, both built in the worker thread."""
    paths, truncated = list_paths(root)
    return paths, truncated, index(paths)


def index(paths):
    """Directory ('' for the root) -> its children [(name, is_dir)], folders first."""
    children = {}
    for path in paths:
        parts = path.split('/')
        for depth, name in enumerate(parts):
            children.setdefault('/'.join(parts[:depth]), set()).add((name, depth < len(parts)-1))
    return {key: sorted(value, key=lambda child: (not child[1], child[0].lower())) for key, value in children.items()}


def mark_of(status, path, is_dir=False):
    """A file's git mark, '•' for a folder holding a change, '' when clean or outside git."""
    if not status:
        return ''
    if is_dir:  # dirs is a tuple: str.startswith takes it at once
        return 'U' if (path+'/').startswith(status['dirs']) else '•' if path in status['changed_dirs'] else ''
    return status['marks'].get(path) or ('U' if path.startswith(status['dirs']) else '')


def changed_dirs(status):
    folders = set()
    for path in [*status['marks'], *status['dirs']]:
        parts = path.rstrip('/').split('/')
        folders.update('/'.join(parts[:depth]) for depth in range(1, len(parts)))
    return folders


def glyph(touch, status=None):
    applied = touch.proposed and bool(status) and touch.path in status['marks']  # the candidate reached the project
    return ('⟳' if touch.running else '!' if touch.failed else 'x' if touch.deleted else
            'p' if touch.proposed and not applied else 'e' if touch.edited else 'r')


def summary_text(found, *, turn_only):
    edited = sum(touch.edited for touch in found)
    return f"{'This turn' if turn_only else 'Session'} · read {len(found)-edited} · edited {edited}"


def label(touch, width, edited_style='yellow', status=None):
    counted = (status or {}).get('numstat', {}).get(touch.path) if touch.edited else None
    suffix = (f' +{counted[0]} −{counted[1]}' if counted else '')+(' · '+touch.role if touch.role else '')
    room = max(8, width-4-len(suffix))
    path = touch.path if len(touch.path) <= room else '…'+touch.path[-(room-1):]
    text = Text(glyph(touch, status)+' ', style='bold' if touch.edited else '')
    text.append(path, style='dim' if touch.outside else edited_style if touch.edited and not touch.proposed else '')
    text.append(suffix, style='dim')
    return text


class FileTree(Tree):
    """The project's files, loaded per folder on expansion from an in-memory index (no I/O on expand)."""

    def __init__(self, **kwargs):
        super().__init__('project', **kwargs)
        self.show_root = False
        self.children_of, self.nodes, self.labels, self.status = {}, {}, {}, None
        self.edited_style = 'yellow'

    def load(self, children_of, *, expand_all=False):
        expanded = [path for path, node in self.nodes.items() if node.is_expanded]
        self.clear()
        self.children_of, self.nodes, self.labels = children_of, {}, {}
        self.populate(self.root, '')
        for path in (sorted(path for path, children in children_of.items() if path) if expand_all else expanded):
            self.open_folder(path)

    def populate(self, node, folder):
        for name, is_dir in self.children_of.get(folder, ()):
            path = folder+'/'+name if folder else name
            text = self.text(path, name, is_dir)
            self.labels[path] = text.plain
            self.nodes[path] = (node.add(text, data=(path, True)) if is_dir else
                                node.add_leaf(text, data=(path, False)))

    def text(self, path, name, is_dir):
        mark = mark_of(self.status, path, is_dir)
        text = Text(name, style=self.edited_style if mark and not is_dir else '')
        if mark:
            text.append('  '+mark, style='dim' if is_dir else 'bold')
        return text

    def on_tree_node_expanded(self, event):
        path, is_dir = event.node.data or ('', False)
        if is_dir and not event.node.children:
            self.populate(event.node, path)

    def open_folder(self, folder):
        """Expand a folder and its ancestors, loading each from the index; False when the index lacks it."""
        parts = folder.split('/')
        for depth in range(1, len(parts)+1):
            path = '/'.join(parts[:depth])
            node = self.nodes.get(path)
            if node is None:
                return False
            if not node.children:
                self.populate(node, path)
            node.expand()
        return True

    def reveal(self, path):
        folder = path.rsplit('/', 1)[0] if '/' in path else ''
        if (folder and not self.open_folder(folder)) or path not in self.nodes:
            return False
        self.last_line  # builds the tree's lines, so a node added since the last redraw has its line
        self.move_cursor(self.nodes[path])
        return True

    def mark(self, status, edited_style):
        """New git marks: only the labels whose text changes are set (set_label redraws)."""
        self.status, self.edited_style = status, edited_style
        for path, node in self.nodes.items():
            text = self.text(path, path.rsplit('/', 1)[-1], node.data[1])
            if self.labels.get(path) != text.plain:
                self.labels[path] = text.plain
                node.set_label(text)


class FilesPane(Vertical):
    """The right pane. Display is set on it only (never a screen class); its tick runs only while shown."""

    BINDINGS = [Binding('t', 'scope', 'Turn/session', show=False), Binding('c', 'changed', 'Changed only', show=False),
                Binding('d', 'diff', 'Diff', show=False), Binding('a', 'mention', '@-mention', show=False),
                Binding('y', 'copy_path', 'Copy path', show=False), Binding('escape', 'leave', 'Back', show=False)]
    HINT = 'Enter open · d diff · a @ · y copy · t turn · c changed · Esc'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.turn_only = True
        self.shown = ()
        self.found = []
        self.root = None  # the project the tree lists
        self.paths, self.truncated, self.status, self.revision = set(), False, None, 0
        self.children_of = {}  # the index of self.paths, built in the worker
        self.changed_only = False
        self.followed = set()
        self.ticks, self.wait, self.failures = 0, 0, 0
        self.polling = False  # one git read at a time; a slow one is never cancelled by the next tick

    def compose(self):
        yield Static('Files', id='files-title', markup=False)
        yield Static('', id='files-git', markup=False)
        yield Static('', id='files-summary', markup=False)
        yield OptionList(id='files-touched', markup=False)
        yield FileTree(id='files-tree')
        yield Static(self.HINT, id='files-hint', markup=False)

    def on_mount(self):
        self.timer = self.set_interval(.5, self.tick, pause=True)

    def set_visible_state(self, shown):
        if shown:
            self.failures, self.wait = 0, 0  # opening the pane retries a git that failed before
            self.tick()
            self.timer.resume()
        else:
            self.timer.pause()

    def tick(self):
        """Every half second while shown; widgets are updated only when what they show changed (an update is a
        layout pass). Git is read in a worker every few ticks, never on the UI thread."""
        app = self.app
        root = str(app.workspace_root)
        if root != self.root:  # another project: list it again, follow from scratch
            self.root, self.paths, self.status, self.followed, self.wait = root, set(), None, set(), 0
            self.children_of, self.failures = {}, 0
            self.query_one(FileTree).load({})
        self.ticks += 1
        if self.ticks >= self.wait and self.failures < 3 and not self.polling:
            self.ticks, self.wait, self.polling = 0, POLL_TICKS, True
            self.poll(root, relist=not self.paths)
        found = touches(getattr(app, 'assistant_history', ()), turn_only=self.turn_only)
        self.follow(found)
        width = max(16, self.size.width or 36)
        title = 'Files · '+Path(root).name
        edited = app.theme_variables.get('warning') or 'yellow'  # follows the light and high-contrast themes
        if (found, width, self.turn_only, title, edited, self.revision) == self.shown:
            return
        self.shown, self.found = (found, width, self.turn_only, title, edited, self.revision), found
        self.query_one('#files-title', Static).update(title)
        self.query_one('#files-summary', Static).update(summary_text(found, turn_only=self.turn_only))
        listing = self.query_one('#files-touched', OptionList)
        highlighted, scrolled = listing.highlighted, listing.scroll_y  # a new row must not jump the view
        listing.clear_options()
        listing.add_options([Option(label(touch, width, edited, self.status), id=touch.path) for touch in found])
        if found:
            listing.highlighted = min(highlighted or 0, len(found)-1)
            listing.scroll_to(y=scrolled, animate=False)

    def follow(self, found):
        """The tree opens at the newest file an agent edited (reads do not move it)."""
        edited = [touch.project_path for touch in found if touch.edited and not touch.outside]
        new = [path for path in edited if path not in self.followed and path in self.paths]
        if new:
            self.followed.update(new)
            if not self.changed_only:
                self.query_one(FileTree).reveal(new[-1])

    @work(group='files-poll')
    async def poll(self, root, relist):
        try:
            await self.read_git(root, relist)
        finally:
            self.polling = False

    async def read_git(self, root, relist):
        started = time.monotonic()
        try:
            status = await asyncio.to_thread(git_status, root)
            if status is not None:
                status['state'] = await asyncio.to_thread(git_state, root)
            known = self.paths
            missing = status and any(path not in known for path, mark in status['marks'].items() if mark != 'D')
            dirs_changed = status and self.status is not None and status['dirs'] != self.status['dirs']
            # A new file shows in git status first (untracked or added): only then is the project listed again.
            listed = await asyncio.to_thread(listing, root) if relist or missing or dirs_changed else None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.failures += 1
            if self.failures >= 3:  # stop polling until the pane is opened again
                self.query_one('#files-git', Static).update(
                    'Git did not answer in time; F8 twice reads it again' if isinstance(exc, subprocess.TimeoutExpired)
                    else 'Git unavailable: '+str(exc)[:120])
            return
        if root != self.root:
            return  # the project changed while git ran
        self.failures = 0
        if time.monotonic()-started > SLOW_SECONDS:
            self.wait = 20  # a slow repository: every 10 s
        if status is not None:
            status['changed_dirs'] = changed_dirs(status)
        tree = self.query_one(FileTree)
        edited_style = self.app.theme_variables.get('warning') or 'yellow'
        tree.status, tree.edited_style = status, edited_style
        if listed is not None:
            self.paths, self.truncated, self.children_of = set(listed[0]), listed[1], listed[2]
            self.load_tree()
            self.followed &= self.paths
        else:
            tree.mark(status, edited_style)
        if status != self.status:
            self.revision += 1  # the touched rows show the new counts and applied edits
        self.status = status
        line = self.query_one('#files-git', Static)
        if line.content != (text := git_line(status, self.truncated)):  # an update is a layout pass
            line.update(text)

    def load_tree(self):
        tree = self.query_one(FileTree)
        if self.changed_only and self.status is not None:
            changed = {*self.status['marks'], *(touch.path for touch in self.found if touch.edited)}
            dirs = self.status['dirs']
            tree.load(index(sorted(path for path in self.paths if path in changed or path.startswith(dirs))),
                      expand_all=True)
        else:
            tree.load(self.children_of)

    def action_changed(self):
        self.changed_only = not self.changed_only
        self.load_tree()

    def on_option_list_option_highlighted(self, event):
        index = event.option_index
        touch = self.found[index] if 0 <= index < len(self.found) else None
        hint = self.HINT
        if touch is not None and touch.failed:
            hint = (touch.reason or 'Not completed')+' · see the row in the conversation'
        self.query_one('#files-hint', Static).update(hint)

    def action_scope(self):
        self.turn_only = not self.turn_only
        self.tick()

    def selected(self):
        """The project path under the cursor of the focused list, or None."""
        focused = self.app.focused
        if isinstance(focused, FileTree):
            return (focused.cursor_node.data or (None,))[0] if focused.cursor_node is not None else None
        listing = self.query_one('#files-touched', OptionList)
        index = listing.highlighted
        touch = self.found[index] if index is not None and 0 <= index < len(self.found) else None
        return touch.project_path if touch is not None and not touch.outside else None

    def on_option_list_option_selected(self, event):
        event.stop()
        touch = self.found[event.option_index] if 0 <= event.option_index < len(self.found) else None
        if touch is not None and not touch.outside:
            self.query_one(FileTree).reveal(touch.project_path)
            self.open_file(touch.project_path, diff=False)

    def on_tree_node_selected(self, event):
        path, is_dir = event.node.data or (None, True)
        if path and not is_dir:
            event.stop()
            self.open_file(path, diff=False)

    def action_diff(self):
        if (path := self.selected()) is not None:
            self.open_file(path, diff=True)

    @work(group='files-open', exclusive=True)
    async def open_file(self, path, *, diff):
        """The file, or what changed in it, in the viewer F3 uses. Read in a thread; nothing reaches a model."""
        from .changes_panel import FileViewer
        try:
            content = await asyncio.to_thread(diff_text if diff else read_text, self.root, path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.app.notify(str(exc), severity='warning')
            return
        self.app.push_screen(FileViewer(path=path, content=content, notice=(
            'What changed since the last commit · captured when opened' if diff else
            'Current file content on disk · captured when opened')))

    def action_mention(self):
        """Attach the path as a context chip of the next message (the only way the pane reaches a model, and only
        as a reference the model may read)."""
        if (path := self.selected()) is not None and self.app.add_context(self.root, path):
            self.app.notify('@'+path+' will go with your next message.')

    def action_copy_path(self):
        if (path := self.selected()) is not None:
            self.app.copy_to_clipboard(path)
            self.app.notify('Copied '+path)

    def action_leave(self):
        self.app.focus_composer()

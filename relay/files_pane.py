"""Read-only files pane of the TUI (F8): what the agents read and edit, this turn or this session.

The rows come from the transcript's own retained tool activity (`assistant_history`), which every route and role
reaches through the same journal projection (relay/native_activity.py), so the pane has no hook per vendor and
always agrees with the transcript, also when a conversation is reopened. It sends nothing to a model.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


SCHEMA = 'tare.tools/console-files-pane/1'
FILE = 'files-pane.json'
FILE_OPERATIONS = {'read', 'edit', 'folder_read'}
FAILED = {'failed', 'refused', 'unknown', 'unconfirmed', 'not_started'}
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


def role_label(row):
    stage = (row.get('source') or {}).get('stage', '')
    phase = stage.rsplit(' · ', 1)[-1]  # "Work W-12 · Implement"
    return ROLES.get(phase, 'expl' if phase.startswith('Explore ') else phase)


def touches(history, *, turn_only):
    """One entry per file the agents read or edited, first touch first; an edit outranks a read and the latest row
    gives the state. A turn starts at the operator's message ('You: ...')."""
    rows = list(history)
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


def glyph(touch):
    return ('⟳' if touch.running else '!' if touch.failed else 'x' if touch.deleted else
            'p' if touch.proposed else 'e' if touch.edited else 'r')


def summary_text(found, *, turn_only):
    edited = sum(touch.edited for touch in found)
    return f"{'This turn' if turn_only else 'Session'} · read {len(found)-edited} · edited {edited}"


def label(touch, width):
    suffix = ' · '+touch.role if touch.role else ''
    room = max(8, width-4-len(suffix))
    path = touch.path if len(touch.path) <= room else '…'+touch.path[-(room-1):]
    text = Text(glyph(touch)+' ', style='bold' if touch.edited else '')
    text.append(path, style='dim' if touch.outside else 'yellow' if touch.edited and not touch.proposed else '')
    text.append(suffix, style='dim')
    return text


class FilesPane(Vertical):
    """The right pane. Display is set on it only (never a screen class); its tick runs only while shown."""

    BINDINGS = [Binding('t', 'scope', 'Turn/session', show=False), Binding('escape', 'leave', 'Back', show=False)]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.turn_only = True
        self.shown = ()
        self.found = []

    def compose(self):
        yield Static('Files', id='files-title', markup=False)
        yield Static('', id='files-summary', markup=False)
        yield OptionList(id='files-touched', markup=False)
        yield Static('t turn/session · Esc back', id='files-hint', markup=False)

    def on_mount(self):
        self.timer = self.set_interval(.5, self.tick, pause=True)

    def set_visible_state(self, shown):
        if shown:
            self.tick()
            self.timer.resume()
        else:
            self.timer.pause()

    def tick(self):
        app = self.app
        self.query_one('#files-title', Static).update('Files · '+Path(str(app.workspace_root)).name)
        found = touches(getattr(app, 'assistant_history', ()), turn_only=self.turn_only)
        width = max(16, self.size.width or 36)
        if (found, width, self.turn_only) == self.shown:
            return
        self.shown, self.found = (found, width, self.turn_only), found
        self.query_one('#files-summary', Static).update(summary_text(found, turn_only=self.turn_only))
        listing = self.query_one('#files-touched', OptionList)
        highlighted = listing.highlighted
        listing.clear_options()
        listing.add_options([Option(label(touch, width), id=touch.path) for touch in found])
        if found:
            listing.highlighted = min(highlighted or 0, len(found)-1)

    def on_option_list_option_highlighted(self, event):
        index = event.option_index
        touch = self.found[index] if 0 <= index < len(self.found) else None
        hint = 't turn/session · Esc back'
        if touch is not None and touch.failed:
            hint = (touch.reason or 'Not completed')+' · see the row in the conversation'
        self.query_one('#files-hint', Static).update(hint)

    def action_scope(self):
        self.turn_only = not self.turn_only
        self.tick()

    def action_leave(self):
        self.app.focus_composer()

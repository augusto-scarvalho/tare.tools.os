"""/guidance: read and change the guidance that reaches the models (relay/guidance.py, <storage>/serve/guidance.json)."""
import asyncio
import difflib

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Select, Static, TextArea

from .console_panels import SupportPanel

STATES = {'default': '', 'yours': ' · yours', 'removed': ' · removed', 'changed': ' · changed underneath',
          'new': ' · new'}


def compare(before, after, old, new):
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), old, new)) or 'No difference.'


def comparison(item):
    """Before × after for the selected item, in words a tool user reads."""
    if item['state'] == 'changed':
        return compare(item['base'], item['default'], 'the default you approved', 'the default now')
    if item['state'] in {'yours', 'removed'}:
        return compare(item['default'], item['text'] if item['state'] == 'yours' else '', 'default', 'yours')
    if item.get('previous'):
        return compare(item['previous'], item['default'], 'earlier reading', 'this reading')
    return ''


def describe(item):
    """Who wrote the text, where it goes, what can be done with it and when a change takes effect, in plain words."""
    origin = 'Added by the CLI on its own. ' + item['note'] if item['owner'] == 'vendor' else 'Written by tare.'
    if not item['controls']:
        how = 'Read-only.' if item['owner'] == 'tare' else 'The CLI offers no way to change it.'
    elif item['owner'] == 'tare':
        how = 'An edit reaches new sessions; sessions already running keep the text they started with.'
    else:
        how = ('You can ' + ' or '.join({'remove': 'remove it', 'replace': 'replace it'}[c] for c in item['controls'])
               + '; a change applies from the next turn.')
    state = {'yours': 'Your text is in use. ', 'removed': 'Removed: tare asks the CLI to leave it out. ',
             'new': 'New: the CLI started sending this; it goes out as the CLI writes it. ',
             'changed': ('The default changed since you chose. Your version is still the one sent. Keep yours, use '
                         'the new default, or edit and Save. ')}
    return f'{state.get(item["state"], "")}{origin} Sent as: {item["sent_as"]}. {how}'


def read_line():
    from .guidance import readings
    parts = []
    for value in readings().values():
        part = f'{value.get("cli")} {value.get("cli_version")} read {value.get("read_at")}'
        if value.get('error'):
            part += f'; could not read it again at {value.get("failed_at")}: {value["error"][:120]}'
        parts.append(part)
    return 'Vendor texts: ' + ('; '.join(parts) if parts else 'not read yet (tare reads them when the TUI opens).')


class GuidancePanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    GuidancePanel #guidance-text, GuidancePanel #guidance-diff { height: 1fr; }
    GuidancePanel #guidance-read, GuidancePanel #guidance-about, GuidancePanel #guidance-status { height: auto;
        max-height: 4; }
    '''

    def compose(self):
        from .guidance import items
        self.items = {item['id']: item for item in items()}
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Guidance sent to the models', classes='panel-title')
                yield Button('Back · Esc', id='guidance-close')
            yield Static(read_line(), id='guidance-read', markup=False)
            yield Select(self.options(), value=next(iter(self.items)), allow_blank=False, id='guidance-item')
            yield Static('', id='guidance-about', markup=False)
            yield TextArea('', soft_wrap=True, id='guidance-text')
            yield TextArea('', soft_wrap=True, read_only=True, id='guidance-diff')
            yield Static('Pick a text to read it. Role guidance changes reach new sessions; vendor blocks change '
                         'from the next turn.', id='guidance-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Save', id='guidance-save', classes='panel-primary')
                yield Button('Remove', id='guidance-remove')
                yield Button('Keep mine', id='guidance-keep')
                yield Button("tare's default", id='guidance-default')
                yield Button('Reset everything', id='guidance-reset-all', classes='panel-danger')

    def options(self):
        return [(item['title'] + STATES[item['state']], key) for key, item in self.items.items()]

    def on_mount(self):
        self.watch(self.query_one('#guidance-item', Select), 'value', self.show)

    def show(self):
        item = self.items.get(self.query_one('#guidance-item', Select).value)
        if item is None:
            return
        self.query_one('#guidance-about', Static).update(describe(item))
        text = self.query_one('#guidance-text', TextArea)
        text.load_text(item['text'])
        text.read_only = not {'edit', 'replace'} & set(item['controls'])
        diff = self.query_one('#guidance-diff', TextArea)
        diff.load_text(comparison(item))
        diff.display = bool(diff.text)
        self.query_one('#guidance-save', Button).disabled = text.read_only
        self.query_one('#guidance-remove', Button).disabled = ('remove' not in item['controls']
                                                               or item['state'] == 'removed')
        self.query_one('#guidance-keep', Button).disabled = item['state'] != 'changed'
        default = self.query_one('#guidance-default', Button)
        default.label = ('Use the new default' if item['state'] == 'changed' else
                         "tare's default" if item['owner'] == 'tare' else "Vendor's default")
        default.disabled = item['state'] in {'default', 'new'}

    async def on_button_pressed(self, event):
        event.stop()
        from .guidance import items, keep, reset, save
        if event.button.id == 'guidance-close':
            self.action_close()
            return
        choice = self.query_one('#guidance-item', Select)
        key = choice.value
        item = self.items[key]
        later = 'New sessions use it.' if item['owner'] == 'tare' else 'It applies from the next turn.'
        try:
            if event.button.id == 'guidance-save':
                await asyncio.to_thread(save, key, self.query_one('#guidance-text', TextArea).text)
                message = 'Saved. ' + later
            elif event.button.id == 'guidance-remove':
                await asyncio.to_thread(save, key, None)
                message = 'Removed. ' + later
            elif event.button.id == 'guidance-keep':
                await asyncio.to_thread(keep, key)
                message = 'Kept your version over the new default.'
            elif event.button.id == 'guidance-default':
                await asyncio.to_thread(reset, key)
                message = ("Back to tare's default. " if item['owner'] == 'tare' else
                           "Back to the vendor's default. ") + later
            else:
                await asyncio.to_thread(reset)
                message = "Every text is back to its default: tare's for role guidance, the vendor's for vendor blocks."
            self.items = {row['id']: row for row in await asyncio.to_thread(items)}
            with choice.prevent(Select.Changed):
                choice.set_options(self.options())
                choice.value = key
            self.show()
        except (ValueError, OSError) as exc:
            message = str(exc)
        if self.is_mounted:
            self.query_one('#guidance-status', Static).update(message)

    def action_close(self):
        self.dismiss(None)

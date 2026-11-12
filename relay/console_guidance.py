"""/guidance: read and change the guidance that reaches the models (relay/guidance.py, <storage>/serve/guidance.json)."""
import asyncio
import difflib

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .console_panels import SupportPanel

# The list's groups, in this order; a CLI heading carries the version its texts were read from.
GROUPS = {'role': 'tare · role guidance', 'protocol': 'tare · protocol texts (read-only)',
          'cli://claude': 'Claude Code', 'cli://codex': 'Codex', 'cli://agy': 'AGY'}
CLIS = ('cli://claude', 'cli://codex', 'cli://agy')
# As the Files pane (F8) marks a changed file: the name in a theme colour, then one bold letter. M and D as git
# says them for a file; C for a default that changed under the owner's choice; U, as untracked, for a block the
# owner has not seen yet; T for a block tare leaves out by default. M also marks a block the owner sends again over
# tare's default: either way the text sent is the owner's choice. (theme variable, fallback colour)
MARKS = {'yours': ('M', 'warning', 'yellow'), 'kept': ('M', 'warning', 'yellow'), 'removed': ('D', 'error', 'red'),
         'tare-default': ('T', 'primary', 'blue'), 'changed': ('C', 'accent', 'magenta'),
         'new': ('U', 'success', 'green')}
LEGEND = ('M your choice · D removed · T left out by tare · C default changed under your edit · U new · '
          'grey: not in the latest reading')


def compare(before, after, old, new):
    # lineterm='': a text without a final newline would otherwise glue its last line to the next diff line
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), old, new, lineterm='')
    return '\n'.join(lines) or 'No difference.'


def comparison(item):
    """Before × after for the selected item, in words a tool user reads."""
    if item['state'] == 'changed':
        return compare(item['base'], item['default'], 'the default you approved', 'the default now')
    if item['state'] in {'yours', 'removed'}:
        return compare(item['default'], item['text'] if item['state'] == 'yours' else '', 'default', 'yours')
    if item['state'] == 'tare-default':  # what tare leaves out
        return compare(item['default'], '', "the CLI's text", 'sent by tare')
    if item.get('previous'):
        return compare(item['previous'], item['default'], 'earlier reading', 'this reading')
    return ''


def describe(item):
    """Who wrote the text, where it goes, what can be done with it and when a change takes effect, in plain words."""
    origin = 'Added by the CLI on its own.' if item['owner'] == 'vendor' else 'Written by tare.'
    if not item['controls']:
        how = 'Read-only.' if item['owner'] == 'tare' else 'The CLI offers no way to change it.'
    elif item['owner'] == 'tare':
        how = 'An edit reaches new sessions; sessions already running keep the text they started with.'
    else:
        how = ('You can ' + ' or '.join({'remove': 'remove it', 'replace': 'replace it'}[c] for c in item['controls'])
               + '; a change applies from the next turn.'
               + (' Keep placeholders such as <date> where you want the real value: tare fills them when it sends'
                  ' your text.' if 'replace' in item['controls'] else ''))
    state = {'yours': 'Your text is in use. ', 'removed': 'Removed: tare asks the CLI to leave it out. ',
             'new': 'New: the CLI started sending this; it goes out as the CLI writes it. ',
             'changed': ('The default changed since you chose. Your version is still the one sent. Keep yours, use '
                         'the new default, or edit and Save. ')}
    if item.get('tare_default'):  # a block tare leaves out by default
        state.update({
            'tare-default': (f"Left out by tare's default: {item['tare_default']} \"Vendor's default\" sends it "
                             'again. '),
            'kept': "You send it as the CLI writes it, over tare's default; \"tare's default\" leaves it out again. "})
        if not item.get('tare_proved') and item['state'] == 'default':
            state['default'] = (f'tare leaves this out by default, but could not check that {item.get("cli")} '
                                'leaves it out when asked, so it is still sent. ')
    # The CLI's long note comes last: this area shows a few lines, and what to do must not be the part cut off.
    note = ' ' + item['note'] if item['owner'] == 'vendor' and item.get('note') else ''
    seen = ' ' + item['seen'] if item.get('seen') else ''
    return f'{state.get(item["state"], "")}{origin}{seen} Sent as: {item["sent_as"]}. {how}{note}'


def group_lines(endpoint, value, reading):
    """Under a CLI's heading: how and when its texts were read, or that they are being read now, in plain words."""
    if reading:
        return ['Being read now: its blocks appear here when the reading is done.']
    if not value:
        return ['Not read yet: tare reads it when the TUI opens.']
    real, lines = value.get('real') or {}, []
    if endpoint == 'cli://agy':
        from .guidance import starting_point
        start = starting_point()
        lines.append(f'Read in a real call {real["read_at"]} (AGY {real.get("cli_version")}).' if real.get('read_at')
                     else f'Shown as AGY {start["cli_version"]} sent it on {start["read_at"]}, until tare reads yours.')
    else:
        lines.append(f'Read without tokens {value.get("read_at")}'
                     + (f', in a real call {real["read_at"]} ({real.get("cli_version")}).' if real.get('read_at') else '.'))
    if value.get('error'):
        lines.append(f'Could not read it again at {value.get("failed_at")}: {value["error"][:100]}. Showing the last '
                     'reading.')
    if real.get('error'):
        lines.append(f'The real call could not be made at {real.get("failed_at")}: {real["error"][:100]}.')
    return lines


class GuidancePanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    GuidancePanel #guidance-body { height: 1fr; }
    GuidancePanel #guidance-item { width: 2fr; height: 1fr; }
    GuidancePanel #guidance-detail { width: 3fr; height: 1fr; }
    GuidancePanel #guidance-text, GuidancePanel #guidance-diff { height: 1fr; }
    GuidancePanel #guidance-read, GuidancePanel #guidance-about, GuidancePanel #guidance-status { height: auto;
        max-height: 4; }
    GuidancePanel #guidance-default { width: 24; }  /* fits "Use the new default": a changed label does not widen it */
    '''

    def compose(self):
        self.items, self.current = {}, None
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Guidance sent to the models', classes='panel-title')
                yield Button('Back · Esc', id='guidance-close')
            yield Static('', id='guidance-read', markup=False)
            with Horizontal(id='guidance-body'):
                yield OptionList(id='guidance-item')
                with Vertical(id='guidance-detail'):
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

    def on_mount(self):
        self.reload()

    def reload(self, keep_text=True):
        """List every text again (the app calls this when the startup reading lands), keeping the selection and,
        unless asked not to, text typed and not saved yet."""
        from .guidance import items, readings
        editor = self.query_one('#guidance-text', TextArea)
        typed = (editor.text if keep_text and self.current in self.items
                 and editor.text != self.items[self.current]['text'] else None)
        self.items = {row['id']: row for row in items()}
        reading = bool(getattr(self.app, 'guidance_reading', False))
        self.query_one('#guidance-read', Static).update(
            LEGEND + ('. Reading what each CLI adds now; it fills in by itself.' if reading else '.'))
        options = self.query_one('#guidance-item', OptionList)
        options.clear_options()
        options.add_options(self.options(readings(), reading))
        listed = [k for k, row in self.items.items() if not (reading and row['group'] in CLIS)]
        self.select(self.current if self.current in listed else listed[0])
        if typed is not None:
            editor.load_text(typed)

    def options(self, read, reading):
        """Each group's heading (not selectable), then its texts; a CLI's texts only once its reading is done."""
        groups = {'role', 'protocol', *(CLIS if reading else ()), *(row['group'] for row in self.items.values())}
        entries = []
        for group in sorted(groups, key=lambda g: list(GROUPS).index(g) if g in GROUPS else len(GROUPS)):
            value = read.get(group) or {}
            version = f' {value["cli_version"]}' if group in CLIS and value.get('cli_version') and not reading else ''
            entries.append(Option(Text(GROUPS.get(group, group) + version, style='bold'), id='group#' + group,
                                  disabled=True))
            if group in CLIS:
                entries += [Option(Text('  ' + line, style='dim'), id=f'note#{group}#{n}', disabled=True)
                            for n, line in enumerate(group_lines(group, value, reading))]
                if reading:
                    continue
            entries += [Option(self.label(row), id=key) for key, row in self.items.items() if row['group'] == group]
        return entries

    def label(self, row):
        """A text's title as the Files pane shows a changed file: in the state's colour, its letter after it."""
        text = Text('  ' + row['title'], style='dim' if row.get('absent') else '')
        if row['state'] in MARKS:
            letter, variable, fallback = MARKS[row['state']]
            colour = self.app.theme_variables.get(variable) or fallback
            text.stylize(colour, 2)
            text.append('  ' + letter, style='bold ' + colour)
        return text

    def select(self, key):
        self.current = key
        options = self.query_one('#guidance-item', OptionList)
        options.highlighted = options.get_option_index(key)
        self.show()

    def on_option_list_option_highlighted(self, event):
        if event.option.id in self.items and event.option.id != self.current:
            self.current = event.option.id
            self.show()

    def show(self):
        item = self.items.get(self.current)
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
                                                               or item['state'] in {'removed', 'tare-default'})
        self.query_one('#guidance-keep', Button).disabled = item['state'] != 'changed'
        default = self.query_one('#guidance-default', Button)
        # Back to the default: tare's own, also for a block tare leaves out; from there, the CLI's text again.
        default.label = ('Use the new default' if item['state'] == 'changed' else
                         "Vendor's default" if item['state'] == 'tare-default' else
                         "tare's default" if item['owner'] == 'tare' or item.get('tare_default')
                         and item.get('tare_proved') else "Vendor's default")
        default.disabled = item['state'] in {'default', 'new'}

    async def on_button_pressed(self, event):
        event.stop()
        from .guidance import keep, reset, save, send_again
        if event.button.id == 'guidance-close':
            self.action_close()
            return
        key = self.current
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
            elif event.button.id == 'guidance-default' and item['state'] == 'tare-default':
                await asyncio.to_thread(send_again, key)
                message = 'Sent again as the CLI writes it. ' + later
            elif event.button.id == 'guidance-default':
                await asyncio.to_thread(reset, key)
                message = ("Back to tare's default. " if item['owner'] == 'tare' or item.get('tare_default') else
                           "Back to the vendor's default. ") + later
            else:
                await asyncio.to_thread(reset)
                message = ("Every text is back to its default: tare's for role guidance and the blocks tare leaves "
                           "out, the vendor's for the other vendor blocks.")
            self.reload(keep_text=False)
        except (ValueError, OSError) as exc:
            message = str(exc)
        if self.is_mounted:
            self.query_one('#guidance-status', Static).update(message)

    def action_close(self):
        self.dismiss(None)

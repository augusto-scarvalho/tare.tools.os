"""Bounded conversation rendering and native subprocess observation."""
import asyncio
import json
import re
import time

from rich.text import Text
from rich.cells import cell_len
from rich.markdown import Markdown
from rich.style import Style
from rich.syntax import SyntaxTheme
from rich.theme import Theme as RichTheme
from pygments.token import Token
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import work
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Static
from .native_activity import OPERATIONS, activity_key, activity_view, stopped_activity


def plain(value):
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(value))
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)


def show(widget, value, *, layout=True):
    """Update a Static only when its content changes. Textual relayouts the whole screen on every update, and the
    composer hint, status and header were rewritten with the same text on each keystroke or timer tick. A line of
    fixed size passes layout=False."""
    if getattr(widget, '_tare_shown', None) == value:
        return
    widget._tare_shown = value
    widget.update(value, layout=layout)


def settled(source):
    """Length of the finished Markdown blocks at the start of source: up to the last blank line outside a code
    fence that a complete unindented line follows. Never after a rule (Rich puts no blank line after one) nor
    before a list item (items apart are still one list)."""
    end = offset = 0
    fenced, previous = False, ''
    lines = source.split('\n')
    for i, line in enumerate(lines[:-2]):
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        offset += len(line) + 1
        if line.strip():
            previous = line
        elif (not fenced and lines[i+1][:1] not in ('', ' ', '\t') and set(previous) - set('-*_ ')
              and not re.match(r'([-*+]|\d+[.)])(\s|$)', lines[i+1])):
            end = offset
    return end


def joined(head, part):
    """Two consecutive Markdown renders as one: Rich puts one blank line between top-level blocks, and a list
    rendered on its own already starts with it."""
    if not head.plain or not part.plain or part.plain.startswith('\n'):
        return head + part
    return head + '\n' + part


class SelectableMessage(Static):
    def get_selection(self, selection):
        # Textual 6's extractor omits the final selected line. Keep identical
        # copy semantics on both supported versions, including whole messages.
        lines = str(self._render()).splitlines()
        if not lines:
            return '', '\n'
        start = selection.start
        end = selection.end
        first, left = (start.y, start.x) if start is not None else (0, 0)
        last, right = (end.y, end.x) if end is not None else (len(lines)-1, len(lines[-1]))
        if first >= len(lines):
            return '', '\n'
        if last >= len(lines):
            last, right = len(lines)-1, len(lines[-1])
        if first == last:
            return lines[first][left:right], '\n'
        return '\n'.join([lines[first][left:], *lines[first+1:last], lines[last][:right]]), '\n'


class ConsoleSyntaxTheme(SyntaxTheme):
    """Keep highlighted code on the same surface as the surrounding console."""

    def __init__(self, theme):
        self.background = Style(color=theme.foreground, bgcolor=theme.panel)
        contrast = theme.name == 'tare-contrast'
        colors = ('#a6c8ff', '#b2d6a1', '#e4c08c', '#a4afba') if theme.dark else (
                  '#334ea0', '#356a36', '#8b531b', '#596671')
        keyword, string, number, comment = colors
        self.styles = {
            Token.Keyword: Style(color=theme.primary if contrast else keyword, bold=contrast),
            Token.Name.Function: Style(color=theme.primary),
            Token.Name.Class: Style(color=theme.primary, bold=True),
            Token.Literal.String: Style(color=theme.foreground if contrast else string),
            Token.Literal.Number: Style(color=theme.foreground if contrast else number),
            Token.Comment: Style(color=theme.foreground if contrast else comment, italic=True),
            Token.Generic.Error: Style(color=theme.error, underline=True),
        }

    def get_background_style(self):
        return self.background

    def get_style_for_token(self, token_type):
        while token_type:
            if token_type in self.styles:
                return self.background + self.styles[token_type]
            token_type = token_type.parent
        return self.background


class AssistantMessage(SelectableMessage):
    """Selectable Markdown; source URLs and images never trigger a fetch."""

    def __init__(self, source='', *, label='tare', continues=False, **kwargs):
        super().__init__('', markup=False, **kwargs)
        self.source, self.label = source, label
        self.continues = continues  # follows another piece of a StreamedReply: starts with the blank line between
        self.render_width = None
        self.rendered = (None, '', Text())  # (width and theme, finished source prefix, its rendered Markdown)

    def on_mount(self):
        self.app.theme_changed_signal.subscribe(self, self.refresh_reply)
        # Nested results can get their first layout before a deferred callback or
        # Resize notification paints them. Render once now; later layout still
        # rewraps at the message's exact width instead of leaving a blank answer.
        width = self.content_size.width
        if not width:
            width = self.app.size.width
            for node in self.ancestors:
                if isinstance(node, Widget) and node.content_size.width:
                    width = node.content_size.width
                    break
            width = max(1, width-self.gutter.width)
        self.refresh_reply(width=width)
        self.call_after_refresh(self.refresh_reply)

    def update_reply(self, source, label):
        self.source, self.label = source, label
        if self.is_mounted:
            self.refresh_reply()

    def on_resize(self, event):
        if self.content_size.width != self.render_width:
            self.refresh_reply()

    def refresh_reply(self, _theme=None, *, width=None):
        width = width or self.content_size.width  # a width lets a new piece of a StreamedReply draw before mounting
        if width < 1:
            return  # Nested results need their first layout before Markdown can wrap.
        self.render_width = width
        theme = self.app.current_theme
        # Textual selection operates on Text, not arbitrary Rich renderables.
        # Keep rendered styles while exposing the same visible text for copying.
        options = self.app.console.options.update(width=width, height=None)

        def draw(renderable):
            text = Text()
            for segment in self.app.console.render(renderable, options):
                if not segment.control:
                    text.append(segment.text, segment.style)
            return text

        def markdown(source):
            return draw(Markdown(source, hyperlinks=False, code_theme=ConsoleSyntaxTheme(theme)))

        styles = {f'markdown.h{i}': Style(color=theme.foreground, bold=True) for i in range(1,7)}
        styles.update({'markdown.code': Style(color=theme.primary, bgcolor=theme.panel, bold=True),
                       'markdown.link': Style(color=theme.primary, underline=True),
                       'markdown.link_url': Style(color=theme.primary)})
        with self.app.console.use_theme(RichTheme(styles)):
            # A streamed reply re-rendered all of its Markdown every 80 ms. Finished blocks now render once and
            # each tick renders only the tail; block by block gives the same Text as the whole source.
            key, done, finished = self.rendered
            if key != (width, theme.name) or not self.source.startswith(done):
                done, finished = '', Text()
            if cut := settled(self.source[len(done):]):
                finished = joined(finished, markdown(self.source[len(done):len(done)+cut]))
                done = self.source[:len(done)+cut]
            self.rendered = ((width, theme.name), done, finished)
            body = joined(finished, markdown(self.source[len(done):]))
            if self.continues and body.plain and not body.plain.startswith('\n'):
                body = Text('\n') + body  # the blank line between blocks
            label = draw(Text(self.label, style=Style(color=theme.primary))) if self.label else Text()
        self.update(label + body)


class StreamedReply(Vertical):
    """A reply while it streams. Textual repaints a changed widget whole, so one growing message cost O(reply) of
    layout and paint every 80 ms tick (18 ms at 1 KB, 99 ms at 9 KB). Finished Markdown blocks stay in pieces that
    stop changing, and each tick redraws only the tail. The finished reply is one message again."""
    DEFAULT_CSS = 'StreamedReply { height: auto; }'
    SEAL = 1000  # ponytail: characters of finished blocks worth a new piece

    def __init__(self, label, **kwargs):
        super().__init__(**kwargs)
        self.pieces, self.tail, self.windowed = [], AssistantMessage(label=label), False

    def compose(self):
        yield self.tail

    def update_reply(self, source, label, final=False):
        done = ''.join(piece.source for piece in self.pieces)
        if final or not source.startswith(done):
            # The reply ended, or the transcript's 16000-character window moved past its start.
            self.windowed = self.windowed or not final
            for piece in self.pieces:
                piece.remove()
            self.pieces, done = [], ''
        rest = source[len(done):]
        if not final and not self.windowed and self.tail.render_width and (cut := settled(rest)) >= self.SEAL:
            # The tail keeps the finished blocks; a new tail, drawn before it mounts so no frame goes blank,
            # takes the rest.
            self.tail.update_reply(rest[:cut], '' if self.pieces else label)
            self.pieces.append(self.tail)
            done, rest = done+rest[:cut], rest[cut:]
            self.tail = AssistantMessage(rest, label='', continues=True)
            self.tail.refresh_reply(width=self.pieces[-1].render_width)
            self.mount(self.tail)
        self.tail.continues = bool(self.pieces)
        self.tail.update_reply(rest, '' if self.pieces else label)


class ExecutionResult(Vertical):
    """An answer, visible evidence summary and selectable execution details."""

    def __init__(self, value):
        super().__init__(classes='execution-result')
        self.value = value

    def compose(self):
        value = self.value
        if value.get('body'):
            yield AssistantMessage(plain(value['body']), classes='message reply')
            from .console_images import ImagePreview, image_paths
            for path in image_paths(value['body'], getattr(self.app, 'workspace_root', None)):
                yield ImagePreview(path)
        yield SelectableMessage(plain(value.get('notice', '')), markup=False,
                                classes='result-notice'+(' attention' if value.get('attention') else ''))
        yield Collapsible(SelectableMessage(plain(value.get('details', '')), markup=False),
                          Button('Copy details', classes='copy-result-details'),
                          Button('Comment on response', classes='comment-result', disabled=not bool(value.get('body'))),
                          title='Execution details', collapsed=True, classes='activity result-details')

    def on_button_pressed(self, event):
        if event.button.has_class('comment-result'):
            event.stop()
            from .console_workbench import artifact
            self.app.open_artifact(artifact('Captured response', self.value.get('body', '')))
        if event.button.has_class('copy-result-details'):
            event.stop()
            self.app.copy_to_clipboard(plain(self.value.get('details', '')))
            event.button.label = 'Copied'


class ToolActivity(Collapsible):
    """One recorded tool invocation, updated in place; tool success is not task acceptance."""

    def __init__(self, value, *, live=False):
        self.value = activity_view(value) if live else stopped_activity(value)
        self.observed_at = time.monotonic()
        self.full_text = None
        self.elapsed_timer = None
        super().__init__(SelectableMessage('', markup=False, classes='tool-detail'),
                         Collapsible(SelectableMessage('', markup=False, classes='tool-receipt'),
                                     title='Recorded details', collapsed=True, classes='activity'),
                         Horizontal(Button('Full output', classes='tool-full-output'),
                                    Button('Copy output', classes='copy-tool-output'), classes='tool-actions'),
                         title='', collapsed=not (live and self.attention), classes='activity tool-activity')

    @property
    def attention(self):
        if self.value.get('agent_status') == 'PAUSED':
            return False
        # A refusal steers the model (route first, fix an argument) and it corrects itself: the row says
        # "Refused" but stays closed instead of opening its raw JSON in the conversation.
        return self.value['state'] in {'failed', 'unknown', 'unconfirmed', 'not_started'}

    def on_mount(self):
        self.refresh_activity()

    def refresh_title(self):
        value = self.value
        state = {'running': 'Awaiting result', 'succeeded': 'Returned' if value['operation'].startswith('terminal_') else 'Done',
                 'failed': 'Failed', 'refused': 'Refused', 'unknown': 'Effects uncertain',
                 'unconfirmed': 'Result unconfirmed', 'not_started': 'Not started'}[value['state']]
        duration = (f" · {value['duration_ms']/1000:.1f}s" if value['duration_ms'] is not None else
                    f' · ~{time.monotonic()-self.observed_at:.0f}s' if value['state'] == 'running' else '')
        target = ' '.join(value['target'].split())
        operation = OPERATIONS[value['operation']]
        if value['operation'] == 'agent_run' and value.get('actor'):
            state = {'PROPOSED': 'Result ready', 'PAUSED': 'Paused', 'CANCELLED': 'Cancelled', 'BLOCKED': 'Blocked',
                     'EXHAUSTED': 'Budget reached', 'UNCONFIRMED': 'Result unconfirmed'}.get(value.get('agent_status'), state)
            from .execution_team import actor_label
            operation = actor_label(value['actor'])+' · '+value['actor']['connection']
            # Identity stays visible when the mission is long; the full mission is in the expanded row.
            label = Text(operation)
            label.truncate(max(16, (self.size.width or 80)-cell_len(state+duration)-8), overflow='ellipsis')
            operation, target = label.plain, ''
        if value.get('source') and value['operation'] != 'agent_run':
            # Leave room for the result state, timing and target even with wide
            # Unicode Work names. The complete stage stays in recorded details.
            stage = Text(value['source']['stage'])
            stage.truncate(max(0, min(30, (self.size.width or 80)-cell_len(operation+state+duration)-19)),
                           overflow='ellipsis')
            if stage.plain:
                operation = stage.plain+' · '+operation
        room = max(1, min(70, (self.size.width or 80)-cell_len(operation+state+duration)-8))
        if cell_len(target) > room and value['operation'] == 'explore':
            shown = Text(target)  # a question reads from its start; a path from its end
            shown.truncate(room, overflow='ellipsis')
            target = shown.plain
        elif cell_len(target) > room:
            suffix, width = '', 0
            for char in reversed(target):
                width += cell_len(char)
                if width > room-1:
                    break
                suffix = char+suffix
            target = '…'+suffix
        self.title = operation+(' '+target if target else '')+' · '+state+duration

    def on_resize(self):
        self.refresh_title()

    def refresh_activity(self):
        if not self.query('.tool-detail'):
            return  # A burst or history clear may prune this row during mounting.
        if self.value['state'] == 'running' and self.elapsed_timer is None:
            self.elapsed_timer = self.set_interval(1, self.refresh_title)
        elif self.value['state'] != 'running' and self.elapsed_timer is not None:
            self.elapsed_timer.stop()
            self.elapsed_timer = None
        self.refresh_title()
        self.set_class(self.attention, 'tool-attention')
        value = self.value
        metadata = (f"Tool: {value['operation']}\nTarget: {value['target']}\nRecorded event: {value['receipt']}\n"
                    'Timing spans the observed request and result/stop record; it is not CPU time.')
        if value.get('actor'):
            from .execution_team import actor_label
            person = value['actor']
            metadata = (actor_label(person)+'\nConnection: '+person['connection']+'\nRoute: '+person['route']+
                        '\nRequested model: '+person['model']+'\nRequested reasoning: '+person['reasoning']+'\n'+metadata)
        if value.get('source'):
            metadata = 'Stage: '+value['source']['stage']+'\nSession: '+value['source']['session']+'\n'+metadata
        detail = (self.full_text if self.full_text is not None else value['detail'])
        if value['operation'] == 'agent_run':
            detail = 'Mission: '+value['target']+'\n'+detail
        if value['truncated'] and self.full_text is None:
            detail += '\n\nExcerpt only. Full output is available after this turn stops.'
        self.query_one('.tool-detail', Static).update(plain(detail))
        self.query_one('.tool-receipt', Static).update(plain(metadata))
        self.query_one('.tool-full-output').display = (value['operation'] != 'agent_run' and
            value['state'] not in {'running', 'unconfirmed'} and self.full_text is None)
        self.query_one('.copy-tool-output', Button).label = ('Copy excerpt' if value['truncated'] and self.full_text is None
                                                            else 'Copy output')

    def update_activity(self, value):
        value = activity_view(value)
        if value == self.value:
            return
        previous = self.value['state']
        self.value = value
        if self.is_mounted:
            self.refresh_activity()
        if previous != self.value['state'] and self.attention:
            self.collapsed = False

    def on_button_pressed(self, event):
        event.stop()
        if event.button.has_class('copy-tool-output'):
            self.app.copy_to_clipboard(plain(self.full_text if self.full_text is not None else self.value['detail']))
            event.button.label = 'Copied'
        elif event.button.has_class('tool-full-output'):
            if self.app.busy:
                self.app.notify('Full recorded output is available after this turn stops.')
            else:
                self.load_output(self.value.get('source', {}).get('session') or
                                 self.app.assistant_session or self.app.selection().session, self.app.workspace_root)

    @work(exclusive=True, group='tool-output')
    async def load_output(self, session, project):
        from .native_activity import full_output
        try:
            value = await asyncio.to_thread(full_output, session, project, self.value)
            if self.is_mounted:
                self.full_text = value
                self.refresh_activity()
        except Exception as exc:
            if self.is_mounted:
                self.app.notify('Tool output unavailable: '+str(exc), severity='error')


DELIVERY_LABELS = {
    'SAVED': 'Saved',
    'RECEIVED': 'Received',
    'RESPONSE_READY': 'Response ready',
}


class MessageDelivery(Collapsible):
    """One queued message delivery observation, updated in place."""

    def __init__(self, item):
        self.item = dict(item)
        self.message_id = item['message_id']
        self.error = None
        self.detail_widget = SelectableMessage('', markup=False, classes='message-delivery-detail')
        super().__init__(self.detail_widget, title=self._compute_title(), collapsed=True, classes='activity message-delivery')
        self._refresh_content()

    @property
    def delivery_status(self):
        return self.item.get('delivery', {}).get('status', 'SAVED')

    @property
    def delivery_label(self):
        return DELIVERY_LABELS.get(self.delivery_status, self.delivery_status)

    def _compute_title(self):
        text = plain(self.item.get('text', ''))
        clean = ' '.join(text.split()) if text else ''
        label = self.delivery_label + (' · observation unavailable' if self.error else '')
        if clean:
            snippet = clean[:24] + ('…' if len(clean) > 24 else '')
            return f'{label} · {snippet}'
        return label

    def _refresh_content(self):
        item = self.item
        delivery = item.get('delivery', {})
        status = delivery.get('status', 'SAVED')
        label = self.delivery_label

        lines = [
            f"Delivery state: {label}",
            f"Message ID: {self.message_id}",
        ]
        if item.get('text'):
            lines.append(f"Text: {item['text']}")
        if item.get('attachments'):
            lines.append(f"Attachments: {len(item['attachments'])} file(s)")

        if status == 'SAVED':
            lines.append("Status details: Message saved in inbox; waiting for next complete round or Resume.")
        elif status == 'RECEIVED':
            seq = delivery.get('receipt_sequence')
            lines.append(f"Status details: Message received into context at sequence {seq}. Task understanding or answer not confirmed.")
        elif status == 'RESPONSE_READY':
            call_id = delivery.get('call_id')
            term_seq = delivery.get('terminal_sequence')
            lines.append(
                f"Status details: Completed response recorded after receipt (call {call_id}, terminal sequence {term_seq}). "
                "Response ready confirms a completed response was recorded after receipt; "
                "does not claim the task is correct or semantically answered."
            )

        if delivery.get('receipt_sequence') is not None and status != 'RECEIVED':
            lines.append(f"Receipt sequence: {delivery['receipt_sequence']}")
        if delivery.get('request_sequence') is not None:
            lines.append(f"Request sequence: {delivery['request_sequence']}")
        if delivery.get('response_sequence') is not None:
            lines.append(f"Response sequence: {delivery['response_sequence']}")
        if delivery.get('terminal_sequence') is not None and status != 'RESPONSE_READY':
            lines.append(f"Terminal sequence: {delivery['terminal_sequence']}")

        if self.error:
            lines.append(f"Observation note: {self.error}")

        self.detail_widget.update(plain('\n'.join(lines)))

    def update_observation(self, item):
        self.item = dict(item)
        self.error = None
        self.title = self._compute_title()
        self._refresh_content()

    def set_error(self, message):
        self.error = str(message)
        self.title = self._compute_title()
        self._refresh_content()


def welcome_text(note=''):
    """A new conversation's welcome; note is a lasting condition of the project worth saying before it starts."""
    welcome = Text('What are we working on?\n', style='bold')
    welcome.append('\nAsk about the code, fix a bug, or build something new.\n', style='not bold')
    if note:
        welcome.append('\n'+note+'\n', style='not bold italic')
    return welcome


class Transcript(VerticalScroll):
    """Messages stay selectable and do not pull the reader away from older output."""
    DEFAULT_CSS = '''
    Transcript { height: 1fr; min-height: 5; padding: 0; scrollbar-size: 1 1; }
    Transcript .transcript-body { height: auto; min-height: 100%; }
    Transcript .message { height: auto; margin: 1 0; padding: 0 1; background: transparent; }
    Transcript .operator { border-left: solid $primary 45%; padding: 0 1; background: $panel; }
    Transcript .reply, Transcript .partial { border: none; }
    Transcript .activity { height: auto; margin: 0; padding: 0; border: none; background: transparent; }
    Transcript .activity > CollapsibleTitle { color: $text-muted; padding: 0 1; text-style: none; }
    Transcript .activity > CollapsibleTitle:hover { color: $foreground; background: $panel; }
    Transcript .activity > CollapsibleTitle:focus { color: $background; background: $primary; text-style: bold; }
    Transcript .activity > Contents { padding: 0 1 0 3; }
    Transcript .activity Static { height: auto; }
    Transcript .tool-attention > CollapsibleTitle { color: $warning; }
    Transcript .tool-actions { height: 1; margin: 0; }
    Transcript .tool-activity Button { height: 1; min-height: 1; min-width: 16; width: auto; border: none; padding: 0 1; margin: 0 1 0 0; background: transparent; color: $text-muted; text-style: none; }
    Transcript .tool-activity Button:focus, Transcript .tool-activity Button:hover { background: $primary; color: $background; }
    Transcript .message-delivery > CollapsibleTitle { color: $text-muted; }
    Transcript .message-delivery-detail { height: auto; color: $text-muted; }
    Transcript .execution-result { height: auto; margin: 0 0 1 0; }
    Transcript .result-notice { height: auto; padding: 0 1; color: $text-muted; }
    Transcript .result-notice.attention { color: $warning; }
    Transcript .copy-result-details { height: 1; min-height: 1; min-width: 0; width: auto; border: none; padding: 0 1; margin: 1 0 0 0; background: $primary 15%; color: $primary; }
    Transcript .copy-result-details:focus, Transcript .copy-result-details:hover { background: $primary; color: $background; }
    Transcript #welcome { height: auto; margin: 2 0; content-align: left top; color: $foreground; border: none; }
    '''

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.items = []
        self.tool_rows = {}
        self.message_rows = {}
        self.generation = 0
        self.delivery_note = None
        self.live = None
        self.live_text = ''
        self.live_key = None
        self.summary_key = None
        self.live_dirty = False
        self.completed_stream = None

    def compose(self):
        welcome = welcome_text()
        # Fill the viewport so anchoring never places short content below a gap.
        # Once messages overflow, the same anchor follows their natural height.
        with Vertical(classes='transcript-body'):
            yield Static(welcome, id='welcome', classes='message')

    def on_mount(self):
        # Textual tracks layout growth and releases the anchor when the reader
        # scrolls away. This also covers nested Markdown mounting after a turn.
        self.anchor()
        self.set_interval(.08, self.flush_live)

    def append(self, widget):
        self.query_one('#welcome').display = False
        self.items.append(widget)
        self.query_one('.transcript-body', Vertical).mount(widget)
        def category(item):
            return 'tool' if isinstance(item, ToolActivity) else 'delivery' if isinstance(item, MessageDelivery) else 'message'
        kind = category(widget)
        matching = [item for item in self.items if category(item) == kind]
        if len(matching) > {'tool': 100, 'delivery': 50, 'message': 120}[kind]:
            old = matching[0]
            self.items.remove(old)
            if isinstance(old, ToolActivity):
                self.tool_rows.pop(activity_key(old.value), None)
            elif isinstance(old, MessageDelivery):
                self.message_rows.pop(old.message_id, None)
            if old is self.delivery_note:
                self.delivery_note = None
            old.remove()

    def write(self, value):
        if isinstance(value, dict) and value.get('kind') == 'tool-activity':
            self.tool_activity(stopped_activity(value), live=False)
            return self
        if isinstance(value, dict) and value.get('kind') == 'execution-result':
            self.finish_stream()
            if self.completed_stream is not None:
                widget, text = self.completed_stream
                if plain(value.get('body', '')).strip() == text.strip() and widget in self.items:
                    self.items.remove(widget)
                    widget.remove()  # authoritative final result replaces its identical preview
            self.completed_stream = None
            self.append(ExecutionResult(value))
            return self
        value = plain(value)[:200000]
        operator = value.startswith('You: ')
        if operator:
            self.completed_stream = None
            body = Text.assemble(('You\n','bold'),value[5:])
            self.append(SelectableMessage(body, classes='message operator'))
        else:
            self.append(AssistantMessage(value, classes='message reply'))
        return self

    def message_delivery(self, item):
        mid = item['message_id']
        widget = self.message_rows.get(mid)
        if widget is None:
            widget = MessageDelivery(item)
            self.message_rows[mid] = widget
            self.append(widget)
        else:
            widget.update_observation(item)
        return widget

    def delivery_notice(self, text):
        if not text:
            if self.delivery_note is not None:
                if self.delivery_note in self.items:
                    self.items.remove(self.delivery_note)
                self.delivery_note.remove()
                self.delivery_note = None
        elif self.delivery_note is None:
            self.delivery_note = Static(plain(text), classes='result-notice attention', markup=False)
            self.append(self.delivery_note)
        else:
            self.delivery_note.update(plain(text))

    def clear_message_deliveries(self):
        self.delivery_notice('')
        for row in list(self.message_rows.values()):
            if row in self.items:
                self.items.remove(row)
            row.remove()
        self.message_rows.clear()

    def activity(self, title, detail=''):
        # Native progress is an observation, never an execution authority or receipt.
        self.append(Collapsible(SelectableMessage(plain(detail or title), markup=False),
                                title=plain(title)[:160], collapsed=True, classes='activity'))

    def tool_activity(self, value, *, live=True):
        value = activity_view(value)
        key = activity_key(value)
        widget = self.tool_rows.get(key)
        if widget is None:
            self.finish_stream()
            self.completed_stream = None
            widget = ToolActivity(value, live=live)
            self.tool_rows[key] = widget
            self.append(widget)
        else:
            widget.update_activity(value)

    def stream(self, event):
        if event.get('channel') != 'text' or not isinstance(event.get('text'), str):
            return  # Never render reasoning payloads.
        if event.get('purpose') == 'summary':
            # Context compaction is internal maintenance, never an answer to the user.
            # Its exact text remains in the session journal and model input.
            summary_key = event.get('call_id')
            if getattr(self, 'summary_key', None) != summary_key or self.live is not None:
                self.finish_stream()
                self.summary_key = summary_key
                self.activity('Compacting conversation context',
                              'Internal context summary in progress; summary text is not shown here.')
            return
        self.summary_key = None
        key = event.get('call_id')
        if self.live is None or key != self.live_key:
            self.finish_stream()
            self.completed_stream = None
            self.live_key = key
            self.live = StreamedReply('tare · working', classes='message partial')
            self.append(self.live)
        self.live_text = (self.live_text + plain(event['text']))[-16000:]
        self.live_dirty = True

    def narration(self, event):
        """A progress sentence the coordinator wrote with its tool calls; it replaces that reply's live preview."""
        if self.live is not None and self.live_key == event.get('call_id'):
            if self.live in self.items:
                self.items.remove(self.live)
            self.live.remove()
            self.live, self.live_key, self.live_text, self.live_dirty = None, None, '', False
        else:
            self.finish_stream()
        self.completed_stream = None
        self.append(AssistantMessage(plain(event['text']), classes='message reply'))

    def flush_live(self):
        if self.live is not None and self.live_dirty:
            self.live.update_reply(self.live_text, 'tare · working')
            self.live_dirty = False

    def finish_stream(self):
        self.flush_live()
        if self.live is not None:
            self.live.update_reply(self.live_text, 'tare · streamed response', final=True)
            self.completed_stream = (self.live, self.live_text)
        self.live = None
        self.live_key = None
        self.live_text = ''

    def clear(self):
        self.completed_stream = None
        self.generation += 1
        self.delivery_note = None
        self.live = None
        self.live_key = None
        self.summary_key = None
        self.live_text = ''
        self.live_dirty = False
        for widget in self.items:
            widget.remove()
        self.items.clear()
        self.tool_rows.clear()
        self.message_rows.clear()
        self.query_one('#welcome').display = True
        self.anchor()
        return self


async def observe_process(process, on_event, on_diagnostic):
    """Drain both pipes concurrently; stdout retains the authoritative final JSON.

    stderr's complete JSON lines carry optional presentation events. Oversized
    diagnostics are bounded and drained, so output cannot deadlock the child.
    """
    if not hasattr(process, 'stdout'):  # Existing backend adapters may expose only communicate.
        return await process.communicate()

    async def result():
        parts, size, exceeded = [], 0, False
        while chunk := await process.stdout.read(65536):
            size += len(chunk)
            if size <= 16 * 1024 * 1024:
                parts.append(chunk)
            else:
                exceeded = True
        if exceeded:
            raise ValueError('Result exceeds the console display limit; inspect the saved session.')
        return b''.join(parts)

    def dispatch(line):
        decoded = line.decode('utf-8', errors='replace')
        try:
            event = json.loads(decoded)
        except ValueError:
            on_diagnostic(plain(decoded))
            return
        if isinstance(event, dict) and 'channel' in event:
            if event.get('channel') == 'text':
                on_event(event)
            elif event.get('channel') == 'activity':
                try:
                    value = activity_view(event.get('activity'))
                except (TypeError, ValueError):
                    return
                on_event({'channel': 'activity', 'activity': value})
            elif event.get('channel') == 'narration' and isinstance(event.get('text'), str):
                on_event({'channel': 'narration', 'call_id': str(event.get('call_id', ''))[:256],
                          'text': plain(event['text'])[:4000]})
            # Other native channels can contain reasoning: discard their contents.
        else:
            on_diagnostic(plain(decoded))

    async def events():
        pending = b''
        dropping = False
        while chunk := await process.stderr.read(4096):
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if not dropping and len(line) <= 65536 and line.strip():
                    dispatch(line)
                dropping = False
            if len(pending) > 65536:
                pending = b''
                dropping = True
        if pending.strip() and not dropping:
            dispatch(pending)

    tasks = [asyncio.create_task(result()), asyncio.create_task(events())]
    try:
        stdout, _ = await asyncio.gather(*tasks)
        await process.wait()
        return stdout, b''
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

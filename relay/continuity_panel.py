"""Review exactly the brief sent when changing provider."""
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, TextArea

from .console_panels import SupportPanel
from .native_continuity import MAX_BRIEF_BYTES


class ContinuityPanel(SupportPanel):
    DEFAULT_CSS = '''
    ContinuityPanel .support-body { width: 100; max-width: 96%; height: 90%; padding: 1 2; }
    ContinuityPanel TextArea { height: 1fr; border: round $primary; }
    ContinuityPanel Static { height: auto; }
    '''

    def __init__(self, text, label):
        super().__init__()
        self.text, self.label = text, label

    def compose(self):
        with Vertical(classes='support-body'):
            yield Static('Continue with '+self.label, markup=False)
            yield Static('Only this brief is sent automatically, along with current project instructions and tool definitions. '
                         'History and attachments stay local. Previous usage and limits are kept. '
                         'Keep the objective, constraints, progress and next step.', markup=False)
            yield TextArea(self.text, id='continuity-brief', soft_wrap=True)
            yield Static('', id='continuity-size', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Cancel', id='continuity-cancel')
                yield Button('Continue task', id='continuity-go', classes='panel-primary')

    def on_mount(self):
        self.update_size()

    def on_text_area_changed(self, event):
        self.update_size()

    def update_size(self):
        text = self.query_one('#continuity-brief', TextArea).text
        size = len(text.encode())
        valid = bool(text.strip()) and size <= MAX_BRIEF_BYTES
        self.query_one('#continuity-size', Static).update(
            f'{size:,} / {MAX_BRIEF_BYTES:,} UTF-8 bytes. No model call to prepare this brief.'+
            (' Shorten the brief to continue; preserve your requirements.' if size > MAX_BRIEF_BYTES else ''))
        self.query_one('#continuity-go', Button).disabled = not valid

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(self.query_one('#continuity-brief', TextArea).text if event.button.id == 'continuity-go' else None)

    def action_close(self):
        self.dismiss(None)

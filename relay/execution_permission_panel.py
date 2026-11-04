"""One question, in the conversation, before a worker needs edit access."""
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from .console_panels import SupportPanel


class ExecutionPermissionPanel(SupportPanel):
    BINDINGS = [('escape', 'deny', 'Deny')]
    DEFAULT_CSS = '''
    ExecutionPermissionPanel .support-body { height: auto; max-height: 90%; max-width: 94; }
    ExecutionPermissionPanel .permission-detail { height: auto; max-height: 18; }
    ExecutionPermissionPanel .permission-buttons { height: auto; }
    ExecutionPermissionPanel .permission-buttons Button { width: 1fr; height: auto; min-height: 3; }
    '''

    def __init__(self, request):
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        value = self.request
        command=value.get('kind')=='command'
        with Vertical(classes='support-body'):
            yield Static('Run this command?' if command else 'Allow this task to change project files?', classes='panel-title')
            yield Static(value['objective'], markup=False)
            with VerticalScroll(classes='permission-detail'):
                if command:
                    yield Static('Project: '+value['project']+'\nWorking folder: '+value['cwd']+
                        '\n\nExact command:\n'+repr(value['argv'])+'\n\nSandbox and execution limits still apply.',markup=False)
                else:
                    yield Static('Project: '+value['project']+'\n\nFiles:\n'+'\n'.join(value['paths']), markup=False)
                from .conversation_tasks import BACKLOG_CHECK
                checks = '\n'.join('Validate Backlog Graph format' if n == BACKLOG_CHECK else n+': '+repr(argv)
                                   for n, argv in value.get('checks',{}).items())
                if not command:yield Static('Checks: '+(checks or 'host diff check; any tests named in the plan')+
                    ('\nTest targets: '+', '.join(value['tests']) if value.get('tests') else '')+
                    '\nThe candidate is checked and independently audited before applying.', markup=False)
            with Horizontal(classes='permission-buttons'):
                yield Button('Allow for this task', id='permission-once', classes='panel-primary')
                if not command:yield Button('Always allow in this project', id='permission-project')
                yield Button('Deny', id='permission-deny')

    def on_mount(self):
        self.query_one('#permission-once', Button).focus()

    def on_button_pressed(self, event):
        if event.button.id in {'permission-once', 'permission-project', 'permission-deny'}:
            event.stop()
            self.dismiss(event.button.id.removeprefix('permission-'))

    def action_deny(self):
        self.dismiss('deny')

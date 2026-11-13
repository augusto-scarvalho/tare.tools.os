"""One question, in the conversation, before a worker needs edit access or a plan is implemented."""
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from .console_panels import SupportPanel


class ExecutionPermissionPanel(SupportPanel):
    BINDINGS = [('escape', 'deny', 'Deny')]
    DEFAULT_CSS = '''
    ExecutionPermissionPanel .support-body { height: auto; max-height: 90%; max-width: 94; }
    ExecutionPermissionPanel .permission-detail { height: auto; max-height: 18; }
    ExecutionPermissionPanel.plan .support-body { height: 90%; }
    ExecutionPermissionPanel.plan .permission-detail { height: 1fr; max-height: 100%; }
    ExecutionPermissionPanel .permission-buttons { height: auto; }
    ExecutionPermissionPanel .permission-buttons Button { width: 1fr; height: auto; min-height: 3; }
    '''

    def __init__(self, request):
        # plan_text: the whole saved plan, when the user chose to see it before implementing.
        super().__init__(classes='plan' if 'plan_text' in request else None)
        self.request = request
        self.changes = ''

    def compose(self) -> ComposeResult:
        value = self.request
        command=value.get('kind')=='command'
        plan = value.get('plan_text')
        with Vertical(classes='support-body'):
            yield Static('Run this command?' if command else 'Implement this plan?' if plan is not None else
                         'Allow this task to change project files?', classes='panel-title')
            yield Static(value['objective'], markup=False)
            with VerticalScroll(classes='permission-detail'):
                if plan is not None:
                    yield Static(plan+'\n', markup=False)
                if command:
                    yield Static('Project: '+value['project']+'\nWorking folder: '+value['cwd']+
                        '\n\nExact command:\n'+repr(value['argv'])+'\n\nSandbox and execution limits still apply.',markup=False)
                else:
                    yield Static('Project: '+value['project']+'\n\nFiles'+
                                 (' (edits already allowed)' if value.get('files_allowed') else '')+':\n'+
                                 '\n'.join(value['paths']), markup=False)
                from .conversation_tasks import BACKLOG_CHECK
                checks = '\n'.join('Validate Backlog Graph format' if n == BACKLOG_CHECK else n+': '+repr(argv)
                                   for n, argv in value.get('checks',{}).items())
                if not command:yield Static('Checks: '+(checks or 'host diff check; any tests named in the plan')+
                    ('\nTest targets: '+', '.join(value['tests']) if value.get('tests') else '')+
                    '\nThe candidate is checked and independently audited before applying.', markup=False)
            if plan is not None:
                yield Input(placeholder='What should change? (optional, sent with Change the plan)',
                            max_length=480, id='permission-changes')
            with Horizontal(classes='permission-buttons'):
                if plan is not None:
                    yield Button('Implement this plan', id='permission-once', classes='panel-primary')
                    if not value.get('files_allowed'):
                        yield Button('Implement, always allow edits here', id='permission-project')
                    yield Button('Change the plan', id='permission-change')
                else:
                    yield Button('Allow this command' if command else 'Allow for this task', id='permission-once', classes='panel-primary')
                    if not command:yield Button('Always allow in this project', id='permission-project')
                yield Button('Deny', id='permission-deny')

    def on_mount(self):
        self.query_one('#permission-once', Button).focus()

    def on_input_submitted(self, event):
        event.stop()
        self.query_one('#permission-change', Button).press()

    def on_button_pressed(self, event):
        if event.button.id in {'permission-once', 'permission-project', 'permission-change', 'permission-deny'}:
            event.stop()
            if event.button.id == 'permission-change':
                self.changes = self.query_one('#permission-changes', Input).value
            self.dismiss(event.button.id.removeprefix('permission-'))

    def action_deny(self):
        self.dismiss('deny')

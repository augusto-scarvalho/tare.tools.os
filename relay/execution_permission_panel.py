"""One question, in the conversation, before a worker needs edit access, a plan is implemented, (local-only
mode) a change whose audit was not independent is applied, or a check the sandbox blocked runs outside it."""
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
        audit=value.get('kind')=='audit'  # local-only: apply a change whose audit was not independent?
        sandbox=value.get('kind')=='sandbox'  # a check the sandbox blocked: run it outside?
        turn=command and value.get('scope')=='turn'  # a yes also covers the turn's later unreviewed commands
        plan = value.get('plan_text')
        with Vertical(classes='support-body'):
            yield Static('Run commands without review this turn?' if turn else
                         'Run this command?' if command else 'Apply this change?' if audit else
                         'Run this check outside the sandbox?' if sandbox else
                         'Implement this plan?' if plan is not None else
                         'Allow this task to change project files?', classes='panel-title')
            yield Static(value['objective'], markup=False)
            with VerticalScroll(classes='permission-detail'):
                if plan is not None:
                    yield Static(plan+'\n', markup=False)
                if sandbox:
                    name,argv=next(iter(value['checks'].items()))
                    yield Static('Project: '+value['project']+'\nCheck: '+name+'\n\nExact command:\n'+repr(argv)+
                        '\n\nOutside the sandbox this program runs as you: it can use the network and read your '
                        'files, secret files included. Not running it leaves the check unjudged, and a change it '
                        'checks is not applied on its strength.', markup=False)
                elif audit:
                    review=value['audit']
                    items=review.get('open_items') or []
                    hidden=review.get('open_items_total',len(items))-len(items)
                    yield Static('Project: '+value['project']+'\n\nFiles:\n'+'\n'.join(value['paths'])+
                        '\n\nAudit ('+review['label']+'): '+review['verdict']+' by '+str(review.get('route'))+
                        ('' if review['verdict']=='ACCEPT' else ' (not accepted)\n\nOpen items the auditor left:\n'+
                         ''.join('- '+str(item)+'\n' for item in items)+
                         (f'({hidden} more in the audit receipt)\n' if hidden>0 else ''))+
                        ('\n'+review['summary'] if review.get('summary') else '')+
                        '\n\nThe checks passed. Not applying keeps the candidate unapplied.', markup=False)
                elif command:
                    yield Static('Project: '+value['project']+'\nWorking folder: '+value['cwd']+
                        '\n\nExact command:\n'+repr(value['argv'])+'\n\nSandbox and execution limits still apply.',markup=False)
                else:
                    yield Static('Project: '+value['project']+'\n\nFiles'+
                                 (' (edits already allowed)' if value.get('files_allowed') else '')+':\n'+
                                 '\n'.join(value['paths']), markup=False)
                from .conversation_tasks import BACKLOG_CHECK
                checks = '\n'.join('Validate Backlog Graph format' if n == BACKLOG_CHECK else n+': '+repr(argv)
                                   for n, argv in value.get('checks',{}).items())
                from .execution_scope import current
                if not command and not audit and not sandbox:yield Static('Checks: '+(checks or 'host diff check; any tests named in the plan')+
                    ('\nTest targets: '+', '.join(value['tests']) if value.get('tests') else '')+
                    ('\nThe candidate is checked and audited before applying; an audit by the same model family is '
                     'not independent, so tare asks you before applying it.' if current().only == 'local' else
                     '\nThe candidate is checked and independently audited before applying.'), markup=False)
            if plan is not None:
                yield Input(placeholder='What should change? (optional, sent with Change the plan)',
                            max_length=480, id='permission-changes')
            with Horizontal(classes='permission-buttons'):
                if sandbox:
                    yield Button('Run it outside the sandbox this turn', id='permission-once')
                    yield Button("Always run this project's blocked checks outside it", id='permission-project')
                    yield Button('Do not run it', id='permission-deny')
                    return
                if audit:
                    accepted=value['audit']['verdict']=='ACCEPT'  # no audit accepted it: nothing invites the yes
                    yield Button('Apply the change' if accepted else 'Apply anyway', id='permission-once',
                                 classes='panel-primary' if accepted else '')
                elif plan is not None:
                    yield Button('Implement this plan', id='permission-once', classes='panel-primary')
                    if not value.get('files_allowed'):
                        yield Button('Implement, always allow edits here', id='permission-project')
                    yield Button('Change the plan', id='permission-change')
                else:
                    yield Button('Run without review this turn' if turn else 'Allow this command' if command else
                                 'Allow for this task', id='permission-once', classes='panel-primary')
                    if not command:yield Button('Always allow in this project', id='permission-project')
                yield Button('Do not apply' if audit else 'Deny', id='permission-deny')

    def on_mount(self):
        # A change no audit accepted, and a check the sandbox blocked, start on the safe answer.
        unaccepted=self.request.get('kind')=='audit' and self.request['audit']['verdict']!='ACCEPT'
        safe=unaccepted or self.request.get('kind')=='sandbox'
        self.query_one('#permission-deny' if safe else '#permission-once', Button).focus()

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

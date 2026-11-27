"""One question, in the conversation, before a worker needs edit access, a plan is implemented, (local-only
mode) a change whose audit was not independent is applied, a check the sandbox blocked runs outside it, a check
or command that shows no activity is stopped, or an implementer starts on checks that already fail."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from .console_panels import SupportPanel


class ExecutionPermissionPanel(SupportPanel):
    # The focus starts on a button, so the keys that scroll a text went nowhere: a long plan could only be read with
    # the mouse wheel (seen 2026-11-16). They scroll the detail whatever has the focus.
    BINDINGS = [('escape', 'deny', 'Deny'), Binding('pagedown', 'page(1)', show=False),
                Binding('pageup', 'page(-1)', show=False), Binding('end', 'page(0)', show=False),
                Binding('home', 'page(None)', show=False)]
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
        checks_proposal=value.get('checks_proposal') if value.get('kind')=='checks' else None
        sandbox=value.get('kind')=='sandbox'  # a check the sandbox blocked: run it outside?
        gui_check=value.get('gui_check') if value.get('kind')=='gui_check' else None  # declared mode="gui" check
        stall=value.get('stall') if value.get('kind')=='stall' else None  # a check or command with no activity
        baseline=value.get('baseline') if value.get('kind')=='baseline' else None  # checks red before any change
        one=len(value.get('checks') or {})==1
        turn=command and value.get('scope')=='turn'  # a yes also covers the turn's later unreviewed commands
        plan = value.get('plan_text')
        with Vertical(classes='support-body'):
            yield Static('Run commands without review this turn?' if turn else
                         'Run this command?' if command else 'Apply this change?' if audit else
                         ('Update .tare/checks.json?' if checks_proposal.get('existed') else 'Create .tare/checks.json?')
                         if checks_proposal is not None else
                         'Run this GUI check on your desktop?' if gui_check is not None else
                         'Run this check outside the sandbox?' if sandbox else
                         ('Implement with a check that already fails?' if one else
                          'Implement with checks that already fail?') if baseline is not None else
                         ('Keep waiting for this check?' if stall['check'] else 'Keep waiting for this command?')
                         if stall is not None else
                         'Implement this plan?' if plan is not None else
                         'Allow this task to change project files?', classes='panel-title')
            yield Static(value['objective'], markup=False)
            with VerticalScroll(classes='permission-detail'):
                if plan is not None:
                    yield Static(plan+'\n', markup=False)
                if stall is not None:
                    from .work_units import QUIET_OUTPUT_SECONDS, STALL_SECONDS
                    from .conversation_permissions import duration
                    name,argv=next(iter(value['checks'].items()))
                    quiet=stall.get('reason')=='quiet_output'
                    tail=str(stall.get('output_tail') or '').strip()
                    yield Static('Project: '+value['project']+('\nCheck: '+name if stall['check'] else '')+
                        '\n\nExact command:\n'+repr(argv)+'\n\nKeep waiting: it keeps running, and tare asks again '
                        'only after another '+duration(QUIET_OUTPUT_SECONDS if quiet else STALL_SECONDS)+
                        (' without new output. ' if quiet else ' without activity. ')+
                        'Continue in background: keeps it running and frees this screen for other conversations '
                        '(/bg to inspect, answer, or stop). Stop it: '+
                        ('the check stays unjudged.' if stall['check'] else
                         'the implementer is told it produced no new output while using CPU and was stopped.' if quiet
                         else 'the implementer is told it showed no activity and was stopped.')+
                        ('\n\nStop it and run it outside the sandbox: outside, this program runs as you: it can use '
                         'the network and read your files, secret files included. '+
                         ('A program retrying something the sandbox refused can also run without output like this; '
                          if quiet else 'A test that waits for a connection the sandbox refused can wait silently '
                          'like this; ')+'nothing here shows that this is the cause.'
                         if stall['outside'] else '')+
                        ('\n\nRecent output:\n'+tail if tail else ''), markup=False)
                elif baseline is not None:
                    them='it' if one else 'them'
                    yield Static('Project: '+value['project']+'\n\nFailing before any change:\n'+
                        '\n'.join(n+': '+repr(argv) for n,argv in value['checks'].items())+
                        ('\n\nThe plan names '+('this test' if one else 'these tests')+' as its own: a failure here is '
                         'usually what the change is meant to fix.' if baseline.get('named') else '')+
                        '\n\nContinue: the change must make '+them+' pass, or nothing is applied.'+
                        ('\nContinue without '+them+': this change is judged by its other checks only, corrections included, and '
                         'its result says so.' if baseline.get('aside') else '')+
                        '\nStop: no implementer starts and nothing is spent on one.'+
                        '\n\nTheir output before any change:\n'+str(baseline.get('output') or ''), markup=False)
                elif gui_check is not None:
                    name,argv=next(iter(value['checks'].items()))
                    desc=str(gui_check.get('description') or '').strip()
                    yield Static('Project: '+value['project']+'\nCheck: '+name+
                        ('\nDescription: '+desc if desc else '')+
                        '\n\nExact command:\n'+repr(argv)+
                        '\n\nOutside the sandbox this GUI check runs directly on your desktop as you: it can open '
                        'windows, use your GPU/display, reach the network, and read your files (secret files included). '
                        'Skipping it leaves the GUI check unrun.'+
                        ('\n\nA local model wrote the code this check runs, so its GUI checks are asked about each turn.'
                         if gui_check.get('always') is False else
                         '\n\nAlways allow binds this exact command only; changing .tare/checks.json asks again, and '
                         '/permissions can clear remembered GUI checks.'),
                        markup=False)
                elif sandbox:
                    name,argv=next(iter(value['checks'].items()))
                    line=str((value.get('sandbox') or {}).get('line') or '')[:200]  # the check's own words: plain
                    yield Static('Project: '+value['project']+'\nCheck: '+name+'\n\nExact command:\n'+repr(argv)+
                        ('\n\nThe line of its output that shows it:\n'+line if line else '')+
                        '\n\nOutside the sandbox this program runs as you: it can use the network and read your '
                        'files, secret files included. '+('Not running it leaves the check failed.' if line else
                        'Not running it leaves the check unjudged, and a change it checks is not applied on its '
                        'strength.')+('\n\nA local model wrote the code this check runs, so its checks are asked '
                        'about each turn.' if (value.get('sandbox') or {}).get('always') is False else ''),
                        markup=False)
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
                elif checks_proposal is not None:
                    removed = checks_proposal.get('removed') or []
                    active_now = checks_proposal.get('active_now', True)
                    effect_note = (
                        'Saving updates .tare/checks.json immediately for this conversation. '
                        if active_now else
                        'Saving writes .tare/checks.json for future conversations; this conversation has explicit '
                        'scoped verifiers that take precedence. ')
                    yield Static(
                        'Project: ' + value['project'] + '\nFile: .tare/checks.json\n'
                        'Proposed by: ' + str(checks_proposal.get('from_role') or 'agent') + ' (advisory only)'
                        + ('\n\nReason:\n' + str(checks_proposal['reason']) if checks_proposal.get('reason') else '')
                        + ('\n\nWARNING — checks removed: ' + ', '.join(removed) if removed else '')
                        + '\n\nExact changes:\n' + str(checks_proposal.get('diff') or '')
                        + '\n\n' + effect_note + 'Choosing Do not save leaves the project unchanged.',
                        markup=False)
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
                if not command and not audit and checks_proposal is None and not sandbox and gui_check is None and stall is None and baseline is None:yield Static('Checks: '+(checks or 'host diff check; any tests named in the plan')+
                    ('\nTest targets: '+', '.join(value['tests']) if value.get('tests') else '')+
                    ('\nThe candidate is checked and audited before applying; an audit by the same model family is '
                     'not independent, so tare asks you before applying it.' if current().only == 'local' else
                     '\nThe candidate is checked and independently audited before applying.'), markup=False)
            if plan is not None:
                yield Input(placeholder='What should change? (optional, sent with Change the plan)',
                            max_length=480, id='permission-changes')
            with Horizontal(classes='permission-buttons'):
                if checks_proposal is not None:
                    yield Button('Save .tare/checks.json', id='permission-once')
                    yield Button('Do not save', id='permission-deny')
                    return
                if stall is not None:
                    yield Button('Keep waiting', id='permission-once')
                    yield Button('Continue in background', id='permission-background')
                    yield Button('Stop it', id='permission-deny')
                    if stall['outside']:
                        yield Button('Stop it and run it outside the sandbox', id='permission-outside')
                    return
                if gui_check is not None:
                    yield Button('Run this GUI check', id='permission-once')
                    if gui_check.get('always') is not False:  # never for a local model's work
                        yield Button('Always allow this exact GUI check here', id='permission-project')
                    yield Button('Skip GUI check', id='permission-deny')
                    return
                if sandbox:
                    yield Button('Run it outside the sandbox this turn', id='permission-once')
                    if (value.get('sandbox') or {}).get('always') is not False:  # never for a local model's work
                        yield Button("Always run this project's blocked checks outside it", id='permission-project')
                    yield Button('Do not run it', id='permission-deny')
                    return
                if baseline is not None:
                    yield Button('Continue: this change must make '+('it' if one else 'them')+' pass',
                                 id='permission-once')
                    if baseline.get('aside'):
                        yield Button('Continue without '+('this check' if one else 'these checks'), id='permission-aside')
                    yield Button('Stop', id='permission-deny')
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
        # A change no audit accepted, a check the sandbox blocked, and checks that already fail (Stop spends
        # nothing) start on the safe answer; a process with no activity starts on keeping it running, and failing
        # tests the plan itself names start on Continue (a bug fix: the change is meant to make them pass).
        unaccepted=self.request.get('kind')=='audit' and self.request['audit']['verdict']!='ACCEPT'
        safe=unaccepted or self.request.get('kind') in {'sandbox', 'gui_check', 'checks'} or (
            self.request.get('kind')=='baseline' and not self.request['baseline'].get('named'))
        self.query_one('#permission-deny' if safe else '#permission-once', Button).focus()

    def on_input_submitted(self, event):
        event.stop()
        self.query_one('#permission-change', Button).press()

    def on_button_pressed(self, event):
        if event.button.id in {'permission-once', 'permission-project', 'permission-change', 'permission-deny',
                               'permission-outside', 'permission-aside', 'permission-background'}:
            event.stop()
            if event.button.id == 'permission-change':
                self.changes = self.query_one('#permission-changes', Input).value
            self.dismiss(event.button.id.removeprefix('permission-'))

    def action_page(self, direction):
        """Scroll the detail: a page down (1) or up (-1), to its end (0) or its start (None)."""
        detail = self.query_one('.permission-detail', VerticalScroll)
        {1: detail.scroll_page_down, -1: detail.scroll_page_up, 0: detail.scroll_end,
         None: detail.scroll_home}[direction](animate=False)

    def action_deny(self):
        self.dismiss('once' if self.request.get('kind')=='stall' else 'deny')  # Escape never stops a process

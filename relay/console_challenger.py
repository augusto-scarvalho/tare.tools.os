"""A project toggle, resource limits and reusable results for native challengers."""
import asyncio
import json
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static, Switch, TextArea

from .console_output import plain
from .console_panels import SupportPanel


class ChallengerPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    ChallengerPanel .challenger-label { height: auto; margin-top: 1; }
    ChallengerPanel #challenger-scroll { height: 1fr; }
    ChallengerPanel #challenger-variants { height: 9; }
    ChallengerPanel #challenger-result { height: 9; border: none; }
    ChallengerPanel Switch { height: 1; border: none; }
    ChallengerPanel Checkbox { height: auto; border: none; margin: 0; }
    ChallengerPanel #challenger-status { height: auto; max-height: 3; }
    ChallengerPanel Collapsible { padding: 0; border: none; }
    ChallengerPanel .panel-actions Button { min-width: 10; }
    ChallengerPanel .panel-actions Input { width: 1fr; margin-right: 1; }
    '''

    def __init__(self, root):
        super().__init__()
        self.root, self.running, self.rows = Path(root), False, {}

    def compose(self):
        from .challenger import policy
        value = policy(self.root)
        self.value = value
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Challenger mode', classes='panel-title')
                yield Button('Back · Esc', id='challenger-close')
            with VerticalScroll(id='challenger-scroll'):
                yield Static('Automatic experiments for new implementation tasks · Save applies these settings.', classes='challenger-label')
                yield Switch(value['enabled'], id='challenger-enabled')
                yield Static('Local task sample % / External task sample %', classes='challenger-label')
                with Horizontal(classes='panel-actions'):
                    yield Input(str(value['local_percent']), id='challenger-local_percent')
                    yield Input(str(value['external_percent']), id='challenger-external_percent')
                yield Static('Daily API budget USD / Maximum USD per paired trial', classes='challenger-label')
                with Horizontal(classes='panel-actions'):
                    yield Input(value['daily_usd'], id='challenger-daily_usd')
                    yield Input(value['trial_usd'], id='challenger-trial_usd')
                yield Static('Daily local seconds / Daily subscription calls', classes='challenger-label')
                with Horizontal(classes='panel-actions'):
                    yield Input(str(value['daily_local_seconds']), id='challenger-daily_local_seconds')
                    yield Input(str(value['daily_quota_calls']), id='challenger-daily_quota_calls')
                with Collapsible(title='Experimental variants and limits', collapsed=True):
                    yield Static('Variants: profile names or objects with profile, reasoning, parameters, task_types, identity, accounting and resource_pool.')
                    yield TextArea(json.dumps(value['models'], indent=2), id='challenger-variants')
                    for key, label in [('reviewer', 'Independent reviewer profile · optional'),
                            ('task_type', 'Task group for new sessions'), ('seconds', 'Seconds per model session'),
                            ('calls', 'Model calls per session'), ('quota_floor', 'Keep at least this quota % for normal work'),
                            ('storage_mib', 'Maximum retained experiment storage MiB')]:
                        yield Static(label, classes='challenger-label')
                        yield Input(json.dumps(value[key]) if isinstance(value[key], dict) else str(value[key]), id='challenger-'+key)
                    yield Checkbox('Allow a bounded subscription probe when quota is unknown',
                                   value=value['allow_unknown_quota'], id='challenger-allow_unknown_quota')
                    yield Static('One challenger at a time across projects. Shared local GPU tasks run in sequence. '
                        'Daily limits include the paired reviews. Calls are a local allowance, not vendor quota units. '
                        'Local hardware cost and unknown subscription charges stay unpriced.')
                yield Static('Recorded experiments', classes='challenger-label')
                yield Select([], prompt='Select a result', id='challenger-run')
                yield TextArea('Select a result to inspect it. Retrying starts new model calls; inspection does not.', read_only=True, id='challenger-result')
                yield Checkbox('Replace the recorded primary changes with this reviewed challenger; preserve later edits.',
                               id='challenger-confirm')
            yield Static('', id='challenger-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Save', id='challenger-save', classes='panel-primary')
                yield Button('Refresh', id='challenger-refresh')
                yield Button('Compare', id='challenger-compare')
                yield Button('View patch', id='challenger-patch')
                yield Button('Use changes', id='challenger-use', disabled=True)
            with Horizontal(classes='panel-actions'):
                yield Button('Retry failed stage', id='challenger-retry', disabled=True)
                yield Button('Stop experiment', id='challenger-stop', disabled=True)
            yield Static('Retry keeps completed implementations and their workspaces. If only review failed, '
                         'only reviews run again. Saved models and session/daily limits apply.', id='challenger-retry-help')

    async def on_mount(self):
        await self.refresh_results()

    async def refresh_results(self):
        from .challenger import inspect
        report = await asyncio.to_thread(inspect, self.root)
        self.rows = {r['id']: r for r in reversed(report['runs'])}
        self.query_one('#challenger-run', Select).set_options([
            (r['name']+' · '+r['status']+' · '+r['id'][:8], r['id']) for r in self.rows.values()])
        self.query_one('#challenger-status', Static).update('Today: USD '+report['committed_usd']+' committed/reserved. '+report['scope'])

    def selected(self):
        value = self.query_one('#challenger-run', Select).value
        if value not in self.rows:
            raise ValueError('Select a recorded experiment first.')
        return self.rows[value]

    def on_select_changed(self, event):
        if event.select.id != 'challenger-run':
            return
        self.query_one('#challenger-confirm', Checkbox).value = False
        row = self.rows.get(event.value)
        from .challenger import ACTIVE
        self.query_one('#challenger-stop', Button).disabled = not row or row['status'] not in ACTIVE
        self.query_one('#challenger-retry', Button).disabled = not row or row['status'] in ACTIVE
        if row:
            text = json.dumps({k: row.get(k) for k in ('name', 'status', 'reason', 'challenger_outcome',
                'challenger_reason', 'checks_passed', 'challenger_verdict', 'reference_verdict', 'known_usd',
                'committed_usd', 'uncertain_calls', 'local_seconds_reserved', 'quota_calls_reserved', 'measured',
                'managed_usage', 'study', 'study_error', 'reference_reuse')}, indent=2)
            self.query_one('#challenger-result', TextArea).load_text(plain(text))

    def on_checkbox_changed(self, event):
        if event.checkbox.id == 'challenger-confirm':
            self.query_one('#challenger-use', Button).disabled = not event.value

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'challenger-close':
            self.action_close(); return
        if self.running:
            return
        self.running = True
        try:
            from . import challenger
            action = event.button.id
            if action == 'challenger-save':
                value = dict(self.value)
                for key in ('local_percent', 'external_percent', 'daily_local_seconds', 'daily_quota_calls', 'seconds', 'calls', 'quota_floor', 'storage_mib'):
                    value[key] = int(self.query_one('#challenger-'+key, Input).value)
                for key in ('daily_usd', 'trial_usd', 'task_type', 'reviewer'):
                    value[key] = self.query_one('#challenger-'+key, Input).value.strip()
                if value['reviewer'].startswith('{'):
                    value['reviewer'] = json.loads(value['reviewer'])
                value['models'] = json.loads(self.query_one('#challenger-variants', TextArea).text)
                value['enabled'] = self.query_one('#challenger-enabled', Switch).value
                value['allow_unknown_quota'] = self.query_one('#challenger-allow_unknown_quota', Checkbox).value
                self.value = await asyncio.to_thread(challenger.configure, self.root, value)
                self.query_one('#challenger-status', Static).update('Challengers '+('enabled for new tasks.' if value['enabled'] else 'disabled; active experiments will stop.'))
            elif action == 'challenger-refresh':
                await self.refresh_results()
            elif action == 'challenger-retry':
                from .challenger_recovery import retry
                result = await asyncio.to_thread(retry, self.root, Path(self.selected()['path']))
                await self.refresh_results()
                self.query_one('#challenger-run', Select).value = Path(result['job']).name
                self.query_one('#challenger-status', Static).update('New challenger started. The finished reference and earlier attempts are preserved.')
            elif action == 'challenger-stop':
                from .challenger_recovery import stop
                message = await asyncio.to_thread(stop, self.root, Path(self.selected()['path']))
                self.query_one('#challenger-status', Static).update(message)
            elif action == 'challenger-patch':
                row = self.selected(); path = Path(row['path'])/'challenger.patch'
                if path.stat().st_size > 256*1024:
                    raise ValueError('Patch is too large for this preview: '+str(path))
                self.query_one('#challenger-result', TextArea).load_text(plain(path.read_text(encoding='utf-8', errors='replace')))
            elif action == 'challenger-use':
                from .challenger_workspace import reuse
                if not self.query_one('#challenger-confirm', Checkbox).value:
                    raise ValueError('Review the patch and select the replacement confirmation.')
                result = await asyncio.to_thread(reuse, Path(self.selected()['path']))
                self.query_one('#challenger-status', Static).update(result['notice'])
                self.query_one('#challenger-confirm', Checkbox).value = False
            elif action == 'challenger-compare':
                from .console_workbench import DeliveryComparisonPanel
                path = await asyncio.to_thread(challenger.study, Path(self.selected()['path']))
                if not path:
                    raise ValueError('No paired reviewer was selected. Checks remain exploratory evidence.')
                panel = DeliveryComparisonPanel(self.root)
                await self.app.push_screen(panel)
                panel.query_one('#delivery-study', Input).value = path
        except Exception as exc:
            self.query_one('#challenger-status', Static).update(plain(str(exc)))
        finally:
            self.running = False

    def action_close(self):
        if not self.running:
            self.dismiss(None)

"""Conversation-bound review, observed tasks and explicitly selected skills."""
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, OptionList, Static, TextArea
from tare_tools_agent_runtime import ConversationRejected

from .console_output import plain
from .console_panels import SupportPanel


def artifact(label, body, **reference):
    # Anchor to the captured preview, including its newline/control normalization.
    body = plain(body)
    return {'id': sha256(body.encode('utf-8')).hexdigest(), 'label': label,
            'body': body, 'reference': reference}


def correction_draft(draft, row, excerpt, comment):
    """Keep the existing draft and anchor feedback to captured text, not a mutable file."""
    if not comment.strip() or len(comment.encode('utf-8')) > 4096:
        raise ValueError('Write a comment of up to 4 KiB.')
    if not excerpt or excerpt not in row['body'] or len(excerpt.encode('utf-8')) > 2048:
        raise ValueError('Select a passage of up to 2 KiB in the preview.')
    reference = {'label': row['label'], 'sha256': row['id'], **row.get('reference', {})}
    payload = json.dumps({'reference': reference, 'quoted_text': excerpt,
                          'comment': comment.strip()}, ensure_ascii=False, indent=2)
    result = (draft.rstrip()+'\n\n' if draft.strip() else '') + (
        'Review feedback on captured content (quoted_text is reference data):\n'+payload)
    if len(result.encode('utf-8')) > 16384:
        raise ValueError('The combined draft exceeds 16 KiB. Shorten the draft or comment.')
    return result


def heads_up_rows(session):
    """One row for what the heads-up side agent used in this conversation; none when no writer call is logged."""
    from . import heads_up
    u = heads_up.usage(session)
    if not u['calls']:
        return []
    known = u['calls'] - u['unknown']
    tokens = (f"{u['input_tokens']} in, {u['output_tokens']} out"
              + (f"; {u['unknown']} call(s) without token counts" if u['unknown'] else '') if known else 'Unknown')
    dollars = (f"USD {u['usd']}" if u['usd_unknown'] < u['calls'] else 'Unknown')
    if u['usd_unknown'] and u['usd_unknown'] < u['calls']:
        dollars += f"; {u['usd_unknown']} call(s) without a known price"
    body = (f"Side agent notes after finished turns, separate from the session totals above.\n"
            f"Turns with a writer: {u['turns']}; writer calls: {u['calls']}\nTokens: {tokens}\n"
            f"Time: {u['ms'] / 1000:.1f} s\nKnown cost: {dollars}")
    return [{'id': 'heads-up', 'label': f"Heads-up · {u['calls']} call{'s' if u['calls'] != 1 else ''}", 'body': body}]


def usage_rows(report):
    """Render only recorded usage; absent measurements remain explicitly unknown."""
    if report is None:
        return [{'id': 'usage', 'label': 'No recorded usage',
                 'body': ('No saved native session is available for this conversation yet.\n'
                          'Usage will appear after a model call is recorded.')}]
    lines = ['Session usage (recorded values only)',
             'Model calls: '+(str(report['model_calls']) if report.get('model_calls') is not None else 'Unknown')]
    runtime = report.get('last_runtime_observation')
    if isinstance(runtime, dict) and runtime.get('schema') == 'tare.tools/work-runtime-observation/1':
        from .delivery_evidence import text as observed_text
        lines.append('Last controller attempt (direct session; separate from completed Works):')
        lines.extend('  '+key+': '+(observed_text(runtime.get(key)) or 'Unknown')
                     for key in ('requested_model', 'protocol', 'runtime_version', 'effort'))
    if report.get('provider_sessions'):
        lines.insert(1, 'Across '+str(report['provider_sessions'])+' provider sessions; previous usage included.')
    if report.get('exploration_sessions'):
        lines.insert(1, str(report['exploration_sessions'])+' exploration sessions included in these totals.')
    for stage, label in (('planning', 'Planning'), ('implementation', 'Implementation')):
        if report.get(stage+'_sessions'):
            lines.insert(1, label+' sessions included: '+str(report[stage+'_sessions']))
    if report.get('unknown_session_ids'):
        lines.append('Sessions missing usage reports: '+str(len(report['unknown_session_ids'])))
    tokens = report.get('tokens')
    labels = (('total_tokens', 'Total tokens'), ('input_tokens', 'Input tokens'),
              ('output_tokens', 'Output tokens'), ('cache_read_tokens', 'Cache reads'),
              ('cache_write_tokens', 'Cache writes'))
    for key, label in labels:
        value = tokens.get(key) if isinstance(tokens, dict) else None
        if isinstance(value, dict):
            known = value.get('known_sum')
            unknown = value.get('unknown_calls')
            text = str(known) if known is not None else 'Unknown'
            if unknown is not None:
                text += f'; {unknown} call(s) missing counts'
            lines.append(label+': '+text)
        elif value is not None:
            lines.append(label+': '+str(value))
        else:
            lines.append(label+': Unknown')
    costs = report.get('known_cost_by_currency')
    if isinstance(costs, dict) and costs:
        lines.append('Known model cost: '+', '.join(f'{currency} {amount}' for currency, amount in sorted(costs.items())))
    else:
        lines.append('Known model cost: Unknown')
    estimates = report.get('provider_estimates_by_currency')
    if isinstance(estimates, dict) and estimates:
        lines.append('Provider estimate (not billed): '+', '.join(f'{currency} {amount}' for currency, amount in sorted(estimates.items())))
    unpriced = report.get('unpriced_or_partial_calls')
    lines.append('Unpriced or partially priced calls: '+(str(unpriced) if unpriced is not None else 'Unknown'))
    lines.append('Subscription quota and API credit: see "Provider quota and credit" in this panel')
    limits = report.get('effective_limits')
    lines.append('Configured limits:')
    if isinstance(limits, dict) and limits:
        lines.extend('  '+str(key).replace('_', ' ')+': '+('Unlimited' if value is None else str(value))
                     for key, value in sorted(limits.items()))
    else:
        lines.append('  Unknown')
    return [{'id': 'usage', 'label': 'Conversation totals and limits', 'body': '\n'.join(lines)}]


def quota_row(reading=False):
    """What each provider last reported as left (tare quota), as saved. `reading`: a new reading is on its way."""
    from . import provider_quota
    report = provider_quota.cached()
    note = 'Reading each provider again; no model call is made.' if reading else 'Reopen /usage to read again.'
    return {'id': 'quota', 'label': 'Provider quota and credit',
            'body': provider_quota.render(report) + '\n' + note if report else 'No reading yet. ' + note}


def observed_tasks(history, *, busy=False, result=None, profile=''):
    """Summarize retained observations without promoting tool success to task success."""
    summary = 'No final result recorded.'
    if result:
        summary = 'Last recorded result: '+str(result.get('status', 'Unknown'))
        if result.get('reason'):
            summary += '\nReason: '+str(result['reason'])
        summary += '\nModel calls: '+str(result.get('model_calls', 'Unknown'))
        tokens = result.get('tokens') or {}
        if isinstance(tokens, dict):
            for name, label in (('total_tokens', 'Total tokens'), ('cache_read_tokens', 'Cache read tokens')):
                count = tokens.get(name)
                if isinstance(count, dict):
                    summary += f"\n{label}: {count.get('known_sum', 0)} recorded; {count.get('unknown_calls', 'unknown')} call(s) without counts"
                elif count is not None:
                    summary += f'\n{label}: {count}'
        summary += '\nSubscription quota: see /usage'
    rows = [{'id': 'current', 'label': 'Current execution · '+('Running' if busy else 'Idle'),
             'body': ('Running; cancellation applies to the whole current execution.' if busy else
                      'No execution is running in this console.')+'\n\n'+
                     ('Profile: '+profile+'\n' if profile else '')+summary, 'reference': {}}]
    groups = {}
    for value in history:
        if not isinstance(value, dict) or value.get('kind') != 'tool-activity':
            continue
        source = value.get('source') or {}
        key = (source.get('session', ''), source.get('stage', 'Direct conversation'))
        groups.setdefault(key, []).append(value)
    for (session, stage), events in groups.items():
        last = events[-1]
        state = last.get('state', 'unknown')
        if state == 'running' and not busy:
            state = 'unconfirmed'
        body = 'Retained tool observations only. Overall task outcome is not inferred.\n'
        body += 'Session: '+(session or 'Current conversation')+'\n'
        body += '\n'.join(f"{v.get('operation', '?')} · {('unconfirmed' if not busy and v.get('state') == 'running' else v.get('state', 'unknown'))} · {v.get('target', '')}\n{v.get('detail', '')}"
                          for v in events)[-24000:]
        rows.append({'id': json.dumps([session, stage]),
                     'label': f'{stage} · last operation {state} · {len(events)} observed',
                     'body': body, 'reference': {}})
    return rows


class WorkbenchPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True),
                Binding('ctrl+home', 'document_start', show=False),
                Binding('ctrl+end', 'document_end', show=False)]
    DEFAULT_CSS = '''
    WorkbenchPanel #workbench-list { height: auto; max-height: 6; min-height: 2; border: none; }
    WorkbenchPanel #workbench-preview { height: 1fr; border: none; margin: 1 0; }
    WorkbenchPanel #workbench-comment { height: 4; border: round $primary 35%; }
    WorkbenchPanel #workbench-status { height: auto; max-height: 3; color: $text-muted; }
    WorkbenchPanel.narrow #workbench-list { max-height: 4; }
    WorkbenchPanel.narrow #workbench-comment { height: 3; }
    '''

    def __init__(self, title, rows, *, kind, submit=None, refresh_rows=None, preview=None, cancel=None, review=None,
                 refresh_interval=1):
        super().__init__()
        self.heading, self.rows, self.kind = title, rows, kind
        self.submit, self.refresh_rows, self.preview, self.cancel = submit, refresh_rows, preview, cancel
        self.review = review
        self.refresh_interval = refresh_interval
        self.current = None

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static(self.heading, classes='panel-title', markup=False)
                yield Button('Back · Esc', id='workbench-close')
            yield OptionList(id='workbench-list')
            yield TextArea('', read_only=True, show_line_numbers=False, id='workbench-preview')
            if self.kind == 'artifacts':
                yield TextArea('', id='workbench-comment')
            yield Static('', id='workbench-status', markup=False)
            with Horizontal(classes='panel-actions'):
                if self.kind in {'artifacts', 'skills'}:
                    yield Button('Add to draft', id='workbench-add', classes='panel-primary', disabled=True)
                if self.cancel:
                    yield Button('Cancel execution', id='workbench-cancel', classes='panel-danger')
                if self.review:
                    yield Button('Review delivery', id='workbench-review', disabled=True)

    def on_mount(self):
        self.set_class(self.app.size.width < 100 or self.app.size.height < 30, 'narrow')
        self.render_rows()
        if self.refresh_rows:
            self.set_interval(self.refresh_interval, self.refresh_observations)

    def action_document_start(self):
        if isinstance(self.app.focused, TextArea):
            self.app.focused.move_cursor((0, 0))

    def action_document_end(self):
        if isinstance(self.app.focused, TextArea):
            self.app.focused.move_cursor(self.app.focused.document.end)

    def render_rows(self):
        selected = self.current['id'] if self.current else None
        menu = self.query_one('#workbench-list', OptionList)
        menu.set_options([Text(plain(row['label'])) for row in self.rows])
        if self.rows:
            index = next((i for i, r in enumerate(self.rows) if r['id'] == selected), 0)
            menu.highlighted = index
            self.show_row(index)
        else:
            self.current = None
            self.query_one('#workbench-preview', TextArea).load_text('Nothing recorded for this conversation.' if self.kind == 'artifacts' else
                'No project skills found. Add skills/<name>/SKILL.md or .agents/skills/<name>/SKILL.md.')

    def refresh_observations(self):
        rows = self.refresh_rows()
        if rows != self.rows:
            self.rows = rows
            self.render_rows()

    def show_row(self, index):
        if not 0 <= index < len(self.rows):
            return
        self.current = self.rows[index]
        try:
            body = self.preview(self.current) if self.preview else self.current['body']
            self.query_one('#workbench-preview', TextArea).load_text(plain(body))
            message = ('Select a passage, write a comment, then add it to your draft. Send when ready.' if self.kind == 'artifacts' else
                       'Adds the complete reviewed snapshot to your draft (16 KiB combined limit). No tools are authorized.' if self.kind == 'skills' else
                       'Read-only observations; opening this view makes no model calls.')
            valid = True
        except (OSError, ValueError, UnicodeError) as exc:
            self.query_one('#workbench-preview', TextArea).load_text('Preview unavailable.')
            message, valid = str(exc), False
        self.query_one('#workbench-status', Static).update(plain(message))
        if self.query_one_optional('#workbench-add'):
            self.query_one('#workbench-add', Button).disabled = not valid
        if self.query_one_optional('#workbench-review'):
            self.query_one('#workbench-review', Button).disabled = not bool(self.current.get('review_targets'))

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == 'workbench-list':
            self.show_row(event.option_index)

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'workbench-close':
            self.action_close()
        elif event.button.id == 'workbench-cancel':
            self.cancel()
        elif event.button.id == 'workbench-review' and self.current and self.review:
            self.review(self.current)
        elif event.button.id == 'workbench-add' and self.current:
            try:
                if self.kind == 'skills':
                    self.submit(self.current, '', '')
                else:
                    selected = self.query_one('#workbench-preview', TextArea).selected_text
                    excerpt = selected or self.current['body']
                    self.submit(self.current, excerpt, self.query_one('#workbench-comment', TextArea).text)
            except (OSError, ValueError) as exc:
                self.query_one('#workbench-status', Static).update(plain(str(exc)))
                return
            self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class DeliveryComparisonPanel(SupportPanel):
    """Inspect existing delivery receipts and export a scoped preference, without inference."""
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    DeliveryComparisonPanel #delivery-result { height: 1fr; margin: 1 0; border: none; }
    DeliveryComparisonPanel #delivery-status { height: auto; max-height: 3; }
    DeliveryComparisonPanel .delivery-label { height: 1; margin-top: 1; }
    '''

    def __init__(self, root):
        super().__init__()
        self.root, self.running = Path(root), False

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Compare delivery cost', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='delivery-close')
            yield Static('Study file or Work run folder · paths may be relative to this project', classes='delivery-label')
            yield Input(placeholder='Study JSON; leave blank for this installation’s audit history', id='delivery-study')
            yield Static('New preference file · keeps a snapshot of the qualified study', classes='delivery-label')
            yield Input(placeholder='Path to a new preference JSON', id='delivery-policy')
            yield TextArea('Compare matching tasks, including failures, corrections and independent review.\n'
                           'Unknown cost stays unknown. Explicit model choices remain available for experiments.\n'
                           'Comparing and saving make no model calls.', read_only=True, id='delivery-result')
            yield Static('', id='delivery-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Compare', id='delivery-compare', classes='panel-primary')
                yield Button('Audit history', id='delivery-audits')
                yield Button('Save preference', id='delivery-save', disabled=True)

    def path(self, field):
        value = self.query_one(field, Input).value.strip()
        if not value:
            raise ValueError('Enter the study path and, when saving, a new preference path.')
        return self.root / value

    def on_input_changed(self, event):
        if event.input.id == 'delivery-study':
            self.query_one('#delivery-save', Button).disabled = True
            self.query_one('#delivery-result', TextArea).load_text('Select Compare to inspect this study. No model calls will be made.')

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'delivery-close':
            self.action_close()
            return
        if self.running or event.button.id not in {'delivery-compare', 'delivery-save', 'delivery-audits'}:
            return
        from .delivery_comparison import compare, write_policy
        self.running = True
        for widget in self.query('Input, Button'):
            widget.disabled = True
        winner = False
        try:
            if event.button.id == 'delivery-audits':
                from .audit_delivery import report, render
                from .native_installation import storage_root
                raw = self.query_one('#delivery-study', Input).value.strip()
                folder = self.root / raw if raw else storage_root() / 'work-runs'
                if not raw and not folder.exists():
                    self.query_one('#delivery-result', TextArea).load_text(
                        'No recorded Work runs in this installation. Enter an existing Work run folder to inspect its audit history.')
                    self.query_one('#delivery-status', Static).update('No model calls. No files created.')
                    return
                result = await asyncio.to_thread(report, folder)
                self.query_one('#delivery-result', TextArea).load_text(plain(render(result)))
                self.query_one('#delivery-status', Static).update('History from '+str(folder)+'. Select Audit history again to refresh.')
                return
            study = self.path('#delivery-study')
            result = await asyncio.to_thread(write_policy, study, self.path('#delivery-policy')) if event.button.id == 'delivery-save' else await asyncio.to_thread(compare, study)
            lines = ['Task group: '+result['cohort'], 'Baseline: '+result['baseline'],
                     'Lower-cost candidate: '+(result['winner'] or 'None; keep the baseline'),
                     'Decision: '+result['reason'], 'Cost basis: '+result['cost_basis'], '']
            for row in result['candidates']:
                lines.append(f"{row['id']}: {row['accepted']}/{row['attempted']} accepted; USD {row['known_cost_usd']} recorded; "
                             f"USD {row['usd_per_accepted_delivery'] or 'Unknown'} per accepted delivery")
                lines.extend('  '+issue for issue in row['issues'])
            lines.extend(['', result['cost_scope'], '', result['basis']])
            if result.get('policy'):
                lines.extend(['', 'Saved preference: '+result['policy'], result['activation']])
            self.query_one('#delivery-result', TextArea).load_text(plain('\n'.join(lines)))
            self.query_one('#delivery-status', Static).update('Recorded evidence only; no model calls. New experiments can continue in the original study.')
            winner = bool(result['winner'])
        except Exception as exc:
            self.query_one('#delivery-result', TextArea).load_text('No current comparison. Correct the path or reported evidence issue and compare again.')
            self.query_one('#delivery-status', Static).update(plain('Comparison unavailable: '+str(exc)))
        finally:
            self.running = False
            for widget in self.query('Input, Button'):
                widget.disabled = False
            self.query_one('#delivery-save', Button).disabled = not winner

    def action_close(self):
        if not self.running:
            self.dismiss(None)


class ConversationWorkbench:
    def workbench_scope(self):
        return (*self.project_navigation_scope(), self.assistant_session, self.selection().mode)

    def add_workbench_draft(self, scope, row, excerpt, comment, *, skill=False):
        if self.busy or self.sending_message or scope != self.workbench_scope():
            raise ValueError('Conversation changed or is running. Reopen this view before adding to the draft.')
        prompt = self.query_one('#prompt', TextArea)
        original = prompt.text
        if skill:
            from .console_resources import preview_skill
            snapshot = preview_skill(Path(self.workspace_root), row['path'], row['sha256'])
            draft = (original.rstrip()+'\n\n' if original.strip() else '') + (
                'Use only this explicitly selected skill snapshot. No auxiliary files, scripts or dependencies '
                'are trusted or granted permissions. Do not replace it by reading a mutable SKILL.md.\n' +
                json.dumps({'project': str(Path(self.workspace_root).resolve()), 'path': row['path'],
                            'sha256': row['sha256'], 'size_bytes': row['size_bytes'],
                            'text': snapshot}, ensure_ascii=False))
            if len(draft.encode('utf-8')) > 16384:
                raise ValueError('The combined draft exceeds 16 KiB.')
        else:
            draft = correction_draft(original, row, excerpt, comment)
        prompt.load_text(draft)
        try:
            self.checkpoint('draft_saved')
        except Exception as exc:
            prompt.load_text(original)
            raise ValueError('Draft could not be saved: '+str(exc)) from exc
        self.set_status('Added to your draft. Review it and Send when ready.')

    def action_artifacts(self):
        self.open_workbench('artifacts')

    def action_tasks(self):
        self.open_workbench('tasks')

    def action_team(self):
        self.open_workbench('team')

    def action_usage(self):
        self.open_workbench('usage')

    def action_delivery(self):
        if not self.busy and not self.sending_message and self.navigation_matches_workspace():
            self.push_screen(DeliveryComparisonPanel(self.workspace_root))

    def action_challenger(self):
        from .console_challenger import ChallengerPanel
        from .challenger import directory
        if not self.sending_message and self.navigation_matches_workspace():
            try:
                directory(self.workspace_root)
                self.push_screen(ChallengerPanel(self.workspace_root))
            except ValueError as exc:
                self.set_status(str(exc))

    def action_delegation(self):
        from .console_delegation import DelegationPanel
        self.push_screen(DelegationPanel())

    def action_routes(self):
        from .console_routes import RoutesPanel
        self.push_screen(RoutesPanel())

    def action_guidance(self):
        from .console_guidance import GuidancePanel
        self.push_screen(GuidancePanel())

    def guidance_landed(self):
        """The startup reading of what each CLI adds is done, or could not run: an open /guidance lists it now."""
        from .console_guidance import GuidancePanel
        self.guidance_reading = False
        for screen in self.screen_stack:
            if isinstance(screen, GuidancePanel):
                screen.reload()

    def action_fleet(self):
        from .console_fleet import FleetPanel
        self.push_screen(FleetPanel())

    def action_setup(self):
        from .console_setup import SetupPanel
        self.push_screen(SetupPanel())

    def action_storage(self):
        from .console_storage import StoragePanel
        from .native_installation import storage_root
        from .native_product import product_home
        try:
            self.push_screen(StoragePanel(self.storage_preferred or storage_root(), product_home()))
        except (OSError, ValueError) as exc:
            self.set_status(str(exc))

    @work(thread=True, group='cli-discovery')
    def run_cli_discovery(self):
        from .cli_discovery import discover, summary
        try:
            report = discover()
        except Exception as exc:  # Optional convenience: report it and keep the console usable.
            self.call_from_thread(self.notify, 'CLI discovery failed: '+type(exc).__name__+': '+str(exc)[:200],
                                  severity='warning', timeout=12)
            self.call_from_thread(self.guidance_landed)  # nothing will be read: /guidance shows the last reading
            return
        # News goes to the status line; only a CLI that is installed but not answering interrupts.
        if message := summary(r for r in report if r['added']):
            self.call_from_thread(self.set_status, message)
        if message := summary(r for r in report if r['status'] == 'failed'):
            self.call_from_thread(self.notify, message, severity='warning', timeout=12)
        # Before the quota refresh, which took three minutes in one real start while /guidance waited behind it.
        try:  # what each CLI adds on its own (/guidance): no tokens; Claude Code is pointed at a local recorder
            from .guidance import news, refresh_readings
            refresh_readings([r['endpoint'] for r in report if r.get('status') in {'ready', 'found'}])
            if message := news():
                self.call_from_thread(self.set_status, message)
        except Exception:  # optional: /guidance keeps the last reading and says when it was taken
            pass
        self.call_from_thread(self.guidance_landed)
        try:  # what each provider has left, so a spent plan is not chosen; no model call
            from . import provider_quota
            provider_quota.refresh()
        except Exception:  # optional: without a reading tare learns a spent plan from the failed call, as before
            pass

    @work(thread=True, group='provider-quota')
    def read_provider_quota(self, state):
        try:  # no model call; the panel shows the saved reading until this one lands
            from . import provider_quota
            provider_quota.refresh()
        except Exception:  # optional: the saved reading stays on screen
            pass
        state['reading'] = False

    def check_storage_space(self):
        from .storage import GIB, low_space
        from .native_product import product_home
        if self.storage_preferred is None:
            return
        free = low_space(self.storage_preferred, product_home())
        # Low space is a lasting condition: the header shows it while it lasts. Only the
        # drop below 5 GiB, where running work may fail, interrupts with a notification.
        critical = free is not None and free < 5*GIB
        if critical and not getattr(self, '_storage_critical', False):
            self.notify(f'Disk almost full: {free/GIB:.1f} GiB free. Running work may fail; '
                        'open /storage to review cleanup.', severity='error', timeout=15)
        self._storage_critical = critical
        note = '' if free is None else f'Low disk: {free/GIB:.1f} GiB free · /storage'
        if note != getattr(self, '_storage_warning', ''):
            self._storage_warning = note
            if self.header_identity is not None:
                self.render_header()

    def action_skills(self):
        self.open_workbench('skills')

    def action_tools(self):
        self.open_workbench('tools')

    def open_artifact(self, row):
        self.open_workbench('artifacts', rows=[row])

    def open_workbench(self, kind, *, rows=None):
        if isinstance(self.screen, WorkbenchPanel):
            return False
        if kind not in {'tasks', 'usage', 'team'} and not self.navigation_matches_workspace():
            return False
        if Path(self.selection().root).resolve() != Path(self.workspace_root).resolve():
            self.notify('Apply the workspace change first.')
            return False
        scope = self.workbench_scope()
        root = Path(self.workspace_root)
        if self.selection().mode != 'assistant' and kind not in {'tasks', 'usage'}:
            self.notify('Open a direct conversation to use this view.')
            return False
        if kind == 'team':
            from .execution_team import saved_rows
            def refresh():
                if scope != self.workbench_scope():
                    return [{'id': 'changed', 'label': 'Conversation changed',
                             'body': 'Reopen Team to inspect the selected conversation.'}]
                return saved_rows(self.assistant_session, root, self.assistant_history)
            panel = WorkbenchPanel('Execution team · roles, models and reasoning', refresh(), kind=kind,
                                   refresh_rows=refresh, refresh_interval=5)
        elif kind == 'tasks':
            def refresh():
                profile = ('Custom profile' if self.active and self.active.profile else
                           self.active.deployment if self.active else '')
                return observed_tasks(self.assistant_history, busy=self.busy, result=self.last_result,
                                      profile=profile)
            def cancel():
                # Do not cancel an execution started after this view was opened.
                if self.active is active and self.busy:
                    self.action_cancel()
            active = self.active
            panel = WorkbenchPanel('Tasks and agents · observed activity', refresh(), kind=kind,
                                   refresh_rows=refresh, cancel=cancel if self.busy else None)
        elif kind == 'usage':
            if self.busy or self.sending_message:
                rows = [{'id': 'unavailable', 'label': 'Usage unavailable while running',
                         'body': ('Wait for the conversation to finish, then reopen /usage. '
                                  'The panel does not read an active writer’s session files.')}]
            elif (not self.assistant_session or
                  not (Path(self.assistant_session)/'native-agent.json').is_file() or
                  not (Path(self.assistant_session)/'journal.jsonl').is_file() or
                  not (Path(self.assistant_session)/'writer.lock').is_file()):
                rows = usage_rows(None)
            else:
                try:
                    from .native_review import review
                    from .native_profiles import usage
                    review(self.assistant_session, project=root, include_diffs=False)
                    rows = usage_rows(usage(self.assistant_session))
                except (OSError, ValueError, KeyError, TypeError, ConversationRejected) as exc:
                    self.notify('Usage unavailable: '+str(exc), severity='error')
                    return False
            if not self.busy and not self.sending_message:
                from .flow_usage import flow_usage_rows
                try:
                    flow_rows = flow_usage_rows(root, proposal=self.entry_proposal[1] if self.entry_proposal else None,
                                                demand=self.demand_state)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self.notify('Flow usage unavailable: '+str(exc), severity='error')
                    return False
                own = list(rows) if self.assistant_session else []
                if self.assistant_session:
                    try:
                        own += heads_up_rows(self.assistant_session)
                    except Exception:
                        pass  # The side agent's row failing must not hide ANY other Usage row.
                rows = own + flow_rows or rows
                baseline = None
                try:
                    from .behavioral_profiles import selected_baseline
                    from .native_generation import load_deployment
                    selection = self.selection()
                    # Only the explicit selected profile, never a roster or receipt-supplied path.
                    if selection.profile:
                        baseline = selected_baseline(load_deployment(selection.profile), selection.deployment)
                except Exception:
                    pass  # Baseline failure must not hide ANY other Usage row.
                try:
                    from .flow_usage import completed_work_usage_rows
                    from . import relay_mesh
                    # Same control owner used by the backend for Work execution; never the project root.
                    rows += completed_work_usage_rows(Path(relay_mesh.RELAY_ROOT), include_provider_pilot=True,
                                                      include_behavioral_profiles=True, baseline=baseline)
                except Exception:
                    rows.append({'id': 'completed-work-review',
                                 'label': 'Completed Work review · unavailable',
                                 'body': 'Saved control-owner Work review unavailable. Other usage is unchanged.'})
                try:
                    from .test_selection_pilot import usage_rows as pilot_usage
                    rows += pilot_usage()
                except (OSError,ValueError,KeyError,TypeError):
                    rows.append({'id':'test-selection-unavailable',
                        'label':'Test selection · unavailable',
                        'body':'The pilot could not be read. Run tare test-selection status. Other usage is unchanged.'})
            def review_delivery(row):
                if self.busy or self.sending_message or scope != self.workbench_scope():
                    self.notify('The conversation changed. Reopen Usage before reviewing a delivery.')
                    return
                from . import relay_mesh
                from .delivery_review_panel import DeliveryReviewPanel
                control = Path(relay_mesh.RELAY_ROOT)

                def saved(changed):
                    if changed and not self.busy and not self.sending_message:
                        from .flow_usage import completed_work_usage_rows
                        refreshed = completed_work_usage_rows(control, include_behavioral_profiles=True, baseline=baseline)
                        replacement = next((r for r in refreshed if r['id'] == row['id']), None)
                        if replacement:
                            panel.rows = [replacement if r['id'] == row['id'] else r for r in panel.rows]
                            panel.render_rows()
                self.push_screen(DeliveryReviewPanel(control, row['review_targets']), saved)

            # Only the interactive entry asks the vendors' CLIs (like CLI discovery); tests show the saved reading.
            quota = {'reading': self.asks_providers and os.environ.get('TARE_CLI_DISCOVERY') != '0'}
            panel = WorkbenchPanel('Usage · provider quota, conversation, flow selection and saved Work',
                                   [quota_row(quota['reading']), *rows], kind=kind,
                                   review=review_delivery if not self.busy and not self.sending_message else None,
                                   refresh_rows=(lambda: [quota_row(quota['reading']), *panel.rows[1:]])
                                   if quota['reading'] else None)
            if quota['reading']:
                self.read_provider_quota(quota)
        elif kind == 'artifacts':
            if rows is None:
                rows = [artifact('Response '+str(i+1), v['body'], history_index=i)
                        for i, v in enumerate(self.assistant_history)
                        if isinstance(v, dict) and v.get('kind') == 'execution-result' and v.get('body')]
            panel = WorkbenchPanel('Artifacts · captured responses and diffs', rows, kind=kind,
                                   submit=lambda *args: self.add_workbench_draft(scope, *args))
        elif kind == 'skills':
            from .console_resources import discover_skills, preview_skill
            try:
                rows = [dict(r, id=r['path'], label=r['path']+' · '+r['sha256']+' · '+str(r['size_bytes'])+' B')
                        for r in discover_skills(root)]
            except (OSError, ValueError) as exc:
                self.notify(str(exc), severity='error')
                return False
            panel = WorkbenchPanel('Project skills', rows, kind=kind,
                                   preview=lambda r: ('Project: '+str(root.resolve())+'\nPath: '+r['path']+
                                       '\nSHA-256: '+r['sha256']+'\nSize: '+str(r['size_bytes'])+
                                       ' bytes\n\n'+preview_skill(root, r['path'], r['sha256'])),
                                   submit=lambda *args: self.add_workbench_draft(scope, *args, skill=True))
        else:
            # Reuse the owner that distinguishes saved authority from new-session defaults.
            from .project_permissions import permissions_view
            from . import relay_mesh
            try:
                value = permissions_view(self.permissions_directory(root), root, relay_mesh.RELAY_ROOT,
                                         session=self.assistant_session, pending=self.selection(),
                                         judges_off=self.judges_off(), local_only=self.local_only(),
                                         local_model=self.local_model(self.selection().deployment))
            except (OSError, ValueError) as exc:
                self.notify('Tool scope unavailable: '+str(exc), severity='error')
                return False
            panel = WorkbenchPanel('Tools · effective conversation scope', [
                {'id': 'scope', 'label': 'Available tools and scope', 'body': value['effective_text']},
                {'id': 'instructions', 'label': 'Project instructions', 'body': value['instructions_text']}], kind=kind)
        self.push_screen(panel)
        return True

"""Review configured checks and finite historical pipe commands without a model."""
import asyncio
from copy import deepcopy
import json
import time
from pathlib import Path
from threading import Event, Lock
import uuid

from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from .console_output import plain
from .console_panels import SupportPanel


def duration(value):
    return f'{float(value or 0)/1000:.1f}s'


class CheckRunConfirmDialog(ModalScreen):
    AUTO_FOCUS = '#check-confirm-cancel'
    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel', priority=True),
    ]
    DEFAULT_CSS = '''
    CheckRunConfirmDialog {
        align: center middle;
        background: $background 80%;
    }
    #check-confirm-dialog {
        width: 70;
        max-width: 90%;
        max-height: 95%;
        height: auto;
        background: $panel;
        border: round $warning 40%;
        padding: 1 2;
    }
    #check-confirm-dialog:focus-within {
        border: round $warning;
    }
    #check-confirm-title {
        height: 1;
        text-style: bold;
        color: $foreground;
        margin-bottom: 1;
    }
    #check-confirm-scroll {
        height: auto;
        max-height: 7;
        overflow-y: auto;
        margin-bottom: 1;
    }
    #check-confirm-command {
        height: auto;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    #check-confirm-details {
        height: auto;
        color: $foreground;
        margin-bottom: 1;
    }
    #check-confirm-warning {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #check-confirm-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #check-confirm-actions Button {
        height: 1;
        min-height: 1;
        width: auto;
        min-width: 14;
        padding: 0 1;
        border: none;
        background: transparent;
        color: $text-muted;
        margin-left: 1;
    }
    #check-confirm-actions Button:hover,
    #check-confirm-actions Button:focus,
    #check-confirm-actions Button.-active {
        background: $primary 20%;
        color: $foreground;
    }
    #check-confirm-actions Button.panel-danger {
        color: $warning;
        text-style: bold;
    }
    '''

    def __init__(self, *, check_id, argv, repository, timeout_seconds, plan):
        super().__init__()
        self.check_id = str(check_id)
        self.argv = list(argv) if isinstance(argv, list) else [str(argv)]
        self.repository = str(repository or '')
        self.timeout_seconds = int(timeout_seconds or 0)
        self.plan = str(plan or '')

    def compose(self):
        with Vertical(id='check-confirm-dialog'):
            yield Static('Confirm check execution', id='check-confirm-title', markup=False)
            with VerticalScroll(id='check-confirm-scroll'):
                yield Static(json.dumps(self.argv, indent=2, ensure_ascii=True), id='check-confirm-command', markup=False)
                repo_str = f"Repository: {plain(self.repository)}" if self.repository else "Repository: project root"
                details_str = f"{repo_str} · Timeout: {self.timeout_seconds}s"
                yield Static(details_str, id='check-confirm-details', markup=False)
            warning = (
                '• This check executes again and may modify files or external systems.\n'
                '• No automatic retry.\n'
                '• Live output is recorded.'
            )
            yield Static(warning, id='check-confirm-warning', markup=False)
            with Horizontal(id='check-confirm-actions', classes='panel-actions'):
                yield Button('Cancel · Esc', id='check-confirm-cancel')
                yield Button('Confirm run', id='check-confirm-confirm', classes='panel-danger')

    def on_mount(self):
        self.query_one('#check-confirm-cancel', Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    def action_confirm(self):
        self.dismiss(True)

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id in ('check-confirm-confirm', 'check-confirm-run'):
            self.action_confirm()
        elif event.button.id == 'check-confirm-cancel':
            self.action_cancel()


class ReviewChecksPanel(SupportPanel):
    BINDINGS = [
        Binding('escape', 'close', 'Back', priority=True),
        Binding('f3', 'close', 'Back', priority=True),
        Binding('ctrl+r', 'reload', 'Refresh', priority=True),
    ]
    DEFAULT_CSS = '''
    ReviewChecksPanel {
        align: center middle;
        background: $background 80%;
    }
    #review-checks-body {
        width: 96%;
        max-width: 120;
        height: 94%;
        padding: 0 1;
        background: $background;
        border: round $primary 35%;
    }
    ReviewChecksPanel.narrow #review-checks-body {
        width: 100%;
        height: 100%;
        border: none;
    }
    #review-checks-selectors {
        height: 1;
        margin-bottom: 1;
    }
    #review-checks-check {
        width: 1fr;
        margin-right: 1;
    }
    #review-checks-history {
        width: 1fr;
    }
    #review-checks-details-scroll {
        height: 4;
        max-height: 5;
        margin-bottom: 1;
        scrollbar-size: 1 1;
        padding: 0;
    }
    ReviewChecksPanel.narrow #review-checks-details-scroll {
        height: 4;
        max-height: 4;
    }
    #review-checks-details {
        height: auto;
        color: $foreground;
    }
    #review-checks-status {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #review-checks-output-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
        border: solid $primary 20%;
        padding: 0 1;
    }
    #review-checks-output {
        height: auto;
        color: $foreground;
    }
    #review-checks-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #review-checks-actions Button {
        min-width: 11;
        margin-left: 1;
    }
    ReviewChecksPanel.narrow #review-checks-actions Button {
        min-width: 10;
    }
    '''

    def __init__(self, session, project, invocation_id=None, *, read_lock=None, reader=None, runner=None, run_reader=None):
        super().__init__()
        self.session = Path(session)
        self.project = Path(project)
        self.invocation_id = str(invocation_id) if invocation_id is not None else None
        self.read_lock = read_lock if read_lock is not None else Lock()
        self.reader = reader
        self.runner = runner
        self.run_reader = run_reader

        self.checks = []
        self.checks_by_id = {}
        self.selected_check_id = None

        self.runs = []
        self.runs_by_id = {}
        self.selected_run_id = None

        self.current_output = ''
        self.revision = 0
        self.running = False
        self.cancel_event = None
        self.active_run_id = None
        self.active_confirmation = None
        self.poll_timer = None
        self.mutation_result = None

    def read_inventory(self):
        with self.read_lock:
            func = self.reader
            if func is None:
                from .native_review_checks import review_checks
                func = review_checks
            return func(self.session, project=self.project, invocation_id=self.invocation_id)

    def execute_check(self, check_id, expected_plan, run_id, cancelled):
        with self.read_lock:
            func = self.runner
            if func is None:
                from .native_review_checks import run_check
                func = run_check
            return func(
                self.session,
                project=self.project,
                invocation_id=self.invocation_id,
                check_id=check_id,
                expected_plan=expected_plan,
                run_id=run_id,
                cancelled=cancelled,
            )

    def read_run_output(self, run_id, max_bytes=65536):
        # Reads of live output must NOT wait on read_lock while execution holds it.
        func = self.run_reader
        if func is None:
            from .native_review_checks import read_check_run
            func = read_check_run
        return func(self.session, run_id=run_id, max_bytes=max_bytes)

    def compose(self):
        with Vertical(id='review-checks-body', classes='support-body'):
            with Horizontal(classes='panel-header'):
                title = f"Checks · {plain(self.project.name)}"
                if self.invocation_id:
                    title += ' · selected turn'
                yield Static(title, id='review-checks-heading', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='review-checks-close')
            with Horizontal(id='review-checks-selectors'):
                yield Select([], prompt='Choose a check', id='review-checks-check')
                yield Select([], prompt='Choose a run', id='review-checks-history')
            with VerticalScroll(id='review-checks-details-scroll'):
                yield Static('Loading check details…', id='review-checks-details', markup=False)
            yield Static('', id='review-checks-status', markup=False)
            with VerticalScroll(id='review-checks-output-scroll'):
                yield Static('No check output available.', id='review-checks-output', markup=False)
            with Horizontal(id='review-checks-actions', classes='panel-actions'):
                yield Button('Run check', id='review-checks-run', classes='panel-primary', disabled=True)
                yield Button('Cancel run', id='review-checks-cancel', classes='panel-danger', disabled=True)
                yield Button('Refresh', id='review-checks-refresh')
                yield Button('Copy output', id='review-checks-copy', disabled=True)
                yield Button('Back', id='review-checks-back')

    def on_mount(self):
        if self.app.size.width < 100 or self.app.size.height < 30:
            self.add_class('narrow')
        self.action_reload()

    def on_unmount(self):
        self.dismiss_active_confirmation()
        if self.running and self.cancel_event is not None:
            self.cancel_event.set()
        if self.poll_timer is not None:
            try:
                self.poll_timer.stop()
            except Exception:
                pass
            self.poll_timer = None

    def view_active(self):
        return (
            self.is_mounted and
            bool(self.query_one_optional('#review-checks-details')) and
            bool(self.query_one_optional('#review-checks-output')) and
            bool(self.query_one_optional('#review-checks-check'))
        )

    def action_close(self):
        if self.running:
            return
        self.dismiss_active_confirmation()
        self.dismiss()

    def dismiss_active_confirmation(self):
        if self.active_confirmation is not None:
            try:
                self.active_confirmation.dismiss(False)
            except Exception:
                pass
            self.active_confirmation = None

    def format_run_status(self, run):
        status = run.get('status', 'UNKNOWN')
        if status == 'RUNNING' and run.get('run_id') != self.active_run_id:
            return 'UNKNOWN'
        return status

    def _matching_runs(self):
        if not self.selected_check_id:
            return self.runs
        return [r for r in self.runs if not r.get('check_id') or r.get('check_id') == self.selected_check_id]

    def _update_check_details_view(self):
        if not self.selected_check_id or self.selected_check_id not in self.checks_by_id:
            self.query_one('#review-checks-details', Static).update('No check selected.')
            return

        check = self.checks_by_id[self.selected_check_id]
        lines = []
        repo = check.get('repository')
        timeout = check.get('timeout_seconds', 0)
        lines.append(f"Repository: {plain(repo if repo else 'project root')} · Timeout: {timeout}s")
        if check.get('disabled_reason'):
            lines.append(f"Disabled: {plain(check['disabled_reason'])}")
        lines.append('Command: '+json.dumps(check.get('argv', []), ensure_ascii=True))
        self.query_one('#review-checks-details', Static).update('\n'.join(lines))

    def _update_history_selector(self, selected_run_id=None):
        matching = self._matching_runs()
        hist_sel = self.query_one('#review-checks-history', Select)

        with self.prevent(Select.Changed):
            if not matching:
                hist_sel.set_options([('No runs recorded', '')])
                hist_sel.value = ''
                self.selected_run_id = None
                self.current_output = ''
                self.query_one('#review-checks-status', Static).update(
                    self.mutation_result if selected_run_id and self.mutation_result else 'No runs recorded for this check.')
                self.query_one('#review-checks-output', Static).update('No check output available.')
                self.query_one('#review-checks-copy', Button).disabled = True
            else:
                options = []
                for i, r in enumerate(matching):
                    st = self.format_run_status(r)
                    elapsed = duration(r.get('elapsed_ms'))
                    label = f"{plain(r.get('label', r.get('run_id', f'Run {i+1}')))} · {st} ({elapsed})"
                    freshness = r.get('freshness')
                    if freshness and st == 'PASS':
                        freshness_label = {
                            'OBSERVED_FILES_MATCH': 'observed files match',
                            'STALE': 'STALE · later changes',
                            'UNKNOWN': 'UNKNOWN · incomplete observations',
                        }.get(freshness, freshness)
                        label += f" · {freshness_label}"
                    options.append((label, r['run_id']))
                hist_sel.set_options(options)

                if selected_run_id and not any(r['run_id'] == selected_run_id for r in matching):
                    hist_sel.clear()
                    self.selected_run_id = None
                    self.query_one('#review-checks-status', Static).update(
                        self.mutation_result or 'The requested result is not in saved history.')
                    return

                target_run_id = None
                if selected_run_id and any(r['run_id'] == selected_run_id for r in matching):
                    target_run_id = selected_run_id
                elif self.selected_run_id and any(r['run_id'] == self.selected_run_id for r in matching):
                    target_run_id = self.selected_run_id
                else:
                    target_run_id = matching[0]['run_id']

                hist_sel.value = target_run_id
                self.selected_run_id = target_run_id
                self.show_run_output(target_run_id)

    def _update_action_buttons(self):
        if not self.is_mounted:
            return
        try:
            run_btn = self.query_one('#review-checks-run', Button)
            cancel_btn = self.query_one('#review-checks-cancel', Button)
            refresh_btn = self.query_one('#review-checks-refresh', Button)
            copy_btn = self.query_one('#review-checks-copy', Button)
            close_btn = self.query_one('#review-checks-close', Button)
            back_btn = self.query_one('#review-checks-back', Button)
            check_sel = self.query_one('#review-checks-check', Select)
            hist_sel = self.query_one('#review-checks-history', Select)
        except Exception:
            return

        if self.running:
            run_btn.disabled = True
            cancel_btn.disabled = False
            refresh_btn.disabled = True
            copy_btn.disabled = not bool(self.current_output)
            close_btn.disabled = True
            back_btn.disabled = True
            check_sel.disabled = True
            hist_sel.disabled = True
        else:
            check = self.checks_by_id.get(self.selected_check_id)
            can_run = bool(check and check.get('plan') and not check.get('disabled_reason'))
            run_btn.disabled = not can_run
            cancel_btn.disabled = True
            refresh_btn.disabled = False
            copy_btn.disabled = not bool(self.current_output)
            close_btn.disabled = False
            back_btn.disabled = False
            check_sel.disabled = False
            hist_sel.disabled = False

    @work(exclusive=True, group='review-checks-load')
    async def action_reload(self, selected_run_id=None):
        if self.running:
            return
        self.dismiss_active_confirmation()
        self.revision += 1
        revision = self.revision
        self.checks_by_id = {}
        self.runs_by_id = {}
        self._update_action_buttons()

        try:
            result = await asyncio.to_thread(self.read_inventory)
            if not self.view_active() or self.revision != revision:
                return
            if not isinstance(result, dict):
                raise ValueError("Invalid review checks response received.")
            if result.get('invocation_id') != self.invocation_id:
                raise ValueError('Check inventory belongs to another turn.')

            self.checks = deepcopy(result.get('checks', []))
            self.checks_by_id = {c['id']: c for c in self.checks if 'id' in c}
            self.runs = deepcopy(result.get('runs', []))
            self.runs_by_id = {r['run_id']: r for r in self.runs if 'run_id' in r}

            with self.prevent(Select.Changed):
                check_sel = self.query_one('#review-checks-check', Select)
                if not self.checks:
                    check_sel.set_options([('No checks configured', '')])
                    check_sel.value = ''
                    self.selected_check_id = None
                else:
                    options = []
                    for i, c in enumerate(self.checks):
                        label = plain(c.get('label', c.get('id', f'Check {i+1}')))
                        if c.get('disabled_reason'):
                            label += f" · disabled: {plain(c['disabled_reason'])}"
                        options.append((label, c['id']))
                    check_sel.set_options(options)
                    if self.selected_check_id in self.checks_by_id:
                        check_sel.value = self.selected_check_id
                    else:
                        self.selected_check_id = self.checks[0]['id']
                        check_sel.value = self.selected_check_id

            if not self.checks:
                msg = (
                    "No review checks configured or completed commands in this selection.\n"
                    "Run a finite test command in the conversation, then return here."
                )
                self.query_one('#review-checks-details', Static).update(msg)
                self.query_one('#review-checks-status', Static).update('Configuration empty.')
                self.query_one('#review-checks-output', Static).update('No check output available.')
                self.current_output = ''
                self._update_history_selector(selected_run_id=selected_run_id)
                self._update_action_buttons()
                return

            self._update_check_details_view()
            self._update_history_selector(selected_run_id=selected_run_id)
            self._update_action_buttons()

        except Exception as exc:
            if self.view_active() and self.revision == revision:
                self.query_one('#review-checks-details', Static).update('Checks unavailable: ' + plain(str(exc)))
                self.query_one('#review-checks-status', Static).update('Error loading checks.')
                self._update_action_buttons()

    @work(exclusive=True, group='review-checks-output')
    async def show_run_output(self, run_id):
        if not run_id or run_id not in self.runs_by_id:
            return
        run = self.runs_by_id[run_id]
        st = self.format_run_status(run)
        lines = [f"{plain(run.get('label','Check'))} · {st}"]
        if run.get('elapsed_ms') is not None:
            lines[0] += ' · '+duration(run['elapsed_ms'])
        if run.get('returncode') is not None:
            lines[0] += f" (code: {run['returncode']})"
        if run.get('freshness') and st == 'PASS':
            freshness_label = {
                'OBSERVED_FILES_MATCH': 'observed files match',
                'STALE': 'STALE · later changes',
                'UNKNOWN': 'UNKNOWN · incomplete observations',
            }.get(run['freshness'], run['freshness'])
            lines.append(f"Freshness: {freshness_label}")
            lines.append('Observed files only; other test inputs may have changed.')
        if run.get('reason'):
            lines.append(f"Reason: {plain(run['reason'])}")

        if self.view_active():
            self.query_one('#review-checks-status', Static).update('\n'.join(lines))
            self.query_one('#review-checks-output', Static).update('Loading output…')

        try:
            res = await asyncio.to_thread(self.read_run_output, run_id)
            if not self.view_active() or self.running or self.selected_run_id != run_id:
                return
            if isinstance(res, dict):
                if res.get('run_id') != run_id:
                    raise ValueError('Output belongs to another check run.')
                output = plain(res.get('output', ''))
                self.current_output = output
                truncated = res.get('output_truncated', False)
                display_text = plain(output) if output else '(No output recorded)'
                if truncated:
                    display_text += '\n[Output truncated at limit]'
                self.query_one('#review-checks-output', Static).update(display_text)
                self.query_one('#review-checks-copy', Button).disabled = not bool(output)
                try:
                    self.query_one('#review-checks-output-scroll', VerticalScroll).scroll_home(animate=False)
                except Exception:
                    pass
            else:
                self.query_one('#review-checks-output', Static).update('Output unavailable.')
                self.query_one('#review-checks-copy', Button).disabled = True
        except Exception as exc:
            if self.view_active() and self.selected_run_id == run_id:
                self.query_one('#review-checks-output', Static).update('Output unavailable: ' + plain(str(exc)))
                self.query_one('#review-checks-copy', Button).disabled = True

    def request_run(self):
        if self.running or self.active_confirmation is not None:
            return
        if not self.selected_check_id or self.selected_check_id not in self.checks_by_id:
            return
        check = self.checks_by_id[self.selected_check_id]
        if check.get('disabled_reason') or not check.get('plan'):
            return

        target_check_id = self.selected_check_id
        target_generation = self.revision
        target_plan = check.get('plan')
        target_argv = check.get('argv', [])
        target_repo = check.get('repository', '')
        target_timeout = check.get('timeout_seconds', 0)

        dialog = CheckRunConfirmDialog(
            check_id=target_check_id,
            argv=target_argv,
            repository=target_repo,
            timeout_seconds=target_timeout,
            plan=target_plan,
        )
        self.active_confirmation = dialog

        def on_dismiss(confirmed):
            self.active_confirmation = None
            if not confirmed:
                return
            if (not self.view_active() or
                self.running or
                self.revision != target_generation or
                self.selected_check_id != target_check_id or
                self.checks_by_id.get(target_check_id, {}).get('plan') != target_plan):
                return
            self.perform_run(target_check_id, target_plan, target_generation)

        self.app.push_screen(dialog, on_dismiss)

    @work(exclusive=False)
    async def perform_run(self, check_id, plan, generation):
        if (not self.view_active() or
            self.running or
            self.revision != generation or
            self.selected_check_id != check_id or
            self.checks_by_id.get(check_id, {}).get('plan') != plan):
            return

        check = self.checks_by_id[check_id]
        if check.get('disabled_reason') or not plan:
            return

        self.running = True
        run_id = uuid.uuid4().hex
        self.active_run_id = run_id
        self.selected_run_id = None
        self.cancel_event = Event()
        self.current_output = ''

        self._update_action_buttons()
        self.started_at = time.monotonic()
        self.query_one('#review-checks-status', Static).update('RUNNING · starting check')
        self.query_one('#review-checks-output', Static).update('Starting check execution…')

        # Live output polling timer
        self.poll_timer = self.set_interval(0.1, self._poll_output)

        try:
            receipt = await asyncio.to_thread(
                self.execute_check,
                check_id,
                plan,
                run_id,
                self.cancel_event.is_set,
            )
            if not self.view_active():
                return
            if not isinstance(receipt, dict):
                raise ValueError("Invalid check execution receipt received.")
            if receipt.get('run_id') != run_id or receipt.get('check_id') != check_id:
                raise ValueError('Execution result belongs to another check run.')

            status = receipt.get('status', 'UNKNOWN')
            reason = receipt.get('reason')
            elapsed = receipt.get('elapsed_ms')
            returncode = receipt.get('returncode')

            msg = f"{plain(check.get('label','Check'))} · {status}"
            if elapsed is not None:
                msg += ' · '+duration(elapsed)
            if returncode is not None:
                msg += f" (code: {returncode})"
            if reason:
                msg += f" · {plain(reason)}"
            self.mutation_result = msg
        except Exception as exc:
            self.mutation_result = f"Run error: {plain(str(exc))}"
        finally:
            if self.poll_timer is not None:
                try:
                    self.poll_timer.stop()
                except Exception:
                    pass
                self.poll_timer = None
            self.running = False
            self.active_run_id = None
            self.checks_by_id = {}

        # Fetch final output
        try:
            final_res = await asyncio.to_thread(self.read_run_output, run_id)
            if isinstance(final_res, dict) and self.view_active():
                if final_res.get('run_id') != run_id:
                    raise ValueError('Output belongs to another check run.')
                output = plain(final_res.get('output', ''))
                self.current_output = output
                truncated = final_res.get('output_truncated', False)
                display_text = plain(output) if output else '(No output recorded)'
                if truncated:
                    display_text += '\n[Output truncated at limit]'
                self.query_one('#review-checks-output', Static).update(display_text)
                self.query_one('#review-checks-copy', Button).disabled = not bool(output)
        except Exception:
            pass

        if self.view_active():
            if self.mutation_result:
                self.query_one('#review-checks-status', Static).update(self.mutation_result)
            # Refresh run inventory and select the completed run
            self.action_reload(selected_run_id=run_id)

    async def _poll_output(self):
        if not self.running or not self.active_run_id:
            return
        run_id = self.active_run_id
        try:
            res = await asyncio.to_thread(self.read_run_output, run_id)
            if not self.view_active() or not self.running or self.active_run_id != run_id:
                return
            if not self.cancel_event.is_set():
                elapsed = time.monotonic()-self.started_at
                self.query_one('#review-checks-status', Static).update(f'RUNNING · {elapsed:.1f}s')
            if isinstance(res, dict) and res.get('run_id') == run_id:
                output = plain(res.get('output', ''))
                if output and output != self.current_output:
                    self.current_output = output
                    truncated = res.get('output_truncated', False)
                    display_text = plain(output) + ('\n[Output truncated at limit]' if truncated else '')
                    self.query_one('#review-checks-output', Static).update(display_text)
                    self.query_one('#review-checks-copy', Button).disabled = not bool(output)
        except Exception:
            pass

    def request_cancel(self):
        if not self.running or self.cancel_event is None:
            return
        self.cancel_event.set()
        if self.view_active():
            self.query_one('#review-checks-status', Static).update('Cancellation requested… awaiting worker return.')
            self.query_one('#review-checks-cancel', Button).disabled = True

    def on_select_changed(self, event):
        if self.running:
            event.stop()
            return
        self.dismiss_active_confirmation()
        if event.select.id == 'review-checks-check':
            if event.value != self.selected_check_id:
                self.selected_check_id = event.value
                self.revision += 1
                self._update_check_details_view()
                self._update_history_selector()
                self._update_action_buttons()
        elif event.select.id == 'review-checks-history':
            if event.value != self.selected_run_id and event.value in self.runs_by_id:
                self.selected_run_id = event.value
                self.show_run_output(event.value)

    def on_button_pressed(self, event):
        event.stop()
        if self.running and event.button.id not in {'review-checks-cancel','review-checks-copy'}:
            return
        if event.button.id in ('review-checks-close', 'review-checks-back'):
            self.action_close()
        elif event.button.id == 'review-checks-refresh':
            self.action_reload()
        elif event.button.id == 'review-checks-run':
            self.request_run()
        elif event.button.id == 'review-checks-cancel':
            self.request_cancel()
        elif event.button.id == 'review-checks-copy':
            if self.current_output:
                self.app.copy_to_clipboard(self.current_output)
                event.button.label = 'Copied'

"""Review historical turn changes and explicitly undo all edits of a turn."""
import asyncio
from copy import deepcopy
from pathlib import Path
from threading import Lock

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from .console_output import plain
from .console_panels import SupportPanel


class TurnUndoConfirmDialog(ModalScreen):
    AUTO_FOCUS = '#turn-undo-cancel'
    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel', priority=True),
    ]
    DEFAULT_CSS = '''
    TurnUndoConfirmDialog {
        align: center middle;
        background: $background 80%;
    }
    #turn-undo-dialog {
        width: 70;
        max-width: 90%;
        max-height: 95%;
        height: auto;
        background: $panel;
        border: round $warning 40%;
        padding: 1 2;
    }
    #turn-undo-dialog:focus-within {
        border: round $warning;
    }
    #turn-undo-title {
        height: 1;
        text-style: bold;
        color: $foreground;
        margin-bottom: 1;
    }
    #turn-undo-target {
        height: auto;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    #turn-undo-file-scroll {
        height: auto;
        max-height: 4;
        overflow-y: auto;
        scrollbar-size: 1 1;
        margin-bottom: 1;
    }
    #turn-undo-files { height: auto; color: $foreground; }
    #turn-undo-warning {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #turn-undo-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #turn-undo-actions Button {
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
    #turn-undo-actions Button:hover,
    #turn-undo-actions Button:focus,
    #turn-undo-actions Button.-active {
        background: $primary 20%;
        color: $foreground;
    }
    #turn-undo-actions Button.panel-danger {
        color: $warning;
        text-style: bold;
    }
    '''

    def __init__(self, *, invocation_id, files, file_count, edit_count, plan, turn_label='Selected turn'):
        super().__init__()
        self.invocation_id = str(invocation_id)
        self.files = list(files)
        self.file_count = int(file_count)
        self.edit_count = int(edit_count)
        self.plan = str(plan)
        self.turn_label = turn_label

    def compose(self):
        with Vertical(id='turn-undo-dialog'):
            yield Static('Confirm whole-turn undo', id='turn-undo-title', markup=False)
            target = f"{plain(self.turn_label)} ({self.file_count} file{'s' if self.file_count != 1 else ''} · {self.edit_count} edit{'s' if self.edit_count != 1 else ''})"
            yield Static(target, id='turn-undo-target', markup=False)

            file_lines = []
            for f in self.files:
                p = f"{f.get('repository') + '/' if f.get('repository') else ''}{f.get('path', '')}"
                c = f.get('edit_count', 1)
                file_lines.append(f"• {plain(p)} ({c} edit{'s' if c != 1 else ''})")
            with VerticalScroll(id='turn-undo-file-scroll'):
                yield Static('\n'.join(file_lines), id='turn-undo-files', markup=False)

            warning = (
                '• Newer or conflicting changes cause refusal.\n'
                '• Shell effects are not undone.\n'
                '• Tests must run again.\n'
                '• Files restore one at a time; a failure can leave a partial result.'
            )
            yield Static(warning, id='turn-undo-warning', markup=False)

            with Horizontal(id='turn-undo-actions', classes='panel-actions'):
                yield Button('Cancel · Esc', id='turn-undo-cancel')
                yield Button('Confirm undo turn', id='turn-undo-confirm', classes='panel-danger')

    def on_mount(self):
        self.query_one('#turn-undo-cancel', Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    def action_confirm(self):
        self.dismiss(True)

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'turn-undo-confirm':
            self.action_confirm()
        elif event.button.id == 'turn-undo-cancel':
            self.action_cancel()


class TurnChangesPanel(SupportPanel):
    BINDINGS = [
        Binding('escape', 'close', 'Back', priority=True),
        Binding('f3', 'close', 'Back', priority=True),
        Binding('ctrl+r', 'reload', 'Refresh', priority=True),
    ]
    DEFAULT_CSS = '''
    #turn-changes-selectors { height: 1; margin-bottom: 1; }
    #turn-changes-file { width: 1fr; }
    #turn-changes-summary { height: auto; margin-bottom: 1; color: $text-muted; }
    #turn-changes-scroll { height: 1fr; scrollbar-size: 1 1; }
    #turn-changes-diff { height: auto; }
    #turn-changes-scope { height: auto; margin-top: 1; color: $text-muted; }
    #turn-changes-actions Button { min-width: 11; margin-left: 1; }
    '''

    def __init__(self, session, project, invocation_id, *, read_lock=None, reader=None, undoer=None, file_reader=None, checks_reader=None, checks_runner=None, checks_run_reader=None):
        super().__init__()
        self.session = Path(session)
        self.project = Path(project)
        self.invocation_id = str(invocation_id)
        self.read_lock = read_lock if read_lock is not None else Lock()
        self.reader = reader
        self.undoer = undoer
        self.file_reader = file_reader
        self.checks_reader = checks_reader
        self.checks_runner = checks_runner
        self.checks_run_reader = checks_run_reader
        self.files = {}
        self.undo_preview = {}
        self.revision = 0
        self.diff_text = None
        self.selected_file_id = None
        self.mutating = False
        self.active_confirmation = None
        self.mutation_result = None
        self.turn_label = 'Selected turn'

    def read(self, **kwargs):
        with self.read_lock:
            func = self.reader
            if func is None:
                from .native_turn_review import review_turn
                func = review_turn
            return func(self.session, project=self.project, invocation_id=self.invocation_id, **kwargs)

    def read_file(self, effect_id, invocation_id=None):
        with self.read_lock:
            func = self.file_reader
            if func is None:
                from .native_review_actions import read_edit_file
                func = read_edit_file
            return func(self.session, project=self.project, effect_id=effect_id, invocation_id=invocation_id or self.invocation_id)

    def run_undo(self, expected_plan):
        with self.read_lock:
            func = self.undoer
            if func is None:
                from .native_turn_review import undo_turn
                func = undo_turn
            return func(self.session, project=self.project, invocation_id=self.invocation_id, expected_plan=expected_plan)

    def compose(self):
        with Vertical(id='turn-changes-body', classes='support-body'):
            with Horizontal(classes='panel-header'):
                title = f"Turn review · {plain(self.project.name)}"
                yield Static(title, id='turn-changes-heading', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='turn-changes-close')
            with Horizontal(id='turn-changes-selectors'):
                yield Select([], prompt='Choose a file', id='turn-changes-file')
            with VerticalScroll(id='turn-changes-scroll'):
                yield Static('Loading recorded turn changes…', id='turn-changes-summary', markup=False)
                yield Static('', id='turn-changes-diff', markup=False)
                yield Static(
                    'Recorded file edits from this turn. Shell changes are excluded.\n'
                    'Undo checks every file and preserves newer changes.',
                    id='turn-changes-scope', markup=False
                )
            with Horizontal(id='turn-changes-actions', classes='panel-actions'):
                yield Button('Open file', id='turn-changes-open', disabled=True)
                yield Button('Undo turn', id='turn-changes-undo', classes='panel-danger', disabled=True)
                yield Button('Checks', id='turn-changes-checks')
                yield Button('Refresh', id='turn-changes-refresh')
                yield Button('Copy diff', id='turn-changes-copy', classes='panel-primary', disabled=True)

    def on_mount(self):
        if self.app.size.width < 100 or self.app.size.height < 30:
            self.add_class('narrow')
        self.action_reload()

    def on_unmount(self):
        self.dismiss_active_confirmation()

    def view_active(self):
        return (self.is_mounted and
                bool(self.query_one_optional('#turn-changes-summary')) and
                bool(self.query_one_optional('#turn-changes-file')) and
                bool(self.query_one_optional('#turn-changes-diff')))

    def action_close(self):
        if self.mutating:
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

    def _can_open(self) -> bool:
        if self.mutating or not self.files:
            return False
        try:
            val = self.query_one('#turn-changes-file', Select).value
        except Exception:
            return False
        return bool(val and val in self.files and self.files[val].get('effect_id'))

    def _can_undo(self) -> bool:
        if self.mutating or not self.files:
            return False
        if not self.undo_preview or self.undo_preview.get('status') != 'READY':
            return False
        if not self.undo_preview.get('plan'):
            return False
        return True

    def _update_action_buttons(self):
        if not self.is_mounted:
            return
        try:
            copy_btn = self.query_one('#turn-changes-copy', Button)
            open_btn = self.query_one('#turn-changes-open', Button)
            undo_btn = self.query_one('#turn-changes-undo', Button)
            checks_btn = self.query_one('#turn-changes-checks', Button)
            refresh_btn = self.query_one('#turn-changes-refresh', Button)
            close_btn = self.query_one('#turn-changes-close', Button)
            file_sel = self.query_one('#turn-changes-file', Select)
        except Exception:
            return

        if self.mutating:
            copy_btn.disabled = True
            open_btn.disabled = True
            undo_btn.disabled = True
            checks_btn.disabled = True
            refresh_btn.disabled = True
            close_btn.disabled = True
            file_sel.disabled = True
        else:
            copy_btn.disabled = self.diff_text is None
            open_btn.disabled = not self._can_open()
            undo_btn.disabled = not self._can_undo()
            checks_btn.disabled = False
            refresh_btn.disabled = False
            close_btn.disabled = False
            file_sel.disabled = False

    @work(exclusive=True, group='turn-changes-load')
    async def action_reload(self):
        if self.mutating:
            return
        self.dismiss_active_confirmation()
        invocation_id = self.invocation_id
        self.revision += 1
        revision = self.revision
        self.diff_text = None
        self.selected_file_id = None
        self.files = {}
        self.undo_preview = {}
        try:
            copy_btn = self.query_one('#turn-changes-copy', Button)
            copy_btn.disabled = True
            copy_btn.label = 'Copy diff'
            self.query_one('#turn-changes-diff', Static).update('')
        except Exception:
            pass
        self._update_action_buttons()
        try:
            result = await asyncio.to_thread(
                self.read,
                max_diff_bytes=256*1024,
            )
            if not self.view_active() or self.revision != revision or self.invocation_id != invocation_id:
                return
            if not isinstance(result, dict):
                raise ValueError("Invalid turn review response received.")
            if result.get('invocation_id') != invocation_id:
                raise ValueError('Review returned a different turn identity.')
            number = result.get('turn_number')
            self.turn_label = f'Turn {number}' if type(number) is int and number > 0 else 'Selected turn'
            self.query_one('#turn-changes-heading',Static).update(self.turn_label+' · '+plain(self.project.name))

            files_list = result.get('files', [])
            self.files = {row['file_id']: deepcopy(row) for row in files_list}
            self.undo_preview = deepcopy(result.get('undo', {}))

            with self.prevent(Select.Changed):
                file_sel = self.query_one('#turn-changes-file', Select)
                options = []
                for i, row in enumerate(files_list):
                    repo = row.get('repository')
                    target = f"{repo}/{row.get('path', '')}" if repo else row.get('path', '')
                    edits = row.get('edit_count', 1)
                    label = f"{i+1} · {plain(target)} ({edits} edit{'s' if edits != 1 else ''})"
                    if row.get('segments', 1) > 1:
                        label += f" · {row['segments']} segments"
                    if row.get('undo_status'):
                        label += f" · {row['undo_status']}"
                    elif row.get('diff_unavailable'):
                        label += " · diff unavailable"
                    options.append((label, row['file_id']))
                file_sel.set_options(options)
                if files_list:
                    file_sel.value = files_list[0]['file_id']
                    self.selected_file_id = files_list[0]['file_id']
                else:
                    self.selected_file_id = None

            lines = []
            if self.mutation_result:
                lines.append(f"Result: {self.mutation_result}")

            total_files = len(files_list)
            total_edits = sum(f.get('edit_count', 1) for f in files_list)
            lines.append(f"{total_files} file{'s' if total_files != 1 else ''} · {total_edits} recorded edit{'s' if total_edits != 1 else ''}")

            undo_status = self.undo_preview.get('status', 'UNKNOWN')
            undo_reason = self.undo_preview.get('reason')
            if undo_status == 'READY':
                lines.append('Undo available · confirmation required')
            elif undo_status == 'UNDONE':
                lines.append("Undo: UNDONE")
            else:
                status_str = f"Undo: {undo_status}"
                if undo_reason and not self.mutation_result:
                    status_str += f" · {plain(undo_reason)}"
                lines.append(status_str)

            for f in files_list:
                f_repo = f.get('repository')
                f_path = f"{f_repo}/{f.get('path', '')}" if f_repo else f.get('path', '')
                if f.get('segments', 1) > 1:
                    lines.append(f"Notice ({plain(f_path)}): discontinuous snapshot chain ({f['segments']} segments; intervening edits preserved).")
                if f.get('diff_unavailable'):
                    lines.append(f"Notice ({plain(f_path)}): snapshot unavailable ({plain(f['diff_unavailable'])}).")

            self.query_one('#turn-changes-summary', Static).update('\n'.join(lines))
            self._update_action_buttons()

            if files_list:
                self.show_diff_for_file(files_list[0]['file_id'])
            else:
                self.query_one('#turn-changes-diff', Static).update('No file changes recorded in this turn.')
                self._update_action_buttons()
        except Exception as exc:
            if self.view_active() and self.revision == revision and self.invocation_id == invocation_id:
                self.query_one('#turn-changes-summary', Static).update('Review unavailable: ' + plain(str(exc)))
                self._update_action_buttons()

    def show_diff_for_file(self, file_id):
        if self.mutating:
            return
        self.diff_text = None
        self.selected_file_id = file_id
        try:
            copy_btn = self.query_one('#turn-changes-copy', Button)
            copy_btn.disabled = True
            copy_btn.label = 'Copy diff'
        except Exception:
            pass

        file_row = self.files.get(file_id)
        if not file_row:
            self.query_one('#turn-changes-diff', Static).update('')
            self._update_action_buttons()
            return

        diff = file_row.get('diff')
        if diff is None:
            reason = file_row.get('diff_unavailable', 'No confirmed matching snapshot.')
            text = Text('Diff unavailable: ' + plain(reason))
        else:
            self.diff_text = plain(diff)
            text = Text()
            theme = self.app.current_theme
            for line in self.diff_text.splitlines(keepends=True):
                style = (
                    theme.success if line.startswith('+')
                    else theme.error if line.startswith('-')
                    else theme.primary if line.startswith('@@')
                    else None
                )
                text.append(line, style=style)

        self.query_one('#turn-changes-diff', Static).update(text)
        self._update_action_buttons()
        try:
            self.query_one('#turn-changes-scroll', VerticalScroll).scroll_home(animate=False)
        except Exception:
            pass

    @work(exclusive=True, group='turn-changes-file-view')
    async def open_file(self):
        if self.mutating:
            return
        invocation_id = self.invocation_id
        revision = self.revision
        try:
            file_id = self.query_one('#turn-changes-file', Select).value
        except Exception:
            return
        if not file_id or file_id not in self.files:
            return
        file_row = self.files[file_id]
        effect_id = file_row.get('effect_id')
        if not effect_id:
            return
        try:
            result = await asyncio.to_thread(self.read_file, effect_id, invocation_id=invocation_id)
            if (not self.view_active() or self.mutating or self.app.screen is not self or
                self.revision != revision or
                self.invocation_id != invocation_id or
                self.query_one('#turn-changes-file', Select).value != file_id):
                return
            if not isinstance(result, dict) or 'content' not in result:
                raise ValueError('Invalid file content received.')
            from .changes_panel import FileViewer
            viewer = FileViewer(
                path=result.get('path', file_row.get('path', '')),
                content=result.get('content', ''),
                sha256=result.get('sha256'),
            )
            self.app.push_screen(viewer)
        except Exception as exc:
            if (self.view_active() and
                self.revision == revision and
                self.invocation_id == invocation_id and
                self.query_one('#turn-changes-file', Select).value == file_id):
                self.query_one('#turn-changes-summary', Static).update('File unavailable: ' + plain(str(exc)))

    def request_undo(self):
        if not self._can_undo() or self.active_confirmation is not None:
            return
        if not self.undo_preview or self.undo_preview.get('status') != 'READY':
            return
        plan = self.undo_preview.get('plan')
        if not plan:
            return

        target_turn = self.invocation_id
        target_generation = self.revision
        target_plan = plan
        target_file = self.query_one('#turn-changes-file',Select).value
        file_list = deepcopy(self.undo_preview.get('targets',[f for f in self.files.values() if f.get('undo_status') != 'UNDONE']))
        file_count = self.undo_preview.get('files', len(file_list))
        edit_count = self.undo_preview.get('edits', sum(f.get('edit_count', 1) for f in file_list))

        dialog = TurnUndoConfirmDialog(
            invocation_id=target_turn,
            files=file_list,
            file_count=file_count,
            edit_count=edit_count,
            plan=target_plan,
            turn_label=self.turn_label,
        )
        self.active_confirmation = dialog

        def on_dismiss(confirmed):
            self.active_confirmation = None
            if not confirmed:
                return
            if (not self.view_active() or
                self.mutating or
                self.revision != target_generation or
                self.invocation_id != target_turn or
                self.query_one('#turn-changes-file',Select).value != target_file or
                self.undo_preview.get('plan') != target_plan):
                return
            self.perform_undo_turn(target_turn, target_plan, target_generation)

        self.app.push_screen(dialog, on_dismiss)

    @work(exclusive=False)
    async def perform_undo_turn(self, invocation_id, plan, generation):
        if (not self.view_active() or self.mutating or self.revision != generation or
            self.invocation_id != invocation_id or self.undo_preview.get('plan') != plan or not self._can_undo()):
            return
        self.mutating = True
        self._update_action_buttons()
        self.query_one('#turn-changes-summary', Static).update('Restoring turn…')
        expected_files = self.undo_preview.get('targets',list(self.files.values()))
        try:
            result = await asyncio.to_thread(self.run_undo, expected_plan=plan)
            if not self.view_active():
                return
            if not isinstance(result, dict):
                raise ValueError('Invalid undo response received.')
            if result.get('invocation_id') != invocation_id:
                raise ValueError('Undo returned a different turn identity; refresh and inspect the receipts.')
            status = result.get('status')
            reason = result.get('reason')
            if status == 'UNDONE':
                observed = result.get('files',[])
                if (len(observed) != len(expected_files) or
                        {(f['repository'],f['path']) for f in observed} != {(f['repository'],f['path']) for f in expected_files} or
                        any(f.get('status') != 'UNDONE' for f in observed)):
                    raise ValueError('Whole-turn undo is unconfirmed: file results do not match the preview.')
                self.mutation_result = f"{self.turn_label} undone successfully. Run relevant tests again."
            elif status == 'PARTIAL':
                msg = "Turn undo PARTIAL"
                if reason:
                    msg += f": {plain(reason)}"
                else:
                    msg += ". Inspect the file results before continuing."
                self.mutation_result = msg
            elif status == 'UNKNOWN':
                msg = "Turn undo UNKNOWN"
                if reason:
                    msg += f": {plain(reason)}"
                else:
                    msg += ". Inspect the file results before continuing."
                self.mutation_result = msg
            elif status == 'REFUSED':
                msg = "Turn undo REFUSED"
                if reason:
                    msg += f": {plain(reason)}"
                self.mutation_result = msg
            else:
                self.mutation_result = f"Undo unconfirmed (status: {status})"
            if status != 'UNDONE':
                for row in result.get('files',[]):
                    self.mutation_result += '\n'+plain(str(row.get('path','')))+': '+plain(str(row.get('status','UNKNOWN')))

            self.mutating = False
            self.query_one('#turn-changes-summary', Static).update('Result: ' + self.mutation_result)
            self.action_reload()
        except Exception as exc:
            self.mutating = False
            self.undo_preview = {}
            self.mutation_result = f"Undo failed: {plain(str(exc))}"
            if self.view_active():
                self.query_one('#turn-changes-summary', Static).update(self.mutation_result)
                self._update_action_buttons()

    def on_select_changed(self, event):
        if self.mutating:
            event.stop()
            return
        self.dismiss_active_confirmation()
        self.revision += 1
        if event.select.id == 'turn-changes-file' and event.value in self.files:
            self.show_diff_for_file(event.value)

    def open_checks(self):
        if self.mutating or self.app.screen is not self:
            return
        from .review_checks_panel import ReviewChecksPanel
        kwargs = {'read_lock': self.read_lock}
        if self.checks_reader is not None:
            kwargs['reader'] = self.checks_reader
        if self.checks_runner is not None:
            kwargs['runner'] = self.checks_runner
        if self.checks_run_reader is not None:
            kwargs['run_reader'] = self.checks_run_reader
        panel = ReviewChecksPanel(
            self.session,
            self.project,
            self.invocation_id,
            **kwargs,
        )
        def on_return(_=None):
            if self.view_active():
                self.action_reload()
        self.app.push_screen(panel, on_return)

    def on_button_pressed(self, event):
        event.stop()
        if self.mutating:
            return
        if event.button.id == 'turn-changes-close':
            self.action_close()
        elif event.button.id == 'turn-changes-refresh':
            self.action_reload()
        elif event.button.id == 'turn-changes-copy' and self.diff_text is not None:
            self.app.copy_to_clipboard(self.diff_text)
            event.button.label = 'Copied'
        elif event.button.id == 'turn-changes-open':
            self.open_file()
        elif event.button.id == 'turn-changes-undo':
            self.request_undo()
        elif event.button.id == 'turn-changes-checks':
            self.open_checks()

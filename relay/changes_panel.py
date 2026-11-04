"""Review confirmed edits and explicitly restore them within their saved scope."""
import asyncio
from pathlib import Path
from threading import Lock

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from .console_output import plain
from .console_panels import SupportPanel
from .native_review import review


class FileViewer(SupportPanel):
    BINDINGS = [
        Binding('escape', 'close', 'Back', priority=True),
        Binding('f3', 'close', 'Back', priority=True),
    ]
    DEFAULT_CSS = '''
    #file-viewer-notice { height: auto; margin-bottom: 1; color: $text-muted; }
    #file-viewer-scroll { height: 1fr; scrollbar-size: 1 1; }
    #file-viewer-content { height: auto; }
    #file-viewer-actions Button { min-width: 12; margin-left: 1; }
    '''

    def __init__(self, *, path, content, sha256=None):
        super().__init__()
        self.file_path = str(path)
        self.content = str(content)
        self.sha256 = sha256

    def compose(self):
        with Vertical(id='file-viewer-body', classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('File · ' + plain(self.file_path.replace('\\', '/').rsplit('/', 1)[-1]), id='file-viewer-heading', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='file-viewer-close')
            yield Static('Current file content on disk · captured when opened\n'+plain(self.file_path), id='file-viewer-notice', markup=False)
            with VerticalScroll(id='file-viewer-scroll'):
                yield Static(plain(self.content), id='file-viewer-content', markup=False)
            with Horizontal(id='file-viewer-actions', classes='panel-actions'):
                yield Button('Back', id='file-viewer-back')
                yield Button('Copy content', id='file-viewer-copy', classes='panel-primary')

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id in ('file-viewer-close', 'file-viewer-back'):
            self.action_close()
        elif event.button.id == 'file-viewer-copy':
            self.app.copy_to_clipboard(self.content)
            event.button.label = 'Copied'


class UndoConfirmDialog(ModalScreen):
    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel', priority=True),
    ]
    DEFAULT_CSS = '''
    UndoConfirmDialog {
        align: center middle;
        background: $background 80%;
    }
    #undo-confirm-dialog {
        width: 60;
        max-width: 90%;
        max-height: 95%;
        height: auto;
        background: $panel;
        border: round $warning 40%;
        padding: 1 2;
    }
    #undo-confirm-dialog:focus-within {
        border: round $warning;
    }
    #undo-confirm-title {
        height: 1;
        text-style: bold;
        color: $foreground;
        margin-bottom: 1;
    }
    #undo-confirm-target {
        height: auto;
        max-height: 5;
        overflow-y: auto;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    #undo-confirm-warning {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #undo-confirm-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #undo-confirm-actions Button {
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
    #undo-confirm-actions Button:hover,
    #undo-confirm-actions Button:focus,
    #undo-confirm-actions Button.-active {
        background: $primary 20%;
        color: $foreground;
    }
    #undo-confirm-actions Button.panel-danger {
        color: $warning;
        text-style: bold;
    }
    '''

    def __init__(self, *, repository, path, effect_id):
        super().__init__()
        self.repository = str(repository)
        self.path = str(path)
        self.effect_id = str(effect_id)

    def compose(self):
        with Vertical(id='undo-confirm-dialog'):
            yield Static('Confirm undo edit', id='undo-confirm-title', markup=False)
            target = f"{self.repository}/{self.path}" if self.repository else self.path
            yield Static('Target: ' + plain(target), id='undo-confirm-target', markup=False)
            warning = (
                '• Only this confirmed file-tool edit will be restored.\n'
                '• Newer changes cause refusal.\n'
                '• Shell effects are not undone.\n'
                '• Tests must run again.'
            )
            yield Static(warning, id='undo-confirm-warning', markup=False)
            with Horizontal(id='undo-confirm-actions', classes='panel-actions'):
                yield Button('Cancel · Esc', id='undo-cancel')
                yield Button('Confirm undo', id='undo-confirm', classes='panel-danger')

    def action_cancel(self):
        self.dismiss(False)

    def action_confirm(self):
        self.dismiss(True)

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'undo-confirm':
            self.action_confirm()
        elif event.button.id == 'undo-cancel':
            self.action_cancel()


class ChangesPanel(SupportPanel):
    BINDINGS = [
        Binding('escape', 'close', 'Close', priority=True),
        Binding('f3', 'close', 'Close', priority=True),
        Binding('ctrl+r', 'reload', 'Refresh', priority=True),
    ]
    DEFAULT_CSS = '''
    #changes-selectors { height: 1; margin-bottom: 1; }
    #changes-turn { width: 24; margin-right: 1; }
    #changes-file { width: 1fr; }
    #changes-summary { height: auto; margin-bottom: 1; color: $text-muted; }
    #changes-scroll { height: 1fr; scrollbar-size: 1 1; }
    #changes-diff { height: auto; }
    #changes-scope { height: auto; margin-top: 1; color: $text-muted; }
    #changes-actions { height: 1; margin-top: 1; grid-size: 6; grid-rows: 1; }
    #changes-actions Button { min-width: 0; width: 1fr; margin: 0; }
    ChangesPanel.narrow #changes-actions {
        height: 2;
        layout: grid;
        grid-size: 3 2;
        grid-rows: 1 1;
        grid-columns: 1fr 1fr 1fr;
    }
    ChangesPanel.narrow #changes-actions Button {
        min-width: 10;
        width: 100%;
        margin: 0;
        padding: 0 1;
    }
    '''

    def __init__(self, session, project, *, reader=review, file_reader=None, undoer=None, turn_reader=None, turn_undoer=None, checks_reader=None, checks_runner=None, checks_run_reader=None, commenter=None, provider_sessions=()):
        super().__init__()
        self.commenter = commenter
        self.session, self.project, self.reader = Path(session), Path(project), reader
        self.file_reader = file_reader
        self.undoer = undoer
        self.turn_reader = turn_reader
        self.turn_undoer = turn_undoer
        self.checks_reader = checks_reader
        self.checks_runner = checks_runner
        self.checks_run_reader = checks_run_reader
        self.turn = None
        self.rows = {}
        self.revision = 0
        self.read_lock = Lock()
        self.diff_text = None
        self.diff_effect_id = None
        self.mutating = False
        self.active_confirmation = None
        self.restoration_result = None
        self.provider_sessions = list(provider_sessions)

    def read(self, **kwargs):
        # Cancelling a UI worker cannot stop its thread. Serialize our own
        # read-only requests so an older lookup cannot hold custody against a
        # newer selection. An actual external writer is still refused.
        with self.read_lock:
            return self.reader(self.session, project=self.project, **kwargs)

    def read_file(self, effect_id, invocation_id=None):
        with self.read_lock:
            func = self.file_reader
            if func is None:
                from .native_review_actions import read_edit_file
                func = read_edit_file
            return func(self.session, project=self.project, effect_id=effect_id, invocation_id=invocation_id)

    def run_undo(self, effect_id, invocation_id=None):
        with self.read_lock:
            func = self.undoer
            if func is None:
                from .native_review_actions import undo_edit
                func = undo_edit
            return func(self.session, project=self.project, effect_id=effect_id, invocation_id=invocation_id)

    def compose(self):
        with Vertical(id='changes-body', classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Changes · ' + plain(self.project.name), id='changes-heading', classes='panel-title', markup=False)
                yield Button('Comment', id='changes-comment', disabled=True)
                yield Button('Back · Esc', id='changes-close')
            if len(self.provider_sessions) > 1:
                yield Select(self.provider_sessions, value=str(self.session), allow_blank=False, id='changes-provider')
            with Horizontal(id='changes-selectors'):
                yield Select([('All turns', '')], value='', allow_blank=False, id='changes-turn')
                yield Select([], prompt='Choose a file edit', id='changes-file')
            yield Static('', id='changes-edit-status', markup=False)
            with VerticalScroll(id='changes-scroll'):
                yield Static('Loading recorded edits…', id='changes-summary', markup=False)
                yield Static('', id='changes-diff', markup=False)
                yield Static('Recorded file changes; shell changes are not captured.\nFreshness covers observed files only. Recorded checks are not a new test run.',
                             id='changes-scope', markup=False)
            with Grid(id='changes-actions', classes='panel-actions'):
                yield Button('Open file', id='changes-open', disabled=True)
                yield Button('Undo edit', id='changes-undo', classes='panel-danger', disabled=True)
                yield Button('Review turn', id='changes-review-turn', disabled=True)
                yield Button('Checks', id='changes-checks')
                yield Button('Refresh', id='changes-refresh')
                yield Button('Copy diff', id='changes-copy', classes='panel-primary', disabled=True)

    def on_mount(self):
        if self.app.size.width < 100 or self.app.size.height < 30:
            self.add_class('narrow')
        self.action_reload()

    def on_unmount(self):
        self.dismiss_active_confirmation()

    def view_active(self):
        # Textual may still report is_mounted while removing child controls.
        return self.is_mounted and bool(self.query_one_optional('#changes-summary')) and bool(self.query_one_optional('#changes-file'))

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
        if self.mutating or not self.rows:
            return False
        try:
            val = self.query_one('#changes-file', Select).value
        except Exception:
            return False
        return bool(val and val in self.rows)

    def _can_undo(self) -> bool:
        if self.mutating or not self.rows:
            return False
        try:
            val = self.query_one('#changes-file', Select).value
        except Exception:
            return False
        if not val or val not in self.rows:
            return False
        edit = self.rows[val]
        if edit.get('status') != 'succeeded':
            return False
        if self.diff_text is None or self.diff_effect_id != val:
            return False
        if edit.get('undo_status') is not None:
            return False
        return True

    def _can_review_turn(self) -> bool:
        if self.mutating:
            return False
        return bool(self.turn)

    def _update_action_buttons(self):
        if not self.is_mounted:
            return
        selected = self.rows.get(self.query_one('#changes-file', Select).value, {})
        self.query_one('#changes-edit-status', Static).update(
            'Edit status: ' + plain(selected.get('undo_status') or selected.get('status', '')) if selected else '')
        if self.query_one_optional('#changes-provider'):
            self.query_one('#changes-provider', Select).disabled = self.mutating
        self.query_one('#changes-comment', Button).disabled = (
            self.mutating or self.diff_text is None or self.commenter is None)
        try:
            copy_btn = self.query_one('#changes-copy', Button)
            open_btn = self.query_one('#changes-open', Button)
            undo_btn = self.query_one('#changes-undo', Button)
            review_turn_btn = self.query_one('#changes-review-turn', Button)
            checks_btn = self.query_one('#changes-checks', Button)
            refresh_btn = self.query_one('#changes-refresh', Button)
            close_btn = self.query_one('#changes-close', Button)
            turn_sel = self.query_one('#changes-turn', Select)
            file_sel = self.query_one('#changes-file', Select)
        except Exception:
            return

        if self.mutating:
            copy_btn.disabled = True
            open_btn.disabled = True
            undo_btn.disabled = True
            review_turn_btn.disabled = True
            checks_btn.disabled = True
            refresh_btn.disabled = True
            close_btn.disabled = True
            turn_sel.disabled = True
            file_sel.disabled = True
        else:
            copy_btn.disabled = self.diff_text is None
            open_btn.disabled = not self._can_open()
            undo_btn.disabled = not self._can_undo()
            review_turn_btn.disabled = not self._can_review_turn()
            checks_btn.disabled = False
            refresh_btn.disabled = False
            close_btn.disabled = False
            turn_sel.disabled = False
            file_sel.disabled = False

    def open_turn_review(self):
        if not self._can_review_turn() or self.app.screen is not self:
            return
        from .turn_changes_panel import TurnChangesPanel
        kwargs = {'read_lock': self.read_lock}
        if self.turn_reader is not None:
            kwargs['reader'] = self.turn_reader
        if self.turn_undoer is not None:
            kwargs['undoer'] = self.turn_undoer
        if self.file_reader is not None:
            kwargs['file_reader'] = self.file_reader
        panel = TurnChangesPanel(
            self.session,
            self.project,
            self.turn,
            **kwargs,
        )
        def on_return(_=None):
            if self.view_active():
                self.action_reload()
        self.app.push_screen(panel, on_return)

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
            self.turn,
            **kwargs,
        )
        def on_return(_=None):
            if self.view_active():
                self.action_reload()
        self.app.push_screen(panel, on_return)

    @work(exclusive=True, group='changes-load')
    async def action_reload(self):
        if self.mutating:
            return
        self.dismiss_active_confirmation()
        turn = self.turn
        self.revision += 1
        revision = self.revision
        self.rows = {}
        self.diff_text = None
        self.diff_effect_id = None
        try:
            copy_btn = self.query_one('#changes-copy', Button)
            copy_btn.disabled = True
            copy_btn.label = 'Copy diff'
            self.query_one('#changes-diff', Static).update('')
        except Exception:
            pass
        self._update_action_buttons()
        try:
            result = await asyncio.to_thread(self.read,
                                             invocation_id=turn, include_diffs=False)
            if not self.view_active() or self.revision != revision or self.turn != turn:
                return
            with self.prevent(Select.Changed):
                turns = result.get('turns', [])
                selector = self.query_one('#changes-turn', Select)
                selector.set_options([('All turns', '')] + [(f'Turn {i+1} · {row["status"]}', row['id'])
                    for i, row in enumerate(turns)])
                selector.value = turn or ''
                rows = result.get('edits', [])[-200:]
                self.rows = {row['effect_id']: row for row in rows}
                files = self.query_one('#changes-file', Select)
                files.set_options([(f'{i+1} · {plain(row["repository"]+"/"+row["path"])} · {row["status"]}' + (f' · {row["undo_status"]}' if row.get('undo_status') else ''), row['effect_id'])
                                   for i, row in enumerate(rows)])
                if rows:
                    files.value = rows[0]['effect_id']
            lines = []
            if self.restoration_result:
                lines.append(f'Restoration result: {self.restoration_result}')
            if result.get('unresolved_effects'):
                lines.append('Some operations remain unresolved.')
            if result.get('changed_since_observation'):
                lines.append('Files changed after the recorded observation.')
            if result.get('unavailable_files'):
                lines.append('Some current files could not be checked.')
            count = len(result.get('edits', []))
            lines.append(f'{count} recorded edit' + ('s' if count != 1 else '') + (' · showing latest 200' if count > 200 else ''))
            for check in result.get('checks', [])[-5:]:
                status = ('PASS' if check.get('returncode') == 0 else 'FAIL') if check.get('status') in {'succeeded', 'failed'} else 'UNKNOWN'
                freshness = {'OBSERVED_FILES_MATCH': 'observed files match', 'STALE': 'STALE · later changes',
                             'UNKNOWN': 'UNKNOWN · incomplete observations'}.get(check.get('freshness'), check.get('freshness', ''))
                lines.append(f'{plain(check.get("name", ""))}: recorded {status} · {freshness}')
            self.query_one('#changes-summary', Static).update('\n'.join(lines))
            self._update_action_buttons()
            if rows:
                self.show_diff(rows[0]['effect_id'])
            else:
                self.query_one('#changes-diff', Static).update('No file-tool edits recorded in this selection.')
                self._update_action_buttons()
        except Exception as exc:
            if self.view_active() and self.revision == revision and self.turn == turn:
                self.query_one('#changes-summary', Static).update('Review unavailable: ' + plain(str(exc)))
                self._update_action_buttons()

    @work(exclusive=True, group='changes-diff')
    async def show_diff(self, effect_id):
        if self.mutating:
            return
        turn = self.turn
        revision = self.revision
        self.diff_text = None
        self.diff_effect_id = None
        try:
            copy_btn = self.query_one('#changes-copy', Button)
            copy_btn.disabled = True
            copy_btn.label = 'Copy diff'
            self.query_one('#changes-diff', Static).update('Loading verified snapshot…')
        except Exception:
            pass
        self._update_action_buttons()
        try:
            result = await asyncio.to_thread(self.read,
                invocation_id=turn, diff_effect_id=effect_id, max_diff_bytes=256*1024)
            if not self.view_active() or self.revision != revision or self.turn != turn or self.query_one('#changes-file', Select).value != effect_id:
                return
            edit = next(row for row in result.get('edits', []) if row['effect_id'] == effect_id)
            self.rows[effect_id] = edit
            if edit.get('diff') is None:
                text = Text('Diff unavailable: ' + plain(edit.get('diff_unavailable', 'No confirmed matching snapshot.')))
            else:
                self.diff_text = plain(edit['diff'])
                self.diff_effect_id = effect_id
                text = Text()
                theme = self.app.current_theme
                for line in plain(edit['diff']).splitlines(keepends=True):
                    style = theme.success if line.startswith('+') else theme.error if line.startswith('-') else theme.primary if line.startswith('@@') else None
                    text.append(line, style=style)
            self.query_one('#changes-diff', Static).update(text)
            self._update_action_buttons()
            self.query_one('#changes-scroll').scroll_home(animate=False)
        except Exception as exc:
            if self.view_active() and self.revision == revision and self.turn == turn and self.query_one('#changes-file', Select).value == effect_id:
                self.query_one('#changes-diff', Static).update('Diff unavailable: ' + plain(str(exc)))
                self._update_action_buttons()

    @work(exclusive=True, group='changes-file-view')
    async def open_file(self):
        if self.mutating:
            return
        turn = self.turn
        revision = self.revision
        try:
            effect_id = self.query_one('#changes-file', Select).value
        except Exception:
            return
        if not effect_id or effect_id not in self.rows:
            return
        try:
            result = await asyncio.to_thread(self.read_file, effect_id, invocation_id=turn)
            if (not self.view_active() or self.mutating or self.app.screen is not self or
                self.revision != revision or
                self.turn != turn or
                self.query_one('#changes-file', Select).value != effect_id):
                return
            if not isinstance(result, dict) or 'content' not in result:
                raise ValueError('Invalid file content received.')
            viewer = FileViewer(
                path=result.get('path', ''),
                content=result.get('content', ''),
                sha256=result.get('sha256'),
            )
            self.app.push_screen(viewer)
        except Exception as exc:
            if (self.view_active() and
                self.revision == revision and
                self.turn == turn and
                self.query_one('#changes-file', Select).value == effect_id):
                self.query_one('#changes-summary', Static).update('File unavailable: ' + plain(str(exc)))

    def request_undo(self):
        if not self._can_undo() or self.active_confirmation is not None:
            return
        try:
            effect_id = self.query_one('#changes-file', Select).value
        except Exception:
            return
        if not effect_id or effect_id not in self.rows:
            return
        edit = self.rows[effect_id]
        if edit.get('status') != 'succeeded' or self.diff_text is None or edit.get('undo_status') is not None:
            return

        req_effect_id = effect_id
        req_turn = self.turn
        req_revision = self.revision

        dialog = UndoConfirmDialog(
            repository=edit.get('repository', ''),
            path=edit.get('path', ''),
            effect_id=req_effect_id,
        )
        self.active_confirmation = dialog

        def on_dismiss(confirmed):
            self.active_confirmation = None
            if not confirmed:
                return
            if (not self.view_active() or
                self.revision != req_revision or
                self.turn != req_turn or
                self.query_one('#changes-file', Select).value != req_effect_id):
                return
            self.perform_undo(req_effect_id, req_turn, req_revision)

        self.app.push_screen(dialog, on_dismiss)

    @work(exclusive=False)
    async def perform_undo(self, effect_id, turn, revision):
        if (not self.view_active() or self.mutating or self.revision != revision or self.turn != turn
                or self.query_one('#changes-file', Select).value != effect_id or not self._can_undo()):
            return
        self.mutating = True
        self._update_action_buttons()
        self.query_one('#changes-summary', Static).update('Restoring edit…')
        try:
            result = await asyncio.to_thread(self.run_undo, effect_id, invocation_id=turn)
            if not self.view_active():
                return
            if not isinstance(result, dict) or result.get('status') != 'UNDONE':
                status = result.get('status') if isinstance(result, dict) else str(result)
                raise ValueError(f'Undo unconfirmed (status: {status})')
            path = result.get('path', '')
            eid = result.get('effect_id', effect_id)
            if eid != effect_id:
                raise ValueError('Undo returned a different edit identity; refresh to inspect its result.')
            label = self.rows[effect_id]['path']
            self.restoration_result = f"Restored {plain(label)}. Run the relevant checks again."
            self.rows[effect_id]['undo_status'] = 'UNDONE'
            self.mutating = False
            self._update_action_buttons()
            self.query_one('#changes-summary', Static).update('Restoration result: '+self.restoration_result)
            self.action_reload()
        except Exception as exc:
            self.mutating = False
            self.restoration_result = None
            if effect_id in self.rows:
                self.rows[effect_id]['undo_status'] = 'UNKNOWN'
            if self.view_active():
                self.query_one('#changes-summary', Static).update('Undo failed: ' + plain(str(exc)))
                self._update_action_buttons()

    def on_select_changed(self, event):
        if event.select.id == 'changes-provider' and not self.mutating and isinstance(event.value, str):
            if event.value in [path for _, path in self.provider_sessions] and Path(event.value) != self.session:
                self.session = Path(event.value)
                self.turn = None
                self.action_reload()
            event.stop()
            return
        if self.mutating:
            event.stop()
            return
        self.dismiss_active_confirmation()
        if event.select.id == 'changes-turn' and isinstance(event.value, str):
            turn = event.value or None
            if turn != self.turn:
                self.turn = turn
                self._update_action_buttons()
                self.action_reload()
        elif event.select.id == 'changes-file' and event.value in self.rows:
            self.revision += 1
            self.diff_effect_id = None
            self._update_action_buttons()
            self.show_diff(event.value)

    def on_button_pressed(self, event):
        event.stop()
        if self.mutating:
            return
        if event.button.id == 'changes-close':
            self.action_close()
        elif event.button.id == 'changes-comment' and self.diff_text is not None and self.commenter:
            from .console_workbench import artifact
            self.commenter(artifact('Recorded file diff', self.diff_text, session=str(self.session),
                                    effect_id=self.diff_effect_id, turn=self.turn))
        elif event.button.id == 'changes-refresh':
            self.action_reload()
        elif event.button.id == 'changes-copy' and self.diff_text is not None:
            self.app.copy_to_clipboard(self.diff_text)
            event.button.label = 'Copied'
        elif event.button.id in ('changes-open', 'changes-open-file'):
            self.open_file()
        elif event.button.id == 'changes-undo':
            self.request_undo()
        elif event.button.id == 'changes-review-turn':
            self.open_turn_review()
        elif event.button.id == 'changes-checks':
            self.open_checks()

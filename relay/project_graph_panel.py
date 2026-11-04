"""Project backlog graph binding modal support panel."""
import asyncio
from pathlib import Path
import threading
from typing import Callable, Optional

from rich.text import Text

from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from .console_output import plain
from .console_panels import SupportPanel

NOTICE_TEXT = (
    "Binding applies to new conversations; saved conversations retain their graph. "
    "Planning and individual Work execution use this graph."
)


class ProjectGraphPanel(SupportPanel):
    """Modal support panel for viewing and configuring default project graph binding."""

    AUTO_FOCUS = '#project-graph-back'
    BINDINGS = [
        Binding('escape', 'close', 'Back', priority=True),
    ]

    DEFAULT_CSS = '''
    #project-graph-dialog {
        width: 96%;
        max-width: 120;
        height: 94%;
    }
    #project-graph-header {
        height: 1;
        margin: 0 0 1 0;
    }
    #project-graph-title {
        width: 1fr;
        height: 1;
        text-style: bold;
        color: $foreground;
    }
    #project-graph-notice {
        height: auto;
        color: $text-muted;
        margin: 0 0 1 0;
    }
    #project-graph-path-block {
        height: auto;
        margin: 0 0 1 0;
    }
    #project-graph-path-label {
        height: 1;
        color: $foreground;
        margin: 0;
    }
    #project-graph-path {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
        background: $panel;
        color: $foreground;
    }
    #project-graph-path:focus {
        border: none;
        background: $primary 15%;
    }
    #project-graph-path.-disabled {
        color: $text-muted;
        opacity: 60%;
    }
    #project-graph-effective-row {
        height: 1;
        margin: 0 0 1 0;
    }
    #project-graph-effective-label {
        width: auto;
        height: 1;
        color: $primary;
        text-style: bold;
        margin-right: 1;
    }
    #project-graph-effective {
        width: 1fr;
        height: 1;
        color: $foreground;
    }
    #project-graph-summary-label {
        height: 1;
        color: $primary;
        text-style: bold;
        margin: 0;
    }
    #project-graph-summary-scroll {
        height: 1fr;
        min-height: 4;
        border: solid $primary 25%;
        padding: 0 1;
        background: $panel 30%;
        margin: 0 0 1 0;
        scrollbar-size: 1 1;
    }
    #project-graph-summary-scroll:focus {
        border: solid $primary 60%;
    }
    #project-graph-summary {
        color: $foreground;
        height: auto;
    }
    #project-graph-status {
        height: 1;
        color: $text-muted;
    }
    #project-graph-status.status-error {
        color: $warning;
    }
    #project-graph-status.status-success {
        color: $primary;
    }
    #project-graph-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #project-graph-actions Button {
        margin-left: 1;
        min-width: 12;
        padding: 0 1;
    }
    ProjectGraphPanel.narrow #project-graph-dialog {
        width: 100%;
        height: 100%;
        border: none;
    }
    ProjectGraphPanel.narrow #project-graph-actions Button {
        min-width: 8;
        padding: 0 1;
    }
    ProjectGraphPanel.narrow #project-graph-notice {
        margin-bottom: 0;
    }
    ProjectGraphPanel.narrow #project-graph-path-block {
        margin-bottom: 0;
    }
    ProjectGraphPanel.narrow #project-graph-effective-row {
        margin-bottom: 0;
    }
    '''

    def __init__(
        self,
        project=None,
        *,
        reader: Optional[Callable[[], dict]] = None,
        saver: Optional[Callable[[str, str], dict]] = None,
        inspector: Optional[Callable[[str], str]] = None,
    ):
        super().__init__()
        self.project_path = Path(project) if project is not None else Path.cwd()
        self.project_name = self.project_path.name or str(self.project_path)
        self.project_full_path = str(self.project_path.resolve() if self.project_path.exists() else self.project_path)
        self.reader = reader
        self.saver = saver
        self.inspector = inspector

        self.revision: Optional[str] = None
        self.saved_view: Optional[dict] = None
        self._is_reading: bool = False
        self._is_previewing: bool = False
        self._is_saving: bool = False
        self._op_seq: int = 0
        self._active: bool = False
        self._effective_path = ''
        self._thread_lock = threading.Lock()

    def compose(self):
        with Vertical(id='project-graph-dialog', classes='support-body'):
            with Horizontal(id='project-graph-header', classes='panel-header'):
                yield Static(f'Project backlog — {self.project_name}', id='project-graph-title', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='project-graph-close')
            yield Static(NOTICE_TEXT, id='project-graph-notice', markup=False)
            with Vertical(id='project-graph-path-block'):
                yield Static('Default project graph (absolute JSON path, blank for no graph):', id='project-graph-path-label', markup=False)
                yield Input(value='', placeholder='Absolute path to work-graph.json (blank for no graph)', id='project-graph-path')
            with Horizontal(id='project-graph-effective-row'):
                yield Static('Conversation graph:', id='project-graph-effective-label', markup=False)
                yield Static('None', id='project-graph-effective', markup=False)
            yield Static('Graph summary', id='project-graph-summary-label', markup=False)
            with VerticalScroll(id='project-graph-summary-scroll', can_focus=True):
                yield Static('', id='project-graph-summary', markup=False)
            yield Static('Loading project graph…', id='project-graph-status', markup=False)
            with Horizontal(id='project-graph-actions', classes='panel-actions'):
                yield Button('Back', id='project-graph-back')
                yield Button('Refresh', id='project-graph-refresh', disabled=True)
                yield Button('Preview', id='project-graph-preview', disabled=True)
                yield Button('Save binding', id='project-graph-save', classes='panel-primary', disabled=True)

    def on_mount(self):
        self._active = True
        self.set_class(self.app.size.width < 100 or self.app.size.height < 30, 'narrow')
        title_widget = self.query_one('#project-graph-title', Static)
        title_widget.tooltip = self.project_full_path
        self.load_graph()

    def on_unmount(self):
        self._active = False
        self._op_seq += 1

    def on_resize(self, event):
        self.set_class(event.size.width < 100 or event.size.height < 30, 'narrow')
        self.call_after_refresh(self._render_effective)

    def _render_effective(self):
        if not self._active or not self.query_one_optional('#project-graph-effective'):
            return
        widget = self.query_one('#project-graph-effective', Static)
        text = Text(plain(self._effective_path or 'None'), no_wrap=True)
        if widget.content_size.width > 0:
            text.truncate(widget.content_size.width, overflow='ellipsis')
        widget.update(text)

    @property
    def _is_busy(self) -> bool:
        return self._is_reading or self._is_previewing or self._is_saving

    def action_close(self):
        if self._is_saving:
            return
        self._active = False
        self._op_seq += 1
        self.dismiss(self.saved_view)

    def action_refresh(self):
        if self._is_busy:
            return
        self.load_graph()

    def action_preview(self):
        if self._is_busy:
            return
        path = self.query_one('#project-graph-path', Input).value.strip()
        self.preview_graph(path)

    def action_save(self):
        if self._is_busy or self.revision is None:
            return
        path = self.query_one('#project-graph-path', Input).value.strip()
        self.save_graph(path, self.revision)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        btn_id = event.button.id
        if btn_id in ('project-graph-close', 'project-graph-back'):
            self.action_close()
        elif btn_id == 'project-graph-refresh':
            self.action_refresh()
        elif btn_id == 'project-graph-preview':
            self.action_preview()
        elif btn_id == 'project-graph-save':
            self.action_save()

    def load_graph(self):
        if self._is_busy:
            return
        self.revision = None
        self._set_ui_operating(reading=True)
        self._op_seq += 1
        op_seq = self._op_seq
        self._run_read(op_seq)

    def preview_graph(self, path: str):
        if self._is_busy:
            return
        self._set_ui_operating(previewing=True)
        self._op_seq += 1
        op_seq = self._op_seq
        self._run_preview(op_seq, path)

    def save_graph(self, path: str, expected_rev: str):
        if self._is_busy or self.revision is None:
            return
        self._set_ui_operating(saving=True)
        self._op_seq += 1
        op_seq = self._op_seq
        self._run_save(op_seq, path, expected_rev)

    def _set_ui_operating(self, reading: bool = False, previewing: bool = False, saving: bool = False):
        self._is_reading = reading
        self._is_previewing = previewing
        self._is_saving = saving

        self.query_one('#project-graph-path', Input).disabled = True
        self.query_one('#project-graph-refresh', Button).disabled = True
        self.query_one('#project-graph-preview', Button).disabled = True
        self.query_one('#project-graph-save', Button).disabled = True

        can_back = (reading or previewing) and not saving
        self.query_one('#project-graph-back', Button).disabled = not can_back
        self.query_one('#project-graph-close', Button).disabled = not can_back

        status = self.query_one('#project-graph-status', Static)
        status.remove_class('status-error')
        status.remove_class('status-success')
        if reading:
            status.update('Loading project graph…')
        elif previewing:
            status.update('Previewing project graph…')
        elif saving:
            status.update('Saving binding…')

    def _set_ui_idle(self):
        self._is_reading = False
        self._is_previewing = False
        self._is_saving = False

        self.query_one('#project-graph-back', Button).disabled = False
        self.query_one('#project-graph-close', Button).disabled = False
        self.query_one('#project-graph-refresh', Button).disabled = False

        has_rev = (self.revision is not None)
        self.query_one('#project-graph-path', Input).disabled = not has_rev
        self.query_one('#project-graph-preview', Button).disabled = not has_rev
        self.query_one('#project-graph-save', Button).disabled = not has_rev

    def _apply_loaded_view(self, view: dict):
        path = view.get('path') or ''
        effective_path = view.get('effective_path') or ''
        summary = view.get('summary') or ''

        path_input = self.query_one('#project-graph-path', Input)
        with self.prevent(Input.Changed):
            path_input.value = str(path)

        eff_str = str(effective_path).strip() if effective_path else ''
        self._effective_path = eff_str
        eff_widget = self.query_one('#project-graph-effective', Static)
        eff_widget.tooltip = Text(plain(eff_str)) if eff_str else None
        self._render_effective()
        self.call_after_refresh(self._render_effective)

        summary_label = self.query_one('#project-graph-summary-label', Static)
        summary_label.update('Graph summary')

        summary_widget = self.query_one('#project-graph-summary', Static)
        summary_widget.update(plain(summary or 'No project graph configured.'))

    @work(exclusive=True, group='project-graph-worker')
    async def _run_read(self, op_seq: int):
        try:
            if self.reader is None:
                raise ValueError("No project graph reader configured")

            def guarded_read():
                with self._thread_lock:
                    if not self._active or op_seq != self._op_seq:
                        return None
                    return self.reader()

            view = await asyncio.to_thread(guarded_read)
            if not self._active or op_seq != self._op_seq or view is None:
                return

            self.revision = str(view['revision'])
            self._apply_loaded_view(view)
            status = self.query_one('#project-graph-status', Static)
            status.update('Project binding loaded.')
            status.remove_class('status-error')
            status.remove_class('status-success')
            self._set_ui_idle()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self._active or op_seq != self._op_seq:
                return
            self.revision = None
            status = self.query_one('#project-graph-status', Static)
            status.update(f"Load failed: {plain(str(exc))}")
            status.remove_class('status-success')
            status.add_class('status-error')
            self._set_ui_idle()

    @work(exclusive=True, group='project-graph-worker')
    async def _run_preview(self, op_seq: int, path: str):
        try:
            if self.inspector is None:
                raise ValueError("No project graph inspector configured")

            def guarded_inspect():
                with self._thread_lock:
                    if not self._active or op_seq != self._op_seq:
                        return None
                    return self.inspector(path)

            preview_text = await asyncio.to_thread(guarded_inspect)
            if not self._active or op_seq != self._op_seq or preview_text is None:
                return

            summary_label = self.query_one('#project-graph-summary-label', Static)
            label_text = f"Preview: {path}" if path else "Preview: (no graph)"
            summary_label.update(plain(label_text))

            summary_widget = self.query_one('#project-graph-summary', Static)
            summary_widget.update(plain(preview_text))

            status = self.query_one('#project-graph-status', Static)
            status.update('Preview ready (not saved).')
            status.remove_class('status-error')
            status.remove_class('status-success')
            self._set_ui_idle()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self._active or op_seq != self._op_seq:
                return
            status = self.query_one('#project-graph-status', Static)
            status.update(f"Preview failed: {plain(str(exc))}")
            status.remove_class('status-success')
            status.add_class('status-error')
            self._set_ui_idle()

    @work(exclusive=True, group='project-graph-worker')
    async def _run_save(self, op_seq: int, path: str, expected_rev: str):
        try:
            if self.saver is None:
                raise ValueError("No project graph saver configured")

            def guarded_save():
                with self._thread_lock:
                    if not self._active or op_seq != self._op_seq:
                        return None
                    return self.saver(path, expected_rev)

            new_view = await asyncio.to_thread(guarded_save)
            if not self._active or op_seq != self._op_seq or new_view is None:
                return

            self.revision = str(new_view['revision'])
            self.saved_view = new_view
            self._apply_loaded_view(new_view)

            status = self.query_one('#project-graph-status', Static)
            status.update('Project graph binding saved.')
            status.remove_class('status-error')
            status.add_class('status-success')
            self._set_ui_idle()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self._active or op_seq != self._op_seq:
                return
            status = self.query_one('#project-graph-status', Static)
            status.update(f"Save failed: {plain(str(exc))}")
            status.remove_class('status-success')
            status.add_class('status-error')
            self._set_ui_idle()

"""Project picker modal panel for browsing and selecting workspace roots."""
import asyncio
import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, OptionList, Select, Static

from .console_output import plain

from .console_panels import SupportPanel

def directory_reader(path: Path | str) -> Tuple[List[Path], bool]:
    """Enumerate immediate directory entries up to 500 entries, returning max 200 sorted dirs."""
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    dirs: List[Path] = []
    scanned = 0
    limited = False

    with os.scandir(resolved) as it:
        for entry in it:
            scanned += 1
            if scanned > 500:
                limited = True
                break
            try:
                # Follow symlinks to check if target is a directory
                if entry.is_dir(follow_symlinks=True):
                    dirs.append(Path(entry.path))
            except OSError:
                continue

    dirs.sort(key=lambda d: (d.name.casefold(), d.name))
    if len(dirs) > 200:
        dirs = dirs[:200]
        limited = True

    return dirs, limited


def format_recent_options(recent_paths: Iterable) -> List[Tuple[str, str]]:
    """Format recent project paths with full-path disambiguation for duplicate basenames."""
    entries = []
    seen = set()
    for item in recent_paths:
        if not item:
            continue
        p = Path(item)
        full_str = str(p.resolve() if p.exists() else p)
        norm = os.path.normcase(full_str)
        if norm not in seen:
            seen.add(norm)
            entries.append((p, full_str))

    counts = {}
    for p, full_str in entries:
        base = p.name or full_str
        counts[base] = counts.get(base, 0) + 1

    options = []
    for p, full_str in entries:
        base = p.name or full_str
        if counts[base] > 1:
            label = f"{base} ({full_str})"
        else:
            label = base
        options.append((plain(label), full_str))
    return options


class ProjectPicker(SupportPanel):
    AUTO_FOCUS = '#project-picker-location'
    BINDINGS = [
        Binding('escape', 'close', 'Back', priority=True),
    ]

    DEFAULT_CSS = '''
    #project-picker-dialog { width: 96%; max-width: 120; height: 94%; }
    #project-picker-header { height: 1; margin: 0 0 1 0; }
    #project-picker-title { width: 1fr; height: 1; text-style: bold; color: $foreground; }
    #project-picker-notice { height: auto; color: $text-muted; margin: 0 0 1 0; }
    #project-picker-location-row { height: 1; margin: 0 0 1 0; }
    #project-picker-up { min-width: 8; width: 8; height: 1; min-height: 1; margin-right: 1;
                         border: none; background: transparent; color: $primary; }
    #project-picker-up:hover, #project-picker-up:focus { background: $primary 20%; color: $foreground; }
    #project-picker-location { width: 1fr; height: 1; border: none; padding: 0 1; background: $panel; }
    #project-picker-location:focus { background: $primary 15%; }
    #project-picker-recent-row { height: 1; margin: 0 0 1 0; }
    #project-picker-recent-label { width: auto; height: 1; color: $text-muted; margin-right: 1; padding: 0 1 0 0; }
    #project-picker-recent { width: 1fr; height: 1; margin: 0; }
    #project-picker-folders { height: 1fr; border: none; padding: 0 1; background: $panel; margin: 0 0 1 0; }
    #project-picker-status { height: auto; max-height: 2; color: $text-muted; }
    #project-picker-actions { height: 1; margin: 1 0 0 0; align-horizontal: right; }
    #project-picker-actions Button { margin-left: 1; }
    ProjectPicker.narrow #project-picker-dialog { width: 100%; height: 100%; border: none; }
    ProjectPicker.narrow #project-picker-status { max-height: 1; }
    '''

    def __init__(self, current, *, recent=(), reader=None):
        super().__init__()
        self.initial_current = current
        self.current_path = Path(current) if current else Path.cwd()
        self.directory = self.current_path.resolve() if self.current_path.exists() else self.current_path
        self.input_value = str(self.directory)
        self.recent = tuple(recent) if recent else ()
        self.recent_options = format_recent_options(self.recent)
        self.reader = reader
        self.rows: List[Path] = []
        self.validated_directory: Optional[Path] = None
        self.loading: bool = False
        self.error: Optional[str] = None
        self.generation: int = 0
        self.dismiss_result = None

    def compose(self):
        with Vertical(id='project-picker-dialog', classes='support-body'):
            with Horizontal(id='project-picker-header', classes='panel-header'):
                yield Static('Open project', id='project-picker-title', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='project-picker-close')
            yield Static(
                'Current conversation and drafts stay in their original project. '
                'Enter loads path or browses folder · Only Open project selects.',
                id='project-picker-notice',
                markup=False,
            )
            with Horizontal(id='project-picker-location-row'):
                yield Button('Up', id='project-picker-up')
                yield Input(plain(str(self.directory)), id='project-picker-location', placeholder='Directory path')
            with Horizontal(id='project-picker-recent-row'):
                yield Static('Recent:', id='project-picker-recent-label', markup=False)
                if self.recent_options:
                    yield Select(
                        [(Text(label), path) for label, path in self.recent_options],
                        prompt='Choose a recent project…',
                        allow_blank=True,
                        id='project-picker-recent',
                    )
                else:
                    yield Select(
                        [],
                        prompt='No recent projects',
                        allow_blank=True,
                        disabled=True,
                        id='project-picker-recent',
                    )
            yield OptionList(id='project-picker-folders', markup=False)
            yield Static('Loading directory…', id='project-picker-status', markup=False)
            with Horizontal(id='project-picker-actions', classes='panel-actions'):
                yield Button('Back', id='project-picker-back')
                yield Button('Open project', id='project-picker-open', classes='panel-primary', disabled=True)

    def on_mount(self):
        self.set_class(self.app.size.width < 100 or self.app.size.height < 30, 'narrow')
        self.browse(self.directory)
        self.call_after_refresh(self.query_one('#project-picker-location', Input).focus)

    def on_unmount(self):
        self.generation += 1

    def action_close(self):
        self.generation += 1
        self.dismiss_result = None
        self.dismiss(None)

    def action_open(self):
        if self.loading or self.error or self.validated_directory is None:
            return
        field = self.query_one('#project-picker-location', Input)
        field_text = field.value.strip()
        try:
            current_input_path = Path(field_text).resolve()
        except Exception:
            return
        if current_input_path != self.validated_directory:
            return
        self.generation += 1
        resolved_str = str(self.validated_directory.resolve())
        self.dismiss_result = resolved_str
        self.dismiss(resolved_str)

    def action_up(self):
        curr = self.directory or self.current_path
        parent = curr.parent
        self.browse(parent)

    def browse(self, path):
        self.generation += 1
        generation = self.generation
        self.loading = True
        self.error = None
        self.validated_directory = None

        target_path = Path(path).expanduser()
        path_str = str(target_path)
        self.input_value = path_str

        # Update input field value
        field = self.query_one('#project-picker-location', Input)
        with self.prevent(Input.Changed):
            field.value = path_str
        self.rows = []
        self.query_one('#project-picker-folders', OptionList).clear_options()

        # Disable Open button while loading
        self.query_one('#project-picker-open', Button).disabled = True

        # Update status
        self.query_one('#project-picker-status', Static).update('Loading directory…')

        # Launch worker
        self.load_directory(target_path, generation, path_str)

    @work(group='project-picker-browser')
    async def load_directory(self, target_path: Path, generation: int, expected_path: str):
        reader = self.reader or directory_reader
        try:
            def read():
                resolved = target_path.resolve()
                dirs, limited = reader(resolved)
                return resolved, dirs, limited
            resolved_path, dirs, limited = await asyncio.to_thread(read)

            # Generation guard
            if (not self.is_mounted or generation != self.generation
                    or self.query_one('#project-picker-location', Input).value != expected_path):
                return

            self.loading = False
            self.error = None
            self.directory = resolved_path
            self.validated_directory = resolved_path
            self.rows = dirs

            # Populate folder OptionList
            folder_menu = self.query_one('#project-picker-folders', OptionList)
            folder_menu.clear_options()
            if dirs:
                folder_menu.add_options([plain(d.name or str(d)) for d in dirs])
                folder_menu.highlighted = 0

            # Update status
            status_widget = self.query_one('#project-picker-status', Static)
            if not dirs:
                status_widget.update('No folders here · Open project selects this directory')
            elif limited:
                status_widget.update(f'List limited (showing {len(dirs)} directories); enter a more specific path.')
            else:
                status_widget.update(f'{len(dirs)} folder{"s" if len(dirs) != 1 else ""} · Enter browses, Open project selects')

            # Update Input value if it still matches expected
            field = self.query_one('#project-picker-location', Input)
            if field.value == expected_path:
                self.input_value = str(resolved_path)
                with self.prevent(Input.Changed):
                    field.value = str(resolved_path)

            # Enable Open if input matches the validated directory
            current_input = field.value.strip()
            try:
                matches = Path(current_input).resolve() == resolved_path
            except Exception:
                matches = False

            self.query_one('#project-picker-open', Button).disabled = not matches

        except Exception as exc:
            # Generation guard
            if (not self.is_mounted or generation != self.generation
                    or self.query_one('#project-picker-location', Input).value != expected_path):
                return

            self.loading = False
            self.error = plain(str(exc)) or exc.__class__.__name__
            self.validated_directory = None
            self.rows = []

            # Clear folder list: Empty/missing/permission errors stay visible and never substitute previous listing
            folder_menu = self.query_one('#project-picker-folders', OptionList)
            folder_menu.clear_options()

            self.query_one('#project-picker-open', Button).disabled = True
            self.query_one('#project-picker-status', Static).update(self.error)

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == 'project-picker-location' and event.value == event.input.value:
            event.stop()
            if event.value == self.input_value:
                return
            self.input_value = event.value
            self.generation += 1
            self.loading = False
            self.error = None
            self.validated_directory = None
            self.rows = []
            self.query_one('#project-picker-folders', OptionList).clear_options()
            self.query_one('#project-picker-open', Button).disabled = True
            self.query_one('#project-picker-status', Static).update('Press Enter to load typed path.')

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == 'project-picker-location':
            event.stop()
            val = event.value.strip()
            if not val:
                self.loading = False
                self.error = 'Directory path cannot be empty.'
                self.validated_directory = None
                self.rows = []
                self.query_one('#project-picker-folders', OptionList).clear_options()
                self.query_one('#project-picker-open', Button).disabled = True
                self.query_one('#project-picker-status', Static).update(self.error)
                return
            self.browse(Path(val))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option_list.id == 'project-picker-folders':
            event.stop()
            if 0 <= event.option_index < len(self.rows):
                selected_folder = self.rows[event.option_index]
                self.browse(selected_folder)

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == 'project-picker-recent':
            if isinstance(event.value, str) and event.value == event.select.value:
                event.stop()
                target = Path(str(event.value))
                self.browse(target)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        btn_id = event.button.id
        if btn_id in ('project-picker-close', 'project-picker-back'):
            self.action_close()
        elif btn_id == 'project-picker-open':
            self.action_open()
        elif btn_id == 'project-picker-up':
            self.action_up()

"""Project permissions modal support panel for configuring defaults and inspecting checks."""
import asyncio
from pathlib import Path
from typing import Callable, Optional

from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from .console_output import plain
from .console_panels import SupportPanel

COMMANDS_WARNING = (
    "Without an Action review, system commands run in a terminal directly on this machine as you, with network "
    "access and your files and credentials in reach: no sandbox and no review. Configured checks also run on this "
    "machine. A local model never gets this terminal. Choose an Action review to run commands in the sandbox."
)


MODE_OPTIONS = [
    ('Read only', 'read'),
    ('Edit files', 'edit'),
    ('Edit + system commands', 'commands'),
]

PLAN_OPTIONS = [('Show me the plan and ask', 'ask'), ('Go straight on', 'go')]
PLAN_HELP = (
    "Show me the plan and ask: before changing files, tare shows the whole plan and waits for Implement this "
    "plan, Change the plan or Deny, even where edits are already allowed. Go straight on: tare implements without "
    "showing the plan and asks only for file access this project does not allow yet. Use my default: this project "
    "follows My default, which applies to every project without a choice of its own. Both apply at once, also to "
    "open conversations."
)


def plan_options(default):
    """This project's choices; the first names what My default currently is."""
    return [('Use my default ('+PLAN_OPTIONS[0 if default else 1][0]+')', 'default'), *PLAN_OPTIONS]


class PermissionsPanel(SupportPanel):
    """Modal support panel for viewing and configuring project defaults."""

    AUTO_FOCUS = '#permissions-back'
    BINDINGS = [
        Binding('escape', 'close', 'Back', priority=True),
    ]

    DEFAULT_CSS = '''
    #permissions-dialog {
        width: 96%;
        max-width: 120;
        height: 94%;
    }
    #permissions-header {
        height: 1;
        margin: 0 0 1 0;
    }
    #permissions-title {
        width: 1fr;
        height: 1;
        text-style: bold;
        color: $foreground;
    }
    #permissions-notice {
        height: auto;
        color: $text-muted;
        margin: 0 0 1 0;
    }
    #permissions-mode-row {
        height: 1;
        margin: 0 0 1 0;
    }
    #permissions-mode-label {
        width: auto;
        height: 1;
        color: $foreground;
        margin-right: 1;
    }
    #permissions-mode {
        width: 1fr;
        height: 1;
    }
    .permissions-protection-row {
        height: 1;
        margin: 0 0 1 0;
    }
    .permissions-protection-row Static { width: 20; }
    .permissions-protection-row Select { width: 1fr; height: 1; }
    #permissions-review-limit { width: 1fr; height: 1; }
    .permissions-plan-row Static { width: 27; }
    #permissions-plan-help {
        height: auto;
        color: $text-muted;
        margin: 0 0 1 0;
    }
    #permissions-command-warning {
        color: $warning;
        height: auto;
        margin: 0 0 1 0;
    }
    #permissions-paths-label {
        height: auto;
        color: $foreground;
        margin: 0 0 1 0;
    }
    #permissions-paths {
        height: 4;
        min-height: 3;
        border: none;
        background: $panel;
        color: $foreground;
        margin: 0 0 1 0;
    }
    #permissions-paths:focus {
        border: none;
        background: $primary 10%;
    }
    #permissions-paths.-disabled {
        color: $text-muted;
        opacity: 60%;
    }
    #permissions-config-scroll {
        height: 1fr;
        min-height: 5;
        scrollbar-size: 1 1;
    }
    #permissions-details-scroll {
        height: 1fr;
        min-height: 4;
        border: solid $primary 25%;
        padding: 0 1;
        background: $panel 30%;
        margin: 0 0 1 0;
        scrollbar-size: 1 1;
    }
    #permissions-details-scroll:focus {
        border: solid $primary 60%;
    }
    .details-label {
        color: $primary;
        text-style: bold;
        margin-top: 1;
    }
    .details-text {
        color: $foreground;
        margin-bottom: 1;
    }
    #permissions-status {
        height: 1;
        color: $text-muted;
    }
    #permissions-status.status-error {
        color: $warning;
    }
    #permissions-status.status-success {
        color: $primary;
    }
    #permissions-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #permissions-actions Button {
        margin-left: 1;
    }
    PermissionsPanel.narrow #permissions-dialog {
        width: 100%;
        height: 100%;
        border: none;
    }
    PermissionsPanel.narrow #permissions-actions Button {
        min-width: 8;
        padding: 0 1;
    }
    PermissionsPanel.narrow #permissions-paths {
        height: 3;
    }
    PermissionsPanel.narrow #permissions-notice,
    PermissionsPanel.narrow #permissions-paths-label {
        margin-bottom: 0;
    }
    '''

    def __init__(self, project, *, reader: Optional[Callable] = None, saver: Optional[Callable] = None,
                 default_saver: Optional[Callable] = None):
        super().__init__()
        self.project_path = Path(project) if project is not None else Path.cwd()
        self.project_name = self.project_path.name or str(self.project_path)
        self.project_full_path = str(self.project_path.resolve() if self.project_path.exists() else self.project_path)
        self.reader = reader
        self.saver = saver
        self.default_saver = default_saver  # My default for every project, saved with the same Save
        self.show_plan_default = True

        self.revision: Optional[str] = None
        self.saved_view: Optional[dict] = None
        self._is_reading: bool = False
        self._is_saving: bool = False
        self._op_seq: int = 0
        self._active = False

    def compose(self):
        with Vertical(id='permissions-dialog', classes='support-body'):
            with Horizontal(id='permissions-header', classes='panel-header'):
                yield Static(f'Permissions — {self.project_name}', id='permissions-title', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='permissions-close')
            yield Static('Saved conversations and Work executions keep their original permissions.', id='permissions-notice', markup=False)
            with VerticalScroll(id='permissions-config-scroll', can_focus=True):
                with Horizontal(id='permissions-mode-row'):
                    yield Static('Defaults:', id='permissions-mode-label', markup=False)
                    yield Select(MODE_OPTIONS, value='read', allow_blank=False, id='permissions-mode')
                with Horizontal(classes='permissions-protection-row permissions-plan-row'):
                    yield Static('Before implementing:', markup=False)
                    yield Select(plan_options(True), value='default', allow_blank=False, id='permissions-plan')
                with Horizontal(classes='permissions-protection-row permissions-plan-row'):
                    yield Static('My default, every project:', markup=False)
                    yield Select(PLAN_OPTIONS, value='ask', allow_blank=False, id='permissions-plan-default')
                yield Static(PLAN_HELP, id='permissions-plan-help', markup=False)
                with Horizontal(classes='permissions-protection-row'):
                    yield Static('Command pre-check:', markup=False)
                    yield Select([('Jev online · review uncertain commands','jev_auto'), ('Jev online · also review every command','jev_always'),
                                  ('Selected model · check every command','model')], value='jev_auto',
                                 allow_blank=False, id='permissions-classifier')
                with Horizontal(classes='permissions-protection-row'):
                    yield Static('Action review:', markup=False)
                    yield Select([('Automatic: Codex or Tare + Jev', 'auto'), ('None: terminal on this machine, no sandbox', 'off'), ('Selected model', 'tare'), ('Codex built-in reviewer (CLI)', 'codex'),
                                  ('Selected model + Codex reviewer', 'both')], value='off', allow_blank=False, id='permissions-review')
                with Horizontal(classes='permissions-protection-row'):
                    yield Static('Execution sandbox:', markup=False)
                    yield Select([('Tare MXC', 'mxc'), ('Codex sandbox', 'codex')],
                                 value='mxc', allow_blank=False, id='permissions-isolation')
                with Horizontal(classes='permissions-protection-row'):
                    yield Static('Review call limit:', markup=False)
                    yield Input('24', placeholder='Calls per request, 1 to 24', type='integer', id='permissions-review-limit')
                yield Checkbox("Trust this repository's AGENTS.md (all its worktrees; uncommitted edits need review)", id='permissions-root')
                yield Checkbox('Allow nested instructions (requires root adoption)', id='permissions-nested', disabled=True)
                yield Static(COMMANDS_WARNING, id='permissions-command-warning', markup=False)
                yield Static(
                    "Edit paths  one relative path per line  . = whole project  no globs",
                    id='permissions-paths-label',
                    markup=False,
                )
                yield TextArea('', id='permissions-paths', show_line_numbers=False)
            with VerticalScroll(id='permissions-details-scroll', can_focus=True):
                yield Static('Effective permissions', id='permissions-effective-label', classes='details-label', markup=False)
                yield Static('No effective permissions configured.', id='permissions-effective-text', classes='details-text', markup=False)
                yield Static('Project instructions', id='permissions-instructions-label', classes='details-label', markup=False)
                yield Static('No project instructions recorded.', id='permissions-instructions-text', classes='details-text', markup=False)
            yield Static('Loading permissions…', id='permissions-status', markup=False)
            with Horizontal(id='permissions-actions', classes='panel-actions'):
                yield Button('Back', id='permissions-back')
                yield Button('Refresh', id='permissions-refresh', disabled=True)
                yield Button('Save for new conversations and Work', id='permissions-save', classes='panel-primary', disabled=True)

    def on_mount(self):
        self._active = True
        self.set_class(self.app.size.width < 100 or self.app.size.height < 30, 'narrow')
        title_widget = self.query_one('#permissions-title', Static)
        title_widget.tooltip = self.project_full_path
        warning_widget = self.query_one('#permissions-command-warning', Static)
        warning_widget.display = False
        paths_area = self.query_one('#permissions-paths', TextArea)
        paths_area.disabled = True
        self.load_permissions()

    def on_unmount(self):
        self._active = False
        self._op_seq += 1

    def action_close(self):
        if self._is_saving:
            return
        self._active = False
        self._op_seq += 1
        self.dismiss(self.saved_view)

    def action_refresh(self):
        if self._is_saving or self._is_reading:
            return
        self.load_permissions()

    def action_save(self):
        if self._is_saving or self._is_reading or self.revision is None:
            return
        mode_val = self.query_one('#permissions-mode', Select).value
        mode = str(mode_val) if mode_val != Select.BLANK else 'read'
        text = self.query_one('#permissions-paths', TextArea).text
        raw_paths = [line.strip() for line in text.splitlines() if line.strip()]
        write_paths = [] if mode == 'read' else raw_paths

        defaults = {
            'mode': mode,
            'write_paths': write_paths,
        }
        plan = self.query_one('#permissions-plan', Select).value
        if plan != 'default':  # Use my default: no choice of the project's own
            defaults['show_plan'] = plan == 'ask'
        review = self.query_one('#permissions-review', Select).value
        if mode == 'commands' and review != 'off':
            try:
                maximum = int(self.query_one('#permissions-review-limit', Input).value)
                from .command_protection import validate
                defaults['command_protection'] = validate({'review': str(review), 'max_reviews': maximum,
                    **({} if review == 'auto' else {
                        'backend': str(self.query_one('#permissions-isolation', Select).value),
                        'classifier': 'model' if self.query_one('#permissions-classifier',Select).value=='model' else 'jev',
                        'review_policy': 'on_demand' if self.query_one('#permissions-classifier',Select).value=='jev_auto' else 'always'})})
            except ValueError as exc:
                self.query_one('#permissions-status', Static).update(str(exc))
                return
        try:
            from .project_permissions import configure_admission
            defaults = configure_admission(defaults, self.project_full_path,
                getattr(self, 'reviewed_preview', None),
                load_instructions=self.query_one('#permissions-root', Checkbox).value,
                nested=self.query_one('#permissions-nested', Checkbox).value)
        except (ValueError, OSError) as exc:
            self.query_one('#permissions-status', Static).update(plain(str(exc)))
            return
        self.save_permissions(defaults, self.revision)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        btn_id = event.button.id
        if btn_id in ('permissions-close', 'permissions-back'):
            self.action_close()
        elif btn_id == 'permissions-refresh':
            self.action_refresh()
        elif btn_id == 'permissions-save':
            self.action_save()

    def on_checkbox_changed(self, event: Checkbox.Changed):
        if event.checkbox.id == 'permissions-root':
            nested = self.query_one('#permissions-nested', Checkbox)
            nested.disabled = not event.value
            if not event.value:
                nested.value = False

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == 'permissions-mode':
            if event.value == Select.BLANK:
                return
            mode = str(event.value)
            paths_area = self.query_one('#permissions-paths', TextArea)
            warning_widget = self.query_one('#permissions-command-warning', Static)
            if mode == 'read':
                paths_area.disabled = True
                warning_widget.display = False
            elif mode == 'commands':
                if not self._is_reading and not self._is_saving:
                    paths_area.disabled = False
                warning_widget.display = True
            else:  # 'edit'
                if not self._is_reading and not self._is_saving:
                    paths_area.disabled = False
                warning_widget.display = False
        if event.select.id in {'permissions-mode', 'permissions-review', 'permissions-isolation', 'permissions-classifier'}:
            self.protection_controls()
        if event.select.id == 'permissions-plan-default':
            self.relabel_plan_default()

    def relabel_plan_default(self):
        """Use my default names what My default is on screen, saved or not."""
        plan = self.query_one('#permissions-plan', Select)
        value = plan.value
        with self.prevent(Select.Changed):
            plan.set_options(plan_options(self.query_one('#permissions-plan-default', Select).value != 'go'))
            plan.value = 'ask'  # set_options leaves the first option selected without redrawing its new label
            plan.value = value

    def protection_controls(self):
        mode = self.query_one('#permissions-mode', Select).value
        enabled = mode == 'commands' and self.revision is not None and not self._is_reading and not self._is_saving
        review = self.query_one('#permissions-review', Select)
        review.disabled = not enabled
        protected = enabled and review.value != 'off'
        advanced = review.value not in ('auto', 'off')
        self.query_one('#permissions-classifier', Select).parent.display = advanced
        self.query_one('#permissions-isolation', Select).parent.display = advanced
        self.query_one('#permissions-classifier', Select).disabled = not protected or review.value == 'auto'
        self.query_one('#permissions-isolation', Select).disabled = not protected or review.value == 'auto'
        self.query_one('#permissions-review-limit', Input).disabled = not protected
        warning = self.query_one('#permissions-command-warning', Static)
        preset=self.query_one('#permissions-classifier',Select).value
        if protected and review.value == 'auto':
            from .turn_engine import jev_key
            key_status='Jev API key configured.' if jev_key() else 'Jev API key missing: add it in /setup > Helpers.'
            warning.update('Codex CLI connections use Codex auto-review and sandbox. Other connections use '
                'Jev with the Tare sandbox; Tare reviews uncertain actions. The execution connection chooses '
                'the path, independently of the coordinator. '+key_status)
        elif protected and preset in ('jev_auto','jev_always'):
            from .turn_engine import jev_key
            key_status='API key configured.' if jev_key() else 'API key missing: add it in /setup → Helpers.'
            detail=('The chosen reviewer runs when needed.' if preset=='jev_auto' else 'The chosen reviewer checks every command.')
            warning.update('Jev online classifies the command using TypeSafe. '+key_status+' '+detail+
                ' This is separate from the local Qwen helper. The sandbox limits execution.')
        else:
            warning.update('Protected commands use the selected Windows sandbox. Reviews use your model connection. '
                           'Interactive terminal is unavailable in this mode.' if protected else COMMANDS_WARNING)
        warning.display = mode == 'commands'

    def load_permissions(self):
        if self._is_saving or self._is_reading:
            return
        self.revision = None
        self._set_ui_operating(reading=True, saving=False)
        self._op_seq += 1
        op_seq = self._op_seq
        self._run_read(op_seq)

    def save_permissions(self, defaults: dict, expected_rev: str):
        if self._is_saving or self._is_reading:
            return
        self._set_ui_operating(reading=False, saving=True)
        self._op_seq += 1
        op_seq = self._op_seq
        self._run_save(op_seq, defaults, expected_rev)

    @work(exclusive=True, group='permissions-worker')
    async def _run_read(self, op_seq: int):
        try:
            if self.reader is None:
                raise ValueError("No permissions reader configured")
            view = await asyncio.to_thread(self.reader)
            if not self._active or op_seq != self._op_seq:
                return
            self.revision = str(view['revision'])
            self._apply_loaded_view(view)
            status = self.query_one('#permissions-status', Static)
            status.update('Permissions loaded.')
            status.remove_class('status-error')
            status.remove_class('status-success')
            self._set_ui_idle()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self._active or op_seq != self._op_seq:
                return
            self.revision = None
            status = self.query_one('#permissions-status', Static)
            status.update(f"Load failed: {plain(str(exc))}")
            status.remove_class('status-success')
            status.add_class('status-error')
            self._set_ui_idle()

    @work(exclusive=True, group='permissions-worker')
    async def _run_save(self, op_seq: int, defaults: dict, expected_rev: str):
        try:
            if self.saver is None:
                raise ValueError("No permissions saver configured")
            default = self.query_one('#permissions-plan-default', Select).value != 'go'
            new_view = await asyncio.to_thread(self.saver, defaults, expected_rev)
            if self.default_saver is not None and default != self.show_plan_default:
                await asyncio.to_thread(self.default_saver, default)
                new_view = {**new_view, 'show_plan_default': default}
            if not self._active or op_seq != self._op_seq:
                return
            self.revision = str(new_view['revision'])
            self.saved_view = new_view
            self._apply_loaded_view(new_view)
            if defaults.get('mode') != 'read' and 'defaults' in new_view:
                returned_paths = new_view['defaults'].get('write_paths', [])
                self.query_one('#permissions-paths', TextArea).load_text('\n'.join(returned_paths))
            status = self.query_one('#permissions-status', Static)
            status.update('Permissions saved for new conversations and Work.')
            status.remove_class('status-error')
            status.add_class('status-success')
            self._set_ui_idle()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self._active or op_seq != self._op_seq:
                return
            status = self.query_one('#permissions-status', Static)
            status.update(f"Save failed: {plain(str(exc))}")
            status.remove_class('status-success')
            status.add_class('status-error')
            self._set_ui_idle()

    def _apply_loaded_view(self, view: dict):
        defaults = view.get('defaults', {})
        self.reviewed_preview = view.get('admission_preview')
        policy = defaults.get('project_admission', {})
        with self.prevent(Checkbox.Changed):
            self.query_one('#permissions-root', Checkbox).value = (
                policy.get('mode') in ('adopted', 'trusted') and self.reviewed_preview is not None)
            self.query_one('#permissions-nested', Checkbox).value = (
                bool(policy.get('nested', False)) and self.reviewed_preview is not None)
        mode = defaults.get('mode', 'read')
        write_paths = defaults.get('write_paths', [])

        mode_select = self.query_one('#permissions-mode', Select)
        protection = defaults.get('command_protection', {})
        with self.prevent(Select.Changed):
            mode_select.value = mode
            self.show_plan_default = view.get('show_plan_default', True)
            self.query_one('#permissions-plan-default', Select).value = 'ask' if self.show_plan_default else 'go'
            self.relabel_plan_default()
            self.query_one('#permissions-plan', Select).value = (
                'default' if 'show_plan' not in defaults else 'ask' if defaults['show_plan'] else 'go')
            self.query_one('#permissions-review', Select).value = protection.get('review', 'off')
            self.query_one('#permissions-isolation', Select).value = protection.get('backend', 'mxc')
            preset=('jev_auto' if protection.get('classifier')=='jev' and protection.get('review_policy')=='on_demand'
                else 'jev_always' if protection.get('classifier')=='jev' else 'model') if protection else 'jev_auto'
            self.query_one('#permissions-classifier',Select).value=preset
        self.query_one('#permissions-review-limit', Input).value = str(protection.get('max_reviews', 24))

        paths_area = self.query_one('#permissions-paths', TextArea)
        if mode == 'read':
            paths_area.disabled = True
            if write_paths:
                paths_area.load_text('\n'.join(write_paths))
        else:
            paths_area.disabled = False
            paths_area.load_text('\n'.join(write_paths))

        warning_widget = self.query_one('#permissions-command-warning', Static)
        warning_widget.display = (mode == 'commands')

        self._update_details(view.get('effective_text', ''), view.get('instructions_text', ''))

    def _update_details(self, effective_text: str, instructions_text: str):
        eff_widget = self.query_one('#permissions-effective-text', Static)
        eff_widget.update(plain(effective_text or 'No effective permissions configured.'))
        inst_widget = self.query_one('#permissions-instructions-text', Static)
        inst_widget.update(plain(instructions_text or 'No project instructions recorded.'))

    def _set_ui_operating(self, reading: bool = False, saving: bool = False):
        self._is_reading = reading
        self._is_saving = saving

        self.query_one('#permissions-root', Checkbox).disabled = True
        self.query_one('#permissions-nested', Checkbox).disabled = True
        self.query_one('#permissions-mode', Select).disabled = True
        self.query_one('#permissions-plan', Select).disabled = True
        self.query_one('#permissions-plan-default', Select).disabled = True
        self.query_one('#permissions-paths', TextArea).disabled = True
        self.query_one('#permissions-review', Select).disabled = True
        self.query_one('#permissions-classifier', Select).disabled = True
        self.query_one('#permissions-isolation', Select).disabled = True
        self.query_one('#permissions-review-limit', Input).disabled = True

        self.query_one('#permissions-refresh', Button).disabled = True
        self.query_one('#permissions-save', Button).disabled = True

        can_back = reading and not saving
        self.query_one('#permissions-back', Button).disabled = not can_back
        self.query_one('#permissions-close', Button).disabled = not can_back

        status = self.query_one('#permissions-status', Static)
        status.remove_class('status-error')
        status.remove_class('status-success')
        if reading:
            status.update('Loading permissions…')
        elif saving:
            status.update('Saving permissions…')

    def _set_ui_idle(self):
        self._is_reading = False
        self._is_saving = False

        self.query_one('#permissions-back', Button).disabled = False
        self.query_one('#permissions-close', Button).disabled = False
        self.query_one('#permissions-refresh', Button).disabled = False

        mode_select = self.query_one('#permissions-mode', Select)
        mode_select.disabled = (self.revision is None)
        self.query_one('#permissions-plan', Select).disabled = (self.revision is None)
        self.query_one('#permissions-plan-default', Select).disabled = (self.revision is None)
        current_mode = str(mode_select.value) if mode_select.value != Select.BLANK else 'read'

        paths_area = self.query_one('#permissions-paths', TextArea)
        paths_area.disabled = (self.revision is None or current_mode == 'read')

        self.query_one('#permissions-save', Button).disabled = (self.revision is None)
        root = self.query_one('#permissions-root', Checkbox)
        root.disabled = self.revision is None or getattr(self, 'reviewed_preview', None) is None
        self.query_one('#permissions-nested', Checkbox).disabled = root.disabled or not root.value
        self.protection_controls()

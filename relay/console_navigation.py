"""Project-bound conversation navigation over existing saved checkpoints."""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea

from . import console_demand, relay_mesh
from .console_output import plain


class RenameDialog(ModalScreen):
    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel', priority=True),
        Binding('enter', 'save', 'Save', priority=False),
    ]

    DEFAULT_CSS = '''
    RenameDialog {
        align: center middle;
        background: $background 80%;
    }
    #rename-dialog {
        width: 58;
        max-width: 90%;
        height: auto;
        background: $panel;
        border: round $primary 40%;
        padding: 1 2;
    }
    #rename-dialog:focus-within {
        border: round $primary;
    }
    #rename-title {
        height: 1;
        text-style: bold;
        color: $foreground;
        margin-bottom: 1;
    }
    #rename-input {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
        background: $background;
    }
    #rename-input:focus {
        background: $primary 15%;
    }
    #rename-error {
        height: auto;
        min-height: 1;
        color: $error;
        margin-top: 1;
    }
    #rename-actions {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #rename-actions Button {
        height: 1;
        min-height: 1;
        width: auto;
        min-width: 10;
        border: none;
        padding: 0 1;
        margin-left: 1;
        background: transparent;
        color: $text-muted;
    }
    #rename-actions Button:hover, #rename-actions Button:focus {
        background: $primary 20%;
        color: $foreground;
    }
    #rename-actions #rename-save {
        color: $primary;
        text-style: bold;
    }
    #rename-actions #rename-save:hover, #rename-actions #rename-save:focus {
        background: $primary;
        color: $background;
    }
    RenameDialog.narrow #rename-dialog {
        width: 92%;
        padding: 1 1;
    }
    '''

    def __init__(self, *, target_id, revision, root, control_root, active_id, active_revision, generation, initial_title, is_current):
        super().__init__()
        self.captured_target_id = target_id
        self.captured_revision = revision
        self.captured_root = root
        self.captured_control_root = control_root
        self.captured_active_id = active_id
        self.captured_active_revision = active_revision
        self.captured_generation = generation
        self.captured_is_current = is_current
        self.initial_title = initial_title
        self.saving = False

    def compose(self):
        with Vertical(id='rename-dialog'):
            yield Static('Rename conversation', id='rename-title', markup=False)
            yield Input(value=self.initial_title, placeholder='Conversation title…', id='rename-input')
            yield Static('', id='rename-error', markup=False)
            with Horizontal(id='rename-actions'):
                yield Button('Cancel', id='rename-cancel')
                yield Button('Save', id='rename-save', variant='primary')

    def on_mount(self):
        self.call_after_refresh(self.query_one('#rename-input', Input).focus)

    def on_resize(self, event):
        self.set_class(event.size.width < 100 or event.size.height < 30, 'narrow')

    def on_key(self, event):
        if event.key == 'ctrl+enter':
            event.stop()
            event.prevent_default()
            return
        if event.key == 'escape':
            event.stop()
            event.prevent_default()
            self.action_cancel()
            return

    def on_input_changed(self, event):
        if event.input.id == 'rename-input':
            self.query_one('#rename-error', Static).update('')

    def on_input_submitted(self, event):
        if event.input.id == 'rename-input':
            event.stop()
            self.action_save()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'rename-save':
            self.action_save()
        elif event.button.id == 'rename-cancel':
            self.action_cancel()

    def action_cancel(self):
        if self.saving:
            return
        self.dismiss(None)

    def show_error(self, message):
        self.query_one('#rename-error', Static).update(plain(str(message)))

    def reset_saving(self):
        self.saving = False
        self.query_one('#rename-input', Input).disabled = False
        self.query_one('#rename-save', Button).disabled = False
        self.query_one('#rename-cancel', Button).disabled = False
        self.query_one('#rename-input', Input).focus()

    def action_save(self):
        if self.saving:
            return
        self.saving = True
        self.query_one('#rename-input', Input).disabled = True
        self.query_one('#rename-save', Button).disabled = True
        self.query_one('#rename-cancel', Button).disabled = True
        self.run_worker(self._do_save(), group='rename-save')

    async def _do_save(self):
        raw_title = self.query_one('#rename-input', Input).value
        try:
            title = console_demand.validate_title(raw_title)
        except ValueError as exc:
            self.show_error(str(exc))
            self.reset_saving()
            return

        app = self.app
        if app.busy or app.sending_message:
            self.show_error('Cannot rename while execution or message persistence is active.')
            self.reset_saving()
            return

        if app.demand_id != self.captured_active_id:
            self.show_error('Active conversation changed after dialog opened.')
            self.reset_saving()
            return

        if self.captured_active_id and app.demand_revision != self.captured_active_revision:
            self.show_error('Active conversation changed in another operation.')
            self.reset_saving()
            return

        if str(Path(relay_mesh.RELAY_ROOT).resolve()) != self.captured_control_root:
            self.show_error('Control root changed after dialog opened.')
            self.reset_saving()
            return

        try:
            underlying = app.screen_stack[0] if getattr(app, 'screen_stack', None) else app
            root_input = underlying.query_one('#root', Input).value.strip()
            if str(Path(root_input).resolve()) != self.captured_root:
                self.show_error('Workspace changed after dialog opened.')
                self.reset_saving()
                return
            if str(Path(app.workspace_root).resolve()) != self.captured_root:
                self.show_error('Workspace changed after dialog opened.')
                self.reset_saving()
                return
        except Exception as exc:
            self.show_error(str(exc))
            self.reset_saving()
            return

        if app.navigation_generation != self.captured_generation:
            self.show_error('Conversations list changed after dialog opened.')
            self.reset_saving()
            return

        try:
            updated_record = await asyncio.to_thread(
                console_demand.rename,
                self.captured_target_id,
                self.captured_revision,
                self.captured_root,
                self.captured_control_root,
                title,
            )
        except Exception as exc:
            self.show_error(f'Cannot rename conversation: {exc}')
            self.reset_saving()
            return

        if app.demand_id == self.captured_target_id:
            app.demand_revision = updated_record['revision']
            app.demand_state = updated_record

        self.dismiss(updated_record)


class ConversationNavigation:
    def dispatch_conversation_command(self, name, arguments):
        """Handle UI commands without passing them to the model or flow router."""
        workbench = {'/artifacts', '/tasks', '/team', '/usage', '/tools', '/skills'}
        if name not in {'/new', '/model', '/reasoning', '/diff', '/sessions', '/terminal', '/project', '/permissions', '/backlog', '/delivery', '/challenger', '/storage', '/delegation', '/routes', '/fleet', '/setup', '/recheck', '/retry_delivery', '/repair_plan'} | workbench:
            return False
        if arguments:
            self.set_status(name+' takes no arguments. Your draft has been kept.')
            return True
        if (self.sending_message or self.busy) and name not in {'/terminal', '/tasks', '/team', '/usage', '/challenger', '/storage'}:
            self.set_status('Wait for the current operation before using '+name+'.')
            return True
        if Path(self.selection().root).resolve() != Path(self.workspace_root).resolve():
            self.set_status('Apply the workspace change before using '+name+'.')
            return True
        prompt = self.query_one('#prompt', TextArea)
        if name in {'/recheck','/retry_delivery','/repair_plan'}:
            if not self.assistant_session:
                self.set_status('Open the saved conversation that produced the candidate first.')
                return True
            prompt.clear()
            self.execute(True, assistant=True, resume_only=True,
                         recheck_candidate=name=='/recheck',retry_delivery=name=='/retry_delivery',repair_plan=name=='/repair_plan')
        elif name == '/delegation':
            prompt.clear()
            self.action_delegation()
        elif name == '/routes':
            prompt.clear()
            self.action_routes()
        elif name == '/fleet':
            prompt.clear()
            self.action_fleet()
        elif name == '/setup':
            prompt.clear()
            self.action_setup()
        elif name == '/storage':
            prompt.clear()
            self.action_storage()
        elif name == '/challenger':
            prompt.clear()
            self.action_challenger()
        elif name == '/delivery':
            prompt.clear()
            self.action_delivery()
        elif name in workbench:
            draft = prompt.text
            prompt.clear()
            if not self.open_workbench(name[1:]):
                prompt.load_text(draft)
        elif name == '/new':
            draft = prompt.text
            prompt.clear()
            if not self.new_conversation():
                prompt.load_text(draft)
        elif name == '/project':
            prompt.clear()
            self.action_projects()
        elif name == '/permissions':
            prompt.clear()
            self.action_permissions()
        elif name == '/backlog':
            prompt.clear()
            self.action_backlog()
        elif name in {'/model', '/reasoning'}:
            prompt.clear()
            self.open_model_picker(reasoning=name == '/reasoning')
        elif name == '/sessions':
            prompt.clear()
            if self.panel != 'navigation':
                self.action_conversations()
            else:
                self.query_one('#conversation-search', Input).focus()
        else:
            screen = self.screen
            if name == '/diff':
                self.action_changes()
            else:
                self.action_processes()
            if self.screen is not screen:
                prompt.clear()
        return True

    def close_navigation(self):
        if self.panel == 'navigation':
            self.show_panel(None)
        self.query_one('#prompt', TextArea).focus()

    def action_conversations(self):
        if self.panel == 'navigation':
            self.close_navigation()
            return
        self.show_panel('navigation')
        self.query_one('#conversation-search', Input).focus()
        self.load_conversations()

    def navigation_matches_workspace(self):
        if self.busy or self.sending_message:
            self.notify('Wait for the current turn and queued message to finish before switching conversations.')
            return False
        try:
            if Path(self.selection().root).resolve() != Path(self.workspace_root):
                self.notify('Apply the workspace folder in Settings before switching conversations.')
                return False
        except OSError as exc:
            self.notify(str(exc))
            return False
        return True

    @work(group='conversation-navigation')
    async def load_conversations(self):
        self.navigation_generation += 1
        generation, root = self.navigation_generation, self.workspace_root
        self.navigation_rows = []
        self.navigation_error = ''
        self.render_conversations(loading=True)
        try:
            rows, errors = await asyncio.to_thread(console_demand.inventory, root, relay_mesh.RELAY_ROOT)
        except Exception as exc:
            rows, errors = [], [str(exc)]
        if (generation != self.navigation_generation or root != self.workspace_root
                or not self.query_one_optional('#conversation-list')):
            return
        self.navigation_rows = rows
        self.navigation_error = f'{len(errors)} saved record(s) unavailable.' if errors else ''
        self.query_one('#conversation-state').tooltip = Text(plain('\n'.join(errors))) if errors else None
        self.render_conversations()

    def render_conversations(self, *, loading=False):
        if not self.query_one_optional('#conversation-list'):
            return
        query = self.query_one('#conversation-search', Input).value.strip().casefold()
        self.navigation_shown = [row for row in self.navigation_rows
                                 if query in (row['title']+' '+row['id']).casefold()]
        menu = self.query_one('#conversation-list', OptionList)
        previous = (menu.highlighted if menu.highlighted is not None else 0)
        selected = self.navigation_selected
        labels = []
        for row in self.navigation_shown:
            title = plain(row['title']).replace('\n', ' ').replace('\t', ' ')
            label = Text(title)
            label.append('\n'+('Current · ' if row['id'] == self.demand_id else '')
                         +row['updated_at'][:10]+' · '+row['id'][:6]+'\n', style='dim')
            labels.append(label)
        menu.set_options(labels)
        if self.navigation_shown:
            menu.highlighted = next((i for i, row in enumerate(self.navigation_shown)
                                     if row['id'] == selected), min(previous, len(labels)-1))
        message = ('Loading conversations…' if loading else
                   'No matching conversations.' if query and not labels else
                   'Your saved conversations will appear here.' if not labels else
                   f'{len(labels)} conversation(s) · Enter to open')
        if self.navigation_error:
            message += '\n'+self.navigation_error
        self.query_one('#conversation-state').update(message)
        self.query_one('#navigation-project').update(Text(plain(Path(self.workspace_root).name), overflow='ellipsis', no_wrap=True))
        self.query_one('#navigation-project').tooltip = Text(plain(self.workspace_root))

    def open_conversation(self, index):
        if not self.navigation_matches_workspace() or not 0 <= index < len(self.navigation_shown):
            return
        identity = self.navigation_shown[index]['id']
        if identity == self.demand_id:
            self.close_navigation()
            return
        try:
            # Validate the destination before saving or replacing any visible state.
            console_demand.read(identity, self.workspace_root, relay_mesh.RELAY_ROOT)
            if self.demand_id or self.selection().prompt.strip() or self.context_paths or self.attachments or self.assistant_history:
                self.checkpoint('draft_saved')
        except Exception as exc:
            self.notify('Cannot switch conversations: '+str(exc), severity='error')
            return
        widget = self.query_one('#saved-demand', Select)
        widget.set_options([(Text(plain(row['title'])), row['id']) for row in self.navigation_rows])
        widget.value = identity
        self.open_demand()
        if self.demand_id == identity:
            self.navigation_selected = identity
            self.close_navigation()

    def new_conversation(self):
        if not self.navigation_matches_workspace():
            return False
        try:
            if self.demand_id is None and self.has_draft():
                self.checkpoint('draft_saved')
        except Exception as exc:
            self.notify('Current draft could not be saved: '+str(exc), severity='error')
            return False
        self.new_demand()
        if self.demand_id is None:
            self.set_status('New conversation · Model connection unchecked')
            self.show_panel(None)
            self.close_navigation()
            return True
        return False

    def on_input_submitted(self, event):
        if event.input.id == 'conversation-search':
            menu = self.query_one('#conversation-list', OptionList)
            if menu.highlighted is not None:
                self.open_conversation(menu.highlighted)
            event.stop()

    def action_rename_conversation(self):
        """Rename the current saved conversation (command palette)."""
        if self.busy or self.sending_message:
            self.notify('Wait for the current operation before renaming conversation.')
            return
        try:
            if Path(self.selection().root).resolve() != Path(self.workspace_root).resolve():
                self.notify('Apply the workspace change before renaming conversation.')
                return
        except OSError as exc:
            self.notify(str(exc))
            return
        if not self.demand_id:
            self.notify('Current conversation is not saved yet. Unsaved conversations cannot be renamed.')
            return
        title = ''
        if self.demand_state and self.demand_state.get('title'):
            title = self.demand_state['title']
        elif self.demand_state:
            events = self.demand_state.get('events', [])
            selection = (self.demand_state.get('snapshot') or {}).get('selection') or {}
            title = next(((e.get('detail') or {}).get('operator') or e.get('selection', {}).get('prompt')
                          for e in events if (e.get('detail') or {}).get('operator') or e.get('selection', {}).get('prompt')),
                         selection.get('work') or 'Demand')
        self.open_rename_dialog(identity=self.demand_id, title=title)

    def action_rename_highlighted(self):
        """Rename the highlighted conversation in the drawer without opening it."""
        if self.busy or self.sending_message:
            self.notify('Wait for the current operation before renaming.')
            return
        if not self.navigation_matches_workspace():
            return
        if not self.query_one_optional('#conversation-list'):
            return
        menu = self.query_one('#conversation-list', OptionList)
        if menu.highlighted is None or not (0 <= menu.highlighted < len(self.navigation_shown)):
            self.notify('Choose a conversation to rename.')
            return
        row = self.navigation_shown[menu.highlighted]
        self.open_rename_dialog(identity=row['id'], title=row['title'])

    def open_rename_dialog(self, identity, title=''):
        if self.busy or self.sending_message:
            self.notify('Wait for the current operation before renaming.')
            return
        try:
            if Path(self.selection().root).resolve() != Path(self.workspace_root).resolve():
                self.notify('Apply the workspace change before renaming.')
                return
        except OSError as exc:
            self.notify(str(exc))
            return

        root = str(Path(self.workspace_root).resolve())
        control_root = str(Path(relay_mesh.RELAY_ROOT).resolve())
        is_current = (identity == self.demand_id)

        if is_current:
            if not self.demand_id:
                self.notify('Current conversation is not saved yet. Unsaved conversations cannot be renamed.')
                return
            revision = self.demand_revision
            if not title and self.demand_state:
                title = self.demand_state.get('title') or ''
        else:
            try:
                record = console_demand.read(identity, root, control_root)
                revision = record['revision']
                if not title:
                    title = record.get('title') or ''
            except Exception as exc:
                self.notify(f'Cannot read conversation: {exc}', severity='error')
                return

        previous_focus = self.focused
        captured_target_id = identity

        def on_dismiss(result):
            if result:
                for r in self.navigation_rows:
                    if r['id'] == captured_target_id:
                        r['title'] = result['title']
                        r['updated_at'] = result['updated_at']
                self.render_conversations()

            if previous_focus is not None:
                try:
                    previous_focus.focus()
                except Exception:
                    pass

        dialog = RenameDialog(
            target_id=identity,
            revision=revision,
            root=root,
            control_root=control_root,
            active_id=self.demand_id,
            active_revision=self.demand_revision if self.demand_id else None,
            generation=self.navigation_generation,
            initial_title=title,
            is_current=is_current,
        )
        self.push_screen(dialog, on_dismiss)

    def on_key(self, event):
        if self.panel == 'navigation':
            if event.key == 'escape':
                self.close_navigation()
                event.stop(); event.prevent_default()
            elif event.key == 'down' and self.focused is self.query_one('#conversation-search'):
                self.query_one('#conversation-list').focus()
                event.stop(); event.prevent_default()
            elif event.key in {'r', 'f2'} and self.focused is self.query_one('#conversation-list'):
                self.action_rename_highlighted()
                event.stop(); event.prevent_default()

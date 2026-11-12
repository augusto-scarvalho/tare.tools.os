"""Interactive terminal over existing native sessions and Work execution."""
import argparse
import asyncio
from dataclasses import replace
from copy import deepcopy
from . import relay_mesh
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.theme import Theme
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.widgets import (Button, Checkbox, Collapsible, Footer, Header, Input, Label, OptionList,
                             Select, Static, TextArea)

from .console_backend import Backend, Selection, command, default_deployment
from .console_output import Transcript, SelectableMessage, observe_process, plain, show
from .demand_console import DemandConsole, EMPTY_SELECTION
from .order_console import OrderConsole
from .console_context import FileContext, context_prompt
from .console_navigation import ConversationNavigation
from .console_appearance import APPEARANCES, read_appearance, save_appearance
from .console_attachments import ComposerAttachments, AttachmentPicker
from .console_header import ConversationHeader
from .console_projects import ProjectNavigation
from .console_permissions import ProjectPermissions
from .console_graph import ProjectGraph
from .console_models import ConversationModels
from .console_workbench import ConversationWorkbench, WorkbenchPanel


from .console_panels import SupportPanel


class ConsentPanel(SupportPanel):
    """Host choices carry no synthetic user message; Escape keeps typing available."""
    BINDINGS = [('escape', 'close', 'Type a reply')]

    def __init__(self, question):
        super().__init__()
        self.question = question

    def compose(self):
        with Vertical(classes='support-body'):
            yield Static(self.question, markup=False)
            with Horizontal():
                yield Button('Deny', id='consent-deny')
                yield Button('Allow', id='consent-allow')
            yield Static('Escape to answer in the conversation.')

    def on_mount(self):
        self.query_one('#consent-deny', Button).focus()

    def action_close(self):
        self.dismiss(None)

    def on_button_pressed(self, event):
        if event.button.id in {'consent-allow', 'consent-deny'}:
            event.stop()
            self.dismiss(event.button.id.removeprefix('consent-'))


class PromptArea(TextArea):
    """Enter sends; completion menus consume their keys before submission."""

    def on_resize(self):
        # Wait for TextArea's own wrapping update. Panels can change the available
        # width without resizing the terminal or modifying the draft.
        self.call_after_refresh(self.app.resize_composer)

    async def _on_key(self, event):
        if event.key in {'up', 'down', 'tab', 'enter', 'escape'} and (self.app.context_key(event.key) or self.app.slash_key(event.key)):
            event.stop()
            event.prevent_default()  # Skips TextArea insertion; stop() skips cursor/focus bindings.
        elif event.key in {'enter', 'ctrl+enter'}:
            event.stop()
            event.prevent_default()
            self.app.action_start()
        elif event.key in {'shift+enter', 'ctrl+j'}:
            event.stop()
            event.prevent_default()
            self.insert('\n')


def session_has_terminal(selection):
    """Commands are enabled for this turn: chosen for a new conversation, or recorded in the resumed one."""
    if getattr(selection, 'terminal', False):
        return True
    try:
        from .native_agent import read_json
        return bool(read_json(Path(selection.session)/'native-agent.json', 1024*1024)['config'].get('terminal'))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):  # RuntimeError: ConversationRejected
        return False


class WorkConsole(ConversationWorkbench, ConversationModels, ProjectGraph, ProjectPermissions, ProjectNavigation, ConversationHeader, ComposerAttachments, ConversationNavigation, FileContext, OrderConsole, DemandConsole, App):
    def get_driver_class(self):
        from .console_windows_input import unicode_windows_driver
        return unicode_windows_driver(super().get_driver_class())

    TITLE = 'tare · assistant'
    BINDINGS = [('ctrl+r', 'reload', 'Reload'), Binding('ctrl+x', 'cancel', 'Cancel run', priority=True),
                ('ctrl+q', 'quit', 'Quit'), ('f1', 'conversations', 'Conversations'), ('f2', 'workspace', 'Settings'),
                Binding('f3','changes','Changes',priority=True),
                Binding('f4','processes','Processes',priority=True),
                Binding('f5','projects','Projects',priority=True),
                Binding('f8', 'files', 'Files', priority=True),
                ('f6', 'next_step', 'Next step'), Binding('ctrl+end', 'latest', 'Latest', priority=True),
                ('ctrl+enter', 'start', 'Send / Start')]
    CSS = '''
    #execution-scope { height: auto; max-height: 3; color: $warning; }
    Screen { background: $surface; }
    #layout { height: 1fr; }
    #selection { width: 34; min-width: 28; padding: 0 1; border-right: solid $primary; }
    #selection Button { width: 1fr; margin-bottom: 1; }
    Collapsible { padding: 0; margin: 1 0; }
    Transcript .activity { padding: 0; margin: 0; border: none; }
    #flow-heading { height: auto; max-height: 2; text-style: bold; color: $accent; padding: 0 1; }
    Screen #selection { display: none; }
    Screen.assistant #demand-status { display: none; }
    Screen.assistant #review { display: none; }
    Screen #prompt-label { display: none; }
    Screen #actions { align-horizontal: right; }
    Screen #actions Button { width: auto; min-width: 14; }
    Screen.compact #layout { layout: vertical; }
    Screen.compact #selection { width: 1fr; height: 1fr; border: none; }
    Screen.compact #detail { height: 1fr; }
    Screen.compact #hint { display: none; }
    Screen.compact #flow-heading { padding: 0; }
    Screen.compact #status { padding: 0; }
    Screen.compact #prompt { height: 5; }
    #detail { width: 1fr; height: 1fr; padding: 0 1; }
    #composer { height: auto; max-height: 70%; padding: 0 1; border-top: solid $primary; }
    Screen #composer { max-height: 55%; }
    Label { margin-top: 1; }
    #prompt, #research-urls, #intake-criteria, #intake-argv { height: 6; }
    #message { height: 3; }
    #outcome-summary, #outcome-remaining, #outcome-resolutions { height: 4; }
    #outcome-options { height: auto; border: round $primary; padding: 0 1; }
    #message-options { height: auto; }
    #message-status { height: auto; }
    #status-row { height: auto; max-height: 3; }
    #activity-symbol { width: 2; height: 1; color: $primary; }
    #status { width: 1fr; height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    #output { height: 1fr; min-height: 5; }
    #actions { height: auto; }
    #actions Button { min-width: 10; width: 1fr; }
    #direct-check-options, #session-options, #work-options, #intake-options,
    #plan-options, #sequence-options, #parallel-options, #shared-findings-options,
    #research-options, #findings-options, #synthesis-options { height: auto; }
    #synthesis-inputs { height: 8; }
    #hint { height: auto; color: $text-muted; }
    #demand-status { height: auto; max-height: 5; color: $text-muted; }
    #slash-box { height: auto; display: none; }
    #slash-menu { max-height: 7; }
    Screen.compact #slash-menu { max-height: 3; }
    #slash-description { height: auto; max-height: 2; color: $text; padding: 0 1; }
    #slash-hint { height: 1; color: $text-muted; padding: 0 1; }
    #context-chips { height: 1; display: none; scrollbar-size: 0 0; }
    #context-chips Button { width: auto; min-width: 4; max-width: 40; height: 1; min-height: 1; border: none; margin-right: 1; padding: 0 1; background: $primary 15%; color: $primary; }
    #attachment-chips { height: 1; display: none; scrollbar-size: 0 0; margin-bottom: 1; }
    #attachment-chips Button { width: auto; min-width: 2; max-width: 36; height: 1; min-height: 1; border: none; padding: 0 1; background: $primary 15%; color: $primary; }
    #attach { background: transparent; color: $text-muted; }
    #context-box { height: auto; display: none; }
    #context-menu { max-height: 5; border: none; padding: 0; background: $panel; }
    Screen.compact #context-menu { max-height: 3; }
    #context-hint { height: auto; max-height: 2; color: $text-muted; }
    Screen { background: $background; }
    Screen Header, Screen Footer { display: none; }
    Screen #layout { align-horizontal: center; }
    Screen #detail { max-width: 104; padding: 0 2; }
    Screen #flow-heading { height: 1; color: $foreground; text-style: none; }
    Screen #status { margin: 0; padding: 0; color: $text-muted; }
    Screen #composer { border: round $primary 25%; padding: 0 1; margin: 0 0 1 0; background: $panel; }
    Screen #composer:focus-within { border: round $primary 65%; }
    Screen #prompt { height: 1; border: none; background: transparent; padding: 0; }
    Screen #prompt .text-area--cursor-line { background: transparent; }
    Screen #prompt .text-area--placeholder { color: $text-muted; }
    Screen #actions { height: 1; margin: 0; }
    Screen #actions Button { height: 1; min-height: 1; min-width: 10; border: none; padding: 0 1; margin: 0 0 0 1; text-style: none; }
    Screen #actions #attach { margin: 0 1 0 0; }
    Screen #actions #resume { background: transparent; color: $text-muted; }
    Screen #actions #resume:hover, Screen #actions #resume:focus,
    Screen #actions #attach:hover, Screen #actions #attach:focus { background: $primary 20%; color: $foreground; }
    Screen #actions #start { background: transparent; color: $primary; text-style: bold; }
    Screen #actions #start:hover, Screen #actions #start:focus { background: $primary; color: $background; }
    Screen #cancel { background: transparent; color: $warning; }
    #composer-hint { display: none; height: 1; width: 1fr; color: $text-muted; }
    Screen #composer-hint { display: block; }
    Screen #hint { display: none; }
    Screen #slash-menu { background: $panel; border: none; padding: 0; }
    #slash-menu > .option-list--option { padding: 0 1; }
    Screen #slash-box { border-top: solid $primary 25%; margin: 0; padding: 0; }
    #attachment-chips.completion-open { margin-bottom: 0; }
    Screen #selection { width: 42; background: $panel; border-right: solid $primary 25%; }
    Screen #selection Label { margin: 1 0 0 0; color: $text-muted; }
    Screen #selection Input { height: 1; min-height: 1; border: none; padding: 0 1; background: $background; }
    Screen #selection Input:focus { background: $primary 15%; }
    Screen #selection Select { height: 1; margin: 0; }
    Screen #selection SelectCurrent { height: 1; border: none; padding: 0 1; background: $background; }
    Screen #selection Select:focus > SelectCurrent { background: $primary 15%; }
    Screen #selection Checkbox { width: 1fr; height: auto; min-height: 1; border: none; padding: 0; margin: 0 0 1 0; }
    Screen #selection Collapsible { border: none; margin: 1 0; padding: 0; background: transparent; }
    Screen #selection Collapsible > Contents { padding: 0 0 0 1; }
    Screen #selection .settings-note { height: auto; color: $text-muted; margin-bottom: 1; }
    Screen #selection #reload { margin-top: 1; }
    Screen #selection #new-demand { background: transparent; color: $primary; }
    Screen #selection #settings-heading { margin-bottom: 1; }
    Screen #selection Button:hover, Screen #selection Button:focus { background: $primary 30%; color: $foreground; }
    Screen #selection #appearance { margin-bottom: 1; }
    Screen.compact #detail { padding: 0 1; }
    Screen.compact #composer { margin-bottom: 0; }
    #files-pane { display: none; width: 36; min-width: 30; height: 1fr; padding: 0 1; background: $panel;
                  border-left: solid $primary 25%; }
    Screen.compact #files-pane { width: 1fr; border: none; }
    #files-title { height: 1; text-style: bold; }
    #files-git, #files-summary, #files-hint { height: auto; max-height: 2; color: $text-muted; }
    #files-touched { height: auto; max-height: 40%; border: none; padding: 0; background: transparent; }
    #files-tree { height: 1fr; padding: 0; background: transparent; }
    #files-touched > .option-list--option { padding: 0; }
    #navigation { display: none; width: 32; min-width: 26; height: 1fr; padding: 1; background: $panel; border-right: solid $primary 25%; }
    #navigation-project { height: 1; text-style: bold; color: $foreground; }
    #navigation-heading { height: 1; margin-top: 1; color: $text-muted; }
    #navigation Button { width: 1fr; min-width: 0; height: 1; min-height: 1; border: none; margin: 1 0 0 0; padding: 0 1; }
    #conversation-new { background: $primary 15%; color: $primary; }
    #conversation-search { margin: 1 0; border: round $primary 40%; }
    #conversation-search:focus { border: round $primary; }
    #conversation-list { height: 1fr; border: none; padding: 0; background: transparent; }
    #conversation-list > .option-list--option { padding: 0 1; }
    #conversation-state { height: auto; max-height: 3; color: $text-muted; margin-top: 1; }
    #conversation-rename { background: transparent; }
    #conversation-rename:hover, #conversation-rename:focus { background: $primary 20%; color: $foreground; }
    #conversation-close { background: transparent; }
    #settings-heading { display: none; height: 1; dock: top; background: $panel; }
    Screen #settings-heading { display: block; }
    #settings-heading Static { width: 1fr; text-style: bold; }
    #settings-heading Button { width: auto; min-width: 0; height: 1; min-height: 1; border: none; background: transparent; margin: 0; padding: 0; color: $text-muted; }
    Screen #selection Button { height: 1; min-height: 1; border: none; padding: 0 1; background: $primary 15%; }
    Screen #selection #settings-close { background: transparent; padding: 0; }
    #chat-toolbar { height: auto; }
    Screen #chat-toolbar { height: 1; margin: 1 0 0 0; }
    Screen #chat-toolbar #flow-heading { width: 1fr; margin: 0; padding: 0; }
    #chat-toolbar Button { display: none; width: auto; min-width: 0; height: 1; min-height: 1; margin-left: 2; padding: 0; border: none; background: transparent; color: $text-muted; }
    Screen #chat-toolbar Button { display: block; }
    #chat-toolbar Button:hover, #chat-toolbar Button:focus { color: $primary; text-style: bold; }
    Screen.compact #navigation { width: 1fr; height: 1fr; border: none; }
    Screen.compact #chat-toolbar { margin: 0; }
    #composer > Button, #decision-actions Button, #send-message {
        height: 1; min-height: 1; width: auto; min-width: 0;
        border: none; padding: 0 1; margin: 1 0 0 0;
        background: $primary 15%;
    }
    #decision-actions { height: 2; }
    #work-controls { height: 1; }
    #work-controls Button { width: auto; min-width: 0; height: 1; min-height: 1; border: none; padding: 0 1; margin-right: 2; }
    #work-note-row { height: 1; }
    #work-note { width: 1fr; height: 1; min-height: 1; border: none; padding: 0 1; }
    #work-note-row Button { width: auto; min-width: 0; height: 1; min-height: 1; border: none; padding: 0 1; margin: 0; }
    #work-control-status { height: auto; max-height: 2; }
    #decision-actions Button { min-width: 0; width: auto; }
    #decision-actions Button { margin-right: 2; }
    #composer > Button:focus, #decision-actions Button:focus, #send-message:focus {
        background: $primary 35%;
    }
    #connection-row { display: none; height: 1; }
    Screen.assistant #connection-row { display: block; }
    #session-context { display: none; height: 1; width: 1fr; color: $text-muted; }
    Screen.assistant #session-context { display: block; }
    #connection-check { height: 1; min-height: 1; width: auto; min-width: 0;
        margin-left: 2; padding: 0; border: none; background: transparent; color: $text-muted; }
    #connection-check:hover, #connection-check:focus { color: $primary; text-style: bold; }
    #model { display: none; }
    #model-controls { height: 1; margin-bottom: 1; display: none; }
    Screen.assistant #model-controls { display: block; }
    #model-controls Button { height: 1; min-height: 1; width: auto; max-width: 65%; min-width: 0;
        border: none; padding: 0 1 0 0; margin-right: 2; background: transparent; color: $text-muted; }
    #model-controls Button:hover, #model-controls Button:focus { color: $primary; }
    '''

    def __init__(self, root=None, *, backend=None, initial_mode='session', guided=False,
                 storage_preferred=None, storage_fallback=None, discover_clis=False,
                 resume_coordinator_seconds=0,resume_coordinator_calls=0,default_coordinator=''):
        super().__init__()
        self.discover_clis = discover_clis
        self.asks_providers = discover_clis  # the interactive entry: /usage reads the providers' quota again
        self.resume_coordinator_seconds=resume_coordinator_seconds
        self.resume_coordinator_calls=resume_coordinator_calls
        from .execution_scope import Scope, current
        self.default_coordinator = Scope(coordinator=default_coordinator or current().coordinator).validate().coordinator
        self.conversation_coordinator = self.default_coordinator
        for name, primary, foreground, background, panel, dark in (
                ('tare-dark', '#8bd5c4', '#e2e8ed', '#14171b', '#1c2229', True),
                ('tare-light', '#176b60', '#25313b', '#faf9f6', '#eeefec', False),
                ('tare-contrast', '#97d9ff', '#ffffff', '#000000', '#101010', True)):
            self.register_theme(Theme(name=name, primary=primary, secondary=primary,
                accent=primary, foreground=foreground, background=background,
                surface=background, panel=panel, dark=dark,
                warning='#d5b778' if dark else '#805e13',
                error='#e89999' if dark else '#a32d35',
                success='#a6c598' if dark else '#34652f',
                variables={'text-muted': foreground if name == 'tare-contrast' else
                           '#9daab6' if dark else '#596671'}))
        self.theme = 'tare-dark'
        self.root = str(Path(root or Path.cwd()).resolve())
        self.workspace_root = self.root
        self.storage_preferred = storage_preferred
        self.storage_fallback = storage_fallback
        self.permission_write_paths = []
        self.project_admission = {'mode': 'quarantine', 'root_sha256': None, 'nested': False}
        self.command_protection = {}
        self.project_graph_path = ''
        self.graph_profile_root = self.graph_profile_error = None
        self.permission_profile_root = self.permission_profile_error = None
        self.assistant_session = None
        self.assistant_history = []
        self.model_preferences = None
        self.backend = backend or Backend()
        self.process = None
        self.busy = False
        self.presence_timer = None
        self.presence_step = 0
        self.presence_kind = None
        self.presence_failed = self.presence_cancelled = False
        self.terminal_title = None
        self.terminal_title_saved = False
        self.close_after_run = False
        self.active = None
        self.last_result = None
        self.folder_request = None  # a folder the agent asked to read, awaiting the user's reply
        self.folder_note = ''
        self.last_progress = None
        self.cancel_requested = False
        self.reviewed_findings = None
        self.reviewed_plan = None
        self.reviewed_preparation = None
        self.reviewed_revision = None
        self.reviewed_decision = None
        self.reviewed_sequence_revision = None
        self.reviewed_outcome = None
        self.sending_message = False
        self.message_draft = None
        self.last_sent_message = None
        self.initial_mode = initial_mode
        self.panel = None  # the open side panel: show_panel()
        self.files_open = False  # the right files pane (F8), remembered per machine: sync_files_pane()
        self.initial_guided = guided
        self.entry_proposal = None
        self.entry_history = ()
        self.entry_questions = ()
        self.entry_root = None
        self.entry_cancel = None
        self.demand_id = None
        self.demand_revision = 0
        self.demand_state = None
        self.demand_stages = {}
        self.order_draft = {}
        self.reviewed_order = self.order_loaded = None
        self.slash_shown = ()
        self.slash_dismissed = None
        self.context_root = ''
        self.context_paths = []
        self.context_query = self.context_dismissed = None
        self.context_choices = []
        self.context_chip_lock = asyncio.Lock()
        self.context_rendered = None
        self.attachments=[]
        self.attachment_root=self.attachment_project=''
        self.attachment_chip_lock=asyncio.Lock()
        try:
            from .native_attachments import require_support
            require_support()
            self.attachment_support=True
        except ValueError:
            self.attachment_support=False
        self.header_identity = self.header_details = None
        self.header_loading = False
        self.header_observed = 0.0
        self.header_refresh_pending = False
        self.connection_observation = self.connection_cancellation = self.connection_task = None
        self.connection_loading = False
        self.connection_closing = False
        self.navigation_generation = 0
        self.navigation_rows = self.navigation_shown = []
        self.navigation_selected = None
        self.navigation_error = ''
        self._message_read_in_flight = False
        self._message_refresh_pending = False
        self._message_proofs = {}
        self._message_generation = 0
        self._message_observed_scope = None
        self._message_next_poll = 0.0
        self._message_unresolved = False
        self._message_save_owner = None

    def compose(self) -> ComposeResult:
        # Set before the widgets mount: adding a screen class later restyles all ~300 of them.
        self.screen.set_class(self.initial_mode == 'assistant', 'assistant')
        yield Header()
        with Horizontal(id='layout'):
            with Vertical(id='navigation'):
                yield Static('', id='navigation-project', markup=False)
                yield Button('Switch project · F5', id='navigation-projects')
                yield Static('Conversations', id='navigation-heading', markup=False)
                yield Button('+ New conversation', id='conversation-new')
                yield Input(placeholder='Search conversations…', id='conversation-search')
                yield OptionList(id='conversation-list', markup=False)
                yield Static('', id='conversation-state', markup=False)
                yield Button('Rename · r', id='conversation-rename')
                yield Button('Back to chat · Esc', id='conversation-close')
            with VerticalScroll(id='selection'):
                with Horizontal(id='settings-heading'):
                    yield Static('Settings', id='settings-title', markup=False)
                    yield Button('Back · F2', id='settings-close')
                from .native_installation import version_label
                yield Static(version_label(), id='product-version', classes='settings-note', markup=False)
                yield Label('Workspace folder')
                yield Input(self.root, id='root')
                yield Button('Browse projects · F5', id='settings-projects')
                yield Button('Apply workspace', id='reload')
                yield Button('New conversation', id='new-demand')
                yield Button('Project permissions', id='settings-permissions')
                yield Button('Project backlog', id='settings-backlog')
                with Collapsible(title='Flow progress and next steps', collapsed=True, id='flow-progress'):
                    yield Static('Demand saved on Send, Start or Save draft.', id='demand-status', markup=False)
                    yield Button('Continue demand', variant='primary', id='continue-demand')
                    yield Checkbox('Open next step after success', self.initial_guided, id='guide-next')
                    yield Button('Review linked Works', id='review-demand-sequence')
                    yield Button('Run reviewed Works', id='run-demand-sequence')
                    yield Button('Review demand result', id='review-demand-outcome')
                yield Label('Model · new conversations')
                yield Select([(default_deployment(),) * 2], value=default_deployment(), allow_blank=False, id='model')
                yield Button('Choose model and reasoning…', id='settings-model')
                yield Checkbox('Local models in this conversation', True, id='local-models',
                               tooltip='Unchecked, this conversation runs on cloud or CLI models only: tare-auto, '
                                       'judges, challengers and delegated exploration skip local ones. Kept with the '
                                       'conversation; set it before the first message to start that way.')
                from .turn_engine import leaves_gpu_to_others
                yield Checkbox('Leave the local GPU to others (whole machine)', leaves_gpu_to_others(),
                               id='avoid-local-gpu',
                               tooltip='While checked, every conversation on this machine (this TUI, tare serve and '
                                       'its VS Code chats) skips the shared local GPU. The choice is saved.')
                from .heads_up import enabled as heads_up_enabled
                yield Checkbox('Heads-up notes after each turn (whole machine)', heads_up_enabled(), id='heads-up',
                               tooltip='After a finished turn, a model may write a note on what the reply left unsaid: '
                                       'it runs after work was delivered or attempted, and now and then otherwise. '
                                       "A note's buttons ask the agent or rate it; /heads-up puts the last notes in the "
                                       'composer. The choice is saved.')
                yield Static('Use Choose model → Continue task to change a saved conversation.', classes='settings-note', markup=False)
                with Collapsible(title='Connection details', collapsed=True, id='connection-section'):
                    yield Static('', id='connection-details', classes='settings-note', markup=False)
                    yield Button('Authenticate', id='connection-authenticate')
                with Collapsible(title='Execution options', collapsed=True, id='model-settings'):
                    yield Static('Applies to new executions.', classes='settings-note', markup=False)
                    yield Label('Custom model profile · optional')
                    yield Input(placeholder='Path to HTTP/profile JSON', id='profile')
                    yield Checkbox('Auto-select executor', False, id='auto-execution')
                    from .execution_scope import current
                    resources = current().unlimited
                    yield Checkbox('Unlimited: '+', '.join(resources) if resources else 'Unlimited calls and time',
                                   True, id='unlimited', disabled=bool(resources))
                    if resources:
                        yield Static('Set by --unlimited for this terminal instance and its workers.',
                                     classes='settings-note', markup=False)
                    yield Label('Model call limit')
                    yield Input(placeholder='Blank: model profile limit', id='max-model-calls', disabled=True)
                    yield Label('Time limit · seconds')
                    yield Input(placeholder='Blank: model profile limit', id='max-seconds', disabled=True)
                    yield Static(('Launch flag controls the listed resources for new executions. ' if resources else
                                  'Turn off Unlimited to set finite call/time limits. ')+
                                 'Blank fields inherit the model profile; '
                                 'without profile limits, the defaults are 16 calls and 300 seconds. '
                                 'Saved sessions keep their recorded limits.', classes='settings-note', markup=False)
                    yield Checkbox('Stream responses', True, id='streaming')
                    yield Checkbox('SpecGraph advisory lookup · new conversations', False, id='specgraph-context')
                    yield Static('Requires a published index. Consultative snapshot lookup: not proof of coverage, '
                                 'not additional permissions. Saved sessions keep their choice.',
                                 classes='settings-note', markup=False)
                    yield Checkbox('Allow system commands', False, id='terminal')
                    with Vertical(id='direct-check-options'):
                        yield Label('Verification argv for a new conversation')
                        yield Input(placeholder='Optional: ["python", "-m", "pytest", "-q"]', id='verifier')
                yield Label('Appearance')
                yield Select([(label, name) for name, label in APPEARANCES],
                             value=self.theme, prompt='Custom theme', id='appearance')
                yield Label('Optional flow')
                yield Select([('Conversation · assistant', 'assistant'), ('Choose flow · optional', 'conversation'),
                              ('Session · direct task', 'session'), ('PLAN · propose graph work', 'plan'),
                              ('IMPLEMENT · graph Work', 'work'), ('SEQUENCE · selected Works', 'sequence'),
                              ('EXPLORE · independent questions', 'parallel'), ('RESEARCH · web sources', 'research'), ('SYNTHESIS · reviewed flows', 'synthesis')],
                             value=self.initial_mode, allow_blank=False, id='mode')
                with Collapsible(title='Saved conversations and flows', collapsed=True, id='demand-library'):
                    yield Button('Find saved demands', id='list-demands')
                    yield Select([], prompt='Choose a demand', id='saved-demand')
                    yield Button('Open demand', id='open-demand')
                    yield Button('Save draft', id='save-demand')
                    yield Button('Check interrupted flow', id='reconcile-demand')
                    yield Select([], prompt='Linked stages', id='demand-stage')
                    yield Button('Open linked stage', id='open-demand-stage')
                with Vertical(id='session-options'):
                    yield Select([('Implementer · may edit', 'implementer'), ('Explorer · read only', 'explorer'),
                                  ('Auditor · read and verify', 'auditor')],
                                 value='implementer', allow_blank=False, id='session-role')
                    yield Label('Saved session')
                    yield Select([], prompt='Choose to resume or review', id='session')
                    yield Checkbox('Recheck changed files on resume', id='refresh')
                with Vertical(id='work-options'):
                    yield Label('Graph Work')
                    yield Select([], prompt='Choose a Work', id='work')
                    yield Input(placeholder='Work ID (also accepts an unlisted ID)', id='work-id')
                    yield Static('Enter inspects the recipe. Start executes the Work.', markup=False,
                                 classes='settings-note')
                    yield Label('Independent code auditor for a new Work run')
                    yield Select([('Use existing audit policy', ''), ('Auto · independent native auditor', 'auto')], value='', allow_blank=False, id='auditor')
                    yield Checkbox('New plan review on Work resume', id='retry-plan')
                    yield Checkbox('Retry failed audit transport/format on resume', id='retry-audit')
                    yield Input(placeholder='Optional pause before implementation: question', id='wait-question')
                    yield Button('Open saved human decision', id='open-decision')
                    yield Input(placeholder='Decision note (optional)', id='decision-note')
                    yield Label('Prepare a PLAN proposal for execution')
                    yield Button('Create source from this Work', id='draft-intake')
                    with Vertical(id='intake-options'):
                        yield Label('Review criteria · one per line')
                        yield TextArea(id='intake-criteria')
                        yield Label('Verification · one argument per line, no shell or quotes')
                        yield TextArea(id='intake-argv')
                        yield Button('Preview new source and preparation', id='preview-intake')
                    yield Label('Or bind an existing source')
                    yield Button('Load sources', id='load-sources')
                    yield Select([], prompt='Choose a spec', id='source-ref')
                    yield Input(placeholder='Criterion IDs: AC-01, AC-02', id='criterion-ids')
                    yield Input(placeholder='Verification argv: ["python", "-m", "pytest", "-q"]', id='work-verifier')
                    yield Button('Preview preparation', id='prepare-work')
                    yield Label('Revise an existing Work using the selected source and criteria')
                    yield Input(placeholder='Reason for revision', id='revision-reason')
                    yield Input(placeholder='New paths JSON (optional): ["src/file.py"]', id='revision-paths')
                    yield Button('Preview Work revision', id='revise-work')
                    yield Label('Review scheduling across Works')
                    yield Button('Load current priority and prerequisites', id='load-order')
                    yield Select([(p, p) for p in ('P0', 'P1', 'P2', 'P3')], value='P2', allow_blank=False, id='order-priority')
                    yield Input(placeholder='Prerequisite Work IDs, separated by commas; empty means none', id='order-prerequisites')
                    yield Button('Stage this Work in the review', id='stage-order')
                    yield Input(placeholder='Existing successor Work ID', id='replacement-work')
                    yield Button('Stage replacement of selected Work', id='stage-replacement')
                    yield Static('No scheduling changes staged.', id='order-staged', markup=False)
                    yield Input(placeholder='Why change this work order?', id='order-reason')
                    yield Button('Preview scheduling changes', id='preview-order')
                    yield Button('Confirm scheduling changes', id='confirm-order', disabled=True)
                    yield Button('Clear scheduling draft', id='clear-order')
                with Vertical(id='plan-options'):
                    yield Checkbox('Automatically select planner', False, id='auto-planner')
                    yield Label('Plan reviewer for a new plan')
                    yield Select([('Use auditor settings', ''), ('Require an independent plan reviewer', 'auto')], value='', allow_blank=False, id='plan-reviewer')
                    yield Input(placeholder='Custom plan reviewer profile JSON (optional)', id='plan-reviewer-profile')
                    yield Label('Saved plan directory')
                    yield Input(placeholder='Generated plan path; Review to reopen', id='plan')
                with Vertical(id='shared-findings-options'):
                    yield Label('Reviewed findings (optional)')
                    yield Input(placeholder='Findings manifest', id='findings')
                    yield Input(placeholder='Findings SHA-256', id='findings-sha256')
                with Vertical(id='sequence-options'):
                    yield Label('Prepared Work IDs (dependency order is computed)')
                    yield Input(placeholder='WORK-A, WORK-B', id='sequence-works')
                    yield Checkbox('New sequence: two isolated implementations', id='sequence-parallel')
                    yield Label('Enable only when tasks and checks affect their own workspaces. Incorporation stays serial.')
                    yield Input(placeholder='Saved sequence directory', id='sequence')
                    yield Checkbox('Resume with one slot; keep prepared results', id='sequence-serial')
                    yield Input(placeholder='Optional question before each implementation', id='sequence-wait-question')
                    yield Button('Open active Work', id='sequence-active')
                    yield Input(placeholder='Reason to revise remaining Works', id='sequence-revision-reason')
                    yield Input(placeholder='Revised Work IDs, comma separated; blank keeps current set', id='sequence-revision-works')
                    yield Button('Review sequence changes', id='review-sequence-revision')
                    yield Button('Confirm sequence changes', id='confirm-sequence-revision', disabled=True)
                with Vertical(id='parallel-options'):
                    yield Label('Independent questions: one per line in the task field')
                    yield Input(placeholder='Saved exploration batch', id='batch')
                    yield Label('Concurrent sessions (1 for serial serving)')
                    yield Input('1', id='workers')
                with Vertical(id='synthesis-options'):
                    yield Label('Advisory synthesis: select 2..8 flows, review positions, then Start. Reopen by saved path.')
                    yield Button('Find saved input flows', id='synthesis-list')
                    yield Select([], prompt='Saved input flow', id='synthesis-choice')
                    yield Button('Add selected input', id='synthesis-add')
                    yield TextArea('[]', id='synthesis-inputs')
                    yield Button('Review source positions', id='synthesis-preview')
                    yield Input(placeholder='Reviewed selection SHA-256', id='synthesis-sha256')
                    yield Input(placeholder='Saved synthesis directory (Resume / Inspect)', id='synthesis')
                with Vertical(id='research-options'):
                    yield Label('Source URLs, one per line (these pages only)')
                    yield TextArea(id='research-urls')
                    yield Button('Find saved research', id='research-list')
                    yield Select([], prompt='Saved research in this project', id='saved-research')
                    yield Input(placeholder='Saved research directory', id='research')
                    yield Button('Open collected sources', id='research-review')
                    yield Select([], prompt='Choose a collected page', id='research-ref')
                    yield Input('1', id='research-line', placeholder='First line')
                    yield Button('Read 200 lines', id='research-read')
                with Vertical(id='findings-options'):
                    yield Input(placeholder='Report IDs, comma-separated; blank selects completed reports', id='finding-items')
                    yield Input(placeholder='Review note / disagreements (optional)', id='finding-note')
                    yield Button('Preview findings', id='preview-findings')
                    yield Button('View ADR/SPEC/BDD template', id='view-promotion-template')
                    yield Button('Attach reviewed findings', id='use-findings', disabled=True)
                    yield Button('Use findings and start PLAN', id='use-findings-start', disabled=True)
            with Vertical(id='detail'):
                with Horizontal(id='chat-toolbar'):
                    yield Static('Direct session', id='flow-heading', markup=False)
                    yield Button('Sessions', id='toolbar-conversations', tooltip='Saved conversations · F1')
                    yield Button('Team', id='toolbar-team', tooltip='Who executed each role, with model and reasoning · /team')
                    yield Button('Settings', id='toolbar-settings', tooltip='Project, model and optional flows · F2')
                    yield Button('Files', id='toolbar-files', tooltip='What the agents read and edit · F8')
                with Horizontal(id='connection-row'):
                    yield Static('', id='session-context', markup=False)
                    yield Button('Check', id='connection-check', tooltip='Check the model connection without generation or model switching')
                from dataclasses import replace
                from .execution_scope import current
                scope=replace(current(),coordinator=self.conversation_coordinator)  # new conversations' coordinator
                banner=Static('This TUI only: '+scope.label(),id='execution-scope',markup=False)
                banner.display=scope.active
                yield banner
                with Horizontal(id='status-row'):
                    yield Static('', id='activity-symbol', markup=False)
                    yield Static('Ready · no model is called while browsing.', id='status', markup=False)
                yield Transcript(id='output')
                with Horizontal(id='model-controls'):
                    yield Button('Model ▾', id='choose-model')
                    yield Button('Default ▾', id='choose-reasoning')
                with VerticalScroll(id='composer'):
                    with Vertical(id='outcome-options'):
                        yield Label('Result against the original request')
                        yield TextArea(id='outcome-summary')
                        yield Label('Remaining obligations and limitations')
                        yield TextArea(id='outcome-remaining')
                        yield Label('Earlier failed attempts addressed · number: reason (optional)')
                        yield TextArea(id='outcome-resolutions')
                        yield Select([('Request fulfilled', 'FULFILLED'), ('Partial result · work remains', 'PARTIAL')],
                                     value='FULFILLED', allow_blank=False, id='outcome-disposition')
                        yield Button('Review result and evidence', id='review-outcome-evidence')
                        yield Button('Confirm reviewed result', id='confirm-demand-outcome', disabled=True)
                    yield Label('Message', id='prompt-label')
                    yield HorizontalScroll(id='context-chips')
                    yield HorizontalScroll(id='attachment-chips')
                    yield PromptArea(id='prompt', placeholder='Ask anything about this project…',
                                     highlight_cursor_line=False)
                    with Vertical(id='context-box'):
                        yield OptionList(id='context-menu', markup=False)
                        yield Static('',id='context-hint',markup=False)
                    with Vertical(id='slash-box'):
                        yield OptionList(id='slash-menu', markup=False)
                        yield Static('', id='slash-description', markup=False)
                        yield Static('↑↓ choose · Tab/Enter insert · Esc close',
                                     id='slash-hint', markup=False)
                    yield Static('Resume uses the saved model, scope and limits. Work criteria come from the graph.',
                                 id='hint', markup=False)
                    with Vertical(id='message-options'):
                        yield Label('Message · delivered between complete rounds; queued if stopped')
                        yield TextArea(id='message')
                        yield Button('Send message', id='send-message')
                        yield Static('', id='message-status', markup=False)
                    with Horizontal(id='actions'):
                        yield Button('Attach',id='attach',tooltip='Attach and preview a saved file or image')
                        yield Static('@ files   / commands   Enter send · Shift+Enter or Ctrl+J newline',
                                     id='composer-hint', markup=False)
                        yield Button('Start', variant='primary', id='start')
                        yield Button('Resume', id='resume')
                        yield Button('Review', id='review')
                        yield Button('Cancel', variant='warning', id='cancel', disabled=True)
                    yield Button('Start suggested flow', id='start-suggested', disabled=True)
                    yield Button('Apply reviewed graph proposal', id='apply-plan', disabled=True)
                    yield Button('Replan with correction above', id='replan')
                    yield Button('Confirm Work preparation', id='confirm-preparation', disabled=True)
                    yield Button('Confirm revision and invalidation', id='confirm-revision', disabled=True)
                    with Horizontal(id='work-controls'):
                        yield Button('Pause Work', id='pause-work')
                        yield Button('Inspect controls', id='inspect-work-control')
                    with Horizontal(id='work-note-row'):
                        yield Input(placeholder='Note for the next agent round', id='work-note', max_length=8192)
                        yield Button('Save note', id='save-work-note', disabled=True)
                    yield Static('', id='work-control-status', markup=False)
                    with Horizontal(id='decision-actions'):
                        yield Button('Approve implementation', id='approve-decision', disabled=True)
                        yield Button('Decline implementation', id='decline-decision', disabled=True)
                        yield Button('Reconsider decision', id='reconsider-decision', disabled=True)
            from .files_pane import FilesPane
            yield FilesPane(id='files-pane')
        yield Footer()

    def on_mount(self):
        self.presence_timer = self.set_interval(.12, self.refresh_presence, pause=True)
        try:
            if (root := self.appearance_root()) is not None:
                self.theme = read_appearance(root)
        except (OSError, ValueError) as exc:
            self.notify('Could not load appearance: '+str(exc), severity='warning')
        try:
            if (root := self.appearance_root()) is not None:
                from .files_pane import read_open
                self.files_open = read_open(root)
        except (OSError, ValueError) as exc:
            self.notify('Could not load the files pane preference: '+str(exc), severity='warning')
        self.call_after_refresh(self.sync_files_pane)
        # Branch and model labels only change from outside the TUI here; each refresh runs git.
        self.set_interval(30, lambda: self.sync_header(force=True))
        self.set_interval(60, self.check_storage_space)
        self.call_after_refresh(self.check_storage_space)
        self.set_interval(1800, self.check_product_update)
        self.set_interval(60, self.check_ci_alerts)
        self.set_interval(1.0, self.poll_message_observations)
        self.theme_changed_signal.subscribe(self, self.sync_appearance)
        self.sync_appearance()
        # Permission controls must follow the role value even when Select.Changed
        # is still queued or suppressed while restoring a selection.
        self.watch(self.query_one('#session-role', Select), 'value', self.sync_terminal_permissions, init=False)
        self.load_model_preferences()
        self.query_one('#intake-options').display = False
        self.query_one('#outcome-options').display = False
        self.update_mode()
        self.action_reload()
        self.query_one('#prompt', TextArea).focus()

    def check_ci_alerts(self):
        """A push from this machine left CI red: tell the operator once per alert (relay/ci_watch.py)."""
        from . import ci_watch
        try:
            fresh = ci_watch.fresh_alerts(self.__dict__.setdefault('ci_seen', set()))
        except (OSError, ValueError):
            return
        if fresh:
            self.notify(ci_watch.notice(fresh), severity='error', timeout=60)

    def check_product_update(self):
        """A managed release tells the operator once when auto-update installed a newer one (it never restarts
        the TUI under them: the next launch runs it, and conversations resume)."""
        home, running = os.environ.get('TARE_MANAGED_HOME'), os.environ.get('TARE_RELEASE_ID')
        if not home or not running or getattr(self, 'update_noticed', False):
            return
        try:
            current = json.loads((Path(home)/'active.json').read_text(encoding='utf-8'))['current']
        except (OSError, ValueError, KeyError):
            return
        if current and current != running:
            self.update_noticed = True
            self.notify('A tare.tools update is installed. Restart the TUI to use it; conversations resume.',
                        timeout=30)

    def appearance_root(self):
        if self.storage_preferred is None:
            return None  # Embedded consoles may deliberately use transient settings.
        from .native_installation import workspace_storage
        return workspace_storage(self.workspace_root, self.storage_preferred,
                                 self.storage_fallback)/'console'

    def sync_appearance(self, _theme=None):
        if self.query_one_optional('#appearance'):
            with self.prevent(Select.Changed):
                self.query_one('#appearance', Select).value = (
                    self.theme if self.theme in dict(APPEARANCES) else EMPTY_SELECTION)

    def select_appearance(self, name):
        if name not in dict(APPEARANCES):
            return
        self.theme = name
        self.sync_appearance()
        try:
            if (root := self.appearance_root()) is not None:
                save_appearance(root, name)
        except (OSError, ValueError) as exc:
            self.notify('Theme applied for this window; could not save appearance: '+str(exc),
                        severity='warning')

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 100, 'compact')
        if self.panel:
            self.show_panel(self.panel)
        else:
            self.sync_files_pane()
        self.call_after_refresh(self.sync_header)

    def show_panel(self, panel):
        """Open the 'workspace' (settings, F2) or 'navigation' (conversations, F1) side panel, or None. Display is
        set on the panels: a class on the screen restyled all ~300 widgets, hidden ones too, on every F1 and F2."""
        self.panel = panel
        self.query_one('#selection').display = panel == 'workspace'
        self.query_one('#navigation').display = panel == 'navigation'
        self.sync_files_pane()

    def sync_files_pane(self):
        """The right pane shows while open and there is room: beside the chat on a wide screen, alone in compact
        (F1/F2 cover it while they are open, as they cover the chat), beside F1/F2 from 140 columns. The one place
        that decides whether the chat (#detail) shows: in compact, any open panel takes the whole screen."""
        compact = self.screen.has_class('compact')
        shown = self.files_open and (self.panel is None or (not compact and self.size.width >= 140))
        self.query_one('#detail').display = not (compact and (self.panel or shown))
        pane = self.query_one_optional('#files-pane')
        if pane is not None:
            pane.display = shown
            pane.set_visible_state(shown)

    def action_files(self):
        if self.files_open and not self.query_one('#files-pane').display:
            self.show_panel(None)  # open but covered by F1/F2: F8 brings it forward instead of closing it
        else:
            self.files_open = not self.files_open
            try:
                if (root := self.appearance_root()) is not None:
                    from .files_pane import save_open
                    save_open(root, self.files_open)
            except (OSError, ValueError) as exc:
                self.notify('Could not save the files pane preference: '+str(exc), severity='warning')
            self.sync_files_pane()
        if self.files_open and self.query_one('#files-pane').display:
            self.query_one('#files-touched').focus()
        else:
            self.focus_composer()

    def focus_composer(self):
        prompt = self.query_one('#prompt', TextArea)
        (prompt if prompt.display and not prompt.disabled else
         self.query_one('#cancel' if self.busy else '#start', Button)).focus()

    def resize_composer(self):
        prompt = self.query_one_optional('#prompt')
        chips = self.query_one_optional('#attachment-chips')
        if prompt is None or chips is None:
            return
        completing = any(self.query_one_optional('#'+name) and self.query_one('#'+name).display
                         for name in ('slash-box', 'context-box'))
        # On the one widget it styles: a class on the screen restyled every widget when a menu opened.
        chips.set_class(completing, 'completion-open')
        limit = 1 if completing and (self.screen.has_class('compact') or self.size.height < 30) else 8
        prompt.styles.height = min(limit, max(1, prompt.wrapped_document.height))

    def action_workspace(self):
        self.show_panel(None if self.panel == 'workspace' else 'workspace')
        if self.panel:
            self.query_one('#root', Input).focus()
        else:
            self.focus_composer()

    def action_latest(self):
        self.query_one('#output', Transcript).scroll_end(animate=False)

    def action_processes(self):
        from .terminal_panel import TerminalPanel
        if isinstance(self.screen,TerminalPanel):
            self.screen.action_close()
            return
        custody = self.active.session if self.busy and self.active else self.assistant_session or self.selection().session
        if not custody:
            self.notify('Open a conversation with system commands enabled to view its processes.')
            return
        try:
            service = self.backend.terminals(custody)
        except (ValueError,AttributeError) as exc:
            self.notify(str(exc)); return
        self.push_screen(TerminalPanel(service,title=service.title))

    def action_changes(self):
        from .changes_panel import ChangesPanel
        if isinstance(self.screen,ChangesPanel):
            self.screen.action_close(); return
        if self.busy:
            self.notify('Wait for this turn to stop before reviewing its changes.'); return
        session = self.assistant_session or self.selection().session
        if not session:
            self.notify('Open a saved conversation to review its changes.'); return
        def comment(row):
            self.pop_screen()
            self.open_artifact(row)
        providers = []
        try:
            from .native_continuity import chain
            from .native_agent import read_json
            for i, path in enumerate(chain(session), 1):
                name = read_json(path/'native-agent.json', 1024*1024)['deployment']['native_profile']['model']
                providers.append((f'Provider {i}: {name}', str(path)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify('Previous provider records unavailable: '+str(exc), severity='warning')
        self.push_screen(ChangesPanel(session,self.workspace_root, commenter=comment, provider_sessions=providers))

    def check_action(self, action, parameters):
        if action in {'focus_next', 'focus_previous'} and not getattr(self.screen, 'keyboard', False):
            # Tab is app.focus_next: the panel guards below keep app shortcuts out, not keyboard navigation
            # (with Tab blocked, /permissions could not reach its own controls).
            return super().check_action(action, parameters)
        if self.is_running and isinstance(self.screen, WorkbenchPanel):
            return action in {'quit'}
        from .model_picker import ModelPicker
        if self.is_running and isinstance(self.screen, ModelPicker):
            return action in {'quit'}
        from .changes_panel import ChangesPanel
        from .console_navigation import RenameDialog
        from .project_picker import ProjectPicker
        from .permissions_panel import PermissionsPanel
        from .project_graph_panel import ProjectGraphPanel
        if self.is_running and isinstance(self.screen, (PermissionsPanel, ProjectGraphPanel)):
            return action in {'quit'}
        if self.is_running and isinstance(self.screen, ProjectPicker):
            return action in {'projects', 'quit'}
        if self.is_running and isinstance(self.screen, RenameDialog):
            return action in {'quit'}
        if self.is_running and isinstance(self.screen, AttachmentPicker):
            return action in {'cancel', 'quit'}
        if self.is_running and isinstance(self.screen,ChangesPanel) and action not in {'changes','quit'}:
            return False
        if self.is_running and getattr(self.screen,'keyboard',False) and action != 'processes':
            return False  # In direct mode only the panel's escape bindings win.
        if self.is_running and self.panel == 'navigation' and action == 'start':
            if (self.screen.has_class('compact') or self.focused is None
                    or self.query_one('#detail') not in self.focused.ancestors):
                return False
        if (self.is_running and self.screen.has_class('assistant', 'compact') and self.panel == 'workspace'
                and action == 'start'):
            return False
        from textual.screen import ModalScreen
        if self.is_running and action == 'files' and isinstance(self.screen, ModalScreen):
            return False  # F8 toggles the pane of the main screen only
        if action == 'cancel':
            # A busy composer must not consume Cancel as TextArea's cut. Idle
            # and direct terminal/modal bindings retain their normal handling.
            return self.busy
        return super().check_action(action,parameters)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand('Artifacts and feedback', 'Review captured responses and draft linked comments', self.action_artifacts)
        yield SystemCommand('Tasks and agents', 'Inspect retained activity and cancel the current execution', self.action_tasks)
        yield SystemCommand('Execution team', 'See recorded roles, models, reasoning and connections', self.action_team)
        yield SystemCommand('Conversation usage', 'Inspect recorded model calls, tokens, costs and limits', self.action_usage)
        yield SystemCommand('Compare delivery cost', 'Compare a study and save a scoped preference without model calls', self.action_delivery)
        yield SystemCommand('Challenger mode', 'Configure automatic experiments and resource limits', self.action_challenger)
        yield SystemCommand('Storage and cleanup', 'Inspect disk usage and preview safe cleanup', self.action_storage)
        yield SystemCommand('Planning and exploration', 'Choose delegated models and shared task limits', self.action_delegation)
        yield SystemCommand('Project skills', 'Preview a project skill and add it to your draft', self.action_skills)
        yield SystemCommand('Available tools', 'Inspect effective conversation tools and project instructions', self.action_tools)
        yield SystemCommand('Conversations', 'Search and reopen saved conversations · F1', self.action_conversations)
        yield SystemCommand('Files pane', 'What the agents read and edit, this turn or this session · F8', self.action_files)
        yield SystemCommand('Switch project', 'Browse folders and recent projects · F5', self.action_projects)
        yield SystemCommand('Project permissions', 'Inspect saved scope, instructions and defaults for new conversations', self.action_permissions)
        yield SystemCommand('Project backlog', 'Bind and inspect the selected project’s backlog graph', self.action_backlog)
        yield SystemCommand('Rename conversation', 'Rename the current saved conversation', self.action_rename_conversation)
        yield SystemCommand('Project and model', 'Show or hide settings', self.action_workspace)
        yield SystemCommand('Choose model', 'Model and connection for new conversations', self.action_model)
        yield SystemCommand('Reasoning', 'Reasoning level for new conversations', self.action_reasoning)
        if self.query_one('#mode', Select).value == 'assistant':
            yield SystemCommand('Check model connection', 'Read-only check of the saved or selected model endpoint', self.action_check_connection)
        yield SystemCommand('Latest messages', 'Scroll to the latest messages', self.action_latest)
        yield SystemCommand('Processes and terminal', 'Output, keyboard, resize and stop · F4', self.action_processes)
        yield SystemCommand('Review changes', 'File diffs and recorded checks by turn · F3', self.action_changes)
        if self.assistant_session and not self.busy and not self.sending_message:
            yield SystemCommand('Resume conversation', 'Continue queued work; keep the draft. An interrupted remote response may still finish.', self.action_resume_conversation)
        yield SystemCommand('Attach a file', 'Preview a document or image snapshot', self.action_attach)
        for name, label in APPEARANCES:
            yield SystemCommand('Appearance: '+label, 'Choose and save the console appearance',
                                lambda name=name: self.select_appearance(name))
        if not self.busy and not self.sending_message:
            yield SystemCommand('New conversation', 'Save this draft and start another conversation', self.new_conversation)
        if self.busy:
            yield SystemCommand('Cancel execution', 'Cancel and wait for the agent to stop', self.action_cancel)

    def action_next_step(self):
        self.continue_demand()

    def action_start(self):
        from .console_navigation import RenameDialog
        if isinstance(self.screen, RenameDialog):
            return
        self.query_one('#start', Button).press()

    def update_mode(self):
        mode = self.query_one('#mode', Select).value
        self.screen.set_class(mode == 'assistant', 'assistant')
        names = {'assistant': 'Conversation', 'conversation': 'Choose a flow', 'session': 'Direct session', 'plan': 'Plan and review',
                 'work': 'Work execution', 'sequence': 'Work sequence', 'parallel': 'Explore questions',
                 'research': 'Research sources', 'synthesis': 'Research synthesis'}
        heading = names.get(mode, 'Work console')
        if mode == 'assistant':
            self.sync_header()
        else:
            self.cancel_connection_check()
            title = self.query_one('#flow-heading', Static)
            show(title, plain(heading+' · '+Path(self.workspace_root).name))  # shared with the header's show()
            title.tooltip = self.workspace_root
        self.query_one('#toolbar-settings', Button).label = 'Settings' if mode == 'assistant' else 'Setup'
        self.query_one('#settings-title', Static).update('Settings' if mode == 'assistant' else 'Flow setup')
        self.query_one('#flow-progress').display = mode != 'assistant'
        if self.query_one_optional('#connection-section'):
            self.query_one('#connection-section').display = mode == 'assistant'
        if self.query_one_optional('#connection-details'):
            self.query_one('#connection-details').display = mode == 'assistant'
        session = mode == 'session' or mode == 'assistant' and self.assistant_session is not None
        conversation = mode in {'assistant', 'conversation'}
        for name in ('continue-demand', 'guide-next', 'review-demand-sequence', 'run-demand-sequence', 'review-demand-outcome'):
            self.query_one('#'+name).display = mode != 'assistant'
        self.query_one('#start', Button).label = ('Queue' if mode == 'assistant' and self.busy else
                                                 'Send' if conversation else 'Start')
        self.query_one('#cancel', Button).label = 'Cancel'
        self.query_one('#cancel').display = mode != 'assistant' or self.busy
        self.query_one('#start-suggested').display = conversation and (mode != 'assistant' or self.entry_proposal is not None)
        self.query_one('#start-suggested', Button).disabled = self.busy or not self.entry_proposal
        from .console_entry import commands_for
        self.query_one('#hint', Static).update(
            'Enter sends · Shift+Enter or Ctrl+J newline · F2 project and model · Ctrl+End latest' if mode == 'assistant' else
            'Describe your demand, or type / for '+', '.join(name for name, _, _ in commands_for(mode))+'. '
            'Free text makes one interpretation call; execution follows your reviewed choice.' if conversation else
            'Resume uses the saved model, scope and limits. Work criteria come from the graph.')
        self.query_one('#auto-execution').display = mode in {'assistant', 'session', 'work', 'sequence', 'parallel', 'research'}
        self.query_one('#session-options').display = mode == 'session'
        self.query_one('#direct-check-options').display = mode in {'assistant','session'}
        self.query_one('#work-options').display = mode == 'work'
        self.query_one('#plan-options').display = mode == 'plan'
        self.query_one('#shared-findings-options').display = mode in {'plan', 'session', 'work', 'sequence'}
        self.query_one('#sequence-options').display = mode == 'sequence'
        self.query_one('#parallel-options').display = mode == 'parallel'
        self.query_one('#synthesis-options').display = mode == 'synthesis'
        self.query_one('#research-options').display = mode == 'research'
        self.query_one('#findings-options').display = mode in {'research', 'parallel'}
        self.query_one('#use-findings', Button).disabled = self.busy or self.reviewed_findings is None
        self.query_one('#use-findings-start', Button).disabled = self.busy or self.reviewed_findings is None
        self.query_one('#apply-plan').display = mode == 'plan'
        self.query_one('#replan').display = mode == 'plan'
        self.query_one('#apply-plan', Button).disabled = self.busy or not self.reviewed_plan
        self.query_one('#confirm-preparation').display = mode == 'work'
        self.query_one('#confirm-preparation', Button).disabled = self.busy or not self.reviewed_preparation
        self.query_one('#confirm-revision').display = mode == 'work'
        self.query_one('#confirm-revision', Button).disabled = self.busy or not self.reviewed_revision
        self.query_one('#decision-actions').display = mode == 'work' and bool(self.reviewed_decision)
        decision_status = self.reviewed_decision[1]['status'] if self.reviewed_decision else None
        for name in ('approve-decision', 'decline-decision'):
            self.query_one('#'+name, Button).disabled = self.busy or decision_status != 'WAITING_FOR_HUMAN'
            self.query_one('#'+name, Button).display = decision_status == 'WAITING_FOR_HUMAN'
        self.query_one('#reconsider-decision', Button).disabled = self.busy or decision_status != 'HUMAN_DECLINED'
        self.query_one('#reconsider-decision', Button).display = decision_status == 'HUMAN_DECLINED'
        control_pending = getattr(self, 'work_control_pending', False)
        control_selection = self.active if self.busy else self.selection()
        work_selected = control_selection is not None and control_selection.mode == 'work' and bool(control_selection.work)
        for name in ('work-controls', 'work-control-status'):
            self.query_one('#'+name).display = mode == 'work'
        for name in ('pause-work', 'inspect-work-control'):
            self.query_one('#'+name, Button).disabled = control_pending or not work_selected
        self.query_one('#pause-work', Button).disabled |= not self.busy
        reviewed = getattr(self, 'reviewed_work_control', None)
        paused = (not self.busy and reviewed is not None and reviewed[0] == self.selection()
                  and reviewed[1]['status'] == 'PAUSED')
        can_receive = paused and bool((reviewed[1].get('record') or {}).get('receiver'))
        self.query_one('#work-note-row').display = mode == 'work' and can_receive
        self.query_one('#work-note', Input).disabled = not can_receive or control_pending
        self.query_one('#save-work-note', Button).disabled = not can_receive or control_pending
        self.query_one('#confirm-order', Button).disabled = self.busy or self.reviewed_order is None
        self.query_one('#confirm-sequence-revision', Button).disabled = self.busy or self.reviewed_sequence_revision is None
        self.query_one('#confirm-demand-outcome', Button).disabled = self.busy or self.reviewed_outcome is None
        self.query_one('#resume').display = mode != 'assistant' or bool(self.assistant_session) and not self.busy
        self.query_one('#resume', Button).disabled = self.busy or self.sending_message or (conversation and mode != 'assistant')
        self.query_one('#review', Button).disabled = self.busy or conversation
        self.query_one('#resume', Button).label = 'Retry review' if mode == 'plan' else 'Resume'
        self.query_one('#resume').styles.min_width = 16 if mode == 'plan' else 10
        self.query_one('#prompt').display = mode not in {'work', 'sequence'}
        self.query_one('#prompt', TextArea).disabled = (self.busy and mode != 'assistant') or mode in {'work', 'sequence'}
        if mode == 'assistant':
            self.query_one('#start', Button).disabled = self.sending_message
        self.query_one('#message-options').display = session and mode != 'assistant'
        self.query_one('#message', TextArea).disabled = not session
        self.query_one('#attach').display = mode in {'assistant','session'}
        self.query_one('#attach',Button).disabled = (not self.attachment_support or self.sending_message
                                                    or self.busy and mode!='assistant')
        for button in self.query('#attachment-chips Button, #context-chips Button'):
            button.disabled = self.busy and mode!='assistant'
        self.query_one('#send-message', Button).disabled = not session or self.sending_message
        for name in ('toolbar-conversations', 'toolbar-settings', 'toolbar-files', 'settings-close', 'conversation-close', 'conversation-search', 'conversation-list', 'appearance', 'connection-check', 'conversation-rename'):
            self.query_one('#'+name).disabled = False
        self.query_one('#conversation-new').disabled = self.busy or self.sending_message
        self.query_one('#conversation-rename').disabled = self.busy or self.sending_message
        self.sync_budget_fields()
        self.sync_slash()
        self.refresh_presence()

    def sync_budget_fields(self):
        unlimited = self.query_one_optional('#unlimited', Checkbox)
        if unlimited is None:
            return  # a toggle's message can arrive while the screen is closing
        from .execution_scope import current
        resources = current().unlimited
        unlimited.disabled = self.busy or bool(resources)
        for name, resource in (('max-model-calls', 'calls'), ('max-seconds', 'time')):
            self.query_one('#'+name, Input).disabled = self.busy or (resource in resources if resources else unlimited.value)

    def slash_state(self):
        """Return (token, matching commands) for the leading /token under the cursor, else (None, ())."""
        from .console_entry import commands_for
        prompt = self.query_one('#prompt', TextArea)
        token = re.match(r'/\S*', prompt.text.partition('\n')[0])
        if (token is None or self.busy or prompt.disabled or prompt.text == self.slash_dismissed
                or self.focused not in (prompt, self.query_one('#slash-menu'))
                or self.query_one('#mode', Select).value not in {'assistant', 'conversation'}):
            return None, ()
        (start_row, start), (end_row, end) = prompt.selection
        if start_row or end_row or start > token.end() or not 0 < end <= token.end():
            return None, ()  # Cursor/selection must edit only the command token.
        return token.group(), tuple(c for c in commands_for(self.query_one('#mode', Select).value)
                                   if c[0].startswith(token.group().lower()))

    def sync_slash(self):
        # Derived from live widget state on every trigger, so queued/stale messages cannot reopen it.
        self.sync_context()
        if not self.query_one_optional('#slash-box'):
            return
        if self.query_one('#prompt', TextArea).text != self.slash_dismissed:
            self.slash_dismissed = None
        box, menu = self.query_one('#slash-box'), self.query_one('#slash-menu', OptionList)
        matches = self.slash_state()[1]
        if not matches:
            box.display = False
            self.resize_composer()
            self.update_composer_hint()
            return
        if matches != self.slash_shown or not box.display:
            self.slash_shown = matches
            from .console_entry import COMMAND_SHORTCUTS, FLOWS
            rows = []
            for name, usage, _ in matches:
                row = Text(name, style='bold', no_wrap=True, overflow='ellipsis')
                row.append(' '+usage if usage else '', style='not bold')
                tag = COMMAND_SHORTCUTS.get(name, 'Flow' if name[1:] in FLOWS or name == '/work' else '')
                if tag:
                    row.append(' '*max(2, 30-row.cell_len)+tag, style='not bold')
                rows.append(row)
            menu.set_options(rows)
            menu.highlighted = 0
        if not box.display:
            box.display = True
        self.update_slash_description()
        self.resize_composer()
        self.update_composer_hint()

    def update_composer_hint(self):
        if not self.query_one_optional('#composer-hint'):
            return
        mode = self.query_one('#mode', Select).value
        if mode not in {'assistant', 'conversation'}:
            show(self.query_one('#composer-hint', Static), 'Ctrl+X cancel' if self.busy else 'Enter start',
                 layout=False)
            return
        draft = self.query_one('#prompt', TextArea).text
        draft_bytes = len(draft.encode('utf-8'))
        draft_lines = draft.count('\n') + 1
        command = draft.lstrip().startswith('/')
        verb = 'queue' if self.busy and mode == 'assistant' else 'run command' if command else 'send'
        menu_open = self.query_one('#slash-box').display or self.query_one('#context-box').display
        shortcut = ('Enter complete' if menu_open else 'Enter '+verb)+' · Shift+Enter / Ctrl+J newline'
        hints = '@ files  / commands  ' if mode == 'assistant' else '/ commands  '
        if draft_bytes > 16384:
            value = f'Over 16 KiB  {draft_bytes} B  {draft_lines} lines'
        elif draft_bytes >= 1024 or draft_lines > 8:
            value = f'{draft_bytes} B · {draft_lines} lines  {shortcut}'
        else:
            value = shortcut if menu_open else hints+shortcut
        show(self.query_one('#composer-hint', Static), value, layout=False)  # one fixed line

    def update_slash_description(self):
        if not self.slash_shown:
            return
        index = self.query_one('#slash-menu', OptionList).highlighted
        if index is None or not 0 <= index < len(self.slash_shown):
            return
        name, usage, description = self.slash_shown[index]
        from .console_entry import FLOWS
        prefix = 'Optional flow · ' if name[1:] in FLOWS or name == '/work' else ''
        self.query_one('#slash-description', Static).update(prefix+description)

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == 'slash-menu':
            self.update_slash_description()

    def slash_key(self, key):
        """Handle a menu key; False leaves it to ordinary editing and bindings."""
        box, menu = self.query_one('#slash-box'), self.query_one('#slash-menu', OptionList)
        if not box.display:
            return False
        self.sync_slash()
        if not box.display:
            return False
        if key == 'escape':
            self.slash_dismissed = self.query_one('#prompt', TextArea).text
            box.display = False
            self.resize_composer()
            self.update_composer_hint()
        elif key in {'up', 'down'}:
            getattr(menu, 'action_cursor_'+key)()
        else:
            self.accept_slash(menu.highlighted or 0)
        return True

    def accept_slash(self, index):
        """Replace only the command token; never sends, starts or changes mode."""
        token, matches = self.slash_state()
        if not matches:
            self.sync_slash()
            return
        name, usage, _ = matches[min(index, len(matches)-1)]
        prompt = self.query_one('#prompt', TextArea)
        after = prompt.text.partition('\n')[0][len(token):]
        prompt.replace(name+(' ' if usage and not after[:1].isspace() else ''), (0, 0), (0, len(token)))
        prompt.move_cursor((0, len(name)+bool(usage)))
        self.slash_dismissed = prompt.text  # Reopens only after the operator edits again.
        self.sync_slash()

    def on_option_list_option_selected(self, event):
        if event.option_list.id == 'conversation-list':
            event.stop()
            self.open_conversation(event.option_index)
            return
        if event.option_list.id == 'context-menu':
            event.stop()
            self.accept_context(event.option_index)
            return
        if event.option_list.id == 'slash-menu':
            event.stop()
            self.accept_slash(event.option_index)
            self.query_one('#prompt', TextArea).focus()

    def on_text_area_changed(self, event):
        if event.text_area.id == 'prompt' and self.is_mounted and self.query_one_optional('#mode'):
            self.resize_composer()
            self.sync_slash()
            self.update_composer_hint()

    def on_text_area_selection_changed(self, event):
        if event.text_area.id == 'prompt':
            self.sync_slash()

    def on_descendant_focus(self, event):
        self.sync_slash()

    def on_descendant_blur(self, event):
        self.sync_slash()

    @staticmethod
    def session_options(rows):
        return [(r['session_id'][:6]+' · '+r['task'][:24], r['session']) for r in rows]

    def workspace_field(self):
        # An emptied field means the open project. Windows refuses an empty working directory (WinError 123).
        return self.query_one('#root', Input).value.strip() or self.workspace_root

    def selection(self):
        def value(name):
            v = self.query_one('#'+name, Select).value
            return '' if v is EMPTY_SELECTION else str(v)
        def field(name):
            return self.query_one('#'+name, Input).value.strip()
        return Selection(root=self.workspace_field(), mode=value('mode'), role=value('session-role'), deployment=value('model'),
            execution_policy={'coordinator': self.conversation_coordinator},
            resume_coordinator_seconds=self.resume_coordinator_seconds,
            resume_coordinator_calls=self.resume_coordinator_calls,
            findings=field('findings'), findings_sha256=field('findings-sha256'),
            finding_items=field('finding-items'), finding_note=field('finding-note'),
            synthesis=field('synthesis'), synthesis_sha256=field('synthesis-sha256'),
            synthesis_inputs=self.query_one('#synthesis-inputs', TextArea).text,
            research=field('research'), urls=self.query_one('#research-urls', TextArea).text, auditor=value('auditor'),
            batch=field('batch'), workers=field('workers'),
            sequence=field('sequence'), sequence_works=field('sequence-works'),
            sequence_parallel=self.query_one('#sequence-parallel', Checkbox).value,
            sequence_serial=self.query_one('#sequence-serial', Checkbox).value,
            sequence_revision_reason=field('sequence-revision-reason'),
            sequence_revision_works=field('sequence-revision-works'),
            wait_before_implement=field('sequence-wait-question') if value('mode') == 'sequence' else field('wait-question'),
            profile=field('profile'), session=value('session'), work=field('work-id') or value('work'),
            prompt=self.query_one('#prompt', TextArea).text, verifier=field('verifier'),
            unlimited=self.query_one('#unlimited', Checkbox).value,
            max_model_calls=field('max-model-calls'), max_seconds=field('max-seconds'),
            refresh=self.query_one('#refresh', Checkbox).value,
            retry_plan=self.query_one('#retry-plan', Checkbox).value, plan=field('plan'),
            retry_audit=self.query_one('#retry-audit', Checkbox).value,
            auto_execution=self.query_one('#auto-execution', Checkbox).value,
            auto_planner=self.query_one('#auto-planner', Checkbox).value, plan_reviewer=value('plan-reviewer'), plan_reviewer_profile=field('plan-reviewer-profile'),
            source_ref=value('source-ref'), criterion_ids=field('criterion-ids'), work_verifier=field('work-verifier'),
            revision_reason=field('revision-reason'), revision_paths=field('revision-paths'),
            intake_criteria=self.query_one('#intake-criteria', TextArea).text,
            intake_argv=self.query_one('#intake-argv', TextArea).text,
            outcome_summary=self.query_one('#outcome-summary', TextArea).text,
            outcome_remaining=self.query_one('#outcome-remaining', TextArea).text,
            outcome_resolutions=self.query_one('#outcome-resolutions', TextArea).text,
            outcome_disposition=value('outcome-disposition'),
            guide_next=self.query_one('#guide-next', Checkbox).value,
            specgraph_context=self.query_one('#specgraph-context', Checkbox).value,
            streaming=self.query_one('#streaming', Checkbox).value,
            local_models=self.query_one('#local-models', Checkbox).value,
            terminal=self.query_one('#terminal', Checkbox).value,
            command_protection=dict(self.command_protection),
            project_admission=dict(self.project_admission),
            write_paths=list(self.permission_write_paths),
            graph_path=self.project_graph_path,
            folder_grants=self.folder_grants(field('root')), folder_note=self.folder_note,
            context_root=self.context_root, context_paths=list(self.context_paths),
            attachments=[dict(row) for row in self.attachments], attachment_root=self.attachment_root,
            attachment_project=self.attachment_project, attachments_enabled=self.attachment_support)

    async def on_unmount(self):
        self.connection_closing = True
        self.stop_heads_up()
        self.restore_terminal_title()
        self.cancel_connection_check()
        task = self.connection_task
        if task is not None:
            await asyncio.shield(task)
            if self.connection_task is task:
                self.connection_task = self.connection_cancellation = None
                self.connection_loading = False
        close = getattr(self.backend,'close',None)
        if close is not None:
            await asyncio.to_thread(close)

    @staticmethod
    def stop_heads_up():
        try:  # a heads-up writer in flight would keep the process alive after Quit; it never blocks quitting
            from . import heads_up
            heads_up.stop()
        except Exception:
            pass

    def say(self, value, *, literal=False):
        value = plain(value)
        if len(value) > 200000:
            value = value[:200000]+'\n[View truncated; full evidence remains in native custody.]'
        transcript = self.query_one('#output', Transcript)
        # Owner reports carry copyable custody/commands, not Markdown prose.
        if literal:
            transcript.append(SelectableMessage(value, markup=False, classes='message owner-report'))
        else:
            transcript.write(value)

    def set_status(self, value):
        show(self.query_one('#status', Static), plain(value))
        self.refresh_presence()

    def show_plan(self, result):
        from .native_plan import render, render_summary
        self.query_one('#output', Transcript).append(SelectableMessage(plain(render_summary(result)),
            markup=False, classes='message plan-review'))
        self.query_one('#output', Transcript).activity('Plan evidence', render(result))

    def controls(self, busy):
        self.busy = busy
        if busy:
            self.reset_presence()
        else:
            self.presence_cancelled = self.cancel_requested
        assistant = self.query_one('#mode', Select).value == 'assistant'
        live_controls = {'prompt', 'start', 'attach'} if assistant else set()
        if assistant and self.focused is not None and self.focused.id in {'resume', 'cancel'}:
            self.query_one('#prompt', TextArea).focus()
        for widget in self.query('Input, Select, Checkbox, TextArea, Button'):
            if widget.id in live_controls:
                continue  # update_mode owns these; avoid a transient disable that moves focus.
            widget.disabled = busy and not (widget.has_class('copy-result-details') or widget.has_class('copy-tool-output'))
        self.query_one('#cancel', Button).disabled = not busy
        self.update_mode()
        self.sync_terminal_permissions()

    @work(exclusive=True, group='catalog')
    async def action_reload(self):
        if self.busy:
            return
        try:
            root = self.prepare_workspace()
            catalog = await asyncio.to_thread(self.backend.catalog, root)
            if self.busy or Path(self.selection().root).resolve() != Path(root):
                return
            if self.project_graph_path:
                from tare_ops import read_graph
                catalog = {**catalog, 'works': read_graph(self.project_graph_path)['nodes']}
            model = self.query_one('#model', Select)
            old = model.value
            model_rows = [(row['deployment']+' · '+row['model'], row['deployment']) for row in catalog['models']]
            # The legacy flow selector cannot have zero options. A machine may
            # intentionally have no listed route while a direct conversation
            # uses its explicitly selected profile; keep the rest of the catalog.
            model.set_options(model_rows or [('Choose a model connection', '')])
            available = [value for _, value in model_rows]
            preferred = default_deployment()
            model.value = (old if old in available else preferred if preferred in available
                           else available[0] if available else '')
            auditor = self.query_one('#auditor', Select)
            prior_auditor = auditor.value
            auditor.set_options([('Use existing audit policy', ''), ('Auto · independent native auditor', 'auto')]+[(r['deployment']+' · '+r['model'], r['deployment']) for r in catalog['models']])
            auditor.value = prior_auditor if prior_auditor in ['', 'auto', *[r['deployment'] for r in catalog['models']]] else ''
            reviewer = self.query_one('#plan-reviewer', Select)
            prior_reviewer = reviewer.value
            reviewer.set_options([('Use auditor settings', ''), ('Require an independent plan reviewer', 'auto')]+[(r['deployment']+' · '+r['model'], r['deployment']) for r in catalog['models']])
            reviewer.value = prior_reviewer if prior_reviewer in ['', 'auto', *[r['deployment'] for r in catalog['models']]] else ''
            sessions = self.query_one('#session', Select)
            previous = sessions.value
            sessions.set_options(self.session_options(catalog['sessions']))
            if previous in [r['session'] for r in catalog['sessions']]:
                sessions.value = previous
            self.query_one('#work', Select).set_options([
                (r['id'][:18]+' · '+r.get('title', '')[:14], r['id']) for r in catalog['works']])
            for error in catalog['errors']:
                self.say('Catalog warning: '+error)
            self.set_status(("Ready"
                             if self.query_one('#mode', Select).value == 'assistant' else
                             f"{len(catalog['sessions'])} saved sessions · {len(catalog['works'])} graph Works · model availability not probed"))
            from .environment import decided, replacements
            # The interactive entry only (like CLI discovery below): tests and fixture journeys stay quiet.
            interactive = self.discover_clis and os.environ.get('TARE_CLI_DISCOVERY') != '0'
            if interactive and not decided():  # the first runs guide the owner to choose what tare uses here
                # A toast: a long status line wraps and moves the transcript while it lays out.
                self.notify('This machine is not set up yet: /setup chooses the local models, CLIs and API '
                            'providers tare uses here; until then it uses every route it finds.')
            newer = replacements() if interactive else []
            if newer:  # a vendor replaced a model a route the owner named runs: ask, never switch silently
                self.notify('; '.join(f"{r['newer']} replaces {r['model']}, which {r['where']} uses"
                                      for r in newer) + '. /setup moves it to the newer model or keeps it.')
            # After the first catalog, so its status line does not hide the discovery result.
            # Only the interactive entry probes vendor CLIs; tests and fixture journeys must not spend quota.
            if self.discover_clis and os.environ.get('TARE_CLI_DISCOVERY') != '0':
                self.discover_clis = False
                self.run_cli_discovery()
        except Exception as exc:
            self.set_status('Catalog unavailable: '+str(exc))

    def on_select_changed(self, event):
        if not self.is_mounted or not self.query_one_optional('#mode'):
            return  # Queued events may arrive after the screen was detached.
        if event.select.id == 'appearance' and event.value is not EMPTY_SELECTION:
            if event.value != event.select.value:
                return  # Ignore a queued selection superseded by theme restoration.
            if str(event.value) != self.theme:
                self.select_appearance(str(event.value))
            return
        if event.select.id == 'mode' or (event.select.id == 'session' and self.selection().mode == 'session'):
            self.invalidate_message_observations()
        if event.select.id in {'mode', 'model'}:
            self.update_mode()
        if event.select.id == 'work' and event.value is not EMPTY_SELECTION and event.value == event.select.value:
            # A queued selection the list no longer holds (a workspace change cleared it) must not come back.
            self.query_one('#work-id', Input).value = str(event.value)
        if event.select.id == 'source-ref' and event.value is not EMPTY_SELECTION and not self.busy:
            self.show_source()
        if event.select.id == 'saved-research' and event.value is not EMPTY_SELECTION and not self.busy:
            self.query_one('#research', Input).value = str(event.value)
            self.review_selection()
        if event.select.id in {'work', 'session'} and event.value is not EMPTY_SELECTION and not self.busy:
            if event.select.id == 'session':
                self.navigation_generation += 1
                self.invalidate_message_observations()
                self.run_worker(self.refresh_messages(), group='message-refresh')
            self.inspect_selection()

    def on_checkbox_changed(self, event):
        if event.checkbox.id == 'auto-execution':
            self.sync_header()
        if event.checkbox.id == 'unlimited':
            self.sync_budget_fields()
        if event.checkbox.id == 'avoid-local-gpu':
            self.leave_local_gpu(event.value)
        if event.checkbox.id == 'heads-up':
            from .heads_up import set_enabled
            try:
                set_enabled(event.value)
            except OSError as exc:
                self.notify('Could not save the heads-up choice: '+str(exc), severity='error')
        if event.checkbox.id == 'local-models' and not event.value:
            self.off_local_model()

    def leave_local_gpu(self, on):
        """Saved for the machine (turn_engine.leave_local_gpu): this TUI, what it starts and tare serve skip the
        shared local GPU while checked."""
        from . import turn_engine
        try:
            turn_engine.leave_local_gpu(on)
        except OSError as exc:
            self.notify('Could not save the local GPU choice: '+str(exc), severity='error')
            return
        if not on:
            os.environ.pop(turn_engine.AVOID_LOCAL_GPU, None)  # a launch with TARE_AVOID_LOCAL_GPU=1 would keep it
        else:
            self.off_local_model()

    def off_local_model(self):
        """A local model picked for new conversations becomes tare-auto, which then skips local ones."""
        model = self.query_one_optional('#model', Select)
        if model is not None and self.local_model(str(model.value)):
            try:
                model.value = 'tare-auto'
                self.notify('Model set to tare-auto, which now skips the local GPU.')
            except Exception:
                self.notify('The selected model runs on the local GPU: choose a cloud or CLI model.', severity='warning')

    @staticmethod
    def local_model(deployment):
        """Whether a deployment (a name, or a saved session's dict) runs on this machine's GPU."""
        try:
            from .challenger import local
            from .native_catalog import select
            value = select(deployment) if isinstance(deployment, str) else deployment
            return 'native_auto_routes' not in value and local(value)
        except Exception:
            return False

    def local_gpu_refusal(self, selected, resume):
        """Why this run would use a local model the conversation or the machine ruled out, or None."""
        from .turn_engine import leaves_gpu_to_others
        if leaves_gpu_to_others():
            why, undo = 'this machine leaves the local GPU to others', 'uncheck Leave the local GPU to others'
        elif not selected.local_models:
            why, undo = 'this conversation uses no local models', 'check Local models in this conversation'
        else:
            return None
        session = self.assistant_session if selected.mode == 'assistant' else selected.session
        if resume and session:
            from .native_agent import read_json
            try:
                saved = read_json(Path(session)/'native-agent.json', 1024*1024).get('deployment')
            except (OSError, ValueError):
                saved = None
            if saved and self.local_model(saved):
                return (f'This conversation runs on a local model, but {why}. Continue it on another model with '
                        f'Choose model → Continue task, or {undo}.')
        elif not resume and self.local_model(selected.deployment):
            return f'This model runs on the local GPU, but {why}. Choose tare-auto or a cloud or CLI model, or {undo}.'
        return None

    def on_input_submitted(self, event):
        if event.input.id == 'work-id':
            event.stop()
            self.inspect_selection()

    def on_input_changed(self, event):
        if self.is_mounted and event.input.id == 'root':
            self.invalidate_message_observations()
        if event.input.id == 'conversation-search' and self.is_mounted:
            self.render_conversations()
        if event.input.id == 'profile' and self.is_mounted and self.query_one_optional('#mode'):
            self.update_mode()

    @work(exclusive=True, group='inspect')
    async def inspect_selection(self):
        try:
            selection = self.selection()
            if selection.mode in {'assistant', 'plan', 'conversation'}:
                return  # Planning proposals are opened through explicit Review.
            if (selection.mode == 'session' and not selection.session) or (selection.mode == 'work' and not selection.work):
                return
            self.say(await asyncio.to_thread(self.backend.inspect, selection), literal=True)
            if selection.mode == 'session' and selection.session:
                await self.refresh_messages()
        except Exception as exc:
            self.say('Inspection unavailable: '+str(exc))

    def on_button_pressed(self, event):
        if event.button.id in {'choose-model', 'choose-reasoning', 'settings-model'}:
            event.stop()
            self.open_model_picker(reasoning=event.button.id == 'choose-reasoning')
            return
        if event.button.id == 'connection-check':
            event.stop(); self.action_check_connection(); return
        if event.button.id == 'connection-authenticate':
            from .credential_panel import authenticate
            event.stop(); authenticate(self); return
        if event.button.id=='attach':
            event.stop();self.action_attach();return
        if event.button.id and event.button.id.startswith(('attachment-preview-','attachment-remove-')):
            event.stop();self.attachment_button(event.button);return
        navigation = {'toolbar-conversations': self.action_conversations, 'toolbar-team': self.action_team,
                      'toolbar-settings': self.action_workspace, 'toolbar-files': self.action_files,
                      'navigation-projects': self.action_projects, 'settings-projects': self.action_projects,
                      'settings-permissions': self.action_permissions,
                      'settings-backlog': self.action_backlog,
                      'settings-close': self.action_workspace,
                      'conversation-new': self.new_conversation, 'conversation-rename': self.action_rename_highlighted,
                      'conversation-close': self.close_navigation}
        if self.query_one('#mode', Select).value == 'assistant':
            navigation['new-demand'] = self.new_conversation
        if event.button.id in navigation:
            event.stop()
            navigation[event.button.id]()
            return
        if event.button.id and event.button.id.startswith('context-remove-'):
            event.stop()
            self.remove_context(event.button)
            return
        if event.button.id != 'cancel' and not self.busy:
            try:
                self.prepare_workspace()
            except Exception as exc:
                self.set_status(str(exc)); return
        actions = {'reload': self.action_reload, 'start': lambda: self.execute(False),
                   'list-demands': self.list_demands, 'open-demand': self.open_demand,
                   'save-demand': self.save_demand, 'new-demand': self.new_demand,
                   'reconcile-demand': self.reconcile_demand,
                   'continue-demand': self.continue_demand,
                   'review-demand-sequence': self.review_demand_sequence,
                   'run-demand-sequence': self.run_demand_sequence,
                   'review-demand-outcome': self.review_demand_outcome,
                   'review-outcome-evidence': self.review_demand_outcome,
                   'confirm-demand-outcome': self.confirm_demand_outcome,
                   'stage-replacement': self.stage_replacement, 'load-order': self.load_order, 'stage-order': self.stage_order,
                   'preview-order': self.preview_order, 'confirm-order': self.confirm_order, 'clear-order': self.clear_order,
                   'open-demand-stage': self.open_demand_stage,
                   'start-suggested': self.start_suggested,
                   'send-message': self.send_operator_message,
                   'resume': self.resume_selected, 'review': self.review_selection, 'cancel': self.action_cancel,
                   'apply-plan': self.apply_reviewed_plan, 'load-sources': self.load_sources,
                   'draft-intake': self.draft_intake, 'preview-intake': lambda: self.preview_preparation(intake=True),
                   'replan': lambda: self.execute(False, replan=True),
                   'revise-work': self.preview_revision, 'confirm-revision': self.confirm_revision,
                   'open-decision': self.open_decision,
                   'pause-work': lambda: self.request_work_control('pause'),
                   'inspect-work-control': lambda: self.request_work_control('inspect'),
                   'save-work-note': lambda: self.request_work_control('intervene'),
                   'reconsider-decision': lambda: self.answer_decision('reconsider'),
                   'sequence-active': self.open_sequence_work,
                   'review-sequence-revision': self.review_sequence_revision,
                   'confirm-sequence-revision': self.confirm_sequence_revision,
                   'research-review': self.review_selection, 'research-read': self.read_research_source,
                   'research-list': self.list_research,
                   'synthesis-list': self.list_synthesis_inputs, 'synthesis-add': self.add_synthesis_input,
                   'synthesis-preview': self.preview_synthesis,
                   'preview-findings': self.preview_findings, 'use-findings': self.use_findings,
                   'view-promotion-template': self.show_promotion_template,
                   'use-findings-start': lambda: self.use_findings(start_plan=True),
                   'approve-decision': lambda: self.answer_decision('approve'),
                   'decline-decision': lambda: self.answer_decision('decline'),
                   'prepare-work': self.preview_preparation, 'confirm-preparation': self.confirm_preparation}
        actions[event.button.id]()

    def send_operator_message(self):
        if self.sending_message:
            return
        selection = self.active if self.busy else self.selection()
        composer = 'prompt' if self.selection().mode == 'assistant' else 'message'
        text = self.query_one('#'+composer, TextArea).text
        if (selection is None or selection.mode != 'session' or not selection.session
                or not text.strip() and not (composer=='prompt' and self.attachments)):
            self.query_one('#message-status', Static).update('Select a direct session and enter a message.')
            return
        draft_text=text
        draft_context=(self.context_root,tuple(self.context_paths))
        draft_attachments=json.dumps(self.attachments,sort_keys=True)
        if composer=='prompt':
            try:
                if Path(self.selection().root).resolve()!=Path(selection.root).resolve():
                    raise ValueError('Wait for the current operation before changing workspace.')
                text=context_prompt(self.selection(),text)
                selection=replace(selection,attachments=[dict(row) for row in self.attachments],
                    attachment_root=self.attachment_root,attachment_project=self.attachment_project)
            except (ValueError,OSError) as exc:
                self.set_status(str(exc)); return
        else:
            selection=replace(selection,attachments=[],attachment_root='',attachment_project='')
        key = (selection.session, text, json.dumps(selection.attachments,sort_keys=True))
        if self.message_draft is None or self.message_draft[:-1] != key:
            self.message_draft = (*key, uuid4().hex)
        self.sending_message = True
        self.update_mode()
        self.save_operator_message(selection, text, self.message_draft[-1], composer, draft_text, draft_context, draft_attachments)

    @work(group='operator-message')
    async def save_operator_message(self, selection, text, message_id, composer='message', draft_text=None,
                                    draft_context=None, draft_attachments=None):
        draft_text=text if draft_text is None else draft_text
        scope = self.message_observation_scope()
        if scope and (scope[0] != str(Path(selection.root).resolve()) or scope[2] != str(Path(selection.session).resolve())):
            scope = None
        owner = object()
        self._message_save_owner = owner
        try:
            await asyncio.to_thread(self.backend.send_message, selection, text, message_id)
            if scope is None or scope != self.message_observation_scope():
                return
            output = self.query_one('#output', Transcript)
            if self._message_observed_scope != scope:
                output.clear_message_deliveries()
                self._message_proofs.clear()
            saved_item = {'message_id': message_id, 'text': text,
                          'attachments': deepcopy(selection.attachments),
                          'delivery': {'status': 'SAVED'}}
            self._message_observed_scope = scope
            self._message_unresolved = True
            self._message_proofs[message_id] = saved_item
            self.last_sent_message = (selection, message_id)
            if (self.query_one('#'+composer, TextArea).text == draft_text and
                    (composer!='prompt' or draft_attachments is None or
                     draft_attachments==json.dumps(self.attachments,sort_keys=True)) and
                    (composer!='prompt' or draft_context is None or draft_context==(self.context_root,tuple(self.context_paths)))):
                self.query_one('#'+composer, TextArea).clear()
                if composer=='prompt': self.clear_context()
            if composer == 'prompt':
                paths=draft_context[1] if draft_context else ()
                self.assistant_say('You: '+draft_text+('\nContext: '+', '.join(paths) if paths else '')+
                    ('\nAttachments: '+', '.join(row['name'] for row in selection.attachments) if selection.attachments else ''))
            self.checkpoint('message_saved', {'message_id': message_id, 'text': text, 'session': selection.session,
                            'attachments':selection.attachments},
                            selection=replace(self.selection(), session=selection.session), required=False)
            self.message_draft = None
            output.message_delivery(saved_item)
            await self.refresh_messages()
        except Exception as exc:
            if scope is not None and scope == self.message_observation_scope():
                self.query_one('#message-status', Static).update(plain('Message not confirmed; draft retained: '+str(exc)))
                if composer == 'prompt':
                    self.set_status('Message not confirmed; draft preserved: '+str(exc))
        finally:
            if self._message_save_owner is owner:
                self._message_save_owner = None
                self.sending_message = False
                if scope is not None and scope == self.message_observation_scope():
                    self.update_mode()

    def invalidate_message_observations(self):
        self._message_generation += 1
        self._message_proofs.clear()
        self._message_observed_scope = None
        self._message_next_poll = 0.0
        self._message_unresolved = False
        if self.query_one_optional('#output'):
            self.query_one('#output', Transcript).clear_message_deliveries()
        if self.query_one_optional('#message-status'):
            self.query_one('#message-status', Static).update('')

    def message_observation_scope(self):
        if not self.is_mounted or self.connection_closing:
            return None
        try:
            mode = self.query_one('#mode', Select).value
            root = str(Path(self.workspace_field()).resolve())
            if root != str(Path(self.workspace_root).resolve()) or mode not in {'assistant', 'session'}:
                return None
            session = self.assistant_session if mode == 'assistant' else self.query_one('#session', Select).value
            if not session or session is EMPTY_SELECTION:
                return None
            output = self.query_one('#output', Transcript)
            self.query_one('#message-status', Static)
        except NoMatches:
            return None  # Timers may tick while the screen's controls unmount.
        return (root, str(Path(relay_mesh.RELAY_ROOT).resolve()), str(Path(session).resolve()),
                mode, self._message_generation, output.generation)

    def poll_message_observations(self):
        scope = self.message_observation_scope()
        if scope is not None and (scope != self._message_observed_scope or self.busy
                                 or self.sending_message or self.has_unresolved_messages() or self._message_refresh_pending):
            if time.monotonic() >= self._message_next_poll:
                self.run_worker(self.refresh_messages(force=False), group='message-refresh')

    def has_unresolved_messages(self):
        return self._message_unresolved or self.sending_message

    async def refresh_messages(self, *, force=True):
        if not force and time.monotonic() < self._message_next_poll:
            return
        if self._message_read_in_flight:
            self._message_refresh_pending = True
            return
        self._message_read_in_flight = True
        try:
            # Coalesce callers; a slow filesystem must not create an endless
            # worker or a parallel journal scan from every timer tick.
            for _ in range(2):
                self._message_refresh_pending = False
                await self._do_refresh_messages()
                if not self._message_refresh_pending:
                    break
        finally:
            self._message_read_in_flight = False

    async def _do_refresh_messages(self):
        scope = self.message_observation_scope()
        if scope is None:
            return
        root, _, session, *_ = scope
        selection = Selection(root=root, mode='session', session=session)
        try:
            read = asyncio.create_task(asyncio.to_thread(self.backend.message_observations, selection))
            try:
                items = await asyncio.shield(read)
            except asyncio.CancelledError:
                await read  # Keep the single-reader guard until its thread exits.
                raise
            items = deepcopy(items)
            ranks = {'SAVED': 1, 'RECEIVED': 2, 'RESPONSE_READY': 3}
            if (not isinstance(items, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get('message_id'), str)
                    or item.get('delivery', {}).get('status') not in ranks for item in items)
                    or len({item['message_id'] for item in items}) != len(items)):
                raise ValueError('Invalid message observation.')
            error = None
        except Exception as exc:
            items, error = None, exc
        if scope != self.message_observation_scope():
            return
        output = self.query_one('#output', Transcript)
        if self._message_observed_scope != scope:
            output.clear_message_deliveries()
            self._message_proofs.clear()
        self._message_observed_scope = scope
        self._message_next_poll = time.monotonic() + (5 if error else 1)
        if error is not None:
            for row in output.message_rows.values():
                row.set_error('Delivery observation unavailable; last confirmed state retained.')
            if not output.message_rows:
                output.delivery_notice('Message delivery observation unavailable.')
            self.query_one('#message-status', Static).update('Message delivery observation unavailable.')
            self._message_unresolved = True
            return
        output.delivery_notice('')
        # Show a bounded recent window. Native custody retains every full receipt;
        # repeatedly reading history must never cycle old rows through the DOM.
        visible = items[-50:]
        for index, item in enumerate(visible):
            mid = item['message_id']
            prior = self._message_proofs.get(mid)
            if prior and ranks[prior['delivery']['status']] > ranks[item['delivery']['status']]:
                item = deepcopy(prior)  # Retain the complete proof, not only its label.
            visible[index] = item
            self._message_proofs[mid] = item
            output.message_delivery(item)
        keep = {item['message_id'] for item in visible}
        self._message_proofs = {mid: row for mid, row in self._message_proofs.items() if mid in keep}
        observed = items[:-50] + visible
        self._message_unresolved = any(item['delivery']['status'] != 'RESPONSE_READY' for item in observed)
        self.query_one('#message-status', Static).update(self._format_session_message_status(observed))

    def _format_session_message_status(self, items):
        counts = {state: sum(item['delivery']['status'] == state for item in items)
                  for state in ('SAVED', 'RECEIVED', 'RESPONSE_READY')}
        labels = {'SAVED': 'saved', 'RECEIVED': 'received', 'RESPONSE_READY': 'response ready'}
        return ' · '.join(f'{count} {labels[state]}' for state, count in counts.items() if count)

    async def refresh_message_status(self):
        await self.refresh_messages()

    def consent_decided(self, choice, custody, identity, question_id, workspace):
        """Persist first; defer recovery until the current worker really unlocks."""
        if choice is None or not self.consent_resume_matches(custody, workspace):
            return
        from .consent_decisions import record
        try:
            receipt = record(custody, identity, question_id, choice=choice)
            self.say(receipt.get('notice') or 'Consent decision saved.')
        except (ValueError, OSError) as exc:
            self.say(str(exc))
            return
        self._consent_resume = (custody, workspace)
        self.call_later(self.drain_consent_resume)

    def consent_resume_matches(self, custody, workspace):
        selected = self.selection()
        return (selected.mode == 'assistant' and str(self.assistant_session) == str(custody)
                and Path(selected.root).resolve() == Path(workspace).resolve()
                and Path(self.workspace_root).resolve() == Path(workspace).resolve())

    def drain_consent_resume(self):
        pending = getattr(self, '_consent_resume', None)
        if pending is None:
            return
        if not self.consent_resume_matches(*pending):
            self._consent_resume = None
            return
        if self.busy or self.sending_message:
            self.set_timer(.05, self.drain_consent_resume)
            return
        self._consent_resume = None
        self.action_resume_conversation()

    def action_resume_conversation(self):
        if self.busy or self.sending_message:
            self.set_status('Wait for the current operation before resuming.'); return
        if self.selection().mode != 'assistant' or not self.assistant_session:
            self.set_status('Open a saved conversation before using Resume.'); return
        if Path(self.selection().root).resolve() != Path(self.workspace_root).resolve():
            self.set_status('Apply the workspace change before opening a saved conversation.'); return
        self.execute(True, assistant=True, resume_only=True)

    def execute(self, resume, *, replan=False, expected_sequence=None, assistant=False, resume_only=False, continuation=None,
                recheck_candidate=False,retry_delivery=False,repair_plan=False):
        selected=self.selection()
        refusal = None if continuation else self.local_gpu_refusal(selected, resume)
        if refusal:
            self.set_status(refusal); return
        parts = selected.prompt.strip().split(maxsplit=1)
        head, path = (parts[0].lower(), parts[1] if len(parts)>1 else '') if parts else ('', '')
        if selected.mode == 'assistant' and not resume_only and self.dispatch_conversation_command(head, path):
            return
        if selected.mode == 'assistant' and head.lower() == '/resume' and not resume_only:
            if path.strip():
                self.set_status('/resume takes no arguments. It continues the saved session and queued messages.'); return
            self.action_resume_conversation(); return
        if selected.mode in {'assistant','session'} and head.lower()=='/attach' and not resume_only:
            self.query_one('#prompt',TextArea).clear()
            self.action_attach(path.strip().strip('"') or None)
            return
        if self.busy:
            if self.selection().mode == 'assistant':
                self.send_operator_message()
            return
        if self.selection().mode == 'assistant' and not assistant:
            if resume:
                self.action_resume_conversation(); return
            self.begin_assistant()
            return
        if self.selection().mode == 'conversation' and not assistant:
            self.begin_entry()
            return
        prior_session = self.assistant_session
        prior_history = self.assistant_history
        try:
            self.prepare_workspace()
            self.active = self.selection()
            if assistant:
                self.active = replace(self.active, mode='session',
                                      session=self.assistant_session or '', guide_next=False)
            if not resume and self.active.mode == 'session' and not continuation:
                self.validate_pending_permissions(self.active.root)
                self.validate_pending_graph(self.active.root)
            if not resume and not replan and self.active.mode == 'plan':
                self.validate_pending_graph(self.active.root)
            if resume_only:
                self.active = replace(self.active, prompt='', context_paths=[], context_root='',
                    attachments=[], attachment_root='', attachment_project='')
            self.active.recheck_candidate = recheck_candidate
            self.active.retry_delivery = retry_delivery
            self.active.repair_plan = repair_plan
            if continuation:
                from .native_continuity import save_request
                from .native_agent import session_root
                preview, brief, deployment = continuation
                self.active.session = str(session_root()/uuid4().hex)
                save_request(self.active.session, preview, brief, deployment)
                self.active.prompt = ''
                args = ['agent', '--continue-provider', '--session', self.active.session,
                        '--format', 'json', '--activity-events']
            else:
                args = command(self.active, resume, replan=replan, assistant=assistant)
            if self.active.mode == 'plan':
                self.project_graph_path = self.active.graph_path
            if resume_only and not (recheck_candidate or retry_delivery or repair_plan) and '--continue-provider' not in args:
                args += ['--resume-interrupted-model']
            if assistant:
                self.assistant_session = self.active.session
                user_message='You: '+self.active.prompt.strip()+(
                    '\nContext: '+', '.join(self.active.context_paths) if self.active.context_paths else '')+(
                    '\nAttachments: '+', '.join(row['name'] for row in self.active.attachments) if self.active.attachments else '')
                if continuation:
                    user_message = 'Provider changed explicitly. Continuity brief: '+str(len(brief.encode()))+' bytes. Previous history kept locally.'
                    self.remember_message(user_message)
                elif not resume_only:
                    self.remember_message(user_message)
            if expected_sequence is not None:
                if resume or self.active.mode != 'sequence':
                    raise ValueError('A reviewed selection applies only to a new sequence.')
                args += ['--expected-selection', expected_sequence]
            if self.active.mode == 'plan':
                self.query_one('#plan', Input).value = self.active.plan
            if self.active.mode == 'sequence':
                self.query_one('#sequence', Input).value = self.active.sequence
            if self.active.mode == 'parallel':
                self.query_one('#batch', Input).value = self.active.batch
            if self.active.mode == 'research':
                self.query_one('#research', Input).value = self.active.research
            if self.active.mode == 'synthesis':
                self.query_one('#synthesis', Input).value = self.active.synthesis
            recovery = replace(self.active, prompt='', context_paths=[], context_root='', attachments=[],
                               attachment_root='', attachment_project='') if self.active.mode == 'session' else self.active
            if resume_only:
                recovery = replace(selected, mode='session', session=self.active.session,
                    prompt='' if selected.prompt.strip().lower() == '/resume' else selected.prompt)
            invocation_id = uuid4().hex
            self.active_invocation = invocation_id  # the turn an audit reviews
            if invocation_id:
                flag = '--run-id' if self.active.mode == 'work' else '--invocation-id'
                position = 2 if self.active.mode == 'work' else 1
                args[position:position] = [flag, invocation_id]  # Preserve Work ID and verifier REMAINDER argv.
            self.checkpoint('execution_requested', {'resume': resume, 'replan': replan,
                'operator': self.active.prompt, 'invocation_id': invocation_id,
                'context_root': self.active.context_root, 'context_paths': self.active.context_paths,
                'attachments': self.active.attachments,
                'demand_sequence': expected_sequence is not None}, selection=recovery)
            if assistant and not resume_only:
                self.say(user_message)
            if resume_only and selected.prompt.strip().lower() == '/resume':
                self.query_one('#prompt', TextArea).clear()
            if self.active.mode == 'session' and not resume_only:
                self.query_one('#prompt', TextArea).clear()
                self.clear_context()
        except Exception as exc:
            if assistant:
                self.assistant_session = prior_session
                self.assistant_history = prior_history
            self.set_status(str(exc)); return
        self.controls(True)
        self.cancel_requested = False
        self.last_result = None
        if self.active.mode == 'work':
            self.reviewed_work_control = None
            self.query_one('#work-control-status', Static).update('')
        self.reviewed_plan = None
        self.reviewed_decision = None
        self.set_status('Preparing execution…')
        self.run_command(args, self.active)

    def stopped_turn(self):
        """The status of the open conversation's last turn when it did not finish, else None."""
        if not self.assistant_session:
            return None
        try:
            from .native_agent import read_json
            invocations = read_json(Path(self.assistant_session)/'native-agent.json', 1024*1024).get('invocations') or []
        except (OSError, ValueError):
            return None
        status = invocations[-1].get('status') if invocations else None
        return status if status and status != 'PROPOSED' else None

    def begin_assistant(self):
        try:
            self.prepare_workspace()
        except Exception as exc:
            self.set_status(str(exc)); return
        text = self.selection().prompt.strip()
        if not text and not self.attachments:
            self.set_status('Write a message for the assistant.'); return
        if text.startswith('/'):
            if self.context_paths or self.attachments:
                self.set_status('Remove selected file context before choosing a flow.'); return
            self.begin_entry()
            return
        try:
            context_prompt(self.selection())
        except (ValueError,OSError) as exc:
            self.set_status(str(exc)); return
        # Replies are journaled unchanged; the isolated host judge settles consent.
        self.folder_note = ''
        session = self.assistant_session
        if session and not any((Path(session)/name).exists() for name in ('native-agent.json', 'continuation.json')):
            # The first turn failed before its session existed: the next message starts it instead of
            # resuming nothing ("No such file …native-agent.json" kept the message unsent, 2026-11-03).
            self.assistant_session = None
        try:
            self.execute(bool(self.assistant_session), assistant=True)
        finally:
            self.folder_note = ''

    def begin_entry(self):
        if self.context_paths:
            self.set_status('Remove selected file context before choosing a flow.'); return
        try:
            self.prepare_workspace()
        except Exception as exc:
            self.set_status(str(exc)); return
        selected = self.selection()
        text = selected.prompt.strip()
        from .console_entry import help_text
        if text == '/help':
            self.say(help_text())
            return
        if text == '/status':
            if self.active and Path(self.active.root).resolve() == Path(selected.root).resolve():
                self.say(json.dumps(self.last_result, ensure_ascii=False) if self.last_result else 'Execution has no final result yet.')
            else:
                self.say('No execution in this conversation/project yet. Select a saved session or Work to inspect it.')
            return
        if not text:
            self.set_status('Describe what you want to do.')
            return
        if self.entry_root != selected.root:
            self.entry_history = ()
            self.entry_questions = ()
        self.entry_root = selected.root
        self.entry_proposal = None
        try:
            self.checkpoint('interpretation_requested', {'operator': text})
        except Exception as exc:
            self.set_status('Demand could not be saved: '+str(exc)); return
        from tare_tools_agent_runtime import CancellationToken
        self.entry_cancel = CancellationToken()
        self.controls(True)
        self.say('You: '+text)
        self.set_status('Choosing a next step; no project tools or execution are enabled.')
        self.interpret_entry(selected, self.entry_cancel)

    @work(group='entry')
    async def interpret_entry(self, selected, cancellation):
        try:
            proposal = await asyncio.to_thread(self.backend.propose_entry, selected, cancellation,
                                               self.entry_history, self.entry_questions)
            if cancellation.is_cancelled():
                self.set_status('Interpretation cancelled; no flow started.')
                return
            if selected != self.selection():
                self.set_status('Inputs changed; send the demand again.')
                return
            self.say('Tare: '+proposal['reason'])
            if proposal.get('tokens') is not None:
                self.say('Interpretation usage: '+json.dumps(proposal['tokens']))
            self.say('Saved proposal: '+proposal['directory'])
            if proposal['status'] == 'NEEDS_INPUT':
                self.entry_history = tuple(proposal['messages'])
                self.entry_questions = (*self.entry_questions, proposal['question'])
                self.query_one('#prompt', TextArea).clear()
                self.say(proposal['question'])
                self.set_status('Reply above, or use an explicit /flow command.')
            elif proposal['status'] == 'PROPOSED':
                self.entry_history = self.entry_questions = ()
                self.entry_proposal = (selected, proposal)
                self.set_status('Suggested: '+proposal['flow']+' · Review and Start suggested flow, or change the demand.')
            else:
                self.set_status(proposal['status']+' · '+proposal['reason'])
            self.checkpoint('interpretation_finished', {key: proposal.get(key) for key in
                ('status', 'reason', 'question', 'flow', 'directory')}, required=False)
        except Exception as exc:
            self.set_status('Suggestion unavailable: '+str(exc))
        finally:
            self.entry_cancel = None
            self.controls(False)
            if self.close_after_run:
                self.exit()

    def start_suggested(self):
        if self.busy or not self.entry_proposal:
            return
        selected, proposal = self.entry_proposal
        if selected != self.selection():
            self.entry_proposal = None
            self.update_mode()
            self.set_status('Inputs changed; send the demand again.')
            return
        from .console_entry import realize
        choice = realize(selected, proposal)
        self.entry_proposal = None
        self.entry_history = ()
        self.entry_questions = ()
        self.query_one('#prompt', TextArea).load_text(choice.prompt)
        self.query_one('#session-role', Select).value = choice.role
        self.query_one('#verifier', Input).value = choice.verifier
        self.query_one('#research-urls', TextArea).load_text(choice.urls)
        if choice.mode == 'work':
            self.query_one('#work-id', Input).value = choice.work
        self.query_one('#mode', Select).value = choice.mode
        self.update_mode()
        self.execute(False)

    @work(group='execution')
    async def run_command(self, args, selection):
        started = time.monotonic()
        succeeded = False
        completion = None
        start_tree = None
        snapshot_gap = None
        launch_attempted = False
        try:
            if selection.mode == 'session' and session_has_terminal(selection):
                # Shell commands have no edit receipts; missing snapshots are a real audit gap.
                from .turn_engine import snapshot
                start_tree, snapshot_gap = await asyncio.to_thread(
                    snapshot, selection.root, cancelled=lambda: self.cancel_requested)
                if snapshot_gap:
                    self.say('Shell-change tracking unavailable: '+snapshot_gap+
                             '. Command effects are not covered by edit receipts.')
            if self.cancel_requested:
                self.last_result = {'status': 'CANCELLED', 'reason': 'CANCELLED_BEFORE_LAUNCH',
                    'execution': 'agent_not_started', 'model_calls': 0, 'authority': 'NONE'}
                self.set_status('Cancelled before launch; no agent started.')
                return
            launch_attempted = True
            from .execution_scope import for_coordinator
            with for_coordinator(selection.execution_policy.get('coordinator', '')):
                self.process = await self.backend.launch(args, selection.root)
            if self.cancel_requested:
                self.backend.cancel(self.process)
            output = self.query_one('#output', Transcript)
            completion = asyncio.create_task(observe_process(self.process, self.assistant_activity, self.say))
            while not completion.done():
                await asyncio.wait({completion}, timeout=1)
                await self.refresh_messages(force=False)
                self.refresh_command_permission()
                if selection.mode == 'session' and self.selection().mode == 'assistant':
                    self.refresh_execution_permission(selection)
                if not completion.done():
                    try:
                        if selection.mode == 'session':
                            progress = next((row.title for row in reversed(list(output.tool_rows.values()))
                                             if row.value['state'] == 'running'),
                                            'Receiving response' if output.live is not None else 'Working')
                        else:
                            progress = await asyncio.to_thread(self.backend.progress, selection)
                        prefix = 'Cancellation requested · ' if self.cancel_requested else ''
                        self.set_status(prefix+f'{time.monotonic()-started:.0f}s · '+progress)
                        # Stage summaries stay in the status line; child tool rows
                        # carry recorded operations without repeated progress cards.
                    except (OSError, ValueError, KeyError):
                        self.set_status('Running; progress observation unavailable.')
            stdout, stderr = await completion
            output.finish_stream()
            if stderr:
                self.say(stderr.decode('utf-8', errors='replace'))
            try:
                result = json.loads(stdout)
            except (ValueError, UnicodeError):
                raise ValueError(f'Process exited {self.process.returncode} without a valid result. Inspect saved custody before resuming.')
            self.last_result = result
            if selection.mode == 'work':
                from .work_console import control_copy
                view = result.get('operational_control')
                self.reviewed_work_control = (selection, view) if view else None
                self.query_one('#work-control-status', Static).update(control_copy(view))
            if snapshot_gap:
                self.last_result['shell_change_tracking_gap'] = snapshot_gap
            for row in result.get('tool_activity', {}).get('items', []):
                self.assistant_activity({'channel': 'activity', 'activity': row})
            succeeded = self.process.returncode == 0 and result.get('status') in {'PROPOSED', 'REVIEWED', 'COMPLETED', 'SETTLED'}
            if selection.mode == 'research':
                from .research_flow import render
                self.say(render(result), literal=True)
                self.research_source_options(result)
            elif selection.mode == 'parallel':
                from .parallel_explore import render
                self.say(render(result), literal=True)
            elif selection.mode == 'synthesis':
                from .research_synthesis import render
                self.say(render(result), literal=True)
            elif selection.mode == 'sequence':
                from .work_sequence import render
                self.say(render(result), literal=True)
            elif selection.mode == 'plan':
                self.show_plan(result)
                self.query_one('#plan', Input).value = selection.plan
                if result.get('status') == 'REVIEWED':
                    self.reviewed_plan = (selection.plan, result['proposal_sha256'], selection.root)
            elif selection.mode == 'session' and 'status' in result:
                from .native_display import render_result
                if self.selection().mode == 'assistant':
                    self.assistant_result(result, exit_code=self.process.returncode)
                else:
                    self.say(render_result(result), literal=True)
                request = result.get('folder_access_request')
                self.folder_request = {**request, 'session': result.get('session')} if request else None
                if request:
                    from .folder_access import presented_question
                    self.say(presented_question(request, self.selection().folder_grants, result['session']))
                    if request.get('question_id'):
                        from .consent_decisions import present
                        present(result['session'], request['question_id'])
                from .consent_decisions import pending as consent_pending
                from .native_agent import read_json
                custody = result.get('session')
                if custody and (Path(custody)/'native-agent.json').exists():
                    identity = read_json(Path(custody)/'native-agent.json', 1024*1024)['session_id']
                    questions = consent_pending(custody, identity)
                    if questions:
                        q = questions[0]
                        workspace = self.selection().root
                        def decided(choice, custody=custody, identity=identity, q=q, workspace=workspace):
                            self.consent_decided(choice, custody, identity, q['question_id'], workspace)
                        self.push_screen(ConsentPanel(q['question']), decided)
            else:
                from .work_console import render
                self.say(render('run', result), literal=True)
            review_hint = (' · F3 review changes' if selection.mode == 'session'
                           and result.get('execution') != 'agent_not_started'
                           and (result.get('source_changes')
                                or result.get('capability_observations', {}).get('effects')) else '')
            if self.selection().mode == 'assistant' and selection.mode == 'session':
                from .native_display import conversation_status
                self.set_status(f'{conversation_status(result, exit_code=self.process.returncode)} · {time.monotonic()-started:.1f}s'+review_hint)
            else:
                self.set_status(f"{result.get('status', 'UNAVAILABLE')} · {time.monotonic()-started:.1f}s · exit {self.process.returncode}"+review_hint)
            if result.get('status') == 'PROPOSED' and selection.mode != 'session':
                self.query_one('#prompt', TextArea).clear()
        except Exception as exc:
            if not launch_attempted:
                self.last_result = {'status': 'BLOCKED', 'reason': 'PREPARATION_FAILED_BEFORE_LAUNCH',
                    'execution': 'agent_not_started', 'model_calls': 0, 'authority': 'NONE'}
            self.presence_failed = True
            self.set_status('Execution unavailable: '+str(exc))
            self.say(str(exc))
        finally:
            preflight_refused = (self.last_result or {}).get('execution') == 'agent_not_started'
            if (not launch_attempted or preflight_refused) and selection.mode == 'session' and selection.session:
                # A reserved path is not a saved session. Preserve any existing custody.
                if not (Path(selection.session)/'native-agent.json').exists():
                    if self.assistant_session == selection.session:
                        self.assistant_session = None
                    selection.session = ''
                    if preflight_refused and self.selection().mode == 'assistant':
                        self.query_one('#output', Transcript).clear_message_deliveries()
                        self._message_proofs.clear()
                        self._message_observed_scope = None
                        self._message_unresolved = False
                        self.query_one('#message-status', Static).update('')
                        composer = self.query_one('#prompt', TextArea)
                        if not composer.text:
                            composer.load_text(selection.prompt)
            if self.process is not None and self.process.returncode is None:
                # Do not unlock the UI while an unsuccessfully observed child can still edit.
                self.backend.cancel(self.process)
                await self.process.wait()
            if completion is not None:
                if not completion.done():
                    completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
            self.query_one('#output', Transcript).finish_stream()
            self.finish_activity()
            self.process = None
            await self.refresh_messages()
            if selection.mode == 'session' and selection.session:
                # Refresh without selecting an unrelated newest session.
                await self.refresh_session(selection.session)
            from .console_demand import result_summary
            recovery = self.selection()
            if selection.mode == 'session':
                recovery.session = selection.session
                if recovery.mode == 'assistant':
                    recovery = replace(recovery, mode='session')
            self.checkpoint('execution_observed' if self.last_result else 'execution_unobserved',
                            result_summary(self.last_result), selection=recovery, required=False)
            from .turn_engine import STOPPED
            stopped = (self.last_result or {}).get('status') in STOPPED
            changed, checks, opened, ids, request = [], [], '', [], ''
            # Only a finished turn is audited: an interrupted one (RECONCILIATION_REQUIRED) has half its change,
            # and a fix round cannot continue a session that has no completed proposal. A turn finished by /resume
            # is audited with the invocations that stopped before it, against the message that opened them.
            if (selection.mode == 'session' and selection.session and not self.cancel_requested
                    and (self.last_result or {}).get('status') == 'PROPOSED' and getattr(self, 'active_invocation', None)):
                from .turn_engine import resumed_turn, turn_changes, turn_checks
                # Read under the UI lock: the custody lock must not race the next turn.
                ids, opened = await asyncio.to_thread(resumed_turn, Path(selection.session), self.active_invocation)
                from .turn_engine import conversation_request
                # The operator's earlier messages give a short reply ("sim") its meaning; the last one is this turn's.
                said = [v[5:] for v in self.assistant_history if isinstance(v, str) and v.startswith('You: ')]
                request = conversation_request([*said[:-1], selection.prompt.strip() or opened])
                runs = [(Path(selection.session), {'invocation_id': i}) for i in ids]
                changed = await asyncio.to_thread(turn_changes, runs, selection.root)
                checks = await asyncio.to_thread(turn_checks, runs, selection.root)
                if start_tree:
                    from .turn_engine import snapshot, tree_changes
                    after, why = await asyncio.to_thread(
                        snapshot, selection.root, cancelled=lambda: self.cancel_requested)
                    if after:
                        changed = await asyncio.to_thread(tree_changes, selection.root, start_tree, after)
                    else:
                        snapshot_gap = why
                        self.say('Shell-change tracking unavailable: '+str(why)+
                                 '. Command effects are not covered by edit receipts.')
                if snapshot_gap:
                    self.last_result['shell_change_tracking_gap'] = snapshot_gap
            permission_panel=getattr(self,'_command_permission_panel',None)
            if permission_panel is not None and permission_panel.is_mounted:permission_panel.dismiss('deny')
            self.controls(False)
            if selection.mode == 'session' and selection.session and not self.cancel_requested and stopped:
                # acts only on tare auto sessions
                self.call_later(self.auto_escalate, selection.session, (self.last_result or {}).get('status'))
            elif changed:
                self.audit_changes(selection.session, changed, selection.root, request, checks)
            elif (selection.mode == 'session' and selection.session and not self.cancel_requested
                    and (self.last_result or {}).get('status') == 'PROPOSED'):
                # tare auto only: a completed turn that changed nothing and only announced the work goes up a tier
                self.judge_answer(selection.session, selection.prompt.strip() or opened,
                                  (self.last_result or {}).get('output'))
            if ids:  # a finished turn: what the reply left unsaid, beside its audit (relay/heads_up.py)
                self.heads_up(selection.session, ids, selection.prompt.strip() or opened)  # earlier: the journal's
            if self.close_after_run:
                self.exit()
            elif succeeded and not self.cancel_requested and self.selection().mode != 'assistant':
                self.follow_demand()

    async def refresh_session(self, path):
        try:
            catalog = await asyncio.to_thread(self.backend.catalog, self.root if not self.active else self.active.root)
            widget = self.query_one('#session', Select)
            widget.set_options(self.session_options(catalog['sessions']))
            if path in [r['session'] for r in catalog['sessions']]:
                widget.value = path
        except Exception as exc:
            self.say('Session saved at '+path+'; catalog refresh failed: '+str(exc))

    @work(exclusive=True, group='review')
    async def review_selection(self):
        if self.busy:
            return
        try:
            selection = self.selection()
            if selection.mode == 'research':
                from .research_flow import render
                value = await asyncio.to_thread(self.backend.research_view, selection)
                if self.busy or (selection.root, selection.research) != (self.selection().root, self.selection().research):
                    return
                self.say(render(value), literal=True)
                self.research_source_options(value)
            elif selection.mode == 'sequence':
                from .work_sequence import render
                self.say(render(await asyncio.to_thread(self.backend.sequence_view, selection)), literal=True)
            elif selection.mode == 'plan':
                self.reviewed_plan = None
                self.update_mode()
                result = await asyncio.to_thread(self.backend.plan_preview, selection)
                if self.busy or selection != self.selection():
                    return
                self.project_graph_path = result['proposal']['graph_path']
                self.show_plan(result)
                if result['status'] == 'REVIEWED':
                    self.reviewed_plan = (selection.plan, result['proposal_sha256'], selection.root)
                self.update_mode()
            else:
                self.say(await asyncio.to_thread(self.backend.review, selection), literal=True)
        except Exception as exc:
            self.say('Review unavailable: '+str(exc))

    @work(exclusive=True, group='synthesis-review')
    async def list_synthesis_inputs(self):
        if self.busy:
            return
        selected = self.selection()
        try:
            rows = await asyncio.to_thread(self.backend.synthesis_inventory, selected.root)
            if selected.root == self.selection().root and not self.busy:
                self.query_one('#synthesis-choice', Select).set_options([
                    (r['kind']+' · '+r['status']+' · '+r['question'][:50],
                     json.dumps({'kind': r['kind'], 'directory': r['directory']})) for r in rows])
        except Exception as exc:
            self.say('Synthesis inputs unavailable: '+str(exc))

    def add_synthesis_input(self):
        if self.busy:
            return
        try:
            choice = self.query_one('#synthesis-choice', Select).value
            if choice is EMPTY_SELECTION:
                raise ValueError('Choose a saved input flow first.')
            field = self.query_one('#synthesis-inputs', TextArea)
            rows = json.loads(field.text)
            rows.append(json.loads(str(choice)))
            field.load_text(json.dumps(rows, indent=2))
            self.query_one('#synthesis-sha256', Input).value = ''
        except Exception as exc:
            self.say(str(exc))

    @work(exclusive=True, group='synthesis-review')
    async def preview_synthesis(self):
        if self.busy:
            return
        selected = self.selection()
        try:
            from .research_synthesis import render
            view = await asyncio.to_thread(self.backend.synthesis_preview, selected)
            if selected == self.selection() and not self.busy:
                self.say(render(view))
                self.query_one('#synthesis-sha256', Input).value = view['sha256']
        except Exception as exc:
            self.say('Synthesis review unavailable: '+str(exc))

    def research_source_options(self, view):
        self.query_one('#research-ref', Select).set_options([
            (p.get('title') or p['url'], p['ref']) for p in view.get('sources', []) if p.get('ref')])

    @work(exclusive=True, group='research-list')
    async def list_research(self):
        if self.busy:
            return
        selected = self.selection()
        try:
            rows = await asyncio.to_thread(self.backend.research_inventory, selected.root)
            if not self.busy and selected.root == self.selection().root:
                self.query_one('#saved-research', Select).set_options([
                    (r['status']+' · '+r['question'][:60], r['directory']) for r in rows])
                self.set_status(f'{len(rows)} saved research requests in this project.')
        except Exception as exc:
            self.say('Research catalog unavailable: '+str(exc))

    @work(exclusive=True, group='research-source')
    async def read_research_source(self):
        if self.busy:
            return
        try:
            selection = self.selection()
            ref = self.query_one('#research-ref', Select).value
            if ref is EMPTY_SELECTION:
                raise ValueError('Open collected sources and choose a page first.')
            start = int(self.query_one('#research-line', Input).value)
            value = await asyncio.to_thread(self.backend.research_source, selection, str(ref), start, start+199)
            if self.busy or (selection.root, selection.research) != (self.selection().root, self.selection().research):
                return
            self.say(value['url']+'\nSHA256: '+value['sha256']+'\n'+
                '\n'.join(f'{i}: {line}' for i, line in enumerate(value['text'].splitlines(), start)))
        except Exception as exc:
            self.say('Source unavailable: '+str(exc))

    @work(exclusive=True, group='inspect')
    async def open_sequence_work(self):
        if self.busy:
            return
        try:
            selected = self.selection()
            view = await asyncio.to_thread(self.backend.sequence_view, selected)
            if self.busy or selected.sequence != self.selection().sequence or selected.root != self.selection().root:
                return
            if not view['active_work']:
                self.set_status('This sequence has no active Work.'); return
            self.query_one('#work-id', Input).value = view['active_work']
            self.query_one('#mode', Select).value = 'work'
            self.set_status('Work opened. After recovery or a human answer, return to SEQUENCE and Resume.')
            await self.open_decision().wait()
        except Exception as exc:
            self.set_status('Sequence unavailable: '+str(exc))

    def apply_reviewed_plan(self):
        if self.busy or not self.reviewed_plan:
            return
        directory, digest, root = self.reviewed_plan
        selection = self.selection()
        if selection.plan != directory or selection.root != root:
            self.reviewed_plan = None
            self.update_mode()
            self.set_status('Selection changed; Review the selected proposal before applying.')
            return
        # A new explicit apply must not inherit a completed operation's cancellation.
        self.cancel_requested = False
        self.controls(True)
        self.query_one('#cancel', Button).disabled = True
        self.apply_graph(directory, digest)

    @work(group='execution')
    async def apply_graph(self, directory, digest):
        completed = False
        try:
            result = await self.confirmed_mutation('graph_apply_requested', {'plan': directory, 'sha256': digest},
                self.backend.apply_plan, directory, digest)
            self.say(f"Plan changes confirmed in {Path(result['graph_path']).name}. "
                     f"{len(result['work_ids'])} proposed tasks; execution admission is still required.")
            self.query_one('#output', Transcript).activity('Graph update details',
                json.dumps(result, ensure_ascii=False, indent=2))
            self.last_result = result
            self.project_graph_path = result['graph_path']
            self.query_one('#work', Select).set_options([])
            self.query_one('#work-id', Input).value = result['work_ids'][0]
            self.query_one('#mode', Select).value = 'work'
            self.set_status('Graph proposals applied. Selected Work still requires execution admission and source/verifier binding.')
            self.reviewed_plan = None
            self.checkpoint('graph_applied', result, required=False)
            completed = True
        except Exception as exc:
            self.set_status('Application unavailable; inspect the saved proposal and graph before retrying: '+str(exc))
        finally:
            self.controls(False)
            if self.close_after_run:
                self.exit()
            elif completed and not self.cancel_requested:
                self.follow_demand()

    def action_cancel(self):
        self.cancel_requested = True
        self.cancel_generation = getattr(self, 'cancel_generation', 0) + 1
        if self.entry_cancel is not None:
            self.entry_cancel.cancel()
            self.set_status('Interpretation cancellation requested; no flow will start.')
        if self.busy and self.process is None and self.entry_cancel is None:
            self.set_status('Cancellation requested; waiting for the current operation to finish. No next stage will start.')
        if self.process is not None:
            try:
                self.backend.cancel(self.process)
                self.set_status('Cancellation requested; waiting for the runner to record its result. Remote inference may still finish.')
            except (OSError, ProcessLookupError) as exc:
                self.set_status('Cancellation request failed: '+str(exc))

    @staticmethod
    def preparation_selection(selection):
        return tuple(getattr(selection, key) for key in
            ('mode', 'root', 'graph_path', 'work', 'source_ref', 'criterion_ids', 'work_verifier',
             'intake_criteria', 'intake_argv'))

    @work(exclusive=True, group='intake-draft')
    async def draft_intake(self):
        if self.busy:
            return
        selected = self.selection()
        try:
            node = await asyncio.to_thread(self.backend.work_intake_draft, selected)
            if self.busy or selected != self.selection():
                return
            self.reviewed_preparation = None
            self.query_one('#intake-options').display = True
            self.query_one('#intake-criteria', TextArea).load_text('\n'.join(node['exit_criteria']))
            self.query_one('#intake-argv', TextArea).clear()
            self.say('Objective: '+node['title']+' — '+node['summary']+'\nScope: '+
                     json.dumps(node['repository_scopes'], ensure_ascii=False))
            self.set_status('Review the criteria and enter verification arguments. Preview before confirming.')
        except Exception as exc:
            self.set_status('Work draft unavailable: '+str(exc))
        finally:
            self.update_mode()

    @work(exclusive=True, group='sources')
    async def load_sources(self):
        if self.busy:
            return
        try:
            selected = self.selection()
            rows = await asyncio.to_thread(self.backend.work_sources, selected)
            if (selected.root, selected.work) != (self.selection().root, self.selection().work):
                return
            self.query_one('#source-ref', Select).set_options([(ref, ref) for ref in rows])
            self.set_status(f'{len(rows)} specs with acceptance criteria. Select one to inspect its criteria.')
        except Exception as exc:
            self.set_status('Sources unavailable: '+str(exc))

    @work(exclusive=True, group='source-details')
    async def show_source(self):
        selected = self.selection()
        try:
            details = await asyncio.to_thread(self.backend.work_source_details, selected)
            if self.preparation_selection(selected) == self.preparation_selection(self.selection()):
                self.say(details)
        except Exception as exc:
            self.say('Source unavailable: '+str(exc))

    @work(exclusive=True, group='prepare-preview')
    async def preview_preparation(self, intake=False):
        if self.busy:
            return
        self.reviewed_preparation = None
        self.update_mode()
        try:
            selected = self.selection()
            method = self.backend.preview_work_intake if intake else self.backend.prepare_work
            view = await asyncio.to_thread(method, selected)
            if self.preparation_selection(selected) != self.preparation_selection(self.selection()) or self.busy:
                return
            if intake:
                from .work_intake import render
            else:
                from .work_prepare import render
            self.say(render(view))
            self.reviewed_preparation = (self.preparation_selection(selected), view)
            self.set_status('Preparation preview ready. Confirm records these choices; Start executes the Work.')
        except Exception as exc:
            self.set_status('Preparation unavailable: '+str(exc))
        finally:
            self.update_mode()

    def confirm_preparation(self):
        if self.busy or not self.reviewed_preparation:
            return
        selected, view = self.reviewed_preparation
        if selected != self.preparation_selection(self.selection()):
            self.reviewed_preparation = None
            self.update_mode()
            self.set_status('Preparation inputs changed; preview again before confirming.')
            return
        # A new explicit confirmation must not inherit a prior cancellation.
        self.cancel_requested = False
        self.controls(True)
        self.query_one('#cancel', Button).disabled = True
        self.save_preparation(view)

    @work(group='execution')
    async def save_preparation(self, view):
        completed = False
        try:
            result = await self.confirmed_mutation('preparation_requested', {'sha256': view['sha256']},
                self.backend.apply_preparation, view)
            self.last_result = result
            self.say(json.dumps(result, ensure_ascii=False, indent=2))
            self.set_status('Work prepared. Start runs IMPLEMENT.' if result['readiness']['ready']
                else 'Work prepared; unresolved prerequisites still block execution.')
            self.reviewed_preparation = None
            self.checkpoint('work_prepared', result, required=False)
            completed = True
        except Exception as exc:
            self.set_status('Preparation could not be confirmed: '+str(exc))
        finally:
            self.controls(False)
            if self.close_after_run:
                self.exit()
            elif completed and not self.cancel_requested:
                self.follow_demand()

    def resume_selected(self):
        if self.busy or getattr(self, 'work_control_pending', False):
            return
        selected = self.selection()
        if selected.mode == 'assistant':
            self.action_resume_conversation()
        elif selected.mode == 'session':
            # Resume is an explicit recovery action in direct sessions too:
            # retain the draft and let the runtime recheck an interrupted reply.
            self.execute(True, resume_only=True)
        elif selected.mode == 'work':
            self.request_work_control('resume')
        else:
            self.execute(True)

    def request_work_control(self, operation):
        if operation == 'pause' and not self.busy:
            self.set_status('No Work is running in this TUI. Resume the saved attempt before requesting a pause.')
            return
        if getattr(self, 'work_control_pending', False) or (self.busy and operation not in {'pause', 'inspect'}):
            return
        selected = self.active if self.busy else self.selection()
        if selected is None or selected.mode != 'work' or not selected.work:
            return
        note = self.query_one('#work-note', Input).value if operation == 'intervene' else ''
        if operation == 'intervene':
            reviewed = getattr(self, 'reviewed_work_control', None)
            if not reviewed or reviewed[0] != selected or reviewed[1]['status'] != 'PAUSED':
                self.set_status('Selection changed; inspect controls again.')
                return
            if not note.strip() or len(note.encode('utf-8')) > 8192:
                self.set_status('Enter a note of up to 8192 UTF-8 bytes.')
                return
        self.work_control_pending = True
        owns_busy = not self.busy
        if owns_busy:
            self.active = selected
            self.controls(True)
            self.query_one('#cancel', Button).disabled = True
        self.update_mode()
        self.save_work_control(selected, operation, note, owns_busy)

    @work(group='work-control')
    async def save_work_control(self, selected, operation, note, owns_busy):
        launch = False
        try:
            try:
                view = await asyncio.to_thread(self.backend.work_control, selected)
            except (FileNotFoundError, ValueError):
                if operation != 'resume':
                    raise
                # The Work owner must reconcile a Git landing already applied
                # before an acknowledgment was lost. It also owns stale-scope
                # refusals; this fallback grants no control command or budget.
                view = None
            current = self.selection() if owns_busy or not self.busy else self.active
            if current != selected:
                raise ValueError('Selection changed; inspect controls again.')
            if operation == 'resume':
                if view is not None and view['status'] not in {'RUNNING', 'PAUSED', 'RESUME_REQUESTED'}:
                    raise ValueError('The current round is still finishing. Inspect controls again.')
                launch = True
            mutate = operation not in {'inspect', 'resume'} or operation == 'resume' and view is not None and view['status'] == 'PAUSED'
            if mutate:
                token = view['token']
                if operation == 'intervene':
                    token = self.reviewed_work_control[1]['token']
                view = await self.confirmed_mutation('work_control_requested',
                    {'operation': operation, 'token': token, 'note': note},
                    self.backend.control_work, selected, operation, token, note, selection=selected)
            current = self.selection() if owns_busy or not self.busy else self.active
            if current != selected:
                launch = False
                raise ValueError('Selection changed; inspect controls again.')
            finished = (self.last_result if not owns_busy and not self.busy
                        and (self.last_result or {}).get('work_id') == selected.work else None)
            if finished:
                # The execution may acknowledge/finish before its control reply
                # reaches this worker. Its later owner receipt wins the display.
                view = finished.get('operational_control') or {
                    'status': finished['status'], 'record': None, 'token': None}
            if mutate:
                self.checkpoint('work_control_observed', view, selection=selected, required=False)
            self.reviewed_work_control = (selected, view) if view is not None else None
            from .work_console import control_copy
            message = ('Note saved for the next agent round.' if operation == 'intervene' else
                       'Checking saved Work recovery.' if view is None else
                       control_copy(view) or 'Work is not paused. Pause Work requests a safe stop.')
            if finished and not control_copy(view):
                message = ('Work completed before the pause took effect.' if finished['status'] == 'SETTLED'
                           else 'Work stopped. Review its saved result.')
            self.query_one('#work-control-status', Static).update(message)
            self.set_status(message)
        except Exception as exc:
            launch = False
            self.set_status('Work control refused: '+str(exc))
        finally:
            self.work_control_pending = False
            if owns_busy:
                self.controls(False)
            else:
                self.update_mode()
        if launch and not self.busy and selected == self.selection() and not self.close_after_run:
            self.execute(True)

    @staticmethod
    def decision_selection(selection):
        return selection.mode, selection.work, selection.root, selection.graph_path

    @work(exclusive=True, group='human-decision')
    async def open_decision(self):
        if self.busy:
            return
        self.reviewed_decision = None
        self.update_mode()
        try:
            selected = self.selection()
            view = await asyncio.to_thread(self.backend.decision, selected)
            if self.busy or self.decision_selection(selected) != self.decision_selection(self.selection()):
                return
            from .human_wait import render
            self.say(render(view))
            if view and view['status'] in {'WAITING_FOR_HUMAN', 'HUMAN_DECLINED'}:
                self.reviewed_decision = (self.decision_selection(selected), view)
            self.set_status(view['status'] if view else 'No pending human decision.')
        except Exception as exc:
            self.set_status('Decision unavailable: '+str(exc))
        finally:
            self.update_mode()

    def answer_decision(self, decision):
        if self.busy or not self.reviewed_decision:
            return
        selected, view = self.reviewed_decision
        if selected != self.decision_selection(self.selection()):
            self.reviewed_decision = None
            self.update_mode()
            self.set_status('Selection changed; open the decision again.')
            return
        selection = self.selection()
        if view['status'] != ('HUMAN_DECLINED' if decision == 'reconsider' else 'WAITING_FOR_HUMAN'):
            return
        note = self.query_one('#decision-note', Input).value
        self.controls(True)
        self.query_one('#cancel', Button).disabled = True
        self.save_decision(selection, view['request_sha256'], decision, note)

    @work(group='execution')
    async def save_decision(self, selection, digest, decision, note):
        try:
            result = await self.confirmed_mutation('decision_requested',
                {'request_sha256': digest, 'decision': decision, 'note': note},
                self.backend.answer_decision, selection, digest, decision, note)
            self.last_result = result
            from .human_wait import render
            self.say(render(result))
            self.reviewed_decision = None
            self.checkpoint('decision_observed', result, required=False)
            self.set_status('Decision reopened. Review and answer again.' if decision == 'reconsider' else
                            'Decision saved. Resume continues implementation.' if decision == 'approve'
                            else 'Decision saved. Implementation will remain stopped.')
        except Exception as exc:
            self.set_status('Decision refused: '+str(exc))
        finally:
            self.controls(False)
            if self.close_after_run:
                self.exit()

    @staticmethod
    def findings_selection(selected):
        return (selected.root, selected.mode, selected.research if selected.mode == 'research' else selected.batch,
                selected.finding_items)

    def show_promotion_template(self):
        from .planning_findings import promotion_template
        try:
            self.say(promotion_template())
            self.set_status('Template for reviewed findings. Reading it starts no model or Work.')
        except (OSError, ValueError) as exc:
            self.set_status('Template unavailable: '+str(exc))

    @work(exclusive=True, group='findings-preview')
    async def preview_findings(self):
        if self.busy:
            return
        self.reviewed_findings = None
        try:
            from . import planning_findings
            selected = self.selection()
            if selected.mode not in {'research', 'parallel'}:
                raise ValueError('Select research or exploration first.')
            root, kind, directory, items = self.findings_selection(selected)
            if not directory:
                raise ValueError('Select a saved flow first.')
            view = await asyncio.to_thread(planning_findings.preview, kind, directory,
                [x.strip() for x in items.split(',') if x.strip()] if items.strip() else None)
            if view['root'] != str(Path(root).resolve()):
                raise ValueError('Findings belong to another project.')
            if self.busy or self.findings_selection(selected) != self.findings_selection(self.selection()):
                return
            self.say(planning_findings.render(view))
            self.reviewed_findings = (self.findings_selection(selected), view)
            self.set_status('Review claims, sources, limitations and omitted reports. Attach reviewed findings makes no model call.')
        except Exception as exc:
            self.set_status('Findings unavailable: '+str(exc))
        finally:
            self.update_mode()

    def use_findings(self, *, start_plan=False):
        if not self.busy and self.reviewed_findings:
            self.cancel_requested = False
        return self._use_findings(start_plan, getattr(self, 'cancel_generation', 0))

    @work(group='findings-save')
    async def _use_findings(self, start_plan, cancellation_generation):
        if (self.busy or not self.reviewed_findings or self.cancel_requested or self.close_after_run
                or cancellation_generation != getattr(self, 'cancel_generation', 0)):
            return
        selection, view = self.reviewed_findings
        if selection != self.findings_selection(self.selection()):
            self.reviewed_findings = None
            self.update_mode()
            self.set_status('Selection changed; preview findings again.')
            return
        selected = self.selection()
        self.controls(True)
        attached = False
        try:
            from . import planning_findings
            from dataclasses import asdict
            saved = await asyncio.to_thread(planning_findings.save, view, view['sha256'], selected.finding_note)
            next_selection = replace(selected, mode='plan', plan='', findings=saved['path'], findings_sha256=saved['sha256'])
            # Checkpoint the new selection before navigation; reopening restores the attachment.
            self.checkpoint('findings_attached', {'path': saved['path'], 'sha256': saved['sha256']}, selection=next_selection)
            self.restore_selection(asdict(next_selection))
            self.set_status('Reviewed findings attached. Automatic continuation cancelled.' if self.cancel_requested
                else 'Reviewed findings attached. Choose a flow, describe the task, then Start.')
            attached = True
        except Exception as exc:
            self.set_status('Findings not attached: '+str(exc))
        finally:
            self.controls(False)
            if self.close_after_run:
                self.exit()
            elif attached and not self.cancel_requested:
                if start_plan:
                    self.execute(False)
                else:
                    self.follow_demand()

    def action_quit(self):
        if self.busy:
            self.close_after_run = True
            self.action_cancel()
        else:
            if (self.demand_id or self.has_draft()) and not self.save_demand():
                return
            self.stop_heads_up()
            self.exit()

    @staticmethod
    def revision_selection(selection):
        return WorkConsole.preparation_selection(selection)+(selection.revision_reason, selection.revision_paths)

    @work(exclusive=True, group='revision-preview')
    async def preview_revision(self):
        if self.busy:
            return
        self.reviewed_revision = None
        self.update_mode()
        try:
            selected = self.selection()
            view = await asyncio.to_thread(self.backend.preview_revision, selected)
            if self.revision_selection(selected) != self.revision_selection(self.selection()) or self.busy:
                return
            from .work_change import render
            self.say(render(view))
            self.reviewed_revision = (self.revision_selection(selected), view)
            self.set_status('Review the criteria and affected Works. Confirm preserves old receipts and requires new verification.')
        except Exception as exc:
            self.set_status('Revision unavailable: '+str(exc))
        finally:
            self.update_mode()

    def confirm_revision(self):
        if self.busy or not self.reviewed_revision:
            return
        selected, view = self.reviewed_revision
        if selected != self.revision_selection(self.selection()):
            self.reviewed_revision = None
            self.update_mode()
            self.set_status('Revision inputs changed; preview again.')
            return
        self.controls(True)
        self.query_one('#cancel', Button).disabled = True
        self.save_revision(view)

    @work(group='execution')
    async def save_revision(self, view):
        try:
            result = await self.confirmed_mutation('revision_requested', {'sha256': view.get('sha256')},
                self.backend.apply_revision, view)
            self.last_result = result
            self.say(json.dumps(result, ensure_ascii=False, indent=2))
            self.set_status('Work revised. Start creates a new execution. Invalidated dependents require preparation.')
            self.reviewed_revision = None
            self.reviewed_preparation = None
            self.checkpoint('work_revised', result, required=False)
        except Exception as exc:
            self.set_status('Revision refused: '+str(exc))
        finally:
            self.controls(False)
            if self.close_after_run:
                self.exit()


def main(argv=None):
    from .model_choices import PIN_HELP, help_summary
    values = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='Workspace assistant with optional native Work flows.', allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=help_summary() if any(v in ('--help', '-h') for v in values) else PIN_HELP)
    parser.add_argument('project',nargs='?',type=Path,help='Project folder; defaults to the current directory.')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--storage-root', type=Path, help='Preferred history folder; workspaces containing it use external user storage')
    parser.add_argument('--resume-coordinator-seconds',type=int,default=0,
                        help='Explicit total active seconds for a saved coordinator; retains used time and call limits')
    parser.add_argument('--resume-coordinator-calls',type=int,default=0,
                        help='Explicit OS conversation call pool, including workers; retains spent calls and the tactical session cap')
    from .execution_scope import add_arguments,from_arguments,activated,normalize_arguments
    add_arguments(parser)
    args = parser.parse_args(normalize_arguments(sys.argv[1:] if argv is None else argv))
    if args.resume_coordinator_seconds and not 1<=args.resume_coordinator_seconds<=86400:
        parser.error('--resume-coordinator-seconds requires 1..86400 total active seconds.')
    if args.resume_coordinator_calls and not 1<=args.resume_coordinator_calls<=10000:
        parser.error('--resume-coordinator-calls requires 1..10000 total calls.')
    if args.project is not None and args.root is not None:
        parser.error('Choose the project folder once: as PATH or --root PATH.')
    args.root=args.root or args.project or Path.cwd()
    args.root = args.root.expanduser().resolve()
    if not args.root.is_dir():
        parser.error('Folder not found: '+str(args.root)+'. Choose an existing project folder.')
    from .native_installation import default_storage, workspace_storage
    preferred = args.storage_root if args.storage_root is not None else default_storage()
    try:
        # Refuse a folder that contains tare's own history (the home folder) here, not as a crash while the TUI mounts.
        chosen = workspace_storage(args.root, preferred)
        if args.storage_root is not None:  # pins name profiles in the history folder this TUI's sessions use
            os.environ['TARE_STORAGE_ROOT'] = str(chosen)
        try:scope=from_arguments(args)
        except ValueError as error:
            # A new history folder has CLI profiles only once discovery writes them (no model call). Sessions
            # started before that failed their pins (2026-11-03): write them now, then check again.
            if 'Pin is unknown' not in str(error) or os.environ.get('TARE_CLI_DISCOVERY') == '0':
                raise
            from .cli_discovery import discover
            try:discover()
            except OSError:pass  # the check below still names the pin
            scope=from_arguments(args)
    except ValueError as error:parser.error(str(error))
    os.environ.setdefault('TARE_CLIENT', 'tare.tools TUI')  # who calls a local gateway; sessions inherit it
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('console requires an interactive terminal; use agent, sessions, run or status for automation.', file=sys.stderr)
        return 2
    from dataclasses import replace
    default_coordinator = scope.coordinator
    with activated(replace(scope, coordinator='')):
        WorkConsole(args.root, initial_mode='assistant', guided=False, storage_preferred=preferred,
                    discover_clis=True,resume_coordinator_seconds=args.resume_coordinator_seconds,
                    resume_coordinator_calls=args.resume_coordinator_calls,
                    default_coordinator=default_coordinator).run()
    return 0

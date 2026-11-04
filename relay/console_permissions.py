"""Compose project defaults with existing new-session controls."""
from pathlib import Path
from dataclasses import replace
from textual.widgets import Checkbox, Input, Select
from . import relay_mesh
from .native_installation import storage_root, workspace_storage
from .project_permissions import read_defaults, profile_values, permissions_view, save_defaults


class ProjectPermissions:
    def refresh_command_permission(self):
        from .command_permission import pending,answer
        from .execution_permission_panel import ExecutionPermissionPanel
        root=getattr(self.backend,'action_approval_root','')
        try:request=pending(root)
        except (ValueError,OSError):return
        panel=getattr(self,'_command_permission_panel',None)
        if not request:
            if panel is not None and panel.is_mounted:panel.dismiss('deny')
            return
        if getattr(self,'_command_permission_seen',None)==request['id']:return
        self._command_permission_seen=request['id']
        self._command_permission_panel=ExecutionPermissionPanel(request)
        self.set_status('Waiting for command approval · no model calls while you decide')
        def decided(choice):
            self._command_permission_panel=None
            try:answer(root,request['id'],choice or 'deny')
            except (ValueError,OSError) as error:
                if self.busy and not self.cancel_requested:self.notify(str(error),severity='warning')
        self.push_screen(self._command_permission_panel,decided)

    def refresh_execution_permission(self, selected):
        """Observe one host request while its tool waits; the model never answers it."""
        from .conversation_permissions import pending, answer
        from .execution_permission_panel import ExecutionPermissionPanel
        try:
            request = pending(selected.session)
        except (OSError, ValueError):
            return
        panel = getattr(self, '_execution_permission_panel', None)
        if not request:
            if panel is not None and panel.is_mounted:
                panel.dismiss('deny')
            return
        if request['id'] == getattr(self, '_execution_permission_seen', None):
            return
        self._execution_permission_seen = request['id']
        panel = self._execution_permission_panel = ExecutionPermissionPanel(request)
        self.set_status('Waiting for your permission · no model calls while you decide')
        def decided(choice):
            self._execution_permission_panel = None
            try:
                answer(selected.session, request['id'], choice or 'deny', project=selected.root,
                    directory=self.permissions_directory(selected.root), control_root=relay_mesh.RELAY_ROOT)
                self.set_status('Permission denied.' if choice == 'deny' else 'Permission granted · continuing the saved plan')
            except (OSError, ValueError) as exc:
                self.notify(str(exc), severity='error')
                self._execution_permission_seen = None
        self.push_screen(panel, decided)

    def permissions_directory(self, project):
        chosen = (workspace_storage(project, self.storage_preferred, self.storage_fallback)
                  if self.storage_preferred is not None else storage_root())
        return chosen/'console'

    def folder_grants(self, project):
        """The project's file of folders the agent may read outside it ('' when unavailable); cached because
        selection() runs on every keystroke."""
        cached = getattr(self, '_folder_grants', None)
        if cached and cached[0] == project:
            return cached[1]
        try:
            from .folder_access import store_path
            value = str(store_path(self.permissions_directory(project), project, relay_mesh.RELAY_ROOT)) if project else ''
        except (OSError, ValueError):
            value = ''
        self._folder_grants = (project, value)
        return value

    def set_pending_permissions(self, defaults, project):
        role, terminal, paths = profile_values(defaults)
        with self.prevent(Select.Changed, Checkbox.Changed):
            self.query_one('#session-role', Select).value = role
            self.query_one('#terminal', Checkbox).value = terminal
        if role == 'explorer':
            self.query_one('#verifier', Input).value = ''
        self.command_protection = dict(defaults.get('command_protection', {}))
        self.sync_terminal_permissions()
        self.project_admission = dict(defaults.get('project_admission',
            {'mode': 'quarantine', 'root_sha256': None, 'nested': False}))
        self.permission_write_paths = paths
        self.permission_profile_root = str(Path(project).resolve())
        self.permission_profile_error = None

    def sync_terminal_permissions(self):
        role=self.query_one('#session-role',Select).value
        terminal=self.query_one('#terminal',Checkbox)
        blocked=role!='implementer' or bool(self.command_protection)
        if blocked:
            with self.prevent(Checkbox.Changed):terminal.value=False
        terminal.disabled=blocked or bool(getattr(self, 'busy', False))
        terminal.tooltip=('System commands require edit access in project permissions (/permissions).'
                          if role!='implementer' else
                          'Commands use the project review and isolation policy.' if self.command_protection else None)

    def load_project_permissions(self, project, *, force=False):
        project = str(Path(project).resolve())
        if not force and self.permission_profile_root == project:
            return
        self.permission_profile_root = project
        try:
            defaults, _ = read_defaults(self.permissions_directory(project), project, relay_mesh.RELAY_ROOT)
            self.set_pending_permissions(defaults, project)
        except Exception as exc:
            self.permission_profile_error = 'Project permission defaults unavailable: '+str(exc)
            self.notify(self.permission_profile_error, severity='error')

    def validate_pending_permissions(self, project):
        # Reopening a draft preserves its selected permissions, but cannot hide
        # a preferences file that became unreadable after startup.
        read_defaults(self.permissions_directory(project), project, relay_mesh.RELAY_ROOT)
        if self.permission_profile_error:
            raise ValueError(self.permission_profile_error)

    def action_permissions(self):
        from .permissions_panel import PermissionsPanel
        if isinstance(self.screen, PermissionsPanel):
            self.screen.action_close()
            return
        if not self.navigation_matches_workspace():
            return
        selected = self.selection()
        pending = selected
        if selected.mode not in {'assistant', 'session'}:
            self.notify('Project permissions apply to direct conversations. Flows keep their own scope.')
            return
        project, control = self.workspace_root, str(Path(relay_mesh.RELAY_ROOT).resolve())
        session = self.assistant_session if selected.mode == 'assistant' else selected.session
        scope = self.project_navigation_scope()
        try:
            directory = self.permissions_directory(project)
        except Exception as exc:
            self.notify(str(exc), severity='error')
            return
        def reader():
            return permissions_view(directory, project, control, session=session, pending=pending)
        def saver(defaults, revision):
            nonlocal pending
            saved, saved_revision = save_defaults(directory, project, control, defaults, revision)
            next_pending = pending
            if not session:
                role, terminal, paths = profile_values(saved)
                next_pending = replace(selected, role=role, terminal=terminal, write_paths=paths,
                                       project_admission=dict(saved.get('project_admission',
                                           {'mode': 'quarantine', 'root_sha256': None, 'nested': False})),
                                       command_protection=dict(saved.get('command_protection', {})),
                                       verifier='' if role == 'explorer' else selected.verifier)
            view = permissions_view(directory, project, control, session=session, pending=next_pending)
            if view['revision'] != saved_revision:
                raise ValueError('Defaults were saved, then changed elsewhere. Refresh to see the current defaults.')
            pending = next_pending
            return view
        def closed(result):
            if result is None:
                return
            if self.busy or self.sending_message or scope != self.project_navigation_scope():
                self.notify('Defaults saved for the selected project. Current conversation was not changed.')
                return
            if not session:
                self.set_pending_permissions(result['defaults'], project)
                self.update_mode()
            self.set_status('Project defaults saved. Existing conversations keep their original permissions.')
        self.push_screen(PermissionsPanel(project, reader=reader, saver=saver), closed)

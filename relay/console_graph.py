"""Project graph defaults and saved-conversation graph inspection."""
from pathlib import Path

from . import relay_mesh
from .project_graph import read_defaults, save_defaults, graph_view, observe


class ProjectGraph:
    def load_project_graph(self, project, *, force=False):
        project = str(Path(project).resolve())
        if not force and self.graph_profile_root == project:
            return
        self.graph_profile_root, self.graph_profile_error = project, None
        self.project_graph_path = ''
        try:
            self.project_graph_path, _ = read_defaults(self.permissions_directory(project), project, relay_mesh.RELAY_ROOT)
        except Exception as exc:
            self.graph_profile_error = 'Project graph defaults unavailable: '+str(exc)
            self.notify(self.graph_profile_error, severity='error')

    def validate_pending_graph(self, project):
        read_defaults(self.permissions_directory(project), project, relay_mesh.RELAY_ROOT)
        if self.graph_profile_error:
            raise ValueError(self.graph_profile_error)

    def action_backlog(self):
        from .project_graph_panel import ProjectGraphPanel
        if isinstance(self.screen, ProjectGraphPanel):
            self.screen.action_close()
            return
        if not self.navigation_matches_workspace():
            return
        selected = self.selection()
        if selected.mode not in {'assistant', 'session'}:
            self.notify('Set graph defaults from a direct conversation. Saved plans keep their original graph.')
            return
        project, control = self.workspace_root, str(Path(relay_mesh.RELAY_ROOT).resolve())
        session = self.assistant_session if selected.mode == 'assistant' else selected.session
        pending = selected.graph_path
        scope = self.project_navigation_scope()
        try:
            directory = self.permissions_directory(project)
        except Exception as exc:
            self.notify(str(exc), severity='error')
            return
        def reader():
            return graph_view(directory, project, control, session=session, pending=pending)
        def saver(path, revision):
            nonlocal pending
            saved, saved_revision = save_defaults(directory, project, control, path, revision)
            next_pending = pending if session else saved
            view = graph_view(directory, project, control, session=session, pending=next_pending)
            if view['revision'] != saved_revision:
                raise ValueError('Binding saved, then changed elsewhere. Refresh to see the current defaults.')
            pending = next_pending
            return view
        def closed(result):
            if result is None:
                return
            if not self.busy and not self.sending_message and scope == self.project_navigation_scope() and not session:
                self.project_graph_path = result['effective_path']
                self.graph_profile_error = None
                self.action_reload()
            self.set_status('Graph defaults saved. Existing conversations keep their original graph.')
        self.push_screen(ProjectGraphPanel(project, reader=reader, saver=saver,
                                           inspector=lambda path: observe(path)['summary']), closed)

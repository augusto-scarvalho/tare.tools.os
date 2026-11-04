"""Explicit project navigation with drafts retained in their original scope."""
from pathlib import Path
import json

from compute_plane.native_journal import custody_lock, regular, save_json
from textual.widgets import Input, TextArea

from . import relay_mesh
from .native_agent import read_json
from .native_installation import storage_root, workspace_storage


def recent_projects(directory):
    path = regular(Path(directory)/'recent-projects.json')
    if not path.exists():
        return []
    value = read_json(path, 65536)
    rows = value.get('projects')
    if (value.get('schema') != 'tare.tools/recent-projects/1'
            or not isinstance(rows, list) or len(rows) > 40
            or any(not isinstance(p, str) or not p or len(p) > 4096
                   or not Path(p).is_absolute() for p in rows)):
        raise ValueError('Recent projects could not be read; the saved file was preserved.')
    return rows


def remember_projects(directory, projects):
    directory = regular(Path(directory))
    directory.mkdir(parents=True, exist_ok=True)
    with custody_lock(directory):
        rows = recent_projects(directory)
        merged = []
        for value in [*projects, *rows]:
            path = str(Path(value).resolve())
            if len(path) > 4096:
                raise ValueError('Project path is too long to save in recent projects.')
            if path not in merged:
                merged.append(path)
        value = {'schema': 'tare.tools/recent-projects/1', 'projects': merged[:40]}
        while len(json.dumps(value, ensure_ascii=True, indent=2).encode()) > 60000:
            value['projects'].pop()
        save_json(directory/'recent-projects.json', value)


class ProjectNavigation:
    def project_navigation_scope(self):
        return (self.workspace_root, self.demand_id, self.demand_revision,
                str(Path(self.selection().root).resolve()),
                str(Path(relay_mesh.RELAY_ROOT).resolve()))

    def action_projects(self):
        from .project_picker import ProjectPicker
        if isinstance(self.screen, ProjectPicker):
            self.screen.action_close()
            return
        if not self.navigation_matches_workspace():
            return
        try:
            recent = recent_projects(storage_root()/'console')
        except Exception as exc:
            self.notify('Recent projects unavailable: '+str(exc), severity='warning')
            recent = []
        scope = self.project_navigation_scope()
        self.push_screen(ProjectPicker(self.workspace_root, recent=recent),
                         lambda target: self.open_project(target, scope, recent) if target else None)

    def open_project(self, target, expected_scope, recent=()):
        if not self.navigation_matches_workspace():
            return False
        if self.project_navigation_scope() != expected_scope:
            self.notify('Conversation or workspace changed. Open Projects again.', severity='warning')
            return False
        old = self.workspace_root
        field = self.query_one('#root', Input)
        try:
            target = str(Path(target).resolve())
            if not Path(target).is_dir():
                raise ValueError('Project folder is unavailable: '+target)
            # Validate external storage before changing the input or saving a draft.
            chosen = (workspace_storage(target, self.storage_preferred, self.storage_fallback)
                      if self.storage_preferred is not None else storage_root().resolve())
            if chosen.is_relative_to(Path(target)):
                raise ValueError('Choose history storage outside the selected project.')
            if target != old:
                field.value = target
                self.prepare_workspace(save_origin=True)
        except Exception as exc:
            if self.workspace_root == old:
                field.value = old
            self.notify('Project was not opened: '+str(exc), severity='error')
            return False
        try:
            remember_projects(chosen/'console', [target, old, *recent])
        except Exception as exc:
            self.notify('Project opened; recent projects could not be saved: '+str(exc), severity='warning')
        self.show_panel(None)
        self.query_one('#prompt', TextArea).focus()
        if target != old:
            self.action_reload()
            self.set_status('Project opened. Start a conversation or use Sessions to reopen saved work.')
        return True

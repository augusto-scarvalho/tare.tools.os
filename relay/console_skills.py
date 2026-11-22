"""/skills panel: project skill snapshots, inert local bundle capture, and managed configurable skills (SKL-01/SKL-02)."""
from pathlib import Path

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, OptionList, Static, TextArea

from .console_workbench import WorkbenchPanel, plain


class SkillsPanel(WorkbenchPanel):
    """Unified /skills modal preserving WorkbenchPanel widget IDs and PA-04 boundaries."""

    DEFAULT_CSS = '''
    SkillsPanel #skills-capture-row { height: 1; margin-bottom: 1; }
    SkillsPanel #skills-capture-path { width: 1fr; }
    SkillsPanel #workbench-list { height: 6; }
    SkillsPanel.narrow #workbench-list { height: 4; }
    SkillsPanel #workbench-preview { height: 1fr; }
    SkillsPanel #workbench-status { height: auto; max-height: 2; }
    SkillsPanel .panel-actions Button { min-width: 6; padding: 0 1; margin-left: 1; margin-right: 0; }
    '''

    def __init__(self, root: Path, *, selected_managed: list[str], on_toggle_managed, submit_project):
        super().__init__('Skills · project, managed & inert snapshots', [], kind='skills', submit=submit_project)
        self.root = Path(root)
        self.selected_managed = list(selected_managed)
        self.on_toggle_managed = on_toggle_managed
        self.view_mode = 'content'  # 'content' | 'diff' | 'bindings'
        self.editing_overlay = False
        self.editing_project_scope = False
        self._load_rows()

    def _load_rows(self):
        from .console_resources import discover_skills
        from . import managed_skills as managed
        from . import skill_bundles as bundles

        rows = []
        for r in discover_skills(self.root):
            rows.append({
                **r,
                'id': 'project:' + r['path'],
                'row_kind': 'project',
                'label': r['path'] + ' · ' + r['sha256'] + ' · ' + str(r['size_bytes']) + ' B',
            })

        managed_list = managed.list_managed(project=self.root)
        managed_bundle_ids = {m['bundle_id'] for m in managed_list}
        for m in managed_list:
            tags = []
            if m['skill_id'] in self.selected_managed:
                tags.append('ACTIVE')
            if m['has_overlay']:
                tags.append('edited')
            if m['parameter_count']:
                tags.append(f"{m['parameter_count']}p")
            if m['upstream_update_available']:
                tags.append('update')
            tag_str = ' · '.join(tags) if tags else 'base'
            rows.append({
                **m,
                'id': 'managed:' + m['skill_id'],
                'row_kind': 'managed',
                'label': f"[managed] {m['skill_id']} ({m['snapshot_id'][:8]}) · {tag_str} · {m['skill_name']}",
            })

        # Show latest inert snapshot per bundle (especially useful for newly captured bundles not yet managed).
        latest_by_bundle = {}
        for s in bundles.list_snapshots():
            latest_by_bundle[s['bundle_id']] = s
        for bid in sorted(latest_by_bundle):
            s = latest_by_bundle[bid]
            state = 'managed' if bid in managed_bundle_ids else 'inert'
            scope_tag = 'full' if s['complete_directory'] else 'subset'
            rows.append({
                **s,
                'id': 'snapshot:' + s['bundle_id'] + '@' + s['snapshot_id'],
                'row_kind': 'snapshot',
                'label': f"[snapshot] {s['bundle_id']}@{s['snapshot_id'][:8]} · {state}/{scope_tag} · {s['skill_name']}",
            })

        self.rows = rows

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static(self.heading, classes='panel-title', markup=False)
                yield Button('Back · Esc', id='workbench-close')
            with Horizontal(id='skills-capture-row'):
                yield Input(placeholder='Local skill folder to capture (inert snapshot)', id='skills-capture-path')
                yield Button('Capture', id='skills-capture')
            yield OptionList(id='workbench-list')
            yield TextArea('', read_only=True, show_line_numbers=False, id='workbench-preview')
            yield Static('', id='workbench-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Add to draft', id='workbench-add', classes='panel-primary', disabled=True)
                yield Button('Activate', id='skills-toggle', disabled=True)
                yield Button('Manage', id='skills-manage', disabled=True)
                yield Button('Edit', id='skills-save', disabled=True)
                yield Button('Update', id='skills-update', disabled=True)
                yield Button('View', id='skills-view')

    def render_rows(self):
        selected = self.current['id'] if self.current else None
        menu = self.query_one('#workbench-list', OptionList)
        menu.set_options([Text(plain(row['label'])) for row in self.rows])
        if self.rows:
            index = next((i for i, r in enumerate(self.rows) if r['id'] == selected), 0)
            menu.highlighted = index
            self.show_row(index)
        else:
            self.current = None
            self.query_one('#workbench-preview', TextArea).load_text(
                'No project skills, managed skills, or inert snapshots found.\n'
                'Add skills/<name>/SKILL.md, or enter a local skill folder above and click Capture.'
            )
            self.query_one('#workbench-status', Static).update(
                'Capturing a local folder creates an inert snapshot outside active discovery; no code runs.'
            )
            self._sync_buttons(None, valid=False)

    def _sync_buttons(self, row, *, valid: bool, has_update: bool = False):
        kind = row['row_kind'] if row else None
        self.query_one('#workbench-add', Button).disabled = not (valid and kind == 'project')
        toggle_btn = self.query_one('#skills-toggle', Button)
        toggle_btn.disabled = not (valid and kind == 'managed')
        if kind == 'managed' and row['skill_id'] in self.selected_managed:
            toggle_btn.label = 'Deactivate'
        else:
            toggle_btn.label = 'Activate'
        self.query_one('#skills-manage', Button).disabled = not (valid and kind == 'snapshot')
        save_btn = self.query_one('#skills-save', Button)
        save_btn.disabled = not (valid and kind == 'managed')
        save_btn.label = 'Save' if self.editing_overlay else 'Edit'
        self.query_one('#skills-update', Button).disabled = not (valid and kind == 'managed' and has_update)

    def show_row(self, index):
        if not 0 <= index < len(self.rows):
            return
        from .console_resources import preview_skill
        from . import managed_skills as managed
        from . import skill_bundles as bundles

        self.current = self.rows[index]
        self.editing_overlay = False
        self.editing_project_scope = False
        preview = self.query_one('#workbench-preview', TextArea)
        preview.read_only = True
        kind = self.current['row_kind']
        has_update = False

        try:
            if self.view_mode == 'bindings':
                body = managed.format_bindings()
                message = 'Per-runtime bindings for tare managed skills. Click Diff / Bindings to return to content.'
                valid = True
                if kind == 'managed':
                    view = managed.effective_view(self.current['skill_id'], project=self.root)
                    has_update = bool(view['upstream_update'].get('available'))
            elif kind == 'project':
                text = preview_skill(self.root, self.current['path'], self.current['sha256'])
                body = (
                    f"Project: {self.root.resolve()}\nPath: {self.current['path']}\n"
                    f"SHA-256: {self.current['sha256']}\nSize: {self.current['size_bytes']} bytes\n\n{text}"
                )
                message = 'Adds the complete reviewed snapshot to your draft (16 KiB combined limit). No tools are authorized.'
                valid = True
            elif kind == 'managed':
                view = managed.effective_view(self.current['skill_id'], project=self.root)
                has_update = bool(view['upstream_update'].get('available'))
                if self.view_mode == 'diff':
                    chunks = []
                    if view['rendered_diff']:
                        chunks.append('=== Base -> Effective ===\n' + view['rendered_diff'])
                    if has_update and view['upstream_update'].get('skill_md_diff'):
                        chunks.append('=== Base -> Upstream ===\n' + view['upstream_update']['skill_md_diff'])
                    body = '\n\n'.join(chunks) or 'No difference against base snapshot or upstream.'
                else:
                    body = view['effective_text']
                active_note = 'ACTIVE for next new session. ' if self.current['skill_id'] in self.selected_managed else ''
                update_note = (
                    f"Upstream {view['upstream_update']['latest_snapshot_id'][:8]} available. "
                    if has_update else ''
                )
                message = (
                    f"{active_note}{update_note}Origin: {view['origin']['bundle_id']}@{view['origin']['snapshot_id'][:8]} "
                    f"({view['text_source']}). Frozen per session; no effects authorized."
                )
                valid = True
            else:
                manifest = bundles.load_snapshot(self.current['bundle_id'], self.current['snapshot_id'], verify=True)
                body = bundles.format_manifest(manifest)
                message = 'Inert snapshot outside active discovery. Click Manage to create a configurable managed skill.'
                valid = True
            preview.load_text(plain(body))
        except (OSError, ValueError, UnicodeError) as exc:
            preview.load_text('Preview unavailable.')
            message, valid = str(exc), False

        self.query_one('#workbench-status', Static).update(plain(message))
        self._sync_buttons(self.current, valid=valid, has_update=has_update)

    def _reload_and_select(self, target_id: str | None, status: str):
        self.editing_overlay = False
        self._load_rows()
        if target_id:
            for r in self.rows:
                if r['id'] == target_id:
                    self.current = r
                    break
        self.render_rows()
        self.query_one('#workbench-status', Static).update(plain(status))

    def on_button_pressed(self, event):
        event.stop()
        bid = event.button.id
        if bid == 'workbench-close':
            self.action_close()
            return
        if bid == 'workbench-add' and self.current and self.current['row_kind'] == 'project':
            try:
                self.submit(self.current, '', '')
            except (OSError, ValueError) as exc:
                self.query_one('#workbench-status', Static).update(plain(str(exc)))
                return
            self.dismiss(None)
            return

        from . import managed_skills as managed
        from . import skill_bundles as bundles

        try:
            if bid == 'skills-capture':
                raw_path = self.query_one('#skills-capture-path', Input).value.strip()
                if not raw_path:
                    raise ValueError('Enter a local skill directory path to capture.')
                captured = bundles.capture_bundle(Path(raw_path))
                self.query_one('#skills-capture-path', Input).value = ''
                self.view_mode = 'content'
                target_id = 'snapshot:' + captured['bundle_id'] + '@' + captured['snapshot_id']
                state = 'already captured' if captured.get('already_captured') else 'captured inertly'
                self._reload_and_select(
                    target_id,
                    f"Bundle {captured['bundle_id']}@{captured['snapshot_id'][:8]} {state} "
                    f"({captured['summary']['file_count']} files; no code executed).",
                )
            elif bid == 'skills-manage' and self.current and self.current['row_kind'] == 'snapshot':
                view = managed.adopt_snapshot(self.current['bundle_id'], self.current['snapshot_id'])
                self.view_mode = 'content'
                self._reload_and_select(
                    'managed:' + view['skill_id'],
                    f"Managed skill {view['skill_id']} created from snapshot {view['origin']['snapshot_id'][:8]}.",
                )
            elif bid == 'skills-toggle' and self.current and self.current['row_kind'] == 'managed':
                sid = self.current['skill_id']
                self.selected_managed = list(self.on_toggle_managed(sid))
                now_on = sid in self.selected_managed
                self._reload_and_select(
                    'managed:' + sid,
                    f"Skill {sid} {'activated' if now_on else 'deactivated'} for new sessions. "
                    "Running and saved sessions keep their frozen skills.",
                )
            elif bid == 'skills-save' and self.current and self.current['row_kind'] == 'managed':
                sid = self.current['skill_id']
                preview = self.query_one('#workbench-preview', TextArea)
                if not self.editing_overlay:
                    view = managed.effective_view(sid, project=self.root)
                    self.view_mode = 'content'
                    preview.load_text(view['raw_text'])
                    preview.read_only = False
                    self.editing_overlay = True
                    self.editing_project_scope = (view['text_source'] == 'project_overlay')
                    scope_label = 'project overlay' if self.editing_project_scope else 'global overlay'
                    self.query_one('#skills-save', Button).label = 'Save'
                    self.query_one('#workbench-status', Static).update(
                        f"Editing {scope_label} for {sid} (use <param> placeholders). Click Save to persist."
                    )
                    preview.focus()
                else:
                    edited = preview.text
                    target_project = self.root if self.editing_project_scope else None
                    view = managed.set_overlay(sid, edited, project=target_project)
                    self.editing_overlay = False
                    self.editing_project_scope = False
                    preview.read_only = True
                    scope_label = 'project overlay' if target_project else 'global overlay'
                    self._reload_and_select(
                        'managed:' + sid,
                        f"Saved {scope_label} for {sid} (sha256={view['effective_sha256'][:12]}). New sessions will freeze it.",
                    )
            elif bid == 'skills-update' and self.current and self.current['row_kind'] == 'managed':
                sid = self.current['skill_id']
                view = managed.apply_upstream_update(sid, 'latest', keep_overlay=False)
                self.view_mode = 'content'
                self._reload_and_select(
                    'managed:' + sid,
                    f"Updated {sid} to upstream snapshot {view['origin']['snapshot_id'][:8]}.",
                )
            elif bid == 'skills-view':
                order = ('content', 'diff', 'bindings')
                self.view_mode = order[(order.index(self.view_mode) + 1) % len(order)]
                if self.rows:
                    idx = next((i for i, r in enumerate(self.rows) if self.current and r['id'] == self.current['id']), 0)
                    self.show_row(idx)
                elif self.view_mode == 'bindings':
                    self.query_one('#workbench-preview', TextArea).load_text(managed.format_bindings())
                    self.query_one('#workbench-status', Static).update('Per-runtime bindings for tare managed skills.')
                else:
                    self.render_rows()
        except (OSError, ValueError, UnicodeError) as exc:
            self.query_one('#workbench-status', Static).update(plain(str(exc)))

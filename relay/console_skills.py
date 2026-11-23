"""/skills panel: project skill snapshots, inert local bundle capture, and managed configurable skills (SKL-01/SKL-02)."""
from pathlib import Path

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, OptionList, Static, TextArea

from .console_panels import SupportPanel
from .console_workbench import WorkbenchPanel, plain


def _set_button_label(btn: Button, label: str):
    """Update a Button label and invalidate layout so auto-width expands instead of clipping."""
    if str(btn.label) != label:
        btn.label = label
        btn.refresh(layout=True)


class SkillRolesDialog(SupportPanel):
    """Modal to configure per-skill allowed roles and global/project per-role skill policy."""

    BINDINGS = [('escape', 'close', 'Back')]
    DEFAULT_CSS = '''
    SkillRolesDialog #roles-list { height: 1fr; }
    SkillRolesDialog #roles-status { height: auto; max-height: 2; margin-top: 1; }
    SkillRolesDialog .panel-actions Button { min-width: 6; padding: 0 1; margin-left: 1; margin-right: 0; }
    '''

    def __init__(self, root: Path, *, skill_id: str | None = None):
        super().__init__()
        self.root = Path(root)
        self.skill_id = skill_id
        self.mode = 'skill' if skill_id else 'policy'
        self.project_scope = False
        if skill_id:
            from . import managed_skills as managed
            try:
                view = managed.effective_view(skill_id, project=self.root)
                self.project_scope = (view['roles_source'] == 'project')
            except (OSError, ValueError):
                pass

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('', id='roles-title', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='roles-close')
            yield OptionList(id='roles-list')
            yield Static('', id='roles-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Toggle', id='roles-toggle', classes='panel-primary')
                yield Button('Tier: any', id='roles-tier', disabled=self.skill_id is None)
                yield Button('Scope: Global', id='roles-scope')
                yield Button('Role policy', id='roles-mode', disabled=self.skill_id is None)
                yield Button('Reset', id='roles-reset')

    def on_mount(self):
        self._render_roles(preserve_index=0)
        self.query_one('#roles-list', OptionList).focus()

    def action_close(self):
        if self.is_current:
            self.dismiss(True)

    def _target_project(self):
        return self.root if self.project_scope else None

    def _render_roles(self, *, preserve_index: int | None = None, status: str | None = None):
        from . import managed_skills as managed

        menu = self.query_one('#roles-list', OptionList)
        idx = menu.highlighted if preserve_index is None else preserve_index
        if idx is None or not 0 <= idx < len(managed.SKILL_ROLES):
            idx = 0

        scope_word = 'project' if self.project_scope else 'global'
        _set_button_label(self.query_one('#roles-scope', Button), 'Scope: Project' if self.project_scope else 'Scope: Global')
        mode_btn = self.query_one('#roles-mode', Button)
        mode_btn.disabled = self.skill_id is None
        _set_button_label(mode_btn, 'Role policy' if self.mode == 'skill' else 'Skill roles')
        tier_btn = self.query_one('#roles-tier', Button)

        options = []
        if self.mode == 'skill' and self.skill_id:
            view = managed.effective_view(self.skill_id, project=self._target_project())
            tier_btn.disabled = False
            _set_button_label(tier_btn, f"Tier: {view['min_model_class'] or 'any'}")
            policy_detail = managed.get_role_policy(project=self._target_project())
            title = f"Skill roles · {self.skill_id} ({scope_word})"
            for role in managed.SKILL_ROLES:
                checked = role in view['allowed_roles']
                pol_on = policy_detail[role]['enabled']
                mark = '[x]' if checked else '[ ]'
                if checked and not pol_on:
                    note = ' · BLOCKED (role policy OFF)'
                elif not pol_on:
                    note = ' · policy OFF'
                else:
                    note = ''
                options.append(Text(plain(f"{mark} {managed.ROLE_LABELS[role]} ({role}){note}")))
            tier_note = f" Min model: {view['min_model_class']} ({view['min_model_class_source']})." if view.get('min_model_class') else ''
            default_status = (
                f"Effective roles ({view['roles_source']}): "
                + (', '.join(view['effective_roles']) if view['effective_roles'] else '(none)')
                + f'.{tier_note} Applies to new conversations and their delegated roles.'
            )
        else:
            self.mode = 'policy'
            tier_btn.disabled = True
            _set_button_label(tier_btn, 'Tier: any')
            policy_detail = managed.get_role_policy(project=self._target_project())
            title = f"Role policy · all skills ({scope_word})"
            for role in managed.SKILL_ROLES:
                info = policy_detail[role]
                mark = '[ON ]' if info['enabled'] else '[OFF]'
                options.append(Text(plain(
                    f"{mark} {managed.ROLE_LABELS[role]} ({role}) · {info['source']}"
                )))
            default_status = (
                'Disabled roles receive zero managed skills regardless of individual skill settings.'
            )

        self.query_one('#roles-title', Static).update(plain(title))
        menu.set_options(options)
        menu.highlighted = idx
        self.query_one('#roles-status', Static).update(plain(status if status is not None else default_status))

    def _toggle_highlighted(self):
        from . import managed_skills as managed

        menu = self.query_one('#roles-list', OptionList)
        idx = menu.highlighted if menu.highlighted is not None else 0
        if not 0 <= idx < len(managed.SKILL_ROLES):
            return
        role = managed.SKILL_ROLES[idx]
        target = self._target_project()
        if self.mode == 'skill' and self.skill_id:
            view = managed.effective_view(self.skill_id, project=target)
            current = list(view['allowed_roles'])
            if role in current:
                current.remove(role)
            else:
                current.append(role)
            updated = managed.set_skill_roles(self.skill_id, current, project=target)
            state = 'allowed' if role in updated['allowed_roles'] else 'removed'
            extra = ' (blocked by role policy)' if role in updated['disabled_by_policy_roles'] else ''
            self._render_roles(
                preserve_index=idx,
                status=f"{self.skill_id}: {role} {state} ({'project' if target else 'global'}){extra}.",
            )
        else:
            detail = managed.get_role_policy(project=target)
            now_on = not detail[role]['enabled']
            managed.set_role_policy(role, now_on, project=target)
            self._render_roles(
                preserve_index=idx,
                status=f"Role {role} {'enabled' if now_on else 'disabled'} for managed skills ({'project' if target else 'global'}).",
            )

    def on_option_list_option_selected(self, event):
        event.stop()
        try:
            self._toggle_highlighted()
        except (OSError, ValueError) as exc:
            self.query_one('#roles-status', Static).update(plain(str(exc)))

    def on_button_pressed(self, event):
        event.stop()
        bid = event.button.id
        if bid == 'roles-close':
            self.action_close()
            return
        from . import managed_skills as managed

        try:
            if bid == 'roles-toggle':
                self._toggle_highlighted()
            elif bid == 'roles-tier' and self.mode == 'skill' and self.skill_id:
                target = self._target_project()
                cur_view = managed.effective_view(self.skill_id, project=target)
                cycle = (None, *managed.MODEL_CLASSES)
                cur_cls = cur_view.get('min_model_class')
                next_cls = cycle[(cycle.index(cur_cls) + 1) % len(cycle)]
                updated = managed.set_model_class(self.skill_id, next_cls, project=target)
                scope_word = 'project' if target else 'global'
                if next_cls is None:
                    if target and updated.get('min_model_class'):
                        msg = (
                            f"{self.skill_id}: cleared project model-class override "
                            f"(inherits global {updated['min_model_class']})."
                        )
                    else:
                        msg = f"{self.skill_id}: minimum model class cleared ({scope_word})."
                else:
                    msg = f"{self.skill_id}: minimum model class set to {next_cls} ({scope_word})."
                self._render_roles(status=msg)
            elif bid == 'roles-scope':
                self.project_scope = not self.project_scope
                self._render_roles()
            elif bid == 'roles-mode' and self.skill_id:
                self.mode = 'policy' if self.mode == 'skill' else 'skill'
                self._render_roles()
            elif bid == 'roles-reset':
                menu = self.query_one('#roles-list', OptionList)
                idx = menu.highlighted if menu.highlighted is not None else 0
                target = self._target_project()
                if self.mode == 'skill' and self.skill_id:
                    managed.set_skill_roles(self.skill_id, None, project=target)
                    msg = (
                        f"Cleared project role override for {self.skill_id}."
                        if target else
                        f"Reset {self.skill_id} roles to defaults."
                    )
                else:
                    role = managed.SKILL_ROLES[idx]
                    managed.set_role_policy(role, None, project=target)
                    msg = (
                        f"Cleared project policy override for {role}."
                        if target else
                        f"Reset {role} policy to default."
                    )
                self._render_roles(preserve_index=idx, status=msg)
        except (OSError, ValueError) as exc:
            self.query_one('#roles-status', Static).update(plain(str(exc)))


class SkillsPanel(WorkbenchPanel):
    """Unified /skills modal preserving WorkbenchPanel widget IDs and PA-04 boundaries."""

    DEFAULT_CSS = '''
    SkillsPanel #skills-capture-row { height: 1; margin-bottom: 1; }
    SkillsPanel #skills-capture-path { width: 1fr; }
    SkillsPanel #workbench-list { height: 6; }
    SkillsPanel.narrow #workbench-list { height: 4; }
    SkillsPanel #workbench-preview { height: 1fr; }
    SkillsPanel #workbench-status { height: auto; max-height: 2; }
    SkillsPanel .panel-actions Button { min-width: 5; padding: 0 1; margin-left: 1; margin-right: 0; }
    SkillsPanel.narrow .panel-actions Button { min-width: 4; padding: 0; margin-left: 1; margin-right: 0; }
    '''

    def __init__(
        self,
        root: Path,
        *,
        selected_managed: list[str],
        on_toggle_managed,
        submit_project,
        auto_skills: bool = False,
        on_toggle_auto_skills=None,
    ):
        super().__init__('Skills · project, managed & vendor catalog', [], kind='skills', submit=submit_project)
        self.root = Path(root)
        self.selected_managed = list(selected_managed)
        self.on_toggle_managed = on_toggle_managed
        self.auto_skills = bool(auto_skills)
        self.on_toggle_auto_skills = on_toggle_auto_skills
        self.view_mode = 'content'  # 'content' | 'diff' | 'bindings'
        self.editing_overlay = False
        self.editing_project_scope = False
        self.show_hidden_vendor = False
        self._pending_status: tuple[str | None, str] | None = None
        self._load_rows()

    def action_close(self):
        if self.editing_overlay:
            self.editing_overlay = False
            self.editing_project_scope = False
            preview = self.query_one('#workbench-preview', TextArea)
            preview.read_only = True
            target_id = self.current['id'] if self.current else None
            self._reload_and_select(target_id, 'Cancelled overlay edit.')
            self.query_one('#workbench-list', OptionList).focus()
            return
        super().action_close()

    def on_input_submitted(self, event):
        if event.input.id == 'skills-capture-path':
            event.stop()
            self._capture_from_input()

    def _load_rows(self):
        from .console_resources import discover_skills
        from . import managed_skills as managed
        from . import skill_bundles as bundles
        from . import vendor_skills as vendor_cat

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
            tags.append(managed.format_role_tags(m['effective_roles']))
            if m.get('min_model_class'):
                tags.append(f"tier={m['min_model_class']}")
            if m['has_overlay']:
                tags.append('edited')
            if m['parameter_count']:
                tags.append(f"{m['parameter_count']}p")
            if m['upstream_update_available']:
                tags.append('update')
            if m.get('origin_drift_available'):
                tags.append('drift')
            tag_str = ' · '.join(tags) if tags else 'base'
            rows.append({
                **m,
                'id': 'managed:' + m['skill_id'],
                'row_kind': 'managed',
                'label': f"[managed] {m['skill_id']} ({m['snapshot_id'][:8]}) · {tag_str} · {m['skill_name']}",
            })

        if (managed_list or self.auto_skills) and self.on_toggle_auto_skills is not None:
            mode_tag = 'ON (shortlist + abstain)' if self.auto_skills else 'OFF (manual only)'
            rows.append({
                'id': 'auto-skills-toggle',
                'row_kind': 'auto_skills_toggle',
                'managed_count': len(managed_list),
                'label': f'[selector] Auto-select skills: {mode_tag}',
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

        # Read-only deduplicated vendor catalog (Claude Code / Codex / AGY).
        catalog = vendor_cat.discover_vendor_catalog(include_hidden=self.show_hidden_vendor)
        for item in catalog['items']:
            if item['sync_status'] == 'in_sync' and not self.show_hidden_vendor:
                continue
            vtag = '+'.join(item['vendors'])
            cls_tag = vendor_cat.CLASS_LABELS.get(item['classification'], item['classification'])
            sync_tag = f" · {item['sync_status']}" if item['sync_status'] != 'not_imported' else ''
            name_suffix = (
                f" · {item['skill_name']}"
                if item['skill_name'].strip().lower() != item['slug'].strip().lower() else ''
            )
            rows.append({
                **item,
                'id': 'vendor:' + item['catalog_id'],
                'row_kind': 'vendor',
                'label': f"[{vtag}] {item['slug']} · {cls_tag}{sync_tag}{name_suffix}",
            })
        if catalog['hidden_count']:
            action = 'Hide' if self.show_hidden_vendor else 'Show'
            rows.append({
                'id': 'vendor-hidden-toggle',
                'row_kind': 'vendor_hidden_toggle',
                'hidden_count': catalog['hidden_count'],
                'counts_by_class': catalog['counts_by_class'],
                'label': f"[catalog] {action} {catalog['hidden_count']} hidden vendor skill(s) (cli-only / blocked-cap)",
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
                yield Button('Roles', id='skills-roles')
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
            _set_button_label(toggle_btn, 'Deactivate')
        else:
            _set_button_label(toggle_btn, 'Activate')
        manage_btn = self.query_one('#skills-manage', Button)
        if kind == 'vendor':
            manage_btn.disabled = not (valid and row.get('sync_status') == 'not_imported')
            _set_button_label(manage_btn, 'Import')
        elif kind == 'auto_skills_toggle':
            manage_btn.disabled = self.on_toggle_auto_skills is None
            _set_button_label(manage_btn, 'Disable' if self.auto_skills else 'Enable')
        elif kind == 'vendor_hidden_toggle':
            manage_btn.disabled = False
            _set_button_label(manage_btn, 'Hide' if self.show_hidden_vendor else 'Show')
        else:
            manage_btn.disabled = not (valid and kind == 'snapshot')
            _set_button_label(manage_btn, 'Manage')
        save_btn = self.query_one('#skills-save', Button)
        save_btn.disabled = not (valid and kind == 'managed')
        _set_button_label(save_btn, 'Save' if self.editing_overlay else 'Edit')
        self.query_one('#skills-update', Button).disabled = not (valid and kind == 'managed' and has_update)

    def show_row(self, index):
        if not 0 <= index < len(self.rows):
            return
        from .console_resources import preview_skill
        from . import managed_skills as managed
        from . import skill_bundles as bundles
        from . import vendor_skills as vendor_cat

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
                message = 'Per-runtime bindings for tare managed skills. Click View to return to content.'
                valid = True
                if kind == 'managed':
                    view = managed.effective_view(self.current['skill_id'], project=self.root)
                    has_update = bool(view['upstream_update'].get('available')) or bool(
                        (view.get('origin_drift') or {}).get('available')
                    )
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
                up_avail = bool(view['upstream_update'].get('available'))
                drift_avail = bool((view.get('origin_drift') or {}).get('available'))
                has_update = up_avail or drift_avail
                if self.view_mode == 'diff':
                    chunks = []
                    if view['rendered_diff']:
                        chunks.append('=== Base -> Effective ===\n' + view['rendered_diff'])
                    if up_avail and view['upstream_update'].get('skill_md_diff'):
                        chunks.append('=== Base -> Upstream ===\n' + view['upstream_update']['skill_md_diff'])
                    if drift_avail and view['origin_drift'].get('skill_md_diff'):
                        chunks.append('=== Base -> Origin (live) ===\n' + view['origin_drift']['skill_md_diff'])
                    body = '\n\n'.join(chunks) or 'No difference against base snapshot, upstream, or live origin.'
                else:
                    body = view['effective_text']
                active_note = 'ACTIVE for next new session. ' if self.current['skill_id'] in self.selected_managed else ''
                update_note = (
                    'Origin changed on disk (click Update to re-capture). '
                    if drift_avail else
                    f"Upstream {view['upstream_update']['latest_snapshot_id'][:8]} available. "
                    if up_avail else ''
                )
                role_note = f"Roles: {managed.format_role_tags(view['effective_roles'])}. "
                tier_note = f"Min model: {view['min_model_class']}. " if view.get('min_model_class') else ''
                message = (
                    f"{active_note}{update_note}{role_note}{tier_note}Origin: {view['origin']['bundle_id']}@{view['origin']['snapshot_id'][:8]} "
                    f"({view['text_source']}). Frozen per session; no effects authorized."
                )
                valid = True
            elif kind == 'vendor':
                body = vendor_cat.preview_vendor_item(self.current)
                cls_tag = self.current['classification']
                if self.current['sync_status'] == 'vendor_changed':
                    message = f"Vendor origin changed for managed skill {self.current['managed_skill_id']}; select the managed row and click Update."
                elif self.current['sync_status'] == 'in_sync':
                    message = f"Already managed as {self.current['managed_skill_id']} (in sync with vendor origin)."
                elif cls_tag == 'needs_sanitization':
                    message = 'Classified as needs_sanitization (oversized or foreign harness cues). Review before importing.'
                elif cls_tag in vendor_cat.HIDDEN_CLASSES:
                    message = f'Auto-hidden ({cls_tag}): vendor-specific or blocked external API; native CLI skill loading stays off.'
                else:
                    message = f"Inert vendor skill ({cls_tag}). Click Import to capture and manage with suggested roles."
                valid = True
            elif kind == 'auto_skills_toggle':
                fp = managed.catalog_index_fingerprint(project=self.root)
                state_word = (
                    'ENABLED (opt-in deterministic shortlist + abstention)'
                    if self.auto_skills else
                    'DISABLED (manual --skill / Activate only)'
                )
                body = (
                    f"Auto-select managed skills: {state_word}\n"
                    f"Managed skills configured: {self.current.get('managed_count', 0)}\n"
                    f"Catalog index SHA-256: {fp[:16]}\n\n"
                    'How it works (SKL-06 Corte A — zero model calls, zero token spend):\n'
                    '- Filters strictly by role policy and per-skill allowed_roles before ranking.\n'
                    '- Ranks lightweight metadata (triggers, skill_id, name, description, headings) into a shortlist of at most 5.\n'
                    '- Abstains cleanly (no_skill) when nothing crosses threshold, and refuses to guess on ambiguous ties.\n'
                    '- Freezes the selection receipt into native-agent.json for new conversations.\n\n'
                    'CLI preview: `tare skills select "<prompt>"`  |  Configure keywords: `tare skills triggers <id> <phrase>...`'
                )
                message = 'Click Enable / Disable to toggle opt-in deterministic auto-selection for new conversations.'
                valid = True
            elif kind == 'vendor_hidden_toggle':
                counts = self.current.get('counts_by_class') or {}
                body = (
                    f"Auto-hidden local vendor skills: {self.current['hidden_count']}\n"
                    f"  - cli_internal (vendor-specific CLI/harness skills): {counts.get('cli_internal', 0)}\n"
                    f"  - blocked_capability (external APIs / credentials blocked by sandbox): {counts.get('blocked_capability', 0)}\n\n"
                    'Click Show / Hide to inspect or hide these items. Native vendor CLI skill loading stays off in tare.'
                )
                message = 'Click Show / Hide to toggle auto-hidden vendor-specific and blocked-capability skills.'
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

        if self._pending_status is not None:
            pending_id, pending_msg = self._pending_status
            if self.current and self.current['id'] == pending_id:
                message = pending_msg
            else:
                self._pending_status = None

        self.query_one('#workbench-status', Static).update(plain(message))
        self._sync_buttons(self.current, valid=valid, has_update=has_update)

    def _reload_and_select(self, target_id: str | None, status: str):
        self.editing_overlay = False
        self._load_rows()
        matched_id = None
        if target_id:
            for r in self.rows:
                if r['id'] == target_id:
                    self.current = r
                    matched_id = r['id']
                    break
        if matched_id is None and self.rows:
            matched_id = self.rows[0]['id']
        self._pending_status = (matched_id, status)
        self.render_rows()
        self.query_one('#workbench-status', Static).update(plain(status))

    def _capture_from_input(self):
        from . import skill_bundles as bundles
        self._pending_status = None
        try:
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
        except (OSError, ValueError, UnicodeError) as exc:
            self.query_one('#workbench-status', Static).update(plain(str(exc)))

    def handle_button_pressed(self, event):
        bid = event.button.id
        self._pending_status = None
        if bid == 'workbench-close':
            self.action_close()
            return
        if bid == 'workbench-add' and self.current and self.current['row_kind'] == 'project':
            try:
                self.submit(self.current, '', '')
            except (OSError, ValueError) as exc:
                self.query_one('#workbench-status', Static).update(plain(str(exc)))
                return
            self.action_close()
            return

        from . import managed_skills as managed

        try:
            if bid == 'skills-capture':
                self._capture_from_input()
                return
            elif bid == 'skills-manage' and self.current and self.current['row_kind'] == 'snapshot':
                view = managed.adopt_snapshot(self.current['bundle_id'], self.current['snapshot_id'])
                self.view_mode = 'content'
                self._reload_and_select(
                    'managed:' + view['skill_id'],
                    f"Managed skill {view['skill_id']} created from snapshot {view['origin']['snapshot_id'][:8]}.",
                )
            elif bid == 'skills-manage' and self.current and self.current['row_kind'] == 'vendor':
                from . import vendor_skills as vendor_cat
                view = vendor_cat.import_vendor_skill(self.current)
                self.view_mode = 'content'
                self._reload_and_select(
                    'managed:' + view['skill_id'],
                    f"Imported {view['skill_id']} from {'+'.join(self.current['vendors'])} "
                    f"(roles={managed.format_role_tags(view['effective_roles'])}; no code executed).",
                )
            elif bid == 'skills-manage' and self.current and self.current['row_kind'] == 'auto_skills_toggle':
                if self.on_toggle_auto_skills is not None:
                    self.auto_skills = bool(self.on_toggle_auto_skills())
                    self._reload_and_select(
                        'auto-skills-toggle',
                        (
                            'Opt-in auto-select skills ENABLED (deterministic shortlist + abstention for new conversations).'
                            if self.auto_skills else
                            'Auto-select skills DISABLED (manual Activate / --skill only).'
                        ),
                    )
            elif bid == 'skills-manage' and self.current and self.current['row_kind'] == 'vendor_hidden_toggle':
                self.show_hidden_vendor = not self.show_hidden_vendor
                self._reload_and_select(
                    'vendor-hidden-toggle',
                    'Showing auto-hidden vendor skills.' if self.show_hidden_vendor else 'Auto-hidden vendor skills collapsed.',
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
                    _set_button_label(self.query_one('#skills-save', Button), 'Save')
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
            elif bid == 'skills-roles':
                sid = self.current['skill_id'] if self.current and self.current['row_kind'] == 'managed' else None
                target_id = self.current['id'] if self.current else None
                self.app.push_screen(
                    SkillRolesDialog(self.root, skill_id=sid),
                    callback=lambda _changed: self._reload_and_select(
                        target_id,
                        f"Updated role settings for {sid}." if sid else 'Updated per-role skill policy.',
                    ),
                )
            elif bid == 'skills-update' and self.current and self.current['row_kind'] == 'managed':
                sid = self.current['skill_id']
                cur_view = managed.effective_view(sid, project=self.root)
                if (cur_view.get('origin_drift') or {}).get('available'):
                    from . import vendor_skills as vendor_cat
                    view = vendor_cat.sync_managed_from_origin(sid, keep_overlay=False)
                    note = f"Re-captured origin and updated {sid} to snapshot {view['origin']['snapshot_id'][:8]}."
                else:
                    view = managed.apply_upstream_update(sid, 'latest', keep_overlay=False)
                    note = f"Updated {sid} to upstream snapshot {view['origin']['snapshot_id'][:8]}."
                self.view_mode = 'content'
                self._reload_and_select('managed:' + sid, note)
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

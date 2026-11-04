"""Small scheduling form for the existing console; Backlog owns graph mutation."""
import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from graph_backlog.jsonutil import sha256_canonical
from textual import work
from textual.widgets import Input, Select, Button, Static
from . import work_order, work_replacement
from .console_backend import require_control_graph
from .console_output import SelectableMessage, plain


class OrderConsole:
    def order_form(self):
        selected = self.selection()
        return (selected.root, selected.work, str(self.query_one('#order-priority', Select).value),
                self.query_one('#order-prerequisites', Input).value)

    def order_snapshot(self):
        return {**copy.deepcopy(self.order_draft), 'reason': self.query_one('#order-reason', Input).value}

    def restore_order(self, value):
        self.order_draft = copy.deepcopy(value)
        self.query_one('#order-reason', Input).value = value.get('reason', '')
        self.reviewed_order = self.order_loaded = None
        self.order_review_started = None
        self.show_order_draft()

    def show_order_draft(self):
        rows = [key+': '+edit['priority']+' · after '+(', '.join(edit['prerequisites']) or 'none')
                for key, edit in self.order_draft.get('edits', {}).items()]
        rows += [old+' → '+new for old, new in self.order_draft.get('replacements', {}).items()]
        self.query_one('#order-staged', Static).update('\n'.join(rows) or 'No scheduling changes staged.')
        self.query_one('#confirm-order', Button).disabled = self.busy or self.reviewed_order is None

    @work(exclusive=True, group='order-load')
    async def load_order(self):
        if self.busy: return
        selected = self.selection(); self.order_loaded = None
        try:
            require_control_graph(selected)
            current = await asyncio.to_thread(work_order.current, selected.work)
            if self.busy or selected != self.selection(): return
            self.query_one('#order-priority', Select).value = current['priority']
            self.query_one('#order-prerequisites', Input).value = ', '.join(current['prerequisites'])
            self.order_loaded = (selected.root, selected.work)
            self.set_status('Edit priority/prerequisites and stage this Work. Add other Works before previewing the set.')
        except Exception as exc: self.set_status('Scheduling unavailable: '+str(exc))

    def stage_order(self):
        if self.busy: return
        root, key, priority, raw = self.order_form()
        if self.order_loaded != (root, key):
            self.set_status('Load current scheduling for this Work first.'); return
        if self.order_draft.get('root', root) != root:
            self.set_status('Clear the scheduling draft before changing project.'); return
        if self.order_draft.get('replacements'):
            self.set_status('Apply or clear the replacement review before staging scheduling changes.'); return
        deps = [v.strip() for v in raw.split(',') if v.strip()]
        self.order_draft = {**self.order_snapshot(), 'root': root,
            'edits': {**self.order_draft.get('edits', {}), key: {'priority': priority, 'prerequisites': deps}}}
        self.reviewed_order = None; self.order_review_started = None; self.show_order_draft()
        self.checkpoint('order_draft_saved', required=False)

    def stage_replacement(self):
        if self.busy: return
        selected = self.selection(); successor = self.query_one('#replacement-work', Input).value.strip()
        if not selected.work or not successor:
            self.set_status('Select the old Work and enter its existing successor ID.'); return
        if self.order_draft.get('edits') or self.order_draft.get('root', selected.root) != selected.root:
            self.set_status('Apply or clear the current review before staging replacements.'); return
        self.order_draft = {**self.order_snapshot(), 'root': selected.root,
            'replacements': {**self.order_draft.get('replacements', {}), selected.work: successor}}
        self.reviewed_order = None; self.order_review_started = None; self.show_order_draft()
        self.checkpoint('order_draft_saved', required=False)

    def clear_order(self):
        if not self.busy:
            reviewed = self.reviewed_order
            observation = self.order_review_observation(reviewed[1], 'withdrawn') if reviewed else None
            self.restore_order({})
            self.checkpoint('order_draft_saved', {'review_observation': observation}, required=False)

    @work(exclusive=True, group='order-preview')
    async def preview_order(self):
        if self.busy: return
        self.reviewed_order = None; self.order_review_started = None; self.show_order_draft()
        selected = self.selection(); draft = self.order_snapshot()
        try:
            require_control_graph(selected)
            if draft.get('root') != selected.root:
                raise ValueError('Stage scheduling for the selected project first.')
            owner = work_replacement if draft.get('replacements') else work_order
            changes = draft.get('replacements') or draft.get('edits', {})
            view = await asyncio.to_thread(owner.preview, selected.root, changes, draft.get('reason', ''))
            if self.busy or selected != self.selection() or draft != self.order_snapshot(): return
            self.reviewed_order = (draft, view)
            # An owner diff is plain text: Markdown collapses its before/after rows.
            self.query_one('#output').append(SelectableMessage(plain(owner.render(view)),
                markup=False, classes='message scheduling-review'))
            self.show_order_draft()
            self.order_review_started = (monotonic(), datetime.now(timezone.utc).isoformat())
            self.checkpoint('order_previewed', {'proposal_sha256': view['sha256'],
                'base_revision': (view.get('order') or view['replacement'])['base_revision'],
                'work_ids': sorted(changes)}, required=False)
            self.set_status('Review scheduling changes and blockers, then Confirm. No work has started.')
        except Exception as exc: self.set_status('Scheduling preview refused: '+str(exc))

    def confirm_order(self):
        if self.busy or not self.reviewed_order: return
        draft, view = self.reviewed_order
        if draft != self.order_snapshot() or draft['root'] != self.selection().root:
            self.reviewed_order = None; self.order_review_started = None; self.show_order_draft()
            self.set_status('Scheduling draft changed; preview again.'); return
        observation = self.order_review_observation(view, 'confirmed')
        self.controls(True); self.query_one('#cancel', Button).disabled = True
        self.apply_order(draft, view, observation)

    def order_review_observation(self, view, decision):
        started = getattr(self, 'order_review_started', None)
        return {'measurement': 'console_preview_ready_to_decision', 'proposal_sha256': view['sha256'],
            'preview_ready_at': started[1] if started else None,
            'decision_at': datetime.now(timezone.utc).isoformat(), 'decision': decision,
            'elapsed_ms': round(max(0, monotonic()-started[0])*1000, 3) if started else None,
            'actor': 'unattributed'}

    @work(group='execution')
    async def apply_order(self, draft, view, observation):
        try:
            require_control_graph(self.selection())
            replacement = 'replacement' in view
            owner = work_replacement if replacement else work_order
            kind = 'work_replacement' if replacement else 'work_order'
            result = await self.confirmed_mutation(kind+'_requested',
                {'sha256': view['sha256'], 'draft_sha256': sha256_canonical(draft),
                 'review_observation': observation}, owner.apply, view)
            self.last_result = result
            self.restore_order({})
            self.checkpoint(kind+'_applied', result, required=False)
            self.set_status('Works replaced. Continue demand follows confirmed successors.' if replacement else
                'Scheduling updated. Continue demand uses the new priorities and prerequisites.')
        except Exception as exc:
            # A refused/uncertain apply no longer has a confirmable preview.
            # Preserve the draft; a fresh preview rechecks graph and execution.
            self.reviewed_order = self.order_review_started = None
            self.set_status('Scheduling not confirmed: '+str(exc))
        finally:
            self.controls(False); self.show_order_draft()
            if self.close_after_run: self.exit()

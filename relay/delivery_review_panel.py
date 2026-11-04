"""Optional human assessment of an exact saved candidate; no inference."""
from copy import deepcopy

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Select, Static

from .console_panels import SupportPanel
from .console_output import plain
from .delivery_evidence import SCHEMA, record_review, withdraw_review, withdrawal_state, active_review


class DeliveryReviewPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    DeliveryReviewPanel #delivery-review-fields { height: 1fr; }
    DeliveryReviewPanel .review-label { height: auto; margin-top: 1; }
    DeliveryReviewPanel #review-status { height: auto; max-height: 3; }
    '''

    def __init__(self, control, targets):
        super().__init__()
        self.control, self.targets = control, targets
        self.selected = None
        self.review = None
        self.finding_index = None
        self.changed = False

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Review recorded delivery', classes='panel-title', markup=False)
                yield Button('Back · Esc', id='review-close')
            yield Select([(t['work_id']+' · '+t['run_id'][:8], i) for i, t in enumerate(self.targets)],
                         value=0, allow_blank=False, id='review-run')
            with VerticalScroll(id='delivery-review-fields'):
                yield Static('', id='review-candidate', markup=False)
                yield Static('', id='review-saved-state', markup=False)
                yield Static('Delivery accepted', classes='review-label')
                yield Select([('Unknown', 'unknown'), ('Yes', 'yes'), ('No', 'no')],
                             value='unknown', allow_blank=False, id='review-accepted')
                yield Static('Material rework needed', classes='review-label')
                yield Select([('Unknown', 'unknown'), ('Yes', 'yes'), ('No', 'no')],
                             value='unknown', allow_blank=False, id='review-rework')
                yield Static('Review reference or explanation', classes='review-label')
                yield Input(placeholder='A test result, review or other supporting reference', id='review-reference')
                yield Static('Audit finding', classes='review-label')
                yield Select([], allow_blank=True, id='review-finding')
                yield Static('', id='review-claim', markup=False)
                yield Select([('Unresolved', 'unresolved'), ('Confirmed', 'confirmed'),
                              ('Corrected', 'corrected'), ('Rejected', 'rejected')],
                             value='unresolved', allow_blank=False, id='review-disposition')
                yield Static('Recorded cause', classes='review-label')
                yield Select([('Unknown', 'unknown'), ('Implementation', 'implementation'),
                              ('Tare tools', 'tooling'), ('Guidance', 'guidance'), ('External', 'external')],
                             value='unknown', allow_blank=False, id='review-cause')
                yield Input(placeholder='Correction reference (required for Corrected)', id='review-correction')
            yield Input(placeholder='Short reason for withdrawing the saved review', id='review-withdraw-reason')
            yield Static('Optional operator observations. Opening this view makes no model calls.',
                         id='review-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Save review', id='review-save', classes='panel-primary')
                yield Button('Withdraw review', id='review-withdraw', disabled=True)
                yield Button('Confirm withdrawal', id='review-withdraw-confirm')
                yield Button('Cancel withdrawal', id='review-withdraw-cancel')

    def on_mount(self):
        self.load_run(0)

    def load_run(self, index):
        if self.selected is self.targets[index]:
            return
        self.selected = self.targets[index]
        self.finding_index = None
        target = self.selected
        self.cancel_withdrawal()
        self.query_one('#review-withdraw', Button).disabled = not bool(active_review(target.get('review')))
        self.query_one('#review-saved-state', Static).update(plain(
            withdrawal_state(target.get('review')) or
            ('Saved review is active.' if target.get('review') else 'No saved eligible review to withdraw.')))
        findings = target['audit']['findings'] if target['audit'] else []
        self.review = deepcopy(target.get('review') or {
            'schema': SCHEMA, 'subject': target['subject'], 'accepted': None, 'material_rework': None,
            'reference': '', 'findings': [{'index': i, 'disposition': 'unresolved', 'cause': 'unknown',
                                         'correction_reference': None} for i in range(len(findings))]})
        self.query_one('#review-candidate', Static).update(plain(
            target['subject']['repository_root']+'\nCandidate: '+target['candidate_commit']+
            '\nAuditor: '+(str(target['audit']['auditor']) if target['audit'] else 'Unknown')))
        for key, selector in (('accepted', '#review-accepted'), ('material_rework', '#review-rework')):
            self.query_one(selector, Select).value = {None: 'unknown', True: 'yes', False: 'no'}[self.review[key]]
        menu = self.query_one('#review-finding', Select)
        menu.set_options([('Finding '+str(i+1), i) for i in range(len(findings))])
        menu.value = 0 if findings else Select.NULL
        self.query_one('#review-reference', Input).value = self.review.get('reference', '')
        self.load_finding(0 if findings else None)

    def load_finding(self, index):
        self.finding_index = index
        available = index is not None
        for selector in ('#review-finding', '#review-disposition', '#review-cause', '#review-correction'):
            self.query_one(selector).disabled = not available
        self.query_one('#review-claim', Static).update(plain(
            self.selected['audit']['findings'][index]['claim'] if available else 'No recorded audit findings.'))
        if available:
            decision = self.review['findings'][index]
            self.query_one('#review-disposition', Select).value = decision['disposition']
            self.query_one('#review-cause', Select).value = decision['cause']
            self.query_one('#review-correction', Input).value = decision['correction_reference'] or ''

    def on_select_changed(self, event):
        event.stop()
        if not self.review or event.value is Select.NULL or event.value != event.select.value:
            return
        if event.select.id == 'review-run':
            self.load_run(event.value)
        elif event.select.id == 'review-finding':
            if self.finding_index != event.value:
                self.store_finding()
                self.load_finding(event.value)
        elif event.select.id in ('review-disposition', 'review-cause'):
            index = self.query_one('#review-finding', Select).value
            if index is not Select.NULL:
                key = 'disposition' if event.select.id == 'review-disposition' else 'cause'
                self.review['findings'][index][key] = event.value

    def on_input_changed(self, event):
        if (event.input.id == 'review-correction' and self.review
                and event.value == event.input.value):
            index = self.finding_index
            if index is not None:
                self.review['findings'][index]['correction_reference'] = event.value or None

    def store_finding(self):
        # Read current controls before saving/switching; queued change messages
        # must never overwrite or lose the operator's last visible choice.
        if self.finding_index is not None:
            self.review['findings'][self.finding_index].update(
                disposition=self.query_one('#review-disposition', Select).value,
                cause=self.query_one('#review-cause', Select).value,
                correction_reference=self.query_one('#review-correction', Input).value or None)

    def cancel_withdrawal(self):
        for selector in ('#review-withdraw-reason', '#review-withdraw-confirm', '#review-withdraw-cancel'):
            self.query_one(selector).display = False
        self.query_one('#review-withdraw-reason', Input).value = ''
        self.query_one('#review-save', Button).disabled = False

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'review-close':
            self.action_close()
        elif event.button.id == 'review-withdraw' and self.selected:
            if not active_review(self.selected.get('review')):
                return
            for selector in ('#review-withdraw-reason', '#review-withdraw-confirm', '#review-withdraw-cancel'):
                self.query_one(selector).display = True
            self.query_one('#review-save', Button).disabled = True
            self.query_one('#review-status', Static).update(
                'Withdraw the saved assertion from quality counts? Delivery, audit and usage will remain. Add a reason and confirm.')
            self.query_one('#review-withdraw-reason', Input).focus()
        elif event.button.id == 'review-withdraw-cancel':
            self.cancel_withdrawal()
            self.query_one('#review-status', Static).update('Withdrawal cancelled. Nothing was written.')
        elif event.button.id == 'review-withdraw-confirm' and self.selected:
            try:
                captured = withdraw_review(self.control, self.selected['run_id'], self.selected['token'],
                                          self.query_one('#review-withdraw-reason', Input).value)
            except (OSError, ValueError) as exc:
                self.query_one('#review-status', Static).update(plain(str(exc)))
                return
            self.changed = True
            self.selected.update(token=captured['token'], review=captured['review'])
            target = self.selected
            self.selected = None
            self.load_run(self.targets.index(target))
            self.query_one('#review-status', Static).update(
                'Review withdrawn. Delivery, audit and usage retained. Only Save review can replace/reactivate it.')
        elif event.button.id == 'review-save' and self.selected:
            try:
                self.store_finding()
                for key, selector in (('accepted', '#review-accepted'), ('material_rework', '#review-rework')):
                    self.review[key] = {'unknown': None, 'yes': True, 'no': False}[self.query_one(selector, Select).value]
                self.review['reference'] = self.query_one('#review-reference', Input).value
                record_review(self.control, self.selected['run_id'], self.selected['token'], self.review)
            except (OSError, ValueError) as exc:
                self.query_one('#review-status', Static).update(plain(str(exc)))
                return
            self.dismiss(True)

    def action_close(self):
        self.dismiss(self.changed)

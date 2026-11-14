"""/routes: the owner's routes per role and their fallback order (the routing policy, <storage>/serve/routing.json)."""
import asyncio

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Select, Static, TabbedContent, TabPane, TextArea

from .console_panels import SupportPanel
from .console_route_matrix import RouteMatrix

ROLE_LABELS = {'implementer': 'Implementer · conversations, sessions and the units they delegate',
               'planner': 'Planner', 'auditor': 'Your auditor routes · used when you choose your list',
               'researcher': 'Researcher · searches the internet; explorers investigate project files',
               'vision': 'Vision · describes attached images for models that cannot see',
               'heads_up': 'Heads-up · writes a note after a reply that follows delivered or attempted work, and by the counts tare keeps or every few turns otherwise'}
AUDITOR_SELECTIONS = [('Automatic · another provider, frontier models', 'cross_vendor_frontier'),
                      ('Use my auditor routes', 'configured')]


def as_text(tiers):
    """One tier per line, its routes separated by commas; a flat list (planner, auditor, vision) one per line."""
    return '\n'.join(', '.join(t) if isinstance(t, list) else t for t in tiers)


def from_text(text):
    return [[n.strip() for n in line.split(',') if n.strip()] for line in text.splitlines() if line.strip()]


class RoutesPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    RoutesPanel #routes-scroll { height: 1fr; }
    RoutesPanel .routes-label { height: auto; margin-top: 1; }
    RoutesPanel TextArea { height: 6; }
    RoutesPanel #routes-status { height: auto; max-height: 4; }
    RoutesPanel #routes-tabs, RoutesPanel #routes-tabs > ContentSwitcher, RoutesPanel TabPane { height: 1fr; }
    RoutesPanel TabPane { padding: 0; }
    '''

    def compose(self):
        from .turn_engine import ROUTE_ROLES, policy
        value = policy()
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Routes and fallback', classes='panel-title')
                yield Button('Back · Esc', id='routes-close')
            with TabbedContent(id='routes-tabs'):
                with TabPane('Order', id='routes-order'):
                    with VerticalScroll(id='routes-scroll'):
                        yield Static('tare tries the routes in order: the first available one of the first line, then the '
                                     'next lines. A route is skipped when its CLI, credential or quota is missing, or when an '
                                     'image job holds the local GPU (nothing waits for it). One line per tier, routes '
                                     'separated by commas.', classes='routes-label')
                        yield Static('Who reviews plans and code?', classes='routes-label')
                        yield Select(AUDITOR_SELECTIONS, value=value['auditor_selection'], allow_blank=False,
                                     id='routes-auditor-selection')
                        yield Static('Automatic prefers another model provider. It uses enabled frontier connections, '
                                     'subscription quota before metered API, Sol before Astra and Opus before Fable. '
                                     'Google uses Argon. No extra model request is made to choose a reviewer.', classes='routes-label')
                        for role in ROUTE_ROLES:
                            yield Static(ROLE_LABELS[role], classes='routes-label')
                            yield TextArea(as_text(value.get(role) or []), id='routes-' + role,
                                           disabled=role == 'auditor' and value['auditor_selection'] != 'configured')
                        yield Static('Explorers fall back to the overflow explorer of /delegation.', classes='routes-label')
                    yield Static('New sessions use these routes; new reviews use the reviewer choice. Saved reviews keep their model.', id='routes-status',
                                 markup=False)
                    with Horizontal(classes='panel-actions'):
                        yield Button('Save', id='routes-save', classes='panel-primary')
                        yield Button("tare's defaults", id='routes-default')
                with TabPane('Models per role', id='routes-matrix'):
                    yield RouteMatrix()

    def on_mount(self):
        # Derived controls follow the value even while Changed is queued or
        # suppressed during restoration; old notifications cannot undo it.
        self.watch(self.query_one('#routes-auditor-selection', Select), 'value', self.sync_auditor_selection)

    def sync_auditor_selection(self):
        self.query_one('#routes-auditor', TextArea).disabled = (
            self.query_one('#routes-auditor-selection', Select).value != 'configured')

    async def on_button_pressed(self, event):
        event.stop()
        from .turn_engine import ROUTE_ROLES, reset_policy, save_policy
        if event.button.id == 'routes-close':
            self.action_close()
            return
        try:
            if event.button.id == 'routes-save':
                roles = {role: from_text(self.query_one('#routes-' + role, TextArea).text) for role in ROUTE_ROLES}
                value = await asyncio.to_thread(save_policy, roles,
                    auditor_selection=self.query_one('#routes-auditor-selection', Select).value)
                message = 'Saved. New sessions take these routes; new reviews use this reviewer choice.'
            else:
                value = await asyncio.to_thread(reset_policy)
                message = "Back to tare's default routes."
            for role in ROUTE_ROLES:
                self.query_one('#routes-' + role, TextArea).load_text(as_text(value.get(role) or []))
            self.query_one('#routes-auditor-selection', Select).value = value['auditor_selection']
            if self.query_one(RouteMatrix).routes is not None:  # its rows follow these lists
                self.query_one(RouteMatrix).open()
        except (ValueError, OSError) as exc:
            message = str(exc)
        if self.is_mounted:
            self.query_one('#routes-status', Static).update(message)

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == 'routes-matrix':
            self.query_one(RouteMatrix).open()

    def action_close(self):
        if self.query_one(RouteMatrix).leave():
            self.dismiss(None)

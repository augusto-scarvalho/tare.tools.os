"""/fleet: which local models the GPU node serves (tare fleet): the catalog tare.tools qualified first, your own
models, and the default tare auto runs. Saved on the node at once."""
import asyncio

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Select, Static, TextArea

from .console_panels import SupportPanel


def label(card):
    roles = ', '.join(card.get('qualified_for') or [])
    return Text(f"{card['id']} · {card.get('display_name') or ''}" + (f' · {roles}' if roles else '')
                + ('' if card.get('on_node') else ' · not on the node'))


class FleetPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    FleetPanel #fleet-scroll { height: 1fr; }
    FleetPanel .fleet-label { height: auto; margin-top: 1; }
    FleetPanel TextArea { height: 5; }
    FleetPanel #fleet-status { height: auto; max-height: 4; }
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.origin, self.value, self.ids = None, None, []

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Local model fleet', classes='panel-title')
                yield Button('Back · Esc', id='fleet-close')
            yield VerticalScroll(id='fleet-scroll')
            yield Static('Asking the node for its fleet…', id='fleet-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Save', id='fleet-save', classes='panel-primary', disabled=True)

    async def on_mount(self):
        from .fleet import view
        from .native_catalog import local_gateway
        try:
            self.origin = local_gateway()
            value = await asyncio.to_thread(view, self.origin)
        except (OSError, ValueError) as exc:
            if self.is_attached:
                self.query_one('#fleet-status', Static).update(
                    f'The model gateway at {self.origin} did not show its fleet: {type(exc).__name__}.')
            return
        if self.is_attached:  # is_mounted turns true only after on_mount
            await self.show(value, 'The node serves the checked models. Save changes it at once.')

    async def show(self, value, message):
        from .fleet import own_line
        self.value, fleet = value, value.get('fleet') or {}
        picked, custom = set(fleet.get('models') or []), fleet.get('custom') or {}
        self.ids = [card['id'] for card in value.get('catalog') or []]
        names = self.ids + [n for n in custom if n not in self.ids]
        default = value.get('default') if value.get('default') in names else (names or [None])[0]
        rows = [Static('Catalog · the models tare.tools qualified, with what each does well', classes='fleet-label')]
        rows += [Checkbox(label(card), card['id'] in picked, id=f'fleet-model-{i}')
                 for i, card in enumerate(value.get('catalog') or [])]
        rows += [Static('Your own models · one per line: name, the weights\' path on the node, the backend\'s '
                        'arguments', classes='fleet-label'),
                 TextArea('\n'.join(own_line(n, c) for n, c in custom.items()), id='fleet-own'),
                 Static('Default · what tare auto runs locally', classes='fleet-label'),
                 Select([(n, n) for n in names], value=default, allow_blank=False, id='fleet-default')]
        scroll = self.query_one('#fleet-scroll', VerticalScroll)
        await scroll.remove_children()
        await scroll.mount(*rows)
        self.query_one('#fleet-save', Button).disabled = False
        self.query_one('#fleet-status', Static).update(message)

    def chosen(self):
        """The fleet the panel shows: the checked catalog models, own models, the default (served with them)."""
        from .fleet import own_models
        models = [m for i, m in enumerate(self.ids) if self.query_one(f'#fleet-model-{i}', Checkbox).value]
        custom = own_models(self.query_one('#fleet-own', TextArea).text, (self.value.get('fleet') or {}).get('custom'))
        default = self.query_one('#fleet-default', Select).value
        if default in self.ids and default not in models:
            models.append(default)
        return {'default': default, 'models': models, 'custom': custom}

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'fleet-close':
            self.action_close()
            return
        from .fleet import change
        from .gpu import node_token
        try:
            value = await asyncio.to_thread(change, self.origin, node_token(), self.chosen())
        except (OSError, ValueError, SystemExit) as exc:
            if self.is_mounted:
                self.query_one('#fleet-status', Static).update(str(exc))
            return
        if self.is_mounted:
            await self.show(value, 'Saved. The node serves this fleet now; tare auto runs its default.')

    def action_close(self):
        self.dismiss(None)

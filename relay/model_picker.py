"""A searchable model list with dependent reasoning and connection choices."""
from copy import deepcopy

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .console_panels import SupportPanel


class ModelPicker(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Cancel', priority=True)]
    DEFAULT_CSS = '''
    ModelPicker .support-body { width: 76; max-width: 96%; height: 90%; max-height: 30; padding: 1 2; }
    ModelPicker.narrow .support-body { width: 96%; height: 96%; padding: 0 1; }
    ModelPicker #model-search { margin-bottom: 1; }
    ModelPicker #model-list { height: 1fr; min-height: 3; border: none; padding: 0; background: transparent; }
    ModelPicker #model-list > .option-list--option { padding: 0 1; }
    ModelPicker .choice-row { height: 1; margin-top: 1; }
    ModelPicker .choice-row Static { width: 13; height: 1; color: $text-muted; }
    ModelPicker .choice-row Select { width: 1fr; }
    ModelPicker #model-single-connection { width: 1fr; }
    ModelPicker #model-note { height: 2; margin-top: 1; color: $text-muted; }
    ModelPicker #model-empty { height: 1; color: $text-muted; }
    ModelPicker .panel-actions { margin-top: 0; }
    '''

    def __init__(self, choices, *, current=None, preferences=None, reasoning_focus=False, can_continue=False, note=None):
        super().__init__()
        self.choices = deepcopy(choices)
        self.preferences = deepcopy(preferences or {'recent': [], 'models': {}})
        self.chosen = deepcopy(current)
        self.reasoning_focus = reasoning_focus
        self.visible_keys = []
        self.can_continue = can_continue
        self.note = note

    def compose(self):
        with Vertical(classes='support-body'):
            yield Static('Model', classes='panel-title', markup=False)
            yield Input(placeholder='Search models or connections…', id='model-search')
            yield OptionList(id='model-list', markup=False)
            yield Static('No matching models.', id='model-empty', markup=False)
            with Horizontal(classes='choice-row'):
                yield Static('Reasoning', markup=False)
                yield Select([('Profile default', 'default')], value='default',
                             allow_blank=False, id='model-reasoning')
            with Horizontal(classes='choice-row'):
                yield Static('Connection', markup=False)
                yield Select([], id='model-connection', allow_blank=True)
                yield Static('', id='model-single-connection', markup=False)
            yield Static(self.note or ('Apply sets the model for new conversations.\nContinue task reviews a brief for this conversation.' if self.can_continue else
                         'Applies to new conversations.\nSaved conversations keep their settings.'),
                         id='model-note', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Cancel · Esc', id='model-cancel')
                yield Button('Apply · Enter', id='model-apply', classes='panel-primary')
                if self.can_continue:
                    yield Button('Continue task', id='model-continue')

    def on_mount(self):
        self.filter_models()
        target = self.query_one('#model-reasoning' if self.reasoning_focus else '#model-search')
        (self.query_one('#model-search') if target.disabled else target).focus()

    def filter_models(self):
        query = self.query_one('#model-search', Input).value.strip().casefold()
        matches = [r for r in self.choices if query in (
            r['key']+' '+r['label']+' '+r['group']+' '+
            ' '.join(x['label']+' '+x['id']+' '+x.get('model', '') for x in r['routes'])).casefold()]
        recent = self.preferences.get('recent', [])
        ordered = [r for key in recent for r in matches if r['key'] == key]
        ordered += [r for r in matches if r['key'] not in recent]
        rows, keys, prior_group = [], [], None
        for row in ordered:
            group = 'Recent' if row['key'] in recent else row['group']
            if group != prior_group:
                rows.append(Option(Text(group.upper(), style='dim'), disabled=True))
                keys.append(None); prior_group = group
            text = Text(row['label'], no_wrap=True, overflow='ellipsis')
            if len(row['routes']) == 1:
                text.append('  '+row['routes'][0]['label'], style='dim')
            rows.append(Option(text)); keys.append(row['key'])
        self.visible_keys = keys
        menu = self.query_one('#model-list', OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            menu.set_options(rows)
            wanted = (self.chosen or {}).get('key')
            menu.highlighted = keys.index(wanted) if wanted is not None and wanted in keys else next((i for i,k in enumerate(keys) if k), None)
        self.query_one('#model-empty').display = not matches
        self.query_one('#model-apply', Button).disabled = not matches
        if self.can_continue:
            self.query_one('#model-continue', Button).disabled = not matches
        if matches:
            self.choose(keys[menu.highlighted], preserve=wanted == keys[menu.highlighted])
        else:
            self.query_one('#model-reasoning').disabled = True
            self.query_one('#model-connection').disabled = True

    def choose(self, key, *, preserve=False):
        item = next(r for r in self.choices if r['key'] == key)
        prior = self.chosen if preserve and self.chosen else self.preferences.get('models', {}).get(key, {})
        route = next((r for r in item['routes'] if r['id'] == prior.get('connection')), item['routes'][0])
        reasoning = prior.get('reasoning', 'default')
        if reasoning not in ['default', *route['efforts']]:
            reasoning = 'default'
        self.chosen = {'key': key, 'connection': route['id'], 'reasoning': reasoning}
        self.render_choices(item, route)

    def render_choices(self, item, route):
        levels = [e for e in route['efforts'] if e not in {'ultra', 'ultracode'}]
        labels = {'xhigh': 'Extra high'}
        default = route['default_effort']
        options = [('Default · '+labels.get(default, default.capitalize()) if default else 'Profile default', 'default')]
        options += [(labels.get(e, e.capitalize()), e) for e in levels]
        with self.prevent(Select.Changed):
            reasoning = self.query_one('#model-reasoning', Select)
            reasoning.set_options(options); reasoning.value = self.chosen['reasoning']
            # The default ID can stay the same while its label changes between models.
            reasoning.mutate_reactive(Select.value)
            reasoning.disabled = not levels
            connection = self.query_one('#model-connection', Select)
            connection.set_options([(r['label'], r['id']) for r in item['routes']])
            connection.value = route['id']; connection.disabled = False
            connection.display = len(item['routes']) > 1
        single = self.query_one('#model-single-connection', Static)
        single.display = len(item['routes']) == 1
        single.update(route['label'])
        note = self.note or ('Apply sets the model for new conversations.\nContinue task reviews a brief for this conversation.'
                if self.can_continue else 'Applies to new conversations.\nSaved conversations keep their settings.')
        if not self.note and route.get('protocol') in {'anthropic-messages', 'gemini-generate-content'}:
            api = 'Claude API' if route['protocol'] == 'anthropic-messages' else 'Gemini API'
            note = note.split('\n')[0] + '\n' + api + ' returns complete responses, without streaming.'
        if (route.get('openrouter') or {}).get('notice'):
            note = 'Meta may use prompts, code and responses to improve its products.'
        self.query_one('#model-note', Static).update(note)

    def on_input_changed(self, event):
        event.stop()
        if event.input.id == 'model-search':
            self.filter_models()

    def on_input_submitted(self, event):
        event.stop(); self.apply()

    def on_option_list_option_highlighted(self, event):
        event.stop()
        if 0 <= event.option_index < len(self.visible_keys):
            key = self.visible_keys[event.option_index]
            if key and key != (self.chosen or {}).get('key'):
                self.choose(key)

    def on_option_list_option_selected(self, event):
        event.stop()
        key = self.visible_keys[event.option_index]
        if key:
            self.choose(key, preserve=key == (self.chosen or {}).get('key'))

    def on_select_changed(self, event):
        event.stop()
        if not self.chosen or event.value != event.select.value:
            return
        item = next(r for r in self.choices if r['key'] == self.chosen['key'])
        if event.select.id == 'model-connection':
            route = next((r for r in item['routes'] if r['id'] == event.value), None)
            if not route:
                return
            self.chosen['connection'] = route['id']
            if self.chosen['reasoning'] not in ['default', *route['efforts']]:
                self.chosen['reasoning'] = 'default'
            self.render_choices(item, route)
        elif event.select.id == 'model-reasoning':
            self.chosen['reasoning'] = str(event.value)

    def on_key(self, event):
        if event.key == 'enter' and self.focused is self.query_one('#model-list'):
            self.apply()
            event.stop(); event.prevent_default()
            return
        if event.key in {'up', 'down'} and self.focused is self.query_one('#model-search'):
            getattr(self.query_one('#model-list'), 'action_cursor_'+event.key)()
            event.stop(); event.prevent_default()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'model-apply':
            self.apply()
        elif event.button.id == 'model-continue' and self.chosen:
            self.dismiss({**self.chosen, 'continue_task': True})
        else:
            self.action_close()

    def apply(self):
        if self.chosen and not self.query_one('#model-apply', Button).disabled:
            self.dismiss(dict(self.chosen))

    def action_close(self):
        self.dismiss(None)

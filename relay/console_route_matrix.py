"""/routes, "Models per role": which routes automatic routing may use for a role, ticked per model line and source.
It edits the routing profile (`routing_profile`, relay/environment.py) and nothing else; Save writes it."""
import asyncio
import copy
import re
from types import SimpleNamespace

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, OptionList, Select, Static

from . import environment as env

ROLES = {'coordinator': 'Coordinator', 'implementer': 'Implementer', 'explorer': 'Explorer', 'planner': 'Planner',
         'plan-auditor': 'Plan auditor', 'auditor': 'Auditor', 'researcher': 'Researcher', 'vision': 'Vision',
         'heads_up': 'Heads-up'}
CHOICES = {**ROLES, 'coordinator': 'Coordinator · the model you talk to, when tare auto starts the conversation'}
VENDORS = {'claude': 'Anthropic', 'gpt': 'OpenAI', 'gemini': 'Google'}
SOURCE_LABELS = {'claude-code': 'Claude Code', 'codex': 'Codex', 'agy': 'AGY', 'opencode-sub': 'OpenCode sub',
                 'opencode-api': 'OpenCode API', 'api': 'Vendor API', 'openrouter': 'OpenRouter', 'bedrock': 'Bedrock',
                 'local': 'Local', 'other-api': 'Other API'}
PAY_PER_TOKEN = ('opencode-api', 'api', 'openrouter', 'bedrock', 'other-api')
NO_ROUTE = {'coordinator': 'tare auto cannot start a conversation; it stops before any model call with '
                           '"tare_auto_no_route_available: No route can start." (pick a model to talk to one)',
            'implementer': 'tare auto has no route (tare_auto_no_route_available)',
            'explorer': 'only the explorers set in /delegation run; without them a session does not delegate',
            'planner': 'a plan stops before any model call (No eligible planner)',
            'plan-auditor': 'a planner reviews its own plan, and a plan stops when its planner is off for plan-auditor',
            'auditor': 'the change is not audited',
            'researcher': '/research and web search say no route is available',
            'vision': 'attached images get no description', 'heads_up': 'no heads-up note is written'}
LEGEND = ('[x] on   [ ] off by you   ! not usable now: a rule decides, the tick stays yours   ~ a version entry differs'
          '   . not carried   n/a not offered for this role')


def entries(value, side, role):
    """The role's saved entries on one side, as written; [] where the profile cannot be read."""
    saved = value.get('routing_profile')
    texts = saved.get(side) if isinstance(saved, dict) else None
    texts = texts.get(role) if isinstance(texts, dict) else None
    return texts if isinstance(texts, list) else []


def reads(value, role, route, key=None):
    found = env.profile_state(role, route, value, key)
    return 'off' if found and found[0] == 'off' else 'on'


def make(value, role, pattern, want, cells, sources):
    """Make every cell `pattern` targets read `want` for `role` (a cell: line@source or a route id, and its
    [(route, key)]), storing only differences from tare's default. The entries inside the target go; "off" then writes
    `pattern` itself when a targeted route still reads on, and each cell that still disagrees, because of an entry
    outside the target, gets its own entry: its line on its source, then a version or a route id. So an "on" is
    written only where something broader is off. `sources`: {id: source} of routes with no line."""
    rule = env.rule_of(pattern)

    def covered(text):
        inner = env.rule_of(text)
        if isinstance(rule, str):
            return inner == rule
        if isinstance(inner, str):  # a route with no line: only `*@SOURCE` and `*@*` cover it
            return rule[0] is None and rule[3] in ('*', sources.get(inner))
        return inner is not None and rule[3] in ('*', inner[3]) and all(w in (None, h) for w, h in zip(rule[:3], inner))
    value = env.edit_auto(value, role, lambda side, texts: [t for t in texts if not covered(t)])
    if want == 'off' and any(reads(value, role, r) == 'on' for _, routes in cells for r, _ in routes):
        value = env.set_auto(value, f'{role}:{pattern}=off')
    for name, routes in cells:
        if '@' in name and reads(value, role, {}, env.rule_of(name)) != want:
            value = env.set_auto(value, f'{role}:{name}={want}')
        for route, key in routes:
            if reads(value, role, route) != want:
                own = env.key_label(key, bool(key[2])) if key[0] else route['id']
                value = env.set_auto(value, f'{role}:{own}={want}')
    return value


def row_of(route, key):
    return f'{key[0]}/{key[1]}' if key[0] else route['id']


def load():
    """This machine's setup; per role, (name, route, now, key) of each route automatic routing considers; and each
    row's label: a line's from the lines table (Claude Fable), a route with no line by its catalogue label or id."""
    from .model_choices import describe
    from .vendor_models import LINES
    value = env.read()
    routes = {role: [(name, route, now, env.route_key(route)) for name, route, now in env.auto_routes(role) if route]
              for role in env.PROFILE_ROLES}
    labels = {}
    for name, route, _, key in (item for rows in routes.values() for item in rows):
        row = row_of(route, key)
        if row in labels:
            continue
        if key[0]:
            found = LINES.get(f'{key[0]}-{key[1]}')
            labels[row] = ' '.join(re.sub(r'-?\{version\}', '', found['label']).split()) if found else row
        else:
            try:
                labels[row] = describe(route['id'], route)['label'] or row
            except (ValueError, OSError, KeyError, TypeError):
                labels[row] = row
    return value, routes, labels


class Matrix(DataTable):
    BINDINGS = [Binding('space', 'select_cursor', 'Toggle', show=False)]


class RouteMatrix(Vertical):
    DEFAULT_CSS = '''
    RouteMatrix { height: 1fr; }
    RouteMatrix .matrix-bar { height: 1; margin-bottom: 1; }
    RouteMatrix #matrix-role { width: 1fr; }  /* the Coordinator entry reads in full on a wide screen */
    RouteMatrix #matrix-search { width: 28; margin-left: 1; }
    RouteMatrix Matrix { height: 1fr; }
    RouteMatrix .matrix-note { height: auto; color: $text-muted; }
    RouteMatrix OptionList { height: auto; max-height: 5; }
    RouteMatrix #matrix-status { height: auto; max-height: 8; }
    RouteMatrix #matrix-bulk { width: 1fr; }
    RouteMatrix #matrix-reload { display: none; }
    '''

    def compose(self):
        self.value = self.routes = self.saved = None
        self.role, self.warned, self.labels = 'coordinator', False, {}  # what a user meets first
        self.message, self.extra, self.identities, self.warnings = 'Reading the routes…', [], {}, []
        with Horizontal(classes='matrix-bar'):
            yield Select([(label, role) for role, label in CHOICES.items()], value=self.role, allow_blank=False,
                         id='matrix-role')
            yield Input(placeholder='Search models', id='matrix-search')
        yield Matrix(cursor_type='cell', fixed_columns=1, zebra_stripes=True, id='matrix-table')
        yield Static(LEGEND, classes='matrix-note', markup=False)
        yield Static('', id='matrix-extra-label', classes='matrix-note', markup=False)
        yield OptionList(id='matrix-extra')
        yield Static(self.message, id='matrix-status', markup=False)
        with Horizontal(classes='panel-actions'):
            yield Select([], prompt='Bulk action…', id='matrix-bulk')
            yield Button('Reload', id='matrix-reload')
            yield Button('Save', id='matrix-save', classes='panel-primary')

    def open(self):
        """Read the routes each time the tab is shown or the Order tab saves (the order tab never pays for it). Unsaved
        ticks are entries of the edited profile, so they apply to the new rows; one whose cell is gone is listed."""
        self.run_worker(self.load(), group='route-matrix', exclusive=True)

    async def load(self):
        value, routes, labels = await asyncio.to_thread(load)
        if self.value is None:
            self.value, self.saved = value, copy.deepcopy(value.get('routing_profile'))
            self.message = ('This machine\'s setup cannot be read: ' + value['error'] + ' (tare setup --reset starts '
                            'again)' if 'error' in value else 'Space, Enter or a click on the selected cell ticks it.')
        self.routes, self.labels, self.identities = routes, labels, {}  # a route may run another model under its id
        self.sources = {route['id']: key[3] for rows in routes.values() for _, route, _, key in rows if not key[0]}
        self.cells = {role: {} for role in routes}
        for role, rows in routes.items():
            for name, route, now, key in rows:
                self.cells[role].setdefault((row_of(route, key), key[3]), []).append((name, route, now, key))
        self.carried = {cell for found in self.cells.values() for cell in found}
        self.build()

    def state(self, route, key=None, role=None):
        return reads(self.value, role or self.role, route, key)

    def look(self, row, where):
        """(the line's or route's own state, whether a route of it differs, whether one is usable now), or None when
        the role has no route there."""
        routes = self.cells[self.role].get((row, where))
        if not routes:
            return None
        own = self.state({}, (*row.split('/'), None, where)) if '/' in row else self.state(routes[0][1])
        return own, any(self.state(r) != own for _, r, _, _ in routes), any(now == 'usable' for _, _, now, _ in routes)

    def glyph(self, row, where):
        seen = self.look(row, where)
        if seen is None:
            return 'n/a' if (row, where) in self.carried else ' .'
        own, mixed, usable = seen
        return ('[x]' if own == 'on' else '[ ]') + ('' if usable else '!') + ('~' if mixed else '')

    def columns(self):
        return [s for s in env.SOURCES if any(where == s for _, where in self.cells[self.role])]

    def groups(self):
        rows = sorted({row for row, _ in self.cells[self.role]}, key=lambda row: (self.labels.get(row, row), row))
        vendor = lambda row: VENDORS.get(row.split('/')[0], row.split('/')[0].capitalize()) if '/' in row else 'Other'
        order = list(VENDORS.values())
        names = sorted({vendor(r) for r in rows},
                       key=lambda v: (v == 'Other', order.index(v) if v in order else len(order), v))
        return [(v, [r for r in rows if vendor(r) == v]) for v in names]

    def build(self):
        """The table for the role and search: rows are model lines grouped by vendor, then routes with no line. A row
        shows its label; the search also matches its technical name."""
        if self.routes is None:
            return
        table, query = self.query_one(Matrix), self.query_one('#matrix-search', Input).value.strip().lower()
        sources, groups, keep = self.columns(), self.groups(), self.query_one(Matrix).cursor_coordinate
        table.clear(columns=True)  # widths given: the table measures cells only once it next gets a message
        table.add_column('Model', key='model', width=max((len(name) + 1 for vendor, rows in groups for name in (
            vendor, *(self.labels.get(r, r) for r in rows))), default=5))
        for where in sources:
            label = SOURCE_LABELS.get(where, where)
            table.add_column(label, key=where, width=max(len(label), 5))
        for vendor, rows in groups:
            rows = [r for r in rows if any(query in text.lower() for text in (r, self.labels.get(r, r), vendor))]
            if rows:  # names come from routes and files: plain text, never markup
                table.add_row(Text(vendor.upper()), *[''] * len(sources), key='#' + vendor)
                for row in rows:
                    table.add_row(Text(' ' + self.labels.get(row, row)), *[''] * len(sources), key=row)
        table.move_cursor(row=keep.row, column=keep.column, animate=False)
        families = sorted({row.split('/')[0] for row, _ in self.cells[self.role] if '/' in row})
        others = [r for r in ROLES if r != self.role]
        self.query_one('#matrix-bulk', Select).set_options([(Text(label), action) for label, action in (
            [(f'{VENDORS.get(f, f)}: every model {want}', f'scope:{f}/*@*={want}')
             for f in families for want in ('off', 'on')]
            + [(f'{SOURCE_LABELS.get(where, where)}: every model {want}', f'scope:*@{where}={want}')
               for where in sources for want in ('off', 'on')]
            + [('Subscriptions only: every pay-per-token source off', 'subscriptions')]
            + [(f'Copy from {ROLES[r]}', 'copy:' + r) for r in others]
            + [(f"tare's defaults for {ROLES[self.role]}", 'reset')])])
        self.paint()

    def paint(self):
        """Glyphs, the entries no cell draws and the status line, from the edited profile."""
        table = self.query_one(Matrix)
        for row_key in list(table.rows):
            if not row_key.value.startswith('#'):
                for where in self.columns():
                    table.update_cell(row_key, where, Text(self.glyph(row_key.value, where)))  # not markup
        self.extra = [(side, text) for side in ('off', 'on') for text in entries(self.value, side, self.role)
                      if not self.drawn(env.rule_of(text))]
        lines = [*env.profile(self.value, self.role)[1],
                 *(['Entries without a cell (Enter or a click removes one; Save writes it):'] if self.extra else [])]
        label = self.query_one('#matrix-extra-label', Static)
        label.update('\n'.join(lines))
        label.display = bool(lines)
        extra = self.query_one(OptionList)
        extra.clear_options()
        extra.add_options([Text(f'{side:<3}  {text}' if isinstance(text, str) and env.rule_of(text) else
                                f'{side:<3}  {text!r} (cannot be read; ignored)') for side, text in self.extra])
        extra.display = bool(self.extra)
        self.warnings = self.safety()
        self.show()

    def drawn(self, rule):
        """Whether a cell stands for this entry: a line on a named source, or a route with no line, that the role
        has (a rule on the cell is only marked)."""
        if isinstance(rule, str):
            return bool(self.look(rule, self.sources.get(rule)))
        return bool(rule and rule[1] and not rule[2] and rule[3] != '*' and self.look(f'{rule[0]}/{rule[1]}', rule[3]))

    def lineage(self, route, key, role):
        """The identity (model_id, vendor, bias_domain) a route's audit independence is judged by: its line's, from
        the lines table, else its roster identity; None when tare cannot tell."""
        from .vendor_models import LINES
        found = key[0] and LINES.get(f'{key[0]}-{key[1]}')
        if found:
            return SimpleNamespace(model_id='-'.join([key[0], key[1], *map(str, key[2] or ())]),
                                   vendor=found['vendor'], bias_domain=found['bias_domain'])
        if (route['id'], role) not in self.identities:
            from .work_audit import identity
            try:
                self.identities[route['id'], role] = identity(route, role)
            except (ValueError, KeyError, TypeError, OSError):
                self.identities[route['id'], role] = None
        return self.identities[route['id'], role]

    def safety(self):
        """What the edited ticks say, for the role shown and every role with unsaved edits, whatever is shown: certain
        lines from ticks alone (no route ticked on; an implementer ticked on that every auditor ticked on would be
        refused for, or that only auditors of its own vendor would audit, as landing_policy.independence judges) and an
        advisory one when every route ticked on is marked not usable now. None says that a route will or will not
        run."""
        shown = [self.role, *self.edited()]
        pair = bool({'implementer', 'auditor'} & set(shown))
        lines = []
        for role in dict.fromkeys([*shown, *(['auditor'] if pair else [])]):
            ticked = [now for _, route, now, _ in self.routes[role] if self.state(route, role=role) == 'on']
            reasons = sorted(set(ticked) - {'usable'})
            if not ticked:
                lines.append(f'No route is ticked on for {ROLES[role]}: {NO_ROUTE[role]}.')
            elif 'usable' not in ticked:
                lines.append(f'{ROLES[role]}: {len(ticked)} route(s) on, none usable now ('
                             + ', '.join(reasons[:3]) + (', …' if len(reasons) > 3 else '') + ').')
        from .landing_policy import independence  # the rule every audit selection and label uses
        auditors = [self.lineage(r, k, 'auditor') for _, r, _, k in self.routes['auditor']
                    if self.state(r, role='auditor') == 'on'] if pair else []
        unaudited = same_vendor = None
        for _, route, _, key in self.routes['implementer'] if auditors and None not in auditors else []:
            me = self.state(route, role='implementer') == 'on' and self.lineage(route, key, 'implementer')
            if not me:  # off, or an identity tare cannot tell: no alarm either way
                continue
            kinds = {independence(me, auditor) for auditor in auditors}
            name = env.key_label(key) if key[0] else route['id']
            if kinds == {'AUDITOR_NOT_INDEPENDENT'}:
                unaudited = unaudited or name
            elif 'INDEPENDENT' not in kinds:
                same_vendor = same_vendor or name
        if unaudited:
            lines.append(f'No auditor ticked on may audit implementer {unaudited} (same model or line): its changes '
                         'are not audited.')
        if same_vendor:
            lines.append(f'Every auditor ticked on for {same_vendor} is of the same vendor: its audits run, labelled '
                         'not independent.')
        return lines

    def edited(self, value=None, base=None):
        """The roles whose entries in `value` (the edited profile) differ from `base` (the profile as loaded)."""
        value = self.value if value is None else value
        base = {'routing_profile': self.saved} if base is None else base
        return [r for r in env.PROFILE_ROLES if any(entries(value, s, r) != entries(base, s, r) for s in ('off', 'on'))]

    def dirty(self):
        return self.value is not None and self.value.get('routing_profile') != self.saved

    def show(self, message=None):
        if message is not None:
            self.message = message
        lines = [*self.warnings, self.message, *(
            ['Unsaved changes: Save writes them; Esc asks before discarding them.'] if self.dirty() else [])]
        self.query_one('#matrix-status', Static).update('\n'.join(line for line in lines if line))

    def edit(self, change, message, name=None):
        before = copy.deepcopy(self.value)
        try:
            self.value = change(self.value)
        except ValueError as exc:
            self.value = before
            return self.show(str(exc))
        gone = [t for side in ('off', 'on') for t in entries(before, side, self.role)
                if t != name and t not in entries(self.value, side, self.role)]
        self.warned = False
        self.paint()
        self.show(message + (' Removed ' + ', '.join(map(str, gone)) + '.' if gone else '')
                  + (' Save writes it.' if self.dirty() else ' Nothing left to save.'))

    def describe(self, row, where):
        name = f'{row}@{where}' if '/' in row else row
        title = f'{self.labels.get(row, row)} on {SOURCE_LABELS.get(where, where)} ({name})'
        seen = self.look(row, where)
        if seen is None:
            return f'{title}: ' + ('not offered for this role: tare\'s routes for it do not list it.'
                                   if (row, where) in self.carried else 'this source does not carry it here.')
        own, _, usable = seen
        routes = self.cells[self.role][row, where]
        reasons = ', '.join(sorted({now for _, _, now, _ in routes} - {'usable'}))
        lead = 'on' if own == 'on' else 'off by you'
        if not usable:
            lead += (', but not used automatically now: ' if own == 'on' else '; not usable now either: ') + reasons + \
                ' (a tick does not override this rule)'
        return f'{title} {self.glyph(row, where)}: {lead}. ' + '; '.join(
            f'{n} {self.state(r)}, {now}' + (f' (prove it once: tare setup --prove {n})'
                                            if now == 'NOT_QUALIFIED_PIN_ONLY' and k[0] else '')
            for n, r, now, k in routes)

    def targets(self, rule):
        """The role's cells `rule` covers (a line on a source, a vendor's lines, a source, a route id), for make()."""
        found = []
        for (row, where), routes in self.cells[self.role].items():
            name = f'{row}@{where}' if '/' in row else row
            if isinstance(rule, str):
                hit = row == rule
            elif rule[0]:  # a vendor or a line: routes with a line only
                hit = '/' in row and rule[3] in ('*', where) and all(
                    w in (None, h) for w, h in zip(rule[:3], env.rule_of(name)))
            else:  # *@SOURCE or *@*
                hit = rule[3] in ('*', where)
            if hit:
                found.append((name, [(r, k) for _, r, _, k in routes]))
        return found

    def make(self, value, pattern, want):
        return make(value, self.role, pattern, want, self.targets(env.rule_of(pattern)), self.sources)

    def toggle(self, row, where):
        """Any cell the source carries for the role ticks; a rule on it stays marked. Nothing to name, nothing to tick."""
        seen = self.look(row, where)
        if seen is None:
            return self.show(self.describe(row, where))
        pattern = f'{row}@{where}' if '/' in row else row
        want = 'off' if seen[0] == 'on' else 'on'
        self.edit(lambda v: self.make(v, pattern, want), f'{pattern}: {want}.', pattern)
        self.show(self.message + '\n' + self.describe(row, where))  # the tick and, still, any rule on the cell

    def bulk(self, action):
        role = self.role
        if action.startswith('scope:'):
            pattern, _, want = action.removeprefix('scope:').rpartition('=')
            self.edit(lambda v: self.make(v, pattern, want), f'{pattern}: {want}.', pattern)
        elif action == 'subscriptions':
            def off(value):
                for where in PAY_PER_TOKEN:
                    value = self.make(value, f'*@{where}', 'off')
                return value
            self.edit(off, 'Every pay-per-token source is off.')
        elif action.startswith('copy:'):
            other = action.removeprefix('copy:')
            self.edit(lambda v: env.edit_auto(v, role, lambda side, _: list(entries(v, side, other))),
                      f'{ROLES[role]} has the entries of {ROLES[other]}.')
        else:
            self.edit(lambda v: env.reset_auto(v, role), f"{ROLES[role]} is back to tare's defaults.")

    def leave(self):
        """Whether Esc may close: unsaved changes ask once."""
        if self.dirty() and not self.warned:
            self.warned = True
            self.show('Press Esc again to discard the unsaved changes, or Save.')
            return False
        return True

    def on_select_changed(self, event):
        event.stop()
        if event.value is Select.NULL:
            return
        if event.select.id == 'matrix-role':
            self.role = event.value
            self.build()
        elif self.routes is not None:
            event.select.clear()
            self.bulk(event.value)

    def on_input_changed(self, event):
        event.stop()
        self.build()

    def on_data_table_cell_highlighted(self, event):
        event.stop()
        row, where = event.cell_key.row_key.value, event.cell_key.column_key.value
        if not row.startswith('#') and where != 'model':
            self.show(self.describe(row, where))

    def on_data_table_cell_selected(self, event):
        event.stop()
        row, where = event.cell_key.row_key.value, event.cell_key.column_key.value
        if not row.startswith('#') and where != 'model':
            self.toggle(row, where)

    def on_option_list_option_selected(self, event):
        event.stop()
        side, text = self.extra[event.option_index]
        self.edit(lambda v: env.edit_auto(v, self.role, lambda s, texts: [t for t in texts if (s, t) != (side, text)]),
                  f'{side} {text}: removed.', text)

    async def on_button_pressed(self, event):
        event.stop()  # the order tab's handler resets its routes for any other button
        if self.value is None:
            return
        if event.button.id == 'matrix-reload':
            value = await asyncio.to_thread(env.read)
            if self.is_mounted:
                self.value, self.saved, self.warned = value, copy.deepcopy(value.get('routing_profile')), False
                event.button.display = False
                self.paint()
                self.show('Read the saved profile again; the edits made here are gone.')
            return
        if event.button.id != 'matrix-save':
            return
        roles = self.edited()
        if not roles:
            return self.show('Nothing to save.')
        mine, base, clash = copy.deepcopy(self.value), {'routing_profile': copy.deepcopy(self.saved)}, []

        def change(value):
            """Only the roles edited here, over the file as it is under the lock; none when one of them changed there
            since the tab read it."""
            clash.extend(r for r in roles if r in self.edited(value, base))
            if clash:
                return None
            for role in roles:
                value = env.edit_auto(value, role, lambda side, _, role=role: list(entries(mine, side, role)))
            return value
        try:
            value = await asyncio.to_thread(env.update, change)
            message = ('Not saved: the entries of ' + ', '.join(ROLES[r] for r in clash) + ' changed outside this '
                       'screen since it opened. Reload reads them again and drops the edits made here.' if clash else
                       'Saved. New starts use it; tare serve and tare setup read the same file.')
        except (ValueError, OSError) as exc:
            value, message = None, f'Not saved: {exc}'
        if not self.is_mounted:
            return
        self.query_one('#matrix-reload', Button).display = bool(clash)
        if value is not None and not clash:
            self.value, self.saved, self.warned = value, copy.deepcopy(value.get('routing_profile')), False
            self.paint()
        self.show(message)

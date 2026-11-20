"""/routes, "Models per role": which routes automatic routing may use for a role, ticked per model line and source.
It edits the routing profile (`routing_profile`, relay/environment.py) and nothing else; Save writes it."""
import asyncio
import copy
import re
from types import SimpleNamespace

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
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
          '   . not carried   n/a not offered for this role   a tick on a Local model grants it the role')


def entries(value, side, role):
    """The role's saved entries on one side, as written; [] where the profile cannot be read."""
    saved = value.get('routing_profile')
    texts = saved.get(side) if isinstance(saved, dict) else None
    texts = texts.get(role) if isinstance(texts, dict) else None
    return texts if isinstance(texts, list) else []


def reads(value, role, route, key=None, grant=False, *, parsed=None):
    """'on' or 'off'; with `grant` (a local route its row does not give the role) 'on' only where the user granted
    it."""
    if grant:
        return 'on' if env.granted(role, route, value, parsed=parsed) else 'off'
    found = env.profile_state(role, route, value, key, parsed=parsed)
    return 'off' if found and found[0] == 'off' else 'on'


def make(value, role, pattern, want, cells, sources, grants=frozenset(), *, resolve_id=None):
    """Make every cell `pattern` targets read `want` for `role` (a cell: line@source or a route id, and its
    [(route, key)]), storing only differences from tare's default. The entries inside the target go; "off" then writes
    `pattern` itself when a targeted route still reads on, and each cell that still disagrees, because of an entry
    outside the target, gets its own entry: its line on its source, then a version or a route id. So an "on" is
    written only where something broader is off, or as the grant of a local route in `grants` (route ids: off until
    granted). `sources`: {id: source} of routes with no line."""
    id_lookup = resolve_id or env.id_rule

    def rule_fn(text):
        parsed_entry = env.entry(text)
        return parsed_entry and (id_lookup(text) if isinstance(parsed_entry[1], str) else parsed_entry)[1]

    rule = rule_fn(pattern)

    def covered(text):
        inner = rule_fn(text)
        if isinstance(rule, str):
            return inner == rule
        if isinstance(inner, str):  # a route with no line: only `*@SOURCE` and `*@*` cover it
            return rule[0] is None and rule[3] in ('*', sources.get(inner))
        return inner is not None and rule[3] in ('*', inner[3]) and all(w in (None, h) for w, h in zip(rule[:3], inner))
    value = env.edit_auto(value, role, lambda side, texts: [t for t in texts if not covered(t)])
    parsed_profile = None

    def current_parsed():
        nonlocal parsed_profile
        if parsed_profile is None:
            parsed_profile = env.profile(value, role, resolve_id=id_lookup)[0]
        return parsed_profile

    if want == 'off':
        parsed_now = current_parsed()
        if any(reads(value, role, r, grant=r.get('id') in grants, parsed=parsed_now) == 'on'
               for _, routes in cells for r, _ in routes):
            value = env.set_auto(value, f'{role}:{pattern}=off')
            parsed_profile = None
    for name, routes in cells:
        if '@' in name and reads(value, role, {}, rule_fn(name), parsed=current_parsed()) != want:
            value = env.set_auto(value, f'{role}:{name}={want}')
            parsed_profile = None
        for route, key in routes:
            if reads(value, role, route, grant=route.get('id') in grants, parsed=current_parsed()) != want:
                own = env.key_label(key, bool(key[2])) if key[0] else route['id']
                value = env.set_auto(value, f'{role}:{own}={want}')
                parsed_profile = None
    return value


def row_of(route, key):
    return f'{key[0]}/{key[1]}' if key[0] else route['id']


def load():
    """This machine's setup; per role, (name, route, now, key) of each route automatic routing considers; each
    row's label: a line's from the lines table (Claude Fable), a route with no line by its catalogue label or id; and
    per role the ids of the local routes only a grant gives it (no other path reaches them: off until ticked)."""
    from .model_choices import describe
    from .native_catalog import select_cache
    from .native_routes import configured
    from .vendor_models import LINES
    with select_cache():
        value = env.read()
        names = {role: set() for role in env.PROFILE_ROLES}
        table = configured()
        credentials = {}
        routes = {role: [(name, route, now, env.route_key(route))
                         for name, route, now in env.auto_routes(
                             role, names[role], configured_table=table, credentials=credentials)
                         if route] for role in env.PROFILE_ROLES}
        grants = {role: {r['id'] for n, r, *_ in rows if n in names[role]} - {r['id'] for n, r, *_ in rows
                                                                                if n not in names[role]}
                  for role, rows in routes.items()}
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
    return value, routes, labels, grants


def mirrors(routes, *, configured_table=None):
    """{(role, name): row id} of each pin-only route with no line that serves a model the row named already serves
    automatically for that role (native_routes.mirror): a proof admits it as that row's reserve."""
    from .native_routes import configured, mirror
    table = configured_table if configured_table is not None else configured()
    rows = {row.get('native_catalog'): row for row in table['deployments'] if row.get('qualified') is False}
    return {(role, name): found['id'] for role, items in routes.items() for name, route, _, key in items
            if name in rows and not key[0] and (found := mirror(rows[name], route, role))}


def _compute_row_classes(cells):
    """Precompute {(role, row): class_str} in one pass with a single model_signals.reading() and per-route cache."""
    from .model_signals import ORDER, klass, reading, shown
    saved = reading()
    by_id = {}
    classes = {}
    for role, role_cells in cells.items():
        by_row = {}
        for (row_name, _), route_items in role_cells.items():
            found = by_row.setdefault(row_name, set())
            for _, route, _, _ in route_items:
                rid = route.get('id')
                if rid not in by_id:
                    try:
                        by_id[rid] = (klass(route, saved=saved) or {}).get('class')
                    except (ValueError, OSError, KeyError, TypeError):
                        by_id[rid] = None
                found.add(by_id[rid])
        for row_name, found in by_row.items():
            classes[role, row_name] = '/'.join(shown(c) for c in reversed(ORDER) if c in found)
    return classes


class Matrix(DataTable):
    BINDINGS = [Binding('space', 'select_cursor', 'Toggle', show=False)]


class ProveDialog(ModalScreen):
    """Names each route a proof would call and what it costs; Prove is the only way on, Cancel and Esc call nothing."""
    BINDINGS = [Binding('escape', 'cancel', 'Cancel', priority=True)]
    DEFAULT_CSS = '''
    ProveDialog { align: center middle; background: $background 80%; }
    ProveDialog #prove-dialog { width: 90; max-width: 95%; height: auto; max-height: 95%; padding: 1 2;
                                background: $panel; border: round $warning; }
    ProveDialog #prove-lines { height: auto; max-height: 14; overflow-y: auto; margin: 1 0; }
    '''

    def __init__(self, lines):
        super().__init__()
        self.lines = lines

    def compose(self):
        with Vertical(id='prove-dialog'):
            yield Static('Per route, one minimal real call to see that it answers, then one short task (read, edit and '
                         'deliver one file in a throwaway folder) to see that it works in a tare role:', markup=False)
            yield Static('\n'.join(self.lines), id='prove-lines', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Cancel · Esc', id='prove-cancel')
                yield Button('Prove', id='prove-go', classes='panel-primary')

    def on_mount(self):
        self.query_one('#prove-cancel', Button).focus()  # a stray Enter spends nothing

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(event.button.id == 'prove-go')


class RouteMatrix(Vertical):
    BINDINGS = [Binding('p', 'prove', 'Prove the selected cell'),
                Binding('P', 'prove_all', 'Prove all pending')]
    DEFAULT_CSS = '''
    RouteMatrix { height: 1fr; }
    RouteMatrix .matrix-bar { height: 1; margin-bottom: 1; }
    RouteMatrix #matrix-role { width: 1fr; }  /* the Coordinator entry reads in full on a wide screen */
    RouteMatrix #matrix-search { width: 28; margin-left: 1; }
    RouteMatrix Matrix { height: 1fr; }
    RouteMatrix .matrix-note { height: auto; color: $text-muted; }
    RouteMatrix OptionList { height: auto; max-height: 5; }
    RouteMatrix #matrix-status { height: 4; overflow-y: auto; }
    RouteMatrix #matrix-bulk { width: 1fr; min-width: 12; }
    RouteMatrix #matrix-reload { display: none; }
    '''

    def compose(self):
        self.value = self.routes = self.saved = None
        self.role, self.warned, self.labels, self.proving, self.proved = 'coordinator', False, {}, False, {}  # what a user meets first
        self.mirrors, self.grants, self.all_provable = {}, {}, []
        self.message, self.extra, self.identities, self.warnings = 'Reading the routes…', [], {}, []
        self._id_rule_cache, self._rule_cache, self._profile_cache = {}, {}, {}
        self._row_classes, self._task_cache, self._drawn_glyphs, self._last_highlighted = {}, {}, {}, None
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
            yield Button('Prove · p', id='matrix-prove')
            yield Button('Prove all · P', id='matrix-prove-all')
            yield Button('Save', id='matrix-save', classes='panel-primary')

    def open(self):
        """Read the routes each time the tab is shown or the Order tab saves (the order tab never pays for it). Unsaved
        ticks are entries of the edited profile, so they apply to the new rows; one whose cell is gone is listed."""
        self.run_worker(self.load(), group='route-matrix', exclusive=True)

    async def load(self):
        from .model_signals import own_record
        from .native_catalog import select_cache
        existing = self.value

        def load_bundle():
            with select_cache():
                value, routes, labels, grants = load()
                record = own_record()  # each route's outcomes as implementer on this machine
                found_mirrors = mirrors(routes)
                cells = {role: {} for role in routes}
                for role, rows in routes.items():
                    for name, route, now, key in rows:
                        cells[role].setdefault((row_of(route, key), key[3]), []).append((name, route, now, key))
                row_classes = _compute_row_classes(cells)
                provable_candidates = sorted({
                    n for r, items in routes.items() for n, _, now, k in items
                    if now == 'NOT_QUALIFIED_PIN_ONLY' and (k[0] or (r, n) in found_mirrors)
                })
                all_provable = [n for n in provable_candidates if env.route_for(n)[0] is not None]
                id_rules = {}
                active = value if existing is None else existing
                for role in env.PROFILE_ROLES:
                    for side in ('off', 'on'):
                        for text in entries(active, side, role):
                            parsed_entry = env.entry(text)
                            if parsed_entry and isinstance(parsed_entry[1], str) and text not in id_rules:
                                id_rules[text] = env.id_rule(text)
            return value, routes, labels, grants, record, found_mirrors, cells, row_classes, all_provable, id_rules

        (value, routes, labels, self.grants, self.record, self.mirrors,
         self.cells, self._row_classes, self.all_provable, id_rules) = await asyncio.to_thread(load_bundle)
        if self.value is None:
            self.value, self.saved = value, copy.deepcopy(value.get('routing_profile'))
            self.message = ('This machine\'s setup cannot be read: ' + value['error'] + ' (tare setup --reset starts '
                            'again)' if 'error' in value else 'Space, Enter or a click on the selected cell ticks it.')
        self.routes, self.labels, self.identities = routes, labels, {}  # a route may run another model under its id
        self._id_rule_cache = id_rules
        self._rule_cache.clear()
        self._profile_cache.clear()
        self._task_cache.clear()
        self._drawn_glyphs.clear()
        self._last_highlighted = None
        self.sources = {route['id']: key[3] for rows in routes.values() for _, route, _, key in rows if not key[0]}
        self.carried = {cell for found in self.cells.values() for cell in found}
        self.build()

    def _id_rule(self, text):
        if text not in self._id_rule_cache:
            self._id_rule_cache[text] = env.id_rule(text)
        return self._id_rule_cache[text]

    def _rule(self, text):
        if not isinstance(text, str):
            return None
        if text not in self._rule_cache:
            parsed = env.entry(text)
            self._rule_cache[text] = parsed and (self._id_rule(text) if isinstance(parsed[1], str) else parsed)[1]
        return self._rule_cache[text]

    def _parsed_profile(self, role):
        if 'error' in self.value:
            return env.profile(self.value, role, resolve_id=self._id_rule)
        rp = self.value.get('routing_profile')
        if rp is not None:
            if not isinstance(rp, dict) or set(rp) - {'off', 'on'}:
                return env.profile(self.value, role, resolve_id=self._id_rule)
            for side in ('off', 'on'):
                roles = rp.get(side, {})
                if not isinstance(roles, dict) or not isinstance(roles.get(role, []), list):
                    return env.profile(self.value, role, resolve_id=self._id_rule)
        try:
            key = (role, tuple(entries(self.value, 'off', role)), tuple(entries(self.value, 'on', role)))
        except TypeError:
            return env.profile(self.value, role, resolve_id=self._id_rule)
        cached = self._profile_cache.get(key)
        if cached is None:
            if len(self._profile_cache) > 64:
                self._profile_cache.clear()
            cached = env.profile(self.value, role, resolve_id=self._id_rule)
            self._profile_cache[key] = cached
        return cached

    def state(self, route, key=None, role=None):
        role = role or self.role
        return reads(self.value, role, route, key, route.get('id') in self.grants.get(role, ()),
                     parsed=self._parsed_profile(role)[0])

    def look(self, row, where):
        """(the line's or route's own state, whether a route of it differs, whether one is usable now), or None when
        the role has no route there."""
        routes = self.cells[self.role].get((row, where))
        if not routes:
            return None
        own = self.state({}, (*row.split('/'), None, where)) if '/' in row else self.state(routes[0][1])
        return own, any(self.state(r) != own for _, r, _, _ in routes), any(now == 'usable' for _, _, now, _ in routes)

    def klass(self, row):
        """The class of a row's model as `tare models` gives it (two when its routes differ), '' with no line."""
        cached = self._row_classes.get((self.role, row))
        if cached is not None:
            return cached
        from .model_signals import ORDER, klass, shown
        found = set()
        for (name, _), routes in self.cells[self.role].items():
            for _, route, _, _ in routes if name == row else ():
                try:
                    found.add((klass(route) or {}).get('class'))
                except (ValueError, OSError, KeyError, TypeError):
                    pass  # a route with no model to read has no class
        value = '/'.join(shown(c) for c in reversed(ORDER) if c in found)
        self._row_classes[self.role, row] = value
        return value

    def task(self, route):
        rid = route.get('id')
        if rid in self._task_cache:
            return self._task_cache[rid]
        from .model_signals import task
        try:
            value = task(route)
        except (ValueError, OSError, KeyError, TypeError):
            value = None  # a route with no model to read has no figure
        if rid is not None:
            self._task_cache[rid] = value
        return value

    def named(self, row):
        kind = self.klass(row)
        return self.labels.get(row, row) + (' · ' + kind if kind else '')

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
        named = {r: self.named(r) for _, rows in groups for r in rows}
        self._drawn_glyphs.clear()
        self._last_highlighted = None
        table.clear(columns=True)  # widths given: the table measures cells only once it next gets a message
        table.add_column('Model', key='model', width=max((len(name) + 1 for vendor, rows in groups for name in (
            vendor, *(named[r] for r in rows))), default=5))
        for where in sources:
            label = SOURCE_LABELS.get(where, where)
            table.add_column(label, key=where, width=max(len(label), 5))
        for vendor, rows in groups:
            rows = [r for r in rows if any(query in text.lower() for text in (r, named[r], vendor))]
            if rows:  # names come from routes and files: plain text, never markup
                table.add_row(Text(vendor.upper()), *[''] * len(sources), key='#' + vendor)
                for row in rows:
                    table.add_row(Text(' ' + named[row]), *[''] * len(sources), key=row)
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

    def paint(self, *, show=True):
        """Glyphs, the entries no cell draws and the status line, from the edited profile."""
        table, cols = self.query_one(Matrix), self.columns()
        for row_key in list(table.rows):
            if not row_key.value.startswith('#'):
                for where in cols:
                    glyph = self.glyph(row_key.value, where)
                    cell = (row_key.value, where)
                    if self._drawn_glyphs.get(cell) != glyph:
                        table.update_cell(row_key, where, Text(glyph))  # not markup
                        self._drawn_glyphs[cell] = glyph
        self.extra = [(side, text) for side in ('off', 'on') for text in entries(self.value, side, self.role)
                      if not self.drawn(self._rule(text))]
        lines = [*self._parsed_profile(self.role)[1],
                 *(['Entries without a cell (Enter or a click removes one; Save writes it):'] if self.extra else [])]
        label = self.query_one('#matrix-extra-label', Static)
        label.update('\n'.join(lines))
        label.display = bool(lines)
        extra = self.query_one(OptionList)
        extra.clear_options()
        extra.add_options([Text(f'{side:<3}  {text}' if self._rule(text) else
                                f'{side:<3}  {text!r} (cannot be read; ignored)') for side, text in self.extra])
        extra.display = bool(self.extra)
        self.warnings = self.safety()
        self._last_highlighted = None
        if show:
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
        self.query_one('#matrix-prove', Button).disabled = self.proving or not self.provable()
        self.query_one('#matrix-prove-all', Button).disabled = self.proving or not self.provable_all()

    def edit(self, change, message, name=None, *, show=True):
        before = copy.deepcopy(self.value)
        try:
            self.value = change(self.value)
        except ValueError as exc:
            self.value = before
            return self.show(str(exc))
        gone = [t for side in ('off', 'on') for t in entries(before, side, self.role)
                if t != name and t not in entries(self.value, side, self.role)]
        self.warned = False
        self.paint(show=False)
        self.message = (message + (' Removed ' + ', '.join(map(str, gone)) + '.' if gone else '')
                        + (' Save writes it.' if self.dirty() else ' Nothing left to save.'))
        if show:
            self.show()

    def describe(self, row, where):
        name = f'{row}@{where}' if '/' in row else row
        title = f'{self.labels.get(row, row)} on {SOURCE_LABELS.get(where, where)} ({name})'
        seen = self.look(row, where)
        if seen is None:
            return f'{title}: ' + ('not offered for this role: tare\'s routes for it do not list it.'
                                   if (row, where) in self.carried else 'this source does not carry it here.')
        own, _, usable = seen
        routes = self.cells[self.role][row, where]
        from .model_signals import describe_record, describe_task
        reasons = ', '.join(sorted({now for _, _, now, _ in routes} - {'usable'}))
        lead = 'on' if own == 'on' else 'off by you'
        if all(r['id'] in self.grants.get(self.role, ()) for _, r, _, _ in routes):  # a local model: the tick grants
            lead = ('granted by you' if own == 'on' else
                    'not granted: its row does not give it this role; a tick grants it')
        if not usable:
            lead += (', but not used automatically now: ' if own == 'on' else '; not usable now either: ') + reasons + \
                ' (a tick does not override this rule)'
        return f'{title} {self.glyph(row, where)}: {lead}. ' + '; '.join(
            f'{n} {self.state(r)}, {now}' + (' (press p to prove it once)' if self.liftable(n, now, k) else '')
            + (f' ({self.proved[n]})' if n in self.proved else '')
            + (f' ({cost})' if (cost := describe_task(self.task(r))) else '')
            + (f' (as implementer here: {did})' if (did := describe_record(self.record.get(n))) else '')
            for n, r, now, k in routes)

    def cursor_cell(self):
        table = self.query_one(Matrix)
        if self.routes is None or not table.row_count or not table.columns:
            return None
        found = table.coordinate_to_cell_key(table.cursor_coordinate)
        return found.row_key.value, found.column_key.value

    def liftable(self, name, now, key, role=None):
        """Whether a proof would lift a route: pin only, of a known model line or a mirror (mirrors())."""
        role = self.role if role is None else role
        return now == 'NOT_QUALIFIED_PIN_ONLY' and bool(key[0] or (role, name) in self.mirrors)

    def provable(self):
        """(route, key) of the selected cell's routes a proof would lift (liftable)."""
        cell = self.cursor_cell()
        return cell and [(n, k) for n, _, now, k in self.cells[self.role].get(cell, []) if self.liftable(n, now, k)]

    def provable_all(self):
        """Sorted list of unique liftable, available pin-only route names across all roles."""
        return list(self.all_provable)

    def action_prove(self):
        """Confirm, then one real call per provable route of the selected cell. Starts only here, from the user."""
        found = self.provable()
        if self.proving:
            return self.show('A proof is already running.')
        if not found:
            return self.show('Nothing to prove in this cell: only a pin-only route of a known model line, or of a model '
                             'another route already serves automatically, can be.')
        self.run_worker(self.confirm_prove([n for n, _ in found]), group='route-matrix-prove')

    def action_prove_all(self):
        """Confirm, then prove every pending pin-only route currently available across all roles."""
        if self.proving:
            return self.show('A proof is already running.')
        names = self.provable_all()
        if not names:
            return self.show('Nothing to prove: no pending pin-only routes are available here.')
        self.run_worker(self.confirm_prove(names, from_all=True), group='route-matrix-prove')

    async def confirm_prove(self, names, *, from_all=False):
        def inspect():  # the line `environment.prove` will ask about, said before anything is spent
            lines, bound, unavailable = [], [], []
            for name in names:
                deployment, why = env.route_for(name)
                if deployment is None:
                    lines.append(f'{name}: cannot be proven: {why}')
                    unavailable.append((name, why))
                else:
                    cost = env.cost_line(deployment)
                    lines.append(f'{name}: {cost}')
                    bound.append((name, env.prove_binding(deployment, cost)))
            return lines, bound, unavailable
        text, bound, unavailable = await asyncio.to_thread(inspect)
        if not self.is_mounted:
            return
        if not bound:
            reason = '; '.join(f'{name}: {why}' for name, why in unavailable)
            return self.show(f'Nothing to prove: {reason}' if reason else
                             'Nothing to prove: no pending pin-only routes are available here.')
        self.app.push_screen(ProveDialog(text), lambda yes: yes and self.run_worker(
            self.prove(bound, skipped=unavailable, from_all=from_all), group='route-matrix-prove'))

    async def prove(self, items, *, skipped=(), from_all=False):
        role, cell, self.proving, said = self.role, self.cursor_cell(), True, []
        try:
            for i, (name, binding) in enumerate(items, 1):
                self.show(f'Proving {name} ({i} of {len(items)}): one minimal call, then a short task…')
                # prove_bound checks that model, account, and cost still match what ProveDialog showed
                try:
                    record = await asyncio.to_thread(env.prove_bound, name, binding)
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    record = {'ok': False, 'reason': str(exc)}
                said.append((name, record))
            await self.load()  # the rows now read the proofs; unsaved ticks are entries of self.value and stay
        finally:
            self.proving = False
        if not self.is_mounted:
            return
        statuses = {}
        for rows in self.routes.values():
            for n, _, status, _ in rows:
                statuses.setdefault(n, set()).add(status)
        for name, why in skipped:
            self.proved[name] = f'not proven: {why}'
        for name, record in said:  # kept for describe(): the reload moves the cursor, which rewrites the status line
            if record.get('ok') is None:
                self.proved[name] = f"skipped: {record.get('reason') or 'not confirmed'}"
                continue
            failed_task = (record.get('role_check') or {}).get('ok') is False
            lifted = any(s != 'NOT_QUALIFIED_PIN_ONLY' for s in statuses.get(name, ()))
            mirror_target = next((self.mirrors[r, name] for r in self.routes if (r, name) in self.mirrors), None)
            self.proved[name] = 'proved here: ' + (
                f"answered, but failed the role check: {record['role_check'].get('reason')}; it stays pin only"
                if failed_task else
                f"failed: {record.get('reason') or 'no reason given'}" if record.get('ok') is not True else
                'answered and did the task; used automatically where it is ticked on' + (
                    f', as a reserve after {mirror_target}' if mirror_target else '')
                if lifted else
                'answered and did the task, but it stays pin only (a local model, or a reseller\'s that no other route '
                'serves automatically, is not proven by this)')
        if not from_all and not skipped and cell and not cell[0].startswith('#') and cell[1] != 'model':
            self.show(self.describe(*cell))
            return
        after_cell = self.cursor_cell()
        if after_cell and not after_cell[0].startswith('#') and after_cell[1] != 'model':
            # Keep build()/load()'s queued CellHighlighted event from overwriting the batch proof summary.
            self._last_highlighted = after_cell
        passed = sum(1 for _, rec in said if rec.get('ok') is True)
        changed = [n for n, rec in said if rec.get('ok') is None]
        failed = [n for n, rec in said if rec.get('ok') is False]
        if len(said) == 1 and not skipped:
            name, _ = said[0]
            self.show(f'{name}: {self.proved[name]}.')
        elif passed == len(said) and not skipped:
            self.show(f'Proved {passed} of {len(said)} route(s).')
        else:
            parts = [f'Proved {passed} of {len(said)} route(s)']
            if changed:
                parts.append(f"skipped (changed after confirmation): {', '.join(changed[:3])}"
                             + ('…' if len(changed) > 3 else ''))
            if failed:
                parts.append(f"failed: {', '.join(failed[:3])}" + ('…' if len(failed) > 3 else ''))
            if skipped:
                parts.append(f'{len(skipped)} unavailable before confirmation')
            self.show('; '.join(parts) + '.')

    def targets(self, rule):
        """The role's cells `rule` covers (a line on a source, a vendor's lines, a source, a route id), for make()."""
        found = []
        for (row, where), routes in self.cells[self.role].items():
            name = f'{row}@{where}' if '/' in row else row
            if isinstance(rule, str):
                hit = row == rule
            elif rule[0]:  # a vendor or a line: routes with a line only
                hit = '/' in row and rule[3] in ('*', where) and all(
                    w in (None, h) for w, h in zip(rule[:3], self._rule(name)))
            else:  # *@SOURCE or *@*
                hit = rule[3] in ('*', where)
            if hit:
                found.append((name, [(r, k) for _, r, _, k in routes]))
        return found

    def make(self, value, pattern, want):
        return make(value, self.role, pattern, want, self.targets(self._rule(pattern)), self.sources,
                    self.grants.get(self.role, frozenset()), resolve_id=self._id_rule)

    def toggle(self, row, where):
        """Any cell the source carries for the role ticks; a rule on it stays marked. Nothing to name, nothing to tick."""
        seen = self.look(row, where)
        if seen is None:
            return self.show(self.describe(row, where))
        pattern = f'{row}@{where}' if '/' in row else row
        want = 'off' if seen[0] == 'on' else 'on'
        self.edit(lambda v: self.make(v, pattern, want), f'{pattern}: {want}.', pattern, show=False)
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
            cell = (row, where)
            if self._last_highlighted == cell:
                return
            self._last_highlighted = cell
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
        if event.button.id == 'matrix-prove':
            return self.action_prove()
        if event.button.id == 'matrix-prove-all':
            return self.action_prove_all()
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

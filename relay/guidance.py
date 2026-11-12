"""/guidance: the owner's edits of tare's role guidance (the prompt capsules) and choices about the blocks each vendor
CLI adds on its own (docs/guides/GUIDANCE.md).

Choices live in <storage>/serve/guidance.json: {'schema', 'capsules': {capsule id: {text field: choice}},
'vendor': {endpoint: {item id: choice}}}, where a choice is {'value': None | text, 'base': {'sha256', 'text'}} and
the base is the default it was made against. When the default changes underneath (a tare update, a CLI update) the
panel shows base -> new default and the owner decides; until then tare keeps sending the owner's version. What each
CLI adds is read when the TUI opens and kept in <storage>/serve/vendor-guidance.json, with one previous text per
item: without tokens at each start, and with one small real call when a CLI version is new. Where nothing is saved,
tare sends what it sent before, except the blocks tare leaves out by default (TARE_DEFAULTS), each only where a
reading of the installed CLI proved it can be left out.

The same file keeps the owner's variables ('variables': {name: text}) and common texts ('common': {id: {'on',
'value'?, 'base'?}}): texts tare adds to its own system text for every route, written once. A text uses a variable as
<name>; the built-in ones are filled on each invocation. Files written before have neither key and stay valid."""
import datetime
import hashlib
import json
import os
import platform
import re
import time

SCHEMA = 'tare.tools/guidance/1'
READING_SCHEMA = 'tare.tools/vendor-guidance-reading/1'
READ_ENDPOINTS = ('cli://claude', 'cli://codex', 'cli://agy')  # AGY: only its version without tokens
# The routes whose CLI makes the real reading, cheapest first (Codex needs none: its zero-token reading matched a
# real call). The call runs at low effort and costs a few hundred tokens, once per CLI version.
REAL_ROUTES = {'cli://claude': ('claude-haiku-5-5-cli', 'claude-sonnet-5-5-cli'),
               'cli://agy': ('agy-cli', 'agy-gemini-4-argon-cli')}
# tare's own defaults for the blocks a CLI adds (None: left out), with why. Applied only where the startup reading
# proved the installed version leaves the block out: an installed copy that updates by itself may run a CLI that
# ignores the switch, and the Kernel then refuses every call. The owner can send a block again from /guidance.
TARE_DEFAULTS = {'cli://codex': {'skills_instructions': None}}
DEFAULT_REASONS = {'cli://codex': {'skills_instructions': (
    'it lists every skill installed on this machine (about 16,000 characters here), and tare turns Codex\'s tools off, '
    'so the model cannot open them; tare will have its own skills and its own way of calling them.')}}
# Variables tare fills itself, with what each stands for; the names are the ones the vendor texts show.
BUILT_IN_VARIABLES = {'date': "today's date", 'local time': 'the date and time here, with the time zone',
                      'OS version': "this machine's operating system", 'working directory': "the session's project folder",
                      'model id': "the session model's id", 'model name': "the session model's name in tare"}
VARIABLE = re.compile(r'<([^<>\n]{1,40})>')
# The built-in common text: off by default, lean. While it is on, tare leaves out the vendor blocks that say the same
# (Codex's only where the startup reading proved it), as with TARE_DEFAULTS; AGY's cannot be left out.
CONTEXT_DEFAULT = "Today's date is <date>. You are <model name> (model id <model id>)."
CONTEXT_REPLACES = {'cli://claude': ('attachment:environment', 'attachment:model', 'attachment:date'),
                    'cli://codex': ('environment_context',)}
CONTEXT_REASON = "the common text \"Context\", which is on, gives the same facts in tare's words to every CLI."
CONTEXT_OVERLAP = {'cli://agy': ('request:ADDITIONAL_METADATA', 'request:USER_SETTINGS_CHANGE')}
TEXT_FIELDS = {'system': ('role text', 'system text of each new session'),
               'system_append': ('added to the role text', 'end of the system text of each new session'),
               'instruction_append': ('added to the first message', 'end of the first message of each new session')}


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _file(name):
    from .native_installation import storage_root
    return storage_root()/'serve'/name


def path():
    return _file('guidance.json')


def readings_path():
    return _file('vendor-guidance.json')


def _read(file, schema):
    value = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {'schema': schema}
    if value.get('schema') != schema:
        raise ValueError(f'{file} is not a {schema} file.')
    return value


def _write(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, file)


def load():
    saved = _read(path(), SCHEMA)
    return {'schema': SCHEMA, **{k: saved.get(k) or {} for k in ('capsules', 'vendor', 'variables', 'common')}}


def _save(value):
    value = {**value, 'capsules': {k: v for k, v in value['capsules'].items() if v},
             'vendor': {k: v for k, v in value['vendor'].items() if v},
             'common': {k: v for k, v in value['common'].items() if v}}
    if value['capsules'] or value['vendor'] or value['variables'] or value['common']:
        # Variables and common texts are written only when there are some: a file without them reads as before.
        _write(path(), {k: v for k, v in value.items() if v or k in ('capsules', 'vendor')})
    else:
        path().unlink(missing_ok=True)  # nothing of the owner's left: every default applies


def fill(text, found):
    """Each exact known <name> replaced by its value, in one pass: any other angle-bracket text stays, and a value
    is not read again."""
    return VARIABLE.sub(lambda m: found.get(m[1], m[0]), text)


def values(*, folder=None, model_id=None, model_name=None, owner=None):
    """What each variable stands for now: the owner's (saved, or frozen with a session), then the built-in ones from
    the clock, this machine, the session's folder and model. A built-in name the caller cannot know stays as written."""
    now = datetime.datetime.now().astimezone()
    built = {'date': now.strftime('%Y-%m-%d'), 'local time': now.isoformat(timespec='minutes'),
             'OS version': platform.platform()}
    built.update({k: str(v) for k, v in (('working directory', folder), ('model id', model_id),
                                         ('model name', model_name or model_id)) if v})
    return {**(load()['variables'] if owner is None else owner), **built}


def common_texts():
    """The common texts that are on, as a new session freezes them: [{'id', 'text', 'sha256'}]."""
    common, found = load()['common'], []
    for key in dict.fromkeys(['context', *common]):
        entry = common.get(key) or {}
        text = entry.get('value') or (CONTEXT_DEFAULT if key == 'context' else '')
        if entry.get('on') and text.strip():
            found.append({'id': key, 'text': text, 'sha256': digest(text)})
    return found


def readings():
    """{endpoint: {'cli', 'cli_version', 'read_at', 'note', 'items': {id: {..., 'sha256', 'previous'?, 'new'?}},
    'error'?, 'real'?: the same for the last real reading, with its 'usage'}} as last read."""
    return {k: v for k, v in _read(readings_path(), READING_SCHEMA).items() if k != 'schema'}


def _failure(exc):
    return {'error': f'{type(exc).__name__}: {exc}'[:300], 'failed_at': time.strftime('%Y-%m-%d %H:%M')}


def _merge(last, value):
    """A reading as stored: each item's sha256, when and on which CLI version it was last seen, its earlier text when
    it changed (one level, no archive) and `new` when the last reading of the same kind lacked it. A block the newest
    reading lacks stays, marked `absent`, with its controls: some come and go (Claude Code's tokens-left reminder was
    in five real readings and missing from a sixth), and the owner can still leave one out. The list only grows with
    distinct block ids, which are few."""
    now, known, items = time.strftime('%Y-%m-%d %H:%M'), last.get('items'), {}
    for item in value['items']:
        old, sha = (known or {}).get(item['id']), digest(item['text'])
        previous = ({'text': old['text'], 'sha256': old['sha256']} if old and old['sha256'] != sha
                    else (old or {}).get('previous'))
        items[item['id']] = {**item, 'controls': list(item['controls']), 'sha256': sha, 'seen_at': now,
                             'seen_version': value.get('cli_version'), **({'previous': previous} if previous else {}),
                             **({'new': True} if known is not None and old is None else {})}
    for key, old in (known or {}).items():
        if key not in items:  # seen before, not now
            items[key] = {**{k: v for k, v in old.items() if k != 'new'}, 'absent': True,
                          'seen_at': old.get('seen_at') or last.get('read_at'),
                          'seen_version': old.get('seen_version') or last.get('cli_version')}
    return {**{k: value[k] for k in ('cli', 'cli_version', 'note', 'usage') if k in value},
            'read_at': now, 'items': items}


def starting_point():
    """What AGY sent in tare's first real reading of it, shown until a reading of the installed AGY exists."""
    from compute_plane.vendor_guidance import AGY_STARTING_POINT as start
    return {**start, 'starting_point': True, 'items': {
        i['id']: {**i, 'controls': [], 'sha256': digest(i['text'])} for i in start['items']}}


def _read_real(endpoint, last):
    """One small real call on the cheapest ready route of this CLI (a few hundred tokens), or the last real reading
    with why this one failed (a spent quota, no ready route)."""
    from compute_plane.vendor_guidance import read_real
    from .turn_engine import available
    try:
        deployment = next((d for d in (available(name, probe=False)[0] for name in REAL_ROUTES[endpoint]) if d), None)
        if deployment is None:
            raise ValueError('no ready route of this CLI')
        model = deployment['native_profile']['model']
        if endpoint == 'cli://agy':
            model = re.sub(r'-(high|medium)$', '-low', model)  # the route's model at its low tier: low effort
        value = read_real(endpoint, deployment['native_cli']['executable'], model=model)
    except Exception as exc:  # the last real reading stays, with why this one failed
        return {**(last or {}), **_failure(exc)}
    # Compared with the last real reading; AGY's first one, with the starting point it replaces.
    known = last if (last or {}).get('items') is not None else starting_point() if endpoint == 'cli://agy' else {}
    return _merge(known, value)


def refresh_readings(endpoints=READ_ENDPOINTS):
    """Read what each installed CLI adds now, in the background when the TUI opens: without tokens each time, and with
    one small real call when the CLI's version differs from that of its last real reading (Claude Code, AGY), never
    more than once per CLI per start. A CLI that cannot be read keeps its last reading with the error."""
    from compute_plane.vendor_guidance import read
    from .cli_discovery import locate
    stored = readings()
    for endpoint in (e for e in endpoints if e in READ_ENDPOINTS):
        executable = locate(endpoint)
        if executable is None:
            continue
        last = stored.get(endpoint) or {}
        try:
            value = read(endpoint, executable)
        except Exception as exc:  # a reading is a convenience: the last one stays, with why this one failed
            stored[endpoint] = {**last, **_failure(exc)}
            continue
        stored[endpoint] = {**_merge(last, value), **({'real': last['real']} if 'real' in last else {})}
        if endpoint in REAL_ROUTES and (last.get('real') or {}).get('cli_version') != value['cli_version']:
            stored[endpoint]['real'] = _read_real(endpoint, last.get('real'))
        if endpoint in PROVED:
            stored[endpoint]['defaults'] = _prove_defaults(read, endpoint, executable, value)
    _write(readings_path(), {'schema': READING_SCHEMA, **stored})
    return stored


# The CLIs whose removals by tare are proved at the startup reading, and which: its defaults and what Context stands
# for, whether Context is on or not, so turning it on needs no new reading.
PROVED = {'cli://codex': {**TARE_DEFAULTS['cli://codex'], **dict.fromkeys(CONTEXT_REPLACES['cli://codex'])}}


def _prove_defaults(read, endpoint, executable, plain):
    """One more zero-token reading, with tare's removals as choices: a removal is proved on this CLI version when its
    block is in the plain reading and not in this one."""
    found = {item['id'] for item in plain['items']}
    try:
        left = {item['id'] for item in read(endpoint, executable, choices=PROVED[endpoint])['items']}
    except Exception as exc:  # unproved: the block is still sent, and the panel says why
        return {'cli_version': plain['cli_version'], 'proved': [], **_failure(exc)}
    return {'cli_version': plain['cli_version'],
            'proved': sorted(key for key in PROVED[endpoint] if key in found and key not in left)}


def _proved(endpoint, reading=None):
    """tare's removals proved for the CLI version of the stored reading; none before any reading."""
    reading = readings().get(endpoint) or {} if reading is None else reading
    defaults = reading.get('defaults') or {}
    return set(defaults.get('proved', ())) if defaults.get('cli_version') == reading.get('cli_version') else set()


def _wanted(endpoint, saved, context=None):
    """The blocks tare would leave out of this CLI, with why: its defaults, and what Context stands for when the call
    carries Context (by default: while Context is on, as the panel shows it)."""
    wanted = dict(DEFAULT_REASONS.get(endpoint, {}))
    if (saved['common'].get('context') or {}).get('on') if context is None else context:
        wanted.update(dict.fromkeys(CONTEXT_REPLACES.get(endpoint, ()), CONTEXT_REASON))
    return wanted


def _applied(endpoint, reading, wanted):
    """Which of those tare does leave out: on a CLI whose removals are proved (Codex), the proved ones; elsewhere
    (Claude Code), those the latest reading found, as the Kernel refuses a Claude too old to be read."""
    able = (_proved(endpoint, reading) if endpoint in PROVED else
            {k for k, v in (reading.get('items') or {}).items() if not v.get('absent')})
    return {key for key in wanted if key in able}


def _blocks(reading):
    """The blocks shown for one CLI: the zero-token reading's, then those only a real call showed; for AGY, which
    has no zero-token reading, the real reading's or, until one exists, the starting point's. Each says its source."""
    real = reading.get('real') or {}
    if reading.get('cli') == 'AGY' and not real.get('items'):
        real = starting_point()
    seen = {False: 'Seen only in a real call: tare\'s reading without tokens does not show it.',
            True: 'Read from AGY\'s own record of a real call.'}[reading.get('cli') == 'AGY']
    if real.get('starting_point'):
        seen = (f'From what AGY {real["cli_version"]} sent on {real["read_at"]}: tare has not read your AGY in a '
                'real call yet.')
    local, found = reading.get('items') or {}, (real.get('items') or {}).items()
    shown = {k: {**v, 'seen': ''} for k, v in local.items() if not v.get('absent')}
    shown.update({k: {**v, 'seen': seen} for k, v in found if not v.get('absent') and k not in shown})
    gone = {}
    for key, item in [*local.items(), *found]:  # in neither of the latest readings: as last seen, by either
        if key not in shown and (key not in gone or item.get('seen_at', '') > gone[key].get('seen_at', '')):
            gone[key] = {**item, 'seen': (f'Not in the latest reading: last seen {item.get("seen_at")} on '
                                          f'{reading.get("cli")} {item.get("seen_version")}.')}
    return {**shown, **gone}


def capsule_edits():
    return {cid: {f: c['value'] for f, c in fields.items()} for cid, fields in load()['capsules'].items()}


def vendor_choices(endpoint, *, context=False):
    """The owner's choices for one vendor CLI, for VendorCliClient(vendor_guidance=...); {} when none. tare's defaults
    are added, and the removals Context stands for only when this call carries the Context text (`context`: a session
    that froze Context on): a call must never lose the CLI's blocks without Context in their place. A choice the
    Kernel cannot apply stops the call with the fix first: the session shows it as its blocked reason, which keeps
    256 characters and some views cut much shorter."""
    from compute_plane.vendor_guidance import validate
    every = load()
    saved = every['vendor'].get(endpoint, {})
    # A keep is the vendor's own text. The owner's variables are filled in a replacement here; the mod fills the
    # vendor's own values (<date>, ...) from the text Claude Code wrote.
    choices = {k: c['value'] if c['value'] is None else fill(c['value'], every['variables'])
               for k, c in saved.items() if not c.get('keep')}
    choices.update(dict.fromkeys(k for k in _applied(endpoint, readings().get(endpoint) or {},
                                                     _wanted(endpoint, every, context)) if k not in saved))
    try:
        return validate(endpoint, choices)
    except ValueError as exc:
        raise ValueError(f'Open /guidance and choose "Reset everything": the saved guidance for {endpoint} does not '
                         f'fit this tare ({exc}).') from None


def _capsules():
    from .native_agent import PROMPT_CAPSULES
    return json.loads(PROMPT_CAPSULES.read_text(encoding='utf-8'))['capsules']


def _state(choice, default, new=False):
    if choice is None:
        return 'new' if new else 'default'
    if choice['base']['sha256'] != digest(default):
        return 'changed'  # the default moved under the owner's choice: they decide
    return 'removed' if choice['value'] is None else 'yours'


def _row(item_id, owner, choice, default, **rest):
    return {'id': item_id, 'owner': owner, 'default': default, 'state': _state(choice, default, rest.pop('new', False)),
            'text': default if choice is None or choice['value'] is None else choice['value'],
            'base': choice['base']['text'] if choice else None, **rest}


def items():
    """Every text the panel lists, each with its `group`: tare's role guidance ('role', editable), the blocks each
    vendor CLI adds as last read (its endpoint, with the controls the CLI allows, in the order they are sent) and
    tare's transport protocol texts ('protocol', read-only)."""
    from compute_plane.vendor_guidance import controls, transport_texts
    saved, read = load(), readings()
    rows = _common_rows(saved) + _variable_rows(saved)
    for capsule in _capsules():
        scope = ', '.join(capsule[k] for k in ('kind', 'deployment', 'endpoint') if k in capsule)
        for field, (label, sent_as) in TEXT_FIELDS.items():
            if field in capsule:
                rows.append(_row(f'role#{capsule["id"]}#{field}', 'tare',
                                 saved['capsules'].get(capsule['id'], {}).get(field), capsule[field],
                                 controls=('edit',), sent_as=sent_as, effect='new sessions', group='role',
                                 title=f'{capsule["role"].replace("*", "every role")}'
                                       f'{" (" + scope + ")" if scope else ""} · {label}'))
    for endpoint in dict.fromkeys([*read, *saved['vendor']]):
        reading, choices = read.get(endpoint) or {}, saved['vendor'].get(endpoint, {})
        found, wanted = _blocks(reading), _wanted(endpoint, saved)
        applied = _applied(endpoint, reading, wanted)
        for key in dict.fromkeys([*found, *choices]):
            item, choice = found.get(key), choices.get(key)
            keep = bool(choice and choice.get('keep'))  # the owner sends again a block tare leaves out by default
            # A block the last reading no longer has defaults to nothing; one never read, to what the owner approved.
            default = (item['text'] if item else '' if reading.get('items') is not None or keep
                       else choice['base']['text'])
            row = _row(f'vendor#{endpoint}#{key}', 'vendor', None if keep else choice, default,
                       new=bool(item and item.get('new')),
                       controls=tuple(item['controls']) if item else controls(endpoint, key),
                       title=item['title'] if item else key, group=endpoint,
                       absent=bool(item and item.get('absent')),
                       sent_as=item['sent_as'] if item else 'no longer sent by this CLI',
                       previous=(item.get('previous') or {}).get('text') if item else None,
                       seen=item['seen'] if item else '', effect='the next turn', note=reading.get('note', ''),
                       tare_default=wanted.get(key), tare_proved=key in applied,
                       overlap=bool((saved['common'].get('context') or {}).get('on'))
                       and key in CONTEXT_OVERLAP.get(endpoint, ()),
                       cli=f'{reading.get("cli", endpoint)} {reading.get("cli_version") or ""}'.strip())
            if keep:
                row['state'] = 'kept'
            elif choice is None and row['tare_default'] and row['tare_proved']:
                row['state'] = 'tare-default'
            rows.append(row)
    for text in transport_texts():
        rows.append(_row(f'transport#{text["id"]}', 'tare', None, text['text'], controls=(), title=text['title'],
                         sent_as=f'tare\'s protocol text for {text["endpoint"]}', effect=None, group='protocol'))
    return rows


def _common_rows(saved):
    """The common texts: Context (tare's, off by default) and the owner's, each with whether it is on."""
    rows = []
    for key in dict.fromkeys(['context', *saved['common']]):
        entry, mine = saved['common'].get(key) or {}, key != 'context'
        choice = None if mine or 'value' not in entry else entry
        row = _row(f'common#{key}', 'you' if mine else 'tare', choice, entry.get('value', '') if mine else CONTEXT_DEFAULT,
                   controls=('edit',), group='common', on=bool(entry.get('on')), effect='new sessions',
                   title=f'{"Context" if not mine else key} · {"on" if entry.get("on") else "off"}',
                   sent_as='end of the system text of each new session, for every CLI and API route')
        rows.append({**row, 'state': 'own'} if mine else row)
    return rows


def _variable_rows(saved):
    """The variables: the built-in ones with their value now (read-only), then the owner's."""
    found = values(owner={})
    rows = [_row(f'variable#{name}', 'tare', None, found.get(name, f'filled per session: {what}'), controls=(),
                 group='variables', title=f'<{name}> · {what}', sent_as=f'{what}, wherever a text uses <{name}>',
                 effect=None) for name, what in BUILT_IN_VARIABLES.items()]
    rows += [{**_row(f'variable#{name}', 'you', None, text, controls=('edit',), group='variables',
                     title=f'<{name}>', sent_as=f'wherever a text uses <{name}>', effect='new sessions'),
              'state': 'own'} for name, text in saved['variables'].items()]
    return rows


def create(kind, name):
    """A new variable or common text of the owner's, empty and (a common text) off; its name is how texts use it."""
    name, value = name.strip(), load()
    if not re.fullmatch(r'[\w .-]{1,40}', name) or name in BUILT_IN_VARIABLES or name == 'context':
        raise ValueError('Use a name of letters, digits, spaces, dots or dashes, not one tare already uses.')
    group = value['variables' if kind == 'variable' else 'common']
    if name in group:
        raise ValueError(f'"{name}" exists already.')
    group[name] = '' if kind == 'variable' else {'on': False, 'value': ''}
    _save(value)
    return f'{"variable" if kind == "variable" else "common"}#{name}'


def delete(item_id):
    layer, key = item_id.split('#')
    value = load()
    value['variables' if layer == 'variable' else 'common'].pop(key, None)
    _save(value)


def toggle(item_id):
    """A common text on or off; one that is on reaches new sessions."""
    key, value = item_id.split('#')[1], load()
    entry = value['common'].setdefault(key, {})
    entry['on'] = not entry.get('on')
    _save(value)


def news():
    """One line for the TUI's status area when a default changed under a choice or a CLI added a block; '' if not."""
    rows = [r for r in items() if r['state'] in {'changed', 'new'}]
    return (f'{len(rows)} guidance text(s) changed under your choice or new from a CLI: open /guidance to review.'
            if rows else '')


def _default(item_id):
    layer, owner_key, key = item_id.split('#')
    if layer == 'role':
        capsule = next((c for c in _capsules() if c['id'] == owner_key), {})
        if key not in TEXT_FIELDS or key not in capsule:
            raise ValueError('This role text cannot be edited.')
        return capsule[key]
    if layer == 'vendor':
        item = _blocks(readings().get(owner_key) or {}).get(key)
        if item is None:
            raise ValueError('tare has not read this block from its CLI; open the TUI again to read it.')
        return item['text']
    raise ValueError('tare\'s protocol texts are read-only.')


def save(item_id, text):
    """Save one item against its current default: a role text (empty or tare's own means tare's default), or a vendor
    block's replacement (None leaves it out; the vendor's own text means its default). The Kernel refuses what the
    CLI cannot do. A common text or a variable of the owner's keeps the text as written."""
    if item_id.startswith(('common#', 'variable#')):
        return _save_own(item_id, text)
    layer, owner_key, key = item_id.split('#')
    default, value = _default(item_id), load()
    group = value['capsules' if layer == 'role' else 'vendor'].setdefault(owner_key, {})
    if text == default or layer == 'role' and (text is None or not text.strip()):
        group.pop(key, None)
    else:
        if layer == 'vendor':
            from compute_plane.vendor_guidance import validate
            validate(owner_key, {key: text})  # ValueError: a control this CLI does not offer, or an empty text
        group[key] = {'value': text, 'base': {'sha256': digest(default), 'text': default}}
    _save(value)


def _save_own(item_id, text):
    layer, key = item_id.split('#')
    value = load()
    if layer == 'variable':
        if key not in value['variables']:
            raise ValueError('tare fills its own variables.')
        value['variables'][key] = text or ''
    elif key != 'context':
        value['common'].setdefault(key, {})['value'] = text or ''
    else:  # Context: empty or tare's own text means tare's default
        entry = value['common'].setdefault('context', {})
        entry.pop('value', None), entry.pop('base', None)
        if text and text.strip() and text != CONTEXT_DEFAULT:
            entry.update(value=text, base={'sha256': digest(CONTEXT_DEFAULT), 'text': CONTEXT_DEFAULT})
    _save(value)


def keep(item_id):
    """Keep the owner's version over a changed default: the choice is now made against the new default."""
    if item_id == 'common#context':
        value = load()
        value['common']['context']['base'] = {'sha256': digest(CONTEXT_DEFAULT), 'text': CONTEXT_DEFAULT}
        return _save(value)
    layer, owner_key, key = item_id.split('#')
    value = load()
    choice = value['capsules' if layer == 'role' else 'vendor'][owner_key][key]
    try:
        default = _default(item_id)
    except ValueError:
        default = ''  # the block is no longer sent: the choice stays, against nothing
    choice['base'] = {'sha256': digest(default), 'text': default}
    _save(value)


def send_again(item_id):
    """Send a block tare leaves out by default as the CLI writes it: a keep, never passed to the Kernel as a choice.
    reset(item_id) takes it back to tare's default."""
    _, endpoint, key = item_id.split('#')
    value = load()
    value['vendor'].setdefault(endpoint, {})[key] = {'keep': True}
    _save(value)


def reset(item_id=None):
    """One item back to its default (tare's or the vendor's), or every item when none is named: Context off with
    tare's text again. The owner's own variables and common texts stay; they have no default to go back to."""
    value = load()
    if item_id is None:
        _save({'schema': SCHEMA, 'capsules': {}, 'vendor': {}, 'variables': value['variables'],
               'common': {k: v for k, v in value['common'].items() if k != 'context'}})
        return
    if item_id == 'common#context':
        return _save_own(item_id, None)
    layer, owner_key, key = item_id.split('#')
    value['capsules' if layer == 'role' else 'vendor'].get(owner_key, {}).pop(key, None)
    _save(value)

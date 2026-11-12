"""/guidance: the owner's edits of tare's role guidance (the prompt capsules) and choices about the blocks each vendor
CLI adds on its own (docs/guides/GUIDANCE.md).

Choices live in <storage>/serve/guidance.json: {'schema', 'capsules': {capsule id: {text field: choice}},
'vendor': {endpoint: {item id: choice}}}, where a choice is {'value': None | text, 'base': {'sha256', 'text'}} and
the base is the default it was made against. When the default changes underneath (a tare update, a CLI update) the
panel shows base -> new default and the owner decides; until then tare keeps sending the owner's version. What each
CLI adds is read when the TUI opens and kept in <storage>/serve/vendor-guidance.json, with one previous text per
item. Where nothing is saved, tare sends exactly what it sent before."""
import hashlib
import json
import os
import time

SCHEMA = 'tare.tools/guidance/1'
READING_SCHEMA = 'tare.tools/vendor-guidance-reading/1'
READ_ENDPOINTS = ('cli://claude', 'cli://codex')  # AGY cannot be read
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
    return {'schema': SCHEMA, 'capsules': saved.get('capsules') or {}, 'vendor': saved.get('vendor') or {}}


def _save(value):
    value = {**value, 'capsules': {k: v for k, v in value['capsules'].items() if v},
             'vendor': {k: v for k, v in value['vendor'].items() if v}}
    if value['capsules'] or value['vendor']:
        _write(path(), value)
    else:
        path().unlink(missing_ok=True)  # nothing of the owner's left: every default applies


def readings():
    """{endpoint: {'cli', 'cli_version', 'read_at', 'note', 'items': {id: {..., 'sha256', 'previous'?, 'new'?}},
    'error'?}} as last read."""
    return {k: v for k, v in _read(readings_path(), READING_SCHEMA).items() if k != 'schema'}


def refresh_readings(endpoints=READ_ENDPOINTS):
    """Read what each installed CLI adds now (no tokens; the TUI does this once per start, in the background). An item
    whose text changed keeps the old one as `previous`; one the last reading lacked is `new`. A CLI that cannot be
    read keeps its last reading with the error."""
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
            stored[endpoint] = {**last, 'error': f'{type(exc).__name__}: {exc}'[:300],
                                'failed_at': time.strftime('%Y-%m-%d %H:%M')}
            continue
        items = {}
        for item in value['items']:
            old = (last.get('items') or {}).get(item['id'])
            sha = digest(item['text'])
            previous = ({'text': old['text'], 'sha256': old['sha256']} if old and old['sha256'] != sha
                        else (old or {}).get('previous'))
            items[item['id']] = {**item, 'controls': list(item['controls']), 'sha256': sha,
                                 **({'previous': previous} if previous else {}),
                                 **({'new': True} if last.get('items') is not None and old is None else {})}
        stored[endpoint] = {'cli': value['cli'], 'cli_version': value['cli_version'], 'note': value['note'],
                            'read_at': time.strftime('%Y-%m-%d %H:%M'), 'items': items}
    _write(readings_path(), {'schema': READING_SCHEMA, **stored})
    return stored


def capsule_edits():
    return {cid: {f: c['value'] for f, c in fields.items()} for cid, fields in load()['capsules'].items()}


def vendor_choices(endpoint):
    """The owner's choices for one vendor CLI, for VendorCliClient(vendor_guidance=...); {} when none. A choice the
    Kernel cannot apply stops the call with the fix first: the session shows it as its blocked reason, which keeps
    256 characters and some views cut much shorter."""
    from compute_plane.vendor_guidance import validate
    choices = {k: c['value'] for k, c in load()['vendor'].get(endpoint, {}).items()}
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
    """Every text the panel lists: tare's role guidance (editable), the blocks each vendor CLI adds as last read
    (with the controls the CLI allows) and tare's transport protocol texts (read-only)."""
    from compute_plane.vendor_guidance import controls, transport_texts
    saved, read = load(), readings()
    rows = []
    for capsule in _capsules():
        scope = ', '.join(capsule[k] for k in ('kind', 'deployment', 'endpoint') if k in capsule)
        for field, (label, sent_as) in TEXT_FIELDS.items():
            if field in capsule:
                rows.append(_row(f'role#{capsule["id"]}#{field}', 'tare',
                                 saved['capsules'].get(capsule['id'], {}).get(field), capsule[field],
                                 controls=('edit',), sent_as=sent_as, effect='new sessions',
                                 title=f'{capsule["role"].replace("*", "every role")}'
                                       f'{" (" + scope + ")" if scope else ""} · {label}'))
    for endpoint in dict.fromkeys([*read, *saved['vendor']]):
        reading, choices = read.get(endpoint) or {}, saved['vendor'].get(endpoint, {})
        found = reading.get('items') or {}
        for key in dict.fromkeys([*found, *choices]):
            item, choice = found.get(key), choices.get(key)
            # A block the last reading no longer has defaults to nothing; one never read, to what the owner approved.
            default = item['text'] if item else '' if reading.get('items') is not None else choice['base']['text']
            rows.append(_row(f'vendor#{endpoint}#{key}', 'vendor', choice, default, new=bool(item and item.get('new')),
                             controls=tuple(item['controls']) if item else controls(endpoint, key),
                             title=f'{reading.get("cli", endpoint)} · {item["title"] if item else key}',
                             sent_as=item['sent_as'] if item else 'no longer sent by this CLI',
                             previous=(item.get('previous') or {}).get('text') if item else None,
                             effect='the next turn', note=reading.get('note', '')))
    for text in transport_texts():
        rows.append(_row(f'transport#{text["id"]}', 'tare', None, text['text'], controls=(), title=text['title'],
                         sent_as=f'tare\'s protocol text for {text["endpoint"]}', effect=None))
    return rows


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
        item = ((readings().get(owner_key) or {}).get('items') or {}).get(key)
        if item is None:
            raise ValueError('tare has not read this block from its CLI; open the TUI again to read it.')
        return item['text']
    raise ValueError('tare\'s protocol texts are read-only.')


def save(item_id, text):
    """Save one item against its current default: a role text (empty or tare's own means tare's default), or a vendor
    block's replacement (None leaves it out; the vendor's own text means its default). The Kernel refuses what the
    CLI cannot do."""
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


def keep(item_id):
    """Keep the owner's version over a changed default: the choice is now made against the new default."""
    layer, owner_key, key = item_id.split('#')
    value = load()
    choice = value['capsules' if layer == 'role' else 'vendor'][owner_key][key]
    try:
        default = _default(item_id)
    except ValueError:
        default = ''  # the block is no longer sent: the choice stays, against nothing
    choice['base'] = {'sha256': digest(default), 'text': default}
    _save(value)


def reset(item_id=None):
    """One item back to its default (tare's or the vendor's), or every item when none is named."""
    if item_id is None:
        _save({'schema': SCHEMA, 'capsules': {}, 'vendor': {}})
        return
    layer, owner_key, key = item_id.split('#')
    value = load()
    value['capsules' if layer == 'role' else 'vendor'].get(owner_key, {}).pop(key, None)
    _save(value)

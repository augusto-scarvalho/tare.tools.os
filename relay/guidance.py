"""/guidance: the owner's edits of tare's role guidance (the prompt capsules) and choices about the blocks each vendor
CLI adds on its own, kept in <storage>/serve/guidance.json. Where nothing is saved, tare's default and the vendor's
apply, and tare sends exactly what it sent before (docs/guides/GUIDANCE.md)."""
import json
import os

SCHEMA = 'tare.tools/guidance/1'
TEXT_FIELDS = {'system': ('role text', 'system text of each new session'),
               'system_append': ('added to the role text', 'end of the system text of each new session'),
               'instruction_append': ('added to the first message', 'end of the first message of each new session')}


def path():
    from .native_installation import storage_root
    return storage_root()/'serve'/'guidance.json'


def load():
    """{'schema', 'capsules': {capsule id: {text field: text}}, 'vendor': {endpoint: {item id: None | text}}}."""
    file = path()
    saved = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {'schema': SCHEMA}
    if saved.get('schema') != SCHEMA:
        raise ValueError(f'{file} is not a {SCHEMA} file.')
    return {'schema': SCHEMA, 'capsules': saved.get('capsules') or {}, 'vendor': saved.get('vendor') or {}}


def capsule_edits():
    return load()['capsules']


def vendor_choices(endpoint):
    """The owner's choices for one vendor CLI, for VendorCliClient(vendor_guidance=...); {} when none."""
    return load()['vendor'].get(endpoint, {})


def _write(value):
    file = path()
    value = {**value, 'capsules': {k: v for k, v in value['capsules'].items() if v},
             'vendor': {k: v for k, v in value['vendor'].items() if v}}
    if not value['capsules'] and not value['vendor']:
        file.unlink(missing_ok=True)  # nothing of the owner's left: every default applies
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, file)


def items():
    """Every text the panel lists: tare's role guidance (editable), the blocks each vendor CLI adds (as measured,
    with the controls the CLI allows) and tare's transport protocol texts (read-only)."""
    from compute_plane.vendor_guidance import catalog, transport_texts
    from .native_agent import PROMPT_CAPSULES
    saved = load()
    rows = []
    for capsule in json.loads(PROMPT_CAPSULES.read_text(encoding='utf-8'))['capsules']:
        scope = ', '.join(capsule[k] for k in ('kind', 'deployment', 'endpoint') if k in capsule)
        for field, (label, sent_as) in TEXT_FIELDS.items():
            if field in capsule:
                edit = saved['capsules'].get(capsule['id'], {}).get(field)
                rows.append({'id': f'role#{capsule["id"]}#{field}', 'owner': 'tare', 'controls': ('edit',),
                             'title': f'{capsule["role"].replace("*", "every role")}'
                                      f'{" (" + scope + ")" if scope else ""} · {label}',
                             'sent_as': sent_as, 'default': capsule[field], 'text': edit or capsule[field],
                             'state': 'yours' if edit else 'default', 'effect': 'new sessions'})
    for endpoint, entry in catalog().items():
        version = f'{entry["cli"]} {entry["measured_version"]}' if entry['measured_version'] else entry['cli']
        for item in entry['items']:
            choices = saved['vendor'].get(endpoint, {})
            chosen = choices.get(item['id'])
            rows.append({'id': f'vendor#{endpoint}#{item["id"]}', 'owner': 'vendor', 'controls': item['controls'],
                         'title': f'{entry["cli"]} · {item["title"]}', 'sent_as': item['sent_as'],
                         'default': item['text'], 'text': item['text'] if chosen is None else chosen,
                         'state': 'default' if item['id'] not in choices else 'removed' if chosen is None else 'yours',
                         'effect': 'the next turn', 'measured': version, 'note': entry['note']})
    for text in transport_texts():
        rows.append({'id': f'transport#{text["id"]}', 'owner': 'tare', 'controls': (), 'title': text['title'],
                     'sent_as': f'tare\'s protocol text for {text["endpoint"]}', 'default': text['text'],
                     'text': text['text'], 'state': 'default', 'effect': None})
    return rows


def save(item_id, text):
    """Save one item: a role text (empty or tare's own means tare's default), or a vendor block's replacement
    (None leaves it out; the vendor's own text means the vendor's default). The Kernel refuses what the CLI cannot do."""
    layer, owner_key, key = item_id.split('#')
    value = load()
    if layer == 'role':
        from .native_agent import PROMPT_CAPSULES
        capsule = next((c for c in json.loads(PROMPT_CAPSULES.read_text(encoding='utf-8'))['capsules']
                        if c['id'] == owner_key), {})
        if key not in TEXT_FIELDS or key not in capsule:
            raise ValueError('This role text cannot be edited.')
        fields = value['capsules'].setdefault(owner_key, {})
        if text is None or not text.strip() or text == capsule[key]:
            fields.pop(key, None)
        else:
            fields[key] = text
    elif layer == 'vendor':
        from compute_plane.vendor_guidance import catalog, validate
        item = next((i for i in catalog().get(owner_key, {}).get('items', []) if i['id'] == key), None)
        if item is None:
            raise ValueError('tare does not list this vendor block.')
        choices = value['vendor'].setdefault(owner_key, {})
        if text == item['text']:
            choices.pop(key, None)
        else:
            validate(owner_key, {key: text})  # ValueError: a control this CLI does not offer, or an empty text
            choices[key] = text
    else:
        raise ValueError('tare\'s protocol texts are read-only.')
    _write(value)


def reset(item_id=None):
    """One item back to its default (tare's or the vendor's), or every item when none is named."""
    value = load()
    if item_id is None:
        _write({'schema': SCHEMA, 'capsules': {}, 'vendor': {}})
        return
    layer, owner_key, key = item_id.split('#')
    group = value['capsules'] if layer == 'role' else value['vendor'] if layer == 'vendor' else {}
    group.get(owner_key, {}).pop(key, None)
    _write(value)

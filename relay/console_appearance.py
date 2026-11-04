"""Small appearance preference stored alongside external console history."""
import json
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular, save_json


APPEARANCES = (('tare-dark', 'Dark'), ('tare-light', 'Light'),
               ('tare-contrast', 'High contrast'))
SCHEMA = 'tare.tools/console-appearance/1'


def read_appearance(root):
    file = regular(Path(root)/'appearance.json')
    try:
        with file.open('rb') as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return 'tare-dark'
    if len(raw) > 4096:
        raise ValueError('Appearance preference exceeds its size limit.')
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'schema', 'theme'}
            or value['schema'] != SCHEMA
            or not isinstance(value['theme'], str)
            or value['theme'] not in dict(APPEARANCES)):
        raise ValueError('Appearance preference uses an unsupported format or theme.')
    return value['theme']


def save_appearance(root, theme):
    if not isinstance(theme, str) or theme not in dict(APPEARANCES):
        raise ValueError('Unknown console appearance.')
    root = regular(root)
    root.mkdir(parents=True, exist_ok=True)
    with custody_lock(root):
        # Do not replace an unreadable or newer preference format implicitly.
        read_appearance(root)
        save_json(root/'appearance.json', {'schema': SCHEMA, 'theme': theme})

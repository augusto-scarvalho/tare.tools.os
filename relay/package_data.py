"""Canonical schema bytes in source checkouts and installed distributions."""
from importlib.resources import files
from pathlib import Path


def schema_text(name):
    if Path(name).name != name or not name.endswith('.schema.json'):
        raise ValueError('invalid_schema_resource')
    try:
        root = files('tare_os_schemas')
    except ModuleNotFoundError:
        root = Path(__file__).resolve().parents[1]/'schemas'
    return root.joinpath(name).read_text(encoding='utf-8')

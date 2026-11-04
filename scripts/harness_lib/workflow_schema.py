#!/usr/bin/env python3
"""DW.2: stdlib schema walker that activates workflow.json as the typed IR.

The workflow packet is validated against `schemas/workflow.schema.json` at plan
time, BEFORE it is written to disk, so a malformed field fails loudly (naming its
JSON path) instead of propagating a silently-wrong IR into the fan-out.

Scope is DELIBERATELY only the subset that workflow.schema.json actually uses:
`type` / `required` / `enum` / `pattern` / `additionalProperties`, recursing via
`properties` / `items`. A full jsonschema vendor stays deferred — harness.py's
optional-dependency `validate_json_schema` path owns draft-2020-12 coverage when
the library happens to be installed.

ponytail: numeric/length bounds (minimum/maximum/minLength/minItems) are NOT
enforced — none gate the zero-migration invariant (ignoring a bound only widens
acceptance, never rejects a real packet) and no writer emits an out-of-range
value. Add them here if a bound ever needs teeth.

Self-check: `python testing/scenarios/dw_workflow_schema.py`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:  # package import from harness.py
    from .common import HarnessError, read_json
except ImportError:  # standalone (self-check adds scripts/ to sys.path)
    from harness_lib.common import HarnessError, read_json

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Walk `instance` against `schema`, raising HarnessError at the FIRST violation
    with its JSON path. Covers type/required/enum/pattern/additionalProperties plus
    properties/items recursion — the subset workflow.schema.json uses."""
    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else list(types)
        if not any(_TYPE_CHECKS.get(t, lambda v: True)(instance) for t in allowed):
            raise HarnessError(f"{path}: expected type {allowed}, got {type(instance).__name__}")
    if "enum" in schema and instance not in schema["enum"]:
        raise HarnessError(f"{path}: {instance!r} not in enum {schema['enum']}")
    if "pattern" in schema and isinstance(instance, str) and not re.search(schema["pattern"], instance):
        raise HarnessError(f"{path}: {instance!r} does not match pattern {schema['pattern']!r}")
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise HarnessError(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    raise HarnessError(f"{path}.{key}: additional property not allowed")
        for key, value in instance.items():
            if key in props:
                validate(value, props[key], f"{path}.{key}")
    elif isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, elem in enumerate(instance):
            validate(elem, schema["items"], f"{path}[{i}]")


def validate_packet(packet: dict[str, Any], schema_path: Path) -> None:
    """Load the workflow schema from disk and validate `packet` against it."""
    schema = read_json(schema_path, {}) or {}
    validate(packet, schema, "$")

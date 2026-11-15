"""Minimal JSON-schema validator (the subset the memory contracts use). Returns a list of error strings."""
from __future__ import annotations

from typing import Any, List

_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is_type(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(value: Any, schema: dict, path: str = "$") -> List[str]:
    errs: List[str] = []
    if "const" in schema and value != schema["const"]:
        errs.append("%s: expected const %r" % (path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r not in enum" % (path, value))
    if "anyOf" in schema and not any(not validate(value, s, path) for s in schema["anyOf"]):
        errs.append("%s: matches no anyOf branch" % path)
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, x) for x in types):
            return errs + ["%s: expected %s, got %s" % (path, "/".join(types), type(value).__name__)]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                errs.append("%s: missing required %r" % (path, r))
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append("%s: unexpected property %r" % (path, k))
        for k, sub in props.items():
            if k in value:
                errs += validate(value[k], sub, "%s.%s" % (path, k))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append("%s: more than %d items" % (path, schema["maxItems"]))
        if "items" in schema:
            for i, v in enumerate(value):
                errs += validate(v, schema["items"], "%s[%d]" % (path, i))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append("%s: shorter than %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append("%s: longer than %d" % (path, schema["maxLength"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("%s: below minimum" % path)
        if "maximum" in schema and value > schema["maximum"]:
            errs.append("%s: above maximum" % path)
    return errs

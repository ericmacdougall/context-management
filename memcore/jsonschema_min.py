"""Minimal JSON-schema validator with an EXPLICIT supported subset (stated here, enforced by check_schema):

  type (string | list of: string integer number boolean object array null), properties, required,
  additionalProperties (true or false only), items (one schema), minItems, maxItems, minLength, maxLength,
  minimum, maximum, enum, const, anyOf, and annotations (title, description, default, examples, $comment).
Anything else ($ref, $defs, oneOf, allOf, not, pattern, format, exclusive*, uniqueItems, patternProperties, ...) is REFUSED by
check_schema (SchemaUnsupported) instead of being silently ignored. enum/const comparison is type-aware (True != 1);
NaN and infinities are not valid numbers. validate() returns a list of error strings.
"""
from __future__ import annotations

import math
from typing import Any, List

from .ports import PortError

SUPPORTED = {"type", "properties", "required", "additionalProperties", "items", "minItems", "maxItems", "minLength", "maxLength",
             "minimum", "maximum", "enum", "const", "anyOf", "title", "description", "default", "examples", "$comment"}
TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}


class SchemaUnsupported(PortError, ValueError):
    pass


MAX_DEPTH = 32          # nesting of properties/items/anyOf; deeper schemas are refused before any spend (no RecursionError)


def _nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_schema(schema: Any, path: str = "$", _depth: int = 0) -> None:
    """Refuse (SchemaUnsupported) anything outside the subset, including a keyword whose VALUE has the wrong type, before
    any backend call is made: a schema that would fail during validation must fail here, not after the call is spent."""
    if _depth > MAX_DEPTH:
        raise SchemaUnsupported("%s: schema nested deeper than %d" % (path, MAX_DEPTH))
    if not isinstance(schema, dict):
        raise SchemaUnsupported("%s: schema must be an object" % path)
    bad = sorted(str(k) for k in set(schema) - SUPPORTED)
    if bad:
        raise SchemaUnsupported("%s: unsupported keyword(s) %s (supported subset: see jsonschema_min)" % (path, bad))
    t = schema.get("type")
    if "type" in schema:
        ts = t if isinstance(t, list) else [t]
        if not ts or any(not isinstance(x, str) or x not in TYPES for x in ts):
            raise SchemaUnsupported("%s: unsupported type %r" % (path, t))
    ap = schema.get("additionalProperties", True)
    if not isinstance(ap, bool):
        raise SchemaUnsupported("%s: additionalProperties must be a boolean" % path)
    for k in ("minItems", "maxItems", "minLength", "maxLength"):
        if k in schema and not _nonneg_int(schema[k]):
            raise SchemaUnsupported("%s: %s must be a non-negative integer" % (path, k))
    for k in ("minimum", "maximum"):
        if k in schema and not _finite_number(schema[k]):
            raise SchemaUnsupported("%s: %s must be a finite number" % (path, k))
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        raise SchemaUnsupported("%s: enum must be a non-empty array" % path)
    if "required" in schema and not (isinstance(schema["required"], list) and all(isinstance(r, str) for r in schema["required"])):
        raise SchemaUnsupported("%s: required must be an array of strings" % path)
    if "properties" in schema and not (isinstance(schema["properties"], dict) and all(isinstance(k, str) for k in schema["properties"])):
        raise SchemaUnsupported("%s: properties must be an object" % path)
    if "anyOf" in schema and not (isinstance(schema["anyOf"], list) and schema["anyOf"]):
        raise SchemaUnsupported("%s: anyOf must be a non-empty array" % path)
    for k in ("title", "description", "$comment"):
        if k in schema and not isinstance(schema[k], str):
            raise SchemaUnsupported("%s: %s must be a string" % (path, k))
    if "examples" in schema and not isinstance(schema["examples"], list):
        raise SchemaUnsupported("%s: examples must be an array" % path)
    if "items" in schema and not isinstance(schema["items"], dict):
        raise SchemaUnsupported("%s: list-form items is unsupported" % path)
    for k, sub in (schema.get("properties") or {}).items():
        check_schema(sub, "%s.%s" % (path, k), _depth + 1)
    if "items" in schema:
        check_schema(schema["items"], path + "[]", _depth + 1)
    for i, sub in enumerate(schema.get("anyOf") or []):
        check_schema(sub, "%s.anyOf[%d]" % (path, i), _depth + 1)


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_eq(a[k], b[k]) for k in a)
    return type(a) == type(b) and a == b


def _is_type(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and not math.isfinite(v))
    return {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}[t] is not None and isinstance(
        v, {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}[t])


def validate(value: Any, schema: dict, path: str = "$") -> List[str]:
    check_schema(schema, path)
    return _validate(value, schema, path)


def _validate(value: Any, schema: dict, path: str) -> List[str]:
    errs: List[str] = []
    if "const" in schema and not _eq(value, schema["const"]):
        errs.append("%s: expected const %r" % (path, schema["const"]))
    if "enum" in schema and not any(_eq(value, e) for e in schema["enum"]):
        errs.append("%s: %r not in enum" % (path, value))
    if "anyOf" in schema and not any(not _validate(value, s, path) for s in schema["anyOf"]):
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
                errs += _validate(value[k], sub, "%s.%s" % (path, k))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append("%s: more than %d items" % (path, schema["maxItems"]))
        if "items" in schema:
            for i, v in enumerate(value):
                errs += _validate(v, schema["items"], "%s[%d]" % (path, i))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append("%s: shorter than %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append("%s: longer than %d" % (path, schema["maxLength"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            errs.append("%s: non-finite number" % path)
        else:
            if "minimum" in schema and value < schema["minimum"]:
                errs.append("%s: below minimum" % path)
            if "maximum" in schema and value > schema["maximum"]:
                errs.append("%s: above maximum" % path)
    return errs

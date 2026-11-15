"""Slot canonicalisation: sees ONLY (slot key, cardinality, modalities); returns a pure key->key mapping, validated in code."""
import json

from .prompts import CANON_TEMPLATE


def build_lines(facts):
    seen = {}
    for f in facts:
        e = seen.setdefault(f["slot"], {"card": set(), "mod": set()})
        e["card"].add(f["cardinality"])
        e["mod"].add(f["modality"])
    return sorted(seen), "\n".join("%s | %s | %s" % (k, "/".join(sorted(v["card"])), "/".join(sorted(v["mod"]))) for k, v in sorted(seen.items()))


def build_prompt(facts):
    keys, lines = build_lines(facts)
    return keys, CANON_TEMPLATE.format(lines=lines)


def parse_mapping(text, keys):
    """Raises ValueError unless the output is a pure mapping over exactly the input keys onto input keys."""
    a, b = text.find("{"), text.rfind("}")
    m = json.loads(text[a:b + 1]).get("mapping")
    if not isinstance(m, dict):
        raise ValueError("no mapping object")
    if set(m) != set(keys):
        raise ValueError("mapping keys differ from input: missing %d extra %d" % (len(set(keys) - set(m)), len(set(m) - set(keys))))
    bad = [v for v in m.values() if v not in set(keys)]
    if bad:
        raise ValueError("canonical value not among input keys: %r" % bad[:3])
    return m

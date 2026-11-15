"""Advisory supersession v3: only labels, nothing is ever hidden. Labels: current | superseded | pending-update | disputed | inactive.
Slot identity = norm_slot(canonical key from the canonicalisation pass). See GATE.md for the registered rules."""
from .supersede_v2 import norm_slot

STATE = ("asserted", "decided")


def _objkey(f):
    import re
    return " ".join(sorted(re.findall(r"[a-z0-9]+", str(f["object"]).lower())))


def _order(f):
    return (f["asserted_at"], f.get("order", 0))


def resolve_v3(facts, mapping=None):
    mapping = mapping or {}
    for i, f in enumerate(facts):
        f["_i"] = i
        f["canon"] = norm_slot(mapping.get(f["slot"], f["slot"]))
        f["valid_to"], f["superseded_by"], f["pending_of"], f["pending_updates"] = None, None, None, []
        f["status"] = "current" if f["modality"] in STATE else "inactive"
    groups = {}
    for f in facts:
        if f["modality"] in ("asserted", "decided", "proposed") and f["cardinality"] == "single" and f["canon"]:
            groups.setdefault((f["canon"], f["scope"]), []).append(f)
    links = []
    for g in groups.values():
        g.sort(key=_order)
        holder = None  # latest asserted value so far
        for f in g:
            if f["modality"] == "asserted":
                if holder is not None and _objkey(holder) != _objkey(f):
                    if f["asserted_at"].date() == holder["asserted_at"].date() and f["session_id"] == holder["session_id"]:
                        holder["status"] = f["status"] = "disputed"
                    elif f["author"] == "assistant" and holder["author"] == "user":
                        f["status"] = "inactive"
                        continue
                    else:
                        holder["status"], holder["valid_to"], holder["superseded_by"] = "superseded", f["asserted_at"], f["_i"]
                        links.append(("supersession", holder["_i"], f["_i"]))
                if holder is None or _objkey(holder) != _objkey(f) or f["status"] != "inactive":
                    if f["status"] == "current" or f["status"] == "disputed":
                        holder = f
            else:  # decided or proposed single fact: pending update when a different asserted value already exists earlier
                if holder is not None and holder["asserted_at"] <= f["asserted_at"] and _objkey(holder) != _objkey(f) and holder["author"] == f["author"]:
                    f["status"], f["pending_of"] = "pending-update", holder["_i"]
                    holder["pending_updates"].append(f["_i"])
                    links.append(("pending", holder["_i"], f["_i"]))
                elif f["modality"] == "proposed":
                    f["status"] = "inactive"
    return facts, links


def slot_rate(facts, key):
    by, singles = {}, []
    for f in facts:
        if f["modality"] in STATE and f["cardinality"] == "single" and f.get(key):
            singles.append(f)
            by.setdefault((f[key], f["scope"]), set()).add(f["session_id"])
    hit = sum(1 for f in singles if len(by[(f[key], f["scope"])]) > 1)
    return {"n": len(singles), "hit": hit, "slots": len(by)}

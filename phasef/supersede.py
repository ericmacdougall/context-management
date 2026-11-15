"""Supersession resolved in code, never by an LLM seeing other sessions' output.

A fact is a dict with subject, predicate, object, valid_from (datetime), asserted_at (datetime), modality, author,
session_id, round, order (extraction order, tie-break). Only modality asserted/decided counts as a state claim; proposed,
hypothetical and question facts are never current and never supersede. An assistant-authored fact never supersedes a
user-authored one (lower trust never retires higher). Two facts are about the same slot when subjects match and the
predicates share enough content tokens. For one slot, facts are ordered by (valid_from, asserted_at, order); a later
fact with a different object retires the earlier (valid_to = later valid_from, superseded_by = later). Equal objects
are the same claim and do not supersede.
"""
import re

STATE_MODALITIES = ("asserted", "decided")
_STOP = {"a", "an", "the", "of", "to", "is", "has", "have", "in", "at", "on", "for", "and", "or", "user", "s"}


def _tokens(s):
    return [t for t in re.findall(r"[a-z0-9]+", str(s).lower()) if t not in _STOP]


def _stem(t):
    for suf in ("ing", "es", "ed", "s"):
        if len(t) > 4 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def norm_subject(s):
    return " ".join(sorted(_stem(t) for t in _tokens(s)))


def pred_tokens(p):
    return set(_stem(t) for t in _tokens(p))


def same_slot(a, b, min_jaccard=0.5):
    if norm_subject(a["subject"]) != norm_subject(b["subject"]):
        return False
    pa, pb = pred_tokens(a["predicate"]), pred_tokens(b["predicate"])
    if not pa or not pb:
        return False
    return len(pa & pb) / float(len(pa | pb)) >= min_jaccard


def same_object(a, b):
    return " ".join(_tokens(a["object"])) == " ".join(_tokens(b["object"]))


def _sort_key(f):
    return (f["valid_from"], f["asserted_at"], f.get("order", 0))


def resolve(facts, min_jaccard=0.5):
    """Mutates and returns facts: sets status ('current'|'superseded'|'duplicate'|'inactive'), valid_to, superseded_by (index into facts)."""
    for i, f in enumerate(facts):
        f["_i"] = i
        f["status"] = "inactive"
        f["valid_to"] = None
        f["superseded_by"] = None
    state = [f for f in facts if f["modality"] in STATE_MODALITIES]
    slots = []  # list of lists, greedy single-link grouping in time order
    for f in sorted(state, key=_sort_key):
        for slot in slots:
            if any(same_slot(f, g, min_jaccard) for g in slot):
                slot.append(f)
                break
        else:
            slots.append([f])
    for slot in slots:
        slot.sort(key=_sort_key)
        current = None
        for f in slot:
            f["status"] = "current"
            if current is not None and not same_object(current, f):
                if f["author"] == "assistant" and current["author"] == "user":
                    f["status"] = "inactive"  # lower trust cannot retire higher
                    continue
                current["status"] = "superseded"
                current["valid_to"] = f["valid_from"]
                current["superseded_by"] = f["_i"]
            elif current is not None:
                f["status"] = "duplicate"  # restatement of the current claim: the earlier fact stays the record
                f["duplicate_of"] = current["_i"]
                continue
            current = f
    return facts

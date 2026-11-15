"""Supersession v2, code only. A fact retires an older one only when ALL hold: same normalised slot key, cardinality 'single',
modality asserted/decided on both, same scope, and the newer is not a lower-trust author than the older (assistant never retires user).
Order is by (asserted_at, extraction order): the later session's statement wins; event_time is kept for the reader but not used for ordering.
The slot normaliser is deterministic: lower-case, split on non-alphanumerics, drop filler tokens, simple lemmatisation, apply the
synonym table, sort tokens. SYNONYMS and FILLER are built ONLY from the five-question pilot dev set (07741c44/45, 6e984301/2 and their
gold sessions) and are declared here; they must not be extended from any other question."""
import re

STATE = ("asserted", "decided")
FILLER = {"user", "old", "new", "current", "currently", "my", "own", "the", "a", "an", "of", "s", "is", "has", "have", "to"}
SYNONYMS = {  # dev-set derived (declared): storage place of shoes
    "sneaker": "shoe", "trainer": "shoe", "footwear": "shoe", "storage": "location", "place": "location", "spot": "location",
    "stored": "location", "keep": "location", "kept": "location", "stores": "location", "store": "location",
}


def lemma(t):
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(t) > 4 and t.endswith(suf):
            return t[: -len(suf)] + rep
    return t


def norm_slot(key):
    toks = [lemma(t) for t in re.findall(r"[a-z0-9]+", str(key).lower())]
    toks = [SYNONYMS.get(t, t) for t in toks if t not in FILLER]
    return ".".join(sorted(set(toks)))


def _key(f):
    return (f["asserted_at"], f.get("order", 0))


def resolve_v2(facts):
    """Mutates facts: status current|superseded|duplicate|inactive|unslotted, valid_to, superseded_by, slot_norm."""
    for i, f in enumerate(facts):
        f["_i"], f["valid_to"], f["superseded_by"] = i, None, None
        f["slot_norm"] = norm_slot(f.get("slot", ""))
        if f["modality"] in STATE and f.get("cardinality") == "single" and f["slot_norm"]:
            f["status"] = "current"
        elif f["modality"] in STATE:
            f["status"] = "current" if f.get("cardinality") == "multi" else "unslotted"
        else:
            f["status"] = "inactive"
    groups = {}
    for f in facts:
        if f["modality"] in STATE and f.get("cardinality") == "single" and f["slot_norm"]:
            groups.setdefault((f["slot_norm"], f.get("scope", "")), []).append(f)
    for g in groups.values():
        g.sort(key=_key)
        cur = None
        for f in g:
            if cur is not None:
                same = " ".join(sorted(re.findall(r"[a-z0-9]+", str(cur["object"]).lower()))) == " ".join(sorted(re.findall(r"[a-z0-9]+", str(f["object"]).lower())))
                if same:
                    f["status"] = "duplicate"
                    continue
                if f["author"] == "assistant" and cur["author"] == "user":
                    f["status"] = "inactive"
                    continue
                cur["status"], cur["valid_to"], cur["superseded_by"] = "superseded", f["asserted_at"], f["_i"]
            cur = f
    return facts


def slot_match_stats(facts):
    """Share of single-cardinality state facts whose normalised slot occurs in at least two different sessions (can be superseded)."""
    by = {}
    for f in facts:
        if f["modality"] in STATE and f.get("cardinality") == "single" and f["slot_norm"]:
            by.setdefault((f["slot_norm"], f.get("scope", "")), set()).add(f["session_id"])
    singles = [f for f in facts if f["modality"] in STATE and f.get("cardinality") == "single" and f["slot_norm"]]
    multi_session = sum(1 for f in singles if len(by[(f["slot_norm"], f.get("scope", ""))]) > 1)
    return {"single_state_facts": len(singles), "in_cross_session_slot": multi_session,
            "rate": (multi_session / float(len(singles))) if singles else 0.0, "distinct_slots": len(by)}

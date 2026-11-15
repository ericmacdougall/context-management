"""Extraction cache keyed by (event id, extractor version), storing the RAW extractor output (encrypted under the speaker's
key). Recipe-specific normalisation is applied after the cache, so a recipe change that does not change the extractor needs
zero extractor invocations. Extractor versions: 1 = memsys.extractor rules; 2 = the same plus one extra pattern (a stand-in
for "the model or prompt changed": nothing cached under version 1 is reusable)."""
import json
import re

from memsys import extractor
from memsys.extractor import _attr, fact_id

DDL = """create table if not exists extract_cache (tenant_id text not null, event_id text not null, extractor_version int not null,
  subject_id text not null, raw_enc bytea not null, primary key (tenant_id, event_id, extractor_version));"""
_WAS = re.compile(r"^(?P<s>[A-Z][\w]*)'s (?P<a>[a-z][\w ]*?) was (?P<v>.+?)\.?$")
RECIPE_EXTRACTOR = {1: 1, 2: 1, 3: 2}      # recipe version -> extractor version


class Counter:
    def __init__(self):
        self.invocations = 0
        self.hits = 0
        self.misses = 0


def raw_extract(ext_version, ev, text):
    """The 'expensive' extractor (an LLM in production). Returns recipe-independent raw claims (v1 attribute spelling)."""
    base = extractor.extract_orig(1, ev, text)
    if ext_version >= 2:
        m = _WAS.match(text.strip())
        if m and not base:
            c = {"subject": m["s"], "predicate": m["a"], "scope": "*", "object": m["v"].strip(), "modality": "asserted"}
            base = [extractor._finish(1, ev, c, 0)]
    return base


def to_recipe(recipe, ev, raw_claims):
    """Recipe-specific post-processing of raw claims (spelling normalisation, cue, derivation-addressed id)."""
    out = []
    for i, c in enumerate(raw_claims):
        d = dict(c)
        d["predicate"] = _attr(c["predicate"], recipe)
        d["recipe_version"] = recipe
        d["id"] = fact_id(ev["tenant_id"], recipe, ev["hash"], i)
        d["cue"] = f"{d['subject']} {d['predicate']}".lower() if recipe >= 2 else None
        d["order_key"] = c["order_key"]
        out.append(d)
    return out


def install(conn, use_cache, counter):
    """Monkeypatch memsys.extractor.extract so memsys.derive goes through the cache; register the cache with erasure."""
    from memsys import erasure, kms as _kms
    conn.execute(DDL)
    if not hasattr(extractor, "extract_orig"):
        extractor.extract_orig = extractor.extract
    if ("extract_cache", "event_id") not in erasure.DERIVED_BY_EVENT:
        erasure.DERIVED_BY_EVENT.append(("extract_cache", "event_id"))

    def wrapped(recipe, ev, text):
        ev_v = RECIPE_EXTRACTOR[recipe]
        if use_cache:
            row = conn.execute("select raw_enc from extract_cache where tenant_id=%s and event_id=%s and extractor_version=%s", (ev["tenant_id"], ev["event_id"], ev_v)).fetchone()
            if row:
                counter.hits += 1
                raw = json.loads(_kms.get().dec_many(ev["tenant_id"], [(ev["subject_id"], bytes(row[0]))])[0])
                return to_recipe(recipe, ev, raw)
            counter.misses += 1
        counter.invocations += 1
        raw = raw_extract(ev_v, ev, text)
        if use_cache:
            enc = _kms.get().enc(ev["tenant_id"], ev["subject_id"], json.dumps(raw, sort_keys=True))
            conn.execute("insert into extract_cache(tenant_id,event_id,extractor_version,subject_id,raw_enc) values (%s,%s,%s,%s,%s) on conflict do nothing",
                         (ev["tenant_id"], ev["event_id"], ev_v, ev["subject_id"], enc))
        return to_recipe(recipe, ev, raw)
    extractor.extract = wrapped


def uninstall():
    if hasattr(extractor, "extract_orig"):
        extractor.extract = extractor.extract_orig

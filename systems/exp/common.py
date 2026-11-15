"""Shared helpers for experiments: connections, ES snapshots, comparison, results."""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memsys import db, esx
from memsys.esc import ES

RES = os.environ.get("RESDIR", os.path.expanduser("~/emlab-16096/systems/results"))
os.makedirs(RES, exist_ok=True)


def conns():
    return db.connect(autocommit=True), db.connect(autocommit=True, application_name="memsys-lock"), ES()


def save(name, obj):
    with open(f"{RES}/{name}.json", "w") as f:
        json.dump(obj, f, indent=1, default=str)


def disk_free_gb():
    st = os.statvfs("/")
    return round(st.f_bavail * st.f_frsize / 1e9, 1)


def scan(es: ES, index, source, size=2000, extra_query=None):
    """All docs of an index/alias via PIT + search_after. Yields (_index, _id, _source)."""
    pit = es.req("POST", f"/{index}/_pit", keep_alive="2m")[1]["id"]
    try:
        after = None
        while True:
            body = {"size": size, "pit": {"id": pit, "keep_alive": "2m"}, "_source": source, "fields": ["vec"] if source is True else [], "sort": [{"_shard_doc": "asc"}],
                    "query": extra_query or {"match_all": {}}}
            if after:
                body["search_after"] = after
            h = es.req("POST", "/_search", body)[1]["hits"]["hits"]
            if not h:
                return
            for x in h:
                src = x.get("_source", {})
                if "fields" in x and "vec" in x["fields"]:
                    src = dict(src, vec=x["fields"]["vec"])
                yield x["_index"], x["_id"], src
            after = h[-1]["sort"]
    finally:
        es.req("DELETE", "/_pit", {"id": pit}, ok=None)


def snapshot_events(es, tenants):
    """{tenant: list of (index, id, doc_hash)} -- a LIST so duplicates are visible."""
    out = {}
    for t in tenants:
        rows = []
        if esx.alias_targets(es, esx.ev_ralias(t)):
            for idx, i, src in scan(es, esx.ev_ralias(t), True):
                rows.append((idx, i, esx.doc_hash(src)))
        out[t] = rows
    return out


def snapshot_facts(es, tenants, version=None):
    out = {}
    for t in tenants:
        rows = []
        if esx.alias_targets(es, esx.fx_ralias(t)):
            for idx, i, src in scan(es, esx.fx_ralias(t), True):
                rows.append((idx, i, esx.doc_hash(src)))
        out[t] = rows
    return out


def strip_gen(idx):
    """Index names differ by generation between runs; compare by (bucket) identity."""
    import re
    return re.sub(r"-g\d+$", "", idx)


def pg_expected_events(conn, tenant):
    """Expected {event_id: (bucket, doc_hash)} computed from Postgres, independent of the projector."""
    from memsys.db import iso
    from memsys import store
    out = {}
    for r in store.read_plain(conn, tenant, "true", ()):
        if r["tomb"] or r["plain"] is None:
            continue
        out[r["event_id"]] = (esx.bucket_of(r["ts_event"]), esx.doc_hash(esx.event_doc(tenant, r["session_id"], r["seq"], r["event_id"], iso(r["ts_event"]), r["actor"],
                                                                                         r["trust_origin"], r["kind"], r["subject_id"], r["plain"])))
    return out


def pg_expected_facts(conn, tenant, recipe):
    from memsys.db import iso
    from memsys import kms as _kms, derive as D
    cur = conn.cursor()
    cur.execute("select id, slot_enc, object_enc, modality, status, author, trust_origin, valid_from, valid_to, input_event_ids, recipe_version, subject_id, rev "
                "from facts where tenant_id=%s and recipe_version=%s", (tenant, recipe))
    rows = cur.fetchall(); n = len(rows)
    dec = _kms.get().dec_many(tenant, [(r[11], r[1]) for r in rows] + [(r[11], r[2]) for r in rows])
    out = {}
    for i, r in enumerate(rows):
        if dec[i] is None or dec[n + i] is None:
            continue
        sub, pred, scope, cue = D.decode_slot(dec[i])
        f = dict(id=r[0], tenant_id=tenant, subject=sub, predicate=pred, scope=scope, object=dec[n + i], modality=r[3], status=r[4], author=r[5], trust_origin=r[6],
                 valid_from=iso(r[7]), valid_to=iso(r[8]) if r[8] else None, input_event_ids=r[9], recipe_version=r[10], subject_id=r[11], cue=cue, rev=r[12])
        out[r[0]] = esx.doc_hash(esx.fact_doc(f))
    return out


def compare_events(conn, es, tenants):
    """Zero duplicates / zero gaps / zero content drift of ES vs Postgres. Returns a summary dict."""
    snap = snapshot_events(es, tenants)
    tot = {"pg": 0, "es": 0, "missing": 0, "extra": 0, "duplicates": 0, "hash_mismatch": 0, "wrong_bucket": 0}
    ex = []
    for t in tenants:
        exp = pg_expected_events(conn, t)
        seen = {}
        for idx, i, h in snap[t]:
            seen.setdefault(i, []).append((idx, h))
        tot["pg"] += len(exp); tot["es"] += len(snap[t])
        for i, (b, h) in exp.items():
            got = seen.get(i)
            if not got:
                tot["missing"] += 1; ex.append(("missing", i)) if len(ex) < 5 else None
                continue
            if len(got) > 1:
                tot["duplicates"] += 1; ex.append(("dup", i, got)) if len(ex) < 5 else None
            if got[0][1] != h:
                tot["hash_mismatch"] += 1
            if f"-b{b:05d}-" not in got[0][0]:
                tot["wrong_bucket"] += 1
        tot["extra"] += sum(1 for i in seen if i not in exp)
    tot["examples"] = ex
    return tot


def compare_facts(conn, es, tenants, recipe):
    snap = snapshot_facts(es, tenants)
    tot = {"pg": 0, "es": 0, "missing": 0, "extra": 0, "duplicates": 0, "hash_mismatch": 0}
    for t in tenants:
        exp = pg_expected_facts(conn, t, recipe)
        seen = {}
        for idx, i, h in snap[t]:
            seen.setdefault(i, []).append(h)
        tot["pg"] += len(exp); tot["es"] += len(snap[t])
        for i, h in exp.items():
            g = seen.get(i)
            if not g:
                tot["missing"] += 1; continue
            if len(g) > 1:
                tot["duplicates"] += 1
            if g[0] != h:
                tot["hash_mismatch"] += 1
        tot["extra"] += sum(1 for i in seen if i not in exp)
    return tot


def percentiles(xs, ps=(50, 90, 95, 99, 100)):
    xs = sorted(xs)
    if not xs:
        return {}
    return {f"p{p}": round(xs[min(len(xs) - 1, int(len(xs) * p / 100 - 1e-9))], 4) for p in ps}

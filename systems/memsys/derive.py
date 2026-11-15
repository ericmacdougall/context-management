"""Deriver: events -> claims (facts rows) in Postgres, one transaction per batch.

Exactly-once by construction: the batch's fact upserts, slot re-resolution,
concept dirty marks and the cursor advance commit together. Fact ids are
derivation-addressed so a replay is a no-op. Writes go through mem_enc(), which
takes FOR SHARE on the subject-key row and refuses a destroyed key: this is the
commit-time fence against a concurrent erasure.
"""
import os
import random
import signal
from collections import defaultdict

import psycopg

from . import extractor, rules
from .db import iso, xmin

BIG = 2 ** 62


def crash(name):
    """Named crash points for the crash experiments: MEMSYS_CRASH='name:prob,name2:prob'."""
    spec = os.environ.get("MEMSYS_CRASH")
    if not spec:
        return
    for part in spec.split(","):
        n, _, p = part.partition(":")
        if n == name and random.random() < float(p or 1):
            os.kill(os.getpid(), signal.SIGKILL)


def get_cursor(cur, view, version, tenant):
    cur.execute("insert into projection_state(view,version,tenant_id) values (%s,%s,%s) on conflict do nothing",
                (view, version, tenant))
    return cur.execute("select hwm_xid,hwm_seq from projection_state where view=%s and version=%s and tenant_id=%s for update",
                       (view, version, tenant)).fetchone()


def set_cursor(cur, view, version, tenant, x, s):
    cur.execute("update projection_state set hwm_xid=%s, hwm_seq=%s, updated_at=now() where view=%s and version=%s and tenant_id=%s "
                "and (hwm_xid, hwm_seq) < (%s, %s)", (x, s, view, version, tenant, x, s))


EV_COLS = ("tenant_id,session_id,seq,event_id,ts_event,actor,trust_origin,kind,subject_id,hash,commit_xid,ingest_seq,"
           "mem_dec(tenant_id, subject_id, content_enc) as plain, "
           "exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) as tomb")


def read_event_batch(cur, tenant, cx, cs, limit):
    xm = xmin(cur)
    cur.execute(f"select {EV_COLS} from events e where tenant_id=%s and (commit_xid, ingest_seq) > (%s, %s) and commit_xid < %s "
                "order by commit_xid, ingest_seq limit %s", (tenant, cx, cs, xm, limit))
    cols = [d.name for d in cur.description]
    return xm, [dict(zip(cols, r)) for r in cur.fetchall()]


def derive_batch(conn, tenant, recipe, limit=1000, touch=True, fence=True, between=None):
    """Process one batch; returns number of events consumed. `between` is a test hook
    called after reading and before writing (used to inject delays in race tests);
    fence=False is the NAIVE CONTROL: encrypt before the hook and write without re-checking."""
    with conn.transaction():
        cur = conn.cursor()
        cx, cs = get_cursor(cur, "derive", recipe, tenant)
        xm, rows = read_event_batch(cur, tenant, cx, cs, limit)
        if not rows:
            set_cursor(cur, "derive", recipe, tenant, xm - 1, BIG)
            return 0
        claims = []
        for r in rows:
            if r["tomb"] or r["plain"] is None:
                continue
            ev = dict(r, ts_event=iso(r["ts_event"]))
            for c in extractor.extract(recipe, ev, r["plain"]):
                c["_obj"] = c["object"]
                claims.append(c)
        crash("derive_after_read")
        pre = None
        if not fence:
            pre = {c["id"]: cur.execute("select mem_enc(%s,%s,%s)", (tenant, c["subject_id"], c["object"])).fetchone()[0] for c in claims}
        if between:
            between(tenant, rows, claims)
        touched = _insert_claims(conn, cur, tenant, recipe, claims, pre)
        crash("derive_after_insert")
        changed_subjects = resolve_slots(cur, tenant, recipe, touched)
        if touch and changed_subjects:
            from .conceptq import touch_concepts
            touch_concepts(cur, tenant, changed_subjects)
        set_cursor(cur, "derive", recipe, tenant, rows[-1]["commit_xid"], rows[-1]["ingest_seq"])
        crash("derive_before_commit")
    return len(rows)


def _insert_claims(conn, cur, tenant, recipe, claims, pre):
    touched = {(c["subject"], c["predicate"], c["scope"]) for c in claims}
    if not claims:
        return touched
    if pre is not None:   # naive control: no key check at write time
        cur.executemany(
            "insert into facts(tenant_id,recipe_version,id,subject,predicate,scope,object_enc,modality,author,trust_origin,valid_from,"
            "input_event_ids,subject_id,order_key,cue) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::timestamptz,%s,%s,%s,%s) on conflict do nothing",
            [(tenant, recipe, c["id"], c["subject"], c["predicate"], c["scope"], pre[c["id"]], c["modality"], c["author"],
              c["trust_origin"], c["valid_from"], c["input_event_ids"], c["subject_id"], c["order_key"], c.get("cue")) for c in claims])
        return touched
    sql = ("insert into facts(tenant_id,recipe_version,id,subject,predicate,scope,object_enc,modality,author,trust_origin,valid_from,"
           "input_event_ids,subject_id,order_key,cue) values (%s,%s,%s,%s,%s,%s,mem_enc(%s,%s,%s),%s,%s,%s,%s::timestamptz,%s,%s,%s,%s) "
           "on conflict do nothing")
    args = lambda c: (tenant, recipe, c["id"], c["subject"], c["predicate"], c["scope"], tenant, c["subject_id"], c["object"],
                      c["modality"], c["author"], c["trust_origin"], c["valid_from"], c["input_event_ids"], c["subject_id"],
                      c["order_key"], c.get("cue"))
    try:
        with conn.transaction():
            cur.executemany(sql, [args(c) for c in claims])
    except psycopg.errors.RaiseException as e:
        if "subject_erased" not in str(e):
            raise
        # a concurrent erasure won the race for some subject: drop only those claims
        kept = set()
        for c in claims:
            try:
                with conn.transaction():
                    cur.execute(sql, args(c))
                kept.add((c["subject"], c["predicate"], c["scope"]))
            except psycopg.errors.RaiseException as e2:
                if "subject_erased" not in str(e2):
                    raise
        return kept
    return touched


def resolve_slots(cur, tenant, recipe, slots):
    """Recompute status/valid_to for every claim of the given slots from the claim set.
    Returns the set of subjects whose rows changed."""
    if not slots:
        return set()
    slots = sorted(slots)
    cur.execute(
        "select id, subject, predicate, scope, mem_dec(tenant_id, subject_id, object_enc), modality, trust_origin, valid_from, "
        "order_key, status, valid_to from facts where tenant_id=%s and recipe_version=%s and "
        "(subject,predicate,scope) in (select * from unnest(%s::text[],%s::text[],%s::text[]))",
        (tenant, recipe, [s[0] for s in slots], [s[1] for s in slots], [s[2] for s in slots]))
    by_slot, old = defaultdict(list), {}
    for fid, sub, pred, sc, obj, mod, tr, vf, ok, st, vt in cur.fetchall():
        if obj is None:
            continue
        by_slot[(sub, pred, sc)].append(rules.Claim(fid, obj, mod, tr, iso(vf), ok))
        old[fid] = (sub, st, vt)
    upd, subjects = [], set()
    for slot, cl in by_slot.items():
        for fid, (st, vt) in rules.resolve_slot(cl).items():
            upd.append((st, vt, tenant, recipe, fid, st, vt))
            subjects.add(old[fid][0])
    cur.executemany(
        "update facts set status=%s, valid_to=%s::timestamptz, rev=nextval('fact_rev'), "
        "changed_xid=(pg_current_xact_id()::text)::bigint where tenant_id=%s and recipe_version=%s and id=%s "
        "and (status is distinct from %s or valid_to is distinct from %s::timestamptz)", upd)
    return subjects


def tenants(conn):
    return [r[0] for r in conn.execute("select distinct tenant_id from sessions order by 1").fetchall()]

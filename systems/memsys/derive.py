"""Deriver: events -> claims (facts rows) in Postgres, one transaction per batch.

Exactly-once by construction: the batch's fact upserts, slot re-resolution, concept dirty marks and the cursor
advance commit together; fact ids are derivation-addressed so a replay is a no-op.
Serialisation, in one global order (subject fence rows sorted, then slot locks sorted):
  1. FOR SHARE on each claim subject's fence row (refuses an erased subject; erasure takes FOR UPDATE);
  2. a transaction-level advisory lock per (tenant, recipe, slot_hash) BEFORE claims are inserted or slots read, so
     derivation and erasure (and two erasures) can never resolve the same slot from stale views.
"""
import json
import os
import random
import signal
from collections import defaultdict

from . import extractor, rules, kms as _kms
from .db import iso, xmin, retry_deadlock
from .kms import SubjectErased

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
    cur.execute("insert into projection_state(view,version,tenant_id) values (%s,%s,%s) on conflict do nothing", (view, version, tenant))
    return cur.execute("select hwm_xid,hwm_seq from projection_state where view=%s and version=%s and tenant_id=%s for update",
                       (view, version, tenant)).fetchone()


def set_cursor(cur, view, version, tenant, x, s):
    cur.execute("update projection_state set hwm_xid=%s, hwm_seq=%s, updated_at=now() where view=%s and version=%s and tenant_id=%s "
                "and (hwm_xid, hwm_seq) < (%s, %s)", (x, s, view, version, tenant, x, s))


def read_event_batch(conn, tenant, cx, cs, limit, kms=None):
    """Rows above the cursor and below the xmin horizon, decrypted app-side (plain=None when the key is gone)."""
    kms = kms or _kms.get()
    cur = conn.cursor()
    xm = xmin(cur)
    cur.execute("select tenant_id,session_id,seq,event_id,ts_event,actor,trust_origin,kind,subject_id,hash,commit_xid,ingest_seq,content_enc,"
                "exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) as tomb "
                "from events e where tenant_id=%s and (commit_xid, ingest_seq) > (%s, %s) and commit_xid < %s order by commit_xid, ingest_seq limit %s",
                (tenant, cx, cs, xm, limit))
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    pl = kms.dec_many(tenant, [(r["subject_id"], r["content_enc"]) for r in rows])
    for r, p in zip(rows, pl):
        r["plain"] = p
        del r["content_enc"]
    return xm, rows


def slot_lock_key(tenant, recipe, slot_hash):
    return f"slot:{tenant}:{recipe}:{slot_hash}"


def lock_slots(cur, tenant, recipe, slot_hashes):
    for h in sorted(set(slot_hashes)):
        cur.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (slot_lock_key(tenant, recipe, h),))


def slot_hashes_for(kms, tenant, claims):
    keys = sorted({f"{c['subject']}\x1f{c['predicate']}" for c in claims})
    hm = dict(zip(keys, kms.hmac_many(tenant, keys)))
    ents = sorted({c["subject"] for c in claims})
    em = dict(zip(ents, kms.hmac_many(tenant, ents)))
    for c in claims:
        c["slot_hash"] = hm[f"{c['subject']}\x1f{c['predicate']}"]
        c["entity_hash"] = em[c["subject"]]


def derive_batch(conn, tenant, recipe, limit=1000, touch=True, fence=True, between=None, kms=None, slot_locks=True, resolve_hook=None):
    """Process one batch; returns the number of events consumed. `between` is a test hook called after reading and
    before writing. fence=False is the NAIVE CONTROL: ciphertext is made before the hook and written with no subject
    check and no slot locks."""
    kms = kms or _kms.get()

    def run():
        with conn.transaction():
            cur = conn.cursor()
            cx, cs = get_cursor(cur, "derive", recipe, tenant)
            xm, rows = read_event_batch(conn, tenant, cx, cs, limit, kms)
            if not rows:
                set_cursor(cur, "derive", recipe, tenant, xm - 1, BIG)
                return 0
            claims = []
            for r in rows:
                if r["tomb"] or r["plain"] is None:
                    continue
                ev = dict(r, ts_event=iso(r["ts_event"]))
                claims += extractor.extract(recipe, ev, r["plain"])
            if claims:
                slot_hashes_for(kms, tenant, claims)
            crash("derive_after_read")
            pre = None
            if not fence:
                pre = {c["id"]: (kms.enc(tenant, c["subject_id"], c["object"]),
                                 kms.enc(tenant, c["subject_id"], json.dumps({"s": c["subject"], "p": c["predicate"], "sc": c["scope"], "cue": c.get("cue")}, sort_keys=True)))
                       for c in claims}
            if between:
                between(tenant, rows, claims)
            slots = _insert_claims(cur, kms, tenant, recipe, claims, pre, fence, slot_locks)
            crash("derive_after_insert")
            changed = resolve_slots(cur, kms, tenant, recipe, slots, after_read=resolve_hook)
            if touch and changed:
                from .conceptq import touch_concepts
                touch_concepts(cur, tenant, changed)
            set_cursor(cur, "derive", recipe, tenant, rows[-1]["commit_xid"], rows[-1]["ingest_seq"])
            crash("derive_before_commit")
            return len(rows)
    return retry_deadlock(run)


def _insert_claims(cur, kms, tenant, recipe, claims, pre, fence, slot_locks=True):
    if not claims:
        return set()
    if fence:
        subs = sorted({c["subject_id"] for c in claims})
        erased = set()
        for s in subs:
            r = cur.execute("select erased from subjects where tenant_id=%s and subject_id=%s for share", (tenant, s)).fetchone()
            if r is not None and r[0]:
                erased.add(s)
        claims[:] = [c for c in claims if c["subject_id"] not in erased]
        if slot_locks:
            lock_slots(cur, tenant, recipe, [c["slot_hash"] for c in claims])
    rows = []
    for c in claims:
        if pre is not None:
            oe, se = pre[c["id"]]
        else:
            try:
                oe = kms.enc(tenant, c["subject_id"], c["object"])
                se = kms.enc(tenant, c["subject_id"], json.dumps({"s": c["subject"], "p": c["predicate"], "sc": c["scope"], "cue": c.get("cue")}, sort_keys=True))
            except SubjectErased:
                continue
        rows.append((tenant, recipe, c["id"], c["slot_hash"], c["entity_hash"], se, oe, c["modality"], c["author"], c["trust_origin"], c["valid_from"],
                     c["input_event_ids"], c["subject_id"], c["order_key"]))
    if rows:
        cur.executemany(
            "insert into facts(tenant_id,recipe_version,id,slot_hash,entity_hash,slot_enc,object_enc,modality,author,trust_origin,valid_from,input_event_ids,subject_id,order_key) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::timestamptz,%s,%s,%s) on conflict do nothing", rows)
    return {r[3] for r in rows}


def decode_slot(slot_json):
    d = json.loads(slot_json)
    return d["s"], d["p"], d["sc"], d.get("cue")


def resolve_slots(cur, kms, tenant, recipe, slot_hashes, after_read=None):
    """Recompute status/valid_to for every claim of the given slots from the claim set. The caller holds the slot
    locks. Returns the set of entity hashes whose rows were part of the resolution."""
    if not slot_hashes:
        return set()
    cur.execute("select id, slot_hash, entity_hash, slot_enc, object_enc, subject_id, modality, trust_origin, valid_from, order_key, status, valid_to "
                "from facts where tenant_id=%s and recipe_version=%s and slot_hash = any(%s)", (tenant, recipe, sorted(slot_hashes)))
    rows = cur.fetchall()
    if after_read:
        after_read()
    dec = kms.dec_many(tenant, [(r[5], r[3]) for r in rows] + [(r[5], r[4]) for r in rows])
    n = len(rows)
    by_slot, ents = defaultdict(list), set()
    for i, r in enumerate(rows):
        slot_json, obj = dec[i], dec[n + i]
        if slot_json is None or obj is None:
            continue
        scope = decode_slot(slot_json)[2]
        by_slot[r[1]].append(rules.Claim(r[0], obj, r[6], r[7], iso(r[8]), r[9], scope))
        ents.add(r[2])
    upd = []
    for h, cl in by_slot.items():
        for fid, (st, vt) in rules.resolve_slot(cl).items():
            upd.append((st, vt, tenant, recipe, fid, st, vt))
    cur.executemany(
        "update facts set status=%s, valid_to=%s::timestamptz, rev=nextval('fact_rev'), "
        "changed_xid=(pg_current_xact_id()::text)::bigint where tenant_id=%s and recipe_version=%s and id=%s "
        "and (status is distinct from %s or valid_to is distinct from %s::timestamptz)", upd)
    return ents


def tenants(conn):
    return [r[0] for r in conn.execute("select distinct tenant_id from sessions order by 1").fetchall()]

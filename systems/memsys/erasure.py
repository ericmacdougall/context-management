"""Deletion cascade for one data subject.

Phase 1 (one Postgres transaction, serialised against derivation by the key row):
  lock the subject's key row FOR UPDATE; tombstone every event of the subject;
  destroy the key (content unreadable); delete every derived row whose input set
  touches a tombstoned event; re-resolve the affected slots from the survivors
  (an erased superseder reinstates its predecessor); invalidate and dirty affected
  concepts (their claim token is cleared, so an in-flight fold cannot commit).
Phase 2 (Elasticsearch, under the exclusive tenant fence):
  deleting a document leaves its bytes in segment files (measured), so purge by
  rebuilding each affected index generation from surviving rows, swapping the
  aliases and deleting the old index in one atomic request.
"""
import json
import time
import uuid

from . import derive as D
from . import esx
from .db import iso
from .esc import ES

from .projector import LOCK_SQL


def phase1(conn, tenant, subject_id, recipes, receipt_id, hook=None):
    t0 = time.time()
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("select 1 from keys where tenant_id=%s and subject_id=%s for update", (tenant, subject_id))
        evs = cur.execute("select event_id, ts_event from events where tenant_id=%s and subject_id=%s", (tenant, subject_id)).fetchall()
        ids = [e[0] for e in evs]
        if hook:
            hook()
        cur.execute("insert into tombstones(tenant_id,event_id,subject_id,receipt_id) select %s, e, %s, %s from unnest(%s::text[]) e on conflict do nothing",
                    (tenant, subject_id, receipt_id, ids))
        cur.execute("update keys set key=null, destroyed_at=coalesce(destroyed_at, now()) where tenant_id=%s and subject_id=%s", (tenant, subject_id))
        deleted = cur.execute("delete from facts where tenant_id=%s and input_event_ids && %s returning recipe_version, subject, predicate, scope, id",
                              (tenant, ids)).fetchall()
        by_recipe, subjects = {}, set()
        fact_ids = {}
        for rv, s, p, sc, fid in deleted:
            fact_ids.setdefault(rv, []).append(fid)
            by_recipe.setdefault(rv, set()).add((s, p, sc))
            subjects.add(s)
        reinstated = 0
        for rv, slots in by_recipe.items():
            D.resolve_slots(cur, tenant, rv, slots)
        c = cur.execute("update concepts set digest=null, input_fact_ids='{}', input_event_ids='{}', dirty=true, gen=gen+1, "
                        "claim_token=null, lease_until=null, pending_since=null, next_fold_at=clock_timestamp(), dirty_since=clock_timestamp() "
                        "where tenant_id=%s and (input_event_ids && %s or concept_id = any(%s))", (tenant, ids, sorted(subjects)))
        concepts = c.rowcount
    return {"event_ids": ids, "ts": [e[1] for e in evs], "fact_ids": fact_ids, "facts_deleted": len(deleted), "concepts_reset": concepts,
            "phase1_s": round(time.time() - t0, 3)}


def _load_events(conn, tenant, b):
    span = esx.BUCKET_DAYS * 86400
    cur = conn.cursor()
    cur.execute("select session_id, seq, event_id, ts_event, actor, trust_origin, kind, subject_id, mem_dec(tenant_id, subject_id, content_enc) "
                "from events e where tenant_id=%s and ts_event >= to_timestamp(%s) and ts_event < to_timestamp(%s) "
                "and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) order by ingest_seq",
                (tenant, b * span, (b + 1) * span))
    return cur


def rebuild_events_bucket(conn, es: ES, tenant, b):
    w = esx.ev_walias(tenant, b)
    olds = esx.alias_targets(es, w)
    if not olds:
        return None
    old = olds[0]
    new = esx.ev_index(tenant, b, esx.gen_of(old) + 1)
    es.req("DELETE", f"/{new}", ok=(200, 404))
    es.req("PUT", f"/{new}")
    lines = []
    for sid, seq, eid, ts, actor, tr, kind, subj, plain in _load_events(conn, tenant, b):
        if plain is None:
            continue
        lines.append({"index": {"_index": new, "_id": eid}})
        lines.append(esx.event_doc(tenant, sid, seq, eid, iso(ts), actor, tr, kind, subj, plain))
    _bulk_plain(es, lines)
    es.req("POST", f"/{new}/_refresh")
    esx.swap_generation(es, old, new, {w: {"is_write_index": True}, esx.ev_ralias(tenant): {}})
    return (old, new, len(lines) // 2)


def rebuild_facts_index(conn, es: ES, tenant, v):
    w = esx.fx_walias(tenant, v)
    olds = esx.alias_targets(es, w)
    if not olds:
        return None
    old = olds[0]
    live = old in esx.alias_targets(es, esx.fx_ralias(tenant))
    new = esx.fx_index(tenant, v, esx.gen_of(old) + 1)
    es.req("DELETE", f"/{new}", ok=(200, 404))
    es.req("PUT", f"/{new}")
    cur = conn.cursor()
    cur.execute("select id, subject, predicate, scope, mem_dec(tenant_id, subject_id, object_enc), modality, status, author, trust_origin, "
                "valid_from, valid_to, input_event_ids, recipe_version, subject_id, cue, rev from facts where tenant_id=%s and recipe_version=%s",
                (tenant, v))
    lines = []
    for (fid, sub, pred, sc, obj, mod, st, au, tr, vf, vt, ie, rv, sid, cue, rev) in cur.fetchall():
        if obj is None:
            continue
        f = dict(id=fid, tenant_id=tenant, subject=sub, predicate=pred, scope=sc, object=obj, modality=mod, status=st, author=au,
                 trust_origin=tr, valid_from=iso(vf), valid_to=iso(vt) if vt else None, input_event_ids=ie, recipe_version=rv,
                 subject_id=sid, cue=cue, rev=rev)
        lines.append({"index": {"_index": new, "_id": fid}})
        lines.append(esx.fact_doc(f))
    _bulk_plain(es, lines)
    es.req("POST", f"/{new}/_refresh")
    aliases = {w: {"is_write_index": True}}
    if live:
        aliases[esx.fx_ralias(tenant)] = {}
    esx.swap_generation(es, old, new, aliases)
    return (old, new, len(lines) // 2)


def _bulk_plain(es, lines, chunk=800):
    from .esc import bulk_errors, ESError
    for i in range(0, len(lines), chunk * 2):
        r = es.bulk(lines[i:i + chunk * 2])
        bad = bulk_errors(r)
        if bad:
            raise ESError(500, bad[:3], "rebuild bulk")


def erase_subject(conn, lockconn, es: ES, tenant, subject_id, recipes=(1,), fence=True, hook_phase1=None, hook_before_es=None, es_purge=True):
    receipt_id = uuid.uuid4().hex[:16]
    t0 = time.time()
    p1 = phase1(conn, tenant, subject_id, recipes, receipt_id, hook_phase1)
    t1 = time.time()
    rebuilt = []
    if es_purge:
        if hook_before_es:
            hook_before_es()
        if fence:
            lockconn.execute(f"select pg_advisory_lock({LOCK_SQL})", (tenant,))
        try:
            buckets = sorted({esx.bucket_of(ts) for ts in p1["ts"]})
            for b in buckets:
                rebuilt.append(["events", b, rebuild_events_bucket(conn, es, tenant, b)])
            for v in recipes:
                rebuilt.append(["facts", v, rebuild_facts_index(conn, es, tenant, v)])
        finally:
            if fence:
                lockconn.execute(f"select pg_advisory_unlock({LOCK_SQL})", (tenant,))
    t2 = time.time()
    # verification queries (receipt evidence)
    ids = p1["event_ids"]
    cur = conn.cursor()
    ver = {
        "events_tombstoned": cur.execute("select count(*) from tombstones where tenant_id=%s and receipt_id=%s", (tenant, receipt_id)).fetchone()[0],
        "events_still_decryptable": cur.execute("select count(*) from events where tenant_id=%s and subject_id=%s and mem_dec(tenant_id, subject_id, content_enc) is not null", (tenant, subject_id)).fetchone()[0],
        "key_destroyed": cur.execute("select count(*) from keys where tenant_id=%s and subject_id=%s and key is null and destroyed_at is not null", (tenant, subject_id)).fetchone()[0],
        "facts_touching_tombstoned": cur.execute("select count(*) from facts where tenant_id=%s and input_event_ids && %s", (tenant, ids)).fetchone()[0],
        "concepts_touching_tombstoned": cur.execute("select count(*) from concepts where tenant_id=%s and input_event_ids && %s", (tenant, ids)).fetchone()[0],
    }
    receipt = {"receipt_id": receipt_id, "tenant": tenant, "subject_id": subject_id, "events_erased": len(ids),
               "facts_deleted": p1["facts_deleted"], "concepts_reset": p1["concepts_reset"], "phase1_s": p1["phase1_s"],
               "es_purge_s": round(t2 - t1, 3), "total_s": round(t2 - t0, 3), "es_rebuilt": rebuilt, "verification": ver,
               "method": "alias-swap rebuild from surviving rows; old index deleted atomically", "bytes_checked_in_es": "see exp/c3 (marker scan)"}
    cur.execute("insert into erasure_receipts(receipt_id, tenant_id, subject_id, doc) values (%s,%s,%s,%s)", (receipt_id, tenant, subject_id, json.dumps(receipt)))
    return receipt

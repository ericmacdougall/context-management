"""Deletion cascade for one data subject (speaker-provenance erasure: events the subject produced and everything
derived from them; data ABOUT the subject spoken by others is NOT covered, see exp/c3 cross-speaker case).

Phase 1 (one Postgres transaction): fence row FOR UPDATE (appends and derivation of this subject now refuse), tombstone
every event, delete every derived fact whose input set touches a tombstoned event (taking the slot locks first),
re-resolve the affected slots from survivors, delete or reset affected concepts, and WRITE A DURABLE erasure_jobs ROW.
Then, idempotently and resumable by the sweeper: destroy the key in the key service (real delete + vacuum + checkpoint),
purge Elasticsearch (rebuild every affected events bucket and EVERY facts index of the tenant from surviving rows, alias
swap + old index delete in one request), verify, write the receipt. Until the job is done, readers hide tombstoned ids.
"""
import json
import time
import uuid

from . import derive as D
from . import esx, kms as _kms
from .db import iso, retry_deadlock
from .esc import ES, ESError, bulk_errors
from .projector import LOCK_SQL


# Registry of derived tables keyed by input event id: (table, event_id column, optional array column). Every derived store
# added later (an extraction cache, a summary table) registers here so the erasure cascade cannot silently miss it.
DERIVED_BY_EVENT = []


def phase1(conn, kms, tenant, subject_id, job_id, hook=None, slot_locks=True, resolve_hook=None):
    t0 = time.time()

    def txn():
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("insert into subjects(tenant_id, subject_id) values (%s,%s) on conflict do nothing", (tenant, subject_id))
            cur.execute("select erased from subjects where tenant_id=%s and subject_id=%s for update", (tenant, subject_id))
            cur.execute("update subjects set erased=true, epoch=epoch+1 where tenant_id=%s and subject_id=%s", (tenant, subject_id))
            evs = cur.execute("select event_id, ts_event from events where tenant_id=%s and subject_id=%s order by event_id", (tenant, subject_id)).fetchall()
            ids = [e[0] for e in evs]
            if hook:
                hook()
            cur.execute("insert into tombstones(tenant_id,event_id,subject_id,receipt_id) select %s, e, %s, %s from unnest(%s::text[]) e on conflict do nothing",
                        (tenant, subject_id, job_id, ids))
            doomed = cur.execute("select recipe_version, slot_hash, entity_hash from facts where tenant_id=%s and input_event_ids && %s", (tenant, ids)).fetchall()
            by_recipe, ents = {}, set()
            for rv, sh, eh in doomed:
                by_recipe.setdefault(rv, set()).add(sh); ents.add(eh)
            for rv in sorted(by_recipe):            # same global order as derivation: slot locks sorted, before any slot is read or changed
                if slot_locks:
                    D.lock_slots(cur, tenant, rv, by_recipe[rv])
            deleted = cur.execute("delete from facts where tenant_id=%s and input_event_ids && %s returning recipe_version", (tenant, ids)).rowcount
            for rv in sorted(by_recipe):
                D.resolve_slots(cur, kms, tenant, rv, by_recipe[rv], after_read=resolve_hook)
            gone = [e for e in ents if not cur.execute("select 1 from facts where tenant_id=%s and entity_hash=%s limit 1", (tenant, e)).fetchone()]
            cdel = cur.execute("delete from concepts where tenant_id=%s and concept_id = any(%s)", (tenant, gone)).rowcount if gone else 0
            c = cur.execute("update concepts set digest=null, input_fact_ids='{}', input_event_ids='{}', dirty=true, gen=gen+1, claim_token=null, lease_until=null, "
                            "pending_since=null, next_fold_at=clock_timestamp(), dirty_since=clock_timestamp() "
                            "where tenant_id=%s and (input_event_ids && %s or concept_id = any(%s))", (tenant, ids, sorted(ents))).rowcount
            purged = {}
            for table, col in DERIVED_BY_EVENT:
                purged[table] = cur.execute(f"delete from {table} where tenant_id=%s and {col} = any(%s)", (tenant, ids)).rowcount
            cur.execute("insert into erasure_jobs(job_id, tenant_id, subject_id, event_ids, event_ts) values (%s,%s,%s,%s,%s)",
                        (job_id, tenant, subject_id, ids, [e[1] for e in evs]))
            return {"events": len(ids), "registered_tables_purged": purged, "facts_deleted": deleted, "concepts_deleted": cdel, "concepts_reset": c}
    r = retry_deadlock(txn)
    r["phase1_s"] = round(time.time() - t0, 3)
    return r


# ------------------------------------------------------------------ Elasticsearch purge
def _bulk_plain(es, lines, chunk=800):
    for i in range(0, len(lines), chunk * 2):
        r = es.bulk(lines[i:i + chunk * 2])
        bad = bulk_errors(r)
        if bad:
            raise ESError(500, bad[:3], "rebuild bulk")


def rebuild_events_bucket(conn, kms, es: ES, tenant, b):
    olds = esx._targets(es, f"sys-{tenant}-events-w{b:05d}-g*")
    if not olds:
        return None
    old = olds[0]
    g = esx.gen_of(old)
    new = esx.ev_index(tenant, b, g + 1)
    es.req("DELETE", f"/{new}", ok=(200, 404))
    es.req("PUT", f"/{new}")
    span = esx.BUCKET_DAYS * 86400
    from . import store
    rows = store.read_plain(conn, tenant, "ts_event >= to_timestamp(%s) and ts_event < to_timestamp(%s) and not exists "
                            "(select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) order by ingest_seq",
                            (b * span, (b + 1) * span), kms)
    lines = []
    for r in rows:
        if r["plain"] is None:
            continue
        lines.append({"index": {"_index": new, "_id": r["event_id"]}})
        lines.append(esx.event_doc(tenant, r["session_id"], r["seq"], r["event_id"], iso(r["ts_event"]), r["actor"], r["trust_origin"], r["kind"], r["subject_id"], r["plain"]))
    _bulk_plain(es, lines)
    es.req("POST", f"/{new}/_refresh")
    esx.swap_generation(es, old, new, {esx.ev_walias(tenant, b, g + 1): {"is_write_index": True}, esx.ev_ralias(tenant): {}})
    conn.execute("insert into es_buckets(tenant_id,bucket,gen) values (%s,%s,%s) on conflict (tenant_id,bucket) do update set gen=excluded.gen", (tenant, b, g + 1))
    return (old, new, len(lines) // 2)


def rebuild_facts_index(conn, kms, es: ES, tenant, v):
    olds = esx._targets(es, f"sys-{tenant}-facts-w{v}-g*")
    if not olds:
        return None
    old = olds[0]
    g = esx.gen_of(old)
    live = old in esx.alias_targets(es, esx.fx_ralias(tenant))
    new = esx.fx_index(tenant, v, g + 1)
    es.req("DELETE", f"/{new}", ok=(200, 404))
    es.req("PUT", f"/{new}")
    cur = conn.cursor()
    cur.execute("select id, slot_enc, object_enc, modality, status, author, trust_origin, valid_from, valid_to, input_event_ids, recipe_version, subject_id, rev "
                "from facts where tenant_id=%s and recipe_version=%s", (tenant, v))
    rows = cur.fetchall()
    n = len(rows)
    dec = kms.dec_many(tenant, [(r[11], r[1]) for r in rows] + [(r[11], r[2]) for r in rows])
    lines = []
    for i, r in enumerate(rows):
        if dec[i] is None or dec[n + i] is None:
            continue
        sub, pred, scope, cue = D.decode_slot(dec[i])
        f = {"id": r[0], "tenant_id": tenant, "subject": sub, "predicate": pred, "scope": scope, "object": dec[n + i], "modality": r[3], "status": r[4], "author": r[5],
             "trust_origin": r[6], "valid_from": iso(r[7]), "valid_to": iso(r[8]) if r[8] else None, "input_event_ids": r[9], "recipe_version": r[10],
             "subject_id": r[11], "cue": cue, "rev": r[12]}
        lines.append({"index": {"_index": new, "_id": r[0]}})
        lines.append(esx.fact_doc(f))
    _bulk_plain(es, lines)
    es.req("POST", f"/{new}/_refresh")
    aliases = {esx.fx_walias(tenant, v, g + 1): {"is_write_index": True}}
    if live:
        aliases[esx.fx_ralias(tenant)] = {}
    esx.swap_generation(es, old, new, aliases)
    conn.execute("insert into es_buckets(tenant_id,bucket,gen) values (%s,%s,%s) on conflict (tenant_id,bucket) do update set gen=excluded.gen", (tenant, -v, g + 1))
    return (old, new, len(lines) // 2)


def es_purge(conn, lockconn, kms, es, job, fence=True):
    tenant = job["tenant_id"]
    if fence:
        lockconn.execute(f"select pg_advisory_lock({LOCK_SQL})", (tenant,))
    rebuilt = []
    try:
        for b in sorted({esx.bucket_of(ts) for ts in job["event_ts"]}):
            rebuilt.append(["events", b, rebuild_events_bucket(conn, kms, es, tenant, b)])
        for v in sorted(esx.facts_versions(es, tenant)):      # EVERY facts index of the tenant, not only the recipes the caller knows
            rebuilt.append(["facts", v, rebuild_facts_index(conn, kms, es, tenant, v)])
    finally:
        if fence:
            lockconn.execute(f"select pg_advisory_unlock({LOCK_SQL})", (tenant,))
    return rebuilt


def verify(conn, kms, es, job):
    tenant, subject_id, ids = job["tenant_id"], job["subject_id"], job["event_ids"]
    cur = conn.cursor()
    rows = cur.execute("select subject_id, content_enc from events where tenant_id=%s and subject_id=%s", (tenant, subject_id)).fetchall()
    readable = sum(p is not None for p in kms.dec_many(tenant, [(r[0], r[1]) for r in rows]))
    es_ev = es.req("POST", f"/sys-{tenant}-events/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"event_id": ids}}}, ok=(200, 404))[1]
    es_ev = es_ev.get("hits", {}).get("total", {}).get("value", 0)
    fx_idx = [r["index"] for r in esx.tenant_indices(es, tenant) if "-facts-v" in r["index"]]
    es_fx = 0
    if fx_idx:
        es_fx = es.req("POST", f"/{','.join(fx_idx)}/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"input_event_ids": ids}}})[1]["hits"]["total"]["value"]
    return {"events_tombstoned": cur.execute("select count(*) from tombstones where tenant_id=%s and subject_id=%s", (tenant, subject_id)).fetchone()[0],
            "events_still_decryptable": readable, "key_destroyed": kms.is_destroyed(tenant, subject_id),
            "pg_facts_touching_tombstoned": cur.execute("select count(*) from facts where tenant_id=%s and input_event_ids && %s", (tenant, ids)).fetchone()[0],
            "pg_concepts_touching_tombstoned": cur.execute("select count(*) from concepts where tenant_id=%s and input_event_ids && %s", (tenant, ids)).fetchone()[0],
            "es_event_docs": es_ev, "es_fact_docs_touching": es_fx, "es_facts_indices_checked": len(fx_idx)}


def load_job(conn, job_id):
    cur = conn.cursor()
    cur.execute("select job_id, tenant_id, subject_id, state, event_ids, event_ts, attempts from erasure_jobs where job_id=%s", (job_id,))
    r = cur.fetchone()
    return dict(zip(("job_id", "tenant_id", "subject_id", "state", "event_ids", "event_ts", "attempts"), r))


def _inject(fail_at, point):
    import os, signal
    if fail_at == point:
        raise RuntimeError(f"injected: {point}")
    if fail_at == "kill:" + point:
        os.kill(os.getpid(), signal.SIGKILL)


def complete_job(conn, lockconn, es, job_id, fence=True, kms=None, fail_at=None, es_purge_enabled=True):
    """Run the remaining steps of a job; each is idempotent and the state advances only after the step succeeded.
    fail_at: test injection, raises RuntimeError('injected: <step>') at the named point."""
    kms = kms or _kms.get()
    job = load_job(conn, job_id)
    t0 = time.time()
    try:
        conn.execute("update erasure_jobs set attempts=attempts+1, updated_at=now() where job_id=%s", (job_id,))
        if job["state"] == "pending":
            _inject(fail_at, "before_key_destroy")
            kms.destroy(job["tenant_id"], job["subject_id"])
            conn.execute("update erasure_jobs set state='key_destroyed', updated_at=now() where job_id=%s", (job_id,)); job["state"] = "key_destroyed"
        rebuilt = []
        if job["state"] == "key_destroyed":
            _inject(fail_at, "before_es_purge")
            if es_purge_enabled:
                rebuilt = es_purge(conn, lockconn, kms, es, job, fence)
            _inject(fail_at, "after_es_purge")
            conn.execute("update erasure_jobs set state='es_purged', updated_at=now() where job_id=%s", (job_id,)); job["state"] = "es_purged"
        ver = verify(conn, kms, es, job)
        receipt = {"receipt_id": job_id, "tenant": job["tenant_id"], "subject_id": job["subject_id"], "events_erased": len(job["event_ids"]), "es_rebuilt": rebuilt,
                   "verification": ver, "attempts": job["attempts"] + 1, "es_and_key_steps_s": round(time.time() - t0, 3),
                   "scope": "speaker-provenance erasure only (events produced by this subject and rows derived from them)"}
        conn.execute("insert into erasure_receipts(receipt_id, tenant_id, subject_id, doc) values (%s,%s,%s,%s) on conflict (receipt_id) do update set doc=excluded.doc",
                     (job_id, job["tenant_id"], job["subject_id"], json.dumps(receipt, default=str)))
        conn.execute("update erasure_jobs set state='done', last_error=null, updated_at=now() where job_id=%s", (job_id,))
        return receipt
    except Exception as e:
        conn.execute("update erasure_jobs set last_error=%s, updated_at=now() where job_id=%s", (f"{type(e).__name__}: {str(e)[:200]}", job_id))
        raise


def sweep(conn, lockconn, es, fence=True):
    """Complete every open erasure job (run periodically and at start-up). Returns {job_id: 'done'|error}."""
    out = {}
    for (jid,) in conn.execute("select job_id from erasure_jobs where state <> 'done' order by created_at").fetchall():
        try:
            complete_job(conn, lockconn, es, jid, fence)
            out[jid] = "done"
        except Exception as e:
            out[jid] = f"{type(e).__name__}: {str(e)[:100]}"
    return out


def erase_subject(conn, lockconn, es: ES, tenant, subject_id, recipes=None, fence=True, hook_phase1=None, fail_at=None, es_purge=True, kms=None, slot_locks=True, resolve_hook=None):
    """phase 1 + completion. `recipes` is accepted for compatibility and ignored: every facts index is discovered.
    On an injected/real failure after phase 1 the job stays open (the sweeper finishes it) and the exception propagates."""
    kms = kms or _kms.get()
    job_id = uuid.uuid4().hex[:16]
    p1 = phase1(conn, kms, tenant, subject_id, job_id, hook_phase1, slot_locks, resolve_hook)
    _inject(fail_at, "after_phase1")
    receipt = complete_job(conn, lockconn, es, job_id, fence, kms, fail_at, es_purge)
    receipt.update({"facts_deleted": p1["facts_deleted"], "concepts_deleted": p1["concepts_deleted"], "concepts_reset": p1["concepts_reset"], "phase1_s": p1["phase1_s"]})
    return receipt

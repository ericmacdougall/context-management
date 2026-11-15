"""Erasure of one data subject (speaker provenance: events the subject produced and everything derived from them).

  request()  durable record first (state 'requested'), before any work: an outage later cannot lose the request.
  phase 1    ONE transaction: subject fence FOR UPDATE + erased, tombstones, delete derived rows in every registered
             Postgres store, and take the projector lease by force (epoch E). Tombstones and the epoch bump commit
             together, so every reader snapshot without the tombstones carries an epoch < E.
  fence      alias CAS to E for every family (stale writers can no longer land anywhere readers look).
  purge      per registered family: fresh index, staging alias attached under a CAS on w<E>, survivors read AFTER the
             fence, bulk through the staging alias, then one atomic request that deletes every other index of the family,
             removes the staging alias (must_exist: fails if a newer holder fenced) and points read and write aliases
             at the new index.
  keys       per-subject key destroyed (independent of the purge; an outage here does not block it).
  verify     a count per verifiable registered store; the store accepts state 'done' only if every count is 0.
Readers filter tombstones unconditionally, so nothing erased is served while a job is open or after it fails."""
import json
import time
import uuid

import psycopg

from . import families as F, keys as K, lease as L, registry as R
from .es import ESError
from .projector import event_actions, fact_actions


def _noop(_n):
    pass


def request(conn, tenant, subject_id):
    jid = uuid.uuid4().hex[:16]
    conn.execute("insert into erasure_jobs(job_id, tenant_id, subject_id, lease_scope, lease_epoch) values (%s,%s,%s,%s,0)", (jid, tenant, subject_id, L.PROJECTOR))
    return jid


def _job(conn, jid):
    r = conn.execute("select tenant_id, subject_id, state, event_ids from erasure_jobs where job_id=%s", (jid,)).fetchone()
    return dict(zip(("tenant", "subject", "state", "event_ids"), r))


def phase1(conn, jid, ttl_s, hook=_noop):
    j = _job(conn, jid)
    t, s = j["tenant"], j["subject"]
    with conn.transaction():
        conn.execute("select 1 from erasure_jobs where job_id=%s for update", (jid,))
        conn.execute("insert into subjects(tenant_id, subject_id) values (%s,%s) on conflict do nothing", (t, s))
        conn.execute("select 1 from subjects where tenant_id=%s and subject_id=%s for update", (t, s))
        conn.execute("update subjects set erased=true where tenant_id=%s and subject_id=%s", (t, s))
        ids = [r[0] for r in conn.execute("select event_id from events where tenant_id=%s and subject_id=%s order by event_id", (t, s)).fetchall()]
        conn.execute("insert into tombstones(tenant_id, event_id, subject_id, job_id) select %s, e, %s, %s from unnest(%s::text[]) e on conflict do nothing", (t, s, jid, ids))
        counts = {
            "facts": conn.execute("delete from facts where tenant_id=%s and (input_event_ids && %s or subject_id=%s)", (t, ids, s)).rowcount,
            "inbox": conn.execute("delete from inbox where tenant_id=%s and subject_id=%s", (t, s)).rowcount,
            "analyst_jobs": conn.execute("delete from analyst_jobs where tenant_id=%s and input_event_ids && %s", (t, ids)).rowcount,
            "quarantine": conn.execute("delete from quarantine where tenant_id=%s and subject_id=%s", (t, s)).rowcount,
        }
        hook("phase1_before_bump")
        lease = L.acquire_in_txn(conn, t, L.PROJECTOR, ttl_s, holder="erasure:" + jid)
        conn.execute("update erasure_jobs set state='fenced', event_ids=%s, attempts=attempts+1, lease_scope=%s, lease_epoch=%s, updated_at=clock_timestamp() where job_id=%s",
                     (ids, L.PROJECTOR, lease.epoch, jid))
    return lease, ids, counts


def _set(conn, jid, lease, **kv):
    cols = ", ".join(f"{k}=%s" for k in kv)
    with conn.transaction():
        conn.execute(f"update erasure_jobs set {cols}, lease_scope=%s, lease_epoch=%s, updated_at=clock_timestamp() where job_id=%s",
                     tuple(json.dumps(v) if isinstance(v, dict) else v for v in kv.values()) + (L.PROJECTOR, lease.epoch, jid))


def _survivors(conn, t, f):
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read")
        if f == "events":
            return conn.execute(
                "select e.event_id, e.session_id, e.seq, e.kind, e.subject_id, e.actor, e.trust_origin, e.ts_event, e.content_enc from events e where e.tenant_id=%s "
                "and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) "
                "and not exists (select 1 from subjects sj where sj.tenant_id=e.tenant_id and sj.subject_id=e.subject_id and sj.erased) order by e.ingest_seq", (t,)).fetchall()
        return conn.execute(
            "select f.fact_id, f.subject_id, f.body_enc, f.valid_from, f.valid_to, f.input_event_ids, f.rev from facts f where f.tenant_id=%s "
            "and not exists (select 1 from tombstones tb where tb.tenant_id=f.tenant_id and tb.event_id = any(f.input_event_ids)) order by f.rev", (t,)).fetchall()


def rebuild_family(conn, ks, es, t, f, epoch, hook=_noop):
    new = F.rebuilt_index(t, f, epoch)
    es.req("PUT", f"/{new}", {"settings": F.SETTINGS, "mappings": F.FAMILIES[f]["mappings"]})
    try:
        F.assert_epoch(es, t, f, epoch, [{"add": {"index": new, "alias": F.stage_alias(t, f, epoch)}}])
    except F.StaleEpoch:
        es.req("DELETE", f"/{new}", ok=(200, 404))
        raise
    rows = _survivors(conn, t, f)
    hook(f"rebuild_after_read:{f}")
    target = F.stage_alias(t, f, epoch)
    acts = event_actions(ks, t, rows, target) if f == "events" else fact_actions(ks, t, rows, target)
    for i in range(0, len(acts), 500):
        ok, bad = es.bulk_fenced(acts[i:i + 500])
        hard = [b for b in bad if b[1] != 409]
        if hard:
            raise F.StaleEpoch(f"rebuild {f}: {len(hard)} refused ({hard[0][2]})")
    es.req("POST", f"/{new}/_refresh")
    hook(f"rebuild_before_swap:{f}")
    others = [i for i in F._family_state(es, t, f) if i != new]
    actions = [{"remove_index": {"index": i}} for i in others] + [
        {"remove": {"index": new, "alias": F.stage_alias(t, f, epoch), "must_exist": True}},
        {"add": {"index": new, "alias": F.write_alias(t, f, epoch), "is_write_index": True}},
        {"add": {"index": new, "alias": F.read_alias(t, f)}}]
    try:
        es.aliases(actions)
    except ESError as e:
        raise F.StaleEpoch(f"swap {f} refused: {e.etype}") from None
    return {"family": f, "new_index": new, "docs": len(acts), "deleted_indices": len(others)}


def verify(conn, kconn_ks, es, t, s, ids):
    ks = kconn_ks
    out = {}
    key_present = ks.key_exists(t, s)
    for st in R.verifiable():
        if st.name == "pg:events":
            n = conn.execute("select count(*) from events where tenant_id=%s and subject_id=%s", (t, s)).fetchone()[0]
            out[st.name] = n if key_present else 0          # ciphertext without a key is not recoverable content
        elif st.name == "pg:quarantine":
            out[st.name] = conn.execute("select count(*) from quarantine where tenant_id=%s and subject_id=%s", (t, s)).fetchone()[0]
        elif st.name == "pg:inbox":
            out[st.name] = conn.execute("select count(*) from inbox where tenant_id=%s and subject_id=%s", (t, s)).fetchone()[0]
        elif st.name == "pg:analyst_jobs":
            out[st.name] = conn.execute("select count(*) from analyst_jobs where tenant_id=%s and input_event_ids && %s", (t, ids)).fetchone()[0]
        elif st.name == "pg:facts":
            out[st.name] = conn.execute("select count(*) from facts where tenant_id=%s and (input_event_ids && %s or subject_id=%s)", (t, ids, s)).fetchone()[0]
        elif st.name == "keys:subject_keys":
            out[st.name] = int(key_present)
        elif st.kind == "es":
            q = {"bool": {"should": [{"term": {"subject_id": s}}, {"terms": {"event_id": ids or ["-"]}}, {"terms": {"input_event_ids": ids or ["-"]}}]}}
            es.req("POST", f"/{F.P}{t}-{st.where}-*/_refresh", ok=(200, 404), expand_wildcards="all", ignore_unavailable=True, allow_no_indices=True)
            out[st.name] = es.count(f"{F.P}{t}-{st.where}-*", q)   # every index of the family, aliased or orphaned
        else:
            raise RuntimeError(f"no verifier for registered store {st.name}")
    return out


def run(conn, ks: K.KeyService, es, jid, ttl_s=120.0, hook=_noop, destroy_key=True):
    """Drive a job to 'done' or leave it open with its residue recorded. Returns the receipt."""
    t0 = time.time()
    j = _job(conn, jid)
    t, s = j["tenant"], j["subject"]
    if j["state"] == "requested":
        lease, ids, p1 = phase1(conn, jid, ttl_s, hook)
    else:                                                    # resume (sweeper): take the lease again under a new epoch
        lease = L.acquire(conn, t, L.PROJECTOR, ttl_s, force=True, holder="erasure:" + jid)
        ids, p1 = j["event_ids"], {}
        _set(conn, jid, lease, attempts=_attempts(conn, jid) + 1)
    hook("after_phase1")
    rebuilt, err = [], None
    try:
        F.fence_es(es, t, lease.epoch)
        hook("after_fence")
        for f in F.FAMILIES:
            rebuilt.append(rebuild_family(conn, ks, es, t, f, lease.epoch, hook))
        _set(conn, jid, lease, state="purged")
    except (F.StaleEpoch, ESError, psycopg.Error) as e:
        err = type(e).__name__
    if destroy_key:
        try:
            ks.destroy(t, s)
            if err is None:
                _set(conn, jid, lease, state="keys_destroyed")
        except Exception as e:  # noqa: BLE001
            err = err or type(e).__name__
    v = verify(conn, ks, es, t, s, ids)
    done = False
    try:
        _set(conn, jid, lease, state="done", verify=v)
        done = True
    except psycopg.Error as e:
        err = err or getattr(e, "sqlstate", type(e).__name__)
        try:
            _set(conn, jid, lease, verify=v, last_error_type=err)
        except psycopg.Error:
            pass
    L.release(conn, lease)
    return {"job_id": jid, "tenant": t, "subject": s, "done": done, "epoch": lease.epoch, "events": len(ids), "phase1_deleted": p1,
            "rebuilt": rebuilt, "verify": v, "error_type": err, "seconds": round(time.time() - t0, 3),
            "untested_stores": [x.name for x in R.STORES if x.kind == "external"]}


def _attempts(conn, jid):
    return conn.execute("select attempts from erasure_jobs where job_id=%s", (jid,)).fetchone()[0]


def erase(conn, ks, es, tenant, subject_id, **kw):
    return run(conn, ks, es, request(conn, tenant, subject_id), **kw)


def sweep(conn, ks, es):
    return {jid: run(conn, ks, es, jid)["done"] for (jid,) in conn.execute("select job_id from erasure_jobs where state <> 'done' order by created_at").fetchall()}

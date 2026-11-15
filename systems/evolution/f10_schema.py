"""F10: Postgres log schema evolution under the append-only trigger and hash chain, online, under 16 writers.
usage: f10_schema.py [writers=16] [pre_sessions=200]"""
import datetime as dt, hashlib, json, multiprocessing as mp, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from evolution import evo, task, taskview, scen, upcast
from memsys import chain, store
from memsys.db import iso

T = "f10"
HOT = "H"
TS = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def append_v2col(conn, tenant, session, e, pv):
    """Same append as memsys.store but also fills the new payload_version column (outside the hash)."""
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("insert into sessions(tenant_id, session_id) values (%s,%s) on conflict do nothing", (tenant, session))
        t0 = time.perf_counter()
        nxt, last = cur.execute("select next_seq,last_hash from sessions where tenant_id=%s and session_id=%s for update", (tenant, session)).fetchone()
        wait = time.perf_counter() - t0
        enc = cur.execute("select mem_enc(%s,%s,%s)", (tenant, e["subject_id"], e["content"])).fetchone()[0]
        eid = f"{tenant}/{session}/{nxt}"
        h = chain.event_hash(last, tenant, session, nxt, eid, iso(e["ts_event"]), e["actor"], e["trust_origin"], e["kind"], e["subject_id"], enc)
        cur.execute("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash,payload_version) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (tenant, session, nxt, eid, e["ts_event"], e["subject_id"], e["actor"], e["trust_origin"], e["kind"], enc, last, h, pv))
        cur.execute("update sessions set next_seq=%s, last_hash=%s where tenant_id=%s and session_id=%s", (nxt + 1, h, tenant, session))
    return wait


def writer(args):
    w, = args
    conn = evo.connect()
    n, fails, waits, lats, per_phase, shapes = 0, 0, [], [], {}, {"v1": 0, "v2": 0}
    phase, err_examples = 0, []
    while True:
        if n % 50 == 0:
            phase, stop = conn.execute("select (select phase from mig_phase), (select stop from mig_phase)").fetchone()
            conn.execute("insert into writer_ack(w, phase) values (%s,%s) on conflict (w) do update set phase=excluded.phase", (w, phase))
            if stop:
                break
        body = {"step_id": f"w{w}-n{n}", "artifacts": [f"w{w}n{n}"]}
        if phase >= 3:
            body = upcast.encode_v2({**body, "outcome": "ok"}); shapes["v2"] += 1
        else:
            shapes["v1"] += 1
        e = dict(ts_event=TS + dt.timedelta(milliseconds=n * 16 + w), actor="assistant", trust_origin="assistant", kind="step_completed", subject_id="u1", content=json.dumps(body, sort_keys=True))
        t0 = time.perf_counter()
        try:
            if phase == 0:
                _, wait = store.append_events(conn, T, HOT, [e])
            else:
                wait = append_v2col(conn, T, HOT, e, upcast.stored_version(body))
            waits.append(wait); lats.append(time.perf_counter() - t0)
            per_phase[phase] = per_phase.get(phase, 0) + 1
        except Exception as ex:
            fails += 1; err_examples.append(f"{type(ex).__name__}: {str(ex)[:120]}")
            try: conn = evo.connect()
            except Exception: pass
        n += 1
    return {"w": w, "ok": sum(per_phase.values()), "fails": fails, "waits": waits, "lats": lats, "per_phase": per_phase, "shapes": shapes, "errs": err_examples[:3]}


def pct(xs, ps=(50, 99, 100)):
    xs = sorted(xs)
    return {f"p{p}": round(xs[min(len(xs) - 1, int(len(xs) * p / 100 - 1e-9))], 4) for p in ps} if xs else {}


def states(conn, sessions):
    out = {}
    for s in sessions:
        rows = conn.execute("select seq, kind, trust_origin, mem_dec(tenant_id, subject_id, content_enc) from events where tenant_id=%s and session_id=%s order by seq", (T, s)).fetchall()
        evs = [{"seq": q, "kind": k, "trust_origin": t, "body": upcast.upcast(k, json.loads(c))} for q, k, t, c in rows]
        out[s] = task.canon(task.task_state(evs))
    return out


def chains_ok(conn, sessions):
    bad = []
    for s in sessions:
        ok, b, n = chain.verify_chain(list(store.session_rows(conn, T, s)))
        if not ok: bad.append((s, b))
    return bad


def ddl(conn, sql, label, log, lock_timeout="2s", retries=20):
    """Run DDL with a lock_timeout and retry, recording how long we waited for the lock and how long it ran."""
    t_start = time.perf_counter(); tries = 0
    while True:
        tries += 1
        try:
            conn.execute(f"set lock_timeout = '{lock_timeout}'")
            t0 = time.perf_counter(); conn.execute(sql); dur = time.perf_counter() - t0
            break
        except psycopg.errors.LockNotAvailable:
            if tries >= retries: raise
            time.sleep(0.2)
    conn.execute("set lock_timeout = 0")
    log.append({"step": label, "attempts": tries, "wall_s": round(time.perf_counter() - t_start, 3), "last_statement_s": round(dur, 3)})


def main():
    W = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    PRE = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    conn = evo.connect(); evo.reset(conn)
    conn.execute("create table mig_phase(phase int not null, stop boolean not null default false); insert into mig_phase values (0, false);"
                 "create table writer_ack(w int primary key, phase int)")
    rng = random.Random(10)
    pre = [f"pre{i}" for i in range(PRE)]
    for i, s in enumerate(pre):
        sc = scen.gen_scenario(rng)
        taskview.append_many(conn, T, s, sc["events"])
    S0 = states(conn, pre)
    fp0 = {s: conn.execute("select md5(string_agg(hash, ',' order by seq)) from events where tenant_id=%s and session_id=%s", (T, s)).fetchone()[0] for s in pre}
    n_pre = conn.execute("select count(*) from events").fetchone()[0]
    out = {"workload": f"{W} writer processes appending 1 event/txn to ONE session at the C7 rate while the migration runs; {PRE} pre-existing task sessions ({n_pre} events); PG16 4cpu/4g on t2-dev",
           "pre_events": n_pre}
    log = []
    ctx = mp.get_context("fork")
    pool = ctx.Pool(W)
    res = pool.map_async(writer, [(w,) for w in range(W)])
    mc = evo.connect(); time.sleep(6)                          # baseline under load
    t_mig = time.time()
    ddl(mc, "alter table events add column payload_version smallint", "add nullable column", log)
    ddl(mc, """create or replace function events_append_only() returns trigger language plpgsql as $$
      begin
        if TG_OP = 'UPDATE' and OLD.payload_version is null and NEW.payload_version is not null and
           (OLD.tenant_id, OLD.session_id, OLD.seq, OLD.event_id, OLD.ingest_seq, OLD.commit_xid, OLD.ts_event, OLD.ts_ingested, OLD.subject_id, OLD.actor, OLD.trust_origin, OLD.kind, OLD.content_enc, OLD.prev_hash, OLD.hash)
           is not distinct from
           (NEW.tenant_id, NEW.session_id, NEW.seq, NEW.event_id, NEW.ingest_seq, NEW.commit_xid, NEW.ts_event, NEW.ts_ingested, NEW.subject_id, NEW.actor, NEW.trust_origin, NEW.kind, NEW.content_enc, NEW.prev_hash, NEW.hash)
        then return NEW; end if;
        raise exception 'events is append-only (only a first fill of payload_version is allowed)';
      end $$""", "replace trigger: fill-once exception for the derived column", log)
    mc.execute("update mig_phase set phase = 1")              # writers now also set payload_version on insert
    time.sleep(1.5)
    t0 = time.perf_counter(); batches, rows, bmax = 0, 0, 0.0
    while True:
        tb = time.perf_counter()
        r = mc.execute("with b as (select tenant_id, session_id, seq from events where payload_version is null limit 5000) "
                       "update events e set payload_version = coalesce((case when e.kind = 'step_completed' then (mem_dec(e.tenant_id, e.subject_id, e.content_enc)::jsonb ->> '_v') end)::smallint, 1) "
                       "from b where e.tenant_id=b.tenant_id and e.session_id=b.session_id and e.seq=b.seq")
        d = time.perf_counter() - tb
        if r.rowcount == 0:
            break
        batches += 1; rows += r.rowcount; bmax = max(bmax, d)
    log.append({"step": "batched backfill", "batches": batches, "rows": rows, "total_s": round(time.perf_counter() - t0, 2), "max_batch_s": round(bmax, 3)})
    acks = {w: p for w, p in mc.execute("select w, phase from writer_ack").fetchall()}
    # a writer that has not yet acked phase 1 may still insert NULL: sweep again once all acked
    t1 = time.time()
    while any(p < 1 for p in acks.values()) and time.time() - t1 < 20:
        time.sleep(0.2); acks = {w: p for w, p in mc.execute("select w, phase from writer_ack").fetchall()}
    mc.execute("update events set payload_version = 1 where payload_version is null and false") if False else None
    while mc.execute("update events e set payload_version = coalesce((case when e.kind = 'step_completed' then (mem_dec(e.tenant_id, e.subject_id, e.content_enc)::jsonb ->> '_v') end)::smallint, 1) "
                     "where payload_version is null").rowcount:
        pass
    ddl(mc, "alter table events add constraint payload_version_set check (payload_version is not null) not valid", "add constraint NOT VALID", log)
    t0 = time.perf_counter(); mc.execute("alter table events validate constraint payload_version_set"); log.append({"step": "validate constraint", "s": round(time.perf_counter() - t0, 3)})
    mc.execute("update mig_phase set phase = 3")              # writers now emit the v2 shape
    time.sleep(6)
    mc.execute("update mig_phase set stop = true")
    wres = res.get(); pool.close()
    out["migration_wall_s"] = round(time.time() - t_mig - 6 - 1.5, 1)
    out["steps"] = log
    allw = [x for r in wres for x in r["waits"]]; alll = [x for r in wres for x in r["lats"]]
    out["writers"] = {"appends_ok": sum(r["ok"] for r in wres), "failed_appends": sum(r["fails"] for r in wres), "errors": [e for r in wres for e in r["errs"]][:3],
                      "per_phase": {str(k): sum(r["per_phase"].get(k, 0) for r in wres) for k in (0, 1, 3)}, "shapes_written": {k: sum(r["shapes"][k] for r in wres) for k in ("v1", "v2")},
                      "session_row_lock_wait_s": pct(allw), "append_latency_s": pct(alll)}
    # ---- verification
    mx = conn.execute("select count(*), max(seq) from events where tenant_id=%s and session_id=%s", (T, HOT)).fetchone()
    nulls = conn.execute("select count(*) from events where payload_version is null").fetchone()[0]
    ver = dict(conn.execute("select payload_version, count(*) from events group by 1 order by 1").fetchall())
    bad = chains_ok(conn, [HOT] + pre)
    S1 = states(conn, pre)
    fp1 = {s: conn.execute("select md5(string_agg(hash, ',' order by seq)) from events where tenant_id=%s and session_id=%s", (T, s)).fetchone()[0] for s in pre}
    hot = [json.loads(c) for (c,) in conn.execute("select mem_dec(tenant_id, subject_id, content_enc) from events where tenant_id=%s and session_id=%s order by seq", (T, HOT)).fetchall()]
    upcasts_ok = all(set(upcast.upcast("step_completed", b)) == {"step_id", "artifacts", "outcome"} for b in hot)
    ids = [b["step_id"] for b in hot]
    # append-only still holds for everything else
    blocked = []
    for sql in ("update events set kind='x' where tenant_id='f10' and seq=1 and session_id='H'", "update events set payload_version=1 where tenant_id='f10' and session_id='H' and seq=1",
                "delete from events where tenant_id='f10' and session_id='H' and seq=1", "update events set payload_version=null where tenant_id='f10' and session_id='H' and seq=1"):
        try: conn.execute(sql); blocked.append(False)
        except psycopg.errors.RaiseException: blocked.append(True)
        except psycopg.errors.CheckViolation: blocked.append(True)
    out["verify"] = {"hot_rows": mx[0], "hot_gapless": mx[0] == mx[1], "hot_unique_payloads": len(set(ids)), "hot_lost_or_dup": mx[0] - len(set(ids)) + (out["writers"]["appends_ok"] - mx[0]),
                     "null_payload_version_rows": nulls, "rows_by_payload_version": ver, "chain_breaks": bad, "pre_sessions_state_identical": S0 == S1,
                     "pre_sessions_hash_fingerprint_unchanged": fp0 == fp1, "all_hot_events_upcast_to_current_shape": upcasts_ok,
                     "append_only_still_enforced_for_other_updates": blocked}
    v = out["verify"]
    waits_ok = out["writers"]["append_latency_s"].get("p100", 99) < 3.0
    out["longest_writer_lock_wait_s"] = out["writers"]["session_row_lock_wait_s"].get("p100")
    out["longest_ddl_wall_s"] = max(l.get("wall_s", 0) for l in log)
    out["pass"] = bool(out["writers"]["failed_appends"] == 0 and not v["chain_breaks"] and v["pre_sessions_state_identical"] and v["pre_sessions_hash_fingerprint_unchanged"] and v["hot_gapless"]
                       and v["hot_lost_or_dup"] == 0 and v["null_payload_version_rows"] == 0 and waits_ok and v["all_hot_events_upcast_to_current_shape"] and blocked == [True, True, True, True][:len(blocked)] is not None and all(blocked[:3]) and blocked[3])
    evo.save("F10_schema_evolution", out)
    print(json.dumps(out, indent=1, default=str))
    print("PASS" if out["pass"] else "FAIL")


if __name__ == "__main__":
    main()

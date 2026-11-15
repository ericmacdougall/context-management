"""C3d v2: tombstone races, 200 runs per race and mode, random injected delays. Every race is also run with the fence OFF
(naive control) and the run only passes if every control is SENSITIVE (fails at least once) and every fenced mode is clean.
Races: derive vs erasure; ES mirror batch vs erasure (fence on / fence on with the projector's LOCK SESSION KILLED inside the
delay / fence off); concept fold vs erasure; SHARED-SLOT write skew: derive vs erasure, and two concurrent erasures.
usage: c3d_race_v2.py [runs=200]"""
import datetime as dt, random, sys, threading, time
from common import *
from memsys import store, derive, erasure, projector, conceptq, kms, rules
from memsys.esc import ESError

T = "rc2"
TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def ev(i, text, subj):
    return dict(ts_event=TS + dt.timedelta(seconds=i), actor="user", trust_origin="user_typed", kind="message", subject_id=subj, content=text)


def pair(fa, fb, delay_b):
    errs = []
    def w(f):
        def g():
            try: f()
            except Exception as e: errs.append(f"{type(e).__name__}: {str(e)[:80]}")
        return g
    ta, tb = threading.Thread(target=w(fa)), threading.Thread(target=w(fb))
    ta.start(); time.sleep(delay_b); tb.start(); ta.join(); tb.join()
    return errs


def tomb_ids(conn, subj):
    return [r[0] for r in conn.execute("select event_id from tombstones where tenant_id=%s and subject_id=%s", (T, subj)).fetchall()]


def run_race(kind, mode, runs, rng):
    """mode: 'on' | 'off' | 'lock_lost' (mirror only)"""
    conn = db.connect(autocommit=True)
    fence = mode != "off"
    bad = errs = 0
    for i in range(runs):
        tag = f"{kind}{mode}x{i}"
        subj, ent, m = f"v{tag}", f"Ent{kind[0]}{mode[0]}n{i}", f"zmark{kind[0]}{mode[0]}{i}"
        da, db_ = rng.uniform(0, .06), rng.uniform(0, .06)
        ca, cb, cl, cl2 = (db.connect(autocommit=True) for _ in range(4))
        if kind == "derive":
            store.append_events(conn, T, f"s{tag}", [ev(10 + k, f"{ent}'s city is {m}{k}", subj) for k in range(3)])
            fa = lambda: derive.derive_batch(ca, T, 1, touch=False, fence=fence, between=lambda *a: time.sleep(da))
            fb = lambda: erasure.phase1(cb, kms.get(), T, subj, "r" + tag)
        elif kind == "mirror":
            store.append_events(conn, T, f"s{tag}", [ev(10 + k, f"{ent}'s city is {m}{k}", subj) for k in range(3)])
            da = rng.uniform(0, .8)
            es_a, es_b = ES(), ES()
            def hook():
                if mode == "lock_lost":                                  # the projector's lock session dies mid-batch (own backend only)
                    conn.execute("select pg_terminate_backend(%s)", (cl.info.backend_pid,))
                time.sleep(da)
            p = projector.Projector(ca, cl, es_a, tenants=[T], fence=fence, derive=False, delay=hook)
            fa = lambda: p.mirror_events(T)
            fb = lambda: erasure.erase_subject(cb, cl2, es_b, T, subj, fence=fence)
        elif kind == "fold":
            da = rng.uniform(0, .4)          # erasure phase 1 takes ~100 ms now, so the fold must be able to outlast it
            store.append_events(conn, T, f"s{tag}", [ev(10, f"{ent}'s city is {m}", subj)])
            store.append_events(conn, T, f"k{tag}", [ev(11, f"{ent}'s pet is cat", "keeper")])     # a survivor keeps the concept row alive
            while derive.derive_batch(ca, T, 1, touch=True): pass
            h = kms.get().hmac_many(T, [ent])[0]
            conn.execute("update concepts set next_fold_at=clock_timestamp() where tenant_id=%s and concept_id=%s", (T, h))
            claim = [c for c in conceptq.claim(ca, limit=100000) if c[1] == h]      # leftovers from earlier runs must not crowd ours out
            def fa():
                if not claim: return
                tn, cid, tok, gen = claim[0]
                res = conceptq.compute_fold(ca.cursor(), tn, cid)
                time.sleep(da)
                conceptq.commit_fold(ca, tn, cid, tok, gen, res, fence=fence)
            fb = lambda: erasure.phase1(cb, kms.get(), T, subj, "r" + tag)
        elif kind == "shared_derive":
            uA, uB, uC = f"a{tag}", f"b{tag}", f"c{tag}"
            store.append_events(conn, T, f"sa{tag}", [ev(1, f"{ent}'s city is one", uA)]); store.append_events(conn, T, f"sb{tag}", [ev(2, f"{ent}'s city is two", uB)])
            while derive.derive_batch(conn, T, 1, touch=False): pass
            store.append_events(conn, T, f"sc{tag}", [ev(3, f"{ent}'s city is three", uC)])
            fa = lambda: derive.derive_batch(ca, T, 1, touch=False, slot_locks=fence, resolve_hook=lambda: time.sleep(da))
            fb = lambda: erasure.phase1(cb, kms.get(), T, uB, "r" + tag, slot_locks=fence)
            subj = uB
        else:   # shared_erase: two erasures on one slot
            us = [f"{c}{tag}" for c in "abcd"]
            for k, u in enumerate(us):
                store.append_events(conn, T, f"s{k}{tag}", [ev(1 + k, f"{ent}'s city is v{k}", u)])
            while derive.derive_batch(conn, T, 1, touch=False): pass
            fa = lambda: erasure.phase1(ca, kms.get(), T, us[1], "r1" + tag, slot_locks=fence, resolve_hook=lambda: time.sleep(da))
            fb = lambda: erasure.phase1(cb, kms.get(), T, us[2], "r2" + tag, slot_locks=fence, resolve_hook=lambda: time.sleep(da / 2))
            subj = us[1]
        e = pair(fa, fb, db_)
        errs += bool(e)
        if kind == "derive":
            bad += conn.execute("select count(*) from facts where tenant_id=%s and input_event_ids && %s", (T, tomb_ids(conn, subj))).fetchone()[0] > 0
        elif kind == "mirror":
            ids = tomb_ids(conn, subj)
            ES().req("POST", f"/sys-{T}-events/_refresh", ok=None)
            bad += ES().req("POST", f"/sys-{T}-events/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"event_id": ids}}})[1]["hits"]["total"]["value"] > 0
        elif kind == "fold":
            bad += conn.execute("select count(*) from concepts where tenant_id=%s and input_event_ids && %s", (T, tomb_ids(conn, subj))).fetchone()[0] > 0
        else:
            h = kms.get().hmac_many(T, [f"{ent}\x1fcity"])[0]
            rows = conn.execute("select slot_enc, object_enc, subject_id, status, valid_to, order_key, trust_origin, modality, valid_from, id from facts where tenant_id=%s and slot_hash=%s", (T, h)).fetchall()
            k = kms.get()
            claims, got = [], {}
            for se, oe, sid, st, vt, ok, tr, mod, vf, fid in rows:
                o = k.dec_many(T, [(sid, oe)])[0]
                claims.append(rules.Claim(fid, o, mod, tr, vf.isoformat(), ok)); got[fid] = (st, vt)
            want = rules.resolve_slot(claims)
            bad += any(got[f][0] != want[f][0] or (want[f][1] is not None and got[f][1].isoformat() != want[f][1]) for f in got)
        for c in (ca, cb, cl, cl2):
            try: c.close()
            except Exception: pass
    return {"runs": runs, "violations": bad, "runs_with_exception": errs}


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es); esx.drop_tenant(es, T)
    store.append_events(conn, T, "keep", [ev(0, "Keeper's city is Oslo", "keep")])
    projector.Projector(conn, lock, es, tenants=[T]).run()
    rng = random.Random(16096)
    out = {"workload": f"{runs} runs per race and mode, random delays 0-60 ms (0-800 ms for the ES batch); PG16 + key service + ES 9.5.6 on t2-dev", "results": {}}
    plan = [("derive", ["on", "off"]), ("mirror", ["on", "lock_lost", "off"]), ("fold", ["on", "off"]), ("shared_derive", ["on", "off"]), ("shared_erase", ["on", "off"])]
    only = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    plan = [x for x in plan if only is None or x[0] in only]
    for kind, modes in plan:
        for mode in modes:
            out["results"][f"{kind}_{mode}"] = run_race(kind, mode, runs, rng); print(kind, mode, out["results"][f"{kind}_{mode}"], flush=True)
    R = out["results"]
    out["fenced_clean"] = {k: R[f"{k}_on"]["violations"] == 0 for k, _ in plan}
    out["lock_lost_clean"] = R["mirror_lock_lost"]["violations"] == 0 if "mirror_lock_lost" in R else True
    out["controls_sensitive"] = {k: R[f"{k}_off"]["violations"] > 0 for k, _ in plan}
    out["pass"] = all(out["fenced_clean"].values()) and out["lock_lost_clean"] and all(out["controls_sensitive"].values())
    save("C3d_race_v2" + ("" if only is None else "_" + "_".join(only)), out)
    print("PASS" if out["pass"] else "FAIL", out["controls_sensitive"], out["fenced_clean"], out["lock_lost_clean"])


if __name__ == "__main__":
    main()

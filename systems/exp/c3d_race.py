"""C3 d: a tombstone racing an in-flight derivation, ES projection batch, or concept fold, 200 runs each,
with randomised injected delays (0-60 ms) on both sides. Each race is also run with the fences OFF (naive control)
to show the test can fail. Resurrection = a derived row/document whose input set touches a tombstoned event.
usage: c3d_race.py [runs=200]"""
import datetime as dt, random, sys, threading, time
from common import *
from memsys import store, derive, erasure, projector, conceptq

T = "rc"
TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)

def ev(i, text, subj):
    return dict(ts_event=TS + dt.timedelta(seconds=i), actor="user", trust_origin="user_typed", kind="message", subject_id=subj, content=text)

def pair(fa, fb, da, db):
    errs = []
    def wrap(f, d):
        def g():
            try:
                f()
            except Exception as e:
                errs.append(f"{type(e).__name__}: {str(e)[:80]}")
        return g
    ta, tb = threading.Thread(target=wrap(fa, da)), threading.Thread(target=wrap(fb, db))
    ta.start(); time.sleep(db); tb.start(); ta.join(); tb.join()
    return errs

def run_race(kind, fence, runs, rng):
    conn = db.connect(autocommit=True)
    resurrect, errors, tomb_first = 0, 0, 0
    for i in range(runs):
        tag = f"{kind}{int(fence)}x{i}"
        subj, ent = f"v{tag}", f"Ent{kind[0]}{int(fence)}n{i}"
        marker = f"zmark{tag}"
        store.append_events(conn, T, f"s{tag}", [ev(10 + k, f"{ent}'s city is {marker}{k}", subj) for k in range(3)])
        da, db_ = rng.uniform(0, 0.06), rng.uniform(0, 0.06)
        ca, cb, cl, cl2 = (db.connect(autocommit=True) for _ in range(4))
        if kind == "derive":
            fa = lambda: derive.derive_batch(ca, T, 1, touch=False, fence=fence, between=lambda *a: time.sleep(da))
            fb = lambda: erasure.phase1(cb, T, subj, (1,), "r" + tag)
        elif kind == "mirror":
            es_a, es_b = ES(), ES()
            p = projector.Projector(ca, cl, es_a, tenants=[T], fence=fence, derive=False, delay=lambda: time.sleep(da))
            fa = lambda: p.mirror_events(T)
            fb = lambda: erasure.erase_subject(cb, cl2, es_b, T, subj, (), fence=fence)
        else:   # fold
            derive.derive_batch(ca, T, 1, touch=True)
            conn.execute("update concepts set next_fold_at=clock_timestamp() where tenant_id=%s and concept_id=%s", (T, ent))
            claim = [c for c in conceptq.claim(ca, limit=50) if c[1] == ent]
            def fa():
                if not claim:
                    return
                tn, cid, tok, gen = claim[0]
                res = conceptq.compute_fold(ca.cursor(), tn, cid)
                time.sleep(da)
                conceptq.commit_fold(ca, tn, cid, tok, gen, res, fence=fence)
            fb = lambda: erasure.phase1(cb, T, subj, (1,), "r" + tag)
        errs = pair(fa, fb, da, db_)
        errors += bool(errs)
        ids = [r[0] for r in conn.execute("select event_id from tombstones where tenant_id=%s and subject_id=%s", (T, subj)).fetchall()]
        if kind == "derive":
            bad = conn.execute("select count(*) from facts where tenant_id=%s and input_event_ids && %s", (T, ids)).fetchone()[0]
        elif kind == "mirror":
            ES().req("POST", f"/sys-{T}-events/_refresh", ok=None)
            bad = ES().req("POST", f"/sys-{T}-events/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"event_id": ids}}})[1]["hits"]["total"]["value"]
        else:
            bad = conn.execute("select count(*) from concepts where tenant_id=%s and input_event_ids && %s", (T, ids)).fetchone()[0]
        resurrect += bad > 0
        for c in (ca, cb, cl, cl2):
            c.close()
    return {"runs": runs, "resurrections": resurrect, "runs_with_exception": errors}

def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es); esx.drop_tenant(es, T)
    # baseline: a surviving event so the bucket index and alias exist
    store.append_events(conn, T, "keep", [ev(0, "Keeper's city is Oslo", "keep")])
    projector.Projector(conn, lock, es, tenants=[T]).run()
    rng = random.Random(16096)
    out = {"workload": f"{runs} runs per race and mode, random injected delay 0-60 ms in the in-flight operation and in the erasure start; PG16+ES 9.5.6 on t2-dev", "results": {}}
    for kind in ("derive", "mirror", "fold"):
        for fence in (True, False):
            r = run_race(kind, fence, runs, rng)
            out["results"][f"{kind}_fence_{'on' if fence else 'off'}"] = r
            print(kind, "fence", fence, r, flush=True)
    R = out["results"]
    out["pass"] = all(R[f"{k}_fence_on"]["resurrections"] == 0 for k in ("derive", "mirror", "fold"))
    out["test_sensitive"] = {k: R[f"{k}_fence_off"]["resurrections"] > 0 for k in ("derive", "mirror", "fold")}
    save("C3d_race", out)
    print("PASS" if out["pass"] else "FAIL", out["test_sensitive"])

if __name__ == "__main__":
    main()

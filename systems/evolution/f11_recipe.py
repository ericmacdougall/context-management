"""F11: recipe change with facts already derived, under live writes, with an extraction cache keyed by (event id, extractor version).
Each mode (cache on / cache off) runs the same data: v1 build -> v2 build under live writes + atomic alias swap under readers ->
v2 rebuild from scratch -> v3 (extractor change) build -> erasure vs the cache. usage: f11_recipe.py [sessions=40] [per_session=50]"""
import datetime as dt, os, random, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import evo, cached_extract as ce
from common import compare_facts, pg_expected_facts
from memsys import gen, store, projector, reader, erasure, kms, esx, extractor
from memsys.esc import ES

TENANTS = ["f11a", "f11b", "f11c"]


def swap_live(es, t, to):
    live = esx._targets(es, esx.fx_ralias(t))
    fv = esx.facts_versions(es, t)
    new = esx.fx_index(t, to, fv[to])
    acts = [{"remove": {"index": i, "alias": esx.fx_ralias(t)}} for i in live if i != new] + [{"add": {"index": new, "alias": esx.fx_ralias(t)}}]
    es.req("POST", "/_aliases", {"actions": acts})


def run_mode(use_cache, ns, per):
    conn = evo.connect(); lock = evo.connect(); es = ES()
    evo.reset(conn); esx.install_templates(es)
    for t in TENANTS: esx.drop_tenant(es, t)
    counter = ce.Counter(); ce.install(conn, use_cache, counter)
    res = {"cache": use_cache}
    for t in TENANTS:
        sess = gen.gen_tenant(t, 11, ns, per, n_users=8)
        # a few past-tense statements that only extractor version 2 understands
        for s in sess[:3]:
            for k in range(5):
                s["events"].append(dict(ts_event=s["events"][-1]["ts_event"] + dt.timedelta(seconds=k + 1), actor="user", trust_origin="user_typed", kind="message", subject_id=s["subject_id"], content=f"Zed{k}'s city was Rome"))
        gen.load(conn, t, sess, 11, store)
    n_events = conn.execute("select count(*) from events").fetchone()[0]; res["events"] = n_events
    snap = lambda: counter.invocations
    # ---- A: build recipe 1
    p = projector.Projector(conn, lock, es, recipes=(1,), live_recipe=1, batch=500)
    t0 = time.time(); p.run(); res["v1_build"] = {"s": round(time.time() - t0, 1), "extractor_invocations": snap(), "cache_hits": counter.hits}
    # ---- B: recipe 2 under live writes + readers + atomic swap
    stop = threading.Event(); stats = {"requests": 0, "mixed_view": 0, "errors": 0, "versions_seen": set()}; lk = threading.Lock(); written = [0]
    def writer():
        wc = evo.connect(); n = 0
        while not stop.is_set():
            store.append_events(wc, "f11a", f"live{n % 3}", [dict(ts_event=dt.datetime.now(dt.timezone.utc), actor="user", trust_origin="user_typed", kind="message", subject_id="uL", content=f"Liv{n % 7}'s favourite colour is blue{n}")])
            n += 1; written[0] = n; time.sleep(0.05)
    def rd_thread(seed):
        rc, ec = evo.connect(), ES(); rd = reader.Reader(rc, ec); rng = random.Random(seed)
        while not stop.is_set():
            try:
                r = rd.search_facts(rng.choice(TENANTS), rng.choice(["blue", "Oslo", "cat", "emacs"]), k=10)
                with lk:
                    stats["requests"] += 1; stats["mixed_view"] += len(r["versions"]) > 1; stats["versions_seen"] |= set(r["versions"])
            except Exception as e:
                with lk:
                    stats["errors"] += 1
    ths = [threading.Thread(target=writer)] + [threading.Thread(target=rd_thread, args=(i,)) for i in range(4)]
    for th in ths: th.start()
    inv0, hit0 = snap(), counter.hits
    p2 = projector.Projector(conn, lock, es, recipes=(1, 2), live_recipe=1, batch=500)
    t0 = time.time(); p2.run(idle_rounds=2)
    res["v2_build_under_live_writes"] = {"s": round(time.time() - t0, 1), "live_events_written": written[0], "extractor_invocations": snap() - inv0, "cache_hits": counter.hits - hit0}
    time.sleep(1)
    swaps = 0
    for rnd in range(10):                     # flip v1 <-> v2 repeatedly while readers and the writer run
        for t in TENANTS:
            swap_live(es, t, 2 if rnd % 2 == 0 else 1); swaps += 1
        time.sleep(0.2)
    swap_live_final = [swap_live(es, t, 2) for t in TENANTS]
    time.sleep(1)
    stop.set()
    for th in ths: th.join()
    p2.run(idle_rounds=2)
    res["readers"] = {"requests": stats["requests"], "mixed_version_responses": stats["mixed_view"], "errors": stats["errors"], "versions_seen": sorted(stats["versions_seen"]), "alias_swaps": swaps}
    # ---- C: rebuild v2 from scratch (facts rows + cursors dropped)
    conn.execute("delete from facts where recipe_version=2"); conn.execute("delete from projection_state where version=2 and view in ('derive','facts')")
    inv0, hit0 = snap(), counter.hits
    p3 = projector.Projector(conn, lock, es, recipes=(2,), live_recipe=2, batch=500)
    t0 = time.time(); p3.run(); res["v2_rebuild_from_scratch"] = {"s": round(time.time() - t0, 1), "extractor_invocations": snap() - inv0, "cache_hits": counter.hits - hit0}
    ident = {t: pg_expected_facts(conn, t, 2) for t in ("f11b", "f11c")}
    res["_identity_v2"] = ident
    res["v2_identity_vs_es"] = compare_facts(conn, es, ["f11b", "f11c"], 2)
    # ---- D: recipe 3 = extractor change
    inv0, hit0 = snap(), counter.hits
    p4 = projector.Projector(conn, lock, es, recipes=(2, 3), live_recipe=2, batch=500)
    t0 = time.time(); p4.run(); res["v3_extractor_change_build"] = {"s": round(time.time() - t0, 1), "extractor_invocations": snap() - inv0, "cache_hits": counter.hits - hit0,
                                                                       "v2_facts": conn.execute("select count(*) from facts where recipe_version=2").fetchone()[0], "v3_facts": conn.execute("select count(*) from facts where recipe_version=3").fetchone()[0]}
    # ---- E: erasure vs the cache
    if use_cache:
        subj = conn.execute("select subject_id from events where tenant_id='f11b' group by 1 order by count(*) desc limit 1").fetchone()[0]
        subj2 = conn.execute("select subject_id from events where tenant_id='f11c' group by 1 order by count(*) desc limit 1").fetchone()[0]
        before = conn.execute("select count(*) from extract_cache where tenant_id='f11b' and subject_id=%s", (subj,)).fetchone()[0]
        saved = list(erasure.DERIVED_BY_EVENT); erasure.DERIVED_BY_EVENT.clear()          # CONTROL: cascade without the registry
        erasure.erase_subject(conn, lock, es, "f11b", subj)
        left_without = conn.execute("select count(*) from extract_cache where tenant_id='f11b' and subject_id=%s", (subj,)).fetchone()[0]
        erasure.DERIVED_BY_EVENT.extend(saved)
        before2 = conn.execute("select count(*) from extract_cache where tenant_id='f11c' and subject_id=%s", (subj2,)).fetchone()[0]
        erasure.erase_subject(conn, lock, es, "f11c", subj2)
        left_with = conn.execute("select count(*) from extract_cache where tenant_id='f11c' and subject_id=%s", (subj2,)).fetchone()[0]
        res["erasure_vs_cache"] = {"cache_rows_before_control": before, "left_after_cascade_without_registry": left_without, "cache_rows_before": before2, "left_after_cascade_with_registry": left_with}
    ce.uninstall()
    return res


def main():
    ns = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    out = {"workload": f"3 tenants x {ns} sessions x {per} events (+15 past-tense statements), live writer 20 events/s and 4 reader threads during the v2 build; rule extractor (stand-in); PG16 + key service + ES 9.5.6 on t2-dev"}
    a = run_mode(True, ns, per); b = run_mode(False, ns, per)
    out["with_cache"], out["without_cache"] = {k: v for k, v in a.items() if not k.startswith("_")}, {k: v for k, v in b.items() if not k.startswith("_")}
    out["v2_facts_identical_cache_vs_no_cache"] = a["_identity_v2"] == b["_identity_v2"]
    out["pass"] = bool(out["v2_facts_identical_cache_vs_no_cache"] and a["readers"]["mixed_version_responses"] == 0 and b["readers"]["mixed_version_responses"] == 0
                       and a["readers"]["errors"] == 0 and a["v2_rebuild_from_scratch"]["extractor_invocations"] == 0 and b["v2_rebuild_from_scratch"]["extractor_invocations"] > 0
                       and a["erasure_vs_cache"]["left_after_cascade_with_registry"] == 0 and a["erasure_vs_cache"]["left_after_cascade_without_registry"] > 0
                       and a["v3_extractor_change_build"]["v3_facts"] > a["v3_extractor_change_build"]["v2_facts"] - 1
                       and a["v2_identity_vs_es"]["missing"] == 0 and a["v2_identity_vs_es"]["hash_mismatch"] == 0)
    evo.save("F11_recipe_change", out)
    import json
    print(json.dumps(out, indent=1, default=str)); print("PASS" if out["pass"] else "FAIL")


if __name__ == "__main__":
    main()

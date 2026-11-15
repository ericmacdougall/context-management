"""F9 (1)+(2): a mapping change that cannot be applied in place, then an embedding-model change, each as a blue/green rebuild from
Postgres with a single atomic alias cutover, under a live writer and 4 reader threads.
usage: f9_mapping.py [events=30000]"""
import datetime as dt, json, os, random, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import evo, families as F
from memsys import gen, store, projector, esx, fusion, kms
from memsys.esc import ES, ESError
from common import scan

T = "f9a"
STABLE = f"sys-{T}-events"


def serving(conn):
    return conn.execute("select fam, vec_field, dim from serving").fetchone()


def search_once(es, conn, text):
    fam, vf, dim = serving(conn)
    qv = fusion.embed(text, dim)
    pit = es.req("POST", f"/{STABLE}/_pit", keep_alive="30s")[1]["id"]
    try:
        pit_ref = {"id": pit, "keep_alive": "30s"}
        a = es.req("POST", "/_search", {"pit": pit_ref, "size": 20, "_source": False, "query": {"match": {"content": text}}, "sort": ["_score", {"_shard_doc": "asc"}]})[1]["hits"]["hits"]
        b = es.req("POST", "/_search", {"pit": pit_ref, "size": 20, "_source": False, "query": {"script_score": {"query": {"match_all": {}}, "script": {"source": f"cosineSimilarity(params.q, '{vf}') + 1.0", "params": {"q": qv}}}}})[1]["hits"]["hits"]
    finally:
        es.req("DELETE", "/_pit", {"id": pit}, ok=None)
    return {F.family_of(h["_index"]) for h in a + b}


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 30000
    conn = evo.connect(); evo.reset(conn); es = ES()
    esx.install_templates(es); F.install_templates(es); esx.drop_tenant(es, T)
    conn.execute("create table serving(fam text, vec_field text, dim int); insert into serving values ('events','vec',64)")
    out = {"workload": f"one tenant, {N} events (+ live writer 20/s), 4 reader threads doing PIT hybrid search through the stable alias {STABLE}; ES 9.5.6 single node on t2-dev"}
    sess = gen.gen_tenant(T, 9, N // 50, 50, n_users=20)
    gen.load(conn, T, sess, 9, store)
    lock = evo.connect()
    base = projector.Projector(conn, lock, es, recipes=(1,), live_recipe=1, tenants=[T], derive=False, batch=1000)
    t0 = time.time(); base.run(); out["baseline_projection_s"] = round(time.time() - t0, 1)
    stop = threading.Event(); proj_stop = threading.Event(); lk = threading.Lock()
    stats = {"requests": 0, "mixed_view": 0, "retried_errors": 0, "failed": 0, "families_seen": set()}

    def writer():
        wc = evo.connect(); n = 0
        while not stop.is_set():
            store.append_events(wc, T, f"live{n % 5}", [dict(ts_event=dt.datetime.now(dt.timezone.utc), actor="user", trust_origin="user_typed", kind="message", subject_id="uL", content=f"live note {n} colour blue")])
            n += 1; time.sleep(0.05)

    def legacy_proj():
        c, l, e = evo.connect(), evo.connect(), ES()
        p = projector.Projector(c, l, e, recipes=(1,), live_recipe=1, tenants=[T], derive=False, batch=1000)
        while not proj_stop.is_set():
            if p.run_once() == 0:
                time.sleep(0.05)

    def fam_proj(fam, stop_ev):
        c, e = evo.connect(), ES(); p = F.FamilyProjector(c, e, fam, [T])
        while not stop_ev.is_set():
            if p.run_once() == 0:
                time.sleep(0.05)

    def rd_thread(seed):
        rc, ec = evo.connect(), ES(); rng = random.Random(seed)
        while not stop.is_set():
            text = rng.choice(["blue colour", "Oslo", "cat", "emacs", "live note", "favourite"])
            for attempt in range(4):
                try:
                    fams = search_once(ec, rc, text)
                    with lk:
                        stats["requests"] += 1; stats["mixed_view"] += len(fams) > 1; stats["families_seen"] |= fams
                    break
                except ESError:
                    with lk:
                        stats["retried_errors"] += 1
                    time.sleep(0.02)
            else:
                with lk:
                    stats["failed"] += 1

    ths = [threading.Thread(target=writer), threading.Thread(target=legacy_proj)] + [threading.Thread(target=rd_thread, args=(i,)) for i in range(4)]
    for th in ths:
        th.start()
    time.sleep(3)

    def build(fam, old_fam):
        stop_f = threading.Event()
        th = threading.Thread(target=fam_proj, args=(fam, stop_f)); t0 = time.time(); th.start()
        while F.lag(conn, "events-" + fam, [T]) > 20:
            time.sleep(0.2)
        old_idx = [r["index"] for r in esx.tenant_indices(es, T) if F.family_of(r["index"]) == old_fam]
        return th, stop_f, round(time.time() - t0, 1), old_idx

    def cutover(fam, old_indices):
        new_idx = [r["index"] for r in esx.tenant_indices(es, T) if f"-{fam}-b" in r["index"]]
        acts = [{"remove": {"index": i, "alias": STABLE}} for i in old_indices] + [{"add": {"index": i, "alias": STABLE}} for i in new_idx]
        t0 = time.time(); es.req("POST", "/_aliases", {"actions": acts}); return round(time.time() - t0, 3)

    # (1) mapping change: events -> ev2
    th2, stop2, catch2, old_idx = build("ev2", "events")
    proj_stop.set(); time.sleep(0.3)
    cut2 = cutover("ev2", old_idx)
    es.req("POST", "/_aliases", {"actions": [{"remove_index": {"index": i}} for i in old_idx]})
    out["mapping_change"] = {"build_catch_up_s": catch2, "alias_cutover_s": cut2, "old_indices_removed": len(old_idx)}
    time.sleep(3)
    # (2) embedding model change: ev2 -> ev3 (new vector field vec2, 32 dims); the serving config flips right after the alias
    th3, stop3, catch3, old_idx = build("ev3", "ev2")
    stop2.set(); time.sleep(0.3)
    cut3 = cutover("ev3", old_idx)
    conn2 = evo.connect(); conn2.execute("update serving set fam='ev3', vec_field='vec2', dim=32")
    es.req("POST", "/_aliases", {"actions": [{"remove_index": {"index": i}} for i in old_idx]})
    out["embedding_change"] = {"build_catch_up_s": catch3, "alias_cutover_s": cut3, "old_indices_removed": len(old_idx)}
    time.sleep(4)
    stop.set()
    t_w = time.time()
    while F.lag(conn, 'events-ev3', [T]) > 0 and time.time() - t_w < 30:       # let the new family drain what the writer appended last
        time.sleep(0.2)
    stop3.set()
    for th in ths + [th2, th3]:
        th.join()
    out["readers"] = {"requests": stats["requests"], "mixed_family_responses": stats["mixed_view"], "retried_errors_in_cutover_windows": stats["retried_errors"],
                      "failed_after_retries": stats["failed"], "families_seen": sorted(stats["families_seen"])}
    # final correctness: ev3 docs vs Postgres, new capabilities, self-recall
    es.req("POST", f"/{STABLE}/_refresh")
    docs = {i: s for _, i, s in scan(es, STABLE, True)}
    pg = {r["event_id"]: r["plain"] for r in store.read_plain(conn, T, "true", ()) if not r["tomb"] and r["plain"]}
    out["identity_vs_postgres"] = {"pg": len(pg), "es": len(docs), "missing": len([i for i in pg if i not in docs]), "extra": len([i for i in docs if i not in pg]),
                                   "content_mismatch": sum(1 for i, s in docs.items() if i in pg and s.get("content") != pg[i])}
    rng = random.Random(1); samp = rng.sample(list(pg), 50); ok = 0
    for i in samp:
        r = es.req("POST", f"/{STABLE}/_search", {"size": 5, "_source": ["content"], "query": {"script_score": {"query": {"match_all": {}}, "script": {"source": "cosineSimilarity(params.q, 'vec2') + 1.0", "params": {"q": fusion.embed(pg[i], 32)}}}}})[1]["hits"]["hits"]
        ok += any(h["_source"]["content"] == pg[i] for h in r)      # identical texts have identical vectors, so ties are expected: the nearest hit must carry the same text
    out["vector_same_text_recall_at_5_new_field"] = ok / 50
    kinds = es.req("POST", f"/{STABLE}/_search", {"size": 0, "aggs": {"k": {"terms": {"field": "kind.raw"}}}})[1]["aggregations"]["k"]["buckets"]
    out["new_subfield_works"] = {"kind.raw_buckets": len(kinds), "content.en_stemmed_match": es.req("POST", f"/{STABLE}/_search", {"size": 0, "track_total_hits": True, "query": {"match": {"content.en": "colours"}}})[1]["hits"]["total"]["value"]}
    out["pass"] = bool(out["readers"]["mixed_family_responses"] == 0 and out["readers"]["failed_after_retries"] == 0 and out["identity_vs_postgres"]["missing"] == 0
                       and out["identity_vs_postgres"]["content_mismatch"] == 0 and out["vector_same_text_recall_at_5_new_field"] >= 0.9 and out["readers"]["requests"] > 500)
    evo.save("F9_mapping_embedding_bluegreen", out)
    print(json.dumps(out, indent=1, default=str)); print("PASS" if out["pass"] else "FAIL")


if __name__ == "__main__":
    main()

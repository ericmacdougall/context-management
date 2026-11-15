"""C1: rebuild every projection from the Postgres log, compare, then blue/green recipe change.
usage: c1_rebuild.py [tenants=20] [sessions=100] [per_session=100] [phase=all|a|b]
Phase a: generate+project+erase+snapshot, drop all ES indices and the facts/concepts tables, rebuild from events (+tombstones/keys), snapshot, compare.
Phase b: blue/green recipe v2 with an atomic alias swap while readers hammer the read helper."""
import datetime as dt, hashlib, random, subprocess, sys, threading, time
from common import *
from memsys import gen, store, erasure, projector, reader, conceptq, fusion

PY = sys.executable

def fold_all(conn):
    conn.execute("update concepts set next_fold_at=clock_timestamp() where dirty")
    n = 0
    while True:
        rows = conceptq.claim(conn, limit=200)
        if not rows:
            return n
        for tn, cid, tok, gen_ in rows:
            conceptq.commit_fold(conn, tn, cid, tok, gen_, conceptq.compute_fold(conn.cursor(), tn, cid))
            n += 1

def concept_snapshot(conn):
    return {(t, c): (d, tuple(ei), tuple(fi)) for t, c, d, ei, fi in
            conn.execute("select tenant_id, concept_id, digest, input_event_ids, input_fact_ids from concepts").fetchall()}

def hsh(x):
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]

def query_set(conn, tenants, seed=99):
    rng = random.Random(seed)
    qs = []
    for t in tenants:
        rows = conn.execute("select mem_dec(tenant_id, subject_id, content_enc), ts_event from events e where tenant_id=%s and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) order by event_id limit 3000", (t,)).fetchall()
        rows = [r for r in rows if r[0]]
        for k in range(4):
            qs.append(("lex", t, " ".join(rng.choice(rows)[0].replace("'s", "").split()[:3])))
        for k in range(3):
            qs.append(("vec", t, rng.choice(rows)[0]))
        for k in range(3):
            r = rng.choice(rows)
            subj = r[0].split("'s")[0].split()[-1]
            qs.append(("asof", t, (subj, r[1].isoformat())))
    return qs

def run_queries(es, rd, qs):
    out = {"lex": [], "vec_exact": [], "vec_ann": [], "asof": [], "hybrid": []}
    for kind, t, q in qs:
        if kind == "lex":
            r = es.req("POST", f"/sys-{t}-events/_search", {"size": 10, "_source": False, "track_total_hits": False, "query": {"match": {"content": q}}, "sort": ["_score", {"event_id": "asc"}]})[1]
            out["lex"].append([h["_id"] for h in r["hits"]["hits"]])
        elif kind == "vec":
            qv = fusion.embed(q)
            r = es.req("POST", f"/sys-{t}-events/_search", {"size": 10, "_source": False, "query": {"script_score": {"query": {"match_all": {}}, "script": {"source": "cosineSimilarity(params.q, 'vec') + 1.0", "params": {"q": qv}}}}, "sort": ["_score", {"event_id": "asc"}]})[1]
            out["vec_exact"].append([h["_id"] for h in r["hits"]["hits"]])
            r = es.req("POST", f"/sys-{t}-events/_search", {"size": 10, "_source": False, "knn": {"field": "vec", "query_vector": qv, "k": 10, "num_candidates": 100}})[1]
            out["vec_ann"].append([h["_id"] for h in r["hits"]["hits"]])
        else:
            subj, ts = q
            out["asof"].append(rd.query_facts(t, subj, None, ts))
            out["hybrid"].append([h["id"] for h in rd.search(t, subj + " city colour", k=10, tail=False)["hits"]])
    return out

def overlap(a, b):
    xs = [len(set(x) & set(y)) / max(1, len(set(x) | set(y))) for x, y in zip(a, b)]
    return round(sum(xs) / len(xs), 4) if xs else None

def snapshot_all(conn, es, tenants):
    ev = snapshot_events(es, tenants)
    fx = snapshot_facts(es, tenants)
    norm = lambda d: {t: sorted((strip_gen(i), j, h) for i, j, h in rows) for t, rows in d.items()}
    pgf = {t: pg_expected_facts(conn, t, 1) for t in tenants}
    return {"es_events": norm(ev), "es_facts": norm(fx), "pg_facts": pgf, "concepts": concept_snapshot(conn)}

def phase_a(nt, ns, per):
    tenants = [f"t{i:02d}" for i in range(1, nt + 1)]
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es)
    for t in tenants:
        esx.drop_tenant(es, t)
    out = {"workload": f"{nt} tenants x {ns} sessions x {per} events = {nt*ns*per} events ({nt*ns} sessions), 18 months, bucket span {esx.BUCKET_DAYS} d, projector single process batch 1000; PG16 (4cpu/4g) + ES 9.5.6 single node on t2-dev",
           "disk_free_gb_before": disk_free_gb()}
    t0 = time.time(); total = 0
    for t in tenants:
        total += gen.load(conn, t, gen.gen_tenant(t, 11, ns, per, n_users=10), 11, store)
    out["events_loaded"] = total; out["load_s"] = round(time.time() - t0, 1)
    print(out, flush=True)
    t0 = time.time(); projector.Projector(conn, lock, es, batch=1000).run(); out["initial_project_s"] = round(time.time() - t0, 1)
    out["facts_rows"] = conn.execute("select count(*) from facts").fetchone()[0]
    out["facts_status"] = dict(conn.execute("select status, count(*) from facts group by 1").fetchall())
    fold_all(conn)
    t0 = time.time(); rcs = []
    for t in tenants:
        for u in ("u2", "u5"):
            rcs.append(erasure.erase_subject(conn, lock, es, t, u, (1,)))
    out["erasures"] = {"count": len(rcs), "events_erased": sum(r["events_erased"] for r in rcs), "total_s": round(time.time() - t0, 1),
                       "facts_deleted": sum(r["facts_deleted"] for r in rcs), "receipt_example": rcs[0]}
    projector.Projector(conn, lock, es, batch=1000).run()
    fold_all(conn)
    out["tombstones"] = conn.execute("select count(*) from tombstones").fetchone()[0]
    rd = reader.Reader(conn, es)
    qs = query_set(conn, tenants)
    out["queries"] = len(qs)
    t0 = time.time(); s1 = snapshot_all(conn, es, tenants); out["snapshot_s"] = round(time.time() - t0, 1)
    q1 = run_queries(es, rd, qs)
    out["docs"] = {"events": sum(len(v) for v in s1["es_events"].values()), "facts": sum(len(v) for v in s1["es_facts"].values())}
    print("snapshot 1 taken", out["docs"], flush=True)
    # ---------------- drop every ES index and the derived tables; keep only the log (events, sessions, keys, tombstones)
    for t in tenants:
        esx.drop_tenant(es, t)
    conn.execute("delete from facts"); conn.execute("delete from concepts"); conn.execute("delete from projection_state"); conn.execute("delete from es_buckets")
    t0 = time.time(); projector.Projector(conn, lock, es, batch=1000).run(); t_rb = time.time() - t0
    t1 = time.time(); fold_all(conn); t_fold = time.time() - t1
    out["rebuild"] = {"project_s": round(t_rb, 1), "events_per_s": round(out["events_loaded"] / t_rb), "fold_s": round(t_fold, 1),
                      "docs_per_s": round((out["docs"]["events"] + out["docs"]["facts"]) / t_rb)}
    s2 = snapshot_all(conn, es, tenants)
    q2 = run_queries(es, reader.Reader(conn, es), qs)
    cmp = {k: (s1[k] == s2[k]) for k in s1}
    cmp["doc_counts_equal"] = {"events": sum(len(v) for v in s2["es_events"].values()) == out["docs"]["events"], "facts": sum(len(v) for v in s2["es_facts"].values()) == out["docs"]["facts"]}
    out["identity"] = cmp
    out["query_results"] = {"lexical_exact_identical": q1["lex"] == q2["lex"], "vector_exact_identical": q1["vec_exact"] == q2["vec_exact"],
                            "asof_facts_identical": q1["asof"] == q2["asof"], "hybrid_identical": q1["hybrid"] == q2["hybrid"],
                            "hybrid_mean_jaccard": overlap(q1["hybrid"], q2["hybrid"]),
                            "vector_ann_identical": q1["vec_ann"] == q2["vec_ann"], "vector_ann_mean_jaccard_vs_before": overlap(q1["vec_ann"], q2["vec_ann"]),
                            "vector_ann_vs_exact_recall_before": overlap(q1["vec_ann"], q1["vec_exact"]), "vector_ann_vs_exact_recall_after": overlap(q2["vec_ann"], q2["vec_exact"])}
    out["disk_free_gb_after"] = disk_free_gb()
    ok = all(v for k, v in cmp.items() if k != "doc_counts_equal") and all(cmp["doc_counts_equal"].values()) and all(out["query_results"][k] for k in ("lexical_exact_identical", "vector_exact_identical", "asof_facts_identical"))
    out["pass_phase_a"] = bool(ok)
    save("C1_rebuild", out)
    print(json.dumps({k: v for k, v in out.items() if k != "erasures"}, indent=1, default=str))
    print("PASS_A" if ok else "FAIL_A")

def phase_b(nt):
    tenants = [f"t{i:02d}" for i in range(1, nt + 1)]
    conn, lock, es = conns()
    out = {"workload": f"recipe v2 built alongside v1 for {nt} tenants (projector recipes 1,2), then alias swaps under 4 reader threads and a 20/s writer; ES 9.5.6 on t2-dev"}
    t0 = time.time(); p = projector.Projector(conn, lock, es, recipes=(1, 2), live_recipe=1, batch=1000); p.run(); out["v2_build_s"] = round(time.time() - t0, 1)
    out["v1_vs_v2_status"] = {"v1": dict(conn.execute("select status,count(*) from facts where recipe_version=1 group by 1").fetchall()), "v2": dict(conn.execute("select status,count(*) from facts where recipe_version=2 group by 1").fetchall())}
    out["v2_identity_vs_pg"] = compare_facts(conn, es, tenants[:1], 1)   # live alias still v1
    sample = tenants[:5]
    stop = threading.Event(); stats = {"requests": 0, "mixed_view": 0, "errors": 0, "versions_seen": set(), "empty": 0}
    lk = threading.Lock()
    def rd_thread(seed):
        rc, ec = db.connect(autocommit=True), ES()
        rd = reader.Reader(rc, ec); rng = random.Random(seed)
        while not stop.is_set():
            t = rng.choice(sample)
            try:
                r = rd.search_facts(t, rng.choice(["blue", "Oslo", "cat Paris", "emacs"]), k=10)
                with lk:
                    stats["requests"] += 1; stats["mixed_view"] += len(r["versions"]) > 1; stats["versions_seen"] |= set(r["versions"]); stats["empty"] += len(r["versions"]) == 0
            except Exception as e:
                with lk:
                    stats["errors"] += 1; stats.setdefault("error_examples", []).append(str(e)[:150])
    def writer():
        wc = db.connect(autocommit=True); n = 0
        while not stop.is_set():
            store.append_events(wc, sample[n % 5], f"bg{n % 7}", [dict(ts_event=dt.datetime.now(dt.timezone.utc), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content=f"Zed's favourite colour is blue{n}")])
            n += 1; time.sleep(0.05)
    proj = subprocess.Popen([PY, "-u", "exp/projector_main.py", ",".join(sample), "1,2", "1", "500"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ths = [threading.Thread(target=rd_thread, args=(i,)) for i in range(4)] + [threading.Thread(target=writer)]
    for th in ths: th.start()
    time.sleep(2)
    cur = {t: 1 for t in sample}
    def swap(t, to, atomic=True):
        a = esx.alias_targets(es, esx.fx_ralias(t))
        new = f"sys-{t}-facts-v{to}-g1"
        if atomic:
            es.req("POST", "/_aliases", {"actions": [{"remove": {"index": a[0], "alias": esx.fx_ralias(t)}}, {"add": {"index": new, "alias": esx.fx_ralias(t)}}]})
        else:
            es.req("POST", "/_aliases", {"actions": [{"remove": {"index": a[0], "alias": esx.fx_ralias(t)}}]}); time.sleep(0.05)
            es.req("POST", "/_aliases", {"actions": [{"add": {"index": new, "alias": esx.fx_ralias(t)}}]})
    swaps = 0
    for rnd in range(20):
        for t in sample:
            cur[t] = 2 if cur[t] == 1 else 1
            swap(t, cur[t]); swaps += 1
        time.sleep(0.3)
    atomic_stats = {k: (sorted(v) if isinstance(v, set) else v) for k, v in stats.items()}
    with lk:
        stats.update({"requests": 0, "mixed_view": 0, "errors": 0, "versions_seen": set(), "empty": 0, "error_examples": []})
    for rnd in range(6):                      # CONTROL: remove then add in two requests
        for t in sample:
            cur[t] = 2 if cur[t] == 1 else 1
            swap(t, cur[t], atomic=False)
        time.sleep(0.3)
    stop.set()
    for th in ths: th.join()
    proj.kill(); proj.wait()
    out["atomic_swap"] = dict(atomic_stats, swaps=swaps)
    out["two_request_swap_control"] = {k: (sorted(v) if isinstance(v, set) else v) for k, v in stats.items()}
    out["pass_phase_b"] = atomic_stats["mixed_view"] == 0 and atomic_stats["errors"] == 0 and atomic_stats["empty"] == 0 and atomic_stats["requests"] > 200
    save("C1_bluegreen", out)
    print(json.dumps(out, indent=1, default=str))
    print("PASS_B" if out["pass_phase_b"] else "FAIL_B")

if __name__ == "__main__":
    nt = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    ns = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    per = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    ph = sys.argv[4] if len(sys.argv) > 4 else "all"
    if ph in ("all", "a"):
        phase_a(nt, ns, per)
    if ph in ("all", "b"):
        phase_b(nt)

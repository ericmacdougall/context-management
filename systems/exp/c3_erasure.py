"""C3 a-c: erasure of one data subject, three ES purge methods, bytes checked on disk, time vs tenant size.
usage: c3_erasure.py [sizes=5000,20000,60000]
Methods: (i) delete docs + lease-gated force merge; (ii) alias-swap rebuild of affected indices from surviving rows;
(iii) drop the tenant's indices and re-project from Postgres. Control: delete + immediate force merge (no gate).
The marker check greps the container data directory read-only via docker exec; a positive control (marker present
before deletion) is required for every subject or the result is void."""
import collections, subprocess, sys, time, uuid
from common import *
from memsys import gen, store, erasure, projector

T = "e3"
ES_C, ES_ROOT = "emlab-es-16096", "/usr/share/elasticsearch/data"
PG_C, PG_ROOT = "emlab-pg-16096", "/var/lib/postgresql/data"

def grep(container, root, marker):
    r = subprocess.run(["docker", "exec", container, "grep", "-rlaF", "--", marker, root], capture_output=True, text=True)
    files = [l for l in r.stdout.splitlines() if l]
    kinds = collections.Counter((f.rsplit(".", 1)[-1] if "." in f.rsplit("/", 1)[-1] else f.rsplit("/", 1)[-1]) for f in files)
    return {"files": len(files), "by_kind": dict(kinds)}

def flush_all(es):
    es.req("POST", f"/sys-{T}-events/_flush", force=True, ok=None)
    es.req("POST", f"/sys-{T}-facts/_flush", force=True, ok=None)

def seq_gate(es, maxseq, timeout=150):
    """Wait until every retention lease of every listed index retains above that index's highest delete seq_no."""
    t0 = time.time()
    names = ",".join(maxseq)
    while time.time() - t0 < timeout:
        j = es.req("GET", f"/{names}/_stats", level="shards", filter_path="indices.*.shards.*.retention_leases")[1]
        ok = True
        for idx, ms in maxseq.items():
            for shard in j["indices"][idx]["shards"].values():
                for sh in shard:
                    for lease in sh["retention_leases"]["leases"]:
                        if lease["retaining_seq_no"] <= ms:
                            ok = False
        if ok:
            return round(time.time() - t0, 2)
        time.sleep(1)
    return None

def es_delete_docs(es, p1, recipe=1):
    """Bulk-delete the subject's event and fact docs; returns {concrete_index: max _seq_no of the deletes}."""
    lines = []
    for eid, ts in zip(p1["event_ids"], p1["ts"]):
        lines.append({"delete": {"_index": esx.ev_walias(T, esx.bucket_of(ts)), "_id": eid}})
    for fid in p1["fact_ids"].get(recipe, []):
        lines.append({"delete": {"_index": esx.fx_walias(T, recipe), "_id": fid}})
    maxseq = {}
    for i in range(0, len(lines), 2000):
        r = es.bulk(lines[i:i + 2000])
        for it in r["items"]:
            d = it["delete"]
            if d.get("status") in (200, 404) and "_seq_no" in d:
                maxseq[d["_index"]] = max(maxseq.get(d["_index"], -1), d["_seq_no"])
    es.req("POST", f"/sys-{T}-events,sys-{T}-facts/_refresh")
    return maxseq

def verify_subject(conn, es, receipt_ids, subject, marker):
    ids = receipt_ids
    pgq = lambda q: conn.execute(q, (T, ids)).fetchone()[0]
    es_events = es.req("POST", f"/sys-{T}-events/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"event_id": ids}}})[1]["hits"]["total"]["value"]
    es_facts = es.req("POST", f"/sys-{T}-facts/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"input_event_ids": ids}}})[1]["hits"]["total"]["value"]
    es_marker = es.req("POST", f"/sys-{T}-events,sys-{T}-facts/_search", {"size": 0, "track_total_hits": True, "query": {"match": {"content": marker}}})[1]["hits"]["total"]["value"]
    return {"pg_facts_touching": pgq("select count(*) from facts where tenant_id=%s and input_event_ids && %s"),
            "pg_concepts_touching": pgq("select count(*) from concepts where tenant_id=%s and input_event_ids && %s"),
            "es_event_docs": es_events, "es_fact_docs_touching": es_facts, "es_search_hits_for_marker": es_marker}

def one_size(N, results, save_receipts):
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es); esx.drop_tenant(es, T)
    markers = {u: "zxq" + uuid.uuid4().hex[:12] for u in ("u3", "u4", "u5", "u6")}
    sessions = gen.gen_tenant(T, 3, max(10, N // 50), 50, n_users=20)
    for s in sessions:
        m = markers.get(s["subject_id"])
        if m:
            for e in s["events"]:
                e["content"] += f" {m}"
    t0 = time.time(); n = gen.load(conn, T, sessions, 3, store); t_load = time.time() - t0
    t0 = time.time(); projector.Projector(conn, lock, es, batch=1000, tenants=[T]).run(); t_proj = time.time() - t0
    flush_all(es)
    own = {u: conn.execute("select count(*) from events where tenant_id=%s and subject_id=%s", (T, u)).fetchone()[0] for u in markers}
    row = {"events": n, "load_s": round(t_load, 1), "project_s": round(t_proj, 1), "subject_events": own, "es_docs": sum(int(r["docs.count"]) for r in esx.tenant_indices(es, T)),
           "es_indices": len(esx.tenant_indices(es, T))}
    pos = {u: grep(ES_C, ES_ROOT, m) for u, m in markers.items()}
    row["positive_control_es"] = pos
    conn.execute("create table c3_ctl(x text)"); conn.execute("insert into c3_ctl values (%s)", (markers["u3"] + "ctl",)); conn.execute("checkpoint")
    row["pg_positive_control_files"] = grep(PG_C, PG_ROOT, markers["u3"] + "ctl")["files"]; conn.execute("drop table c3_ctl")
    row["pg_marker_before"] = {u: grep(PG_C, PG_ROOT, m)["files"] for u, m in markers.items()}
    # --- control: delete + immediate force merge, no gate
    u = "u6"; p1 = erasure.phase1(conn, T, u, (1,), "ctl")
    ms = es_delete_docs(es, p1); flush_all(es)
    for idx in ms: es.req("POST", f"/{idx}/_forcemerge", max_num_segments=1)
    flush_all(es)
    row["control_delete_then_immediate_forcemerge"] = {"marker_files_after": grep(ES_C, ES_ROOT, markers[u])}
    # --- (i) delete + lease-gated force merge
    u = "u3"; t0 = time.time(); p1 = erasure.phase1(conn, T, u, (1,), "m1"); t_p1 = time.time() - t0
    t1 = time.time(); ms = es_delete_docs(es, p1); flush_all(es); t_del = time.time() - t1
    gate = seq_gate(es, ms)
    t2 = time.time()
    for idx in ms: es.req("POST", f"/{idx}/_forcemerge", max_num_segments=1)
    flush_all(es); t_merge = time.time() - t2
    row["m1_lease_gated_forcemerge"] = {"phase1_pg_s": round(t_p1, 2), "es_delete_s": round(t_del, 2), "lease_gate_wait_s": gate, "forcemerge_s": round(t_merge, 2),
                                         "es_total_s": round(t_del + (gate or 0) + t_merge, 2), "indices_merged": len(ms), "marker_files_after": grep(ES_C, ES_ROOT, markers[u]),
                                         "verify": verify_subject(conn, es, p1["event_ids"], u, markers[u])}
    # --- (ii) alias-swap rebuild
    u = "u4"; rc = erasure.erase_subject(conn, lock, es, T, u, (1,)); flush_all(es)
    m2ids = [r[0] for r in conn.execute("select event_id from tombstones where receipt_id=%s", (rc["receipt_id"],)).fetchall()]
    row["m2_alias_swap_rebuild"] = {"phase1_pg_s": rc["phase1_s"], "es_purge_s": rc["es_purge_s"], "indices_rebuilt": len(rc["es_rebuilt"]),
                                     "marker_files_after": grep(ES_C, ES_ROOT, markers[u]),
                                     "verify": verify_subject(conn, es, m2ids, u, markers[u])}
    save_receipts[N] = rc
    # --- (iii) drop everything of the tenant, re-project survivors
    u = "u5"; p1 = erasure.phase1(conn, T, u, (1,), "m3")
    t0 = time.time(); names = esx.drop_tenant(es, T); t_drop = time.time() - t0
    conn.execute("delete from projection_state where tenant_id=%s and view in ('events','facts')", (T,))
    t0 = time.time(); projector.Projector(conn, lock, es, batch=1000, tenants=[T], derive=False).run(); t_re = time.time() - t0
    flush_all(es)
    row["m3_drop_and_reproject"] = {"phase1_pg_s": round(p1["phase1_s"], 2), "drop_s": round(t_drop, 2), "indices_dropped": len(names), "reproject_s": round(t_re, 2),
                                     "es_total_s": round(t_drop + t_re, 2), "marker_files_after": grep(ES_C, ES_ROOT, markers[u]),
                                     "verify": verify_subject(conn, es, p1["event_ids"], u, markers[u])}
    row["pg_marker_after"] = {u: grep(PG_C, PG_ROOT, m)["files"] for u, m in markers.items()}
    row["survivor_identity"] = {"events": compare_events(conn, es, [T]), "facts": compare_facts(conn, es, [T], 1)}
    row["receipt_verification"] = {"m2": rc["verification"]}
    results[str(N)] = row
    print(json.dumps(row, default=str), flush=True)

def main():
    sizes = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "5000,20000,60000").split(",")]
    results, receipts = {}, {}
    out = {"workload": f"one tenant, N events (50/session, 20 data subjects, ~5% per subject, marker string in the subject's events), bucket span {esx.BUCKET_DAYS} d, 1 shard 0 replicas; "
                       "ES 9.5.6 single node and PG16 container on t2-dev (shared host)", "disk_free_gb_before": disk_free_gb()}
    for N in sizes:
        one_size(N, results, receipts)
    out["results"] = results
    out["receipt_sample"] = receipts.get(sizes[min(1, len(sizes) - 1)])
    ok = True
    for r in results.values():
        for k in ("m1_lease_gated_forcemerge", "m2_alias_swap_rebuild", "m3_drop_and_reproject"):
            m = r[k]
            ok &= m["marker_files_after"]["files"] == 0 and all(v == 0 for v in m["verify"].values())
        ok &= all(v["files"] > 0 for v in r["positive_control_es"].values())
        ok &= r["survivor_identity"]["events"]["missing"] == 0 and r["survivor_identity"]["facts"]["missing"] == 0
        ok &= all(v == 0 for v in r["pg_marker_after"].values())
    out["pass"] = bool(ok)
    out["disk_free_gb_after"] = disk_free_gb()
    save("C3_erasure", out)
    print("PASS" if ok else "FAIL")

if __name__ == "__main__":
    main()

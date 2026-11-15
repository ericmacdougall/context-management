"""C3 v2 (after the independent review): erasure of one data subject, SPEAKER-PROVENANCE scope only.
Three ES purge methods + control, markers in event content AND in slot fields (entity name, predicate, scope, object),
a second recipe's facts index present, key-byte residue check in the key service and the event DB (with positive
controls), cross-speaker case documenting the scope gap. usage: c3_erasure_v2.py [sizes=5000,20000,60000]"""
import collections, subprocess, sys, time, uuid
from common import *
from memsys import gen, store, erasure, projector, kms

T = "e3"
ES_C, ES_ROOT = "emlab-es-16096", "/usr/share/elasticsearch/data"
PG_C, PG_ROOT = "emlab-pg-16096", "/var/lib/postgresql/data"
KEY_C = "emlab-keys-16096"


def grep(container, root, marker, stdin=False):
    """Read-only grep inside the container. stdin=True passes the pattern on stdin (used for key bytes: never printed or logged)."""
    cmd = ["docker", "exec"] + (["-i"] if stdin else []) + [container, "grep", "-rlaF"] + (["-f", "-"] if stdin else ["--", marker]) + [root]
    r = subprocess.run(cmd, capture_output=True, text=True, input=(marker + "\n") if stdin else None)
    files = [l for l in r.stdout.splitlines() if l]
    kinds = collections.Counter((f.split(root)[-1].strip("/").split("/")[0] if root in f else "?") + ":" + (f.rsplit(".", 1)[-1] if "." in f.rsplit("/", 1)[-1] else f.rsplit("/", 1)[-1][:8]) for f in files)
    return {"files": len(files), "by_kind": dict(kinds)}


def flush_all(es):
    es.req("POST", f"/sys-{T}-events/_flush", force=True, ok=None)
    for r in esx.tenant_indices(es, T):
        if "-facts-v" in r["index"]:
            es.req("POST", f"/{r['index']}/_flush", force=True, ok=None)


def tenant_idx(es, kind):
    return [r["index"] for r in esx.tenant_indices(es, T) if f"-{kind}-" in r["index"]]


def seq_gate(es, indices, timeout=150):
    t0 = time.time()
    names = ",".join(indices)
    j = es.req("GET", f"/{names}/_stats", level="shards", filter_path="indices.*.shards.*.seq_no")[1]
    mx = {i: max(sh["seq_no"]["max_seq_no"] for shards in j["indices"][i]["shards"].values() for sh in shards) for i in indices}
    while time.time() - t0 < timeout:
        j = es.req("GET", f"/{names}/_stats", level="shards", filter_path="indices.*.shards.*.retention_leases")[1]
        ok = all(lease["retaining_seq_no"] > mx[i] for i in indices for shards in j["indices"][i]["shards"].values() for sh in shards for lease in sh["retention_leases"]["leases"])
        if ok:
            return round(time.time() - t0, 2)
        time.sleep(1)
    return None


def es_delete_docs(es, ids):
    es.req("POST", f"/sys-{T}-events/_delete_by_query", {"query": {"terms": {"event_id": ids}}}, refresh=True, conflicts="proceed", slices=1)
    for i in tenant_idx(es, "facts"):
        es.req("POST", f"/{i}/_delete_by_query", {"query": {"terms": {"input_event_ids": ids}}}, refresh=True, conflicts="proceed", slices=1)


def verify_subject(conn, es, ids, marker):
    r = erasure.verify(conn, kms.get(), es, {"tenant_id": T, "subject_id": "?", "event_ids": ids}) if False else None
    v = {"pg_facts_touching": conn.execute("select count(*) from facts where tenant_id=%s and input_event_ids && %s", (T, ids)).fetchone()[0],
         "pg_concepts_touching": conn.execute("select count(*) from concepts where tenant_id=%s and input_event_ids && %s", (T, ids)).fetchone()[0],
         "es_event_docs": es.req("POST", f"/sys-{T}-events/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"event_id": ids}}})[1]["hits"]["total"]["value"]}
    fx = tenant_idx(es, "facts")
    v["es_fact_docs_touching"] = es.req("POST", f"/{','.join(fx)}/_search", {"size": 0, "track_total_hits": True, "query": {"terms": {"input_event_ids": ids}}})[1]["hits"]["total"]["value"]
    v["es_hits_for_marker_text"] = es.req("POST", f"/sys-{T}-events,{','.join(fx)}/_search", {"size": 0, "track_total_hits": True, "query": {"multi_match": {"query": marker, "fields": ["content", "object", "cue"]}}})[1]["hits"]["total"]["value"]
    v["es_facts_indices"] = len(fx)
    return v


def inject_markers(sessions, markers):
    for u, m in markers.items():
        mine = [s for s in sessions if s["subject_id"] == u]
        for s in mine:
            for e in s["events"]:
                e["content"] += f" {m}"                                   # marker in event content
        base = mine[0]["events"][-1]["ts_event"]
        import datetime as dt
        for k in range(3):                                                # marker in every slot field and the object
            mine[0]["events"].append(dict(ts_event=base + dt.timedelta(seconds=k + 1), actor="user", trust_origin="user_typed", kind="message", subject_id=u,
                                          content=f"Zxq{m}{k}'s hobby{m} for work{m} is value{m}."))


def one_size(N, results, receipts):
    conn, lock, es = conns()
    db.reset_schema(conn); esx.install_templates(es); esx.drop_tenant(es, T)
    sessions = gen.gen_tenant(T, 3, max(10, N // 50), 50, n_users=20)
    cnt = collections.Counter(s["subject_id"] for s in sessions)
    top = [u for u, _ in cnt.most_common(4)]
    markers = {u: "zxq" + uuid.uuid4().hex[:12] for u in top}
    U_CTL, U_1, U_2, U_3 = top
    inject_markers(sessions, markers)
    t0 = time.time(); n = gen.load(conn, T, sessions, 3, store); t_load = time.time() - t0
    t0 = time.time(); projector.Projector(conn, lock, es, recipes=(1, 2), live_recipe=1, batch=1000, tenants=[T]).run(); t_proj = time.time() - t0
    flush_all(es)
    row = {"events": n, "load_s": round(t_load, 1), "project_s_both_recipes": round(t_proj, 1), "subjects": {"control": U_CTL, "m1": U_1, "m2": U_2, "m3": U_3},
           "facts_indices": tenant_idx(es, "facts"), "es_docs": sum(int(r["docs.count"]) for r in esx.tenant_indices(es, T))}
    k = kms.get()
    row["positive_control_es"] = {u: grep(ES_C, ES_ROOT, m)["files"] for u, m in markers.items()}
    # Postgres: plaintext control, key-byte control
    ctl = "zxq" + uuid.uuid4().hex[:12]
    conn.execute("create table c3_ctl(x text)"); conn.execute("insert into c3_ctl values (%s)", (ctl,)); conn.execute("checkpoint")
    row["pg_plaintext_positive_control_files"] = grep(PG_C, PG_ROOT, ctl)["files"]; conn.execute("drop table c3_ctl")
    keys = {u: k.key_hex(T, u) for u in top}
    row["key_bytes_before"] = {"in_key_service": {u: grep(KEY_C, PG_ROOT, kh, stdin=True)["files"] for u, kh in keys.items()},
                               "in_event_db": {u: grep(PG_C, PG_ROOT, kh, stdin=True)["files"] for u, kh in keys.items()}}
    row["pg_marker_files_event_db_before"] = {u: grep(PG_C, PG_ROOT, m)["files"] for u, m in markers.items()}
    # --- control: delete + immediate force merge
    u = U_CTL; p1 = erasure.phase1(conn, k, T, u, "ctl"); ids = [r[0] for r in conn.execute("select event_id from tombstones where receipt_id='ctl'").fetchall()]
    erasure.complete_job(conn, lock, es, "ctl", es_purge_enabled=False)
    es_delete_docs(es, ids); flush_all(es)
    for i in tenant_idx(es, "events") + tenant_idx(es, "facts"): es.req("POST", f"/{i}/_forcemerge", max_num_segments=1)
    flush_all(es)
    row["control_delete_then_immediate_forcemerge"] = {"marker_files_after": grep(ES_C, ES_ROOT, markers[u])}
    # --- (i) delete + lease-gated force merge
    u = U_1; jid = "m1"; t0 = time.time(); p1 = erasure.phase1(conn, k, T, u, jid); t_p1 = time.time() - t0
    ids = [r[0] for r in conn.execute("select event_id from tombstones where receipt_id=%s", (jid,)).fetchall()]
    erasure.complete_job(conn, lock, es, jid, es_purge_enabled=False)
    t1 = time.time(); es_delete_docs(es, ids); flush_all(es); t_del = time.time() - t1
    idxs = tenant_idx(es, "events") + tenant_idx(es, "facts")
    gate = seq_gate(es, idxs); t2 = time.time()
    for i in idxs: es.req("POST", f"/{i}/_forcemerge", max_num_segments=1)
    flush_all(es); t_merge = time.time() - t2
    row["m1_lease_gated_forcemerge"] = {"phase1_pg_s": round(t_p1, 2), "es_delete_s": round(t_del, 2), "lease_gate_wait_s": gate, "forcemerge_s": round(t_merge, 2),
                                         "es_total_s": round(t_del + (gate or 0) + t_merge, 2), "indices_merged": len(idxs), "marker_files_after": grep(ES_C, ES_ROOT, markers[u]),
                                         "verify": verify_subject(conn, es, ids, markers[u])}
    # --- (ii) alias-swap rebuild (full cascade: phase 1, key destroy, rebuild every events bucket and every facts index)
    u = U_2; rc = erasure.erase_subject(conn, lock, es, T, u); flush_all(es)
    ids = [r[0] for r in conn.execute("select event_id from tombstones where receipt_id=%s", (rc["receipt_id"],)).fetchall()]
    row["m2_alias_swap_rebuild"] = {"phase1_pg_s": rc["phase1_s"], "key_destroy_and_es_purge_s": rc["es_and_key_steps_s"], "indices_rebuilt": len(rc["es_rebuilt"]),
                                     "facts_indices_rebuilt": [x[1] for x in rc["es_rebuilt"] if x[0] == "facts"], "marker_files_after": grep(ES_C, ES_ROOT, markers[u]),
                                     "verify": verify_subject(conn, es, ids, markers[u])}
    receipts[N] = rc
    # --- (iii) drop everything of the tenant, re-project survivors
    u = U_3; jid = "m3"; p1 = erasure.phase1(conn, k, T, u, jid); ids = [r[0] for r in conn.execute("select event_id from tombstones where receipt_id=%s", (jid,)).fetchall()]
    erasure.complete_job(conn, lock, es, jid, es_purge_enabled=False)
    t0 = time.time(); names = esx.drop_tenant(es, T); t_drop = time.time() - t0
    conn.execute("delete from projection_state where tenant_id=%s and view in ('events','facts')", (T,)); conn.execute("delete from es_buckets where tenant_id=%s", (T,))
    t0 = time.time(); projector.Projector(conn, lock, es, recipes=(1, 2), live_recipe=1, batch=1000, tenants=[T], derive=False).run(); t_re = time.time() - t0
    flush_all(es)
    row["m3_drop_and_reproject"] = {"phase1_pg_s": round(p1["phase1_s"], 2), "drop_s": round(t_drop, 2), "indices_dropped": len(names), "reproject_s": round(t_re, 2),
                                     "es_total_s": round(t_drop + t_re, 2), "marker_files_after": grep(ES_C, ES_ROOT, markers[u]), "verify": verify_subject(conn, es, ids, markers[u])}
    # --- Postgres residue after all erasures
    row["pg_marker_files_event_db_after"] = {u: grep(PG_C, PG_ROOT, m)["files"] for u, m in markers.items()}
    row["key_bytes_after"] = {"in_key_service": {u: grep(KEY_C, PG_ROOT, kh, stdin=True) for u, kh in keys.items() if u != top[0] or True}, "in_event_db": {u: grep(PG_C, PG_ROOT, kh, stdin=True)["files"] for u, kh in keys.items()}}
    row["survivor_identity"] = {"events": compare_events(conn, es, [T]), "facts_v1": compare_facts(conn, es, [T], 1)}
    row["receipt_verification"] = rc["verification"]
    results[str(N)] = row
    print(json.dumps(row, default=str), flush=True)


def cross_speaker(conn, lock, es):
    """Documents the scope: data ABOUT a person spoken by someone else survives erasure of that person's own events."""
    T2 = "xs"
    esx.drop_tenant(es, T2)
    import datetime as dt
    ts = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    ev = lambda i, subj, text: dict(ts_event=ts + dt.timedelta(minutes=i), actor="user", trust_origin="user_typed", kind="message", subject_id=subj, content=text)
    store.append_events(conn, T2, "sA", [ev(0, "uAnna", "Bobby's city is Paris")])
    store.append_events(conn, T2, "sB", [ev(1, "uBobby", "Bobby's pet is cat")])
    projector.Projector(conn, lock, es, tenants=[T2]).run()
    erasure.erase_subject(conn, lock, es, T2, "uBobby")
    rows = conn.execute("select slot_enc, object_enc, subject_id, status from facts where tenant_id=%s", (T2,)).fetchall()
    kk = kms.get(); left = [(kk.dec_many(T2, [(s, a)])[0], kk.dec_many(T2, [(s, b)])[0], st) for a, b, s, st in rows]
    es_hits = es.req("POST", f"/sys-{T2}-facts/_search", {"size": 5, "query": {"match_all": {}}})[1]["hits"]["total"]["value"]
    return {"erased_subject": "uBobby (speaker)", "facts_about_Bobby_remaining_in_pg": len(left), "es_fact_docs_remaining": es_hits,
            "interpretation": "facts ABOUT Bobby that another speaker (uAnna) stated remain current: erasure here is speaker-provenance only; the user must decide what a data subject is"}


def main():
    sizes = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "5000,20000,60000").split(",")]
    results, receipts = {}, {}
    out = {"scope": "speaker-provenance erasure only (events produced by the subject and rows derived from them)",
           "workload": "one tenant, N events (50/session, 20 data subjects, ~5% per subject), markers in event content and in entity/predicate/scope/object fields, recipes 1 and 2 projected "
                       "(two facts indices), bucket span 90 d; ES 9.5.6 + PG16 (events) + PG16 key service on t2-dev (shared)", "disk_free_gb_before": disk_free_gb()}
    for N in sizes:
        one_size(N, results, receipts)
        if disk_free_gb() < 18:
            out["stopped_for_disk"] = disk_free_gb(); break
    conn, lock, es = conns()
    out["results"] = results; out["receipt_sample"] = receipts.get(sizes[min(1, len(sizes) - 1)])
    out["cross_speaker_case"] = cross_speaker(conn, lock, es)
    ok = True
    for r in results.values():
        for kk in ("m1_lease_gated_forcemerge", "m2_alias_swap_rebuild", "m3_drop_and_reproject"):
            m = r[kk]
            ok &= m["marker_files_after"]["files"] == 0 and all(v == 0 for key, v in m["verify"].items() if key != "es_facts_indices") and m["verify"]["es_facts_indices"] >= 2
        ok &= all(v > 0 for v in r["positive_control_es"].values()) and r["pg_plaintext_positive_control_files"] > 0
        ok &= all(v > 0 for v in r["key_bytes_before"]["in_key_service"].values())          # positive control for key-byte search
        ok &= all(v["files"] == 0 for v in r["key_bytes_after"]["in_key_service"].values()) and all(v == 0 for v in r["key_bytes_after"]["in_event_db"].values())
        ok &= all(v == 0 for v in r["pg_marker_files_event_db_after"].values()) and all(v == 0 for v in r["key_bytes_before"]["in_event_db"].values())
        ok &= r["survivor_identity"]["events"]["missing"] == 0 and r["survivor_identity"]["facts_v1"]["missing"] == 0
    out["pass"] = bool(ok)
    out["disk_free_gb_after"] = disk_free_gb()
    save("C3_erasure_v2", out)
    print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()

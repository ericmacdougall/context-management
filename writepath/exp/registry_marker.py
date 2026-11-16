"""Store inventory (B) and erasure reach (C, N6) with planted markers.

1. registry.unregistered() on the live system must be [] ; controls: a rogue table, a rogue index and a rogue log file
   must each be listed (then removed).
2. Plant a unique marker for subject X (and a survivor marker for Y) through every write path: events, facts, inbox,
   analyst job result, quarantine (a zombie write), decision payload. Project to ES.
3. Before erasure: SQL scan of every table of wp_events and wp_keys (row::text) = 0 hits (no plaintext in Postgres);
   ES API hits > 0; on-disk greps of the PG data directory (after CHECKPOINT) and the ES data directory are recorded.
   Positive control for the PG grep: the old code's plaintext marker in database 'emlab' on the same cluster.
4. Erase X. ES API hits for X = 0, Y > 0. Force-merge and flush the tenant's indices; on-disk ES grep again.
   X's key hex: grep of the key database files before destroy (positive) and after (must be 0).
Greps run inside this stream's own containers via docker exec; patterns go in on stdin, never on a command line."""
import subprocess
import sys
import uuid

from exp.common import write_result
from exp.util import tenant, msg, fact, session, es_count_all
from wp import config, db, erasure, keys as K, lease as L, log, projector, registry
from wp.es import ES

PG_C, ES_C = "emlab-pg-wp-16096", "emlab-es-wp-16096"


def grep_files(container, path, pattern):
    """Number of files under path containing pattern (fixed string, from stdin)."""
    r = subprocess.run(["docker", "exec", "-i", container, "grep", "-rlaF", "-f", "-", path], input=pattern.encode() + b"\n",
                       capture_output=True, timeout=600)
    return len([x for x in r.stdout.decode().splitlines() if x.strip()])


def sql_scan(conn, marker):
    hits = {}
    for (rel,) in conn.execute("select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind='r' and n.nspname='public'").fetchall():
        n = conn.execute(f'select count(*) from "{rel}" x where x::text like %s', (f"%{marker}%",)).fetchone()[0]
        if n:
            hits[rel] = n
    return hits


def broken_rebuild(conn, ks, es, t, f, epoch, hook=None):
    """BROKEN purge (control): delete-by-query in place, no fresh index (deleted docs stay in segments)."""
    q = {"bool": {"should": [{"term": {"subject_id": "X"}}]}}
    es.req("POST", f"/{config.INDEX_PREFIX}{t}-{f}-*/_delete_by_query", {"query": q}, refresh="true", conflicts="proceed")
    return {"family": f, "broken": True}


def run_one(arm):
    conn, ks, es = db.connect(), K.KeyService(), ES()
    out = {"arm": arm}
    # ---- 1. inventory
    out["unregistered_clean"] = registry.unregistered(conn, ks.conn, es)
    conn.execute("create table rogue_cache(x text)")
    es.req("PUT", f"/{config.INDEX_PREFIX}zz-rogue")
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "rogue.txt").write_text("x")
    flagged = registry.unregistered(conn, ks.conn, es)
    out["controls_flagged"] = {"table": "pg:rogue_cache" in flagged, "index": f"es-index:{config.INDEX_PREFIX}zz-rogue" in flagged,
                               "logfile": any(f.endswith("rogue.txt") for f in flagged)}
    conn.execute("drop table rogue_cache"); es.req("DELETE", f"/{config.INDEX_PREFIX}zz-rogue"); (config.LOG_DIR / "rogue.txt").unlink()
    out["unregistered_after_cleanup"] = registry.unregistered(conn, ks.conn, es)
    out["stores_registered"] = len(registry.STORES)
    out["stores_verifiable"] = [s.name for s in registry.verifiable()]
    # ---- 2. plant
    t = tenant("mk")
    mx, my = "zqx" + uuid.uuid4().hex[:10], "zqy" + uuid.uuid4().hex[:10]
    s = session(conn, t)
    r = log.append(conn, ks, s, [msg(0, f"X says {mx}", "X"), msg(1, f"Y says {my}", "Y"), fact(2, "X", "Xavier", "note", mx), fact(3, "Y", "Yolanda", "note", my)])
    log.inbox_enqueue(conn, ks, s, "i1", "s1", "X", {"text": mx})
    log.decide(conn, ks, s, "X", "scope", 0, "state", {"text": mx}, r["accepted"][0], "dk1", msg(4, "", "X")["ts_event"])
    jobs_done = 0
    while (job := log.job_claim(conn, t)) is not None:
        subj = conn.execute("select subject_id from events where tenant_id=%s and event_id=%s", (t, job[2][0])).fetchone()[0]
        jobs_done += log.job_complete(conn, ks, t, job[0], job[1], f"summary {mx if subj == 'X' else my}", {"ok": True, "inputs_checked": 1})
    stale = L.Lease(t, s.scope, s.epoch - 1, "zombie", None)
    zr = log.append(conn, ks, stale, [msg(5, f"zombie {mx}", "X")])
    out["planted"] = {"events": 4, "inbox": 1, "decision": 1, "analyst_jobs_done": jobs_done, "quarantined": zr["quarantined"]}
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()
    conn.execute("checkpoint")
    out["before"] = {"pg_sql_hits_events_db": sql_scan(conn, mx), "pg_sql_hits_keys_db": sql_scan(ks.conn, mx),
                     "es_api_X": es_count_all(es, t, {"multi_match": {"query": mx, "fields": ["content", "value"]}}),
                     "es_api_Y": es_count_all(es, t, {"multi_match": {"query": my, "fields": ["content", "value"]}}),
                     "pg_disk_files_X": grep_files(PG_C, "/var/lib/postgresql/data", mx),
                     "pg_disk_positive_control_old_plaintext": grep_files(PG_C, "/var/lib/postgresql/data", "zqn6secret"),
                     "es_disk_files_X": grep_files(ES_C, "/usr/share/elasticsearch/data", mx)}
    keyhex = ks.keys(t, ["X"])["X"].hex()
    out["before"]["keydb_disk_files_keyhex"] = grep_files(PG_C, "/var/lib/postgresql/data", keyhex)
    # ---- 4. erase
    real = erasure.rebuild_family
    if arm == "control":                    # BROKEN: in-place delete, key kept, no force merge
        erasure.rebuild_family = broken_rebuild
    try:
        rec = erasure.erase(conn, ks, es, t, "X", destroy_key=(arm != "control"))
    finally:
        erasure.rebuild_family = real
    names = [i for i in es.indices(f"{config.INDEX_PREFIX}{t}-*")]
    if arm != "control":
        es.req("POST", f"/{','.join(names)}/_forcemerge", max_num_segments=1)
    es.req("POST", f"/{','.join(names)}/_flush")
    conn.execute("checkpoint")
    out["receipt"] = {k: rec[k] for k in ("done", "verify", "rebuilt", "error_type", "untested_stores")}
    out["after"] = {"pg_sql_hits_events_db": sql_scan(conn, mx), "pg_sql_hits_keys_db": sql_scan(ks.conn, mx),
                    "es_api_X": es_count_all(es, t, {"multi_match": {"query": mx, "fields": ["content", "value"]}}),
                    "es_api_Y": es_count_all(es, t, {"multi_match": {"query": my, "fields": ["content", "value"]}}),
                    "es_disk_files_X": grep_files(ES_C, "/usr/share/elasticsearch/data", mx),
                    "es_disk_files_Y": grep_files(ES_C, "/usr/share/elasticsearch/data", my),
                    "keydb_disk_files_keyhex": grep_files(PG_C, "/var/lib/postgresql/data", keyhex)}
    del keyhex
    b, a = out["before"], out["after"]
    es_disk_claim = b["es_disk_files_X"] > 0
    out["es_on_disk_claim_made"] = es_disk_claim
    out["holds"] = (out["unregistered_clean"] == [] and all(out["controls_flagged"].values()) and out["unregistered_after_cleanup"] == []
                   and not b["pg_sql_hits_events_db"] and not b["pg_sql_hits_keys_db"] and b["pg_disk_files_X"] == 0
                   and b["pg_disk_positive_control_old_plaintext"] > 0 and b["es_api_X"] > 0 and b["keydb_disk_files_keyhex"] > 0
                   and rec["done"] and a["es_api_X"] == 0 and a["es_api_Y"] > 0 and a["keydb_disk_files_keyhex"] == 0
                   and (a["es_disk_files_X"] == 0 if es_disk_claim else True))
    out["residue_on_disk"] = {"es_X_files": a["es_disk_files_X"], "keyhex_files": a["keydb_disk_files_keyhex"], "es_api_X": a["es_api_X"]}
    conn.close(); ks.conn.close()
    return out


def main(R):
    arms = {}
    for arm in ("fenced", "control"):
        runs = []
        for _ in range(R):
            try:
                runs.append(run_one(arm))
            except Exception as e:  # noqa: BLE001
                runs.append({"arm": arm, "holds": None, "error": f"{type(e).__name__}: {str(e)[:160]}"})
        arms[arm] = runs
    f_ok = sum(1 for r in arms["fenced"] if r["holds"] is True)
    # control violated = erasure reported, yet marker or key bytes remain on disk / in the API
    c_viol = sum(1 for r in arms["control"] if r.get("residue_on_disk") and (r["residue_on_disk"]["es_X_files"] > 0 or r["residue_on_disk"]["keyhex_files"] > 0))
    res = {"R": R, "fenced_holds": f_ok, "control_violated": c_viol, "pass": f_ok == R and c_viol >= int(0.9 * R), "arms": arms}
    print("registry_marker", "PASS" if res["pass"] else "FAIL", "fenced", f_ok, "/", R, "control violated", c_viol, "/", R)
    print("  sample fenced:", {k: arms["fenced"][0].get(k) for k in ("before", "after", "controls_flagged")})
    print("  sample control:", arms["control"][0].get("residue_on_disk"))
    write_result("registry_marker", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)

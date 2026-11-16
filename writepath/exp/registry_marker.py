"""Store inventory (B) and erasure reach (C, N6) with planted markers.

1. registry.unregistered() on the live system must be [] ; controls: a rogue table, a rogue index and a rogue log file
   must each be listed (then removed).
2. Plant a unique marker for subject X (and a survivor marker for Y) through every write path: events, facts, inbox,
   analyst job result, quarantine (a zombie write), decision payload. Project to ES.
3. Before erasure: SQL scan of every table of wp_events and wp_keys (row::text) = 0 hits (no plaintext in Postgres);
   ES API hits > 0; on-disk greps of the PG data directory (after CHECKPOINT) and the ES data directory are recorded.
   Positive control for the PG grep: the old code's plaintext marker in database 'emlab' on the same cluster.
4. Erase X (the verifier runs the expunge). ES API hits for X = 0, Y > 0; on-disk ES grep again after a flush.
   Controls, one change each: ctl_no_placeholders (runner writes no placeholders), ctl_key_kept (key not destroyed).
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


def run_one(arm):
    conn, ks, es = db.connect(), K.KeyService(), ES()
    adm = db.admin()
    vc, vk = db.verifier(), db.verifier(db=config.KEYS_DB)       # the gate runs as the verifier role (read access to every table)
    out = {"arm": arm}
    # ---- 1. inventory
    out["unregistered_clean"] = registry.unregistered(vc, vk, es)
    adm.execute("create table rogue_cache(x text)")
    es.req("PUT", f"/{config.INDEX_PREFIX}zz-rogue")
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "rogue.txt").write_text("x")
    flagged = registry.unregistered(vc, vk, es)
    out["controls_flagged"] = {"table": "pg:rogue_cache" in flagged, "index": f"es-index:{config.INDEX_PREFIX}zz-rogue" in flagged,
                               "logfile": any(f.endswith("rogue.txt") for f in flagged)}
    adm.execute("drop table rogue_cache"); es.req("DELETE", f"/{config.INDEX_PREFIX}zz-rogue"); (config.LOG_DIR / "rogue.txt").unlink()
    out["unregistered_after_cleanup"] = registry.unregistered(vc, vk, es)
    out["stores_registered"] = len(registry.STORES)
    out["stores_verifiable"] = [s.name for s in registry.verifiable()]
    # ---- 2. plant
    t = tenant("mk")
    mx, my = "zqx" + uuid.uuid4().hex[:10], "zqy" + uuid.uuid4().hex[:10]
    s = session(conn, t)
    r = log.append(conn, ks, s, [msg(0, f"X says {mx}", "X"), msg(1, f"Y says {my}", "Y"), fact(2, "X", "Xavier", "note", mx), fact(3, "Y", "Yolanda", "note", my)])
    log.inbox_enqueue(conn, ks, s, "i1", "s1", "X", {"text": mx})
    log.decide(conn, ks, s, "X", "scope", "approve", None, {"text": mx}, r["accepted"][0], "dk1", msg(4, "", "X")["ts_event"])
    jobs_done = 0
    while (job := log.job_claim(conn, t)) is not None:
        subj = conn.execute("select subject_id from events where tenant_id=%s and event_id=%s", (t, job[2][0])).fetchone()[0]
        jobs_done += log.job_complete(conn, ks, t, job[0], job[1], f"summary {mx if subj == 'X' else my}", {"ok": True, "inputs_checked": 1})
    stale = L.Lease(t, s.scope, s.epoch - 1, "zombie", None)
    zr = log.append(conn, ks, stale, [msg(5, f"zombie {mx}", "X")])
    out["planted"] = {"events": 4, "inbox": 1, "decision": 1, "analyst_jobs_done": jobs_done, "quarantined": zr["quarantined"]}
    p = projector.Projector(conn, ks, es, t)
    p.run(); p.stop()
    adm.execute("checkpoint")
    es.req("POST", f"/{config.INDEX_PREFIX}{t}-*/_flush", expand_wildcards="all")
    out["before"] = {"pg_sql_hits_events_db": sql_scan(adm, mx), "pg_sql_hits_keys_db": sql_scan(ks.conn, mx),
                     "es_api_X": es_count_all(es, t, {"multi_match": {"query": mx, "fields": ["content", "value"]}}),
                     "es_api_Y": es_count_all(es, t, {"multi_match": {"query": my, "fields": ["content", "value"]}}),
                     "pg_disk_files_X": grep_files(PG_C, "/var/lib/postgresql/data", mx),
                     "pg_disk_positive_control_old_plaintext": grep_files(PG_C, "/var/lib/postgresql/data", "zqn6secret"),
                     "es_disk_files_X": grep_files(ES_C, "/usr/share/elasticsearch/data", mx)}
    keyhex = ks.keys(t, ["X"])["X"].hex()
    out["before"]["keydb_disk_files_keyhex"] = grep_files(PG_C, "/var/lib/postgresql/data", keyhex)
    # ---- 4. erase
    # ONE patch per control arm: no ES placeholders (documents stay), or the subject key kept
    rec = erasure.erase(conn, ks, es, t, "X", placeholders=(arm != "ctl_no_placeholders"), destroy_key=(arm != "ctl_key_kept"))
    es.req("POST", f"/{config.INDEX_PREFIX}{t}-*/_flush", expand_wildcards="all")
    adm.execute("checkpoint")
    out["receipt"] = {k: rec.get(k) for k in ("done", "verify", "residue", "error_type", "key_store")}
    out["after"] = {"pg_sql_hits_events_db": sql_scan(adm, mx), "pg_sql_hits_keys_db": sql_scan(ks.conn, mx),
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
    conn.close(); ks.conn.close(); adm.close()
    return out


def main(R):
    arms = {}
    for arm in ("fenced", "ctl_no_placeholders", "ctl_key_kept"):
        runs = []
        for _ in range(R):
            try:
                runs.append(run_one(arm))
            except Exception as e:  # noqa: BLE001
                runs.append({"arm": arm, "holds": None, "error": f"{type(e).__name__}: {str(e)[:160]}"})
        arms[arm] = runs
    f_ok = sum(1 for r in arms["fenced"] if r["holds"] is True)
    # control violated = erasure reported, yet marker or key bytes remain on disk / in the API
    viol = lambda r: bool(r.get("residue_on_disk")) and any(v > 0 for v in r["residue_on_disk"].values())   # noqa: E731
    c_viol = {a: sum(1 for r in arms[a] if viol(r)) for a in ("ctl_no_placeholders", "ctl_key_kept")}
    res = {"R": R, "fenced_holds": f_ok, "control_violated": c_viol, "pass": f_ok == R and all(v >= int(0.9 * R) for v in c_viol.values()), "arms": arms}
    print("registry_marker", "PASS" if res["pass"] else "FAIL", "fenced", f_ok, "/", R, "controls violated", c_viol)
    print("  sample fenced:", {k: arms["fenced"][0].get(k) for k in ("before", "after", "controls_flagged")})
    print("  sample controls:", {a: arms[a][0].get("residue_on_disk") for a in ("ctl_no_placeholders", "ctl_key_kept")})
    write_result("registry_marker", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)

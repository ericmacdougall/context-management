"""F9 (3): Elasticsearch version upgrade. A SECOND throwaway node on the Basic licence (never a trial), older 9.4.x image,
then an in-place upgrade to 9.5.6 by replacing the container on the same volume following the documented single-node steps,
then the alternative path (new version, empty, rebuild from Postgres). Security is DISABLED on this localhost-bound throwaway node
(deviation: certificates and keys live in container config, which a replace-container upgrade would regenerate).
usage: f9_upgrade.py [events=20000]   (runs on t2-dev; docker CLI; removes its containers and volumes at the end)"""
import http.client, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import evo
from memsys import gen, store, projector, reader, esx, db
from memsys.esc import ES
from common import compare_events, compare_facts

NAME, VOL, VOL2 = "emlab-es-upg-16096", "emlab-es-upg-16096-data", "emlab-es-upg-16096-fresh"
PORT = 19310
NEW = "docker.elastic.co/elasticsearch/elasticsearch:9.5.6"
T = "upg1"


class ESPlain(ES):
    def __init__(self):
        self.conn = None; self._auth = "ApiKey none"

    def _c(self):
        if self.conn is None:
            self.conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
        return self.conn


def sh(*a, check=True):
    r = subprocess.run(list(a), capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{a[:4]}: {r.stderr[-300:]}")
    return r.stdout.strip()


def watermarks():
    """Copy the first node's absolute-byte disk watermarks (read through its API; nothing else is read)."""
    try:
        j = ES().req("GET", "/_cluster/settings", flat_settings=True, ok=(200,))[1]
        s = {**j.get("persistent", {}), **j.get("transient", {})}
        w = {k: v for k, v in s.items() if "watermark" in k}
        if w:
            return w, "first node cluster settings"
    except Exception as e:
        err = str(e)[:80]
    return {"cluster.routing.allocation.disk.watermark.low": "10gb", "cluster.routing.allocation.disk.watermark.high": "5gb",
            "cluster.routing.allocation.disk.watermark.flood_stage": "2gb"}, "could not read first node settings; absolute-byte values chosen by me"


def start(image, vol, wm):
    args = ["docker", "run", "-d", "--name", NAME, "--cpus", "4", "--memory", "6g", "-p", f"127.0.0.1:{PORT}:9200", "-v", f"{vol}:/usr/share/elasticsearch/data",
            "-e", "discovery.type=single-node", "-e", "xpack.security.enabled=false", "-e", "ES_JAVA_OPTS=-Xms2g -Xmx2g", "-e", "cluster.name=emlab-upg-16096"]
    for k, v in wm.items():
        args += ["-e", f"{k}={v}"]
    sh(*args, image)
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            es = ESPlain(); es.req("GET", "/_cluster/health", wait_for_status="yellow", timeout="30s"); return es, round(time.time() - t0, 1)
        except Exception:
            time.sleep(2)
    raise RuntimeError("node did not come up")


def exact_results(conn, es, qs):
    from c1_rebuild_v2 import run_queries
    r = run_queries(es, reader.Reader(conn, es), qs)
    return {k: r[k] for k in ("lex", "vec_exact", "asof")}


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    tag = open(os.path.expanduser("~/emlab-16096/systems/upg_image_tag")).read().strip()
    OLD = f"docker.elastic.co/elasticsearch/elasticsearch:{tag}"
    free0 = evo.disk_free_gb()
    if free0 < 33:
        print("not enough free disk for the 8 GB budget:", free0); return
    wm, wm_src = watermarks()
    out = {"workload": f"tenant {T}: {N} events projected into a throwaway ES node (old image {tag}, Basic licence, security off, localhost only, 4 cpus 6 GB), then upgraded to 9.5.6", "old_image": OLD,
           "new_image": NEW, "watermarks": wm, "watermark_source": wm_src, "disk_free_gb_before": free0}
    for v in (VOL, VOL2):
        sh("docker", "volume", "rm", "-f", v, check=False)
    conn = evo.connect(); evo.reset(conn); lock = evo.connect()
    sess = gen.gen_tenant(T, 5, N // 50, 50, n_users=20); gen.load(conn, T, sess, 5, store)
    es, up_s = start(OLD, VOL, wm)
    out["old_version"] = es.req("GET", "/")[1]["version"]["number"]; out["old_build"] = es.req("GET", "/")[1]["version"].get("build_hash", "")[:12]
    lic = es.req("GET", "/_license")[1]["license"]; out["old_license"] = {"type": lic["type"], "status": lic["status"]}
    esx.install_templates(es)
    t0 = time.time(); projector.Projector(conn, lock, es, recipes=(1,), live_recipe=1, tenants=[T], batch=1000).run(); out["project_into_old_s"] = round(time.time() - t0, 1)
    from c1_rebuild_v2 import query_set
    qs = query_set(conn, [T])
    before = {"events": compare_events(conn, es, [T]), "facts": compare_facts(conn, es, [T], 1), "queries": exact_results(conn, es, qs), "docs": es.req("GET", "/_cat/count", format="json")[1][0]["count"]}
    out["pre_upgrade"] = {"events_vs_pg": before["events"], "facts_vs_pg": before["facts"], "docs": before["docs"]}
    try:
        dep = es.req("GET", "/_migration/deprecations", ok=None)[1]
        out["deprecation_api"] = {"cluster_settings": len(dep.get("cluster_settings", [])), "index_settings": {k: len(v) for k, v in dep.get("index_settings", {}).items()}, "node_settings": len(dep.get("node_settings", []))}
    except Exception as e:
        out["deprecation_api"] = str(e)[:100]
    # ---- documented single-node upgrade: stop allocation, flush, stop, replace on the same volume, start, re-enable
    t_up = time.time()
    es.req("PUT", "/_cluster/settings", {"persistent": {"cluster.routing.allocation.enable": "primaries"}})
    es.req("POST", "/_flush", ok=(200,))
    sh("docker", "stop", "-t", "60", NAME); t_stop = time.time()
    sh("docker", "rm", NAME)
    es2, _ = start(NEW, VOL, wm)
    es2.req("PUT", "/_cluster/settings", {"persistent": {"cluster.routing.allocation.enable": None}})
    es2.req("GET", "/_cluster/health", wait_for_status="green", timeout="120s")
    out["in_place_upgrade"] = {"total_s": round(time.time() - t_up, 1), "downtime_stop_to_green_s": round(time.time() - t_stop, 1)}
    out["new_version"] = es2.req("GET", "/")[1]["version"]["number"]
    lic2 = es2.req("GET", "/_license")[1]["license"]; out["new_license"] = {"type": lic2["type"], "status": lic2["status"]}
    idx = es2.req("GET", "/_cat/indices", format="json", h="index,status,health,docs.count")[1]
    out["indices_after"] = {"count": len(idx), "all_open": all(i["status"] == "open" for i in idx), "health": sorted({i["health"] for i in idx})}
    after = {"events": compare_events(conn, es2, [T]), "facts": compare_facts(conn, es2, [T], 1), "queries": exact_results(conn, es2, qs)}
    out["post_upgrade"] = {"events_vs_pg": after["events"], "facts_vs_pg": after["facts"], "exact_query_results_identical": after["queries"] == before["queries"]}
    # writes continue
    import datetime as dt
    store.append_events(conn, T, "post-upgrade", [dict(ts_event=dt.datetime.now(dt.timezone.utc), actor="user", trust_origin="user_typed", kind="message", subject_id="u1", content="Quokka's city is Perth zzpostupgrade")])
    projector.Projector(conn, lock, es2, recipes=(1,), live_recipe=1, tenants=[T], batch=1000).run()
    found = es2.req("POST", f"/sys-{T}-events/_search", {"size": 0, "track_total_hits": True, "query": {"match": {"content": "zzpostupgrade"}}})[1]["hits"]["total"]["value"]
    out["writes_continue_after_upgrade"] = {"new_event_searchable": found == 1, "events_vs_pg": compare_events(conn, es2, [T])}
    # ---- alternative: new version, empty, rebuild from Postgres
    sh("docker", "rm", "-f", NAME)
    es3, up3 = start(NEW, VOL2, wm)
    esx.install_templates(es3)
    conn.execute("delete from projection_state where tenant_id=%s and view in ('events','facts')", (T,)); conn.execute("delete from es_buckets where tenant_id=%s", (T,))
    t0 = time.time(); projector.Projector(conn, lock, es3, recipes=(1,), live_recipe=1, tenants=[T], derive=False, batch=1000).run(); rb = round(time.time() - t0, 1)
    alt = {"events": compare_events(conn, es3, [T]), "facts": compare_facts(conn, es3, [T], 1), "queries": exact_results(conn, es3, qs)}
    out["alternative_fresh_rebuild"] = {"node_start_s": up3, "rebuild_from_postgres_s": rb, "events_vs_pg": alt["events"], "facts_vs_pg": alt["facts"],
                                        "exact_query_results_identical_to_pre_upgrade": alt["queries"] == before["queries"]}
    out["comparison"] = {"in_place_downtime_s": out["in_place_upgrade"]["downtime_stop_to_green_s"], "fresh_rebuild_total_s": round(up3 + rb, 1),
                         "notes": ["in-place keeps old Lucene segments readable by the new version (no reindex) but cannot be rolled back once the new version has written to the volume; take a volume copy first",
                                   "fresh rebuild never touches the old data (old node can stay live until cutover) but needs the full Postgres replay and cursors per target"]}
    ok = (out["new_version"] == "9.5.6" and out["indices_after"]["all_open"] and out["post_upgrade"]["exact_query_results_identical"] and out["old_license"]["type"] == "basic"
          and all(out["post_upgrade"][k]["missing"] == 0 and out["post_upgrade"][k]["hash_mismatch"] == 0 and out["post_upgrade"][k]["duplicates"] == 0 for k in ("events_vs_pg", "facts_vs_pg"))
          and out["writes_continue_after_upgrade"]["new_event_searchable"] and out["alternative_fresh_rebuild"]["exact_query_results_identical_to_pre_upgrade"]
          and out["alternative_fresh_rebuild"]["events_vs_pg"]["missing"] == 0)
    out["pass"] = bool(ok)
    # cleanup: my second node, its volumes and the old image I pulled
    sh("docker", "rm", "-f", NAME, check=False)
    for v in (VOL, VOL2):
        sh("docker", "volume", "rm", "-f", v, check=False)
    sh("docker", "rmi", OLD, check=False)
    out["disk_free_gb_after_cleanup"] = evo.disk_free_gb()
    evo.save("F9_es_upgrade", out)
    print(json.dumps(out, indent=1, default=str)); print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()

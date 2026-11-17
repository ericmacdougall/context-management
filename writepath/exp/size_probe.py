"""Disk sizing for the 1,000,000-document erasure run (exploratory measurement, no PASS).

Builds ONE tenant of N documents exactly as exp/scale_1m.py does (same subjects, exp.erasure_cost._build), then
measures on this stream's own stores: Postgres database sizes (events and keys), the pg_wal directory, the container
data directories (docker exec du), the Elasticsearch store of the stream's indices, and the host's free space; again
after one 10,000-event erasure with physical="wait" (cascade, VACUUM FULL of the key table, expunge merge). Projects
the peak for scale_1m (100,000 + 1,000,000 documents kept side by side) linearly, and states the margin against the
host rule (never leave under 15 GB free). usage: python -m exp.size_probe [N]   (default 100000)"""
import json
import os
import subprocess
import sys
import time

from exp.common import write_result
from exp.erasure_cost import _build
from exp.util import tenant, env
from wp import config, db, erasure

FLOOR_GB = 15.0
SCALE_1M_DOCS = 1_100_000          # scale_1m keeps the 100k and the 1M tenant side by side


def _du(container, path):
    r = subprocess.run(["docker", "exec", container, "du", "-sb", path], capture_output=True, text=True, timeout=600)
    return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout else None


def measure(es):
    adm = db.admin()
    st = os.statvfs(os.path.expanduser("~"))
    _, idx = es.req("GET", f"/_cat/indices/{config.INDEX_PREFIX}*", format="json", bytes="b", h="index,store.size,docs.count",
                    expand_wildcards="all")
    return {"free_gb": round(st.f_bavail * st.f_frsize / 1e9, 2),
            "pg_events_db_b": adm.execute("select pg_database_size(%s)", (config.EVENTS_DB,)).fetchone()[0],
            "pg_keys_db_b": adm.execute("select pg_database_size(%s)", (config.KEYS_DB,)).fetchone()[0],
            "pg_wal_b": _du(config.PG_CONTAINER, "/var/lib/postgresql/data/pg_wal"),
            "pg_datadir_b": _du(config.PG_CONTAINER, "/var/lib/postgresql/data"),
            "es_datadir_b": _du(config.ES_CONTAINER, "/usr/share/elasticsearch/data"),
            "es_store_b": sum(int(i["store.size"] or 0) for i in idx), "es_docs": sum(int(i["docs.count"] or 0) for i in idx)}


def main(n):
    conn, ks, es = env()
    before = measure(es)
    t = tenant("sz")
    subj = {"S10": 10, "S1000": 1000, "S10000": 10000, "X10000": 10000, "H1": 200, "H2": 200, "H3": 200, "H4": 200, "H5": 200, "H6": 200}
    t0 = time.time()
    build = _build(t, n, subj)
    after_build = measure(es)
    rec = erasure.erase(conn, ks, es, t, "S10000", physical="wait")
    after_erase = measure(es)
    keys = ("pg_datadir_b", "es_datadir_b")
    grow_build = sum(after_build[k] - before[k] for k in keys)
    grow_peak = max(grow_build, sum(after_erase[k] - before[k] for k in keys))
    host_used = round(before["free_gb"] - min(after_build["free_gb"], after_erase["free_gb"]), 2)
    per_doc = grow_peak / n
    proj_gb = round(per_doc * SCALE_1M_DOCS / 1e9, 2)
    out = {"n": n, "build": build, "build_and_erase_s": round(time.time() - t0, 1), "erase": {k: rec.get(k) for k in ("done", "error_type", "receipt_state")},
           "before": before, "after_build": after_build, "after_erase": after_erase,
           "growth_build_b": grow_build, "growth_peak_b": grow_peak, "host_free_drop_gb": host_used, "bytes_per_doc_peak": round(per_doc, 1),
           "projection": {"scale_1m_docs": SCALE_1M_DOCS, "linear_gb": proj_gb,
                          "free_now_gb": after_erase["free_gb"], "free_after_projected_gb": round(after_erase["free_gb"] - proj_gb, 2),
                          "floor_gb": FLOOR_GB, "note": "linear in documents; ignores transient merge and WAL peaks beyond those seen at N"}}
    out["projection"]["fits_floor_linear"] = out["projection"]["free_after_projected_gb"] >= FLOOR_GB
    print("size_probe", json.dumps(out["projection"]), flush=True)
    write_result("size_probe", out)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100000)

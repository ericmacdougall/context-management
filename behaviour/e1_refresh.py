import statistics, threading, uuid, concurrent.futures as cf
from esc import *
IDX = "e1-refresh"
req("DELETE", f"/{IDX}")
req("PUT", f"/{IDX}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {"k": {"type": "keyword"}}}})
out = {"workload": "1 shard, 0 replicas, default refresh_interval (unset), t2-dev docker 12cpu/16g, node busy host load~10"}
def found(i): 
    return req("POST", f"/{IDX}/_search", {"query": {"term": {"k": i}}, "size": 0, "track_total_hits": True})[1]["hits"]["total"]["value"]
# --- (a)(b)(c)
rows = []
for n in range(20):
    i = f"abc-{n}"
    req("PUT", f"/{IDX}/_doc/{i}", {"k": i})
    a = found(i); c, g, _ = req("GET", f"/{IDX}/_doc/{i}"); b = g.get("found")
    i2 = f"w-{n}"
    c2, j2, _ = req("PUT", f"/{IDX}/_doc/{i2}", {"k": i2}, refresh="wait_for")
    rows.append({"a_search_immediate_hits": a, "b_get_found": b, "c_waitfor_search_hits": found(i2), "waitfor_forced_refresh": j2.get("forced_refresh")})
out["abc"] = {"n": len(rows), "a_hit": sum(r["a_search_immediate_hits"] for r in rows), "b_hit": sum(1 for r in rows if r["b_get_found"]),
              "c_hit": sum(r["c_waitfor_search_hits"] for r in rows), "forced": sum(1 for r in rows if r["waitfor_forced_refresh"]), "rows": rows}
# --- delay distribution, 200 writes, shard search-active (we poll constantly)
d = []
for n in range(200):
    i = f"d-{n}"
    req("PUT", f"/{IDX}/_doc/{i}", {"k": i}); t = time.perf_counter()
    while found(i) == 0: pass
    d.append((time.perf_counter() - t) * 1000)
q = statistics.quantiles(d, n=100)
out["delay_active_ms"] = {"n": 200, "min": min(d), "p50": q[49], "p90": q[89], "p99": q[98], "max": max(d), "raw": d}
# --- idle shard: wait >30s (search.idle.after default 30s) then write, poll
idle = []
for n in range(3):
    time.sleep(35)
    i = f"idle-{n}"
    req("PUT", f"/{IDX}/_doc/{i}", {"k": i}); t = time.perf_counter()
    first = found(i); t1 = (time.perf_counter() - t) * 1000
    while found(i) == 0: pass
    idle.append({"first_search_hits": first, "first_search_ms": t1, "visible_after_ms": (time.perf_counter() - t) * 1000})
out["idle_shard_after_35s"] = idle
save("E1_refresh_part1.json", out)
# --- wait_for listener overflow
IDX2 = "e1-listeners"
req("DELETE", f"/{IDX2}")
req("PUT", f"/{IDX2}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "20s"}, "mappings": {"properties": {"k": {"type": "keyword"}}}})
out["max_refresh_listeners_setting"] = req("GET", f"/{IDX2}/_settings", include_defaults="true", filter_path="**.max_refresh_listeners")[1]
import requests
from requests.adapters import HTTPAdapter
ses = requests.Session(); ses.verify = S.verify; ses.headers.update(S.headers); ses.mount("https://", HTTPAdapter(pool_maxsize=1300))
def one(n):
    t = time.perf_counter()
    r = ses.put(f"{URL}/{IDX2}/_doc/L{n}", params={"refresh": "wait_for"}, data=json.dumps({"k": n}), timeout=60)
    j = r.json(); return {"n": n, "status": r.status_code, "forced_refresh": j.get("forced_refresh"), "ms": (time.perf_counter() - t) * 1000, "err": j.get("error", {}).get("type") if r.status_code >= 400 else None}
with cf.ThreadPoolExecutor(1300) as ex: res = list(ex.map(one, range(1100)))
out["listeners_1100_concurrent"] = {"n": len(res), "forced_true": sum(1 for r in res if r["forced_refresh"]), "errors": sum(1 for r in res if r["err"]), "statuses": sorted({r["status"] for r in res}),
   "median_ms_forced": statistics.median([r["ms"] for r in res if r["forced_refresh"]] or [0]), "median_ms_unforced": statistics.median([r["ms"] for r in res if not r["forced_refresh"]] or [0]),
   "first_forced_n": next((r["n"] for r in res if r["forced_refresh"]), None)}
save("E1_refresh.json", out)
print(json.dumps({k: (v if k not in ("abc","delay_active_ms") else {kk: vv for kk, vv in v.items() if kk not in ("rows","raw")}) for k, v in out.items()}, indent=1))

"""DESIGN 3.2.3 (THRESHOLDS.md section V): search visibility after a write at the default refresh. Own node, own code.
Timing is INDICATIVE (shared host; load average recorded).
Added beside the committed design (EXPLORATORY, not a gate): the same 200-write loop with a random 0-1000 ms pause
before each write, to show whether the measured delay depends on the loop's phase relative to the refresh tick.

usage: python refresh_test.py
"""
import json
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import repc  # noqa: E402

ES = repc.rep_es()
IDX, IDX30 = "rep16096-refresh-default", "rep16096-refresh-30s"


def mk(idx, settings):
    ES.req("DELETE", "/" + idx)
    c, j, _ = ES.req("PUT", "/" + idx, {"settings": dict({"number_of_shards": 1, "number_of_replicas": 0}, **settings),
                                       "mappings": {"properties": {"k": {"type": "keyword"}}}})
    assert c == 200, j


def hits(idx, k):
    return ES.req("POST", "/%s/_search" % idx, {"size": 0, "track_total_hits": True, "query": {"term": {"k": k}}})[1]["hits"]["total"]["value"]


def q(v, p):
    s = sorted(v)
    return s[min(len(s) - 1, int(p * len(s)))]


def delays(idx, n, jitter, rng, prefix):
    out = []
    for i in range(n):
        if jitter:
            time.sleep(rng.random())
        k = "%s-%d" % (prefix, i)
        ES.req("PUT", "/%s/_doc/%s" % (idx, k), {"k": k})
        t = time.perf_counter()
        while hits(idx, k) == 0:
            pass
        out.append((time.perf_counter() - t) * 1000)
    return out


def main():
    rng = random.Random(16096)
    la0 = os.getloadavg()
    mk(IDX, {})
    st = ES.req("GET", "/%s/_settings" % IDX, include_defaults="true", filter_path="**.refresh_interval")[1]
    abc = []
    for i in range(20):
        k = "abc-%d" % i
        ES.req("PUT", "/%s/_doc/%s" % (IDX, k), {"k": k})
        a = hits(IDX, k)
        g = ES.req("GET", "/%s/_doc/%s" % (IDX, k))[1].get("found")
        abc.append({"immediate_search_hits": a, "realtime_get_found": g})
    d = delays(IDX, 200, False, rng, "d")
    dj = delays(IDX, 200, True, rng, "j")
    mk(IDX30, {"refresh_interval": "30s"})
    ctrl = []
    for i in range(30):
        k = "c-%d" % i
        ES.req("PUT", "/%s/_doc/%s" % (IDX30, k), {"k": k})
        t = time.perf_counter()
        seen = None
        while time.perf_counter() - t < 3.0:
            if hits(IDX30, k):
                seen = (time.perf_counter() - t) * 1000
                break
            time.sleep(0.02)
        ctrl.append(seen)
    summ = lambda v: {"n": len(v), "min": min(v), "p50": statistics.median(v), "p90": q(v, 0.9), "max": max(v), "mean": statistics.mean(v)}
    res = {"stamp": repc.stamp(__file__, {"writes": 200, "abc": 20, "control_writes": 30, "control_poll_s": 3}),
           "es": repc.es_fingerprint(ES), "refresh_setting_read_back": st, "loadavg_before": la0, "loadavg_after": os.getloadavg(),
           "abc": {"n": 20, "immediate_search_found": sum(1 for r in abc if r["immediate_search_hits"]),
                   "realtime_get_found": sum(1 for r in abc if r["realtime_get_found"]), "rows": abc},
           "sequential_loop_ms": dict(summ(d), raw=d),
           "exploratory_random_phase_loop_ms": dict(summ(dj), raw=dj),
           "control_refresh_30s": {"n": 30, "invisible_at_3s": sum(1 for s in ctrl if s is None), "seen_ms": ctrl},
           "owner": {"p50_ms": 1001, "n": 200, "source": "BEH E1_refresh.json at 6f53c80"}}
    p50 = res["sequential_loop_ms"]["p50"]
    res["threshold_checks"] = {"p50_in_500_1500": 500 <= p50 <= 1500,
                               "realtime_get_20_of_20": res["abc"]["realtime_get_found"] == 20,
                               "immediate_search_le_2_of_20": res["abc"]["immediate_search_found"] <= 2,
                               "control_ge_90pct_invisible_at_3s": res["control_refresh_30s"]["invisible_at_3s"] >= 27}
    ES.req("DELETE", "/" + IDX)
    ES.req("DELETE", "/" + IDX30)
    p = repc.save("refresh_3_2_3.json", res)
    print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk not in ("raw", "rows", "seen_ms")})
                      for k, v in res.items() if k != "stamp"}, indent=1))
    print("saved", p)


if __name__ == "__main__":
    main()

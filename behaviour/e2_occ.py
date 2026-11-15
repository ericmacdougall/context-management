import threading
from esc import *
IDX = "e2-occ"; req("DELETE", f"/{IDX}"); req("PUT", f"/{IDX}", {"settings": {"number_of_replicas": 0}})
out = {"workload": "50 trials x 2 threads, barrier-started, same doc, if_seq_no/if_primary_term"}
trials = []
for n in range(50):
    c, j, _ = req("PUT", f"/{IDX}/_doc/d{n}", {"v": 0}, refresh="true")
    sn, pt = j["_seq_no"], j["_primary_term"]; res = [None, None]; bar = threading.Barrier(2)
    def w(k):
        bar.wait(); c, jj, _ = req("PUT", f"/{IDX}/_doc/d{n}", {"v": k + 1}, if_seq_no=sn, if_primary_term=pt)
        res[k] = (c, jj.get("error", {}).get("type"))
    ts = [threading.Thread(target=w, args=(k,)) for k in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
    final = req("GET", f"/{IDX}/_doc/d{n}")[1]["_source"]["v"]
    trials.append({"res": res, "final_v": final})
out["trials_summary"] = {"exactly_one_win": sum(1 for t in trials if sorted(r[0] for r in t["res"]) in ([200, 409],)),
    "both_win": sum(1 for t in trials if all(r[0] == 200 for r in t["res"])), "none_win": sum(1 for t in trials if all(r[0] == 409 for r in t["res"])),
    "loser_error_types": sorted({r[1] for t in trials for r in t["res"] if r[1]}), "final_matches_winner": all(t["final_v"] == [k + 1 for k in range(2) if t["res"][k][0] == 200][0] for t in trials if any(r[0] == 200 for r in t["res"]))}
out["trials"] = trials
# bulk atomicity
req("PUT", f"/{IDX}/_doc/exists", {"v": 1})
nd = "\n".join(json.dumps(x) for x in [{"create": {"_index": IDX, "_id": "b1"}}, {"v": 1}, {"create": {"_index": IDX, "_id": "exists"}}, {"v": 2},
      {"index": {"_index": IDX, "_id": "b3"}}, {"v": 3}, {"update": {"_index": IDX, "_id": "b4", "if_seq_no": 0, "if_primary_term": 1}}, {"doc": {"v": 4}}, {"index": {"_index": IDX, "_id": "b5"}}, {"v": 5}]) + "\n"
c, j, _ = req("POST", "/_bulk", ndjson=nd, refresh="true")
out["bulk"] = {"http": c, "errors_flag": j["errors"], "items": [{k: {"status": v["status"], "err": v.get("error", {}).get("type")}} for it in j["items"] for k, v in it.items()]}
out["bulk"]["applied"] = {i: req("GET", f"/{IDX}/_doc/{i}")[1].get("found") for i in ["b1", "b3", "b5", "b4"]}
save("E2_occ.json", out); print(json.dumps({k: v for k, v in out.items() if k != "trials"}, indent=1))

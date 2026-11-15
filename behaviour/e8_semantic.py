import sys
from esc import *
phase = sys.argv[1]
MODEL = ".multilingual-e5-small"; EP = "emlab-e5"
def cause(j):
    e = j.get("error", {}); return {"type": e.get("type"), "reason": e.get("reason"), "root": (e.get("root_cause") or [{}])[0].get("reason"), "caused_by": (e.get("caused_by") or {}).get("reason")} if isinstance(e, dict) else j
def counts():
    c, j, _ = req("GET", f"/_ml/trained_models/{MODEL}/_stats")
    if c != 200: return {"err": cause(j)}
    t = j["trained_model_stats"][0]
    dn = (t.get("deployment_stats") or {}).get("nodes") or []
    return {"inference_stats_count": (t.get("inference_stats") or {}).get("inference_count"), "deployment_inference_count": sum(n.get("inference_count", 0) for n in dn), "deployment_number_of_pending": sum(n.get("number_of_pending_requests", 0) for n in dn)}
if phase == "deploy":
    out = {}
    out["license"] = req("GET", "/_license")[1]["license"]
    out["preconfigured_endpoints"] = [e.get("inference_id") for e in req("GET", "/_inference")[1].get("endpoints", [])]
    t = time.time()
    c, j, ms = req("PUT", f"/_inference/text_embedding/{EP}", {"service": "elasticsearch", "service_settings": {"model_id": MODEL, "num_threads": 1, "num_allocations": 1}}, timeout="600s")
    out["put_endpoint"] = {"status": c, "seconds": time.time() - t, "body": j if c != 200 else {k: v for k, v in j.items() if k != "service_settings"}}
    out["model_stats"] = req("GET", f"/_ml/trained_models/{MODEL}/_stats")[1]
    out["model_config_size"] = req("GET", f"/_ml/trained_models/{MODEL}", include="definition_status")[1]
    out["ml_memory"] = req("GET", "/_ml/memory/_stats")[1]
    save("E8_deploy.json", out); print(json.dumps(out, indent=1)[:3000])
elif phase == "calls":
    IDX = "e8-sem"; req("DELETE", f"/{IDX}"); req("DELETE", f"/{IDX}-copy")
    mp = {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"title": {"type": "text"}, "tag": {"type": "keyword"}, "body": {"type": "semantic_text", "inference_id": EP}}}}
    out = {"create": req("PUT", f"/{IDX}", mp)[1]}
    txt = "The customer moved to Berlin in March and asked to cancel the Paris dentist appointment."
    ops = []
    def step(name, fn):
        b = counts(); c, j = fn(); time.sleep(1); a = counts()
        ops.append({"op": name, "status": c, "result": j, "before": b, "after": a, "delta_inference_stats": (a.get("inference_stats_count") or 0) - (b.get("inference_stats_count") or 0), "delta_deployment": a.get("deployment_inference_count", 0) - b.get("deployment_inference_count", 0)}); print(ops[-1]["op"], ops[-1]["status"], ops[-1]["delta_inference_stats"], ops[-1]["delta_deployment"], flush=True)
    def R(m, p, body=None, **kw):
        c, j, _ = req(m, p, body, **kw); return c, (j.get("result") or cause(j))
    def RB(nd):
        c, j, _ = req("POST", "/_bulk", ndjson=nd, refresh="true"); it = j["items"][0]; k = list(it)[0]; return c, {"item_status": it[k]["status"], "result": it[k].get("result"), "error": it[k].get("error")}
    step("0 calibration: index API full doc d1", lambda: R("PUT", f"/{IDX}/_doc/d1", {"title": "t", "tag": "t0", "body": txt}, refresh="true"))
    step("1 calibration: bulk index full doc d2", lambda: RB(json.dumps({"index": {"_index": IDX, "_id": "d2"}}) + "\n" + json.dumps({"title": "t", "tag": "t0", "body": txt + " second"}) + "\n"))
    step("2 bulk partial update omitting semantic_text (d1.tag)", lambda: RB(json.dumps({"update": {"_index": IDX, "_id": "d1"}}) + "\n" + json.dumps({"doc": {"tag": "t1"}}) + "\n"))
    step("3 Update API doc partial omitting semantic_text (d1.tag)", lambda: R("POST", f"/{IDX}/_update/d1", {"doc": {"tag": "t2"}}, refresh="true"))
    step("4 scripted Update API on other field (d1.tag)", lambda: R("POST", f"/{IDX}/_update/d1", {"script": {"source": "ctx._source.tag = params.t", "params": {"t": "t3"}}}, refresh="true"))
    step("5 scripted update inside _bulk (d1.tag)", lambda: RB(json.dumps({"update": {"_index": IDX, "_id": "d1"}}) + "\n" + json.dumps({"script": {"source": "ctx._source.tag = params.t", "params": {"t": "t4"}}}) + "\n"))
    step("6 full re-index d1 via index API, identical body", lambda: R("PUT", f"/{IDX}/_doc/d1", {"title": "t", "tag": "t5", "body": txt}, refresh="true"))
    step("7 Update API doc partial that RESENDS semantic_text unchanged", lambda: R("POST", f"/{IDX}/_update/d1", {"doc": {"tag": "t6", "body": txt}}, refresh="true"))
    req("PUT", f"/{IDX}-copy", mp)
    step("8 _reindex e8-sem -> e8-sem-copy (2 docs, same inference_id)", lambda: (lambda c, j, _: (c, {k: j.get(k) for k in ("total", "created", "failures")}))(*req("POST", "/_reindex", {"source": {"index": IDX}, "dest": {"index": IDX + "-copy"}}, refresh="true")))
    step("9 update_by_query touching tag only", lambda: (lambda c, j, _: (c, {k: j.get(k) for k in ("total", "updated", "failures")} if c == 200 else cause(j)))(*req("POST", f"/{IDX}/_update_by_query", {"script": {"source": "ctx._source.tag = 'ubq'"}}, refresh="true", conflicts="proceed")))
    out["ops"] = ops
    c, j, _ = req("POST", f"/{IDX}/_search", {"query": {"semantic": {"field": "body", "query": "dentist in the french capital"}}, "size": 2, "_source": ["tag"]}); out["semantic_query"] = (c, [(h["_id"], h["_score"]) for h in j["hits"]["hits"]] if c == 200 else cause(j))
    c, j, _ = req("POST", f"/{IDX}/_search", {"query": {"match": {"body": "dentist in the french capital"}}, "size": 2, "_source": ["tag"]}); out["match_on_semantic_text"] = (c, [(h["_id"], h["_score"]) for h in j["hits"]["hits"]] if c == 200 else cause(j))
    save("E8_calls.json", out)

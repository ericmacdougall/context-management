"""Licence feature matrix (THRESHOLDS.md section L). No inference is executed: every inference-dependent probe names a
nonexistent model or endpoint, so a licence refusal and a not-found error can be told apart without running a model.

usage: python licence_matrix.py <phase> where phase in {basic, trial_rep, trial_shared}
  basic        own node emlab-es-rep-16096 on its default Basic licence (asserted before every cell)
  trial_rep    own node after POST _license/start_trial (asserted)
  trial_shared shared emlab-es-16096 (trial); only cells whose writes stay under rep16096- (guarded client)
"""
import json
import os
import random
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import repc  # noqa: E402

PHASE = sys.argv[1]
ES = repc.shared_es() if PHASE == "trial_shared" else repc.rep_es()
WANT = {"basic": "basic", "trial_rep": "trial", "trial_shared": "trial"}[PHASE]
P = repc.PREFIX + "lic-"
NOMODEL = "rep16096-no-such-model"
NOEP = "rep16096-no-such-endpoint"


def lic():
    return ES.req("GET", "/_license")[1]["license"]


def classify(code, j, witness=None):
    """-> cell dict. AVAILABLE needs a non-empty witness supplied by the caller."""
    if 200 <= code < 300 and not (isinstance(j, dict) and j.get("errors")):
        ok = bool(witness)
        return {"class": "AVAILABLE" if ok else "OTHER_ERROR", "status": code, "witness": witness,
                **({} if ok else {"note": "2xx but empty witness"})}
    e = (j or {}).get("error", {}) if isinstance(j, dict) else {}
    if isinstance(e, str):
        e = {"reason": e}
    reason = e.get("reason") or json.dumps(j)[:400]
    root = (e.get("root_cause") or [{}])[0].get("reason") if isinstance(e.get("root_cause"), list) else None
    cb = (e.get("caused_by") or {}).get("reason") if isinstance(e.get("caused_by"), dict) else None
    text = " ".join(str(x) for x in (reason, root, cb) if x)
    cls = "LICENCE_ERROR" if ("non-compliant" in text or "license" in text.lower() and "current license" in text.lower()) else "OTHER_ERROR"
    return {"class": cls, "status": code, "type": e.get("type"), "reason": reason, "root": root, "caused_by": cb}


def hits(j):
    return [h["_id"] for h in j.get("hits", {}).get("hits", [])] if isinstance(j, dict) else []


def S(idx, body):
    c, j, _ = ES.req("POST", "/%s/_search" % idx, body)
    return classify(c, j, hits(j) if c == 200 else None)


def drop(path):
    ES.req("DELETE", path)


def rvec(d, rng):
    v = [rng.gauss(0, 1) for _ in range(d)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def main():
    L0 = lic()
    assert L0["type"] == WANT, "licence is %s, phase wants %s" % (L0["type"], WANT)
    rng = random.Random(16096)
    T = {}
    base = P + "base"
    drop("/" + base)
    c, j, _ = ES.req("PUT", "/" + base, {"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                                         "mappings": {"properties": {"title": {"type": "text"}, "kw": {"type": "keyword"},
                                                                     "v": {"type": "dense_vector", "dims": 4, "index": True,
                                                                           "similarity": "cosine", "index_options": {"type": "hnsw"}}}}})
    T["_setup_base"] = {"status": c}
    docs = [("paris trip planning", "a", [1, 0, 0, 0]), ("berlin move notes", "b", [0, 1, 0, 0]),
            ("dentist appointment paris", "a", [0, 0, 1, 0]), ("refund policy", "b", [1, 1, 0, 0]),
            ("moved to berlin", "a", [0, 1, 1, 0]), ("paris berlin train", "b", [0, 0, 0, 1])]
    nd = "".join(json.dumps({"index": {"_id": str(i)}}) + "\n" + json.dumps({"title": t, "kw": k, "v": v}) + "\n"
                 for i, (t, k, v) in enumerate(docs))
    c, j, _ = ES.req("POST", "/%s/_bulk" % base, ndjson=nd, refresh="true")
    T["_setup_base_bulk"] = {"status": c, "errors": j.get("errors")}
    std = {"standard": {"query": {"match": {"title": "paris"}}}}
    knn = {"knn": {"field": "v", "query_vector": [1, 0, 0, 0], "k": 3, "num_candidates": 10}}
    T["bm25_match"] = S(base, {"query": {"match": {"title": "paris"}}})
    T["retriever_standard"] = S(base, {"retriever": std})
    T["knn_top_level_float"] = S(base, {"knn": knn["knn"]})
    T["retriever_knn"] = S(base, {"retriever": knn})
    T["retriever_rescorer"] = S(base, {"retriever": {"rescorer": {"retriever": std, "rescore": {"window_size": 5, "query": {
        "rescore_query": {"match": {"title": "dentist"}}}}}}})
    T["retriever_pinned"] = S(base, {"retriever": {"pinned": {"ids": ["3"], "retriever": std}}})
    T["retriever_diversify_mmr"] = S(base, {"retriever": {"diversify": {"type": "mmr", "field": "v", "query_vector": [1, 0, 0, 0],
                                                                        "lambda": 0.5, "retriever": knn}}})
    # client-side fusion: two legs fetched separately, fused here (RRF 60)
    c1, j1, _ = ES.req("POST", "/%s/_search" % base, {"query": {"match": {"title": "paris"}}, "size": 5})
    c2, j2, _ = ES.req("POST", "/%s/_search" % base, {"knn": knn["knn"], "size": 5})
    fused = {}
    for lst in (hits(j1), hits(j2)):
        for r, d in enumerate(lst):
            fused[d] = fused.get(d, 0) + 1.0 / (61 + r)
    T["client_side_fusion"] = classify(200 if c1 == 200 and c2 == 200 else max(c1, c2), j1 if c1 != 200 else j2,
                                       sorted(fused, key=lambda d: -fused[d]))
    T["retriever_rrf"] = S(base, {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window_size": 10, "rank_constant": 60}}})
    T["retriever_linear"] = S(base, {"retriever": {"linear": {"retrievers": [
        {"retriever": std, "weight": 1.0, "normalizer": "minmax"}, {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}],
        "rank_window_size": 10}}})
    T["rank_rrf_legacy"] = S(base, {"query": std["standard"]["query"], "knn": knn["knn"], "rank": {"rrf": {}}})
    T["text_similarity_reranker_nonexistent_endpoint"] = S(base, {"retriever": {"text_similarity_reranker": {
        "retriever": std, "field": "title", "inference_id": NOEP, "inference_text": "paris", "rank_window_size": 5}}})

    # vectors: bbq_hnsw, int8_hnsw, bbq_disk at 64 dims, plus rescore_vector on bbq
    for typ in ("bbq_hnsw", "int8_hnsw", "bbq_disk"):
        idx = P + "vec-" + typ.replace("_", "-")
        if PHASE == "trial_shared" and typ != "bbq_disk":
            continue  # Basic availability is decided on the own node; trial_shared only re-checks bbq_disk
        drop("/" + idx)
        cm, jm, _ = ES.req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {
            "properties": {"v": {"type": "dense_vector", "dims": 64, "index": True, "similarity": "cosine",
                                 "index_options": {"type": typ}}}}})
        vecs = [rvec(64, rng) for _ in range(40)]
        nd = "".join(json.dumps({"index": {"_id": str(i)}}) + "\n" + json.dumps({"v": v}) + "\n" for i, v in enumerate(vecs))
        cb, jb, _ = ES.req("POST", "/%s/_bulk" % idx, ndjson=nd, refresh="true")
        item_err = [it["index"].get("error") for it in jb.get("items", []) if it["index"].get("error")]
        cell = {"mapping_status": cm, "mapping_ok": cm == 200, "bulk_status": cb, "bulk_errors_flag": jb.get("errors"),
                "items": len(jb.get("items", [])), "items_failed": len(item_err),
                "first_item_error": item_err[0] if item_err else None}
        q = {"knn": {"field": "v", "query_vector": vecs[7], "k": 5, "num_candidates": 20}, "size": 5}
        cs, js, _ = ES.req("POST", "/%s/_search" % idx, q)
        cell["search_status"], cell["search_hits"] = cs, hits(js)
        cell["self_found_top1"] = bool(hits(js)) and hits(js)[0] == "7"
        if item_err:
            sub = classify(item_err[0].get("status", 400) if isinstance(item_err[0], dict) else 400, {"error": item_err[0]})
            cell["class"], cell["reason"] = sub["class"], sub.get("reason")
        elif cm == 200 and cell["self_found_top1"]:
            cell["class"] = "AVAILABLE"
        else:
            cell["class"] = "OTHER_ERROR"
        if typ == "bbq_hnsw" and cell["class"] == "AVAILABLE":
            q2 = dict(q)
            q2["knn"] = dict(q["knn"], rescore_vector={"oversample": 3})
            cr, jr, _ = ES.req("POST", "/%s/_search" % idx, q2)
            T["rescore_vector_oversample3_on_bbq_hnsw"] = classify(cr, jr, hits(jr) if cr == 200 else None)
        T["vector_" + typ] = cell
        drop("/" + idx)

    # inference API: list, and create with a model id that does not exist (no model runs either way)
    c, j, _ = ES.req("GET", "/_inference")
    eps = [e.get("inference_id") for e in j.get("endpoints", [])] if c == 200 else None
    T["inference_get_list"] = classify(c, j, eps)
    if PHASE != "trial_shared":
        c, j, _ = ES.req("PUT", "/_inference/text_embedding/" + NOEP, {"service": "elasticsearch", "service_settings": {
            "model_id": NOMODEL, "num_allocations": 1, "num_threads": 1}})
        T["inference_put_nonexistent_model"] = classify(c, j, [NOEP] if c == 200 else None)
        if c == 200:
            drop("/_inference/text_embedding/" + NOEP)
    # semantic_text: mapping and one document, endpoint id that does not exist
    idx = P + "semtext"
    drop("/" + idx)
    cm, jm, _ = ES.req("PUT", "/" + idx, {"mappings": {"properties": {"t": {"type": "semantic_text", "inference_id": NOEP}}}})
    cell = {"mapping": classify(cm, jm, [idx] if cm == 200 else None)}
    if cm == 200:
        cd, jd, _ = ES.req("PUT", "/%s/_doc/1" % idx, {"t": "paris trip"}, refresh="true")
        cell["index_doc"] = classify(cd, jd, None)
    cell["class"] = ("LICENCE_ERROR" if "LICENCE_ERROR" in (cell["mapping"]["class"], cell.get("index_doc", {}).get("class"))
                     else "OTHER_ERROR (licence passed; endpoint not found)" if cm == 200 or "not" in str(jm).lower() else "OTHER_ERROR")
    T["semantic_text_nonexistent_endpoint"] = cell
    drop("/" + idx)

    # ES|QL
    def Q(q):
        c, j, _ = ES.req("POST", "/_query", {"query": q})
        return classify(c, j, j.get("values") if c == 200 else None)
    T["esql_match"] = Q('FROM %s METADATA _score | WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3 | KEEP title, _score' % base)
    fork = ('FROM %s METADATA _id, _index, _score | FORK (WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3) '
            '(WHERE MATCH(title, "berlin") | SORT _score DESC | LIMIT 3)' % base)
    T["esql_fork"] = Q(fork + " | KEEP _id, _fork, _score")
    T["esql_fork_fuse_rrf"] = Q(fork + " | FUSE | KEEP _id, _score")
    T["esql_fuse_linear"] = Q(fork + " | FUSE LINEAR | KEEP _id, _score")
    T["esql_rerank_nonexistent_endpoint"] = Q('FROM %s METADATA _score | WHERE MATCH(title, "paris") | LIMIT 3 | RERANK "paris" ON title '
                                              'WITH {"inference_id": "%s"} | KEEP title, _score' % (base, NOEP))
    if PHASE == "basic":
        # Preconfigured endpoints, Basic only: the nonexistent-id probe is answered "not found" before the licence
        # check, so it cannot discriminate. On Basic the licence refuses before any model is deployed or run.
        T["esql_rerank_preconfigured_endpoint_basic_only"] = Q(
            'FROM %s METADATA _score | WHERE MATCH(title, "paris") | LIMIT 3 | RERANK "paris" ON title '
            'WITH {"inference_id": ".rerank-v1-elasticsearch"} | KEEP title, _score' % base)
        si = P + "semtext-e5"
        drop("/" + si)
        cm, jm, _ = ES.req("PUT", "/" + si, {"mappings": {"properties": {"t": {"type": "semantic_text",
                                                                                "inference_id": ".multilingual-e5-small-elasticsearch"}}}})
        cell = {"mapping": classify(cm, jm, [si] if cm == 200 else None)}
        if cm == 200:
            cd, jd, _ = ES.req("PUT", "/%s/_doc/1" % si, {"t": "paris trip"}, refresh="true")
            cell["index_doc"] = classify(cd, jd, ["1"] if cd in (200, 201) else None)
        cell["class"] = cell.get("index_doc", cell["mapping"])["class"]
        T["semantic_text_preconfigured_e5_basic_only"] = cell
        drop("/" + si)
    lk, kw = P + "lookup", P + "kw"
    drop("/" + lk), drop("/" + kw)
    ES.req("PUT", "/" + lk, {"settings": {"index.mode": "lookup"}, "mappings": {"properties": {"title": {"type": "keyword"}, "tag": {"type": "keyword"}}}})
    ES.req("PUT", "/%s/_doc/1" % lk, {"title": "refund policy", "tag": "billing"}, refresh="true")
    ES.req("PUT", "/" + kw, {"mappings": {"properties": {"title": {"type": "keyword"}}}})
    ES.req("PUT", "/%s/_doc/1" % kw, {"title": "refund policy"}, refresh="true")
    T["esql_lookup_join"] = Q("FROM %s | LOOKUP JOIN %s ON title | KEEP title, tag" % (kw, lk))
    drop("/" + lk), drop("/" + kw)

    # percolator (3.7.9): stored term query matches (control); stored knn query is rejected
    idx = P + "perc"
    drop("/" + idx)
    ES.req("PUT", "/" + idx, {"mappings": {"properties": {"q": {"type": "percolator"}, "title": {"type": "text"},
                                                          "v": {"type": "dense_vector", "dims": 4, "index": True, "similarity": "cosine"}}}})
    ct, jt, _ = ES.req("PUT", "/%s/_doc/term1" % idx, {"q": {"match": {"title": "paris"}}}, refresh="true")
    cp, jp, _ = ES.req("POST", "/%s/_search" % idx, {"query": {"percolate": {"field": "q", "document": {"title": "trip to paris"}}}})
    T["percolator_lexical"] = classify(cp if ct in (200, 201) else ct, jp if ct in (200, 201) else jt, hits(jp) if cp == 200 else None)
    ck, jk, _ = ES.req("PUT", "/%s/_doc/knn1" % idx, {"q": {"knn": {"field": "v", "query_vector": [1, 0, 0, 0], "num_candidates": 5}}}, refresh="true")
    T["percolator_knn_stored"] = {"status": ck, "accepted": ck in (200, 201), **({} if ck in (200, 201) else classify(ck, jk))}
    drop("/" + idx)

    # document-level security (own node only: needs a role and a user)
    if PHASE != "trial_shared":
        role, user = P + "dls-role", P + "dls-user"
        pw = secrets.token_hex(16)  # in memory only
        cr, jr, _ = ES.req("PUT", "/_security/role/" + role, {"indices": [{"names": [base], "privileges": ["read"],
                                                                         "query": {"term": {"kw": "a"}}}]})
        cu, ju, _ = ES.req("PUT", "/_security/user/" + user, {"password": pw, "roles": [role]})
        import requests
        s = requests.Session()
        s.auth = (user, pw)
        r = s.post(ES.url + "/%s/_search" % base, json={"query": {"match_all": {}}, "size": 10}, timeout=60)
        jj = r.json()
        cell = classify(r.status_code, jj, hits(jj) if r.status_code == 200 else None)
        cell["role_status"], cell["user_status"] = cr, cu
        if cr != 200:
            cell["role_create"] = classify(cr, jr)
            cell["class"] = cell["role_create"]["class"]
        # second path (as the owner's E9): an API key whose role descriptor carries the DLS query
        ck, jk, _ = ES.req("POST", "/_security/api_key", {"name": P + "dls-key", "expiration": "1h", "role_descriptors": {
            "dls": {"indices": [{"names": [base], "privileges": ["read"], "query": {"term": {"kw": "a"}}}]}}})
        if ck == 200:
            s2 = requests.Session()
            s2.headers["Authorization"] = "ApiKey " + jk["encoded"]
            r2 = s2.post(ES.url + "/%s/_search" % base, json={"query": {"match_all": {}}, "size": 10}, timeout=60)
            j2 = r2.json()
            cell["api_key_path"] = classify(r2.status_code, j2, hits(j2) if r2.status_code == 200 else None)
            if r2.status_code == 200:
                cell["api_key_path"]["only_kw_a_returned"] = sorted(hits(j2)) == ["0", "2", "4"]
            ES.req("DELETE", "/_security/api_key", {"ids": [jk["id"]]})
            del s2
        else:
            cell["api_key_path"] = {"create": classify(ck, jk)}
        if r.status_code == 200:
            cell["only_kw_a_returned"] = sorted(hits(jj)) == ["0", "2", "4"]
            if not cell["only_kw_a_returned"]:
                cell["class"] = "OTHER_ERROR"
                cell["note"] = "200 but the DLS filter was not applied"
        T["document_level_security"] = cell
        ES.req("DELETE", "/_security/user/" + user)
        ES.req("DELETE", "/_security/role/" + role)
        del pw

    # data stream
    tpl, ds = P + "ds-tpl", P + "ds-a"
    ES.req("DELETE", "/_data_stream/" + ds)
    ES.req("DELETE", "/_index_template/" + tpl)
    ct, jt, _ = ES.req("PUT", "/_index_template/" + tpl, {"index_patterns": [P + "ds-*"], "data_stream": {}, "priority": 500,
                                                          "template": {"settings": {"number_of_replicas": 0}}})
    cd, jd, _ = ES.req("POST", "/%s/_doc" % ds, {"@timestamp": "2026-11-16T00:00:00Z", "m": "x"}, refresh="true")
    cg, jg, _ = ES.req("GET", "/_data_stream/" + ds)
    cs, js, _ = ES.req("POST", "/%s/_search" % ds, {"query": {"match_all": {}}})
    cell = classify(cs if cd in (200, 201) else cd, js if cd in (200, 201) else jd, hits(js) if cs == 200 else None)
    cell.update({"template_status": ct, "create_status": cd, "get_data_stream_status": cg,
                 "backing_indices": [i["index_name"] for i in jg.get("data_streams", [{}])[0].get("indices", [])] if cg == 200 else None})
    T["data_stream"] = cell
    ES.req("DELETE", "/_data_stream/" + ds)
    ES.req("DELETE", "/_index_template/" + tpl)

    # aliases: require_alias and must_exist (own node only: /_aliases is not under the prefix)
    if PHASE == "trial_shared":
        return finish(L0, T, base)
    ai, al = P + "al-1", P + "al"
    drop("/" + ai)
    ES.req("PUT", "/" + ai, {"settings": {"number_of_replicas": 0}})
    ca, ja, _ = ES.req("POST", "/_aliases", {"actions": [{"add": {"index": ai, "alias": al, "is_write_index": True}}]})
    nd = json.dumps({"index": {}}) + "\n" + json.dumps({"m": 1}) + "\n"
    cb, jb, _ = ES.req("POST", "/%s/_bulk" % al, ndjson=nd, require_alias="true", refresh="true")
    cb2, jb2, _ = ES.req("POST", "/%s/_bulk" % (P + "al-missing"), ndjson=nd, require_alias="true")
    miss_err = (jb2.get("items") or [{}])[0].get("index", {}).get("error") if cb2 == 200 else jb2.get("error")
    cm1, jm1, _ = ES.req("POST", "/_aliases", {"actions": [{"remove": {"index": ai, "alias": P + "al-nope", "must_exist": True}}]})
    cm2, jm2, _ = ES.req("POST", "/_aliases", {"actions": [{"remove": {"index": ai, "alias": al, "must_exist": True}}]})
    cc, jc, _ = ES.req("GET", "/%s/_count" % ai)
    ok = (ca == 200 and cb == 200 and not jb.get("errors") and miss_err is not None and cm1 >= 400 and cm2 == 200
          and jc.get("count") == 1)
    T["aliases_require_alias_must_exist"] = {"class": "AVAILABLE" if ok else "OTHER_ERROR", "add_status": ca,
                                             "bulk_via_alias_status": cb, "bulk_via_alias_errors": jb.get("errors"),
                                             "bulk_to_missing_alias_error": miss_err, "remove_missing_must_exist_status": cm1,
                                             "remove_missing_must_exist_error": (jm1.get("error") or {}).get("type") if isinstance(jm1, dict) else None,
                                             "remove_existing_must_exist_status": cm2, "doc_count_witness": jc.get("count")}
    licence_err = [c for c in (ca, cb, cm2) if c == 403]
    if licence_err:
        T["aliases_require_alias_must_exist"]["class"] = "LICENCE_ERROR"
    drop("/" + ai)

    # force merge to one segment
    fi = P + "fm"
    drop("/" + fi)
    ES.req("PUT", "/" + fi, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
    for b in range(3):
        nd = "".join(json.dumps({"index": {}}) + "\n" + json.dumps({"m": b * 10 + i}) + "\n" for i in range(10))
        ES.req("POST", "/%s/_bulk" % fi, ndjson=nd, refresh="true")
    def segs():
        c, j, _ = ES.req("GET", "/%s/_segments" % fi)
        sh = j["indices"][fi]["shards"]["0"][0]["segments"]
        return len(sh)
    before = segs()
    cf, jf, _ = ES.req("POST", "/%s/_forcemerge" % fi, max_num_segments=1)
    after = segs()
    T["force_merge"] = classify(cf, jf, {"segments_before": before, "segments_after": after} if after == 1 and before > 1 else None)
    drop("/" + fi)
    return finish(L0, T, base)


def finish(L0, T, base):
    drop("/" + base)
    L1 = lic()
    out = {"stamp": repc.stamp(__file__, {"phase": PHASE}), "es": repc.es_fingerprint(ES),
           "licence_before": {k: L0.get(k) for k in ("type", "uid", "status", "expiry_date")},
           "licence_after": {k: L1.get(k) for k in ("type", "uid", "status", "expiry_date")},
           "cells": T, "summary": {k: v.get("class") for k, v in T.items() if not k.startswith("_")}}
    p = repc.save("licence_matrix_%s.json" % PHASE, out)
    print(json.dumps(out["summary"], indent=1))
    print("saved", p)


if __name__ == "__main__":
    main()

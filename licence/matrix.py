"""Experiment 1: feature matrix. run(label) probes every feature on the node's CURRENT licence; quick_probes() is the fast subset
polled around start_basic / start_trial. No model runs anywhere: embeddings come from the loopback stub (licence/stub_embed.py)."""
import json
import os
import secrets
import time

from lib import bulk, cell, classify, embed, err, req

STUB_URL = os.environ.get("LIC_STUB_URL", "http://172.17.0.1:19271/embed")
STUB_EP = "lic-stub"
NOEP = "lic-no-such-endpoint"
DLS_USER = "lic_dls_user"
DLS_ROLE = "lic_dls_role"
DLS_PW = os.path.expanduser("~/emlab-16096/lic/dls_password")
BASE_IDX = "lic1-base"
DOCS = [("paris trip planning", "a"), ("berlin move notes", "b"), ("dentist appointment paris", "a"), ("refund policy", "b"),
        ("moved to berlin", "a"), ("paris berlin train", "b"), ("tax form paris office", "a"), ("book club berlin", "b")]


def drop(idx):
    req("DELETE", "/" + idx)


def hits(j):
    return [h["_id"] for h in j.get("hits", {}).get("hits", [])]


def S(idx, body, **kw):
    c, j, _ = req("POST", "/%s/_search" % idx, body, **kw)
    return cell(c, j, hits(j) if c == 200 else None), c, j


def stub_endpoint_body():
    return {"service": "custom", "service_settings": {"url": STUB_URL, "headers": {"Content-Type": "application/json"},
            "request": "{\"input\": ${input}}", "response": {"json_parser": {"text_embeddings": "$.data[*].embedding[*]"}},
            "dimensions": 8, "similarity": "cosine"}}


def make_base(idx=BASE_IDX):
    drop(idx)
    req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
        "title": {"type": "text"}, "kw": {"type": "keyword"}, "tenant": {"type": "keyword"},
        "v": {"type": "dense_vector", "dims": 8, "index": True, "similarity": "cosine"}}}})
    bulk(idx, [(str(i), {"title": t, "kw": k, "tenant": "t", "v": embed(t)}) for i, (t, k) in enumerate(DOCS)])


def ensure_dls():
    """role + user (created while the licence allows it; kept across the transition). Password in a 0600 file, never printed."""
    if not os.path.exists(DLS_PW):
        old = os.umask(0o077)
        try:
            with open(DLS_PW, "w") as fh:
                fh.write(secrets.token_hex(16))
        finally:
            os.umask(old)
    pw = open(DLS_PW).read().strip()
    cr, jr, _ = req("PUT", "/_security/role/" + DLS_ROLE, {"indices": [{"names": [BASE_IDX], "privileges": ["read"], "query": {"term": {"kw": "a"}}}]})
    cu, ju, _ = req("PUT", "/_security/user/" + DLS_USER, {"password": pw, "roles": [DLS_ROLE]})
    return {"role": cell(cr, jr), "user": cell(cu, ju)}


def dls_search():
    c, j, _ = req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}, "size": 20}, user=DLS_USER, pwfile=DLS_PW)
    ids = sorted(hits(j)) if c == 200 else None
    r = cell(c, j, ids)
    if c == 200:
        r["only_kw_a"] = ids == ["0", "2", "4", "6"]
        r["returned"] = len(ids)
    return r


def quick_probes(sem_idx="lic-sem"):
    """Fast subset. Keys are stable; values are class strings plus status."""
    out = {}
    std = {"standard": {"query": {"match": {"title": "paris"}}}}
    knn = {"knn": {"field": "v", "query_vector": embed("paris trip planning"), "k": 3, "num_candidates": 10}}
    t = time.time()
    for name, body in (("rrf", {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window_size": 10}}}),
                       ("linear", {"retriever": {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"},
                                                                          {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}], "rank_window_size": 10}}}),
                       ("bm25", {"query": {"match": {"title": "paris"}}}),
                       ("knn", {"knn": knn["knn"]})):
        c, j, _ = req("POST", "/%s/_search" % BASE_IDX, body, timeout=15)
        out[name] = [classify(c, j), c]
    c, j, _ = req("POST", "/%s/_search" % sem_idx, {"query": {"semantic": {"field": "body", "query": "paris"}}}, timeout=15)
    out["semantic_query"] = [classify(c, j), c]
    c, j, _ = req("POST", "/_inference/text_embedding/%s" % STUB_EP, {"input": "x"}, timeout=15)
    out["inference_use"] = [classify(c, j), c]
    d = dls_search() if os.path.exists(DLS_PW) else {"class": "SKIP", "status": None}
    out["dls_search"] = [d["class"], d["status"], d.get("returned")]
    out["_t"] = t
    return out


def run(label):
    """Full matrix on the current licence. Index names carry the label so phases never share state."""
    P = "lic1-%s-" % label
    T = {}
    base = P + "base"
    drop(base)
    req("PUT", "/" + base, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
        "title": {"type": "text"}, "kw": {"type": "keyword"}, "v": {"type": "dense_vector", "dims": 8, "index": True, "similarity": "cosine"}}}})
    bulk(base, [(str(i), {"title": t, "kw": k, "v": embed(t)}) for i, (t, k) in enumerate(DOCS)])
    qv = embed("paris trip planning")
    std = {"standard": {"query": {"match": {"title": "paris"}}}}
    knn = {"knn": {"field": "v", "query_vector": qv, "k": 3, "num_candidates": 10}}
    T["bm25_match"] = S(base, {"query": {"match": {"title": "paris"}}})[0]
    T["dense_knn"] = S(base, {"knn": knn["knn"]})[0]
    # client-side fusion
    lex, c1, j1 = S(base, {"query": {"match": {"title": "paris"}}, "size": 5})
    vec, c2, j2 = S(base, {"knn": knn["knn"], "size": 5})
    f = {}
    for lst in (hits(j1), hits(j2)):
        for r, d in enumerate(lst):
            f[d] = f.get(d, 0) + 1.0 / (61 + r)
    T["client_side_fusion"] = {"class": "AVAILABLE" if c1 == 200 and c2 == 200 else "OTHER_ERROR", "status": [c1, c2], "witness": sorted(f, key=lambda d: -f[d])}
    T["retriever_rrf"] = S(base, {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window_size": 10, "rank_constant": 60}}})[0]
    T["retriever_linear"] = S(base, {"retriever": {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"},
                                                                             {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}], "rank_window_size": 10}}})[0]
    T["text_similarity_reranker_gate"] = S(base, {"retriever": {"text_similarity_reranker": {"retriever": std, "field": "title", "inference_id": NOEP,
                                                                                            "inference_text": "paris", "rank_window_size": 5}}})[0]
    # vector types
    for typ in ("int8_hnsw", "bbq_hnsw", "bbq_disk"):
        idx = P + typ
        drop(idx)
        cm, jm, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
            "v": {"type": "dense_vector", "dims": 64, "index": True, "similarity": "cosine", "index_options": {"type": typ}}}}})
        cb, sb = bulk(idx, [(str(i), {"v": embed("d%d" % i, 64)}) for i in range(40)])
        sr, cs, js = S(idx, {"knn": {"field": "v", "query_vector": embed("d7", 64), "k": 3, "num_candidates": 20}} if not typ.startswith("int8") else
                       {"knn": {"field": "v", "query_vector": embed("d7", 64), "k": 3, "num_candidates": 20, "rescore_vector": {"oversample": 3}}})
        T["vec_" + typ] = {"mapping": cell(cm, jm), "bulk": sb, "search": sr, "class": "AVAILABLE" if cm < 300 and sb["failed"] == 0 and cs == 200 else
                           ("LICENCE_ERROR" if sb["failed"] and "license" in json.dumps(sb["first_error"]).lower() else "OTHER_ERROR")}
        if typ == "int8_hnsw":
            T["knn_rescore_vector"] = sr
        drop(idx)
    # ES|QL
    def Q(q):
        c, j, _ = req("POST", "/_query", {"query": q})
        return cell(c, j, j.get("values") if c == 200 else None)
    T["esql_match"] = Q('FROM %s METADATA _score | WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3 | KEEP title, _score' % base)
    fork = ('FROM %s METADATA _id, _index, _score | FORK (WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3) '
            '(WHERE MATCH(title, "berlin") | SORT _score DESC | LIMIT 3)' % base)
    T["esql_fork"] = Q(fork + " | KEEP _id, _fork, _score")
    T["esql_fuse"] = Q(fork + " | FUSE | KEEP _id, _score")
    # inference
    c, j, _ = req("GET", "/_inference")
    T["inference_list"] = cell(c, j, [e.get("inference_id") for e in j.get("endpoints", [])][:5] if c == 200 else None)
    ep = "lic-ep-" + label
    c, j, _ = req("PUT", "/_inference/text_embedding/" + ep, stub_endpoint_body())
    T["inference_create"] = cell(c, j)
    if c == 200:
        c2, j2, _ = req("POST", "/_inference/text_embedding/" + ep, {"input": "x"})
        T["inference_use_new"] = cell(c2, j2)
        req("DELETE", "/_inference/text_embedding/" + ep + "?force=true")
    else:
        T["inference_use_new"] = {"class": "SKIPPED (create refused)"}
    c, j, _ = req("GET", "/_inference/text_embedding/" + STUB_EP)
    if c == 200:
        c2, j2, _ = req("POST", "/_inference/text_embedding/" + STUB_EP, {"input": "x"})
        T["inference_use_existing"] = cell(c2, j2)
    else:
        T["inference_use_existing"] = {"class": "NO_ENDPOINT", "status": c}
    # semantic_text (needs STUB_EP; on fresh Basic it does not exist)
    sem = P + "sem"
    drop(sem)
    cm, jm, _ = req("PUT", "/" + sem, {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"body": {"type": "semantic_text", "inference_id": STUB_EP}}}})
    T["semantic_text_mapping"] = cell(cm, jm)
    if cm < 300:
        cd, jd, _ = req("PUT", "/%s/_doc/1?refresh=true" % sem, {"body": "paris trip planning"})
        T["semantic_text_index"] = cell(cd, jd)
        T["semantic_query"] = S(sem, {"query": {"semantic": {"field": "body", "query": "paris"}}})[0]
        T["match_on_semantic_text"] = S(sem, {"query": {"match": {"body": "paris"}}})[0]
    drop(sem)
    # DLS (role was made by ensure_dls while allowed; on fresh Basic its creation is the cell)
    T["dls_role_create"] = ensure_dls()["role"]
    T["dls_search_as_user"] = dls_search() if T["dls_role_create"]["class"] == "AVAILABLE" or os.path.exists(DLS_PW) else {"class": "SKIPPED"}
    # percolator (lexical)
    perc = P + "perc"
    drop(perc)
    req("PUT", "/" + perc, {"mappings": {"properties": {"q": {"type": "percolator"}, "title": {"type": "text"}}}})
    ct, jt, _ = req("PUT", "/%s/_doc/t1?refresh=true" % perc, {"q": {"match": {"title": "paris"}}})
    cp, jp, _ = req("POST", "/%s/_search" % perc, {"query": {"percolate": {"field": "q", "document": {"title": "trip to paris"}}}})
    T["percolator_lexical"] = cell(cp if ct < 300 else ct, jp if ct < 300 else jt, hits(jp) if cp == 200 else None)
    drop(perc)
    # aliases: require_alias and must_exist
    g1, al = P + "gen1", P + "alias"
    drop(g1)
    req("PUT", "/" + g1, {"aliases": {al: {"is_write_index": True}}, "mappings": {"properties": {"title": {"type": "text"}}}})
    ca, ja, _ = req("PUT", "/%s/_doc/1?require_alias=true&refresh=true" % al, {"title": "x"})
    cb2, jb, _ = req("PUT", "/%s-notalias/_doc/1?require_alias=true" % P, {"title": "x"})
    cm2, jm2, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al, "must_exist": True}}]})
    cm3, jm3, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al + "-ghost", "must_exist": True}}]})
    T["alias_require_alias"] = {"write_via_alias": cell(ca, ja), "write_to_non_alias_refused": {"status": cb2, "class": classify(cb2, jb)}}
    T["alias_must_exist"] = {"existing": cell(cm2, jm2), "ghost_refused": {"status": cm3, "class": classify(cm3, jm3)}}
    # force merge
    fm = P + "fm"
    drop(fm)
    req("PUT", "/" + fm, {"settings": {"number_of_replicas": 0}})
    bulk(fm, [(str(i), {"t": "x%d" % i}) for i in range(20)])
    cf, jf, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % fm)
    T["force_merge"] = cell(cf, jf)
    for i in (base, g1, fm):
        drop(i)
    return T

"""Experiment 1: feature matrix. run(label) probes every feature on the node's CURRENT licence; quick_probes() is the fast subset
polled around start_basic / start_trial. No model runs anywhere: embeddings come from the loopback stub (licence/stub_embed.py)."""
import json
import os
import secrets
import time

from lib import BulkError, bulk, bulk_strict, cell, classify, embed, err, req

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


class FixtureError(RuntimeError):
    """A fixture that was expected to hold documents does not. Every measurement taken on it would be about an empty or partial index: abort."""


def make_fixture(idx, body, docs, spot=None):
    """Create `idx` and fill it with `docs` (list of (id, source)); MUST succeed. Then establish the contents before anything is measured:
    refresh answered 200 with zero shard failures, `_count` equals len(docs), and (if given) document `spot[0]` has the expected `_source` fields."""
    drop(idx)
    c, j, _ = req("PUT", "/" + idx, body)
    if c != 200 or not (isinstance(j, dict) and j.get("acknowledged")):
        raise FixtureError("create %s: HTTP %s %s" % (idx, c, json.dumps(err(j))[:160]))
    try:
        bulk_strict(idx, docs, refresh="false")
    except BulkError as e:
        raise FixtureError("fill %s: %s" % (idx, e))
    check_fixture(idx, len(docs), spot)


def check_fixture(idx, n, spot=None):
    c, j, _ = req("POST", "/%s/_refresh" % idx)
    sh = j.get("_shards") if isinstance(j, dict) else None
    if c != 200 or not isinstance(sh, dict) or sh.get("failed") != 0:
        raise FixtureError("refresh %s: HTTP %s shards %s" % (idx, c, sh))
    c, j, _ = req("GET", "/%s/_count" % idx)
    if c != 200 or j.get("count") != n or (j.get("_shards") or {}).get("failed") != 0:
        raise FixtureError("count %s: HTTP %s count %s, expected %d" % (idx, c, j.get("count") if isinstance(j, dict) else None, n))
    if spot:
        c, j, _ = req("GET", "/%s/_doc/%s" % (idx, spot[0]))
        src = (j.get("_source") or {}) if isinstance(j, dict) else {}
        if c != 200 or any(src.get(k) != v for k, v in spot[1].items()):
            raise FixtureError("spot check %s/%s: HTTP %s" % (idx, spot[0], c))


def base_mapping(tenant):
    props = {"title": {"type": "text"}, "kw": {"type": "keyword"}, "v": {"type": "dense_vector", "dims": 8, "index": True, "similarity": "cosine"}}
    if tenant:
        props["tenant"] = {"type": "keyword"}
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": props}}


def base_docs(tenant):
    return [(str(i), dict({"title": t, "kw": k, "v": embed(t)}, **({"tenant": "t"} if tenant else {}))) for i, (t, k) in enumerate(DOCS)]


def make_base(idx=BASE_IDX):
    make_fixture(idx, base_mapping(True), base_docs(True), spot=("0", {"title": DOCS[0][0], "kw": DOCS[0][1]}))


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


def dls_battery():
    """Every read path as the DLS-restricted user. Allowed doc: id 0 (kw a). Hidden doc: id 1 (kw b). Returns status plus whether any document body came back."""
    U = dict(user=DLS_USER, pwfile=DLS_PW)
    out = {}

    def rec(name, c, j):
        body = json.dumps(j)
        out[name] = {"status": c, "returned_hidden_doc": ('"kw":"b"' in body.replace(" ", "")) or ("berlin move notes" in body), "returned_any_source": '"_source"' in body and c < 300}
    rec("get_allowed_doc0", *req("GET", "/%s/_doc/0" % BASE_IDX, **U)[:2])
    rec("get_hidden_doc1", *req("GET", "/%s/_doc/1" % BASE_IDX, **U)[:2])
    rec("search_match_all", *req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}, "size": 20}, **U)[:2])
    rec("search_term_hidden", *req("POST", "/%s/_search" % BASE_IDX, {"query": {"term": {"kw": "b"}}}, **U)[:2])
    rec("count", *req("GET", "/%s/_count" % BASE_IDX, **U)[:2])
    rec("mget", *req("POST", "/%s/_mget" % BASE_IDX, {"ids": ["0", "1"]}, **U)[:2])
    rec("msearch", *req("POST", "/_msearch", ndjson='{"index":"%s"}\n{"query":{"match_all":{}}}\n' % BASE_IDX, **U)[:2])
    rec("scroll_open", *req("POST", "/%s/_search?scroll=1m" % BASE_IDX, {"size": 2, "query": {"match_all": {}}}, **U)[:2])
    rec("knn_search", *req("POST", "/%s/_search" % BASE_IDX, {"knn": {"field": "v", "query_vector": embed("berlin move notes"), "k": 3, "num_candidates": 10}}, **U)[:2])
    rec("termvectors_hidden", *req("GET", "/%s/_termvectors/1?fields=title" % BASE_IDX, **U)[:2])
    rec("wildcard_index", *req("POST", "/lic1-*/_search", {"query": {"match_all": {}}}, **U)[:2])
    rec("esql", *req("POST", "/_query", {"query": "FROM %s | LIMIT 10" % BASE_IDX}, **U)[:2])
    return out


def quick_probes(sem_idx="lic-sem"):
    """Fast subset. Keys are stable; values are class strings plus status."""
    out = {"_times": {}}
    std = {"standard": {"query": {"match": {"title": "paris"}}}}
    knn = {"knn": {"field": "v", "query_vector": embed("paris trip planning"), "k": 3, "num_candidates": 10}}
    t = time.time()
    for name, body in (("rrf", {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window_size": 10}}}),
                       ("linear", {"retriever": {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"},
                                                                          {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}], "rank_window_size": 10}}}),
                       ("bm25", {"query": {"match": {"title": "paris"}}}),
                       ("knn", {"knn": knn["knn"]})):
        t0 = time.time()
        c, j, _ = req("POST", "/%s/_search" % BASE_IDX, body, timeout=15)
        out[name] = [classify(c, j), c]
        out["_times"][name] = [t0, time.time()]
    t0 = time.time()
    c, j, _ = req("POST", "/%s/_search" % sem_idx, {"query": {"semantic": {"field": "body", "query": "paris"}}}, timeout=15)
    out["semantic_query"] = [classify(c, j), c]
    out["_times"]["semantic_query"] = [t0, time.time()]
    t0 = time.time()
    c, j, _ = req("POST", "/_inference/text_embedding/%s" % STUB_EP, {"input": "x"}, timeout=15)
    out["inference_use"] = [classify(c, j), c]
    out["_times"]["inference_use"] = [t0, time.time()]
    t0 = time.time()
    d = dls_search() if os.path.exists(DLS_PW) else {"class": "SKIP", "status": None}
    out["dls_search"] = [d["class"], d["status"], d.get("returned")]
    out["_times"]["dls_search"] = [t0, time.time()]
    out["_t"] = t
    return out


def run(label):
    """Full matrix on the current licence. Index names carry the label so phases never share state."""
    P = "lic1-%s-" % label
    T = {}
    base = P + "base"
    make_fixture(base, base_mapping(False), base_docs(False), spot=("0", {"title": DOCS[0][0], "kw": DOCS[0][1]}))   # must succeed; contents established before any cell
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
        # INTENTIONAL PROBE: this write may be refused by the licence (bbq_disk on Basic); the error is the observation, so the recording helper is used
        cb, sb = bulk(idx, [(str(i), {"v": embed("d%d" % i, 64)}) for i in range(40)])
        sr, cs, js = S(idx, {"knn": {"field": "v", "query_vector": embed("d7", 64), "k": 3, "num_candidates": 20}} if not typ.startswith("int8") else
                       {"knn": {"field": "v", "query_vector": embed("d7", 64), "k": 3, "num_candidates": 20, "rescore_vector": {"oversample": 3}}})
        populated = cm < 300 and cb == 200 and sb["failed"] == 0 and sb["items"] == 40
        if populated:   # an AVAILABLE label needs a populated index, not only 200 answers
            try:
                check_fixture(idx, 40)
            except FixtureError:
                populated = False
        T["vec_" + typ] = {"mapping": cell(cm, jm), "bulk": sb, "bulk_http": cb, "search": sr, "populated": populated,
                           "class": "AVAILABLE" if populated and cs == 200 else
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
    cpc, jpc, _ = req("PUT", "/" + perc, {"mappings": {"properties": {"q": {"type": "percolator"}, "title": {"type": "text"}}}})
    if cpc != 200:   # the index must exist; the percolator doc write and search below are the probe
        raise FixtureError("create %s: HTTP %s" % (perc, cpc))
    ct, jt, _ = req("PUT", "/%s/_doc/t1?refresh=true" % perc, {"q": {"match": {"title": "paris"}}})
    cp, jp, _ = req("POST", "/%s/_search" % perc, {"query": {"percolate": {"field": "q", "document": {"title": "trip to paris"}}}})
    T["percolator_lexical"] = cell(cp if ct < 300 else ct, jp if ct < 300 else jt, hits(jp) if cp == 200 else None)
    drop(perc)
    # aliases: require_alias and must_exist
    g1, al = P + "gen1", P + "alias"
    drop(g1)
    cg, jg, _ = req("PUT", "/" + g1, {"aliases": {al: {"is_write_index": True}}, "mappings": {"properties": {"title": {"type": "text"}}}})
    if cg != 200:
        raise FixtureError("create %s: HTTP %s" % (g1, cg))
    ca, ja, _ = req("PUT", "/%s/_doc/1?require_alias=true&refresh=true" % al, {"title": "x"})
    cb2, jb, _ = req("PUT", "/%s-notalias/_doc/1?require_alias=true" % P, {"title": "x"})
    cm2, jm2, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al, "must_exist": True}}]})
    cm3, jm3, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al + "-ghost", "must_exist": True}}]})
    T["alias_require_alias"] = {"write_via_alias": cell(ca, ja), "write_to_non_alias_refused": {"status": cb2, "class": classify(cb2, jb)}}
    T["alias_must_exist"] = {"existing": cell(cm2, jm2), "ghost_refused": {"status": cm3, "class": classify(cm3, jm3)}}
    # force merge
    fm = P + "fm"
    make_fixture(fm, {"settings": {"number_of_replicas": 0}}, [(str(i), {"t": "x%d" % i}) for i in range(20)], spot=("0", {"t": "x0"}))
    cf, jf, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % fm)
    T["force_merge"] = cell(cf, jf)
    for i in (base, g1, fm):
        drop(i)
    return T

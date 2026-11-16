"""Experiment 3: data written with a licensed feature (semantic_text via the loopback stub; bbq_disk), then operated on after start_basic.
Twin indices (suffix -t) take the same operations while the node is still on trial, giving the 'same observable result as on trial' baseline."""
import json
import time

import lib
import matrix
from lib import bulk, classify, embed, err, req

N_SEM, N_VEC, DIM = 200, 300, 64


def title(i):
    return "doc %d about %s" % (i, ["paris", "berlin", "refund", "dentist", "train"][i % 5])


def idx_names(kind, twin=False):
    return "lic3-%s%s" % (kind, "-t" if twin else "")


def mapping(kind):
    if kind == "sem":
        return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
            "title": {"type": "text"}, "tag": {"type": "keyword"}, "body": {"type": "semantic_text", "inference_id": matrix.STUB_EP}}}}
    typ = {"bbq_disk": "bbq_disk", "bbq_hnsw": "bbq_hnsw", "plain": "hnsw"}[kind]
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
        "title": {"type": "text"}, "tag": {"type": "keyword"},
        "v": {"type": "dense_vector", "dims": DIM, "index": True, "similarity": "cosine", "index_options": {"type": typ}}}}}


def doc(kind, i):
    d = {"title": title(i), "tag": "t0"}
    if kind == "sem":
        d["body"] = title(i)
    else:
        d["v"] = embed(title(i), DIM)
    return d


def n_docs(kind):
    return N_SEM if kind == "sem" else N_VEC


def fixtures():
    """Call on trial. Creates the stub endpoint, 4 indices and their twins; returns write results."""
    out = {}
    c, j, _ = req("PUT", "/_inference/text_embedding/" + matrix.STUB_EP, matrix.stub_endpoint_body())
    out["endpoint"] = [c, err(j)]
    for kind in ("sem", "bbq_disk", "bbq_hnsw", "plain"):
        for twin in (False, True):
            ix = idx_names(kind, twin)
            req("DELETE", "/" + ix)
            c, j, _ = req("PUT", "/" + ix, mapping(kind))
            cb, sb = bulk(ix, [(str(i), doc(kind, i)) for i in range(n_docs(kind))])
            out[ix] = {"create": c, "bulk": sb}
            if kind == "sem":
                req("POST", "/%s/_refresh" % ix)
    return out


def count(ix):
    req("POST", "/%s/_refresh" % ix)
    c, j, _ = req("GET", "/%s/_count" % ix)
    return j.get("count") if c == 200 else {"status": c, "error": err(j)}


def inference_stored(ix, ids):
    """How many of ids have stored chunk embeddings (read via _source_includes=_inference_fields)."""
    have, wrong = 0, 0
    for i in ids:
        c, j, _ = req("GET", "/%s/_doc/%s?_source_includes=_inference_fields" % (ix, i))
        if c != 200:
            wrong += 1
            continue
        f = (j.get("_source") or {}).get("_inference_fields", {}).get("body", {}).get("inference", {}).get("chunks", {}).get("body")
        if f and f[0].get("embeddings"):
            have += 1
    return {"with_embeddings": have, "unreadable": wrong, "of": len(ids)}


def reads(kind, ix):
    """Read-only battery. Each entry -> compact observable (class, status, witness)."""
    R = {}
    # get every doc
    found, miss = 0, []
    for i in range(n_docs(kind)):
        c, j, _ = req("GET", "/%s/_doc/%d" % (ix, i))
        if c == 200 and j.get("found"):
            found += 1
        else:
            miss.append(i)
    R["get_all"] = {"found": found, "of": n_docs(kind), "missing_first": miss[:3]}
    R["count"] = count(ix)
    if kind == "sem":
        R["inference_embeddings_sample"] = inference_stored(ix, list(range(0, N_SEM, 10)))
        for name, body in (("lexical_title", {"query": {"match": {"title": "paris"}}}),
                           ("semantic_query", {"query": {"semantic": {"field": "body", "query": "paris"}}}),
                           ("match_on_semantic_field", {"query": {"match": {"body": "paris"}}}),
                           ("query_string_on_all", {"query": {"query_string": {"query": "paris"}}})):
            c, j, _ = req("POST", "/%s/_search" % ix, dict(body, size=50, track_total_hits=True))
            R[name] = {"class": classify(c, j), "status": c, "hits": j.get("hits", {}).get("total", {}).get("value") if c == 200 else None,
                       "error": err(j) if c >= 300 else None}
    else:
        c, j, _ = req("POST", "/%s/_search" % ix, {"query": {"match": {"title": "paris"}}, "size": 50, "track_total_hits": True})
        R["lexical_title"] = {"class": classify(c, j), "status": c, "hits": j.get("hits", {}).get("total", {}).get("value") if c == 200 else None}
        tops, tot = 0, 0
        for i in range(0, N_VEC, 6):
            c, j, _ = req("POST", "/%s/_search" % ix, {"knn": {"field": "v", "query_vector": embed(title(i), DIM), "k": 3, "num_candidates": 50}, "size": 3})
            tot += 1
            if c == 200 and j["hits"]["hits"] and j["hits"]["hits"][0]["_id"] == str(i):
                tops += 1
            last = (c, j)
        eq, tot2 = 0, 0
        for i in range(0, N_VEC, 5):
            c, j, _ = req("GET", "/%s/_doc/%d?_source_includes=v&_source_exclude_vectors=false" % (ix, i))
            if c == 200:
                tot2 += 1
                v = (j.get("_source") or {}).get("v") or []
                if len(v) == DIM and max(abs(a - b) for a, b in zip(embed(title(i), DIM), v)) < 1e-6:
                    eq += 1
        R["vectors_equal_ground_truth"] = {"equal": eq, "of": tot2}
        R["knn_self_top1"] = {"top1": tops, "of": tot, "last_status": last[0], "last_error": err(last[1]) if last[0] >= 300 else None}
    return R


def mutations(kind, ix):
    """Mutating battery in a fixed order; every entry records the call result AND the observable consequence."""
    M = {}

    def get(i, inc=None):
        p = "/%s/_doc/%s" % (ix, i) + ("?_source_includes=%s&_source_exclude_vectors=false" % inc if inc else "")
        c, j, _ = req("GET", p)
        return c, j

    def rec(name, c, j, check=None):
        M[name] = {"class": classify(c, j), "status": c, "error": err(j) if c >= 300 else None}
        if check:
            M[name]["effect"] = check()
    ntag = lambda i: (get(i)[1].get("_source") or {}).get("tag")
    emb = (lambda i: inference_stored(ix, [i])["with_embeddings"]) if kind == "sem" else (lambda i: 1 if len((get(i, "v")[1].get("_source") or {}).get("v") or []) == DIM else 0)
    # full overwrite
    d = doc(kind, 1)
    d["tag"] = "full"
    c, j, _ = req("PUT", "/%s/_doc/1?refresh=true" % ix, d)
    rec("overwrite_full_doc", c, j, lambda: {"tag": ntag(1), "embedding_present": emb(1)})
    # partial update
    c, j, _ = req("POST", "/%s/_update/2?refresh=true" % ix, {"doc": {"tag": "partial"}})
    rec("update_partial_api", c, j, lambda: {"tag": ntag(2), "embedding_present": emb(2)})
    # bulk partial update
    c, s = bulk(ix, [("3", {"tag": "bulkpartial"})], op="update")
    M["bulk_partial_update"] = {"status": c, "summary": s, "effect": {"tag": ntag(3), "embedding_present": emb(3)}}
    # scripted
    c, j, _ = req("POST", "/%s/_update/4?refresh=true" % ix, {"script": {"source": "ctx._source.tag = params.t", "params": {"t": "scripted"}}})
    rec("update_scripted", c, j, lambda: {"tag": ntag(4), "embedding_present": emb(4)})
    # delete
    c, j, _ = req("DELETE", "/%s/_doc/5?refresh=true" % ix)
    rec("delete_one", c, j, lambda: {"get_after_status": get(5)[0]})
    # update by query
    c, j, _ = req("POST", "/%s/_update_by_query?refresh=true&conflicts=proceed" % ix,
                  {"query": {"term": {"tag": "t0"}}, "script": {"source": "ctx._source.tag = 'ubq'", "lang": "painless"}})
    rec("update_by_query", c, j, lambda: {"updated": j.get("updated"), "failures": len(j.get("failures", [])) if c == 200 else None,
                                          "tag_of_10": ntag(10), "embedding_present_10": emb(10)})
    # server-side reindex to same mapping
    dst = ix + "-rx"
    req("DELETE", "/" + dst)
    req("PUT", "/" + dst, mapping(kind))
    c, j, _ = req("POST", "/_reindex?refresh=true", {"source": {"index": ix}, "dest": {"index": dst}}, timeout=300)
    rec("reindex_same_mapping", c, j, lambda: {"dst_count": count(dst), "failures": len(j.get("failures", [])) if c == 200 else None,
                                              "created": j.get("created"), "dst_embeddings": inference_stored(dst, list(range(0, 40, 4))) if kind == "sem" else None,
                                              "dst_vectors_present": None if kind == "sem" else sum(1 for q in (0, 10, 20, 30) if len((req("GET", "/%s/_doc/%d?_source_includes=v&_source_exclude_vectors=false" % (dst, q))[1].get("_source") or {}).get("v") or []) == DIM)})
    req("DELETE", "/" + dst)
    # force merge
    c, j, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % ix, timeout=300)
    rec("force_merge", c, j, lambda: {"count_after": count(ix)})
    # new doc
    d = doc(kind, 9999)
    c, j, _ = req("PUT", "/%s/_doc/9999?refresh=true" % ix, d)
    rec("index_new_doc", c, j, lambda: {"count": count(ix)})
    return M


def migrate_vectors(src, dst, typ="hnsw", corrupt=False):
    """Recipe for bbq_disk (or any float-source index) -> Basic-safe mapping: scroll _source, bulk into a dense_vector index of a Basic type."""
    req("DELETE", "/" + dst)
    c, j, _ = req("PUT", "/" + dst, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
        "title": {"type": "text"}, "tag": {"type": "keyword"}, "v": {"type": "dense_vector", "dims": DIM, "index": True, "similarity": "cosine", "index_options": {"type": typ}}}}})
    assert c == 200, (c, j)
    docs, sid = {}, None
    c, j, _ = req("POST", "/%s/_search?scroll=2m" % src, {"size": 100, "query": {"match_all": {}}, "_source": {"exclude_vectors": False}}, timeout=120)
    sid = j.get("_scroll_id")
    while c == 200 and j["hits"]["hits"]:
        for h in j["hits"]["hits"]:
            docs[h["_id"]] = h["_source"]
        c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)
    if sid:
        req("DELETE", "/_search/scroll", {"scroll_id": sid})
    items = sorted(docs.items())
    if corrupt and items:
        i0, s0 = items[0]
        s0 = dict(s0)
        s0["v"] = list(s0["v"])
        s0["v"][0] += 0.01
        items[0] = (i0, s0)
    for k in range(0, len(items), 100):
        cb, sb = bulk(dst, items[k:k + 100], refresh="false")
        assert sb["failed"] == 0, sb
    req("POST", "/%s/_refresh" % dst)
    return docs


def check_vectors(src_docs, dst, dim=DIM):
    """Element-wise equality of dst _source vectors to src_docs; kNN self-hit top-1 rate; count equality."""
    mism, got = [], {}
    c, j, _ = req("POST", "/%s/_search?scroll=2m" % dst, {"size": 100, "query": {"match_all": {}}, "_source": {"exclude_vectors": False}}, timeout=120)
    sid = j.get("_scroll_id")
    while c == 200 and j["hits"]["hits"]:
        for h in j["hits"]["hits"]:
            got[h["_id"]] = h["_source"]
        c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)
    if sid:
        req("DELETE", "/_search/scroll", {"scroll_id": sid})
    for i, s in src_docs.items():
        if i not in got or got[i].get("v") != s.get("v"):
            mism.append(i)
    worst = 0.0
    for i, sd in src_docs.items():
        if i.isdigit() and int(i) < N_VEC:
            worst = max(worst, max(abs(a - b) for a, b in zip(embed(title(int(i)), DIM), sd["v"])))
    tops, tot = 0, 0
    for i in list(src_docs)[::3]:
        c, j, _ = req("POST", "/%s/_search" % dst, {"knn": {"field": "v", "query_vector": src_docs[i]["v"], "k": 3, "num_candidates": 100}, "size": 1})
        tot += 1
        if c == 200 and j["hits"]["hits"] and j["hits"]["hits"][0]["_id"] == i:
            tops += 1
    return {"src_count": len(src_docs), "dst_count": len(got), "vector_mismatches": len(mism), "mismatch_ids": mism[:3], "knn_self_top1": tops, "knn_self_of": tot, "src_vs_ground_truth_max_abs": worst}


def migrate_semantic(src, dst):
    """Recipe for semantic_text -> text + dense_vector(8): copy the stored chunk embedding (read from _inference_fields) next to the text."""
    req("DELETE", "/" + dst)
    c, j, _ = req("PUT", "/" + dst, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
        "title": {"type": "text"}, "tag": {"type": "keyword"}, "body": {"type": "text"},
        "v": {"type": "dense_vector", "dims": 8, "index": True, "similarity": "cosine"}}}})
    assert c == 200, (c, j)
    docs, multi = {}, 0
    c, j, _ = req("POST", "/%s/_search?scroll=2m" % src, {"size": 50, "query": {"match_all": {}}, "_source": ["title", "tag", "body", "_inference_fields"]}, timeout=120)
    first_status = c
    sid = j.get("_scroll_id")
    while c == 200 and j["hits"]["hits"]:
        for h in j["hits"]["hits"]:
            s = h["_source"]
            chunks = (s.get("_inference_fields", {}).get("body", {}).get("inference", {}).get("chunks", {}) or {}).get("body") or []
            if len(chunks) != 1:
                multi += 1
                continue
            docs[h["_id"]] = {"title": s.get("title"), "tag": s.get("tag"), "body": s.get("body"), "v": chunks[0]["embeddings"]}
        c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)
    if sid:
        req("DELETE", "/_search/scroll", {"scroll_id": sid})
    items = sorted(docs.items())
    for k in range(0, len(items), 100):
        cb, sb = bulk(dst, items[k:k + 100], refresh="false")
        assert sb["failed"] == 0, sb
    req("POST", "/%s/_refresh" % dst)
    return docs, {"first_search_status": first_status, "skipped_not_single_chunk": multi}


def check_semantic(docs, dst):
    got = {}
    c, j, _ = req("POST", "/%s/_search?scroll=2m" % dst, {"size": 100, "query": {"match_all": {}}, "_source": {"exclude_vectors": False}}, timeout=120)
    sid = j.get("_scroll_id")
    while c == 200 and j["hits"]["hits"]:
        for h in j["hits"]["hits"]:
            got[h["_id"]] = h["_source"]
        c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)
    if sid:
        req("DELETE", "/_search/scroll", {"scroll_id": sid})
    mism = [i for i, s in docs.items() if i not in got or got[i].get("v") != s["v"]]
    worst_abs, worst_cos = 0.0, 1.0
    for i, s in docs.items():
        ref = embed(s["body"], 8)
        a = max(abs(x - y) for x, y in zip(ref, s["v"]))
        dot = sum(x * y for x, y in zip(ref, s["v"]))
        na, nb = sum(x * x for x in ref) ** 0.5, sum(y * y for y in s["v"]) ** 0.5
        worst_abs, worst_cos = max(worst_abs, a), min(worst_cos, dot / (na * nb))
    tops, tot = 0, 0
    for i in list(docs)[::2]:
        cc, jj, _ = req("POST", "/%s/_search" % dst, {"knn": {"field": "v", "query_vector": docs[i]["v"], "k": 3, "num_candidates": 100}, "size": 3})
        tot += 1
        ids = [h["_id"] for h in jj["hits"]["hits"]] if cc == 200 else []
        # many docs share one text (title has 5 variants plus number), body unique per i so top1 must be i
        if ids and ids[0] == i:
            tops += 1
    return {"docs": len(docs), "dst_count": len(got), "vector_mismatches_vs_stored": len(mism), "worst_abs_err_vs_stub": worst_abs, "worst_cosine_vs_stub": worst_cos,
            "knn_self_top1": tops, "knn_self_of": tot}

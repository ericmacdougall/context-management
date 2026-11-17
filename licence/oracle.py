"""Independent oracle for experiment 2: raw HTTP legs and our OWN reciprocal-rank-fusion (does not import memcore.fusion or the adapter)."""
from lib import req

FIELD_T, FIELD_V = "text", "vector"


def _flt(tenant):
    return [{"term": {"tenant": tenant}}]


def raw_legs(index, text, vector, tenant, window=100):
    c1, j1, _ = req("POST", "/%s/_search" % index, {"size": window, "query": {"bool": {"must": [{"match": {FIELD_T: text}}], "filter": _flt(tenant)}}})
    c2, j2, _ = req("POST", "/%s/_search" % index, {"size": window, "knn": {"field": FIELD_V, "query_vector": list(vector), "k": window,
                                                                          "num_candidates": window, "filter": _flt(tenant)}})
    if c1 != 200 or c2 != 200:
        raise RuntimeError("oracle legs failed %s %s" % (c1, c2))
    return [h["_id"] for h in j1["hits"]["hits"]], [h["_id"] for h in j2["hits"]["hits"]]


def rrf_scores(lists, c=60):
    s = {}
    for lst in lists:
        seen = set()
        for r, d in enumerate(lst, 1):
            if d in seen:
                continue
            seen.add(d)
            s[d] = s.get(d, 0.0) + 1.0 / (c + r)
    return s


def valid_topk(ids, lists, k, c=60):
    """True when ids is a legal top-k of reciprocal-rank fusion of lists (ties at the cut and inside ties may order either way)."""
    sc = rrf_scores(lists, c)
    n = min(k, len(sc))
    if len(ids) != n or len(set(ids)) != n or any(i not in sc for i in ids):
        return False
    vals = sorted(sc.values(), reverse=True)
    kth = vals[n - 1] if n else 0.0
    got = [sc[i] for i in ids]
    if any(got[i] < got[i + 1] - 1e-12 for i in range(len(got) - 1)):
        return False
    if any(g < kth - 1e-12 for g in got):
        return False
    must = {d for d, v in sc.items() if v > kth + 1e-12}
    return must <= set(ids)


def raw_backend(index, kind, text, vector, tenant, k=10, window=100):
    """The licensed retriever asked directly (rrf or linear), same legs as the adapter. -> (status, ids, error text)."""
    std = {"standard": {"query": {"bool": {"must": [{"match": {FIELD_T: text}}], "filter": _flt(tenant)}}}}
    knn = {"knn": {"field": FIELD_V, "query_vector": list(vector), "k": max(k, window), "num_candidates": 100, "filter": _flt(tenant)}}
    if kind == "linear":
        r = {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"}, {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}],
                        "rank_window_size": window}}
    else:
        r = {"rrf": {"retrievers": [std, knn], "rank_window_size": window, "rank_constant": 60}}
    c, j, _ = req("POST", "/%s/_search" % index, {"size": k, "retriever": r})
    return c, ([h["_id"] for h in j["hits"]["hits"]] if c == 200 else None), (None if c == 200 else str(j)[:200])

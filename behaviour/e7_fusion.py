import sys
from esc import *
phase = sys.argv[1]
IDX = "e7-fusion"
if phase == "basic":
    req("DELETE", f"/{IDX}")
    req("PUT", f"/{IDX}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"title": {"type": "text"}, "body": {"type": "text"}, "v": {"type": "dense_vector", "dims": 3, "index": True, "similarity": "cosine"}}}})
    for i, (t, v) in enumerate([("paris trip planning", [1, 0, 0]), ("berlin move notes", [0, 1, 0]), ("dentist appointment paris", [0, 0, 1]), ("refund policy", [1, 1, 0]), ("moved to berlin", [0, 1, 1])]):
        req("PUT", f"/{IDX}/_doc/{i}", {"title": t, "body": t + " extra", "v": v})
    req("POST", f"/{IDX}/_refresh")
def r(c, j): 
    if c == 200: return {"status": 200, "ok": True, "hits": [h["_id"] for h in j.get("hits", {}).get("hits", [])] if "hits" in j else None, "values": j.get("values")}
    e = j.get("error", {}); return {"status": c, "type": e.get("type"), "reason": e.get("reason"), "root": (e.get("root_cause") or [{}])[0].get("reason"), "caused_by": (e.get("caused_by") or {}).get("reason")}
std = {"standard": {"query": {"match": {"title": "paris"}}}}
knn = {"knn": {"field": "v", "query_vector": [1, 0, 0], "k": 3, "num_candidates": 10}}
S = lambda ret: r(*req("POST", f"/{IDX}/_search", {"retriever": ret, "size": 3})[:2])
T = {}
T["retriever_standard"] = S(std)
T["retriever_knn"] = S(knn)
T["retriever_rescorer"] = S({"rescorer": {"retriever": std, "rescore": {"window_size": 5, "query": {"rescore_query": {"match": {"title": "dentist"}}, "query_weight": 1, "rescore_query_weight": 2}}}})
T["retriever_pinned"] = S({"pinned": {"ids": ["3"], "retriever": std}})
T["retriever_rrf"] = S({"rrf": {"retrievers": [std, knn], "rank_window_size": 10, "rank_constant": 60}})
T["retriever_linear"] = S({"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"}, {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}], "rank_window_size": 10}})
T["retriever_text_similarity_reranker_builtin_elastic_rerank"] = S({"text_similarity_reranker": {"retriever": std, "field": "title", "inference_id": ".rerank-v1-elasticsearch", "inference_text": "paris", "rank_window_size": 5}})
T["retriever_diversify_mmr"] = S({"diversify": {"type": "mmr", "field": "v", "query_vector": [1, 0, 0], "lambda": 0.5, "retriever": knn}})
T["knn_search_top_level"] = r(*req("POST", f"/{IDX}/_search", {"knn": knn["knn"], "size": 3})[:2])
T["rank_rrf_legacy_top_level"] = r(*req("POST", f"/{IDX}/_search", {"query": std["standard"]["query"], "knn": knn["knn"], "rank": {"rrf": {}}, "size": 3})[:2])
Q = lambda q: r(*req("POST", "/_query", {"query": q})[:2])
T["esql_match"] = Q(f'FROM {IDX} METADATA _score | WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3 | KEEP title, _score')
T["esql_fork"] = Q(f'FROM {IDX} METADATA _id, _index, _score | FORK (WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3) (WHERE MATCH(title, "berlin") | SORT _score DESC | LIMIT 3) | KEEP _id, _fork, _score')
T["esql_fork_fuse"] = Q(f'FROM {IDX} METADATA _id, _index, _score | FORK (WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3) (WHERE MATCH(title, "berlin") | SORT _score DESC | LIMIT 3) | FUSE | KEEP _id, _score')
T["esql_fuse_linear"] = Q(f'FROM {IDX} METADATA _id, _index, _score | FORK (WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3) (WHERE MATCH(title, "berlin") | SORT _score DESC | LIMIT 3) | FUSE LINEAR | KEEP _id, _score')
T["esql_rerank"] = Q(f'FROM {IDX} METADATA _score | WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3 | RERANK "paris" ON title WITH {{"inference_id": ".rerank-v1-elasticsearch"}} | KEEP title, _score')
T["esql_completion"] = Q(f'FROM {IDX} | LIMIT 1 | COMPLETION x = "hello" WITH {{"inference_id": ".rainbow-sprinkles-elastic"}} | KEEP x')
T["esql_lookup_join"] = (lambda: (req("PUT", "/e7-lookup", {"settings": {"index.mode": "lookup"}, "mappings": {"properties": {"title": {"type": "keyword"}, "tag": {"type": "keyword"}}}}), req("PUT", "/e7-lookup/_doc/1", {"title": "refund policy", "tag": "billing"}, refresh="true"), req("PUT", "/e7-kw", {"mappings": {"properties": {"title": {"type": "keyword"}}}}), req("PUT", "/e7-kw/_doc/1", {"title": "refund policy"}, refresh="true"), Q('FROM e7-kw | LOOKUP JOIN e7-lookup ON title | KEEP title, tag')))()[-1]
out = {"phase": phase, "license": req("GET", "/_license")[1]["license"]["type"], "tests": T}
save(f"E7_{phase}.json", out); print(json.dumps(out, indent=1))

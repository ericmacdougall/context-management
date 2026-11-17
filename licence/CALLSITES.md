# Elasticsearch call sites under licence/ (audit after final-gate review-3 F3)

Kinds: **must-succeed** (a fixture or precondition: status and body are checked and failure aborts), **probe** (the licence-gated or error response is the observation: status and class are recorded), **cleanup** (DELETE, 404 expected), **verification read**, **helper**. Scope: every `req(`, `bulk(`, `bulk_strict(`, `bulk_add(` call and the `S(`/`Q(`/`rec(` wrappers in `licence/*.py`; shell scripts only start a container and write a curl config (no Elasticsearch response is read).

| file:line | call | HTTP status checked | body validated | on failure | kind |
|---|---|---|---|---|---|
| lib.py:47 | `c, j, _ = req("GET", "/_license")` | yes (c != 200 handled by caller) | partly | returns the error body; recorded | probe |
| lib.py:84 | `v = req("GET", "/")[1].get("version", {}).get("number")` | no | version field only | version None in the stamp (metadata, not a measurement) | stamp |
| lib.py:104 | `return req("POST", "/_bulk?refresh=" + refresh, ndjson=nd, timeout=120)` | returned to caller | n/a | see bulk and bulk_strict | helper |
| matrix.py:22 | `req("DELETE", "/" + idx)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| matrix.py:30 | `c, j, _ = req("POST", "/%s/_search" % idx, body, **kw)` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:48 | `c, j, _ = req("PUT", "/" + idx, body)` | yes (HTTP 200, acknowledged, shard failures, count, spot check) | yes | FixtureError: the run aborts; no cell is produced | must-succeed |
| matrix.py:52 | `bulk_strict(idx, docs, refresh="false")` | yes (HTTP 200, acknowledged, shard failures, count, spot check) | yes | FixtureError: the run aborts; no cell is produced | must-succeed |
| matrix.py:59 | `c, j, _ = req("POST", "/%s/_refresh" % idx)` | yes (HTTP 200, acknowledged, shard failures, count, spot check) | yes | FixtureError: the run aborts; no cell is produced | must-succeed |
| matrix.py:63 | `c, j, _ = req("GET", "/%s/_count" % idx)` | yes (HTTP 200, acknowledged, shard failures, count, spot check) | yes | FixtureError: the run aborts; no cell is produced | must-succeed |
| matrix.py:67 | `c, j, _ = req("GET", "/%s/_doc/%s" % (idx, spot[0]))` | yes (HTTP 200, acknowledged, shard failures, count, spot check) | yes | FixtureError: the run aborts; no cell is produced | must-succeed |
| matrix.py:98 | `cr, jr, _ = req("PUT", "/_security/role/" + DLS_ROLE, {"indices": [{"names": [BASE_IDX], "` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:99 | `cu, ju, _ = req("PUT", "/_security/user/" + DLS_USER, {"password": pw, "roles": [DLS_ROLE]` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:104 | `c, j, _ = req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}, "size": 20}, ` | recorded (classify) | no | class TRANSPORT/LICENCE_ERROR/OTHER_ERROR recorded with time | probe (polled around the transition) |
| matrix.py:121 | `rec("get_allowed_doc0", *req("GET", "/%s/_doc/0" % BASE_IDX, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:122 | `rec("get_hidden_doc1", *req("GET", "/%s/_doc/1" % BASE_IDX, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:123 | `rec("search_match_all", *req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:124 | `rec("search_term_hidden", *req("POST", "/%s/_search" % BASE_IDX, {"query": {"term": {"kw":` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:125 | `rec("count", *req("GET", "/%s/_count" % BASE_IDX, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:126 | `rec("mget", *req("POST", "/%s/_mget" % BASE_IDX, {"ids": ["0", "1"]}, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:127 | `rec("msearch", *req("POST", "/_msearch", ndjson='{"index":"%s"}\n{"query":{"match_all":{}}` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:128 | `rec("scroll_open", *req("POST", "/%s/_search?scroll=1m" % BASE_IDX, {"size": 2, "query": {` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:129 | `rec("knn_search", *req("POST", "/%s/_search" % BASE_IDX, {"knn": {"field": "v", "query_vec` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:130 | `rec("termvectors_hidden", *req("GET", "/%s/_termvectors/1?fields=title" % BASE_IDX, **U)[:` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:131 | `rec("wildcard_index", *req("POST", "/lic1-*/_search", {"query": {"match_all": {}}}, **U)[:` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:132 | `rec("esql", *req("POST", "/_query", {"query": "FROM %s \| LIMIT 10" % BASE_IDX}, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back | probe (DLS read battery) |
| matrix.py:148 | `c, j, _ = req("POST", "/%s/_search" % BASE_IDX, body, timeout=15)` | recorded (classify) | no | class TRANSPORT/LICENCE_ERROR/OTHER_ERROR recorded with time | probe (polled around the transition) |
| matrix.py:152 | `c, j, _ = req("POST", "/%s/_search" % sem_idx, {"query": {"semantic": {"field": "body", "q` | recorded (classify) | no | class TRANSPORT/LICENCE_ERROR/OTHER_ERROR recorded with time | probe (polled around the transition) |
| matrix.py:156 | `c, j, _ = req("POST", "/_inference/text_embedding/%s" % STUB_EP, {"input": "x"}, timeout=1` | recorded (classify) | no | class TRANSPORT/LICENCE_ERROR/OTHER_ERROR recorded with time | probe (polled around the transition) |
| matrix.py:176 | `T["bm25_match"] = S(base, {"query": {"match": {"title": "paris"}}})[0]` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:177 | `T["dense_knn"] = S(base, {"knn": knn["knn"]})[0]` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:179 | `lex, c1, j1 = S(base, {"query": {"match": {"title": "paris"}}, "size": 5})` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:180 | `vec, c2, j2 = S(base, {"knn": knn["knn"], "size": 5})` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:186 | `T["retriever_rrf"] = S(base, {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:187 | `T["retriever_linear"] = S(base, {"retriever": {"linear": {"retrievers": [{"retriever": std` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:189 | `T["text_similarity_reranker_gate"] = S(base, {"retriever": {"text_similarity_reranker": {"` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:195 | `cm, jm, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas` | recorded (cell) | error body recorded | class LICENCE_ERROR or OTHER_ERROR; populated=false so never AVAILABLE | probe (licence may refuse the mapping) |
| matrix.py:198 | `cb, sb = bulk(idx, [(str(i), {"v": embed("d%d" % i, 64)}) for i in range(40)])` | recorded (bulk_http) | items counted | failed counts unanswered operations; AVAILABLE needs populated index (count 40) | probe (licence may refuse the write: the error is the observation) |
| matrix.py:199 | `sr, cs, js = S(idx, {"knn": {"field": "v", "query_vector": embed("d7", 64), "k": 3, "num_c` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:215 | `c, j, _ = req("POST", "/_query", {"query": q})` | recorded (cell) | values as witness | class recorded | probe |
| matrix.py:217 | `T["esql_match"] = Q('FROM %s METADATA _score \| WHERE MATCH(title, "paris") \| SORT _score D` | recorded (cell) | values as witness | class recorded | probe |
| matrix.py:220 | `T["esql_fork"] = Q(fork + " \| KEEP _id, _fork, _score")` | recorded (cell) | values as witness | class recorded | probe |
| matrix.py:221 | `T["esql_fuse"] = Q(fork + " \| FUSE \| KEEP _id, _score")` | recorded (cell) | values as witness | class recorded | probe |
| matrix.py:223 | `c, j, _ = req("GET", "/_inference")` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:226 | `c, j, _ = req("PUT", "/_inference/text_embedding/" + ep, stub_endpoint_body())` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:229 | `c2, j2, _ = req("POST", "/_inference/text_embedding/" + ep, {"input": "x"})` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:231 | `req("DELETE", "/_inference/text_embedding/" + ep + "?force=true")` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| matrix.py:234 | `c, j, _ = req("GET", "/_inference/text_embedding/" + STUB_EP)` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:236 | `c2, j2, _ = req("POST", "/_inference/text_embedding/" + STUB_EP, {"input": "x"})` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:243 | `cm, jm, _ = req("PUT", "/" + sem, {"settings": {"number_of_replicas": 0}, "mappings": {"pr` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:246 | `cd, jd, _ = req("PUT", "/%s/_doc/1?refresh=true" % sem, {"body": "paris trip planning"})` | recorded (cell) | error body recorded | class recorded | probe |
| matrix.py:248 | `T["semantic_query"] = S(sem, {"query": {"semantic": {"field": "body", "query": "paris"}}})` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:249 | `T["match_on_semantic_text"] = S(sem, {"query": {"match": {"body": "paris"}}})[0]` | recorded (cell) | hit ids as witness | class recorded; base index contents were established first | probe |
| matrix.py:257 | `cpc, jpc, _ = req("PUT", "/" + perc, {"mappings": {"properties": {"q": {"type": "percolato` | yes (HTTP 200) | no | FixtureError: the run aborts | must-succeed |
| matrix.py:260 | `ct, jt, _ = req("PUT", "/%s/_doc/t1?refresh=true" % perc, {"q": {"match": {"title": "paris` | recorded (cell) | hits | class recorded; index creation is must-succeed (above) | probe |
| matrix.py:261 | `cp, jp, _ = req("POST", "/%s/_search" % perc, {"query": {"percolate": {"field": "q", "docu` | recorded (cell) | hits | class recorded; index creation is must-succeed (above) | probe |
| matrix.py:267 | `cg, jg, _ = req("PUT", "/" + g1, {"aliases": {al: {"is_write_index": True}}, "mappings": {` | yes (HTTP 200) | no | FixtureError: the run aborts | must-succeed |
| matrix.py:270 | `ca, ja, _ = req("PUT", "/%s/_doc/1?require_alias=true&refresh=true" % al, {"title": "x"})` | recorded | status/class | class recorded | probe (refusals are the observation) |
| matrix.py:271 | `cb2, jb, _ = req("PUT", "/%s-notalias/_doc/1?require_alias=true" % P, {"title": "x"})` | recorded | status/class | class recorded | probe (refusals are the observation) |
| matrix.py:272 | `cm2, jm2, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al, ` | recorded | status/class | class recorded | probe (refusals are the observation) |
| matrix.py:273 | `cm3, jm3, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al +` | recorded | status/class | class recorded | probe (refusals are the observation) |
| matrix.py:279 | `cf, jf, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % fm)` | recorded (cell) | no | class recorded; fixture contents were established first | probe |
| exp2.py:100 | `req("DELETE", "/" + idx)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp2.py:101 | `c, j, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas":` | yes (assert c == 200) | no | AssertionError aborts | must-succeed |
| exp2.py:106 | `self.a_plain.bulk_add("static", dict(items[i:i + 500]))` | yes (adapter raises on HTTP error and on errors:true) | errors flag only | exception aborts setup; count then checked | must-succeed |
| exp2.py:108 | `req("DELETE", "/lic-sem2")` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp2.py:109 | `c, j, _ = req("PUT", "/lic-sem2", {"settings": {"number_of_replicas": 0}, "mappings": {"pr` | recorded (events.sem2_create) | error body | recorded | probe (semantic index may be refused) |
| exp2.py:111 | `c, j, _ = req("GET", "/mc-%s-static/_count" % TENANT)` | yes (c == 200 and count == number of documents) | count | AssertionError aborts (new) | must-succeed |
| exp2.py:164 | `self.a_plain.bulk_add("live", batch)` | adapter raises on failure | errors flag only | failure recorded as a failed batch (attempted_failed); acknowledged ids reconciled later | workload write |
| exp2.py:179 | `c, j, _ = req("PUT", "/lic-sem2/_doc/%s" % i, {"body": "alpha bravo %d" % seq}, timeout=20` | recorded per row | error body per row | classified by classify_sem_failure; UNVERIFIED for old lives | workload write (loud failures expected) |
| exp2.py:210 | `cr, jr, _ = req("POST", "/mc-%s-live/_refresh" % TENANT)` | yes (new) | no | AssertionError aborts verification | must-succeed |
| exp2.py:214 | `c, j, _ = req("POST", "/mc-%s-live/_search?scroll=2m" % TENANT, {"size": 5000, "_source": ` | loop stops on non-200 | hits | a short read can only inflate acked_absent (fails safe); not raised | verification read |
| exp2.py:218 | `c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)` | loop stops on non-200 | hits | a short read can only inflate acked_absent (fails safe); not raised | verification read |
| exp2.py:220 | `req("DELETE", "/_search/scroll", {"scroll_id": sid})` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp2.py:224 | `req("POST", "/lic-sem2/_refresh")` | no | no | a missing refresh can only hide sem documents: sem_acked_absent inflates (fails safe) | verification read |
| exp2.py:225 | `c, j, _ = req("POST", "/lic-sem2/_search", {"size": 10000, "_source": False, "query": {"ma` | search status checked | hit ids | short read can only inflate sem_acked_absent (fails safe) | verification read |
| exp3.py:47 | `c, j, _ = req("PUT", "/_inference/text_embedding/" + matrix.STUB_EP, matrix.stub_endpoint_` | yes (new) | no | ScrollError aborts fixtures | must-succeed (on trial) |
| exp3.py:54 | `req("DELETE", "/" + ix)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:55 | `c, j, _ = req("PUT", "/" + ix, mapping(kind))` | yes (new) | no | ScrollError aborts | must-succeed (on trial) |
| exp3.py:58 | `cb, sb = bulk_strict(ix, [(str(i), doc(kind, i)) for i in range(n_docs(kind))])` | yes | one result per operation | BulkError aborts; count then checked | must-succeed (on trial) |
| exp3.py:68 | `req("POST", "/%s/_refresh" % ix)` | no (recorded call) | no | the subsequent count/search shows the effect; fixtures re-checked by source_count | helper: recorded |
| exp3.py:69 | `c, j, _ = req("GET", "/%s/_count" % ix)` | yes | integer count, shards (source_count) | count() returns a status dict; source_count raises | helper: recorded (count) or must-succeed (source_count) |
| exp3.py:77 | `c, j, _ = req("GET", "/%s/_doc/%s?_source_includes=_inference_fields" % (ix, i))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:93 | `c, j, _ = req("GET", "/%s/_doc/%d" % (ix, i))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:106 | `c, j, _ = req("POST", "/%s/_search" % ix, dict(body, size=50, track_total_hits=True))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:110 | `c, j, _ = req("POST", "/%s/_search" % ix, {"query": {"match": {"title": "paris"}}, "size":` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:114 | `c, j, _ = req("POST", "/%s/_search" % ix, {"knn": {"field": "v", "query_vector": embed(tit` | status checked inside the expression | hit ids | a failure counts as a miss, which lowers the rate and fails the gate (fails safe) | measurement |
| exp3.py:121 | `c, j, _ = req("GET", "/%s/_doc/%d?_source_includes=v&_source_exclude_vectors=false" % (ix,` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:138 | `c, j, _ = req("GET", p)` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:154 | `c, j, _ = req("PUT", "/%s/_doc/1?refresh=true" % ix, d)` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:155 | `rec("overwrite_full_doc", c, j, lambda: {"tag": ntag(1), "embedding_present": emb(1)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:157 | `c, j, _ = req("POST", "/%s/_update/2?refresh=true" % ix, {"doc": {"tag": "partial"}})` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:158 | `rec("update_partial_api", c, j, lambda: {"tag": ntag(2), "embedding_present": emb(2)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:160 | `c, s = bulk(ix, [("3", {"tag": "bulkpartial"})], op="update")` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:163 | `c, j, _ = req("POST", "/%s/_update/4?refresh=true" % ix, {"script": {"source": "ctx._sourc` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:164 | `rec("update_scripted", c, j, lambda: {"tag": ntag(4), "embedding_present": emb(4)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:166 | `c, j, _ = req("DELETE", "/%s/_doc/5?refresh=true" % ix)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:167 | `rec("delete_one", c, j, lambda: {"get_after_status": get(5)[0]})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:169 | `c, j, _ = req("POST", "/%s/_update_by_query?refresh=true&conflicts=proceed" % ix,` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:171 | `rec("update_by_query", c, j, lambda: {"updated": j.get("updated"), "failures": len(j.get("` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:175 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:176 | `cdst, _jd, _ = req("PUT", "/" + dst, mapping(kind)) # recorded below as dst_create (a refu` | recorded (dst_create) | no | recorded | probe |
| exp3.py:177 | `c, j, _ = req("POST", "/_reindex?refresh=true", {"source": {"index": ix}, "dest": {"index"` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:178 | `rec("reindex_same_mapping", c, j, lambda: {"dst_create": cdst, "dst_count": count(dst), "f` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:180 | `"dst_vectors_present": None if kind == "sem" else sum(1 for q in (0, 10, 20, 30) if len((r` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:181 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:183 | `c, j, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % ix, timeout=300)` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:184 | `rec("force_merge", c, j, lambda: {"count_after": count(ix)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:187 | `c, j, _ = req("PUT", "/%s/_doc/9999?refresh=true" % ix, d)` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:188 | `rec("index_new_doc", c, j, lambda: {"count": count(ix)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:204 | `c, j, _ = req("POST", "/%s/_refresh" % ix)` | yes | hits, _shards, timed_out, ids | ScrollError | must-succeed |
| exp3.py:214 | `c, j, _ = req("GET", "/%s/_count" % ix)` | yes | integer count, shards (source_count) | count() returns a status dict; source_count raises | helper: recorded (count) or must-succeed (source_count) |
| exp3.py:234 | `c, j, _ = req("POST", path, body, timeout=page_timeout)` | yes | hits, _shards, timed_out, ids | ScrollError | must-succeed |
| exp3.py:247 | `c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=page_` | yes | hits, _shards, timed_out, ids | ScrollError | must-succeed |
| exp3.py:252 | `req("DELETE", "/_search/scroll", {"scroll_id": sid})` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:260 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:261 | `c, j, _ = req("PUT", "/" + dst, {"settings": {"number_of_shards": 1, "number_of_replicas":` | yes (assert c == 200) | no | AssertionError aborts | must-succeed |
| exp3.py:275 | `bulk_strict(dst, items[k:k + 100], refresh="false")` | yes | one result per operation | BulkError aborts the migration | must-succeed |
| exp3.py:293 | `c, j, _ = req("POST", "/%s/_search" % dst, {"knn": {"field": "v", "query_vector": src_docs` | status checked inside the expression | hit ids | a failure counts as a miss, which lowers the rate and fails the gate (fails safe) | measurement |
| exp3.py:302 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:303 | `c, j, _ = req("PUT", "/" + dst, {"settings": {"number_of_shards": 1, "number_of_replicas":` | yes (assert c == 200) | no | AssertionError aborts | must-succeed |
| exp3.py:320 | `bulk_strict(dst, items[k:k + 100], refresh="false")` | yes | one result per operation | BulkError aborts the migration | must-succeed |
| exp3.py:337 | `cc, jj, _ = req("POST", "/%s/_search" % dst, {"knn": {"field": "v", "query_vector": docs[i` | status checked inside the expression | hit ids | a failure counts as a miss, which lowers the rate and fails the gate (fails safe) | measurement |
| exp3.py:352 | `req("DELETE", "/" + ix)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:355 | `c, j, _ = req("PUT", "/" + ix, m)` | yes (new) | no | ScrollError aborts | must-succeed (on trial) |
| exp3.py:358 | `cb, sb = bulk_strict(ix, [(str(i), doc("sem", i)) for i in range(20)], refresh="false")` | yes | one result per operation | BulkError aborts; count then checked | must-succeed (on trial) |
| exp3.py:365 | `cv, jv, _ = req("POST", "/%s/_search" % ix, {"size": 0, "track_total_hits": True, "query":` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:367 | `c, s = bulk(ix, [(str(i), {"tag": "u"}) for i in range(20)], refresh="false", op="update")` | recorded (c) | items counted | failed counts unanswered operations | probe (partial update on Basic) |
| exp3.py:368 | `req("POST", "/%s/_refresh" % ix)` | no (recorded call) | no | the subsequent count/search shows the effect; fixtures re-checked by source_count | helper: recorded |
| exp3.py:371 | `cc, j, _ = req("GET", "/%s/_doc/%d" % (ix, i))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:374 | `cs, js, _ = req("POST", "/%s/_search" % ix, {"query": {"semantic": {"field": "body", "quer` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:385 | `req("DELETE", "/" + SEMONLY)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:386 | `c, j, _ = req("PUT", "/" + SEMONLY, {"settings": {"number_of_shards": 1, "number_of_replic` | yes (new) | no | ScrollError aborts | must-succeed (on trial) |
| exp3.py:390 | `cb, sb = bulk_strict(SEMONLY, [(str(i), {"body": "doc %d about %s" % (i, ["paris", "berlin` | yes | one result per operation | BulkError aborts; count then checked | must-succeed (on trial) |
| exp3.py:412 | `c, j, _ = req(m, path, b)` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| oracle.py:12 | `c1, j1, _ = req("POST", "/%s/_search" % index, {"size": window, "query": {"bool": {"must":` | yes (both 200 or RuntimeError) | hit ids | RuntimeError aborts | must-succeed (oracle legs) |
| oracle.py:13 | `c2, j2, _ = req("POST", "/%s/_search" % index, {"size": window, "knn": {"field": FIELD_V, ` | yes (both 200 or RuntimeError) | hit ids | RuntimeError aborts | must-succeed (oracle legs) |
| oracle.py:58 | `c, j, _ = req("POST", "/%s/_search" % index, {"size": k, "retriever": r})` | yes (both 200 or RuntimeError) | hit ids | RuntimeError aborts | must-succeed (oracle legs) |
| life.py:35 | `c, j, _ = req("GET", "/_cluster/health?wait_for_status=yellow&timeout=90s", timeout=100)` | wait_health loops until 200; first one recorded | status field | recorded; readiness only | readiness |
| life.py:43 | `c, j, _ = req("GET", "/_cluster/health?wait_for_status=yellow&timeout=10s", timeout=20)` | wait_health loops until 200; first one recorded | status field | recorded; readiness only | readiness |
| life.py:52 | `with urllib.request.urlopen("http://172.17.0.1:19271/", timeout=3) as r:` | exception caught by caller | no | reported not ready | readiness (stub) |
| life.py:60 | `c, j, ms = req("POST", "/_license/start_trial?acknowledge=true", timeout=60)` | recorded (c) | body recorded | recorded; later steps depend on the licence state read back from lib.licence() | probe (the licence transition itself) |
| life.py:66 | `c0, j0, _ = req("POST", "/_license/start_basic")` | recorded (c) | body recorded | recorded; later steps depend on the licence state read back from lib.licence() | probe (the licence transition itself) |
| life.py:68 | `c, j, ms = req("POST", "/_license/start_basic?acknowledge=true", timeout=60)` | recorded (c) | body recorded | recorded; later steps depend on the licence state read back from lib.licence() | probe (the licence transition itself) |
| life.py:179 | `c, j, _ = req("POST", "/%s/_update/1?refresh=true" % nm, {"doc": {"tag": "m"}}); r["update` | recorded (status stored) | no | stored status is checked by summarize (migrated_ops_ok: all < 300) | probe (ops on migrated indices) |
| life.py:180 | `c, j, _ = req("DELETE", "/%s/_doc/2?refresh=true" % nm); r["delete"] = c` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| life.py:181 | `c, j, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % nm); r["force_merge"] = c` | recorded (status stored) | no | stored status is checked by summarize (migrated_ops_ok: all < 300) | probe (ops on migrated indices) |
| life.py:182 | `c, j, _ = req("PUT", "/%s/_doc/new1?refresh=true" % nm, {"title": "x", "tag": "n"}); r["in` | recorded (status stored) | no | stored status is checked by summarize (migrated_ops_ok: all < 300) | probe (ops on migrated indices) |
| life.py:199 | `c, j, _ = req("GET", "/_cat/indices/lic3-*?format=json&h=index,health,docs.count,store.siz` | recorded | no | recorded as is | record |
| life.py:202 | `R["trial_status"] = req("GET", "/_license/trial_status")[1]` | recorded | no | recorded as is | record |
| life.py:237 | `c, j, _ = req("PUT", "/_inference/text_embedding/" + matrix.STUB_EP, matrix.stub_endpoint_` | recorded | error recorded | recorded | probe |
| exp4.py:50 | `return s._req("GET", p)` | recorded | no | the probe's own error handling is the thing under test | probe (start-up probe under test) |
| exp4.py:65 | `c, j, _ = req("GET", "/_license", timeout=1.5)` | recorded | no | the probe's own error handling is the thing under test | probe (start-up probe under test) |

Not listed above because they are not `req` calls: the workload readers and writers in `exp2.py` go through the memcore adapter (`a_auto`, `a_lin`, `a_ctl`, `a_brk`, `a_plain`), which raises on any HTTP error or `errors:true`; each exception is recorded as a failed row with its error text and never as a success. The adapter `bulk_add` does not check that every item answered, so `exp2.setup` now establishes the static index count before taking oracle legs.

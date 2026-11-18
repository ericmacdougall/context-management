# Elasticsearch call sites under licence/ (audit after final-gate review-3 F3; matrix rows regenerated after gate review 4 F3)

Kinds: **must-succeed** (a fixture or precondition: status and body are checked and failure aborts), **probe** (the licence-gated or error response is the observation: status and class are recorded), **cleanup** (DELETE, 404 expected), **verification read**, **helper**. Scope: every `req(`, `bulk(`, `bulk_strict(`, `bulk_add(` call and the `S(`/`Q(`/`rec(` wrappers in `licence/*.py`; shell scripts only start a container and write a curl config (no Elasticsearch response is read).

| file:line | call | HTTP status checked | body validated | on failure | kind |
|---|---|---|---|---|---|
| lib.py:49 | `c, j, _ = req("GET", "/_license")` | yes (c != 200 handled by caller) | partly | returns the error body; recorded | probe |
| lib.py:98 | `v = req("GET", "/")[1].get("version", {}).get("number")` | no | version field only | version None in the stamp (metadata, not a measurement) | stamp |
| lib.py:118 | `return req("POST", "/_bulk?refresh=" + refresh, ndjson=nd, timeout=120)` | returned to caller | n/a | see bulk and bulk_strict | helper |
| matrix.py:171 | `req("DELETE", "/" + idx)` | no | no | ignored (404 expected); every fixture or prerequisite that follows is checked | cleanup |
| matrix.py:179 | `c, j, _ = req("POST", "/%s/_search" % idx, body, **kw)` | returned to caller | hit ids / ES\|QL values | the caller emits the cell; cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | helper |
| matrix.py:193 | `c, j, _ = req("PUT", "/" + idx, body)` | yes (create 200 acknowledged; bulk_strict; refresh 200, 0 shard failures; _count exact; spot read where given) | yes | FixtureError. Run-scope fixture (base, fm): the run aborts and no cell is emitted. Cell-scope prerequisite: recorded as not established and every dependent cell is UNVERIFIED_PREREQUISITE (see the prerequisite table) | must-succeed / prerequisite check |
| matrix.py:197 | `bulk_strict(idx, docs, refresh="false")` | yes (create 200 acknowledged; bulk_strict; refresh 200, 0 shard failures; _count exact; spot read where given) | yes | FixtureError. Run-scope fixture (base, fm): the run aborts and no cell is emitted. Cell-scope prerequisite: recorded as not established and every dependent cell is UNVERIFIED_PREREQUISITE (see the prerequisite table) | must-succeed / prerequisite check |
| matrix.py:204 | `c, j, _ = req("POST", "/%s/_refresh" % idx)` | yes (create 200 acknowledged; bulk_strict; refresh 200, 0 shard failures; _count exact; spot read where given) | yes | FixtureError. Run-scope fixture (base, fm): the run aborts and no cell is emitted. Cell-scope prerequisite: recorded as not established and every dependent cell is UNVERIFIED_PREREQUISITE (see the prerequisite table) | must-succeed / prerequisite check |
| matrix.py:208 | `c, j, _ = req("GET", "/%s/_count" % idx)` | yes (create 200 acknowledged; bulk_strict; refresh 200, 0 shard failures; _count exact; spot read where given) | yes | FixtureError. Run-scope fixture (base, fm): the run aborts and no cell is emitted. Cell-scope prerequisite: recorded as not established and every dependent cell is UNVERIFIED_PREREQUISITE (see the prerequisite table) | must-succeed / prerequisite check |
| matrix.py:212 | `c, j, _ = req("GET", "/%s/_doc/%s" % (idx, spot[0]))` | yes (create 200 acknowledged; bulk_strict; refresh 200, 0 shard failures; _count exact; spot read where given) | yes | FixtureError. Run-scope fixture (base, fm): the run aborts and no cell is emitted. Cell-scope prerequisite: recorded as not established and every dependent cell is UNVERIFIED_PREREQUISITE (see the prerequisite table) | must-succeed / prerequisite check |
| matrix.py:258 | `c, j, _ = req("GET", "/_security/user/" + DLS_USER)` | yes (HTTP 200) | no | FixtureError, recorded as dls_principal not established: dls_search_as_user / dls_search are UNVERIFIED_PREREQUISITE (a 401 for a missing user is not a DLS refusal) | prerequisite check |
| matrix.py:273 | `cr, jr, _ = req("PUT", "/_security/role/" + DLS_ROLE, {"indices": [{"names": [BASE_IDX], "` | recorded | error body recorded | role: the dls_role_create cell; user: not a verdict, the user is established separately (check_dls_principal) | probe |
| matrix.py:274 | `cu, ju, _ = req("PUT", "/_security/user/" + DLS_USER, {"password": pw, "roles": [DLS_ROLE]` | recorded | error body recorded | role: the dls_role_create cell; user: not a verdict, the user is established separately (check_dls_principal) | probe |
| matrix.py:285 | `c, j, _ = req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}, "size": 20}, ` | recorded | hit ids | returned to the caller; cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness (dls_search, dls_search_as_user) | probe |
| matrix.py:301 | `rec("get_allowed_doc0", *req("GET", "/%s/_doc/0" % BASE_IDX, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:302 | `rec("get_hidden_doc1", *req("GET", "/%s/_doc/1" % BASE_IDX, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:303 | `rec("search_match_all", *req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:304 | `rec("search_term_hidden", *req("POST", "/%s/_search" % BASE_IDX, {"query": {"term": {"kw":` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:305 | `rec("count", *req("GET", "/%s/_count" % BASE_IDX, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:306 | `rec("mget", *req("POST", "/%s/_mget" % BASE_IDX, {"ids": ["0", "1"]}, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:307 | `rec("msearch", *req("POST", "/_msearch", ndjson='{"index":"%s"}\n{"query":{"match_all":{}}` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:308 | `rec("scroll_open", *req("POST", "/%s/_search?scroll=1m" % BASE_IDX, {"size": 2, "query": {` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:309 | `rec("knn_search", *req("POST", "/%s/_search" % BASE_IDX, {"knn": {"field": "v", "query_vec` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:310 | `rec("termvectors_hidden", *req("GET", "/%s/_termvectors/1?fields=title" % BASE_IDX, **U)[:` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:311 | `rec("wildcard_index", *req("POST", "/lic1-*/_search", {"query": {"match_all": {}}}, **U)[:` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:312 | `rec("esql", *req("POST", "/_query", {"query": "FROM %s \| LIMIT 10" % BASE_IDX}, **U)[:2])` | recorded | leak check on body | status recorded; a 200 is judged by whether a hidden document came back; the shared base is established first (check_shared_base, FixtureError aborts) | probe (DLS read battery) |
| matrix.py:336 | `c, j, ids = S(idx, body, timeout=15)` | recorded | hit ids where the feature returns hits | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness; prerequisites established once before polling (establish_quick) | probe (polled around the transition) |
| matrix.py:340 | `c, j, _ = req("POST", "/_inference/text_embedding/%s" % STUB_EP, {"input": "x"}, timeout=1` | recorded | hit ids where the feature returns hits | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness; prerequisites established once before polling (establish_quick) | probe (polled around the transition) |
| matrix.py:366 | `K.put("bm25_match", *S(base, {"query": {"match": {"title": "paris"}}}))` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:367 | `K.put("dense_knn", *S(base, {"knn": knn["knn"]}))` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:369 | `c1, j1, l1 = S(base, {"query": {"match": {"title": "paris"}}, "size": 5})` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:370 | `c2, j2, l2 = S(base, {"knn": knn["knn"], "size": 5})` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:377 | `K.put("retriever_rrf", *S(base, {"retriever": {"rrf": {"retrievers": [std, knn], "rank_win` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:378 | `K.put("retriever_linear", *S(base, {"retriever": {"linear": {"retrievers": [{"retriever": ` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:380 | `K.put("text_similarity_reranker_gate", *S(base, {"retriever": {"text_similarity_reranker":` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:387 | `cm, jm, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas` | recorded (mapping) | error body recorded | part of the vec_<type> prerequisite (create 2xx) | probe (licence may refuse the mapping) |
| matrix.py:389 | `cb, sb = bulk(idx, [(str(i), {"v": embed("d%d" % i, 64)}) for i in range(VEC_N)])` | recorded (bulk_http) | items counted | INTENTIONAL PROBE (licence may refuse the write): a licence refusal makes the vec cell LICENCE_ERROR; any other shortfall leaves vec_<type> not established (check_vec_fixture: HTTP 200, 40 item results, refresh, _count 40), so vec_<type> and knn_rescore_vector are UNVERIFIED_PREREQUISITE | probe |
| matrix.py:395 | `cs, js, ids = S(idx, q)` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:404 | `c, j, _ = req("POST", "/_query", {"query": q})` | returned to caller | hit ids / ES\|QL values | the caller emits the cell; cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | helper |
| matrix.py:406 | `K.put("esql_match", *Q('FROM %s METADATA _score \| WHERE MATCH(title, "paris") \| SORT _scor` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:409 | `K.put("esql_fork", *Q(fork + " \| KEEP _id, _fork, _score"))` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:410 | `K.put("esql_fuse", *Q(fork + " \| FUSE \| KEEP _id, _score"))` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:412 | `c, j, _ = req("GET", "/_inference")` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:415 | `c, j, _ = req("PUT", "/_inference/text_embedding/" + ep, stub_endpoint_body())` | recorded | error body recorded | inference_create cell; establishes inference_ep_new only on 200 | probe |
| matrix.py:418 | `c2, j2, _ = req("POST", "/_inference/text_embedding/" + ep, {"input": "x"})` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:420 | `req("DELETE", "/_inference/text_embedding/" + ep + "?force=true")` | no | no | ignored (404 expected); every fixture or prerequisite that follows is checked | cleanup |
| matrix.py:421 | `c, j, _ = req("GET", "/_inference/text_embedding/" + STUB_EP)` | recorded | no | establishes stub_endpoint only on 200 (not a cell) | prerequisite check |
| matrix.py:423 | `c2, j2, _ = req("POST", "/_inference/text_embedding/" + STUB_EP, {"input": "x"})` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:428 | `cm, jm, _ = req("PUT", "/" + sem, {"settings": {"number_of_replicas": 0}, "mappings": {"pr` | recorded | error body recorded | semantic_text_mapping cell; establishes sem_mapping only on 2xx | probe |
| matrix.py:431 | `cd, jd, _ = req("PUT", "/%s/_doc/1?refresh=true" % sem, {"body": "paris trip planning"})` | recorded | error body recorded | semantic_text_index cell; establishes sem_doc only on 2xx then _count 1 (check_written) | probe |
| matrix.py:434 | `K.put("semantic_query", *S(sem, {"query": {"semantic": {"field": "body", "query": "paris"}` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:435 | `K.put("match_on_semantic_text", *S(sem, {"query": {"match": {"body": "paris"}}}))` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:450 | `cpc, jpc, _ = req("PUT", "/" + perc, {"mappings": {"properties": {"q": {"type": "percolato` | yes (HTTP 200) | no | FixtureError: the run aborts (run-scope prerequisite) | must-succeed |
| matrix.py:454 | `ct, jt, _ = req("PUT", "/%s/_doc/t1?refresh=true" % perc, {"q": {"match": {"title": "paris` | recorded | error body recorded | establishes perc_doc only on 2xx then _count 1 (check_written); else percolator_lexical is UNVERIFIED_PREREQUISITE | probe |
| matrix.py:456 | `cp, jp, idsp = S(perc, {"query": {"percolate": {"field": "q", "document": {"title": "trip ` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:462 | `cg, jg, _ = req("PUT", "/" + g1, {"aliases": {al: {"is_write_index": True}}, "mappings": {` | yes (HTTP 200) | no | FixtureError: the run aborts (run-scope prerequisite) | must-succeed |
| matrix.py:466 | `ca, ja, _ = req("PUT", "/%s/_doc/1?require_alias=true&refresh=true" % al, {"title": "x"})` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:467 | `cb2, jb, _ = req("PUT", "/%s-notalias/_doc/1?require_alias=true" % P, {"title": "x"})` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:468 | `cm2, jm2, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al, ` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:469 | `cm3, jm3, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al +` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| matrix.py:476 | `cf, jf, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % fm)` | recorded | per cell table | cell decided by verdict(): prerequisites and witness per the cell table below; never AVAILABLE on an unestablished prerequisite or an empty witness | probe |
| exp2.py:100 | `req("DELETE", "/" + idx)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp2.py:101 | `c, j, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas":` | yes (assert c == 200) | no | AssertionError aborts | must-succeed |
| exp2.py:106 | `self.a_plain.bulk_add("static", dict(items[i:i + 500]))` | yes (adapter raises on HTTP error and on errors:true) | errors flag only | exception aborts setup; count then checked | must-succeed |
| exp2.py:108 | `req("DELETE", "/lic-sem2")` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp2.py:109 | `c, j, _ = req("PUT", "/lic-sem2", {"settings": {"number_of_replicas": 0}, "mappings": {"pr` | recorded (events.sem2_create) | error body | recorded | probe (semantic index may be refused) |
| exp2.py:111 | `c, j, _ = req("GET", "/mc-%s-static/_count" % TENANT)` | yes (c == 200 and count == number of documents) | count | AssertionError aborts (new) | must-succeed |
| exp2.py:164 | `self.a_plain.bulk_add("live", batch)` | adapter raises on failure | errors flag only | failure recorded as a failed batch (attempted_failed); acknowledged ids reconciled later | workload write |
| exp2.py:179 | `c, j, _ = req("PUT", "/lic-sem2/_doc/%s" % i, {"body": "alpha bravo %d" % seq}, timeout=20` | recorded per row | error body per row | classified by classify_sem_failure; UNVERIFIED for old lives | workload write (loud failures expected) |
| exp2.py:210 | `cr, jr, _ = req("POST", "/mc-%s-live/_refresh" % TENANT)` | yes (new) | no | AssertionError aborts verification | must-succeed |
| exp2.py:214 | `c, j, _ = req("POST", "/mc-%s-live/_search?scroll=2m" % TENANT, {"size": 5000, "_source": ` | yes (every page HTTP 200, no shard failure, not timed out) | hits | AssertionError aborts verification (gate review 4 follow-up: a short read made present_unacked and failed_ids_present vacuously small) | must-succeed (verification read) |
| exp2.py:223 | `c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)` | yes (every page HTTP 200, no shard failure, not timed out) | hits | AssertionError aborts verification (gate review 4 follow-up: a short read made present_unacked and failed_ids_present vacuously small) | must-succeed (verification read) |
| exp2.py:225 | `req("DELETE", "/_search/scroll", {"scroll_id": sid})` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp2.py:229 | `cr, jr, _ = req("POST", "/lic-sem2/_refresh")` | yes (HTTP 200) | no | AssertionError aborts verification | must-succeed (verification read) |
| exp2.py:230 | `c, j, _ = req("POST", "/lic-sem2/_search", {"size": 10000, "track_total_hits": True, "_sour` | yes (HTTP 200, no shard failure, total equals hits returned) | hit ids | AssertionError aborts verification (an unread or truncated index made sem_failed_present and sem_present_unacked vacuously 0) | must-succeed (verification read) |
| exp3.py:48 | `c, j, _ = req("PUT", "/_inference/text_embedding/" + matrix.STUB_EP, matrix.stub_endpoint_` | yes (new) | no | ScrollError aborts fixtures | must-succeed (on trial) |
| exp3.py:55 | `req("DELETE", "/" + ix)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:56 | `c, j, _ = req("PUT", "/" + ix, mapping(kind))` | yes (new) | no | ScrollError aborts | must-succeed (on trial) |
| exp3.py:59 | `cb, sb = bulk_strict(ix, [(str(i), doc(kind, i)) for i in range(n_docs(kind))])` | yes | one result per operation | BulkError aborts; count then checked | must-succeed (on trial) |
| exp3.py:69 | `req("POST", "/%s/_refresh" % ix)` | no (recorded call) | no | the subsequent count/search shows the effect; fixtures re-checked by source_count | helper: recorded |
| exp3.py:70 | `c, j, _ = req("GET", "/%s/_count" % ix)` | yes | integer count, shards (source_count) | count() returns a status dict; source_count raises | helper: recorded (count) or must-succeed (source_count) |
| exp3.py:78 | `c, j, _ = req("GET", "/%s/_doc/%s?_source_includes=_inference_fields" % (ix, i))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:94 | `c, j, _ = req("GET", "/%s/_doc/%d" % (ix, i))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:107 | `c, j, _ = req("POST", "/%s/_search" % ix, dict(body, size=50, track_total_hits=True))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:111 | `c, j, _ = req("POST", "/%s/_search" % ix, {"query": {"match": {"title": "paris"}}, "size":` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:115 | `c, j, _ = req("POST", "/%s/_search" % ix, {"knn": {"field": "v", "query_vector": embed(tit` | status checked inside the expression | hit ids | a failure counts as a miss, which lowers the rate and fails the gate (fails safe) | measurement |
| exp3.py:122 | `c, j, _ = req("GET", "/%s/_doc/%d?_source_includes=v&_source_exclude_vectors=false" % (ix,` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:139 | `c, j, _ = req("GET", p)` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:155 | `c, j, _ = req("PUT", "/%s/_doc/1?refresh=true" % ix, d)` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:156 | `rec("overwrite_full_doc", c, j, lambda: {"tag": ntag(1), "embedding_present": emb(1)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:158 | `c, j, _ = req("POST", "/%s/_update/2?refresh=true" % ix, {"doc": {"tag": "partial"}})` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:159 | `rec("update_partial_api", c, j, lambda: {"tag": ntag(2), "embedding_present": emb(2)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:161 | `c, s = bulk(ix, [("3", {"tag": "bulkpartial"})], op="update")` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:164 | `c, j, _ = req("POST", "/%s/_update/4?refresh=true" % ix, {"script": {"source": "ctx._sourc` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:165 | `rec("update_scripted", c, j, lambda: {"tag": ntag(4), "embedding_present": emb(4)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:167 | `c, j, _ = req("DELETE", "/%s/_doc/5?refresh=true" % ix)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:168 | `rec("delete_one", c, j, lambda: {"get_after_status": get(5)[0]})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:170 | `c, j, _ = req("POST", "/%s/_update_by_query?refresh=true&conflicts=proceed" % ix,` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:172 | `rec("update_by_query", c, j, lambda: {"updated": j.get("updated"), "failures": len(j.get("` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:176 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:177 | `cdst, _jd, _ = req("PUT", "/" + dst, mapping(kind)) # recorded below as dst_create (a refu` | recorded (dst_create) | no | recorded | probe |
| exp3.py:178 | `c, j, _ = req("POST", "/_reindex?refresh=true", {"source": {"index": ix}, "dest": {"index"` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:179 | `rec("reindex_same_mapping", c, j, lambda: {"dst_create": cdst, "dst_count": count(dst), "f` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:182 | `"dst_vectors_present": None if kind == "sem" else sum(1 for q in (0, 10, 20, 30) if len((r` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:182 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:184 | `c, j, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % ix, timeout=300)` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:185 | `rec("force_merge", c, j, lambda: {"count_after": count(ix)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:188 | `c, j, _ = req("PUT", "/%s/_doc/9999?refresh=true" % ix, d)` | recorded (class/status) | error body recorded | recorded; effect verified by a follow-up read | probe (mutation under Basic: the status and error are the observation) |
| exp3.py:189 | `rec("index_new_doc", c, j, lambda: {"count": count(ix)})` | recorded or counted | varies | recorded | probe/read (recorded) |
| exp3.py:205 | `c, j, _ = req("POST", "/%s/_refresh" % ix)` | yes | hits, _shards, timed_out, ids | ScrollError | must-succeed |
| exp3.py:215 | `c, j, _ = req("GET", "/%s/_count" % ix)` | yes | integer count, shards (source_count) | count() returns a status dict; source_count raises | helper: recorded (count) or must-succeed (source_count) |
| exp3.py:235 | `c, j, _ = req("POST", path, body, timeout=page_timeout)` | yes | hits, _shards, timed_out, ids | ScrollError | must-succeed |
| exp3.py:248 | `c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=page_` | yes | hits, _shards, timed_out, ids | ScrollError | must-succeed |
| exp3.py:253 | `req("DELETE", "/_search/scroll", {"scroll_id": sid})` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:261 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:262 | `c, j, _ = req("PUT", "/" + dst, {"settings": {"number_of_shards": 1, "number_of_replicas":` | yes (assert c == 200) | no | AssertionError aborts | must-succeed |
| exp3.py:276 | `bulk_strict(dst, items[k:k + 100], refresh="false")` | yes | one result per operation | BulkError aborts the migration | must-succeed |
| exp3.py:294 | `c, j, _ = req("POST", "/%s/_search" % dst, {"knn": {"field": "v", "query_vector": src_docs` | status checked inside the expression | hit ids | a failure counts as a miss, which lowers the rate and fails the gate (fails safe) | measurement |
| exp3.py:303 | `req("DELETE", "/" + dst)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:304 | `c, j, _ = req("PUT", "/" + dst, {"settings": {"number_of_shards": 1, "number_of_replicas":` | yes (assert c == 200) | no | AssertionError aborts | must-succeed |
| exp3.py:321 | `bulk_strict(dst, items[k:k + 100], refresh="false")` | yes | one result per operation | BulkError aborts the migration | must-succeed |
| exp3.py:338 | `cc, jj, _ = req("POST", "/%s/_search" % dst, {"knn": {"field": "v", "query_vector": docs[i` | status checked inside the expression | hit ids | a failure counts as a miss, which lowers the rate and fails the gate (fails safe) | measurement |
| exp3.py:353 | `req("DELETE", "/" + ix)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:356 | `c, j, _ = req("PUT", "/" + ix, m)` | yes (new) | no | ScrollError aborts | must-succeed (on trial) |
| exp3.py:359 | `cb, sb = bulk_strict(ix, [(str(i), doc("sem", i)) for i in range(20)], refresh="false")` | yes | one result per operation | BulkError aborts; count then checked | must-succeed (on trial) |
| exp3.py:366 | `cv, jv, _ = req("POST", "/%s/_search" % ix, {"size": 0, "track_total_hits": True, "query":` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:369 | `c, s = bulk(ix, [(str(i), {"tag": "u"}) for i in range(20)], refresh="false", op="update")` | recorded (c) | items counted | failed counts unanswered operations | probe (partial update on Basic) |
| exp3.py:369 | `req("POST", "/%s/_refresh" % ix)` | no (recorded call) | no | the subsequent count/search shows the effect; fixtures re-checked by source_count | helper: recorded |
| exp3.py:372 | `cc, j, _ = req("GET", "/%s/_doc/%d" % (ix, i))` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:375 | `cs, js, _ = req("POST", "/%s/_search" % ix, {"query": {"semantic": {"field": "body", "quer` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| exp3.py:386 | `req("DELETE", "/" + SEMONLY)` | no | no | ignored (404 expected); a stale index would make the next create fail, which is checked where the fixture must succeed | cleanup |
| exp3.py:387 | `c, j, _ = req("PUT", "/" + SEMONLY, {"settings": {"number_of_shards": 1, "number_of_replic` | yes (new) | no | ScrollError aborts | must-succeed (on trial) |
| exp3.py:391 | `cb, sb = bulk_strict(SEMONLY, [(str(i), {"body": "doc %d about %s" % (i, ["paris", "berlin` | yes | one result per operation | BulkError aborts; count then checked | must-succeed (on trial) |
| exp3.py:413 | `c, j, _ = req(m, path, b)` | recorded or counted | varies | non-200 counted as unreadable/missing or recorded as class | probe/read (recorded) |
| oracle.py:12 | `c1, j1, _ = req("POST", "/%s/_search" % index, {"size": window, "query": {"bool": {"must":` | yes (both 200 or RuntimeError) | hit ids | RuntimeError aborts | must-succeed (oracle legs) |
| oracle.py:13 | `c2, j2, _ = req("POST", "/%s/_search" % index, {"size": window, "knn": {"field": FIELD_V, ` | yes (both 200 or RuntimeError) | hit ids | RuntimeError aborts | must-succeed (oracle legs) |
| oracle.py:58 | `c, j, _ = req("POST", "/%s/_search" % index, {"size": k, "retriever": r})` | yes (both 200 or RuntimeError) | hit ids | RuntimeError aborts | must-succeed (oracle legs) |
| life.py:35 | `c, j, _ = req("GET", "/_cluster/health?wait_for_status=yellow&timeout=90s", timeout=100)` | wait_health loops until 200; first one recorded | status field | recorded; readiness only | readiness |
| life.py:43 | `c, j, _ = req("GET", "/_cluster/health?wait_for_status=yellow&timeout=10s", timeout=20)` | wait_health loops until 200; first one recorded | status field | recorded; readiness only | readiness |
| life.py:52 | `with urllib.request.urlopen("http://172.17.0.1:19271/", timeout=3) as r:` | exception caught by caller | no | reported not ready | readiness (stub) |
| life.py:60 | `c, j, ms = req("POST", "/_license/start_trial?acknowledge=true", timeout=60)` | recorded (c) | body recorded | recorded; later steps depend on the licence state read back from lib.licence() | probe (the licence transition itself) |
| life.py:66 | `c0, j0, _ = req("POST", "/_license/start_basic")` | recorded (c) | body recorded | recorded; later steps depend on the licence state read back from lib.licence() | probe (the licence transition itself) |
| life.py:68 | `c, j, ms = req("POST", "/_license/start_basic?acknowledge=true", timeout=60)` | recorded (c) | body recorded | recorded; later steps depend on the licence state read back from lib.licence() | probe (the licence transition itself) |
| life.py:179 | `c, j, _ = req("POST", "/%s/_update/1?refresh=true" % nm, {"doc": {"tag": "m"}}); r["update` | recorded (status stored) | no | stored status is checked by summarize (EXP3-migration `migration_status`: every expected index and op 2xx; missing UNVERIFIED, other FAIL) | probe (ops on migrated indices) |
| life.py:180 | `c, j, _ = req("DELETE", "/%s/_doc/2?refresh=true" % nm); r["delete"] = c` | recorded (status stored as `r["delete"]`) | no | stored status is checked by the EXP3 migration verdict: `MigratedOps.delete` must be SUCCESS (missing UNVERIFIED, other FAIL); review 8 F16 corrected the earlier "ignored cleanup" entry | probe (ops on migrated indices) |
| life.py:181 | `c, j, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % nm); r["force_merge"] = c` | recorded (status stored) | no | stored status is checked by summarize (EXP3-migration `migration_status`: every expected index and op 2xx; missing UNVERIFIED, other FAIL) | probe (ops on migrated indices) |
| life.py:182 | `c, j, _ = req("PUT", "/%s/_doc/new1?refresh=true" % nm, {"title": "x", "tag": "n"}); r["in` | recorded (status stored) | no | stored status is checked by summarize (EXP3-migration `migration_status`: every expected index and op 2xx; missing UNVERIFIED, other FAIL) | probe (ops on migrated indices) |
| life.py:199 | `c, j, _ = req("GET", "/_cat/indices/lic3-*?format=json&h=index,health,docs.count,store.siz` | recorded | no | recorded as is | record |
| life.py:202 | `R["trial_status"] = req("GET", "/_license/trial_status")[1]` | recorded | no | recorded as is | record |
| life.py:237 | `c, j, _ = req("PUT", "/_inference/text_embedding/" + matrix.STUB_EP, matrix.stub_endpoint_` | recorded | error recorded | recorded | probe |
| exp4.py:50 | `return s._req("GET", p)` | recorded | no | the probe's own error handling is the thing under test | probe (start-up probe under test) |
| exp4.py:65 | `c, j, _ = req("GET", "/_license", timeout=1.5)` | recorded | no | the probe's own error handling is the thing under test | probe (start-up probe under test) |

Not listed above because they are not `req` calls: the workload readers and writers in `exp2.py` go through the memcore adapter (`a_auto`, `a_lin`, `a_ctl`, `a_brk`, `a_plain`), which raises on any HTTP error or `errors:true`; each exception is recorded as a failed row with its error text and never as a success. The adapter `bulk_add` does not check that every item answered, so `exp2.setup` now establishes the static index count before taking oracle legs.




## Matrix cells and their prerequisites (generated from `matrix.CELLS` and `matrix.PREREQS`; tested by test_callsites_table_matches_registry)

`verdict()` is the only function that decides a cell. Precedence: an observed licence refusal is kept (in the cell's own probe, or the vec write that is itself the probe); otherwise an unestablished prerequisite gives `UNVERIFIED_PREREQUISITE` with the raw observation nested under `observed`; otherwise a non-2xx class; otherwise a 2xx without the declared witness gives `EMPTY_WITNESS`; otherwise `AVAILABLE`. `Cells.put` refuses an unregistered or duplicate cell and `Cells.done` refuses to return unless every registered cell of the phase was emitted. A witness of `status` means the feature returns no hits, so the status is the whole observation (inference, mapping, write, alias and force-merge cells).

| phase | cell | prerequisites | witness |
|---|---|---|---|
| run | bm25_match | base | hits |
| run | dense_knn | base | hits |
| run | client_side_fusion | base | hits |
| run | retriever_rrf | base | hits |
| run | retriever_linear | base | hits |
| run | text_similarity_reranker_gate | base | hits |
| run | vec_int8_hnsw | vec_int8_hnsw | hits |
| run | knn_rescore_vector | vec_int8_hnsw | hits |
| run | vec_bbq_hnsw | vec_bbq_hnsw | hits |
| run | vec_bbq_disk | vec_bbq_disk | hits |
| run | esql_match | base | values |
| run | esql_fork | base | values |
| run | esql_fuse | base | values |
| run | inference_list | none | status |
| run | inference_create | none | status |
| run | inference_use_new | inference_ep_new | status |
| run | inference_use_existing | stub_endpoint | status |
| run | semantic_text_mapping | none | status |
| run | semantic_text_index | sem_mapping | status |
| run | semantic_query | sem_mapping, sem_doc | hits |
| run | match_on_semantic_text | sem_mapping, sem_doc | hits |
| run | dls_role_create | none | status |
| run | dls_search_as_user | shared_base, dls_principal | hits |
| run | percolator_lexical | perc_index, perc_doc | hits |
| run | alias_require_alias | alias_index | status |
| run | alias_must_exist | alias_index | status |
| run | force_merge | fm | status |
| quick | rrf | shared_base | hits |
| quick | linear | shared_base | hits |
| quick | bm25 | shared_base | hits |
| quick | knn | shared_base | hits |
| quick | semantic_query | none | hits |
| quick | inference_use | none | status |
| quick | dls_search | shared_base, dls_principal | hits |

| prerequisite | scope | established when |
|---|---|---|
| base | run | phase index lic1-<label>-base: create 200 acknowledged, bulk_strict of 8 documents, refresh 200 with 0 shard failures, _count 8, document 0 read back (make_fixture) |
| shared_base | cell | the shared lic1-base written by make_base: refresh 200 with 0 shard failures, _count 8, document 0 read back (check_fixture, read-only) |
| dls_principal | cell | the DLS password file exists and GET /_security/user/lic_dls_user answers 200 |
| vec_int8_hnsw | cell | index create 2xx; the recording bulk of 40 vectors answered HTTP 200 with 40 successful item results; refresh, _count 40 |
| vec_bbq_hnsw | cell | as vec_int8_hnsw, for the bbq_hnsw index |
| vec_bbq_disk | cell | as vec_int8_hnsw, for the bbq_disk index |
| inference_ep_new | cell | PUT of the per-phase endpoint lic-ep-<label> answered 200 |
| stub_endpoint | cell | GET /_inference/text_embedding/lic-stub answered 200 |
| sem_mapping | cell | the semantic_text index lic1-<label>-sem was created (2xx) |
| sem_doc | cell | document 1 written to the semantic_text index (2xx), then refresh, _count 1 |
| perc_index | run | percolator index create 200 |
| perc_doc | cell | percolator query document t1 written (2xx), then refresh, _count 1 |
| alias_index | run | write index lic1-<label>-gen1 with its alias created (200) |
| fm | run | force-merge index: make_fixture with 20 documents, count and document 0 read back |

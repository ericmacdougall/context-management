"""Experiment 1: feature matrix. run(label) probes every feature on the node's CURRENT licence; quick_probes() is the fast subset
polled around start_basic / start_trial. No model runs anywhere: embeddings come from the loopback stub (licence/stub_embed.py).

Every cell this module can emit is declared in CELLS with the prerequisites it depends on and the witness that shows it ran on data.
verdict() is the ONLY place a cell's class is decided; run() and quick_probes() emit cells only through Cells.put (gate review 4, F3)."""
import collections
import json
import os
import secrets
import time

import observe as observe_mod   # the module; `observe` below is this file's sub-observation helper (review 12: it shadowed the module)
from lib import BulkError, bulk, bulk_strict, classify, embed, err, last_response, req

STUB_URL = os.environ.get("LIC_STUB_URL", "http://172.17.0.1:19271/embed")
STUB_EP = "lic-stub"
NOEP = "lic-no-such-endpoint"
DLS_USER = "lic_dls_user"
DLS_ROLE = "lic_dls_role"
DLS_PW = os.path.expanduser("~/emlab-16096/lic/dls_password")
BASE_IDX = "lic1-base"
DOCS = [("paris trip planning", "a"), ("berlin move notes", "b"), ("dentist appointment paris", "a"), ("refund policy", "b"),
        ("moved to berlin", "a"), ("paris berlin train", "b"), ("tax form paris office", "a"), ("book club berlin", "b")]
VEC_TYPES = ("int8_hnsw", "bbq_hnsw", "bbq_disk")
VEC_N = 40

# ---- verdict classes beyond lib.classify's TRANSPORT / AVAILABLE / LICENCE_ERROR / OTHER_ERROR
UNVERIFIED = "UNVERIFIED_PREREQUISITE"   # a prerequisite of the cell was not established: the answer says nothing about the feature
EMPTY = "EMPTY_WITNESS"                   # 2xx, but a feature that returns hits/values returned none: not evidence that it works
NOT_OBSERVED = ("SKIP", UNVERIFIED, EMPTY)   # neither a success nor a refusal (exp2.moment_analysis and recompute.py exclude these)

# ---- prerequisites. scope "run": failure raises FixtureError and the run emits no cell at all.
# scope "cell": failure is recorded in the run's establishment record and every dependent cell becomes UNVERIFIED_PREREQUISITE.
PREREQS = {
    "base": ("run", "phase index lic1-<label>-base: create 200 acknowledged, bulk_strict of 8 documents, refresh 200 with 0 shard failures, _count 8, document 0 read back (make_fixture)"),
    "shared_base": ("cell", "the shared lic1-base written by make_base: refresh 200 with 0 shard failures, _count 8, document 0 read back (check_fixture, read-only)"),
    "dls_principal": ("cell", "the DLS password file exists and GET /_security/user/lic_dls_user answers 200"),
    "vec_int8_hnsw": ("cell", "index create 2xx; the recording bulk of 40 vectors answered HTTP 200 with 40 successful item results; refresh, _count 40"),
    "vec_bbq_hnsw": ("cell", "as vec_int8_hnsw, for the bbq_hnsw index"),
    "vec_bbq_disk": ("cell", "as vec_int8_hnsw, for the bbq_disk index"),
    "inference_ep_new": ("cell", "PUT of the per-phase endpoint lic-ep-<label> answered 200"),
    "stub_endpoint": ("cell", "GET /_inference/text_embedding/lic-stub answered 200"),
    "sem_mapping": ("cell", "the semantic_text index lic1-<label>-sem was created (2xx)"),
    "sem_doc": ("cell", "document 1 written to the semantic_text index (2xx), then refresh, _count 1"),
    "perc_index": ("run", "percolator index create 200"),
    "perc_doc": ("cell", "percolator query document t1 written (2xx), then refresh, _count 1"),
    "alias_index": ("run", "write index lic1-<label>-gen1 with its alias created (200)"),
    "fm": ("run", "force-merge index: make_fixture with 20 documents, count and document 0 read back"),
}

# witness kinds: "hits" (non-empty list of hit ids), "values" (non-empty ES|QL values), "status" (the feature returns no hits; the status is the observation)
Spec = collections.namedtuple("Spec", "prereqs witness")
CELLS = {
    ("run", "bm25_match"): Spec(("base",), "hits"),
    ("run", "dense_knn"): Spec(("base",), "hits"),
    ("run", "client_side_fusion"): Spec(("base",), "hits"),
    ("run", "retriever_rrf"): Spec(("base",), "hits"),
    ("run", "retriever_linear"): Spec(("base",), "hits"),
    ("run", "text_similarity_reranker_gate"): Spec(("base",), "hits"),
    ("run", "vec_int8_hnsw"): Spec(("vec_int8_hnsw",), "hits"),
    ("run", "knn_rescore_vector"): Spec(("vec_int8_hnsw",), "hits"),
    ("run", "vec_bbq_hnsw"): Spec(("vec_bbq_hnsw",), "hits"),
    ("run", "vec_bbq_disk"): Spec(("vec_bbq_disk",), "hits"),
    ("run", "esql_match"): Spec(("base",), "values"),
    ("run", "esql_fork"): Spec(("base",), "values"),
    ("run", "esql_fuse"): Spec(("base",), "values"),
    ("run", "inference_list"): Spec((), "status"),
    ("run", "inference_create"): Spec((), "status"),
    ("run", "inference_use_new"): Spec(("inference_ep_new",), "status"),
    ("run", "inference_use_existing"): Spec(("stub_endpoint",), "status"),
    ("run", "semantic_text_mapping"): Spec((), "status"),
    ("run", "semantic_text_index"): Spec(("sem_mapping",), "status"),
    ("run", "semantic_query"): Spec(("sem_mapping", "sem_doc"), "hits"),
    ("run", "match_on_semantic_text"): Spec(("sem_mapping", "sem_doc"), "hits"),
    ("run", "dls_role_create"): Spec((), "status"),
    ("run", "dls_search_as_user"): Spec(("shared_base", "dls_principal"), "hits"),
    ("run", "percolator_lexical"): Spec(("perc_index", "perc_doc"), "hits"),
    ("run", "alias_require_alias"): Spec(("alias_index",), "status"),
    ("run", "alias_must_exist"): Spec(("alias_index",), "status"),
    ("run", "force_merge"): Spec(("fm",), "status"),
    # quick probes, polled around a transition. semantic_query and inference_use declare no prerequisite: the polled index and endpoint may only
    # come into existence during the transition (up direction), so a hit list (semantic) or a 200 (inference) is itself the evidence.
    ("quick", "rrf"): Spec(("shared_base",), "hits"),
    ("quick", "linear"): Spec(("shared_base",), "hits"),
    ("quick", "bm25"): Spec(("shared_base",), "hits"),
    ("quick", "knn"): Spec(("shared_base",), "hits"),
    ("quick", "semantic_query"): Spec((), "hits"),
    ("quick", "inference_use"): Spec((), "status"),
    ("quick", "dls_search"): Spec(("shared_base", "dls_principal"), "hits"),
}


class FixtureError(RuntimeError):
    """A fixture that was expected to hold documents does not. Every measurement taken on it would be about an empty or partial index: abort."""

    def __init__(self, msg, observed=None):
        super().__init__(msg)
        self.observed = observed   # lib.classify of the failing response, when there was one (keeps a licence refusal visible)


def observe(c, j):
    """A sub-observation inside a cell (a write or a mapping call). Not a verdict: verdict() alone decides a cell's class."""
    o = {"class": classify(c, j), "status": c}
    if o["class"] != "AVAILABLE":
        o["error"] = err(j)
    return o


def verdict(phase, name, est, status, body=None, witness=None, extra=None, refused=False):
    """The one function that decides a cell. est: prerequisite -> True (established) or a failure dict; an absent key is not established.
    Precedence: an observed licence refusal (in this cell's own probe, or `refused` for a write that is itself the probe) is kept;
    else any unestablished prerequisite -> UNVERIFIED_PREREQUISITE with the raw observation nested; else a non-2xx class;
    else a 2xx without the declared witness -> EMPTY_WITNESS; else AVAILABLE."""
    spec = CELLS[(phase, name)]
    observed = "NOT_RUN" if status is None else classify(status, body)
    missing = {p: (est.get(p) or {"failure": "not attempted"}) for p in spec.prereqs if est.get(p) is not True}
    out = dict(extra or {})
    if observed == "LICENCE_ERROR" or refused:
        out.update({"class": "LICENCE_ERROR", "status": status, "error": err(body)})
        if refused and observed != "LICENCE_ERROR":
            out["refused_write"] = True   # the probe write was refused by the licence; `status` is the later search's
        if missing:
            out["unestablished"] = missing
    elif missing:
        if status is None and not spec.prereqs:
            raise ValueError("cell %s/%s was not run and has no prerequisite" % (phase, name))
        p = sorted(missing)[0]
        out.update({"class": UNVERIFIED, "status": status, "prerequisite": p, "prerequisite_failure": missing[p], "unestablished": missing,
                    "observed": {"class": observed, "status": status, "error": err(body) if observed != "AVAILABLE" else None}})
    elif status is None:
        raise ValueError("cell %s/%s was not run although its prerequisites were established" % (phase, name))
    elif observed != "AVAILABLE":
        out.update({"class": observed, "status": status, "error": err(body)})
    elif spec.witness != "status" and not witness:
        out.update({"class": EMPTY, "status": status, "witness": witness})
    else:
        out.update({"class": "AVAILABLE", "status": status})
        if witness is not None:
            out["witness"] = witness
    code, raw = last_response()
    if status is not None and status >= 400 and code == status and _same_response(raw, body):   # EXP1-T3: what the adapter would see
        out["adapter_message"] = observe_mod.adapter_message(status, raw)
        out["adapter_detects"] = observe_mod.adapter_detects(out["adapter_message"])
    return out


def _same_response(raw, body):
    """Review 12 (F29's class): the thread's last raw response belongs to this cell only if it decodes to this cell's body; a matching
    status alone could attach another request's bytes (client_side_fusion is judged on one of two requests)."""
    try:
        if isinstance(body, dict) and set(body) == {"_raw"}:   # lib.req keeps the first 300 bytes of a non-JSON body
            return (raw or b"")[:300].decode("utf-8", "replace") == body["_raw"]
        return json.loads((raw or b"").decode("utf-8", "replace")) == body
    except Exception:
        return False


class Cells:
    """Emitter for one phase. put() refuses unregistered and duplicate cells; done() refuses to return unless every registered cell was emitted."""

    def __init__(self, phase, est):
        self.phase, self.est, self.out = phase, est, {}

    def put(self, name, status, body=None, witness=None, extra=None, refused=False):
        if (self.phase, name) not in CELLS or name in self.out:
            raise KeyError("cell %s/%s is unregistered or emitted twice" % (self.phase, name))
        self.out[name] = verdict(self.phase, name, self.est, status, body, witness, extra, refused)
        return self.out[name]

    def done(self):
        missing = sorted(n for p, n in CELLS if p == self.phase and n not in self.out)
        if missing:
            raise RuntimeError("cells not emitted in phase %s: %s" % (self.phase, missing))
        return self.out


def establish(fn, *a, **kw):
    """Run a prerequisite check: True when it holds, else a failure record (never raises FixtureError)."""
    try:
        fn(*a, **kw)
        return True
    except FixtureError as e:
        return {"failure": str(e)[:240], "observed": e.observed}


def drop(idx):
    req("DELETE", "/" + idx)


def hits(j):
    return [h["_id"] for h in j.get("hits", {}).get("hits", [])] if isinstance(j, dict) else []


def S(idx, body, **kw):
    c, j, _ = req("POST", "/%s/_search" % idx, body, **kw)
    return c, j, (hits(j) if c == 200 else None)


def stub_endpoint_body():
    return {"service": "custom", "service_settings": {"url": STUB_URL, "headers": {"Content-Type": "application/json"},
            "request": "{\"input\": ${input}}", "response": {"json_parser": {"text_embeddings": "$.data[*].embedding[*]"}},
            "dimensions": 8, "similarity": "cosine"}}


def make_fixture(idx, body, docs, spot=None):
    """Create `idx` and fill it with `docs` (list of (id, source)); MUST succeed. Then establish the contents before anything is measured:
    refresh answered 200 with zero shard failures, `_count` equals len(docs), and (if given) document `spot[0]` has the expected `_source` fields."""
    drop(idx)
    c, j, _ = req("PUT", "/" + idx, body)
    if c != 200 or not (isinstance(j, dict) and j.get("acknowledged")):
        raise FixtureError("create %s: HTTP %s %s" % (idx, c, json.dumps(err(j))[:160]), classify(c, j))
    try:
        bulk_strict(idx, docs, refresh="false")
    except BulkError as e:
        raise FixtureError("fill %s: %s" % (idx, e))
    check_fixture(idx, len(docs), spot)


def check_fixture(idx, n, spot=None):
    c, j, _ = req("POST", "/%s/_refresh" % idx)
    sh = j.get("_shards") if isinstance(j, dict) else None
    if c != 200 or not isinstance(sh, dict) or sh.get("failed") != 0:
        raise FixtureError("refresh %s: HTTP %s shards %s" % (idx, c, sh), classify(c, j))
    c, j, _ = req("GET", "/%s/_count" % idx)
    if c != 200 or not isinstance(j, dict) or j.get("count") != n or (j.get("_shards") or {}).get("failed") != 0:
        raise FixtureError("count %s: HTTP %s count %s, expected %d" % (idx, c, j.get("count") if isinstance(j, dict) else None, n), classify(c, j))
    if spot:
        c, j, _ = req("GET", "/%s/_doc/%s" % (idx, spot[0]))
        src = (j.get("_source") or {}) if isinstance(j, dict) else {}
        if c != 200 or any(src.get(k) != v for k, v in spot[1].items()):
            raise FixtureError("spot check %s/%s: HTTP %s" % (idx, spot[0], c), classify(c, j))


def check_written(idx, c, j):
    """A single-document write that a dependent cell relies on: 2xx, then refresh and _count 1."""
    if not 200 <= c < 300:
        raise FixtureError("write into %s: HTTP %s %s" % (idx, c, json.dumps(err(j))[:160]), classify(c, j))
    check_fixture(idx, 1)


def check_vec_fixture(idx, cm, jm, cb, sb):
    if not 200 <= cm < 300:
        raise FixtureError("create %s: HTTP %s" % (idx, cm), classify(cm, jm))
    if cb != 200 or sb["failed"] != 0 or sb["items"] != VEC_N:
        raise FixtureError("fill %s: HTTP %s, %s item results, %s failed" % (idx, cb, sb["items"], sb["failed"]))
    check_fixture(idx, VEC_N)


def base_mapping(tenant):
    props = {"title": {"type": "text"}, "kw": {"type": "keyword"}, "v": {"type": "dense_vector", "dims": 8, "index": True, "similarity": "cosine"}}
    if tenant:
        props["tenant"] = {"type": "keyword"}
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": props}}


def base_docs(tenant):
    return [(str(i), dict({"title": t, "kw": k, "v": embed(t)}, **({"tenant": "t"} if tenant else {}))) for i, (t, k) in enumerate(DOCS)]


BASE_SPOT = ("0", {"title": DOCS[0][0], "kw": DOCS[0][1]})


def make_base(idx=BASE_IDX):
    make_fixture(idx, base_mapping(True), base_docs(True), spot=BASE_SPOT)


def check_shared_base():
    check_fixture(BASE_IDX, len(DOCS), BASE_SPOT)


def check_dls_principal():
    if not os.path.exists(DLS_PW):
        raise FixtureError("no DLS password file")
    c, j, _ = req("GET", "/_security/user/" + DLS_USER)
    if c != 200:
        raise FixtureError("DLS user lookup: HTTP %s" % c, classify(c, j))


def _ensure_dls():
    """role + user (created while the licence allows it; kept across the transition). Password in a 0600 file, never printed. -> raw (cr, jr, cu, ju)."""
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
    return cr, jr, cu, ju


def ensure_dls():
    cr, jr, cu, ju = _ensure_dls()
    return {"role": observe(cr, jr), "user": observe(cu, ju)}


def docs_in_payload(j):
    """Review 12 F28: how many documents (or document-derived values) a response body carries, read from the body's SHAPE and never
    from its HTTP status -> int >= 0, or None when the body has no shape this function knows (an unknown body is never "nothing").
    Shapes, one per read path of the DLS battery and the DLS search: search `hits.hits` (and `hits.total`), get `found`/`_source`,
    mget `docs[]`, msearch `responses[]` (each counted the same way), ES|QL `values[]`, termvectors `term_vectors`, `_count` `count`.
    An error body (`error`) with none of them is a known shape carrying 0 documents."""
    if not isinstance(j, dict) or "_raw" in j:
        return None
    n, known = 0, "error" in j
    h = j.get("hits")
    if h is not None:
        if not isinstance(h, dict) or not isinstance(h.get("hits", []), list):
            return None
        tot = h.get("total")
        tot = tot.get("value") if isinstance(tot, dict) else tot
        n += max(len(h.get("hits", [])), tot if type(tot) is int and tot > 0 else 0)
        known = True
    if "found" in j or "_source" in j:
        known = True
        n += 1 if (j.get("found") is True or "_source" in j or "term_vectors" in j) else 0
    elif "term_vectors" in j:
        known, n = True, n + 1
    if "docs" in j:
        if not isinstance(j["docs"], list):
            return None
        known = True
        n += sum(1 for d in j["docs"] if isinstance(d, dict) and (d.get("found") is True or "_source" in d))
    if "responses" in j:
        if not isinstance(j["responses"], list):
            return None
        known = True
        for r in j["responses"]:
            k = docs_in_payload(r)
            if k is None:
                return None
            n += k
    if "values" in j:
        if not isinstance(j["values"], list):
            return None
        known, n = True, n + len(j["values"])
    if "count" in j:
        if type(j["count"]) is not int or j["count"] < 0:
            return None
        known, n = True, n + j["count"]
    return n if known else None


def dls_search():
    """-> (status, body, sorted ids or None, extra fields). The caller emits the cell. `returned` (the 200-only id count) is kept as
    recorded; `docs_in_payload` is the status-independent witness of THRESHOLDS EXP1-T4 "returning any document is a FAIL" (review 12)."""
    c, j, _ = req("POST", "/%s/_search" % BASE_IDX, {"query": {"match_all": {}}, "size": 20}, user=DLS_USER, pwfile=DLS_PW)
    ids = sorted(hits(j)) if c == 200 else None
    extra = {"only_kw_a": ids == ["0", "2", "4", "6"], "returned": len(ids)} if c == 200 else {"returned": None}
    extra["docs_in_payload"] = docs_in_payload(j)
    return c, j, ids, extra


def dls_battery():
    """Every read path as the DLS-restricted user. Allowed doc: id 0 (kw a). Hidden doc: id 1 (kw b). Returns status plus whether any document body came back.
    The shared base must hold its 8 documents first (FixtureError otherwise): a 'no hidden document returned' on an empty index proves nothing."""
    check_shared_base()
    U = dict(user=DLS_USER, pwfile=DLS_PW)
    out = {}

    def rec(name, c, j):
        body = json.dumps(j)
        out[name] = {"status": c, "returned_hidden_doc": ('"kw":"b"' in body.replace(" ", "")) or ("berlin move notes" in body),
                     "returned_any_source": '"_source"' in body and c < 300,   # legacy (2xx only); kept as recorded
                     "docs_in_payload": docs_in_payload(j)}                    # review 12 F28: by shape, whatever the status
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


def establish_quick():
    """Prerequisites of the quick probes, checked ONCE before polling (no extra requests inside the 50 ms loop)."""
    return {"shared_base": establish(check_shared_base), "dls_principal": establish(check_dls_principal)}


def quick_probes(sem_idx="lic-sem", est=None):
    """Fast subset. Rows keep the recorded shape: name -> [class, status] ([class, status, returned, docs_in_payload] for dls_search;
    rows recorded before review 12 have no fourth element), plus _times and _t."""
    est = establish_quick() if est is None else est
    K = Cells("quick", est)
    times = {}
    std = {"standard": {"query": {"match": {"title": "paris"}}}}
    knn = {"knn": {"field": "v", "query_vector": embed("paris trip planning"), "k": 3, "num_candidates": 10}}
    t = time.time()
    for name, idx, body in (("rrf", BASE_IDX, {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window_size": 10}}}),
                            ("linear", BASE_IDX, {"retriever": {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"},
                                                                                       {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}], "rank_window_size": 10}}}),
                            ("bm25", BASE_IDX, {"query": {"match": {"title": "paris"}}}),
                            ("knn", BASE_IDX, {"knn": knn["knn"]}),
                            ("semantic_query", sem_idx, {"query": {"semantic": {"field": "body", "query": "paris"}}})):
        t0 = time.time()
        c, j, ids = S(idx, body, timeout=15)
        K.put(name, c, j, ids)
        times[name] = [t0, time.time()]
    t0 = time.time()
    c, j, _ = req("POST", "/_inference/text_embedding/%s" % STUB_EP, {"input": "x"}, timeout=15)
    K.put("inference_use", c, j)
    times["inference_use"] = [t0, time.time()]
    t0 = time.time()
    if est.get("dls_principal") is True:
        c, j, ids, extra = dls_search()
        K.put("dls_search", c, j, ids, extra)
    else:   # no password file or no user: the probe cannot be sent
        K.put("dls_search", None, extra={"returned": None, "docs_in_payload": None})
    times["dls_search"] = [t0, time.time()]
    cells = K.done()
    rows = {n: [v["class"], v["status"]] + ([v.get("returned"), v.get("docs_in_payload")] if n == "dls_search" else []) for n, v in cells.items()}
    return dict(rows, _times=times, _t=t)


def run(label):
    """Full matrix on the current licence. Index names carry the label so phases never share state."""
    P = "lic1-%s-" % label
    est = {}
    K = Cells("run", est)
    base = P + "base"
    make_fixture(base, base_mapping(False), base_docs(False), spot=BASE_SPOT)   # must succeed (run-scope prerequisite)
    est["base"] = True
    qv = embed("paris trip planning")
    std = {"standard": {"query": {"match": {"title": "paris"}}}}
    knn = {"knn": {"field": "v", "query_vector": qv, "k": 3, "num_candidates": 10}}
    K.put("bm25_match", *S(base, {"query": {"match": {"title": "paris"}}}))
    K.put("dense_knn", *S(base, {"knn": knn["knn"]}))
    # client-side fusion: both legs must answer; the fused list is the witness
    c1, j1, l1 = S(base, {"query": {"match": {"title": "paris"}}, "size": 5})
    c2, j2, l2 = S(base, {"knn": knn["knn"], "size": 5})
    f = {}
    for lst in (l1 or [], l2 or []):
        for r, d in enumerate(lst):
            f[d] = f.get(d, 0) + 1.0 / (61 + r)
    worst = (c1, j1) if c1 != 200 else (c2, j2)
    K.put("client_side_fusion", worst[0], worst[1], sorted(f, key=lambda d: -f[d]), extra={"leg_status": [c1, c2]})
    K.put("retriever_rrf", *S(base, {"retriever": {"rrf": {"retrievers": [std, knn], "rank_window_size": 10, "rank_constant": 60}}}))
    K.put("retriever_linear", *S(base, {"retriever": {"linear": {"retrievers": [{"retriever": std, "weight": 1.0, "normalizer": "minmax"},
                                                                                 {"retriever": knn, "weight": 1.0, "normalizer": "minmax"}], "rank_window_size": 10}}}))
    K.put("text_similarity_reranker_gate", *S(base, {"retriever": {"text_similarity_reranker": {"retriever": std, "field": "title", "inference_id": NOEP,
                                                                                               "inference_text": "paris", "rank_window_size": 5}}}))
    # vector types. INTENTIONAL PROBE: the 40-vector write may be refused by the licence (bbq_disk on Basic); the refusal is the observation, so the
    # recording helper is used, and population is then established separately (count 40) before any search is believed.
    for typ in VEC_TYPES:
        idx = P + typ
        drop(idx)
        cm, jm, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
            "v": {"type": "dense_vector", "dims": 64, "index": True, "similarity": "cosine", "index_options": {"type": typ}}}}})
        cb, sb = bulk(idx, [(str(i), {"v": embed("d%d" % i, 64)}) for i in range(VEC_N)])
        est["vec_" + typ] = establish(check_vec_fixture, idx, cm, jm, cb, sb)
        refused = classify(cm, jm) == "LICENCE_ERROR" or bool(sb["failed"] and "license" in json.dumps(sb["first_error"]).lower())
        q = {"knn": {"field": "v", "query_vector": embed("d7", 64), "k": 3, "num_candidates": 20}}
        if typ == "int8_hnsw":
            q["knn"]["rescore_vector"] = {"oversample": 3}
        cs, js, ids = S(idx, q)
        K.put("vec_" + typ, cs, js, ids, refused=refused, extra={"mapping": observe(cm, jm), "bulk": sb, "bulk_http": cb,
                                                                  "populated": est["vec_" + typ] is True})
        if typ == "int8_hnsw":
            K.put("knn_rescore_vector", cs, js, ids)
        drop(idx)

    # ES|QL
    def Q(q):
        c, j, _ = req("POST", "/_query", {"query": q})
        return c, j, (j.get("values") if c == 200 and isinstance(j, dict) else None)
    K.put("esql_match", *Q('FROM %s METADATA _score | WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3 | KEEP title, _score' % base))
    fork = ('FROM %s METADATA _id, _index, _score | FORK (WHERE MATCH(title, "paris") | SORT _score DESC | LIMIT 3) '
            '(WHERE MATCH(title, "berlin") | SORT _score DESC | LIMIT 3)' % base)
    K.put("esql_fork", *Q(fork + " | KEEP _id, _fork, _score"))
    K.put("esql_fuse", *Q(fork + " | FUSE | KEEP _id, _score"))
    # inference
    c, j, _ = req("GET", "/_inference")
    K.put("inference_list", c, j, [e.get("inference_id") for e in j.get("endpoints", [])][:5] if c == 200 else None)
    ep = "lic-ep-" + label
    c, j, _ = req("PUT", "/_inference/text_embedding/" + ep, stub_endpoint_body())
    K.put("inference_create", c, j)
    est["inference_ep_new"] = True if c == 200 else {"failure": "create HTTP %s" % c, "observed": classify(c, j)}
    c2, j2, _ = req("POST", "/_inference/text_embedding/" + ep, {"input": "x"})
    K.put("inference_use_new", c2, j2)
    req("DELETE", "/_inference/text_embedding/" + ep + "?force=true")
    c, j, _ = req("GET", "/_inference/text_embedding/" + STUB_EP)
    est["stub_endpoint"] = True if c == 200 else {"failure": "GET HTTP %s" % c, "observed": classify(c, j)}
    c2, j2, _ = req("POST", "/_inference/text_embedding/" + STUB_EP, {"input": "x"})
    K.put("inference_use_existing", c2, j2)
    # semantic_text (needs STUB_EP; on fresh Basic it does not exist). The searches run whatever happened, so a licence refusal is still observed.
    sem = P + "sem"
    drop(sem)
    cm, jm, _ = req("PUT", "/" + sem, {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"body": {"type": "semantic_text", "inference_id": STUB_EP}}}})
    K.put("semantic_text_mapping", cm, jm)
    est["sem_mapping"] = True if 200 <= cm < 300 else {"failure": "create HTTP %s" % cm, "observed": classify(cm, jm)}
    cd, jd, _ = req("PUT", "/%s/_doc/1?refresh=true" % sem, {"body": "paris trip planning"})
    K.put("semantic_text_index", cd, jd)
    est["sem_doc"] = establish(check_written, sem, cd, jd)
    K.put("semantic_query", *S(sem, {"query": {"semantic": {"field": "body", "query": "paris"}}}))
    K.put("match_on_semantic_text", *S(sem, {"query": {"match": {"body": "paris"}}}))
    drop(sem)
    # DLS (role was made by ensure_dls while allowed; on fresh Basic its creation is the cell). The shared lic1-base and the user are prerequisites.
    cr, jr, _cu, _ju = _ensure_dls()
    K.put("dls_role_create", cr, jr)
    est["shared_base"] = establish(check_shared_base)
    est["dls_principal"] = establish(check_dls_principal)
    if est["dls_principal"] is True:
        c, j, ids, extra = dls_search()
        K.put("dls_search_as_user", c, j, ids, extra)
    else:
        K.put("dls_search_as_user", None, extra={"returned": None, "docs_in_payload": None})
    # percolator (lexical)
    perc = P + "perc"
    drop(perc)
    cpc, jpc, _ = req("PUT", "/" + perc, {"mappings": {"properties": {"q": {"type": "percolator"}, "title": {"type": "text"}}}})
    if cpc != 200:   # the index must exist; the percolator doc write and search below are the probe
        raise FixtureError("create %s: HTTP %s" % (perc, cpc), classify(cpc, jpc))
    est["perc_index"] = True
    ct, jt, _ = req("PUT", "/%s/_doc/t1?refresh=true" % perc, {"q": {"match": {"title": "paris"}}})
    est["perc_doc"] = establish(check_written, perc, ct, jt)
    cp, jp, idsp = S(perc, {"query": {"percolate": {"field": "q", "document": {"title": "trip to paris"}}}})
    K.put("percolator_lexical", cp, jp, idsp, extra={"write": observe(ct, jt)})
    drop(perc)
    # aliases: require_alias and must_exist
    g1, al = P + "gen1", P + "alias"
    drop(g1)
    cg, jg, _ = req("PUT", "/" + g1, {"aliases": {al: {"is_write_index": True}}, "mappings": {"properties": {"title": {"type": "text"}}}})
    if cg != 200:
        raise FixtureError("create %s: HTTP %s" % (g1, cg), classify(cg, jg))
    est["alias_index"] = True
    ca, ja, _ = req("PUT", "/%s/_doc/1?require_alias=true&refresh=true" % al, {"title": "x"})
    cb2, jb, _ = req("PUT", "/%s-notalias/_doc/1?require_alias=true" % P, {"title": "x"})
    cm2, jm2, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al, "must_exist": True}}]})
    cm3, jm3, _ = req("POST", "/_aliases", {"actions": [{"remove": {"index": g1, "alias": al + "-ghost", "must_exist": True}}]})
    K.put("alias_require_alias", ca, ja, extra={"write_via_alias": observe(ca, ja), "write_to_non_alias_refused": observe(cb2, jb)})
    K.put("alias_must_exist", cm2, jm2, extra={"existing": observe(cm2, jm2), "ghost_refused": observe(cm3, jm3)})
    # force merge
    fm = P + "fm"
    make_fixture(fm, {"settings": {"number_of_replicas": 0}}, [(str(i), {"t": "x%d" % i}) for i in range(20)], spot=("0", {"t": "x0"}))
    est["fm"] = True
    cf, jf, _ = req("POST", "/%s/_forcemerge?max_num_segments=1" % fm)
    K.put("force_merge", cf, jf)
    for i in (base, g1, fm):
        drop(i)
    return dict(K.done(), _prerequisites={p: est[p] for p in sorted(est)})

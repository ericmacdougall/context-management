"""Experiment 2: live read/write workload through memcore ElasticsearchSearch (strategy auto) while the licence changes."""
import gzip
import json
import random
import threading
import time

import lib
import evidence
import matrix
import observe
import oracle
from lib import req

VOCAB = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima", "mike", "nov", "oscar", "papa",
         "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "xray", "yankee", "zulu", "paris", "berlin", "refund", "tax", "dentist",
         "train", "move", "club", "notes", "plan", "form", "office", "book", "trip"]
TENANT = "lic"
DIMS = 8


def auth():
    return lib._auth()


def patch_fast_http():
    """HARNESS DEVIATION (recorded in every result): the reviewed http_util.urlopen builds an opener per call, which loads the system CA store
    (21 ms of GIL-held CPU per request, measured by cProfile: ssl.set_default_verify_paths), capping one process near 45 requests/s. The
    patched opener is built once, still never follows redirects. Licence latch, fusion and request shapes are the reviewed code unchanged."""
    import urllib.request
    import memcore.adapters.elasticsearch_search as es
    from memcore.http_util import _NoRedirect
    op = urllib.request.build_opener(_NoRedirect())
    es._urlopen = lambda req, timeout=None, context=None: op.open(req, timeout=timeout)


patch_fast_http()
HTTP_PATCHED = True


def make_adapter(strategy="auto", ttl=5.0, backend_fusion="rrf"):
    from memcore.adapters.elasticsearch_search import ElasticsearchSearch
    a = ElasticsearchSearch(lib.BASE, auth, tenant=TENANT, strategy=strategy, timeout_s=20.0, backend_fusion=backend_fusion)
    a.probe_ttl_s = ttl
    return a


def make_broken(ttl):
    """CONTROL for EXP2-T1: an adapter whose Basic fallback silently returns lexical-only results labelled as fused (wrong but successful)."""
    from memcore.adapters.elasticsearch_search import ElasticsearchSearch
    from memcore.ports import SearchResult

    class Broken(ElasticsearchSearch):
        def _client_fused(self, index, query, vector, filter, k, tenant):
            return SearchResult(self.lexical(index, query, filter, k, tenant).hits, "fused", "client")
    a = Broken(lib.BASE, auth, tenant=TENANT, strategy="auto", timeout_s=20.0)
    a.probe_ttl_s = ttl
    return a


classify_sem_failure = observe.classify_sem_failure   # one classification for the recorder and the loader (review 13 F32)


class _NoLock:   # a test fixture may build a Workload without __init__
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def mk_text(rng):
    return " ".join(rng.choice(VOCAB) for _ in range(rng.randint(5, 9)))


class Workload:
    def __init__(self, seed, ttl, n_docs=1500, n_queries=60, readers=8, lin_readers=2, ctl_readers=4, plain_writers=3):
        self.seed, self.ttl, self.n_docs, self.nq = seed, ttl, n_docs, n_queries
        self.rd, self.lin, self.ctl, self.pw = readers, lin_readers, ctl_readers, plain_writers
        rng = random.Random(seed)
        self.docs = {"d%05d" % i: None for i in range(n_docs)}
        for k in self.docs:
            t = mk_text(rng)
            self.docs[k] = {"text": t, "vector": lib.embed(t, DIMS)}
        self.queries = []
        for _ in range(n_queries):
            t = " ".join(rng.choice(VOCAB) for _ in range(rng.randint(2, 3)))
            self.queries.append((t, lib.embed(t, DIMS)))
        self.rows, self.lock, self.stop_ev = [], threading.Lock(), threading.Event()
        self.threads, self.events = [], {}
        self.acked, self.attempted_failed, self.sem_rows = {}, [], []
        self.latch = []
        self.oracle_legs, self.oracle_rrf, self.oracle_lin = {}, {}, {}
        self.a_auto, self.a_lin, self.a_ctl, self.a_plain = (make_adapter("auto", ttl), make_adapter("auto", ttl, "linear"), make_adapter("backend", ttl),
                                                             make_adapter("auto", ttl))
        self.a_brk = make_broken(ttl)

    # ---- setup
    def setup(self, with_sem_index=True):
        for logical in ("static", "live"):
            idx = "mc-%s-%s" % (TENANT, logical)
            req("DELETE", "/" + idx)
            c, j, _ = req("PUT", "/" + idx, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
                "text": {"type": "text"}, "vector": {"type": "dense_vector", "dims": DIMS, "similarity": "cosine"}, "tenant": {"type": "keyword"}}}})
            assert c == 200, (c, j)
        items = list(self.docs.items())
        for i in range(0, len(items), 500):
            self.a_plain.bulk_add("static", dict(items[i:i + 500]))
        if with_sem_index:
            req("DELETE", "/lic-sem2")
            c, j, _ = req("PUT", "/lic-sem2", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": {"body": {"type": "semantic_text", "inference_id": matrix.STUB_EP}}}})
            self.events["sem2_create"] = [c, lib.err(j)]
        c, j, _ = req("GET", "/mc-%s-static/_count" % TENANT)
        self.events["static_count"] = j.get("count")
        if c != 200 or j.get("count") != len(self.docs):   # MUST SUCCEED: the oracle legs below are taken from this index
            raise AssertionError("static fixture holds %s documents, expected %d (HTTP %s)" % (j.get("count") if isinstance(j, dict) else None, len(self.docs), c))
        for qi, (t, v) in enumerate(self.queries):
            self.oracle_legs[qi] = oracle.raw_legs("mc-%s-static" % TENANT, t, v, TENANT)

    def compute_backend_oracles(self):
        idx = "mc-%s-static" % TENANT
        bad = 0
        for qi, (t, v) in enumerate(self.queries):
            for kind, store in (("rrf", self.oracle_rrf), ("linear", self.oracle_lin)):
                c, ids, e = oracle.raw_backend(idx, kind, t, v, TENANT)
                if c == 200 and ids:   # an empty reference list on the established static index is not an oracle
                    store[qi] = ids
                else:
                    bad += 1
        return bad

    # ---- workers
    def _log(self, row):
        with self.lock:
            self.rows.append(row)

    def _reader(self, arm, adapter, seed):
        rng = random.Random(seed)
        while not self.stop_ev.is_set():
            qi = rng.randrange(self.nq)
            t, v = self.queries[qi]
            t0 = time.time()
            try:
                if arm == "lex":
                    r = adapter.lexical("static", t, k=10)
                elif arm == "vec":
                    r = adapter.vector("static", v, k=10)
                else:
                    r = adapter.fused("static", t, v, k=10)
                self._log([t0, time.time(), arm, qi, 1, r.fusion, [h.id for h in r.hits], None])
            except Exception as e:  # noqa: BLE001
                self._log([t0, time.time(), arm, qi, 0, None, None, type(e).__name__ + ": " + str(e)[:160]])
            time.sleep(0.002)

    def _plain_writer(self, wid):
        seq = 0
        rng = random.Random(self.seed * 100 + wid)
        while not self.stop_ev.is_set():
            batch = {}
            for _ in range(5):
                t = mk_text(rng)
                batch["w%d-%06d" % (wid, seq)] = {"text": t, "vector": lib.embed(t, DIMS)}
                seq += 1
            t0 = time.time()
            try:
                self.a_plain.bulk_add("live", batch)
                with self.lock:
                    for k in batch:
                        self.acked[k] = time.time()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.attempted_failed.append([t0, list(batch), type(e).__name__ + ": " + str(e)[:160]])
            time.sleep(0.1)

    def _sem_writer(self):
        seq = 0
        while not self.stop_ev.is_set():
            i = "s%06d" % seq
            seq += 1
            t0 = time.time()
            c, j, _ = req("PUT", "/lic-sem2/_doc/%s" % i, {"body": "alpha bravo %d" % seq}, timeout=20)
            with self.lock:
                self.sem_rows.append([t0, i, c, lib.err(j) if c >= 300 else None, time.time()])   # t1: review 13 F32 (up: on Basic)
            time.sleep(0.25)

    def _sampler(self):
        while not self.stop_ev.is_set():
            self.latch.append([time.time(), self.a_auto._probe, self.a_auto.fusion_location, self.a_lin._probe])
            time.sleep(0.1)

    def start(self):
        self.events["t_start"] = time.time()
        spec = [("auto", self.a_auto, self.rd), ("lin", self.a_lin, self.lin), ("ctl", self.a_ctl, self.ctl), ("lex", self.a_plain, 2), ("vec", self.a_plain, 2), ("brk", self.a_brk, 2)]
        for arm, ad, n in spec:
            for i in range(n):
                self.threads.append(threading.Thread(target=self._reader, args=(arm, ad, self.seed * 1000 + len(self.threads)), daemon=True))
        for w in range(self.pw):
            self.threads.append(threading.Thread(target=self._plain_writer, args=(w,), daemon=True))
        self.threads.append(threading.Thread(target=self._sem_writer, daemon=True))
        self.threads.append(threading.Thread(target=self._sampler, daemon=True))
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_ev.set()
        for t in self.threads:
            t.join(timeout=30)
        self.events["t_stop"] = time.time()

    # ---- verification
    def verify_writes(self, t_req=None, t_resp=None, direction=None):
        """Read back what is present, keep the raw write record (self.writes_raw, dumped by dump_writes) and reduce it with
        observe.writes, the same function the loader runs on results/<life>.writes.gz. Transition times select the Basic interval."""
        cr, jr, _ = req("POST", "/mc-%s-live/_refresh" % TENANT)
        if cr != 200:   # MUST SUCCEED: an unrefreshed index would make acknowledged writes look absent
            raise AssertionError("refresh of the live index: HTTP %s" % cr)
        present = set()
        c, j, _ = req("POST", "/mc-%s-live/_search?scroll=2m" % TENANT, {"size": 5000, "_source": False, "query": {"match_all": {}}}, timeout=120)
        sid = j.get("_scroll_id") if isinstance(j, dict) else None
        while True:   # MUST SUCCEED: a failed or shard-failed page would make present_unacked and failed_ids_present vacuously small
            if c != 200 or (j.get("_shards") or {}).get("failed") or j.get("timed_out"):
                raise AssertionError("live index scroll page: HTTP %s shards %s" % (c, j.get("_shards") if isinstance(j, dict) else None))
            if not j["hits"]["hits"]:
                break
            present.update(h["_id"] for h in j["hits"]["hits"])
            sid = j.get("_scroll_id", sid)
            c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)
        if sid:
            req("DELETE", "/_search/scroll", {"scroll_id": sid})
        cr, jr, _ = req("POST", "/lic-sem2/_refresh")
        c, j, _ = req("POST", "/lic-sem2/_search", {"size": 10000, "track_total_hits": True, "_source": False, "query": {"match_all": {}}})
        got = j.get("hits", {}).get("hits", []) if c == 200 and isinstance(j, dict) else None
        if cr != 200 or got is None or (j.get("_shards") or {}).get("failed") or j["hits"].get("total", {}).get("value") != len(got):
            # MUST SUCCEED: an unread or truncated semantic index would make sem_failed_present and sem_present_unacked vacuously 0
            raise AssertionError("semantic index read-back: refresh HTTP %s, search HTTP %s" % (cr, c))
        with self.lock if hasattr(self, "lock") else _NoLock():
            self.writes_raw = {"acked": dict(self.acked), "failed_batches": [[b[0], list(b[1]), b[2]] for b in self.attempted_failed],
                               "sem_rows": [list(r) for r in self.sem_rows], "live_present": sorted(present),
                               "sem_present": sorted(h["_id"] for h in got)}
        return observe.writes(self.writes_raw, t_req, t_resp, direction)

    def dump_writes(self, path):
        with gzip.open(path, "wt") as fh:
            json.dump(self.writes_raw, fh)

    def _typed_figures(self, out, rows, t_req, t_resp, direction):
        # T2, T3, T5 are computed only from the TYPED workload stream (review 9 F17): finite times inside the run window, completion
        # order, closed arms. The same observe functions re-derive them from rows.gz at load (summarize._backfill_observed). A stream
        # that does not parse records the reason and no figure, so those gates are UNVERIFIED.
        ev = getattr(self, "events", {})   # a test fixture may build a Workload without __init__
        window = (ev.get("t_start"), ev.get("t_stop"))
        try:
            TW = observe.parse_work([list(r) for r in rows], window if None not in window else None)
        except observe.Unparsed as e:
            out["raw_check"] = "workload stream does not parse: %s" % e
            return
        out.update(observe.availability(TW, t_req, t_resp))
        out.update(observe.flip(TW, t_req, t_resp, direction))
        if direction == "down":
            out["T3_backend_labelled_after_resp_start"] = sum(1 for w in TW if w.arm == "auto" and w.ok and w.label == "backend" and w.t0 > t_resp)
        else:
            out["T3_ttl_s"] = self.ttl
        out.update(observe.control(TW, t_req, t_resp, direction))
        out["T1_answers_bound"] = observe.answers_bound(TW)
        # T1 on the same typed rows (review 10 F21: one implementation, also used by summarize.rebuild on the saved oracle)
        out.update(observe.correctness(TW, getattr(self, "oracle_legs", {}), getattr(self, "oracle_rrf", {}), getattr(self, "oracle_lin", {})))

    def analyse(self, t_req, t_resp, direction):
        """direction 'down' (trial->basic at t_resp) or 'up' (basic->trial)."""
        rows = self.rows
        out = {"requests_total": len(rows)}
        arms = {}
        for r in rows:
            a = arms.setdefault(r[2], {"n": 0, "ok": 0, "fail": 0, "backend": 0, "client": 0, "errs": {}})
            a["n"] += 1
            a["ok" if r[4] else "fail"] += 1
            if r[4] and r[5] in ("backend", "client"):
                a[r[5]] += 1
            if not r[4]:
                k = r[7][:60]
                a["errs"][k] = a["errs"].get(k, 0) + 1
        out["arms"] = arms
        self._typed_figures(out, rows, t_req, t_resp, direction)
        # T6 ranking function change (rrf vs linear vs client), recorded only
        if self.oracle_rrf:
            diff_list = sum(1 for qi in self.oracle_rrf if self.oracle_rrf[qi] != _client_ids(self.oracle_legs[qi]))
            diff_set = sum(1 for qi in self.oracle_rrf if set(self.oracle_rrf[qi]) != set(_client_ids(self.oracle_legs[qi])))
            out["T6_rrf_backend_vs_client_top10"] = {"queries": len(self.oracle_rrf), "list_differs": diff_list, "set_differs": diff_set}
        if self.oracle_lin:
            out["T6_linear_backend_vs_client_rrf_top10"] = {"queries": len(self.oracle_lin),
                                                           "list_differs": sum(1 for qi in self.oracle_lin if self.oracle_lin[qi] != _client_ids(self.oracle_legs[qi])),
                                                           "set_differs": sum(1 for qi in self.oracle_lin if set(self.oracle_lin[qi]) != set(_client_ids(self.oracle_legs[qi])))}
        out["linear_arm_labels"] = {k: v for k, v in arms.get("lin", {}).items() if k in ("backend", "client")}
        return out

    def dump_oracles(self, path):
        with gzip.open(path, "wt") as fh:
            json.dump({"queries": [q[0] for q in self.queries], "legs": {str(k): v for k, v in self.oracle_legs.items()},
                       "rrf": {str(k): v for k, v in self.oracle_rrf.items()}, "linear": {str(k): v for k, v in self.oracle_lin.items()},
                       "seed": self.seed, "http_patch": True}, fh)

    def dump(self, path):
        with gzip.open(path, "wt") as fh:
            for r in self.rows:
                fh.write(json.dumps(r) + "\n")


def _client_ids(legs, k=10):
    sc = oracle.rrf_scores(list(legs))
    order = {}
    for lst in legs:
        for p, d in enumerate(lst):
            order.setdefault(d, p)
    return [d for d, _ in sorted(sc.items(), key=lambda kv: (-kv[1], order[kv[0]], kv[0]))][:k]


class Poller(threading.Thread):
    """EXP1-T2: fast licensed/unlicensed probes every ~50 ms around a transition."""
    def __init__(self, sem_idx):
        super().__init__(daemon=True)
        self.sem_idx, self.stop_ev, self.rows = sem_idx, threading.Event(), []

    def run(self):
        self.est = matrix.establish_quick()   # prerequisites checked once, before polling, not inside the 50 ms loop
        while not self.stop_ev.is_set():
            t0 = time.time()
            try:
                p = matrix.quick_probes(self.sem_idx, self.est)
            except Exception as e:  # noqa: BLE001
                p = {"_error": type(e).__name__}
            p["_t0"] = t0
            p["_t1"] = time.time()
            self.rows.append(p)
            time.sleep(0.05)


def moment_analysis(rows, t_resp, features=("rrf", "linear", "semantic_query", "inference_use", "dls_search"), direction="down", window=None,
                    t_req=None):
    """First refusal (down) or first success (up) per feature, relative to the transition response, from the TYPED poll stream
    (observe.parse_poll: a row or ordering violation gives every probe a stream_error and no first_refusal). window: the run's
    (t_start, t_stop)."""
    return observe.moments(rows, t_resp, tuple(features), window, direction, t_req)

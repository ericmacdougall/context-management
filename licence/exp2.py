"""Experiment 2: live read/write workload through memcore ElasticsearchSearch (strategy auto) while the licence changes."""
import gzip
import json
import random
import threading
import time

import lib
import matrix
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
        for qi, (t, v) in enumerate(self.queries):
            self.oracle_legs[qi] = oracle.raw_legs("mc-%s-static" % TENANT, t, v, TENANT)

    def compute_backend_oracles(self):
        idx = "mc-%s-static" % TENANT
        bad = 0
        for qi, (t, v) in enumerate(self.queries):
            for kind, store in (("rrf", self.oracle_rrf), ("linear", self.oracle_lin)):
                c, ids, e = oracle.raw_backend(idx, kind, t, v, TENANT)
                if c == 200:
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
                self.sem_rows.append([t0, i, c, lib.err(j) if c >= 300 else None])
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
    def verify_writes(self):
        req("POST", "/mc-%s-live/_refresh" % TENANT)
        present = set()
        c, j, _ = req("POST", "/mc-%s-live/_search?scroll=2m" % TENANT, {"size": 5000, "_source": False, "query": {"match_all": {}}}, timeout=120)
        sid = j.get("_scroll_id")
        while c == 200 and j["hits"]["hits"]:
            present.update(h["_id"] for h in j["hits"]["hits"])
            c, j, _ = req("POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid}, timeout=120)
        if sid:
            req("DELETE", "/_search/scroll", {"scroll_id": sid})
        acked = set(self.acked)
        control_detects = len((acked | {"__injected_lost_write__"}) - present) == len(acked - present) + 1
        failed_ids = {i for _, ids, _ in self.attempted_failed for i in ids}
        req("POST", "/lic-sem2/_refresh")
        c, j, _ = req("POST", "/lic-sem2/_search", {"size": 10000, "_source": False, "query": {"match_all": {}}})
        sem_present = {h["_id"] for h in j.get("hits", {}).get("hits", [])} if c == 200 else set()
        sem_ack = {r[1] for r in self.sem_rows if r[2] in (200, 201)}
        sem_fail = [r for r in self.sem_rows if r[2] >= 300 or r[2] == 0]
        loud = sum(1 for r in sem_fail if r[3] and (("non-compliant" in json.dumps(r[3])) or r[2] in (404, 403)))
        return {"control_injected_loss_detected": control_detects, "live_present": len(present), "acked": len(acked), "acked_absent": len(acked - present), "present_unacked": len(present - acked),
                "failed_batches": len(self.attempted_failed), "failed_ids_present": len(failed_ids & present),
                "sem_acked": len(sem_ack), "sem_acked_absent": len(sem_ack - sem_present), "sem_present_unacked": len(sem_present - sem_ack),
                "sem_failed": len(sem_fail), "sem_failed_loud_licence_or_missing": loud,
                "sem_failed_present": len({r[1] for r in sem_fail} & sem_present),
                "sem_first_ok_t": min([r[0] for r in self.sem_rows if r[2] in (200, 201)] or [None]) if sem_ack else None}

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
        # T1: wrong-but-successful
        ex, wrong, wrong_examples = 0, 0, []
        unlabeled_backend_without_oracle = 0
        for r in rows:
            if r[2] not in ("auto", "lin") or not r[4]:
                continue
            qi, ids, fus = r[3], r[6], r[5]
            ex += 1
            if fus == "client":
                ok = oracle.valid_topk(ids, list(self.oracle_legs[qi]), 10)
            elif fus == "backend":
                ref = (self.oracle_rrf if r[2] == "auto" else self.oracle_lin).get(qi)
                if ref is None:
                    unlabeled_backend_without_oracle += 1
                    continue
                ok = ids == ref
            else:
                ok = False
            if not ok:
                wrong += 1
                if len(wrong_examples) < 5:
                    wrong_examples.append({"arm": r[2], "qi": qi, "fusion": fus, "ids": ids[:10]})
        brk_client = [r for r in rows if r[2] == "brk" and r[4] and r[5] == "client"]
        brk_flagged = sum(1 for r in brk_client if not oracle.valid_topk(r[6], list(self.oracle_legs[r[3]]), 10))
        brk_backend = [r for r in rows if r[2] == "brk" and r[4] and r[5] == "backend" and r[3] in self.oracle_rrf]
        out["T1_control_broken_fallback"] = {"client_labelled": len(brk_client), "flagged_wrong": brk_flagged,
                                             "flag_rate": brk_flagged / max(1, len(brk_client)),
                                             "backend_labelled_checked": len(brk_backend), "backend_wrong": sum(1 for r in brk_backend if r[6] != self.oracle_rrf[r[3]])}
        out["T1_examined"], out["T1_mismatches"], out["T1_examples"], out["T1_backend_without_oracle"] = ex, wrong, wrong_examples, unlabeled_backend_without_oracle
        # T2: failures
        fails = sorted(r for r in rows if not r[4] and r[2] in ("auto", "lin", "lex", "vec"))
        wins, cur = [], None
        for r in fails:
            if cur and r[0] - cur[1] < 1.0:
                cur[1] = max(cur[1], r[1]); cur[2] += 1
            else:
                cur = [r[0], r[1], 1]; wins.append(cur)
        out["T2_failed_requests"] = len(fails)
        out["T2_non_control_requests"] = sum(1 for r in rows if r[2] in ("auto", "lin", "lex", "vec"))
        out["T2_longest_failure_window_s"] = max([w[1] - w[0] for w in wins] or [0.0])
        out["T2_failure_windows"] = wins[:10]
        # T3: latch flip
        auto = sorted(r for r in rows if r[2] == "auto" and r[4])
        if direction == "down":
            first = next((r for r in auto if r[5] == "client" and r[1] >= t_req), None)
            out["T3_flip_latency_s"] = None if first is None else first[1] - t_resp
            out["T3_backend_after_first_client"] = sum(1 for r in auto if first and r[5] == "backend" and r[0] > first[1])
            out["T3_backend_labelled_after_resp_start"] = sum(1 for r in auto if r[5] == "backend" and r[0] > t_resp)
        else:
            first = next((r for r in auto if r[5] == "backend" and r[1] >= t_resp), None)
            out["T3_flip_latency_s"] = None if first is None else first[1] - t_resp
            out["T3_client_after_first_backend"] = sum(1 for r in auto if first and r[5] == "client" and r[0] > first[1])
            out["T3_ttl_s"] = self.ttl
        # T5 control
        ctl = [r for r in rows if r[2] == "ctl" and r[0] >= t_resp + 1.0]
        if direction == "down":
            lic = sum(1 for r in ctl if (not r[4]) and "non-compliant" in (r[7] or ""))
            out["T5_control_after_1s"] = {"n": len(ctl), "licence_failures": lic, "other_failures": sum(1 for r in ctl if not r[4]) - lic,
                                          "success": sum(1 for r in ctl if r[4]), "fail_fraction": (len(ctl) - sum(1 for r in ctl if r[4])) / max(1, len(ctl))}
        else:
            pre = [r for r in rows if r[2] == "ctl" and r[1] < t_req]
            post = [r for r in rows if r[2] == "ctl" and r[0] >= t_resp + 1.0]
            out["T5_control_before_start_trial"] = {"n": len(pre), "failures": sum(1 for r in pre if not r[4])}
            out["T5_control_after_trial"] = {"n": len(post), "success": sum(1 for r in post if r[4])}
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
        while not self.stop_ev.is_set():
            t0 = time.time()
            try:
                p = matrix.quick_probes(self.sem_idx)
            except Exception as e:  # noqa: BLE001
                p = {"_error": type(e).__name__}
            p["_t0"] = t0
            p["_t1"] = time.time()
            self.rows.append(p)
            time.sleep(0.05)


def moment_analysis(rows, t_resp, features=("rrf", "linear", "semantic_query", "inference_use", "dls_search"), direction="down"):
    """First refusal (down) or first success (up) per feature, relative to the transition response; plus success-after-refusal count."""
    out = {}
    for f in features:
        seq = [(r["_t1"], r.get(f)) for r in rows if f in r]
        if not seq or all(x[1] and x[1][0] == "SKIP" for x in seq):
            out[f] = {"skipped": True}
            continue
        if direction == "down":
            ref = next(((t, v) for t, v in seq if t >= t_resp - 0.0 and v[0] in ("LICENCE_ERROR", "OTHER_ERROR")), None)
            before_ok = sum(1 for t, v in seq if t < t_resp and v[0] == "AVAILABLE")
            refusal = (lambda v: v[0] not in ("AVAILABLE", "SKIP")) if f == "dls_search" else (lambda v: v[0] == "LICENCE_ERROR")
            first_ref_t = next((t for t, v in seq if t >= t_resp - 0.5 and refusal(v)), None)
            dls_before = sorted({v[2] for t, v in seq if t < t_resp and v[0] == "AVAILABLE" and len(v) > 2}) if f == "dls_search" else None
            succ_after = sum(1 for t, v in seq if first_ref_t is not None and t > first_ref_t and v[0] == "AVAILABLE")
            out[f] = {"first_refusal_rel_resp_s": None if first_ref_t is None else first_ref_t - t_resp, "success_before_transition": before_ok,
                      "success_after_first_refusal": succ_after, "dls_returned_counts_before": dls_before, "first_refusal_status": ref[1][1] if ref else None, "n": len(seq)}
        else:
            first_ok = next((t for t, v in seq if t >= t_resp and v[0] == "AVAILABLE"), None)
            out[f] = {"first_success_rel_resp_s": None if first_ok is None else first_ok - t_resp, "n": len(seq)}
    return out

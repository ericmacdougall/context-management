"""Read helper: hybrid search with client-side fusion + a bounded tail scan.

Order of operations matters:
  1. read the tenant's high-water mark (indexed AND refreshed) FIRST;
  2. open a point-in-time on the read alias so the BM25 leg and the vector leg
     see one set of index generations (no mixed view during an alias swap);
  3. run both legs; 4. scan Postgres for events above the mark (no xmin horizon:
     everything committed is visible); 5. fuse with RRF.
The tail scan is bounded; if more rows sit above the mark than the bound the
result is flagged `tail_truncated` (degraded), never silently incomplete.
"""
from . import esx, fusion
from .esc import ESError

LEG = 50


class Reader:
    def __init__(self, conn, es, tail_bound=2000):
        self.conn, self.es, self.tail_bound = conn, es, tail_bound
        self.pit_retries = 0

    # ----------------------------------------------------------- helpers
    def hwm(self, tenant, view="events", version=1):
        r = self.conn.execute("select hwm_xid, hwm_seq from projection_state where view=%s and version=%s and tenant_id=%s",
                              (view, version, tenant)).fetchone()
        return r or (0, 0)

    def _with_pit(self, alias, fn):
        for attempt in (0, 1):
            pit = None
            try:
                pit = self.es.req("POST", f"/{alias}/_pit", keep_alive="30s")[1]["id"]
                return fn({"id": pit, "keep_alive": "30s"})
            except ESError as e:
                self.pit_retries += 1
                if attempt:
                    raise
            finally:
                if pit:
                    try:
                        self.es.req("DELETE", "/_pit", {"id": pit}, ok=None)
                    except Exception:
                        pass

    def _legs(self, pit, text_field, text, qv, vec_exact=False, extra_filter=None, src=None):
        filt = [extra_filter] if extra_filter else []
        bm = {"pit": pit, "size": LEG, "_source": src or False, "track_total_hits": False,
              "query": {"bool": {"must": [{"match": {text_field: text}}], "filter": filt}},
              "sort": ["_score", {"_shard_doc": "asc"}]}
        if vec_exact:
            vq = {"pit": pit, "size": LEG, "_source": src or False, "track_total_hits": False,
                  "query": {"script_score": {"query": {"bool": {"filter": filt}} if filt else {"match_all": {}},
                            "script": {"source": "cosineSimilarity(params.q, 'vec') + 1.0", "params": {"q": qv}}}}}
        else:
            vq = {"pit": pit, "size": LEG, "_source": src or False, "track_total_hits": False,
                  "knn": {"field": "vec", "query_vector": qv, "k": LEG, "num_candidates": 200, **({"filter": filt} if filt else {})}}
        out = []
        for body in (bm, vq):
            h = self.es.req("POST", "/_search", body)[1]["hits"]["hits"]
            out.append(h)
        return out

    # ------------------------------------------------------------- events
    def search(self, tenant, text, k=10, vec_exact=False, tail=True):
        mark = self.hwm(tenant)                      # 1. mark BEFORE the ES search
        qv = fusion.embed(text)
        bm, kn = self._with_pit(f"sys-{tenant}-events", lambda pit: self._legs(pit, "content", text, qv, vec_exact))
        lists = [[h["_id"] for h in bm], [h["_id"] for h in kn]]
        es_ids = set(lists[0]) | set(lists[1])
        tail_info = {"rows": 0, "truncated": False}
        tail_ids = set()
        if tail:
            tl, tv, tail_info = self._tail(tenant, mark, text, qv)
            lists += [tl, tv]
            tail_ids = set(tl) | set(tv)
        fused = fusion.rrf(lists, topn=k)
        hits = [{"id": i, "score": s, "via": ("es" if i in es_ids else "tail")} for i, s in fused]
        return {"hits": hits, "mark": mark, "tail": tail_info, "es_only_ids": lists[0] + [x for x in lists[1] if x not in lists[0]]}

    def _tail(self, tenant, mark, text, qv):
        cur = self.conn.cursor()
        cur.execute(
            "select event_id, mem_dec(tenant_id, subject_id, content_enc) from events e where tenant_id=%s "
            "and (commit_xid, ingest_seq) > (%s, %s) and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) "
            "order by commit_xid desc, ingest_seq desc limit %s", (tenant, mark[0], mark[1], self.tail_bound + 1))
        rows = cur.fetchall()
        truncated = len(rows) > self.tail_bound
        rows = [r for r in rows[: self.tail_bound] if r[1] is not None]
        qt = fusion.tokens(text)
        lex = sorted(((fusion.lexical_score(qt, fusion.tokens(c)), eid) for eid, c in rows), key=lambda x: (-x[0], x[1]))
        vec = sorted(((fusion.cosine(qv, fusion.embed(c)), eid) for eid, c in rows), key=lambda x: (-x[0], x[1]))
        tl = [e for s, e in lex if s > 0][:LEG]
        tv = [e for s, e in vec if s > 0.05][:LEG]
        return tl, tv, {"rows": len(rows), "truncated": truncated}

    # -------------------------------------------------------------- facts
    def search_facts(self, tenant, text, k=10, vec_exact=False):
        qv = fusion.embed(text)
        def go(pit):
            bm, kn = self._legs(pit, "object", text, qv, vec_exact, src=["recipe_version", "status", "subject", "predicate"])
            return bm, kn
        bm, kn = self._with_pit(f"sys-{tenant}-facts", go)
        versions = {h["_source"]["recipe_version"] for h in bm + kn}
        fused = fusion.rrf([[h["_id"] for h in bm], [h["_id"] for h in kn]], topn=k)
        return {"hits": [i for i, _ in fused], "versions": versions}

    def query_facts(self, tenant, subject, predicate=None, as_of=None):
        filt = [{"term": {"subject": subject}}, {"terms": {"status": ["current", "superseded"]}}]
        if predicate:
            filt.append({"term": {"predicate": predicate}})
        if as_of:
            filt += [{"range": {"valid_from": {"lte": as_of}}},
                     {"bool": {"should": [{"bool": {"must_not": {"exists": {"field": "valid_to"}}}}, {"range": {"valid_to": {"gt": as_of}}}]}}]
        r = self.es.req("POST", f"/sys-{tenant}-facts/_search", {"size": 50, "_source": ["predicate", "scope", "object", "status", "valid_from", "valid_to"],
                                                                 "query": {"bool": {"filter": filt}}, "sort": [{"valid_from": "asc"}, {"id": "asc"}]})[1]
        return [h["_source"] for h in r["hits"]["hits"]]

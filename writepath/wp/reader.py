"""Read paths. Every result passes the tombstone and erased-subject filter; there is no parameter to switch it off,
and it does not depend on whether an erasure job is open (review N4, N5)."""
from . import families as F


class Reader:
    def __init__(self, conn, es, tenant):
        self.conn, self.es, self.t = conn, es, tenant
        self.filtered = 0

    def _hidden(self, event_ids, subjects):
        ev = {r[0] for r in self.conn.execute("select event_id from tombstones where tenant_id=%s and event_id = any(%s)", (self.t, list(event_ids))).fetchall()}
        sj = {r[0] for r in self.conn.execute("select subject_id from subjects where tenant_id=%s and erased and subject_id = any(%s)", (self.t, list(subjects))).fetchall()}
        return ev, sj

    def _search(self, family, query, size):
        s, j = self.es.req("POST", f"/{F.read_alias(self.t, family)}/_search", {"size": size, "query": query}, ok=(200, 404))
        return [h["_source"] for h in j.get("hits", {}).get("hits", [])] if s == 200 else []

    def search(self, text, k=10):
        hits = self._search("events", {"match": {"content": text}}, k * 3)
        ev, sj = self._hidden([h["event_id"] for h in hits], [h["subject_id"] for h in hits])
        out = [h for h in hits if h["event_id"] not in ev and h["subject_id"] not in sj]
        self.filtered += len(hits) - len(out)
        return out[:k]

    def facts(self, entity, attribute=None, as_of=None, k=50):
        filt = [{"term": {"entity": entity}}]
        if attribute:
            filt.append({"term": {"attribute": attribute}})
        if as_of:
            filt += [{"range": {"valid_from": {"lte": as_of}}},
                     {"bool": {"should": [{"bool": {"must_not": {"exists": {"field": "valid_to"}}}}, {"range": {"valid_to": {"gt": as_of}}}]}}]
        hits = self._search("facts", {"bool": {"filter": filt}}, k)
        ev, sj = self._hidden([e for h in hits for e in h["input_event_ids"]], [h["subject_id"] for h in hits])
        out = [h for h in hits if not (set(h["input_event_ids"]) & ev) and h["subject_id"] not in sj]
        self.filtered += len(hits) - len(out)
        return out

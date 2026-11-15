"""Index families for non-in-place changes. 'events' = the current layout. 'ev2' = a mapping change that cannot be applied in
place (kind and session_id change from keyword to text+keyword subfield, content gains an english subfield). 'ev3' = ev2 plus a new
embedding model: vector field vec2 with 32 dims (replaces vec with 64)."""
import copy
import json
import os
from memsys import derive as D, esx, fusion
from memsys.db import iso

D32 = 32


def mapping(fam):
    m = copy.deepcopy(esx.EVENTS_MAPPING)
    p = m["properties"]
    if fam in ("ev2", "ev3"):
        p["kind"] = {"type": "text", "fields": {"raw": {"type": "keyword"}}}
        p["session_id"] = {"type": "text", "fields": {"raw": {"type": "keyword"}}}
        p["content"] = {"type": "text", "fields": {"en": {"type": "text", "analyzer": "english"}}}
    if fam == "ev3":
        del p["vec"]
        p["vec2"] = {"type": "dense_vector", "dims": D32, "similarity": "cosine", "index_options": {"type": "int8_hnsw"}}
    return m


def install_templates(es):
    for fam, prio in (("ev2", 302), ("ev3", 303)):
        es.req("PUT", f"/_index_template/sys-emlab-{fam}", {"index_patterns": [f"sys-*-{fam}-b*"], "priority": prio, "template": {"settings": esx.SETTINGS, "mappings": mapping(fam)}})


def vec_field(fam):
    return ("vec2", D32) if fam == "ev3" else ("vec", fusion.DIM)


def index_name(t, fam, b, g):
    return f"sys-{t}-{fam}-b{b:05d}-g{g}"


def doc(fam, t, r):
    d = esx.event_doc(t, r["session_id"], r["seq"], r["event_id"], iso(r["ts_event"]), r["actor"], r["trust_origin"], r["kind"], r["subject_id"], r["plain"])
    if fam == "ev3":
        d["vec2"] = fusion.embed(r["plain"], D32); del d["vec"]
    return d


def family_of(index):
    for f in ("ev3", "ev2"):
        if f"-{f}-b" in index:
            return f
    return "events"


class FamilyProjector:
    """Mirrors events into a family with its own cursor (view 'events-<fam>'); create-only, per-generation write alias."""
    def __init__(self, conn, es, fam, tenants, batch=500):
        self.conn, self.es, self.fam, self.tenants, self.batch = conn, es, fam, tenants, batch
        from memsys import kms
        self.kms = kms.get(); self.known = {}

    def ensure(self, t, b):
        if (t, b) in self.known:
            return self.known[(t, b)]
        found = esx._targets(self.es, f"sys-{t}-{self.fam}-w{b:05d}-g*")
        if found:
            g = max(esx.gen_of(i) for i in found)
        else:
            g = 1
            esx._create_family_index(self.es, index_name(t, self.fam, b, g), {f"sys-{t}-{self.fam}-w{b:05d}-g{g}": {"is_write_index": True}, f"sys-{t}-{self.fam}": {}})
        self.known[(t, b)] = g
        return g

    def run_once(self):
        n = 0
        for t in self.tenants:
            with self.conn.transaction():
                cx, cs = D.get_cursor(self.conn.cursor(), "events-" + self.fam, 1, t)
                xm, rows = D.read_event_batch(self.conn, t, cx, cs, self.batch, self.kms)
            if not rows:
                with self.conn.transaction():
                    D.set_cursor(self.conn.cursor(), "events-" + self.fam, 1, t, xm - 1, D.BIG)
                continue
            lines, touched = [], set()
            for r in rows:
                if r["tomb"] or r["plain"] is None:
                    continue
                b = esx.bucket_of(r["ts_event"]); g = self.ensure(t, b)
                w = f"sys-{t}-{self.fam}-w{b:05d}-g{g}"; touched.add(w)
                lines.append(({"create": {"_index": w, "_id": r["event_id"]}}, doc(self.fam, t, r)))
            esx.bulk_index(self.es, lines, chunk=500)
            if touched:
                self.es.req("POST", "/" + ",".join(sorted(touched)) + "/_refresh")
            with self.conn.transaction():
                D.set_cursor(self.conn.cursor(), "events-" + self.fam, 1, t, rows[-1]["commit_xid"], rows[-1]["ingest_seq"])
            n += len(rows)
        return n


def lag(conn, fam_view, tenants):
    """Events above the family cursor (0 = caught up to what is visible)."""
    tot = 0
    for t in tenants:
        tot += conn.execute("select count(*) from events e where tenant_id=%s and (commit_xid, ingest_seq) > coalesce((select (hwm_xid, hwm_seq) from projection_state where view=%s and version=1 and tenant_id=%s), (0::bigint,0::bigint))", (t, fam_view, t)).fetchone()[0]
    return tot

"""Projector: Postgres -> Elasticsearch mirror, at-least-once and idempotent.

Per tenant and view the cursor is (commit_xid, ingest_seq), read only below
pg_snapshot_xmin so a transaction that commits late can never be skipped. A batch
is: read rows -> bulk with deterministic _id into a write alias with
require_alias=true -> refresh the touched indices -> advance the cursor. The
cursor therefore means "indexed AND searchable"; it is the high-water mark the
read helper uses for the tail scan. A crash anywhere replays the batch.

Tenant fence: the ES stage runs while holding a shared advisory lock on the
tenant; an erasure purge takes it exclusively, so no in-flight batch can write
after the purge.
"""
import time
from collections import defaultdict

from . import derive as D
from . import esx
from .db import iso, xmin
from .esc import ES

LOCK_SQL = "hashtextextended('proj:' || %s, 0)"


class Projector:
    def __init__(self, conn, lockconn, es: ES, recipes=(1,), live_recipe=1, batch=500, tenants=None, fence=True, derive=True):
        self.conn, self.lock, self.es = conn, lockconn, es
        self.recipes, self.live, self.batch = list(recipes), live_recipe, batch
        self.tenant_filter, self.fence, self.do_derive = tenants, fence, derive
        self.known_ev, self.known_fx = set(), set()

    def tenants(self):
        ts = D.tenants(self.conn)
        return [t for t in ts if self.tenant_filter is None or t in self.tenant_filter]

    # ------------------------------------------------------------- fence
    def _lock(self, t):
        if not self.fence:
            return True
        return self.lock.execute(f"select pg_try_advisory_lock_shared({LOCK_SQL})", (t,)).fetchone()[0]

    def _unlock(self, t):
        if self.fence:
            self.lock.execute(f"select pg_advisory_unlock_shared({LOCK_SQL})", (t,))

    # ------------------------------------------------------------- events
    def mirror_events(self, t):
        if not self._lock(t):
            return 0
        try:
            return self._mirror_events(t)
        finally:
            self._unlock(t)

    def _mirror_events(self, t):
        with self.conn.transaction():
            cur = self.conn.cursor()
            cx, cs = D.get_cursor(cur, "events", 1, t)
            xm, rows = D.read_event_batch(cur, t, cx, cs, self.batch)
        if not rows:
            self._advance("events", 1, t, xm - 1, D.BIG)
            return 0
        D.crash("mirror_after_read")
        lines, touched = [], set()
        for r in rows:
            if r["tomb"] or r["plain"] is None:
                continue
            b = esx.bucket_of(r["ts_event"])
            if (t, b) not in self.known_ev:
                D.crash("bucket_create")
                esx.ensure_events_bucket(self.es, t, b)
                self.known_ev.add((t, b))
            w = esx.ev_walias(t, b)
            touched.add(w)
            doc = esx.event_doc(t, r["session_id"], r["seq"], r["event_id"], iso(r["ts_event"]), r["actor"],
                                r["trust_origin"], r["kind"], r["subject_id"], r["plain"])
            lines.append(({"create": {"_index": w, "_id": r["event_id"]}}, doc))   # immutable event: create, 409 = already mirrored
        self._bulk(lines, "mirror_mid_bulk", events=True)
        D.crash("mirror_after_bulk")
        if touched:
            self.es.req("POST", "/" + ",".join(sorted(touched)) + "/_refresh")
        D.crash("mirror_after_refresh")
        self._advance("events", 1, t, rows[-1]["commit_xid"], rows[-1]["ingest_seq"])
        D.crash("mirror_after_cursor")
        return len(rows)

    # -------------------------------------------------------------- facts
    def mirror_facts(self, t, v):
        if not self._lock(t):
            return 0
        try:
            return self._mirror_facts(t, v)
        finally:
            self._unlock(t)

    def _mirror_facts(self, t, v):
        with self.conn.transaction():
            cur = self.conn.cursor()
            cx, cs = D.get_cursor(cur, "facts", v, t)
            xm = xmin(cur)
            cur.execute(
                "select id, subject, predicate, scope, mem_dec(tenant_id, subject_id, object_enc) as object, modality, status, author, "
                "trust_origin, valid_from, valid_to, input_event_ids, recipe_version, subject_id, cue, rev, changed_xid "
                "from facts where tenant_id=%s and recipe_version=%s and (changed_xid, rev) > (%s,%s) and changed_xid < %s "
                "order by changed_xid, rev limit %s", (t, v, cx, cs, xm, self.batch))
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not rows:
            self._advance("facts", v, t, xm - 1, D.BIG)
            return 0
        if (t, v) not in self.known_fx:
            esx.ensure_facts_index(self.es, t, v, live=(v == self.live))
            self.known_fx.add((t, v))
        w = esx.fx_walias(t, v)
        lines = []
        for r in rows:
            if r["object"] is None:
                continue
            r["tenant_id"] = t
            r["valid_from"], r["valid_to"] = iso(r["valid_from"]), (iso(r["valid_to"]) if r["valid_to"] else None)
            lines.append(({"index": {"_index": w, "_id": r["id"], "version": r["rev"], "version_type": "external_gte"}}, esx.fact_doc(r)))
        self._bulk(lines, "mirror_mid_bulk", events=False)
        D.crash("mirror_after_bulk")
        self.es.req("POST", f"/{w}/_refresh")
        D.crash("mirror_after_refresh")
        self._advance("facts", v, t, rows[-1]["changed_xid"], rows[-1]["rev"])
        return len(rows)

    # ------------------------------------------------------------ helpers
    def _bulk(self, lines, crash_name, events):
        for i in range(0, len(lines), 200):
            try:
                esx.bulk_index(self.es, lines[i:i + 200], chunk=200)
            except Exception as e:
                # an index/alias vanished underneath us (rebuild, drop): forget what we know and let the batch replay
                self.known_ev.clear(); self.known_fx.clear()
                raise
            if i == 0 and len(lines) > 200:
                D.crash(crash_name)

    def _advance(self, view, version, t, x, s):
        with self.conn.transaction():
            D.set_cursor(self.conn.cursor(), view, version, t, x, s)

    # --------------------------------------------------------------- loop
    def run_once(self):
        n = 0
        for t in self.tenants():
            if self.do_derive:
                for r in self.recipes:
                    n += D.derive_batch(self.conn, t, r, limit=self.batch, touch=(r == self.live))
            n += self.mirror_events(t)
            for r in self.recipes:
                n += self.mirror_facts(t, r)
        return n

    def run(self, until_idle=True, sleep=0.05, max_seconds=None, idle_rounds=3):
        t0, idle = time.time(), 0
        while True:
            n = self.run_once()
            idle = idle + 1 if n == 0 else 0
            if until_idle and idle >= idle_rounds:
                return
            if max_seconds and time.time() - t0 > max_seconds:
                return
            if n == 0:
                time.sleep(sleep)

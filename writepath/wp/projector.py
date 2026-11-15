"""Projector: Postgres log -> derived facts (Postgres) -> Elasticsearch families. At-least-once, idempotent, fenced.

Fence: the projector takes the per-tenant 'projector' lease (epoch E) and fences ES to E before any write. Each batch
reads its rows in a REPEATABLE READ transaction that also checks the lease epoch, so the rows and the epoch come from
one snapshot. The batch then writes ONLY through w<E> (require_alias) and advances its cursor with lease_epoch=E
(store-fenced). An erasure commits tombstones and the epoch bump in one transaction and moves the aliases before it
purges, so a batch read before the tombstones can only address w<E>, which no longer exists by then.

Late commits: rows are read only below pg_snapshot_xmin, so a transaction that commits after later ones is never
skipped (top-30 row 23)."""
import json

import psycopg

from . import families as F, keys as K, lease as L
from .es import ES
from .log import StaleWrite

BIG = 2 ** 62
FACT_KIND = "fact"


def _noop(_n):
    pass


class LostLease(Exception):
    pass


class Projector:
    def __init__(self, conn, ks: K.KeyService, es: ES, tenant, ttl_s=30.0, batch=500, hook=_noop):
        assert "-" not in tenant
        self.conn, self.ks, self.es, self.t = conn, ks, es, tenant
        self.ttl, self.batch, self.hook = ttl_s, batch, hook
        self.lease = None
        self.stats = {"stale_bulk_items": 0, "stale_cursor": 0, "docs_written": 0}

    # ------------------------------------------------------------ lease
    def start(self, force=False):
        self.lease = L.acquire(self.conn, self.t, L.PROJECTOR, self.ttl, force=force)
        if self.lease is None:
            return False
        F.fence_es(self.es, self.t, self.lease.epoch)
        for f in ("events", "facts", "derive"):
            self._fenced("insert into cursors(tenant_id, family, lease_scope, lease_epoch) values (%s,%s,%s,%s) on conflict (tenant_id, family) do nothing",
                         (self.t, f, L.PROJECTOR, self.lease.epoch))
        return True

    def _fenced(self, sql, args):
        try:
            with self.conn.transaction():
                return self.conn.execute(sql, args).rowcount
        except psycopg.Error as ex:
            if L.is_stale_error(ex):
                self.stats["stale_cursor"] += 1
                raise StaleWrite(str(ex).split("\n")[0]) from None
            raise

    def _snapshot_rows(self, family, sql_rows, args):
        """(xmin, rows) read in ONE repeatable-read snapshot together with the cursor."""
        with self.conn.transaction():
            self.conn.execute("set transaction isolation level repeatable read")
            cx, cs = self.conn.execute("select cx, cs from cursors where tenant_id=%s and family=%s", (self.t, family)).fetchone()
            xm = int(self.conn.execute("select pg_snapshot_xmin(pg_current_snapshot())::text").fetchone()[0])
            rows = self.conn.execute(sql_rows, (self.t, cx, cs, xm) + tuple(args)).fetchall()
        return xm, rows

    def _advance(self, family, x, s):
        self._fenced("update cursors set cx=%s, cs=%s, lease_scope=%s, lease_epoch=%s where tenant_id=%s and family=%s and (cx, cs) < (%s, %s)",
                     (x, s, L.PROJECTOR, self.lease.epoch, self.t, family, x, s))

    def _bulk(self, family, actions):
        if not actions:
            return
        ok, bad = self.es.bulk_fenced(actions)
        self.stats["docs_written"] += ok
        hard = [b for b in bad if b[1] != 409]
        if hard:
            self.stats["stale_bulk_items"] += len(hard)
            raise F.StaleEpoch(f"{family}: {len(hard)} items refused ({hard[0][2]})")
        self.es.req("POST", f"/{F.read_alias(self.t, family)}/_refresh")

    def _keys(self, subjects):
        return self.ks.keys(self.t, subjects)

    # ------------------------------------------------------------ derive facts (Postgres, one fenced transaction)
    def derive(self):
        xm, rows = self._snapshot_rows("derive",
            "select e.event_id, e.subject_id, e.ts_event, e.content_enc, e.commit_xid, e.ingest_seq from events e where e.tenant_id=%s "
            "and (e.commit_xid, e.ingest_seq) > (%s, %s) and e.commit_xid < %s and e.kind = %s "
            "and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) "
            "order by e.commit_xid, e.ingest_seq limit %s", (FACT_KIND, self.batch))
        if not rows:
            self._advance("derive", xm - 1, BIG)
            return 0
        self.hook("derive_after_read")
        keys = self._keys([r[1] for r in rows])
        try:
            with self.conn.transaction():
                for eid, subj, ts, enc, _, _ in rows:
                    plain = K.open_(keys[subj], self.t, subj, eid, enc)
                    if plain is None:
                        continue
                    b = json.loads(plain)
                    fid = K.derive_id(self.t, eid)
                    slot = self.ks.tenant_hmac(self.t, f"slot:{b['entity']}|{b['attribute']}")
                    body = K.seal(keys[subj], self.t, subj, "fact:" + fid, json.dumps({"entity": b["entity"], "attribute": b["attribute"], "value": b["value"]}))
                    n = self.conn.execute("insert into facts(tenant_id, fact_id, subject_id, slot, body_enc, valid_from, input_event_ids, lease_scope, lease_epoch) "
                                          "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing", (self.t, fid, subj, slot, body, ts, [eid], L.PROJECTOR, self.lease.epoch)).rowcount
                    if n:
                        self.conn.execute("update facts set valid_to=%s, rev=nextval('fact_rev'), changed_xid=(pg_current_xact_id()::text)::bigint, lease_scope=%s, lease_epoch=%s "
                                          "where tenant_id=%s and slot=%s and fact_id<>%s and valid_to is null and valid_from < %s",
                                          (ts, L.PROJECTOR, self.lease.epoch, self.t, slot, fid, ts))
                self.conn.execute("update cursors set cx=%s, cs=%s, lease_scope=%s, lease_epoch=%s where tenant_id=%s and family='derive'",
                                  (rows[-1][4], rows[-1][5], L.PROJECTOR, self.lease.epoch, self.t))
        except psycopg.Error as ex:
            if L.is_stale_error(ex):
                self.stats["stale_cursor"] += 1
                raise StaleWrite("derive") from None
            raise
        return len(rows)

    # ------------------------------------------------------------ events -> ES
    def mirror_events(self):
        xm, rows = self._snapshot_rows("events",
            "select e.event_id, e.session_id, e.seq, e.kind, e.subject_id, e.actor, e.trust_origin, e.ts_event, e.content_enc, e.commit_xid, e.ingest_seq "
            "from events e where e.tenant_id=%s and (e.commit_xid, e.ingest_seq) > (%s, %s) and e.commit_xid < %s "
            "and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) "
            "order by e.commit_xid, e.ingest_seq limit %s", (self.batch,))
        if not rows:
            self._advance("events", xm - 1, BIG)
            return 0
        self.hook("mirror_after_read")
        keys = self._keys([r[4] for r in rows])
        acts = []
        for eid, sid, seq, kind, subj, actor, trust, ts, enc, _, _ in rows:
            plain = K.open_(keys[subj], self.t, subj, eid, enc)
            if plain is None:
                continue
            acts.append(({"create": {"_index": F.write_alias(self.t, "events", self.lease.epoch), "_id": eid}},
                         {"event_id": eid, "session_id": sid, "seq": seq, "kind": kind, "subject_id": subj, "actor": actor, "trust_origin": trust,
                          "ts_event": ts.isoformat(), "content": plain}))
        self.hook("mirror_before_bulk")
        self._bulk("events", acts)
        self._advance("events", rows[-1][9], rows[-1][10])
        return len(rows)

    # ------------------------------------------------------------ facts -> ES
    def mirror_facts(self):
        xm, rows = self._snapshot_rows("facts",
            "select f.fact_id, f.subject_id, f.body_enc, f.valid_from, f.valid_to, f.input_event_ids, f.rev, f.changed_xid from facts f where f.tenant_id=%s "
            "and (f.changed_xid, f.rev) > (%s, %s) and f.changed_xid < %s "
            "order by f.changed_xid, f.rev limit %s", (self.batch,))
        if not rows:
            self._advance("facts", xm - 1, BIG)
            return 0
        self.hook("facts_after_read")
        acts = fact_actions(self.ks, self.t, rows, F.write_alias(self.t, "facts", self.lease.epoch))
        self._bulk("facts", acts)
        self._advance("facts", rows[-1][7], rows[-1][6])
        return len(rows)

    def step(self):
        """One round. Returns rows handled; on a lost lease or refused write, drops the lease and returns -1."""
        if self.lease is None and not self.start():
            return 0
        try:
            return self.derive() + self.mirror_events() + self.mirror_facts()
        except (StaleWrite, F.StaleEpoch):
            self.lease = None
            return -1

    def run(self, max_rounds=1000):
        idle = 0
        for _ in range(max_rounds):
            n = self.step()
            idle = idle + 1 if n == 0 else 0
            if idle >= 2:
                return True
        return False

    def stop(self):
        if self.lease:
            L.release(self.conn, self.lease)
            self.lease = None


def event_actions(ks, t, rows, target):
    keys = ks.keys(t, [r[4] for r in rows])
    acts = []
    for eid, sid, seq, kind, subj, actor, trust, ts, enc in rows:
        plain = K.open_(keys[subj], t, subj, eid, enc)
        if plain is None:
            continue
        acts.append(({"create": {"_index": target, "_id": eid}},
                     {"event_id": eid, "session_id": sid, "seq": seq, "kind": kind, "subject_id": subj, "actor": actor, "trust_origin": trust,
                      "ts_event": ts.isoformat(), "content": plain}))
    return acts


def fact_actions(ks, t, rows, target):
    keys = ks.keys(t, [r[1] for r in rows])
    acts = []
    for fid, subj, enc, vf, vt, inputs, rev, *_ in rows:
        plain = K.open_(keys[subj], t, subj, "fact:" + fid, enc)
        if plain is None:
            continue
        b = json.loads(plain)
        acts.append(({"index": {"_index": target, "_id": fid, "version": rev, "version_type": "external_gte"}},
                     {"fact_id": fid, "subject_id": subj, "entity": b["entity"], "attribute": b["attribute"], "value": b["value"],
                      "valid_from": vf.isoformat(), "valid_to": vt.isoformat() if vt else None, "input_event_ids": list(inputs), "rev": rev}))
    return acts

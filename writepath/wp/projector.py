"""Projector: Postgres log -> derived facts (Postgres) -> Elasticsearch families. At-least-once, idempotent, fenced.

Fence: the projector takes the per-tenant 'projector' lease (epoch E), renews it every step (heartbeat), and fences
ES to E before any write. Each batch reads its rows and cursor in one REPEATABLE READ snapshot, writes ONLY through
w<E> (require_alias) and advances its cursor with lease_epoch=E (store-fenced): a projector that lost the lease can
no longer write ES (alias gone) or Postgres (WPS01). Erasure does not touch the projector lease: rows of an erased
subject are filtered here from the moment the subject is marked, and a batch read before that is stopped per
document by the erasure placeholders (409), whichever arrives first (families.py).

Late commits: rows are read only below pg_snapshot_xmin, so a transaction that commits after later ones is never
skipped (top-30 row 23).

Broker mode (D6, WP_BROKER set or a broker client passed): the projector holds no Elasticsearch credential of its own.
start() asks the key broker (wp/broker.py) for a writer key for (tenant, epoch, holder); the broker checks the lease,
runs the alias CAS itself and mints a key that can write only through -w<E>. The projector never runs fence_es and
never reads aliases; a refused bulk item is classified by the lease (renew raises LeaseLost: a confirmed loss; else
ProjectionError). A broker refusal of the lease is a confirmed loss (LeaseLost). There is no fallback to a role-key
file or the admin key. The key is renewed through the broker before it expires (KEY_REFRESH_S)."""
import json
import time

import psycopg

from . import broker as B, families as F, keys as K, lease as L
from .es import ES
from .log import StaleWrite

BIG = 2 ** 62
FACT_KIND = "fact"
KEY_REFRESH_S = 45 * 60            # broker keys expire after broker.KEY_TTL (1 h)


def _noop(_n, _data=None):
    pass


class LostLease(Exception):
    pass


class Projector:
    def __init__(self, conn, ks: K.KeyService, es, tenant, ttl_s=30.0, batch=500, hook=_noop, broker=None):
        """es: the projector's ES client, or None in broker mode (then any es passed is ignored: keys come only from
        the broker)."""
        L.check_tenant(tenant)
        self.broker = broker if broker is not None else (B.Client() if B.broker_mode() else None)
        self.conn, self.ks, self.t = conn, ks, tenant
        self.es = None if self.broker is not None else es
        self.ttl, self.batch, self.hook = ttl_s, batch, hook
        self.lease = None
        self.key_id, self._key_at = None, 0.0
        self.stats = {"stale_bulk_items": 0, "stale_cursor": 0, "docs_written": 0}

    # ------------------------------------------------------------ lease
    def _broker_key(self):
        """A writer key for the live lease. The broker re-runs its (idempotent) CAS for this epoch."""
        try:
            k = self.broker.writer_key(self.t, self.lease.epoch, self.lease.holder)
        except B.Refused as e:
            if "lease_not_current" in str(e):
                raise L.LeaseLost(f"broker: lease not current for e{self.lease.epoch}") from None
            raise
        self.es, self.key_id, self._key_at = B.es_with_key(k, "projector"), k["id"], time.monotonic()

    def start(self, force=False):
        self.lease = L.acquire(self.conn, self.t, L.PROJECTOR, self.ttl, force=force)
        if self.lease is None:
            return False
        if self.broker is not None:
            self._broker_key()
        else:
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
            # Review-2 N1: a refused item is a lost epoch only when that is confirmed. At 1fcf3f7 every refusal became
            # StaleEpoch, so a 429 or a mapping error looked like a lost race and the work looked done.
            kinds = sorted({f"{st} {et}" for _, st, et in hard})[:3]
            if self.broker is not None:
                # the writer key cannot read aliases (by design); the lease decides (Postgres is the authority)
                try:
                    L.renew(self.conn, self.lease, self.ttl)
                except L.LeaseLost:
                    self.stats["stale_bulk_items"] += len(hard)
                    raise
                raise F.ProjectionError(f"{family}: {len(hard)} items refused while the lease is ours ({kinds})")
            if F.newer_epoch_visible(self.es, self.t, family, self.lease.epoch):
                self.stats["stale_bulk_items"] += len(hard)
                raise F.StaleEpoch(f"{family}: {len(hard)} items refused under a newer epoch ({kinds})")
            L.renew(self.conn, self.lease, self.ttl)       # LeaseLost here is a confirmed loss too
            raise F.ProjectionError(f"{family}: {len(hard)} items refused while the lease is ours ({kinds})")
        self.es.req("POST", f"/{F.read_alias(self.t, family)}/_refresh")

    def _keys(self, subjects):
        return self.ks.keys(self.t, subjects)

    # ------------------------------------------------------------ derive facts (Postgres, one fenced transaction)
    def derive(self):
        xm, rows = self._snapshot_rows("derive",
            "select e.event_id, e.subject_id, e.ts_event, e.content_enc, e.commit_xid, e.ingest_seq from events e where e.tenant_id=%s "
            "and (e.commit_xid, e.ingest_seq) > (%s, %s) and e.commit_xid < %s and e.kind = %s "
            "and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) "
            "and not exists (select 1 from subjects sj where sj.tenant_id=e.tenant_id and sj.subject_id=e.subject_id and sj.erased) "
            "and not exists (select 1 from subjects sm where sm.tenant_id=e.tenant_id and sm.subject_id = any(e.mentions) and sm.erased) "
            "and e.content_enc is not null "
            "order by e.commit_xid, e.ingest_seq limit %s", (FACT_KIND, self.batch))
        if not rows:
            self._advance("derive", xm - 1, BIG)
            return 0
        self.hook("derive_after_read", rows)
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
            "select e.event_id, e.session_id, e.seq, e.kind, e.subject_id, e.actor, e.trust_origin, e.ts_event, e.content_enc, e.commit_xid, e.ingest_seq, e.mentions "
            "from events e where e.tenant_id=%s and (e.commit_xid, e.ingest_seq) > (%s, %s) and e.commit_xid < %s "
            "and not exists (select 1 from tombstones tb where tb.tenant_id=e.tenant_id and tb.event_id=e.event_id) "
            "and not exists (select 1 from subjects sj where sj.tenant_id=e.tenant_id and sj.subject_id=e.subject_id and sj.erased) "
            "and not exists (select 1 from subjects sm where sm.tenant_id=e.tenant_id and sm.subject_id = any(e.mentions) and sm.erased) "
            "and e.content_enc is not null "
            "order by e.commit_xid, e.ingest_seq limit %s", (self.batch,))
        if not rows:
            self._advance("events", xm - 1, BIG)
            return 0
        docs = decode_events(self.ks, self.t, rows)        # plaintext exists from here on, as in any real projector
        self.hook("mirror_after_read", rows)
        acts = to_actions(docs, F.write_alias(self.t, "events", self.lease.epoch))   # target = epoch from acquire()
        self.hook("mirror_before_bulk", acts)
        self._bulk("events", acts)
        self._advance("events", rows[-1][9], rows[-1][10])
        return len(rows)

    # ------------------------------------------------------------ facts -> ES
    def mirror_facts(self):
        xm, rows = self._snapshot_rows("facts",
            "select f.fact_id, f.subject_id, f.body_enc, f.valid_from, f.valid_to, f.input_event_ids, f.rev, f.changed_xid from facts f where f.tenant_id=%s "
            "and (f.changed_xid, f.rev) > (%s, %s) and f.changed_xid < %s "
            "and not exists (select 1 from tombstones tb where tb.tenant_id=f.tenant_id and tb.event_id = any(f.input_event_ids)) "
            "and not exists (select 1 from subjects sj where sj.tenant_id=f.tenant_id and sj.subject_id=f.subject_id and sj.erased) "
            "order by f.changed_xid, f.rev limit %s", (self.batch,))
        if not rows:
            self._advance("facts", xm - 1, BIG)
            return 0
        docs = decode_facts(self.ks, self.t, rows)
        self.hook("facts_after_read", rows)
        acts = to_actions(docs, F.write_alias(self.t, "facts", self.lease.epoch))
        self._bulk("facts", acts)
        self._advance("facts", rows[-1][7], rows[-1][6])
        return len(rows)

    def step(self):
        """One round. Returns rows handled; on a CONFIRMED loss of the lease (StaleWrite, StaleEpoch, LeaseLost) it
        drops the lease and returns -1. start() sits inside the handler: a forced takeover landing between acquire and
        the alias CAS or before the cursor insert means this projector lost the start-up race, not that it crashed
        (amendment 19).
        Every other error propagates (review-2 N1: an Elasticsearch failure is never a lost race and never idle
        success). On every exit with an error the projector releases ITS OWN lease first, so the work is retryable at
        once instead of after the TTL. The release matches epoch and holder (wp_lease_release), so it is a no-op when
        another projector already holds a newer epoch, and it is best-effort: it never replaces the original error."""
        try:
            if self.lease is None and not self.start():
                return 0
            L.renew(self.conn, self.lease, self.ttl)          # heartbeat: a live projector never expires
            if self.broker is not None and time.monotonic() - self._key_at > KEY_REFRESH_S:
                self._broker_key()
            return self.derive() + self.mirror_events() + self.mirror_facts()
        except (StaleWrite, F.StaleEpoch, L.LeaseLost):
            self._drop_lease()
            return -1
        except BaseException:
            self._drop_lease()
            raise

    def _drop_lease(self):
        lease, self.lease = self.lease, None
        if lease is None:
            return
        try:
            L.release(self.conn, lease)
        except Exception:  # noqa: BLE001  (best-effort: the connection may be the thing that failed)
            self.stats["release_failed"] = self.stats.get("release_failed", 0) + 1

    def run(self, max_rounds=1000):
        """Steps until two idle rounds (True) or max_rounds (False). True means there is nothing for THIS projector to
        do: the work is projected, or another live projector holds the lease. Errors propagate (review-2 N1)."""
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


def decode_events(ks, t, rows):
    """[(action_template, doc)] for event rows (decrypted; rows whose key is gone are skipped)."""
    keys = ks.keys(t, [r[4] for r in rows])
    out = []
    for eid, sid, seq, kind, subj, actor, trust, ts, enc, _cx, _cs, mentions, *_ in rows:
        plain = K.open_(keys[subj], t, subj, eid, enc) if enc is not None else None
        if plain is None:
            continue
        out.append(({"create": {"_id": eid}}, {"event_id": eid, "session_id": sid, "seq": seq, "kind": kind, "subject_id": subj, "actor": actor,
                                                "trust_origin": trust, "ts_event": ts.isoformat(), "content": plain, "mentions": list(mentions or [])}))
    return out


def decode_facts(ks, t, rows):
    keys = ks.keys(t, [r[1] for r in rows])
    out = []
    for fid, subj, enc, vf, vt, inputs, rev, *_ in rows:
        plain = K.open_(keys[subj], t, subj, "fact:" + fid, enc)
        if plain is None:
            continue
        b = json.loads(plain)
        out.append(({"index": {"_id": fid, "version": rev, "version_type": "external_gte"}},
                    {"fact_id": fid, "subject_id": subj, "entity": b["entity"], "attribute": b["attribute"], "value": b["value"],
                     "valid_from": vf.isoformat(), "valid_to": vt.isoformat() if vt else None, "input_event_ids": list(inputs), "rev": rev}))
    return out


def to_actions(docs, target):
    """Bind decoded docs to a write target (an alias; es.bulk_fenced sends require_alias=true)."""
    acts = []
    for tmpl, doc in docs:
        (op, meta), = tmpl.items()
        acts.append(({op: dict(meta, _index=target)}, doc))
    return acts

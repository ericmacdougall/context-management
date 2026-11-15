"""Integration tests that need Postgres (events + key service) and Elasticsearch (run on t2-dev):
  ~/emlab-16096/systems/venv/bin/python exp/run_integration.py
They use tenant names starting with 'ti' and reset both databases."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from memsys import db, esx, store, projector, derive, erasure, chain, kms
    from memsys.esc import ES
    HAVE = (db.HOME / "pg_port").exists() and (db.HOME / "kms_port").exists()
except Exception:
    HAVE = False

TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def ev(i, text, subject="u1", trust="user_typed", actor="user", **kw):
    return dict(ts_event=TS + dt.timedelta(minutes=i), actor=actor, trust_origin=trust, kind="message", subject_id=subject, content=text, **kw)


@unittest.skipUnless(HAVE, "needs the t2-dev Postgres/ES")
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect(autocommit=True)
        cls.lock = db.connect(autocommit=True)
        cls.es = ES()
        db.reset_schema(cls.conn)
        esx.install_templates(cls.es)
        for t in ("ti1", "ti2"):
            esx.drop_tenant(cls.es, t)

    def facts(self, tenant, subject):
        rows = self.conn.execute("select slot_enc, object_enc, subject_id, status from facts where tenant_id=%s", (tenant,)).fetchall()
        k = kms.get()
        out = {}
        for (se, oe, sid, st) in rows:
            s = derive.decode_slot(k.dec_many(tenant, [(sid, se)])[0])
            if s[0] == subject:
                out[k.dec_many(tenant, [(sid, oe)])[0]] = st
        return out

    def test_01_outbox_cursor_never_skips_a_late_commit(self):
        store.append_events(self.conn, "ti1", "seed", [ev(0, "warm up")])
        holder = db.connect(autocommit=False)
        from memsys.db import iso
        holder.execute("select 1")
        enc = kms.get().enc("ti1", "u1", "Zed's city is Oslo")
        r = (holder.execute("insert into sessions(tenant_id,session_id) values ('ti1','held') on conflict do nothing"), None)
        last = "0" * 64
        eid = "ti1/held/1"
        h = chain.event_hash(last, "ti1", "held", 1, eid, iso(TS), "user", "user_typed", "message", "u1", enc)
        holder.execute("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash) "
                       "values ('ti1','held',1,%s,%s,'u1','user','user_typed','message',%s,%s,%s)", (eid, TS, enc, last, h))
        holder.execute("update sessions set next_seq=2, last_hash=%s where tenant_id='ti1' and session_id='held'", (h,))
        store.append_events(self.conn, "ti1", "other", [ev(1, "Yan's city is Rome")])
        p = projector.Projector(self.conn, self.lock, self.es, tenants=["ti1"])
        for _ in range(3):
            p.run_once()
        hx = self.conn.execute("select hwm_xid from projection_state where view='events' and tenant_id='ti1'").fetchone()[0]
        axid = holder.execute("select (pg_current_xact_id()::text)::bigint").fetchone()[0]
        self.assertLess(hx, axid, "cursor must not pass the still-open transaction")
        holder.commit(); holder.close()
        for _ in range(3):
            p.run_once()
        got = {h["_id"] for h in self.es.req("POST", "/sys-ti1-events/_search", {"size": 10, "_source": False})[1]["hits"]["hits"]}
        self.assertEqual(got, {"ti1/held/1", "ti1/other/1", "ti1/seed/1"})

    def test_02_events_append_only_and_trust_constraint(self):
        import psycopg
        store.append_events(self.conn, "ti2", "s", [ev(0, "hello")])
        with self.assertRaises(psycopg.errors.RaiseException):
            self.conn.execute("update events set kind='x' where tenant_id='ti2'")
        with self.assertRaises(psycopg.errors.RaiseException):
            self.conn.execute("delete from events where tenant_id='ti2'")
        with self.assertRaises(psycopg.errors.CheckViolation):          # an assistant event cannot claim user_typed
            store.append_events(self.conn, "ti2", "s", [ev(1, "Zed's city is Oslo", actor="assistant", trust="user_typed")])
        with self.assertRaises(psycopg.errors.CheckViolation):
            store.append_events(self.conn, "ti2", "s", [ev(1, "x", actor="tool", trust="user_typed")])

    def test_03_hash_chain_survives_key_destruction_and_append_is_refused_after(self):
        store.append_events(self.conn, "ti2", "chain", [ev(0, "a", subject="uc"), ev(1, "b", subject="uc")])
        store.append_events(self.conn, "ti2", "chain", [ev(2, "c", subject="uc")])
        self.assertTrue(chain.verify_chain(list(store.session_rows(self.conn, "ti2", "chain")))[0])
        erasure.erase_subject(self.conn, self.lock, self.es, "ti2", "uc")
        self.assertTrue(chain.verify_chain(list(store.session_rows(self.conn, "ti2", "chain")))[0])
        self.assertEqual(store.read_plain(self.conn, "ti2", "subject_id=%s", ("uc",))[0]["plain"], None)
        with self.assertRaises(kms.SubjectErased):
            store.append_events(self.conn, "ti2", "chain", [ev(3, "late", subject="uc")])

    def test_04_erased_superseder_reinstates_predecessor(self):
        store.append_events(self.conn, "ti1", "ra", [ev(10, "Mia's city is Rome", subject="uA")])
        store.append_events(self.conn, "ti1", "rb", [ev(11, "Mia's city is Oslo", subject="uB")])
        p = projector.Projector(self.conn, self.lock, self.es, tenants=["ti1"])
        for _ in range(4):
            p.run_once()
        self.assertEqual(self.facts("ti1", "Mia"), {"Rome": "superseded", "Oslo": "current"})
        erasure.erase_subject(self.conn, self.lock, self.es, "ti1", "uB")
        self.assertEqual(self.facts("ti1", "Mia"), {"Rome": "current"})

    def test_05_client_event_id_makes_append_idempotent(self):
        a, _ = store.append_events(self.conn, "ti2", "idem", [ev(0, "once", client_event_id="c-1")])
        b, _ = store.append_events(self.conn, "ti2", "idem", [ev(0, "once", client_event_id="c-1")])
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("select count(*) from events where tenant_id='ti2' and session_id='idem'").fetchone()[0], 1)

    def test_06_erasure_job_is_durable_and_sweeper_completes_it(self):
        store.append_events(self.conn, "ti2", "dj", [ev(0, "Dee's city is Paris", subject="uD")])
        with self.assertRaises(RuntimeError):
            erasure.erase_subject(self.conn, self.lock, self.es, "ti2", "uD", fail_at="before_es_purge")
        st = self.conn.execute("select state from erasure_jobs where tenant_id='ti2' and subject_id='uD'").fetchone()[0]
        self.assertEqual(st, "key_destroyed")
        self.assertEqual(erasure.sweep(self.conn, self.lock, self.es).popitem()[1], "done")
        self.assertEqual(self.conn.execute("select state from erasure_jobs where tenant_id='ti2' and subject_id='uD'").fetchone()[0], "done")


if __name__ == "__main__":
    unittest.main()

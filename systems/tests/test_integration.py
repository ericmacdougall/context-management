"""Integration tests that need Postgres and Elasticsearch (run on t2-dev):
  ~/emlab-16096/systems/venv/bin/python -m unittest tests.test_integration
They use tenant names starting with 'ti' and reset the Postgres schema."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from memsys import db, esx, store, projector, derive, erasure, chain
    from memsys.esc import ES
    HAVE = (db.HOME / "pg_port").exists()
except Exception:
    HAVE = False

TS = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def ev(i, text, subject="u1", trust="user_typed", actor="user"):
    return dict(ts_event=TS + dt.timedelta(minutes=i), actor=actor, trust_origin=trust, kind="message", subject_id=subject, content=text)


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

    def test_01_outbox_cursor_never_skips_a_late_commit(self):
        store.append_events(self.conn, "ti1", "seed", [ev(0, "warm up")])   # creates the u1 key row (a concurrent first insert would block on it)
        holder = db.connect(autocommit=False)
        # txn A takes the lower xid and stays open
        with holder.cursor() as c:
            c.execute("insert into sessions(tenant_id,session_id) values ('ti1','held') on conflict do nothing")
            holder.commit()
        from memsys.db import iso
        holder.execute("select 1")  # begin
        r = holder.execute("select next_seq,last_hash from sessions where tenant_id='ti1' and session_id='held' for update").fetchone()
        enc = holder.execute("select mem_enc('ti1','u1',%s)", ("Zed's city is Oslo",)).fetchone()[0]
        eid = "ti1/held/1"
        h = chain.event_hash(r[1], "ti1", "held", 1, eid, iso(TS), "user", "user_typed", "message", "u1", bytes(enc))
        holder.execute("insert into events(tenant_id,session_id,seq,event_id,ts_event,subject_id,actor,trust_origin,kind,content_enc,prev_hash,hash) "
                       "values ('ti1','held',1,%s,%s,'u1','user','user_typed','message',%s,%s,%s)", (eid, TS, enc, r[1], h))
        # txn B starts later (higher xid) and commits first
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
        got = {i for _, i, _ in self.es.req("POST", "/sys-ti1-events/_search", {"size": 10, "_source": False})[1]["hits"]["hits"] and
               [(h["_index"], h["_id"], 0) for h in self.es.req("POST", "/sys-ti1-events/_search", {"size": 10, "_source": False})[1]["hits"]["hits"]]}
        self.assertEqual(got, {"ti1/held/1", "ti1/other/1", "ti1/seed/1"})

    def test_02_events_append_only(self):
        store.append_events(self.conn, "ti2", "s", [ev(0, "hello")])
        import psycopg
        with self.assertRaises(psycopg.errors.RaiseException):
            self.conn.execute("update events set kind='x' where tenant_id='ti2'")
        with self.assertRaises(psycopg.errors.RaiseException):
            self.conn.execute("delete from events where tenant_id='ti2'")

    def test_03_hash_chain_survives_key_destruction(self):
        store.append_events(self.conn, "ti2", "chain", [ev(0, "a"), ev(1, "b")], )
        store.append_events(self.conn, "ti2", "chain", [ev(2, "c")])
        ok = chain.verify_chain(list(store.session_rows(self.conn, "ti2", "chain")))
        self.assertTrue(ok[0])
        self.conn.execute("update keys set key=null, destroyed_at=now() where tenant_id='ti2' and subject_id='u1'")
        self.assertTrue(chain.verify_chain(list(store.session_rows(self.conn, "ti2", "chain")))[0])
        self.assertIsNone(self.conn.execute("select mem_dec(tenant_id,subject_id,content_enc) from events where tenant_id='ti2' and session_id='chain' limit 1").fetchone()[0])

    def test_04_erased_superseder_reinstates_predecessor(self):
        store.append_events(self.conn, "ti1", "ra", [ev(10, "Mia's city is Rome", subject="uA")])
        store.append_events(self.conn, "ti1", "rb", [ev(11, "Mia's city is Oslo", subject="uB")])
        p = projector.Projector(self.conn, self.lock, self.es, tenants=["ti1"])
        for _ in range(4):
            p.run_once()
        st = dict(self.conn.execute("select mem_dec(tenant_id,subject_id,object_enc), status from facts where tenant_id='ti1' and subject='Mia'").fetchall())
        self.assertEqual(st, {"Rome": "superseded", "Oslo": "current"})
        erasure.erase_subject(self.conn, self.lock, self.es, "ti1", "uB")
        st = dict(self.conn.execute("select mem_dec(tenant_id,subject_id,object_enc), status from facts where tenant_id='ti1' and subject='Mia'").fetchall())
        self.assertEqual(st, {"Rome": "current"})


if __name__ == "__main__":
    unittest.main()

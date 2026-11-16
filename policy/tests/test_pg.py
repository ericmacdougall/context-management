"""Postgres proofs (run on the lab host against the experiment's own container). Skipped unless POL_PG_PORT, POL_PG_SUPER_PASSFILE,
POL_PG_APP_PASSWORD_FILE and POL_PG_APP_PASSFILE are set (paths to 0600 files; no secret is ever in the environment)."""
import os
import threading
import time
import unittest

try:
    import psycopg
    from pol import _core, pgcore
except ImportError:              # pragma: no cover
    psycopg = None

ENV = ("POL_PG_PORT", "POL_PG_SUPER_PASSFILE", "POL_PG_APP_PASSWORD_FILE", "POL_PG_APP_PASSFILE")
T = "tenant-a"
CFG = {"tenant": T, "principals": {"alice": "human", "bob": "human", "bot": "agent", "x": "analyst", "admin": "human"},
       "authority": {"r1": ["alice", "bob", "bot"], "r2": ["alice"]}, "proposers": {"r1": ["x"]}, "admins": ["admin"]}
LAB = {"tenants": [T], "acl": [], "regions": None, "sensitivity": 0, "models": None, "banned": [], "local_only": False, "restricted": False,
       "holds": [], "subjects": []}


def ev(t, pid, n, **kw):
    ch = "owner_ui" if CFG["principals"].get(pid) == "human" else "agent_api"
    d = {"type": t, "id": "%s/%s" % (pid, n), "idem": "k-%s" % n, "tenant": T, "principal": pid, "channel": ch}
    d.update(kw)
    return d


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class PgProofs(unittest.TestCase):
    port = int(os.environ.get("POL_PG_PORT") or 0)

    def app(self):
        return pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"])

    def sup(self):
        return pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "pol-test-super")

    def setUp(self):
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG})
        self.c = self.app()
        self.e = pgcore.PgEngine(self.c, T)

    def tearDown(self):
        self.c.close()

    def test_isolation_level_explicit(self):
        self.assertEqual(self.c.isolation_level, psycopg.IsolationLevel.READ_COMMITTED)
        with self.c.transaction():
            self.assertEqual(self.c.execute("show transaction_isolation").fetchone()[0], "read committed")

    def test_passwordless_connection_refused(self):
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(host="127.0.0.1", port=self.port, dbname="pol", user="dc_app", passfile="/dev/null", password="", connect_timeout=5)
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(host="127.0.0.1", port=self.port, dbname="pol", user="postgres", passfile="/dev/null", connect_timeout=5)

    def test_events_append_only_for_app_and_owner(self):
        self.assertEqual(self.e._submit(ev("supersede", "alice", 1, req="r1", value_ref="v1", label=LAB, based_on=0))[0], "superseded")
        for stmt in ("update dc.events set code='x'", "delete from dc.events", "truncate dc.events", "alter table dc.events disable trigger events_no_update",
                     "update dc.principal set admin=true", "insert into dc.role values ('tenant-a','r1','x','decide')", "update dc.chainhead set n=0"):
            with self.assertRaises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException), msg=stmt):
                with self.c.transaction():
                    self.c.execute(stmt)
        s = self.sup()
        try:
            for stmt in ("update dc.events set code='x'", "delete from dc.events", "truncate dc.events"):
                with self.assertRaises(psycopg.errors.RaiseException, msg=stmt):
                    with s.transaction():
                        s.execute("set local role dc_owner")
                        s.execute(stmt)
        finally:
            s.close()
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.c.transaction():
                self.c.execute("select dc.apply_authority(1)")                 # not an authority event

    def test_hash_chain_verifies_and_detects_tampering(self):
        for i in range(12):
            self.e._submit(ev("propose", "x", i, req="r1", value_ref="v%d" % i, label=LAB, based_on=0))
        self.assertEqual(tuple(self.e._verify_chain()), (12, True))
        s = self.sup()                                   # control: a superuser bypassing the triggers rewrites one row
        try:
            with s.transaction():
                s.execute("set local session_replication_role = replica")
                s.execute("update dc.events set code='granted' where chain_n=7")
        finally:
            s.close()
        self.assertEqual(tuple(self.e._verify_chain()), (12, False))

    def test_retry_returns_original_outcome_including_concurrent_twins(self):
        e1 = ev("supersede", "alice", "s", req="r1", value_ref="v1", label=LAB, based_on=0)
        self.assertEqual(self.e._submit(e1), ("superseded", None))
        self.assertEqual(self.e._submit(dict(e1)), ("duplicate", "superseded"))
        self.assertEqual(self.e._submit(dict(e1, value_ref="v2")), ("idem_conflict", "superseded"))
        # twins on two connections, released together
        out, conns = [], [self.app(), self.app()]
        twin = ev("supersede", "bob", "t", req="r1", value_ref="vb", label=LAB, based_on=1)
        bar = threading.Barrier(2)

        def run(c):
            bar.wait()
            out.append(pgcore.PgEngine(c, T)._submit(dict(twin)))
        th = [threading.Thread(target=run, args=(c,)) for c in conns]
        [t.start() for t in th]
        [t.join() for t in th]
        [c.close() for c in conns]
        self.assertEqual(sorted(out), [("duplicate", "superseded"), ("superseded", None)])

    def test_revoke_through_definer_function_invalidates_open_items(self):
        for i in range(3):
            self.e._submit(ev("propose", "x", i, req="r1", value_ref="v", label=LAB, based_on=0))
        self.assertEqual(self.e._submit(ev("revoke", "admin", "rv", target="x"))[0], "revoked")
        comp = self.e._comparable()
        self.assertEqual(comp["revoked"], ["x"])
        self.assertEqual(set(comp["proposals"]["r1"].values()), {"invalidated"})
        self.assertEqual(self.c.execute("select open_n from dc.head where req='r1'").fetchone()[0], 0)
        self.assertEqual(self.e._submit(ev("propose", "x", 9, req="r1", value_ref="v", label=LAB, based_on=0))[0], "principal_revoked")
        self.assertEqual(self.c.execute("select count(*) from dc.audit where code='principal_revoked'").fetchone()[0], 1)

    def test_proposal_cost_linear_to_20000(self):
        """Pre-registered (THRESHOLDS amendment 3): 20,000 proposals created and closed (analyst proposes, agent rejects); the mean time
        per event of each of the last three 2,000-proposal blocks is below 3x the median of the first three blocks."""
        blocks, t0 = [], time.perf_counter()
        for i in range(20000):
            a = self.e._submit(ev("propose", "x", "p%d" % i, req="r1", value_ref="v%d" % i, label=LAB, based_on=0))
            b = self.e._submit(ev("decide", "bot", "d%d" % i, req="r1", verb="reject", proposal="x/p%d" % i,
                                  proposal_hash=_core.proposal_hash("x/p%d" % i, "v%d" % i, "x"), based_on=0))
            self.assertEqual((a[0], b[0]), ("proposed", "decided_reject"))
            if (i + 1) % 2000 == 0:
                t1 = time.perf_counter()
                blocks.append(t1 - t0)
                t0 = t1
        first = sorted(blocks[:3])[1]
        print("\nPG N2 blocks (s per 2000 proposals): " + " ".join("%.2f" % b for b in blocks))
        self.assertEqual(self.c.execute("select count(*) from dc.props").fetchone()[0], 20000)
        self.assertEqual(self.e._head("r1")["version"], 0)
        self.assertLess(max(blocks[-3:]) / first, 3.0)


if __name__ == "__main__":
    unittest.main()

"""Live Postgres test (review B1 at 76c9276): after the supplied schema setup, the key broker's role can run its lease
check and nothing more.

Builds the schema FROM SCRATCH in a throwaway database exactly as `wp.setup --reset` does (schema public recreated,
owned by wp_owner; schema.sql applied as wp_owner), grants connect to wp_broker, then as the wp_broker LOGIN (its own
pgpass file):
  P1  select wp_lease_holder_current(...)  -> false for an absent lease (callable)
  P2  select from leases                    -> 42501 insufficient_privilege (no direct table access)
  P3  a lease taken through wp_lease_acquire (as the admin) makes P1 true for its exact (tenant, scope, epoch, holder) only
The database is dropped afterwards. Runs only with WP_PG_LIVE=1 on the lab host (needs this stream's Postgres and
setup's login roles). SCHEMA_UNDER_TEST names another schema.sql (failing-first: 290990e, before 4338c2b).
usage (on t2-dev, from WP_HOME/src): WP_PG_LIVE=1 ../venv/bin/python -m unittest tests.test_schema_broker_pg -v"""
import os
import pathlib
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("WP_PG_LIVE") == "1", "live Postgres test: set WP_PG_LIVE=1 on the lab host")
class BrokerSchemaFromScratch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg  # noqa: F401
        from wp import db
        cls.db = db
        cls.name = f"wp_b1check_{uuid.uuid4().hex[:8]}"
        a = db.admin(db="postgres")
        a.execute(f'create database "{cls.name}"')
        a.execute(f'revoke connect on database "{cls.name}" from public')
        a.execute(f'grant connect on database "{cls.name}" to wp_broker')
        a.close()
        ev = db.admin(db=cls.name)
        ev.execute("drop schema public cascade")
        ev.execute("create schema public authorization wp_owner")
        ev.execute("set role wp_owner")
        ev.execute(pathlib.Path(os.environ.get("SCHEMA_UNDER_TEST") or HERE / "wp" / "schema.sql").read_text())
        ev.execute("reset role")
        cls.ev = ev

    @classmethod
    def tearDownClass(cls):
        cls.ev.close()
        a = cls.db.admin(db="postgres")
        a.execute(f'drop database if exists "{cls.name}" with (force)')
        a.close()

    def broker(self):
        return self.db.connect(db=self.name, application_name="wp-broker-b1", role=self.db.BROKER)

    def test_p1_lease_check_callable(self):
        c = self.broker()
        try:
            got = c.execute("select wp_lease_holder_current('b1t', 'projector', 1, 'h')").fetchone()[0]
        finally:
            c.close()
        self.assertIs(got, False)

    def test_p2_no_direct_table_access(self):
        import psycopg
        c = self.broker()
        try:
            with self.assertRaises(psycopg.Error) as cm:
                c.execute("select count(*) from leases").fetchone()
        finally:
            c.close()
        self.assertEqual(cm.exception.sqlstate, "42501")

    def test_p3_exact_tuple_only(self):
        e = self.ev.execute("select epoch from wp_lease_acquire('b1u'::tenant_t, 'projector', 300.0, false, 'h4', 2000)").fetchone()[0]
        self.assertEqual(e, 1)                     # leases are written only through the owner functions
        c = self.broker()
        try:
            q = "select wp_lease_holder_current(%s, %s, %s, %s)"
            got = {k: c.execute(q, k).fetchone()[0] for k in (("b1u", "projector", 1, "h4"), ("b1u", "projector", 2, "h4"),
                                                                ("b1u", "projector", 1, "hX"), ("b1v", "projector", 1, "h4"))}
        finally:
            c.close()
        self.assertEqual(list(got.values()), [True, False, False, False])


if __name__ == "__main__":
    unittest.main(verbosity=2)

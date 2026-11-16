"""R3-7 and R3-8 (round-3 review, p10_pg_forge executed by the reviewer): the Postgres trust boundary, as raw SQL by the roles.

Red first: run at 7080a22 (artifact agent-ad21c9e102633fab2/artifacts/red_pg_boundary_7080a22.out). The application role (dc_app) must
hold NO table, column, sequence or schema-CREATE privilege and no role membership, and EXECUTE exactly on the session-keyed API below;
the identity adapter's role (dc_idp) EXECUTE exactly on dc.open_session. Every forgery from p10 must be refused. The hash chain must be
checked against an anchor OUTSIDE the database, so an owner who rewrites rows AND recomputes every hash is detected (R3-8).

Same environment as tests/test_pg.py (paths to 0600 files only)."""
import os
import tempfile
import unittest

try:
    import psycopg
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.test_pg import CFG, ENV, LAB, T, ev

# The ONLY functions each login role may execute (regprocedure text). A change here is a reviewed API change.
# Change from 2b73a68 (justified): erasure is two calls, erase_candidates and erase_values, because the legal-hold decision is made by
# the in-process policy point between them (holds come from the policy document); the deletion and the token-mapping removal happen
# in the database under the tenant row lock.
APP_API = {"dc.submit(text,jsonb)", "dc.explain(text,text)", "dc.read_table(text)", "dc.erase_candidates(text,text)",
           "dc.erase_values(text,text,text[])"}
IDP_API = {"dc.open_session(text,text,text,text,integer)"}
PRIVS_TABLE = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
DENIED = (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedFunction, psycopg.errors.UndefinedTable,
          psycopg.errors.RaiseException) if psycopg else ()


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class PgBoundary(unittest.TestCase):
    port = int(os.environ.get("POL_PG_PORT") or 0)

    def setUp(self):
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.c = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"])
        self.s = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "pol-test-super")

    def tearDown(self):
        self.c.close()
        self.s.close()

    def _refused(self, stmt, params=None):
        with self.assertRaises(DENIED, msg=stmt):
            with self.c.transaction():
                self.c.execute(stmt, params)

    # ---------------------------------------------------------------- p10 forgeries, as dc_app
    def test_app_cannot_insert_a_forged_authority_event(self):
        """p10 A: dc_app inserted principal=admin, code=granted and dc.apply_authority applied it (mallory got decide on r1)."""
        self._refused("insert into dc.events(tenant,principal,idem,req,evid,fp,code,ev) values "
                      "('tenant-a','admin','x',null,'admin/x','f','granted','{\"target\":\"mallory\",\"req\":\"r1\",\"role\":\"decide\"}')")
        self._refused("select dc.apply_authority(1)")
        self.assertEqual(self.s.execute("select count(*) from dc.role where pid='mallory'").fetchone()[0], 0)

    def test_app_cannot_write_heads_props_or_values(self):
        """p10 B: dc_app ran `update dc.head set doc=...` and the head of record diverged from the log."""
        for stmt in ("update dc.head set doc = '{\"version\": 99, \"value_ref\": \"forged\"}'", "update dc.head set open_n = 0",
                     "insert into dc.props values ('tenant-a','r1','x/1','proposal','x','open','{}')", "update dc.props set status='open'",
                     "insert into dc.chain(tenant, req, doc) values ('tenant-a','r1','{}')", "delete from dc.vals",
                     "insert into dc.subject_tokens values ('tenant-a','dana','subj-x')", "update dc.tenant set lk = 1",
                     "select * from dc.subject_tokens", "select * from dc.events", "select doc from dc.head", "select * from dc.vals"):
            self._refused(stmt)

    def test_privileges_are_exactly_the_api(self):
        """Enumerates every privilege of dc_app and dc_idp from the catalogs (PUBLIC grants included, via has_*_privilege)."""
        s = self.s
        for role, api in (("dc_app", APP_API), ("dc_idp", IDP_API)):
            r = s.execute("select rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication, rolinherit from pg_roles where rolname=%s",
                          (role,)).fetchone()
            self.assertIsNotNone(r, role)
            self.assertEqual(tuple(r[:5]), (False,) * 5, role)
            self.assertEqual(s.execute("select count(*) from pg_auth_members m join pg_roles r on r.oid = m.member where r.rolname=%s",
                                       (role,)).fetchone()[0], 0, role + " is a member of another role")
            rels = s.execute("""select n.nspname || '.' || c.relname, c.oid, c.relkind from pg_class c join pg_namespace n on n.oid = c.relnamespace
                                where n.nspname not in ('pg_catalog', 'information_schema') and n.nspname not like 'pg_toast%%'
                                  and c.relkind in ('r','p','v','m','f','S')""").fetchall()
            bad = []
            for name, oid, kind in rels:
                if kind == "S":
                    for p in ("USAGE", "SELECT", "UPDATE"):
                        if s.execute("select has_sequence_privilege(%s, %s, %s)", (role, oid, p)).fetchone()[0]:
                            bad.append((name, p))
                    continue
                for p in PRIVS_TABLE:
                    if s.execute("select has_table_privilege(%s, %s, %s)", (role, oid, p)).fetchone()[0]:
                        bad.append((name, p))
                if s.execute("select has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE,REFERENCES')", (role, oid)).fetchone()[0]:
                    bad.append((name, "column"))
            for (nsp,) in s.execute("select nspname from pg_namespace where nspname not like 'pg_%%' and nspname <> 'information_schema'").fetchall():
                if s.execute("select has_schema_privilege(%s, %s, 'CREATE')", (role, nsp)).fetchone()[0]:
                    bad.append((nsp, "CREATE"))
            if s.execute("select has_database_privilege(%s, current_database(), 'CREATE')", (role,)).fetchone()[0]:
                bad.append(("database", "CREATE"))
            self.assertEqual(bad, [], role + " holds write or read privileges outside its API")
            fns = {f for (f,) in s.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                                               where n.nspname not in ('pg_catalog', 'information_schema')
                                                 and has_function_privilege(%s, p.oid, 'EXECUTE')""", (role,)).fetchall()}
            self.assertEqual(fns, api, role + " may execute functions outside its API")
            for f in api:
                d = s.execute("select prosecdef, proconfig from pg_proc where oid = %s::regprocedure", (f,)).fetchone()
                self.assertTrue(d[0], f + " must be SECURITY DEFINER")
                self.assertIn("search_path=dc, pg_temp", d[1] or [], f + " must pin search_path")

    def test_forged_session_row_and_token_are_refused(self):
        """A forged token (never issued) is refused by the API; dc_idp cannot write the sessions table either; a real token acts as the
        principal of its session row, whatever the body says."""
        idp = pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"])
        try:
            with self.assertRaises(DENIED):
                with idp.transaction():
                    idp.execute("insert into dc.sessions values ('x','tenant-a','admin','owner_ui','f', now() + interval '1 hour', false)")
            with self.assertRaises(psycopg.errors.RaiseException):
                idp.execute("select dc.open_session('tenant-a','alice','agent_api','f',60)")          # channel must match the kind
            tok = idp.execute("select dc.open_session('tenant-a','alice','owner_ui','f',60)").fetchone()[0]
        finally:
            idp.close()
        body = '{"type":"retract","client_id":"r","idem":"r","req":"r1","based_on":0}'
        out = self.c.execute("select dc.submit(%s, %s::jsonb)", (tok[:-1] + ("0" if tok[-1] != "0" else "1"), body)).fetchone()[0]
        self.assertTrue(out["code"].startswith("unauthenticated"), out)
        out = self.c.execute("select dc.submit(%s, %s::jsonb)", (tok, body)).fetchone()[0]
        self.assertEqual((out["code"], out["id"]), ("nothing_to_retract", "alice/r"))
        out = self.c.execute("select dc.submit(%s, %s::jsonb)", (tok, '{"type":"retract","client_id":"q","idem":"q","req":"r1","based_on":0,'
                                                                       '"principal":"admin"}')).fetchone()[0]
        self.assertEqual(out["code"], "identity_in_body")

    def test_app_cannot_open_a_session(self):
        """R3-1 in the database: only the identity adapter's role opens sessions; dc_app cannot mint one or write the sessions table."""
        self._refused("select dc.open_session('tenant-a','admin','owner_ui','f',60)")
        self._refused("insert into dc.sessions select")

    def test_submit_without_a_valid_session_is_refused(self):
        """No decision path without a session: a guessed or missing token is refused and writes nothing."""
        for tok in ("", "forged", "0" * 64):
            with self.c.transaction():
                out = self.c.execute("select dc.submit(%s, %s::jsonb)", (tok, '{"type":"propose","client_id":"p1","idem":"p1","req":"r1"}')).fetchone()[0]
            self.assertTrue(out["code"].startswith("unauthenticated"), out)
        self.assertEqual(self.s.execute("select count(*) from dc.events").fetchone()[0], 0)

    # ---------------------------------------------------------------- R3-8 external anchor
    def test_owner_who_recomputes_the_chain_is_detected_by_the_external_anchor(self):
        """p10 C left this as a hypothesis: an owner who rewrites a row AND recomputes every hash passes dc.verify_chain. The anchor file
        (outside the database) must detect it."""
        e = pgcore.PgEngine(self.s, T)
        for i in range(6):
            e._submit(ev("propose", "x", i, req="r1", value_ref="v%d" % i, label=LAB, based_on=0))
        anchor = pgcore.FileAnchor(os.path.join(tempfile.mkdtemp(), "anchor.jsonl"))
        pgcore.anchor_export(self.s, anchor, T)
        with self.s.transaction():                          # the owner rewrites history and recomputes the chain consistently
            self.s.execute("set local session_replication_role = replica")
            self.s.execute("update dc.events set ev = jsonb_set(ev, '{value_ref}', '\"forged\"') where chain_n = 3")
            self.s.execute("""
                do $$ declare h text := repeat('0', 64); r record; begin
                  for r in select seq, tenant, chain_n, code, ev from dc.events where tenant = 'tenant-a' order by chain_n loop
                    update dc.events set prev_hash = h, hash = encode(sha256(convert_to(h || '|' || r.tenant || '|' || r.chain_n::text
                      || '|' || r.code || '|' || r.ev::text, 'UTF8')), 'hex') where seq = r.seq returning hash into h;
                  end loop;
                  update dc.chainhead set hash = h where tenant = 'tenant-a'; end $$""")
        self.assertEqual(tuple(e._verify_chain()), (6, True))                 # the in-database check is fooled (R3-8 confirmed)
        self.assertFalse(pgcore.anchor_check(self.s, anchor, T)["ok"])         # the external anchor is not


if __name__ == "__main__":
    unittest.main()

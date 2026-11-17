"""Review-3 findings on the Postgres path (reviews/policy/review-3.md, run a883d987, at 0a20b64), reproduced first.

Each test was run against 0a20b64 before its fix (artifact in the policy owner's work dir, red-artifacts/) and failed there for the
reason in its docstring. Same environment as tests/test_pg.py (paths to 0600 files only; skipped without it).

  N1  a PgSession whose in-process Session and database token do not belong together is refused BEFORE any database call, on every
      entry point (submit, explain, table, erase_subject): mixed principal, mixed channel, mixed tenant. A counting proxy around the
      dc_app connection proves "before any call"; owner-side row counts prove "no effect".
  N3  PgDecisionService asks THE revocation source (pol.enforce.Revocations) like the memory service, so a principal revoked by the
      policy document cannot submit through the Postgres path; the two paths agree (differential), look-alike over-match included.
  N5  (dc.submit) a value reference is not computable from the value, its label or a subject: after erasure no key held by the database
      reproduces it.
  N4  ops/migrate_r3_7.rollback refuses when the migrated schema was written after the migration (submit, erase, or a refused request's
      audit row), and still rolls back when nothing was written (logins only).
  R3-8 anchor_check with zero anchors for the tenant is not ok, and the result shows the unanchored suffix."""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import types
import unittest

try:
    import psycopg
    from pol import pgcore
except ImportError:              # pragma: no cover
    psycopg = None

from tests.test_pg import CFG, ENV, LAB, T, ev

TB = "tenant-b"
CFG_B = dict(CFG, tenant=TB)
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(os.path.dirname(HERE), "exp", "fixtures", "pgcore_5a80e52.py")


class Counting:
    """The dc_app connection, counting every execute (the service under test uses only .execute)."""

    def __init__(self, conn):
        self._c, self.calls = conn, []

    def execute(self, q, *a, **kw):
        self.calls.append(q.split("(")[0] if isinstance(q, str) else str(q))
        return self._c.execute(q, *a, **kw)

    def __getattr__(self, n):
        return getattr(self._c, n)


def _principals(tenant):
    from tests.kit import P
    return [P("alice", tenant=tenant), P("bob", tenant=tenant), P("bot", kind="agent", model="local", tenant=tenant),
            P("x", kind="analyst", tenant=tenant), P("admin", tenant=tenant)]


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class Review3Pg(unittest.TestCase):
    port = int(os.environ.get("POL_PG_PORT") or 0)

    def setUp(self):
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG, TB: CFG_B},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.raw = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"])
        self.o = pgcore._connect("postgres", os.environ["POL_PG_SUPER_PASSFILE"], self.port, "pol-test-super")
        self.idp = pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"])
        self.addCleanup(self.raw.close)
        self.addCleanup(self.o.close)
        self.addCleanup(self.idp.close)

    # ---------------------------------------------------------------- worlds
    def _world(self, tenant=T, **pol):
        from pol.policy import parse_policy
        from tests.kit import World, policy_doc
        doc = policy_doc(**pol)
        doc["grants"][TB] = doc["grants"][T]
        for d in doc["destinations"].values():
            d["tenants"] = [T, TB]
        w = World(principals=_principals(tenant), policy=parse_policy(doc), core_config=dict(CFG, tenant=tenant))
        conn = Counting(self.raw)
        svc = pgcore.PgDecisionService(w.enf, conn)
        bridge = pgcore.PgSessionBridge(w.sessions, self.idp)

        def s(pid, channel=None):
            return bridge.issue(w.idp.credential(pid, channel or ("owner_ui" if CFG["principals"][pid] == "human" else "agent_api")))
        return w, svc, s, conn

    def _counts(self):
        q = lambda s: self.o.execute(s).fetchone()[0]          # noqa: E731
        return {"events": q("select count(*) from dc.events"), "vals": q("select count(*) from dc.vals"), "audit": q("select count(*) from dc.audit"),
                "tokens": q("select count(*) from dc.subject_tokens")}

    def _seed_value(self, tenant=T, subject="dana"):
        from psycopg.types.json import Jsonb
        self.o.execute("insert into dc.vals values (%s, %s, 'about ' || %s, %s, array[%s])", (tenant, "ref-" + subject, subject,
                                                                                               Jsonb(dict(LAB, tenants=[tenant], subjects=[subject])), subject))

    @staticmethod
    def _call(fn, *a):
        """The service's answer, or the exception it raised (0a20b64 raises after committing)."""
        try:
            return fn(*a)
        except Exception as e:          # noqa: BLE001
            return {"raised": type(e).__name__}

    # ---------------------------------------------------------------- N1
    BODY = {"type": "propose", "client_id": "n1", "idem": "n1", "req": "r1", "value": "v", "label": LAB, "based_on": 0}

    def _assert_refused_before_any_call(self, svc, conn, pair):
        before = self._counts()
        conn.calls.clear()
        out = self._call(svc.submit, pair, dict(self.BODY))
        ex = self._call(svc.explain, pair, "r1")
        tb = self._call(svc.table, pair)
        er = self._call(svc.erase_subject, pair, "dana")
        self.assertEqual(self._counts(), before, "the mismatched pair changed the database (submit committed, or values erased)")
        self.assertEqual(conn.calls, [], "the mismatched pair reached the database API")
        self.assertTrue(isinstance(out, dict) and str(out.get("code", "")).startswith("unauthenticated"), out)
        self.assertIsNone(ex)
        self.assertEqual(tb, [])
        self.assertEqual(er.get("refused"), "unauthorized", er)

    def test_n1_mixed_principal_pair_is_refused_before_any_call(self):
        """0a20b64: PgSession(alice.session, bob.db_token) passes the in-process check; submit commits Bob's event (autocommit) and only
        then raises; explain and table call the database as Bob."""
        w, svc, s, conn = self._world()
        alice, bob = s("alice"), s("bob")
        self._assert_refused_before_any_call(svc, conn, pgcore.PgSession(alice.session, bob.db_token))
        self.assertEqual(self.o.execute("select count(*) from dc.events where principal = 'bob'").fetchone()[0], 0)

    def test_n1_mixed_channel_pair_is_refused_before_any_call(self):
        """0a20b64: alice's owner_ui Session with alice's owner_cli token: same principal, so the late id-prefix check passes and the event is
        committed on the OTHER channel (same_channel_confirmation and channel rules then judge the wrong channel)."""
        w, svc, s, conn = self._world()
        ui, cli = s("alice", "owner_ui"), s("alice", "owner_cli")
        self._assert_refused_before_any_call(svc, conn, pgcore.PgSession(ui.session, cli.db_token))

    def test_n1_mixed_tenant_pair_is_refused_before_any_call(self):
        """0a20b64: tenant-a's alice Session with tenant-b's alice token: the id prefix matches, so tenant-b receives a write that the
        tenant-a service authorised, with no error at all."""
        wa, svc, s, conn = self._world(T)
        wb, _svc_b, sb, _conn_b = self._world(TB)
        self._assert_refused_before_any_call(svc, conn, pgcore.PgSession(s("alice").session, sb("alice").db_token))
        self.assertEqual(self.o.execute("select count(*) from dc.events where tenant = %s", (TB,)).fetchone()[0], 0)

    def test_n1_erase_split_across_two_principals_is_refused(self):
        """0a20b64: PgSession(alice.session, admin.db_token): the database lists and deletes as ADMIN while the in-process policy point
        decides each value as ALICE, so a non-admin erases. Must be refused before any call, values untouched."""
        w, svc, s, conn = self._world()
        self._seed_value()
        pair = pgcore.PgSession(s("alice").session, s("admin").db_token)
        conn.calls.clear()
        out = self._call(svc.erase_subject, pair, "dana")
        self.assertEqual(self.o.execute("select count(*) from dc.vals where tenant=%s", (T,)).fetchone()[0], 1, "a non-admin pair erased")
        self.assertEqual(conn.calls, [])
        self.assertEqual(out.get("refused"), "unauthorized", out)

    def test_n1_matching_pair_still_works_and_ended_session_is_refused(self):
        """Control: the bridge's own pair (and an equal PgSession carrying the same two parts) works; after SessionAuthority.end it does not."""
        w, svc, s, conn = self._world()
        self._seed_value()
        a = s("alice")
        self.assertEqual(svc.submit(a, dict(self.BODY))["code"], "proposed")
        self.assertEqual(svc.submit(pgcore.PgSession(a.session, a.db_token), dict(self.BODY, client_id="n2", idem="n2"))["code"], "proposed")
        adm = s("admin")
        self.assertEqual(svc.erase_subject(adm, "dana")["erased"], 1)
        w.sessions.end(a.session)
        conn.calls.clear()
        self.assertTrue(svc.submit(a, dict(self.BODY, client_id="n3", idem="n3"))["code"].startswith("unauthenticated"))
        self.assertEqual(conn.calls, [])

    # ---------------------------------------------------------------- N3
    def _memory_world(self, **pol):
        from pol.policy import parse_policy
        from tests.kit import World, policy_doc
        return World(principals=_principals(T), policy=parse_policy(policy_doc(**pol)), core_config=dict(CFG))

    def test_n3_policy_revoked_principal_cannot_submit_through_postgres(self):
        """0a20b64: PgDecisionService.submit never asks the revocation source: bob, revoked by the policy document (not by a core event),
        proposes successfully through the Postgres path."""
        w, svc, s, conn = self._world(revoked=["bob"])
        out = svc.submit(s("bob"), dict(self.BODY))
        self.assertEqual(out["code"], "principal_revoked", out)
        self.assertEqual(self.o.execute("select count(*) from dc.events where principal = 'bob'").fetchone()[0], 0)

    def test_n3_memory_and_postgres_agree_on_policy_revocation(self):
        """Differential: the same requests through the memory service and the Postgres service, with the policy revoking 'b0b' (a
        look-alike of bob: the shared revocation source over-matches by skeleton; the database's own revoked flag matches exactly).
        Codes and read results must be equal."""
        def run(submit, explain, table, sess):
            out = []
            for pid, n in (("alice", 1), ("bob", 2), ("x", 3), ("bot", 4)):
                r = submit(sess(pid), {"type": "propose", "client_id": "d%d" % n, "idem": "d%d" % n, "req": "r1", "value": "v%d" % n,
                                       "label": LAB, "based_on": 0})
                out.append(("submit", pid, r["code"]))
            for pid in ("alice", "bob"):
                e = explain(sess(pid), "r1")
                out.append(("explain", pid, None if e is None else (e["version"], sorted(i["author"] for i in e["open"]))))
                out.append(("table", pid, len(table(sess(pid)))))
            return out
        m = self._memory_world(revoked=["b0b"])
        mem = run(m.svc.submit, m.svc.explain, m.svc.table, m.s)
        w, svc, s, conn = self._world(revoked=["b0b"])
        pg = run(svc.submit, svc.explain, svc.table, s)
        self.assertIn(("submit", "bob", "principal_revoked"), mem)
        self.assertEqual(pg, mem)

    # ---------------------------------------------------------------- N5 (dc.submit)
    def test_n5_database_value_ref_is_not_recomputable_after_erasure(self):
        """0a20b64: vref = HMAC(dc.vkey, [tenant, principal, idem, text, tokenised label]); after erasure the token and the label are still
        in the log, so the key holder confirms a guessed TEXT (which may carry the subject's name). After the fix no database key
        reproduces the reference and no table but the log mentions it."""
        w, svc, s, conn = self._world()
        from tests.kit import L
        text = "dana missed the hearing"
        out = svc.submit(s("alice"), {"type": "propose", "client_id": "v1", "idem": "kv1", "req": "r1", "value": text,
                                      "label": L(subjects=["dana"]).to_dict(), "based_on": 0})
        ref = out["value_ref"]
        self.assertEqual(svc.erase_subject(s("admin"), "dana")["erased"], 1)
        tlab = self.o.execute("select ev->'label' from dc.events where tenant=%s and ev->>'value_ref' = %s", (T, ref)).fetchone()[0]
        keys = []
        if self.o.execute("select to_regclass('dc.vkey')").fetchone()[0] is not None:
            keys = [bytes(k) for (k,) in self.o.execute("select k from dc.vkey").fetchall()]
        hits = 0
        for k in keys:
            for guess in (text, "someone else's text"):
                msg = self.o.execute("select jsonb_build_array(%s::text, 'alice'::text, 'kv1'::text, %s::text, %s::jsonb)::text",
                                     (T, guess, json.dumps(tlab))).fetchone()[0]
                if "val-" + hmac.new(k, msg.encode(), hashlib.sha256).hexdigest()[:40] == ref:
                    hits += 1
        self.assertEqual(hits, 0, "a database key reproduces the erased value's reference from a guessed text")
        for tbl in ("vals", "subject_tokens"):
            self.assertEqual(self.o.execute("select count(*) from dc.%s x where row_to_json(x)::text like %%s" % tbl, ("%" + ref + "%",)).fetchone()[0], 0)

    def test_n5_database_retry_keeps_the_reference(self):
        """M2 in the database: the same body retried (new session) is a duplicate with the original reference; a changed body is idem_conflict."""
        w, svc, s, conn = self._world()
        body = {"type": "propose", "client_id": "v1", "idem": "kv1", "req": "r1", "value": "t", "label": LAB, "based_on": 0}
        r1 = svc.submit(s("alice"), dict(body))
        r2 = svc.submit(s("alice"), dict(body))
        self.assertEqual((r2["code"], r2["value_ref"]), ("duplicate", r1["value_ref"]))
        self.assertEqual(svc.submit(s("alice"), dict(body, value="t2"))["code"], "idem_conflict")
        self.assertEqual(self.o.execute("select count(*) from dc.vals").fetchone()[0], 1)

    # ---------------------------------------------------------------- R3-8 anchor
    def test_anchor_check_without_anchors_is_not_ok(self):
        """0a20b64: anchor_check returns ok=True with zero anchors for the tenant (nothing outside the database was ever compared)."""
        e = pgcore.PgEngine(self.o, T)
        for i in range(3):
            e._submit(ev("propose", "x", i, req="r1", value_ref="v%d" % i, label=LAB, based_on=0))
        path = os.path.join(tempfile.mkdtemp(), "anchor.jsonl")
        r = pgcore.anchor_check(self.o, pgcore.FileAnchor(path), T)
        self.assertFalse(r["ok"], r)
        pgcore.anchor_export(self.o, pgcore.FileAnchor(path), T)
        e._submit(ev("propose", "x", 9, req="r1", value_ref="v9", label=LAB, based_on=0))
        r = pgcore.anchor_check(self.o, pgcore.FileAnchor(path), T)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r.get("anchored_through"), r.get("unanchored_suffix")), (3, 1), r)


# ------------------------------------------------------------------------------------------------ N4: migration rollback
def _old_module():
    mod = types.ModuleType("pol.pgcore_5a80e52_t")
    mod.__file__, mod.__package__ = FIXTURE, "pol"
    sys.modules[mod.__name__] = mod
    with open(FIXTURE, encoding="utf-8") as f:
        exec(compile(f.read(), FIXTURE, "exec"), mod.__dict__)        # the verbatim pre-R3-7 module (test fixture, as exp/mig_rehearsal)
    return mod


@unittest.skipUnless(psycopg is not None and all(os.environ.get(k) for k in ENV), "Postgres lab environment not configured")
class Review3Rollback(unittest.TestCase):
    port = int(os.environ.get("POL_PG_PORT") or 0)

    def setUp(self):
        from ops import migrate_r3_7 as MIG
        self.MIG = MIG
        sp = os.environ["POL_PG_SUPER_PASSFILE"]
        self.o = pgcore._connect("postgres", sp, self.port, "pol-test-mig")
        self.addCleanup(self.o.close)
        self.o.execute("drop schema if exists %s cascade" % MIG.PRE)
        self.o.execute("drop schema if exists dc_crypto cascade")
        OLD = _old_module()
        OLD.install(self.port, sp, os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG})
        self.o.execute("insert into dc.vals values (%s,'val-x1','about dana','{}'::jsonb, array['dana'])", (T,))
        self.o.execute("insert into dc.subject_tokens values (%s,'dana','subj-t1')", (T,))
        MIG.forward(self.o, {T: CFG}, os.environ["POL_PG_IDP_PASSWORD_FILE"])

    def _token(self, pid="alice"):
        idp = pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"])
        try:
            return idp.execute("select dc.open_session(%s,%s,'owner_ui','f',60)", (T, pid)).fetchone()[0]
        finally:
            idp.close()

    def _app(self, q, *a):
        c = pgcore.connect_app(self.port, os.environ["POL_PG_APP_PASSFILE"])
        try:
            return c.execute(q, a).fetchone()[0]
        finally:
            c.close()

    def _is_old_schema(self):
        return self.o.execute("select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'dc' "
                              "and p.proname = 'apply_authority'").fetchone() is not None

    def _assert_refused_and_kept(self, what):
        with self.assertRaises(self.MIG.MigrationRefused, msg="rollback discarded a post-migration %s" % what):
            self.MIG.rollback(self.o)
        self.assertFalse(self._is_old_schema(), "the migrated schema was replaced although it held post-migration writes")

    def test_n4_rollback_without_writes_proceeds(self):
        """Logins are not data: a session opened after the migration does not block the rollback; the old schema comes back."""
        self._token()
        self.MIG.rollback(self.o)
        self.assertTrue(self._is_old_schema())

    def test_n4_rollback_after_a_submit_is_refused(self):
        """0a20b64: rollback drops the new schema unconditionally; an applied request made after the migration silently disappears."""
        out = self._app("select dc.submit(%s, %s::jsonb)", self._token(), json.dumps({"type": "propose", "client_id": "m1", "idem": "m1",
                                                                                       "req": "r1", "value": "post", "label": LAB, "based_on": 0}))
        self.assertEqual(out["code"], "proposed")
        self._assert_refused_and_kept("submit")
        self.assertEqual(self.o.execute("select count(*) from dc.events where ev->>'id' = 'alice/m1'").fetchone()[0], 1)

    def test_n4_rollback_after_an_erasure_is_refused(self):
        """0a20b64: a value erased after the migration comes back with the old schema (erasure undone)."""
        tok = self._token("admin")
        self.assertEqual(self._app("select dc.erase_values(%s,'dana',array['val-x1'])", tok)["erased"], 1)
        self._assert_refused_and_kept("erasure")
        self.assertEqual(self.o.execute("select count(*) from dc.vals where ref = 'val-x1'").fetchone()[0], 0)

    def test_n4_rollback_after_a_refused_request_is_refused(self):
        """A refused request writes an audit row: audit history is data too (0a20b64 drops it)."""
        out = self._app("select dc.submit(%s, %s::jsonb)", self._token(), json.dumps({"type": "propose", "client_id": "m2", "idem": "m2",
                                                                                       "req": "nope", "value": "x", "label": LAB, "based_on": 0}))
        self.assertNotEqual(out["code"], "proposed")
        self._assert_refused_and_kept("audit row")


if __name__ == "__main__":
    unittest.main()

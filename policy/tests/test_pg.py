"""Postgres proofs (run on the lab host against the experiment's own container). Skipped unless POL_PG_PORT, POL_PG_SUPER_PASSFILE,
POL_PG_APP_PASSWORD_FILE, POL_PG_APP_PASSFILE, POL_PG_IDP_PASSWORD_FILE and POL_PG_IDP_PASSFILE are set (paths to 0600 files; no
secret is ever in the environment). Since R3-7 the raw core (PgEngine) runs on an OWNER connection: dc_app can only call the
session-keyed API."""
import os
import threading
import time
import unittest

try:
    import psycopg
    from pol import _core, pgcore
except ImportError:              # pragma: no cover
    psycopg = None

ENV = ("POL_PG_PORT", "POL_PG_SUPER_PASSFILE", "POL_PG_APP_PASSWORD_FILE", "POL_PG_APP_PASSFILE", "POL_PG_IDP_PASSWORD_FILE", "POL_PG_IDP_PASSFILE")
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
        pgcore.install(self.port, os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"], {T: CFG},
                       idp_password_file=os.environ["POL_PG_IDP_PASSWORD_FILE"])
        self.c = self.app()
        self.o = self.sup()
        self.e = pgcore.PgEngine(self.o, T)

    def tearDown(self):
        self.c.close()
        self.o.close()

    def test_isolation_level_explicit(self):
        self.assertEqual(self.c.isolation_level, psycopg.IsolationLevel.READ_COMMITTED)
        with self.c.transaction():
            self.assertEqual(self.c.execute("show transaction_isolation").fetchone()[0], "read committed")

    def test_passwordless_connection_refused(self):
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(host="127.0.0.1", port=self.port, dbname=os.environ.get("POL_PG_DB") or "pol", user="dc_app", passfile="/dev/null", password="", connect_timeout=5)
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(host="127.0.0.1", port=self.port, dbname=os.environ.get("POL_PG_DB") or "pol", user="postgres", passfile="/dev/null", connect_timeout=5)

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
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.c.transaction():                                      # the raw core is owner-only
                self.c.execute("select dc._apply_event('tenant-a', '{}'::jsonb)")

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
        out, conns = [], [self.sup(), self.sup()]
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

    def test_grant_may_reuse_an_applied_event_id_like_the_oracle(self):
        """CORE-RULES rule 6 precedes rule 8: a grant is not refused duplicate_id. Found by exp/pg_diff (dev, dirty tree): the backstop index
        raised UniqueViolation where pol/_core applies `granted`. A later non-grant event with that id is still duplicate_id."""
        from pol import _core
        st = _core.MemoryState(CFG)
        evs = [dict(ev("supersede", "alice", "s", req="r2", value_ref="v1", label=LAB, based_on=0), id="e1"),
               dict(ev("grant", "admin", "g", req="r2", target="bob", role="decide"), id="e1"),
               dict(ev("grant", "admin", "g2", req="r2", target="bob", role="propose"), id="e1"),
               dict(ev("supersede", "bob", "s2", req="r2", value_ref="v2", label=LAB, based_on=1), id="e1")]
        codes = []
        for e in evs:
            want, orig, d = _core.reduce(st, e)
            st.apply_delta(d)
            codes.append(want)
            self.assertEqual(self.e._submit(e), (want, orig), e)
        self.assertEqual(codes, ["superseded", "granted", "granted", "duplicate_id"])
        self.assertEqual(self.e._comparable(), st.comparable())
        self.assertEqual(tuple(self.e._verify_chain()), (3, True))

    def test_revoke_through_definer_function_invalidates_open_items(self):
        for i in range(3):
            self.e._submit(ev("propose", "x", i, req="r1", value_ref="v", label=LAB, based_on=0))
        self.assertEqual(self.e._submit(ev("revoke", "admin", "rv", target="x"))[0], "revoked")
        comp = self.e._comparable()
        self.assertEqual(comp["revoked"], ["x"])
        self.assertEqual(set(comp["proposals"]["r1"].values()), {"invalidated"})
        self.assertEqual(self.o.execute("select open_n from dc.head where req='r1'").fetchone()[0], 0)
        self.assertEqual(self.e._submit(ev("propose", "x", 9, req="r1", value_ref="v", label=LAB, based_on=0))[0], "principal_revoked")
        self.assertEqual(self.o.execute("select count(*) from dc.audit where code='principal_revoked'").fetchone()[0], 1)

    def test_proposal_cost_linear_to_20000(self):
        """Pre-registered (THRESHOLDS amendment 3): 20,000 proposals created and closed; the mean time per event of each of the last three
        2,000-proposal blocks is below 3x the median of the first three blocks. Workload change (round 3, ASSUMPTION that agents only
        propose and remark): amendment 3 named an agent reject; a reject now needs a human with authority, and each one versions the head."""
        blocks, t0 = [], time.perf_counter()
        for i in range(20000):
            a = self.e._submit(ev("propose", "x", "p%d" % i, req="r1", value_ref="v%d" % i, label=LAB, based_on=0))
            b = self.e._submit(ev("decide", "alice", "d%d" % i, req="r1", verb="reject", proposal="x/p%d" % i,
                                  proposal_hash=_core.proposal_hash("x/p%d" % i, "v%d" % i, "x"), based_on=i))
            self.assertEqual((a[0], b[0]), ("proposed", "decided_reject"))
            if (i + 1) % 2000 == 0:
                t1 = time.perf_counter()
                blocks.append(t1 - t0)
                t0 = t1
        first = sorted(blocks[:3])[1]
        print("\nPG N2 blocks (s per 2000 proposals): " + " ".join("%.2f" % b for b in blocks))
        self.assertEqual(self.o.execute("select count(*) from dc.props").fetchone()[0], 20000)
        self.assertEqual(self.e._head("r1")["version"], 20000)
        self.assertLess(max(blocks[-3:]) / first, 3.0)


    # ---------------------------------------------------------------- the deployment path: dc_idp opens sessions, dc_app calls the API
    def _svc(self, **kw):
        from pol.policy import parse_policy
        from tests.kit import P, World, policy_doc
        w = World(principals=[P("alice"), P("bob"), P("bot", kind="agent", model="local"), P("x", kind="analyst"), P("admin"),
                              P("carol", clearance=0, groups=("other",))],
                  policy=parse_policy(policy_doc(**kw)), core_config=dict(CFG))
        self.idp = pgcore.connect_idp(self.port, os.environ["POL_PG_IDP_PASSFILE"])
        self.addCleanup(self.idp.close)
        bridge = pgcore.PgSessionBridge(w.sessions, self.idp)

        def s(pid):
            return bridge.issue(w.idp.credential(pid, "owner_ui" if CFG["principals"][pid] == "human" else "agent_api"))
        return w, pgcore.PgDecisionService(w.enf, self.c), s

    def test_service_over_postgres(self):
        """The service through the definer API: value refs, retry after a session refresh, forged body, scoped read, erasure with a hold."""
        from tests.kit import L
        w, svc, s = self._svc(hold_events=["hold-7"])
        a = s("alice")
        body = {"type": "supersede", "client_id": "s1", "idem": "k1", "req": "r1", "value": "dana is late",
                "label": L(subjects=["dana"], holds=["hold-7"]).to_dict(), "based_on": 0}
        r1 = svc.submit(a, body)
        self.assertEqual(r1["code"], "superseded")
        r2 = svc.submit(s("alice"), dict(body))                   # a NEW session: idempotency is per (tenant, principal, idem), not per session
        self.assertEqual((r2["code"], r2["original"], r2["value_ref"]), ("duplicate", "superseded", r1["value_ref"]))
        self.assertEqual(svc.submit(a, dict(body, tenant="tenant-b", client_id="s2", idem="k2"))["code"], "identity_in_body")
        self.assertEqual(self.o.execute("select count(*) from dc.vals").fetchone()[0], 1)        # refused/duplicate values not kept
        self.assertEqual(svc.explain(a, "r1")["value"]["text"], "dana is late")
        self.assertIsNone(svc.explain(s("x"), "r2"))               # x is not a participant of r2
        with self.assertRaises(psycopg.errors.RaiseException):
            pgcore.PgSessionBridge(w.sessions, self.idp).issue(w.idp.credential("carol", "owner_ui"))   # not a principal of the core
        self.assertEqual([x["req"] for x in svc.table(a)], ["r1", "r2"])
        svc.submit(s("bob"), {"type": "propose", "client_id": "p1", "idem": "k3", "req": "r1", "value": "bob on dana",
                              "label": L(subjects=["dana"]).to_dict(), "based_on": 1})
        self.assertEqual(svc.erase_subject(s("alice"), "dana")["refused"], "unauthorized")
        self.assertEqual(svc.erase_subject(s("admin"), "dana"), {"erased": 1, "queued": 1, "denied": 0, "refused": None, "result": "incomplete"})
        self.assertEqual(self.o.execute("select count(*) from dc.vals").fetchone()[0], 1)
        self.assertEqual(svc.erase_subject(s("admin"), "nobody")["result"], "no_values_found")

    def test_no_subject_names_in_log_heads_props_or_audit(self):
        """(f) After a mixed run, no subject name appears in events, heads, props, chain or audit (tokens only)."""
        from tests.kit import L
        w, svc, s = self._svc()
        names = ["dana ruiz", "zoltan", "josé"]
        for i, n in enumerate(names):
            svc.submit(s("bob"), {"type": "propose", "client_id": "p%d" % i, "idem": "p%d" % i, "req": "r1", "value": "about %d" % i,
                                  "label": L(subjects=[n]).to_dict(), "based_on": 0})
        svc.submit(s("alice"), {"type": "supersede", "client_id": "s1", "idem": "s1", "req": "r1", "value": "v", "label": L(subjects=names).to_dict(),
                                "based_on": 0})
        svc.submit(s("alice"), {"type": "supersede", "client_id": "s2", "idem": "s2", "req": "r1", "value": "w", "label": L(subjects=["zoltan"]).to_dict(),
                                "based_on": 1})
        svc.submit(s("bot"), {"type": "decide", "client_id": "d1", "idem": "d1", "req": "r1", "verb": "reject", "proposal": "bob/p0",
                              "proposal_hash": "x", "based_on": 2})
        raw = dict(L(subjects=["zoltan"]).to_dict(), subjects=["Zoltan"])          # a non-canonical spelling sent past the Python layer: refused
        self.assertEqual(svc.submit(s("bob"), {"type": "propose", "client_id": "bad", "idem": "bad", "req": "r1", "value": "x", "label": raw,
                                               "based_on": 0})["code"], "bad_event")
        dump = repr([self.o.execute("select %s" % q).fetchall() for q in (
            "ev::text from dc.events", "doc::text from dc.head", "doc::text from dc.props", "doc::text from dc.chain", "coalesce(ev::text,'') from dc.audit")])
        self.assertGreater(self.o.execute("select count(*) from dc.events").fetchone()[0], 3)
        for n in names + ["Zoltan", "jos"]:
            self.assertNotIn(n, dump)
        self.assertIn("subj-", dump)

    def test_tokens_unconfirmable_after_erasure_in_the_database(self):
        """(g) After erasure no row links the token to the name (the value-ref key never leaves the database and refs are not derived from
        the name); a re-appearing subject gets a fresh token."""
        from tests.kit import L
        w, svc, s = self._svc()
        svc.submit(s("bob"), {"type": "propose", "client_id": "p1", "idem": "p1", "req": "r1", "value": "about dana", "label": L(subjects=["dana"]).to_dict(),
                              "based_on": 0})
        tok = self.o.execute("select doc->'label'->'subjects'->>0 from dc.props").fetchone()[0]
        self.assertTrue(tok.startswith("subj-"))
        self.assertEqual(svc.erase_subject(s("admin"), "dana")["result"], "erased")
        everything = repr([self.o.execute("select %s" % q).fetchall() for q in (
            "* from dc.subject_tokens", "text, label::text, subjects from dc.vals", "* from dc.audit", "* from dc.sessions")])
        self.assertNotIn("dana", everything)
        self.assertNotIn(tok, everything)
        svc.submit(s("bob"), {"type": "propose", "client_id": "p2", "idem": "p2", "req": "r1", "value": "dana again", "label": L(subjects=["dana"]).to_dict(),
                              "based_on": 0})
        tok2 = self.o.execute("select doc->'label'->'subjects'->>0 from dc.props where id='bob/p2'").fetchone()[0]
        self.assertNotEqual(tok, tok2)

    def test_erase_and_propose_cannot_interleave(self):
        """The erase/propose race: a propose about the subject holds the tenant row FOR SHARE from token minting to commit, and erasure
        deletes under FOR UPDATE, so erasure waits, then sees the new value and keeps the mapping. Never: a value stored under a token
        whose mapping was deleted."""
        from psycopg.types.json import Jsonb
        from tests.kit import L
        w, svc, s = self._svc()
        adm, bob = s("admin"), s("bob")
        svc.submit(bob, {"type": "propose", "client_id": "p0", "idem": "p0", "req": "r1", "value": "v0", "label": L(subjects=["dana"]).to_dict(),
                         "based_on": 0})
        held = self.app()
        held.autocommit = False                                     # a propose whose transaction stays open after minting
        out = held.execute("select dc.submit(%s, %s)", (bob.db_token, Jsonb(
            {"type": "propose", "client_id": "p1", "idem": "p1", "req": "r1", "value": "v1", "label": L(subjects=["dana"]).to_dict(),
             "based_on": 0}))).fetchone()[0]
        self.assertEqual(out["code"], "proposed")
        res = {}
        other = self.app()
        self.addCleanup(other.close)

        def erase():
            res["out"] = pgcore.PgDecisionService(w.enf, other).erase_subject(adm, "dana")
        th = threading.Thread(target=erase)
        th.start()
        time.sleep(1.0)
        waiting = self.o.execute("select count(*) from pg_stat_activity where wait_event_type = 'Lock' and query like 'select dc.erase_values%%'").fetchone()[0]
        held.commit()
        th.join(10)
        held.close()
        self.assertEqual(waiting, 1, "erasure must wait for the open propose")
        self.assertEqual(res["out"]["erased"], 1)                  # it erased v0 (listed before the wait) ...
        self.assertEqual(res["out"]["result"], "incomplete")       # ... saw v1 committed under the lock, and kept the mapping
        tok = self.o.execute("select doc->'label'->'subjects'->>0 from dc.props where id='bob/p1'").fetchone()[0]
        self.assertEqual(self.o.execute("select count(*) from dc.subject_tokens where token=%s", (tok,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

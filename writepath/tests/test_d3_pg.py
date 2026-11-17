"""Live Postgres test for D3 (DESIGN-NOTES "Design: D3"; ASSUMPTION under decision 10): the reviewer role and span
redaction as the STORE enforces them. Builds the schema from scratch in a throwaway database (as wp.setup --reset
does), connects as the real login roles (wp_app, wp_rev1, wp_rev2: their own pgpass files), and drops the database
afterwards. HMACs and ciphertexts here are opaque test bytes: the store never sees plaintext and checks only who,
shape, binding and chain links. Fixture events are inserted as wp_owner with ONLY the insert guard (events_guard,
fence and scan checks, not under test) disabled for the insert and re-enabled ALWAYS afterwards.

  A1  the app role cannot write subject_identifiers, nor call wp_identifier_declare or wp_identifier_approve
  A2  a reviewer cannot approve its own proposal (one login holding both rights); another reviewer can
  A3  approve refuses an HMAC other than the proposal's, another rule version, and a closed proposal
  B1  redact refuses malformed spans, the subject's own event, a non-mentioning event, a stale base, a wrong prev_hash
  B2  redact under another subject's legal hold is refused
  B3  a good redaction nulls the original, stores the chained row, and the same job cannot redact twice
  B4  the cascade keeps THIS job's redaction text, nulls a redaction text made for another subject (fallback), and
      the residue counts no redaction text
  B5  the redaction chain verifies with the chain key; a wrong key, and NULL content with no tombstone or redaction, fail
Runs only with WP_PG_LIVE=1 on the lab host. SCHEMA_UNDER_TEST names another schema.sql (failing-first).
usage (on t2-dev, from WP_HOME/src): WP_PG_LIVE=1 ../venv/bin/python -m unittest tests.test_d3_pg -v"""
import hashlib
import os
import pathlib
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parents[1]
T = "d3t"


def sha(b):
    return hashlib.sha256(b).hexdigest()


@unittest.skipUnless(os.environ.get("WP_PG_LIVE") == "1", "live Postgres test: set WP_PG_LIVE=1 on the lab host")
class D3Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from wp import db, log
        cls.db, cls.LG = db, log
        cls.name = f"wp_d3check_{uuid.uuid4().hex[:8]}"
        a = db.admin(db="postgres")
        a.execute(f'create database "{cls.name}"')
        a.execute(f'revoke connect on database "{cls.name}" from public')
        a.execute(f'grant connect on database "{cls.name}" to wp_app, wp_legal, ' + ", ".join(db.REVIEWERS))
        a.close()
        ev = db.admin(db=cls.name)
        ev.execute("drop schema public cascade")
        ev.execute("create schema public authorization wp_owner")
        ev.execute("set role wp_owner")
        ev.execute(pathlib.Path(os.environ.get("SCHEMA_UNDER_TEST") or HERE / "wp" / "schema.sql").read_text())
        ev.execute("reset role")
        cls.ev = ev
        cls.app = db.connect(db=cls.name, application_name="wp-d3-app")
        cls.r1 = db.connect(db=cls.name, application_name="wp-d3-r1", role=db.REVIEWERS[0])
        cls.r2 = db.connect(db=cls.name, application_name="wp-d3-r2", role=db.REVIEWERS[1])

    @classmethod
    def tearDownClass(cls):
        for c in (cls.app, cls.r1, cls.r2, cls.ev):
            c.close()
        a = cls.db.admin(db="postgres")
        a.execute(f'drop database if exists "{cls.name}" with (force)')
        a.close()

    # ------------------------------------------------------------------ helpers
    def state(self, conn, sql, args=()):
        import psycopg
        try:
            conn.execute(sql, args)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate

    def propose(self, conn, s, h, rv="r2:m2:ucd15.0.0"):
        pid = str(uuid.uuid4())
        conn.execute("select wp_identifier_propose(%s::tenant_t, %s, %s, %s, %s, %s)", (T, s, pid, h, b"sealed", rv))
        return pid

    def event(self, eid, speaker, mentions, content=b"orig"):
        ev = self.ev
        ev.execute("set role wp_owner")
        try:
            ev.execute("insert into subjects(tenant_id, subject_id) select %s, x from unnest(%s::text[]) x on conflict do nothing",
                       (T, [speaker] + list(mentions)))
            ev.execute("alter table events disable trigger events_guard")
            n = ev.execute("select count(*) from events where tenant_id=%s", (T,)).fetchone()[0]
            ev.execute("insert into events(tenant_id, session_id, seq, event_id, idem_key, req_hash, kind, subject_id, actor, trust_origin, "
                       "ts_event, content_enc, content_digest, mentions, prev_hash, hash, lease_scope, lease_epoch) values "
                       "(%s, 's1', %s, %s, %s, 'rh', 'message', %s, 'user', 'user_typed', now(), %s, %s, %s, 'p', %s, 'session:s1', 1)",
                       (T, n + 1, eid, eid, speaker, content, sha(content), list(mentions), "h-" + eid))
        finally:
            ev.execute("alter table events enable always trigger events_guard")
            ev.execute("reset role")

    def job(self, s):
        jid = str(uuid.uuid4())
        self.app.execute("insert into erasure_jobs(job_id, tenant_id, subject_id) values (%s, %s, %s)", (jid, T, s))
        self.app.execute("select wp_erasure_mark(%s)", (jid,))
        return jid

    def redact(self, jid, eid, base, enc, spans, prev, h="hx"):
        return self.state(self.app, "select wp_erasure_redact(%s, %s, %s, %s, %s, %s, %s)", (jid, eid, base, enc, spans, prev, h))

    # ------------------------------------------------------------------ A: reviewer role
    def test_a1_app_has_no_path_to_subject_identifiers(self):
        self.assertEqual(self.state(self.app, "insert into subject_identifiers values (%s, 'h', 'X', 1)", (T,)), "42501")
        self.assertEqual(self.state(self.app, "select wp_identifier_declare(%s::tenant_t, 'X', array['h'])", (T,)), "42501")
        pid = self.propose(self.app, "PA", "hA")
        self.assertEqual(self.state(self.app, "select wp_identifier_approve(%s, 'hA', 'r2:m2:ucd15.0.0')", (pid,)), "42501")
        self.assertEqual(self.ev.execute("select proposed_by, state from identifier_proposals where proposal_id=%s", (pid,)).fetchone(),
                         ("wp_app", "proposed"))

    def test_a2_no_self_approval(self):
        pid = self.propose(self.r1, "PB", "hB")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hB', 'r2:m2:ucd15.0.0')", (pid,)), "WPS30")
        self.assertEqual(self.state(self.r2, "select wp_identifier_approve(%s, 'hB', 'r2:m2:ucd15.0.0')", (pid,)), "ok")
        self.assertEqual(self.ev.execute("select subject_id from subject_identifiers where tenant_id=%s and ident_hmac='hB'", (T,)).fetchone(), ("PB",))

    def test_a3_binding_rule_version_and_state(self):
        pid = self.propose(self.app, "PC", "hC")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hOther', 'r2:m2:ucd15.0.0')", (pid,)), "WPS31")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hC', 'r2:m2:ucd13.0.0')", (pid,)), "WPS32")
        self.assertEqual(self.state(self.r1, "select wp_identifier_reject(%s, 'test')", (pid,)), "ok")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hC', 'r2:m2:ucd15.0.0')", (pid,)), "WPS33")
        self.assertEqual(self.ev.execute("select count(*) from subject_identifiers where tenant_id=%s and ident_hmac='hC'", (T,)).fetchone(), (0,))

    # ------------------------------------------------------------------ B: span redaction
    def test_b_redaction(self):
        self.event("e1", "Y", ["X"])                       # another speaker mentions X: redactable
        self.event("e2", "X", [])                          # X's own event
        self.event("e3", "Y", [])                          # does not mention X
        self.event("e4", "Z", ["X"])                       # Z will be under a legal hold
        self.event("e5", "Y", ["X", "W"])                  # redacted earlier for W
        self.event("e6", "Y", [])                          # for the B5 negative
        jw = self.job("W")
        self.assertEqual(self.redact(jw, "e5", sha(b"orig"), b"red-w", [0, 3], "h-e5"), "ok")
        self.ev.execute("set role wp_owner")
        self.ev.execute("select wp_hold_place(%s::tenant_t, 'Z', 'hold1', 'test')", (T,))
        self.ev.execute("reset role")
        j = self.job("X")
        base = sha(b"orig")
        # B1
        for bad in ([], [5, 3], [0, 4, 3, 8], [0, 4, 1], [-1, 2]):
            self.assertEqual(self.redact(j, "e1", base, b"r", bad, "h-e1"), "WPS36", bad)
        self.assertEqual(self.redact(j, "e2", base, b"r", [0, 1], "h-e2"), "WPS34")
        self.assertEqual(self.redact(j, "e3", base, b"r", [0, 1], "h-e3"), "WPS34")
        self.assertEqual(self.redact(j, "e1", sha(b"other"), b"r", [0, 1], "h-e1"), "WPS37")
        self.assertEqual(self.redact(j, "e1", base, b"r", [0, 1], "wrong"), "WPS37")
        # B2
        self.assertEqual(self.redact(j, "e4", base, b"r", [0, 1], "h-e4"), "WPS35")
        # B3
        key = b"test-chain-key"
        h1 = self.LG.redaction_hash("h-e1", T, "e1", 1, j, base, sha(b"red-x"), [0, 3], key=key)
        self.assertEqual(self.redact(j, "e1", base, b"red-x", [0, 3], "h-e1", h1), "ok")
        self.assertEqual(self.ev.execute("select content_enc from events where tenant_id=%s and event_id='e1'", (T,)).fetchone(), (None,))
        self.assertEqual(self.ev.execute("select n, redacted_digest from event_redactions where job_id=%s", (j,)).fetchone(), (1, sha(b"red-x")))
        self.assertEqual(self.redact(j, "e1", sha(b"red-x"), b"red-x2", [0, 3], h1), "WPS37")
        self.assertEqual(self.state(self.app, "update event_redactions set spans = '{0,1}' where job_id=%s", (j,)), "42501")
        # B4
        for _ in range(20):
            if self.app.execute("select wp_erasure_cascade(%s, 100)", (j,)).fetchone()[0] == 0:
                break
        txt = dict(self.ev.execute("select event_id, redacted_enc is not null from event_redactions where tenant_id=%s", (T,)).fetchall())
        self.assertEqual(txt, {"e1": True, "e5": False}, "this job's text kept; the text made for W nulled (e5 falls back)")
        res = self.ev.execute("select wp_erasure_pg_residue(%s, 'X')", (T,)).fetchone()[0]
        self.assertEqual(res.get("pg:redaction_text"), 0)
        # B5
        cols = ("select seq, event_id, kind, subject_id, actor, trust_origin, ts_event, content_digest, prev_hash, hash, lease_epoch, "
                "decision_item, decision_action, based_on_event_id, source_event_id, content_enc, mentions, mention_scan_upto "
                "from events where tenant_id=%s and event_id = any(%s) order by seq")
        rows = self.ev.execute(cols, (T, ["e1"])).fetchall()
        self.assertTrue(self.LG._verify_redactions(self.ev, T, "s1", key, rows))
        self.assertFalse(self.LG._verify_redactions(self.ev, T, "s1", b"wrong-key", rows))
        self.ev.execute("set role wp_owner")
        self.ev.execute("update events set content_enc = null where tenant_id=%s and event_id='e6'", (T,))
        self.ev.execute("reset role")
        self.assertFalse(self.LG._verify_redactions(self.ev, T, "s1", key, self.ev.execute(cols, (T, ["e6"])).fetchall()))


if __name__ == "__main__":
    unittest.main()

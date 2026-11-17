"""Live Postgres test for D3 after review C (DESIGN-NOTES "Redesign after review C"; ASSUMPTION under decision 10 and
decision R1: no span redaction, whole-event removal only). Builds the schema from scratch in a throwaway database (as
wp.setup --reset does), connects as the real login roles (wp_app, wp_rev1, wp_rev2: their own pgpass files), and drops
the database afterwards. HMACs, matcher ids and ciphertexts here are opaque test bytes: the store never sees plaintext.
Fixture events are inserted as wp_owner with ONLY the insert guard (events_guard) disabled for the insert and
re-enabled ALWAYS afterwards; C4's appends go through the guard as the app role.

  A1  the app role cannot write subject_identifiers, nor call wp_identifier_declare or wp_identifier_approve
  A2  a reviewer cannot approve its own proposal (one login holding both rights); another reviewer can
  A3  approve refuses an HMAC other than the proposal's, another rule version, and a closed proposal
  C1  (F1)  the app has no redaction path: storing the unchanged plaintext as a "redaction" is refused; another
            speaker's event that mentions the subject is removed whole by the cascade
  C2  (F8)  a proposal paused after its erased check cannot commit after the erasure finished: the mark waits for it
            (lock before check), and the cascade then deletes it
  C3        a proposal approved after the mark is refused (WPS03)
  C4  (F3)  the tenant's matcher is stored and enforced: approval under another rule version (WPS32), changing the
            matcher while identifiers exist (WPS38), an append or a rescan under another matcher (WPS38)
  C5  (F7)  NULL content verifies only with a tombstone whose erasure job exists for that subject
Runs only with WP_PG_LIVE=1 on the lab host. SCHEMA_UNDER_TEST names another schema.sql (failing-first).
usage (on t2-dev, from WP_HOME/src): WP_PG_LIVE=1 ../venv/bin/python -m unittest tests.test_d3_pg -v"""
import hashlib
import os
import pathlib
import threading
import time
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parents[1]
T, TM = "d3t", "d3m"
MID = "m3:test"                      # an opaque matcher id: the store compares it, never interprets it
RV13 = "r2:m2:ucd13.0.0"             # an identifier reviewed under another matcher and Unicode version (review C F3)


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
        # the tenant's matcher (class 3); a schema without wp_matcher_set (4fe3ac1) runs A1-A3 with its own r2 version
        cls.RV = "r3:" + MID if cls.owner_state(cls, "select wp_matcher_set(%s::tenant_t, %s)", (T, MID)) == "ok" else "r2:m2:ucd15.0.0"
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

    def owner_state(self, sql, args=()):
        self.ev.execute("set role wp_owner")
        try:
            return D3Store.state(self, self.ev, sql, args)
        finally:
            self.ev.execute("reset role")

    def propose(self, conn, s, h, rv=None, t=T, pid=None):
        pid = pid or str(uuid.uuid4())
        conn.execute("select wp_identifier_propose(%s::tenant_t, %s, %s, %s, %s, %s)", (t, s, pid, h, b"sealed", rv or self.RV))
        return pid

    def event(self, eid, speaker, mentions, content=b"orig", t=T):
        ev = self.ev
        ev.execute("set role wp_owner")
        try:
            ev.execute("insert into subjects(tenant_id, subject_id) select %s, x from unnest(%s::text[]) x on conflict do nothing",
                       (t, [speaker] + list(mentions)))
            ev.execute("alter table events disable trigger events_guard")
            n = ev.execute("select count(*) from events where tenant_id=%s", (t,)).fetchone()[0]
            ev.execute("insert into events(tenant_id, session_id, seq, event_id, idem_key, req_hash, kind, subject_id, actor, trust_origin, "
                       "ts_event, content_enc, content_digest, mentions, prev_hash, hash, lease_scope, lease_epoch) values "
                       "(%s, 's1', %s, %s, %s, 'rh', 'message', %s, 'user', 'user_typed', now(), %s, %s, %s, 'p', %s, 'session:s1', 1)",
                       (t, n + 1, eid, eid, speaker, content, sha(b"orig"), list(mentions), "h-" + eid))
        finally:
            ev.execute("alter table events enable always trigger events_guard")
            ev.execute("reset role")

    def new_job(self, s, t=T):
        jid = str(uuid.uuid4())
        self.app.execute("insert into erasure_jobs(job_id, tenant_id, subject_id) values (%s, %s, %s)", (jid, t, s))
        return jid

    def job(self, s, t=T):
        jid = self.new_job(s, t)
        self.app.execute("select wp_erasure_mark(%s)", (jid,))
        return jid

    def cascade(self, jid):
        for _ in range(50):
            if self.app.execute("select wp_erasure_cascade(%s, 100)", (jid,)).fetchone()[0] == 0:
                return
        raise AssertionError("cascade did not finish")

    def waiting(self, app_name, alive, secs=5.0):
        """True once the backend named app_name waits on a lock; False if `alive()` turns false first or on timeout."""
        t0 = time.time()
        while time.time() - t0 < secs:
            if self.ev.execute("select count(*) from pg_stat_activity where application_name=%s and wait_event_type='Lock'",
                               (app_name,)).fetchone()[0]:
                return True
            if not alive():
                return False
            time.sleep(0.05)
        return False

    def one(self, sql, args=()):
        return self.ev.execute(sql, args).fetchone()

    # ------------------------------------------------------------------ A: reviewer role
    def test_a1_app_has_no_path_to_subject_identifiers(self):
        self.assertEqual(self.state(self.app, "insert into subject_identifiers(tenant_id, ident_hmac, subject_id, declared_seq) values (%s, 'h', 'X', 1)", (T,)), "42501")
        self.assertEqual(self.state(self.app, "select wp_identifier_declare(%s::tenant_t, 'X', array['h'])", (T,)), "42501")
        pid = self.propose(self.app, "PA", "hA")
        self.assertEqual(self.state(self.app, "select wp_identifier_approve(%s, 'hA', %s)", (pid, self.RV)), "42501")
        self.assertEqual(self.one("select proposed_by, state from identifier_proposals where proposal_id=%s", (pid,)), ("wp_app", "proposed"))

    def test_a2_no_self_approval(self):
        pid = self.propose(self.r1, "PB", "hB")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hB', %s)", (pid, self.RV)), "WPS30")
        self.assertEqual(self.state(self.r2, "select wp_identifier_approve(%s, 'hB', %s)", (pid, self.RV)), "ok")
        self.assertEqual(self.one("select subject_id from subject_identifiers where tenant_id=%s and ident_hmac='hB'", (T,)), ("PB",))

    def test_a3_binding_rule_version_and_state(self):
        pid = self.propose(self.app, "PC", "hC")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hOther', %s)", (pid, self.RV)), "WPS31")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hC', %s)", (pid, RV13)), "WPS32")
        self.assertEqual(self.state(self.r1, "select wp_identifier_reject(%s, 'test')", (pid,)), "ok")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hC', %s)", (pid, self.RV)), "WPS33")
        self.assertEqual(self.one("select count(*) from subject_identifiers where tenant_id=%s and ident_hmac='hC'", (T,)), (0,))

    # ------------------------------------------------------------------ C1 (F1): no redaction path
    def test_c1_app_has_no_redaction_path(self):
        self.event("c1e", "Y1", ["X1"])                     # another speaker's event that mentions X1
        j = self.job("X1")
        # review C F1 on 4fe3ac1: the app "redacts" by sealing the UNCHANGED plaintext; the store accepted it
        got = self.state(self.app, "select wp_erasure_redact(%s, %s, %s, %s, %s, %s, %s)",
                         (j, "c1e", sha(b"orig"), b"orig", [0, 4], "h-c1e", "hx"))
        self.assertNotEqual(got, "ok", "the app stored a 'redaction' that it computed itself")
        self.assertEqual(self.one("select to_regclass('event_redactions') is null, "
                                  "not exists (select 1 from pg_proc where proname = 'wp_erasure_redact')"), (True, True))
        self.cascade(j)
        self.assertEqual(self.one("select content_enc is null from events where tenant_id=%s and event_id='c1e'", (T,)), (True,))
        self.assertEqual(self.one("select subject_id, job_id from tombstones where tenant_id=%s and event_id='c1e'", (T,)), ("X1", j))

    # ------------------------------------------------------------------ C2 (F8): lock before check
    def test_c2_proposal_cannot_commit_after_the_erasure(self):
        """Reviewer's schedule: propose reads erased=false and pauses; the mark and the whole cascade finish; then the
        proposal commits. The pause is forced: a holder transaction inserts the SAME proposal_id uncommitted, so
        propose's insert waits AFTER its erased check. With lock-before-check the mark must wait for the proposal."""
        s, pid = "PF", str(uuid.uuid4())
        self.app.execute("select wp_subjects_ensure(%s::tenant_t, array[%s])", (T, s))
        jid = self.new_job(s)
        hold = self.db.admin(db=self.name, autocommit=False)
        p = self.db.connect(db=self.name, application_name="wp-d3-c2-prop")
        m = self.db.connect(db=self.name, application_name="wp-d3-c2-mark")
        out = {}
        try:
            hold.execute("set role wp_owner")
            hold.execute("insert into identifier_proposals(tenant_id, proposal_id, subject_id, ident_hmac, ident_enc, rule_version, proposed_by) "
                         "values (%s, %s, %s, 'hold', '\\x00', 'hold', 'wp_owner')", (T, pid, s))
            tp = threading.Thread(target=lambda: out.__setitem__("propose", self.state(p, "select wp_identifier_propose(%s::tenant_t, %s, %s, %s, %s, %s)",
                                                                                   (T, s, pid, "hF", b"sealed", self.RV))))
            tp.start()
            self.assertTrue(self.waiting("wp-d3-c2-prop", tp.is_alive), "propose did not reach its insert")
            tm = threading.Thread(target=lambda: out.__setitem__("mark", self.state(m, "select wp_erasure_mark(%s)", (jid,))))
            tm.start()
            mark_waited = self.waiting("wp-d3-c2-mark", tm.is_alive, secs=3.0)
            if not mark_waited:                              # the mark went past the paused proposal: finish the erasure
                tm.join(10)
                self.cascade(jid)
            hold.rollback()
            tp.join(10)
            tm.join(10)
            if mark_waited:
                self.cascade(jid)
        finally:
            hold.rollback()
            for c in (hold, p, m):
                c.close()
        # the lock-order assertion first (F8), so a red names its cause; the cleanup assertion after it
        self.assertTrue(mark_waited, "the mark did not wait for an in-flight proposal (check without a lock)")
        self.assertEqual(out.get("mark"), "ok")
        self.assertIn(out.get("propose"), ("ok", "WPS03"))
        self.assertEqual(self.one("select count(*) from identifier_proposals where tenant_id=%s and subject_id=%s", (T, s)), (0,),
                         "a proposal survived the completed erasure")

    def test_c3_approval_after_mark_refused(self):
        pid = self.propose(self.app, "PG", "hG")
        self.job("PG")
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hG', %s)", (pid, self.RV)), "WPS03")

    # ------------------------------------------------------------------ C4 (F3): the matcher is stored and enforced
    def append(self, sid, subject, matcher):
        """One append through events_guard as the app role (lease, scan versions, digest, chain all valid)."""
        self.app.execute("select wp_subjects_ensure(%s::tenant_t, array[%s])", (TM, subject))
        epoch = self.app.execute("select epoch from wp_lease_acquire(%s::tenant_t, %s, 60, true, 'c4', 2000)", (TM, "session:" + sid)).fetchone()[0]
        mx = self.one("select max_seq from identifier_epochs where tenant_id=%s", (TM,))[0]
        scan_v = self.one("select wp_secret_scan_version()")[0]      # the app sends the literal (log.append: SEC.VERSION)
        eid = str(uuid.uuid4())
        return self.state(self.app, "insert into events(tenant_id, session_id, seq, event_id, idem_key, req_hash, kind, subject_id, actor, "
                          "trust_origin, ts_event, content_enc, content_digest, mentions, mention_scan_upto, mention_matcher, secret_scan, "
                          "prev_hash, hash, lease_scope, lease_epoch) values (%s, %s, 1, %s, %s, 'rh', 'message', %s, 'user', 'user_typed', "
                          "now(), %s, %s, '{}', %s, %s, %s, %s, 'hz', %s, %s)",
                          (TM, sid, eid, eid, subject, b"c", sha(b"c"), mx, matcher, scan_v, "0" * 64, "session:" + sid, epoch))

    def test_c4_matcher_stored_and_enforced(self):
        self.assertEqual(self.owner_state("select wp_matcher_set(%s::tenant_t, %s)", (TM, "mA")), "ok")
        old = self.propose(self.app, "PM1", "hM1", rv=RV13, t=TM)
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hM1', %s)", (old, RV13)), "WPS32",
                         "an identifier reviewed under another matcher was approved")
        new = self.propose(self.app, "PM2", "hM2", rv="r3:mA", t=TM)
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, 'hM2', 'r3:mA')", (new,)), "ok")
        self.assertEqual(self.one("select matcher from subject_identifiers where tenant_id=%s and ident_hmac='hM2'", (TM,)), ("mA",))
        self.assertEqual(self.owner_state("select wp_matcher_set(%s::tenant_t, %s)", (TM, "mB")), "WPS38",
                         "the matcher changed under existing identifiers")
        self.assertEqual(self.append("c4a", "W1", "mB"), "WPS38", "an append scanned by another matcher")
        self.assertEqual(self.append("c4b", "W1", "mA"), "ok")
        j = self.job("PM2", t=TM)
        upto = self.one("select max_seq from identifier_epochs where tenant_id=%s", (TM,))[0]
        self.assertEqual(self.state(self.app, "select wp_erasure_rescan_page(%s, %s, %s)", (j, [], "mB")), "WPS38")
        self.assertEqual(self.state(self.app, "select wp_erasure_rescan_done(%s, %s, %s)", (j, upto, "mB")), "WPS38")
        self.assertEqual(self.state(self.app, "select wp_erasure_rescan_page(%s, %s, %s)", (j, [], "mA")), "ok")

    # ------------------------------------------------------------------ C5 (F7): verification sees deletion
    def verify_nulls(self, eids):
        cols = ("select seq, event_id, kind, subject_id, actor, trust_origin, ts_event, content_digest, prev_hash, hash, lease_epoch, "
                "decision_item, decision_action, based_on_event_id, source_event_id, content_enc, mentions, mention_scan_upto "
                "from events where tenant_id=%s and event_id = any(%s) order by seq")
        rows = self.ev.execute(cols, (T, eids)).fetchall()
        f = getattr(self.LG, "_verify_nulls", None)
        if f is None:                                        # 4fe3ac1: the same rule lived in _verify_redactions
            return self.LG._verify_redactions(self.ev, T, "s1", None, rows)
        return f(self.ev, T, rows)

    def raw(self, sql, args=()):
        """A database-level change by the table owner, below every app grant (what F7 asks verification to notice).
        The schema enables its triggers ALWAYS, so session_replication_role would not bypass them; wp_owner may."""
        self.ev.execute("set role wp_owner")
        try:
            self.ev.execute(sql, args)
        finally:
            self.ev.execute("reset role")

    def test_c5_null_content_needs_tombstone_and_job(self):
        self.event("c5a", "V1", [])
        self.event("c5b", "V2", [])
        self.event("c5c", "V3", [])
        ja, jb, jc = self.job("V1"), self.job("V2"), self.job("V3")
        for j in (ja, jb, jc):
            self.cascade(j)
        self.assertTrue(self.verify_nulls(["c5a", "c5b", "c5c"]), "control: an erased event with its tombstone and job")
        self.raw("delete from erasure_jobs where job_id=%s", (ja,))
        self.assertFalse(self.verify_nulls(["c5a"]), "NULL content verified after its erasure job was deleted")
        self.raw("delete from tombstones where tenant_id=%s and event_id='c5b'", (T,))
        self.assertFalse(self.verify_nulls(["c5b"]), "NULL content verified with no tombstone")
        self.raw("update tombstones set job_id=%s where tenant_id=%s and event_id='c5c'", (jb, T))
        self.assertFalse(self.verify_nulls(["c5c"]), "NULL content verified with another subject's job")


if __name__ == "__main__":
    unittest.main()

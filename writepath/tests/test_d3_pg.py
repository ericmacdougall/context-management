"""Live Postgres test for D3 after review C (DESIGN-NOTES "Redesign after review C"; ASSUMPTION under decision 10;
decision R1, decided by the coordinator on the user's delegation 2026-11-17: no span redaction, whole-event removal
only). Builds the schema from scratch in a throwaway database (as
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
  E1-E5 (review E F4, the hold class) forced interleavings of rescan links and hold placement/release against a paused
        removal: a link to an event being removed waits; a hold on any subject the removal judged (speaker, rescan
        link, the subject itself) waits and then reports the event as removed; a hold placed first withholds it
  E6  (review E F12) a repeated mark during a batch does not deadlock (mark takes the subject before the job)
  B1-B4 (matcher budget m5 b1, DESIGN-NOTES "Matcher budget") a write refused for its matcher budget (WPM02), size
        (WPM01) or the per-call limit (WPM03) leaves no events, quarantine or key row and is counted in write_refusals; the store bounds ciphertext
        size; the erasure rescan finds a mention under the budget and fails closed (WPS24) on an over-budget legacy event
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

    # ------------------------------------------------------------------ E (review E F4, F12): the hold class
    # Forced interleavings. A removal is PAUSED right after its hold decision: a holder transaction (wp_owner) inserts,
    # uncommitted, the tombstone row the batch is about to insert, so the batch waits on that row with every lock it
    # took before (event rows, footprint subjects). The concurrent operation then runs; "waited" means its backend waited
    # on a lock while the removal was in flight.
    def legal(self, name):
        return self.db.connect(db=self.name, application_name=name, role=self.db.LEGAL)

    def run_bg(self, out, key, conn, sql, args, fetch=False):
        import psycopg

        def go():
            try:
                cur = conn.execute(sql, args)
                out[key] = cur.fetchone()[0] if fetch else "ok"
            except psycopg.Error as e:
                out[key] = e.sqlstate
        th = threading.Thread(target=go)
        th.start()
        return th

    def paused_batch(self, jid, subject, eid, name, out):
        """Start one cascade batch of jid and pause it at the tombstone of eid (after its hold decision)."""
        hold = self.db.admin(db=self.name, autocommit=False)
        hold.execute("set role wp_owner")
        hold.execute("insert into tombstones(tenant_id, event_id, subject_id, job_id) values (%s, %s, %s, %s)", (T, eid, subject, jid))
        c = self.db.connect(db=self.name, application_name=name)
        th = self.run_bg(out, "batch", c, "select wp_erasure_cascade(%s, 100)", (jid,))
        self.assertTrue(self.waiting(name, th.is_alive), "the batch did not reach its tombstone insert")
        return hold, th, c

    def place(self, conn, s, hid):
        return conn.execute("select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, s, hid)).fetchone()[0]

    def content_gone(self, eid):
        return self.one("select content_enc is null from events where tenant_id=%s and event_id=%s", (T, eid))[0]

    def test_e1_rescan_link_waits_for_an_inflight_removal(self):
        """Review E F4, the reviewer's schedule: A's batch removes e (written by B); C's rescan links e to C; a hold on
        C commits. The link must wait for the removal, so the hold on C (which does not wait: C is not linked yet)
        does not cover e, and the link lands after e is gone: hold, removal, link is a consistent serial history."""
        self.event("e1e", "E1B", ["E1A"])
        jc = self.job("E1C")
        ja = self.job("E1A")
        out, lg = {}, self.legal("wp-e1-legal")
        link = self.db.connect(db=self.name, application_name="wp-e1-link")
        hold, tb, c = self.paused_batch(ja, "E1A", "e1e", "wp-e1-batch", out)
        try:
            tl = self.run_bg(out, "link", link, "select wp_erasure_add_mentions(%s, %s)", (jc, ["e1e"]))
            link_waited = self.waiting("wp-e1-link", tl.is_alive, secs=3.0)
            th = self.run_bg(out, "hold", lg, "select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, "E1C", "h-e1"), fetch=True)
            th.join(10)
            hold.rollback()
            tb.join(10)
            tl.join(10)
        finally:
            hold.rollback()
            for x in (hold, c, link, lg):
                x.close()
        self.assertTrue(link_waited, "a rescan link committed while the removal of its event was in flight")
        self.assertEqual((out.get("batch"), out.get("link")), ("ok", "ok"))
        self.assertTrue(self.content_gone("e1e"))
        self.assertNotIn("e1e", (out.get("hold") or {}).get("linked_removed_ids") or [], "the hold on C did not cover e")
        self.assertEqual(self.one("select count(*) from erasure_mentions where tenant_id=%s and subject_id='E1C' and event_id='e1e'", (T,)), (1,))

    def test_e2_hold_on_the_speaker_waits_and_reports_the_removal(self):
        self.event("e2e", "E2B", ["E2A"])
        ja = self.job("E2A")
        out, lg = {}, self.legal("wp-e2-legal")
        hold, tb, c = self.paused_batch(ja, "E2A", "e2e", "wp-e2-batch", out)
        try:
            th = self.run_bg(out, "hold", lg, "select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, "E2B", "h-e2"), fetch=True)
            hold_waited = self.waiting("wp-e2-legal", th.is_alive, secs=3.0)
            hold.rollback()
            tb.join(10)
            th.join(10)
        finally:
            hold.rollback()
            for x in (hold, c, lg):
                x.close()
        self.assertTrue(hold_waited, "a hold on the speaker landed while the removal it judged was in flight")
        self.assertTrue(self.content_gone("e2e"))
        self.assertIn("e2e", (out.get("hold") or {}).get("linked_removed_ids") or [], "the hold did not record the removal")

    def test_e3_hold_on_a_rescan_linked_subject_waits_and_reports(self):
        self.event("e3e", "E3B", ["E3A"])
        jc = self.job("E3C")
        self.app.execute("select wp_erasure_add_mentions(%s, %s)", (jc, ["e3e"]))      # link committed first
        ja = self.job("E3A")
        out, lg = {}, self.legal("wp-e3-legal")
        hold, tb, c = self.paused_batch(ja, "E3A", "e3e", "wp-e3-batch", out)
        try:
            th = self.run_bg(out, "hold", lg, "select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, "E3C", "h-e3"), fetch=True)
            hold_waited = self.waiting("wp-e3-legal", th.is_alive, secs=3.0)
            hold.rollback()
            tb.join(10)
            th.join(10)
        finally:
            hold.rollback()
            for x in (hold, c, lg):
                x.close()
        self.assertTrue(hold_waited, "a hold on a rescan-linked subject landed while the removal it judged was in flight")
        self.assertIn("e3e", (out.get("hold") or {}).get("linked_removed_ids") or [])

    def test_e4_hold_first_withholds_and_release_waits(self):
        self.event("e4a", "E4B", ["E4A"])
        self.event("e4b", "E4D", ["E4A"])
        lg = self.legal("wp-e4-legal")
        self.place(lg, "E4B", "h-e4")                                                 # placed before the removal
        ja = self.job("E4A")
        out = {}
        hold, tb, c = self.paused_batch(ja, "E4A", "e4b", "wp-e4-batch", out)
        try:
            tr = self.run_bg(out, "release", lg, "select wp_hold_release(%s)", ("h-e4",))
            release_waited = self.waiting("wp-e4-legal", tr.is_alive, secs=3.0)
            hold.rollback()
            tb.join(10)
            tr.join(10)
        finally:
            hold.rollback()
            c.close()
            hold.close()
        self.assertTrue(release_waited, "a release landed while the removal that judged the hold was in flight")
        self.assertEqual((self.content_gone("e4a"), self.content_gone("e4b")), (False, True), "e4a withheld, e4b removed")
        self.assertEqual(self.one("select count(*) from erasure_withheld where job_id=%s and event_id='e4a'", (ja,)), (1,))
        self.cascade(ja)                                                              # the hold is gone: retried
        lg.close()
        self.assertTrue(self.content_gone("e4a"))

    def test_e5_hold_on_the_subject_itself(self):
        self.event("e5a", "E5A", [])
        ja = self.job("E5A")
        lg = self.legal("wp-e5-legal")
        self.place(lg, "E5A", "h-e5a")
        self.assertEqual(self.state(self.app, "select wp_erasure_cascade(%s, 100)", (ja,)), "WPS16")
        lg.execute("select wp_hold_release('h-e5a')")
        self.cascade(ja)
        self.assertTrue(self.content_gone("e5a"))
        self.event("e5b", "E5S", [])
        js = self.job("E5S")
        out = {}
        hold, tb, c = self.paused_batch(js, "E5S", "e5b", "wp-e5-batch", out)
        try:
            th = self.run_bg(out, "hold", lg, "select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, "E5S", "h-e5s"), fetch=True)
            hold_waited = self.waiting("wp-e5-legal", th.is_alive, secs=3.0)
            hold.rollback()
            tb.join(10)
            th.join(10)
        finally:
            hold.rollback()
            for x in (hold, c, lg):
                x.close()
        self.assertTrue(hold_waited)
        self.assertIn("e5b", (out.get("hold") or {}).get("linked_removed_ids") or [])

    def test_e6_repeated_mark_during_a_batch_does_not_deadlock(self):
        """Review E F12: a batch holds the subject FOR SHARE and later updates the job cursor; a repeated mark took the
        job FOR UPDATE and then waited for the subject: a cycle (40P01). Mark now takes the subject first."""
        self.event("e6e", "E6A", [])
        ja = self.job("E6A")
        out = {}
        m = self.db.connect(db=self.name, application_name="wp-e6-mark")
        hold, tb, c = self.paused_batch(ja, "E6A", "e6e", "wp-e6-batch", out)
        try:
            tm = self.run_bg(out, "mark", m, "select wp_erasure_mark(%s)", (ja,))
            mark_waited = self.waiting("wp-e6-mark", tm.is_alive, secs=3.0)
            hold.rollback()
            tb.join(15)
            tm.join(15)
        finally:
            hold.rollback()
            for x in (hold, c, m):
                x.close()
        self.assertTrue(mark_waited)
        self.assertEqual((out.get("batch"), out.get("mark")), ("ok", "ok"), "40P01 is the deadlock")

    def test_e7_hold_against_mark(self):
        """Hold x mark, both orders. A hold placed first refuses the mark (WPS16). A hold placed while a mark is paused
        (after its hold check: a holder transaction inserts the erasure_mentions row the mark is about to insert) waits
        and then reports the subject past mark. A regression guard: mark holds the subject three ways (FOR UPDATE, the
        hold check's FOR SHARE, the erased update before the pause), so no single-lock variant can make it fail."""
        lg = self.legal("wp-e7-legal")
        self.place(lg, "E7F", "h-e7f")
        jf = self.new_job("E7F")
        self.assertEqual(self.state(self.app, "select wp_erasure_mark(%s)", (jf,)), "WPS16")
        self.event("e7e", "E7B", ["E7S"])
        js = self.new_job("E7S")
        hold = self.db.admin(db=self.name, autocommit=False)
        hold.execute("set role wp_owner")
        hold.execute("insert into erasure_mentions(tenant_id, subject_id, event_id, job_id, source) values (%s, 'E7S', 'e7e', 'x', 'write')", (T,))
        out = {}
        m = self.db.connect(db=self.name, application_name="wp-e7-mark")
        try:
            tm = self.run_bg(out, "mark", m, "select wp_erasure_mark(%s)", (js,))
            self.assertTrue(self.waiting("wp-e7-mark", tm.is_alive), "the mark did not reach its insert")
            th = self.run_bg(out, "hold", lg, "select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, "E7S", "h-e7s"), fetch=True)
            hold_waited = self.waiting("wp-e7-legal", th.is_alive, secs=3.0)
            hold.rollback()
            tm.join(10)
            th.join(10)
        finally:
            hold.rollback()
            for x in (hold, m, lg):
                x.close()
        self.assertTrue(hold_waited, "a hold landed inside a mark")
        self.assertEqual(out.get("mark"), "ok")
        self.assertEqual((out.get("hold") or {}).get("open_erasure_state"), "marked")

    def test_e8_hold_waits_for_the_final_pass(self):
        """Hold x the cascade's final pass (facts, inbox, quarantine, proposals, identifiers of the subject), which no
        apply() runs in: only the batch's hold check (subject FOR SHARE) keeps a hold out. The pass is paused on the
        subject's proposal row (a holder transaction locks it FOR UPDATE)."""
        pid = self.propose(self.app, "E8S", "hE8")
        js = self.job("E8S")
        hold = self.db.admin(db=self.name, autocommit=False)
        hold.execute("set role wp_owner")
        hold.execute("select 1 from identifier_proposals where proposal_id=%s for update", (pid,))
        out, lg = {}, self.legal("wp-e8-legal")
        c = self.db.connect(db=self.name, application_name="wp-e8-batch")
        try:
            tb = self.run_bg(out, "batch", c, "select wp_erasure_cascade(%s, 100)", (js,))
            self.assertTrue(self.waiting("wp-e8-batch", tb.is_alive), "the final pass did not reach the proposal")
            th = self.run_bg(out, "hold", lg, "select wp_hold_place(%s::tenant_t, %s, %s, 'test')", (T, "E8S", "h-e8"), fetch=True)
            hold_waited = self.waiting("wp-e8-legal", th.is_alive, secs=3.0)
            hold.rollback()
            tb.join(10)
            th.join(10)
        finally:
            hold.rollback()
            for x in (hold, c, lg):
                x.close()
        self.assertTrue(hold_waited, "a hold landed inside the final pass")
        self.assertEqual(out.get("batch"), "ok")
        self.assertEqual(self.one("select count(*) from identifier_proposals where proposal_id=%s", (pid,)), (0,))


    # ------------------------------------------------------------------ B (matcher budget m5 b1, DESIGN-NOTES B3-B7)
    def refused(self, call, code):
        try:
            call()
        except Exception as e:                           # noqa: BLE001  (the type does not exist before b1; the code is the contract)
            self.assertEqual(getattr(e, "code", None), code, f"refused with {type(e).__name__}: {e}")
            return e
        self.fail(f"not refused: expected {code}")

    def test_b1_refused_write_leaves_no_row_and_is_recorded(self):
        import datetime
        import types
        TR = "d3b1"

        class NoKS:                                      # a refused write must not reach the key service (no subject key)
            def __getattr__(self, name):
                raise RuntimeError(f"key service used before the refusal: {name}")

        lease = types.SimpleNamespace(tenant=TR, scope="session:b1", epoch=1)

        def ev(content):
            return dict(subject_id="B1S", kind="message", actor="user", trust_origin="user_typed",
                        ts_event=datetime.datetime.now(datetime.timezone.utc), content=content)
        crafted = " ".join("1I1I1I" for _ in range(300))
        self.refused(lambda: self.LG.append(self.app, NoKS(), lease, [ev("fine"), ev(crafted)]), "WPM02")
        self.refused(lambda: self.LG.append(self.app, NoKS(), lease, [ev("a" * 65537)]), "WPM01")
        self.refused(lambda: self.LG.append(self.app, NoKS(), lease, [ev("x")] * 1001), "WPM03")       # per-call limit (B10)
        self.assertEqual(self.one("select count(*) from events where tenant_id=%s", (TR,)), (0,))
        self.assertEqual(self.one("select count(*) from quarantine where tenant_id=%s", (TR,)), (0,))
        self.assertEqual(self.ev.execute("select code, n from write_refusals where tenant_id=%s order by code", (TR,)).fetchall(),
                         [("WPM01", 1), ("WPM02", 1), ("WPM03", 1)])
        self.assertEqual(self.state(self.app, "insert into write_refusals(tenant_id, day, code, n) values (%s, current_date, 'WPM02', 1)", (TR,)),
                         "42501", "the app writes the refusal table directly")
        self.assertNotEqual(self.state(self.app, "select wp_write_refused(%s::tenant_t, 'not-a-code')", (TR,)), "ok")

    def test_b2_store_bounds_ciphertext_size(self):
        # 65,536 bytes of content plus 28 (nonce and tag) is the largest ciphertext the store keeps
        for size, want in ((65564, "ok"), (65565, "23514")):
            got = self.owner_state("insert into quarantine(tenant_id, session_id, subject_id, presented_epoch, reason, kind, content_enc) "
                                   "values ('d3b2', 's', 'B2S', 1, 'stale_epoch', 'message', %s)", (b"x" * size,))
            self.assertEqual(got, want, f"quarantine ciphertext of {size} bytes")
        try:
            self.event("b2-ok", "B2S", [], content=b"x" * 65564, t="d3b2")
            fine = "ok"
        except Exception as e:                           # noqa: BLE001
            fine = getattr(e, "sqlstate", repr(e))
        self.assertEqual(fine, "ok")
        try:
            self.event("b2-big", "B2S", [], content=b"x" * 65565, t="d3b2")
            big = "ok"
        except Exception as e:                           # noqa: BLE001
            big = getattr(e, "sqlstate", repr(e))
        self.assertEqual(big, "23514", "an oversized event ciphertext was stored")

    def rescan_tenant(self, t, subject, name):
        """A tenant on THIS process's matcher with one approved identifier for `subject`, and its marked erasure job."""
        from wp import mentions as M
        self.assertEqual(self.owner_state("select wp_matcher_set(%s::tenant_t, %s)", (t, M.matcher_id())), "ok")
        h = M.subject_hmacs(FixedKS(), t, [name]).pop()
        pid = self.propose(self.app, subject, h, rv=M.rule_version(), t=t)
        self.assertEqual(self.state(self.r1, "select wp_identifier_approve(%s, %s, %s)", (pid, h, M.rule_version())), "ok")
        return self.job(subject, t=t)

    def legacy_event(self, t, eid, speaker, text):
        """An event as an older matcher admitted it: sealed under the fixed test key, scanned against version 0."""
        from wp import keys as K
        self.event(eid, speaker, [], content=K.seal(FixedKS.SUBJ_KEY, t, speaker, eid, text), t=t)

    def test_b3_rescan_finds_a_mention_under_the_budget(self):
        from wp import erasure
        j = self.rescan_tenant("d3b3", "B3X", "Rosalind Okafor")
        self.legacy_event("d3b3", "b3e", "B3Y", "I met Rosalind Okafor today.")
        scanned, found = erasure.rescan(self.app, FixedKS(), j)
        self.assertEqual((scanned, found), (1, 1))

    def test_b4_rescan_of_an_over_budget_legacy_event_fails_closed(self):
        from wp import erasure, mentions as M
        j = self.rescan_tenant("d3b4", "B4X", "Rosalind Okafor")
        self.legacy_event("d3b4", "b4e", "B4Y", " ".join("1I1I1I" for _ in range(300)) + " Rosalind Okafor")
        self.refused(lambda: erasure.rescan(self.app, FixedKS(), j), "WPM02")
        self.assertEqual(self.one("select rescanned_xmax from erasure_jobs where job_id=%s", (j,)), (None,))
        upto = self.one("select ident_xmax from erasure_jobs where job_id=%s", (j,))[0]
        self.assertEqual(self.state(self.app, "select wp_erasure_rescan_done(%s, %s, %s)", (j, upto, M.matcher_id())), "WPS24",
                         "the erasure completed without scanning the over-budget event")


class FixedKS:
    """Fixed test keys: one subject key for every speaker, one tenant HMAC key (opaque test bytes, not a secret)."""
    SUBJ_KEY = bytes(range(32))
    HKEY = b"test-only-fixed-key"

    def keys(self, tenant, subjects, create=False):
        return {s: self.SUBJ_KEY for s in subjects}

    def tenant_hmac_many(self, tenant, texts):
        import hmac as _h
        return [_h.new(self.HKEY, x.encode(), hashlib.sha256).hexdigest() for x in texts]


if __name__ == "__main__":
    unittest.main()

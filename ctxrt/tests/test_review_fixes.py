"""One test per review finding (research/ctxrt-review-1d19cd8.md). Each asserts the FIXED behaviour; ctxrt/results/repro-at-1d19cd8.txt holds the reviewer's
probe output showing the same cases CONFIRMED (failing) at 1d19cd8."""
import json
import tempfile
import time
import unittest

from memcore.inbox import InboxItem
from ctxrt import msgshape as M
from ctxrt.config import SecretConfigError, load_secret
from ctxrt.decisions import DecisionLog
from ctxrt.delivery import AllowAllForTests, Delivery, DeliverySink
from ctxrt.findings import host_check, render_findings, to_item
from ctxrt.kernel import MemoryLog
from ctxrt.pulltools import CallerContext, PullTools
from ctxrt.routing import Agent, RecordStore, Registry, RoutingRefused
from ctxrt.rundb import Fenced, NestStore, RunDB
from ctxrt import session as S

SEC = b"test-secret-0123456789abcdef"


def newd(**kw):
    kw.setdefault("policy", AllowAllForTests())
    return Delivery(NestStore(":memory:"), lease_s=0.05, **kw)


class T(unittest.TestCase):
    def test_f1_partial_confirm_refused(self):
        d = newd()
        d.new_lineage("S", "L")
        for f in ("fX", "fY"):
            d.produce("S", f)
        a = d.begin_turn("L", "A", limit=5)
        time.sleep(0.07)
        b = d.begin_turn("L", "B", limit=1)
        d.commit_turn("L", "B", b)
        with self.assertRaises(Fenced):
            d.commit_turn("L", "A", a)
        time.sleep(0.07)
        d.run_turn("L", "D")
        self.assertEqual(max(d.appearances("L").values()), 1)

    def test_f2_id_collision_rollback(self):
        d = newd()
        d.new_lineage("S1", "L1")
        d.new_lineage("S2", "L2")
        d.produce("S1", "same")
        d.produce("S2", "same")
        cp = d.checkpoint("L2")
        d.run_turn("L2", "T")
        d.rollback("L2", cp)
        time.sleep(0.07)
        d.run_turn("L2", "R")
        self.assertEqual(d.appearances("L2")["same"], 1)

    def test_f10_turn_id_reuse_after_rollback(self):
        d = newd()
        d.new_lineage("S", "L")
        d.produce("S", "p0")
        d.run_turn("L", "T0")
        cp = d.checkpoint("L")
        d.produce("S", "fA")
        d.run_turn("L", "T7")
        d.rollback("L", cp)
        shown = d.run_turn("L", "T7")["finding_ids"]
        time.sleep(0.07)
        shown += d.run_turn("L", "T8")["finding_ids"]
        self.assertEqual(shown.count("fA"), 1)

    def _log(self):
        log = MemoryLog(SEC)
        log.append("S1", "2026-01-01T00:00:00Z", "user", "my lunch is at noon today.", speaker="user", round=1)
        s2 = log.append("S2", "2026-01-01T00:00:00Z", "user", "please do NOT transfer the funds to Bob.", speaker="user", round=1)
        return log, log.events[s2 - 1]

    def test_f3_cross_session_and_fragment(self):
        log, ev = self._log()
        f = {"event_id": ev["oid"], "round": 1, "speaker": "user", "quote": "please do NOT transfer the funds to Bob"}
        self.assertEqual(host_check(log, f, frozenset(["S1"]), "t0")[0], "REJECT:session_not_readable")
        self.assertEqual(host_check(log, f, frozenset(["S2"]), "t0")[0], "PASS")
        self.assertTrue(host_check(log, dict(f, quote="transfer the funds to Bob"), frozenset(["S2"]), "t0")[0].startswith("REJECT"))

    def test_f3_speaker_rendered(self):
        log = MemoryLog(SEC)
        log.append("S1", "2026-01-01T00:00:00Z", "tool", "A web page said hello there.", speaker="tool", round=1)
        v, e = host_check(log, {"event_id": log.events[0]["oid"], "round": 1, "speaker": "tool", "quote": "A web page said hello there"}, frozenset(["S1"]), "t0")
        self.assertIn('"trust_origin":"tool"', render_findings([to_item("q", "S1", e["event"], e["span"], "tool", ["req1"], 0.0)], "t"))

    def test_f4_tenant_scope_and_cursor_owner(self):
        log, dec = MemoryLog(SEC), DecisionLog()
        for i in range(6):
            log.append("S1", "2026-01-0%dT00:00:00Z" % (i + 1), "user", "staging database item %d" % i, tenant="t1")
        log.append("S9", "2026-01-01T00:00:00Z", "user", "staging database SECRET salary", tenant="t2")
        pt = PullTools(log, dec, SEC)
        c1, c2 = CallerContext("t1", "u1", frozenset(["S1"])), CallerContext("t1", "u2", frozenset(["S1"]))
        r = pt.search(c1, "staging database", limit=2)
        self.assertNotIn("salary", json.dumps(r))
        self.assertEqual(pt.search(c2, "staging database", cursor=r["next_cursor"]), {"status": "error", "code": "invalid_cursor"})
        self.assertEqual(PullTools(log, dec, SEC).search(c1, "staging database", cursor=r["next_cursor"])["status"], "ok")

    def test_f5_seen_set_scoped_and_fenced(self):
        st = NestStore(":memory:")
        db = RunDB(st)
        db.open_step("s", 10)
        r = db.launch("A", "s", "q", 1, "v", 3, "o")
        db.seen_add(r["key"], r["epoch"], "t0", "A", "q", "c", "x")
        self.assertIsNone(db.seen_get("t0", "B", "q", "c"))
        db.stop_run(r["key"], "x")
        with self.assertRaises(Fenced):
            db.seen_add(r["key"], r["epoch"], "t0", "A", "q", "c2", "x")

    def test_f6_registry_rules(self):
        reg = Registry([Agent("boss", "m-a", "LB"), Agent("sub", "m-b", "LS", "boss"), Agent("peer", "m-a", "LP"), Agent("ok", "m-c", "LO")])
        self.assertIn("boss", reg.author_set("sub"))
        rs = RecordStore(reg)
        rec = rs.create("finding", "sub", "m-b", "b")
        with self.assertRaises(RoutingRefused):
            rs.approve(rec["rid"], "ok")
        self.assertEqual(rs.route_review(rec["rid"], ["boss", "sub", "peer", "ok"]), "ok")
        with self.assertRaises(RoutingRefused):
            rs.approve(rec["rid"], "nobody-registered")

    def test_f7_thinking_server_tool_and_placement(self):
        conv = [{"role": "user", "content": "task"},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {}},
                                                  {"type": "tool_use", "id": "c1", "name": "f", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "r"}]}]
        self.assertEqual(M.validate("anthropic", conv), [])
        out = M.inject("anthropic", conv, "<<<B>>> d <<<B>>>")
        self.assertEqual(M.validate("anthropic", out), [])
        self.assertTrue(M.validate("anthropic", M.text_after_results_inject("anthropic", conv, "<<<B>>> d <<<B>>>")))
        big = [{"role": "user", "content": "do the task " + "x" * 400}] + conv[1:]
        self.assertTrue(M.keeps_task("anthropic", big, M.trim("anthropic", big, 20).messages))

    def test_p2_ledger_secret_truncation_resume(self):
        d = tempfile.mkdtemp()
        path = d + "/l.sqlite"
        log = MemoryLog(SEC)
        for i in range(4):
            log.append("o", "2026-01-0%dT00:00:00Z" % (i + 1), "user", "the office is in place%d office" % i)
        h = S.Host(log, "office", SEC, layer="merged", ledger_path=path)
        h.turn("q about the office")
        blk = [e for e in h.history if "<<<MEMCORE" in e["text"]][0]["text"]
        self.assertEqual(S.Ledger(path).strip(blk).strip(), "q about the office")
        cut = blk[: blk.rindex("<<<")]
        self.assertIn("my words", S.Ledger(path).strip(cut + "\nmy words"))
        with self.assertRaises(SecretConfigError):
            load_secret(path)
        h.checkpoint()
        h.turn("again")
        h.resume(0)
        h.turn("post resume")
        self.assertEqual(len({e["id"] for e in h.history}), len(h.history))

    def test_p4_stop_withdraws(self):
        d = newd()
        d.new_lineage("S", "L")
        db = RunDB(d.store)
        db.open_step("s", 10)
        r = db.launch("S", "s", "q", 1, "v", 3, "o")
        sink = DeliverySink(d, "S")
        it = InboxItem("fW", "S", ["e"], "quote text here", "user", ["req1"], 0.0, 1e12)
        db.write_gate(r["key"], r["epoch"], lambda: sink.enqueue(it, author=("a", "m"), run_key=r["key"]))
        db.stop_run(r["key"], "misbehaved")
        self.assertEqual(d.run_turn("L", "T")["finding_ids"], [])


if __name__ == "__main__":
    unittest.main()

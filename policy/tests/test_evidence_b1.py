"""Evidence identity properties of verify/b1.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS amendments 3-5,
exp/b1_modelcheck.py; SEEDLESS): one run row per (arm, scenario, depth): enforced on r1 6, r2 8, r3 8, caps 8, joint 4, and each control
on its scenarios at the control depth (r1 6, r2 6, r3 6, caps 6); one counterexample row per control (identity: control); no enforced
counterexample. The audit at ea4677a found enforced scenarios could be omitted."""
import hashlib
import json
import unittest

from tests.evidence_props import Fam, Props, load

DEPTH = {"r1": 6, "r2": 8, "r3": 8, "caps": 8, "joint": 4}
CTL_DEPTH = {"r1": 6, "r2": 6, "r3": 6, "caps": 6}
CONTROLS = [("agent_may_decide", "I2_agent_head_move", ["r3"]), ("cap_blocks_humans", "I18_human_locked_out_by_nonhumans", ["caps"]),
            ("n2_no_role", "I15_proposal_without_role", ["r2"]), ("n2_no_cap", "I17_open_cap", ["caps"]),
            ("stale_unchecked", "I4_decision_not_bound_to_head", ["r1", "r2"]), ("agent_may_supersede", "I2_agent_head_move", ["r3"]),
            ("m2_no_original", "I6_redelivery_changed_state_or_lost_outcome", ["r2"]),
            ("idempotency_removed", "I6_redelivery_changed_state_or_lost_outcome", ["r2"]), ("authority_removed", "I1_authority", ["r2"])]
CODES = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
         "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted", "bad_event", "tenant_mismatch", "unauthorized",
         "principal_revoked", "unknown_req", "duplicate_id", "proposal_cap", "channel_mismatch", "agent_not_permitted", "agent_cannot_confirm",
         "unknown_pending", "pending_closed", "same_channel_confirmation", "unknown_proposal", "proposal_mismatch", "stale", "proposal_closed",
         "contested_needs_resolve", "not_contested", "nothing_to_retract", "unknown_candidate", "idem_conflict", "duplicate")
DOC = {"name": "b1_modelcheck", "params": {"depth": DEPTH, "ctl_depth": CTL_DEPTH}, "result": {"verdict": "PASS"}}
PRE = {"principals": {"u1": "human", "a1": "agent"}, "heads": {"r1": {"version": 0, "value_ref": None, "status": "current"}}, "revoked": [],
       "authority": {"r1": []}, "proposers": {"r1": []}, "props": {}, "idems": []}


def _ev(pid, **kw):
    return dict({"type": "accept", "id": "e1", "idem": "k1", "tenant": "T1", "principal": pid, "channel": "owner_ui", "req": "r1"}, **kw)


def _cex(name, inv):
    pre, post, code, orig = json.loads(json.dumps(PRE)), json.loads(json.dumps(PRE)), "decided_accept", None
    ev = _ev("u1", based_on=0)
    if inv == "I2_agent_head_move":
        ev = _ev("a1", based_on=0)
        post["heads"]["r1"]["version"] = 1
    elif inv == "I18_human_locked_out_by_nonhumans":
        ev, code = _ev("u1", type="propose"), "proposal_cap"
    elif inv == "I15_proposal_without_role":
        ev, code = _ev("u1", type="propose"), "proposed"
    elif inv == "I17_open_cap":
        ev, code = _ev("a1", type="propose"), "proposed"
        post["props"] = {"r1": {"p%d" % k: {"status": "open", "author": "a1", "author_kind": "agent", "kind": "proposal"} for k in range(4)}}
    elif inv == "I4_decision_not_bound_to_head":
        ev = _ev("u1", based_on=5)
    elif inv == "I6_redelivery_changed_state_or_lost_outcome":
        ev, code = _ev("u1", type="propose"), "proposed"
        fp = hashlib.sha256(json.dumps(ev, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        pre["idems"] = [["u1", "k1", fp, "proposed"]]
    return {"kind": "cex", "control": name, "expected": inv, "invariants": [inv], "trace": [ev], "code": code, "orig": orig, "pre": pre,
            "post": post, "ev": ev}


def _run(arm, scen, depth, codes):
    return {"kind": "run", "arm": arm, "scenario": scen, "depth": depth, "pool": 10, "states": 400, "transitions": 5000, "codes": codes,
            "violations": 0 if arm == "enforced" else 3, "violated": {}, "contested_states": 2 if arm == "enforced" else 0, "l1_checks": 2,
            "seconds": 1.0}


def honest():
    rows = [_cex(n, inv) for n, inv, _s in CONTROLS]
    rows += [_run("enforced", s, d, {c: 1 for c in CODES} if s == "r1" else {"proposed": 3}) for s, d in DEPTH.items()]
    rows += [_run(n, s, CTL_DEPTH[s], {"proposed": 1}) for n, _i, scs in CONTROLS for s in scs]
    return rows


class B1Evidence(Props, unittest.TestCase):
    V = load("b1")
    FAMILIES = (Fam("run", lambda r: r.get("kind") == "run", ["kind", "arm", "scenario", "depth"], index="depth", arm="arm",
                    end=lambda s, r: (DEPTH if r["arm"] == "enforced" else CTL_DEPTH)[r["scenario"]] + 1),
                Fam("control counterexample", lambda r: r.get("kind") == "cex" and r.get("control") != "enforced", ["kind", "control"], arm="control"))

    def doc(self):
        return DOC

    def build(self):
        return honest()

    def sample(self, fam):
        rows = self.rows()
        if fam.name == "run":
            return next(k for k, r in enumerate(rows) if r.get("kind") == "run" and r["arm"] == "enforced" and r["scenario"] == "r2"), \
                next(k for k, r in enumerate(rows) if r.get("kind") == "run" and r["arm"] == "enforced" and r["scenario"] == "r3")
        return Props.sample(self, fam)

    def break_row(self, fam, row):
        return dict(row, violations=1) if fam.name == "run" else dict(row, trace=[])

    def shrunk(self):
        d = dict(DOC, params=dict(DOC["params"], depth=dict(DEPTH, r1=3)))
        return [("enforced r1 at depth 3, declared", d, [dict(r, depth=3) if r.get("kind") == "run" and r["arm"] == "enforced" and r["scenario"] == "r1"
                                                         else r for r in honest()])]

    def test_audit_probe_enforced_pools_omitted(self):
        """The audit's probe 1: enforced r2 and r3 omitted, the remaining enforced pools large enough for the totals and the codes still
        covered (PASSED at ea4677a: it summed counters over whatever enforced rows were present)."""
        rows = [dict(r, states=500, transitions=10000) if r.get("kind") == "run" and r["arm"] == "enforced" else r for r in honest()
                if not (r.get("kind") == "run" and r["arm"] == "enforced" and r["scenario"] in ("r2", "r3"))]
        self.bad(rows, "enforced r2 and r3 omitted", ("missing identity",))

    def test_an_enforced_counterexample_fails(self):
        self.assertEqual(self.v(honest() + [dict(_cex("x", "I1_authority"), control="enforced", expected=None)]).get("verdict"), "FAIL")


if __name__ == "__main__":
    unittest.main()

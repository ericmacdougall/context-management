"""E4: an item queued under a permissive policy is refused at delivery after the policy tightens. Control: enqueue-time-only check.
usage: python -m exp.e4_tighten"""
import random
from dataclasses import replace

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.gen import base_policy
from pol.enforce import Block, Enforcer, Outbox
from pol.labels import Label
from pol.pdp import Principal
from pol.stubs import Network, StubChat

ROWS = []
R = 200
COMBOS = [("model", "model_banned"), ("model", "region_removed"), ("model", "sender_revoked"), ("model", "tenant_killed"),
          ("inbox", "model_banned"), ("inbox", "region_removed"), ("inbox", "recipient_revoked"), ("inbox", "tenant_killed"), ("inbox", "group_removed"),
          ("export", "region_removed"), ("export", "sender_revoked"), ("export", "tenant_killed"),
          ("inbox", "label_narrowed"), ("inbox", "source_erased"), ("inbox", "legal_hold_recorded")]


def one(seed, path, tighten, enqueue_only):
    rng = random.Random(seed)
    pol1 = base_policy()
    cur = [pol1]
    directory = {}
    enf = Enforcer(lambda: cur[0], directory=directory.get, clock=lambda: 10)
    net = Network()
    net.register("model", "send_model")
    net.register("inbox", "send_inbox")
    net.register("export", "export")
    sender = Principal("S", "t1", frozenset(["g1", "g2"]), 3, True, region="EU")
    rcp = Principal("R", "t1", frozenset(rng.choice([["g1"], ["g1", "g2"]])), rng.choice([2, 3]), False, region="EU", model="d_model_eu")
    directory.update({"S": sender, "R": rcp})
    marker = "MK%08x" % rng.getrandbits(32)
    lab = Label(["t1"], acl=[["g1"]] + ([["g2", "g1"]] if rng.random() < 0.5 else []), regions=["EU"], sensitivity=rng.choice([0, 1, 2]),
                subjects=rng.choice([[], ["s1"]]))
    blocks = [Block("payload %d " % k + marker, lab, ("x",)) for k in range(rng.randint(1, 3))]
    chat = StubChat(net, "d_model_eu", "m1")
    from pol.enforce import Record
    records = {"rec": Record("rec", "src", lab)}
    if path == "model":
        ob = Outbox(enf, directory.get, "send_model", lambda p, b, c: chat.complete([{"role": "user", "content": b[0].text}]), enqueue_only)
        ctx, rid = {"destination": "d_model_eu"}, None
    elif path == "export":
        ob = Outbox(enf, directory.get, "export", lambda p, b, c: net.record("export", "d_export_eu", p.tenant, b[0].text), enqueue_only)
        ctx, rid = {"destination": "d_export_eu"}, None
    else:
        ob = Outbox(enf, directory.get, "send_inbox", lambda p, b, c: net.record("inbox", "rcp:R", p.tenant, b[0].text), enqueue_only, resolver=records.get)
        ctx, rid = {}, "R"
    ob.enqueue("S", blocks, ctx, rid, **({"record_ids": ["rec"]} if path == "inbox" else {}))
    enq_entry = dict(enf.log.entries[-1]) if enf.log.entries else None
    if tighten:
        d = base_policy().destinations
        if tighten == "model_banned":
            cur[0] = pol1.tighten(banned_models=frozenset(["m1"]))
        elif tighten == "region_removed":
            dd = dict(pol1.destinations)
            for k in ("d_model_eu", "d_export_eu"):
                dd[k] = replace(dd[k], region="US")
            cur[0] = pol1.tighten(destinations=dd)
        elif tighten == "sender_revoked":
            cur[0] = pol1.tighten(revoked=frozenset(["S"]))
        elif tighten == "recipient_revoked":
            cur[0] = pol1.tighten(revoked=frozenset(["R"]))
        elif tighten == "tenant_killed":
            cur[0] = pol1.tighten(deny_tenants=frozenset(["t1"]))
        elif tighten == "group_removed":
            directory["R"] = replace(rcp, groups=frozenset(["g9"]))
        elif tighten == "label_narrowed":                # the SOURCE record is narrowed after enqueue; the policy is unchanged
            records["rec"] = Record("rec", "src", Label(["t1"], acl=[["hr"]], regions=["EU"]))
        elif tighten == "source_erased":
            del records["rec"]
        elif tighten == "legal_hold_recorded":           # the source is narrowed by a hold label after enqueue
            records["rec"] = Record("rec", "src", Label(["t1"], acl=[["hr"]], holds=["h1"]))
    delivered, refused = ob.deliver_all()
    escaped = [p for p in net.payloads if marker in p.text]
    last = enf.log.entries[-1] if enf.log.entries else None
    refusal_logged = bool(refused) and last is not None and last["allow"] is False and (
        last["policy_version"] == (2 if tighten not in ("group_removed", "label_narrowed", "source_erased", "legal_hold_recorded") else 1))
    return {"delivered": len(delivered), "refused": len(refused), "escaped_payloads": len(escaped), "enqueue_allow": bool(enq_entry and enq_entry["allow"]),
            "enqueue_version": enq_entry["policy_version"] if enq_entry else None, "refusal_logged_with_version": refusal_logged,
            "refusal_reason": refused[0]["refusal"].reason if refused else None}


def arm(enqueue_only, tighten_on, arm_name=""):
    out = {"runs": 0, "delivered": 0, "refused": 0, "escaped": 0, "enqueue_allowed_v1": 0, "refusal_logged_v2": 0, "by_combo": {}, "reasons": {}}
    for ci, (path, t) in enumerate(COMBOS):
        for i in range(R):
            r = one(1000 * ci + i, path, t if tighten_on else None, enqueue_only)
            ROWS.append({"arm": arm_name, "combo": "%s/%s" % (path, t), "i": i, "delivered": r["delivered"], "refused": r["refused"], "escaped": r["escaped_payloads"],
                         "enqueue_allow": bool(r["enqueue_allow"]), "enqueue_version": r["enqueue_version"], "refusal_logged": bool(r["refusal_logged_with_version"])})
            out["runs"] += 1
            out["delivered"] += r["delivered"]
            out["refused"] += r["refused"]
            out["escaped"] += r["escaped_payloads"]
            out["enqueue_allowed_v1"] += r["enqueue_allow"] and r["enqueue_version"] == 1
            out["refusal_logged_v2"] += r["refusal_logged_with_version"]
            k = "%s/%s" % (path, t)
            out["by_combo"][k] = out["by_combo"].get(k, 0) + r["delivered"]
            if r["refusal_reason"]:
                out["reasons"][r["refusal_reason"]] = out["reasons"].get(r["refusal_reason"], 0) + 1
    return out


def main():
    fenced = arm(False, True, "fenced")
    positive = arm(False, False, "positive")
    control = arm(True, True, "control_enqueue_only")
    ok = (fenced["delivered"] == 0 and fenced["escaped"] == 0 and fenced["enqueue_allowed_v1"] == fenced["runs"] and fenced["refusal_logged_v2"] == fenced["runs"]
          and positive["delivered"] == positive["runs"] and positive["escaped"] == positive["runs"])
    ctl = control["delivered"] >= 0.9 * control["runs"]
    verdict = "PASS" if ok and ctl else "FAIL"
    data = {"verdict": verdict, "fenced_after_tighten": fenced, "positive_no_tighten": positive, "control_enqueue_only": control,
            "pass_conditions_met": ok, "control_meets_floor": ctl, "control_reason": "enqueue-only check trusts the stale v1 decision and delivers",
            "note": "group_removed is a directory change, not a policy version change: its refusal is logged with policy version 1 and the refusal reason recipient_acl",
            "not_proven": "refusal logged for the last-checked item only; no in-flight (already sent) item can be recalled"}
    data["items"] = dict(zip(("file", "sha256"), write_items("e4_tighten", ROWS)))
    write_result("e4_tighten", {"R": R, "combos": COMBOS}, data)
    print(verdict, "fenced delivered", fenced["delivered"], "of", fenced["runs"], "| positive delivered", positive["delivered"], "| control delivered", control["delivered"], "of", control["runs"])


if __name__ == "__main__":
    main()

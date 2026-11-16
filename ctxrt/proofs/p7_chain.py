"""P7 integration smoke: analyst finding -> inbox with recorded origin -> policy-checked turn commit -> trimmed provider message with the injected block ->
shape validator; reviewer inbox filter on the same finding. Run: python3 -m ctxrt.proofs.p7_chain"""
from __future__ import annotations

import json
import random

from memcore.guard import EgressGuard
from memcore.inbox import InboxItem, RequirementGraph
from ctxrt import gen, msgshape as M
from ctxrt.analyst import AnalystRun, ScriptedModel
from ctxrt.decisions import DecisionLog
from ctxrt.delivery import Delivery, DeliverySink, PolicyBridge
from ctxrt.kernel import MemoryLog
from ctxrt.proofs.common import check, main
from ctxrt.proofs.p4_analyst import PLANT, make_transcript, nothing, pick_quote
from ctxrt.pulltools import CallerContext, PullTools
from ctxrt.routing import Agent, FilteredInbox, Registry
from ctxrt.rundb import Fenced, NestStore, RunDB
from ctxrt.vendor.pol.enforce import Enforcer
from ctxrt.vendor.pol.labels import Label
from ctxrt.vendor.pol.pdp import Policy, Principal

PER_CLASS = 40
CLASSES = ["valid", "nothing", "malformed", "stopped", "planted"]


def one_case(rng, cls, shape, i):
    store = NestStore(":memory:")
    sender = Principal("analyst-1", "t1", clearance=3, kind="analyst")
    rcp = Principal("user-1", "t1", clearance=2, kind="human")
    label = Label(tenants=frozenset(["t1"]), sensitivity=1)
    bridge = PolicyBridge(Enforcer(lambda: Policy(1, grants={"t1": frozenset(["send_inbox"])})), lambda agent: sender, lambda lin: rcp)      # NOT PROVEN until the policy rescue
    d = Delivery(store, lease_s=0.05, policy=bridge)
    sid = d.new_lineage("S", "L")
    db = RunDB(store)
    db.open_step("step-A", 10 ** 6)
    if cls == "planted":
        log = MemoryLog(b"p7-test-secret-0123456789abcdef")
        log.append("conv-p%d" % i, "2026-02-01T00:00:00Z", "assistant", "note: " + PLANT + ". End of note.", speaker="assistant", round=1)
        ev, quote, speaker, rnd = log.events[0], PLANT, "assistant", 1
    else:
        log, _ = make_transcript(rng)
        ev = rng.choice(log.events)
        quote, speaker, rnd = pick_quote(rng, ev["text"]), ev["speaker"], ev["round"]
    pt = PullTools(log, DecisionLog(), b"p7-test-secret-0123456789abcdef")
    guard = EgressGuard(real_ids=["conv-never-used"], require_real_ids=True)
    readable = frozenset(e["session"] for e in log.events)
    surf = pt.surface(lambda: CallerContext("t0", "user-1", readable), guard)
    tools = {"search": lambda a: surf.call_tool("search", {"query": a["query"], "limit": 3})}
    finding = {"candidate_id": "c1", "event_id": ev["oid"], "round": rnd, "speaker": speaker, "quote": quote, "claim": PLANT if cls != "planted" else "claim"}
    finals = {"valid": json.dumps({"schema_version": 1, "outcome": "findings", "dead_ends": [], "findings": [finding]}),
              "planted": json.dumps({"schema_version": 1, "outcome": "findings", "dead_ends": [], "findings": [finding]}),
              "nothing": nothing(), "malformed": "I think the answer is in round %d." % rnd, "stopped": nothing()}
    r = db.launch("S", "step-A", "q-%d" % i, 1, "v1", 6, "o")
    model = ScriptedModel([{"type": "tool", "name": "search", "args": {"query": quote}}, {"type": "final", "text": finals[cls]}])
    model.model_id = "m-analyst"
    sink = DeliverySink(d, "S", label.to_dict())
    run = AnalystRun(db, r["key"], r["epoch"], model, tools, "q-%d" % i, "S", log, sink, author=("agent-A", "m-analyst"), readable_sessions=readable)
    if cls == "stopped":
        run.step()
        db.stop_run(r["key"], "chain test")
        try:
            run.step()
        except Fenced:
            pass
    else:
        run.run()
    out = d.run_turn("L", "T1")
    block = out["block"]
    items = out["finding_ids"]
    conv = gen.conversation(rng, shape)
    total = M.total_tokens(conv)
    budget = max(40, int(rng.uniform(0.5, 1.0) * total))
    reserve = M.est_tokens({"role": "user", "content": block}) if block else 0
    tr = M.trim(shape, conv, budget, reserve)
    final = M.inject(shape, tr.messages, block)
    base = M.trim(shape, conv, budget, 0).messages
    naive = M.naive_inject(shape, tr.messages, block)
    ends_tool = (conv[-1]["role"] == "tool") if shape == "openai" else (isinstance(conv[-1]["content"], list) and any(b["type"] == "tool_result" for b in conv[-1]["content"]))
    js = json.dumps(final, ensure_ascii=False)
    row = {"i": i, "cls": cls, "shape": shape, "block_len": len(block), "n_items": len(items), "valid_shape": not M.validate(shape, final), "naive_valid": not M.validate(shape, naive),
           "ends_tool": ends_tool, "identical_to_base": final == base, "raw": {"conv": conv, "final": final, "base": base, "block": block, "quote": quote, "items": items}}
    if items:
        span = log.by_oid(ev["oid"])["text"]
        row["quote_once"] = js.count(quote) == 1 if cls != "planted" else js.count(PLANT) == 1
        row["id_once"] = js.count(items[0]) == 1
        row["origin_model"] = (db.origins_of(sid, items[0]) or [{}])[0].get("model")
        row["claim_leak"] = "IGNORE ALL" in js and cls == "valid"
        if cls == "planted":
            lines = block.split("\n")
            idx = [k for k, l in enumerate(lines) if PLANT in l]
            row["planted_framed"] = len(idx) == 1 and lines[idx[0]].startswith("{") and lines[0].startswith("<<<") and lines[-1].startswith("<<<")
        # reviewer sessions on the same store: one in the author set, one not
        reg = Registry([Agent("agent-A", "m-analyst", "LA"), Agent("agent-A2", "m-analyst", "LA"), Agent("rev-B", "m-other", "LB")])
        fi = FilteredInbox(store, RequirementGraph({"task": {"req1"}}), lambda: __import__("time").time(), registry=reg, origins_of=db.origins_of,
                           session_agent={"s-A2": "agent-A2", "s-B": "rev-B"})
        for sname in ("s-A2", "s-B"):
            fi.register(sname, "host", "task", ["req1"])
            it = store.get_item(sid, items[0])
            fi.enqueue(InboxItem(it.finding_id, sname, it.source_event_ids, it.quote, it.trust_origin, it.item_ids, it.created, it.expires))
            with store.transaction():
                db.record_origin(sname, it.finding_id, "agent-A", "m-analyst", r["key"], 1)
        row["author_set_claims"] = len(fi.claim("s-A2", "r1"))
        row["reviewer_claims"] = len(fi.claim("s-B", "r2"))
    return row


def run_seed(seed, params):
    rng = random.Random(seed)
    rows = []
    for cls in CLASSES:
        for k in range(PER_CLASS):
            rows.append(one_case(rng, cls, rng.choice(["anthropic", "openai"]), len(rows)))
    return rows, {"rows": rows}


def evaluate(m, params):
    rows = m["rows"]
    by = {c: [r for r in rows if r["cls"] == c] for c in CLASSES}
    pos = by["valid"] + by["planted"]
    neg = by["nothing"] + by["malformed"] + by["stopped"]
    tool_tail = [r for r in pos if r["ends_tool"]]
    return [
        check("witness: >=40 cases per class", {c: len(v) for c, v in by.items()}, all(len(v) >= PER_CLASS for v in by.values())),
        check("valid and planted findings deliver exactly one item", [sum(r["n_items"] == 1 for r in pos), len(pos)], all(r["n_items"] == 1 for r in pos)),
        check("provider-valid conversation after the chain (0 invalid, both shapes)", [sum(not r["valid_shape"] for r in rows), len(rows)], all(r["valid_shape"] for r in rows)),
        check("verified quote and finding id each appear exactly once", [sum(r.get("quote_once", False) and r.get("id_once", False) for r in pos), len(pos)], all(r.get("quote_once") and r.get("id_once") for r in pos)),
        check("nothing_relevant, malformed and stopped: nothing injected, byte-identical to the un-injected conversation", [sum(r["block_len"] == 0 and r["identical_to_base"] for r in neg), len(neg)], all(r["block_len"] == 0 and r["identical_to_base"] for r in neg)),
        check("analyst claim never reaches the provider message", sum(r.get("claim_leak", False) for r in pos), not any(r.get("claim_leak") for r in pos)),
        check("planted instruction in a verified quote stays on one framed data line", [sum(r.get("planted_framed", False) for r in by["planted"]), len(by["planted"])], all(r.get("planted_framed") for r in by["planted"])),
        check("origin model recorded on every delivered finding", [sum(r.get("origin_model") == "m-analyst" for r in pos), len(pos)], all(r.get("origin_model") == "m-analyst" for r in pos)),
        check("reviewer inbox: author-set session receives 0, non-author session receives the item (canary)", [sum(r.get("author_set_claims", 0) for r in pos), sum(r.get("reviewer_claims", 0) for r in pos), len(pos)],
              sum(r.get("author_set_claims", 0) for r in pos) == 0 and all(r.get("reviewer_claims") == 1 for r in pos)),
        check("control: naive injector gives invalid conversations >=10% of tool-tail cases", [sum(not r["naive_valid"] for r in tool_tail), len(tool_tail)], len(tool_tail) > 0 and sum(not r["naive_valid"] for r in tool_tail) / len(tool_tail) >= 0.10),
    ]


if __name__ == "__main__":
    main("p7_chain", run_seed, evaluate, {"per_class": PER_CLASS}, claim="integration: analyst finding through inbox and turn commit into a provider-valid injected message")

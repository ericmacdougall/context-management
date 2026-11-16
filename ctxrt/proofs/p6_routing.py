"""P6: reviewer routing. Run: python3 -m ctxrt.proofs.p6_routing"""
from __future__ import annotations

import random

from memcore.inbox import InboxItem, InboxService, RequirementGraph
from ctxrt.rundb import NestStore, RunDB
from ctxrt.proofs.common import check, main
from ctxrt.routing import Agent, FilteredInbox, RecordStore, Registry, RoutingRefused, route_by_availability

N = 2000
MODELS = ["m-a", "m-b", "m-c"]


def make_registry(rng):
    n = rng.randint(5, 12)
    agents = []
    for i in range(n):
        lineage = "L%d" % (rng.randrange(max(2, n // 2)) if rng.random() < 0.5 else i)
        parent = rng.choice(agents).agent_id if agents and rng.random() < 0.4 else None
        agents.append(Agent("a%d" % i, rng.choice(MODELS), lineage, parent, rng.random() < 0.85, rng.choice(["worker", "reviewer", "click_tester", "worker"])))
    return Registry(agents)


def brute_author_set(reg, author):
    """Independent fixpoint: relation 'linked' = same lineage OR delegated_by; closure over the DOWNWARD direction only (ancestors excluded)."""
    ids = list(reg.agents)
    lin = reg.agents[author].lineage
    s = {author} | {i for i in ids if reg.agents[i].lineage == lin}
    while True:
        add = {i for i in ids if i not in s and (reg.agents[i].delegated_by in s or reg.agents[i].lineage in {reg.agents[j].lineage for j in s})}
        if not add:
            return s
        s |= add


def run_seed(seed, params):
    rng = random.Random(seed)
    c = dict(cases=0, set_mismatch=0, route_in_author_set=0, route_same_model=0, route_refused=0, refusal_correct=0, route_assigned=0, only_author_pool=0, only_author_refused=0,
             approve_checks=0, approve_wrong=0, approve_author_refused=0, create_refused=0, create_attempts=0, records=0, records_with_model=0,
             ctrl_assigned_author=0, ctrl_cases=0, ctrl_only_author_assigned=0, inbox_author_claims=0, inbox_nonauthor_claims=0, inbox_nonauthor_expected=0,
             ctrl_inbox_author_claims=0, inbox_cases=0)
    rows = []
    for k in range(N):
        reg = make_registry(rng)
        store = RecordStore(reg)
        author = rng.choice(list(reg.agents))
        # creation without author/model refused
        for bad in [(None, "m-a"), ("a0", None), ("", "m-a"), ("nobody", "m-a"), (author, "wrong-model")]:
            c["create_attempts"] += 1
            try:
                store.create("finding", bad[0], bad[1], "x")
            except RoutingRefused:
                c["create_refused"] += 1
        rec = store.create(rng.choice(["finding", "proposal"]), author, reg.agents[author].model_id, "body")
        c["cases"] += 1
        c["records"] += 1
        c["records_with_model"] += bool(rec["author_model"] and rec["author_id"])
        aset = reg.author_set(author)
        bset = brute_author_set(reg, author)
        c["set_mismatch"] += aset != bset
        only_author = rng.random() < 0.15
        pool = sorted(aset) if only_author else rng.sample(list(reg.agents), rng.randint(1, len(reg.agents)))
        if rng.random() < 0.5 and not only_author:
            pool = sorted(set(pool) | {author})
        c["only_author_pool"] += only_author
        # independent expectation
        exp = sorted(i for i in pool if i not in bset and reg.agents[i].available and reg.agents[i].role in ("worker", "reviewer", "click_tester") and reg.agents[i].model_id != reg.agents[author].model_id)
        try:
            rv = store.route_review(rec["rid"], pool)
            c["route_assigned"] += 1
            c["route_in_author_set"] += rv in bset
            c["route_same_model"] += reg.agents[rv].model_id == reg.agents[author].model_id
            c["refusal_correct"] += bool(exp) and rv == exp[0]
        except RoutingRefused as e:
            c["route_refused"] += 1
            c["refusal_correct"] += (not exp) and e.code == "no_eligible_reviewer"
            c["only_author_refused"] += only_author
        # approvals
        for _ in range(3):
            ap = rng.choice(list(reg.agents))
            c["approve_checks"] += 1
            try:
                store2 = RecordStore(reg)
                r2 = store2.create("finding", author, reg.agents[author].model_id, "b")
                store2.approve(r2["rid"], ap)
                c["approve_wrong"] += ap in bset
            except RoutingRefused as e:
                c["approve_author_refused"] += 1
                c["approve_wrong"] += not (ap in bset and e.code == "author_cannot_approve")
        # control: availability-only routing
        pc = route_by_availability(reg, rec, pool)
        if pc is not None:
            c["ctrl_cases"] += 1
            c["ctrl_assigned_author"] += pc in bset
            c["ctrl_only_author_assigned"] += only_author
        # inbox filter: reviewer sessions
        if k % 4 == 0:
            c["inbox_cases"] += 1
            sess_agent = {}
            graph = RequirementGraph({"task": {"req1"}})
            now = [1000.0]
            ostore = NestStore(":memory:")
            odb = RunDB(ostore)
            real = FilteredInbox(ostore, graph, lambda: now[0], registry=reg, origin_of=lambda f: (odb.origin_of(f) or {}).get("agent"), session_agent=sess_agent)
            plain = InboxService(NestStore(":memory:"), graph, lambda: now[0])
            nonauthors = [i for i in reg.agents if i not in bset]
            targets = sorted(bset)[:3] + nonauthors[:2]
            for svc in (real, plain):
                for a in targets:
                    svc.register("s-" + a, "host", "task", ["req1"])
                    sess_agent["s-" + a] = a
                    svc.enqueue(InboxItem("f-%d" % k, "s-" + a, ["ev1"], "quote", "agent", ["req1"], 1000.0, 5000.0))
            with ostore.transaction():
                odb.record_origin("f-%d" % k, author, reg.agents[author].model_id, "run-%d" % k, 1)
            for a in targets:
                got = real.claim("s-" + a, "t1")
                g2 = plain.claim("s-" + a, "t1")
                if a in bset:
                    c["inbox_author_claims"] += len(got)
                    c["ctrl_inbox_author_claims"] += len(g2) > 0
                else:
                    c["inbox_nonauthor_expected"] += 1
                    c["inbox_nonauthor_claims"] += len(got) == 1
        rows.append({"k": k, "author": author, "author_model": rec["author_model"], "author_set": sorted(aset), "brute_set": sorted(bset), "pool": pool, "assigned": rec["reviewer"],
                     "expected_first": exp[:1], "ctrl_assigned": pc, "only_author_pool": only_author})
    rows.append({"counts": c})
    return rows, c


def evaluate(c, params):
    return [
        check("0 routings to the author set", [c["route_in_author_set"], c["route_assigned"]], c["route_in_author_set"] == 0 and c["route_assigned"] > 0),
        check("different-model rule held on every assignment", c["route_same_model"], c["route_same_model"] == 0),
        check("routing equals independent expectation or typed refusal (100%)", [c["refusal_correct"], c["cases"]], c["refusal_correct"] == c["cases"]),
        check("author-set closure equals independent fixpoint", c["set_mismatch"], c["set_mismatch"] == 0),
        check("author-only pools: typed refusal, never fallback", [c["only_author_refused"], c["only_author_pool"]], c["only_author_pool"] > 0 and c["only_author_refused"] == c["only_author_pool"]),
        check("approvals: 0 by author set, all others allowed", [c["approve_wrong"], c["approve_checks"]], c["approve_wrong"] == 0),
        check("creation without author id/model refused 100%", [c["create_refused"], c["create_attempts"]], c["create_refused"] == c["create_attempts"]),
        check("author model on 100% of records", [c["records_with_model"], c["records"]], c["records_with_model"] == c["records"]),
        check("inbox: 0 author-origin claims by author-set sessions", c["inbox_author_claims"], c["inbox_author_claims"] == 0),
        check("canary: non-author sessions DO receive the item", [c["inbox_nonauthor_claims"], c["inbox_nonauthor_expected"]], c["inbox_nonauthor_expected"] > 0 and c["inbox_nonauthor_claims"] == c["inbox_nonauthor_expected"]),
        check("control: availability-only routing assigns author set >=10%", [c["ctrl_assigned_author"], c["ctrl_cases"]], c["ctrl_assigned_author"] / max(1, c["ctrl_cases"]) >= 0.10),
        check("control: unfiltered inbox delivers author-origin items >=10%", [c["ctrl_inbox_author_claims"], c["inbox_cases"]], c["ctrl_inbox_author_claims"] / max(1, c["inbox_cases"]) >= 0.10),
    ]


if __name__ == "__main__":
    main("p6_routing", run_seed, evaluate, {"N": N}, claim="author never routed to review or approve; model recorded; inbox origin filter")

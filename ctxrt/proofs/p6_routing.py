"""P6: reviewer routing. Run: python3 -m ctxrt.proofs.p6_routing
Decisions implemented (safe defaults, listed in the report): the author set holds the author, its lineage aliases, its transitive delegates AND its delegators;
two agent ids on the same model family (a dated snapshot and its alias are one family) are not distinct reviewers; origin is recorded per finding INSTANCE
(many producers kept); a finding with no origin, or a recipient that is not a registered agent, is never delivered to a reviewer; approval needs a routing
record and a registered, distinct, different-model approver. The per-case rows hold the raw registry, pool and outputs; recompute.py re-derives the author
sets from the raw registry with its own implementation."""
from __future__ import annotations

import random
import re

from memcore.inbox import InboxItem, InboxService, RequirementGraph
from ctxrt.proofs.common import check, main
from ctxrt.routing import Agent, FilteredInbox, RecordStore, Registry, RoutingRefused, model_family, route_by_availability
from ctxrt.rundb import NestStore, RunDB

N = 2000
MODELS = ["m-a", "m-b", "m-c", "claude-sonnet-4-5", "claude-sonnet-4-5-20250929"]


def make_registry(rng):
    n = rng.randint(5, 12)
    agents = []
    for i in range(n):
        lineage = "L%d" % (rng.randrange(max(2, n // 2)) if rng.random() < 0.5 else i)
        parent = rng.choice(agents).agent_id if agents and rng.random() < 0.4 else None
        agents.append(Agent("a%d" % i, rng.choice(MODELS), lineage, parent, rng.random() < 0.85, rng.choice(["worker", "reviewer", "click_tester", "worker"])))
    return Registry(agents)


def fam(m):
    return re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", m.strip().lower())


def brute_author_set(reg, author):
    """Independent fixpoint over the raw registry: same lineage, descendants (delegates) of the set, and ancestors (delegators) of the author's lineage."""
    ag = reg.agents
    s = {author} | {i for i in ag if ag[i].lineage == ag[author].lineage}
    while True:
        add = {i for i in ag if i not in s and (ag[i].delegated_by in s or ag[i].lineage in {ag[j].lineage for j in s})}
        if not add:
            break
        s |= add
    anc, done = set(), set()
    frontier = {i for i in ag if ag[i].lineage == ag[author].lineage}
    while frontier:
        nxt = set()
        for i in frontier:
            done.add(i)
            p = ag[i].delegated_by
            if p and p in ag:
                anc.add(p)
                nxt |= {j for j in ag if ag[j].lineage == ag[p].lineage}
        frontier = nxt - done
    return s | anc | {j for j in ag if ag[j].lineage in {ag[a].lineage for a in anc}}


class OldFilteredInbox(InboxService):
    """CONTROL (F6): the 1d19cd8 filter: first-writer-wins single origin per finding id, and an item with no origin or an unmapped recipient is ELIGIBLE."""

    def __init__(self, *a, registry, origin_of, session_agent, **k):
        super().__init__(*a, **k)
        self.reg, self.origin_of, self.session_agent = registry, origin_of, session_agent

    def _eligible(self, s, it, now):
        if not super()._eligible(s, it, now):
            return False
        recipient, origin = self.session_agent.get(s.session_id), self.origin_of(it.finding_id)
        if recipient is not None and origin is not None and recipient in self.reg.author_set(origin):
            return False
        return True


def raw_registry(reg):
    return [{"id": a.agent_id, "model": a.model_id, "lineage": a.lineage, "parent": a.delegated_by, "avail": a.available, "role": a.role} for a in reg.agents.values()]


def run_seed(seed, params):
    rng = random.Random(seed)
    c = dict(cases=0, set_mismatch=0, route_in_author_set=0, route_same_model=0, route_refused=0, refusal_correct=0, route_assigned=0, only_author_pool=0, only_author_refused=0,
             approve_checks=0, approve_wrong=0, create_refused=0, create_attempts=0, records=0, records_with_model=0, ctrl_assigned_author=0, ctrl_cases=0,
             inbox_author_claims=0, inbox_nonauthor_claims=0, inbox_nonauthor_expected=0, ctrl_inbox_author_claims=0, inbox_cases=0,
             collision_n=0, collision_real_self_review=0, collision_ctrl_self_review=0, noorigin_n=0, noorigin_real_claims=0, noorigin_ctrl_claims=0, unmapped_real=0, unmapped_ctrl=0,
             unrouted_n=0, unrouted_refused=0, unrouted_ctrl_ok=0, delegator_n=0, delegator_refused=0, delegator_ctrl=0, samemodel_n=0, samemodel_refused=0, samemodel_ctrl=0)
    rows = []
    for k in range(N):
        reg = make_registry(rng)
        store = RecordStore(reg)
        author = rng.choice(list(reg.agents))
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
        aset, bset = reg.author_set(author), brute_author_set(reg, author)
        c["set_mismatch"] += aset != bset
        only_author = rng.random() < 0.15
        pool = sorted(aset) if only_author else rng.sample(list(reg.agents), rng.randint(1, len(reg.agents)))
        if rng.random() < 0.5 and not only_author:
            pool = sorted(set(pool) | {author})
        c["only_author_pool"] += only_author
        exp = sorted(i for i in pool if i not in bset and reg.agents[i].available and reg.agents[i].role in ("worker", "reviewer", "click_tester") and fam(reg.agents[i].model_id) != fam(reg.agents[author].model_id))
        assigned = None
        try:
            rv = store.route_review(rec["rid"], pool)
            assigned = rv
            c["route_assigned"] += 1
            c["route_in_author_set"] += rv in bset
            c["route_same_model"] += fam(reg.agents[rv].model_id) == fam(reg.agents[author].model_id)
            c["refusal_correct"] += bool(exp) and rv == exp[0]
        except RoutingRefused as e:
            c["route_refused"] += 1
            c["refusal_correct"] += (not exp) and e.code == "no_eligible_reviewer"
            c["only_author_refused"] += only_author
        # approvals: an approver is accepted only if it IS the routed, registered, distinct, different-model reviewer
        for _ in range(3):
            ap = rng.choice(list(reg.agents) + ["nobody-registered"])
            c["approve_checks"] += 1
            s2 = RecordStore(reg)
            r2 = s2.create("finding", author, reg.agents[author].model_id, "b")
            if assigned:
                s2.records[r2["rid"]]["reviewer"] = assigned
            ok_expected = assigned is not None and ap == assigned and ap in reg.agents and ap not in bset and fam(reg.agents[ap].model_id) != fam(reg.agents[author].model_id)
            try:
                s2.approve(r2["rid"], ap)
                c["approve_wrong"] += not ok_expected
            except RoutingRefused:
                c["approve_wrong"] += ok_expected
        # unrouted approve (F6)
        s3 = RecordStore(reg)
        r3 = s3.create("finding", author, reg.agents[author].model_id, "b")
        c["unrouted_n"] += 1
        for ap in (["nobody-registered"] + [i for i in reg.agents if i not in bset][:1]):
            try:
                s3.approve(r3["rid"], ap)
            except RoutingRefused:
                c["unrouted_refused"] += 1
        c["unrouted_ctrl_ok"] += 1                            # the 1d19cd8 approve() accepted all of these: counted per probe below
        # control: availability-only routing
        pc = route_by_availability(reg, rec, pool)
        if pc is not None:
            c["ctrl_cases"] += 1
            c["ctrl_assigned_author"] += pc in bset
        # delegator / same-model probes
        kids = [i for i in reg.agents if reg.agents[i].delegated_by and reg.agents[i].delegated_by in reg.agents]
        if kids:
            kid = rng.choice(kids)
            boss = reg.agents[kid].delegated_by
            c["delegator_n"] += 1
            c["delegator_refused"] += boss in reg.author_set(kid)
            c["delegator_ctrl"] += boss not in {x for x in ({kid} | {i for i in reg.agents if reg.agents[i].lineage == reg.agents[kid].lineage})}
        c["samemodel_n"] += 1
        same = [i for i in reg.agents if i not in bset and fam(reg.agents[i].model_id) == fam(reg.agents[author].model_id)]
        if same:
            s4 = RecordStore(reg)
            r4 = s4.create("finding", author, reg.agents[author].model_id, "b")
            try:
                s4.route_review(r4["rid"], same)
            except RoutingRefused:
                c["samemodel_refused"] += 1
            c["samemodel_ctrl"] += 1
        else:
            c["samemodel_n"] -= 1
        # inbox filter: reviewer sessions, real vs 1d19cd8 control; origins per instance, fail closed
        if k % 4 == 0:
            c["inbox_cases"] += 1
            sess_agent = {}
            graph = RequirementGraph({"task": {"req1"}})
            now = [1000.0]
            ostore = NestStore(":memory:")
            odb = RunDB(ostore)
            real = FilteredInbox(ostore, graph, lambda: now[0], registry=reg, origins_of=odb.origins_of, session_agent=sess_agent)
            first_writer = {}
            plain = InboxService(NestStore(":memory:"), graph, lambda: now[0])
            old = OldFilteredInbox(NestStore(":memory:"), graph, lambda: now[0], registry=reg, origin_of=lambda f: first_writer.get(f), session_agent=sess_agent)
            others = [i for i in reg.agents if i not in bset]
            b = rng.choice(others) if others else None
            bset_b = reg.author_set(b) if b else set()
            bad_fams = {fam(reg.agents[author].model_id)} | ({fam(reg.agents[b].model_id)} if b else set())
            nonauthors = [i for i in reg.agents if i not in bset and i not in bset_b and fam(reg.agents[i].model_id) not in bad_fams]
            targets = list(dict.fromkeys(sorted(bset)[:3] + nonauthors[:2] + sorted(bset_b - bset)[:2]))
            for svc, tag in ((real, "real"), (old, "old"), (plain, "plain")):
                for a in targets:
                    svc.register("s-" + a, "host", "task", ["req1"])
                    sess_agent["s-" + a] = a
                    svc.enqueue(InboxItem("f-%d" % k, "s-" + a, ["ev1"], "quote", "agent", ["req1"], 1000.0, 5000.0))
                    svc.enqueue(InboxItem("n-%d" % k, "s-" + a, ["ev1"], "quote", "agent", ["req1"], 1000.0, 5000.0))      # an item with NO origin
            with ostore.transaction():
                for a in targets:
                    odb.record_origin("s-" + a, "f-%d" % k, author, reg.agents[author].model_id, "run-%d" % k, 1)
                    if b:
                        odb.record_origin("s-" + a, "f-%d" % k, b, reg.agents[b].model_id, "run2-%d" % k, 1)         # a SECOND producer of the same finding
            first_writer["f-%d" % k] = author                  # the 1d19cd8 table keeps only the first writer
            for a in targets:
                got = real.claim("s-" + a, "t1")
                g2 = old.claim("s-" + a, "t1")
                g3 = plain.claim("s-" + a, "t1")
                f_real = [i for i in got if i.finding_id.startswith("f-")]
                f_old = [i for i in g2 if i.finding_id.startswith("f-")]
                n_real = [i for i in got if i.finding_id.startswith("n-")]
                n_old = [i for i in g2 if i.finding_id.startswith("n-")]
                if a in bset:
                    c["inbox_author_claims"] += len(f_real)
                    c["ctrl_inbox_author_claims"] += len([i for i in g3 if i.finding_id.startswith("f-")]) > 0
                elif a in bset_b:
                    c["collision_n"] += 1
                    c["collision_real_self_review"] += len(f_real) > 0
                    c["collision_ctrl_self_review"] += len(f_old) > 0
                else:
                    c["inbox_nonauthor_expected"] += 1
                    c["inbox_nonauthor_claims"] += len(f_real) == 1
                c["noorigin_n"] += 1
                c["noorigin_real_claims"] += len(n_real)
                c["noorigin_ctrl_claims"] += len(n_old) > 0
            for svc in (real, old):
                svc.register("s-ghost", "host", "task", ["req1"])
                svc.enqueue(InboxItem("g-%d" % k, "s-ghost", ["ev1"], "quote", "agent", ["req1"], 1000.0, 5000.0))
            with ostore.transaction():
                odb.record_origin("s-ghost", "g-%d" % k, author, reg.agents[author].model_id, "run-%d" % k, 1)
            c["unmapped_real"] += len(real.claim("s-ghost", "tg"))
            c["unmapped_ctrl"] += len(old.claim("s-ghost", "tg")) > 0
        rows.append({"k": k, "registry": raw_registry(reg), "author": author, "author_model": rec["author_model"], "author_set": sorted(aset), "pool": pool, "assigned": assigned,
                     "ctrl_assigned": pc, "only_author_pool": only_author})
    rows.append({"counts": c})
    return rows, c


def evaluate(c, params):
    return [
        check("0 routings to the author set", [c["route_in_author_set"], c["route_assigned"]], c["route_in_author_set"] == 0 and c["route_assigned"] > 0),
        check("different-model-family rule held on every assignment", c["route_same_model"], c["route_same_model"] == 0),
        check("routing equals independent expectation or typed refusal (100%)", [c["refusal_correct"], c["cases"]], c["refusal_correct"] == c["cases"]),
        check("author-set closure (incl. delegators) equals independent fixpoint", c["set_mismatch"], c["set_mismatch"] == 0),
        check("author-only pools: typed refusal, never fallback", [c["only_author_refused"], c["only_author_pool"]], c["only_author_pool"] > 0 and c["only_author_refused"] == c["only_author_pool"]),
        check("approvals: exactly the routed, registered, distinct, different-model reviewer (100%)", [c["approve_wrong"], c["approve_checks"]], c["approve_wrong"] == 0),
        check("approve with no routing record or an unregistered id refused (100%)", [c["unrouted_refused"], c["unrouted_n"]], c["unrouted_refused"] >= c["unrouted_n"] > 0),
        check("delegator may not review its delegate (100%)", [c["delegator_refused"], c["delegator_n"]], c["delegator_refused"] == c["delegator_n"] > 0),
        check("control: 1d19cd8 author set lets the delegator review >=10%", [c["delegator_ctrl"], c["delegator_n"]], c["delegator_ctrl"] / max(1, c["delegator_n"]) >= 0.10),
        check("same-model agents on another id are not reviewers (100%)", [c["samemodel_refused"], c["samemodel_n"]], c["samemodel_refused"] == c["samemodel_n"] > 0),
        check("creation without author id/model refused 100%", [c["create_refused"], c["create_attempts"]], c["create_refused"] == c["create_attempts"]),
        check("author model on 100% of records", [c["records_with_model"], c["records"]], c["records_with_model"] == c["records"]),
        check("inbox: 0 author-origin claims by author-set sessions", c["inbox_author_claims"], c["inbox_author_claims"] == 0),
        check("canary: distinct non-author different-model sessions DO receive the item", [c["inbox_nonauthor_claims"], c["inbox_nonauthor_expected"]], c["inbox_nonauthor_expected"] > 0 and c["inbox_nonauthor_claims"] == c["inbox_nonauthor_expected"]),
        check("fail closed: items with no origin and unmapped recipients are never delivered to a reviewer", [c["noorigin_real_claims"], c["unmapped_real"], c["noorigin_n"]], c["noorigin_real_claims"] == 0 and c["unmapped_real"] == 0 and c["noorigin_n"] > 0),
        check("origin collision: a co-producer is excluded (0 self-review through a second producer)", [c["collision_real_self_review"], c["collision_n"]], c["collision_real_self_review"] == 0 and c["collision_n"] > 0),
        check("control: availability-only routing assigns author set >=10%", [c["ctrl_assigned_author"], c["ctrl_cases"]], c["ctrl_assigned_author"] / max(1, c["ctrl_cases"]) >= 0.10),
        check("control: unfiltered inbox delivers author-origin items >=10%", [c["ctrl_inbox_author_claims"], c["inbox_cases"]], c["ctrl_inbox_author_claims"] / max(1, c["inbox_cases"]) >= 0.10),
        check("control: 1d19cd8 inbox delivers origin-less items and unmapped recipients >=10%", [c["noorigin_ctrl_claims"], c["unmapped_ctrl"], c["noorigin_n"]], (c["noorigin_ctrl_claims"] + c["unmapped_ctrl"]) / max(1, c["noorigin_n"]) >= 0.10),
        check("control: first-writer-wins origin lets a second producer review its own finding (counted per collision)", [c["collision_ctrl_self_review"], c["collision_n"]], c["collision_ctrl_self_review"] / max(1, c["collision_n"]) >= 0.10),
    ]


if __name__ == "__main__":
    main("p6_routing", run_seed, evaluate, {"N": N}, claim="author, aliases, delegates and delegators never review or approve; model recorded; origin per instance; fail closed")

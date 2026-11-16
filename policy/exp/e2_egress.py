"""E2: egress canary. Marker strings planted in records of each label class, every outbound path driven through recording stubs
under random scenarios; an independent oracle scans what each stub recorded. Controls disable enforcement at one choke point,
drop the derivation join, or replace the decision point with a canary-blind id-pattern guard.
usage: python -m exp.e2_egress"""
import os
import random
import re
import sys

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.gen import base_policy
from pol.enforce import Block, Enforcer, GuardedChat, GuardedEmbedder, GuardedExport, Outbox, PolicyDenied, Record, Store, derive, inject
from pol.labels import Label
from pol.pdp import ACTIONS, Decision, Principal
from pol.stubs import Network, StubChat, StubEmbedder

ROWS = []
SEEDS = [21, 22, 23, 24, 25]
if os.environ.get("E2_DEV_SEEDS"):      # DEV tuning only (never for a stamped run)
    SEEDS = [1, 2, 3, 4, 5]
PER_SEED = 400
CLASSES = ["eu_only", "local_only", "model_banned", "restricted_channel", "other_tenant", "plain"]
MARKER_RE = re.compile(r"@@([a-z_]+):([0-9a-f]{8})@@")

P1 = Principal("P1", "t1", frozenset(["g1", "g2", "g3", "g4"]), clearance=3, restricted_ok=True, region="EU")
RECIPIENTS = {
    "r1": Principal("r1", "t1", frozenset(["g1"]), 3, True, region="EU", model="d_model_eu"),
    "r2": Principal("r2", "t1", frozenset(["g1"]), 3, True, region="EU", model="d_model_us"),
    "r3": Principal("r3", "t1", frozenset(["g1"]), 3, True, region="US", model="d_model_eu"),
    "r4": Principal("r4", "t1", frozenset(["g1"]), 3, False, region="EU", model=None),
    "r5": Principal("r5", "t1", frozenset(["g1"]), 3, False, region="US", model=None),
    "r6": Principal("r6", "t1", frozenset(["g1"]), 3, True, region="US", model="d_model_cyber"),
}


def label_for(cls):
    if cls == "eu_only":
        return Label(["t1"], regions=["EU"])
    if cls == "local_only":
        return Label(["t1"], local_only=True)
    if cls == "model_banned":
        return Label(["t1"], banned=["m3"])
    if cls == "restricted_channel":
        return Label(["t1"], restricted=True, sensitivity=2)
    if cls == "other_tenant":
        return Label(["t2"])
    return Label(["t1"])


def oracle_forbidden(cls, ch, dest, tenant, pol):
    """Independent of pdp.decide: forbidden-ness of a marker class at a recorded destination."""
    if cls == "plain":
        return False
    if cls == "other_tenant":
        return tenant != "t2"
    if ch == "inbox":
        r = RECIPIENTS[dest[4:]]
        md = pol.destinations.get(r.model) if r.model else None
        if cls == "eu_only":
            return r.region != "EU" or (md is not None and md.region != "EU")
        if cls == "local_only":
            return md is not None and not md.local
        if cls == "model_banned":
            return md is not None and md.model == "m3"
        if cls == "restricted_channel":
            return not (r.restricted_ok and (md is None or md.programme == "cyber"))
    d = pol.destinations[dest]
    if cls == "eu_only":
        return d.region != "EU"
    if cls == "local_only":
        return not d.local
    if cls == "model_banned":
        return d.model == "m3"
    if cls == "restricted_channel":
        return d.programme != "cyber"
    raise AssertionError(cls)


class IdPatternGuard(Enforcer):
    """Control (f): a canary-blind guard that only refuses text that looks like a real session id."""
    PAT = re.compile(r"answer_[0-9a-f]{4,}|sharegpt_\w+")

    blocked = 0

    def check(self, principal, action, labels, ctx=None, texts=(), resolve=None):
        self.evaluated_actions.add(action)
        if any(self.PAT.search(t) for t in texts):  # scans the text for id-shaped strings only
            IdPatternGuard.blocked += 1
            raise PolicyDenied(Decision(False, "id_pattern", None, action))
        return None


def run_scenario(seed, mode):
    rng = random.Random(seed)
    pol = base_policy()
    net = Network()
    directory = {"P1": P1, **RECIPIENTS}
    enf = Enforcer(lambda: pol, disabled=[mode[1]] if mode and mode[0] == "disable" else (), directory=directory.get, clock=lambda: 10)
    if mode and mode[0] == "idguard":
        enf = IdPatternGuard(lambda: pol, directory=directory.get, clock=lambda: 10)
    store = Store(enf, check_lineage=not (mode and mode[0] == "derive_first"))
    dmode = "first" if mode and mode[0] == "derive_first" else "join"
    for ch, act in (("model", "send_model"), ("embed", "embed"), ("inbox", "send_inbox"), ("export", "export")):
        net.register(ch, act)
    chats = {n: GuardedChat(StubChat(net, n, d.model), enf, n) for n, d in pol.destinations.items() if d.kind == "model"}
    embs = {n: GuardedEmbedder(StubEmbedder(net, n), enf, n) for n, d in pol.destinations.items() if d.kind == "embedder"}
    exps = {n: GuardedExport(lambda dest, tenant, text: net.record("export", dest, tenant, text), enf, n)
            for n, d in pol.destinations.items() if d.kind == "export"}
    directory = {"P1": P1, **RECIPIENTS}.get
    inbox = Outbox(enf, directory, "send_inbox", lambda p, blocks, ctx: [net.record("inbox", "rcp:" + ctx["recipient"].id, p.tenant, b.text) for b in blocks])
    net.current_tenant = "t1"

    # plant records: one of each class first, then random; markers are unique per record
    recs, cls_of = [], {}
    n = rng.randint(12, 16)
    for i in range(n):
        cls = CLASSES[i] if i < len(CLASSES) else rng.choice(CLASSES)
        mk = "@@%s:%08x@@" % (cls, rng.getrandbits(32))
        cls_of[mk] = cls
        idlike = " answer_%08x" % rng.getrandbits(32) if (cls == "plain" and rng.random() < 0.5) else ""
        rec = Record("rec%d" % i, "note %s about item %d%s" % (mk, i, idlike), label_for(cls))
        store.records[rec.id] = rec           # planted directly: this experiment is about egress, not append
        recs.append(rec)
    pool = list(recs)                          # grows with derived records

    st = {"attempts_forbidden": {c: 0 for c in CLASSES}, "attempts_total": 0, "denied": 0, "ops": 0}
    ctx_base = lambda: {"authorization": rng.choice(["auth1", None, "zzz"])}

    def forbidden_classes(blocks, ch, dest):
        out = set()
        for b in blocks:
            for m in MARKER_RE.finditer(b.text):
                c = cls_of.get(m.group(0))
                if c and oracle_forbidden(c, ch, dest, "t1", pol):
                    out.add(c)
        return out

    def attempt(ch, dest, blocks, fn):
        st["attempts_total"] += 1
        for c in forbidden_classes(blocks, ch, dest):
            st["attempts_forbidden"][c] += 1
        try:
            return fn()
        except PolicyDenied:
            st["denied"] += 1
            return None

    for _ in range(rng.randint(4, 10)):
        st["ops"] += 1
        op = rng.choice(["inject_send", "direct_send", "embed", "inbox", "export", "derive_send", "inject_send", "direct_send", "compliant", "compliant", "compliant", "compliant"])
        picks = rng.sample(pool, rng.randint(1, min(4, len(pool))))
        bias = rng.random()
        if bias < 0.5:                         # steer part of the traffic at the restricted and banned-model cases
            sub = [r for r in pool if "restricted_channel" in r.text or "model_banned" in r.text or "local_only" in r.text or "eu_only" in r.text]
            if sub:
                picks = rng.sample(sub, rng.randint(1, min(3, len(sub))))
        blocks = [Block(r.text, r.label, (r.id,)) for r in picks]
        c = ctx_base()
        if op == "compliant":              # route a record where the oracle says it may go (positive witness: markers do get delivered)
            options = [("model", n) for n in chats] + [("embed", n) for n in embs] + [("export", n) for n in exps] + [("inbox", "rcp:" + r) for r in RECIPIENTS]
            rs_ = [x for x in pool if "restricted_channel" in x.text]
            r = rng.choice(rs_) if (rs_ and rng.random() < 0.4) else rng.choice(pool)
            ok = [o for o in options if not any(oracle_forbidden(cls_of[m.group(0)], o[0], o[1], "t1", pol) for m in MARKER_RE.finditer(r.text) if m.group(0) in cls_of)]
            if ok:
                ch, dest = rng.choice(ok)
                b1 = [Block(r.text, r.label, (r.id,))]
                c["authorization"] = "auth1"
                if ch == "model":
                    attempt(ch, dest, b1, lambda: chats[dest].complete(P1, b1, ctx=c))
                elif ch == "embed":
                    attempt(ch, dest, b1, lambda: embs[dest].embed(P1, b1, ctx=c))
                elif ch == "export":
                    attempt(ch, dest, b1, lambda: exps[dest].export(P1, b1, ctx=c))
                else:
                    attempt(ch, dest, b1, lambda: inbox.enqueue("P1", b1, c, dest[4:]))
                    inbox.deliver_all()
        elif op == "inject_send":
            dest = rng.choice(list(chats))
            ib, _w = inject(enf, P1, picks, dict(c, destination=dest))
            res = attempt("model", dest, ib, lambda: chats[dest].complete(P1, ib, ctx=c, derive_mode=dmode))
            if res is not None and rng.random() < 0.6:
                d = Record("d%d" % len(pool), res.text, res.label, tuple(res.lineage), "summary")
                try:
                    store.append(P1, d, {})
                    pool.append(d)
                except PolicyDenied:
                    pass
        elif op == "direct_send":
            dest = rng.choice(list(chats) + ["d_model_cyber", "d_model_cyber", "d_model_us"])
            if any("model_banned" in x.text for x in picks) and rng.random() < 0.5:
                dest = "d_model_us"
            if dest == "d_model_cyber" and rng.random() < 0.8:
                c["authorization"] = "auth1"
            res = attempt("model", dest, blocks, lambda: chats[dest].complete(P1, blocks, ctx=c, derive_mode=dmode))
            if res is not None and rng.random() < 0.5:
                d = Record("d%d" % len(pool), res.text, res.label, tuple(res.lineage), "summary")
                try:
                    store.append(P1, d, {})
                    pool.append(d)
                except PolicyDenied:
                    pass
        elif op == "derive_send":
            if len(blocks) < 2:
                blocks = blocks + [Block(pool[0].text, pool[0].label, (pool[0].id,))]
            summ = derive(blocks, "digest of: " + " | ".join(b.text for b in blocks), dmode)
            path = rng.choice(["model", "embed", "export", "inbox"])
            if path == "model":
                dest = rng.choice(list(chats))
                attempt("model", dest, [summ], lambda: chats[dest].complete(P1, [summ], ctx=c))
            elif path == "embed":
                dest = rng.choice(list(embs))
                attempt("embed", dest, [summ], lambda: embs[dest].embed(P1, [summ], ctx=c))
            elif path == "export":
                dest = rng.choice(list(exps))
                attempt("export", dest, [summ], lambda: exps[dest].export(P1, [summ], ctx=c))
            else:
                rid = rng.choice(list(RECIPIENTS))
                attempt("inbox", "rcp:" + rid, [summ], lambda: inbox.enqueue("P1", [summ], c, rid))
                inbox.deliver_all()
        elif op == "embed":
            dest = "d_embed_us" if (any("model_banned" in x.text for x in picks) and rng.random() < 0.5) else rng.choice(list(embs) + ["d_embed_us"])
            attempt("embed", dest, blocks, lambda: embs[dest].embed(P1, blocks, ctx=c))
        elif op == "export":
            dest = rng.choice(list(exps))
            attempt("export", dest, blocks, lambda: exps[dest].export(P1, blocks, ctx=c))
        elif op == "inbox":
            rid = "r2" if (any("model_banned" in x.text for x in picks) and rng.random() < 0.5) else rng.choice(list(RECIPIENTS) + ["r2"])
            attempt("inbox", "rcp:" + rid, blocks, lambda: inbox.enqueue("P1", blocks, c, rid))
            inbox.deliver_all()

    viol, permitted = [], {c: 0 for c in CLASSES}
    for p in net.payloads:
        for m in set(x.group(0) for x in MARKER_RE.finditer(p.text)):
            c = cls_of.get(m)
            if c is None:
                continue
            if oracle_forbidden(c, p.channel, p.dest, p.tenant, pol):
                viol.append({"marker_class": c, "channel": p.channel, "dest": p.dest})
            else:
                permitted[c] += 1
    chan_counts = {}
    for p in net.payloads:
        chan_counts[p.channel] = chan_counts.get(p.channel, 0) + 1
    return {"violations": viol, "permitted": permitted, "attempts": st, "channels": chan_counts, "registered_actions": sorted(set(net.channels.values())),
            "evaluated_actions": sorted(enf.evaluated_actions)}


def run_arm(mode, arm_name=""):
    tot = {"scenarios": 0, "scenarios_with_leak": 0, "violations": 0, "leak_classes": {}, "leak_channels": {},
           "attempts_forbidden": {c: 0 for c in CLASSES}, "permitted": {c: 0 for c in CLASSES}, "channels": {}, "denied": 0, "attempts_total": 0,
           "registered_actions": None, "evaluated_actions": set()}
    for seed in SEEDS:
        for i in range(PER_SEED):
            r = run_scenario(seed * 100000 + i, mode)
            ROWS.append({"arm": arm_name, "seed": seed, "i": i, "violations": len(r["violations"]), "violation_classes": sorted({v["marker_class"] for v in r["violations"]}), "forbidden_attempts": r["attempts"]["attempts_forbidden"], "permitted": r["permitted"], "channels": r["channels"]})
            tot["scenarios"] += 1
            tot["violations"] += len(r["violations"])
            tot["scenarios_with_leak"] += bool(r["violations"])
            for v in r["violations"]:
                tot["leak_classes"][v["marker_class"]] = tot["leak_classes"].get(v["marker_class"], 0) + 1
                tot["leak_channels"][v["channel"]] = tot["leak_channels"].get(v["channel"], 0) + 1
            for c in CLASSES:
                tot["attempts_forbidden"][c] += r["attempts"]["attempts_forbidden"][c]
                tot["permitted"][c] += r["permitted"][c]
            for k, v in r["channels"].items():
                tot["channels"][k] = tot["channels"].get(k, 0) + v
            tot["denied"] += r["attempts"]["denied"]
            tot["attempts_total"] += r["attempts"]["attempts_total"]
            tot["registered_actions"] = r["registered_actions"]
            tot["evaluated_actions"].update(r["evaluated_actions"])
    tot["evaluated_actions"] = sorted(tot["evaluated_actions"])
    return tot


def main():
    arms = {"enforced": run_arm(None, "enforced")}
    for name, mode in [("ctl_model_port_disabled", ("disable", "send_model")), ("ctl_embed_disabled", ("disable", "embed")),
                       ("ctl_inbox_disabled", ("disable", "send_inbox")), ("ctl_export_disabled", ("disable", "export")),
                       ("ctl_derive_join_dropped", ("derive_first",)), ("ctl_id_pattern_guard", ("idguard",)),
                       ("info_inject_only_disabled", ("disable", "inject"))]:
        arms[name] = run_arm(mode, name)
    a = arms["enforced"]
    expected_actions = sorted(x for x in ACTIONS if x not in ("append", "read", "inject", "erase"))
    pos_ok = (a["violations"] == 0 and a["scenarios"] >= 2000 and all(a["attempts_forbidden"][c] >= 1000 for c in CLASSES if c != "plain")
              and all(a["permitted"][c] >= 1000 for c in CLASSES if c not in ("other_tenant",))
              and set(a["channels"]) == {"model", "embed", "inbox", "export"} and a["registered_actions"] == expected_actions
              and set(expected_actions) <= set(a["evaluated_actions"]) and "inject" in a["evaluated_actions"])
    ctl_report = {}
    for k, v in arms.items():
        if k.startswith("ctl_"):
            frac = v["scenarios_with_leak"] / v["scenarios"]
            ctl_report[k] = {"leak_rate": frac, "violations_detected": v["violations"], "leak_classes": v["leak_classes"], "leak_channels": v["leak_channels"],
                             "meets_floor": frac >= 0.10 and v["violations"] >= 1}
    ctl_ok = all(c["meets_floor"] for c in ctl_report.values()) and IdPatternGuard.blocked > 0
    verdict = "PASS" if pos_ok and ctl_ok else "FAIL"
    data = {"verdict": verdict, "arms": arms, "controls": ctl_report, "positive_conditions_met": pos_ok, "controls_meet_floor": ctl_ok,
            "defence_in_depth_inject_only_disabled": {"violations": arms["info_inject_only_disabled"]["violations"]},
            "idpattern_guard_blocks_idlike_strings": IdPatternGuard.blocked,
            "not_proven": "stubs only (no real adapter); oracle and decide share the destination table; other_tenant has no permitted delivery by design; "
                          "record content is plain strings (no encodings, translations or paraphrase of markers, which no label system can follow)"}
    data["items"] = dict(zip(("file", "sha256"), write_items("e2_egress", ROWS)))
    write_result("e2_egress", {"seeds": SEEDS, "per_seed": PER_SEED, "classes": CLASSES}, data)
    print(verdict, "enforced violations", a["violations"], "forbidden attempts", a["attempts_forbidden"], "permitted", a["permitted"], a["channels"])
    for k, c in ctl_report.items():
        print(" ", k, "leak_rate %.2f detected %d floor=%s %s" % (c["leak_rate"], c["violations_detected"], c["meets_floor"], c["leak_channels"]))
    print(" inject-only disabled violations:", arms["info_inject_only_disabled"]["violations"])


if __name__ == "__main__":
    main()

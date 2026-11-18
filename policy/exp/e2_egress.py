"""E2: egress canary. Marker strings are planted in records of each label class, and every outbound path is driven through recording
stubs (GuardedChat, GuardedEmbedder, GuardedExport, Outbox for send_inbox, send_model and export, Store.context for injection,
Store.append_derived for derived records) under random scenarios, with sessions issued by SessionAuthority. The per-item artifact
holds RAW observations: every recorded payload (channel, destination, the destination attributes the oracle needs, tenant, the
markers it contains with their classes), every attempt (path, destination attributes, marker classes of the inputs, outcome) and
every forged request (path, forge, outcome, payloads produced). verify/e2.py recomputes the verdict from these rows with its own oracle.

Controls are source variants of pol/enforce.py (exp/e245_kit.py): the decision bypassed for send_model, embed, send_inbox or
export; derived records labelled with the first input only; an id-pattern guard instead of the decision point (two fragments,
disclosed). Bypassing only `inject` is a defence-in-depth measurement, not a control.
usage: python -m exp.e2_egress"""
import random
import re
import time

from exp import common  # noqa: F401
from exp import e245_kit as K
from exp.common import write_items, write_result, lab_authority, lab_issue
from exp.seeds import seeds
from pol.directory import Directory, Principal, Session
from pol.labels import Label
from pol.policy import ACTIONS, parse_policy
import pol.enforce as PE

PER_SEED = 1000
CLASSES = ["eu_only", "local_only", "model_banned", "restricted_channel", "other_tenant", "plain"]
MARKER_RE = re.compile(r"@@([a-z_]+):([0-9a-f]{8})@@")
FAR = 4.0e9

DESTS = {  # the experiment's own destination table: the policy is built from it and the oracle reads its attributes
    "d_model_eu": {"kind": "model", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-eu-1", "programme": None},
    "d_model_us": {"kind": "model", "region": "us", "local": False, "max_sensitivity": 3, "model": "m3-1", "programme": None},
    "d_model_cyber": {"kind": "model", "region": "us", "local": False, "max_sensitivity": 3, "model": "m-cy-1", "programme": "cyber"},
    "d_embed_eu": {"kind": "embedder", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m-eu-1", "programme": None},
    "d_embed_us": {"kind": "embedder", "region": "us", "local": False, "max_sensitivity": 3, "model": "m3-1", "programme": None},
    "d_export_eu": {"kind": "export", "region": "eu", "local": False, "max_sensitivity": 3, "model": None, "programme": None},
    "d_export_us": {"kind": "export", "region": "us", "local": False, "max_sensitivity": 3, "model": None, "programme": None},
}
FAMILY = {"m-eu-1": "m-eu", "m3-1": "m3", "m-cy-1": "m-cy"}
RECIPIENTS = {  # id: (region, restricted_ok, model destination or None)
    "r1": ("eu", True, "d_model_eu"), "r2": ("eu", True, "d_model_us"), "r3": ("us", True, "d_model_eu"), "r4": ("eu", False, None),
    "r5": ("us", False, None), "r6": ("us", True, "d_model_cyber"), "r7": ("eu", True, None)}


def policy():
    doc = {"version": 1, "grants": {"t1": list(ACTIONS)},
           "models": {m: {"family": f, "version": "1"} for m, f in FAMILY.items()},
           "destinations": {n: dict({k: v for k, v in d.items() if k != "programme" or v is not None}, tenants=["t1"]) for n, d in DESTS.items()},
           "authorizations": {"auth1": {"principal": "p1", "tenant": "t1", "programme": "cyber", "expires": FAR},
                              "auth2": {"principal": "p2", "tenant": "t1", "programme": "cyber", "expires": FAR},
                              "auth-r6": {"principal": "r6", "tenant": "t1", "programme": "cyber", "expires": FAR}}}
    return parse_policy(doc)


def dest_attrs(name):
    d = DESTS[name]
    return {"kind": d["kind"], "region": d["region"], "local": d["local"], "family": FAMILY.get(d["model"]), "programme": d["programme"]}


def rcp_attrs(rcp):
    return {"kind": "inbox", "rcp_region": rcp.region, "rcp_restricted_ok": rcp.restricted_ok,
            "model_dest": dest_attrs(rcp.model) if rcp.model else None}


def label_for(cls):
    return {"eu_only": Label(["t1"], regions=["eu"]), "local_only": Label(["t1"], local_only=True), "model_banned": Label(["t1"], banned=["m3"]),
            "restricted_channel": Label(["t1"], restricted=True, sensitivity=2), "other_tenant": Label(["t2"]), "plain": Label(["t1"])}[cls]


def oracle_forbidden(cls, attrs, tenant):
    """Independent of the decision point: is a marker of this class forbidden at a destination with these attributes?"""
    if cls == "plain":
        return False
    if cls == "other_tenant":
        return tenant != "t2"
    if attrs["kind"] == "inbox":
        md = attrs["model_dest"]
        if cls == "eu_only":
            return attrs["rcp_region"] != "eu" or (md is not None and md["region"] != "eu")
        if cls == "local_only":
            return md is not None and not md["local"]
        if cls == "model_banned":
            return md is not None and md["family"] == "m3"
        return not (attrs["rcp_restricted_ok"] and (md is None or md["programme"] == "cyber"))
    if cls == "eu_only":
        return attrs["region"] != "eu"
    if cls == "local_only":
        return not attrs["local"]
    if cls == "model_banned":
        return attrs["family"] == "m3"
    return attrs["programme"] != "cyber"


class Scenario:
    def __init__(self, E):
        self.E = E
        self.pol = policy()
        ps = [Principal("p1", "t1", frozenset(["g1", "g2"]), 3, True, "human", "eu", None),
              Principal("p2", "t1", frozenset(["g1"]), 3, True, "human", "eu", None),
              Principal("q1", "t2", frozenset(["g1"]), 3, True, "human", "eu", None)]
        for rid, (reg, rok, model) in RECIPIENTS.items():
            ps.append(Principal(rid, "t1", frozenset(["g1"]), 3, rok, "agent" if model else "human", reg, model))
        self.dir = Directory(ps)
        self.sessions = lab_authority(self.dir)
        self.audit = E.AuditLog()
        self.enf = E.Enforcer(lambda: self.pol, self.dir, self.sessions, self.audit)
        self.store = E.Store(self.enf)
        self.net = net = K.Net()
        for ch, act in (("model", "send_model"), ("embed", "embed"), ("inbox", "send_inbox"), ("export", "export")):
            net.register(ch, act)
        self.chats = {n: E.GuardedChat(K.StubChat(net, n, d["model"], dest_attrs(n), "t1"), self.enf, self.store, n)
                      for n, d in DESTS.items() if d["kind"] == "model"}
        self.embs = {n: E.GuardedEmbedder(K.StubEmbedder(net, n, dest_attrs(n), "t1"), self.enf, self.store, n)
                     for n, d in DESTS.items() if d["kind"] == "embedder"}
        self.exps = {n: E.GuardedExport(lambda dest, tenant, text: net.record("export", dest, dest_attrs(dest), tenant, text), self.enf, self.store, n)
                     for n, d in DESTS.items() if d["kind"] == "export"}
        self.inbox = E.Outbox(self.enf, self.store, "send_inbox",
                              lambda s, r, texts, q: [net.record("inbox", "rcp:" + r.id, rcp_attrs(r), s.tenant, t) for t in texts])
        self.mqueue = E.Outbox(self.enf, self.store, "send_model",
                               lambda s, r, texts, q: [net.record("model", q["destination"], dest_attrs(q["destination"]), s.tenant, t) for t in texts])
        self.xqueue = E.Outbox(self.enf, self.store, "export",
                               lambda s, r, texts, q: [net.record("export", q["destination"], dest_attrs(q["destination"]), s.tenant, t) for t in texts])
        self.s = lab_issue(self.sessions, "p1", "owner_ui")
        self.cls_of = {}

    def classes(self, rids):
        out = set()
        for rid in rids:
            r = self.store._get(rid)
            if r is not None:
                out |= {self.cls_of[m.group(0)] for m in MARKER_RE.finditer(r.text) if m.group(0) in self.cls_of}
        return sorted(out)


def run_scenario(E, seed, i, arm, rows):
    rng = random.Random("%s/%d" % (seed, i))
    n0 = len(rows)                      # identity (GoodQA #16096 class B): this scenario's child rows are rows[n0:]
    sc = Scenario(E)
    st, s = sc.store, sc.s
    pool = []
    for k in range(rng.randint(12, 16)):
        cls = CLASSES[k] if k < len(CLASSES) else rng.choice(CLASSES)
        mk = "@@%s:%08x@@" % (cls, rng.getrandbits(32))
        sc.cls_of[mk] = cls
        idlike = " answer_%08x" % rng.getrandbits(32) if (cls == "plain" and rng.random() < 0.5) else ""
        lab = label_for(cls)
        pool.append(st._put(sorted(lab.tenants)[0], "note %s about item %d%s" % (mk, k, idlike), lab, ()))     # planted directly

    def attempt(op, channel, dest, attrs, refs, fn):
        before = len(sc.net.payloads)
        try:
            res = fn()
            out = "ok"
        except E.PolicyDenied as e:
            res, out = None, "denied:" + e.decision.reason
        rows.append({"t": "attempt", "arm": arm, "seed": seed, "i": i, "op": op, "channel": channel, "dest": dest, "attrs": attrs,
                     "classes": sc.classes(refs), "outcome": out, "payloads": len(sc.net.payloads) - before})
        return res

    def via(channel, dest, refs, request, op):
        if channel == "model" and "authorization" not in request:
            res = attempt(op, "model", dest, dest_attrs(dest), refs, lambda: sc.chats[dest].complete(s, refs))
            if res is not None:
                pool.append(res.id)
        elif channel == "model":
            attempt(op, "model", dest, dest_attrs(dest), refs, lambda: sc.mqueue.enqueue(s, refs, dict(request, destination=dest)))
            sc.mqueue.deliver_all()
        elif channel == "embed":
            attempt(op, "embed", dest, dest_attrs(dest), refs, lambda: sc.embs[dest].embed(s, refs))
        elif channel == "export":
            if rng.random() < 0.5:
                attempt(op, "export", dest, dest_attrs(dest), refs, lambda: sc.exps[dest].export(s, refs))
            else:
                attempt(op, "export", dest, dest_attrs(dest), refs, lambda: sc.xqueue.enqueue(s, refs, {"destination": dest}))
                sc.xqueue.deliver_all()
        else:
            rid = dest[4:]
            rq = dict(request, recipient_id=rid)
            attempt(op, "inbox", dest, rcp_attrs(sc.dir.lookup(rid)), refs, lambda: sc.inbox.enqueue(s, refs, rq))
            sc.inbox.deliver_all()

    for _ in range(rng.randint(4, 10)):
        op = rng.choice(["inject_send", "direct_send", "queue_model", "embed", "inbox", "export", "derive_send", "inject_send", "direct_send",
                         "compliant", "compliant", "compliant", "compliant", "forge", "derive_send", "derive_send"])
        picks = rng.sample(pool, rng.randint(1, min(4, len(pool))))
        if rng.random() < 0.5:
            sub = [r for r in pool if set(sc.classes([r])) & {"restricted_channel", "model_banned", "local_only", "eu_only"}]
            if sub:
                picks = rng.sample(sub, rng.randint(1, min(3, len(sub))))
        auth = {"authorization": rng.choice(["auth1", "zzz"])} if rng.random() < 0.6 else {}
        if op == "compliant":
            options = [("model", n) for n in sc.chats] + [("embed", n) for n in sc.embs] + [("export", n) for n in sc.exps] + \
                      [("inbox", "rcp:" + r) for r in RECIPIENTS]
            rs_ = [x for x in pool if "restricted_channel" in sc.classes([x])]
            r = rng.choice(rs_) if (rs_ and rng.random() < 0.4) else rng.choice(pool)
            cl = sc.classes([r])

            def attrs_of(o):
                return rcp_attrs(sc.dir.lookup(o[1][4:])) if o[0] == "inbox" else dest_attrs(o[1])
            ok = [o for o in options if not any(oracle_forbidden(c, attrs_of(o), "t1") for c in cl)]
            if ok:
                ch, dest = rng.choice(ok)
                rq = {}
                if "restricted_channel" in cl and ch == "model":
                    rq = {"authorization": "auth1"}
                if "restricted_channel" in cl and dest == "rcp:r6":
                    rq = {"authorization": "auth-r6"}
                via(ch, dest, [r], rq, op)
        elif op == "inject_send":
            dest = rng.choice(list(sc.chats))
            ok_refs, w = st.context(s, picks, dest)
            rows.append({"t": "inject", "arm": arm, "seed": seed, "i": i, "dest": dest, "attrs": dest_attrs(dest), "offered": sc.classes(picks),
                         "kept": sc.classes(ok_refs), "withheld": w})
            if ok_refs:
                via("model", dest, ok_refs, {}, op)
        elif op == "direct_send":
            dest = rng.choice(list(sc.chats) + ["d_model_cyber", "d_model_us"])
            via("model", dest, picks, {}, op)
        elif op == "queue_model":
            dest = rng.choice(list(sc.chats))
            via("model", dest, picks, auth or {"authorization": "none"}, op)
        elif op == "derive_send":
            if len(picks) < 2:
                picks = picks + [pool[0]]
            text = "digest of: " + " | ".join(st._get(x).text for x in picks if st._get(x) is not None)
            try:
                d = st.append_derived(s, text, picks)
                pool.append(d)
            except E.PolicyDenied as e:
                rows.append({"t": "attempt", "arm": arm, "seed": seed, "i": i, "op": "derive", "channel": "store", "dest": "store", "attrs": None,
                             "classes": sc.classes(picks), "outcome": "denied:" + e.decision.reason, "payloads": 0})
                continue
            path = rng.choice(["model", "embed", "export", "inbox"])
            dest = {"model": rng.choice(list(sc.chats)), "embed": rng.choice(list(sc.embs)), "export": rng.choice(list(sc.exps)),
                    "inbox": "rcp:" + rng.choice(list(RECIPIENTS))}[path]
            via(path, dest, [d], {}, op)
        elif op == "embed":
            dest = "d_embed_us" if ("model_banned" in sc.classes(picks) and rng.random() < 0.5) else rng.choice(list(sc.embs))
            via("embed", dest, picks, {}, op)
        elif op == "export":
            via("export", rng.choice(list(sc.exps)), picks, {}, op)
        elif op == "inbox":
            rid = "r2" if ("model_banned" in sc.classes(picks) and rng.random() < 0.5) else rng.choice(list(RECIPIENTS))
            via("inbox", "rcp:" + rid, picks, auth, op)
        else:
            forge(sc, rng, pool, arm, seed, i, rows)

    for p in sc.net.payloads:
        mks = sorted({m.group(0) for m in MARKER_RE.finditer(p.text)})
        rows.append({"t": "payload", "arm": arm, "seed": seed, "i": i, "channel": p.channel, "dest": p.dest, "attrs": p.attrs, "tenant": p.tenant,
                     "markers": [[m, sc.cls_of[m]] for m in mks if m in sc.cls_of]})
    for k, r in enumerate(rows[n0:]):   # each child's index in its scenario; the scenario row declares the count
        r["k"] = k
    rows.append({"t": "scenario", "arm": arm, "seed": seed, "i": i, "payloads": len(sc.net.payloads),
                 "evaluated_actions": sorted({r["action"] for r in sc.audit._snapshot()}), "registered": sorted(sc.net.registered.values()),
                 "items": len(rows) - n0})


FORGES = ["extra_tenant", "extra_principal", "extra_now", "extra_recipient_object", "credential_unknown_key", "foreign_recipient", "unknown_recipient",
          "other_principals_authorization", "forged_session", "other_authority_session", "ended_session", "principal_as_session"]


def forge(sc, rng, pool, arm, seed, i, rows):
    """Everything a caller can send, forged. Each forge must be refused with nothing recorded."""
    E, st = sc.E, sc.store
    f = rng.choice(FORGES)
    rs_ = [x for x in pool if "restricted_channel" in sc.classes([x])] or pool
    refs = [rng.choice(rs_ if f == "other_principals_authorization" else pool)]
    s = sc.s
    path = rng.choice(["inbox", "model_queue", "export_queue", "chat", "embed", "export", "context"])
    rq = {}
    if f == "extra_tenant":
        rq = {"tenant": "t2"}
    elif f == "extra_principal":
        rq = {"principal": "p2"}
    elif f == "extra_now":
        rq = {"now": 0.0}
    elif f == "extra_recipient_object":
        rq = {"recipient": sc.dir.lookup("r1")}
    elif f == "credential_unknown_key":
        rq = {"credential": {rng.choice(["scope", "cap_actions ", "cаp_actions", "CAP_CLEARANCE"]): ["send_inbox", "send_model", "export"]}}
    elif f == "foreign_recipient":
        rq, path = {"recipient_id": "q1"}, "inbox"
    elif f == "unknown_recipient":
        rq, path = {"recipient_id": "nobody"}, "inbox"
    elif f == "other_principals_authorization":
        rq, path = {"authorization": "auth2"}, "model_queue"
    elif f == "forged_session":
        s = Session("t1", "p1", "owner_ui", "forged-token")
    elif f == "other_authority_session":
        s = lab_issue(lab_authority(sc.dir), "p1", "owner_ui")
    elif f == "ended_session":
        s = lab_issue(sc.sessions, "p1", "owner_cli")
        sc.sessions.end(s)
    elif f == "principal_as_session":
        s = sc.dir.lookup("p1")
    if f.startswith(("extra_", "credential_")) and path in ("chat", "embed", "export", "context"):
        path = rng.choice(["inbox", "model_queue", "export_queue"])
    before = len(sc.net.payloads)
    try:
        if path == "inbox":
            q = dict(rq)
            q.setdefault("recipient_id", "r7")
            sc.inbox.enqueue(s, refs, q)
            _d, r = sc.inbox.deliver_all()
            out = "ok" if len(sc.net.payloads) > before else "denied_at_delivery"
        elif path == "model_queue":
            sc.mqueue.enqueue(s, refs, dict(rq, destination="d_model_cyber"))
            sc.mqueue.deliver_all()
            out = "ok" if len(sc.net.payloads) > before else "denied_at_delivery"
        elif path == "export_queue":
            sc.xqueue.enqueue(s, refs, dict(rq, destination="d_export_eu"))
            sc.xqueue.deliver_all()
            out = "ok" if len(sc.net.payloads) > before else "denied_at_delivery"
        elif path == "chat":
            sc.chats["d_model_eu"].complete(s, refs)
            out = "ok"
        elif path == "embed":
            sc.embs["d_embed_eu"].embed(s, refs)
            out = "ok"
        elif path == "export":
            sc.exps["d_export_eu"].export(s, refs)
            out = "ok"
        else:
            ok_refs, _w = st.context(s, refs, "d_model_eu")
            out = "ok" if ok_refs else "denied:withheld"
    except E.PolicyDenied as e:
        out = "denied:" + e.decision.reason
    rows.append({"t": "forge", "arm": arm, "seed": seed, "i": i, "forge": f, "path": path, "classes": sc.classes(refs), "outcome": out,
                 "payloads": len(sc.net.payloads) - before})


ARMS = [("enforced", lambda: PE), ("ctl_model_port_disabled", lambda: K.bypass("send_model")), ("ctl_embed_disabled", lambda: K.bypass("embed")),
        ("ctl_inbox_disabled", lambda: K.bypass("send_inbox")), ("ctl_export_disabled", lambda: K.bypass("export")),
        ("ctl_derive_join_dropped", K.derive_first), ("ctl_id_pattern_guard", K.id_pattern_guard), ("info_inject_only_disabled", lambda: K.bypass("inject"))]


def summarise(rows):
    """The experiment's own verdict (verify/e2.py recomputes it independently from the same rows)."""
    out = {}
    for arm, _ in ARMS:
        ar = [r for r in rows if r["arm"] == arm]
        scen = {(r["seed"], r["i"]) for r in ar if r["t"] == "scenario"}
        leak_sc, viol, permitted, chans = set(), 0, {c: 0 for c in CLASSES}, set()
        for r in ar:
            if r["t"] != "payload":
                continue
            chans.add(r["channel"])
            for _m, c in r["markers"]:
                if oracle_forbidden(c, r["attrs"], r["tenant"]):
                    viol += 1
                    leak_sc.add((r["seed"], r["i"]))
                else:
                    permitted[c] += 1
        forb = {c: 0 for c in CLASSES}
        for r in ar:
            if r["t"] == "attempt" and r["attrs"] is not None:
                for c in r["classes"]:
                    forb[c] += oracle_forbidden(c, r["attrs"], "t1")
        forges = [r for r in ar if r["t"] == "forge"]
        out[arm] = {"scenarios": len(scen), "scenarios_with_leak": len(leak_sc), "violations": viol, "permitted": permitted, "attempts_forbidden": forb,
                    "channels": sorted(chans), "forges": len(forges), "forges_not_denied": sum(1 for r in forges if not r["outcome"].startswith("denied")),
                    "forge_payloads": sum(r["payloads"] for r in forges),
                    "id_pattern_blocks": sum(1 for r in ar if r["t"] == "attempt" and r["outcome"] == "denied:id_pattern"),
                    "evaluated_actions": sorted({a for r in ar if r["t"] == "scenario" for a in r["evaluated_actions"]}),
                    "registered": sorted({a for r in ar if r["t"] == "scenario" for a in r["registered"]})}
    a = out["enforced"]
    expected = sorted(x for x in ACTIONS if x not in ("append", "read", "inject", "erase"))
    pos = (a["violations"] == 0 and a["scenarios"] >= 2000 and all(a["attempts_forbidden"][c] >= 1000 for c in CLASSES if c != "plain")
           and all(a["permitted"][c] >= 1000 for c in CLASSES if c != "other_tenant") and a["channels"] == ["embed", "export", "inbox", "model"]
           and a["registered"] == expected and set(expected) | {"inject"} <= set(a["evaluated_actions"])
           and a["forges"] >= 1000 and a["forges_not_denied"] == 0 and a["forge_payloads"] == 0)
    ctl = {k: {"leak_rate": v["scenarios_with_leak"] / max(1, v["scenarios"]), "violations": v["violations"], "id_pattern_blocks": v["id_pattern_blocks"],
               "meets_floor": v["scenarios_with_leak"] >= 0.10 * v["scenarios"] and v["violations"] >= 1
               and (k != "ctl_id_pattern_guard" or v["id_pattern_blocks"] >= 1)} for k, v in out.items() if k.startswith("ctl_")}
    return out, pos, ctl


def main():
    t0 = time.time()
    rows = []
    sd = seeds("e2")
    for arm, mk in ARMS:
        E = mk()
        for seed in sd:
            for i in range(PER_SEED):
                run_scenario(E, seed, i, arm, rows)
    arms, pos, ctl = summarise(rows)
    verdict = "PASS" if pos and all(c["meets_floor"] for c in ctl.values()) else "FAIL"
    data = {"verdict": verdict, "arms": arms, "controls": ctl, "positive_conditions_met": pos, "runtime_s": round(time.time() - t0, 1),
            "defence_in_depth_inject_only_disabled": {"violations": arms["info_inject_only_disabled"]["violations"]},
            "not_proven": "stubs only (no real adapter); the oracle and the policy are built from the same destination table; the oracle does not see "
                          "restricted authorisations; other_tenant has no permitted destination by design; markers are plain strings (no encodings or "
                          "paraphrase); GuardedChat takes no authorisation, so restricted content reaches a model only through the Outbox(send_model) path"}
    data["items"] = dict(zip(("file", "sha256"), write_items("e2_egress", rows)))
    write_result("e2_egress", {"seeds": sd, "per_seed": PER_SEED, "classes": CLASSES, "forges": FORGES}, data)
    a = arms["enforced"]
    print(verdict, "violations", a["violations"], "forbidden", a["attempts_forbidden"], "permitted", a["permitted"], a["channels"],
          "forges", a["forges"], "not denied", a["forges_not_denied"], "rows", len(rows))
    for k, c in ctl.items():
        print(" ", k, c)
    print("  inject-only disabled violations:", arms["info_inject_only_disabled"]["violations"], "runtime", data["runtime_s"])


if __name__ == "__main__":
    import os
    PER_SEED = int(os.environ.get("E2_PER_SEED", PER_SEED))
    main()

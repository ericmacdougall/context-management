"""E5: parallel related threads with different access groups, and viewer-scoped decision reads.

Arm family A (thread worlds, exp/threads_world.py): a member of one thread never receives content from another through records,
prompt injection, analyst findings (inbox) or cross-reference expansion, all assembled through production calls with the viewer's own
session; non-interference: mutating everything the viewer cannot read leaves the viewer's serialised view byte-identical.
Controls (one-fragment variants of pol/enforce.py): derived records labelled with the first input only; Store.read without the
per-reference decision (expansion without a decision).

Arm family B (viewer-scoped reads): DecisionService.explain and table for viewers with different clearances, groups, regions and
restricted flags, over requirements with open proposals, current values and contested candidates. Notice NONE (default) and
existence_only (opt-in). PASS: no marker of an unreadable value, proposal or candidate in any view; the hidden_candidates key exists
only when opted in, and when present it is true exactly when something was withheld; non-interference as above. Controls:
exp.patches control('h7_flag_always') (notice carried even when not opted in) and control('h7_unscoped_value') (values shown without a
read decision).

The per-item artifact holds RAW observations: each viewer's serialised view, the SHA-256 of the view built from the mutated world,
the viewer's attributes, and the ground truth (tokens or markers with the labels they come from). verify/e5.py recomputes the verdict.
usage: python -m exp.e5_threads"""
import hashlib
import json
import random
import time

from exp import common  # noqa: F401
from exp import e245_kit as K
from exp import threads_world as TW
from exp.common import write_items, write_result
from exp.patches import control, variant
from exp.seeds import seeds
from pol import _core
from pol.directory import Directory, Principal, SessionAuthority
from pol.enforce import AuditLog, Enforcer
from pol.labels import Label
from pol.policy import parse_policy
import pol.decisions as PD
import pol.enforce as PE

PER_SEED = 200
GROUPS = ["ga", "gb", "gc"]


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ arm family A
def run_thread_arm(E, arm, sd, rows):
    for seed in sd:
        for i in range(PER_SEED):
            rng = random.Random("%s/%d" % (seed, i))
            w = TW.gen_world(rng)
            rows.append({"t": "world", "fam": "A", "arm": arm, "seed": seed, "i": i,
                         "edges": [[w.items[a].thread, w.items[b].thread, t] for a, b, t in w.edges], "findings": len(w.findings),
                         "sources": TW.sources(w)})
            for v in TW.viewers(w):
                blob = TW.serialise(TW.build_view(E, w, v))
                mblob = TW.serialise(TW.build_view(E, TW.mutate(w, v), v))
                rows.append({"t": "view", "fam": "A", "arm": arm, "seed": seed, "i": i, "viewer": {"id": v[0], "tenant": v[1], "groups": v[2]},
                             "blob": blob, "blob_sha256": sha(blob), "mutated_sha256": sha(mblob)})


# ------------------------------------------------------------------------------------------------ arm family B
def rand_label(rng):
    return Label(["t1"], acl=rng.choice([[], [["ga"]], [["gb"]], [["ga", "gb"]], [["gc"]]]), sensitivity=rng.choice([0, 0, 1, 2, 3]),
                 restricted=rng.random() < 0.1, regions=rng.choice([None, None, ["eu"]]))


def gen_decision_world(rng):
    humans = ["h%d" % k for k in range(4)]
    attrs = {h: {"groups": sorted(rng.sample(GROUPS, rng.randint(0, 3))), "clearance": rng.randint(0, 3), "restricted_ok": rng.random() < 0.3,
                 "region": rng.choice(["eu", "us"])} for h in humans}
    authority = {r: sorted(rng.sample(humans, rng.randint(2, 3))) for r in ("r1", "r2", "r3")}
    script = []
    n = 0
    for req in ("r1", "r2", "r3"):
        auth = authority[req]
        props = []
        for _ in range(rng.randint(2, 4)):
            n += 1
            props.append(("p%d" % n, rng.choice(auth), "MK-%s" % TW.hexs(rng), rand_label(rng)))
        script.append(("props", req, props))
        mode = rng.choice(["open", "accept", "contest", "contest", "contest_resolve"])
        if mode != "open":
            a, b = rng.sample(auth, 2)
            pa, pb = rng.sample(props, 2)
            script.append(("accept", req, a, pa[0], 0))
            if mode.startswith("contest"):
                script.append(("accept", req, b, pb[0], 0))
                if mode == "contest_resolve":
                    script.append(("resolve", req, rng.choice(auth)))
    return attrs, authority, script


def build_decision_views(D, attrs, authority, script, notice, mutated_for=None):
    """Replay the script with the given decisions module; returns ({viewer: blob}, ground truth per requirement). With mutated_for =
    a viewer, every value that viewer may not read gets other text (the same events otherwise)."""
    ps = [Principal(h, "t1", frozenset(a["groups"]), a["clearance"], a["restricted_ok"], "human", a["region"], None) for h, a in attrs.items()]
    d = Directory(ps)
    sa = SessionAuthority(d)
    pol = parse_policy({"version": 1, "grants": {"t1": ["read", "append"]}, "conflict_notice": notice})
    enf = Enforcer(lambda: pol, d, sa, AuditLog())
    eng = D.MemoryEngine({"tenant": "t1", "principals": {h: "human" for h in attrs}, "authority": authority, "admins": []})
    vault = D.Vault()
    vault._key = b"e5-fixed-key-so-refs-compare-across-replays"
    svc = D.DecisionService(enf, {"t1": eng}, vault)
    sess = {h: sa.issue(h, "owner_ui") for h in attrs}
    refs, n = {}, [0]
    for step in script:
        if step[0] == "props":
            _k, req, props = step
            for pid, author, marker, lab in props:
                text = "value %s" % marker
                if mutated_for is not None and not readable_dec(lab, attrs[mutated_for]):
                    text = "value MK-mut-%s" % pid
                r = svc.submit(sess[author], {"type": "propose", "client_id": pid, "idem": pid, "req": req, "value": text, "label": lab.to_dict(),
                                              "based_on": 0})
                assert r["code"] == "proposed", r
                refs[pid] = (r["id"], r["value_ref"], author)
        elif step[0] == "accept":
            _k, req, who, pid, based = step
            iid, ref, author = refs[pid]
            n[0] += 1
            svc.submit(sess[who], {"type": "decide", "client_id": "d%d" % n[0], "idem": "d%d" % n[0], "req": req, "verb": "accept", "proposal": iid,
                                   "proposal_hash": _core.proposal_hash(iid, ref, author), "based_on": based})
        else:
            _k, req, who = step
            h = eng._head(req)
            if h["contested"] is not None:
                n[0] += 1
                svc.submit(sess[who], {"type": "decide", "client_id": "d%d" % n[0], "idem": "d%d" % n[0], "req": req, "verb": "resolve",
                                       "choose": h["contested"]["candidates"][0]["event"], "based_on": h["version"]})
    truth = {}
    for req in authority:
        h = eng._head(req)
        el = []
        if h["value_ref"] is not None:
            el.append({"role": "head", "ref": h["value_ref"], "label": h["value_label"]})
        if h["contested"] is not None:
            for c in h["contested"]["candidates"]:
                if c["effect"][0] == "set":
                    el.append({"role": "candidate", "ref": c["effect"][1], "label": c["label"]})
        for it in eng._open_items(req):
            el.append({"role": "open", "ref": it["value_ref"], "label": it["label"]})
        for e in el:
            v = vault._get("t1", e["ref"])
            e["text"] = v["text"] if v else None
        truth[req] = el
    blobs = {}
    for h in attrs:
        view = {"explain": {r: svc.explain(sess[h], r) for r in sorted(authority)}, "table": svc.table(sess[h])}
        blobs[h] = json.dumps(view, sort_keys=True)
    return blobs, truth


def readable_dec(lab, a):
    """Used only to choose what the mutated replay changes (the verifier has its own oracle)."""
    return (lab.tenants == frozenset(["t1"]) and all(set(c) & set(a["groups"]) for c in lab.acl) and a["clearance"] >= lab.sensitivity
            and (not lab.restricted or a["restricted_ok"]) and (lab.regions is None or a["region"] in lab.regions))


def run_decision_arm(D, arm, notice, sd, rows):
    for seed in sd:
        for i in range(PER_SEED):
            rng = random.Random("dec/%s/%d" % (seed, i))
            attrs, authority, script = gen_decision_world(rng)
            blobs, truth = build_decision_views(D, attrs, authority, script, notice)
            rows.append({"t": "dworld", "fam": "B", "arm": arm, "seed": seed, "i": i, "notice": notice, "authority": authority, "truth": truth})
            for h in sorted(attrs):
                mblobs, _t = build_decision_views(D, attrs, authority, script, notice, mutated_for=h)
                rows.append({"t": "dview", "fam": "B", "arm": arm, "seed": seed, "i": i, "notice": notice, "viewer": dict(attrs[h], id=h),
                             "blob": blobs[h], "blob_sha256": sha(blobs[h]), "mutated_sha256": sha(mblobs[h])})


def own_summary(rows):
    """The experiment's own verdict, with its own oracles (verify/e5.py recomputes it independently)."""
    W = {(r["arm"], r["seed"], r["i"]): r for r in rows if r["t"] in ("world", "dworld")}
    arms = {}
    for r in rows:
        if r["t"] not in ("view", "dview"):
            continue
        k = (r["arm"], r["seed"], r["i"])
        a = arms.setdefault(r["arm"], {"worlds": set(), "leak": set(), "interf": set(), "keybad": set(), "flags_true": 0, "flag_wrong": 0})
        a["worlds"].add(k)
        if r["mutated_sha256"] != r["blob_sha256"]:
            a["interf"].add(k)
        v = r["viewer"]
        if r["t"] == "view":
            vv = (v["id"], v["tenant"], v["groups"])
            for s_ in W[k]["sources"]:
                if not all(TW.readable_by(Label.from_dict(l), vv) for l in s_["labels"]) and any(t in r["blob"] for t in s_["tokens"]):
                    a["leak"].add(k)
            continue
        view = json.loads(r["blob"])
        opted = W[k]["notice"] == "existence_only"
        for req, els in W[k]["truth"].items():
            hid = [e for e in els if not readable_dec(Label.from_dict(e["label"]), v)]
            if any(e["text"].split()[-1] in r["blob"] for e in hid):
                a["leak"].add(k)
            ex = view["explain"][req]
            if ex is not None:
                if ("hidden_candidates" in ex) != opted:
                    a["keybad"].add(k)
                elif opted:
                    a["flags_true"] += ex["hidden_candidates"]
                    a["flag_wrong"] += ex["hidden_candidates"] != bool(hid)
    out = {n: {"worlds": len(a["worlds"]), "leak_worlds": len(a["leak"]), "interference_worlds": len(a["interf"]), "key_rule_worlds": len(a["keybad"]),
               "flags_true": a["flags_true"], "flag_wrong": a["flag_wrong"]} for n, a in arms.items()}
    pos = (all(out[n]["leak_worlds"] == 0 and out[n]["interference_worlds"] == 0 and out[n]["key_rule_worlds"] == 0 and out[n]["worlds"] >= 1000
               for n in ("A_enforced", "B_notice_none", "B_notice_existence_only"))
           and out["B_notice_existence_only"]["flag_wrong"] == 0 and out["B_notice_existence_only"]["flags_true"] >= 1000)
    ctl = {n: max(x["leak_worlds"], x["key_rule_worlds"]) >= 0.10 * x["worlds"] for n, x in out.items() if "_ctl_" in n and n != "B_ctl_unscoped_value"}
    return {"verdict": "PASS" if pos and all(ctl.values()) else "FAIL", "checks": dict(positive=pos, **{"control:" + n: c for n, c in ctl.items()}),
            "arms": out}


ARMS_A = [("A_enforced", lambda: PE), ("A_ctl_derive_join_dropped", K.derive_first), ("A_ctl_xref_without_decision", K.read_without_decision)]
ARMS_B = [("B_notice_none", lambda: PD, "none"), ("B_notice_existence_only", lambda: PD, "existence_only"),
          ("B_ctl_flag_always", lambda: control("h7_flag_always"), "none"), ("B_ctl_unscoped_value", lambda: control("h7_unscoped_value"), "none"),
          ("B_ctl_no_read_check", lambda: no_read_check(), "none")]


def no_read_check():
    """DecisionService reads without the read decision (one fragment of pol/decisions.py: _readable always true). The shared control
    h7_unscoped_value removes only the inner check in _value, which explain never reaches for an unreadable label; it is kept as an
    informational arm and does not need to meet the floor."""
    return variant("pol.decisions", "        if label is None:\n            return True\n", "        if True:\n            return True\n", "h7_no_read_check")


def main():
    t0 = time.time()
    rows = []
    sd = seeds("e5")
    for arm, mk in ARMS_A:
        run_thread_arm(mk(), arm, sd, rows)
    for arm, mk, notice in ARMS_B:
        run_decision_arm(mk(), arm, notice, sd, rows)
    summary = own_summary(rows)
    data = {"verdict": summary["verdict"], "summary": summary, "runtime_s": round(time.time() - t0, 1),
            "not_proven": "timing and ordering side channels; the 'context withheld' count returned by Store.context to the caller; graph shape beyond one "
                          "hop; store-item conflict notices no longer exist in production (the notice contract is measured on DecisionService only)"}
    data["items"] = dict(zip(("file", "sha256"), write_items("e5_threads", rows)))
    write_result("e5_threads", {"seeds": sd, "per_seed": PER_SEED}, data)
    print(summary["verdict"], json.dumps(summary["checks"], indent=0), "runtime", data["runtime_s"])
    for k, v in summary["arms"].items():
        print(" ", k, v)


if __name__ == "__main__":
    import os
    PER_SEED = int(os.environ.get("E5_PER_SEED", PER_SEED))
    main()

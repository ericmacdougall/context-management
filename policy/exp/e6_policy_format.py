"""E6: client-supplied policy format. An empty, absent or malformed policy denies everything (the validator raises PolicyError on
every malformed document and safe_policy falls back to deny-all); a valid document is not deny-all; the example policy is marked as an
example; a recorded legal hold beats erasure (queued, not refused for good) and erasure covers what others said about a subject.
Control: a lenient loader, built as a one-patch source variant of pol/policy.py safe_policy (on a parse error it keeps whatever grants it
can read, else allows every action).
Rows hold raw observations (documents, parse outcomes, per-probe allow/reason, erasure counts and vault contents); verify/e6.py decides.
usage: python -m exp.e6_policy_format"""
import copy
import json
import os
import random
import time

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.patches import variant
from exp.seeds import seeds
from pol import _pdp
from pol.directory import Principal
from pol.labels import Label, join
from pol.policy import ACTIONS, DENY_ALL, EXAMPLE_POLICY, Policy, PolicyError, parse_policy, safe_policy

N_MALFORMED = int(os.environ.get("E6_MALFORMED", "3000"))
N_PROBES = 5
N_EMPTY = 3000
N_VALID = 3000
NOW = 10.0
LENIENT = ("        return DENY_ALL\n",
           "        try:\n"
           "            return Policy(1, grants={t: frozenset(a) for t, a in doc[\"grants\"].items()})\n"
           "        except Exception:       # noqa: BLE001\n"
           "            return Policy(1, grants={\"t1\": frozenset(ACTIONS), \"t2\": frozenset(ACTIONS)})\n")
LEN = variant("pol.policy", LENIENT[0], LENIENT[1], "e6_lenient_loader")


def lenient_load(doc):
    """The control loader; its Policy is re-expressed as the production Policy class (same fields) so the decision point accepts it."""
    import dataclasses
    lp = LEN.safe_policy(doc)
    return Policy(**{f.name: getattr(lp, f.name) for f in dataclasses.fields(lp)})

GOOD = {"version": 3, "grants": {"t1": list(ACTIONS), "t2": list(ACTIONS)}, "models": {"m1@1": {"family": "m1", "version": "1"}},
        "destinations": {"m": {"kind": "model", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m1@1", "tenants": ["t1", "t2"]},
                         "e": {"kind": "embedder", "region": "eu", "local": True, "max_sensitivity": 3, "model": "m1@1", "tenants": ["t1", "t2"]},
                         "x": {"kind": "export", "region": "eu", "local": False, "max_sensitivity": 2, "model": None, "tenants": ["t1"]}},
        "authorizations": {"a": {"tenant": "t1", "programme": "x", "expires": 5, "principal": "p"}}, "banned_models": ["m9"], "hold_events": ["h1"]}


def mutate(rng, doc):
    d = copy.deepcopy(doc)
    k = rng.choice(["unknown_key", "version", "grants_type", "bad_action", "dest_kind", "dest_pin", "none", "list", "string", "dest_local",
                    "auth_keys", "banned_type", "cn", "drop_version", "dest_tenants", "model_family", "alias_cycle", "alias_dangling"])
    if k == "unknown_key":
        d[rng.choice(["allow_all", "Grants", "grants ", "versión"])] = True
    elif k == "version":
        d["version"] = rng.choice([0, -1, "1", None, True, 1.5])
    elif k == "grants_type":
        d["grants"] = rng.choice([[], "x", 5, {"t1": "send_model"}, {"t1": [1]}, {5: ["read"]}])
    elif k == "bad_action":
        d["grants"] = {"t1": ["read", rng.choice(["root", "READ", "read ", "*"])]}
    elif k == "dest_kind":
        d["destinations"]["m"]["kind"] = rng.choice(["x", None, 3])
    elif k == "dest_pin":
        d["destinations"]["m"].pop("model", None)
    elif k == "dest_local":
        d["destinations"]["m"]["local"] = rng.choice(["yes", 1, None])
    elif k == "dest_tenants":
        d["destinations"]["m"].pop("tenants")
    elif k == "auth_keys":
        d["authorizations"] = {"a": {"tenant": "t1"}}
    elif k == "banned_type":
        d["banned_models"] = rng.choice(["m1", 5, [1], {"a": 1}])
    elif k == "cn":
        d["conflict_notice"] = rng.choice(["full", None, 1])
    elif k == "drop_version":
        d.pop("version")
    elif k == "model_family":
        d["models"]["m1x@1"] = {"family": "m1-other", "version": "1"}
    elif k == "alias_cycle":
        d["model_aliases"] = {"a1": "a2", "a2": "a1"}
    elif k == "alias_dangling":
        d["model_aliases"] = {"a1": "nowhere"}
    elif k == "none":
        return k, None
    elif k == "list":
        return k, []
    else:
        return k, "policy"
    return k, d


def probe(rng):
    t = rng.choice(["t1", "t2"])
    p = Principal("p%d" % rng.randrange(10 ** 6), t, frozenset(rng.sample(["g1", "g2", "g3"], 2)), rng.choice([1, 2, 3]), rng.random() < 0.5,
                  rng.choice(["human", "agent"]), rng.choice(["eu", "us", None]), None)
    a = rng.choice(ACTIONS)
    lab = Label([t], acl=rng.choice([[], [["g1"]], [["g2", "g3"]]]), regions=rng.choice([None, ["eu"]]), sensitivity=rng.choice([0, 1, 2]))
    req = {}
    if a in ("send_model", "inject"):
        req["destination"] = "m"
    elif a == "embed":
        req["destination"] = "e"
    elif a == "export":
        req["destination"] = "x"
    rcp = Principal("r%d" % rng.randrange(1000), t, frozenset(["g1", "g2", "g3"]), 3, True, "human", "eu", None) if a == "send_inbox" else None
    return p, a, lab, req, rcp


def ev(pol, pr):
    p, a, lab, req, rcp = pr
    d = _pdp.evaluate(p, a, [lab], dict(req), pol, NOW, rcp)
    return [bool(d.allow), d.reason, a]


def holds_rows(seed):
    """Legal hold and erasure scope, at the decision point and through the decision service (what others said about a subject)."""
    from tests.kit import World, policy_doc
    pol = parse_policy(GOOD)
    p = Principal("p", "t1", frozenset(), 3, True, "human", "eu", None)
    q = Principal("q", "t2", frozenset(), 3, True, "human", "eu", None)
    hold_lab = Label(["t1"], holds=["h1"], subjects=["s1"])
    rows = [{"arm": "hold_pdp", "seed": seed, "case": c, "returned": ev(pl, (pp, "erase", lab, {}, None))} for c, pl, pp, lab in [
        ("recorded_hold", pol, p, hold_lab),
        ("unrecorded_hold", pol, p, Label(["t1"], holds=["hx"], subjects=["s1"])),
        ("no_hold", pol, p, Label(["t1"], subjects=["s1"])),
        ("hold_event_removed", pol.with_changes({"hold_events": frozenset()}), p, hold_lab),
        ("other_tenant", pol, q, hold_lab)]]
    rows.append({"arm": "hold_join", "seed": seed, "inputs": [hold_lab.to_dict(), Label(["t1"], subjects=["s2"]).to_dict()],
                 "derived": join([hold_lab, Label(["t1"], subjects=["s2"])]).to_dict()})
    w = World(policy=parse_policy(policy_doc(hold_events=["hold-7"])))
    a, b = w.s("alice"), w.s("bob")
    w.propose(a, "p1", "alice-about-dana-held", subjects=["dana"], holds=["hold-7"])
    w.propose(b, "p2", "bob-about-dana", subjects=["dana"])
    w.propose(a, "p3", "alice-about-dana", subjects=["dana"])
    w.propose(b, "p4", "bob-about-erin", subjects=["erin"])
    before = sorted(v["text"] for v in w.vault._vals.values())
    out_user = w.svc.erase_subject(a, "dana")
    out = w.svc.erase_subject(w.s("admin"), "dana")
    after = sorted(v["text"] for v in w.vault._vals.values())
    rows.append({"arm": "erase_service", "seed": seed, "before": before, "after": after, "non_admin": out_user, "admin": out})
    return rows


def main():
    sd = seeds("e6")
    t0 = time.time()
    rows = []
    parse_policy(GOOD)
    for s in sd:
        rng = random.Random("%s|e6" % s)
        for i in range(N_EMPTY):
            pr = probe(rng)
            rows.append({"arm": "empty", "seed": s, "returned": {"Policy(1)": ev(Policy(1), pr), "DENY_ALL": ev(DENY_ALL, pr),
                                                                 "safe({})": ev(safe_policy({}), pr), "safe(None)": ev(safe_policy(None), pr)}})
        for i in range(N_MALFORMED):
            kind, bad = mutate(rng, GOOD)
            try:
                parse_policy(bad)
                parsed = "returned"
            except PolicyError:
                parsed = "PolicyError"
            except Exception as e:      # noqa: BLE001  any other escape is recorded raw (a validator bug)
                parsed = "escaped:" + type(e).__name__
            sp, lp = safe_policy(bad), lenient_load(bad)
            prs = [probe(rng) for _ in range(N_PROBES)]
            rows.append({"arm": "malformed", "seed": s, "kind": kind, "doc": json.dumps(bad, default=repr)[:600], "parse": parsed,
                         "safe": [ev(sp, pr) for pr in prs], "lenient_ctl": [ev(lp, pr) for pr in prs]})
        good = parse_policy(GOOD)
        for i in range(N_VALID):
            rows.append({"arm": "valid", "seed": s, "returned": ev(good, probe(rng))})
        ex = parse_policy(EXAMPLE_POLICY)
        rows.append({"arm": "example", "seed": s, "example_flag": EXAMPLE_POLICY.get("example"), "example_tenants": sorted(ex.grants)})
        rows += holds_rows(s)
    # the experiment's own summary (verify/e6.py recomputes independently)
    mal = [r for r in rows if r["arm"] == "malformed"]
    res = {"empty_allows": sum(v[0] for r in rows if r["arm"] == "empty" for v in r["returned"].values()),
           "malformed": len(mal), "parse_errors": sum(r["parse"] == "PolicyError" for r in mal),
           "safe_allows": sum(x[0] for r in mal for x in r["safe"]), "lenient_allow_rate": sum(x[0] for r in mal for x in r["lenient_ctl"]) / max(1, N_PROBES * len(mal)),
           "valid_allow_rate": sum(r["returned"][0] for r in rows if r["arm"] == "valid") / max(1, sum(r["arm"] == "valid" for r in rows))}
    hp = {r["case"]: r["returned"] for r in rows if r["arm"] == "hold_pdp" and r["seed"] == sd[0]}
    es = next(r for r in rows if r["arm"] == "erase_service")
    holds_ok = (hp["recorded_hold"][1] == "erasure_queued" and hp["unrecorded_hold"][0] and hp["no_hold"][0] and hp["hold_event_removed"][0]
                and not hp["other_tenant"][0] and es["admin"] == {"erased": 2, "queued": 1, "denied": 0, "refused": None, "result": "incomplete"}
                and es["after"] == ["alice-about-dana-held", "bob-about-erin"])
    ok = (res["empty_allows"] == 0 and res["safe_allows"] == 0 and res["parse_errors"] == res["malformed"] and res["malformed"] >= 3000
          and res["valid_allow_rate"] > 0.05 and holds_ok and EXAMPLE_POLICY.get("example") is True)
    ctl = res["lenient_allow_rate"] >= 0.10
    params = {"seeds": sd, "malformed_per_seed": N_MALFORMED, "probes_per_doc": N_PROBES, "empty_per_seed": N_EMPTY, "valid_per_seed": N_VALID,
              "lenient_patch": {"module": "pol.policy", "old": LENIENT[0], "new": LENIENT[1]}, "good_doc": GOOD}
    fn, h = write_items("e6_policy_format", rows)
    size = os.path.getsize(os.path.join(common.RESULTS, fn))
    data = {"verdict": "PASS" if ok and ctl else "FAIL", "results": res, "holds_ok": holds_ok, "hold_pdp": hp, "erase_service": es,
            "pass_conditions_met": ok, "control_meets_floor": ctl, "items": {"file": fn, "sha256": h, "bytes": size}, "runtime_s": round(time.time() - t0, 1),
            "licence_note": "no Elasticsearch feature is used: enforcement is application-side; nothing here needs more than Basic",
            "not_proven": "format covers the dimensions this lab models; the client's real policies are not shipped; the example is illustrative only"}
    write_result("e6_policy_format", params, data)
    print(data["verdict"], res, "holds_ok", holds_ok, "artifact_bytes", size, "runtime_s", data["runtime_s"])


if __name__ == "__main__":
    main()

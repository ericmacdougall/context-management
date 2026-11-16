"""E6: client-supplied policy format. An empty, absent or malformed policy denies everything; example policy is marked as an example;
legal hold beats erasure only under a recorded hold event (queued, not refused for good); erasure scope follows the subjects label.
Control: a lenient loader that skips bad parts and a policy-less evaluator that allows when nothing forbids.
usage: python -m exp.e6_policy_format"""
import copy
import random

from exp import common  # noqa: F401
from exp.common import write_result
from exp.gen import rand_label, rand_principal, rand_probe, base_policy
from pol.labels import Label
from pol.pdp import ACTIONS, DENY_ALL, EXAMPLE_POLICY, Policy, PolicyError, Principal, decide, parse_policy, safe_policy

SEED = 606
N_MALFORMED = 3000
N_PROBES = 5


def mutate_doc(rng, doc):
    d = copy.deepcopy(doc)
    k = rng.choice(["unknown_key", "version", "grants_type", "bad_action", "dest_kind", "dest_pin", "none", "list", "string", "dest_local", "auth_keys", "banned_type", "cn", "drop_version"])
    if k == "unknown_key":
        d["allow_all"] = True
    elif k == "version":
        d["version"] = rng.choice([0, -1, "1", None, True, 1.5])
    elif k == "grants_type":
        d["grants"] = rng.choice([[], "x", 5, {"t1": "send_model"}, {"t1": [1]}, {5: ["read"]}])
    elif k == "bad_action":
        d["grants"] = {"t1": ["read", "root"]}
    elif k == "dest_kind":
        d["destinations"]["m"]["kind"] = rng.choice(["x", None, 3])
    elif k == "dest_pin":
        d["destinations"]["m"].pop("model", None)
    elif k == "dest_local":
        d["destinations"]["m"]["local"] = rng.choice(["yes", 1, None])
    elif k == "auth_keys":
        d["authorizations"] = {"a": {"tenant": "t1"}}
    elif k == "banned_type":
        d["banned_models"] = rng.choice(["m1", 5, [1], {"a": 1}])
    elif k == "cn":
        d["conflict_notice"] = rng.choice(["full", None, 1])
    elif k == "drop_version":
        d.pop("version")
    elif k == "none":
        return None
    elif k == "list":
        return []
    else:
        return "policy"
    return d


def lenient_load(doc):
    """CONTROL: skips what it cannot parse and keeps going; defaults to allow-all grants when grants is missing or broken."""
    try:
        v = doc.get("version", 1) if isinstance(doc, dict) else 1
    except Exception:       # noqa: BLE001
        v = 1
    g = {}
    try:
        g = {t: frozenset(a) for t, a in doc["grants"].items()}
    except Exception:       # noqa: BLE001
        g = {"t1": frozenset(ACTIONS), "t2": frozenset(ACTIONS)}
    return Policy(v if isinstance(v, int) else 1, grants=g)


def probes(rng, pol, n):
    out = []
    for _ in range(n):
        p = rand_principal(rng)
        a, ctx = rand_probe(rng, base_policy())
        out.append((p, a, rand_label(rng, 0.9), ctx))
    return out


def main():
    rng = random.Random(SEED)
    res = {"empty_policy_allows": 0, "empty_probes": 0, "deny_all_allows": 0, "malformed_docs": 0, "malformed_parse_errors": 0, "malformed_safe_allows": 0,
           "malformed_safe_probes": 0, "lenient_allows": 0, "lenient_probes": 0, "valid_doc_allows": 0, "valid_doc_probes": 0}
    good_doc = {"version": 3, "grants": {"t1": list(ACTIONS), "t2": list(ACTIONS)},
                "destinations": {"m": {"kind": "model", "region": "EU", "local": True, "max_sensitivity": 3, "model": "m1"}},
                "authorizations": {"a": {"tenant": "t1", "programme": "x", "expires": 5}}, "banned_models": ["m9"], "hold_events": ["h1"]}
    parse_policy(good_doc)
    ex = parse_policy(EXAMPLE_POLICY)          # the example parses, and is marked as an example, but grants only its own demo tenant
    for pr, a, lab, ctx in probes(rng, None, 3000):
        res["empty_probes"] += 1
        res["empty_policy_allows"] += decide(pr, a, lab, ctx, policy=Policy(1)).allow
        res["deny_all_allows"] += decide(pr, a, lab, ctx, policy=DENY_ALL).allow
        res["empty_policy_allows"] += decide(pr, a, lab, ctx, policy=safe_policy({})).allow
        res["empty_policy_allows"] += decide(pr, a, lab, ctx, policy=safe_policy(None)).allow
    for i in range(N_MALFORMED):
        bad = mutate_doc(rng, good_doc)
        res["malformed_docs"] += 1
        try:
            parse_policy(bad)
        except PolicyError:
            res["malformed_parse_errors"] += 1
        except Exception:       # noqa: BLE001
            res["malformed_parse_errors"] += 0       # any non-PolicyError escape from the validator is a validator bug: counted as a miss
        sp, lp = safe_policy(bad), lenient_load(bad)
        for pr, a, lab, ctx in probes(rng, None, N_PROBES):
            res["malformed_safe_probes"] += 1
            res["malformed_safe_allows"] += decide(pr, a, lab, ctx, policy=sp).allow
            res["lenient_probes"] += 1
            res["lenient_allows"] += decide(pr, a, lab, ctx, policy=lp).allow
    for pr, a, lab, ctx in probes(rng, None, 3000):
        res["valid_doc_probes"] += 1
        res["valid_doc_allows"] += decide(pr, a, lab, ctx, policy=parse_policy(good_doc)).allow
    # legal hold precedence and erasure scope
    pol = base_policy()
    hold_lab = Label(["t1"], holds=["h1"], subjects=["s1"])
    unrecorded = Label(["t1"], holds=["hX"], subjects=["s1"])
    p = Principal("p", "t1", frozenset(), 3, True)
    hold = {
        "recorded_hold_blocks_erase": decide(p, "erase", hold_lab, {}, policy=pol).reason == "legal_hold_erasure_queued",
        "unrecorded_hold_does_not_block": decide(p, "erase", unrecorded, {}, policy=pol).allow,
        "no_hold_erases": decide(p, "erase", Label(["t1"], subjects=["s1"]), {}, policy=pol).allow,
        "derived_inherits_hold_and_subject": (lambda j: j.holds == frozenset(["h1"]) and j.subjects == frozenset(["s1", "s2"]))(
            __import__("pol.labels", fromlist=["join"]).join([hold_lab, Label(["t1"], subjects=["s2"])])),
        "hold_released_when_event_removed": decide(p, "erase", hold_lab, {}, policy=pol.tighten(hold_events=frozenset())).allow,
        "other_tenant_cannot_erase": not decide(Principal("q", "t2", frozenset(), 3, True), "erase", hold_lab, {}, policy=pol).allow,
    }
    ok = (res["empty_policy_allows"] == 0 and res["deny_all_allows"] == 0 and res["malformed_safe_allows"] == 0 and res["malformed_parse_errors"] == res["malformed_docs"]
          and res["valid_doc_allows"] > 0.05 * res["valid_doc_probes"] and all(hold.values()) and EXAMPLE_POLICY.get("example") is True)
    ctl = res["lenient_allows"] >= 0.10 * res["lenient_probes"]
    verdict = "PASS" if ok and ctl else "FAIL"
    data = {"verdict": verdict, "results": res, "hold_and_subject_checks": hold, "pass_conditions_met": ok, "control_lenient_loader_allows_rate": res["lenient_allows"] / res["lenient_probes"],
            "control_meets_floor": ctl, "licence_note": "no Elasticsearch feature is used: enforcement is application-side; nothing here needs more than Basic",
            "not_proven": "format covers the dimensions this lab models; the client's real policies are not shipped (they supply them); example policy is illustrative only"}
    write_result("e6_policy_format", {"seed": SEED, "malformed": N_MALFORMED}, data)
    print(verdict, res, hold)


if __name__ == "__main__":
    main()

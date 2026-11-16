"""E3: identity attributes cannot be overwritten by credential or derived attributes; any non-boolean or erroring evaluation denies.
Control: a naive evaluator (merge with credential last, truthiness, errors swallowed to continue).
usage: python -m exp.e3_clash"""
import math
import random
import types

from exp import common  # noqa: F401
from exp.common import write_result
from pol.labels import Label, acl_satisfied, join
from pol.pdp import Dest, Policy, Principal, decide

SEED = 77
N_PLANTED, N_FAULT, N_LEGIT = 2200, 2400, 1400


class TruthyBomb:
    def __bool__(self):
        raise RuntimeError("bool bomb")


class Truthy:
    def __bool__(self):
        return True

    def __eq__(self, o):
        return True


class Weird(BaseException):
    pass


def _raise_exc(*a):
    raise ValueError("rule error")


def _raise_weird(*a):
    raise Weird()


FAULT_RESULTS = [lambda *a: "yes", lambda *a: 1, lambda *a: 2, lambda *a: None, lambda *a: [], lambda *a: {}, lambda *a: "True",
                 lambda *a: float("nan"), lambda *a: Truthy(), lambda *a: TruthyBomb(), lambda *a: NotImplemented, _raise_exc, _raise_weird,
                 lambda *a: b"\xff\xfe", lambda *a: (True,), lambda *a: [True]]


def mk_policy(extra=(), revoked=()):
    return Policy(1, grants={"t1": frozenset(["send_model"])}, destinations={"dm": Dest("model", "EU", True, 3, model="m1")}, extra_rules=tuple(extra), revoked=frozenset(revoked))


def base_case(rng):
    """A request that is allowed on its own merits: tenant t1, group satisfied."""
    p = Principal("p%d" % rng.randrange(10 ** 6), "t1", frozenset(["g1", "g2"]), clearance=2)
    lab = Label(["t1"], acl=[["g1"]], regions=["EU"], sensitivity=1)
    return p, lab, {"destination": "dm", "region": "EU"}


def victim_case(rng, key):
    """A request denied on exactly one ground, the one that overriding attribute `key` would remove."""
    p = Principal("p%d" % rng.randrange(10 ** 6), "t1", frozenset(["g1"]), clearance=2)
    lab = Label(["t1"], acl=[["g1"]], regions=["EU"], sensitivity=1)
    if key == "tenant":
        lab = Label(["t2"], regions=["EU"])
    elif key == "groups":
        lab = Label(["t1"], acl=[["g9"]], regions=["EU"])
    elif key == "clearance":
        lab = Label(["t1"], regions=["EU"], sensitivity=3)
    elif key == "restricted_ok":
        lab = Label(["t1"], regions=["EU"], restricted=True)
    return p, lab, {"destination": "dm", "region": "EU"}


KEYS = ["tenant", "groups", "clearance", "restricted_ok", "id", "subject.identifier"]


def planted_cases(rng, n):
    out = []
    for i in range(n):
        base = rng.choice(KEYS)
        p, lab, ctx = victim_case(rng, base)
        form = rng.choice(["exact", "exact", "exact", "case", "space", "fullwidth", "zerowidth", "dash", "bytes", "int", "tuple", "nested", "derived_exact"])
        val = {"tenant": "t2", "groups": ["g9"], "clearance": 3, "restricted_ok": True, "id": "admin", "subject.identifier": "admin"}[base]
        if form in ("exact", "derived_exact"):
            k = base
        elif form == "case":
            k = base.upper() if base != "id" else "Id"
        elif form == "space":
            k = " " + base + " "
        elif form == "fullwidth":
            k = "".join(chr(ord(c) + 0xFEE0) if c.isalpha() else c for c in base)
        elif form == "zerowidth":
            k = base[:1] + "​" + base[1:]
        elif form == "dash":
            k = base.replace(".", "-").replace("_", "-")
        elif form == "bytes":
            k = base.encode()
        elif form == "int":
            k = 7
        elif form == "tuple":
            k = (base,)
        else:
            k = "claims"
            val = {base: val}
        src = "derived" if form == "derived_exact" else rng.choice(["credential", "credential", "derived"])
        ctx[src] = {k: val}
        out.append((p, lab, ctx, {"form": form, "key": base}))
    return out


def fault_cases(rng, n):
    out = []
    for i in range(n):
        p, lab, ctx = base_case(rng)
        kind = rng.choice(["rule", "rule", "rule", "label", "principal", "action", "context", "policy"])
        pol_extra = ()
        args = [p, "send_model", lab, ctx]
        if kind == "rule":
            fn = rng.choice(FAULT_RESULTS)
            pol_extra = [("fuzz", fn)]
        elif kind == "label":
            bad = rng.choice([types.SimpleNamespace(tenants="t1", acl=1, regions="EU", sensitivity=True),
                              types.SimpleNamespace(), None, "label", {"tenants": ["t1"]}, [], 5])
            args[2] = bad
        elif kind == "principal":
            args[0] = rng.choice([types.SimpleNamespace(id="x", tenant="t1"), None, "p", {"id": "p"},
                                  Principal("p", "t1", frozenset(["g1"]), clearance="3"), Principal("p", None, frozenset()),
                                  Principal("p", "t1", ["g1"], clearance=1), Principal("", "t1")])
        elif kind == "action":
            args[1] = rng.choice(["", None, "SEND_MODEL", "send_model ", 5, ["send_model"], "exfiltrate"])
        elif kind == "context":
            args[3] = rng.choice([None, [], "ctx", 5, {"destination": ["dm"]}, {"destination": None}, {"destination": "dm", "credential": "x"},
                                  {"destination": "dm", "credential": [("tenant", "t2")]}])
        elif kind == "policy":
            args.append(None)
        out.append((args, pol_extra, kind))
    return out


def legit_cases(rng, n):
    out = []
    for i in range(n):
        p, lab, ctx = base_case(rng)
        ctx["credential"] = rng.choice([{"email": "a@b", "exp": 1700000000}, {"cap_clearance": 3, "cap_actions": ["send_model", "read"]}, {"roles": ["x"], "tenant_name": "Acme"},
                                        {}, {"cap_restricted": True, "nested": {"tenant": "t2"}}])
        ctx["derived"] = rng.choice([None, {"request_id": "r1"}, {"trace": {"id": "abc"}}])
        out.append((p, lab, ctx))
    return out


def naive_decide(principal, action, label, ctx, policy):
    """The control. Credential and derived attributes merged over identity; truthiness for rules; errors skip the check."""
    def attempt(fn):
        try:
            return fn()
        except BaseException as e:      # noqa: BLE001
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            return True                 # swallowed: the check is skipped
    attrs = {}
    attempt(lambda: attrs.update({"tenant": principal.tenant, "groups": principal.groups, "clearance": principal.clearance,
                                  "restricted_ok": principal.restricted_ok, "id": principal.id, "subject.identifier": principal.id}))
    try:
        attrs.update(ctx.get("credential") or {})
        attrs.update(ctx.get("derived") or {})
    except BaseException:       # noqa: BLE001
        pass
    checks = [lambda: label.tenants == frozenset([attrs["tenant"]]),
              lambda: acl_satisfied(label, attrs["groups"]),
              lambda: attrs["clearance"] >= label.sensitivity,
              lambda: (not label.restricted) or attrs["restricted_ok"]]
    for c in checks:
        if attempt(c) is False:
            return False
    for _n, fn in getattr(policy, "extra_rules", ()):
        r = attempt(lambda: bool(fn(principal, action, label, ctx)))
        if not r:
            return False
    return True


def run_one(case_kind, args, pol_extra, fn):
    pol = mk_policy(pol_extra)
    if len(args) == 5:
        pol = args[4]
    p, a, l, c = args[:4]
    return fn(p, a, l, c, pol)


def main():
    rng = random.Random(SEED)
    planted = planted_cases(rng, N_PLANTED)
    faults = fault_cases(rng, N_FAULT)
    legit = legit_cases(rng, N_LEGIT)
    res = {"planted": {"n": 0, "ours_allow": 0, "naive_allow": 0, "reasons": {}, "by_form_naive_allow": {}},
           "fault": {"n": 0, "ours_allow": 0, "naive_allow": 0, "by_kind": {}, "reasons": {}},
           "legit": {"n": 0, "ours_allow": 0, "naive_allow": 0, "misses": []}}
    wrong_reason = []
    for p, lab, ctx, meta in planted:
        pol = mk_policy(revoked=[p.id] if meta["key"] in ("id", "subject.identifier") else ())   # identity overrides: the only deny ground is revocation of this id
        d = decide(p, "send_model", lab, ctx, policy=pol)
        nv = naive_decide(p, "send_model", lab, ctx, pol)
        r = res["planted"]
        r["n"] += 1
        r["ours_allow"] += d.allow
        r["naive_allow"] += nv
        r["reasons"][d.reason] = r["reasons"].get(d.reason, 0) + 1
        if nv:
            r["by_form_naive_allow"][meta["form"]] = r["by_form_naive_allow"].get(meta["form"], 0) + 1
        if meta["form"] != "nested" and d.reason not in ("attr_clash", "attr_key_type"):
            wrong_reason.append(meta)          # identity-like and odd keys must be refused as a clash, not merely by luck of another rule
    for args, extra, kind in faults:
        pol = mk_policy(extra)
        a = list(args)
        if len(a) == 5:
            pol_used = a[4]
        else:
            pol_used = pol
        d = decide(a[0], a[1], a[2], a[3], policy=pol_used)
        try:
            nv = naive_decide(a[0], a[1], a[2], a[3], pol_used)
        except BaseException:      # noqa: BLE001
            nv = True
        r = res["fault"]
        r["n"] += 1
        r["ours_allow"] += d.allow
        r["naive_allow"] += bool(nv)
        r["by_kind"][kind] = r["by_kind"].get(kind, 0) + 1
        r["reasons"][d.reason] = r["reasons"].get(d.reason, 0) + 1
    for p, lab, ctx in legit:
        pol = mk_policy()
        d = decide(p, "send_model", lab, ctx, policy=pol)
        r = res["legit"]
        r["n"] += 1
        r["ours_allow"] += d.allow
        r["naive_allow"] += naive_decide(p, "send_model", lab, ctx, pol)
        if not d.allow and len(r["misses"]) < 10:
            r["misses"].append(d.reason)
    ok = (res["planted"]["ours_allow"] == 0 and res["fault"]["ours_allow"] == 0 and not wrong_reason
          and res["legit"]["ours_allow"] >= 0.99 * res["legit"]["n"]
          and res["planted"]["n"] >= 2000 and res["fault"]["n"] >= 2000 and res["legit"]["n"] >= 1000)
    ctl = (res["planted"]["naive_allow"] >= 0.10 * res["planted"]["n"] and res["fault"]["naive_allow"] >= 0.10 * res["fault"]["n"])
    verdict = "PASS" if ok and ctl else "FAIL"
    data = {"verdict": verdict, "results": res, "wrong_reason_count": len(wrong_reason), "wrong_reason_sample": wrong_reason[:5], "pass_conditions_met": ok,
            "control_meets_floor": ctl, "control_reason": "naive merge lets credential attrs override identity; truthiness + swallowed errors allow faults",
            "not_proven": "only the credential and derived attribute channels are fuzzed; extra_rules are caller code and only their return value is constrained"}
    write_result("e3_clash", {"seed": SEED, "planted": N_PLANTED, "fault": N_FAULT, "legit": N_LEGIT}, data)
    print(verdict, {k: {a: b for a, b in v.items() if a in ("n", "ours_allow", "naive_allow")} for k, v in res.items()}, "wrong_reason", len(wrong_reason))


if __name__ == "__main__":
    main()

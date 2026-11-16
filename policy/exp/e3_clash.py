"""E3: identity cannot be overridden by anything a caller sends; non-boolean or erroring evaluation denies; forged sessions, requests and
decision bodies are refused at every public entry point a request handler can reach.

Arms (enforced = production code; ctl = broken implementations built as source variants, never formulas):
  planted   identity/tenant overrides in the credential or as top-level request keys (case, space, NFKC, zero-width, bytes, nested ...)
  fault     rule results that are not exactly True, rules that raise, malformed label / principal / action / request / policy
  legit     benign requests with allowed narrowing credentials (must be allowed >= 99 percent: deny-all cannot pass)
  context   forged region, clock, recipient object, extra top-level keys, misspelt narrowing keys (amendment 2 arm)
  forge     derived from the public-surface enumeration (tests/surface.py walk): every public method whose first parameter is `session`
            gets every session forgery; Outbox.enqueue gets request forgeries; DecisionService.submit gets body forgeries.
Control for planted/fault/context: NAIVE_PDP, a multi-patch variant of pol/_pdp.py (credential and request keys merged over the
principal, unknown keys ignored, truthiness for rules, errors swallowed as allow, request clock and recipient trusted). Controls for the
forge arm: the same naive PDP swapped in behind the Enforcer (request rows), pol.enforce without the session identity check (session
rows), and a pol.decisions variant that trusts identity keys in the body (body rows; exp.patches m1_body_identity alone is masked
by the BODY_KEYS check, so this control also widens BODY_KEYS and lets body keys win over the session stamp).
Rows record what was sent and what came back (raw); verify/e3.py decides acceptance from those observations.
usage: python -m exp.e3_clash"""
import inspect
import json
import os
import random
import sys
import time
import types

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.patches import ROOT
from exp.seeds import seeds
from pol import _pdp
from pol import enforce as ENF
from pol.decisions import DecisionService
from pol.directory import Principal, Session
from pol.labels import Label
from pol.policy import ACTIONS, Dest, Policy, parse_policy

N_PLANTED, N_FAULT, N_LEGIT, N_CONTEXT = [int(x) for x in os.environ.get("E3_SIZES", "2200,2400,1400,3000").split(",")]
FORGE_ROUNDS = int(os.environ.get("E3_FORGE_ROUNDS", "10"))
NOW = 10.0
APPLIED = ("proposed", "pending_created", "confirmed_applied", "decided_accept", "decided_reject", "decided_amend", "superseded", "retracted",
           "contested_opened", "contested_joined", "concurrent_agree", "resolved", "revoked", "granted")

NAIVE_PATCHES = [
    ("        if not isinstance(k, str) or not k.isascii() or k not in CREDENTIAL_KEYS:\n            _deny(\"attr_unrecognised_key\")\n",
     "        pass\n"),
    ("    if not isinstance(request, dict) or set(request) - REQUEST_KEYS:\n", "    if not isinstance(request, dict):\n"),
    ("    t = principal.tenant\n",
     "    _over = {}\n"
     "    for _src in (request, request.get(\"credential\")):\n"
     "        if isinstance(_src, dict):\n"
     "            _over.update({_k: _v for _k, _v in _src.items() if isinstance(_k, str)})\n"
     "    import types as _types\n"
     "    principal = _types.SimpleNamespace(**dict(principal.__dict__, **{_k: _v for _k, _v in _over.items() if _k in principal.__dict__}))\n"
     "    t = principal.tenant\n"),
    ("or auth.expires <= now)", "or auth.expires <= request.get(\"now\", now))"),
    ("        rcp = recipient\n", "        rcp = request.get(\"recipient\", recipient)\n"),
    ("        if fn(principal, action, label, request) is not True:\n",
     "        try:\n            _r = bool(fn(principal, action, label, request))\n        except BaseException:\n            _r = True\n        if not _r:\n"),
    ("        return Decision(False, \"label_error\", ver, act)", "        return Decision(True, \"label_error_swallowed\", ver, act)"),
    ("        return Decision(False, \"eval_error:\" + type(e).__name__, ver, act)",
     "        return Decision(True, \"eval_error_swallowed:\" + type(e).__name__, ver, act)"),
]
BODY_TRUST_PATCHES = [
    ("        if IDENTITY_KEYS & set(body):\n            return self._refuse(p.id, \"identity_in_body\", body)\n", ""),
    ("        if set(body) - BODY_KEYS:\n", "        if set(body) - BODY_KEYS - IDENTITY_KEYS:\n"),
    ("        ev.update(id=p.id + \"/\" + cid, tenant=session.tenant, principal=p.id, channel=session.channel)\n",
     "        ev.update({k: v for k, v in dict(id=p.id + \"/\" + cid, tenant=session.tenant, principal=p.id, channel=session.channel).items() if k not in body})\n"),
]
NO_SESSION_VERIFY = ("pol.enforce", "        if not self._sessions.verify(session):\n", "        if False:\n")


def multi_variant(module_name, patches, tag):
    """Like exp.patches.variant, with several fragments; each must occur exactly once in the production source."""
    path = os.path.join(ROOT, *module_name.split(".")) + ".py"
    src = open(path, encoding="utf-8").read()
    for old, new in patches:
        if src.count(old) != 1:
            raise AssertionError("patch %s: %r occurs %d times" % (tag, old[:60], src.count(old)))
        src = src.replace(old, new)
    mod = types.ModuleType(module_name + "__ctl_" + tag)
    mod.__file__ = path + "#" + tag
    mod.__package__ = module_name.rsplit(".", 1)[0]
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


NAIVE = multi_variant("pol._pdp", NAIVE_PATCHES, "e3_naive_pdp")
BODY_TRUST = None          # built after BODY_TRUST_PATCHES is defined (below)


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
FAULT_NAMES = ["str_yes", "int_1", "int_2", "none", "list", "dict", "str_True", "nan", "truthy_obj", "bool_bomb", "notimpl", "raise_value",
               "raise_base", "bytes", "tuple_true", "list_true"]


def P(pid, tenant="t1", groups=("g1",), clearance=2, restricted_ok=False, region="eu", kind="human", model=None):
    return Principal(pid, tenant, frozenset(groups), clearance, restricted_ok, kind, region, model)


def mk_policy(extra=(), revoked=()):
    return Policy(1, grants={"t1": frozenset(ACTIONS)}, models={"m1@1": {"family": "m1", "version": "1"}},          # R3-6: pinned id
                  destinations={"dm": Dest("model", "eu", True, 3, "m1@1", "cyber", ["t1"])}, extra_rules=tuple(extra), revoked=frozenset(revoked),
                  authorizations={"a": {"tenant": "t1", "programme": "cyber", "expires": 5, "principal": "pq"}})


def victim_case(rng, key):
    """A request denied on exactly one ground, the one that overriding attribute `key` would remove."""
    p = P("p%d" % rng.randrange(10 ** 6))
    lab = Label(["t1"], acl=[["g1"]], regions=["eu"], sensitivity=1)
    if key == "tenant":
        lab = Label(["t2"], regions=["eu"])
    elif key == "groups":
        lab = Label(["t1"], acl=[["g9"]], regions=["eu"])
    elif key == "clearance":
        lab = Label(["t1"], regions=["eu"], sensitivity=3)
    elif key == "restricted_ok":
        lab = Label(["t1"], regions=["eu"], acl=[["g1"]], restricted=True)
    elif key == "region":
        lab = Label(["t1"], regions=["us"])
    return p, lab


KEYS = ["tenant", "groups", "clearance", "restricted_ok", "id", "region"]
VALS = {"tenant": "t2", "groups": ["g9"], "clearance": 3, "restricted_ok": True, "id": "admin", "region": "us"}


def keyform(rng, base):
    form = rng.choice(["exact", "exact", "exact", "case", "space", "fullwidth", "zerowidth", "dash", "bytes", "int", "tuple", "nested"])
    k = {"exact": base, "case": base.upper(), "space": " " + base + " ", "zerowidth": base[:1] + "\u200b" + base[1:],
         "fullwidth": "".join(chr(ord(c) + 0xFEE0) if c.isalpha() else c for c in base), "dash": base.replace("_", "-"),
         "bytes": base.encode(), "int": 7, "tuple": (base,), "nested": "claims"}[form]
    return form, k


def rr(x):
    return repr(x)[:200]


def call(fn, *a):
    try:
        d = fn(*a)
        return {"allow": bool(d.allow) if isinstance(d.allow, bool) else repr(d.allow), "reason": d.reason}
    except BaseException as e:      # noqa: BLE001  an escaping exception is recorded raw
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return {"allow": "raised", "reason": type(e).__name__}


def pdp_arms(rng, rows):
    for i in range(N_PLANTED):
        key = rng.choice(KEYS)
        p, lab = victim_case(rng, key)
        form, k = keyform(rng, key)
        val = {key: VALS[key]} if form == "nested" else VALS[key]
        chan = rng.choice(["credential", "credential", "request"])
        req = {"destination": "dm"}
        if chan == "credential":
            req["credential"] = {k: val}
        else:
            req[k] = val
        pol = mk_policy(revoked=[p.id] if key == "id" else ())
        for arm, fn in (("enforced", _pdp.evaluate), ("ctl_naive_pdp", NAIVE.evaluate)):
            rows.append({"arm": arm, "cat": "planted", "key": key, "form": form, "channel": chan, "sent": rr(req),
                         "returned": call(fn, p, "send_model", [lab], dict(req), pol, NOW, None)})
    for i in range(N_FAULT):
        p, lab = P("p%d" % rng.randrange(10 ** 6)), Label(["t1"], acl=[["g1"]], regions=["eu"], sensitivity=1)
        kind = rng.choice(["rule", "rule", "rule", "label", "principal", "action", "request", "policy"])
        args = [p, "send_model", [lab], {"destination": "dm"}, mk_policy(), NOW, None]
        detail = None
        if kind == "rule":
            j = rng.randrange(len(FAULT_RESULTS))
            args[4], detail = mk_policy([("fuzz", FAULT_RESULTS[j])]), FAULT_NAMES[j]
        elif kind == "label":
            j = rng.randrange(6)
            args[2], detail = [[types.SimpleNamespace(tenants="t1"), None, "label", {"tenants": ["t1"]}, [], 5][j]], "label_%d" % j
        elif kind == "principal":
            j = rng.randrange(5)
            args[0], detail = [types.SimpleNamespace(id="x", tenant="t1", groups=["g1"], clearance=3, restricted_ok=True, region="eu"), None, "p",
                               {"id": "p"}, 7][j], "principal_%d" % j
        elif kind == "action":
            j = rng.randrange(7)
            args[1], detail = ["", None, "SEND_MODEL", "send_model ", 5, ["send_model"], "exfiltrate"][j], "action_%d" % j
        elif kind == "request":
            j = rng.randrange(6)
            args[3], detail = [None, [], "ctx", 5, {"destination": ["dm"]}, {"destination": "dm", "credential": "x"}][j], "request_%d" % j
        else:
            j = rng.randrange(3)
            args[4], detail = [None, {"version": 1}, "policy"][j], "policy_%d" % j
        for arm, fn in (("enforced", _pdp.evaluate), ("ctl_naive_pdp", NAIVE.evaluate)):
            rows.append({"arm": arm, "cat": "fault", "kind": kind, "detail": detail, "returned": call(fn, *args)})
    for i in range(N_LEGIT):
        p, lab = P("p%d" % rng.randrange(10 ** 6), groups=("g1", "g2")), Label(["t1"], acl=[["g1"]], regions=["eu"], sensitivity=1)
        cred = rng.choice([None, {}, {"cap_clearance": 3}, {"cap_actions": ["send_model", "read"]}, {"cap_restricted": True},
                           {"cap_clearance": 2, "cap_actions": ["send_model"], "cap_restricted": False}])
        req = {"destination": "dm"}
        if cred is not None:
            req["credential"] = cred
        rows.append({"arm": "enforced", "cat": "legit", "sent": rr(req), "returned": call(_pdp.evaluate, p, "send_model", [lab], req, mk_policy(), NOW, None)})
    variants = ["CAP_ACTIONS", "cap-actions", "Cap_Actions", "cap_actions\u200b", " cap_actions", "cap.actions", "capactions", "cap_scope", "cаp_actions"]
    for i in range(N_CONTEXT):
        us = P("pq", groups=("g1",), clearance=3, restricted_ok=True, region="us")
        kind = rng.choice(["region", "now", "recipient", "key_variant", "extra_key", "principal_override"])
        pol = mk_policy()
        if kind == "region":
            a = ("read", [Label(["t1"], regions=["eu"])], {"region": "eu"}, None)
        elif kind == "now":
            a = ("send_model", [Label(["t1"], restricted=True)], {"destination": "dm", "authorization": "a", "now": 1.0}, None)
        elif kind == "recipient":
            a = ("send_inbox", [Label(["t1"], acl=[["fin"]])], {"recipient": P("c", groups=("fin",), clearance=3)}, None)
        elif kind == "key_variant":
            a = ("send_model", [Label(["t1"])], {"destination": "dm", "credential": {rng.choice(variants): ["read"]}}, None)
        elif kind == "extra_key":
            a = ("read", [Label(["t1"])], {rng.choice(["region", "now", "principal", "tenant", "groups", "recipient", "authorization_override", "x"]): "eu"}, None)
        else:
            a = ("read", [Label(["t1"], acl=[["g9"]])], {"principal": {"groups": ["g9"]}}, None)
        for arm, fn in (("enforced", _pdp.evaluate), ("ctl_naive_pdp", NAIVE.evaluate)):
            rows.append({"arm": arm, "cat": "context", "kind": kind, "sent": rr(a[2]),
                         "returned": call(fn, us, a[0], a[1], dict(a[2]), pol, NOW, a[3])})


# ------------------------------------------------------------------------------------------------ forge arm (surface-derived)
def surface_entries():
    """Every public class method of `pol` whose first parameter is `session` (request-handler entry points), from the surface walk."""
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from tests.surface import class_methods, exported, public_modules      # the public API of tests/surface since 7080a22
    out, others = [], []
    for mod, name, _path in public_modules("pol"):
        if mod is None:
            others.append(name + " (AST-only: driver-specific module, reached through DecisionService)")
    for where, obj in exported("pol"):
        if not inspect.isclass(obj):
            continue
        for mn, f, _static in class_methods(obj, "pol"):                  # inherited public methods count too
            ps = [p for p in inspect.signature(f).parameters if p not in ("self", "cls")]
            if ps and ps[0] == "session":
                out.append({"entry": "%s.%s" % (where, mn), "params": ps})
            else:
                others.append("%s.%s(%s)" % (where, mn, ",".join(ps)))
    return out, others


class Stub:
    def __init__(self):
        self.payloads = []

    def complete(self, msgs, system=None):
        self.payloads.append(json.dumps(msgs) + str(system))
        return types.SimpleNamespace(text="answer", model="m1@1")

    def embed(self, texts, kind):
        self.payloads.append(json.dumps(texts))
        return [[0.0] for _ in texts]


def build_world(E, pdp_eval=None, decisions_mod=None):
    """A wired deployment from enforcement module E (production or a variant). Victim: alice. Attacker: bob (same tenant), eve (tenant-b)."""
    from tests.kit import World, P as KP, policy_doc
    w = World(principals=[KP("alice", groups=("staff", "alice-only")), KP("bob"), KP("carol", clearance=0, groups=("other",)),
                          KP("bot", kind="agent", model="local"), KP("ann", kind="analyst"), KP("admin"), KP("eve", tenant="tenant-b")],
              policy=parse_policy(policy_doc()))
    w.audit = E.AuditLog()
    w.enf = E.Enforcer(lambda: w.pol, w.dir, w.sessions, w.audit)
    w.store = E.Store(w.enf)
    w.E = E
    if pdp_eval is not None:
        E._evaluate = pdp_eval
    if E is ENF:
        DS = (decisions_mod or __import__("pol.decisions", fromlist=["DecisionService"])).DecisionService
        w.svc = DS(w.enf, {"tenant-a": w.engine}, w.vault)
    else:
        w.svc = None
    alice = w.s("alice")
    secret = Label(["tenant-a"], acl=[["alice-only"]], sensitivity=2, subjects=["dana"])
    w.marker = "MARK-%d" % random.randrange(10 ** 9)
    w.rid = w.store.append(alice, w.marker, secret)
    w.secret = secret
    if w.svc is not None:
        r = w.svc.submit(alice, {"type": "supersede", "client_id": "s0", "idem": "s0", "req": "r2", "value": w.marker, "label": secret.to_dict(),
                                 "based_on": 0})
        assert r["code"] == "superseded", r
    w.stub, w.exported = Stub(), []
    w.chat = E.GuardedChat(w.stub, w.enf, w.store, "local")
    w.emb = E.GuardedEmbedder(w.stub, w.enf, w.store, "emb")
    w.exp = E.GuardedExport(lambda d, t, x: w.exported.append(x), w.enf, w.store, "exp")
    w.outbox = E.Outbox(w.enf, w.store, "send_inbox", lambda s, r, t, q: w.exported.append("".join(t)))
    return w


def session_forgeries(w):
    a = w.s("alice")
    bob = w.s("bob")
    ended = w.s("alice")
    w.sessions.end(ended)
    out = [("handbuilt", lambda: Session("tenant-a", "alice", "owner_ui", "f" * 32)),
           ("attacker_token_victim_id", lambda: Session("tenant-a", "alice", "owner_ui", bob.token)),
           ("victim_token_copy", lambda: Session(a.tenant, a.principal_id, a.channel, a.token)),
           ("principal_object", lambda: w.dir.lookup("alice")),
           ("none", lambda: None), ("string", lambda: "alice"), ("dict", lambda: {"tenant": "tenant-a", "principal_id": "alice", "token": a.token}),
           ("ended_victim_session", lambda: ended),
           ("attacker_real_session", lambda: bob),
           ("other_tenant_session", lambda: w.s("eve")),
           ("namespace_obj", lambda: types.SimpleNamespace(tenant="tenant-a", principal_id="alice", channel="owner_ui", token=a.token))]
    return out


def drive(w, entry, s):
    """Call one entry point with session s; return the raw observation."""
    E = w.E
    name = entry.split(".", 2)[2]
    try:
        if name == "Store.read":
            r = w.store.read(s, w.rid)
            return {"type": "value", "text": r.text}
        if name == "Store.append":
            return {"type": "value", "id": w.store.append(s, "forged", w.secret)}
        if name == "Store.append_derived":
            rid = w.store.append_derived(s, "forged", [w.rid])
            return {"type": "value", "id": rid}
        if name == "Store.context":
            ok, wh = w.store.context(s, [w.rid], "local")
            return {"type": "value", "ok": ok, "withheld": wh}
        if name == "Store.erase":
            return {"type": "value", "value": w.store.erase(s, w.rid)}
        if name == "GuardedChat.complete":
            n = len(w.stub.payloads)
            w.chat.complete(s, [w.rid])
            return {"type": "value", "payloads": w.stub.payloads[n:]}
        if name == "GuardedEmbedder.embed":
            n = len(w.stub.payloads)
            w.emb.embed(s, [w.rid])
            return {"type": "value", "payloads": w.stub.payloads[n:]}
        if name == "GuardedExport.export":
            n = len(w.exported)
            w.exp.export(s, [w.rid])
            return {"type": "value", "payloads": w.exported[n:]}
        if name == "Outbox.enqueue":
            w.outbox.enqueue(s, [w.rid], {"recipient_id": "alice"})
            return {"type": "value", "value": "queued"}
        if name == "Enforcer.principal_of":
            return {"type": "value", "principal": w.enf.principal_of(s).id}
        if name == "SessionAuthority.verify":
            return {"type": "value", "value": w.sessions.verify(s)}
        if name == "SessionAuthority.end":
            probe = w.s("alice")
            w.sessions.end(s)
            return {"type": "value", "victim_session_still_valid": w.sessions.verify(probe)}
        if name == "DecisionService.submit":
            return {"type": "value", "code": w.svc.submit(s, {"type": "supersede", "client_id": "f%d" % random.randrange(10 ** 9), "idem": "f%d" % random.randrange(10 ** 9),
                                                              "req": "r2", "value": "forged", "label": w.secret.to_dict(), "based_on": 1})["code"]}
        if name == "DecisionService.erase_subject":
            return {"type": "value", "value": w.svc.erase_subject(s, "dana")}
        if name == "DecisionService.explain":
            return {"type": "value", "value": json.dumps(w.svc.explain(s, "r2"))}
        if name == "DecisionService.table":
            return {"type": "value", "value": json.dumps(w.svc.table(s))}
        return {"type": "no_driver"}
    except (E.PolicyDenied, ENF.PolicyDenied) as e:
        return {"type": "refused", "reason": e.decision.reason}
    except Exception as e:      # noqa: BLE001
        return {"type": "exception", "reason": type(e).__name__}


REQ_FORGES = [("tenant", {"tenant": "tenant-b"}), ("principal", {"principal": "alice"}), ("recipient_obj", None), ("now", {"now": 1.0}),
              ("region", {"region": "eu"}), ("cred_tenant", {"credential": {"tenant": "tenant-b"}}), ("cred_clearance", {"credential": {"clearance": 3}}),
              ("cred_groups", {"credential": {"groups": ["alice-only"]}}), ("cred_confusable", {"credential": {"cаp_clearance": 3}}),
              ("cred_case", {"credential": {"CAP_CLEARANCE": 3}}), ("cred_space", {"credential": {"cap_clearance ": 3}}),
              ("cred_zero_width", {"credential": {"cap_\u200bclearance": 3}}), ("viewer", {"viewer": "alice"}), ("trusted", {"trusted": True}),
              ("labels", {"labels": [{"tenants": ["tenant-a"]}]})]


def body_forges(w):
    lab = Label(["tenant-a"]).to_dict()
    base = {"type": "supersede", "req": "r1", "value": "x", "label": lab, "based_on": w.engine._head("r1")["version"]}
    n = random.randrange(10 ** 9)
    return [("body_tenant", dict(base, client_id="b%d" % n, idem="b%d" % n, tenant="tenant-b")),
            ("body_principal", dict(base, client_id="c%d" % n, idem="c%d" % n, principal="alice")),
            ("body_channel", dict(base, client_id="d%d" % n, idem="d%d" % n, channel="owner_cli")),
            ("body_id", dict(base, client_id="e%d" % n, idem="e%d" % n, id="alice/s0")),
            ("body_value_ref", {k: v for k, v in dict(base, client_id="f%d" % n, idem="f%d" % n, value_ref="val-alice").items() if k != "value"}),
            ("body_namespaced_client_id", dict(base, client_id="alice/x%d" % n, idem="g%d" % n)),
            ("body_traversal_client_id", dict(base, client_id="../alice", idem="h%d" % n)),
            ("body_revoke_victim", {"type": "revoke", "client_id": "i%d" % n, "idem": "i%d" % n, "target": "alice"}),
            ("body_grant_self", {"type": "grant", "client_id": "j%d" % n, "idem": "j%d" % n, "req": "r2", "target": "bob", "role": "decide"}),
            ("body_unknown_key", dict(base, client_id="k%d" % n, idem="k%d" % n, sudo=True))]


def forge_arm(rows, entries):
    for rnd in range(FORGE_ROUNDS):
        configs = [("enforced", lambda: build_world(ENF, pdp_eval=_pdp.evaluate)),
                   ("ctl_no_session_verify", lambda: build_world(control_no_session_verify())),
                   ("ctl_naive_pdp", lambda: build_world(ENF, pdp_eval=NAIVE.evaluate)),
                   ("ctl_body_identity_trusted", lambda: build_world(ENF, pdp_eval=_pdp.evaluate, decisions_mod=BODY_TRUST))]
        for arm, mk in configs:
            try:
                if arm in ("enforced", "ctl_no_session_verify"):
                    for e in entries:
                        if e["entry"].startswith("pol.decisions") and arm != "enforced":
                            continue
                        for idx in range(11):
                            w = mk()                                    # a fresh world per forge; the forgery is built in that world
                            fk, fs = session_forgeries(w)[idx]
                            s = fs()
                            rows.append({"arm": arm, "cat": "forge", "round": rnd, "group": "session", "entry": e["entry"], "forge": fk,
                                         "sent": rr(s), "session_principal_id": getattr(s, "principal_id", None) if not isinstance(s, dict) else s.get("principal_id"),
                                         "victim": "alice", "returned": drive(w, e["entry"], s)})
                if arm in ("enforced", "ctl_naive_pdp"):
                    for fk, extra in REQ_FORGES:
                        w = mk()
                        bob = w.s("bob")
                        rid = w.store.append(bob, "bob-note", Label(["tenant-a"]))
                        req = {"recipient_id": "carol"}
                        req.update(extra if extra is not None else {"recipient": w.dir.lookup("alice")})
                        try:
                            w.outbox.enqueue(bob, [rid], dict(req))
                            ret = {"type": "value", "value": "queued"}
                        except (ENF.PolicyDenied,) as ex:
                            ret = {"type": "refused", "reason": ex.decision.reason}
                        rows.append({"arm": arm, "cat": "forge", "round": rnd, "group": "request", "entry": "pol.enforce.Outbox.enqueue", "forge": fk,
                                     "sent": rr(req), "victim": None, "returned": ret})
                if arm in ("enforced", "ctl_body_identity_trusted"):
                    w = mk()
                    for fk, body in body_forges(w):
                        r = w.svc.submit(w.s("bob"), body)
                        rows.append({"arm": arm, "cat": "forge", "round": rnd, "group": "body", "entry": "pol.decisions.DecisionService.submit", "forge": fk,
                                     "sent": rr(body), "victim": None, "returned": {"type": "value", "code": r["code"]}})
            finally:
                ENF.evaluate = _pdp.evaluate


_NSV = []
BODY_TRUST = multi_variant("pol.decisions", BODY_TRUST_PATCHES, "e3_body_identity_trusted")


def control_no_session_verify():
    if not _NSV:
        from exp.patches import variant
        _NSV.append(variant(*NO_SESSION_VERIFY, tag="e3_no_session_verify"))
    return _NSV[0]


def main():
    sd = seeds("e3")
    t0 = time.time()
    rows = []
    for s in sd:
        rng = random.Random("%s|e3" % s)
        random.seed("%s|e3-forge" % s)
        pdp_arms(rng, rows)
        entries, others = surface_entries()
        forge_arm(rows, entries)
    # the experiment's own summary (verify/e3.py recomputes independently)
    def acc(r):
        x = r["returned"]
        if r["cat"] != "forge":
            return x["allow"] is True
        if x["type"] != "value":
            return False
        return ("code" in x and x["code"] in APPLIED) or x.get("value") in ("queued", "erased") or (x.get("value") is True and r.get("session_principal_id") == "alice") or x.get("principal") == "alice" \
            or x.get("victim_session_still_valid") is False or bool(x.get("ok")) or bool(x.get("payloads")) or "id" in x or "text" in x \
            or (isinstance(x.get("value"), dict) and x["value"].get("refused") is None) or ("MARK-" in str(x.get("value")))
    summ = {}
    for r in rows:
        k = "%s/%s" % (r["arm"], r["cat"] if r["cat"] != "forge" else "forge_" + r["group"])
        c = summ.setdefault(k, [0, 0])
        c[0] += 1
        c[1] += acc(r)
    covered = {r["entry"] for r in rows if r["arm"] == "enforced" and r["cat"] == "forge" and r["returned"]["type"] != "no_driver"}
    missing = sorted({e["entry"] for e in entries} - covered)
    params = {"seeds": sd, "planted": N_PLANTED, "fault": N_FAULT, "legit": N_LEGIT, "context": N_CONTEXT, "forge_rounds": FORGE_ROUNDS,
              "naive_pdp_patches": NAIVE_PATCHES, "no_session_verify_patch": NO_SESSION_VERIFY, "body_trust_patches": BODY_TRUST_PATCHES, "surface_entries": entries,
              "surface_non_session_callables": others}
    fn, h = write_items("e3_clash", rows)
    size = os.path.getsize(os.path.join(common.RESULTS, fn))
    en = {k: v for k, v in summ.items() if k.startswith("enforced/")}
    legit = summ["enforced/legit"]
    ok = (all(v[1] == 0 for k, v in en.items() if k != "enforced/legit") and legit[1] >= 0.99 * legit[0] and not missing
          and summ["enforced/planted"][0] >= 2000 and summ["enforced/fault"][0] >= 2000 and legit[0] >= 1000 and summ["enforced/context"][0] >= 3000)
    ctl = {k: v[1] / max(1, v[0]) for k, v in summ.items() if k.startswith("ctl_")}
    ok_ctl = all(ctl.get(k, 0) >= 0.10 for k in ("ctl_naive_pdp/planted", "ctl_naive_pdp/fault", "ctl_naive_pdp/context", "ctl_naive_pdp/forge_request",
                                                  "ctl_no_session_verify/forge_session", "ctl_body_identity_trusted/forge_body"))
    data = {"verdict": "PASS" if ok and ok_ctl else "FAIL", "counts_n_accepted": summ, "control_rates": ctl, "uncovered_entries": missing,
            "pass_conditions_met": ok, "controls_meet_floor": ok_ctl, "items": {"file": fn, "sha256": h, "bytes": size}, "runtime_s": round(time.time() - t0, 1),
            "not_proven": "forgeries are the enumerated kinds; pgcore is reached only through DecisionService (its engine is not a request-handler entry point); "
                          "Python in-process code can still reach private names (see DESIGN.md)"}
    write_result("e3_clash", params, data)
    print(data["verdict"], json.dumps(summ, sort_keys=True), "missing", missing, "artifact_bytes", size, "runtime_s", data["runtime_s"])
    print("control rates", json.dumps(ctl, sort_keys=True))


if __name__ == "__main__":
    main()

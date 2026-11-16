"""Random generators shared by the policy experiments (deterministic from a random.Random)."""
from pol.labels import Label
from pol.pdp import ACTIONS, Dest, Policy, Principal

TENANTS = ["t1", "t2"]
GROUPS = ["g1", "g2", "g3", "g4"]
REGIONS = ["EU", "US", "APAC"]
MODELS = ["m1", "m2", "m3", "m4"]
GRANTS = {t: frozenset(ACTIONS) for t in ("t1", "t2")}


def rand_label(rng, permissive=0.65):
    tenants = ["t1"] if rng.random() < 0.85 else ["t1", "t2"]
    acl = []
    for _ in range(rng.choice([0, 0, 0, 1, 1, 2])):
        acl.append(rng.sample(GROUPS, rng.choice([1, 2, 3])))
    regions = None if rng.random() < permissive else rng.sample(REGIONS, rng.choice([1, 2]))
    models = None if rng.random() < permissive else rng.sample(MODELS, rng.choice([1, 2, 3]))
    banned = [] if rng.random() < 0.6 else rng.sample(MODELS, rng.choice([1, 2]))
    return Label(tenants=tenants, acl=acl, regions=regions, sensitivity=rng.choice([0, 0, 0, 1, 1, 2, 3]), models=models, banned=banned,
                 local_only=rng.random() < 0.25, restricted=rng.random() < 0.08,
                 holds=[] if rng.random() < 0.7 else ["h1"], subjects=[] if rng.random() < 0.7 else rng.sample(["s1", "s2", "s3"], rng.choice([1, 2])))


def rand_principal(rng, tenant="t1", pid=None):
    return Principal(pid or "p%d" % rng.randrange(10 ** 6), tenant, frozenset(rng.sample(GROUPS, rng.choice([2, 3, 4, 4]))),
                     clearance=rng.choice([2, 3, 3, 3]), restricted_ok=rng.random() < 0.7, kind=rng.choice(["human", "agent", "analyst"]),
                     region=rng.choice(REGIONS + [None]), model=rng.choice(["d_model_eu", "d_model_us", None]))


def base_policy():
    dests = {
        "d_model_eu": Dest("model", "EU", True, 3, model="m1"),
        "d_model_eu_cloud": Dest("model", "EU", False, 3, model="m2"),
        "d_model_us": Dest("model", "US", False, 2, model="m3"),
        "d_model_cyber": Dest("model", "US", False, 3, model="m4", programme="cyber"),
        "d_embed_eu": Dest("embedder", "EU", True, 3, model="m1"),
        "d_embed_us": Dest("embedder", "US", False, 2, model="m3"),
        "d_export_eu": Dest("export", "EU", True, 3),
        "d_export_us": Dest("export", "US", False, 1),
    }
    return Policy(1, grants=GRANTS, hold_events=frozenset(["h1"]), destinations=dests, authorizations={"auth1": {"tenant": "t1", "programme": "cyber", "expires": 1000}})


def rand_probe(rng, policy):
    action = rng.choice(ACTIONS) if rng.random() < 0.5 else rng.choice(["send_model", "embed", "send_inbox", "export", "send_model", "erase"])
    ctx = {"region": rng.choice(REGIONS + [None]), "now": 10, "authorization": rng.choice(["auth1", None, "zzz"])}
    if action in ("send_model", "embed", "export") or (action == "inject" and rng.random() < 0.7):
        kind = {"send_model": "model", "embed": "embedder", "export": "export", "inject": "model"}[action]
        names = [n for n, d in policy.destinations.items() if d.kind == kind]
        ctx["destination"] = rng.choice(names + ["unknown_dest"] if rng.random() < 0.05 else names)
    if action == "send_inbox":
        ctx["recipient"] = rand_principal(rng, pid="rcp%d" % rng.randrange(1000))
    return action, ctx

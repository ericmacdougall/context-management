"""C5: trust-gated supersession, property-style over generated slots, with rule counts.
Also runs the SAME checks against a broken variant (no gating) to show they can fail."""
import json, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
from memsys.rules import Claim, resolve_slot, TRUST_RANK

def mk(i, obj, trust, mod, t):
    ts = f"2026-01-01T00:{t // 60:02d}:{t % 60:02d}.000000Z"
    return Claim(f"c{i}", obj, mod, trust, ts, f"{ts}|s|{i:012d}|000")

def check(claims, res):
    """Invariants over the RESULT only (independent of how resolve_slot works)."""
    v = set()
    for c in claims:
        s, vt = res[c.id]
        if c.modality in ("hypothetical", "proposed") and s in ("current", "superseded"):
            v.add("hypothetical_or_proposed_became_current")
        if s in ("current", "superseded") and c.modality not in ("hypothetical", "proposed"):
            # whoever held the slot just before c (earlier in canonical order, still open at c's time) must not outrank c
            # unless c has the same value
            for x in claims:
                if x is c or x.order_key >= c.order_key or x.modality in ("hypothetical", "proposed"):
                    continue
                sx, vx = res[x.id]
                if sx in ("current", "superseded") and (vx is None or vx > c.valid_from) and TRUST_RANK[x.trust_origin] > TRUST_RANK[c.trust_origin]:
                    v.add("lower_trust_displaced_or_coexisted_with_higher")
        if s == "superseded":
            closers = [x for x in claims if x.valid_from == vt and res[x.id][0] in ("current", "superseded")]
            if not closers or all(TRUST_RANK[x.trust_origin] < TRUST_RANK[c.trust_origin] for x in closers):
                v.add("lower_trust_closed_higher")
    return v

def run(rank=None, n=20000, seed=16096):
    rng = random.Random(seed)
    origins = list(TRUST_RANK)
    counts = {"cases": 0, "web_vs_user_typed": 0, "pasted_vs_user_typed": 0, "assistant_vs_user_typed": 0,
              "hypothetical_or_proposed": 0, "partial_scope_vs_whole": 0, "violations": {}}
    for _ in range(n):
        k = rng.randint(2, 9)
        cs = [mk(i, rng.choice("abcd"), rng.choice(origins), rng.choice(["asserted"] * 4 + ["decided", "hypothetical", "proposed"]), t) for i, t in enumerate(rng.sample(range(3000), k))]
        res = resolve_slot(cs, **({"rank": rank} if rank else {}))
        counts["cases"] += 1
        ordered = sorted(cs, key=lambda c: c.order_key)
        has_user = any(c.trust_origin == "user_typed" and c.modality in ("asserted", "decided") for c in cs)
        if has_user:
            counts["web_vs_user_typed"] += any(c.trust_origin == "web" for c in cs)
            counts["pasted_vs_user_typed"] += any(c.trust_origin == "user_supplied_external" for c in cs)
            counts["assistant_vs_user_typed"] += any(c.trust_origin == "assistant" for c in cs)
        counts["hypothetical_or_proposed"] += any(c.modality in ("hypothetical", "proposed") for c in cs)
        for r in check(cs, res):
            counts["violations"][r] = counts["violations"].get(r, 0) + 1
    # partial scope: slots are keyed by scope, so a scoped claim is never in the whole slot's claim set
    from memsys import extractor
    ev = dict(tenant_id="t", session_id="s", seq=1, event_id="e", hash="h", ts_event="2026-01-01T00:00:00.000000Z", trust_origin="user_typed", actor="user", subject_id="u")
    for _ in range(2000):
        a = extractor.extract(1, ev, f"Alice's phone is {rng.randint(1,9)}")[0]
        b = extractor.extract(1, ev, f"Alice's phone for work is {rng.randint(1,9)}")[0]
        assert (a["subject"], a["predicate"], a["scope"]) != (b["subject"], b["predicate"], b["scope"])
        counts["partial_scope_vs_whole"] += 1
    return counts

if __name__ == "__main__":
    good = run()
    broken = run(rank={k: 1 for k in TRUST_RANK})
    out = {"correct_variant": good, "broken_variant_no_gating": broken,
           "pass": not good["violations"] and bool(broken["violations"])}
    print(json.dumps(out, indent=1))
    d = os.environ.get("RESDIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))
    os.makedirs(d, exist_ok=True)
    json.dump(out, open(f"{d}/C5_trust.json", "w"), indent=1)

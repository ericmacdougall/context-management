"""C5 v2: trust-gated supersession tested through the REAL path: envelope constraint, extractor (paste/quote markers),
claim grouping and resolve_slot, and (second part, needs the DBs) the database constraint and derivation.
usage: c5_attack.py [cases=6000] [db_cases=200]   (db part is skipped if no Postgres is configured)"""
import datetime as dt, json, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memsys import extractor, rules
from memsys.rules import Claim, resolve_slot, TRUST_RANK

ACTORS = {"user": ("user_typed", "user_supplied_external"), "assistant": ("assistant",), "tool": ("tool", "web")}


def envelope_ok(actor, trust):
    return trust in ACTORS.get(actor, ())


def pipeline(events, markers=True, cross_scope=True, check_envelope=True):
    """events: dicts actor, trust_origin, text, i (order). Returns list of (event, claim_trust, status, scope, object)."""
    saved = dict(extractor._MARKER_TRUST)
    if not markers:
        extractor._MARKER_TRUST.clear()
    try:
        claims, meta = [], {}
        for k, e in enumerate(events):
            if check_envelope and not envelope_ok(e["actor"], e["trust_origin"]):
                continue                                          # the append is refused by the database constraint
            ev = dict(tenant_id="t", session_id="s", seq=k + 1, event_id=f"t/s/{k+1}", hash=f"h{k}", ts_event=f"2026-01-01T00:{k // 60:02d}:{k % 60:02d}.000000Z",
                      trust_origin=e["trust_origin"], actor=e["actor"], subject_id="u")
            for c in extractor.extract(1, ev, e["text"]):
                cl = Claim(c["id"], c["object"], c["modality"], c["trust_origin"], c["valid_from"], c["order_key"], c["scope"])
                claims.append((e, cl)); meta[(c["subject"], c["predicate"])] = 1
        by = {}
        for e, cl in claims:
            by.setdefault(0, []).append(cl)
        res = resolve_slot([cl for _, cl in claims], cross_scope=cross_scope)
        return [(e, cl.trust_origin, res[cl.id][0], cl.scope, cl.object) for e, cl in claims]
    finally:
        extractor._MARKER_TRUST.update(saved)


ATTACKS = ["mislabelled_envelope", "pasted_marker", "assistant_restatement_marker", "assistant_actor", "web_marker", "web_actor", "scoped_low_trust",
           "hypothetical", "proposal", "scoped_pasted"]


def gen_case(rng):
    typed = rng.choice(["Rome", "Oslo", "Lima"])
    evs = [{"actor": "user", "trust_origin": "user_typed", "text": f"Alice's city is {typed}", "legit": True, "kind": "typed"}]
    expect = typed
    for _ in range(rng.randint(1, 5)):
        a = rng.choice(ATTACKS + ["legit_update"] * 2)
        v = rng.choice(["Paris", "Cairo", "Quito", "Perth"])
        if a == "legit_update":
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"Alice's city is {v}", "legit": True, "kind": a}); expect = v
        elif a == "mislabelled_envelope":
            evs.append({"actor": rng.choice(["assistant", "tool"]), "trust_origin": "user_typed", "text": f"Alice's city is {v}", "legit": False, "kind": a})
        elif a == "pasted_marker":
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"Pasted: Alice's city is {v}", "legit": False, "kind": a})
        elif a == "assistant_restatement_marker":
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"As you said, Alice's city is {v}", "legit": False, "kind": a})
        elif a == "assistant_actor":
            evs.append({"actor": "assistant", "trust_origin": "assistant", "text": f"Alice's city is {v}", "legit": False, "kind": a})
        elif a == "web_marker":
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"According to the page, Alice's city is {v}", "legit": False, "kind": a})
        elif a == "web_actor":
            evs.append({"actor": "tool", "trust_origin": "web", "text": f"Alice's city is {v}", "legit": False, "kind": a})
        elif a == "scoped_low_trust":
            evs.append({"actor": "tool", "trust_origin": "web", "text": f"Alice's city for work is {v}", "legit": False, "kind": a})
        elif a == "scoped_pasted":
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"Pasted: Alice's city for home is {v}", "legit": False, "kind": a})
        elif a == "hypothetical":
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"If Alice's city were {v}", "legit": False, "kind": a})
        else:
            evs.append({"actor": "user", "trust_origin": "user_typed", "text": f"Proposal: set Alice's city to {v}", "legit": False, "kind": a})
    return evs, expect


def violations(evs, expect, out):
    v = set()
    cur = [(e, tr, sc, o) for e, tr, st, sc, o in out if st == "current"]
    whole = [x for x in cur if x[2] == "*"]
    if not whole or whole[0][3] != expect:
        v.add("typed_fact_displaced")
    for e, tr, sc, o in cur:
        if not e["legit"] and e["kind"] not in ("hypothetical", "proposal") and o != expect:
            v.add("lower_trust_claim_current:" + e["kind"])
        if e["kind"] in ("hypothetical", "proposal"):
            v.add("non_assertive_current")
    return v


def run_pure(n, variant):
    rng = random.Random(16096)
    kinds, viol, count = {}, {}, 0
    for _ in range(n):
        evs, expect = gen_case(rng)
        out = pipeline(evs, **variant)
        for e in evs: kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        for x in violations(evs, expect, out):
            viol[x] = viol.get(x, 0) + 1
        count += 1
    return {"cases": count, "attack_events_by_kind": kinds, "violations": viol}


def db_part(n):
    from memsys import db, store, derive, kms
    import psycopg
    conn = db.connect(autocommit=True); db.reset_schema(conn)
    ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    ev = lambda i, a, t, text, subj="u1": dict(ts_event=ts + dt.timedelta(seconds=i), actor=a, trust_origin=t, kind="message", subject_id=subj, content=text)
    res = {"matrix": {"accepted_valid": 0, "rejected_invalid": 0, "wrongly_accepted": 0, "wrongly_rejected": 0}}
    for actor in ("user", "assistant", "tool", "system"):
        for trust in ("user_typed", "user_supplied_external", "assistant", "tool", "web"):
            for rep in range(10):
                try:
                    store.append_events(conn, "c5", "m", [ev(rep, actor, trust, "hello")])
                    ok = True
                except psycopg.errors.CheckViolation:
                    ok = False
                want = envelope_ok(actor, trust)
                res["matrix"]["accepted_valid" if ok and want else "rejected_invalid" if not ok and not want else "wrongly_accepted" if ok else "wrongly_rejected"] += 1
    rng = random.Random(5); bad = {}; kinds = {}
    for k in range(n):
        evs, expect = gen_case(rng)
        tenant, sess = "c5e", f"s{k}"
        subj = f"w{k}"
        stored = []
        for i, e in enumerate(evs):
            try:
                store.append_events(conn, tenant, sess, [ev(i, e["actor"], e["trust_origin"], e["text"].replace("Alice", f"Alice{k}"), subj)]); stored.append(e)   # one entity per sequence
            except psycopg.errors.CheckViolation:
                pass
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        while derive.derive_batch(conn, tenant, 1, limit=500, touch=False):
            pass
        rows = conn.execute("select slot_enc, object_enc, subject_id, status, trust_origin, modality from facts where tenant_id=%s and subject_id=%s", (tenant, subj)).fetchall()
        kk = kms.get()
        out = []
        for se, oe, sid, st, tr, mod in rows:
            sc = derive.decode_slot(kk.dec_many(tenant, [(sid, se)])[0])[2]
            o = kk.dec_many(tenant, [(sid, oe)])[0]
            kind = {"hypothetical": "hypothetical", "proposed": "proposal"}.get(mod, "fact")
            out.append(({"legit": tr == "user_typed" and kind == "fact", "kind": kind if kind != "fact" else "lower_trust_fact"}, tr, st, sc, o))
        # map each stored fact back to its attack kind by matching object+text (objects are unique per attack mostly)
        for x in violations(stored, expect, out):
            bad[x] = bad.get(x, 0) + 1
    res["end_to_end"] = {"sequences": n, "attack_events_by_kind": kinds, "violations": bad}
    return res


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
    dn = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    out = {"workload": f"{n} generated typed-fact histories with 1-5 attack or update events each (10 attack types); each correct variant must show 0 violations, each broken variant must show >0"}
    out["correct"] = run_pure(n, {})
    out["broken_no_paste_quote_markers"] = run_pure(n, {"markers": False})
    out["broken_scope_bypasses_trust"] = run_pure(n, {"cross_scope": False})
    out["broken_envelope_unchecked"] = run_pure(n, {"check_envelope": False})
    try:
        out["database"] = db_part(dn)
    except Exception as e:
        out["database"] = {"skipped": f"{type(e).__name__}: {str(e)[:150]}"}
    d = out["database"]
    out["pass"] = bool(not out["correct"]["violations"] and all(out[k]["violations"] for k in ("broken_no_paste_quote_markers", "broken_scope_bypasses_trust", "broken_envelope_unchecked"))
                       and "matrix" in d and d["matrix"]["wrongly_accepted"] == 0 and d["matrix"]["wrongly_rejected"] == 0 and not d["end_to_end"]["violations"])
    print(json.dumps(out, indent=1))
    rd = os.environ.get("RESDIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))
    os.makedirs(rd, exist_ok=True)
    json.dump(out, open(f"{rd}/C5_attack.json", "w"), indent=1)


if __name__ == "__main__":
    main()

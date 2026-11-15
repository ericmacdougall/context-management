"""Scenario generator with an independent TRUTH model for I4/I5. The truth is kept as plain dicts updated by the rules written
out directly here (it never calls decisions.py / task.py), and records the expected per-item state after EVERY event."""
import random

LOW = ["assistant", "web", "user_supplied_external"]
WORDS = "alpha bravo cedar delta ember fjord grove harbor ivory jasper kernel lumen meadow nectar onyx prism quartz raven sable tundra".split()


def gen(rng, immediate_revert=False):
    n_items = rng.randint(3, 10)
    items = {f"i{k}": f"item i{k} {rng.choice(WORDS)} v0" for k in range(n_items)}
    ev, truth_core, work_log, inj_log = [], [], [], []
    snaps = [dict(items)]                       # snaps[v-1] = definition at version v
    cur = dict(items)
    pend = {}                                   # change_id -> dict(item, op, text, asked)
    dispo = {}
    decs = {}                                   # decision_id -> dict(kind, touched, before, change, version)
    last_dec = [None]
    counters = {"c": 0, "d": 0, "r": 0, "s": 0, "n": 0}
    unauthorized = [0]
    reject_events = []                          # (event index of detect, event index of reject, change_id)
    detect_idx = {}
    revert_pairs = []                           # (index before decision, index of revert) for adjacent reverts
    uniq = [0]

    def txt(i):
        uniq[0] += 1
        return f"item {i} {rng.choice(WORDS)} v{uniq[0]}"

    def push(kind, body, trust="user_typed"):
        ev.append({"seq": len(ev) + 1, "kind": kind, "trust_origin": trust, "body": body})
        truth_core.append(snapshot())

    def status_of(i):
        cs = [c for c in pend.values() if c["item"] == i]
        if not cs:
            return "current"
        if len({(c["op"], c["text"]) for c in cs}) > 1:
            return "disputed"
        if any(c["asked"] for c in cs):
            return "unresolved"
        return "pending_change"

    def snapshot():
        return {i: [t, status_of(i)] for i, t in sorted(cur.items())}

    def bump():
        snaps.append(dict(cur))

    def apply_change(c):
        """returns the touched item list if the definition changed"""
        i = c["item"]
        if c["op"] == "remove":
            if i in cur:
                del cur[i]; return [i]
        elif i in cur and cur[i] != c["text"]:
            cur[i] = c["text"]; return [i]
        return []

    push("task_defined", {"task_id": "T", "items": [{"id": i, "type": "scope", "text": t} for i, t in items.items()]})
    n_actions = rng.randint(15, 60)
    imm_at = rng.randrange(5, n_actions) if immediate_revert else None
    for a in range(n_actions):
        if a == imm_at and (last_dec[0] is None or ev[-1]["body"].get("decision_id") != last_dec[0]):
            imm_at += 1                       # the previous event must be the decision itself, so the revert is adjacent
        if a == imm_at:
            d = decs[last_dec[0]]
            revert_decision(rng, d, ev, push, decs, cur, snaps, pend, dispo, counters, truth_core, revert_pairs, adjacent=True, last_dec=last_dec)
            continue
        r = rng.random()
        items_now = list(cur)
        if r < 0.20 or not items_now:
            push("message", {"text": " ".join(rng.choice(WORDS) for _ in range(6))}, rng.choice(["user_typed", "assistant", "web"]))
        elif r < 0.38:
            counters["c"] += 1
            cid = f"c{counters['c']}"
            i = rng.choice(items_now)
            op = "remove" if rng.random() < 0.12 else "change"
            t = txt(i)
            pend[cid] = {"item": i, "op": op, "text": t if op == "change" else None, "asked": False}
            dispo[cid] = "pending"; detect_idx[cid] = len(ev)
            push("change_detected", {"change_id": cid, "item": i, "op": op, **({"proposed_text": t} if op == "change" else {}), "quote": "q", "quote_event_id": "e", "speaker": "someone",
                                     "speaker_trust": rng.choice(["user_typed"] + LOW), "detector": "d1", "confidence": 0.8}, "tool")
        elif r < 0.45 and pend:
            counters["r"] += 1
            rid = f"rb{counters['r']}"
            inc = rng.sample(list(pend), min(len(pend), rng.randint(1, 3)))
            for c in inc: pend[c]["asked"] = True
            decs.setdefault("_rb", {})[rid] = list(inc)
            push("read_back", {"read_back_id": rid, "version": len(snaps), "includes": inc}, "assistant")
        elif r < 0.60 and pend:
            cid = rng.choice(list(pend)); counters["d"] += 1; did = f"d{counters['d']}"
            c = pend.pop(cid); idx = len(ev)
            before = {c["item"]: cur.get(c["item"])}
            touched = apply_change(c)
            if touched: bump(); dispo[cid] = "accepted"
            else: dispo[cid] = "accepted_no_effect"
            decs[did] = {"kind": "accept", "touched": touched, "before": before, "change": cid, "chg": c, "index_before": idx - 1, "version": len(snaps), "effective": bool(touched)}
            last_dec[0] = did
            push("decision_recorded", {"decision_id": did, "action": "accept", "change_id": cid})
        elif r < 0.68 and pend:
            cid = rng.choice(list(pend)); counters["d"] += 1; did = f"d{counters['d']}"
            c = pend.pop(cid); idx = len(ev); dispo[cid] = "rejected"
            decs[did] = {"kind": "reject", "touched": [c["item"]], "before": None, "change": cid, "chg": c, "index_before": idx - 1, "version": len(snaps), "effective": True}
            last_dec[0] = did
            reject_events.append((detect_idx[cid], idx, cid))
            push("decision_recorded", {"decision_id": did, "action": "reject", "change_id": cid})
        elif r < 0.74:
            counters["d"] += 1; did = f"d{counters['d']}"
            tr = rng.choice(LOW)
            i = rng.choice(items_now)
            unauthorized[0] += 1
            push("decision_recorded", {"decision_id": did, "action": "state", "ops": [{"op": "change", "id": i, "text": "UNAUTHORIZED " + txt(i)}]}, tr)
        elif r < 0.80:
            counters["d"] += 1; did = f"d{counters['d']}"
            i = rng.choice(items_now); t = txt(i); idx = len(ev)
            before = {i: cur.get(i)}
            c = {"item": i, "op": "change", "text": t}
            touched = apply_change(c)
            if touched: bump()
            decs[did] = {"kind": "state", "touched": touched, "before": before, "change": None, "chg": None, "index_before": idx - 1, "version": len(snaps), "effective": bool(touched)}
            last_dec[0] = did
            push("decision_recorded", {"decision_id": did, "action": "state", "ops": [{"op": "change", "id": i, "text": t}]})
        elif r < 0.84 and decs.get("_rb"):
            rid = rng.choice(list(decs["_rb"])); counters["d"] += 1; did = f"d{counters['d']}"
            for cid in decs["_rb"][rid]:
                if cid in pend:
                    c = pend.pop(cid)
                    touched = apply_change(c)
                    if touched: bump(); dispo[cid] = "accepted"
                    else: dispo[cid] = "accepted_no_effect"
            decs["_rb"].pop(rid)
            push("decision_recorded", {"decision_id": did, "action": "accept_read_back", "read_back_id": rid})
        elif r < 0.88 and [d for d in decs if d != "_rb"]:
            ds = [d for d in decs if d != "_rb"]
            d = decs[last_dec[0]] if (last_dec[0] and rng.random() < 0.5) else decs[rng.choice(ds)]
            revert_decision(rng, d, ev, push, decs, cur, snaps, pend, dispo, counters, truth_core, revert_pairs, adjacent=False, last_dec=last_dec)
        elif r < 0.95:
            counters["s"] += 1; sid = f"s{counters['s']}"
            serves = rng.sample(list(cur), min(len(cur), rng.randint(1, 3)))
            v_rec = len(snaps) if rng.random() < 0.6 else rng.randint(1, len(snaps))      # a stale worker records an older version
            push("step_started", {"step_id": sid, "serves": serves, "def_version": v_rec, "watermark": len(ev)}, "assistant")
            work_log.append((len(ev), sid, "step_started", v_rec, list(serves)))
            if rng.random() < 0.5:
                v2 = rng.randint(v_rec, len(snaps))
                push("step_completed", {"step_id": sid, "serves": serves, "artifacts": ["a"], "def_version": v2, "watermark": len(ev)}, "assistant")
                work_log.append((len(ev), sid, "step_completed", v2, list(serves)))
        else:
            counters["n"] += 1
            presented = rng.sample(list(cur), min(len(cur), rng.randint(1, 4)))
            v_rec = len(snaps) if rng.random() < 0.6 else rng.randint(1, len(snaps))
            presented = [i for i in presented if i in snaps[v_rec - 1]] or presented[:1]
            push("context_injected", {"injection_id": f"x{counters['n']}", "def_version": v_rec, "watermark": len(ev), "items": presented}, "assistant")
            inj_log.append((len(ev), f"x{counters['n']}", v_rec, list(presented)))
    # expected acted-on lists, from snapshots only
    def changed(v, items):
        return sorted(i for i in items if snaps[v - 1].get(i) != cur.get(i))
    stale_work = [(seq, sid, kind, v, changed(v, serves)) for seq, sid, kind, v, serves in work_log if changed(v, serves)]
    stale_ctx = [(seq, iid, v, changed(v, items)) for seq, iid, v, items in inj_log if changed(v, items)]
    return {"events": ev, "truth_core": truth_core, "reject_events": reject_events, "revert_pairs": revert_pairs, "unauthorized": unauthorized[0],
            "stale_work": stale_work, "stale_contexts": stale_ctx, "final_version": len(snaps), "dispositions": dict(dispo), "pending_final": sorted(pend)}


def revert_decision(rng, d, ev, push, decs, cur, snaps, pend, dispo, counters, truth_core, revert_pairs, adjacent, last_dec):
    counters["d"] += 1
    did = f"d{counters['d']}"
    target = next(k for k, v in decs.items() if v is d)
    idx_before = d["index_before"]
    if d["kind"] == "reject":
        pend[d["change"]] = dict(d["chg"])
        dispo[d["change"]] = "pending"
    else:
        changed = False
        for i, old in (d["before"] or {}).items():
            if old is None and i in cur:
                del cur[i]; changed = True
            elif old is not None and cur.get(i) != old:
                cur[i] = old; changed = True
        if changed:
            snaps.append(dict(cur))
        if d["kind"] == "accept" and d["change"] and changed:
            pend[d["change"]] = dict(d["chg"])
            dispo[d["change"]] = "pending"
    adj = adjacent and d["index_before"] == len(ev) - 2
    push("decision_recorded", {"decision_id": did, "action": "revert", "reverts": target})
    if adj and d["effective"]:
        revert_pairs.append((idx_before, len(ev) - 1))
    last_dec[0] = did if False else last_dec[0]

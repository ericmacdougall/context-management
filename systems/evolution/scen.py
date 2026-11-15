"""Scenario generator for the task-amendment experiments (fixed seed, no LLM)."""
import random

WORDS = "alpha bravo cedar delta ember fjord grove harbor ivory jasper kernel lumen meadow nectar onyx prism quartz raven sable tundra".split()
TYPES = ["scope", "constraint", "acceptance"]
LOW = ["assistant", "web", "user_supplied_external"]


def gen_scenario(rng, immediate_revert=None):
    n_items = rng.randint(5, 30)
    n_steps = rng.randint(10, 200)
    n_amend = rng.randint(1, 5)
    rev = [0]

    def text(t, iid):
        rev[0] += 1
        return f"{t} {iid} rev{rev[0]} {rng.choice(WORDS)} {rng.choice(WORDS)}"
    items = {}
    for k in range(n_items):
        t = "goal" if k == 0 else TYPES[k % 3]
        iid = f"i{k}"
        items[iid] = {"type": t, "text": text(t, iid)}
    ev = []
    def push(kind, body, trust="user_typed"):
        ev.append({"seq": len(ev) + 1, "kind": kind, "trust_origin": trust, "body": body})
    push("task_defined", {"task_id": "T", "items": [{"id": i, **v} for i, v in items.items()]})
    snaps = [{i: v["text"] for i, v in items.items()}]
    cur = {i: dict(v) for i, v in items.items()}
    amend_log = []          # (amendment_id, version, ids touched, before-snapshot-of-touched {id: (type,text)|None})
    steps = {}
    # per-step micro-event chains, randomly interleaved
    chains = []
    for s in range(n_steps):
        c = ["plan"]
        r = rng.random()
        if r < 0.9: c.append("start")
        if r < 0.8: c.append("complete")
        if rng.random() < 0.03: c.append("invalidate")
        chains.append((f"s{s}", c))
    pending = [[sid, list(c)] for sid, c in chains]
    micro = []
    while pending:
        j = rng.randrange(len(pending))
        sid, c = pending[j]
        micro.append((sid, c.pop(0)))
        if not c: pending.pop(j)
    # amendment slots: positions in micro list
    kinds = ["narrow", "widen", "change_acceptance", "add_constraint", "revert", "same_item_twice", "remove"]
    slots = sorted(rng.randrange(len(micro) + 1) for _ in range(n_amend))
    noise = sorted(rng.randrange(len(micro) + 1) for _ in range(rng.randint(0, 4)))
    ctr = [0]
    immediate = None

    def amend(ops, reverts=None):
        ctr[0] += 1
        aid = f"A{ctr[0]}"
        before = {}
        for o in ops:
            i = o["id"] if "id" in o else o["item"]["id"]
            before[i] = (dict(cur[i]) if i in cur else None)
        push("task_amended", {"amendment_id": aid, "ops": ops, **({"reverts": reverts} if reverts else {})})
        effective = False
        for o in ops:
            if o["op"] == "add" and o["item"]["id"] not in cur:
                cur[o["item"]["id"]] = {"type": o["item"]["type"], "text": o["item"]["text"]}; effective = True
            elif o["op"] == "remove" and o["id"] in cur:
                del cur[o["id"]]; effective = True
            elif o["op"] == "change" and o["id"] in cur and cur[o["id"]]["text"] != o["text"]:
                cur[o["id"]]["text"] = o["text"]; effective = True
        if effective:
            snaps.append({i: v["text"] for i, v in cur.items()})
            amend_log.append((aid, len(snaps), sorted(before), before))
        return aid, effective

    def pick(t):
        c = [i for i, v in cur.items() if v["type"] == t]
        return rng.choice(c) if c else (rng.choice(list(cur)) if cur else None)

    def do_amend():
        nonlocal immediate
        k = rng.choice(kinds)
        if not cur: return
        if k in ("narrow", "widen"):
            i = pick("scope"); amend([{"op": "change", "id": i, "text": text(cur[i]["type"], i)}])
        elif k == "change_acceptance":
            i = pick("acceptance"); amend([{"op": "change", "id": i, "text": text("acceptance", i)}])
        elif k == "add_constraint":
            iid = f"c{ctr[0] + 1}x{rev[0]}"; amend([{"op": "add", "item": {"id": iid, "type": "constraint", "text": text("constraint", iid)}}])
        elif k == "remove":
            i = rng.choice(list(cur)); amend([{"op": "remove", "id": i}])
        elif k == "same_item_twice":
            i = rng.choice(list(cur)); amend([{"op": "change", "id": i, "text": text(cur[i]["type"], i)}]); amend([{"op": "change", "id": i, "text": text(cur[i]["type"], i)}])
        elif k == "revert" and amend_log:
            aid, ver, touched, before = amend_log[-1] if (immediate_revert or rng.random() < .5) else rng.choice(amend_log)
            ops = []
            for i in touched:
                b = before[i]
                if b is None and i in cur: ops.append({"op": "remove", "id": i})
                elif b is not None and i not in cur: ops.append({"op": "add", "item": {"id": i, **b}})
                elif b is not None and cur[i]["text"] != b["text"]: ops.append({"op": "change", "id": i, "text": b["text"]})
            if ops: amend(ops, reverts=aid)

    def low_trust():
        if not cur: return
        i = rng.choice(list(cur))
        push("task_amended", {"amendment_id": f"P{len(ev)}", "ops": [{"op": "change", "id": i, "text": "PROPOSED-" + text("x", i)}]}, rng.choice(LOW))

    nlow = 0
    imm_pos = None
    if immediate_revert:     # A followed directly by revert(A), after some steps
        imm_pos = rng.randrange(len(micro) + 1)
    for idx in range(len(micro) + 1):
        for _ in range(slots.count(idx)): do_amend()
        for _ in range(noise.count(idx)):
            if rng.random() < .6: low_trust(); nlow += 1
            else: push("task_amended", {"amendment_id": f"X{len(ev)}", "ops": [{"op": "remove", "id": "nonexistent"}]})
        if idx == imm_pos and cur:
            i = rng.choice(list(cur)); n0 = len(ev)
            aid, eff = amend([{"op": "change", "id": i, "text": text(cur[i]["type"], i)}])
            before_idx = n0
            if eff:
                _, ver, touched, before = amend_log[-1]
                amend([{"op": "change", "id": i, "text": before[i]["text"]}], reverts=aid)
                immediate = (before_idx, len(ev))
        if idx == len(micro): break
        sid, what = micro[idx]
        st = steps.setdefault(sid, {"serves": [], "basis": None, "invalid": False})
        if what == "plan":
            st["serves"] = rng.sample(list(cur), min(len(cur), rng.randint(1, 3)))
            push("step_planned", {"step_id": sid, "serves": st["serves"]})
        elif what == "start":
            st["basis"] = len(snaps); push("step_started", {"step_id": sid})
        elif what == "complete":
            if st["basis"] is None: st["basis"] = len(snaps)
            push("step_completed", {"step_id": sid, "artifacts": [f"art/{sid}/{k}" for k in range(rng.randint(0, 3))]})
        else:
            st["invalid"] = True; push("step_invalidated", {"step_id": sid, "reason": "operator"})
    # steps planned but not started have basis None
    return {"events": ev, "truth": {"snaps": snaps, "steps": steps}, "n_low_trust": nlow, "immediate": immediate, "n_items": n_items, "n_steps": n_steps}

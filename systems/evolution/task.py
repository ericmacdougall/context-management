"""Task definition as data in the log, and the derived `task_state` view (pure, rebuildable).

Event kinds (JSON bodies, stored as the event content):
  task_defined   {task_id, items:[{id,type,text}]}                     only user_typed takes effect
  task_amended   {amendment_id, ops:[{op:add,item:{..}}|{op:remove,id}|{op:change,id,text}], reverts?}   only user_typed
  step_planned   {step_id, serves:[item ids]}
  step_started   {step_id}
  step_completed {step_id, serves?:[..], artifacts:[refs]}
  step_invalidated {step_id, reason}
Low-trust task_defined/task_amended are recorded as proposals and change nothing.

Classification of a step against the CURRENT definition compares the text of each served item at the
step's basis (the definition version when it STARTED, or when it completed if no start was recorded) with the
text now: still_valid, needs_recheck (a served item changed), obsolete (every served item removed). Nothing is
deleted. State is JSON-native (string keys) so a persisted view resumes exactly.
"""
import json
import copy

KINDS = ("task_defined", "task_amended", "step_planned", "step_started", "step_completed", "step_invalidated")
AMEND_TRUST = "user_typed"


def initial():
    return {"version": 0, "items": {}, "defs": {}, "amendments": [], "steps": {}, "proposals": [], "ignored": [], "applied": 0}


def _snap(items):
    return {k: dict(v) for k, v in items.items()}


def apply(state, ev, inplace=False):
    """ev: {seq, kind, trust_origin, body}. Returns the new state (the input is not mutated unless inplace)."""
    s = state if inplace else copy.deepcopy(state)
    k, b, seq = ev["kind"], ev["body"], ev["seq"]
    s["applied"] = seq
    if k == "task_defined":
        if ev["trust_origin"] != AMEND_TRUST:
            s["proposals"].append({"seq": seq, "kind": k, "trust": ev["trust_origin"], "ops": [{"op": "define", "items": b["items"]}]})
        elif s["version"] > 0:
            s["ignored"].append({"seq": seq, "why": "already defined"})
        else:
            s["items"] = {i["id"]: {"type": i["type"], "text": i["text"]} for i in b["items"]}
            s["version"] = 1
            s["defs"]["1"] = _snap(s["items"])
            s["amendments"].append({"id": "definition", "version": 1, "seq": seq, "author_trust": ev["trust_origin"], "touched": sorted(s["items"]), "reverts": None})
    elif k == "task_amended":
        if ev["trust_origin"] != AMEND_TRUST:
            s["proposals"].append({"seq": seq, "kind": k, "trust": ev["trust_origin"], "ops": b["ops"], "amendment_id": b.get("amendment_id")})
        elif s["version"] == 0 or any(a["id"] == b["amendment_id"] for a in s["amendments"]):
            s["ignored"].append({"seq": seq, "why": "no definition or duplicate amendment id"})
        else:
            items, touched = _snap(s["items"]), []
            for op in b["ops"]:
                if op["op"] == "add" and op["item"]["id"] not in items:
                    items[op["item"]["id"]] = {"type": op["item"]["type"], "text": op["item"]["text"]}; touched.append(op["item"]["id"])
                elif op["op"] == "remove" and op["id"] in items:
                    del items[op["id"]]; touched.append(op["id"])
                elif op["op"] == "change" and op["id"] in items and items[op["id"]]["text"] != op["text"]:
                    items[op["id"]]["text"] = op["text"]; touched.append(op["id"])
                else:
                    s["ignored"].append({"seq": seq, "why": "ineffective op", "op": op["op"]})
            if touched:
                s["items"] = items
                s["version"] += 1
                s["defs"][str(s["version"])] = _snap(items)
                s["amendments"].append({"id": b["amendment_id"], "version": s["version"], "seq": seq, "author_trust": ev["trust_origin"],
                                        "touched": sorted(set(touched)), "reverts": b.get("reverts")})
    elif k in ("step_planned", "step_started", "step_completed", "step_invalidated"):
        st = s["steps"].setdefault(b["step_id"], {"serves": [], "artifacts": [], "status": "planned", "basis_version": None, "start_version": None,
                                                   "completion_version": None, "invalidated": None, "seq": {}})
        if k == "step_planned":
            st["serves"] = list(b.get("serves", [])); st["seq"]["planned"] = seq
        elif k == "step_started":
            st["status"] = "in_flight"; st["start_version"] = st["basis_version"] = s["version"]; st["seq"]["started"] = seq
        elif k == "step_completed":
            if "serves" in b:
                st["serves"] = list(b["serves"])
            st["artifacts"] = list(b.get("artifacts", [])); st["status"] = "completed"
            st["completion_version"] = s["version"]; st["seq"]["completed"] = seq
            if st["basis_version"] is None:
                st["basis_version"] = s["version"]
        else:
            st["invalidated"] = {"reason": b.get("reason", ""), "seq": seq}
    return s


def _classify(defs, items, version_from, serves, version_to_items):
    base = defs[str(version_from)]
    surviving = [i for i in serves if i in version_to_items]
    if serves and not surviving:
        return "obsolete", [], [i for i in serves]
    changed = [i for i in surviving if i not in base or base[i]["text"] != version_to_items[i]["text"]]
    removed = [i for i in serves if i not in version_to_items]
    return ("needs_recheck" if changed else "still_valid"), changed, removed


def view(state):
    """Derived, read-only view: the state plus classifications. A pure function of the state."""
    out = {"version": state["version"], "items": state["items"], "amendments": state["amendments"], "proposals": state["proposals"],
           "ignored": state["ignored"], "applied": state["applied"], "steps": []}
    for sid in sorted(state["steps"]):
        st = state["steps"][sid]
        row = {"step_id": sid, "status": st["status"], "serves": st["serves"], "artifacts": st["artifacts"], "basis_version": st["basis_version"],
               "completion_version": st["completion_version"], "class_at_completion": None, "class_now": None, "reason": None}
        if st["invalidated"]:
            row["class_now"] = "invalidated"; row["reason"] = {"invalidated": st["invalidated"]["reason"]}
        elif st["basis_version"] is not None:
            if st["completion_version"] is not None:
                row["class_at_completion"] = _classify(state["defs"], None, st["basis_version"], st["serves"], state["defs"][str(st["completion_version"])])[0]
            cls, changed, removed = _classify(state["defs"], None, st["basis_version"], st["serves"], state["items"])
            row["class_now"] = cls
            causes = sorted({a["id"] for a in state["amendments"] if a["version"] > st["basis_version"] and (set(a["touched"]) & set(changed + removed))})
            row["reason"] = {"changed_items": changed, "removed_items": removed, "caused_by": causes} if cls != "still_valid" else {}
        out["steps"].append(row)
    return out


def fold(events):
    s = initial()
    for ev in sorted(events, key=lambda e: e["seq"]):
        s = apply(s, ev, inplace=True)
    return s


def task_state(events):
    return view(fold(events))


def redo_set(v):
    """Steps whose work must be redone/rechecked: exactly needs_recheck, never a still_valid one."""
    return sorted(r["step_id"] for r in v["steps"] if r["class_now"] == "needs_recheck")


def canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ context block
def render_context(v):
    """Deterministic block for the next turn."""
    L = [f"TASK DEFINITION [CURRENT] version {v['version']}"]
    for iid in sorted(v["items"]):
        it = v["items"][iid]
        L.append(f"[CURRENT] {iid} ({it['type']}): {it['text']}")
    sup = _superseded_lines(v)
    L.append("SUPERSEDED (changed by the latest amendment; do NOT treat as requirements)")
    L += sup if sup else ["(none)"]
    L.append("PROGRESS")
    for r in v["steps"]:
        reason = ""
        if r["reason"]:
            reason = " " + " ".join(f"{k}={','.join(x) if isinstance(x, list) else x}" for k, x in sorted(r["reason"].items()))
        L.append(f"step {r['step_id']} | {r['status']} | {r['class_now']} | artifacts={len(r['artifacts'])}{reason}")
    L.append("PROPOSALS (recorded, not in effect)")
    L += [f"[PROPOSAL] seq {p['seq']} from {p['trust']}: {p['kind']} {json.dumps(p['ops'], sort_keys=True)}" for p in v["proposals"]] or ["(none)"]
    return "\n".join(L)


def _superseded_lines(v):
    return v.get("_superseded", [])


def render(state):
    """Render from the raw state (needs the previous definition for the diff)."""
    vw = view(state)
    ver = state["version"]
    sup = []
    if ver > 1:
        prev, cur = state["defs"][str(ver - 1)], state["items"]
        for iid in sorted(prev):
            if iid not in cur:
                sup.append(f"[SUPERSEDED] {iid} removed in version {ver}; was: {prev[iid]['text']}")
            elif prev[iid]["text"] != cur[iid]["text"]:
                sup.append(f"[SUPERSEDED] {iid} changed in version {ver}; was: {prev[iid]['text']} (current text: see [CURRENT] {iid})")
    vw["_superseded"] = sup
    return render_context(vw)

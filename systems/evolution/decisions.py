"""I4 recorded decisions, I5 acted-on version, I3 freshness structure: a layer over the task_state fold (task.py).

Event kinds added (JSON bodies):
  change_detected   {change_id, item, op: change|remove, proposed_text?, quote, quote_event_id, speaker, speaker_trust, detector, confidence}
                    a CANDIDATE found in prose. It never changes the definition.
  read_back         {read_back_id, version, includes:[change_id,...]}  the system's restatement, awaiting acceptance
  decision_recorded {decision_id, action: accept|reject|state|accept_read_back|revert, change_id?, read_back_id?, ops?, reverts?}
                    the ONLY way state changes here; takes effect only from a user_typed event (accept_read_back is the user's
                    acceptance of a read-back). Anything else is recorded under `unauthorized` and changes nothing.
  context_injected  {injection_id, def_version, watermark, items:[item ids presented as current]}
  step_started / step_completed may carry def_version and watermark (version and log watermark the work acted on).
Per-item status: current | pending_change | unresolved (a read-back asked, unanswered) | disputed (conflicting pending changes).
`broken` selects deliberately wrong variants used as controls by the experiments.
"""
import copy
import json

from . import task

USER = "user_typed"


def initial():
    s = task.initial()
    s.update({"pending": {}, "history": {}, "decisions": [], "read_backs": {}, "item_chain": {}, "unauthorized": [], "injections": [], "work": []})
    return s


def _chain(s, items, entry):
    for i in items:
        s["item_chain"].setdefault(i, []).append(entry)


def _apply_ops(s, ev, decision_id, ops, reverts=None, source=None, action="accept", change_id=None):
    """Turn a decision into a definition change using the F1 machinery (a synthesised user_typed amendment)."""
    v0 = s["version"]
    task.apply(s, {"seq": ev["seq"], "kind": "task_amended", "trust_origin": USER, "body": {"amendment_id": decision_id, "ops": ops, **({"reverts": reverts} if reverts else {})}}, inplace=True)
    if s["version"] == v0:
        return False
    touched = s["amendments"][-1]["touched"]
    rec = {"decision_id": decision_id, "action": action, "change_id": change_id, "seq": ev["seq"], "source_seq": source if source is not None else ev["seq"], "by_trust": ev["trust_origin"],
           "version_after": s["version"], "touched": touched, "reverts": reverts}
    s["decisions"].append(rec)
    _chain(s, touched, rec)
    return True


def _change_ops(c):
    if c["op"] == "remove":
        return [{"op": "remove", "id": c["item"]}]
    return [{"op": "change", "id": c["item"], "text": c["proposed_text"]}]


def apply(state, ev, inplace=False, broken=None):
    s = state if inplace else copy.deepcopy(state)
    k, b, seq = ev["kind"], ev["body"], ev["seq"]
    if k in task.KINDS:
        if k in ("step_started", "step_completed") and ("def_version" in b or "watermark" in b):
            st_serves = b.get("serves") or s["steps"].get(b["step_id"], {}).get("serves", [])
            s["work"].append({"seq": seq, "kind": k, "step_id": b["step_id"], "serves": list(st_serves), "def_version": b.get("def_version"), "watermark": b.get("watermark")})
        task.apply(s, ev, inplace=True)
        return s
    s["applied"] = seq
    if k == "context_injected":
        s["injections"].append({"seq": seq, "injection_id": b["injection_id"], "def_version": b["def_version"], "watermark": b["watermark"], "items": list(b["items"])})
    elif k == "change_detected":
        if b["change_id"] not in s["history"]:
            c = dict(b, detected_seq=seq, asked=False)
            s["history"][b["change_id"]] = dict(c, disposition="pending")
            if broken == "detected_applies" and s["version"] > 0:
                _apply_ops(s, ev, "bad-" + b["change_id"], _change_ops(c), source=seq, change_id=b["change_id"])
                s["history"][b["change_id"]]["disposition"] = "accepted"
            else:
                s["pending"][b["change_id"]] = c
    elif k == "read_back":
        s["read_backs"][b["read_back_id"]] = {"seq": seq, "version": b["version"], "includes": list(b["includes"]), "accepted": False}
        for cid in b["includes"]:
            if cid in s["pending"]:
                s["pending"][cid]["asked"] = True
    elif k == "decision_recorded":
        allowed = ev["trust_origin"] == USER or (broken == "assistant_decides")
        if not allowed:
            s["unauthorized"].append({"seq": seq, "decision_id": b["decision_id"], "trust": ev["trust_origin"], "action": b["action"]})
            return s
        a = b["action"]
        if a == "accept" and b.get("change_id") in s["pending"]:
            c = s["pending"].pop(b["change_id"])
            if _apply_ops(s, ev, b["decision_id"], _change_ops(c), source=c["detected_seq"], action="accept", change_id=b["change_id"]):
                s["history"][b["change_id"]]["disposition"] = "accepted"
                s["decisions"][-1]["restore"] = dict(c)          # exactly what the pending change looked like (incl. whether it had been asked)
            else:
                s["history"][b["change_id"]]["disposition"] = "accepted_no_effect"
        elif a == "reject" and b.get("change_id") in s["pending"]:
            c = s["pending"].pop(b["change_id"])
            s["history"][b["change_id"]]["disposition"] = "rejected"
            rec = {"decision_id": b["decision_id"], "action": "reject", "change_id": b["change_id"], "seq": seq, "source_seq": c["detected_seq"], "by_trust": ev["trust_origin"], "version_after": s["version"], "touched": [c["item"]], "reverts": None, "restore": c}
            s["decisions"].append(rec)
            if broken == "reject_leaves_trace":
                s["item_chain"].setdefault(c["item"], [])           # nothing; the trace we model is in the pending set below
                s["pending"][b["change_id"]] = dict(c, asked=False)
            _chain(s, [c["item"]], rec)
        elif a == "state":
            _apply_ops(s, ev, b["decision_id"], b["ops"], source=seq, action="state")
        elif a == "accept_read_back" and b.get("read_back_id") in s["read_backs"]:
            rb = s["read_backs"][b["read_back_id"]]
            rb["accepted"] = True
            for cid in rb["includes"]:
                if cid in s["pending"]:
                    c = s["pending"].pop(cid)
                    did = f"{b['decision_id']}:{cid}"
                    if _apply_ops(s, ev, did, _change_ops(c), source=rb["seq"], action="accept_read_back", change_id=cid):
                        s["history"][cid]["disposition"] = "accepted"
                        s["decisions"][-1]["restore"] = dict(c)
        elif a == "revert":
            d = next((x for x in s["decisions"] if x["decision_id"] == b["reverts"] and x["action"] != "revert"), None)
            if d is not None:
                if d["action"] == "reject":
                    s["pending"][d["change_id"]] = dict(d["restore"]); s["history"][d["change_id"]]["disposition"] = "pending"
                    rec = {"decision_id": b["decision_id"], "action": "revert", "change_id": d["change_id"], "seq": seq, "source_seq": seq, "by_trust": ev["trust_origin"], "version_after": s["version"], "touched": d["touched"], "reverts": d["decision_id"]}
                    s["decisions"].append(rec); _chain(s, d["touched"], rec)
                else:
                    before = s["defs"][str(d["version_after"] - 1)]
                    ops = []
                    for i in d["touched"]:
                        if i in before and i not in s["items"]:
                            ops.append({"op": "add", "item": {"id": i, **before[i]}})
                        elif i in before and s["items"][i]["text"] != before[i]["text"]:
                            ops.append({"op": "change", "id": i, "text": before[i]["text"]})
                        elif i not in before and i in s["items"]:
                            ops.append({"op": "remove", "id": i})
                    if _apply_ops(s, ev, b["decision_id"], ops, reverts=d["decision_id"], source=seq, action="revert", change_id=d.get("change_id")) and d.get("restore"):
                        s["pending"][d["change_id"]] = dict(d["restore"]); s["history"][d["change_id"]]["disposition"] = "pending"      # the change returns to pending, as it was before the decision
    return s


def fold(events, broken=None):
    s = initial()
    for ev in sorted(events, key=lambda e: e["seq"]):
        apply(s, ev, inplace=True, broken=broken)
    return s


def item_status(s):
    by = {}
    for cid, c in s["pending"].items():
        by.setdefault(c["item"], []).append(c)
    out = {}
    for i in s["items"]:
        cs = by.get(i, [])
        if not cs:
            out[i] = "current"
        elif len({(c["op"], c.get("proposed_text")) for c in cs}) > 1:
            out[i] = "disputed"
        elif any(c["asked"] for c in cs):
            out[i] = "unresolved"
        else:
            out[i] = "pending_change"
    return out


def core(s):
    """What 'the state' means for exact-restoration checks: current text and status per item (no version numbers, no history)."""
    st = item_status(s)
    return {i: [v["text"], st[i]] for i, v in sorted(s["items"].items())}


def view(s):
    v = task.view(s)
    v["item_status"] = item_status(s)
    v["item_chain"] = {i: [{k: d[k] for k in ("decision_id", "action", "change_id", "source_seq", "by_trust", "version_after")} for d in ch] for i, ch in sorted(s["item_chain"].items())}
    v["history"] = {cid: {"disposition": h["disposition"], "item": h["item"], "quote": h["quote"], "speaker": h["speaker"], "speaker_trust": h["speaker_trust"], "detector": h["detector"],
                          "confidence": h["confidence"]} for cid, h in sorted(s["history"].items())}
    v["pending"] = {cid: {"item": c["item"], "proposed_text": c.get("proposed_text"), "op": c["op"], "asked": c["asked"]} for cid, c in sorted(s["pending"].items())}
    v["unauthorized"] = s["unauthorized"]
    return v


# ---------------------------------------------------------------------------- I5
def text_at(s, version, item):
    d = s["defs"].get(str(version))
    return d[item]["text"] if d and item in d else None


def _changed_since(s, version, items):
    """Items whose current text differs from their text at `version` (removed counts as changed)."""
    out = []
    for i in items:
        now = s["items"].get(i, {}).get("text")
        if text_at(s, version, i) != now:
            out.append(i)
    return out


def stale_work(s, broken=None):
    """All recorded work whose (definition version) is superseded for the items it served: [(seq, step_id, kind, def_version, items)]."""
    out = []
    for w in s["work"]:
        if w["def_version"] is None:
            continue
        if broken == "version_compare":
            items = list(w["serves"]) if w["def_version"] < s["version"] else []
        elif broken == "ignore_serves":
            items = _changed_since(s, w["def_version"], list(s["items"]))
        else:
            items = _changed_since(s, w["def_version"], w["serves"])
        if items:
            out.append((w["seq"], w["step_id"], w["kind"], w["def_version"], sorted(items)))
    return out


def stale_contexts(s, broken=None):
    """Injected contexts that presented, as current, an item whose text has since changed: [(seq, injection_id, def_version, items)]."""
    out = []
    for c in s["injections"]:
        if broken == "version_compare":
            items = list(c["items"]) if c["def_version"] < s["version"] else []
        else:
            items = _changed_since(s, c["def_version"], c["items"])
        if items:
            out.append((c["seq"], c["injection_id"], c["def_version"], sorted(items)))
    return out


def canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=str)

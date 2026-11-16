"""Invariants of the decision core over MemoryState.snapshot() dicts (pre-state, delivered event, post-state, outcome code, original
code), written independently of the reducer's branching. Each function returns a list of violated invariant names (empty = holds).

  I1  authority: a decision-like outcome needs decide authority and an unrevoked principal
  I2  a non-human moves the head or the value only by accepting an open HUMAN-authored proposal
  I3  a proposal or remark never changes the head
  I4  head-moving outcomes are bound to the head version (or the contested sibling / contest base)
  I5  a refused event changes nothing
  I6  a redelivered event (same principal, idem key and fingerprint) changes nothing and returns `duplicate` with the ORIGINAL code
  I8  versions move by at most one per event
  I9  snapshot -> restore -> snapshot is the identity
  I10 contested status is coherent (status contested <=> contested record; no value; >= 2 candidates)
  I11 head-moving decisions arrive on the principal kind's channel; a confirmation comes from a human on another owner channel
  I12 an accepted or amended value comes from an OPEN proposal
  I13 an event id is applied at most once per requirement (grant is exempt: CORE-RULES ambiguity 8)
  I14 every contested candidate is human-authored and there are at most MAX_CANDIDATES
  I15 proposing or remarking needs a role (decide authority or proposer) and an unrevoked principal
  I16 a non-human event that changes no value never moves the head (N3; CORE-RULES rules 11 and 15)
  I17 open items stay within the caps (per principal per requirement, per requirement)
  L2  head-movement starvation bound: a non-human event never increases the number of open human-authored proposals on a requirement,
      and moves the head only if it consumes one of them. By induction, a human who reads version v and retries after each `stale` is
      applied within (open human-authored proposals at the read) + 1 attempts, whatever non-humans deliver."""
import hashlib
import json

OWNER = ("owner_ui", "owner_cli")
AGENT = ("agent_api",)
NORMAL_HEAD = ("decided_accept", "decided_amend", "superseded", "retracted", "confirmed_applied", "resolved")
DECISION_LIKE = NORMAL_HEAD + ("decided_reject", "contested_opened", "contested_joined", "concurrent_agree")
HEAD_TYPES = ("confirm", "decide", "supersede", "retract")


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def sha(o):
    return hashlib.sha256(canon(o).encode()).hexdigest()


def open_human(s, req):
    return sum(1 for d in s["props"].get(req, {}).values() if d["kind"] == "proposal" and d["status"] == "open" and d["author_kind"] == "human")


def transition(pre, ev, post, code, orig, refused):
    bad = []
    if not isinstance(ev, dict):
        return ["I5_refused_event_changed_state"] if (code in refused and canon(pre) != canon(post)) else []
    t, req, pid = ev.get("type"), ev.get("req"), ev.get("principal")
    kind = pre["principals"].get(pid)
    if code in refused and canon(pre) != canon(post):
        bad.append("I5_refused_event_changed_state")
    idem = {(a, b): (c, d) for a, b, c, d in pre["idems"]}
    k = (pid, ev.get("idem"))
    if k in idem and idem[k][0] == sha(ev) and (code != "duplicate" or orig != idem[k][1] or canon(pre) != canon(post)):
        bad.append("I6_redelivery_changed_state_or_lost_outcome")
    if t != "revoke" and isinstance(req, str) and req in pre["heads"]:
        h0, h1 = pre["heads"][req], post["heads"][req]
        alive = pid not in pre["revoked"]
        if code in DECISION_LIKE and not (pid in pre["authority"][req] and alive):
            bad.append("I1_authority")
        if code in ("proposed", "pending_created") and not ((pid in pre["authority"][req] or pid in pre["proposers"][req]) and alive):
            bad.append("I15_proposal_without_role")
        moved = h1["version"] != h0["version"]
        valchg = (h0["value_ref"], h0["status"]) != (h1["value_ref"], h1["status"])
        if kind != "human" and kind is not None:
            if moved or valchg:
                prop = pre["props"][req].get(ev.get("proposal"))
                if not (code == "decided_accept" and prop is not None and prop["kind"] == "proposal" and prop["author_kind"] == "human"):
                    bad.append("I2_agent_head_move")
            # I16 is CORE-RULES rule 11 exactly: a non-human REJECT changes no value and never moves the head. (An earlier, stricter
            # reading, "any non-human event that changes no value never moves the head", is contradicted by rule 15 + section 7 +
            # reference ambiguity 11: a non-human may accept an open human proposal whose value is already current after a resolve.)
            # I16 (N3 as the brief states it, amendment 3): a non-human event that changes no value never moves the head.
            if moved and not valchg:
                bad.append("I16_nonhuman_head_move_without_value_change")
            oh0, oh1 = open_human(pre, req), open_human(post, req)
            if oh1 > oh0 or (moved and oh1 > oh0 - 1):
                bad.append("L2_head_starvation")
        if t in ("propose", "remark") and (moved or valchg):
            bad.append("I3_remark_or_proposal_changed_head")
        if code in NORMAL_HEAD and ev.get("based_on") != h0["version"]:
            bad.append("I4_decision_not_bound_to_head")
        if code == "contested_opened" and (h0["last"] is None or ev.get("based_on") != h0["last"]["based_on"]):
            bad.append("I4_decision_not_bound_to_head")
        if code == "contested_joined" and (h0["contested"] is None or ev.get("based_on") != h0["contested"]["base"]):
            bad.append("I4_decision_not_bound_to_head")
        if not (h0["version"] <= h1["version"] <= h0["version"] + 1):
            bad.append("I8_version_monotonic")
        if code in DECISION_LIKE and t in HEAD_TYPES and ((kind == "human" and ev.get("channel") not in OWNER)
                                                          or (kind != "human" and ev.get("channel") not in AGENT)):
            bad.append("I11_channel")
        if code == "confirmed_applied":
            pc = pre["props"][req].get(ev.get("pending"))
            if kind != "human" or pc is None or pc["channel"] == ev.get("channel"):
                bad.append("I11_channel")
        if code in ("decided_accept", "decided_amend"):
            pr = pre["props"][req].get(ev.get("proposal"))
            if pr is None or pr["status"] != "open":
                bad.append("I12_value_from_closed_proposal")
        if code not in refused and t != "grant" and ev.get("id") in pre["ids"][req]:
            bad.append("I13_duplicate_event_id_applied")
    return bad


def state(s, restore_snapshot, max_candidates, max_open_principal, max_open_req):
    bad = []
    for r, h in s["heads"].items():
        c = h["contested"]
        if (h["status"] == "contested") != (c is not None) or (c is not None and (h["value_ref"] is not None or len(c["candidates"]) < 2)):
            bad.append("I10_contested_state_coherent")
        if c is not None and (len(c["candidates"]) > max_candidates or any(s["principals"].get(x["by"]) != "human" for x in c["candidates"])):
            bad.append("I14_contest_nonhuman_or_unbounded")
        per = {}
        tot = 0
        for d in s["props"][r].values():
            if d["status"] == "open":
                per[d["author"]] = per.get(d["author"], 0) + 1
                tot += 1
        if tot > max_open_req or any(v > max_open_principal for v in per.values()):
            bad.append("I17_open_cap")
    if canon(restore_snapshot(s)) != canon(s):
        bad.append("I9_snapshot_roundtrip")
    return bad

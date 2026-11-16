"""Viewer-scoped views of the decision core: every value, decider and candidate passes the policy decision point (read) with its own
COMPUTED label. What the viewer may not read is replaced by a redaction marker; contested status of a readable requirement is shown with
only the candidates the viewer may read plus a boolean that restricted candidates exist. Unlabelled data is redacted (default deny)."""
from __future__ import annotations

from .labels import Label
from .pdp import decide

RED = {"redacted": True}


def _ok(viewer, label_dict, policy):
    if label_dict is None:
        return False
    try:
        lab = Label.from_dict(label_dict)
    except Exception:       # noqa: BLE001
        return False
    return decide(viewer, "read", lab, {}, policy=policy, trusted={"now": 0}).allow


def explain_for(state: dict, req: str, viewer, policy) -> dict:
    if req not in state["reqs"] or not _ok(viewer, state["req_labels"][req], policy):
        return {"req": req, "status": "not_visible"}
    q = state["reqs"][req]
    out = {"req": req, "status": q["status"], "version": q["version"]}
    if q["status"] == "current":
        if _ok(viewer, q["value_label"], policy):
            out.update(value=q["value"], decision_event=q["value_event"], decided_by=q["value_by"])
        else:
            out.update(value=RED, decision_event=None, decided_by=None)
    out["superseded_chain"] = [dict(value=c["value"], version=c["version"], event=c["event"], by=c["by"]) if _ok(viewer, c.get("label"), policy) else dict(RED)
                               for c in q["chain"]]
    if q["contested"] is not None:
        cands = q["contested"]["candidates"]
        vis = [dict(event=c["event"], by=c["by"], effect=c["effect"]) for c in cands if _ok(viewer, c.get("label"), policy)]
        out["contested"] = {"candidates": vis, "restricted_candidate_exists": len(vis) < len(cands)}
    return out


def table_for(state: dict, viewer, policy):
    return [explain_for(state, r, viewer, policy) for r in sorted(state["reqs"])]

"""Independent recomputation of the MIG verdict (exp/mig_rehearsal.py) from its per-arm rows. Stdlib only. The ok flags the experiment
wrote are ignored; each arm is re-judged from its raw observations.

Clean arm (one per seed): after each forward the pre-migration anchor checks (ok, anchored > 0), the rebuilt view equals the memory fold,
every probe statement by dc_app was refused (an exception name, never False), and the real request through dc_idp + dc_app got an
outcome code that is not an authentication/authorisation refusal; each forward replayed > 0 events; the second forward was refused;
the fingerprint hash after rollback equals the one before forward; the old engine's chain verifies after rollback.
Control arms (forged_grant, head_tampered per seed): forward refused with the reason the procedure gives for that forgery (a replayed
event judged differently; state differs from the old tables), not an error, and the fingerprint hash unchanged.
PASS: every seed of params.seeds has one clean arm and both controls, and all of them pass."""

REFUSALS = {"unauthorized", "principal_revoked", "bad_event", "tenant_mismatch", "channel_mismatch", "unauthenticated:invalid_session"}
REASON = {"forged_grant": "judged", "head_tampered": "differs from the old tables"}


def _check(c, outcome_codes):
    if not isinstance(c, dict):
        return False
    a = c.get("anchor_check") or {}
    denied = c.get("app_denied") or {}
    return (a.get("ok") is True and (a.get("anchored") or 0) > 0 and c.get("view_equals_memory_fold") is True and len(denied) >= 3
            and all(isinstance(v, str) and v for v in denied.values()) and c.get("api_request") in outcome_codes and c.get("api_request") not in REFUSALS)


def _replayed(rep):
    t = (rep or {}).get("tenants") or {}
    return bool(t) and all((x.get("events_replayed") or 0) > 0 for x in t.values())


def verify(doc, rows):
    res, par = doc["result"], doc["params"]
    oc = set(res.get("outcome_codes") or [])
    clean, ctl = {}, {}
    for r in rows:
        if r.get("kind") == "clean":
            clean[r["seed"]] = (_check(r.get("checks1"), oc) and _check(r.get("checks2"), oc) and _replayed(r.get("forward1"))
                                and _replayed(r.get("forward2")) and isinstance(r.get("forward_twice_refused"), str) and bool(r["forward_twice_refused"])
                                and bool(r.get("fp_before")) and r.get("fp_before") == r.get("fp_after_rollback") and r.get("old_engine_after_rollback") is True)
        elif r.get("kind") == "control":
            ref = r.get("refused")
            ctl[(r["seed"], r.get("control"))] = (isinstance(ref, str) and bool(ref) and not ref.startswith("error:")
                                                  and REASON.get(r.get("control"), "\0") in ref and bool(r.get("fp_before")) and r.get("fp_before") == r.get("fp_after"))
    seeds = list(par["seeds"])
    complete = set(clean) == set(seeds) and set(ctl) == {(s, h) for s in seeds for h in REASON}
    verdict = "PASS" if complete and all(clean.values()) and all(ctl.values()) and bool(seeds) else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res.get("verdict"),
            "checks": {"complete": complete, "clean": {str(k): v for k, v in clean.items()}, "controls": {"%s/%s" % k: v for k, v in ctl.items()}}}

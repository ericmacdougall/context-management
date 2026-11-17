"""Independent recomputation of the MIG verdict (exp/mig_rehearsal.py) from its per-arm rows. Stdlib only. The ok flags the experiment
wrote are ignored; each arm is re-judged from its raw observations.

Clean arm (one per seed): after each forward the pre-migration anchor checks (ok, anchored > 0), the rebuilt view equals the memory fold,
and each of the three named probe statements by dc_app failed with InsufficientPrivilege exactly (review-3 N8: any other exception, an
error or a missing probe is not a denial). After the FIRST forward no API request was made (api_request null: the rollback window is
clean); after the SECOND, the real request through dc_idp + dc_app was APPLIED (a write), the rollback that followed was REFUSED with
the procedure's reason ("written after the migration") and the written event was still there (review-3 N4). Each forward replayed > 0
events; the second forward was refused; the fingerprint hash after the first rollback equals the one before forward; the old engine's
chain verifies after that rollback.
Control arms per seed: forged_grant and head_tampered: forward refused with the reason the procedure gives for that forgery (a replayed
event judged differently; state differs from the old tables), not an error, and the fingerprint hash unchanged. rollback_unchecked
(the 0a20b64 rollback): the applied post-migration write existed, the rollback ran, and the write is gone.
PASS: every seed of params.seeds has one clean arm and all three controls, and all of them pass."""

REASON = {"forged_grant": "judged", "head_tampered": "differs from the old tables"}
CONTROLS = tuple(REASON) + ("rollback_unchecked",)
PROBES = ("select count(*) from dc_pre_r3_7.events", "select count(*) from dc.events", "select dc._apply_event('t1', '{}'::jsonb)")


def _check(c, api, applied):
    if not isinstance(c, dict):
        return False
    a = c.get("anchor_check") or {}
    denied = c.get("app_denied") or {}
    api_ok = c.get("api_request") in applied and bool(c.get("api_event")) if api else c.get("api_request") is None
    return (a.get("ok") is True and (a.get("anchored") or 0) > 0 and c.get("view_equals_memory_fold") is True
            and sorted(denied) == sorted(PROBES) and all(denied[q] == "InsufficientPrivilege" for q in PROBES) and api_ok)


def _replayed(rep):
    t = (rep or {}).get("tenants") or {}
    return bool(t) and all((x.get("events_replayed") or 0) > 0 for x in t.values())


def verify(doc, rows):
    res, par = doc["result"], doc["params"]
    ap = set(res.get("applied_codes") or [])
    clean, ctl = {}, {}
    for r in rows:
        if r.get("kind") == "clean":
            raw = r.get("rollback_after_write")
            clean[r["seed"]] = (_check(r.get("checks1"), False, ap) and _check(r.get("checks2"), True, ap) and _replayed(r.get("forward1"))
                                and _replayed(r.get("forward2")) and isinstance(r.get("forward_twice_refused"), str) and bool(r["forward_twice_refused"])
                                and bool(r.get("fp_before")) and r.get("fp_before") == r.get("fp_after_rollback") and r.get("old_engine_after_rollback") is True
                                and isinstance(raw, str) and not raw.startswith("error:") and "written after the migration" in raw
                                and r.get("write_kept") is True)
        elif r.get("kind") == "control" and r.get("control") == "rollback_unchecked":
            ctl[(r["seed"], "rollback_unchecked")] = (r.get("api_request") in ap and bool(r.get("api_event")) and r.get("written") is True
                                                      and r.get("rolled_back") is True and r.get("write_lost") is True)
        elif r.get("kind") == "control":
            ref = r.get("refused")
            ctl[(r["seed"], r.get("control"))] = (isinstance(ref, str) and bool(ref) and not ref.startswith("error:")
                                                  and REASON.get(r.get("control"), "\0") in ref and bool(r.get("fp_before")) and r.get("fp_before") == r.get("fp_after"))
    seeds = list(par["seeds"])
    complete = set(clean) == set(seeds) and set(ctl) == {(s, h) for s in seeds for h in CONTROLS}
    verdict = "PASS" if complete and all(clean.values()) and all(ctl.values()) and bool(seeds) else "FAIL"
    return {"verdict": verdict, "agrees": verdict == res.get("verdict"),
            "checks": {"complete": complete, "clean": {str(k): v for k, v in clean.items()}, "controls": {"%s/%s" % k: v for k, v in ctl.items()}}}

"""Independent recomputation of the S2 verdict (exp/s2_sessions.py, step 2) from its rows. Stdlib only; the experiment's own verdict
and counts are ignored.

PASS needs, for EVERY seed of params.seeds:
  real arm
    mint     at least one row for each (role, what) the experiment lists, and every one refused (the in-process bare issue included).
    proof    every class of params.classes plus replay_first and revoked_principal has at least params.trials_per_class rows; valid and
             replay_first rows are accepted, stamped as the factor's principal and authenticated on dc.submit; every other class is
             refused (zero accepted attacks, absolute).
    replay_concurrent  at least one round, and every round has exactly one winner.
    token    every (token class, call) pair of params.tokens x params.calls is present once. valid: submit is an authenticated
             decision (not unauthenticated:*), explain answers for tenant-a and shows the seeded proposal, read_table shows it,
             erase_candidates finds one value, erase_values is not refused. forged, expired, revoked, ended, empty: submit
             unauthenticated:invalid_session, explain null, read_table empty, both erase calls refused "unauthorized". other_tenant:
             submit authenticated, explain answers for tenant-b and never shows the seeded proposal, read_table never shows it,
             erase_candidates finds nothing, erase_values erases nothing. token_after: the seeded value is still there, end_session
             returned true.
    origin   each case applied, with (actor, trust_origin) as the table below gives for its kind and channel.
    body_identity  every one of tenant, principal, channel, id, actor, trust_origin is refused identity_in_body and wrote no event.
    install_lookalike  refused.
  controls (each must CATCH)
    ctl_bare_mint_80bb6a7   the bare in-process issue accepted.
    ctl_bare_mint_0a20b64   the bare dc_idp mint accepted and the token authenticated.
    ctl_no_replay           at least one replay accepted.
    ctl_no_mac              at least one forged_mac or other_key proof accepted.
    ctl_identity_from_body  at least one other_principal or look_alike proof accepted with a stamped principal different from the
                            factor's (a session as someone the factor does not belong to).
    ctl_origin_from_body    the body naming actor "user" from a non-human was applied with event actor "user"."""

ORIGIN = {("human", "owner_ui"): ("user", "user_typed"), ("human", "owner_cli"): ("user", "user_typed")}
NON_USER = ("assistant", "agent_generated")
BAD_TOKENS = ("forged", "expired", "revoked", "ended", "empty")
BODY_KEYS = ("tenant", "principal", "channel", "id", "actor", "trust_origin")
CLAIMS_OTHER = ("body_edited", "look_alike", "other_principal")      # these trials must name a principal other than the factor's
MINT_EXPECTED = 17         # 16 raw SQL attempts by dc_app and dc_idp, plus the in-process bare issue


def _real(rows, par, seed):
    why = []
    r = [x for x in rows if x.get("arm") == "real" and x.get("seed") == seed]
    mint = [x for x in r if x["kind"] == "mint"]
    if len(mint) != MINT_EXPECTED or any(x["outcome"] != "refused" and not str(x["outcome"]).startswith("refused") for x in mint):
        why.append("mint: %d rows, accepted %s" % (len(mint), [x["what"] + "/" + x["role"] for x in mint if x["outcome"] == "accepted"]))
    proofs = [x for x in r if x["kind"] == "proof"]
    n = par.get("trials_per_class") or 0
    for cls in list(par.get("classes") or []) + ["replay_first", "revoked_principal"]:
        got = [x for x in proofs if x["cls"] == cls]
        if n < 1 or len(got) < n:
            why.append("proof %s: %d rows < %d" % (cls, len(got), n))
        for x in got:
            if cls in ("valid", "replay_first"):
                if not (x["outcome"] == "accepted" and x.get("stamped") == (x.get("factor_principal") or x.get("principal"))
                        and x.get("authenticated") is True):
                    why.append("proof %s: valid proof not a working session (%s)" % (cls, x.get("outcome")))
            elif x["outcome"] != "refused":
                why.append("proof %s: ACCEPTED (claimed %s, stamped %s)" % (cls, x.get("claimed"), x.get("stamped")))
            if cls in CLAIMS_OTHER and x.get("claimed") == x.get("factor_principal"):
                why.append("proof %s: vacuous trial (claims the factor's own principal)" % cls)
    conc = [x for x in r if x["kind"] == "replay_concurrent"]
    if not conc or any(x["winners"] != 1 for x in conc):
        why.append("replay_concurrent: winners %s" % [x["winners"] for x in conc])
    tok = {(x["cls"], x["call"]): x["obs"] for x in r if x["kind"] == "token"}
    want = [(c, k) for c in par.get("tokens") or [] for k in par.get("calls") or []]
    if not want or sorted(tok) != sorted(want) or len([x for x in r if x["kind"] == "token"]) != len(want):
        why.append("token: pairs %d, want %d" % (len(tok), len(want)))
    else:
        v = lambda c, k: tok[(c, k)]          # noqa: E731
        if not (isinstance(v("valid", "submit"), str) and not v("valid", "submit").startswith("unauthenticated")
                and (v("valid", "explain") or {}).get("tenant") == "tenant-a" and (v("valid", "explain") or {}).get("seen", 0) > 0
                and v("valid", "read_table")["seen"] > 0 and v("valid", "erase_candidates") == {"refused": None, "values": 1}
                and v("valid", "erase_values")["refused"] is None):
            why.append("token valid: positive control failed %s" % {k: v("valid", k) for k in par["calls"]})
        for c in BAD_TOKENS:
            if not (v(c, "submit") == "unauthenticated:invalid_session" and v(c, "explain") is None and v(c, "read_table") == {"n": 0, "seen": 0}
                    and v(c, "erase_candidates") == {"refused": "unauthorized", "values": 0}
                    and v(c, "erase_values") == {"refused": "unauthorized", "erased": None}):
                why.append("token %s: not refused on every call %s" % (c, {k: v(c, k) for k in par["calls"]}))
        o = "other_tenant"
        if not (isinstance(v(o, "submit"), str) and not v(o, "submit").startswith("unauthenticated")
                and (v(o, "explain") or {}).get("tenant") == "tenant-b" and (v(o, "explain") or {}).get("seen") == 0
                and v(o, "read_table")["seen"] == 0 and v(o, "erase_candidates") == {"refused": None, "values": 0}
                and v(o, "erase_values") == {"refused": None, "erased": 0}):
            why.append("token other_tenant: saw or changed tenant-a %s" % {k: v(o, k) for k in par["calls"]})
    after = [x for x in r if x["kind"] == "token_after"]
    if len(after) != 1 or after[0]["dana_values"] != 1 or after[0]["ended_returned"] is not True:
        why.append("token_after: %s" % after)
    org = [x for x in r if x["kind"] == "origin"]
    if not org:
        why.append("origin: no rows")
    for x in org:
        exp = ORIGIN.get((x["principal_kind"], x["channel"]), NON_USER)
        if x["code"] != "proposed" or (x["actor"], x["trust_origin"]) != exp:
            why.append("origin %s/%s: %s %s,%s != %s" % (x["principal"], x["channel"], x["code"], x["actor"], x["trust_origin"], exp))
    body = {x["key"]: x for x in r if x["kind"] == "body_identity"}
    if sorted(body) != sorted(BODY_KEYS) or any(x["code"] != "identity_in_body" or x["event_actor"] is not None for x in body.values()):
        why.append("body_identity: %s" % {k: (x["code"], x["event_actor"]) for k, x in body.items()})
    inst = [x for x in r if x["kind"] == "install_lookalike"]
    if len(inst) != 1 or not str(inst[0]["outcome"]).startswith("refused"):
        why.append("install_lookalike: %s" % inst)
    return why


def _controls(rows, seed):
    c = {}
    pick = lambda arm: [x for x in rows if x.get("arm") == arm and x.get("seed") == seed]     # noqa: E731
    c["ctl_bare_mint_80bb6a7"] = any(x["kind"] == "mint" and x["outcome"] == "accepted" for x in pick("ctl_bare_mint_80bb6a7"))
    c["ctl_bare_mint_0a20b64"] = any(x["kind"] == "mint" and x["outcome"] == "accepted" and x.get("authenticated") is True
                                     for x in pick("ctl_bare_mint_0a20b64"))
    c["ctl_no_replay"] = any(x["kind"] == "proof" and x["cls"] == "replay" and x["outcome"] == "accepted" for x in pick("ctl_no_replay"))
    c["ctl_no_mac"] = any(x["kind"] == "proof" and x["cls"] in ("forged_mac", "other_key") and x["outcome"] == "accepted" for x in pick("ctl_no_mac"))
    c["ctl_identity_from_body"] = any(x["kind"] == "proof" and x["cls"] in ("other_principal", "look_alike") and x["outcome"] == "accepted"
                                      and x.get("stamped") not in (None, x.get("factor_principal")) for x in pick("ctl_identity_from_body"))
    c["ctl_origin_from_body"] = any(x["kind"] == "body_identity" and x["key"] == "actor" and x["code"] == "proposed" and x["event_actor"] == "user"
                                    for x in pick("ctl_origin_from_body"))
    return c


def verify(doc, rows):
    par = doc["params"]
    sd = par.get("seeds") or []
    checks, ok = {}, bool(sd)
    for s in sd:
        why = _real(rows, par, s)
        ctl = _controls(rows, s)
        missing = sorted(set(par.get("controls") or []) - set(ctl)) or [k for k in ctl if k not in (par.get("controls") or [])]
        checks[str(s)] = {"real_failures": why[:20], "real_failure_count": len(why), "controls_caught": ctl, "controls_mismatch": missing}
        ok = ok and not why and all(ctl.values()) and not missing
    return {"verdict": "PASS" if ok else "FAIL", "checks": checks}

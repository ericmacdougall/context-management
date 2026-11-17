"""Independent recomputation of the S2 verdict (exp/s2_sessions.py, step 2) from its rows. Stdlib only; the experiment's own verdict
and counts are ignored, and so is its own statement of what it covered: the coverage below is pinned in this file from THRESHOLDS.md
amendment 7 (delta review M2). params.classes, tokens, calls, controls and trials_per_class must EQUAL the pins; params.seeds must hold
at least MIN_SEEDS distinct seeds and every row must carry one of them.

PASS needs, for EVERY seed:
  real arm
    mint     exactly one row for each of the 17 pinned (role, what) probes and no other, every one refused (the in-process bare issue
             included).
    proof    exactly TRIALS DISTINCT trials of each pinned class, of revoked_principal and of replay_first, and no other class: the rows
             of one class carry the trial indices 0..TRIALS-1, each exactly once (integers, final review P2: at a2d65fd TRIALS copies of
             one observation counted as TRIALS trials). Each replay_first is the first use of its replay trial's proof: same trial,
             same nonce identity, same factor. Valid and replay_first rows are accepted, stamped as the factor's principal and
             authenticated on dc.submit; every other class is refused (zero accepted attacks, absolute); a class claiming another
             principal must name one (not vacuous).
    replay_concurrent  exactly rounds 0..REPLAY_ROUNDS-1, each presenting REPLAY_WAYS proofs with exactly one winner.
  trial identity (across ALL seeds; final review P2) every real-arm proof and replay_concurrent row carries nonce_sha256, the first 16
             hex digits of sha256 of the nonce the experiment drew for that trial or round (the experiment asserts it is the nonce of the
             proof it presented, where the presented text has a parseable nonce). Every identity is distinct, except that a replay_first
             row and its replay row share one (they are one presentation). So a copied, renumbered or relabelled observation (another
             class, trial, round or seed) FAILS.
    token    every (token class, call) pair of the pinned TOKENS x CALLS exactly once. valid: submit is an authenticated decision (not
             unauthenticated:*), explain answers for tenant-a and shows the seeded proposal, read_table shows it, erase_candidates finds
             one value, erase_values is not refused. forged, expired, revoked, ended, empty, expired_in_txn (a token valid when an
             explicit transaction began and expired inside it) and repeatable_read (a REPEATABLE READ transaction takes its
             snapshot, then dc_idp ends the token): submit unauthenticated:invalid_session, explain null, read_table empty, both erase calls refused
             "unauthorized". other_tenant: submit authenticated, explain answers for tenant-b and never shows the seeded proposal,
             read_table never shows it, erase_candidates finds nothing, erase_values erases nothing. token_after: exactly one row, the
             seeded value is still there, end_session returned true.
    origin   exactly one row for each of the 6 pinned (principal, kind, channel) cases and no other, each applied with (actor,
             trust_origin) as ORIGIN gives for its kind and channel (anything not listed there is assistant / agent_generated).
    body_identity  exactly one row for each of tenant, principal, channel, id, actor, trust_origin, refused identity_in_body, no event.
    install_lookalike  exactly one row, refused.
  controls (each pinned control must CATCH, and no other control may be declared)
    ctl_bare_mint_80bb6a7   the bare in-process issue accepted.
    ctl_bare_mint_0a20b64   the bare dc_idp mint accepted and the token authenticated.
    ctl_no_replay           at least one replay accepted.
    ctl_no_mac              at least one forged_mac or other_key proof accepted.
    ctl_identity_from_body  at least one other_principal or look_alike proof accepted with a stamped principal different from the
                            factor's (a session as someone the factor does not belong to).
    ctl_origin_from_body    the body naming actor "user" from a non-human was applied with event actor "user".
    ctl_txn_clock           (the d05d731 transaction-start clock) an expired_in_txn proof accepted, or an expired_in_txn token that
                            reads the seeded proposal."""
import re

ORIGIN ={("human", "owner_ui"): ("user", "user_typed"), ("human", "owner_cli"): ("user", "user_typed")}
NON_USER = ("assistant", "agent_generated")
BODY_KEYS = ("tenant", "principal", "channel", "id", "actor", "trust_origin")
CLAIMS_OTHER = ("body_edited", "look_alike", "other_principal")      # these trials must name a principal other than the factor's

# Coverage pinned HERE, from THRESHOLDS.md amendment 7 (delta review M2), never taken from the result document: a result whose params
# differ from these, or whose rows miss, repeat or add coverage, FAILS.
TRIALS = 12                                                          # rows per proof class per seed (stamped and DEV alike)
MIN_SEEDS = 3
PROOF_CLASSES = ("valid", "forged_mac", "body_edited", "other_key", "other_factor_id", "look_alike", "other_principal", "other_channel",
                 "other_tenant", "expired", "far_future", "replay", "revoked_factor", "malformed", "expired_in_txn")
DERIVED_CLASSES = ("replay_first", "revoked_principal")             # replay_first: one per replay trial; revoked_principal: TRIALS
ACCEPTED_CLASSES = ("valid", "replay_first")
TOKENS = ("valid", "forged", "expired", "revoked", "ended", "empty", "other_tenant", "expired_in_txn", "repeatable_read")
BAD_TOKENS = ("forged", "expired", "revoked", "ended", "empty", "expired_in_txn", "repeatable_read")
CALLS = ("submit", "explain", "read_table", "erase_candidates", "erase_values")
CONTROLS = ("ctl_bare_mint_80bb6a7", "ctl_bare_mint_0a20b64", "ctl_no_replay", "ctl_no_mac", "ctl_identity_from_body",
            "ctl_origin_from_body", "ctl_txn_clock")
MINT_PROBES = frozenset([
    ("dc_idp", "bare_principal_open_session"), ("dc_app", "bare_principal_open_session"), ("dc_app", "open_session_with_proof"),
    ("dc_app", "end_session"), ("dc_app", "enroll_factor"), ("dc_idp", "enroll_factor"), ("dc_app", "insert_session"),
    ("dc_idp", "insert_session"), ("dc_app", "update_session"), ("dc_idp", "update_session"), ("dc_app", "read_sessions"),
    ("dc_idp", "read_sessions"), ("dc_app", "read_factors"), ("dc_idp", "read_factors"), ("dc_idp", "alter_factors"),
    ("dc_idp", "clear_used_proofs"), ("in_process", "bare_principal_issue")])
MINT_EXPECTED = len(MINT_PROBES)                                     # 16 raw SQL attempts by dc_app and dc_idp, plus the bare issue
ORIGIN_CASES = frozenset([("alice", "human", "owner_ui"), ("alice", "human", "owner_cli"), ("bob", "human", "owner_ui"),
                          ("carol", "human", "owner_ui"), ("bot", "agent", "agent_api"), ("x", "analyst", "agent_api")])
REPLAY_ROUNDS, REPLAY_WAYS = 3, 8
PINNED_PARAMS = {"trials_per_class": TRIALS, "classes": list(PROOF_CLASSES), "tokens": list(TOKENS), "calls": list(CALLS),
                 "controls": list(CONTROLS)}
TRIAL_CLASSES = PROOF_CLASSES + DERIVED_CLASSES                      # each: trials 0..TRIALS-1 per seed, each exactly once
NONCE_ID = re.compile(r"^[0-9a-f]{16}$")


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def _index(i):
    return isinstance(i, int) and not isinstance(i, bool) and 0 <= i < TRIALS


def _identities(rows, seeds):
    """Final review P2: one nonce identity per presentation, across every seed (see the module docstring)."""
    why, by = [], {}
    sd = {str(s) for s in seeds}
    for x in rows:
        if x.get("arm") != "real" or x.get("kind") not in ("proof", "replay_concurrent") or str(x.get("seed")) not in sd:
            continue
        n = x.get("nonce_sha256")
        if not (isinstance(n, str) and NONCE_ID.match(n)):
            why.append("no nonce identity: seed %s %s %s trial %r" % (x.get("seed"), x.get("kind"), x.get("cls"), x.get("trial", x.get("round"))))
            continue
        by.setdefault(n, []).append(x)
    for n, xs in by.items():
        if len(xs) == 1:
            continue
        pair = (len(xs) == 2 and sorted(str(x.get("cls")) for x in xs) == ["replay", "replay_first"]
                and all(x.get("kind") == "proof" for x in xs) and len({(str(x.get("seed")), repr(x.get("trial")), x.get("factor")) for x in xs}) == 1)
        if not pair:
            why.append("nonce identity %s on %d rows: %s" % (n, len(xs), sorted({(str(x.get("seed")), str(x.get("cls")), repr(x.get("trial", x.get("round"))))
                                                                                 for x in xs})[:4]))
    return why


def _real(rows, seed):
    why = []
    r = [x for x in rows if x.get("arm") == "real" and x.get("seed") == seed]
    mint = [x for x in r if x["kind"] == "mint"]
    got = _count((x.get("role"), x.get("what")) for x in mint)
    if set(got) != MINT_PROBES or any(n != 1 for n in got.values()):
        why.append("mint probes: missing %s, unexpected %s, repeated %s" % (sorted(MINT_PROBES - set(got)), sorted(set(got) - MINT_PROBES),
                                                                          sorted(k for k, n in got.items() if n != 1)))
    acc = [x["what"] + "/" + x["role"] for x in mint if x["outcome"] != "refused" and not str(x["outcome"]).startswith("refused")]
    if acc:
        why.append("mint accepted: %s" % acc)
    proofs = [x for x in r if x["kind"] == "proof"]
    per = _count(x.get("cls") for x in proofs)
    want = {c: TRIALS for c in TRIAL_CLASSES}
    if per != want:
        why.append("proof classes: %s != %s" % ({k: per.get(k, 0) for k in sorted(set(per) | set(want), key=str) if per.get(k, 0) != want.get(k)},
                                                 {k: want.get(k, 0) for k in sorted(set(per) | set(want), key=str) if per.get(k, 0) != want.get(k)}))
    for c in TRIAL_CLASSES:                                          # final review P2: distinct trials, not rows
        t = [x.get("trial") for x in proofs if x.get("cls") == c]
        if not all(_index(i) for i in t) or sorted(t) != list(range(TRIALS)):
            why.append("proof %s: trials %s, want each of 0..%d exactly once" % (c, sorted(map(repr, t)), TRIALS - 1))
    replays = {}
    for x in proofs:
        if x.get("cls") == "replay" and _index(x.get("trial")):
            replays.setdefault(x["trial"], []).append(x)
    for x in proofs:
        if x.get("cls") != "replay_first":
            continue
        twin = replays.get(x.get("trial")) if _index(x.get("trial")) else None
        if not twin or len(twin) != 1 or (x.get("nonce_sha256"), x.get("factor")) != (twin[0].get("nonce_sha256"), twin[0].get("factor")):
            why.append("proof replay_first trial %r: not the first use of its replay trial's proof" % (x.get("trial"),))
    for x in proofs:
        cls = x.get("cls")
        if cls in ACCEPTED_CLASSES:
            if not (x["outcome"] == "accepted" and x.get("stamped") == (x.get("factor_principal") or x.get("principal"))
                    and x.get("authenticated") is True):
                why.append("proof %s: valid proof not a working session (%s)" % (cls, x.get("outcome")))
        elif x["outcome"] != "refused":
            why.append("proof %s: ACCEPTED (claimed %s, stamped %s)" % (cls, x.get("claimed"), x.get("stamped")))
        if cls in CLAIMS_OTHER and x.get("claimed") == x.get("factor_principal"):
            why.append("proof %s: vacuous trial (claims the factor's own principal)" % cls)
    conc = [x for x in r if x["kind"] == "replay_concurrent"]
    if (sorted(x.get("round") for x in conc) != list(range(REPLAY_ROUNDS)) or any(x.get("presented") != REPLAY_WAYS for x in conc)
            or any(x.get("winners") != 1 for x in conc)):
        why.append("replay_concurrent: %s" % [(x.get("round"), x.get("presented"), x.get("winners")) for x in conc])
    toks = [x for x in r if x["kind"] == "token"]
    tok = {(x["cls"], x["call"]): x["obs"] for x in toks}
    pairs = [(c, k) for c in TOKENS for k in CALLS]
    if sorted(tok) != sorted(pairs) or len(toks) != len(pairs):
        why.append("token: %d rows, %d distinct pairs, want %d" % (len(toks), len(tok), len(pairs)))
    else:
        v = lambda c, k: tok[(c, k)]          # noqa: E731
        if not (isinstance(v("valid", "submit"), str) and not v("valid", "submit").startswith("unauthenticated")
                and (v("valid", "explain") or {}).get("tenant") == "tenant-a" and (v("valid", "explain") or {}).get("seen", 0) > 0
                and v("valid", "read_table")["seen"] > 0 and v("valid", "erase_candidates") == {"refused": None, "values": 1}
                and v("valid", "erase_values")["refused"] is None):
            why.append("token valid: positive control failed %s" % {k: v("valid", k) for k in CALLS})
        for c in BAD_TOKENS:
            if not (v(c, "submit") == "unauthenticated:invalid_session" and v(c, "explain") is None and v(c, "read_table") == {"n": 0, "seen": 0}
                    and v(c, "erase_candidates") == {"refused": "unauthorized", "values": 0}
                    and v(c, "erase_values") == {"refused": "unauthorized", "erased": None}):
                why.append("token %s: not refused on every call %s" % (c, {k: v(c, k) for k in CALLS}))
        o = "other_tenant"
        if not (isinstance(v(o, "submit"), str) and not v(o, "submit").startswith("unauthenticated")
                and (v(o, "explain") or {}).get("tenant") == "tenant-b" and (v(o, "explain") or {}).get("seen") == 0
                and v(o, "read_table")["seen"] == 0 and v(o, "erase_candidates") == {"refused": None, "values": 0}
                and v(o, "erase_values") == {"refused": None, "erased": 0}):
            why.append("token other_tenant: saw or changed tenant-a %s" % {k: v(o, k) for k in CALLS})
    after = [x for x in r if x["kind"] == "token_after"]
    if len(after) != 1 or after[0]["dana_values"] != 1 or after[0]["ended_returned"] is not True:
        why.append("token_after: %s" % after)
    org = [x for x in r if x["kind"] == "origin"]
    oc = _count((x.get("principal"), x.get("principal_kind"), x.get("channel")) for x in org)
    if set(oc) != ORIGIN_CASES or any(n != 1 for n in oc.values()):
        why.append("origin cases: missing %s, unexpected %s, repeated %s" % (sorted(ORIGIN_CASES - set(oc)), sorted(set(oc) - ORIGIN_CASES),
                                                                           sorted(k for k, n in oc.items() if n != 1)))
    for x in org:
        exp = ORIGIN.get((x["principal_kind"], x["channel"]), NON_USER)
        if x["code"] != "proposed" or (x["actor"], x["trust_origin"]) != exp:
            why.append("origin %s/%s: %s %s,%s != %s" % (x["principal"], x["channel"], x["code"], x["actor"], x["trust_origin"], exp))
    bodies = [x for x in r if x["kind"] == "body_identity"]
    body = {x["key"]: x for x in bodies}
    if (sorted(body) != sorted(BODY_KEYS) or len(bodies) != len(BODY_KEYS)
            or any(x["code"] != "identity_in_body" or x["event_actor"] is not None for x in body.values())):
        why.append("body_identity: %s" % {k: (x["code"], x["event_actor"]) for k, x in body.items()})
    inst = [x for x in r if x["kind"] == "install_lookalike"]
    if len(inst) != 1 or not str(inst[0]["outcome"]).startswith("refused"):
        why.append("install_lookalike: %s" % inst)
    known = {"mint", "proof", "replay_concurrent", "token", "token_after", "origin", "body_identity", "install_lookalike"}
    other = sorted({x["kind"] for x in r} - known)
    if other:
        why.append("unexpected row kinds: %s" % other)
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
    c["ctl_txn_clock"] = any((x["kind"] == "proof" and x.get("cls") == "expired_in_txn" and x["outcome"] == "accepted")
                             or (x["kind"] == "token" and x.get("cls") == "expired_in_txn" and x.get("call") == "read_table"
                                 and (x.get("obs") or {}).get("seen", 0) > 0) for x in pick("ctl_txn_clock"))
    return c


def verify(doc, rows):
    par = doc.get("params") or {}
    sd = par.get("seeds") or []
    pin = {k: par.get(k) for k in PINNED_PARAMS if par.get(k) != PINNED_PARAMS[k]}
    stray = sorted({str(x.get("seed")) for x in rows} - {str(s) for s in sd})
    arms = sorted({x.get("arm") for x in rows} - {"real"} - set(CONTROLS))
    ident = _identities(rows, sd)
    checks = {"params_not_pinned": pin, "seeds": len(set(map(str, sd))), "rows_of_other_seeds": stray, "unexpected_arms": arms,
              "trial_identity_failures": ident[:20], "trial_identity_failure_count": len(ident)}
    ok = not pin and len(set(map(str, sd))) >= MIN_SEEDS and len(set(map(str, sd))) == len(sd) and not stray and not arms and not ident
    for s in sd:
        why = _real(rows, s)
        ctl = _controls(rows, s)
        checks[str(s)] = {"real_failures": why[:20], "real_failure_count": len(why), "controls_caught": ctl}
        ok = ok and not why and all(ctl.values())
    return {"verdict": "PASS" if ok else "FAIL", "checks": checks}

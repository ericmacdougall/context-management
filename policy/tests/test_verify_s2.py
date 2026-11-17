"""verify/s2.py re-judges the S2 rows (step 2) without trusting the experiment: a synthetic row set built to the rules passes, and each
single break fails (an accepted attack, a vacuous attack, a valid proof that does not authenticate, a concurrent replay with two
winners, a bad token that reads data, another tenant's token that sees the seeded proposal, a missing token pair, a wrong origin, a
body origin accepted, a look-alike install accepted, a control that does not catch). Stdlib only."""
import copy
import hashlib
import importlib.util
import os
import unittest

POLICY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("verify_s2_under_test", os.path.join(POLICY, "verify", "s2.py"))
S2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S2)

# The coverage of THRESHOLDS.md amendment 7, written out here independently of verify/s2.py (delta review M2)
CLASSES = ["valid", "forged_mac", "body_edited", "other_key", "other_factor_id", "look_alike", "other_principal", "other_channel",
           "other_tenant", "expired", "far_future", "replay", "revoked_factor", "malformed", "expired_in_txn"]
TOKENS = ["valid", "forged", "expired", "revoked", "ended", "empty", "other_tenant", "expired_in_txn", "repeatable_read"]
CALLS = ["submit", "explain", "read_table", "erase_candidates", "erase_values"]
CONTROLS = ["ctl_bare_mint_80bb6a7", "ctl_bare_mint_0a20b64", "ctl_no_replay", "ctl_no_mac", "ctl_identity_from_body", "ctl_origin_from_body",
            "ctl_txn_clock"]
MINT = [("dc_idp", "bare_principal_open_session"), ("dc_app", "bare_principal_open_session"), ("dc_app", "open_session_with_proof"),
        ("dc_app", "end_session"), ("dc_app", "enroll_factor"), ("dc_idp", "enroll_factor"), ("dc_app", "insert_session"),
        ("dc_idp", "insert_session"), ("dc_app", "update_session"), ("dc_idp", "update_session"), ("dc_app", "read_sessions"),
        ("dc_idp", "read_sessions"), ("dc_app", "read_factors"), ("dc_idp", "read_factors"), ("dc_idp", "alter_factors"),
        ("dc_idp", "clear_used_proofs"), ("in_process", "bare_principal_issue")]
ORIGINS = [("alice", "human", "owner_ui", "user", "user_typed"), ("alice", "human", "owner_cli", "user", "user_typed"),
           ("bob", "human", "owner_ui", "user", "user_typed"), ("carol", "human", "owner_ui", "user", "user_typed"),
           ("bot", "agent", "agent_api", "assistant", "agent_generated"), ("x", "analyst", "agent_api", "assistant", "agent_generated")]
TRIALS, SEEDS = 12, [1, 2, 3]
CTL_PROOFS = [("ctl_no_replay", ["replay"], TRIALS), ("ctl_no_mac", ["forged_mac", "other_key"], TRIALS),
              ("ctl_identity_from_body", ["other_principal", "look_alike"], TRIALS), ("ctl_txn_clock", ["expired_in_txn"], 3)]
DOC = {"params": {"seeds": SEEDS, "trials_per_class": TRIALS, "classes": CLASSES, "tokens": TOKENS, "calls": CALLS, "controls": CONTROLS}}


def nonce(*parts):
    """A row's trial identity as exp/s2_sessions.py writes it: the first 16 hex digits of sha256 of the nonce drawn for the trial."""
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:16]


def good_rows(seeds=SEEDS):
    R = []
    for seed in seeds:
        add = lambda arm, **kw: R.append(dict(arm=arm, seed=seed, **kw))                 # noqa: E731
        for role, what in MINT:
            add("real", kind="mint", role=role, what=what, outcome="refused")
        for cls in CLASSES + ["revoked_principal"]:
            for i in range(TRIALS):
                fac, ns = "f-%d-%s-%d" % (seed, cls, i % 3), nonce(seed, cls, i)
                if cls == "valid":
                    add("real", kind="proof", cls=cls, trial=i, nonce_sha256=ns, factor=fac, outcome="accepted", factor_principal="alice",
                        claimed="alice", stamped="alice", authenticated=True)
                else:
                    claimed = "bob" if cls in S2.CLAIMS_OTHER else "alice"
                    add("real", kind="proof", cls=cls, trial=i, nonce_sha256=ns, factor=fac, outcome="refused", factor_principal="alice",
                        claimed=claimed, stamped=None)
                if cls == "replay":
                    add("real", kind="proof", cls="replay_first", trial=i, nonce_sha256=ns, factor=fac, outcome="accepted", principal="alice",
                        stamped="alice", authenticated=True)
        for rnd in range(3):
            add("real", kind="replay_concurrent", round=rnd, presented=8, winners=1, nonce_sha256=nonce(seed, "concurrent", rnd))
        for c in TOKENS:
            if c == "valid":
                obs = {"submit": "unauthorized", "explain": {"tenant": "tenant-a", "seen": 1}, "read_table": {"n": 2, "seen": 1},
                       "erase_candidates": {"refused": None, "values": 1}, "erase_values": {"refused": None, "erased": 0}}
            elif c == "other_tenant":
                obs = {"submit": "unauthorized", "explain": {"tenant": "tenant-b", "seen": 0}, "read_table": {"n": 2, "seen": 0},
                       "erase_candidates": {"refused": None, "values": 0}, "erase_values": {"refused": None, "erased": 0}}
            else:
                obs = {"submit": "unauthenticated:invalid_session", "explain": None, "read_table": {"n": 0, "seen": 0},
                       "erase_candidates": {"refused": "unauthorized", "values": 0}, "erase_values": {"refused": "unauthorized", "erased": None}}
            for k in CALLS:
                add("real", kind="token", cls=c, call=k, obs=obs[k])
        add("real", kind="token_after", ended_returned=True, dana_values=1)
        for pid, kind, ch, a, t in ORIGINS:
            add("real", kind="origin", principal=pid, principal_kind=kind, channel=ch, code="proposed", actor=a, trust_origin=t)
        for k in S2.BODY_KEYS:
            add("real", kind="body_identity", key=k, code="identity_in_body", event_actor=None)
        add("real", kind="install_lookalike", base="alice", look_alike="a1ice", outcome="refused:ValueError")
        # the control arms as exp/s2_sessions.py controls() writes them (GoodQA #16096 class B: every row of every arm is identified)
        add("ctl_bare_mint_80bb6a7", kind="mint", role="in_process", what="bare_principal_issue", outcome="accepted", stamped="alice")
        add("ctl_bare_mint_0a20b64", kind="mint", role="dc_idp", what="bare_principal_open_session", outcome="accepted", authenticated=True)
        for arm, classes, trials in CTL_PROOFS:
            for cls in classes:
                for i in range(trials):
                    ns = nonce(seed, arm, cls, i)
                    stamped = "bob" if arm == "ctl_identity_from_body" else "alice"
                    add(arm, kind="proof", cls=cls, trial=i, nonce_sha256=ns, factor="f-%d-%d" % (seed, i % 3), outcome="accepted",
                        factor_principal="alice", claimed="bob" if cls in S2.CLAIMS_OTHER else "alice", stamped=stamped, authenticated=True)
                    if cls == "replay":
                        add(arm, kind="proof", cls="replay_first", trial=i, nonce_sha256=ns, factor="f-%d-%d" % (seed, i % 3), outcome="accepted",
                            principal="alice", stamped="alice", authenticated=True)
        for pid, kind, ch, a, t in ORIGINS:
            add("ctl_origin_from_body", kind="origin", principal=pid, principal_kind=kind, channel=ch, code="proposed", actor=a, trust_origin=t)
        for k in S2.BODY_KEYS:
            add("ctl_origin_from_body", kind="body_identity", key=k, code="proposed" if k == "actor" else "identity_in_body",
                event_actor="user" if k == "actor" else None)
    return R


def _find(rows, **kw):
    return next(r for r in rows if all(r.get(k) == v for k, v in kw.items()))


class VerifyS2(unittest.TestCase):
    def _v(self, rows):
        return S2.verify(DOC, rows)["verdict"]

    def test_good_set_passes(self):
        self.assertEqual(self._v(good_rows()), "PASS")

    def _broken(self, change, **where):
        rows = copy.deepcopy(good_rows())
        change(_find(rows, **where))
        return self._v(rows)

    def _silenced(self, change, **where):
        """A control arm made silent: the change applied to EVERY matching row (one catching row is enough for a control)."""
        rows = copy.deepcopy(good_rows())
        for r in rows:
            if all(r.get(k) == v for k, v in where.items()):
                change(r)
        return self._v(rows)

    def test_each_break_fails(self):
        breaks = {
            "accepted attack": (lambda r: r.update(outcome="accepted", stamped="alice"), dict(arm="real", cls="other_key")),
            "vacuous attack": (lambda r: r.update(claimed="alice"), dict(arm="real", cls="look_alike")),
            "valid proof not authenticated": (lambda r: r.update(authenticated=False), dict(arm="real", cls="valid")),
            "valid proof stamped as another": (lambda r: r.update(stamped="bob"), dict(arm="real", cls="valid")),
            "accepted mint": (lambda r: r.update(outcome="accepted"), dict(arm="real", kind="mint")),
            "two concurrent winners": (lambda r: r.update(winners=2), dict(arm="real", kind="replay_concurrent")),
            "ended token reads": (lambda r: r.update(obs={"n": 2, "seen": 1}), dict(arm="real", kind="token", cls="ended", call="read_table")),
            "forged token submits": (lambda r: r.update(obs="unauthorized"), dict(arm="real", kind="token", cls="forged", call="submit")),
            "other tenant sees": (lambda r: r.update(obs={"tenant": "tenant-b", "seen": 1}), dict(arm="real", kind="token", cls="other_tenant", call="explain")),
            "other tenant erases": (lambda r: r.update(obs={"refused": None, "erased": 1}), dict(arm="real", kind="token", cls="other_tenant", call="erase_values")),
            "valid token positive control": (lambda r: r.update(obs=None), dict(arm="real", kind="token", cls="valid", call="explain")),
            "value lost": (lambda r: r.update(dana_values=0), dict(arm="real", kind="token_after")),
            "agent stamped user": (lambda r: r.update(actor="user", trust_origin="user_typed"), dict(arm="real", kind="origin", principal="bot")),
            "body actor accepted": (lambda r: r.update(code="proposed", event_actor="user"), dict(arm="real", kind="body_identity", key="actor")),
            "look-alike installed": (lambda r: r.update(outcome="accepted"), dict(arm="real", kind="install_lookalike")),
            "in-txn expired token reads": (lambda r: r.update(obs={"n": 2, "seen": 1}), dict(arm="real", kind="token", cls="expired_in_txn", call="read_table")),
            "repeatable-read token erases": (lambda r: r.update(obs={"refused": None, "erased": 1}), dict(arm="real", kind="token", cls="repeatable_read", call="erase_values")),
            "in-txn expired proof accepted": (lambda r: r.update(outcome="accepted", stamped="alice"), dict(arm="real", cls="expired_in_txn")),
        }
        for name, (change, where) in breaks.items():
            self.assertEqual(self._broken(change, **where), "FAIL", name)
        silenced = {
            "control 80bb6a7 silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_bare_mint_80bb6a7")),
            "control 0a20b64 not authenticated": (lambda r: r.update(authenticated=False), dict(arm="ctl_bare_mint_0a20b64")),
            "control replay silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_no_replay", cls="replay")),
            "control mac silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_no_mac")),
            "control identity stamps the factor's own": (lambda r: r.update(stamped="alice"), dict(arm="ctl_identity_from_body")),
            "control origin silent": (lambda r: r.update(event_actor="assistant"), dict(arm="ctl_origin_from_body", kind="body_identity", key="actor")),
            "control txn clock silent": (lambda r: r.update(outcome="refused"), dict(arm="ctl_txn_clock")),
        }
        for name, (change, where) in silenced.items():
            self.assertEqual(self._silenced(change, **where), "FAIL", name)

    def test_missing_rows_fail(self):
        for name, drop in (("token pair", lambda r: r["kind"] == "token" and r["cls"] == "revoked" and r["call"] == "erase_values"),
                           ("proof class", lambda r: r.get("cls") == "expired"),
                           ("concurrency", lambda r: r["kind"] == "replay_concurrent"),
                           ("a control arm", lambda r: r["arm"] == "ctl_no_mac"),
                           ("a body key", lambda r: r["kind"] == "body_identity" and r["arm"] == "real" and r["key"] == "trust_origin")):
            self.assertEqual(self._v([r for r in good_rows() if not drop(r)]), "FAIL", name)
        self.assertEqual(S2.verify(dict(DOC, params=dict(DOC["params"], seeds=[1, 2, 3, 4])), good_rows())["verdict"], "FAIL", "seed 4 missing")


def _drop_classes(doc, rows):
    """Reviewer 1: the declared classes removed with all their real-arm rows; only replay_first and revoked_principal remain."""
    keep = ("replay_first", "revoked_principal")
    doc["params"]["classes"] = []
    return [r for r in rows if not (r["arm"] == "real" and r["kind"] == "proof" and r["cls"] not in keep)]


def _one_mint_probe(doc, rows):
    """Reviewer 2: the 17 distinct mint probes replaced by 17 copies of one refused probe."""
    first = next(r for r in rows if r["arm"] == "real" and r["kind"] == "mint")
    return [dict(r, role=first["role"], what=first["what"]) if r["arm"] == "real" and r["kind"] == "mint" else r for r in rows]


def _no_agent_origin(doc, rows):
    """Reviewer 3: the agent-origin rows removed."""
    return [r for r in rows if not (r["kind"] == "origin" and r["principal_kind"] != "human")]


def _small_replay(doc, rows):
    """Own 1: concurrency reduced to one round of two proofs (one winner, so the old check saw nothing wrong)."""
    out = [r for r in rows if not (r["kind"] == "replay_concurrent" and r["round"] != 0)]
    return [dict(r, presented=2) if r["kind"] == "replay_concurrent" else r for r in out]


def _fewer_trials(doc, rows):
    """Own 2: one trial per class, declared as such in params (the old check took the trial count from the result)."""
    doc["params"]["trials_per_class"] = 1
    seen, out = set(), []
    for r in rows:
        if r["arm"] == "real" and r["kind"] == "proof":
            k = (r["seed"], r["cls"])
            if k in seen:
                continue
            seen.add(k)
        out.append(r)
    return out


def _token_class_dropped(doc, rows):
    """Own 3: the ended token class removed from params and rows (the old check took the token list from the result)."""
    doc["params"]["tokens"] = [t for t in doc["params"]["tokens"] if t != "ended"]
    return [r for r in rows if not (r["kind"] == "token" and r["cls"] == "ended")]


def _one_seed(doc, rows):
    """Own 4: a single seed declared and run (the old check accepted any nonempty seed list)."""
    first = doc["params"]["seeds"][0]
    doc["params"]["seeds"] = [first]
    return [r for r in rows if r["seed"] == first]


# _token_class_dropped did not PASS at d05d731: the old verifier raised KeyError on it (a crash, not a false pass); it stays as a case
MANIPULATIONS = (_drop_classes, _one_mint_probe, _no_agent_origin, _small_replay, _fewer_trials, _one_seed, _token_class_dropped)


class CoverageIsPinned(unittest.TestCase):
    """Delta review M2: at d05d731 verify/s2.py took required classes, trial counts and token pairs from the result document, counted
    mint rows without their identities and needed only a nonempty origin set, so each manipulation below PASSED there (red log
    policy/results/red/delta1_m2_verifier_d05d731.out, run on d05d731's own fixture). Each must FAIL."""

    def test_each_manipulation_fails(self):
        for m in MANIPULATIONS:
            with self.subTest(m.__name__):
                doc = copy.deepcopy(DOC)
                rows = m(doc, copy.deepcopy(good_rows()))
                self.assertEqual(S2.verify(doc, rows)["verdict"], "FAIL", m.__name__)

    def test_duplicated_and_unexpected_coverage_fails(self):
        rows = good_rows()
        extra = dict(_find(rows, arm="real", kind="origin", principal="bot"))
        self.assertEqual(S2.verify(DOC, rows + [extra])["verdict"], "FAIL", "repeated origin row")
        mint = dict(_find(rows, arm="real", kind="mint"), what="something_else")
        self.assertEqual(S2.verify(DOC, rows + [mint])["verdict"], "FAIL", "unexpected mint probe")
        self.assertEqual(S2.verify(DOC, rows + [dict(rows[0], arm="ctl_made_up")])["verdict"], "FAIL", "undeclared control arm")
        self.assertEqual(S2.verify(dict(DOC, params=dict(DOC["params"], seeds=[1, 2])), good_rows([1, 2]))["verdict"], "FAIL", "two seeds")


def _proofs(rows, seed=None, cls=None, arm="real"):
    return [r for r in rows if r["arm"] == arm and r["kind"] == "proof" and (seed is None or r["seed"] == seed) and (cls is None or r["cls"] == cls)]


def _copies_of_one(rows, renumber):
    """Every class's TRIALS rows replaced by copies of its first observation (renumber: the copies get trials 0..TRIALS-1 and keep the
    first's nonce). The final review's P2 probe is renumber=False."""
    out, first = [], {}
    for r in rows:
        if r["arm"] == "real" and r["kind"] == "proof":
            k = (r["seed"], r["cls"])
            first.setdefault(k, r)
            c = dict(first[k], trial=r.get("trial")) if renumber else dict(first[k])
            out.append(c)
        else:
            out.append(r)
    return out


def _p2_copies(doc, rows):
    """Final review P2: each class's 12 observations are 12 copies of one observation."""
    return _copies_of_one(rows, renumber=False)


def _p2_copies_renumbered(doc, rows):
    """Copies of one observation with distinct trial numbers (only the nonce identity shows they are one presentation)."""
    return _copies_of_one(rows, renumber=True)


def _p2_one_trial_twice(doc, rows):
    """One class has trial 0 twice and no trial 11 (counts still 12)."""
    r = next(x for x in _proofs(rows, 1, "expired") if x["trial"] == 11)
    first = next(x for x in _proofs(rows, 1, "expired") if x["trial"] == 0)
    r.update(trial=0, nonce_sha256=first["nonce_sha256"])
    return rows


def _p2_trial_gap(doc, rows):
    """One class's trials are 0..10 and 12."""
    next(x for x in _proofs(rows, 2, "other_key") if x["trial"] == 11)["trial"] = 12
    return rows


def _p2_seed_copied(doc, rows):
    """Seed 3's proof rows are seed 1's, relabelled (a seed that was never run)."""
    keep = [r for r in rows if not (r["seed"] == 3 and r["arm"] == "real" and r["kind"] == "proof")]
    return keep + [dict(r, seed=3) for r in _proofs(rows, 1)]


def _p2_replay_first_unlinked(doc, rows):
    """A replay_first row whose nonce is not its replay's (the first use and the replay were different proofs)."""
    next(x for x in _proofs(rows, 1, "replay_first") if x["trial"] == 4)["nonce_sha256"] = nonce("elsewhere")
    return rows


def _p2_replay_first_other_factor(doc, rows):
    """A replay_first row on another factor than its replay."""
    next(x for x in _proofs(rows, 2, "replay_first") if x["trial"] == 7)["factor"] = "f-other"
    return rows


def _p2_revoked_principal_copies(doc, rows):
    """The revoked_principal rows are copies of one observation."""
    out, first = [], {}
    for r in rows:
        if r["arm"] == "real" and r["kind"] == "proof" and r["cls"] == "revoked_principal":
            out.append(dict(first.setdefault(r["seed"], r)))
        else:
            out.append(r)
    return out


def _p2_concurrent_round_copied(doc, rows):
    """Rounds 1 and 2 of the concurrent replay are round 0's observation, renumbered."""
    out = []
    for r in rows:
        if r["kind"] == "replay_concurrent" and r["round"] != 0:
            r0 = next(x for x in rows if x["kind"] == "replay_concurrent" and x["seed"] == r["seed"] and x["round"] == 0)
            out.append(dict(r0, round=r["round"]))
        else:
            out.append(r)
    return out


def _p2_no_identity(doc, rows):
    """Rows without trial identities (as d05d731's experiment wrote them)."""
    return [{k: v for k, v in r.items() if k not in ("trial", "nonce_sha256")} for r in rows]


def _p2_nonce_shared_across_classes(doc, rows):
    """A refused-class row reuses a valid trial's nonce (one presentation counted in two classes)."""
    v = next(x for x in _proofs(rows, 1, "valid") if x["trial"] == 3)
    next(x for x in _proofs(rows, 1, "far_future") if x["trial"] == 3)["nonce_sha256"] = v["nonce_sha256"]
    return rows


P2_MANIPULATIONS = (_p2_copies, _p2_copies_renumbered, _p2_one_trial_twice, _p2_trial_gap, _p2_seed_copied, _p2_replay_first_unlinked,
                    _p2_replay_first_other_factor, _p2_revoked_principal_copies, _p2_concurrent_round_copied, _p2_no_identity,
                    _p2_nonce_shared_across_classes)


class TrialsAreIndependent(unittest.TestCase):
    """Final review P2: at a2d65fd verify/s2.py counted proof ROWS per class, so TRIALS copies of one observation passed as TRIALS
    independent trials. Each trial now carries its index and the identity of the nonce drawn for it; per seed and class the indices
    must be exactly 0..TRIALS-1, every nonce identity distinct across all proof rows of all seeds, except that a replay_first and its
    replay are the same presentation (same trial, nonce and factor). Each manipulation below PASSED at a2d65fd and must FAIL."""

    def test_good_set_passes(self):
        self.assertEqual(S2.verify(DOC, good_rows())["verdict"], "PASS")

    def test_each_duplication_fails(self):
        for m in P2_MANIPULATIONS:
            with self.subTest(m.__name__):
                doc = copy.deepcopy(DOC)
                rows = m(doc, copy.deepcopy(good_rows()))
                self.assertEqual(S2.verify(doc, rows)["verdict"], "FAIL", m.__name__)

    def test_a_trial_index_must_be_an_integer(self):
        for bad in ("0", 0.0, True, None, -1):
            with self.subTest(bad=repr(bad)):
                rows = copy.deepcopy(good_rows())
                next(x for x in _proofs(rows, 1, "valid") if x["trial"] == 0)["trial"] = bad
                self.assertEqual(S2.verify(DOC, rows)["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()

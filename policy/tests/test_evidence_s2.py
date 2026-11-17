"""Evidence identity properties of verify/s2.py (GoodQA #16096 class B; see tests/evidence_props.py). Design (THRESHOLDS amendment 7,
exp/s2_sessions.py), per seed: the real arm (17 mint probes, 17 proof classes x 12 trials, 3 concurrent rounds, 9 x 5 token pairs, one
token_after, 6 origin cases, 6 body keys, one install_lookalike) and the control arms as controls() writes them (one mint row each for
the two bare-mint controls; replay and its replay_first, forged_mac and other_key, other_principal and look_alike at 12 trials;
expired_in_txn at 3 trials for ctl_txn_clock; the 6 origin cases and 6 body keys for ctl_origin_from_body). The honest set is
tests/test_verify_s2.good_rows(). At ea4677a the real arm was already judged by identity; the control arms were judged by any(), so a
control's rows could be dropped, copied, renumbered or overwritten."""
import unittest

from tests.evidence_props import Fam, Props, load
from tests.test_verify_s2 import DOC, TRIALS, good_rows


def _f(name, kind, ident, index=None, end=None):
    return Fam(name, lambda r: r["kind"] in kind, ["arm", "seed", "kind"] + ident, index=index, seed="seed", arm="arm", end=end)


class S2Evidence(Props, unittest.TestCase):
    V = load("s2")
    FAMILIES = (_f("proof", ("proof",), ["cls", "trial"], index="trial", end=lambda s, r: 3 if r["arm"] == "ctl_txn_clock" else TRIALS),
                _f("mint", ("mint",), ["role", "what"]),
                _f("concurrent", ("replay_concurrent",), ["round"], index="round", end=lambda s, r: 3),
                _f("token", ("token",), ["cls", "call"]),
                _f("origin", ("origin",), ["principal", "principal_kind", "channel"]),
                _f("body", ("body_identity",), ["key"]),
                _f("single", ("token_after", "install_lookalike"), []))

    def doc(self):
        return DOC

    def build(self):
        return good_rows()

    def sample(self, fam):
        rows = self.rows()
        arm = {"proof": "ctl_no_mac", "mint": "ctl_bare_mint_0a20b64", "origin": "ctl_origin_from_body", "body": "ctl_origin_from_body"}.get(fam.name)
        if arm is None:
            return Props.sample(self, fam)
        pos = [k for k, r in enumerate(rows) if fam.pick(r) and r["arm"] == arm and r["seed"] == 2]
        return pos[len(pos) // 2], next(k for k, r in enumerate(rows) if fam.pick(r) and r["arm"] == "real")

    def test_control_arm_cut_to_one_catching_row(self):
        """The control arms as the old fixture had them: one catching row each (PASSED at ea4677a)."""
        rows, seen = [], set()
        for r in good_rows():
            if r["arm"] != "real" and r["kind"] == "proof":
                k = (r["arm"], r["seed"])
                if k in seen:
                    continue
                seen.add(k)
            rows.append(r)
        self.bad(rows, "control proof arms cut to one row", ("missing identity",))

    def test_silent_control_overwritten_by_a_catching_row(self):
        """ctl_no_mac made silent on seed 2 (every proof refused: FAIL), then one accepted copy of a trial added: at ea4677a any()
        took the copy and the control 'caught' (PASSED)."""
        rows = [dict(r, outcome="refused", stamped=None) if r["arm"] == "ctl_no_mac" and r["seed"] == 2 else r for r in good_rows()]
        self.assertEqual(self.v(rows).get("verdict"), "FAIL", "precondition: a silent control fails")
        one = next(r for r in good_rows() if r["arm"] == "ctl_no_mac" and r["seed"] == 2 and r["trial"] == 4)
        self.bad(rows + [dict(one)], "silent control overwritten by a catching copy", ("duplicate identity",))

    def test_catching_row_copied_from_another_seed(self):
        """ctl_no_replay silent on seed 3; one accepted replay row of seed 1 relabelled seed 3 replaces its refused twin (PASSED at
        ea4677a: the control nonce was never checked). The nonce identity now spans every arm."""
        rows = [dict(r, outcome="refused") if r["arm"] == "ctl_no_replay" and r["seed"] == 3 and r["cls"] == "replay" else r for r in good_rows()]
        src = next(r for r in good_rows() if r["arm"] == "ctl_no_replay" and r["seed"] == 1 and r["cls"] == "replay" and r["trial"] == 2)
        rows = [dict(src, seed=3) if r["arm"] == "ctl_no_replay" and r["seed"] == 3 and r["cls"] == "replay" and r["trial"] == 2 else r for r in rows]
        self.bad(rows, "catching replay copied from seed 1 into seed 3", ("nonce identity",))


if __name__ == "__main__":
    unittest.main()

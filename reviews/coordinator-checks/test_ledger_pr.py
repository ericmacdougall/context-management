"""Main's consistency checks on the ledger write-up PR head (docs only). Run from the wt-integ-ledger worktree."""
import re, subprocess, unittest
def show(path):
    return open(path, encoding="utf-8").read()
D, L, O = show("DESIGN.md"), show("LEDGER.md"), show("OPEN-QUESTIONS.md")
ALL = D + L + O + show("CLAIMS-AUDIT.md")
class Ledger(unittest.TestCase):
    def test_only_docs_changed(self):
        out = subprocess.run(["git", "diff", "--name-only", "b50f9c8a70a586e2f50cdb070bd5f7666856c1ec", "HEAD"], capture_output=True, text=True).stdout.split()
        self.assertTrue(out)
        self.assertTrue(all(p in {"DESIGN.md", "LEDGER.md", "CLAIMS-AUDIT.md", "OPEN-QUESTIONS.md"} or p.startswith(("reviews/", "eval/")) for p in out), out)
    def test_ingest_figures(self):
        self.assertRegex(ALL, r"800(/| of )960"); self.assertRegex(ALL, r"0(/| of )720")
        self.assertRegex(ALL, r"(?i)exactly.once[^.\n]{0,160}withdrawn|withdrawn[^.\n]{0,160}exactly.once")
    def test_scanner_heldout(self):
        self.assertRegex(ALL, r"483(/| of )1,?169"); self.assertRegex(ALL, r"150(/| of )750")
    def test_policy_final_review(self):
        self.assertRegex(ALL, r"P1[^\n]{0,80}P4|P1-P4|P1 to P4")
    def test_espilot_limits(self):
        self.assertRegex(ALL, r"0\.0053")
    def test_assumption_labels(self):
        self.assertRegex(ALL, r"(?i)parity rule[^\n]{0,400}ASSUMPTION|ASSUMPTION[^\n]{0,400}parity rule")
    def test_stage11_approval_and_later_unapproved(self):
        self.assertRegex(ALL, r"(?i)stage 11[^\n]{0,300}approv")
        self.assertRegex(ALL, r"(?i)unapproved|not approved|needs? (the )?user'?s? approval|User approval")
    def test_no_secret_shapes(self):
        for rx in (r"sk-ant-[A-Za-z0-9_-]{20,}", r"gh[pousr]_[A-Za-z0-9]{30,}", r"AKIA[0-9A-Z]{16}", r"BEGIN [A-Z ]*PRIVATE KEY"):
            self.assertIsNone(re.search(rx, ALL))
if __name__ == "__main__":
    unittest.main()

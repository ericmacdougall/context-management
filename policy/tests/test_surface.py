"""The public surface of `pol` has no optional safety dependency, no parameter through which a caller supplies identity, tenant, clock,
labels or recipient or switches a check off, no **kwargs, no unscoped read path, and no import of tests-only code. Control: a planted
package with one violation of each kind must be caught (otherwise the scanner itself is vacuous)."""
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from surface import scan  # noqa: E402

PLANTED = {
    "__init__.py": "",
    "a.py": textwrap.dedent('''
        class Enforcer:
            def __init__(self, policy_source, directory=None):
                pass
            def check(self, session, action, trusted=None):
                pass
        def decide(principal, action, **kw):
            pass
        def explain(state, req):
            pass
        def read_value(state, req):
            pass
    '''),
    "b.py": "from tests.kit import FixedClock\n",
    "_private.py": "def anything(principal, directory=None, flags=()):\n    pass\n",
}


class Surface(unittest.TestCase):
    def test_pol_public_surface_is_clean(self):
        v = scan("pol")
        self.assertEqual(v, [], "public-surface violations:\n" + "\n".join("%s %s %s" % x for x in v))

    def test_control_planted_violations_are_found(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "planted_pkg"))
        for fn, src in PLANTED.items():
            with open(os.path.join(d, "planted_pkg", fn), "w") as fh:
                fh.write(src)
        sys.path.insert(0, d)
        try:
            v = scan("planted_pkg")
        finally:
            sys.path.remove(d)
        rules = {(r, w.split(".")[-1] if r != "S5" else w, x) for r, w, x in v}
        want = {("S1", "__init__", "directory"), ("S2", "check", "trusted"), ("S2", "decide", "principal"), ("S3", "decide", "kw"),
                ("S4", "explain", "unscoped read exported"), ("S4", "read_value", "first parameter is not session"), ("S5", "b.py", "tests.kit")}
        self.assertTrue(want <= rules, "scanner missed: %s" % sorted(want - rules))
        self.assertFalse(any("_private" in w for _r, w, _x in v if _r != "S5"), "private module must not count as surface")


if __name__ == "__main__":
    unittest.main()

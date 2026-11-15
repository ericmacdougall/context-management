"""Run the review-fix tests against ONE code tree and write a stamped JSON report:
   cd <code dir> && python -m engine.tests.run_report <label> [module ...]
Each test records pass/fail and the first line of the failure, so the report shows WHY a test fails on old code."""
import json
import pathlib
import platform
import sys
import time
import unittest

import engine

MODULES = ["engine.tests.test_review_fixes"]


class Collect(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rows = {}

    def _first(self, err):
        return str(err[1]).strip().splitlines()[0][:200] if str(err[1]).strip() else type(err[1]).__name__

    def addSuccess(self, test):
        super().addSuccess(test)
        self.rows[test.id()] = {"outcome": "pass"}

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.rows[test.id()] = {"outcome": "fail", "reason": self._first(err)}

    def addError(self, test, err):
        super().addError(test, err)
        self.rows[test.id()] = {"outcome": "error", "reason": f"{err[0].__name__}: {self._first(err)}"}


def main():
    label = sys.argv[1]
    mods = sys.argv[2:] or MODULES
    root = pathlib.Path(engine.__file__).parent
    gc = root / "GIT_COMMIT"
    started = time.time()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromNames(mods))
    res = unittest.TextTestRunner(resultclass=Collect, verbosity=0, stream=open("/dev/null", "w")).run(suite)
    out = {"label": label, "code_dir": str(root.parent), "code_commit": json.loads(gc.read_text()) if gc.exists() else None,
           "modules": mods, "host": platform.node(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
           "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tests_run": res.testsRun,
           "failures": len(res.failures), "errors": len(res.errors), "tests": res.rows}
    p = pathlib.Path.home() / "emlab-16096/engine/results" / f"unit-{label}.json"
    p.write_text(json.dumps(out, indent=1))
    print(label, "run", res.testsRun, "failures", len(res.failures), "errors", len(res.errors))


if __name__ == "__main__":
    main()

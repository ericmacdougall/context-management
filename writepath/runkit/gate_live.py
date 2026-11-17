"""LABELLED TEST SEAM for the review-A live confirmation of exp/disk_gate.py (GoodQA #16096). Never used for a
stamped run, and nothing in the run kit calls it.

It runs `exp.disk_gate <command> [args]` unchanged except for one function: `_targets()`, which in a real run names
the stream's containers from WP_HOME/conf through wp.config, returns the two disposable containers named in
GATE_LIVE_ES and GATE_LIVE_PG instead. wp.config only accepts names starting with emlab2-wphard-, so a gate-test home
names nonexistent emlab2-wphard-gatefixnone-* containers in its conf: any path that bypasses the seam addresses
nothing. Everything the gate judges (inspect by ID, the cgroup files, ES's node stats, PG's statfs, the ledger) runs
as in a real run; after start the gate addresses the containers by the IDs in its run record, not through this seam.

usage (on the lab host, in a gate-test home):
  WP_HOME=~/emlab2-16096/gatefix/home GATE_LIVE_ES=emlab2-gatefix-es GATE_LIVE_PG=emlab2-gatefix-pg \\
      python3 runkit/gate_live.py init <reason>|start|check <label>|end|stop <reason>|stops|clear-stop <id> <reason>|\\
      abort <reason>|ledger-reset <reason>"""
import os
import pathlib
import sys

PREFIX = "emlab2-gatefix-"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(argv):
    es, pg = os.environ.get("GATE_LIVE_ES", ""), os.environ.get("GATE_LIVE_PG", "")
    if not (es.startswith(PREFIX) and pg.startswith(PREFIX)):
        sys.exit(f"gate_live: GATE_LIVE_ES and GATE_LIVE_PG must name {PREFIX}* containers")
    if "gatefix" not in pathlib.Path(os.environ.get("WP_HOME", "/")).parts:
        sys.exit("gate_live: WP_HOME must be a gate-test home (a path with a gatefix directory)")
    sys.path.insert(0, str(ROOT))
    from exp import disk_gate
    disk_gate._targets = lambda: (es, pg)
    return disk_gate.main(*argv)


if __name__ == "__main__":
    main(sys.argv[1:])

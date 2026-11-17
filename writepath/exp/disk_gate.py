"""Run-validity gate for run_all (THRESHOLDS amendment 17c; main's disk rule of 2026-11-17).

  start         refuse (exit 2) unless the host has >= START_GB free; record this stream's own usage as the baseline
  check NAME    before each experiment: own usage may grow by at most CAP_GB over the baseline, else exit 3 (the run
                stops itself and is VOID); a host under FLOOR_GB from other work is recorded, and the run continues
  end           VOID (exit 3) if the ES node logged a flood-stage event since start (it then refuses writes)
  operator stop: write a reason into WP_HOME/state/STOP; the next check ends the run as VOID
Own usage = du of this stream's Postgres and ES data directories (inside its own containers) plus WP_HOME. Sizes in GiB
(as df -h prints them). Prints numbers only."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from wp import config

START_GB, FLOOR_GB, CAP_GB = 17.0, 15.0, 2.0
GIB = 1024 ** 3
HOME = pathlib.Path(os.environ["WP_HOME"])
# NOT under WP_HOME/logs: that directory is a registered store whose files the erasure gate inventories (only *.log is
# allowed), so a harness file there makes every erasure report gate:unregistered residue (the 1f059f4 run was voided
# by exactly this).
STATE_DIR = HOME / "state"
STATE = STATE_DIR / "disk_gate.json"
STOP = STATE_DIR / "STOP"          # an operator stop: the next check ends the run cleanly as VOID (no process kill)


def _du(container, path):
    r = subprocess.run(["docker", "exec", container, "du", "-sb", path], capture_output=True, text=True, timeout=600)
    return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.strip() else None


def own():
    pg = _du(config.PG_CONTAINER, "/var/lib/postgresql/data")
    es = _du(config.ES_CONTAINER, "/usr/share/elasticsearch/data")
    r = subprocess.run(["du", "-sb", str(HOME)], capture_output=True, text=True, timeout=600)
    home = int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.strip() else None
    if None in (pg, es, home):
        raise SystemExit(f"disk_gate: cannot measure own usage (pg={pg} es={es} home={home})")
    return {"pg": pg, "es": es, "home": home, "total": pg + es + home}


def free_gb():
    return shutil.disk_usage(HOME).free / GIB


def main(cmd, name=""):
    f = free_gb()
    if cmd == "start":
        if f < START_GB:
            print(f"disk_gate BLOCKED start: host free {f:.1f} GiB < {START_GB}")
            sys.exit(2)
        o = own()
        STATE_DIR.mkdir(exist_ok=True)
        if STOP.exists():
            STOP.unlink()                  # a stop file left by an earlier run does not stop this one
        STATE.write_text(json.dumps({"base": o, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "floor_breaches": []}))
        print(f"disk_gate start free {f:.1f} GiB own {o['total'] / GIB:.2f} GiB (pg {o['pg'] / GIB:.2f} es {o['es'] / GIB:.2f} "
              f"home {o['home'] / GIB:.2f}) cap +{CAP_GB} GiB")
        return
    if cmd == "preflight":             # after setup: the erasure gate's inventory must be empty before any experiment
        from wp import db, registry
        from wp.es import ES
        unreg = registry.unregistered(db.admin(), db.keys_connect(), ES())
        print(f"disk_gate preflight unregistered {len(unreg)} {unreg[:10]}")
        if unreg:
            print("disk_gate BLOCKED: the store inventory is not empty before the run (every erasure would report residue)")
            sys.exit(2)
        return
    st = json.loads(STATE.read_text())
    if cmd == "check":
        if STOP.exists():
            print(f"disk_gate VOID: operator stop ({STOP}: {STOP.read_text().strip()[:200]}) before {name}")
            sys.exit(3)
        o = own()
        d = (o["total"] - st["base"]["total"]) / GIB
        print(f"disk_gate {name} free {f:.1f} GiB own +{d:.2f} GiB")
        if d > CAP_GB:
            print(f"disk_gate VOID: own usage grew {d:.2f} GiB > cap {CAP_GB} before {name}")
            sys.exit(3)
        if f < FLOOR_GB:
            st["floor_breaches"].append({"before": name, "free_gb": round(f, 2), "own_delta_gb": round(d, 2)})
            STATE.write_text(json.dumps(st))
            print(f"disk_gate FLOOR: host free {f:.1f} GiB < {FLOOR_GB} from other work; own usage within cap, continuing")
        return
    if cmd == "end":
        r = subprocess.run(["docker", "logs", "--since", st["since"], config.ES_CONTAINER], capture_output=True, text=True, timeout=300)
        flood = sum(1 for line in (r.stdout + r.stderr).splitlines() if "flood stage" in line.lower())
        o = own()
        print(f"disk_gate end free {f:.1f} GiB own +{(o['total'] - st['base']['total']) / GIB:.2f} GiB "
              f"floor_breaches {len(st['floor_breaches'])} es_flood_lines {flood}")
        if flood:
            print("disk_gate VOID: the ES node hit its flood stage during the run (writes were refused)")
            sys.exit(3)
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else "")

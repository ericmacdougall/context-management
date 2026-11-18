"""The gate's live-test experiment (GoodQA #16096, gate v6 live items GF16096-L1, L6, L7, L9). Never a stamped
experiment: it measures nothing; each mode makes the gate face one situation, and the gate's verdict is the result.

  ok             write one result row and exit 0 (VALID expected)
  sleep N        sleep N seconds, write one row, exit 0 (a run that stays alive, for lock and abort refusals)
  linger         start `sleep 120` in the run's process group and exit 0 (VOID expected: P11)
  selfkill       kill itself with SIGKILL, a signal the gate did not send (VOID expected: F-2)
  grant          as the cluster admin, GRANT DELETE ON events TO wp_app, then exit 0 (VOID expected: F-3); the
                 grant stays until wp.setup --reset, which the next admission (or the operator) runs

usage: python -I -B runkit/isolated.py exp.live_probe MODE [N]  (under exp.disk_gate run, or launch_one.sh)"""
import os
import signal
import subprocess
import sys
import time

from exp.common import write_result


def main(argv):
    mode = argv[0] if argv else ""
    if mode == "ok":
        write_result("live_probe", {"mode": "ok"})
    elif mode == "sleep":
        n = int(argv[1]) if len(argv) > 1 else 30
        time.sleep(max(0, min(n, 600)))
        write_result("live_probe", {"mode": "sleep", "seconds": n})
    elif mode == "linger":
        subprocess.Popen(["sleep", "120"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        write_result("live_probe", {"mode": "linger"})
    elif mode == "selfkill":
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    elif mode == "grant":
        from wp import config, db
        with db.connect(db=config.EVENTS_DB, role=config.PG_USER, application_name="live_probe") as c:
            c.execute("GRANT DELETE ON public.events TO wp_app")
            c.commit()
        write_result("live_probe", {"mode": "grant"})
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

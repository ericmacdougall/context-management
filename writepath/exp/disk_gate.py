"""Run-validity gate for run_all (THRESHOLDS amendment 17c; main's disk rule of 2026-11-17).

  start         refuse (exit 2) unless the host has >= START_GB free; record this stream's own usage as the baseline
  check NAME    before each experiment: own usage may grow by at most CAP_GB over the baseline, else exit 3 (the run
                stops itself and is VOID); a host under FLOOR_GB from other work is recorded, and the run continues
  end           VOID (exit 3) unless the absence of a flood stage and the cap were OBSERVED: VOID if a STOP file exists,
                the ES container cannot be inspected, `docker logs` fails or times out, the log shows a flood-stage
                line or a high-watermark-exceeded line (amendment 23a), own usage cannot be measured, or own usage grew
                over CAP_GB (review N2). Log coverage (step-1 delta review P1): start records the container id, its
                full-precision StartedAt, its log driver config, the boot id, the wall and monotonic clocks, and writes
                a nonce marker into the container's own log; end VOIDs on a different id or StartedAt, another boot, a
                wall-clock step over CLOCK_SLACK_S, an end marker it cannot write or that does not appear, or a start
                marker missing from the RETAINED log (rotated, truncated or replaced), and judges only the lines between
                the two markers. The log check has log-line granularity: ES repeats these warnings about every 30 s,
                so a run shorter than that can fall between two lines.
  operator stop: write a reason into WP_HOME/state/STOP; the next check (or end) ends the run as VOID
Any failure to measure own usage at check or end is VOID (exit 3), never a pass.
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

START_GB, FLOOR_GB, CAP_GB = 20.0, 15.0, 2.0   # start line 20 GB from amendment 24 (17 GB before)
GIB = 1024 ** 3
HOME = pathlib.Path(os.environ["WP_HOME"])
# NOT under WP_HOME/logs: that directory is a registered store whose files the erasure gate inventories (only *.log is
# allowed), so a harness file there makes every erasure report gate:unregistered residue (the 1f059f4 run was voided
# by exactly this).
STATE_DIR = HOME / "state"
STATE = STATE_DIR / "disk_gate.json"
STOP = STATE_DIR / "STOP"          # an operator stop: the next check ends the run cleanly as VOID (no process kill)


def _stop_reason():
    """Who wrote the STOP file, from its own text (the launcher's flood watcher writes 'flood watcher: ...'; an
    operator writes a reason). An empty file is reported as such, never attributed to an operator."""
    t = STOP.read_text().strip()[:200] if STOP.exists() else ""
    return f"written reason: {t}" if t else "no reason written in the file (writer unknown)"


INSPECT_FMT = "{{.Id}}|{{.State.StartedAt}}|{{json .HostConfig.LogConfig}}"
MARK = "wp16096-disk-gate"
CLOCK_SLACK_S = 2.0      # wall-clock and monotonic elapsed may differ by this much before the run is VOID
_sleep = time.sleep


def _identity():
    """(container id, full-precision StartedAt, log config JSON) of this stream's ES container, or None."""
    r = subprocess.run(["docker", "inspect", "-f", INSPECT_FMT, config.ES_CONTAINER], capture_output=True, text=True, timeout=60)
    parts = r.stdout.strip().split("|", 2) if r.returncode == 0 else []
    return tuple(parts) if len(parts) == 3 and all(parts) else None


def _boot_id():
    try:
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _mark(kind, nonce):
    """Write a marker line into the ES container's own log (PID 1's stdout, which the log driver records)."""
    line = f"{MARK} {kind} {nonce}"
    r = subprocess.run(["docker", "exec", config.ES_CONTAINER, "sh", "-c", f"echo {line} > /proc/1/fd/1"],
                       capture_output=True, text=True, timeout=60)
    return line if r.returncode == 0 else None


def _window_lines(anchor, end_line):
    """The ES log lines between this run's start and end markers, judged from the RETAINED log with the driver's
    timestamps. Returns (lines, why_not): why_not names the doubt when coverage is not established."""
    since = str(int(anchor["wall0"]) - 600)
    for _ in range(20):
        r = subprocess.run(["docker", "logs", "--timestamps", "--since", since, config.ES_CONTAINER], capture_output=True,
                           text=True, timeout=300)
        if r.returncode != 0:
            return None, f"docker logs exited {r.returncode}; the absence of a flood stage was not observed"
        rows = [ln.split(" ", 1) for ln in (r.stdout + "\n" + r.stderr).splitlines() if " " in ln]
        t_start = [ts for ts, txt in rows if txt.strip() == anchor["start_line"]]
        t_end = [ts for ts, txt in rows if txt.strip() == end_line]
        if t_start and t_end:
            lo, hi = t_start[0], t_end[0]
            return [txt.lower() for ts, txt in rows if lo <= ts <= hi], None
        if not t_start:
            return None, ("the start marker is not in the retained ES log (rotated, truncated or replaced): log coverage "
                          "of the run is not established")
        _sleep(0.5)
    return None, "the end marker did not appear in the ES log within 10 s: log coverage of the run's end is not established"


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


def void(why):
    print(f"disk_gate VOID: {why}")
    sys.exit(3)


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
        # review P1: pin the container's identity and the clocks, and anchor the window in the container's own log
        ident = _identity()
        if ident is None:
            print("disk_gate BLOCKED start: cannot inspect the ES container (identity and log driver unknown)")
            sys.exit(2)
        nonce = os.urandom(8).hex()
        anchor = {"cid": ident[0], "started": ident[1], "log_config": ident[2], "nonce": nonce,
                  "wall0": time.time(), "mono0": time.monotonic(), "boot": _boot_id()}
        anchor["start_line"] = _mark("start", nonce)
        if anchor["start_line"] is None:
            print("disk_gate BLOCKED start: cannot write the start marker into the ES container's log")
            sys.exit(2)
        STATE.write_text(json.dumps({"base": o, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "floor_breaches": [],
                                     "anchor": anchor}))
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
            print(f"disk_gate VOID: STOP file ({STOP}), {_stop_reason()}, before {name}")
            sys.exit(3)
        try:
            o = own()
        except SystemExit as e:
            void(f"own usage not measured before {name} ({e})")
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
        # Fail closed (review N2): the run is valid only if the absence of a flood stage was OBSERVED (the log read
        # succeeded and covers the whole run) and own usage was measured and stayed within the cap.
        if STOP.exists():
            void(f"STOP file, {_stop_reason()}, during the run")
        # review P1: log coverage is established, not assumed: the same container (id and full-precision start time),
        # the same boot, no host clock step, and both of this run's markers in the RETAINED log; any doubt is VOID
        a = st.get("anchor")
        if not a:
            void("no log anchor recorded at start (gate state from an older gate); log coverage unknown")
        try:
            ident = _identity()
            if ident is None:
                void("cannot inspect the ES container at end; log coverage unknown")
            if ident[0] != a["cid"]:
                void("the ES container was replaced during the run (different container id); log coverage unknown")
            if ident[1] != a["started"]:
                void(f"the ES container restarted during the run (StartedAt {ident[1]} != {a['started']}); log coverage unknown")
            if _boot_id() != a["boot"]:
                void("the host rebooted during the run; log coverage unknown")
            step = (time.time() - a["wall0"]) - (time.monotonic() - a["mono0"])
            if abs(step) > CLOCK_SLACK_S:
                void(f"the host wall clock stepped {step:+.1f} s during the run; log timestamps cannot bound the window")
            end_line = _mark("end", a["nonce"])
            if end_line is None:
                void("cannot write the end marker into the ES container's log; log coverage unknown")
            lines, why = _window_lines(a, end_line)
        except (OSError, subprocess.TimeoutExpired) as e:
            void(f"cannot read the ES container's state or log ({type(e).__name__})")
        if why:
            void(why)
        flood = sum(1 for line in lines if "flood stage" in line)
        # amendment 23a: past the HIGH watermark the node will not allocate new primaries, so a new index stays red and
        # writes to it fail (d3 at 31112b4: 503 unavailable_shards_exception). The low watermark only stops replicas.
        high = sum(1 for line in lines if "high disk watermark" in line and "exceeded" in line)
        try:
            o = own()
        except SystemExit as e:
            void(f"own usage not measured at end ({e})")
        d = (o["total"] - st["base"]["total"]) / GIB
        print(f"disk_gate end free {f:.1f} GiB own +{d:.2f} GiB floor_breaches {len(st['floor_breaches'])} es_flood_lines {flood} "
              f"es_high_watermark_lines {high}")
        if flood:
            void("the ES node hit its flood stage during the run (writes were refused)")
        if high:
            void("the ES node passed its high disk watermark during the run (new shards were not allocated)")
        if d > CAP_GB:
            void(f"own usage grew {d:.2f} GiB > cap {CAP_GB} by the end of the run")
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else "")

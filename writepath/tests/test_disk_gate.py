"""Offline test of exp/disk_gate.py (no stores, no real docker): the run-validity gate voids every run it is meant to
void (review-2 N2, step-1 delta review P1/P2, review A findings A1/A2/A3; design in runkit/GATE-DESIGN.md).

docker and du are FAKES on PATH: this same file, run as `python3 test_disk_gate.py --fake docker|du ...`, backed by a
scenario directory (FAKE_DOCKER_STATE). The fake keeps, per container, an append-only log of (driver timestamp, stream,
text) lines, a docker events file, and the inspect fields; `docker exec ... sh -c "echo X > /proc/1/fd/N"` appends X
to that stream as a real container would; `docker logs` serves the retained log (rotation and truncation drop its
head) or follows it by position; `docker events` streams the events. Faults are injected by the test (lines, clock
steps, restarts, replacements, pauses, rotation) or by triggers that fire inside a given docker call. The gate runs
in-process and spawns its real companion process, which uses the same fakes.

Failing-first: DISK_GATE_UNDER_TEST names another gate file (runkit/failing_first.sh runs the 7b4839b blob, the gate
review A inspected). Tests named test_a1_*, test_a2_*, test_a3_* fail there on an exit-code assertion; tests that need
a command the old gate lacks are skipped there, never counted as red.
usage (from writepath/): python3 -m unittest tests.test_disk_gate -v"""
import fcntl
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import signal
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
GIB = 1024 ** 3
MARK = "wp16096-disk-gate"
FLOOD = "WARN flood stage disk watermark [15gb] exceeded on [node]"
# amendment 23a: the real line ES logged at 15:54:43Z during d3 at 31112b4 (node id shortened)
HIGH = ('{"log.level": "WARN","message":"high disk watermark [17gb] exceeded on [node][7b751d48f8c8]'
        '[/usr/share/elasticsearch/data] free: 15.7gb[2.4%], shards will be relocated away from this node"}')
LOW = ('{"log.level": "INFO","message":"low disk watermark [18gb] exceeded on [node][7b751d48f8c8]'
       '[/usr/share/elasticsearch/data] free: 17.7gb[2.7%], replicas will not be assigned to this node"}')


# ------------------------------------------------------------------------------------------------------- fake docker
def _complete_lines(p):
    try:
        data = p.read_text()
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in data.split("\n")[:-1] if x.strip()]


class Fake:
    """The scenario directory: state.json (containers, flags, triggers, clock offset), log.<id>, events."""

    def __init__(self, root):
        self.root = pathlib.Path(root)

    @classmethod
    def create(cls, root):
        f = cls(root)
        (f.root / "state.json").write_text(json.dumps({"c": {}, "clock": 0.0, "flags": {}, "triggers": [], "calls": {},
                                                       "home_du": GIB}))
        (f.root / "events").touch()
        return f

    def st(self):
        return json.loads((self.root / "state.json").read_text())

    def locked(self, fn):
        with open(self.root / "lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = self.st()
            r = fn(st)
            tmp = self.root / f"state.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(st))
            os.replace(tmp, self.root / "state.json")
            return r

    def ts(self, st=None):
        t = time.time() + (st or self.st())["clock"]
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%09dZ" % min(int((t % 1) * 1e9), 999999999)

    def norm(self, s):
        if s is None:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", s):
            t = float(s)
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%09dZ" % min(int((t % 1) * 1e9), 999999999)
        return s

    def resolve(self, st, ref):
        for cid, c in st["c"].items():
            if not c["gone"] and (cid == ref or c["name"] == ref or (len(ref) >= 12 and cid.startswith(ref))):
                return cid
        return None

    def lines(self, cid):
        return _complete_lines(self.root / f"log.{cid}")

    def events(self):
        return _complete_lines(self.root / "events")

    def _append(self, path, obj):
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")

    def _event(self, st, cid, action, typ="container"):
        self._append(self.root / "events", {"Type": typ, "Action": action, "status": action, "id": cid,
                                            "Actor": {"ID": cid, "Attributes": {"name": st["c"][cid]["name"]}},
                                            "time": int(time.time())})

    # --- operations (tests call them directly; triggers call them by name inside a docker call)
    def add(self, name, data_path, size):
        cid = os.urandom(32).hex()

        def f(st):
            st["c"][cid] = {"name": name, "started": self.ts(st), "running": True, "paused": False, "restarts": 0,
                            "gone": False, "logcfg": {"Type": "json-file", "Config": {}}, "tty": False,
                            "du": {data_path: size}, "kept": 0, "drop": []}
        self.locked(f)
        (self.root / f"log.{cid}").touch()
        return cid

    def line(self, cid, text, s="o", drop=False, ts=None):
        def f(st):
            if drop:
                st["c"][cid]["drop"].append(len(self.lines(cid)))
            self._append(self.root / f"log.{cid}", {"t": ts or self.ts(st), "s": s, "x": text})
        self.locked(f)

    def event(self, cid, action, typ="container"):
        self.locked(lambda st: self._event(st, cid, action, typ))

    def restart(self, cid, started=None):
        def f(st):
            for a in ("kill", "die", "stop", "start", "restart"):
                self._event(st, cid, a)
            c = st["c"][cid]
            c["started"], c["restarts"] = started or self.ts(st), c["restarts"] + 1
        self.locked(f)

    def replace(self, name):
        def f(st):
            old = self.resolve(st, name)
            for a in ("kill", "die", "destroy"):
                self._event(st, old, a)
            st["c"][old].update(gone=True, running=False)
            return st["c"][old]["du"]
        du = self.locked(f)
        path, size = next(iter(du.items()))
        return self.add(name, path, size)

    def pause(self, cid):
        self.event(cid, "pause")
        self.event(cid, "unpause")

    def rotate(self, cid):
        n = len(self.lines(cid))
        self.locked(lambda st: st["c"][cid].update(kept=n))

    def set(self, cid, **kw):
        self.locked(lambda st: st["c"][cid].update(kw))

    def flag(self, **kw):
        self.locked(lambda st: st["flags"].update(kw))

    def clock(self, offset):
        self.locked(lambda st: st.update(clock=offset))

    def write_file(self, path, text):
        pathlib.Path(path).write_text(text)

    def trigger(self, on, n, *do):
        self.locked(lambda st: (st["triggers"].append({"on": on, "n": st["calls"].get(on, 0) + n, "do": list(do)})))

    def fire(self, kind):
        def f(st):
            st["calls"][kind] = st["calls"].get(kind, 0) + 1
            return [t["do"] for t in st["triggers"] if t["on"] == kind and t["n"] == st["calls"][kind]]
        for do in self.locked(f):
            getattr(self, do[0])(*do[1:])


def fake_main(argv):
    F = Fake(os.environ["FAKE_DOCKER_STATE"])
    prog, args = argv[0], argv[1:]
    if prog == "du":
        F.fire("du")
        st = F.st()
        if st["flags"].get("du_rc"):
            return 1
        print(f"{st['home_du']}\t{args[-1]}")
        return 0
    cmd, args = args[0], args[1:]
    if cmd == "inspect":
        fmt = None
        if args[0] == "-f":
            fmt, args = args[1], args[2:]
        st = F.st()
        cid = F.resolve(st, args[0])
        if cid is None:
            print("Error: No such object: " + args[0], file=sys.stderr)
            return 1
        c = st["c"][cid]
        if fmt == "{{.State.StartedAt}}":
            print(c["started"])
        elif fmt:                                   # the 7b4839b gate's format
            print(f"{cid}|{c['started']}|{json.dumps(c['logcfg'])}")
        else:
            print(json.dumps([{"Id": cid, "Name": "/" + c["name"], "RestartCount": c["restarts"],
                               "State": {"Running": c["running"], "Paused": c["paused"], "StartedAt": c["started"]},
                               "HostConfig": {"LogConfig": c["logcfg"]}, "Config": {"Tty": c["tty"]}}]))
        return 0
    if cmd == "exec":
        ref, ex = args[0], args[1:]
        kind = "du" if ex[:1] == ["du"] else "exec"
        F.fire(kind)
        st = F.st()
        cid = F.resolve(st, ref)
        if cid is None or not st["c"][cid]["running"]:
            print("Error response from daemon: container is not running", file=sys.stderr)
            return 1
        text = " ".join(ex)
        F.event(cid, "exec_create: " + text)
        F.event(cid, "exec_start: " + text)
        rc = 0
        if kind == "du":
            if st["flags"].get("du_rc"):
                rc = 1
            else:
                print(f"{st['c'][cid]['du'].get(ex[-1], 0)}\t{ex[-1]}")
        else:
            m = re.fullmatch(r"echo (.*) > /proc/1/fd/([12])", ex[2]) if ex[:2] == ["sh", "-c"] and len(ex) == 3 else None
            if m is None:
                rc = 126
            elif not st["flags"].get("echo_lost"):
                F.line(cid, m.group(1), "o" if m.group(2) == "1" else "e")
        F.event(cid, "exec_die")
        return rc
    if cmd == "logs":
        follow = stamps = False
        since = until = tail = ref = None
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-f", "--follow"):
                follow = True
            elif a in ("-t", "--timestamps"):
                stamps = True
            elif a in ("--since", "--until", "--tail", "-n"):
                since, until, tail = ((args[i + 1], until, tail) if a == "--since" else
                                      (since, args[i + 1], tail) if a == "--until" else (since, until, args[i + 1]))
                i += 1
            else:
                ref = a
            i += 1
        if not follow:
            F.fire("logs_read")
        st = F.st()
        if st["flags"].get("logs_rc"):
            print("Error response from daemon: fake failure", file=sys.stderr)
            return st["flags"]["logs_rc"]
        if not follow and st["flags"].get("logs_sleep"):
            time.sleep(st["flags"]["logs_sleep"])
        cid = F.resolve(st, ref)
        if cid is None:
            print("Error: No such container: " + str(ref), file=sys.stderr)
            return 1
        since, until = F.norm(since), F.norm(until)

        def ok(r):
            return (since is None or r["t"] >= since) and (until is None or r["t"] <= until)

        def show(r):
            out = sys.stdout if r["s"] == "o" else sys.stderr
            out.write((r["t"] + " " if stamps else "") + r["x"] + "\n")
            out.flush()
        L, c = F.lines(cid), st["c"][cid]
        hist = [r for r in L[c["kept"]:] if ok(r)]
        if tail not in (None, "all"):
            hist = hist[len(hist) - int(tail):] if int(tail) else []
        for r in hist:
            show(r)
        if not follow:
            return 0
        pos, gen = len(L), c["started"]
        while True:
            st = F.st()
            c, L = st["c"][cid], F.lines(cid)
            for k in range(pos, len(L)):
                if k not in c["drop"] and ok(L[k]):
                    show(L[k])
            pos = len(L)
            if c["gone"] or not c["running"] or c["started"] != gen or st["flags"].get("kill_followers"):
                return 0
            time.sleep(0.02)
    if cmd == "events":
        ids, i = set(), 0
        while i < len(args):
            if args[i] == "--filter":
                k, _, v = args[i + 1].partition("=")
                if k == "container":
                    ids.add(F.resolve(F.st(), v) or v)
                i += 2
            else:
                i += 2 if args[i] == "--format" else 1
        pos = len(F.events())
        while True:
            st = F.st()
            if st["flags"].get("kill_followers"):
                return 0
            evs = F.events()
            for e in evs[pos:]:
                if e["Actor"]["ID"] in ids:
                    sys.stdout.write(json.dumps(e) + "\n")
                    sys.stdout.flush()
            pos = len(evs)
            time.sleep(0.02)
    print(f"fake docker: unsupported {cmd}", file=sys.stderr)
    return 2


# ------------------------------------------------------------------------------------------------------------- tests
class Gate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        home = pathlib.Path(cls.tmp) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        bindir = pathlib.Path(cls.tmp) / "bin"
        bindir.mkdir()
        for prog in ("docker", "du"):
            p = bindir / prog
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{pathlib.Path(__file__).resolve()}" --fake {prog} "$@"\n')
            p.chmod(0o755)
        cls.env = mock.patch.dict(os.environ, {"WP_HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
                                               "PYTHONPATH": str(HERE), "FAKE_DOCKER_STATE": ""})
        cls.env.start()
        sys.path.insert(0, str(HERE))
        src = os.environ.get("DISK_GATE_UNDER_TEST") or str(HERE / "exp" / "disk_gate.py")
        spec = importlib.util.spec_from_file_location("disk_gate_under_test", src)
        cls.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.g)
        cls.home = home
        cls.new = hasattr(cls.g, "open_stops")      # the ledger gate; the 7b4839b gate lacks it

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(self.home / "state", ignore_errors=True)
        root = pathlib.Path(tempfile.mkdtemp(dir=self.tmp))
        os.environ["FAKE_DOCKER_STATE"] = str(root)
        self.F = Fake.create(root)
        self.es = self.F.add("emlab2-wphard-es", "/usr/share/elasticsearch/data", GIB)
        self.pg = self.F.add("emlab2-wphard-pg", "/var/lib/postgresql/data", GIB)
        self.free = 30
        patches = [mock.patch.object(shutil, "disk_usage", lambda p: SimpleNamespace(
            total=100 * GIB, used=(100 - self.free) * GIB, free=self.free * GIB))]
        if hasattr(self.g, "_sleep"):
            patches.append(mock.patch.object(self.g, "_sleep", lambda s: time.sleep(min(s, 0.02))))
        for name, v in (("SYNC_TIMEOUT_S", 15.0), ("CLOSE_TIMEOUT_S", 15.0)):
            if hasattr(self.g, name):
                patches.append(mock.patch.object(self.g, name, v))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._reap)

    def _reap(self):
        """Stop the companions THIS test's gate started (test-owned processes only)."""
        self.F.flag(kill_followers=True)
        runs = self.home / "state" / "gate-runs"
        pids = []
        for d in (runs.iterdir() if runs.exists() else []):
            (d / "stop").touch()
            try:
                pids.append(json.loads((d / "run.json").read_text())["companion_pid"])
            except (OSError, ValueError, KeyError):
                pass
        t0 = time.monotonic()
        while pids and time.monotonic() - t0 < 5:
            pids = [p for p in pids if _alive(p)]
            time.sleep(0.05)
        for p in pids:
            try:
                os.killpg(p, signal.SIGKILL)
            except OSError:
                pass

    # helpers
    def call(self, *args):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.g.main(*args)
            return 0, buf.getvalue()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) or e.code is None else 1
            return code or 0, buf.getvalue() + ("" if isinstance(e.code, int) or e.code is None else str(e.code))

    def start(self):
        rc, out = self.call("start")
        self.assertEqual(rc, 0, f"start should admit the run: {out.strip()[-300:]}")

    def line(self, text, s="o", **kw):
        self.F.line(self.es, text, s, **kw)

    def stop_file(self, text):
        (self.home / "state").mkdir(exist_ok=True)
        (self.home / "state" / "STOP").write_text(text)

    def open_stop_ids(self):
        return [s["id"] for s in self.g.open_stops()] if self.new else None

    def ack(self, reason, ids=None):
        """Acknowledge stops through the gate's own interface: by stop id (ledger gate), or the 7b4839b clear-stop."""
        if not self.new:
            return self.call("clear-stop", reason)
        for i in (self.open_stop_ids() if ids is None else ids):
            self.call("clear-stop", i, reason)

    def assertVoid(self, rc, out, *why):
        """VOID; on the ledger gate also for the stated reason(s), so a test cannot pass on an incidental failure."""
        self.assertEqual(rc, 3, f"expected VOID (exit 3), got {rc!r}: {out.strip()[-300:]}")
        self.assertIn("VOID", out)
        for w in (why if self.new else ()):
            self.assertIn(w, out, f"VOID, but not for the reason under test ({w!r})")

    def assertValid(self, rc, out):
        self.assertEqual(rc, 0, f"expected a valid run (exit 0), got {rc!r}: {out.strip()[-300:]}")

    def need_new(self):
        if not self.new:
            self.skipTest("needs a command or knob the 7b4839b gate lacks (not counted as red)")

    # ---- positive controls
    def test_clean_run_passes(self):
        self.start()
        self.assertValid(*self.call("check", "x"))
        self.assertValid(*self.call("end"))

    def test_flood_line_voids(self):
        self.start()
        self.line(FLOOD)
        self.assertVoid(*self.call("end"))

    def test_high_watermark_line_voids(self):
        self.start()
        self.line(HIGH)
        self.assertVoid(*self.call("end"))

    def test_high_watermark_on_stderr_voids(self):
        self.start()
        self.line(HIGH, s="e")
        self.assertVoid(*self.call("end"))

    def test_low_watermark_line_does_not_void(self):
        self.start()
        self.line(LOW)
        self.assertValid(*self.call("end"))

    def test_hyphenated_flood_stage_line_voids(self):
        # the wording Elastic's "Fix watermark errors" page gives (fetched 2026-11-17): "flood-stage watermark [95%]
        # exceeded on ..."; the gate must not depend on one spelling of the flood-stage message
        self.start()
        self.line('{"log.level": "WARN","message":"flood-stage watermark [95%] exceeded on [node][7b751d48f8c8]'
                  '[/usr/share/elasticsearch/data] free: 1gb[4%], all indices on this node will be marked read-only"}')
        self.assertVoid(*self.call("end"), "flood stage")

    def test_flood_stage_setting_name_does_not_void(self):
        # a settings line naming watermark.flood_stage is not a watermark event (guards against over-normalising)
        self.start()
        self.line('{"log.level": "INFO","message":"updating [cluster.routing.allocation.disk.watermark.flood_stage]'
                  ' from [95%] to [1gb]"}')
        self.assertValid(*self.call("end"))

    def test_flood_line_before_the_run_does_not_void(self):
        self.line(FLOOD)
        self.start()
        self.assertValid(*self.call("end"))

    # ---- amendment 24 start line
    def test_start_under_20gb_blocked(self):
        self.free = 19
        rc, out = self.call("start")
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)

    def test_start_at_20gb_allowed(self):
        self.free = 20
        self.start()

    # ---- review-2 N2
    def test_log_read_failure_voids(self):
        self.start()
        self.F.flag(logs_rc=1)
        self.assertVoid(*self.call("end"))

    def test_log_read_timeout_voids(self):
        self.need_new()
        self.start()
        self.F.flag(logs_sleep=5)
        with mock.patch.object(self.g, "DOCKER_TIMEOUT_S", 1):
            self.assertVoid(*self.call("end"))

    def test_cap_breach_voids(self):
        self.start()
        self.assertValid(*self.call("check", "last"))
        self.F.set(self.es, du={"/usr/share/elasticsearch/data": 4 * GIB})
        self.assertVoid(*self.call("end"))

    def test_usage_not_measured_at_end_voids(self):
        self.start()
        self.F.flag(du_rc=1)
        self.assertVoid(*self.call("end"))

    def test_usage_not_measured_at_check_voids(self):
        self.start()
        self.F.flag(du_rc=1)
        self.assertVoid(*self.call("check", "x"))

    # ---- STOP (review P2, amendment H4)
    def test_stop_written_during_run_voids(self):
        self.start()
        self.stop_file("flood stage seen\n")
        self.assertVoid(*self.call("end"))

    def test_stop_reason_names_the_watcher_not_an_operator(self):
        self.start()
        self.stop_file("flood watcher: flood stage or high watermark in the ES log at 10:00:00\n")
        rc, out = self.call("end")
        self.assertVoid(rc, out)
        self.assertIn("flood watcher", out)
        self.assertNotIn("operator stop", out)

    def test_empty_stop_file_is_not_attributed(self):
        self.start()
        self.stop_file("")
        rc, out = self.call("check", "x")
        self.assertVoid(rc, out)
        self.assertIn("writer unknown", out)

    def test_start_with_stop_present_is_blocked_and_the_stop_kept(self):
        self.stop_file("flood watcher: flood stage at 10:00:00\n")
        rc, out = self.call("start")
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)
        self.assertTrue((self.home / "state" / "STOP").exists(), "start deleted a STOP")
        self.assertEqual(self.call("start")[0], 2, "a second start went through: the STOP was lost")

    def test_stop_arriving_while_end_measures_voids(self):
        self.start()
        self.F.trigger("du", 1, "write_file", str(self.home / "state" / "STOP"), "operator: stop now\n")
        self.assertVoid(*self.call("end"))

    def test_ack_with_a_reason_unblocks_start_and_is_recorded(self):
        self.stop_file("flood watcher: flood stage at 10:00:00\n")
        self.ack("read it; another tenant's run, ended")
        self.start()
        recorded = "".join(p.read_text(errors="replace") for p in (self.home / "state").rglob("*") if p.is_file())
        self.assertIn("another tenant", recorded)

    def test_ack_needs_a_reason(self):
        self.stop_file("x\n")
        if self.new:
            rc, _ = self.call("clear-stop", self.open_stop_ids()[0], "  ")
        else:
            rc, _ = self.call("clear-stop", "  ")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.call("start")[0], 2)

    # ---- review P1
    def test_rotated_log_voids(self):
        self.start()
        self.F.rotate(self.es)
        self.assertVoid(*self.call("end"))

    def test_truncated_log_with_a_flood_line_voids(self):
        self.start()
        self.line(FLOOD)
        self.F.rotate(self.es)
        self.assertVoid(*self.call("end"))

    def test_same_second_restart_voids(self):
        self.start()
        s = self.F.st()["c"][self.es]["started"]
        self.F.restart(self.es, started=s[:20] + "999999999Z")
        self.assertVoid(*self.call("end"))

    def test_replaced_container_voids(self):
        self.start()
        self.F.replace("emlab2-wphard-es")
        self.assertVoid(*self.call("end"))

    def test_wall_clock_step_voids(self):
        self.start()
        real = time.time
        with mock.patch.object(time, "time", lambda: real() - 3600):
            self.assertVoid(*self.call("end"))

    def test_close_marker_never_lands_voids(self):
        self.start()
        self.F.flag(echo_lost=True)
        with mock.patch.object(self.g, "CLOSE_TIMEOUT_S", 2.0) if self.new else _nullctx():
            self.assertVoid(*self.call("end"))

    def test_follower_loss_voids(self):
        """A line the capture never received (follower loss) while the retained log has it: VOID."""
        self.start()
        self.line(FLOOD, drop=True)
        self.assertVoid(*self.call("end"))

    # ---- review A, A1: markers and wall-clock filtering can conceal an in-window warning (red on 7b4839b)
    def _forge_end_marker(self):
        start = [r["x"] for r in self.F.lines(self.es) if r["x"].startswith(MARK + " start ")]
        self.assertTrue(start, "no start marker in the container log")
        self.line(MARK + " end " + start[0].split()[-1])

    def test_a1_forged_end_marker_conceals_a_later_flood_line(self):
        self.start()
        self._forge_end_marker()
        self.line(FLOOD)
        self.assertVoid(*self.call("end"), "flood stage", "forged, replayed or missing marker")

    def test_a1_forged_marker_alone_voids(self):
        self.start()
        self._forge_end_marker()
        self.assertVoid(*self.call("end"), "forged, replayed or missing marker")

    def test_a1_small_backward_clock_step_hides_a_flood_line(self):
        self.start()
        self.F.clock(-1.5)                          # under the old gate's 2 s slack
        self.line(FLOOD)
        self.F.clock(0.0)
        self.assertVoid(*self.call("end"), "flood stage")

    def test_a1_reversed_endpoints_hide_a_flood_line(self):
        self.start()
        self.F.clock(-30.0)
        self.line(FLOOD)
        self.assertVoid(*self.call("end"), "flood stage")

    def test_a1_truncation_then_start_marker_replay_hides_a_flood_line(self):
        self.start()
        start = [r["x"] for r in self.F.lines(self.es) if r["x"].startswith(MARK + " start ")]
        self.line(FLOOD)
        self.F.rotate(self.es)                      # the file is emptied, the flood line with it
        self.line(start[0])                         # ... and the public start marker written again
        self.assertVoid(*self.call("end"), "flood stage", "forged, replayed or missing marker", "contiguous")

    def test_a1_non_blocking_log_driver_is_never_certified(self):
        self.F.set(self.es, logcfg={"Type": "json-file", "Config": {"mode": "non-blocking", "max-buffer-size": "4m"}})
        rs, out = self.call("start")
        if self.new:
            self.assertIn("cannot give lossless coverage", out)
        re_ = self.call("end")[0] if rs == 0 else None
        self.assertTrue(rs == 2 or re_ == 3, f"a non-blocking driver (drops are invisible) was certified: start {rs}, end {re_}")

    # ---- review A, A2: identity can change after the only completion inspection (red on 7b4839b)
    def test_a2_restart_between_end_inspection_and_log_retrieval_voids(self):
        self.start()
        self.F.trigger("logs_read", 1, "restart", self.es)
        self.assertVoid(*self.call("end"), "started changed")

    def test_a2_replacement_during_end_measurement_voids(self):
        self.start()
        self.F.trigger("du", 1, "replace", "emlab2-wphard-es")
        self.assertVoid(*self.call("end"), "own usage not measured", "no longer exists")

    def test_a2_pause_during_the_run_voids(self):
        self.start()
        self.F.pause(self.es)
        self.assertVoid(*self.call("end"), "container:pause")

    # ---- review A, A3: an acknowledgement can erase a run's invalidation (red on 7b4839b)
    def test_a3_stop_then_ack_during_the_run_still_voids(self):
        self.start()
        self.stop_file("flood watcher: flood stage at 10:00:00\n")
        self.ack("operator read it mid-run")
        self.assertVoid(*self.call("end"), "STOP: stop")

    def test_a3_newer_stop_during_an_ack_still_blocks_start(self):
        self.stop_file("flood watcher: first\n")
        seen = self.open_stop_ids()                 # the operator reads the stop it will acknowledge
        time.sleep(0.01)
        self.stop_file("flood watcher: second, arrived while the operator was reading\n")
        self.ack("acknowledging the first", ids=seen)
        rc, out = self.call("start")
        self.assertEqual(rc, 2, f"start went through with an unacknowledged newer STOP: {out.strip()[-300:]}")
        self.assertIn("second, arrived", out)

    def test_a3_readmission_after_an_ack_does_not_rebaseline_the_window(self):
        self.start()
        self.stop_file("flood watcher: flood stage at 10:00:00\n")
        self.ack("operator read it mid-run")
        rc, out = self.call("start")                # a second admission while the first run is open
        if self.new:
            self.assertEqual(rc, 2, out)
            self.assertIn("has no end record", out)
        self.assertVoid(*self.call("end"), "STOP: stop")

    # ---- the ledger gate's own design (need commands the 7b4839b gate lacks)
    def test_companion_killed_voids(self):
        self.need_new()
        self.start()
        run = json.loads(next((self.home / "state" / "gate-runs").glob("*/run.json")).read_text())
        os.killpg(run["companion_pid"], signal.SIGKILL)
        time.sleep(0.2)
        self.assertVoid(*self.call("check", "x"))

    def test_follower_eof_voids(self):
        self.need_new()
        self.start()
        self.F.flag(kill_followers=True)
        time.sleep(0.5)
        self.assertVoid(*self.call("end"))

    def test_capture_edited_voids(self):
        self.need_new()
        self.start()
        self.line("INFO a line")
        time.sleep(0.3)
        cap = next((self.home / "state" / "gate-runs").glob("*/capture.jsonl"))
        lines = cap.read_text().splitlines(True)
        cap.write_text("".join(lines[:1] + lines[2:]))
        self.assertVoid(*self.call("end"))

    def test_unsupported_log_driver_blocks_start(self):
        self.need_new()
        self.F.set(self.es, logcfg={"Type": "journald", "Config": {}})
        self.assertEqual(self.call("start")[0], 2)

    def test_tty_container_blocks_start(self):
        self.need_new()
        self.F.set(self.es, tty=True)
        self.assertEqual(self.call("start")[0], 2)

    def test_update_event_voids(self):
        self.need_new()
        self.start()
        self.F.event(self.pg, "update")
        self.assertVoid(*self.call("end"))

    def test_void_check_closes_the_run(self):
        self.need_new()
        self.start()
        self.stop_file("operator: stop\n")
        self.assertVoid(*self.call("check", "x"))
        self.ack("handled")
        self.start()
        self.assertValid(*self.call("end"))

    def test_open_run_blocks_start_until_aborted(self):
        self.need_new()
        self.start()
        self.assertEqual(self.call("start")[0], 2)
        self.assertEqual(self.call("abort", "the launcher died")[0], 0)
        self.start()
        self.assertValid(*self.call("end"))

    def test_edited_ledger_blocks_start(self):
        self.need_new()
        self.start()
        self.assertValid(*self.call("end"))
        led = self.home / "state" / "stop_ledger.jsonl"
        led.write_text("".join(led.read_text().splitlines(True)[1:]))
        self.assertEqual(self.call("start")[0], 2)

    def test_stop_command_and_stops_listing(self):
        self.need_new()
        self.assertEqual(self.call("stops")[0], 0)
        self.assertEqual(self.call("stop", "operator: hold the chain")[0], 0)
        rc, out = self.call("stops")
        self.assertEqual(rc, 2)
        self.assertIn("hold the chain", out)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        sys.exit(fake_main(sys.argv[2:]))
    unittest.main(verbosity=2)

"""Run-validity gate for stamped runs (amendment 17c; reviews N2, P1/P2 and A1/A2/A3). Threat model, design and the
exact guarantee: runkit/GATE-DESIGN.md. The gate judges ONLY evidence it captures itself.

  start          BLOCKED (exit 2) under START_GB host free, while a stop is unacknowledged, while an earlier run has no
                 end record, if the ES or PG container cannot be inspected, or if the ES log driver cannot give lossless
                 coverage (json-file or local in blocking mode, no TTY). Otherwise: pins both container IDs, measures own
                 usage, spawns the capture companion (`docker logs --follow --tail 0 --timestamps <es id>` and
                 `docker events` for both IDs, into a sequence-numbered, hash-chained capture file), writes a fresh
                 start token to PID 1's stdout and stderr and waits until it is captured on both streams and in the
                 events stream, then admits the run in the stop ledger in one locked step.
  check NAME     VOID (exit 3) if a stop was recorded after admission; if the capture so far holds a flood-stage or
                 high-watermark line, a gate-marker line the gate did not write, a lifecycle event or an early end; if
                 the companion is not running; or if own usage is unmeasured or grew over CAP_GB. A VOID check closes
                 the run. A host under FLOOR_GB from other work is recorded and the run continues.
  end            measures own usage, writes fresh close tokens and waits for them (both streams and the events stream),
                 stops the companion, verifies the capture (chain, sequence, final record, exactly the gate's own
                 marker lines), judges EVERY captured line, allows only exec/top/archive-path events, checks that the
                 capture is a contiguous run of the retained log, re-inspects both containers by ID, keeps the boot
                 and 2 s wall-clock-step rules, and in one locked ledger step voids the run on any stop recorded after
                 its admission and writes the end record. VOID (exit 3) on any reason; every reason is printed.
  preflight      the erasure gate's store inventory must be empty before any experiment.
  stop REASON    record a stop.   stops: list unacknowledged stops (exit 2 if any).
  clear-stop ID REASON   acknowledge ONE stop by id; it unblocks later starts and never changes a run's verdict.
  abort REASON   close an open run whose launcher died (VOID).
A legacy WP_HOME/state/STOP file is ingested as a stop (each new write is a new stop) and left in place.
Guarantee (see GATE-DESIGN.md for the limits): 20 GiB free at admission; every line the ES container wrote to
stdout/stderr between admission and the close tokens captured, none a flood-stage or high-watermark line and no forged
marker; the admitted containers not stopped, restarted, paused, updated or replaced in the window; no stop recorded
in the window; own usage SAMPLED within the cap (not a bound on a transient peak); no reboot or wall-clock step.
Prints numbers, ids and reasons only."""
import fcntl
import hashlib
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
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
STOP = STATE_DIR / "STOP"                  # legacy stop file: ingested into the ledger, never deleted by the gate
LEDGER = STATE_DIR / "stop_ledger.jsonl"   # append-only, hash-chained: stop, ack, admit, end
LOCK = STATE_DIR / "stop_ledger.lock"
RUNS = STATE_DIR / "gate-runs"             # per run: run.json, capture.jsonl, companion.err (kept as evidence)
ROOT = pathlib.Path(__file__).resolve().parents[1]
MARK = "wp16096-disk-gate"
CLOCK_SLACK_S = 2.0
SYNC_TIMEOUT_S = 30.0
CLOSE_TIMEOUT_S = 30.0
DOCKER_TIMEOUT_S = 300
DEADLINE_S = 48 * 3600
POLL_S = 0.05
ALLOWED_EVENTS = ("exec_create", "exec_start", "exec_die", "exec_detach", "top", "archive-path")
DRIVERS = ("json-file", "local")
_sleep = time.sleep


class GateError(Exception):
    pass


class LedgerError(Exception):
    pass


def _targets():
    """The ES and PG container names this stream's conf names (wp.config). runkit/gate_live.py replaces this function,
    and only it, for its labelled live check on its own containers."""
    return config.ES_CONTAINER, config.PG_CONTAINER


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id():
    return "s-" + os.urandom(4).hex()


def _docker(*args, timeout=None):
    return subprocess.run(["docker", *args], capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=timeout or DOCKER_TIMEOUT_S)


def _bad(text):
    """The amendment-23a predicates: past the HIGH watermark the node allocates no new primaries (d3 at 31112b4: 503
    unavailable_shards_exception); at the flood stage writes are refused. The low watermark only stops replicas."""
    t = text.lower()
    if "flood stage" in t:
        return "flood-stage"
    if "high disk watermark" in t and "exceeded" in t:
        return "high-watermark"
    return None


def _boot_id():
    try:
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _alive(pid):
    if not pid:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:   # our own child (a test) that already exited
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)                             # signal 0: existence only, nothing is sent
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_json(p, obj):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, p)


# ------------------------------------------------------------------------------------------------------ stop ledger
def _read_ledger():
    """(records, hash of the last record). Any edit, removal, reordering or partial record is a LedgerError."""
    try:
        raw = LEDGER.read_bytes()
    except FileNotFoundError:
        return [], "0" * 64
    if raw and not raw.endswith(b"\n"):
        raise LedgerError("the stop ledger ends in a partial record")
    recs, prev = [], "0" * 64
    for i, ln in enumerate(raw.split(b"\n")[:-1]):
        try:
            r = json.loads(ln)
        except ValueError:
            raise LedgerError(f"stop ledger record {i + 1} is not JSON") from None
        if not isinstance(r, dict) or r.get("seq") != i + 1 or r.get("prev") != prev:
            raise LedgerError(f"stop ledger record {i + 1} breaks the sequence or the hash chain (edited or removed records)")
        recs.append(r)
        prev = hashlib.sha256(ln).hexdigest()
    return recs, prev


class Ledger:
    """The stop ledger under an exclusive flock, verified on entry. Appends are fsynced; nothing is ever rewritten."""

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._f = open(LOCK, "a")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        try:
            self.recs, self.prev = _read_ledger()
        except LedgerError:
            self.__exit__()
            raise
        return self

    def __exit__(self, *a):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        return False

    def append(self, kind, **kw):
        rec = dict(kw, seq=len(self.recs) + 1, prev=self.prev, kind=kind, at=_utc())
        ln = json.dumps(rec, sort_keys=True).encode()
        fd = os.open(LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, ln + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        self.recs.append(rec)
        self.prev = hashlib.sha256(ln).hexdigest()
        return rec

    def ingest_legacy(self):
        """A state/STOP file (old instructions, other scripts) becomes a stop record; a file not yet recorded (by inode,
        size, mtime and content) is a new stop. The file is left in place."""
        try:
            stt = os.stat(STOP)
            data = STOP.read_bytes()
        except FileNotFoundError:
            return
        fp = f"{stt.st_ino}:{stt.st_size}:{stt.st_mtime_ns}:{hashlib.sha256(data).hexdigest()[:16]}"
        if any(r["kind"] == "stop" and r.get("fingerprint") == fp for r in self.recs):
            return
        text = data.decode("utf-8", "replace").strip()[:200]
        self.append("stop", id=_new_id(), source="state/STOP file", fingerprint=fp,
                    reason=f"written reason: {text}" if text else "no reason written in the file (writer unknown)")

    def open_stops(self):
        acked = {r.get("stop_id") for r in self.recs if r["kind"] == "ack"}
        return [r for r in self.recs if r["kind"] == "stop" and r["id"] not in acked]

    def closed(self, run):
        return any(r["kind"] == "end" and r.get("run") == run for r in self.recs)

    def open_run(self):
        adm = [r for r in self.recs if r["kind"] == "admit" and not self.closed(r["run"])]
        return adm[-1] if adm else None

    def stops_after(self, seq):
        return [r for r in self.recs if r["kind"] == "stop" and r["seq"] > seq]


def _stop_text(s):
    return f"stop {s['id']} at seq {s['seq']} ({s.get('source')}): {s.get('reason')}"


def open_stops():
    with Ledger() as L:
        L.ingest_legacy()
        return L.open_stops()


def _admission_blocker(L):
    op = L.open_stops()
    if op:
        return ("unacknowledged stop(s): " + "; ".join(_stop_text(s) for s in op) +
                "; read them, then 'python -m exp.disk_gate clear-stop <stop id> <reason>'")
    o = L.open_run()
    if o:
        return (f"run {o['run']} (admitted at seq {o['seq']}) has no end record; if its launcher died: "
                f"'python -m exp.disk_gate abort <reason>'")
    return None


# ---------------------------------------------------------------------------------------------- containers, usage
def _inspect(ref):
    try:
        r = _docker("inspect", ref, timeout=60)
        j = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return j[0] if isinstance(j, list) and j and isinstance(j[0], dict) else None


def _ident(j):
    s = j.get("State") or {}
    return {"id": j.get("Id"), "name": (j.get("Name") or "").lstrip("/"), "started": s.get("StartedAt"),
            "restarts": j.get("RestartCount"), "running": s.get("Running"), "paused": s.get("Paused")}


def _num(r):
    o = r.stdout.decode("utf-8", "replace").split()
    return int(o[0]) if r.returncode == 0 and o and o[0].isdigit() else None


def own(es, pg):
    """du of this stream's PG and ES data directories (addressed by container ID) plus WP_HOME, in bytes."""
    try:
        pgb = _num(_docker("exec", pg, "du", "-sb", "/var/lib/postgresql/data", timeout=600))
        esb = _num(_docker("exec", es, "du", "-sb", "/usr/share/elasticsearch/data", timeout=600))
        home = _num(subprocess.run(["du", "-sb", str(HOME)], capture_output=True, timeout=600))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GateError(f"cannot measure own usage ({type(e).__name__})") from None
    if None in (pgb, esb, home):
        raise GateError(f"cannot measure own usage (pg={pgb} es={esb} home={home})")
    return {"pg": pgb, "es": esb, "home": home, "total": pgb + esb + home}


def free_gb():
    return shutil.disk_usage(HOME).free / GIB


# ------------------------------------------------------------------------------------------------- capture companion
def _write_token(es, token):
    """One line on PID 1's stdout and one on its stderr: the two streams the log driver records."""
    ok = True
    for fd in (1, 2):
        try:
            ok = _docker("exec", es, "sh", "-c", f"echo {token} > /proc/1/fd/{fd}", timeout=60).returncode == 0 and ok
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    return ok


def _read_capture(rd):
    """(records, problem): complete records with contiguous numbers and an intact hash chain; a trailing partial
    record (the companion mid-write) is ignored."""
    try:
        raw = (rd / "capture.jsonl").read_bytes()
    except FileNotFoundError:
        return [], "no capture file"
    recs, h = [], "0" * 64
    for i, ln in enumerate(raw.split(b"\n")[:-1]):
        try:
            r = json.loads(ln)
            body = {k: v for k, v in r.items() if k != "h"}
        except (ValueError, AttributeError):
            return recs, f"capture record {i} is not a JSON object"
        want = hashlib.sha256((h + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        if r.get("n") != i or r.get("h") != want:
            return recs, f"capture record {i} breaks the sequence or the hash chain (edited, removed or reordered)"
        h = want
        recs.append(r)
    return recs, None


def _await_token(rd, token, timeout):
    """None once the token is captured on both streams and an exec event carrying it is in the events capture."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        recs, problem = _read_capture(rd)
        if problem:
            return problem
        streams = {r["s"] for r in recs if r["k"] == "log" and r["x"].strip() == token}
        if streams == {"o", "e"} and any(r["k"] == "ev" and token in (r["e"].get("Action") or "") for r in recs):
            return None
        if any(r["k"] == "final" for r in recs):
            return "the capture companion ended before the token was captured"
        _sleep(POLL_S)
    return (f"the token was not captured on both streams and in the events stream within {timeout:.0f} s "
            f"(watchers not attached or not caught up)")


def _stop_companion(rd, timeout):
    """Ask the companion to stop (a file in its run directory; never a signal to a pid) and wait for its final record."""
    (rd / "stop").touch()
    t0 = time.monotonic()
    while True:
        recs, problem = _read_capture(rd)
        if problem or any(r["k"] == "final" for r in recs) or time.monotonic() - t0 > timeout:
            return recs, problem
        _sleep(POLL_S)


def _capture(rd):
    """The companion: copies the ES container's log stream and the two containers' events into capture.jsonl until
    end asks it to stop, a stream closes, its run is closed or never admitted, or the deadline passes."""
    rd = pathlib.Path(rd)
    run = json.loads((rd / "run.json").read_text())
    es, pg = run["es"]["id"], run["pg"]["id"]
    PIPE, NUL = subprocess.PIPE, subprocess.DEVNULL
    procs = {"logs": subprocess.Popen(["docker", "logs", "--follow", "--tail", "0", "--timestamps", es],
                                      stdout=PIPE, stderr=PIPE, stdin=NUL),
             "events": subprocess.Popen(["docker", "events", "--filter", f"container={es}", "--filter",
                                         f"container={pg}", "--format", "{{json .}}"], stdout=PIPE, stderr=NUL, stdin=NUL)}
    q = queue.Queue()

    def pump(pipe, kind, src):
        for raw in iter(pipe.readline, b""):
            q.put((kind, raw.decode("utf-8", "replace").rstrip("\n")))
        q.put(("eof", src))
    threads = [threading.Thread(target=pump, args=a, daemon=True) for a in
               ((procs["logs"].stdout, "o", "log stdout"), (procs["logs"].stderr, "e", "log stderr"),
                (procs["events"].stdout, "ev", "events"))]
    for t in threads:
        t.start()
    out = open(rd / "capture.jsonl", "ab")
    s = {"n": 0, "h": "0" * 64, "flagged": False, "draining": False}

    def emit(rec):
        rec["n"] = s["n"]
        rec["h"] = hashlib.sha256((s["h"] + json.dumps(rec, sort_keys=True)).encode()).hexdigest()
        out.write((json.dumps(rec, sort_keys=True) + "\n").encode())
        out.flush()
        s["n"], s["h"] = s["n"] + 1, rec["h"]

    def handle(kind, payload):
        if kind in ("o", "e"):
            ts, _, text = payload.partition(" ")
            emit({"k": "log", "s": kind, "t": ts, "x": text})
            bad = _bad(text)
            if bad and not s["flagged"]:             # replaces the launcher's wall-clock watcher
                s["flagged"] = True
                try:
                    with Ledger() as L:
                        L.append("stop", id=_new_id(), source="gate capture", run=run["run"],
                                 reason=f"gate capture: a {bad} line in the ES log at {ts}")
                except (OSError, LedgerError):
                    pass                              # the line is in the capture, and end judges it
        elif kind == "ev":
            try:
                e = json.loads(payload)
            except ValueError:
                e = {"Type": "unparsed", "Action": payload[:200]}
            emit({"k": "ev", "e": {"Type": e.get("Type"), "Action": e.get("Action") or e.get("status"),
                                   "id": (e.get("Actor") or {}).get("ID") or e.get("id")}})
        else:
            emit({"k": "eof", "src": payload, "after_stop": s["draining"]})
            return payload
        return None

    emit({"k": "ready", "pid": os.getpid()})
    reason, t0, polled = None, time.monotonic(), time.monotonic()
    while reason is None:
        try:
            ended = handle(*q.get(timeout=0.1))
        except queue.Empty:
            ended = None
        if ended:
            reason = f"{ended} closed"
        elif (rd / "stop").exists():
            reason = "stop requested"
        now = time.monotonic()
        if reason is None and now - t0 > DEADLINE_S:
            reason = "deadline"
        if reason is None and now - polled > 2:
            polled = now
            try:
                recs = _read_ledger()[0]
                if any(r["kind"] == "end" and r.get("run") == run["run"] for r in recs):
                    reason = "run closed in the ledger"
                elif now - t0 > 2 * SYNC_TIMEOUT_S + 60 and not any(r["kind"] == "admit" and r.get("run") == run["run"]
                                                                     for r in recs):
                    reason = "run never admitted"
            except (OSError, LedgerError):
                pass
    s["draining"] = True
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    for p in procs.values():
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    for t in threads:
        t.join(5)
    while True:
        try:
            handle(*q.get_nowait())
        except queue.Empty:
            break
    emit({"k": "final", "reason": reason, "rc": {k: p.returncode for k, p in procs.items()}})
    out.close()


# ---------------------------------------------------------------------------------------------------------- judging
def _judge(recs, run, close_token=None):
    """(reasons, counts) for the captured records. Every captured log line is judged: the capture starts when the
    follower attached, before admission, so it is a superset of the window."""
    why = []
    logs = [r for r in recs if r["k"] == "log"]
    flood = sum(1 for r in logs if _bad(r["x"]) == "flood-stage")
    high = sum(1 for r in logs if _bad(r["x"]) == "high-watermark")
    if flood:
        why.append(f"the ES node hit its flood stage during the run ({flood} captured lines; writes were refused)")
    if high:
        why.append(f"the ES node passed its high disk watermark during the run ({high} captured lines; new shards were "
                   f"not allocated)")
    # every marker-shaped line must be a token the gate wrote, none twice; the admitted start token exactly once, and
    # after it nothing but the close token (earlier start attempts may precede it)
    start, written = run["start_token"], set(run.get("tokens") or []) | {run["start_token"], close_token}
    for st, name in (("o", "stdout"), ("e", "stderr")):
        seen = [r["x"].strip() for r in logs if r["s"] == st and MARK in r["x"]]
        ok = seen.count(start) == 1 and len(set(seen)) == len(seen) and all(x in written for x in seen)
        if ok:
            ok = seen[seen.index(start) + 1:] == ([close_token] if close_token else [])
        if not ok:
            why.append(f"the gate-marker lines on the ES container's {name} are not the gate's own tokens in order "
                       f"({len(seen)} seen): a forged, replayed or missing marker")
    evs = [r["e"] for r in recs if r["k"] == "ev"]
    bad_ev = sorted({f"{e.get('Type')}:{(e.get('Action') or '?').split(':')[0]}" for e in evs
                     if not (e.get("Type") == "container" and (e.get("Action") or "").split(":")[0] in ALLOWED_EVENTS)})
    if bad_ev:
        why.append("lifecycle events on the ES or PG container during the run: " + ", ".join(bad_ev))
    early = [r["src"] for r in recs if r["k"] == "eof" and not r.get("after_stop")]
    if early:
        why.append("the capture ended early: " + ", ".join(early) + " closed (container stopped, restarted or removed, "
                   "or the docker daemon restarted)")
    return why, {"lines": len(logs), "flood": flood, "high": high, "events": len(evs)}


def _contiguous(cap, ret):
    if not cap:
        return True
    n = len(cap)
    return any(ret[i:i + n] == cap for i in range(len(ret) - n + 1) if ret[i] == cap[0])


def _cross_check(es, recs):
    """Loss detection: per stream, the capture must be a contiguous run of the retained log read now by ID."""
    try:
        r = _docker("logs", "--timestamps", es)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"cannot read the retained ES log for the loss check ({type(e).__name__})"]
    if r.returncode != 0:
        return [f"docker logs exited {r.returncode}: loss of captured lines cannot be ruled out"]
    why = []
    for st, raw, name in (("o", r.stdout, "stdout"), ("e", r.stderr, "stderr")):
        ret = raw.decode("utf-8", "replace").split("\n")
        if ret and ret[-1] == "":
            ret.pop()
        cap = [f"{x['t']} {x['x']}" for x in recs if x["k"] == "log" and x["s"] == st]
        if not _contiguous(cap, ret):
            why.append(f"the capture of the ES {name} is not a contiguous run of the retained log (lines lost by the "
                       f"follower, or the log rotated or truncated during the run)")
    return why


# --------------------------------------------------------------------------------------------------------- commands
def _blocked(msg):
    print(f"disk_gate BLOCKED start: {msg}")
    sys.exit(2)


def _void_now(msg):
    print(f"disk_gate VOID: {msg}")
    sys.exit(3)


def _start():
    f = free_gb()
    if f < START_GB:
        _blocked(f"host free {f:.1f} GiB < {START_GB}")
    try:
        with Ledger() as L:
            L.ingest_legacy()
            b = _admission_blocker(L)
    except LedgerError as e:
        b = f"the stop ledger is not intact ({e})"
    if b:
        _blocked(b)
    es_j, pg_j = (_inspect(n) for n in _targets())
    if es_j is None or pg_j is None:
        _blocked("cannot inspect the ES or PG container")
    es, pg = _ident(es_j), _ident(pg_j)
    lc = (es_j.get("HostConfig") or {}).get("LogConfig") or {}
    mode = (lc.get("Config") or {}).get("mode") or "blocking"
    if lc.get("Type") not in DRIVERS or mode != "blocking":
        _blocked(f"the ES log driver ({lc.get('Type')}, mode {mode}) cannot give lossless coverage (supported: "
                 f"{', '.join(DRIVERS)} in blocking mode)")
    if (es_j.get("Config") or {}).get("Tty"):
        _blocked("the ES container has a TTY (stdout and stderr merged): coverage is not established")
    if not (es["running"] and pg["running"]) or es["paused"] or pg["paused"]:
        _blocked("the ES or PG container is not running")
    try:
        base = own(es["id"], pg["id"])
    except GateError as e:
        _blocked(str(e))
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(4).hex()
    rd = RUNS / run_id
    rd.mkdir(parents=True)
    run = {"run": run_id, "es": es, "pg": pg, "log_config": lc, "start_token": None, "tokens": []}
    _write_json(rd / "run.json", run)
    pp = os.environ.get("PYTHONPATH")
    env = dict(os.environ, PYTHONPATH=str(ROOT) + (os.pathsep + pp if pp else ""))
    with open(rd / "companion.err", "ab") as err:
        p = subprocess.Popen([sys.executable, "-m", "exp.disk_gate", "_capture", str(rd)], cwd=str(ROOT), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
    run["companion_pid"] = p.pid
    _write_json(rd / "run.json", run)
    # `--tail 0` attaches at an unknown moment: a token written before the follower attached is never delivered, so
    # write a fresh token until one is captured on both streams and in the events stream. Every token is recorded
    # before it is written; the admitted start token is the one that was captured.
    why, t0 = "no start token captured", time.monotonic()
    while time.monotonic() - t0 < SYNC_TIMEOUT_S:
        tok = f"{MARK} start {os.urandom(16).hex()}"
        run["tokens"].append(tok)
        _write_json(rd / "run.json", run)
        why = None if _write_token(es["id"], tok) else "cannot write the start token into the ES log"
        why = why or _await_token(rd, tok, min(2.0, SYNC_TIMEOUT_S))
        if why is None or "ended" in why:
            break
    if why is None:
        run["start_token"] = tok
        _write_json(rd / "run.json", run)
    if why:
        _stop_companion(rd, 10)
        _blocked("log capture not established: " + why)
    wall0, mono0 = time.time(), time.monotonic()
    try:
        with Ledger() as L:                         # no unacknowledged stop and no open run, then admit: one step
            L.ingest_legacy()
            b = _admission_blocker(L)
            adm = None if b else L.append("admit", run=run_id, es=es["id"], pg=pg["id"])
    except LedgerError as e:
        b = f"the stop ledger is not intact ({e})"
    if b:
        _stop_companion(rd, 10)
        _blocked(b)
    _write_json(STATE, {"run": run_id, "admit_seq": adm["seq"], "run_dir": str(rd), "base": base, "floor_breaches": [],
                        "es": es, "pg": pg, "wall0": wall0, "mono0": mono0, "boot": _boot_id(), "since": _utc()})
    print(f"disk_gate start run {run_id} free {f:.1f} GiB own {base['total'] / GIB:.2f} GiB (pg {base['pg'] / GIB:.2f} "
          f"es {base['es'] / GIB:.2f} home {base['home'] / GIB:.2f}) cap +{CAP_GB} GiB es {es['id'][:12]} "
          f"pg {pg['id'][:12]} admitted at seq {adm['seq']}")


def _state():
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or "run" not in st:
        _void_now("no admitted run in the gate state (none, or written by an older gate): coverage unknown")
    return st, pathlib.Path(st["run_dir"])


def _close(st, why, **extra):
    """Close the run in the ledger as VOID (unless already closed) and let its companion stop."""
    try:
        with Ledger() as L:
            if not L.closed(st["run"]):
                L.append("end", run=st["run"], verdict="VOID", reasons=why[:20], **extra)
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e}); the run could not be closed in it")
    (pathlib.Path(st["run_dir"]) / "stop").touch()


def _check(name):
    st, rd = _state()
    why = []
    try:
        with Ledger() as L:
            L.ingest_legacy()
            if L.closed(st["run"]):
                _void_now(f"run {st['run']} is already closed")
            why += [f"STOP: {_stop_text(s)}, before {name}" for s in L.stops_after(st["admit_seq"])]
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e})")
    run = json.loads((rd / "run.json").read_text())
    recs, problem = _read_capture(rd)
    if problem:
        why.append("capture: " + problem)
    why += _judge(recs, run)[0]
    if any(r["k"] == "final" for r in recs):
        why.append("the capture companion ended before the run did")
    elif not _alive(run.get("companion_pid")):
        why.append("the capture companion is not running")
    f, d = free_gb(), None
    try:
        o = own(st["es"]["id"], st["pg"]["id"])
        d = (o["total"] - st["base"]["total"]) / GIB
        print(f"disk_gate {name} free {f:.1f} GiB own +{d:.2f} GiB")
        if d > CAP_GB:
            why.append(f"own usage grew {d:.2f} GiB > cap {CAP_GB} before {name}")
    except GateError as e:
        why.append(f"own usage not measured before {name} ({e})")
    if why:
        _close(st, why, at_check=name)
        for w in why:
            print(f"disk_gate VOID: {w}")
        sys.exit(3)
    if f < FLOOR_GB:
        st["floor_breaches"].append({"before": name, "free_gb": round(f, 2), "own_delta_gb": round(d, 2)})
        _write_json(STATE, st)
        print(f"disk_gate FLOOR: host free {f:.1f} GiB < {FLOOR_GB} from other work; own usage within cap, continuing")


def _end():
    st, rd = _state()
    try:
        with Ledger() as L:
            if L.closed(st["run"]):
                _void_now(f"run {st['run']} was already closed (a VOID check or an abort)")
    except LedgerError:
        pass                                        # judged again in the final locked step
    run = json.loads((rd / "run.json").read_text())
    es = st["es"]["id"]
    why, f, d = [], free_gb(), None
    # A2: own usage first, so every later observation of the containers happens inside the watched window
    try:
        o = own(es, st["pg"]["id"])
        d = (o["total"] - st["base"]["total"]) / GIB
        if d > CAP_GB:
            why.append(f"own usage grew {d:.2f} GiB > cap {CAP_GB} by the end of the run")
    except GateError as e:
        why.append(f"own usage not measured at end ({e})")
    close = f"{MARK} end {os.urandom(16).hex()}"
    if _write_token(es, close):
        w = _await_token(rd, close, CLOSE_TIMEOUT_S)
        if w:
            why.append("close: " + w)
    else:
        why.append("cannot write the close token into the ES log (container gone or not running)")
        close = None
    recs, problem = _stop_companion(rd, CLOSE_TIMEOUT_S)
    if problem:
        why.append("capture: " + problem)
    fin = [r for r in recs if r["k"] == "final"]
    if not fin:
        why.append("the capture companion did not close its capture (dead or hung): coverage unknown")
    elif fin[0]["reason"] != "stop requested":
        why.append(f"the capture ended before end asked it to: {fin[0]['reason']}")
    jw, cnt = _judge(recs, run, close)
    why += jw
    why += _cross_check(es, recs)
    for key in ("es", "pg"):
        pinned, j = st[key], _inspect(st[key]["id"])
        now = _ident(j) if j else None
        if now is None:
            why.append(f"the {key.upper()} container {pinned['id'][:12]} no longer exists (removed or replaced)")
            continue
        for fld in ("id", "started", "restarts"):
            if now[fld] != pinned[fld]:
                why.append(f"the {key.upper()} container's {fld} changed during the run ({pinned[fld]} -> {now[fld]})")
        if not now["running"] or now["paused"]:
            why.append(f"the {key.upper()} container is not running at end")
    if _boot_id() != st["boot"]:
        why.append("the host rebooted during the run")
    step = (time.time() - st["wall0"]) - (time.monotonic() - st["mono0"])
    if abs(step) > CLOCK_SLACK_S:
        why.append(f"the host wall clock stepped {step:+.1f} s during the run")
    sha = hashlib.sha256((rd / "capture.jsonl").read_bytes()).hexdigest() if (rd / "capture.jsonl").exists() else None
    try:
        with Ledger() as L:                         # the final stop read and the end record: one locked step
            L.ingest_legacy()
            why += [f"STOP: {_stop_text(s)}, during the run" for s in L.stops_after(st["admit_seq"])]
            if L.closed(st["run"]):
                why.append(f"run {st['run']} was closed while end was measuring (a VOID check or an abort)")
            else:
                L.append("end", run=st["run"], verdict="VOID" if why else "VALID", reasons=why[:20],
                         capture_sha256=sha, counts=cnt)
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e})")
    print(f"disk_gate end free {f:.1f} GiB own {'+%.2f GiB' % d if d is not None else 'unmeasured'} floor_breaches "
          f"{len(st['floor_breaches'])} es_flood_lines {cnt['flood']} es_high_watermark_lines {cnt['high']} "
          f"capture_lines {cnt['lines']} events {cnt['events']} capture_sha256 {sha}")
    for w in why:
        print(f"disk_gate VOID: {w}")
    if why:
        sys.exit(3)


def _ledger_cmd(cmd, args):
    reason = " ".join(args[1:] if cmd == "clear-stop" else args).strip()
    with Ledger() as L:
        L.ingest_legacy()
        if cmd == "stop":
            if not reason:
                raise SystemExit("stop needs a reason")
            r = L.append("stop", id=_new_id(), source="operator", reason=f"written reason: {reason[:200]}")
            print(f"disk_gate stop {r['id']} recorded at seq {r['seq']}")
        elif cmd == "stops":
            for s in L.open_stops():
                print(f"disk_gate open {_stop_text(s)}")
            if L.open_stops():
                sys.exit(2)
            print("disk_gate stops: none open")
        elif cmd == "clear-stop":
            ids = {s["id"] for s in L.open_stops()}
            if not args or args[0] not in ids or not reason:
                print("disk_gate clear-stop: usage clear-stop <stop id> <reason>; open stops: " +
                      ("; ".join(_stop_text(s) for s in L.open_stops()) or "none"))
                sys.exit(2)
            L.append("ack", stop_id=args[0], reason=reason[:200])
            print(f"disk_gate clear-stop: stop {args[0]} acknowledged (no run's verdict changes)")
        elif cmd == "abort":
            if not reason:
                raise SystemExit("abort needs a reason")
            o = L.open_run()
            if not o:
                print("disk_gate abort: no open run")
                return
            L.append("end", run=o["run"], verdict="VOID", reasons=[f"aborted: {reason[:200]}"])
            if (RUNS / o["run"]).is_dir():
                (RUNS / o["run"] / "stop").touch()
            print(f"disk_gate abort: run {o['run']} closed VOID")


def main(cmd="", *args):
    if cmd == "_capture":
        return _capture(args[0])
    if cmd == "start":
        return _start()
    if cmd == "check":
        return _check(args[0] if args else "")
    if cmd == "end":
        return _end()
    if cmd in ("stop", "stops", "clear-stop", "abort"):
        try:
            return _ledger_cmd(cmd, list(args))
        except LedgerError as e:
            print(f"disk_gate BLOCKED: the stop ledger is not intact ({e})")
            sys.exit(2)
    if cmd == "preflight":             # after setup: the erasure gate's inventory must be empty before any experiment
        from wp import db, registry
        from wp.es import ES
        unreg = registry.unregistered(db.admin(), db.keys_connect(), ES())
        print(f"disk_gate preflight unregistered {len(unreg)} {unreg[:10]}")
        if unreg:
            print("disk_gate BLOCKED: the store inventory is not empty before the run (every erasure would report residue)")
            sys.exit(2)
        return None
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])

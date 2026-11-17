"""Run-validity gate v3 for stamped runs (review D). Design, argument and limits: runkit/GATE-DESIGN.md section 5.

The hazard is removed, not detected: each store's data directory is a private tmpfs, larger than its container's
memory limit plus Elasticsearch's largest watermark threshold, with no swap. Every tmpfs page is charged to the
container's memory cgroup, so no writer in the container can bring free space down to a watermark (or, for PG, to
zero) without exceeding the limit, and the kernel's lifetime memory.peak proves afterwards that it never did. Other
tenants cannot write into the tmpfs. The gate checks that arrangement and the containers' identity; it reads no log
text and compares no wall-clock times inside the window.

  init REASON    start a new stop ledger (every other command is BLOCKED without one), or adopt an existing ledger that
                 has no init record (a v2 ledger, or one whose start was cut): never done implicitly.
  start          BLOCKED (exit 2) unless, for both containers (by ID): the data directory is a Docker tmpfs mount with
                 no mount inside it, and the store itself reports it as tmpfs (ES's node stats; PG's statfs); memory
                 limit set, swap 0, size - limit >= ES's low-watermark free-space threshold + MARGIN (PG: MARGIN);
                 lifetime memory.peak within the same bound; free space now >= threshold + MARGIN and used space <=
                 memory.peak (no page charged elsewhere); PG's PGDATA inside the tmpfs; running, not paused, no OOM kill
                 flagged; the ES API
                 bound to the pinned container (its published port; its JVM started with it). Then, in one locked
                 ledger step: no unacknowledged stop, no open run, host free >= START_GB (measured there), admit.
  check NAME     VOID (exit 3) if any start rule fails now, or anything differs from admission: Id, StartedAt, ES node
                 id, oom_kill count, memory limit, swap, tmpfs size, ES's thresholds; or a stop was recorded since
                 admission; or WP_HOME grew over CAP_GB. A VOID check closes the run.
  end            the same checks, then in one locked ledger step: this run's admit record must be there, any stop
                 after it voids the run, and the end record is written. VOID (exit 3) on any reason; all are printed.
  preflight      the erasure gate's store inventory must be empty before any experiment.
  stop REASON    record a stop.   stops: list unacknowledged stops (exit 2 if any).
  clear-stop ID REASON   acknowledge ONE stop by id; it unblocks later starts and never changes a run's verdict.
  abort REASON   close an open run whose launcher died (VOID).
  ledger-reset REASON    keep a DAMAGED ledger under a new name and start a new one holding one open stop (open runs
                 end VOID); an intact ledger is refused.
The legacy path state/STOP is a directory the gate keeps there: a legacy `> state/STOP` write fails instead of being
missed; a regular file found there is ingested as a stop and kept as state/STOP.ingested-<seq>; and any change to the
directory (a touch, a file copied or moved into it) is a stop too, detected against a signature in the head anchor.
Prints numbers, ids and reasons only."""
import calendar
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

from wp import config

FORMAT = 3
START_GB, FLOOR_GB, CAP_GB = 20.0, 15.0, 2.0   # start line 20 GB from amendment 24
GIB = 1024 ** 3
MARGIN = 256 * 1024 ** 2                       # headroom over the bound: statfs rounding, tmpfs inodes
HOME = pathlib.Path(os.environ["WP_HOME"])
# NOT under WP_HOME/logs: that directory is a registered store the erasure gate inventories.
STATE_DIR = HOME / "state"
STATE = STATE_DIR / "disk_gate.json"
STOP = STATE_DIR / "STOP"                      # kept as a directory: legacy writers fail (GATE-DESIGN 5.5)
LEDGER = STATE_DIR / "stop_ledger.jsonl"       # append-only, hash-chained: init, stop, ack, admit, end
HEAD = STATE_DIR / "stop_ledger.head"          # count and SHA-256 of the last record, replaced atomically
LOCK = STATE_DIR / "stop_ledger.lock"
DATA = {"es": "/usr/share/elasticsearch/data", "pg": "/var/lib/postgresql/data"}
NAMES = {"es": "ES", "pg": "PG"}
PROC_ROOT = pathlib.Path("/proc")
CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
CLOCK_SLACK_S = 2.0
JVM_BIND_S = 300
API_TIMEOUT_S = 10
DOCKER_TIMEOUT_S = 300


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


def _ms(rfc):
    """Docker's RFC3339Nano UTC time in epoch milliseconds."""
    base, _, frac = str(rfc).rstrip("Z").partition(".")
    return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) * 1000 + int((frac + "000")[:3])


def _boot_id():
    try:
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(p, obj):
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(obj))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    _fsync_dir(p.parent)


# ------------------------------------------------------------------------------------------------------ stop ledger
def _read_head():
    try:
        h = json.loads(HEAD.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise LedgerError("the ledger's head anchor cannot be read") from None
    if not (isinstance(h, dict) and isinstance(h.get("count"), int) and h["count"] >= 1 and isinstance(h.get("tip"), str)):
        raise LedgerError("the ledger's head anchor is malformed")
    return h


def _read_ledger():
    """(records, hash of the last record), or (None, None) when there is no ledger yet. A LedgerError for any edit,
    removal, reordering or partial record, for fewer records than the head anchor holds or a different record at its
    position (truncated or replaced), for a missing ledger whose anchor remains, and for a v3 ledger without anchor."""
    head = _read_head()
    try:
        raw = LEDGER.read_bytes()
    except FileNotFoundError:
        if head:
            raise LedgerError(f"the stop ledger is missing but its head anchor says it held {head['count']} records")
        return None, None
    if raw and not raw.endswith(b"\n"):
        raise LedgerError("the stop ledger ends in a partial record")
    recs, prev, hashes = [], "0" * 64, []
    for i, ln in enumerate(raw.split(b"\n")[:-1]):
        try:
            r = json.loads(ln)
        except ValueError:
            raise LedgerError(f"stop ledger record {i + 1} is not JSON") from None
        if not isinstance(r, dict) or r.get("seq") != i + 1 or r.get("prev") != prev:
            raise LedgerError(f"stop ledger record {i + 1} breaks the sequence or the hash chain (edited or removed)")
        recs.append(r)
        prev = hashlib.sha256(ln).hexdigest()
        hashes.append(prev)
    if head:
        n = head["count"]
        if len(recs) < n or hashes[n - 1] != head["tip"]:
            raise LedgerError(f"the stop ledger holds {len(recs)} records, but its head anchor holds {n} with a "
                              f"different last record: truncated or replaced")
    elif any(r.get("kind") == "init" for r in recs):
        raise LedgerError("the head anchor of this ledger is missing")
    elif not recs:
        return None, None                           # an empty file and no anchor: not a ledger yet
    return recs, prev


def _stopdir_sig():
    """The STOP directory's change signature (ctime and mtime in ns, entries), or None when it is not a directory."""
    try:
        st = os.lstat(STOP)
        if not stat.S_ISDIR(st.st_mode):
            return None
        return [st.st_ctime_ns, st.st_mtime_ns, sorted(os.listdir(STOP))]
    except OSError:
        return None


class Ledger:
    """The stop ledger under an exclusive flock, verified on entry; nothing is ever rewritten. `need` (default True)
    makes a missing ledger, or one without an init record (a v2 ledger, or a ledger whose start was cut), a LedgerError:
    only 'init <reason>' adopts such a ledger, as a recorded operator action."""

    def __init__(self, need=True):
        self.need = need

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._f = open(LOCK, "a")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        try:
            self.recs, self.prev = _read_ledger()
            if self.recs is None and self.need:
                raise LedgerError("there is no stop ledger in this home: run 'python -m exp.disk_gate init <reason>' "
                                  "(a deleted ledger is never read as an empty one)")
            if self.need and not any(r.get("kind") == "init" for r in self.recs):
                raise LedgerError(f"this ledger ({len(self.recs)} records) has no init record: a v2 ledger, or one whose "
                                  "start was cut; read it, then adopt it with 'python -m exp.disk_gate init <reason>'")
            if self.need:
                self.legacy()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *a):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        return False

    def append(self, kind, **kw):
        recs = self.recs or []
        rec = dict(kw, seq=len(recs) + 1, prev=self.prev or "0" * 64, kind=kind, at=_utc())
        ln = json.dumps(rec, sort_keys=True).encode()
        fd = os.open(LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, ln + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        self.recs = recs + [rec]
        self.prev = hashlib.sha256(ln).hexdigest()
        self._write_head()
        return rec

    def _write_head(self):
        _write_json(HEAD, {"count": len(self.recs), "tip": self.prev, "at": _utc(), "stopdir": _stopdir_sig()})

    def _readme(self):
        (STOP / "README").write_text("Not a stop file. Record a stop with: python -m exp.disk_gate stop <reason>. Any "
                                     "change to this directory is recorded as a stop (GATE-DESIGN 5.5).\n")

    def _stopdir_changed(self):
        """A touch of the STOP directory, or anything copied, moved or written into it, is a stop: record it, keep
        what was written as STOP.ingested-<seq>-<n>, and anchor the directory's new signature in the head."""
        was, now = (_read_head() or {}).get("stopdir", "absent"), _stopdir_sig()
        if was == "absent":             # a head written before this rule: anchor the directory as it is
            self._write_head()
            return
        if now == was:
            return
        names = [n for n in (now or [0, 0, []])[2] if n != "README"]
        texts = []
        for n in names:
            try:
                if (STOP / n).is_file() and not (STOP / n).is_symlink():
                    texts.append((STOP / n).read_bytes()[:400].decode("utf-8", "replace").strip()[:100])
            except OSError:
                pass
        what = f"{len(names)} entries written into it" if names else "touched or replaced"
        text = " | ".join(t for t in texts if t)[:200]
        rec = self.append("stop", id=_new_id(), source="state/STOP directory",
                          reason=f"state/STOP directory {what}" + (f": written reason: {text}" if text else ""))
        for i, n in enumerate(names):
            try:
                os.replace(STOP / n, STATE_DIR / f"STOP.ingested-{rec['seq']}-{i}")
            except OSError:
                pass
        if not (STOP / "README").exists():
            self._readme()
        self._write_head()

    def legacy(self):
        """Keep state/STOP a directory (GATE-DESIGN 5.5). A regular file there is a stop: record it, keep it under a
        new name, make the directory; retried while a legacy writer recreates the file. A change to the directory is
        a stop too (_stopdir_changed)."""
        for _ in range(10):
            try:
                st = os.lstat(STOP)
            except FileNotFoundError:
                try:
                    STOP.mkdir()
                    self._readme()
                except FileExistsError:
                    continue
                self._write_head()
                return
            if stat.S_ISDIR(st.st_mode):
                self._stopdir_changed()
                return
            if stat.S_ISREG(st.st_mode):
                try:
                    text = STOP.read_bytes()[:400].decode("utf-8", "replace").strip()[:200]
                except OSError:
                    text = ""
                reason = f"written reason: {text}" if text else "no reason written in the file (writer unknown)"
            else:
                reason = "state/STOP was not a regular file (not read)"
            rec = self.append("stop", id=_new_id(), source="state/STOP file", reason=reason)
            try:
                os.replace(STOP, STATE_DIR / f"STOP.ingested-{rec['seq']}")
            except FileNotFoundError:
                pass
        raise LedgerError("state/STOP keeps reappearing as a file; a legacy writer is still running")

    def open_stops(self):
        acked = {r.get("stop_id") for r in self.recs if r["kind"] == "ack"}
        return [r for r in self.recs if r["kind"] == "stop" and r["id"] not in acked]

    def closed(self, run):
        return any(r["kind"] == "end" and r.get("run") == run for r in self.recs)

    def open_run(self):
        adm = [r for r in self.recs if r["kind"] == "admit" and not self.closed(r["run"])]
        return adm[-1] if adm else None

    def admitted(self, run, seq):
        return any(r["kind"] == "admit" and r.get("run") == run and r["seq"] == seq and r.get("v") == FORMAT
                   for r in self.recs)

    def stops_after(self, seq):
        return [r for r in self.recs if r["kind"] == "stop" and r["seq"] > seq]


def _stop_text(s):
    return f"stop {s['id']} at seq {s['seq']} ({s.get('source')}): {s.get('reason')}"


def open_stops():
    with Ledger() as L:
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


# ------------------------------------------------------------------------------------------------------- evidence
def _inspect(ref):
    try:
        r = _docker("inspect", ref, timeout=60)
        j = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return j[0] if isinstance(j, list) and j and isinstance(j[0], dict) else None


def _cgroup(j):
    """The container's cgroup v2 memory figures (bytes, or "max" for no limit) and its oom_kill count. The cgroup is
    the one /proc/<Pid>/cgroup names, and its path must name the container's full Id."""
    pid, cid = (j.get("State") or {}).get("Pid"), j.get("Id") or ""
    try:
        lines = (PROC_ROOT / str(int(pid)) / "cgroup").read_text().splitlines()
    except (OSError, TypeError, ValueError):
        raise GateError(f"cannot read the cgroup of the container's PID {pid}") from None
    path = next((x[3:] for x in lines if x.startswith("0::")), None)
    if not path or len(cid) < 64 or cid not in path:
        raise GateError("the container's PID is not in a cgroup v2 group named by its Id")
    d = CGROUP_ROOT / path.lstrip("/")

    def rd(name):
        v = (d / name).read_text().strip()
        return int(v) if v.isdigit() else v
    try:
        ev = dict(x.split() for x in (d / "memory.events").read_text().splitlines())
        out = {"max": rd("memory.max"), "swap": rd("memory.swap.max"), "peak": rd("memory.peak"),
               "oom_kill": int(ev["oom_kill"])}
    except (OSError, ValueError, KeyError) as e:
        raise GateError(f"cannot read the container's cgroup memory files ({type(e).__name__})") from None
    if not isinstance(out["peak"], int):
        raise GateError("the container's cgroup memory.peak is not a number")
    return out


def _es_get(path):
    """GET from the stream's ES with the gate's auth header, held in this process's memory only: never on a command
    line, never printed or written. Returns (status, body)."""
    req = urllib.request.Request(config.ES_URL.rstrip("/") + path)
    try:
        h = config.es_auth_header()
    except (OSError, ValueError, KeyError) as e:
        return f"no usable ES credential ({type(e).__name__})", None
    if h:
        req.add_header("Authorization", h)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError) as e:
        return type(e).__name__, None


def _es_fs(j):
    """ES's own view of its data path, from the API bound to the pinned container: (evidence, reasons)."""
    why, port = [], str(config.ES_PORT)
    ports = (j.get("NetworkSettings") or {}).get("Ports") or {}
    if not any(b.get("HostIp") == "127.0.0.1" and str(b.get("HostPort")) == port
               for bs in ports.values() for b in (bs or [])):
        why.append(f"the pinned ES container does not publish es_url's port {port} on 127.0.0.1, so the API may answer "
                   "for another process")
    code, nb = _es_get("/_nodes/_local/stats/fs,jvm")
    if code != 200 or not isinstance(nb, dict):
        return None, why + [f"the ES API could not be read (node stats {code})"]
    nodes = nb.get("nodes") or {}
    if len(nodes) != 1:
        return None, why + [f"the ES API reported {len(nodes)} local nodes"]
    (node_id, node), = nodes.items()
    paths = (node.get("fs") or {}).get("data") or []
    if len(paths) != 1 or paths[0].get("path") != DATA["es"]:
        return None, why + [f"ES does not report exactly one data path, at {DATA['es']} ({len(paths)} reported)"]
    p, jvm = paths[0], node.get("jvm") or {}
    try:
        ev = {"node": node_id, "type": p.get("type"), "total": int(p["total_in_bytes"]),
              "free": min(int(p["free_in_bytes"]), int(p["available_in_bytes"])),
              "thr": [int(p["low_watermark_free_space_in_bytes"]), int(p["high_watermark_free_space_in_bytes"]),
                      int(p["flood_stage_free_space_in_bytes"])],
              "jvm_start_ms": int(jvm["timestamp"]) - int(jvm["uptime_in_millis"])}
    except (KeyError, TypeError, ValueError):
        return None, why + ["the ES node stats lack the data path's size, free space, watermark thresholds or JVM figures"]
    if ev["type"] != "tmpfs":
        why.append(f"ES reports its data path on {ev['type']}, not tmpfs: its free space is shared with other tenants")
    return ev, why


def _pg_fs(cid):
    """PG's data directory filesystem, from statfs inside the container (the exec text holds no secret)."""
    try:
        r = _docker("exec", cid, "stat", "-f", "-c", "%T %b %S %a", DATA["pg"], timeout=60)
        typ, b, s, a = r.stdout.decode("utf-8", "replace").split()
        if r.returncode != 0:
            raise ValueError
        ev = {"type": typ, "total": int(b) * int(s), "free": int(a) * int(s), "thr": [0, 0, 0]}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, ["the PG data directory's statfs could not be read"]
    if typ != "tmpfs":
        return ev, [f"PG's data directory is on {typ}, not tmpfs: its free space is shared with other tenants"]
    return ev, []


def _observe(key, ref):
    """(evidence, reasons) for one container, by name at start and by Id after: the start rules R1 to R4."""
    K = NAMES[key]
    j = _inspect(ref)
    if j is None:
        return None, [f"the {K} container {str(ref)[:12]} cannot be inspected (removed or replaced)"]
    s = j.get("State") or {}
    ev = {"id": j.get("Id"), "name": (j.get("Name") or "").lstrip("/"), "started": s.get("StartedAt")}
    why = []
    if not s.get("Running") or s.get("Paused"):
        why.append(f"the {K} container is not running or is paused")
    if s.get("OOMKilled"):
        why.append(f"Docker reports an OOM kill in the {K} container")
    mounts, data = j.get("Mounts") or [], DATA[key]
    at = [m for m in mounts if m.get("Destination") == data]
    if len(at) != 1 or at[0].get("Type") != "tmpfs":
        kind = at[0].get("Type") if at else "the container's own layer"
        why.append(f"the {K} data directory {data} is not a Docker tmpfs mount ({kind}): its free space is shared "
                   "with other tenants")
    inside = sorted(m.get("Destination") for m in mounts if str(m.get("Destination", "")).startswith(data + "/"))
    if inside:
        why.append(f"a mount inside the data directory ({', '.join(inside)[:200]}) is outside the private tmpfs")
    if key == "pg":                     # ES confirms its own data path; PG's comes from the container's PGDATA
        env = dict(e.split("=", 1) for e in (j.get("Config") or {}).get("Env") or [] if "=" in e)
        pgd = env.get("PGDATA")
        if not pgd or not (pgd == data or pgd.startswith(data + "/")):
            why.append(f"PG's PGDATA ({pgd or 'not set'}) is not inside the tmpfs data directory {data}")
    try:
        ev["cg"] = _cgroup(j)
    except GateError as e:
        why.append(f"{K}: {e}")
    fs, w = _es_fs(j) if key == "es" else _pg_fs(ev["id"])
    ev["fs"] = fs
    why += [f"{K}: {x}" if not x.startswith(("the ", "ES ", "PG")) else x for x in w]
    cg = ev.get("cg")
    if fs and cg:
        T = max(fs["thr"])
        bound = fs["total"] - T - MARGIN
        if not isinstance(cg["max"], int):
            why.append(f"the {K} container has no memory limit (memory.max {cg['max']}): its tmpfs use is unbounded")
        elif cg["swap"] != 0:
            why.append(f"the {K} container may swap (memory.swap.max {cg['swap']}): tmpfs pages can leave the memory "
                       "bound")
        elif cg["max"] > bound:
            why.append(f"the {K} memory limit {cg['max'] / GIB:.2f} GiB can bring free space below "
                       f"{'the low watermark' if key == 'es' else 'the margin'}: tmpfs {fs['total'] / GIB:.2f} GiB, "
                       f"threshold {T / GIB:.2f} GiB, margin {MARGIN / GIB:.2f} GiB, bound {bound / GIB:.2f} GiB")
        if cg["peak"] > bound:
            why.append(f"the {K} container's lifetime memory peak {cg['peak'] / GIB:.2f} GiB exceeds the bound "
                       f"{bound / GIB:.2f} GiB: its tmpfs free space may have reached the threshold")
        if fs["free"] < T + MARGIN:      # the argument's conclusion, observed directly
            why.append(f"the {K} tmpfs has {fs['free'] / GIB:.2f} GiB free, under threshold {T / GIB:.2f} GiB + margin "
                       f"{MARGIN / GIB:.2f} GiB")
        used = fs["total"] - fs["free"]
        if used > cg["peak"]:            # the argument's premise: every tmpfs page is charged to this container
            why.append(f"the {K} tmpfs holds {used / GIB:.2f} GiB, more than the container's lifetime memory peak "
                       f"{cg['peak'] / GIB:.2f} GiB: pages in it are charged to another cgroup (written from outside the "
                       "container), so the memory limit does not bound it")
    return ev, why


def _drift(now, adm):
    """R4 and R5: what differs from admission (Id, StartedAt, node id, oom_kill, limit, swap, size, thresholds)."""
    why = []
    for key in ("es", "pg"):
        K, a, n = NAMES[key], adm[key], now.get(key)
        if n is None:
            continue
        if n["id"] != a["id"]:
            why.append(f"the {K} container was replaced ({a['id'][:12]} -> {str(n['id'])[:12]})")
        if n["started"] != a["started"]:
            why.append(f"the {K} container restarted: StartedAt {a['started']} -> {n['started']}")
        ac, nc = a.get("cg") or {}, n.get("cg") or {}
        if nc and ac:
            if nc["oom_kill"] != ac["oom_kill"]:
                why.append(f"an OOM kill in the {K} container during the run (oom_kill {ac['oom_kill']} -> "
                           f"{nc['oom_kill']})")
            if (nc["max"], nc["swap"]) != (ac["max"], ac["swap"]):
                why.append(f"the {K} memory limit or swap changed during the run")
        af, nf = a.get("fs") or {}, n.get("fs") or {}
        if nf and af:
            if nf["total"] != af["total"]:
                why.append(f"the {K} tmpfs size changed during the run")
            if key == "es" and nf["node"] != af["node"]:
                why.append("the ES node id changed: its data path was wiped or replaced during the run")
            if key == "es" and nf["thr"] != af["thr"]:
                why.append("ES's watermark thresholds changed during the run")
    return why


def _home_bytes():
    try:
        r = subprocess.run(["du", "-sb", str(HOME)], capture_output=True, timeout=600)
        o = r.stdout.decode("utf-8", "replace").split()
        if r.returncode == 0 and o and o[0].isdigit():
            return int(o[0])
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise GateError("cannot measure WP_HOME's size")


def free_gb():
    return shutil.disk_usage(HOME).free / GIB


def _summary(obs):
    out = []
    for key in ("es", "pg"):
        o = obs.get(key) or {}
        fs, cg = o.get("fs") or {}, o.get("cg") or {}
        if fs and cg and isinstance(cg.get("max"), int):
            out.append(f"{key} {str(o['id'])[:12]} {fs['type']} {fs['total'] / GIB:.2f} GiB limit {cg['max'] / GIB:.2f} "
                       f"peak {cg['peak'] / GIB:.2f} free {fs['free'] / GIB:.2f}")
    return "; ".join(out)


# --------------------------------------------------------------------------------------------------------- commands
def _blocked(msg):
    print(f"disk_gate BLOCKED start: {msg}")
    sys.exit(2)


def _void_now(msg):
    print(f"disk_gate VOID: {msg}")
    sys.exit(3)


def _start():
    try:
        with Ledger() as L:
            b = _admission_blocker(L)
    except LedgerError as e:
        b = f"the stop ledger is not intact ({e})"
    if b:
        _blocked(b)
    names = dict(zip(("es", "pg"), _targets()))
    obs, why = {}, []
    for key in ("es", "pg"):
        obs[key], w = _observe(key, names[key])
        why += w
    es = obs["es"]
    if es and es.get("fs"):
        lag = (es["fs"]["jvm_start_ms"] - _ms(es["started"])) / 1000
        if not -CLOCK_SLACK_S <= lag <= JVM_BIND_S:
            why.append(f"the ES API's JVM did not start with the pinned container (JVM start {lag:+.0f} s from its "
                       "StartedAt): the API answers for another process or run")
    if why:
        _blocked("; ".join(why))
    try:
        base = _home_bytes()
    except GateError as e:
        _blocked(str(e))
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(4).hex()
    try:
        with Ledger() as L:                         # stops, open run, host free space and the admit record: one step
            b = _admission_blocker(L)
            f = free_gb()
            if not b and f < START_GB:
                b = f"host free {f:.1f} GiB < {START_GB} (measured at admission)"
            adm = None if b else L.append("admit", v=FORMAT, run=run_id, es=es["id"], pg=obs["pg"]["id"],
                                          free_bytes=int(f * GIB), free_at=_utc())
    except LedgerError as e:
        b = f"the stop ledger is not intact ({e})"
    if b:
        _blocked(b)
    _write_json(STATE, {"v": FORMAT, "run": run_id, "admit_seq": adm["seq"], "obs": obs, "base_home": base,
                        "boot": _boot_id(), "floor_breaches": []})
    print(f"disk_gate start run {run_id} free {f:.1f} GiB {_summary(obs)} admitted at seq {adm['seq']}")


def _state():
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or st.get("v") != FORMAT or "run" not in st:
        _void_now("no run admitted by this gate (none, or admitted by an older gate: close it with 'abort')")
    return st


def _judge(st, when):
    """Every rule against the containers now, and against admission."""
    obs, why = {}, []
    for key in ("es", "pg"):
        obs[key], w = _observe(key, st["obs"][key]["id"])
        why += w
    why += _drift(obs, st["obs"])
    if _boot_id() != st["boot"]:
        why.append("the host rebooted during the run")
    try:
        d = (_home_bytes() - st["base_home"]) / GIB
        if d > CAP_GB:
            why.append(f"WP_HOME grew {d:.2f} GiB > cap {CAP_GB} {when}")
    except GateError as e:
        why.append(f"{e} {when}")
    return obs, why


def _close(st, why, **extra):
    try:
        with Ledger() as L:
            if not L.closed(st["run"]):
                L.append("end", run=st["run"], verdict="VOID", reasons=why[:20], **extra)
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e}); the run could not be closed in it")


def _ledger_why(L, st, when):
    why = []
    if not L.admitted(st["run"], st["admit_seq"]):
        why.append(f"this run's admission record (run {st['run']}, seq {st['admit_seq']}) is not in the ledger: "
                   "truncated, reset or replaced")
    why += [f"STOP: {_stop_text(s)}, {when}" for s in L.stops_after(st["admit_seq"])]
    return why


def _check(name):
    st = _state()
    why = []
    try:
        with Ledger() as L:
            if L.closed(st["run"]):
                _void_now(f"run {st['run']} is already closed")
            why += _ledger_why(L, st, f"before {name}")
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e})")
    obs, w = _judge(st, f"before {name}")
    why += w
    f = free_gb()
    if why:
        _close(st, why, at_check=name)
        for x in why:
            print(f"disk_gate VOID: {x}")
        sys.exit(3)
    if f < FLOOR_GB:
        st["floor_breaches"].append({"before": name, "free_gb": round(f, 2)})
        _write_json(STATE, st)
    print(f"disk_gate {name} free {f:.1f} GiB {_summary(obs)}")


def _end():
    st = _state()
    obs, why = _judge(st, "by the end of the run")
    f = free_gb()
    try:
        with Ledger() as L:                         # the final stop read and the end record: one locked step
            if L.closed(st["run"]):
                why.append(f"run {st['run']} was closed before end (a VOID check or an abort)")
            else:
                why += _ledger_why(L, st, "during the run")
                L.append("end", run=st["run"], verdict="VOID" if why else "VALID", reasons=why[:20],
                         free_bytes=int(f * GIB), evidence=_summary(obs))
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e})")
    print(f"disk_gate end free {f:.1f} GiB floor_breaches {len(st['floor_breaches'])} {_summary(obs)}")
    for x in why:
        print(f"disk_gate VOID: {x}")
    if why:
        sys.exit(3)


def _ledger_reset(reason):
    """Keep a DAMAGED ledger (and its anchor) under new names and start a new one that names them, with one open stop:
    the stops in the damaged ledger cannot be read, so a person acknowledges them by id before any start. An intact
    ledger is refused (its stops are acknowledged one by one with clear-stop)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            recs, _ = _read_ledger()
        except LedgerError:
            recs = "damaged"
        if recs is None:
            print("disk_gate ledger-reset: refused: there is no ledger to reset ('init <reason>' starts one)")
            sys.exit(2)
        if recs != "damaged":
            print("disk_gate ledger-reset: refused: the ledger is intact " +
                  ("(acknowledge stops one by one with clear-stop)" if any(r.get("kind") == "init" for r in recs)
                   else "(a ledger without an init record is adopted with 'init <reason>')"))
            sys.exit(2)
        tag = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        old = {}
        if LEDGER.exists():
            data = LEDGER.read_bytes()
            old = {"file": f"stop_ledger.broken-{tag}.jsonl", "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
            os.replace(LEDGER, STATE_DIR / old["file"])
        if HEAD.exists():
            os.replace(HEAD, STATE_DIR / f"stop_ledger.head.broken-{tag}")
        L = Ledger(need=False)
        L.recs, L.prev = None, None
        L.append("init", v=FORMAT, source="ledger-reset", reason=reason[:200], reset_from=old)
        s = L.append("stop", id=_new_id(), source="ledger-reset",
                     reason=f"the stops in the damaged ledger {old.get('file', '(missing)')} cannot be read: check it, "
                            "then acknowledge this stop")
        L.legacy()
    print(f"disk_gate ledger-reset: new ledger started; the old one kept as {old.get('file', '(none)')}; a run open "
          f"before the reset ends VOID; stop {s['id']} is open until acknowledged")


def _ledger_cmd(cmd, args):
    reason = " ".join(args[1:] if cmd == "clear-stop" else args).strip()
    if cmd == "ledger-reset":
        if not reason:
            raise SystemExit("ledger-reset needs a reason")
        return _ledger_reset(reason)
    if cmd == "init":
        if not reason:
            raise SystemExit("init needs a reason")
        with Ledger(need=False) as L:
            if L.recs is not None and any(r.get("kind") == "init" for r in L.recs):
                print("disk_gate init: the ledger exists already (nothing changed)")
                return None
            if L.recs is not None:           # a v2 ledger, or one whose start was cut: adopted by this explicit act
                L.append("init", v=FORMAT, source="init: adopted a ledger without an init record",
                         adopted_records=len(L.recs), reason=reason[:200])
                L.legacy()
                print(f"disk_gate init: adopted the existing {len(L.recs) - 1} records; open stops in them stay open")
                return None
            L.append("init", v=FORMAT, source="init", reason=reason[:200])
            L.legacy()
        print("disk_gate init: new stop ledger started")
        return None
    with Ledger() as L:
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
                return None
            L.append("end", run=o["run"], verdict="VOID", reasons=[f"aborted: {reason[:200]}"])
            print(f"disk_gate abort: run {o['run']} closed VOID")
    return None


def main(cmd="", *args):
    if cmd == "start":
        return _start()
    if cmd == "check":
        return _check(args[0] if args else "")
    if cmd == "end":
        return _end()
    if cmd in ("init", "stop", "stops", "clear-stop", "abort", "ledger-reset"):
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

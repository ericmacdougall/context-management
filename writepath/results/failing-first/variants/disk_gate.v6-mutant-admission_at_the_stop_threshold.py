"""Run-validity gate v6 for stamped runs (reviews a108fc3 and aacf3c0). Design, argument and limits: runkit/GATE-DESIGN.md
sections 5 (v3, the tmpfs argument), 6 (v4: the thing inspected is the thing used), 8 (v5: the gate is the launcher)
and 9 (v6: the gate owns the stores' reset, the live catalogs and the experiment's death). Every path to VALID or exit
0, with the check behind each conjunct: runkit/VERDICT-PATHS.md.

The storage hazard is removed, not detected (v4, unchanged): each store's data directory is a private tmpfs, larger
than its container's memory limit plus Elasticsearch's largest watermark threshold, with no swap and no inode limit,
bound to the endpoints the experiment uses. v5 owns the run around it: the gate itself admits the run, starts the
experiment, waits for it and ends the run, in one process that holds an exclusive lock for the whole run and shares
it with the experiment, so no other command can close, abort or start a run while either lives. v6 resets the stores
inside that lock, checks the live catalogs against the committed expectation before and after the reset and at end,
and judges how the experiment ended.

  run LABEL MODULE [ARGS]   the only way to make a stamped run; start it as
                 `python -I -B <tree>/runkit/isolated.py exp.disk_gate run LABEL MODULE [ARGS]`. BLOCKED (exit 2)
                 unless: the interpreter is isolated (-I) with bytecode off; the gate's tree is a pinned tree under
                 WP_HOME/runs whose files equal TREE.manifest (no special file, untracked file or cache) and whose
                 manifest names this gate's own hash; the registered fixtures are in place; no other run holds the run
                 lock; the ledger is intact with no open stop, no open run and no legacy state/STOP path; this gate's
                 cgroup memory.events can be read; the cluster catalogs show no weakening before the reset; the reset
                 (exp.cleanup, wp.setup --reset, run by the gate under its lock) succeeds; after it the stores are
                 clean and the catalogs equal the committed expectation (exp/guard_policy.py); no container but the two
                 stores bind-mounts WP_HOME; every v4 storage rule holds; host free >= START_GB. Then the experiment
                 runs as `python -I -B runkit/isolated.py MODULE ARGS` from the tree, with a listed environment plus
                 WP_GATE_RUN and WP_RUN_DIR (its own new directory results/runs/<run>/, where its output goes too). If
                 host free falls under STOP_GB the gate stops it. At its exit: VOID (exit 3) if it died by a signal the
                 gate did not send, an OOM kill happened in the gate's cgroup, a process of the run outlived it, the
                 tree or fixtures changed, a run file is special or unreadable as a row, a row names another run or
                 none, the catalogs differ from admission or from the expectation (a reset, a replaced store, a
                 dropped guard, a grant, a role, a setting), a container bind-mounts WP_HOME, a stop was recorded,
                 host free went under STOP_GB, or any v4 rule or drift check fails; else VALID (exit 0). The verdict is
                 the end record in the ledger (with every run file's SHA-256) and this exit status; the experiment's
                 text never reaches the gate's output. After the end record, before the lock is released, the gate
                 deletes the stream's indices (exp.cleanup).
  verdict RUN    print the ledger's verdict for RUN: exit 0 VALID (exactly one end record), 3 VOID or conflicting end
                 records, 2 open or unknown.
  abort RUN REASON   close the open run RUN VOID, only when its gate and experiment are dead: the run lock is free and
                 the run's process group has no member. Anything else is refused (exit 2) and nothing is written.
  init REASON    start a new stop ledger, or adopt an existing ledger that has no init record.
  preflight      the stores must be clean (the same rule as admission).
  stop REASON    record a stop.   stops: list unacknowledged stops and a legacy path (exit 2 if any).
  clear-stop ID REASON   acknowledge ONE stop by id; it unblocks later runs and never changes a run's verdict.
  ledger-reset REASON    keep a DAMAGED ledger under a new name and start a new one holding one open stop.
  retire-legacy-stop REASON   move a legacy state/STOP path aside unread and record a stop to acknowledge (migration).
  version        gate_version=<version> sha256 <this file's SHA-256> and gate_limits=<LIMITS>.
start, check and end are gone (exit 2): they acted on whatever run was current. Prints numbers, ids, store paths and
reasons only, never a credential."""
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from exp import guard_policy as GP
from wp import config

FORMAT = 6                                     # v6 records; abort also closes a v5 run (same lock, same group rule)
ABORTABLE = (5, 6)
VERSION = "disk_gate v6"
GATE_SHA256 = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
LIMITS_ID = "v6"
LIMITS = ("LIMITS v6 (GATE-DESIGN 9.4, 8.4, 6.4): not detected, the operator trust boundary: tampering by the same OS "
          "user with the gate, its tree, state, ledger, lock or manifest; root on the host; a process that leaves the "
          "run's process group or cgroup on purpose; rows written outside the run directory (never staged); a docker "
          "pause and unpause, an ES disk or allocation setting changed and restored, or an endpoint switched and back, "
          "between two gate steps; pages written into a store's tmpfs from outside its container; anything after "
          "window_end; clock stability. Not inspected: Elasticsearch indices deleted during the run by a process that "
          "does not take the run lock (an older tree's exp.cleanup, a direct DELETE); Elasticsearch users, roles, API "
          "keys, ingest pipelines and snapshot repositories; a catalog change made and reverted within the run; the "
          "venv's packages, .pth files and sitecustomize (versions recorded, not verified); store content written by "
          "a process outside the run. The OOM counter is the gate's whole cgroup: an OOM kill of any process sharing "
          "it voids the run. The manifest's commit is named, not judged: tree_manifest.py check recomputes it.")
# amendment GF1 (the user's ruling of 2026-11-18, answer 3335a07a): a lab run starts with >= 10 GB host free and stops
# itself under 8 GB; the 2 GB own-usage cap is amendment 17c's
START_GB, STOP_GB, CAP_GB = 8.0, 8.0, 2.0
POLL_S = 5.0                                   # the in-run host free check
TERM_GRACE_S = 30                              # the stop signal, then the kill, when the gate stops an experiment
TOOL_TIMEOUT_S = 900                           # exp.cleanup and wp.setup --reset under the gate's lock
ROW_CAP = 16 * 1024 ** 2                       # a JSON row larger than this is not parsed: a reason (m-C)
ISOLATED = "runkit/isolated.py"                # m-G: every interpreter the gate starts runs -I -B through it
# m-G: the experiment's environment is these names and the WP_* names, nothing else; the admit record holds the names
# and a SHA-256 prefix of each value
EXP_ENV_NAMES = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "DOCKER_HOST",
                 "DOCKER_CONTEXT", "DOCKER_CONFIG")
EXP_ENV_PREFIXES = ("WP_",)
GATE_ENV = ("WP_GATE_RUN", "WP_RUN_DIR", "WP_RUN_LOCK_FD")       # set by the gate only, never inherited
RESET_STEPS = (("cleanup", "exp.cleanup", ("gate-admission",)), ("reset", "wp.setup", ("--reset",)))
AFTER_STEPS = (("cleanup-after", "exp.cleanup", ("gate-after-end",)),)
GIB = 1024 ** 3
MARGIN = 256 * 1024 ** 2                       # headroom over the bound for statfs rounding (inodes: B4, nr_inodes=0)
HOME = pathlib.Path(os.environ["WP_HOME"])
ROOT = pathlib.Path(__file__).resolve().parents[1]     # the pinned tree this gate runs from (P4)
# NOT under WP_HOME/logs: that directory is a registered store the erasure gate inventories.
STATE_DIR = HOME / "state"
STOP = STATE_DIR / "STOP"                      # legacy path: never read; its existence stops (GATE-DESIGN 6.2 B7)
LEDGER = STATE_DIR / "stop_ledger.jsonl"       # append-only, hash-chained: init, stop, ack, admit, end
HEAD = STATE_DIR / "stop_ledger.head"          # count and SHA-256 of the last record, replaced atomically
LOCK = STATE_DIR / "stop_ledger.lock"
RUN_LOCK = STATE_DIR / "run.lock"              # P1: held by the gate and its experiment for the whole run
RUNS_DIR = HOME / "results" / "runs"           # P8: one new directory per run
MANIFEST, GIT_HEAD = "TREE.manifest", "GIT_HEAD"
FIXTURES = "runkit/FIXTURES.sha256"            # P6
FIXTURE_DIRS, FIXTURE_FILES = ("exp", "wp", "old_repro", "host"), ("requirements.lock",)
DATA = {"es": "/usr/share/elasticsearch/data", "pg": "/var/lib/postgresql/data"}
NAMES = {"es": "ES", "pg": "PG"}
PROC_ROOT = pathlib.Path("/proc")
CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
API_TIMEOUT_S = 10
DOCKER_TIMEOUT_S = 300
PG_STATEMENT_TIMEOUT = "10s"                   # m-6: on every connection the gate opens
NONCE = "disk_gate.nonce"                      # written into PG's data directory for one read, then removed (B1)
CLUSTER_KEYS = ("cluster.routing.allocation.", "cluster.info.", "cluster.blocks.")      # B5
INDEX_KEYS = ("index.blocks.", "index.routing.allocation.")
INDEX_DEFAULTS = ("index.routing.allocation.include._tier_preference",)                 # ES sets it on every index
RUN_RX = re.compile(r"^r-[0-9a-f]{32}$")
UTC_RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
LABEL_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODULE_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
# B3, m-1: run inside the container with `sh -c PROBE disk_gate-probe <mount> <path>...`. Prints NUL-terminated
# fields, a fixed count per tag: M <mountinfo line>; F <statfs: type, blocks, block size, available, inodes, free
# inodes>; P <device> <resolved> <path> for the mount and every argument; L <device> <resolved> <link> for every link
# under the mount (names come to it as arguments of find -exec, never as lines); then END. "-" when a path does not
# resolve. Arguments are store paths, never secrets.
PROBE = r"""# disk_gate probe v5
r=$1; shift
R='z() { printf "%s\0" "$@"; }
res() { q=$(realpath -e -- "$2" 2>/dev/null && echo x) && q=${q%?x} && d=$(stat -L -c %d -- "$2" 2>/dev/null) &&
  z "$1" "$d" "$q" "$2" || z "$1" - - "$2"; }'
eval "$R"
while IFS= read -r l; do z M "$l"; done < /proc/self/mountinfo
s=$(stat -f --printf '%T\t%b\t%S\t%a\t%c\t%d' -- "$r") || exit 3
z F "$s"
for p in "$r" "$@"; do res P "$p"; done
find "$r" -type l > /dev/null 2>&1 || { z E find; exit 3; }
find "$r" -type l -exec sh -c "$R"'
for l do res L "$l"; done' disk_gate-links {} + || { z E find; exit 3; }
printf 'END\0'"""
PROBE_FIELDS = {"M": 1, "F": 1, "P": 3, "L": 3}


class GateError(Exception):
    pass


class LedgerError(Exception):
    pass


class Blocked(Exception):
    """Admission refused: the run is not admitted and nothing of it was recorded."""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def _targets():
    """The ES and PG container names this stream's conf names (wp.config). runkit/gate_live.py replaces this function,
    and only it, for its labelled live check on its own containers."""
    return config.ES_CONTAINER, config.PG_CONTAINER


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id():
    return "s-" + os.urandom(4).hex()


def _docker(*args, timeout=None, input=None):
    """docker with no terminal. `input` (bytes) goes on stdin: the only way a credential reaches a docker exec, since
    Docker's event stream records every exec command line."""
    kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout or DOCKER_TIMEOUT_S, **kw)


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


def _legacy_stop():
    """What exists at the legacy path state/STOP (its kind only; never read), or None. Any form of it is a stop
    (GATE-DESIGN 6.2 B7)."""
    try:
        st = os.lstat(STOP)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"unreadable ({type(e).__name__})"
    return ("a directory" if stat.S_ISDIR(st.st_mode) else "a link" if stat.S_ISLNK(st.st_mode) else
            f"a file of {st.st_size} bytes" if stat.S_ISREG(st.st_mode) else "a special file")


def _legacy_why():
    k = _legacy_stop()
    return (f"a legacy state/STOP path exists ({k}): the gate never reads it; read it, then 'python -m exp.disk_gate "
            "retire-legacy-stop <reason>' and acknowledge the stop that records") if k else None


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
        _write_json(HEAD, {"count": len(self.recs), "tip": self.prev, "at": _utc()})

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
    lg = _legacy_why()
    if lg:
        return lg
    op = L.open_stops()
    if op:
        return ("unacknowledged stop(s): " + "; ".join(_stop_text(s) for s in op) +
                "; read them, then 'python -m exp.disk_gate clear-stop <stop id> <reason>'")
    o = L.open_run()
    if o:
        return (f"run {o['run']} (admitted at seq {o['seq']}) has no end record; if its gate and experiment are dead: "
                f"'python -B -m exp.disk_gate abort {o['run']} <reason>'")
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
    """The container's cgroup v2 memory figures (bytes, or "max" for no limit), its swap in use and its OOM counts
    (memory.events: oom, an allocation about to fail at the limit; oom_kill, a process killed). The cgroup is the one
    /proc/<Pid>/cgroup names, and its path must name the container's full Id."""
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
        out = {"max": rd("memory.max"), "swap": rd("memory.swap.max"), "swap_cur": rd("memory.swap.current"),
               "peak": rd("memory.peak"), "oom": int(ev["oom"]), "oom_kill": int(ev["oom_kill"])}
    except (OSError, ValueError, KeyError) as e:
        raise GateError(f"cannot read the container's cgroup memory files ({type(e).__name__})") from None
    if not isinstance(out["peak"], int) or not isinstance(out["swap_cur"], int):
        raise GateError("the container's cgroup memory.peak or memory.swap.current is not a number")
    return out


def _es_get(path):
    """GET from the stream's ES (es_url, the experiment's URL) with the gate's auth header, held in this process's
    memory only: never on a command line, never printed or written. Returns (status, body)."""
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


def _container_port(j):
    """The container port that publishes es_url's port on 127.0.0.1, or None."""
    for k, bs in ((j.get("NetworkSettings") or {}).get("Ports") or {}).items():
        if any(b.get("HostIp") == "127.0.0.1" and str(b.get("HostPort")) == str(config.ES_PORT) for b in (bs or [])):
            return str(k).split("/")[0]
    return None


def _es_fs(j):
    """ES's own view of its data path through es_url, and the 127.0.0.1 publish check: (evidence, reasons)."""
    why = []
    if not _container_port(j):
        why.append(f"the pinned ES container does not publish es_url's port {config.ES_PORT} on 127.0.0.1, so the API "
                   "may answer for another process")
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
    p = paths[0]
    try:
        ev = {"node": node_id, "path": p["path"], "type": p.get("type"), "total": int(p["total_in_bytes"]),
              "free": min(int(p["free_in_bytes"]), int(p["available_in_bytes"])),
              "thr": [int(p["low_watermark_free_space_in_bytes"]), int(p["high_watermark_free_space_in_bytes"]),
                      int(p["flood_stage_free_space_in_bytes"])]}
    except (KeyError, TypeError, ValueError):
        return None, why + ["the ES node stats lack the data path's size, free space or watermark thresholds"]
    if ev["type"] != "tmpfs":
        why.append(f"ES reports its data path on {ev['type']}, not tmpfs: its free space is shared with other tenants")
    return ev, why


def _nodes_eph(body):
    return {str(k): (v or {}).get("ephemeral_id") for k, v in ((body or {}).get("nodes") or {}).items()}


def _es_bind(j):
    """B2: the node at es_url (node id, ephemeral id: new at every process start) must be the node listening inside
    the inspected container, asked from inside it with curl; the credential goes on curl's stdin, never into argv."""
    q = "/_cluster/state/nodes?local=true&filter_path=nodes.*.ephemeral_id"
    code, body = _es_get(q)
    if code != 200 or not isinstance(body, dict):
        return None, [f"the ES API could not be read (cluster state {code})"]
    outside, cport = _nodes_eph(body), _container_port(j)
    if not cport:
        return outside, []                          # reported by the publish check
    scheme = urllib.parse.urlsplit(config.ES_URL).scheme or "http"
    try:
        hdr = config.es_auth_header()
    except (OSError, ValueError, KeyError) as e:
        return outside, [f"no usable ES credential ({type(e).__name__})"]
    args = (["exec", "-i", str(j.get("Id")), "curl", "-sS", "--max-time", str(API_TIMEOUT_S)] +
            (["-k"] if scheme == "https" else []) + (["-H", "@-"] if hdr else []) + [f"{scheme}://127.0.0.1:{cport}{q}"])
    try:
        r = _docker(*args, input=(f"Authorization: {hdr}\n".encode() if hdr else b""), timeout=60)
        inside = _nodes_eph(json.loads(r.stdout)) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        inside = None
    if not inside:
        return outside, ["the ES node inside the pinned container could not be asked for its node and ephemeral id"]
    if len(outside) != 1 or outside != inside:
        return outside, ["the ES node at es_url (node id and ephemeral id) is not the node running in the inspected "
                         "container: es_url answers for another process"]
    return outside, []


def _es_settings():
    """B5: ES's disk and allocation settings, every layer (persistent, transient, defaults), and every index's blocks
    and allocation filters, as read through es_url: (settings, reasons)."""
    code, b = _es_get("/_cluster/settings?include_defaults=true&flat_settings=true")
    if code != 200 or not isinstance(b, dict):
        return None, [f"ES's cluster settings could not be read ({code})"]
    cl = {layer: {k: v if isinstance(v, str) else json.dumps(v, sort_keys=True)
                  for k, v in (b.get(layer) or {}).items() if k.startswith(CLUSTER_KEYS)}
          for layer in ("persistent", "transient", "defaults")}
    code, ib = _es_get("/_all/_settings/index.blocks.*,index.routing.allocation.*?flat_settings=true"
                       "&expand_wildcards=all")
    if code != 200 or not isinstance(ib, dict):
        return None, [f"ES's index settings could not be read ({code})"]
    ix = {str(n): {k: str(v) for k, v in ((d or {}).get("settings") or {}).items()
                   if k.startswith(INDEX_KEYS) and k not in INDEX_DEFAULTS} for n, d in ib.items()}
    why = []
    key = "cluster.routing.allocation.disk.threshold_enabled"
    te = next((cl[x][key] for x in ("transient", "persistent", "defaults") if key in cl[x]), None)
    if str(te).lower() != "true":
        why.append(f"ES's disk decider is not enabled ({key} {te}): the watermarks the bound is made for do not apply")
    flood = sorted(n for n, d in ix.items() if str(d.get("index.blocks.read_only_allow_delete")).lower() == "true")
    if flood:
        why.append(f"index(es) under a flood-stage block (index.blocks.read_only_allow_delete): {', '.join(flood)[:200]}")
    return {"cluster": cl, "index": ix}, why


def _settings_drift(a, n):
    if not a or not n:
        return []
    why = []
    keys = sorted({k for x in ("persistent", "transient", "defaults")
                   for k in set(a["cluster"][x]) | set(n["cluster"][x]) if a["cluster"][x].get(k) != n["cluster"][x].get(k)})
    if keys:
        why.append(f"ES's disk or allocation settings changed during the run: {', '.join(keys)[:300]}")
    changed = [f"{name} {k}" for name, d in sorted(n["index"].items())
               for k in sorted(set(d) | set(a["index"].get(name, {}))) if d.get(k) != a["index"].get(name, {}).get(k)]
    if changed:
        why.append(f"index blocks or allocation settings changed during the run: {'; '.join(changed)[:300]}")
    return why


def _unoct(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _probe(K, cid, root, paths):
    """B3: run PROBE inside the container: (parsed output, reasons). Fails closed on any error."""
    try:
        r = _docker("exec", str(cid), "sh", "-c", PROBE, "disk_gate-probe", root, *paths, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is None or r.returncode != 0:
        return None, [f"the {K} data mount's statfs and mount table could not be read (the in-container probe failed)"]
    try:
        return _parse_probe(r.stdout, root, paths), []
    except (ValueError, IndexError):
        return None, [f"the {K} in-container probe's output could not be read"]


def _parse_probe(data, root, paths):
    """m-1: the probe's NUL-terminated fields, a fixed count per tag, ending in END and nothing after it. Refuses an
    unknown tag, a short record, a second statfs, a device that is not a number, and a path list that is not exactly
    the mount followed by the requested paths."""
    parts = data.split(b"\0")
    if len(parts) < 2 or parts[-1] != b"" or parts[-2] != b"END":
        raise ValueError("no END, or data after it")
    f = [x.decode("utf-8", "surrogateescape") for x in parts[:-2]]
    pr, plist, i = {"mounts": [], "paths": {}, "links": []}, [], 0
    while i < len(f):
        tag, n = f[i], PROBE_FIELDS.get(f[i])
        if n is None or i + 1 + n > len(f):
            raise ValueError("unknown tag or short record")
        v, i = f[i + 1:i + 1 + n], i + 1 + n
        if tag == "M":
            if "\n" in v[0]:
                raise ValueError("mountinfo")
            pre, _, post = v[0].partition(" - ")
            a, g = pre.split(), post.split()
            pr["mounts"].append({"mp": _unoct(a[4]), "mm": a[2], "fs": g[0], "so": g[2] if len(g) > 2 else ""})
        elif tag == "F":
            t, b, s, av, c, d = v[0].split("\t")
            if "statfs" in pr:
                raise ValueError("second statfs")
            pr["statfs"] = {"type": t, "total": int(b) * int(s), "free": int(av) * int(s), "files": int(c),
                            "ffree": int(d)}
        else:
            dev, real, p = v
            if not (dev == "-" or dev.isdigit()) or (dev == "-") != (real == "-"):
                raise ValueError("device")
            if tag == "P":
                plist.append(p)
                pr["paths"][p] = (dev, real)
            else:
                pr["links"].append((p, dev, real))
    if "statfs" not in pr or plist != [root] + list(paths):
        raise ValueError("statfs or path list")
    return pr


def _topology(K, root, pr, named):
    """B3, B4: (reasons, isolated). One tmpfs mount at root and none below it, with nr_inodes=0; every named path and
    every link under root on that mount's device."""
    why = []
    at = [m for m in pr["mounts"] if m["mp"] == root]
    below = sorted(m["mp"] for m in pr["mounts"] if m["mp"].startswith(root.rstrip("/") + "/"))
    rdev = pr["paths"].get(root, ("-", "-"))[0]
    if len(at) != 1 or at[0]["fs"] != "tmpfs" or pr["statfs"]["type"] != "tmpfs":
        why.append(f"the {K} data mount {root} is not a single tmpfs mount in the container's mount table "
                   f"({', '.join(m['fs'] for m in at) or 'none'}; statfs {pr['statfs']['type']})")
    elif rdev == "-" or f"{os.major(int(rdev))}:{os.minor(int(rdev))}" != at[0]["mm"]:
        why.append(f"the {K} data mount {root} does not resolve to the tmpfs mount's device")
    else:
        opts = at[0]["so"].split(",")
        if "nr_inodes=0" not in opts or pr["statfs"]["files"] != 0:
            lim = next((o for o in opts if o.startswith("nr_inodes=")), "the default nr_inodes")
            why.append(f"the {K} tmpfs has an inode limit ({lim}, {pr['statfs']['files']} inodes): inode exhaustion is "
                       "not bounded by the memory limit; mount it with nr_inodes=0")
    if below:
        why.append(f"mount(s) below the {K} data mount: {', '.join(below)[:200]}: their space is outside the private "
                   "tmpfs")
    isolated = not why
    for label, p in named:
        dev, real = pr["paths"].get(p, ("-", "-"))
        if dev == "-":
            why.append(f"{label} {p} does not resolve inside the {K} container")
        elif dev != rdev:
            why.append(f"{label} {p} resolves to {real}, which is not on the private tmpfs")
    for link, dev, real in pr["links"]:
        if dev == "-":
            why.append(f"a link under the {K} data mount does not resolve ({ascii(link)[:200]})")
        elif dev != rdev:
            why.append(f"the link {ascii(link)[:200]} under the {K} data mount resolves to {ascii(real)[:200]}, which is "
                       "not on the private tmpfs")
    return why, isolated and not why


def _pg_connect(dbname):
    """The experiment's own connection function (wp.db.connect) to its endpoint config.PG_HOST:PG_PORT, database
    dbname, as the cluster admin (its pgpass file; nothing secret is passed or printed). Only _pg calls it."""
    from wp import db
    return db.connect(db=dbname, role=config.PG_USER, application_name="disk_gate", connect_timeout=API_TIMEOUT_S)


def _pg_nonce(cid, real, q, ep):
    """B1: a fresh nonce written into the inspected container's data directory must be read back by the server at
    the experiment's endpoint; the file is removed again either way."""
    nonce = _new_id() + _new_id()
    path = real.rstrip("/") + "/" + NONCE
    try:
        w = _docker("exec", "-i", str(cid), "sh", "-c", 'umask 022; cat > "$1"  # disk_gate nonce', "disk_gate-nonce",
                    path, input=nonce.encode(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        w = None
    if w is None or w.returncode != 0:
        return ["the nonce could not be written into the PG container's data directory"]
    got, err = None, None
    try:
        got = str(q("SELECT pg_read_file(%s)", (NONCE,))[0][0])
    except Exception as e:                   # the driver's own error classes; only the class name is kept
        err = type(e).__name__
    finally:
        try:
            _docker("exec", str(cid), "rm", "-f", "--", path, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if err and ("Privilege" in err or "Permission" in err):
        return [f"the PG role the gate uses cannot read server files (pg_read_file: {err}), so the endpoint cannot be "
                "bound to the container: use the cluster admin, or grant it pg_read_server_files"]
    if got != nonce:
        return [f"the PG server at the experiment's endpoint {ep} did not read the nonce written into the inspected "
                f"container's data directory ({err or 'other content'}): it serves another data directory"]
    return []


def _pg_slow(cid):
    """B1, B3: the server at the experiment's endpoint (identifier, data_directory, pg_wal, tablespaces), those paths
    in the inspected container, and the nonce read. (evidence, reasons, isolated)."""
    ep = f"{config.PG_HOST}:{config.PG_PORT}"
    try:
        conn = _pg("postgres")
    except Exception as e:
        return None, [f"the PG endpoint {ep} the experiment uses could not be reached ({type(e).__name__})"], False
    try:
        def q(sql, params=()):
            return conn.execute(sql, params).fetchall()
        dd = str(q("SHOW data_directory")[0][0])
        sysid = str(q("SELECT system_identifier FROM pg_control_system()")[0][0])
        ts = [(str(n), str(loc)) for n, loc in q("SELECT spcname, pg_tablespace_location(oid) FROM pg_tablespace")
              if loc]
        named = ([("PG's data_directory", dd), ("PG's WAL directory", dd.rstrip("/") + "/pg_wal")] +
                 [(f"PG tablespace {n}", loc) for n, loc in ts])
        pr, why = _probe("PG", cid, DATA["pg"], [p for _, p in named])
        if pr is None:
            return None, why, False
        why, iso = _topology("PG", DATA["pg"], pr, named)
        ev = {"sysid": sysid, "tablespaces": ts, "fs": dict(pr["statfs"], thr=[0, 0, 0])}
        real = pr["paths"].get(dd, ("-", "-"))[1]
        if real != "-":
            why += _pg_nonce(cid, real, q, ep)
        return ev, why, iso
    except Exception as e:
        return None, [f"the PG endpoint {ep} could not be queried ({type(e).__name__})"], False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _slow(key, ref):
    """Everything except identity and lifetime counters, which _fast reads afterwards (B6): Docker's mount
    configuration, the store's own view through the experiment's endpoint, the in-container probe, ES's settings.
    (evidence, reasons); evidence["iso"] says whether the store's isolation held, so its figures can be compared."""
    K = NAMES[key]
    j = _inspect(ref)
    if j is None:
        return None, [f"the {K} container {str(ref)[:12]} cannot be inspected (removed or replaced)"]
    cid, data = j.get("Id"), DATA[key]
    ev, why = {"id": cid, "name": (j.get("Name") or "").lstrip("/")}, []
    mounts = j.get("Mounts") or []
    at = [m for m in mounts if m.get("Destination") == data]
    if not ((len(at) == 1 and at[0].get("Type") == "tmpfs") or
            (not at and data in ((j.get("HostConfig") or {}).get("Tmpfs") or {}))):
        kind = at[0].get("Type") if at else "the container's own layer"
        why.append(f"the {K} data directory {data} is not a Docker tmpfs mount ({kind}): its free space is shared "
                   "with other tenants")
    inside = sorted(m.get("Destination") for m in mounts if str(m.get("Destination", "")).startswith(data + "/"))
    if inside:
        why.append(f"a mount inside the data directory ({', '.join(inside)[:200]}) is outside the private tmpfs")
    iso = not why
    if key == "es":
        fs, w = _es_fs(j)
        why += w
        iso = iso and fs is not None and not w
        if fs:
            ev["fs"], ev["node"] = fs, fs["node"]
        ev["eph"], w = _es_bind(j)
        why += w
        ev["settings"], w = _es_settings()
        why += w
        pr, w = _probe("ES", cid, data, [fs["path"]] if fs else [])
        if pr is None:
            why += w
            iso = False
        else:
            w, ok = _topology("ES", data, pr, [("ES's data path", fs["path"])] if fs else [])
            why += w
            iso = iso and ok
    else:
        pe, w, ok = _pg_slow(cid)
        why += w
        iso = iso and ok
        if pe:
            ev.update(pe)
    ev["iso"] = iso
    return ev, why


def _fast(key, cid):
    """Identity and lifetime counters, read last and back to back inside the ledger step (B6)."""
    K = NAMES[key]
    j = _inspect(cid)
    if j is None:
        return None, [f"the {K} container {str(cid)[:12]} cannot be inspected (removed or replaced)"]
    s = j.get("State") or {}
    ev, why = {"id": j.get("Id"), "started": s.get("StartedAt")}, []
    if not ev["started"]:                       # m-4: a restart could not be seen without it
        why.append(f"Docker reports no StartedAt for the {K} container: a restart cannot be ruled out")
    if not s.get("Running") or s.get("Paused"):
        why.append(f"the {K} container is not running or is paused")
    if s.get("OOMKilled"):
        why.append(f"Docker reports an OOM kill in the {K} container")
    try:
        ev["cg"] = _cgroup(j)
    except GateError as e:
        why.append(f"{K}: {e}")
    return ev, why


def _bound(key, slow, fast):
    """R2, R3 and B9 for one store: the bound, its premise (used <= peak) and its conclusion (free >= threshold +
    margin). Only for a store whose isolation held: other figures would not be a private tmpfs's (F20)."""
    K, fs, cg = NAMES[key], (slow or {}).get("fs"), (fast or {}).get("cg")
    why = []
    if cg and cg["swap_cur"] != 0:
        why.append(f"the {K} container has {cg['swap_cur']} bytes in swap (memory.swap.current): tmpfs pages can be "
                   "outside the memory bound")
    if not (slow and slow.get("iso") and fs and cg):
        return why
    T = max(fs["thr"])
    bound = fs["total"] - T - MARGIN
    if not isinstance(cg["max"], int):
        why.append(f"the {K} container has no memory limit (memory.max {cg['max']}): its tmpfs use is unbounded")
    elif cg["swap"] != 0:
        why.append(f"the {K} container may swap (memory.swap.max {cg['swap']}): tmpfs pages can leave the memory bound")
    elif cg["max"] > bound:
        why.append(f"the {K} memory limit {cg['max'] / GIB:.2f} GiB can bring free space below "
                   f"{'the low watermark' if key == 'es' else 'the margin'}: tmpfs {fs['total'] / GIB:.2f} GiB, "
                   f"threshold {T / GIB:.2f} GiB, margin {MARGIN / GIB:.2f} GiB, bound {bound / GIB:.2f} GiB")
    if cg["peak"] > bound:
        why.append(f"the {K} container's lifetime memory peak {cg['peak'] / GIB:.2f} GiB exceeds the bound "
                   f"{bound / GIB:.2f} GiB: its tmpfs free space may have reached the threshold")
    if fs["free"] < T + MARGIN:          # the argument's conclusion, observed directly
        why.append(f"the {K} tmpfs has {fs['free'] / GIB:.2f} GiB free, under threshold {T / GIB:.2f} GiB + margin "
                   f"{MARGIN / GIB:.2f} GiB")
    used = fs["total"] - fs["free"]
    if used > cg["peak"]:                # the argument's premise: every tmpfs page is charged to this container
        why.append(f"the {K} tmpfs holds {used / GIB:.2f} GiB, more than the container's lifetime memory peak "
                   f"{cg['peak'] / GIB:.2f} GiB: pages in it are charged to another cgroup (written from outside the "
                   "container), so the memory limit does not bound it")
    return why


def _rules(slow, fast):
    return [w for key in ("es", "pg") for w in _bound(key, slow.get(key), fast.get(key))]


def _merge(slow, fast):
    return {k: (dict(slow.get(k) or {}, **(fast.get(k) or {})) if (slow.get(k) or fast.get(k)) else None)
            for k in ("es", "pg")}


def _drift(now, adm):
    """R4, R5 and B5: what differs from admission (Id, StartedAt, ES node and process, PG system identifier, OOM
    counts, limit, swap, size, thresholds, settings)."""
    why = []
    for key in ("es", "pg"):
        K, a, n = NAMES[key], adm[key], now.get(key)
        if not n:
            continue
        if n.get("id") != a["id"]:
            why.append(f"the {K} container was replaced ({a['id'][:12]} -> {str(n.get('id'))[:12]})")
        if n.get("started") != a.get("started"):
            why.append(f"the {K} container restarted: StartedAt {a['started']} -> {n['started']}")
        ac, nc = a.get("cg") or {}, n.get("cg") or {}
        if nc and ac:
            if nc["oom_kill"] != ac["oom_kill"]:
                why.append(f"an OOM kill in the {K} container during the run (oom_kill {ac['oom_kill']} -> "
                           f"{nc['oom_kill']})")
            if nc["oom"] != ac["oom"]:
                why.append(f"an allocation failed at the {K} container's memory limit during the run (memory.events "
                           f"oom {ac['oom']} -> {nc['oom']})")
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
        if key == "es" and n.get("eph") and a.get("eph") and n["eph"] != a["eph"]:
            why.append("the ES node process restarted during the run (its ephemeral id changed)")
        if key == "es":
            why += _settings_drift(a.get("settings"), n.get("settings"))
        if key == "pg" and n.get("sysid") and a.get("sysid") and n["sysid"] != a["sysid"]:
            why.append("the PG system identifier changed during the run: a new cluster in the data directory")
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


# --------------------------------------------------------------------------------------- what runs (P4, P5, P6)
def _open_regular(p):
    """m-B: a read descriptor of p and its stat, opened without following a link and without blocking (a FIFO or a
    socket never hangs or crashes the gate), or (None, None) when p is not a regular file or cannot be opened."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None, None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None, None
    return fd, st


def _hash_fd(fd, keep=0):
    """m-C: (SHA-256, the content when it is at most `keep` bytes, else None), read in 1 MiB blocks."""
    h, kept, n = hashlib.sha256(), [], 0
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        h.update(b)
        n += len(b)
        if n <= keep:
            kept.append(b)
    return h.hexdigest(), (b"".join(kept) if n <= keep else None)


def _sha256_file(p):
    """SHA-256 of the regular file p, or None (a link, a special file, unreadable)."""
    fd, _ = _open_regular(p)
    if fd is None:
        return None
    try:
        return _hash_fd(fd)[0]
    except OSError:
        return None
    finally:
        os.close(fd)


def _tree_files(root):
    """{relative path: (mode, SHA-256)} of every file and link under root, links not followed, as git records them;
    the bytecode caches found (names only); and every entry that is not a regular file, link or directory, or that
    cannot be read or listed (names only, never opened for reading: m-B)."""
    out, caches, special = {}, [], []

    def onerror(e):
        special.append(f"{os.path.relpath(getattr(e, 'filename', root) or root, root)} (cannot be listed)")
    for d, dirs, files in os.walk(root, onerror=onerror):
        dirs.sort()
        for n in [x for x in dirs if x == "__pycache__"]:
            caches.append(os.path.relpath(os.path.join(d, n), root))
        for n in sorted(files) + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root)
            if n.endswith((".pyc", ".pyo")):
                caches.append(rel)
                continue
            try:
                st = os.lstat(p)
            except OSError:
                special.append(f"{rel} (cannot be examined)")
                continue
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("120000", hashlib.sha256(os.readlink(p).encode("utf-8", "surrogateescape")).hexdigest())
                continue
            h = _sha256_file(p) if stat.S_ISREG(st.st_mode) else None
            if h is None:
                special.append(rel)
            else:
                out[rel] = ("100755" if st.st_mode & 0o100 else "100644", h)
    return out, caches, special


def _manifest():
    """(commit, manifest SHA-256, {path: (mode, sha256)}) from the tree's TREE.manifest; GateError if malformed."""
    try:
        raw = (ROOT / MANIFEST).read_bytes()
    except OSError:
        raise GateError(f"the tree {ROOT.name} has no readable {MANIFEST} (not a tree pinned by sync.sh)") from None
    lines = raw.decode("utf-8", "surrogateescape").split("\n")
    m = re.fullmatch(r"commit ([0-9a-f]{40})", lines[1]) if len(lines) > 2 and lines[0] == "tree-manifest v1" else None
    if not m or lines[-1] != "":
        raise GateError(f"{MANIFEST} of {ROOT.name} is not a tree-manifest v1")
    ent = {}
    for ln in lines[2:-1]:
        x = re.fullmatch(r"([0-9a-f]{64}) (100644|100755|120000) (.+)", ln)
        if not x or x.group(3) in ent:
            raise GateError(f"{MANIFEST} of {ROOT.name} has a malformed or duplicate entry")
        ent[x.group(3)] = (x.group(2), x.group(1))
    return m.group(1), hashlib.sha256(raw).hexdigest(), ent


def _tree():
    """P4, P5: ({commit, manifest}, reasons). The tree must be a direct child of WP_HOME/runs whose files equal the
    manifest's (from the commit's git objects), whose GIT_HEAD names that commit without -dirty, and whose manifest
    names this gate's own hash; bytecode writing is off and every loaded exp/wp module comes from this tree."""
    why = []
    try:
        runs = (HOME / "runs").resolve()
    except OSError:
        runs = None
    if ROOT.parent != runs:
        why.append(f"the gate runs from {ROOT}, not from a pinned tree directly under WP_HOME/runs")
    try:
        commit, msha, ent = _manifest()
    except GateError as e:
        return {}, why + [str(e)]
    try:
        head = (ROOT / GIT_HEAD).read_text().strip()
    except OSError:
        head = ""
    if "dirty" in head:
        why.append(f"the tree's GIT_HEAD {head[:40]!r} is dirty: it was pinned from a working tree with changes")
    elif len(head) < 7 or not commit.startswith(head):
        why.append(f"the tree's GIT_HEAD {head[:40]!r} does not name the manifest's commit {commit[:12]}")
    if (ent.get("exp/disk_gate.py") or ("", ""))[1] != GATE_SHA256:
        why.append(f"the running gate (sha256 {GATE_SHA256[:16]}) is not the gate of commit {commit[:12]}")
    have, caches, special = _tree_files(ROOT)
    for c in sorted(caches)[:5]:
        why.append(f"a bytecode cache in the pinned tree: {c} (__pycache__ or .pyc; the tree runs with -B only)")
    for x in sorted(special)[:5]:
        why.append(f"a special or unreadable entry in the pinned tree: {ascii(x)[:200]} (not a regular file, link or "
                   "directory; never opened)")
    for rel in sorted(set(have) - set(ent) - {MANIFEST, GIT_HEAD})[:10]:
        why.append(f"an untracked file in the pinned tree: {rel} (not a file of commit {commit[:12]})")
    for rel in sorted(set(ent) - set(have))[:10]:
        why.append(f"{rel} of commit {commit[:12]} is missing from the tree")
    for rel in sorted(r for r in set(ent) & set(have) if ent[r] != have[r])[:10]:
        why.append(f"{rel} differs from commit {commit[:12]} (content or exec bit)")
    if not sys.dont_write_bytecode:
        why.append("the gate runs with bytecode writing on (python -B is required, so no cache enters the tree)")
    if not _isolated():
        why.append("the gate's interpreter is not isolated (python -I is required: PYTHON* variables, the user site "
                   "and the working directory must not shape what runs; start it through runkit/isolated.py)")
    for name, mod in sorted(sys.modules.items()):
        if name in ("wp", "exp") or name.startswith(("wp.", "exp.")):
            f = getattr(mod, "__file__", None)
            where = [f] if f else list(getattr(mod, "__path__", None) or [])
            if not where or any(not str(pathlib.Path(x).resolve()).startswith(str(ROOT) + os.sep) for x in where):
                why.append(f"module {name} was loaded from outside the pinned tree ({where[:1]})")
    return {"commit": commit, "manifest": msha}, why


def _fixtures():
    """P6: every non-.py file under FIXTURE_DIRS and each FIXTURE_FILES entry is listed in FIXTURES with its SHA-256,
    and every listed file exists. Reasons."""
    try:
        reg = {}
        for ln in (ROOT / FIXTURES).read_text().splitlines():
            if ln.strip() and not ln.startswith("#"):
                x = re.fullmatch(r"([0-9a-f]{64})  (\S+)", ln)
                if not x:
                    raise ValueError(ln[:60])
                reg[x.group(2)] = x.group(1)
    except (OSError, ValueError) as e:
        return [f"the fixture registry {FIXTURES} cannot be read ({type(e).__name__})"]
    have = {}
    for top in FIXTURE_DIRS:
        for p in sorted((ROOT / top).rglob("*")) if (ROOT / top).is_dir() else []:
            if (p.is_file() or p.is_symlink()) and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts:
                have[str(p.relative_to(ROOT))] = p
    for n in FIXTURE_FILES:
        if (ROOT / n).exists():
            have[n] = ROOT / n
    why = [f"fixture {rel} is not registered in {FIXTURES}" for rel in sorted(set(have) - set(reg))]
    why += [f"registered fixture {rel} is missing from the tree" for rel in sorted(set(reg) - set(have))]
    for rel in sorted(set(have) & set(reg)):
        if have[rel].is_symlink() or _sha256_file(have[rel]) != reg[rel]:
            why.append(f"fixture {rel} does not match its registered SHA-256 in {FIXTURES}")
    return why


def _isolated():
    """m-G: the interpreter runs with -I (sys.flags.isolated)."""
    return bool(sys.flags.isolated)


# ------------------------------------------------------------------------------------------------- clean stores (P7)
def _pg(db):
    """A gate connection to database db of the experiment's PG endpoint whose first statement bounds every later one
    (m-6)."""
    conn = _pg_connect(db)
    try:
        conn.execute(f"SET statement_timeout = '{PG_STATEMENT_TIMEOUT}'")
    except BaseException:
        conn.close()
        raise
    return conn


def _unregistered(ev, ks):
    """The erasure gate's store inventory (wp.registry): everything that can hold content and is not registered."""
    from wp import registry
    from wp.es import ES
    return registry.unregistered(ev, ks, ES())


TABLES_SQL = ("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
              "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1")


def _clean():
    """P7: reasons the stores are not clean before the run."""
    why, conns = [], []
    try:
        for db in (config.EVENTS_DB, config.KEYS_DB):
            conns.append(_pg(db))
            full = []
            for (t,) in conns[-1].execute(TABLES_SQL).fetchall():
                t = str(t)
                q = 'SELECT exists(SELECT 1 FROM public."' + t.replace('"', '""') + '")'
                if t != "store_registry" and conns[-1].execute(q).fetchall()[0][0]:
                    full.append(t)
            if full:
                why.append(f"stores not clean before the run: tables of {db} holding rows: {', '.join(full)[:200]}")
        unreg = _unregistered(*conns)
        if unreg:
            why.append(f"stores not clean before the run: {len(unreg)} unregistered: {', '.join(map(str, unreg[:10]))[:200]}")
    except Exception as e:                        # the driver's own error classes; only the class name is kept
        why.append(f"the stores could not be checked clean before the run ({type(e).__name__})")
    finally:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
    code, body = _es_get(f"/_cat/indices/{config.INDEX_PREFIX}*?format=json&expand_wildcards=all&h=index")
    if code != 200 or not isinstance(body, list):
        why.append(f"the stores could not be checked clean before the run (ES indices {code})")
    elif body:
        names = sorted(str((x or {}).get("index")) for x in body)
        why.append(f"stores not clean before the run: ES indices under {config.INDEX_PREFIX}: {', '.join(names)[:200]}")
    logs = [p for p in config.LOG_DIR.rglob("*") if p.is_file()] if config.LOG_DIR.is_dir() else []
    if logs:
        why.append(f"stores not clean before the run: {len(logs)} file(s) in the log store {config.LOG_DIR.name}/")
    return why


# ------------------------------------------------------------------------------- the live catalogs (F-1, F-3, v6)
# Read-only queries, each tagged so a reader of the server log (and the offline fake) can tell them apart. Values are
# compared as text. The cluster part is read from database postgres; the schema part from each stream database.
CLUSTER_Q = {
    "roles": "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, "
             "rolbypassrls FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1",
    "members": "SELECT r.rolname, m.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid "
               "JOIN pg_roles m ON m.oid = a.member WHERE m.rolname !~ '^pg_' ORDER BY 1, 2",
    "settings": "SELECT coalesce(r.rolname, ''), coalesce(d.datname, ''), array_to_string(ARRAY(SELECT x FROM "
                "unnest(s.setconfig) AS x ORDER BY 1), ',') FROM pg_db_role_setting s LEFT JOIN pg_roles r "
                "ON r.oid = s.setrole LEFT JOIN pg_database d ON d.oid = s.setdatabase ORDER BY 1, 2",
    "databases": "SELECT d.datname, d.oid::bigint, d.datallowconn, pg_get_userbyid(d.datdba), array_to_string(ARRAY("
                 "SELECT (CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END) || ':' || "
                 "a.privilege_type FROM aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) AS a ORDER BY 1), "
                 "',') FROM pg_database d ORDER BY 1",
    "parameter_acl": "SELECT parname, coalesce(paracl::text, '') FROM pg_parameter_acl ORDER BY 1",
    "auto_conf": "SELECT name, coalesce(setting, ''), applied FROM pg_file_settings "
                 "WHERE sourcefile LIKE '%postgresql.auto.conf' ORDER BY 1",
}
_PUBLIC = "n.nspname = 'public'"
DB_Q = {
    "schema": "SELECT n.nspname, n.oid::bigint, pg_get_userbyid(n.nspowner), coalesce(n.nspacl::text, '') "
              f"FROM pg_namespace n WHERE {_PUBLIC}",
    "relations": "SELECT c.relname, c.oid::bigint, c.relkind::text, pg_get_userbyid(c.relowner), "
                 "coalesce(c.relacl::text, ''), c.relrowsecurity, c.relforcerowsecurity FROM pg_class c JOIN "
                 f"pg_namespace n ON n.oid = c.relnamespace WHERE {_PUBLIC} AND c.relkind IN ('r', 'p', 'v', 'm', 'S', "
                 "'f') ORDER BY 1",
    "column_acl": "SELECT c.relname, a.attname, a.attacl::text FROM pg_attribute a JOIN pg_class c ON c.oid = "
                  f"a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_PUBLIC} AND a.attacl IS NOT NULL "
                  "ORDER BY 1, 2",
    "triggers": "SELECT c.relname, t.tgname, t.tgtype::int, t.tgenabled::text, p.proname FROM pg_trigger t JOIN "
                "pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_proc p ON "
                f"p.oid = t.tgfoid WHERE {_PUBLIC} AND NOT t.tgisinternal ORDER BY 1, 2",
    "functions": "SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_userbyid(p.proowner), "
                 "p.prosecdef, coalesce(p.proacl::text, ''), md5(coalesce(p.prosrc, '')), "
                 "coalesce(array_to_string(p.proconfig, ','), '') FROM pg_proc p JOIN pg_namespace n ON n.oid = "
                 f"p.pronamespace WHERE {_PUBLIC} ORDER BY 1, 2",
    "constraints": "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid) FROM pg_constraint k JOIN pg_class c ON "
                   f"c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_PUBLIC} ORDER BY 1, 2",
    "indexes": "SELECT i.relname, x.indisunique, x.indisvalid, pg_get_indexdef(x.indexrelid) FROM pg_index x JOIN "
               f"pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = i.relnamespace WHERE {_PUBLIC} "
               "ORDER BY 1",
    "policies": "SELECT tablename::text, policyname::text, cmd, roles::text, coalesce(qual, ''), "
                "coalesce(with_check, '') FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2",
    "default_acl": "SELECT pg_get_userbyid(d.defaclrole), coalesce(n.nspname, ''), d.defaclobjtype::text, "
                   "d.defaclacl::text FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
                   "ORDER BY 1, 2, 3",
}
EVENTS_Q = {
    "app_privs": "SELECT c.relname, p.priv FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace CROSS JOIN "
                 "unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS p(priv) "
                 f"WHERE {_PUBLIC} AND c.relkind IN ('r', 'p') AND has_table_privilege('wp_app', c.oid, p.priv) "
                 "ORDER BY 1, 2",
    "app_column_update": "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE "
                         f"{_PUBLIC} AND c.relkind IN ('r', 'p') AND has_any_column_privilege('wp_app', c.oid, "
                         "'UPDATE') ORDER BY 1",
    "app_execute": "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE "
                   f"{_PUBLIC} AND has_function_privilege('wp_app', p.oid, 'EXECUTE') ORDER BY 1",
}
KEYS_Q = {
    "verifier_key_read": "SELECT has_column_privilege('wp_verifier', 'public.subject_keys', 'key_hex', 'SELECT')",
}


def _catalog(full):
    """F-1, F-3: (snapshot, reasons): {section: {tag: rows as lists of text}}; section "cluster" always, and with
    full the events and keys databases. Read-only; every statement under the gate's statement_timeout."""
    plan = [("cluster", "postgres", CLUSTER_Q)]
    if full:
        plan += [(config.EVENTS_DB, config.EVENTS_DB, dict(DB_Q, **EVENTS_Q)),
                 (config.KEYS_DB, config.KEYS_DB, dict(DB_Q, **KEYS_Q))]
    snap, conns = {}, []
    try:
        for section, db, qs in plan:
            conns.append(_pg(db))
            snap[section] = {tag: [[str(v) for v in row] for row in
                                   conns[-1].execute(f"/* gate:{tag} */ {sql}").fetchall()] for tag, sql in qs.items()}
    except Exception as e:                        # the driver's own error classes; only the class name is kept
        return None, [f"the live catalogs could not be read ({type(e).__name__})"]
    finally:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
    return snap, []


def _catalog_rules(snap, phase):
    """F-3: reasons the live catalogs differ from the committed expectation (exp/guard_policy.py, bound to
    wp/setup.py and the schema files). phase "before" (before the reset: the cluster part only, where a missing role
    or database is allowed, since setup creates it, but every weakening counts, because setup would repair role
    attributes and settings without a trace), "after" (after the reset) or "end"."""
    why, cl, strict = [], snap["cluster"], phase != "before"
    admin, login = config.PG_USER, set(GP.LOGIN_ROLES)
    expected = login | {GP.OWNER_ROLE}
    roles = {r[0]: r for r in cl["roles"]}
    for n in sorted(set(roles) - expected - {admin}):
        why.append(f"a role outside the committed set: {n}")
    for n in sorted(expected - set(roles)) if strict else ():
        why.append(f"committed role {n} is missing")
    cols = ("rolsuper", "rolinherit", "rolcreaterole", "rolcreatedb", "rolcanlogin", "rolreplication", "rolbypassrls")
    for n in sorted(expected & set(roles)):
        got = dict(zip(cols, roles[n][1:]))
        want = dict(GP.ROLE_FLAGS, rolcanlogin=n in login)
        bad = [f"{k}={got.get(k)}" for k in cols if str(want[k]) != got.get(k)]
        if bad:
            why.append(f"role {n} has {', '.join(bad)} (not its committed attributes)")
    members = {(r[0], r[1]) for r in cl["members"] if r[1] != admin}
    for g, m in sorted(members - GP.MEMBERSHIPS):
        why.append(f"role {m} is a member of {g} (not a committed membership)")
    for g, m in sorted(GP.MEMBERSHIPS - members) if strict else ():
        why.append(f"committed membership of {m} in {g} is missing")
    want = ",".join(sorted(f"{k}={v}" for k, v in GP.ROLE_SETTINGS.items()))
    have = {(r[0], r[1]): r[2] for r in cl["settings"]}
    for (r, d), conf in sorted(have.items()):
        if d or r not in login:
            why.append(f"a setting of role {r or '(all)'} in database {d or '(all)'} that is not committed: {conf[:120]}")
        elif conf != want:
            why.append(f"role {r} has settings {conf[:160]} (committed: {want})")
    for r in sorted(login & set(roles)):
        if (r, "") not in have:
            why.append(f"role {r} has none of its committed settings ({want})")
    dbs = {r[0]: r for r in cl["databases"]}
    stream = {"events": config.EVENTS_DB, "keys": config.KEYS_DB}
    for n in sorted(set(dbs) - set(GP.SYSTEM_DATABASES) - set(stream.values())):
        why.append(f"a database outside the committed set: {n}")
    for k, n in sorted(stream.items()):
        if n not in dbs:
            if strict:
                why.append(f"the {k} database {n} is missing")
            continue
        owner, acl = dbs[n][3], [x.rsplit(":", 1) for x in dbs[n][4].split(",") if ":" in x]
        conn = {g for g, p in acl if p in ("CONNECT", "CREATE")}
        for g in sorted(conn - set(GP.CONNECT[k]) - {owner, admin}):
            why.append(f"{g} may connect to or create in the {k} database {n} (not committed)")
        for g in sorted(set(GP.CONNECT[k]) - conn) if strict else ():
            why.append(f"committed CONNECT of {g} on the {k} database {n} is missing")
    for r in cl["parameter_acl"]:
        why.append(f"a parameter privilege that is not committed: {r[0]} {r[1][:120]}")
    for r in cl["auto_conf"]:
        why.append(f"an ALTER SYSTEM setting that is not committed: {r[0]} = {r[1][:80]}")
    if phase == "before":
        return why
    ev, ks = snap[config.EVENTS_DB], snap[config.KEYS_DB]
    tables = {r[0]: r for r in ev["relations"] if r[2] in ("r", "p")}
    for t in sorted(tables):
        if tables[t][3] != GP.EVENTS_OWNER or t not in GP.POLICY:
            why.append(f"G1 table {t}: owner {tables[t][3]}, committed policy {'yes' if t in GP.POLICY else 'none'}")
    for t in sorted(set(GP.POLICY) - set(tables)):
        why.append(f"G1 committed table {t} is missing")
    privs = {}
    for t, p in ev["app_privs"]:
        privs.setdefault(t, set()).add(p)
    for t in sorted(set(tables) & set(GP.POLICY)):
        if privs.get(t, set()) != GP.POLICY[t]:
            why.append(f"G2 wp_app on {t}: {sorted(privs.get(t, set()))} (committed {sorted(GP.POLICY[t])})")
    for (t,) in ev["app_column_update"]:
        if t in GP.POLICY and "UPDATE" not in GP.POLICY[t] and t not in GP.COLUMN_UPDATE_OK:
            why.append(f"G2 wp_app may UPDATE columns of {t}")
    trg = {}
    for t, name, typ, en, _fn in ev["triggers"]:
        trg.setdefault(t, []).append((name, int(typ), en))
        if en != "A":
            why.append(f"G3 trigger {t}.{name} is enabled {en!r}, not ALWAYS")
    for t in sorted(tables):
        cov = set()
        for _n, typ, _e in trg.get(t, []):
            if typ & GP.BEFORE and typ & GP.ROW:
                cov |= {k for k, b in (("INSERT", GP.INS), ("UPDATE", GP.UPD), ("DELETE", GP.DEL)) if typ & b}
            if typ & GP.BEFORE and typ & GP.TRUNC:
                cov.add("TRUNCATE")
        miss = [k for k in ("INSERT", "UPDATE", "DELETE", "TRUNCATE") if k not in cov]
        if miss:
            why.append(f"G3 table {t} has no guard trigger for {','.join(miss)}")
    for (f,) in ev["app_execute"]:
        if f in GP.APP_NO_EXECUTE:
            why.append(f"G4 wp_app may execute {f}")
    if any(r and r[0] == "True" for r in ks["verifier_key_read"]):
        why.append("G4 wp_verifier may read key material (subject_keys.key_hex)")
    for r in ks["relations"]:
        if r[2] in ("r", "p") and r[3] != GP.KEYS_OWNER:
            why.append(f"G5 keys table {r[0]}: owner {r[3]}")
    idx = {r[0]: r for r in ev["indexes"]}
    for ix in GP.UNIQUE_INDEXES:
        if ix not in idx or idx[ix][1] != "True" or idx[ix][2] != "True":
            why.append(f"G7 unique index {ix} is missing or not valid")
    return why


def _catalog_diff(adm, now):
    """F-1, F-3: reasons the catalogs at end differ from admission's: a store reset or replaced (a database or the
    public schema with another identity), or any guard, grant, role, membership, setting or object changed."""
    why = []
    for db in (config.EVENTS_DB, config.KEYS_DB):
        a = {r[0]: r[1] for r in adm["cluster"]["databases"]}.get(db)
        n = {r[0]: r[1] for r in now["cluster"]["databases"]}.get(db)
        sa, sn = [r[1] for r in adm[db]["schema"]], [r[1] for r in now[db]["schema"]]
        if a != n or sa != sn:
            why.append(f"the {db} database or its public schema was reset or replaced during the run (identity "
                       f"{a}/{sa[:1]} -> {n}/{sn[:1]})")
    for section in sorted(set(adm) | set(now)):
        for tag in sorted(set(adm.get(section, {})) | set(now.get(section, {}))):
            ra = {json.dumps(r) for r in adm.get(section, {}).get(tag, [])}
            rn = {json.dumps(r) for r in now.get(section, {}).get(tag, [])}
            if ra != rn:
                ex = sorted(rn - ra)[:1] or sorted(ra - rn)[:1]
                why.append(f"catalog {section}.{tag} changed during the run: {len(rn - ra)} row(s) new, {len(ra - rn)} "
                           f"gone (e.g. {ex[0][:160] if ex else ''})")
    return why


# ------------------------------------------------------------------------- the experiment's process and memory (F-2)
def _own_oom():
    """F-2: the oom_kill count of this gate's own cgroup v2 (memory.events, which counts processes of the cgroup and
    of every cgroup below it killed by any OOM killer): the experiment and every child it starts are in it unless they
    leave it. None when it cannot be read."""
    try:
        lines = (PROC_ROOT / "self" / "cgroup").read_text().splitlines()
        path = next(x[3:] for x in lines if x.startswith("0::"))
        ev = dict(x.split() for x in (CGROUP_ROOT / path.lstrip("/") / "memory.events").read_text().splitlines())
        return int(ev["oom_kill"])
    except (OSError, StopIteration, ValueError, KeyError):
        return None


def _home_users(stores):
    """F-4 (C12): (names of the containers other than the run's two stores, `stores`, with a bind mount whose source
    is WP_HOME or below it: they use this home's credentials or files; reasons when the list cannot be read). A
    container removed between the list and the inspection is retried."""
    home = os.path.realpath(HOME)
    for _ in range(3):
        try:
            r = _docker("ps", "-aq", "--no-trunc", timeout=60)
            if r.returncode != 0:
                return None, ["the container list (docker ps) could not be read"]
            ids = r.stdout.decode("ascii", "replace").split()
            if not ids:
                return [], []
            r = _docker("inspect", "--type", "container", *ids, timeout=120)
            js = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            js = None
        if isinstance(js, list) and len(js) == len(ids):
            break
    else:
        return None, ["the containers on this daemon could not be inspected (docker inspect failed three times)"]
    out = []
    for j in js:
        if not isinstance(j, dict) or j.get("Id") in stores:
            continue
        for m in j.get("Mounts") or []:
            src = str(m.get("Source") or "")
            if m.get("Type") == "bind" and (src == home or src.startswith(home.rstrip("/") + "/")):
                out.append(str(j.get("Name") or j.get("Id"))[:80].lstrip("/"))
                break
    return sorted(out), []


def _exp_env():
    """m-G: the environment every process the gate starts gets: the listed names and the WP_* names of the gate's
    own environment, without the variables only the gate sets."""
    return {k: v for k, v in os.environ.items()
            if (k in EXP_ENV_NAMES or k.startswith(EXP_ENV_PREFIXES)) and k not in GATE_ENV}


def _isolated_cmd(module, *args):
    return [sys.executable, "-I", "-B", str(ROOT / ISOLATED), module, *args]


def _run_tools(steps, lock, tag):
    """F-1: run each (name, module, args) of the pinned tree in an isolated interpreter, given this gate's run lock
    descriptor (WP_RUN_LOCK_FD: wp/runlock.py accepts only a descriptor that holds the lock), its output in a new file
    state/tools/<utc>-<pid>-<tag>.<n>.<name>.out. Stops at the first failure. Reasons."""
    d = STATE_DIR / "tools"
    d.mkdir(parents=True, exist_ok=True)
    env = dict(_exp_env(), WP_RUN_LOCK_FD=str(lock.fileno()))
    for i, (name, module, args) in enumerate(steps):
        p = d / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}-{tag}.{i}.{name}.out"
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as e:
            return [f"the output file of {name} could not be created ({type(e).__name__})"]
        try:
            rc = subprocess.run(_isolated_cmd(module, *args), cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                stdout=fd, stderr=fd, pass_fds=(lock.fileno(),), timeout=TOOL_TIMEOUT_S).returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            rc = type(e).__name__
        finally:
            os.close(fd)
        if rc != 0:
            return [f"{name} ({module} {' '.join(args)}) failed under the run lock ({rc}); output: state/tools/{p.name}"]
    return []


# --------------------------------------------------------------------------------------------- the run (P1 to P11)
def _take_run_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(RUN_LOCK, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _admit(label, pgid):
    """Admission (P1, P4 to P7 and every v4 rule): the run dict, holding the run lock; Blocked otherwise, with the
    lock released and nothing recorded."""
    info, why = _tree()
    why += _fixtures()
    if why:
        raise Blocked(why)
    lock = _take_run_lock()
    if lock is None:
        raise Blocked(["another run's launcher or experiment is alive (it holds the run lock state/run.lock)"])
    try:
        try:
            with Ledger() as L:
                b = _admission_blocker(L)
        except LedgerError as e:
            b = f"the stop ledger is not intact ({e})"
        if b:
            raise Blocked([b])
        oom = _own_oom()                            # F-2: the experiment's memory must be observable
        if oom is None:
            raise Blocked(["the experiment's memory cannot be observed: this gate's cgroup v2 memory.events "
                           "(oom_kill) cannot be read"])
        snap0, w = _catalog(False)                  # F-3: before the reset, which repairs role attributes silently
        if snap0 is not None:
            w = [f"before the store reset: {x}" for x in _catalog_rules(snap0, "before")]
        if w:
            raise Blocked(w)
        w = _run_tools(RESET_STEPS, lock, "admission")   # F-1: reset and cleanup only under this run's lock
        if w:
            raise Blocked(w)
        names = dict(zip(("es", "pg"), _targets()))
        try:
            base = _home_bytes()                    # slow steps first (B6)
        except GateError as e:
            raise Blocked([str(e)]) from None
        why = _clean()
        snap, w = _catalog(True)                    # F-3: the committed expectation after the reset
        why += w if snap is None else _catalog_rules(snap, "after")
        slow = {}
        for key in ("es", "pg"):
            slow[key], w = _slow(key, names[key])
            why += w
        users, w = _home_users({(slow.get(k) or {}).get("id") for k in ("es", "pg")} - {None})
        why += w + [f"a container other than the run's stores uses this home (a bind mount under WP_HOME): {u}"
                    for u in users or []]
        if why:
            raise Blocked(why)
        env = _exp_env()
        run_id = "r-" + os.urandom(16).hex()
        fast, adm = {}, None
        try:
            with Ledger() as L:                     # stops, legacy path, identity, host free and admit: one step
                b = _admission_blocker(L)
                w = [b] if b else []
                if not b:
                    t = _utc()
                    for key in ("es", "pg"):
                        fast[key], x = _fast(key, slow[key]["id"])
                        w += x
                    w += _rules(slow, fast)
                f = free_gb()
                if not w and f < START_GB:
                    w.append(f"host free {f:.1f} GiB < {START_GB} (measured at admission)")
                if not w:
                    adm = L.append("admit", v=FORMAT, gate=VERSION, gate_sha256=GATE_SHA256, run=run_id, label=label,
                                   pid=os.getpid(), pgid=pgid, commit=info["commit"], manifest=info["manifest"],
                                   es=slow["es"]["id"], pg=slow["pg"]["id"], free_bytes=int(f * GIB), free_at=_utc(),
                                   window_start=t, oom_kill=oom, python=sys.version.split()[0],
                                   env={k: hashlib.sha256(v.encode("utf-8", "surrogateescape")).hexdigest()[:16]
                                        for k, v in sorted(env.items())},
                                   catalog=hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest())
        except LedgerError as e:
            w = [f"the stop ledger is not intact ({e})"]
        if w:
            raise Blocked(w)
        d = RUNS_DIR / run_id
        d.parent.mkdir(parents=True, exist_ok=True)
        d.mkdir(mode=0o700)                         # P10: never an existing directory
        return {"run": run_id, "seq": adm["seq"], "label": label, "obs": _merge(slow, fast), "base_home": base,
                "free": f, "lock": lock, "dir": d, "info": info, "window_start": t, "oom": oom, "catalog": snap,
                "env": env}
    except BaseException:
        lock.close()
        raise


def _run_files(run):
    """P8: ([{name, sha256}] of every file in the run directory, reasons). Every JSON row must carry this run id and
    a time `at` inside the run window (from the admit record's window_start to now, to the second)."""
    files, why = [], []
    d, now = str(run["dir"]), _utc()

    def onerror(e):
        why.append(f"a directory in the run directory cannot be listed ({type(e).__name__})")
    for top, dirs, names in os.walk(d, onerror=onerror):
        dirs.sort()
        for n in sorted(names) + sorted(x for x in dirs if os.path.islink(os.path.join(top, x))):
            p = os.path.join(top, n)
            rel = os.path.relpath(p, d)
            fd, _ = _open_regular(p)                # m-B: a link, FIFO, socket or device is a reason, never opened
            if fd is None:
                why.append(f"a link, special or unreadable file in the run directory: {ascii(rel)[:200]}")
                continue
            try:
                digest, data = _hash_fd(fd, keep=ROW_CAP if rel.endswith(".json") else 0)   # m-C: streamed
            except OSError as e:
                why.append(f"the run file {ascii(rel)[:200]} could not be read ({type(e).__name__})")
                continue
            finally:
                os.close(fd)
            files.append({"name": rel, "sha256": digest})
            if not rel.endswith(".json"):
                continue
            if data is None:
                why.append(f"the result row {rel} is larger than {ROW_CAP // 1024 ** 2} MiB: it is not read as a row")
                continue
            try:
                doc = json.loads(data)
                got, at = doc.get("gate_run"), doc.get("at")
            except (ValueError, AttributeError):
                got = at = "(unreadable)"
            if got != run["run"]:
                why.append(f"the result row {rel} carries run {str(got)[:40]!r}: a row of another run (or of none)")
            if not (isinstance(at, str) and UTC_RX.match(at) and run["window_start"] <= at <= now):
                why.append(f"the result row {rel} has time {str(at)[:40]!r}, outside the run window "
                           f"{run['window_start']} to {now}")
    return files, why


def _ledger_why(L, run, when):
    why = []
    if not L.admitted(run["run"], run["seq"]):
        why.append(f"this run's admission record (run {run['run']}, seq {run['seq']}) is not in the ledger: "
                   "truncated, reset or replaced")
    why += [f"STOP: {_stop_text(s)}, {when}" for s in L.stops_after(run["seq"])]
    return why


def _finish(run, extra=()):
    """The end of the run (B6): WP_HOME's size, the tree, the fixtures, the run's files and the slow evidence first;
    then, in one locked ledger step, the ledger, the legacy path, both containers' identity and lifetime counters back
    to back (window_end), the rules, the drift from the baseline in this process's memory and from the admit record,
    and the end record. Releases the run lock. (obs, reasons, host free GiB, window_end)."""
    when = "by the end of the run"
    why = list(extra)
    try:
        try:
            d = (_home_bytes() - run["base_home"]) / GIB
            why += [f"WP_HOME grew {d:.2f} GiB > cap {CAP_GB} {when}"] if d > CAP_GB else []
        except GateError as e:
            why.append(f"{e} {when}")
        info, tw = _tree()
        why += [f"the pinned tree changed during the run: {w}" for w in tw]
        if info and info != run["info"]:
            why.append("the pinned tree changed during the run: its manifest is not the one admitted")
        why += [f"the fixtures changed during the run: {w}" for w in _fixtures()]
        files, w = _run_files(run)
        why += w
        oom = _own_oom()                            # F-2: an OOM kill of the experiment or of anything it started
        if oom is None:
            why.append(f"this gate's cgroup memory.events (oom_kill) cannot be read {when}")
        elif oom != run["oom"]:
            why.append(f"an OOM kill in the gate's cgroup during the run (memory.events oom_kill {run['oom']} -> {oom}): "
                       "the experiment or a process it started was killed for memory")
        snap, w = _catalog(True)                    # F-1, F-3: the expectation, and admission's catalogs, still hold
        why += w if snap is None else _catalog_rules(snap, "end") + _catalog_diff(run["catalog"], snap)
        slow = {}
        for key in ("es", "pg"):
            slow[key], w = _slow(key, run["obs"][key]["id"])
            why += w
        users, w = _home_users({run["obs"][k]["id"] for k in ("es", "pg")})
        why += w + [f"a container other than the run's stores uses this home (a bind mount under WP_HOME) {when}: {u}"
                    for u in users or []]
        f, obs, window_end = free_gb(), {}, None
        if f < STOP_GB:
            why.append(f"host free {f:.1f} GiB < {STOP_GB} {when} (amendment GF1)")
        try:
            with Ledger() as L:
                closed = L.closed(run["run"])
                if closed:
                    why.append(f"run {run['run']} was closed before its end: a second end record, VOID, is written "
                               "(the verdict of a run with two end records is VOID)")
                why += _ledger_why(L, run, when)
                adm = next((r for r in L.recs if r["kind"] == "admit" and r.get("run") == run["run"]), None)
                if adm and (adm.get("es"), adm.get("pg")) != (run["obs"]["es"]["id"], run["obs"]["pg"]["id"]):
                    why.append("the admit record names other containers than the run's baseline")
                lg = _legacy_why()
                if lg:
                    why.append(f"{lg}, {when}")
                window_end, fast = _utc(), {}
                for key in ("es", "pg"):
                    fast[key], w = _fast(key, run["obs"][key]["id"])
                    why += w
                why += _rules(slow, fast)
                obs = _merge(slow, fast)
                why += _drift(obs, run["obs"])
                if not lg and _legacy_why():
                    why.append(f"{_legacy_why()}, {when}")
                _end_record(L, run["run"], "VOID" if why else "VALID", why, window_end=window_end, label=run["label"],
                            free_bytes=int(f * GIB), evidence=_summary(obs), files=files, exp_rc=run.get("exp_rc"),
                            commit=run["info"]["commit"], oom_kill=oom)
        except LedgerError as e:
            why.append(f"the stop ledger is not intact ({e})")
        # F-1: the stream's indices are deleted before the lock is released; a failure is reported, the verdict stays
        # (the next admission resets the stores under its own lock)
        for w in _run_tools(AFTER_STEPS, run["lock"], "after"):
            print(f"disk_gate after the end record: {w}")
        return obs, why, f, window_end
    finally:
        run["lock"].close()


def _report(run, obs, why, f, window_end):
    print(f"disk_gate end free {f:.1f} GiB {_summary(obs)}")
    for x in why:
        print(f"disk_gate VOID: {x}")
    _verdict_line(run["run"], "VOID" if why else "VALID", window_end)
    return 3 if why else 0


def _group_members(pgid):
    """Live processes of process group pgid other than this one (P11), listed by ps in its own session so that it is
    not one of them. None when they cannot be listed."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid="], capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        rows = [ln.split() for ln in r.stdout.decode("ascii", "replace").splitlines()]
        if r.returncode != 0:
            return None
        return sorted(int(a) for a, b in (x for x in rows if len(x) == 2 and x[0].isdigit() and x[1].isdigit())
                      if int(b) == pgid and int(a) != os.getpid())
    except (OSError, subprocess.TimeoutExpired):
        return None


def _own_session():
    """m-I: this process leads its process group (an interactive shell's job, setsid(1), a wrapper's exec), so it
    cannot setsid, and other members of that group (its parent's other jobs) would be counted as the run's survivors.
    Fork: the child goes on to run the gate in a session of its own; this parent forwards TERM, INT and HUP to it and
    exits with its status (128 + signal when it was killed). The parent holds no lock and writes nothing."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        pid = os.fork()
    except OSError as e:
        _print_blocked([f"the gate leads its process group and cannot fork a session leader ({e.strerror})"])
        sys.exit(2)
    if pid == 0:
        return
    def forward(n, _frame):
        try:
            os.kill(pid, n)
        except OSError:
            pass
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, forward)
    while True:
        try:
            _, st = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    rc = os.waitstatus_to_exitcode(st)
    os._exit(rc if rc >= 0 else 128 - rc)


def _wait(p, low, sig):
    """Wait for the experiment; poll host free space every POLL_S. Under STOP_GB (amendment GF1) the gate stops the
    experiment itself (TERM, KILL after TERM_GRACE_S) and records the free space in low. A TERM the gate forwarded on
    a signal is followed by KILL after the same grace."""
    stop_at = None
    while True:
        try:
            return p.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if stop_at is None and sig:
            stop_at = now
        if stop_at is None:
            try:
                f = free_gb()
            except OSError:
                f = 0.0
            if f < STOP_GB:
                low.append(f"{f:.1f} GiB")
                p.terminate()
                stop_at = now
        if stop_at is not None and now - stop_at > TERM_GRACE_S:
            p.kill()


def _run(args):
    if len(args) < 2 or not LABEL_RX.match(args[0]) or not MODULE_RX.match(args[1]):
        print("disk_gate BLOCKED: usage: run LABEL MODULE [ARGS] (LABEL letters, digits, '_', '.', '-'; MODULE a dotted "
              "module name)")
        sys.exit(2)
    label, module, margs = args[0], args[1], args[2:]
    rel = pathlib.Path(*module.split("."))
    if not ((ROOT / rel).with_suffix(".py").is_file() or (ROOT / rel / "__main__.py").is_file()):
        _print_blocked([f"module {module} is not a module of the pinned tree"])
        sys.exit(2)
    if os.getpgrp() == os.getpid():                 # m-I: a group leader cannot setsid, and its group may hold others
        _own_session()
    try:
        os.setsid()                                 # the run's process group is this process's alone (P3, P11)
    except OSError as e:
        _print_blocked([f"the gate cannot lead a new session and process group ({e.strerror})"])
        sys.exit(2)
    print(f"gate_version={VERSION} sha256 {GATE_SHA256}")
    print(f"gate_limits={LIMITS}")
    sig, child = [], []

    def on_signal(n, _frame):
        sig.append(n)
        for p in child:
            try:
                p.send_signal(signal.SIGTERM)
            except OSError:
                pass
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    try:
        run = _admit(label, os.getpgrp())
    except Blocked as b:
        _print_blocked(b.reasons)
        sys.exit(2)
    print(f"disk_gate start run {run['run']} label {label} commit {run['info']['commit'][:12]} free {run['free']:.1f} "
          f"GiB {_summary(run['obs'])}")
    sys.stdout.flush()
    extra = []
    if sig:
        extra.append(f"the gate was signalled ({sig[0]}) before the experiment started")
        rc = None
    else:
        out = os.open(run["dir"] / "experiment.out", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        env = dict(run["env"], WP_GATE_RUN=run["run"], WP_RUN_DIR=str(run["dir"]))     # m-G: the listed names only
        low = []
        try:
            p = subprocess.Popen(_isolated_cmd(module, *margs), cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=out, pass_fds=(run["lock"].fileno(),))
            child.append(p)
            rc = _wait(p, low, sig)
        except OSError as e:
            rc = None
            extra.append(f"the experiment could not be started ({type(e).__name__})")
        finally:
            os.close(out)
        if sig:
            extra.append(f"the gate was signalled ({sig[0]}) during the run; the experiment was stopped")
        if low:
            extra.append(f"host free fell to {low[0]} during the run, under {STOP_GB} GiB (amendment GF1): the gate "
                         "stopped the experiment")
        if isinstance(rc, int) and rc < 0 and not sig and not low:     # F-2: a death the gate did not cause
            try:
                name = signal.Signals(-rc).name
            except ValueError:
                name = "unknown"
            extra.append(f"the experiment was killed by signal {-rc} ({name}), which the gate did not send: an OOM "
                         "kill, a memory or CPU limit, or another process")
    left = _group_members(os.getpgrp())
    if left is None:
        extra.append("the run's process group could not be listed after the experiment exited")
    elif left:
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        extra.append(f"{len(left)} process(es) of the run outlived the experiment (pids {left[:5]}): killed")
    run["exp_rc"] = rc
    print(f"experiment_exit={rc if rc is not None else 'not run'}")
    sys.exit(_report(run, *_finish(run, extra)))


def _abort(args):
    """P3: close the open run RUN VOID, only when its gate and experiment are dead."""
    reason = " ".join(args[1:]).strip()
    if not args or not reason:
        raise SystemExit("abort needs a run id and a reason: abort RUN REASON")
    run_id = args[0]

    def refuse(msg):
        print(f"disk_gate abort: refused: {msg}; nothing was written")
        sys.exit(2)
    lk = _take_run_lock()
    if lk is None:
        refuse("the run lock is held: a gate or its experiment is alive")
    try:
        with Ledger() as L:
            o = L.open_run()
            if not o or o.get("run") != run_id:
                refuse(f"{run_id[:40]} is not the open run (open: {o['run'] if o else 'none'})")
            if o.get("v") not in ABORTABLE:         # an older gate took no lock and recorded no process group (v5+ do)
                refuse(f"run {run_id} was admitted by an older gate (format {o.get('v')}), which recorded no "
                       "process group; close it with that pinned tree's own abort")   # cannot observe it dead
            pg = o.get("pgid")
            if not isinstance(pg, int) or pg <= 1:
                refuse(f"run {run_id}'s admit record names no process group")
            try:
                os.killpg(pg, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except OSError as e:
                alive = e.errno != errno.ESRCH
            if alive:
                refuse(f"process group {pg} of run {run_id} still has a live member")
            _end_record(L, run_id, "VOID", [f"aborted: {reason[:200]}"], window_end=_utc(), label=o.get("label"))
        print(f"disk_gate abort: run {run_id} closed VOID")
        _verdict_line(run_id, "VOID", None)
    finally:
        lk.close()


def _verdict(args):
    run_id = args[0] if args else ""
    with Ledger() as L:
        adm = [r for r in L.recs if r["kind"] == "admit" and r.get("run") == run_id]
        end = [r for r in L.recs if r["kind"] == "end" and r.get("run") == run_id]
    if not adm:
        print(f"disk_gate verdict: no run {run_id[:40]} in the ledger")
        sys.exit(2)
    if not end:
        print(f"disk_gate verdict: run {run_id} is open (no end record)")
        sys.exit(2)
    if len(end) > 1:                                # m-H: a run has exactly one end; a second one voids it
        print(f"disk_gate VOID: run {run_id} has {len(end)} end records (verdicts "
              f"{[r.get('verdict') for r in end][:5]}); exactly one is written per run")
        _verdict_line(run_id, "VOID", end[-1].get("window_end"))
        sys.exit(3)
    e = end[0]
    for x in e.get("reasons") or []:
        print(f"disk_gate VOID: {x}")
    _verdict_line(run_id, e.get("verdict"), e.get("window_end"))
    sys.exit(0 if e.get("verdict") == "VALID" else 3)


# --------------------------------------------------------------------------------------------------------- commands
def _print_blocked(reasons):
    for r in reasons:
        print(f"disk_gate BLOCKED: {r}")
    print(f"disk_gate BLOCKED {VERSION} sha256 {GATE_SHA256[:16]}")


def _end_record(L, run, verdict, why, **kw):
    """Every end record states the gate, its hash and the limits of its guarantee (B8)."""
    return L.append("end", run=run, verdict=verdict, reasons=why[:20], gate=VERSION, gate_sha256=GATE_SHA256,
                    limits=LIMITS_ID, **kw)


def _verdict_line(run, verdict, window_end):
    print(f"disk_gate {verdict} run {run} window_end {window_end or '-'} {VERSION} sha256 {GATE_SHA256[:16]} {LIMITS}")


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
                print(f"disk_gate init: adopted the existing {len(L.recs) - 1} records; open stops in them stay open")
                return None
            L.append("init", v=FORMAT, source="init", reason=reason[:200])
        print("disk_gate init: new stop ledger started")
        return None
    if cmd == "retire-legacy-stop":
        return _retire_legacy(reason)
    with Ledger() as L:
        if cmd == "stop":
            if not reason:
                raise SystemExit("stop needs a reason")
            r = L.append("stop", id=_new_id(), source="operator", reason=f"written reason: {reason[:200]}")
            print(f"disk_gate stop {r['id']} recorded at seq {r['seq']}")
        elif cmd == "stops":
            for s in L.open_stops():
                print(f"disk_gate open {_stop_text(s)}")
            lg = _legacy_why()
            if lg:
                print(f"disk_gate open {lg}")
            if L.open_stops() or lg:
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
    return None


def _retire_legacy(reason):
    """B7 migration: record a stop naming the legacy path, then move the path aside unread (STOP.retired-<seq>). The
    stop stays open until a person reads what was there and acknowledges it; start stays BLOCKED until then."""
    if not reason:
        raise SystemExit("retire-legacy-stop needs a reason")
    with Ledger() as L:
        k = _legacy_stop()
        if not k:
            print("disk_gate retire-legacy-stop: no legacy state/STOP path (nothing changed)")
            return None
        s = L.append("stop", id=_new_id(), source="retire-legacy-stop",
                     reason=f"a legacy state/STOP path ({k}) was moved aside unread: read it, then acknowledge this "
                            f"stop; written reason: {reason[:200]}")
        dest = STATE_DIR / f"STOP.retired-{s['seq']}"
        if os.path.lexists(dest):
            raise SystemExit(f"retire-legacy-stop: {dest.name} exists already; nothing was moved (stop {s['id']} open)")
        os.replace(STOP, dest)
        _fsync_dir(STATE_DIR)
    print(f"disk_gate retire-legacy-stop: moved state/STOP ({k}) to {dest.name} unread; stop {s['id']} is open "
          "until acknowledged")
    return None


def main(cmd="", *args):
    if cmd == "run":
        return _run(list(args))
    if cmd in ("start", "check", "end"):            # review a108fc3 G-1: they acted on whatever run was current
        print(f"disk_gate: '{cmd}' is gone in v5: 'python -B -m exp.disk_gate run LABEL MODULE [ARGS]' admits, runs and "
              "ends a run in one process")
        sys.exit(2)
    if cmd == "version":
        print(f"gate_version={VERSION} sha256 {GATE_SHA256}")
        print(f"gate_limits={LIMITS}")
        return None
    if cmd in ("init", "stop", "stops", "clear-stop", "abort", "verdict", "ledger-reset", "retire-legacy-stop"):
        try:
            if cmd == "abort":
                return _abort(list(args))
            if cmd == "verdict":
                return _verdict(list(args))
            return _ledger_cmd(cmd, list(args))
        except LedgerError as e:
            print(f"disk_gate BLOCKED: the stop ledger is not intact ({e})")
            sys.exit(2)
    if cmd == "preflight":             # the stores must be clean (P7), as at admission
        why = _clean()
        for w in why:
            print(f"disk_gate BLOCKED: {w}")
        if why:
            sys.exit(2)
        print("disk_gate preflight: stores clean")
        return None
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])

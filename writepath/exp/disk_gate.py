"""Run-validity gate v4 for stamped runs (review F). Design, argument and limits: runkit/GATE-DESIGN.md sections 5
(v3, the tmpfs argument) and 6 (v4: the thing inspected is the thing used).

The hazard is removed, not detected: each store's data directory is a private tmpfs, larger than its container's
memory limit plus Elasticsearch's largest watermark threshold, with no swap and no inode limit. Every tmpfs page and
inode is charged to the container's memory cgroup, so no writer in the container can bring free space down to a
watermark (or, for PG, to zero) without exceeding the limit, and the kernel's lifetime memory.peak proves afterwards
that it never did. v4 binds that evidence to what the experiment uses: the server at the experiment's PG endpoint
reads back a nonce written into the inspected container's tmpfs, the node at es_url is the node running in the
inspected ES container, and every storage path those servers report is on the tmpfs by device number.

  init REASON    start a new stop ledger (every other command is BLOCKED without one), or adopt an existing ledger that
                 has no init record (a v2 ledger, or one whose start was cut): never done implicitly.
  start          BLOCKED (exit 2) unless, for both containers: the data directory is a Docker tmpfs mount, the only
                 mount at or below that point in the container's own mount table, with nr_inodes=0; every path the
                 store reports (PG: data_directory, pg_wal, tablespaces; ES: its data path) and every link under the
                 mount resolve onto it; memory limit set, swap limit and swap use 0, size - limit >= ES's largest
                 free-space threshold + MARGIN (PG: MARGIN); lifetime memory.peak within that bound; free space >=
                 threshold + MARGIN and used <= memory.peak; running, not paused, no OOM flagged; the PG endpoint's
                 server read the nonce; the ES node at es_url is the container's node; ES's disk decider enabled and no
                 index under a flood-stage block. Then, in one locked ledger step: no unacknowledged stop, no legacy
                 state/STOP path, no open run, host free >= START_GB (measured there), identity and counters, admit.
  check NAME     VOID (exit 3) if any start rule fails now, or anything differs from admission: Id, StartedAt, ES node
                 id, PG system identifier, oom and oom_kill counts, memory limit, swap, tmpfs size, ES's thresholds,
                 ES's disk and allocation settings, index blocks; or a stop was recorded since admission; or WP_HOME
                 grew over CAP_GB. A VOID check closes the run.
  end            the same checks; the slow ones first, then, in one locked ledger step, the containers' identity and
                 lifetime counters (the window's end), the ledger, and the end record. VOID (exit 3) on any reason.
  preflight      the erasure gate's store inventory must be empty before any experiment.
  stop REASON    record a stop.   stops: list unacknowledged stops and a legacy path (exit 2 if any).
  clear-stop ID REASON   acknowledge ONE stop by id; it unblocks later starts and never changes a run's verdict.
  abort REASON   close an open run whose launcher died (VOID).
  ledger-reset REASON    keep a DAMAGED ledger under a new name and start a new one holding one open stop (open runs
                 end VOID); an intact ledger is refused.
  retire-legacy-stop REASON   move a legacy state/STOP path aside unread and record a stop to acknowledge (migration).
  version        gate_version=<version> sha256 <this file's SHA-256> and gate_limits=<LIMITS>, for launch_one.sh.
The gate never creates, reads, moves or anchors state/STOP in normal operation: while that path exists in any form,
start is BLOCKED and check and end are VOID. Every verdict carries the gate version, its SHA-256, window_end and
LIMITS. Prints numbers, ids, store paths and reasons only, never a credential."""
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from wp import config

FORMAT = 4
VERSION = "disk_gate v4"
GATE_SHA256 = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
LIMITS_ID = "v4"
LIMITS = ("LIMITS v4 (GATE-DESIGN 6.4): not detected, the operator trust boundary (main's decision for this lab "
          "host): a docker pause and unpause between two steps (needs Docker access, root-equivalent on the host); an "
          "ES disk or allocation setting changed and restored between steps (needs the stream's ES admin credential); "
          "an endpoint switched to another server and back, or a tablespace created and dropped, between steps (needs "
          "Docker or host root, or the PG admin credential). Outside the guarantee: tampering by the same OS user with "
          "the gate's code, state or ledger; root on the host; pages written into a store's tmpfs from outside its "
          "container and mounts shared with other containers (used <= peak is sampled at steps); anything after "
          "window_end; clock stability.")
START_GB, FLOOR_GB, CAP_GB = 20.0, 15.0, 2.0   # start line 20 GB from amendment 24
GIB = 1024 ** 3
MARGIN = 256 * 1024 ** 2                       # headroom over the bound for statfs rounding (inodes: B4, nr_inodes=0)
HOME = pathlib.Path(os.environ["WP_HOME"])
# NOT under WP_HOME/logs: that directory is a registered store the erasure gate inventories.
STATE_DIR = HOME / "state"
STATE = STATE_DIR / "disk_gate.json"
STOP = STATE_DIR / "STOP"                      # legacy path: never read; its existence stops (GATE-DESIGN 6.2 B7)
LEDGER = STATE_DIR / "stop_ledger.jsonl"       # append-only, hash-chained: init, stop, ack, admit, end
HEAD = STATE_DIR / "stop_ledger.head"          # count and SHA-256 of the last record, replaced atomically
LOCK = STATE_DIR / "stop_ledger.lock"
DATA = {"es": "/usr/share/elasticsearch/data", "pg": "/var/lib/postgresql/data"}
NAMES = {"es": "ES", "pg": "PG"}
PROC_ROOT = pathlib.Path("/proc")
CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
API_TIMEOUT_S = 10
DOCKER_TIMEOUT_S = 300
NONCE = "disk_gate.nonce"                      # written into PG's data directory for one read, then removed (B1)
CLUSTER_KEYS = ("cluster.routing.allocation.", "cluster.info.", "cluster.blocks.")      # B5
INDEX_KEYS = ("index.blocks.", "index.routing.allocation.")
INDEX_DEFAULTS = ("index.routing.allocation.include._tier_preference",)                 # ES sets it on every index
# B3: run inside the container with `sh -c PROBE disk_gate-probe <mount> <path>...`. Prints the container's own mount
# table (M), the mount's statfs (F: type, blocks, block size, available, inodes, free inodes), every argument and every
# link under the mount resolved with its device number (P, L; "-" when it does not resolve), then END. Arguments are
# store paths, never secrets.
PROBE = r"""# disk_gate probe v4
r=$1; shift
while IFS= read -r l; do printf 'M\t%s\n' "$l"; done < /proc/self/mountinfo
s=$(stat -f --printf '%T\t%b\t%S\t%a\t%c\t%d' -- "$r") || exit 3
printf 'F\t%s\n' "$s"
res() { if q=$(realpath -e -- "$2" 2>/dev/null) && d=$(stat -L -c %d -- "$q" 2>/dev/null); then
  printf '%s\t%s\t%s\t%s\n' "$1" "$d" "$q" "$2"; else printf '%s\t-\t-\t%s\n' "$1" "$2"; fi; }
for p in "$r" "$@"; do res P "$p"; done
find "$r" -type l > /dev/null 2>&1 || { printf 'E\tfind\n'; exit 3; }
find "$r" -type l 2>/dev/null | while IFS= read -r l; do res L "$l"; done
echo END"""


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


def _docker(*args, timeout=None, input=None):
    """docker with no terminal. `input` (bytes) goes on stdin: the only way a credential reaches a docker exec, since
    Docker's event stream records every exec command line."""
    kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout or DOCKER_TIMEOUT_S, **kw)


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
        lines = r.stdout.decode("utf-8", "replace").splitlines()
    except (OSError, subprocess.TimeoutExpired):
        r, lines = None, []
    if r is None or r.returncode != 0 or not lines or lines[-1] != "END":
        return None, [f"the {K} data mount's statfs and mount table could not be read (the in-container probe failed)"]
    pr = {"mounts": [], "paths": {}, "links": []}
    try:
        for ln in lines[:-1]:
            tag, _, rest = ln.partition("\t")
            if tag == "M":
                pre, _, post = rest.partition(" - ")
                f, g = pre.split(), post.split()
                pr["mounts"].append({"mp": _unoct(f[4]), "mm": f[2], "fs": g[0], "so": g[2] if len(g) > 2 else ""})
            elif tag == "F":
                t, b, s, a, c, d = rest.split("\t")
                pr["statfs"] = {"type": t, "total": int(b) * int(s), "free": int(a) * int(s), "files": int(c),
                                "ffree": int(d)}
            elif tag in ("P", "L"):
                dev, real, p = rest.split("\t", 2)
                if tag == "P":
                    pr["paths"][p] = (dev, real)
                else:
                    pr["links"].append((p, dev, real))
            else:
                raise ValueError(tag)
    except (ValueError, IndexError):
        return None, [f"the {K} in-container probe's output could not be read"]
    if "statfs" not in pr:
        return None, [f"the {K} in-container probe reported no statfs"]
    return pr, []


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
            why.append(f"a link under the {K} data mount does not resolve ({link})")
        elif dev != rdev:
            why.append(f"the link {link} under the {K} data mount resolves to {real}, which is not on the private tmpfs")
    return why, isolated and not why


def _pg_connect():
    """The experiment's own connection function (wp.db.connect) to its endpoint config.PG_HOST:PG_PORT, as the
    cluster admin (its pgpass file; nothing secret is passed or printed)."""
    from wp import db
    return db.connect(db="postgres", role=config.PG_USER, application_name="disk_gate", connect_timeout=API_TIMEOUT_S)


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
        conn = _pg_connect()
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
        if n.get("started") and n["started"] != a["started"]:
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


# --------------------------------------------------------------------------------------------------------- commands
def _blocked(msg):
    print(f"disk_gate BLOCKED start: {msg}")
    sys.exit(2)


def _void_now(msg):
    print(f"disk_gate VOID: {msg}")
    sys.exit(3)


def _end_record(L, run, verdict, why, **kw):
    """Every end record states the gate, its hash and the limits of its guarantee (B8)."""
    return L.append("end", run=run, verdict=verdict, reasons=why[:20], gate=VERSION, gate_sha256=GATE_SHA256,
                    limits=LIMITS_ID, **kw)


def _verdict_line(run, verdict, window_end):
    print(f"disk_gate {verdict} run {run} window_end {window_end or '-'} {VERSION} sha256 {GATE_SHA256[:16]} {LIMITS}")


def _start():
    try:
        with Ledger() as L:
            b = _admission_blocker(L)
    except LedgerError as e:
        b = f"the stop ledger is not intact ({e})"
    if b:
        _blocked(b)
    names = dict(zip(("es", "pg"), _targets()))
    try:
        base = _home_bytes()                        # slow steps first (B6)
    except GateError as e:
        _blocked(str(e))
    slow, why = {}, []
    for key in ("es", "pg"):
        slow[key], w = _slow(key, names[key])
        why += w
    if why:
        _blocked("; ".join(why))
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(4).hex()
    fast, f, t = {}, None, None
    try:
        with Ledger() as L:                         # stops, legacy path, identity, host free and admit: one step
            b = _admission_blocker(L)
            if not b:
                t, w = _utc(), []
                for key in ("es", "pg"):
                    fast[key], x = _fast(key, slow[key]["id"])
                    w += x
                w += _rules(slow, fast)
                b = "; ".join(w) or None
            f = free_gb()
            if not b and f < START_GB:
                b = f"host free {f:.1f} GiB < {START_GB} (measured at admission)"
            adm = None if b else L.append("admit", v=FORMAT, gate=VERSION, run=run_id, es=slow["es"]["id"],
                                          pg=slow["pg"]["id"], free_bytes=int(f * GIB), free_at=_utc(), window_start=t)
    except LedgerError as e:
        b = f"the stop ledger is not intact ({e})"
    if b:
        _blocked(b)
    obs = _merge(slow, fast)
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


def _ledger_why(L, st, when):
    why = []
    if not L.admitted(st["run"], st["admit_seq"]):
        why.append(f"this run's admission record (run {st['run']}, seq {st['admit_seq']}) is not in the ledger: "
                   "truncated, reset or replaced")
    why += [f"STOP: {_stop_text(s)}, {when}" for s in L.stops_after(st["admit_seq"])]
    return why


def _step(st, name, end):
    """check and end (B6): WP_HOME's size and the slow evidence first; then, in one locked ledger step, the ledger,
    the legacy path, both containers' identity and lifetime counters back to back (window_end), the rules, and the
    record (end: always; check: only when VOID, which closes the run). (obs, reasons, host free GiB, window_end)."""
    when = "by the end of the run" if end else f"before {name}"
    try:
        d = (_home_bytes() - st["base_home"]) / GIB
        why = [f"WP_HOME grew {d:.2f} GiB > cap {CAP_GB} {when}"] if d > CAP_GB else []
    except GateError as e:
        why = [f"{e} {when}"]
    slow = {}
    for key in ("es", "pg"):
        slow[key], w = _slow(key, st["obs"][key]["id"])
        why += w
    f, obs, window_end = free_gb(), {}, None
    try:
        with Ledger() as L:
            closed = L.closed(st["run"])
            if closed and not end:
                _void_now(f"run {st['run']} is already closed")
            if closed:
                why.append(f"run {st['run']} was closed before end (a VOID check or an abort)")
            why += _ledger_why(L, st, when)
            lg = _legacy_why()
            if lg:
                why.append(f"{lg}, {when}")
            window_end, fast = _utc(), {}
            for key in ("es", "pg"):
                fast[key], w = _fast(key, st["obs"][key]["id"])
                why += w
            why += _rules(slow, fast)
            obs = _merge(slow, fast)
            why += _drift(obs, st["obs"])
            if _boot_id() != st["boot"]:
                why.append("the host rebooted during the run")
            if not lg and _legacy_why():
                why.append(f"{_legacy_why()}, {when}")
            if not closed and (end or why):
                extra = {} if end else {"at_check": name}
                _end_record(L, st["run"], "VOID" if why else "VALID", why, window_end=window_end,
                            free_bytes=int(f * GIB), evidence=_summary(obs), **extra)
    except LedgerError as e:
        why.append(f"the stop ledger is not intact ({e})")
    return obs, why, f, window_end


def _check(name):
    st = _state()
    obs, why, f, window_end = _step(st, name, end=False)
    if why:
        for x in why:
            print(f"disk_gate VOID: {x}")
        _verdict_line(st["run"], "VOID", window_end)
        sys.exit(3)
    if f < FLOOR_GB:
        st["floor_breaches"].append({"before": name, "free_gb": round(f, 2)})
        _write_json(STATE, st)
    print(f"disk_gate {name} free {f:.1f} GiB {_summary(obs)}")


def _end():
    st = _state()
    obs, why, f, window_end = _step(st, "", end=True)
    print(f"disk_gate end free {f:.1f} GiB floor_breaches {len(st['floor_breaches'])} {_summary(obs)}")
    for x in why:
        print(f"disk_gate VOID: {x}")
    _verdict_line(st["run"], "VOID" if why else "VALID", window_end)
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
        elif cmd == "abort":
            if not reason:
                raise SystemExit("abort needs a reason")
            o = L.open_run()
            if not o:
                print("disk_gate abort: no open run")
                return None
            _end_record(L, o["run"], "VOID", [f"aborted: {reason[:200]}"], window_end=_utc())
            print(f"disk_gate abort: run {o['run']} closed VOID")
            _verdict_line(o["run"], "VOID", None)
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
    if cmd == "start":
        return _start()
    if cmd == "check":
        return _check(args[0] if args else "")
    if cmd == "end":
        return _end()
    if cmd == "version":               # runkit/launch_one.sh writes these into every run's output, before the verdict
        print(f"gate_version={VERSION} sha256 {GATE_SHA256}")
        print(f"gate_limits={LIMITS}")
        return None
    if cmd in ("init", "stop", "stops", "clear-stop", "abort", "ledger-reset", "retire-legacy-stop"):
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

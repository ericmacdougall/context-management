"""Offline test of exp/disk_gate.py, the run-validity gate v3 (review D; design in runkit/GATE-DESIGN.md section 5): no
stores, no real docker, no real cgroups.

docker and du are FAKES on PATH: this same file, run as `python3 test_disk_gate.py --fake docker|du ...`, backed by a
scenario directory (FAKE_DOCKER_STATE). Per container the fake keeps the inspect fields (Id, StartedAt, Pid,
OOMKilled, Mounts), the data path's statfs (type, size, free), an ES node id, and, as real files under the scenario's
proc/ and cg/ trees, /proc/<pid>/cgroup and the cgroup v2 memory files (memory.max, memory.swap.max, memory.peak,
memory.events). A small HTTP server answers the ES node-stats API for the fake ES container. The fake also keeps a log
and an events stream, so the v2 gate (eeb9b82) runs under it too: runkit/failing_first.sh runs these tests against
that gate, where every test of a v3 rule fails on an exit-code assertion (a run it should block or void, admitted or
passed). Tests that need a v3-only command skip there, never counted as red.
usage (from writepath/): python3 -m unittest tests.test_disk_gate -v"""
import calendar
import fcntl
import http.server
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
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
GIB = 1024 ** 3
MIB = 1024 ** 2
ESDATA, PGDATA = "/usr/share/elasticsearch/data", "/var/lib/postgresql/data"


def _ms(rfc):
    base, _, frac = rfc.rstrip("Z").partition(".")
    return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) * 1000 + int((frac + "000")[:3])


class FakeES(http.server.BaseHTTPRequestHandler):
    """ES's node stats for the fake ES container S["cid"]: one data path with the container's statfs, ES's three
    free-space thresholds (S["thr"], default 3, 2 and 1 GiB like the settings below), the container's node id and a
    JVM that started `jvm_offset_s` after the container. /_cluster/settings is what the v2 gate reads. S is reset by
    each test; `auth` records the headers received and `reads` counts requests."""
    S = {}

    def do_GET(self):
        S = FakeES.S
        S.setdefault("auth", []).append(self.headers.get("Authorization"))
        S["reads"] = S.get("reads", 0) + 1
        path = self.path.split("?")[0]
        body, code = None, S.get("status", 200)
        c = S["F"].st()["c"][S["cid"]] if code == 200 else None
        if code == 200 and path == "/_cluster/settings":
            body = {"persistent": dict(S.get("persistent", {})), "transient": dict(S.get("transient", {})),
                    "defaults": {"cluster.routing.allocation.disk.threshold_enabled": "true",
                                 "cluster.routing.allocation.disk.watermark.low": "3gb",
                                 "cluster.routing.allocation.disk.watermark.high": "2gb",
                                 "cluster.routing.allocation.disk.watermark.flood_stage": "1gb",
                                 "cluster.info.update.interval": "30s", "search.default_search_timeout": "-1"}}
        elif code == 200 and path == "/_cluster/state/nodes":
            # v4 (B2): the node answering at es_url; S["eph"] makes it another process than the container's node
            body = {"nodes": {c["node"]: {"name": "node", "ephemeral_id": S.get("eph") or c["eph"]}}}
        elif code == 200 and path.startswith("/_all/_settings"):
            body = S.get("idx", {})
        elif code == 200 and path == "/_nodes/_local/stats/fs,jvm":
            started, now = _ms(c["started"]), int(time.time() * 1000)
            typ, total, free = c["statfs"][ESDATA]
            low, high, flood = S.get("thr", (3 * GIB, 2 * GIB, GIB))
            body = {"nodes": {c["node"]: {"name": "node", "fs": {"data": [{
                "path": S.get("path", ESDATA), "mount": f"{ESDATA} ({typ})", "type": S.get("type", typ),
                "total_in_bytes": total, "free_in_bytes": free, "available_in_bytes": free,
                "low_watermark_free_space_in_bytes": low, "high_watermark_free_space_in_bytes": high,
                "flood_stage_free_space_in_bytes": flood}]},
                "jvm": {"timestamp": now, "uptime_in_millis": now - started - int(S.get("jvm_offset_s", 2) * 1000)}}}}
        elif code == 200:
            code = 404
        data = json.dumps(body if body is not None else {"error": code}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


# ------------------------------------------------------------------------------------------------------- fake docker
def _complete_lines(p):
    try:
        data = p.read_text()
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in data.split("\n")[:-1] if x.strip()]


class Fake:
    """The scenario directory: state.json (containers, flags, triggers, clock offset), log.<id>, events, and the
    proc/ and cg/ trees the gate reads in place of /proc and /sys/fs/cgroup."""

    def __init__(self, root):
        self.root = pathlib.Path(root)

    @classmethod
    def create(cls, root):
        f = cls(root)
        (f.root / "state.json").write_text(json.dumps({"c": {}, "clock": 0.0, "flags": {}, "triggers": [], "calls": {},
                                                       "home_du": GIB}))
        (f.root / "events").touch()
        (f.root / "proc").mkdir()
        (f.root / "cg").mkdir()
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

    # --- cgroup v2 files and /proc/<pid>/cgroup, as real files
    def cgdir(self, cid):
        return self.root / "cg" / "system.slice" / f"docker-{cid}.scope"

    def cg(self, cid, **kw):
        """Set memory.max (int or "max"), memory.swap.max, memory.swap.current, memory.peak and the oom_kill count."""
        d = self.cgdir(cid)
        d.mkdir(parents=True, exist_ok=True)
        for k, name in (("max", "memory.max"), ("swap", "memory.swap.max"), ("peak", "memory.peak"),
                        ("swap_cur", "memory.swap.current")):
            if k in kw:
                (d / name).write_text(f"{kw[k]}\n")
        if "oom_kill" in kw:
            (d / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom {kw['oom_kill']}\noom_kill {kw['oom_kill']}\n"
                                             "oom_group_kill 0\n")

    def oom(self, cid, kill=1):
        """One memory-limit OOM event in the container; with kill=0 the allocation failed and nothing was killed."""
        p = self.cgdir(cid) / "memory.events"
        ev = dict((k, int(v)) for k, v in (x.split() for x in p.read_text().splitlines()))
        ev["oom"] += 1
        ev["oom_kill"] += int(kill)
        p.write_text("".join(f"{k} {v}\n" for k, v in ev.items()))

    def probe(self, c, root, paths):
        """The v4 in-container probe's output for container c (see exp/disk_gate.py PROBE): its mount table, the data
        mount's statfs, each requested path and each link under root resolved, with device numbers."""
        out = [f"M\t{20 + i} 1 {mm} / {mp} rw,relatime - {fs} {fs} {so}" for i, (mp, fs, mm, so) in enumerate(c["mi"])]
        typ, total, free = c["statfs"][root]
        files, ffree = c["inodes"]
        out.append(f"F\t{typ}\t{total // 4096}\t4096\t{free // 4096}\t{files}\t{ffree}")
        mm_root = next((mm for mp, fs, mm, so in c["mi"] if mp == root), "0:40")

        def dev(mm):
            return os.makedev(*map(int, mm.split(":")))

        def res(p):
            real = os.path.normpath(p) if p.startswith("/") else p
            for link, target in c["links"]:
                if real == link or real.startswith(link + "/"):
                    real = os.path.normpath(target + real[len(link):])
                    break
            if real in c.get("missing", []):
                return ["-", "-"]
            d = dev(mm_root) if (real == root or real.startswith(root + "/")) else dev("0:40")
            for mp, fs, mm, so in c["mi"]:
                if mp.startswith(root + "/") and (real == mp or real.startswith(mp + "/")):
                    d = dev(mm)
            return [str(d), real]
        for p in [root] + list(paths):
            out.append("P\t%s\t%s\t%s" % (*res(p), p))
        for link, target in c["links"]:
            if link.startswith(root + "/"):
                out.append("L\t%s\t%s\t%s" % (*res(target), link))
        out.append("END")
        return out

    def cgread(self, cid):
        d = self.cgdir(cid)
        ev = dict(x.split() for x in (d / "memory.events").read_text().splitlines())
        return {"peak": int((d / "memory.peak").read_text()), "oom_kill": int(ev["oom_kill"])}

    def _proc(self, cid, pid, path=None):
        p = self.root / "proc" / str(pid)
        p.mkdir(parents=True, exist_ok=True)
        (p / "cgroup").write_text(f"0::{path or f'/system.slice/docker-{cid}.scope'}\n")

    # --- operations (tests call them directly; triggers call them by name inside a docker call)
    def add(self, name, data_path, used, total, mem, peak):
        cid = os.urandom(32).hex()
        pid = int.from_bytes(os.urandom(2), "big") + 1000

        def f(st):
            st["c"][cid] = {"name": name, "started": self.ts(st), "running": True, "paused": False, "restarts": 0,
                            "gone": False, "logcfg": {"Type": "json-file", "Config": {}}, "tty": False,
                            "du": {data_path: used}, "kept": 0, "drop": [], "pid": pid, "oom": False,
                            "node": os.urandom(8).hex(), "mem": mem, "env": [f"PGDATA={data_path}", "LANG=C.UTF-8"],
                            "mounts": [{"Type": "tmpfs", "Source": "", "Destination": data_path, "RW": True}],
                            "statfs": {data_path: ["tmpfs", total, total - used]},
                            # v4: the node's process id (ES), files written into the container, links under the data
                            # mount, the container's own mount table, inode counts, and what its PG server reports
                            "eph": os.urandom(8).hex(), "files": {}, "links": [], "inodes": [0, 0],
                            "mi": [["/", "overlay", "0:40", "rw,lowerdir=/l,upperdir=/u"],
                                   [data_path, "tmpfs", "0:50", f"rw,nosuid,nodev,size={total // 1024}k,nr_inodes=0,"
                                                                "mode=1777,inode64"]],
                            "pgsrv": {"sysid": str(int.from_bytes(os.urandom(7), "big")), "dd": None, "ts": [],
                                      "su": True}}
        self.locked(f)
        (self.root / f"log.{cid}").touch()
        self.cg(cid, max=mem, swap=0, peak=peak, oom_kill=0, swap_cur=0)
        self._proc(cid, pid)
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
        """docker restart: same Id, new StartedAt and PID, the tmpfs wiped (a new ES node id), a new cgroup."""
        pid = int.from_bytes(os.urandom(2), "big") + 1000

        def f(st):
            for a in ("kill", "die", "stop", "start", "restart"):
                self._event(st, cid, a)
            c = st["c"][cid]
            c.update(started=started or self.ts(st), restarts=c["restarts"] + 1, pid=pid, node=os.urandom(8).hex(),
                     eph=os.urandom(8).hex(), files={})
            c["pgsrv"]["sysid"] = str(int.from_bytes(os.urandom(7), "big"))     # the wiped tmpfs: a new initdb
            for p, v in c["statfs"].items():
                v[2] = v[1]
            return c["mem"]
        mem = self.locked(f)
        self.cg(cid, max=mem, swap=0, peak=GIB // 4, oom_kill=0, swap_cur=0)
        self._proc(cid, pid)

    def replace(self, name):
        def f(st):
            old = self.resolve(st, name)
            for a in ("kill", "die", "destroy"):
                self._event(st, old, a)
            st["c"][old].update(gone=True, running=False)
            c = st["c"][old]
            path, (typ, total, free) = next(iter(c["statfs"].items()))
            return path, total, c["mem"]
        path, total, mem = self.locked(f)
        return self.add(name, path, GIB // 4, total, mem, GIB // 4)

    def pause(self, cid):
        self.event(cid, "pause")
        self.event(cid, "unpause")

    def rotate(self, cid):
        n = len(self.lines(cid))
        self.locked(lambda st: st["c"][cid].update(kept=n))

    def set(self, cid, **kw):
        self.locked(lambda st: st["c"][cid].update(kw))

    def statfs(self, cid, path, typ=None, total=None, free=None):
        def f(st):
            v = st["c"][cid]["statfs"][path]
            st["c"][cid]["statfs"][path] = [typ or v[0], total or v[1], v[2] if free is None else free]
        self.locked(f)

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


class FakePG:
    """The PG server at the experiment's endpoint, as the v4 gate's connection sees it (patched in as _pg_connect).
    By default it is the server in the emlab2-wphard-pg container; state "pg_endpoint" = "other" makes it another
    server (a copy with the same layout but not the inspected container's files), "unreachable" refuses connections.
    pg_read_file reads the files written into the serving container (the nonce)."""

    def __init__(self, F):
        self.F = F
        if F.st().get("pg_endpoint") == "unreachable":
            raise ConnectionRefusedError("connection refused")

    def _server(self):
        st = self.F.st()
        if st.get("pg_endpoint") == "other":
            return {"pgsrv": {"sysid": "1", "dd": None, "ts": [], "su": True}, "env": [f"PGDATA={PGDATA}"]}, {}
        c = st["c"][self.F.resolve(st, "emlab2-wphard-pg")]
        return c, c["files"]

    def execute(self, sql, params=()):
        c, files = self._server()
        s = c["pgsrv"]
        dd = s["dd"] or dict(e.split("=", 1) for e in c.get("env", []) if "=" in e).get("PGDATA")
        if "data_directory" in sql:
            rows = [(dd,)]
        elif "pg_control_system" in sql:
            rows = [(int(s["sysid"]),)]
        elif "pg_tablespace" in sql:
            rows = [("pg_default", ""), ("pg_global", "")] + [tuple(x) for x in s["ts"]]
        elif "pg_read_file" in sql:
            if not s["su"]:
                raise PermissionError("permission denied for function pg_read_file")
            real = os.path.normpath(dd)
            for link, target in c.get("links", []):
                if real == link or real.startswith(link + "/"):
                    real = os.path.normpath(target + real[len(link):])
            v = files.get(real + "/" + params[0])
            if v is None:
                raise FileNotFoundError("could not open file for reading")
            rows = [(v,)]
        else:
            raise ValueError("FakePG: unexpected query")
        return SimpleNamespace(fetchall=lambda: rows)

    def close(self):
        pass


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
        F.fire("inspect")
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
                               "State": {"Running": c["running"], "Paused": c["paused"], "StartedAt": c["started"],
                                         "Pid": c["pid"] if c["running"] else 0, "OOMKilled": c["oom"]},
                               "Mounts": c["mounts"],
                               "HostConfig": {"LogConfig": c["logcfg"], "Memory": c["mem"]},
                               "Config": {"Tty": c["tty"], "Env": c.get("env", [])},
                               "NetworkSettings": {"Ports": c.get("ports") or {}}}]))
        return 0
    if cmd == "exec":
        opts = []
        while args and args[0].startswith("-"):
            n = 2 if args[0] in ("-u", "--user", "-e", "--env", "-w", "--workdir") else 1
            opts, args = opts + args[:n], args[n:]
        ref, ex = args[0], args[1:]
        stdin = sys.stdin.read() if "-i" in opts else ""
        sh = ex[:2] == ["sh", "-c"] and len(ex) > 2
        kind = ("du" if ex[:1] == ["du"] else "stat" if ex[:2] == ["stat", "-f"] else
                "probe" if sh and "disk_gate probe" in ex[2] else "nonce" if sh and "disk_gate nonce" in ex[2] else
                "rm" if ex[:1] == ["rm"] else "curl" if ex[:1] == ["curl"] else "exec")
        F.fire(kind)
        st = F.st()
        cid = F.resolve(st, ref)
        if cid is None or not st["c"][cid]["running"]:
            print("Error response from daemon: container is not running", file=sys.stderr)
            return 1
        text = " ".join(opts + [ref] + ex)               # what Docker's event stream records: the command line
        F.event(cid, "exec_create: " + text)
        F.event(cid, "exec_start: " + text)
        rc = 0
        if kind == "probe":
            c = st["c"][cid]
            if st["flags"].get("stat_rc") or ex[4] not in c["statfs"]:
                print("stat: cannot read file system information", file=sys.stderr)
                rc = 1
            else:
                print("\n".join(F.probe(c, ex[4], ex[5:])))
        elif kind == "nonce":
            F.locked(lambda s: s["c"][cid]["files"].update({ex[-1]: stdin}))
        elif kind == "rm":
            F.locked(lambda s: s["c"][cid]["files"].pop(ex[-1], None))
        elif kind == "curl":
            # only whether the credential arrived on stdin is kept, never the header itself
            F.locked(lambda s: s["c"][cid].update(curl_auth=stdin.startswith("Authorization: ")))
            c = st["c"][cid]
            print(json.dumps({"nodes": {c["node"]: {"name": "node", "ephemeral_id": c["eph"]}}}))
        elif kind == "du":
            if st["flags"].get("du_rc"):
                rc = 1
            else:
                print(f"{st['c'][cid]['du'].get(ex[-1], 0)}\t{ex[-1]}")
        elif kind == "stat":
            ent = st["c"][cid]["statfs"].get(ex[-1])
            if ent is None or st["flags"].get("stat_rc"):
                print("stat: cannot read file system information", file=sys.stderr)
                rc = 1
            else:
                print(f"{ent[0]} {ent[1] // 4096} 4096 {ent[2] // 4096}")
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
        st = F.st()
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
    """Defaults (GATE-DESIGN 5.3, MARGIN 256 MiB): ES tmpfs 6 GiB, limit 2 GiB, peak 1 GiB, ES low threshold 3 GiB
    (bound 6 - 3 - 0.25 = 2.75 GiB); PG tmpfs 2 GiB, limit 1 GiB, peak 0.5 GiB (bound 1.75 GiB); swap 0."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        home = pathlib.Path(cls.tmp) / "home"
        (home / "conf").mkdir(parents=True)
        cls.es_api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeES)
        threading.Thread(target=cls.es_api.serve_forever, daemon=True).start()
        cls.es_port = cls.es_api.server_address[1]
        for k, v in dict(GOOD, es_url=f"http://127.0.0.1:{cls.es_port}").items():
            (home / "conf" / k).write_text(v + "\n")
        bindir = pathlib.Path(cls.tmp) / "bin"
        bindir.mkdir()
        for prog in ("docker", "du"):
            p = bindir / prog
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{pathlib.Path(__file__).resolve()}" --fake {prog} "$@"\n')
            p.chmod(0o755)
        cls.env = mock.patch.dict(os.environ, {"WP_HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
                                               "PYTHONPATH": str(HERE), "FAKE_DOCKER_STATE": "",
                                               "DISK_GATE_SAMPLE_S": "0.5"})   # the v2 gate's sampler, in tests
        cls.env.start()
        sys.path.insert(0, str(HERE))
        src = os.environ.get("DISK_GATE_UNDER_TEST") or str(HERE / "exp" / "disk_gate.py")
        spec = importlib.util.spec_from_file_location("disk_gate_under_test", src)
        cls.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.g)
        cls.home = home
        cls.v3 = hasattr(cls.g, "MARGIN")           # the v3 gate; the v2 gate (eeb9b82) runs these as red evidence

    @classmethod
    def tearDownClass(cls):
        cls.es_api.shutdown()
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(self.home / "state", ignore_errors=True)
        root = pathlib.Path(tempfile.mkdtemp(dir=self.tmp))
        os.environ["FAKE_DOCKER_STATE"] = str(root)
        self.F = Fake.create(root)
        self.es = self.F.add("emlab2-wphard-es", ESDATA, GIB, 6 * GIB, 2 * GIB, GIB)
        self.pg = self.F.add("emlab2-wphard-pg", PGDATA, GIB // 4, 2 * GIB, GIB, GIB // 2)
        self.F.set(self.es, ports={"9200/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.es_port)}]})
        FakeES.S = {"F": self.F, "cid": self.es}
        self.free = lambda: 30
        patches = [mock.patch.object(shutil, "disk_usage", lambda p: SimpleNamespace(
            total=100 * GIB, used=(100 - self.free()) * GIB, free=self.free() * GIB))]
        if hasattr(self.g, "_sleep"):
            patches.append(mock.patch.object(self.g, "_sleep", lambda s: time.sleep(min(s, 0.02))))
        for name, v in (("SYNC_TIMEOUT_S", 15.0), ("CLOSE_TIMEOUT_S", 15.0)):
            if hasattr(self.g, name):
                patches.append(mock.patch.object(self.g, name, v))
        if self.v3:
            patches += [mock.patch.object(self.g, "PROC_ROOT", root / "proc"),
                        mock.patch.object(self.g, "CGROUP_ROOT", root / "cg"),
                        # v4's connection to the experiment's PG endpoint (B1); create=True so that an older gate
                        # under test runs these tests to a FAIL, not an error
                        mock.patch.object(self.g, "_pg_connect", lambda: FakePG(self.F), create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._reap)
        if self.v3:
            rc, out = self.call("init", "test home")
            self.assertEqual(rc, 0, out)

    def _reap(self):
        """Stop capture companions a v2 gate under test started (test-owned processes only)."""
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

    # ---- helpers
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
        self.assertEqual(rc, 0, f"start should admit the run: {out.strip()[-400:]}")

    def need_v3(self):
        if not self.v3:
            self.skipTest("needs a command the v2 gate lacks (not counted as red)")

    def need_v4(self):
        if not hasattr(self.g, "LIMITS"):
            self.skipTest("needs a command the v3 gate lacks (not counted as red)")

    def last_record(self):
        return json.loads((self.home / "state" / "stop_ledger.jsonl").read_text().splitlines()[-1])

    def legacy_write(self, how):
        """A legacy writer at state/STOP: `touch` (a directory's times, or a new file), `cp` (a file landing inside a
        directory, or at the path) or `overwrite` (an existing entry's content, or the path itself)."""
        p = self.stop_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if how == "touch":
            p.touch()
        elif p.is_dir():
            (p / ("why" if how == "cp" else "README")).write_text("legacy: stop now\n")
        else:
            p.write_text("legacy: stop now\n")

    def assertBlocked(self, rc, out, *why):
        """BLOCKED; on the v3 gate also for the stated reason(s), so a test cannot pass on an incidental failure."""
        self.assertEqual(rc, 2, f"start must be BLOCKED (exit 2), got {rc!r}: {out.strip()[-400:]}")
        for w in (why if self.v3 else ()):
            self.assertIn(w, out, f"BLOCKED, but not for the reason under test ({w!r})")

    def assertVoid(self, rc, out, *why):
        self.assertEqual(rc, 3, f"expected VOID (exit 3), got {rc!r}: {out.strip()[-400:]}")
        self.assertIn("VOID", out)
        for w in (why if self.v3 else ()):
            self.assertIn(w, out, f"VOID, but not for the reason under test ({w!r})")

    def assertValid(self, rc, out):
        self.assertEqual(rc, 0, f"expected a valid run (exit 0), got {rc!r}: {out.strip()[-400:]}")

    def stop_path(self):
        return self.home / "state" / "STOP"

    def ack_all(self, reason):
        if self.v3 or hasattr(self.g, "open_stops"):
            for s in self.g.open_stops():
                self.call("clear-stop", s["id"], reason)

    # ---- positive control
    def test_valid_run_passes(self):
        self.start()
        self.assertValid(*self.call("check", "x"))
        self.assertValid(*self.call("end"))

    # ---- R1 isolation: the data paths must be private tmpfs mounts
    def test_es_data_on_a_shared_volume_blocks(self):
        self.F.set(self.es, mounts=[{"Type": "volume", "Name": "emlab2-wphard-es-data", "Destination": ESDATA}])
        self.F.statfs(self.es, ESDATA, typ="ext4", total=643 * GIB, free=57 * GIB)
        self.assertBlocked(*self.call("start"), "not a Docker tmpfs mount")

    def test_pg_data_on_a_shared_volume_blocks(self):
        self.F.set(self.pg, mounts=[{"Type": "volume", "Name": "emlab2-wphard-pg-data", "Destination": PGDATA}])
        self.F.statfs(self.pg, PGDATA, typ="ext2/ext3", total=643 * GIB, free=57 * GIB)
        self.assertBlocked(*self.call("start"), "not a Docker tmpfs mount")

    def test_es_reporting_another_filesystem_blocks(self):
        FakeES.S.update(type="ext4")                    # inspect says tmpfs, ES's own view of its data path does not
        self.assertBlocked(*self.call("start"), "ES reports its data path on ext4")

    def test_es_reporting_another_data_path_blocks(self):
        FakeES.S.update(path="/srv/esdata")
        self.assertBlocked(*self.call("start"), "data path")

    def test_a_mount_inside_the_data_directory_blocks(self):
        self.F.set(self.es, mounts=[{"Type": "tmpfs", "Destination": ESDATA},
                                    {"Type": "bind", "Source": "/home/x", "Destination": ESDATA + "/nodes"}])
        self.assertBlocked(*self.call("start"), "inside the data directory")

    # ---- R2 bound: tmpfs larger than limit + threshold + margin; no swap; a limit at all
    def test_es_tmpfs_not_above_limit_plus_threshold_blocks(self):
        self.F.statfs(self.es, ESDATA, total=5 * GIB)      # 5 - 2 = 3 GiB < 3 GiB threshold + 256 MiB
        self.assertBlocked(*self.call("start"), "can bring free space below")

    def test_pg_tmpfs_not_above_limit_blocks(self):
        self.F.cg(self.pg, max=2 * GIB)                    # PG tmpfs 2 GiB, limit 2 GiB: the tmpfs could fill
        self.assertBlocked(*self.call("start"), "can bring free space below")

    def test_swap_allowed_blocks(self):
        self.F.cg(self.es, swap=GIB)
        self.assertBlocked(*self.call("start"), "swap")

    def test_no_memory_limit_blocks(self):
        self.F.cg(self.pg, max="max")
        self.assertBlocked(*self.call("start"), "no memory limit")

    # ---- R3 lifetime peak
    def test_peak_already_over_the_bound_blocks(self):
        self.F.cg(self.es, peak=3 * GIB)                   # earlier in its life the container used more than 2.75 GiB
        self.assertBlocked(*self.call("start"), "memory peak")

    def test_limit_raised_and_restored_with_a_peak_over_the_bound_voids(self):
        # docker update raised the limit, the store grew past the bound, then the limit was put back: only the
        # kernel's lifetime peak still shows it
        self.start()
        self.F.cg(self.es, max=4 * GIB)
        self.F.cg(self.es, peak=int(3.5 * GIB))
        self.F.cg(self.es, max=2 * GIB)
        self.assertVoid(*self.call("end"), "memory peak")

    # ---- R4 identity
    def test_oom_kill_of_a_process_in_the_container_voids(self):
        # live 2026-11-17: an OOM-killed child leaves PID 1, StartedAt and the Id unchanged
        self.start()
        self.F.cg(self.pg, oom_kill=1)
        self.assertVoid(*self.call("end"), "OOM kill")

    def test_docker_oomkilled_flag_blocks(self):
        self.F.set(self.es, oom=True)
        self.assertBlocked(*self.call("start"), "OOM kill")

    def test_restart_voids(self):
        self.start()
        self.F.restart(self.es)
        self.assertVoid(*self.call("end"), "StartedAt")

    def test_recreate_voids(self):
        self.start()
        new = self.F.replace("emlab2-wphard-pg")
        self.assertTrue(new)
        self.assertVoid(*self.call("end"), "PG")

    def test_es_node_id_changed_voids(self):
        # the data path was wiped or swapped under a running container: ES keeps its node id there
        self.start()
        self.F.set(self.es, node="0123456789abcdef")
        self.assertVoid(*self.call("end"), "node id")

    def test_paused_at_end_voids(self):
        self.start()
        self.F.set(self.pg, paused=True)
        self.assertVoid(*self.call("end"), "not running")

    # ---- R5 configuration
    def test_memory_limit_changed_voids(self):
        self.start()
        self.F.cg(self.es, max=GIB + GIB // 2)
        self.assertVoid(*self.call("end"), "memory limit")

    def test_es_threshold_changed_voids(self):
        self.start()
        FakeES.S.update(thr=(GIB, GIB // 2, GIB // 4))
        self.assertVoid(*self.call("end"), "watermark thresholds")

    # ---- evidence that cannot be read fails the rule, never passes it
    def test_cgroup_unreadable_blocks(self):
        (self.F.cgdir(self.es) / "memory.peak").unlink()
        self.assertBlocked(*self.call("start"), "cgroup")

    def test_cgroup_not_naming_the_container_blocks(self):
        pid = self.F.st()["c"][self.es]["pid"]
        self.F._proc(self.es, pid, "/user.slice/user-1000.slice/session-1.scope")
        self.assertBlocked(*self.call("start"), "cgroup")

    def test_es_api_unreadable_blocks(self):
        FakeES.S.update(status=503)
        self.assertBlocked(*self.call("start"), "could not be read")

    def test_es_api_unreadable_at_end_voids(self):
        self.start()
        FakeES.S.update(status=503)
        self.assertVoid(*self.call("end"), "could not be read")

    def test_pg_statfs_unreadable_voids(self):
        self.start()
        self.F.flag(stat_rc=1)
        self.assertVoid(*self.call("end"), "statfs")

    # ---- B2 (review F F14, ES): the node at es_url is the node running in the inspected container
    def test_es_url_answered_by_another_node_process_blocks(self):
        FakeES.S.update(eph="0f0f0f0f0f0f0f0f")       # es_url answers for a process that is not the container's node
        self.assertBlocked(*self.call("start"), "ephemeral")

    def test_es_url_switched_to_another_node_during_the_run_voids(self):
        self.start()
        FakeES.S.update(eph="0f0f0f0f0f0f0f0f")
        self.assertVoid(*self.call("end"), "ephemeral")

    # ---- R7 host free space measured inside the locked admission step (review D F1)
    def test_tmpfs_written_from_outside_the_container_voids(self):
        # review D advisor: pages written into the tmpfs by another cgroup (docker cp, a host process in the mount
        # namespace) are not bounded by the container's limit; the premise used <= peak is checked on every observation
        self.need_v3()
        self.start()
        self.F.statfs(self.pg, PGDATA, free=2 * GIB - (3 * GIB) // 4)        # 0.75 GiB used, lifetime peak 0.5 GiB
        self.assertVoid(*self.call("end"), "charged to another cgroup")

    def test_pgdata_outside_the_tmpfs_blocks(self):
        self.need_v3()
        self.F.set(self.pg, env=["PGDATA=/var/lib/postgresql/elsewhere"])
        self.assertBlocked(*self.call("start"), "/var/lib/postgresql/elsewhere")

    # ---- B1 (review F F14): the server at the experiment's endpoint is the one in the inspected container
    def test_another_pg_server_at_the_experiments_endpoint_blocks(self):
        # a qualifying tmpfs container is inspected while 127.0.0.1:pg_port is served by another PG server
        self.F.locked(lambda st: st.update(pg_endpoint="other"))
        self.assertBlocked(*self.call("start"), "nonce")

    def test_endpoint_switched_to_another_pg_server_during_the_run_voids(self):
        self.start()
        self.F.locked(lambda st: st.update(pg_endpoint="other"))
        self.assertVoid(*self.call("end"), "nonce")

    def test_pg_endpoint_unreachable_at_end_voids(self):
        self.start()
        self.F.locked(lambda st: st.update(pg_endpoint="unreachable"))
        self.assertVoid(*self.call("end"), "endpoint")

    def test_admin_role_that_cannot_read_server_files_blocks(self):
        self.F.locked(lambda st: st["c"][self.pg]["pgsrv"].update(su=False))
        self.assertBlocked(*self.call("start"), "server file")

    def test_pg_cluster_replaced_under_the_container_voids(self):
        # a new initdb in the same tmpfs (same container, same StartedAt): the system identifier changes
        self.start()
        self.F.locked(lambda st: st["c"][self.pg]["pgsrv"].update(sysid="42"))
        self.assertVoid(*self.call("end"), "system identifier")

    def test_nonce_file_is_removed_after_each_step(self):
        self.start()
        self.assertEqual(self.F.st()["c"][self.pg]["files"], {}, "the gate left its nonce file in PG's data directory")

    # ---- B3 (review F F15): every storage path the server names, and every link, is on the private tmpfs
    def test_pgdata_dotdot_escape_blocks(self):
        self.F.set(self.pg, env=[f"PGDATA={PGDATA}/../on-disk"])
        self.assertBlocked(*self.call("start"), "on-disk")

    def test_data_directory_overriding_pgdata_blocks(self):
        # PGDATA names the tmpfs, but the server runs with data_directory set elsewhere (postgresql.conf, -D)
        self.F.locked(lambda st: st["c"][self.pg]["pgsrv"].update(dd="/srv/pgdata"))
        self.assertBlocked(*self.call("start"), "/srv/pgdata")

    def test_pg_wal_linked_outside_the_tmpfs_blocks(self):
        self.F.set(self.pg, links=[[PGDATA + "/pg_wal", "/var/lib/pgwal"]])
        self.assertBlocked(*self.call("start"), "/var/lib/pgwal")

    def test_tablespace_outside_the_tmpfs_blocks(self):
        self.F.locked(lambda st: st["c"][self.pg]["pgsrv"].update(ts=[["ts_ext", "/mnt/ts"]]))
        self.F.set(self.pg, links=[[PGDATA + "/pg_tblspc/16400", "/mnt/ts"]])
        self.assertBlocked(*self.call("start"), "/mnt/ts")

    def test_tablespace_created_during_the_run_outside_the_tmpfs_voids(self):
        self.start()
        self.F.locked(lambda st: st["c"][self.pg]["pgsrv"].update(ts=[["ts_ext", "/mnt/ts"]]))
        self.assertVoid(*self.call("end"), "/mnt/ts")

    def test_mount_below_the_data_mount_in_the_containers_mount_table_blocks(self):
        self.F.locked(lambda st: st["c"][self.pg]["mi"].append([PGDATA + "/base", "ext4", "8:1", "rw"]))
        self.assertBlocked(*self.call("start"), "below")

    def test_es_data_link_outside_the_tmpfs_blocks(self):
        self.F.set(self.es, links=[[ESDATA + "/indices", "/var/tmp/indices"]])
        self.assertBlocked(*self.call("start"), "/var/tmp/indices")

    def test_dangling_link_under_the_data_mount_blocks(self):
        self.F.set(self.pg, links=[[PGDATA + "/pg_tblspc/16401", "/gone"]], missing=["/gone"])
        self.assertBlocked(*self.call("start"), "resolve")

    # ---- B4 (review F F16): no inode limit; an allocation failure at the memory limit voids
    def test_tmpfs_with_an_inode_limit_blocks(self):
        def f(st):
            st["c"][self.pg]["mi"][1][3] = "rw,nosuid,nodev,size=2097152k,nr_inodes=4096,mode=1777"
            st["c"][self.pg]["inodes"] = [4096, 4000]
        self.F.locked(f)
        self.assertBlocked(*self.call("start"), "inode")

    def test_oom_event_without_a_kill_voids(self):
        self.start()
        self.F.oom(self.pg, kill=0)                         # an allocation failed at the limit; nothing was killed
        self.assertVoid(*self.call("end"), "oom")

    # ---- B5 (review F F17): the disk and allocation settings, at admission and every step
    def test_disk_threshold_check_disabled_at_admission_blocks(self):
        FakeES.S.update(persistent={"cluster.routing.allocation.disk.threshold_enabled": "false"})
        self.assertBlocked(*self.call("start"), "threshold_enabled")

    def test_disk_threshold_check_disabled_persistently_during_the_run_voids(self):
        # unchanged threshold figures (node stats) do not show that the disk decider was turned off
        self.start()
        FakeES.S.update(persistent={"cluster.routing.allocation.disk.threshold_enabled": "false"})
        self.assertVoid(*self.call("check", "x"), "threshold_enabled")

    def test_transient_watermark_change_still_present_voids(self):
        self.start()
        FakeES.S.update(transient={"cluster.routing.allocation.disk.watermark.flood_stage": "10mb"})
        self.assertVoid(*self.call("end"), "flood_stage")

    def test_flood_stage_block_on_an_index_voids(self):
        self.start()
        FakeES.S.update(idx={"wp16096-events": {"settings": {"index.blocks.read_only_allow_delete": "true"}}})
        self.assertVoid(*self.call("end"), "read_only_allow_delete")

    def test_index_created_during_the_run_with_es_defaults_stays_valid(self):
        self.start()
        FakeES.S.update(idx={"wp16096-new": {"settings": {
            "index.routing.allocation.include._tier_preference": "data_content"}}})
        self.assertValid(*self.call("end"))

    # ---- B6 (review F F18): nothing slow after the final identity read
    def test_restart_during_the_end_home_measurement_voids(self):
        self.start()
        self.F.trigger("du", 1, "restart", self.pg)         # fires inside end's WP_HOME measurement
        self.assertVoid(*self.call("end"), "restarted")

    def test_oom_kill_during_the_check_home_measurement_voids(self):
        self.start()
        self.F.trigger("du", 1, "oom", self.es, 1)
        self.assertVoid(*self.call("check", "x"), "OOM")

    # ---- B8 (review F F19): every verdict carries the gate version, its hash, the window's end and the limits
    def test_every_verdict_states_the_gate_and_its_limits(self):
        self.start()
        rc, out = self.call("end")
        self.assertValid(rc, out)
        self.assertIn("LIMITS v4", out, "the VALID verdict line does not state the operator trust boundary")
        rec = self.last_record()
        self.assertEqual(rec.get("limits"), "v4")
        self.assertEqual(rec.get("gate"), "disk_gate v4")
        self.assertRegex(rec.get("gate_sha256") or "", r"^[0-9a-f]{64}$")
        self.assertLessEqual(rec.get("window_end") or "~", rec["at"])

    # ---- B9 (review F F20, F21)
    def test_store_on_a_volume_is_not_called_a_tmpfs(self):
        self.F.set(self.pg, mounts=[{"Type": "volume", "Name": "emlab2-wphard-pg-data", "Destination": PGDATA}])
        self.F.statfs(self.pg, PGDATA, typ="ext2/ext3", total=643 * GIB, free=57 * GIB)
        rc, out = self.call("start")
        self.assertBlocked(rc, out, "not a Docker tmpfs mount")
        self.assertNotIn("tmpfs holds", out, "a volume's figures were reported as a tmpfs's")

    def test_swap_in_use_blocks(self):
        self.F.cg(self.pg, swap_cur=4096)                   # memory.swap.max 0 now, pages swapped before it was set
        self.assertBlocked(*self.call("start"), "swap")

    def test_no_stream_code_writes_disk_or_block_settings(self):
        # GATE-DESIGN 5.4: thresholds changed and restored between two observations are outside what the gate sees, so
        # no stream code may change cluster or index settings at all (bootstrap sets the watermarks as node settings);
        # this fence fails when such a call is added anywhere in writepath. runkit/live/ is left out: its kits change
        # settings on purpose, as test cases, on their own disposable emlab2-gatefix-* containers only.
        pat = re.compile(r"\b(PUT|POST)\b[^\n]*(_cluster/settings|/_settings\b|index\.blocks|disk\.watermark)|"
                         r"(_cluster/settings|/_settings\b)[^\n]*\b(PUT|POST)\b")
        found = []
        for f in sorted(HERE.rglob("*")):
            rel = f.relative_to(HERE)
            if (rel.parts[0] in ("tests", "results") or rel.parts[:2] == ("runkit", "live") or
                    f.suffix not in (".py", ".sh") or not f.is_file()):
                continue
            for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if pat.search(line):
                    found.append(f"{rel}:{n}")
        self.assertEqual(found, [], "stream code that writes cluster or index settings (disk thresholds, blocks)")

    def test_free_space_dropping_during_setup_blocks(self):
        # 30 GiB free before the gate observed anything, 18 GiB once it had read ES: admission must use the latter
        self.free = lambda: 18 if FakeES.S.get("reads") else 30
        self.assertBlocked(*self.call("start"), "host free")

    # ---- R8 own files
    def test_own_files_growing_over_the_cap_voids(self):
        self.start()
        self.F.locked(lambda st: st.update(home_du=4 * GIB))
        self.assertVoid(*self.call("end"), "grew")

    # ---- R6 stops (A3, P2)
    def test_stop_in_the_window_voids_even_after_ack(self):
        self.start()
        self.call("stop", "operator stop mid-run")
        self.ack_all("acknowledged during the run")
        self.assertVoid(*self.call("end"), "STOP")

    def test_open_stop_blocks_start(self):
        self.call("stop", "left by an earlier run")
        self.assertBlocked(*self.call("start"), "unacknowledged stop")

    def test_check_void_closes_the_run(self):
        self.start()
        self.F.cg(self.pg, oom_kill=2)
        self.assertVoid(*self.call("check", "x"))
        rc, out = self.call("end")
        self.assertEqual(rc, 3, out)

    # ---- ledger continuity (review D F7)
    def _ledger(self):
        return self.home / "state" / "stop_ledger.jsonl"

    def test_truncated_ledger_blocks(self):
        self.call("stop", "a stop that must not vanish")
        p = self._ledger()
        lines = p.read_bytes().split(b"\n")[:-1]
        p.write_bytes(b"\n".join(lines[:-1]) + (b"\n" if lines[:-1] else b""))   # drop the last record, the stop
        rc, out = self.call("stops")
        self.assertEqual(rc, 2, f"a ledger missing its last record must not read as intact: {out.strip()[-300:]}")
        if self.v3:
            self.assertIn("not intact", out)

    def test_deleted_ledger_blocks(self):
        self.call("stop", "a stop that must not vanish")
        for p in self.home.joinpath("state").glob("stop_ledger*"):
            p.unlink()
        self.assertBlocked(*self.call("start"), "ledger")

    def test_head_anchor_required(self):
        self.call("stop", "s")
        self.ack_all("acknowledged")
        (self.home / "state" / "stop_ledger.head").unlink(missing_ok=True)
        self.assertBlocked(*self.call("start"), "head anchor")

    def test_end_requires_this_runs_admit_record(self):
        self.start()
        for p in self.home.joinpath("state").glob("stop_ledger*"):
            p.unlink()
        if self.v3:
            self.call("init", "a fresh ledger replaced the one that admitted the run")
        self.assertVoid(*self.call("end"), "admission record")

    def test_ledger_reset_keeps_the_broken_ledger(self):
        self.need_v3()
        self.call("stop", "s")
        p = self._ledger()
        p.write_bytes(p.read_bytes()[:-5] + b"}\n")        # damage the last record
        self.assertBlocked(*self.call("start"), "not intact")
        rc, out = self.call("ledger-reset", "damaged in a test")
        self.assertEqual(rc, 0, out)
        kept = list(self.home.joinpath("state").glob("stop_ledger.broken-*.jsonl"))
        self.assertEqual(len(kept), 1, "the broken ledger must be kept")
        # the damaged ledger's stops cannot be read: the new ledger holds one open stop until a person acknowledges it
        self.assertBlocked(*self.call("start"), "unacknowledged stop")
        self.ack_all("checked the damaged ledger")
        self.start()

    def test_ledger_reset_refuses_an_intact_ledger(self):
        self.need_v3()
        self.call("stop", "an open stop that one command must not clear")
        rc, out = self.call("ledger-reset", "trying to clear the stops")
        self.assertEqual(rc, 2, f"ledger-reset on an intact ledger must be refused: {out.strip()[-200:]}")
        self.assertIn("intact", out)
        self.assertBlocked(*self.call("start"), "unacknowledged stop")

    def _write_v2_ledger(self):
        for p in self.home.joinpath("state").glob("stop_ledger*"):
            p.unlink()
        import hashlib
        prev, lines = "0" * 64, []
        for i, rec in enumerate([{"kind": "stop", "id": "s-1", "source": "operator", "reason": "x"},
                                 {"kind": "ack", "stop_id": "s-1", "reason": "y"}]):
            ln = json.dumps(dict(rec, seq=i + 1, prev=prev, at="2026-11-16T00:00:00Z"), sort_keys=True).encode()
            lines.append(ln)
            prev = hashlib.sha256(ln).hexdigest()
        self._ledger().write_bytes(b"\n".join(lines) + b"\n")
        return lines

    def test_ledger_cut_back_past_its_adoption_is_not_readopted_silently(self):
        # review D advisor: a ledger adopted at record k+1, cut back to k records with its anchor removed, read like
        # a v2 ledger and was adopted again, dropping every later record.
        self.need_v3()
        lines = self._write_v2_ledger()
        self.call("init", "adopting the v2 ledger")
        self.call("stop", "a stop recorded after the adoption")
        self._ledger().write_bytes(b"\n".join(lines) + b"\n")
        (self.home / "state" / "stop_ledger.head").unlink()
        rc, out = self.call("stops")
        self.assertEqual(rc, 2, f"a ledger cut back past its init record read as intact: {out.strip()[-200:]}")
        self.assertBlocked(*self.call("start"), "init record")

    def test_v2_ledger_is_adopted_once_and_only_by_init(self):
        self.need_v3()
        self._write_v2_ledger()
        self.assertBlocked(*self.call("start"), "init record")     # never adopted implicitly
        rc, out = self.call("init", "adopting the v2 ledger")
        self.assertEqual(rc, 0, out)
        self.start()
        self.assertValid(*self.call("end"))
        kinds = [json.loads(x)["kind"] for x in self._ledger().read_text().splitlines()]
        self.assertEqual(kinds[:3], ["stop", "ack", "init"])
        self.assertEqual(kinds.count("init"), 1)

    def test_run_admitted_by_an_older_gate_is_refused(self):
        self.need_v3()
        (self.home / "state" / "disk_gate.json").write_text(json.dumps({"run": "old", "admit_seq": 1,
                                                                        "run_dir": "/nonexistent"}))
        self.assertVoid(*self.call("end"), "older gate")

    # ---- legacy STOP path (review D F3, review F F13): never read, never moved, never anchored; its existence stops
    def test_legacy_stop_file_blocks_and_stays_where_it_was_written(self):
        p = self.stop_path()
        if p.is_dir():
            shutil.rmtree(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("operator: stop now\n")
        self.assertBlocked(*self.call("start"), "state/STOP")
        self.assertTrue(p.is_file() and p.read_text() == "operator: stop now\n",
                        "the gate moved or changed a legacy STOP file instead of leaving it for a person")

    def _finalization_write(self, how):
        """A legacy write made inside end's locked ledger step, after the gate read the ledger (review F F13)."""
        self.start()
        orig = self.g._ledger_why

        def lw(L, st, when):
            self.legacy_write(how)
            return orig(L, st, when)
        with mock.patch.object(self.g, "_ledger_why", lw):
            rc, out = self.call("end")
        rc2, out2 = self.call("start")
        self.assertTrue(rc == 3 or rc2 == 2,
                        f"a legacy STOP {how} during finalization was lost: end rc {rc}, next start rc {rc2}")
        if rc2 == 2 and self.v3:
            self.assertIn("STOP", out2)

    def test_legacy_stop_touched_during_finalization_is_never_lost(self):
        self._finalization_write("touch")

    def test_legacy_stop_copied_during_finalization_is_never_lost(self):
        self._finalization_write("cp")

    def test_legacy_stop_overwritten_during_finalization_is_never_lost(self):
        self._finalization_write("overwrite")

    def test_legacy_stop_entry_overwritten_in_the_window_voids(self):
        # review F F13: overwriting an existing entry changes no directory time and no entry name
        self.start()
        self.legacy_write("overwrite")
        self.assertVoid(*self.call("end"), "STOP")

    def test_legacy_stop_written_in_the_window_voids_and_persists(self):
        self.start()
        self.legacy_write("touch")
        self.assertVoid(*self.call("end"), "STOP")
        self.assertBlocked(*self.call("start"), "STOP")

    def test_stop_and_abort_still_work_while_a_legacy_path_exists(self):
        self.start()
        self.legacy_write("touch")
        rc, out = self.call("stop", "recorded while a legacy STOP path exists")
        self.assertEqual(rc, 0, out)
        rc, out = self.call("abort", "launcher gone")
        self.assertEqual(rc, 0, out)

    def test_retire_legacy_stop_moves_it_unread_and_leaves_a_stop_to_acknowledge(self):
        self.need_v4()
        p = self.stop_path()
        p.mkdir(parents=True, exist_ok=True)                 # what every v3.0 home holds
        (p / "README").write_text("Not a stop file.\n")
        self.assertBlocked(*self.call("start"), "state/STOP")
        rc, out = self.call("clear-stop", "state/STOP", "trying to acknowledge the legacy path")
        self.assertEqual(rc, 2, "clear-stop must not acknowledge the legacy path")
        rc, out = self.call("retire-legacy-stop", "v3.0 home: the gate-made directory")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(p), "the legacy path is still there after retire")
        kept = list(p.parent.glob("STOP.retired-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / "README").read_text(), "Not a stop file.\n", "retire changed what was there")
        self.assertBlocked(*self.call("start"), "unacknowledged stop")
        self.ack_all("read the retired directory")
        self.start()
        self.assertFalse(os.path.lexists(p), "the gate created state/STOP again")

    # ---- the credential (GATE-DESIGN 4.3, kept in v3)
    def test_api_key_is_sent_and_never_written(self):
        sys.path.insert(0, str(HERE / "runkit"))
        import leak_scan
        import secrets as _s
        kid, key = _s.token_urlsafe(15)[:20], _s.token_urlsafe(16)[:22]
        enc = __import__("base64").b64encode(f"{kid}:{key}".encode()).decode()
        sec = self.home / "secrets"
        sec.mkdir(exist_ok=True)
        (sec / "es_api_key.json").write_text(json.dumps({"id": kid, "encoded": enc}))
        try:
            FakeES.S.update(status=401)
            rc, out1 = self.call("start")
            self.assertEqual(rc, 2, "an API that refuses the key must block start")
            FakeES.S.update(status=200, type="ext4")
            rc, out2 = self.call("start")
            self.assertEqual(rc, 2, "a data path off tmpfs must block start")
            FakeES.S.pop("type")
            rc, out3 = self.call("start")
            self.assertEqual(rc, 0, out3)
            rc, out4 = self.call("end")
            out2 += out3 + out4
        finally:
            (sec / "es_api_key.json").unlink()
        self.assertIn("ApiKey " + enc, FakeES.S.get("auth", []), "the gate did not send the stream's API key")
        # Docker's event stream records every exec command line: the key must never be in one (review F, advisor)
        argv = [json.dumps(e) for e in self.F.events()]
        self.assertFalse(any(key in a or enc in a for a in argv), "the API key was put on a docker command line")
        if hasattr(self.g, "LIMITS"):
            self.assertTrue(self.F.st()["c"][self.es].get("curl_auth"),
                            "the in-container ES request did not receive the credential on stdin")
        written = [p for p in (self.home / "state").rglob("*") if p.is_file()]
        hits = [p.name for p in written if leak_scan.find(p.read_bytes(), [key, enc])]
        self.assertEqual(hits, [], "a gate file holds the API key")
        self.assertEqual(leak_scan.find((out1 + out2).encode(), [key, enc]), [], "the gate printed the API key")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        sys.exit(fake_main(sys.argv[2:]))
    unittest.main(verbosity=2)

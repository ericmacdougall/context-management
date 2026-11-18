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
import hashlib
import http.server
import importlib.util
import inspect
import io
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
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


# ------------------------------------------------------------------------------------------- pinned tree (v5, G-2)
COMMIT = "c0ffee1" + "0" * 33               # the fake commit the test tree's manifest names
THIS = pathlib.Path(__file__).resolve()

# The experiment the gate runs in the Launcher tests: a module at the pinned tree's root. Driven by STUB_* variables:
# STUB_STARTED (file: its pid, written first), STUB_OPS (JSON list of steps, below), STUB_RELEASE (file to wait for),
# STUB_ROWS (JSON list of result rows to write), STUB_PRINT (text to print), STUB_RC (exit status).
STUB = r'''"""Test experiment for tests/test_disk_gate.py (not a lab experiment)."""
import json, os, pathlib, subprocess, sys, time
env = os.environ
if env.get("STUB_STARTED"):
    pathlib.Path(env["STUB_STARTED"]).write_text(str(os.getpid()))
sys.path.insert(0, env["STUB_TESTS"])
from test_disk_gate import Fake                      # noqa: E402
F = Fake(env["FAKE_DOCKER_STATE"])
for op in json.loads(env.get("STUB_OPS") or "[]"):
    if op[0] == "fake":                              # a fake docker operation in the window
        getattr(F, op[1])(*op[2:])
    elif op[0] == "gate":                            # a gate command in the window (e.g. stop)
        subprocess.run([sys.executable, "-B", env["STUB_TESTS"] + "/test_disk_gate.py", "--gate"] + op[1:],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    elif op[0] == "append":                          # append to a file of the pinned tree
        with open(op[1], "a") as f:
            f.write(op[2])
    elif op[0] == "write":                           # write a file anywhere (e.g. a forged state file)
        pathlib.Path(op[1]).write_text(op[2])
    elif op[0] == "linger":                          # a child that outlives the experiment, in its process group
        subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    elif op[0] == "dump":                            # v6 m-G: what the experiment was started with
        pathlib.Path(op[1]).write_text(json.dumps({"env": sorted(env), "isolated": sys.flags.isolated,
                                                   "bytecode_off": sys.dont_write_bytecode}))
    elif op[0] == "mkfifo":                          # v6 m-B: a FIFO in the run directory
        os.mkfifo(os.path.join(env["WP_RUN_DIR"], op[1]))
    elif op[0] == "sparse":                          # v6 m-C: a large sparse file in the run directory
        with open(os.path.join(env["WP_RUN_DIR"], op[1]), "wb") as f:
            f.truncate(int(op[2]))
    elif op[0] == "bigrow":                          # v6 m-C: a JSON row over the gate's size cap
        with open(os.path.join(env["WP_RUN_DIR"], op[1]), "w") as f:
            f.write('{"gate_run": "%s", "at": "%s", "pad": "' % (env["WP_GATE_RUN"],
                                                                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            f.write("x" * int(op[2]))
            f.write('"}')
    elif op[0] == "selfkill":                        # v6 F-2: the experiment dies by a signal the gate did not send
        os.kill(os.getpid(), int(op[1]))
rel = env.get("STUB_RELEASE")
t0 = time.monotonic()
while rel and not os.path.exists(rel) and time.monotonic() - t0 < 120:
    time.sleep(0.05)
out = pathlib.Path(env.get("WP_RUN_DIR") or pathlib.Path(env["WP_HOME"]) / "results")
out.mkdir(parents=True, exist_ok=True)
for i, r in enumerate(json.loads(env.get("STUB_ROWS") or "[]")):
    run = env.get("WP_GATE_RUN") if r.get("gate_run") == "self" else r.get("gate_run")
    doc = {"name": r["name"], "at": r.get("at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "result": {"i": i}}
    if run:
        doc["gate_run"] = run
    (out / f"{r['name']}.{i:016x}.json").write_text(json.dumps(doc))
print(env.get("STUB_PRINT", ""))
sys.exit(int(env.get("STUB_RC", "0")))
'''


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


def write_manifest(tree, commit=COMMIT):
    """TREE.manifest of a tree, in the format runkit/tree_manifest.py writes from git objects: a header, the commit,
    then '<sha256> <mode> <path>' per file, sorted; GIT_HEAD and the manifest itself are not listed."""
    rows = []
    for d, dirs, files in os.walk(tree):
        dirs.sort()
        for n in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, tree)
            if rel in ("TREE.manifest", "GIT_HEAD"):
                continue
            if os.path.islink(p):
                rows.append((rel, "120000", _sha256(os.readlink(p).encode())))
            else:
                rows.append((rel, "100755" if os.stat(p).st_mode & 0o100 else "100644",
                             _sha256(pathlib.Path(p).read_bytes())))
    text = "tree-manifest v1\ncommit %s\n" % commit + "".join(f"{h} {m} {r}\n" for r, m, h in sorted(rows))
    (pathlib.Path(tree) / "TREE.manifest").write_text(text)
    (pathlib.Path(tree) / "GIT_HEAD").write_text(commit[:7] + "\n")


def make_tree(src_root, dest):
    """A pinned tree: the writepath tree of the gate under test (no results, tests or bytecode), the test experiment
    and a manifest naming COMMIT."""
    shutil.copytree(src_root, dest, ignore=shutil.ignore_patterns("results", "tests", "__pycache__", "*.pyc",
                                                                  "TREE.manifest", "GIT_HEAD"))
    (dest / "stubexp.py").write_text(STUB)
    write_manifest(dest)
    return dest


# ------------------------------------------------------- the live catalogs as a clean lab cluster shows them (v6, F-3)
# Written out here, not imported from exp/guard_policy.py: a weakened expectation in the gate's tree must not move the
# scenario with it. Values are text, as the gate compares them. test_v6_guard_policy_is_bound_to_setup binds the
# expectation file to wp/setup.py.
T_POLICY = {"events": "SI", "quarantine": "SI", "inbox": "SIU", "analyst_jobs": "SIU", "facts": "SIU", "cursors": "SIU",
            "erasure_jobs": "SI", "tombstones": "S", "store_registry": "S", "decision_heads": "S", "chains": "S",
            "subjects": "S", "leases": "", "quarantine_quota": "", "identifier_epochs": "S", "subject_identifiers": "S",
            "erasure_mentions": "S", "legal_holds": "S", "erasure_withheld": "S", "erasure_rescanned": "S",
            # integ: the hardening tables (no app privilege; written through SECURITY DEFINER functions)
            "write_refusals": "", "identifier_proposals": "", "quarantine_removed": ""}
T_UNGUARDED = {"write_refusals"}                # schema.sql gives it no guard triggers (exp/guard_policy.py exemption)
T_PRIV = {"S": "SELECT", "I": "INSERT", "U": "UPDATE"}
T_LOGIN = ["wp_app", "wp_broker", "wp_kms", "wp_legal", "wp_rev1", "wp_rev2", "wp_sealer", "wp_verifier", "wp_watchdog"]
T_GROUP = ["wp_reviewer"]
T_REVIEWERS = ["wp_rev1", "wp_rev2", "wp_reviewer"]
T_EVENTS_CONNECT = ["wp_app", "wp_broker", "wp_legal", "wp_rev1", "wp_rev2", "wp_sealer", "wp_verifier"]
T_CONF = "idle_in_transaction_session_timeout=5s,lock_timeout=10s,statement_timeout=30s"
T_ROW, T_BEFORE, T_INS, T_DEL, T_UPD, T_TRUNC = 1, 2, 4, 8, 16, 32
# effective privileges of the roles other than wp_app, as wp/schema.sql grants them (reviewers inherit wp_reviewer's)
T_ROLE_TABLES = {"wp_legal": ["legal_holds", "erasure_jobs", "erasure_withheld"],
                 "wp_sealer": ["identifier_epochs", "subject_identifiers"],
                 **{r: ["identifier_proposals", "subjects"] for r in T_REVIEWERS}}
T_EXECUTE = {"public.wp_identifier_approve": T_REVIEWERS, "public.wp_identifier_reject": T_REVIEWERS,
             "public.wp_identifier_propose": ["wp_app"] + T_REVIEWERS, "public.wp_identifier_declare": [],
             "public.wp_hold_place": ["wp_legal"], "public.wp_hold_release": ["wp_legal"],
             "public.wp_erasure_complete": ["wp_verifier"], "public.wp_write_refused": ["wp_app"],
             "public.wp_lease_holder_current": ["wp_broker"],
             "wp_seal.install": [], "wp_seal.message": [], "wp_seal.verify": []}


def _db_acl(conn):
    # integ: setup revokes TEMPORARY from PUBLIC on both stream databases (review J M5)
    return ",".join(sorted(["wp:CONNECT", "wp:CREATE", "wp:TEMPORARY"] + [f"{g}:CONNECT" for g in conn]))


def good_catalog():
    """{section: {tag: rows}}: what the gate's tagged catalog queries read on a cluster wp.setup --reset just built
    (integ: with the hardening objects of feat/writepath-hardening)."""
    roles = [["wp", "True", "True", "True", "True", "True", "True", "True"]]
    roles += [[r, "False", "True", "False", "False", str(r in T_LOGIN), "False", "False"]
              for r in sorted(T_LOGIN + T_GROUP + ["wp_owner"])]
    cluster = {
        "roles": roles,
        "members": sorted([["pg_checkpoint", "wp_kms"], ["pg_read_all_stats", "wp_kms"],
                           ["pg_signal_backend", "wp_watchdog"], ["pg_read_all_stats", "wp_watchdog"],
                           ["wp_reviewer", "wp_rev1"], ["wp_reviewer", "wp_rev2"]]),
        "settings": [[r, "", T_CONF] for r in T_LOGIN],
        "databases": [["postgres", "5", "True", "wp", "PUBLIC:CONNECT,PUBLIC:TEMPORARY,wp:CONNECT,wp:CREATE,wp:TEMPORARY"],
                      ["template0", "4", "False", "wp", ""], ["template1", "1", "True", "wp", ""],
                      ["wp_events", "16384", "True", "wp", _db_acl(T_EVENTS_CONNECT)],
                      ["wp_keys", "16385", "True", "wp", _db_acl(["wp_kms", "wp_verifier"])]],
        "parameter_acl": [], "auto_conf": []}
    tables = sorted(T_POLICY)
    guarded = [t for t in tables if t not in T_UNGUARDED]
    definer = [f.split(".", 1)[1] for f in sorted(T_EXECUTE) if f.startswith("public.")]
    ev = {"schema": [["public", "2200", "wp_owner", ""]],
          "relations": [[t, str(17000 + i), "r", "wp_owner", "", "False", "False"] for i, t in enumerate(tables)],
          "column_acl": [["erasure_jobs", "status", "{wp_app=w/wp_owner}"]],
          "triggers": sorted([[t, f"{t}_guard", str(T_BEFORE | T_ROW | T_INS | T_UPD | T_DEL), "A", "wp_guard"]
                              for t in guarded] + [[t, f"{t}_no_truncate", str(T_BEFORE | T_TRUNC), "A", "wp_guard"]
                                                   for t in guarded]),
          "functions": [["wp_guard", "", "wp_owner", "False", "", "0" * 32, ""]]
                       + [[f, "x text", "wp_owner", "True", "", "1" * 32, ""] for f in definer],
          "constraints": [["events", "events_pkey", "PRIMARY KEY (id)"], ["facts", "facts_pkey", "PRIMARY KEY (id)"]],
          "indexes": [["decision_source_once", "True", "True", "CREATE UNIQUE INDEX decision_source_once ..."],
                      ["erasure_one_open", "True", "True", "CREATE UNIQUE INDEX erasure_one_open ..."]],
          "policies": [], "default_acl": [],
          "namespaces": [["public", "wp_owner"], ["wp_seal", "wp_owner"]],
          "app_privs": [[t, T_PRIV[p]] for t in tables for p in sorted(T_POLICY[t])],
          "app_column_update": [[t] for t in tables if "U" in T_POLICY[t] or t == "erasure_jobs"],
          "app_execute": [["wp_guard"]] + [[f.split(".", 1)[1]] for f in sorted(T_EXECUTE) if "wp_app" in T_EXECUTE[f]],
          # integ: the private seal schema and every role's effective privileges (wp_app's tables are app_privs)
          "seal_schema": [["wp_seal", "2300", "wp_owner", "{wp_owner=UC/wp_owner}"]],
          "seal_relations": [["seal_keys", "17100", "r", "wp_owner", "{wp_owner=arwdDxt/wp_owner}", "False", "False"]],
          "seal_triggers": [],
          "seal_functions": [[f, "x text", "wp_owner", "False", "{wp_owner=X/wp_owner}", "2" * 32, "search_path=pg_catalog, pg_temp"]
                             for f in ("install", "message", "verify")],
          "role_privs": sorted([["wp_verifier", f"public.{t}", "SELECT"] for t in tables]
                               + [[r, f"public.{t}", "SELECT"] for r, ts in T_ROLE_TABLES.items() for t in ts]),
          "role_schema": sorted([r, "public", "USAGE"] for r in
                                ["wp_app", "wp_broker", "wp_legal", "wp_sealer", "wp_verifier"] + T_REVIEWERS),
          "role_execute": sorted([f, r] for f, rs in T_EXECUTE.items() for r in rs)}
    ks = {"schema": [["public", "2201", "wp_kms", ""]],
          "relations": [["subject_keys", "18000", "r", "wp_kms", "", "False", "False"],
                        ["tenant_keys", "18001", "r", "wp_kms", "", "False", "False"]],
          "column_acl": [], "triggers": [], "functions": [], "constraints": [], "indexes": [], "policies": [],
          "default_acl": [], "namespaces": [["public", "wp_kms"]], "verifier_key_read": [["False"]]}
    return {"cluster": cluster, "wp_events": ev, "wp_keys": ks}


GATE_CG = "user.slice/gate-test.scope"         # the gate's own cgroup in the scenario (v6 F-2)


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
        elif code == 200 and path.startswith("/_cat/indices/"):
            # v5 P7: indices under the stream prefix left by an earlier run (none on clean stores); "cat_status" makes
            # this one request fail (review aacf3c0 mutant es_clean_error_passes)
            code = S.get("cat_status", 200)
            body = [{"index": n} for n in S.get("es_indices", [])] if code == 200 else {"error": code}
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
                                                       "home_du": GIB, "catalog": good_catalog()}))
        (f.root / "events").touch()
        (f.root / "proc").mkdir()
        (f.root / "cg").mkdir()
        # v6 F-2: the gate's own cgroup (/proc/self/cgroup) and its memory.events
        (f.root / "proc" / "self").mkdir()
        (f.root / "proc" / "self" / "cgroup").write_text(f"0::/{GATE_CG}\n")
        (f.root / "cg" / GATE_CG).mkdir(parents=True)
        (f.root / "cg" / GATE_CG / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        return f

    # --- v6: the gate's own cgroup, the live catalogs, other containers using the home
    def gate_oom(self):
        """An OOM kill in the gate's own cgroup (the experiment or a process it started)."""
        p = self.root / "cg" / GATE_CG / "memory.events"
        ev = dict((k, int(v)) for k, v in (x.split() for x in p.read_text().splitlines()))
        ev["oom"] += 1
        ev["oom_kill"] += 1
        p.write_text("".join(f"{k} {v}\n" for k, v in ev.items()))

    def cat(self, section, tag, how, *row):
        """Change what a catalog query returns: how "add" appends row, "drop" removes the rows starting with row,
        "set" replaces the rows starting with row[0] by row."""
        def f(st):
            rows = st["catalog"][section][tag]
            n = len(row) if how == "drop" else 1
            keep = [r for r in rows if r[:n] != list(row[:n])] if how in ("drop", "set") else rows
            st["catalog"][section][tag] = keep + ([list(row)] if how in ("add", "set") else [])
        self.locked(f)

    def cat_restore(self, section, tag):
        self.locked(lambda st: st["catalog"][section].update({tag: good_catalog()[section][tag]}))

    def reset_db(self, db):
        """wp.setup --reset of database db (drop schema public cascade; create; schema files): new object ids."""
        def f(st):
            s = st["catalog"][db]
            s["schema"] = [[r[0], str(int(r[1]) + 100000)] + r[2:] for r in s["schema"]]
            s["relations"] = [[r[0], str(int(r[1]) + 100000)] + r[2:] for r in s["relations"]]
            for tag in ("seal_schema", "seal_relations"):
                if tag in s:
                    s[tag] = [[r[0], str(int(r[1]) + 100000)] + r[2:] for r in s[tag]]
        self.locked(f)

    def add_user(self, name, source):
        """A container that is not one of the run's stores, bind-mounting `source` (v6 F-4)."""
        cid = self.add(name, "/srv/other", GIB // 4, GIB, GIB, GIB // 4)
        self.set(cid, mounts=[{"Type": "bind", "Source": source, "Destination": "/w", "RW": True}])
        return cid

    def remove(self, cid):
        """docker rm of a container that is not a store (integ: a service container an experiment removes)."""
        self.locked(lambda st: st["c"][cid].update(gone=True, running=False))

    def setflag(self, name, value):
        self.flag(**{name: value})

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

    def probe(self, c, root, paths, v5=False):
        """The in-container probe's output for container c (see exp/disk_gate.py PROBE): its mount table, the data
        mount's statfs, each requested path and each link under root resolved, with device numbers. v4 prints one
        tab-separated line per record; v5 (review a108fc3 m-1) prints NUL-terminated fields, a fixed count per tag."""
        recs = [("M", f"{20 + i} 1 {mm} / {mp} rw,relatime - {fs} {fs} {so}") for i, (mp, fs, mm, so) in enumerate(c["mi"])]
        typ, total, free = c["statfs"][root]
        files, ffree = c["inodes"]
        recs.append(("F", f"{typ}\t{total // 4096}\t4096\t{free // 4096}\t{files}\t{ffree}"))
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
            recs.append(("P", *res(p), p))
        for link, target in c["links"]:
            if link.startswith(root + "/"):
                recs.append(("L", *res(target), link))
        if v5:
            return b"".join(f.encode("utf-8", "surrogateescape") + b"\0" for r in recs for f in r) + b"END\0"
        return ("\n".join("\t".join(r) for r in recs) + "\nEND\n").encode("utf-8", "surrogateescape")

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

    # v5 P7: the base tables of the public schema per database, and whether each holds a row (clean = only
    # store_registry, which setup fills); a scenario replaces it with state "tables"
    TABLES = {"wp_events": {"store_registry": True, "events": False, "facts": False},
              "wp_keys": {"subject_keys": False, "tenant_keys": False}}

    def __init__(self, F, db=None, **kw):
        self.F, self.db = F, db or "postgres"
        if F.st().get("pg_endpoint") == "unreachable":
            raise ConnectionRefusedError("connection refused")

    def _server(self):
        st = self.F.st()
        if st.get("pg_endpoint") == "other":
            return {"pgsrv": {"sysid": "1", "dd": None, "ts": [], "su": True}, "env": [f"PGDATA={PGDATA}"]}, {}
        c = st["c"][self.F.resolve(st, "emlab2-wphard-pg")]
        return c, c["files"]

    def execute(self, sql, params=()):
        self.F.locked(lambda st: st.setdefault("pg_sql", []).append([self.db, str(sql)]))   # m-6: what the gate sent
        c, files = self._server()
        s = c["pgsrv"]
        dd = s["dd"] or dict(e.split("=", 1) for e in c.get("env", []) if "=" in e).get("PGDATA")
        tables = (self.F.st().get("tables") or self.TABLES).get(self.db, {})
        m = re.search(r'exists\s*\(\s*select 1 from public\."((?:[^"]|"")+)"', str(sql), re.I)
        tag = re.match(r"/\* gate:(\w+) \*/", str(sql))
        if tag:                                     # v6: a catalog query, answered from the scenario's catalog
            st = self.F.st()
            if st["flags"].get("catalog_fail"):
                raise RuntimeError("FakePG: the catalog cannot be read")
            rows = [tuple(r) for r in st["catalog"]["cluster" if self.db == "postgres" else self.db][tag.group(1)]]
        elif re.match(r"\s*set\s+statement_timeout", str(sql), re.I):
            rows = []
        elif m:
            rows = [(bool(tables.get(m.group(1).replace('""', '"'))),)]
        elif "pg_class" in str(sql) and "public" in str(sql):
            rows = [(t,) for t in tables]
        elif "data_directory" in sql:
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
    F = Fake(os.environ.get("FAKE_DOCKER_STATE") or "/nonexistent-fake-state")
    prog, args = argv[0], argv[1:]
    if prog == "du":
        F.fire("du")
        st = F.st()
        if st["flags"].get("du_rc"):
            return 1
        print(f"{st['home_du']}\t{args[-1]}")
        return 0
    if prog == "ps":                                # the host's ps; "host_ps_rc" makes it fail (mutant group_list)
        if F.root.is_dir() and F.st()["flags"].get("host_ps_rc"):
            print("ps: cannot list processes", file=sys.stderr)
            return 1
        os.execv("/bin/ps", ["ps", *args])
    cmd, args = args[0], args[1:]
    if cmd == "ps":                                 # v6 F-4: docker ps -aq --no-trunc
        st = F.st()
        if st["flags"].get("docker_ps_rc"):
            print("Cannot connect to the Docker daemon", file=sys.stderr)
            return 1
        for cid, c in st["c"].items():
            if not c["gone"]:
                print(cid)
        return 0
    if cmd == "inspect":
        fmt = None
        if args[0] == "-f":
            fmt, args = args[1], args[2:]
        if args[:1] == ["--type"]:
            args = args[2:]
        F.fire("inspect")
        st = F.st()
        objs = []
        for ref in args:
            cid = F.resolve(st, ref)
            if cid is None:
                print("Error: No such object: " + ref, file=sys.stderr)
                return 1
            c = st["c"][cid]
            if fmt == "{{.State.StartedAt}}":
                print(c["started"])
                return 0
            if fmt:                                 # the 7b4839b gate's format
                print(f"{cid}|{c['started']}|{json.dumps(c['logcfg'])}")
                return 0
            objs.append({"Id": cid, "Name": "/" + c["name"], "RestartCount": c["restarts"],
                         "State": dict({"Running": c["running"], "Paused": c["paused"],
                                        "Pid": c["pid"] if c["running"] else 0, "OOMKilled": c["oom"]},
                                       **({} if st["flags"].get("no_started") else {"StartedAt": c["started"]})),
                         "Mounts": c["mounts"],
                         "HostConfig": {"LogConfig": c["logcfg"], "Memory": c["mem"]},
                         "Config": {"Tty": c["tty"], "Env": c.get("env", []), "Labels": c.get("labels") or {}},
                         "NetworkSettings": {"Ports": c.get("ports") or {}}})
        print(json.dumps(objs))
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
                sys.stdout.flush()
                sys.stdout.buffer.write(F.probe(c, ex[4], ex[5:], v5="probe v5" in ex[2]))
        elif kind == "nonce" and st["flags"].get("nonce_rc"):
            print("sh: cannot create the file", file=sys.stderr)
            rc = 1
        elif kind == "nonce":
            F.locked(lambda s: s["c"][cid]["files"].update({ex[-1]: stdin}))
        elif kind == "rm":
            F.locked(lambda s: s["c"][cid]["files"].pop(ex[-1], None))
        elif kind == "curl" and st["flags"].get("curl_rc"):
            print("curl: (7) Failed to connect", file=sys.stderr)
            rc = 7
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
        for prog in ("docker", "du", "ps"):
            p = bindir / prog
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{pathlib.Path(__file__).resolve()}" --fake {prog} "$@"\n')
            p.chmod(0o755)
        cls.env = mock.patch.dict(os.environ, {"WP_HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
                                               "PYTHONPATH": str(HERE), "FAKE_DOCKER_STATE": "",
                                               "DISK_GATE_SAMPLE_S": "0.5"})   # the v2 gate's sampler, in tests
        cls.env.start()
        cls.home = home
        # v5 (review a108fc3 G-2): the gate runs from a PINNED tree under WP_HOME/runs whose TREE.manifest names a
        # commit. The tree here is a copy of the tree of the gate under test, with its own manifest (a fake commit).
        src = pathlib.Path(os.environ.get("DISK_GATE_UNDER_TEST") or HERE / "exp" / "disk_gate.py").resolve()
        cls.bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True                # a pinned tree holds no bytecode cache (v5 P4)
        # a gate at <tree>/exp/disk_gate.py brings its tree; a VARIANT file elsewhere (a mutant, for a failing-first
        # receipt) replaces the gate in this tree, and the manifest is written for it
        whole = src.parent.name == "exp" and src.name == "disk_gate.py"
        cls.tree = make_tree(src.parents[1] if whole else HERE, home / "runs" / f"{COMMIT[:7]}-20261118T000000Z")
        if not whole:
            shutil.copyfile(src, cls.tree / "exp" / "disk_gate.py")
            write_manifest(cls.tree)
        os.environ["PYTHONPATH"] = str(cls.tree)
        for m in [m for m in sys.modules if m in ("wp", "exp") or m.startswith(("wp.", "exp."))]:
            del sys.modules[m]
        sys.path.insert(0, str(cls.tree))
        spec = importlib.util.spec_from_file_location("disk_gate_under_test", cls.tree / "exp" / "disk_gate.py")
        cls.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.g)
        cls.v3 = hasattr(cls.g, "MARGIN")           # the v3 gate; the v2 gate (eeb9b82) runs these as red evidence
        cls.v5 = hasattr(cls.g, "RUN_LOCK")         # the v5 gate: `run` is the only way to start and end a run

    @classmethod
    def tearDownClass(cls):
        cls.es_api.shutdown()
        cls.env.stop()
        sys.dont_write_bytecode = cls.bytecode
        if str(cls.tree) in sys.path:
            sys.path.remove(str(cls.tree))
        for m in [m for m in sys.modules if m in ("wp", "exp") or m.startswith(("wp.", "exp."))]:
            del sys.modules[m]
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
                        mock.patch.object(self.g, "_pg_connect", lambda *a, **k: FakePG(self.F, *a, **k), create=True),
                        # v5 P7: the store inventory's unregistered list (wp.registry), from the scenario
                        mock.patch.object(self.g, "_unregistered", lambda *a: self.F.st().get("unregistered", []),
                                          create=True),
                        # v6: the store reset and cleanup the gate runs under its lock (recorded, not run), and the
                        # interpreter's -I flag (this test process is not isolated; test_v6_m_g_* tests the check)
                        mock.patch.object(self.g, "_run_tools", fake_tools(self.F, self.g), create=True),
                        mock.patch.object(self.g, "_isolated", lambda: True, create=True)]
        self._run = None
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
        """A gate command in this process. On the v5 gate, `start` and `end` have no command (only `run` starts and
        ends a run); the storage tests drive the same two steps `run` takes, admission and end, through the gate's
        own functions, so every rule of those steps is tested here and `run` itself in the Launcher tests."""
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                if self.v5 and args and args[0] in ("start", "end"):
                    return self._step(args[0]), buf.getvalue()
                self.g.main(*args)
            return 0, buf.getvalue()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) or e.code is None else 1
            return code or 0, buf.getvalue() + ("" if isinstance(e.code, int) or e.code is None else str(e.code))

    def _step(self, which):
        g = self.g
        if which == "start":
            try:
                self._run = g._admit("t", os.getpgrp())
            except g.Blocked as b:
                g._print_blocked(b.reasons)
                return 2
            print(f"disk_gate start run {self._run['run']}")
            return 0
        if self._run is None:
            print("disk_gate VOID: no run admitted in this test")
            return 3
        return g._report(self._run, *g._finish(self._run))

    def start(self):
        rc, out = self.call("start")
        self.assertEqual(rc, 0, f"start should admit the run: {out.strip()[-400:]}")

    def need_v3(self):
        if not self.v3:
            self.skipTest("needs a command the v2 gate lacks (not counted as red)")

    def need_v4(self):
        if not hasattr(self.g, "LIMITS"):
            self.skipTest("needs a command the v3 gate lacks (not counted as red)")

    def need_v5(self):
        if not self.v5:
            self.skipTest("drives v5's own launcher or run lock, which the v4 gate lacks (not counted as red)")

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
        self.assertVoid(*self.call("end"), "threshold_enabled")

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

    def test_oom_kill_during_the_end_home_measurement_voids(self):
        self.start()
        self.F.trigger("du", 1, "oom", self.es, 1)
        self.assertVoid(*self.call("end"), "OOM")

    # ---- B8 (review F F19): every verdict carries the gate version, its hash, the window's end and the limits
    def test_every_verdict_states_the_gate_and_its_limits(self):
        self.start()
        rc, out = self.call("end")
        self.assertValid(rc, out)
        self.assertIn(self.g.LIMITS, out, "the VALID verdict line does not state the operator trust boundary")
        rec = self.last_record()
        self.assertEqual(rec.get("limits"), self.g.LIMITS_ID)
        self.assertEqual(rec.get("gate"), self.g.VERSION)
        self.assertRegex(rec.get("gate_sha256") or "", r"^[0-9a-f]{64}$")
        self.assertLessEqual(rec.get("window_end") or "~", rec["at"])

    def test_version_names_the_gate_its_hash_and_its_limits(self):
        # what runkit/launch_one.sh writes into every run's output before the verdict (B8)
        rc, out = self.call("version")
        self.assertEqual(rc, 0, f"no version command: {out.strip()[-200:]}")
        f = dict(x.split("=", 1) for x in out.splitlines() if x.startswith("gate_"))
        sha = hashlib.sha256(pathlib.Path(self.g.__file__).read_bytes()).hexdigest()
        self.assertEqual(f.get("gate_version"), f"{self.g.VERSION} sha256 {sha}")
        self.assertEqual(f.get("gate_limits"), self.g.LIMITS, "the limits are not stated")

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
        # 30 GiB free before the gate observed anything, 9 GiB once it had read ES: admission must use the latter
        # (amendment GF1: the start line is 10 GB; it was 20 GB and 18 GiB here before GF1)
        self.free = lambda: 9 if FakeES.S.get("reads") else 30
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
        if self.v5:
            self.skipTest("v5 keeps no run state file: a run lives in its launcher's memory (GATE-DESIGN 8.2)")
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
        if not self.v5:                    # v5 abort needs a dead launcher: the G-1 tests cover it
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


    # ================================================================ review a108fc3 (gate v5): the gate is the launcher
    def records(self):
        p = self.home / "state" / "stop_ledger.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def ends(self):
        return [(r.get("run"), r.get("verdict")) for r in self.records() if r["kind"] == "end"]

    def launch(self, label="x", tree=None, **stub):
        return Launch(self, label, tree, **stub)

    def fresh_tree(self):
        """A pinned tree of its own for a test that changes it (same commit, same files)."""
        d = self.home / "runs" / f"{COMMIT[:7]}-{os.urandom(4).hex()}"
        shutil.copytree(self.tree, d, symlinks=True)
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def abort(self, run, reason="launcher looked dead"):
        return self.call("abort", run or "-", reason)

    def raw_call(self, *args):
        """A gate command exactly as the command line gives it (no start/end shim)."""
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.g.main(*args)
            return 0, buf.getvalue()
        except SystemExit as e:
            return (e.code if isinstance(e.code, int) else 1 if e.code else 0), buf.getvalue() + (
                "" if isinstance(e.code, int) or e.code is None else str(e.code))

    def test_g1_no_command_but_run_admits_or_closes_a_run(self):
        # review a108fc3 G-1: start, check and end were separate commands that acted on whatever run was current
        self.start()
        for cmd in (("check", "x"), ("end",)):
            rc, out = self.raw_call(*cmd)
            self.assertEqual(rc, 2, f"{cmd[0]} is still a command that acts on the current run: {out.strip()[-200:]}")
        self.assertEqual(self.ends(), [], "a command other than run closed the run")

    # ---- G-1: one launcher owns a run; abort only of a dead one; a late call never touches another run
    def test_g1_abort_while_the_launcher_lives_is_refused_and_a_second_run_blocks(self):
        A = self.launch("a").begin()
        self.assertTrue(A.run_id, f"run A was not admitted: {A.output()[-400:]}")
        rc, out = self.abort(A.run_id)
        self.assertEqual(rc, 2, f"abort closed run A while its launcher is alive: {out.strip()[-300:]}")
        self.assertIn("run lock is held", out)
        B = self.launch("b").begin()
        rcb, outb = B.finish()
        self.assertEqual(rcb, 2, f"run B was admitted while run A's launcher is alive: {outb.strip()[-300:]}")
        rca, outa = A.finish()
        self.assertEqual(rca, 0, outa[-400:])
        self.assertEqual(self.ends(), [(A.run_id, "VALID")], "the ledger's end records are not A's own VALID only")

    def test_g1_abort_while_the_experiment_outlives_its_launcher_is_refused(self):
        A = self.launch("a").begin()
        self.assertTrue(A.run_id, f"run A was not admitted: {A.output()[-400:]}")
        A.kill_launcher()                                  # the launcher dies; its experiment keeps the stores busy
        rc, out = self.abort(A.run_id)
        self.assertEqual(rc, 2, f"abort closed run A while its experiment still runs: {out.strip()[-300:]}")
        self.assertIn("run lock is held", out)
        A.kill_experiment()
        rc, out = self.abort_when_dead(A.run_id)
        self.assertEqual(rc, 0, f"abort refused although run A's launcher and experiment are dead: {out[-300:]}")

    def test_g1_late_call_of_a_dead_launcher_cannot_touch_another_run(self):
        A = self.launch("a").begin()
        self.assertTrue(A.run_id, f"run A was not admitted: {A.output()[-400:]}")
        A.kill_launcher()
        A.kill_experiment()
        rc, out = self.abort_when_dead(A.run_id)
        self.assertEqual(rc, 0, f"abort refused although run A is dead: {out[-300:]}")
        B = self.launch("b").begin()
        self.assertTrue(B.run_id and B.run_id != A.run_id, f"run B was not admitted: {B.output()[-400:]}")
        rc, out = A.late_call()                            # whatever A's launcher could still call
        self.assertNotEqual(rc, 0, f"a late call of run A's launcher succeeded: {out.strip()[-300:]}")
        self.assertIn("run lock is held", out)
        self.assertNotIn(B.run_id, [r for r, _ in self.ends()], "a late call of run A's launcher closed run B")
        rcb, outb = B.finish()
        self.assertEqual(rcb, 0, outb[-400:])
        self.assertEqual(self.ends(), [(A.run_id, "VOID"), (B.run_id, "VALID")])

    def test_g1_abort_naming_another_run_is_refused(self):
        A = self.launch("a").begin()
        self.assertTrue(A.run_id, f"run A was not admitted: {A.output()[-400:]}")
        rc, out = self.abort("r-" + "0" * 32, "wrong run id")
        self.assertEqual(rc, 2, f"abort naming a run that is not open closed run {A.run_id}: {out.strip()[-300:]}")
        self.assertIn("run lock is held", out)
        A.finish()

    def abort_when_dead(self, run, within=15):
        """abort, retried while the killed processes are being reaped (the kernel frees the lock and the group)."""
        t0 = time.monotonic()
        while True:
            rc, out = self.abort(run)
            if rc == 0 or time.monotonic() - t0 > within:
                return rc, out
            time.sleep(0.2)

    # ---- G-2: the tree is the pinned commit, the gate is its gate, fixtures registered, stores clean, rows bound
    def test_g2_tree_differing_from_its_commit_is_refused(self):
        t = self.fresh_tree()
        with open(t / "wp" / "config.py", "a") as f:
            f.write("\n# an uncommitted edit\n")
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "differs from commit")

    def test_g2_untracked_file_in_the_tree_is_refused(self):
        t = self.fresh_tree()
        (t / "exp" / "scratch_helper.py").write_text("X = 1\n")
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "untracked")

    def test_g2_bytecode_cache_in_the_tree_is_refused(self):
        t = self.fresh_tree()
        (t / "wp" / "__pycache__").mkdir()
        (t / "wp" / "__pycache__" / "config.cpython-312.pyc").write_bytes(b"\x00stale")
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "__pycache__")

    def test_g2_dirty_head_is_refused(self):
        t = self.fresh_tree()
        (t / "GIT_HEAD").write_text(COMMIT[:7] + "-dirty\n")
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "dirty")

    def test_g2_gate_that_is_not_the_commits_gate_is_refused(self):
        t = self.fresh_tree()
        m = (t / "TREE.manifest").read_text()
        own = _sha256((t / "exp" / "disk_gate.py").read_bytes())
        (t / "TREE.manifest").write_text(m.replace(own, "0" * 64))
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "is not the gate of commit")

    def test_g2_tree_changed_during_the_run_voids(self):
        t = self.fresh_tree()
        rc, out = self.launch("x", tree=t, ops=[["append", str(t / "wp" / "config.py"), "\n# edited mid-run\n"]]).run()
        self.assertVoid(rc, out, "changed during the run")

    def test_g2_unregistered_fixture_is_refused(self):
        t = self.fresh_tree()
        (t / "exp" / "extra_fixture.sql").write_text("select 1;\n")
        write_manifest(t)                                   # committed with the code, but never registered
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "fixture")

    def test_g2_changed_fixture_is_refused(self):
        t = self.fresh_tree()
        with open(t / "wp" / "schema.sql", "a") as f:
            f.write("\n-- a changed fixture\n")
        write_manifest(t)                                   # committed, but the registered hash was not updated
        rc, out = self.launch("x", tree=t).run()
        self.assertBlocked(rc, out, "fixture")

    def test_g2_rows_left_in_a_table_block(self):
        self.F.locked(lambda st: st.update(tables={"wp_events": {"store_registry": True, "events": True},
                                                   "wp_keys": {"subject_keys": False}}))
        self.assertBlocked(*self.call("start"), "not clean")

    def test_g2_index_left_in_elasticsearch_blocks(self):
        FakeES.S.update(es_indices=["wp16096-t1-events-base"])
        self.assertBlocked(*self.call("start"), "not clean")

    def test_g2_unregistered_store_blocks(self):
        self.F.locked(lambda st: st.update(unregistered=["pg:scratch_table"]))
        self.assertBlocked(*self.call("start"), "not clean")

    def test_g2_row_of_another_run_voids(self):
        rc, out = self.launch("x", rows=[{"name": "r", "gate_run": "r-" + "1" * 32}]).run()
        self.assertVoid(rc, out, "another run")

    def test_g2_row_written_before_the_run_window_voids(self):
        rc, out = self.launch("x", rows=[{"name": "r", "gate_run": "self", "at": "2020-01-01T00:00:00Z"}]).run()
        self.assertVoid(rc, out, "outside the run window")

    def test_g2_row_without_a_time_voids(self):
        rc, out = self.launch("x", rows=[{"name": "r", "gate_run": "self", "at": "yesterday"}]).run()
        self.assertVoid(rc, out, "outside the run window")

    def test_g2_end_record_lists_every_run_file_with_its_hash(self):
        A = self.launch("x", rows=[{"name": "r", "gate_run": "self"}], print="hello from the experiment")
        rc, out = A.run()
        self.assertValid(rc, out)
        rec = [r for r in self.records() if r["kind"] == "end"][-1]
        files = {f["name"]: f["sha256"] for f in rec.get("files") or []}
        self.assertTrue(files, "the end record lists no file of the run")
        d = self.home / "results" / "runs" / A.run_id
        on_disk = {str(p.relative_to(d)): _sha256(p.read_bytes()) for p in d.rglob("*") if p.is_file()}
        self.assertEqual(files, on_disk, "the end record does not list exactly the run directory's files")
        self.assertIn("hello from the experiment", (d / "experiment.out").read_text())

    def test_g2_write_result_stamps_the_run_and_writes_into_its_directory(self):
        import exp.common as common
        d = self.home / "results" / "runs" / "r-test"
        d.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"WP_RUN_DIR": str(d), "WP_GATE_RUN": "r-test"}), \
                mock.patch.object(common, "fingerprint", lambda: {}), redirect_stdout(io.StringIO()):
            _, p = common.write_result("stamp_probe", {"ok": True})
        self.assertEqual(pathlib.Path(p).parent, d, "write_result did not write into the run's directory")
        self.assertEqual(json.loads(pathlib.Path(p).read_text()).get("gate_run"), "r-test",
                         "write_result did not stamp the gate's run id")

    def test_p11_process_outliving_the_experiment_voids(self):
        rc, out = self.launch("x", ops=[["linger"]]).run()
        self.assertVoid(rc, out, "outlived")

    def test_p10_two_runs_with_one_label_keep_both_records(self):
        A = self.launch("same", rows=[{"name": "r", "gate_run": "self"}])
        self.assertValid(*A.run())
        d = self.home / "results" / "runs" / str(A.run_id)
        before = {str(p): p.read_bytes() for p in d.rglob("*") if p.is_file()}
        self.assertTrue(before, "run A left no record directory")
        B = self.launch("same", rows=[{"name": "r", "gate_run": "self"}])
        self.assertValid(*B.run())
        self.assertNotEqual(A.run_id, B.run_id)
        self.assertEqual({str(p): p.read_bytes() for p in d.rglob("*") if p.is_file()}, before,
                         "the second run with the same label changed the first run's record")

    def test_g3_experiment_text_never_becomes_the_verdict(self):
        # the experiment prints what the old launcher's verdict lines looked like, and a stop lands in its window
        A = self.launch("x", print="gate=VALID\nend 2026-11-18T00:00:00Z",
                        ops=[["gate", "stop", "operator stop in the window"]])
        rc, out = A.run()
        self.assertVoid(rc, out, "STOP")
        self.assertNotIn("end 2026-11-18T00:00:00Z", out, "the experiment's text reached the gate's own output")

    # ---- m-1: the probe's output cannot be forged through a crafted name
    def test_m1_forged_probe_lines_through_a_crafted_link_name_block(self):
        dev = str(os.makedev(0, 50))
        name = f"{PGDATA}/x\nP\t{dev}\t/srv/pgdata\t/srv/pgdata\nP\t{dev}\t/srv/pgdata/pg_wal\t/srv/pgdata/pg_wal"
        self.F.locked(lambda st: st["c"][self.pg].update(links=[[name, PGDATA]]) or
                      st["c"][self.pg]["pgsrv"].update(dd="/srv/pgdata"))
        self.assertBlocked(*self.call("start"), "not on the private tmpfs")

    # ---- m-2: the drift baseline is not a file the run can rewrite
    def test_m2_baseline_rewritten_by_the_run_does_not_hide_a_replaced_store(self):
        self.start()
        new = self.F.replace("emlab2-wphard-pg")
        old = self.pg                                      # a copy of the old cluster: the same system identifier
        self.F.locked(lambda st: st["c"][new]["pgsrv"].update(sysid=st["c"][old]["pgsrv"]["sysid"]))
        p = self.home / "state" / "disk_gate.json"
        if p.exists():                                     # what the run's own OS user can do to a v4 state file
            st = json.loads(p.read_text())
            st["obs"]["pg"]["id"] = new
            st["obs"]["pg"]["started"] = self.F.st()["c"][new]["started"]
            p.write_text(json.dumps(st))
        self.assertVoid(*self.call("end"), "replaced")

    # ---- G-1, abort's two checks apart (the lock, then the process group), and an older gate's open run
    def group_of(self, pgid):
        r = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "comm="], capture_output=True, text=True)
        return [x.split(None, 2) for x in r.stdout.splitlines() if len(x.split(None, 2)) == 3 and x.split()[1] == str(pgid)]

    def test_g1_abort_with_the_lock_free_but_a_live_group_member_is_refused(self):
        self.need_v5()
        A = self.launch("a", ops=[["linger"]]).begin()
        self.assertTrue(A.run_id, f"run A was not admitted: {A.output()[-400:]}")
        pg = A.p.pid                                       # the gate leads its own process group
        self.addCleanup(lambda: _killpg_quiet(pg))
        t0 = time.monotonic()
        while not any(c[2].endswith("sleep") for c in self.group_of(pg)) and time.monotonic() - t0 < 30:
            time.sleep(0.05)
        A.kill_launcher()
        A.kill_experiment()                                # the lock dies with them; the `sleep` stays in the group
        t0 = time.monotonic()
        while True:
            rc, out = self.abort(A.run_id)
            if "run lock is held" not in out or time.monotonic() - t0 > 15:
                break
            time.sleep(0.2)
        self.assertEqual(rc, 2, f"abort closed run A while a member of its process group lives: {out.strip()[-300:]}")
        self.assertIn("process group", out)
        _killpg_quiet(pg)
        rc, out = self.abort_when_dead(A.run_id)
        self.assertEqual(rc, 0, f"abort refused although run A's whole group is dead: {out[-300:]}")

    def test_g1_abort_checks_the_run_id_and_then_the_group(self):
        self.need_v5()
        self.start()                                       # admitted in this process: the group is this test's
        self._run["lock"].close()                          # the lock alone no longer refuses
        rc, out = self.abort("r-" + "0" * 32, "wrong run id")
        self.assertEqual(rc, 2, out[-300:])
        self.assertIn("is not the open run", out)
        rc, out = self.abort(self._run["run"], "right run id, live group")
        self.assertEqual(rc, 2, out[-300:])
        self.assertIn("process group", out)
        self.assertEqual(self.ends(), [], "abort closed a run")

    def test_g1_abort_of_an_older_gates_open_run_is_refused(self):
        old = "r-" + "a" * 32
        with self.g.Ledger() as L:
            L.append("admit", v=4, gate="disk_gate v4", run=old, label="old", pid=1)
        rc, out = self.abort(old, "the old launcher is gone")
        self.assertEqual(rc, 2, f"abort closed an older gate's run it cannot observe dead: {out.strip()[-300:]}")
        self.assertIn("older gate", out)
        self.assertEqual(self.ends(), [])
        self.assertBlocked(*self.call("start"))

    # ---- branches of admission and of `run` (review a108fc3 asked for a gate whose every refusal is tested)
    def test_p7_file_in_the_log_store_blocks(self):
        logs = self.home / "logs"
        logs.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, logs, True)
        (logs / "left.log").write_text("x\n")
        self.assertBlocked(*self.call("start"), "log store")

    def test_p4_tree_not_directly_under_runs_is_refused(self):
        t = pathlib.Path(self.tmp) / "elsewhere" / self.tree.name
        shutil.copytree(self.tree, t, symlinks=True)
        self.addCleanup(shutil.rmtree, t.parent, True)
        self.assertBlocked(*self.launch("x", tree=t).run(), "not from a pinned tree directly under WP_HOME/runs")

    def test_p4_missing_manifest_is_refused(self):
        t = self.fresh_tree()
        (t / "TREE.manifest").unlink()
        self.assertBlocked(*self.launch("x", tree=t).run(), "no readable TREE.manifest")

    def test_p4_malformed_manifest_is_refused(self):
        t = self.fresh_tree()
        (t / "TREE.manifest").write_text("tree-manifest v0\ncommit %s\n" % COMMIT)
        self.assertBlocked(*self.launch("x", tree=t).run(), "is not a tree-manifest v1")

    def test_p4_module_loaded_from_outside_the_tree_is_refused(self):
        with mock.patch.dict(sys.modules, {"wp.zz_outside": SimpleNamespace(__file__="/elsewhere/wp/zz_outside.py")}):
            self.assertBlocked(*self.call("start"), "loaded from outside the pinned tree")

    def test_p4_bytecode_writing_on_is_refused(self):
        with mock.patch.object(sys, "dont_write_bytecode", False):
            self.assertBlocked(*self.call("start"), "bytecode writing on")

    def test_run_refuses_a_module_outside_the_tree_and_a_bad_label(self):
        rc, out = self.raw_call("run", "x", "json")
        self.assertEqual(rc, 2, out[-300:])
        self.assertIn("is not a module of the pinned tree", out)
        rc, out = self.raw_call("run", "-bad label", "stubexp")
        self.assertEqual(rc, 2, out[-300:])
        self.assertIn("usage: run LABEL MODULE", out)
        self.assertEqual(self.ends(), [])

    def test_p3_sigterm_during_the_run_voids(self):
        self.need_v5()
        A = self.launch("x").begin()
        self.assertTrue(A.run_id, f"run A was not admitted: {A.output()[-400:]}")
        os.kill(A.p.pid, signal.SIGTERM)
        self.assertVoid(*A.finish(), "signalled")

    def test_verdict_exit_codes(self):
        A = self.launch("a", rows=[{"name": "r", "gate_run": "self"}])
        self.assertValid(*A.run())
        B = self.launch("b", rows=[{"name": "r", "gate_run": "r-" + "1" * 32}])
        self.assertVoid(*B.run())
        rc, out = self.raw_call("verdict", A.run_id)
        self.assertEqual(rc, 0, out[-300:])
        self.assertNotIn("VOID", out)
        rc, out = self.raw_call("verdict", B.run_id)
        self.assertEqual(rc, 3, out[-300:])
        self.assertIn("another run", out)
        rc, out = self.raw_call("verdict", "r-" + "2" * 32)
        self.assertEqual(rc, 2, out[-300:])
        self.assertIn("no run", out)
        self.start()
        rc, out = self.raw_call("verdict", self._run["run"])
        self.assertEqual(rc, 2, out[-300:])
        self.assertIn("is open", out)

    # ---- m-4
    def test_m4_missing_startedat_at_end_voids(self):
        self.start()
        self.F.flag(no_started=True)
        self.assertVoid(*self.call("end"), "StartedAt")

    # ---- m-6
    def test_m6_every_gate_pg_connection_sets_a_statement_timeout(self):
        self.start()
        sql = self.F.st().get("pg_sql", [])
        self.assertTrue(sql, "the gate sent no PG statement")
        first = {}
        for db, s in sql:
            first.setdefault(db, s)
        for db, s in first.items():
            self.assertRegex(s, r"(?i)^\s*set\s+statement_timeout",
                             f"the gate's first statement on {db} does not set a statement timeout")

    # ---- the four surviving mutants of review a108fc3
    def test_mutant_oom_kill_during_the_slow_end_probes_voids(self):
        # fast_before_slow: identity and counters must be read after the slow probes (B6, F18)
        self.start()
        self.F.trigger("rm", 1, "oom", self.pg, 1)         # the next nonce removal: end's PG slow phase
        self.assertVoid(*self.call("end"), "OOM")

    def test_mutant_es_curl_failure_inside_the_container_blocks(self):
        self.F.flag(curl_rc=1)                             # es_bind_pass_on_curl_failure
        self.assertBlocked(*self.call("start"), "could not be asked")

    def test_mutant_nonce_write_failure_blocks(self):
        self.F.flag(nonce_rc=1)                            # nonce_write_failure_passes
        self.assertBlocked(*self.call("start"), "nonce could not be written")

    def test_mutant_inode_count_without_the_limit_option_blocks(self):
        self.F.set(self.es, inodes=[1048576, 1048000])     # inodes_statfs_ignored: nr_inodes=0 in the options only
        self.assertBlocked(*self.call("start"), "inode")

    # ============================================================== review aacf3c0 (gate v6): VERDICT-PATHS.md conjuncts
    def assertOnly(self, rc, out, code, *why):
        """Exit `code` (2 BLOCKED, 3 VOID) and every stated reason matches one of `why`: the scenario violates the
        conjunct under test and nothing else, so the test cannot pass on an incidental reason."""
        kind = {2: "BLOCKED", 3: "VOID"}[code]
        self.assertEqual(rc, code, f"expected {kind} (exit {code}), got {rc!r}: {out.strip()[-600:]}")
        lines = [x.split(f"disk_gate {kind}: ", 1)[1] for x in out.splitlines() if x.startswith(f"disk_gate {kind}: ")]
        self.assertTrue(lines, f"{kind} without a stated reason: {out.strip()[-400:]}")
        for x in lines:
            self.assertTrue(any(w in x for w in why), f"{kind} for a reason other than the one under test: {x[:300]}")

    def tools(self):
        return self.F.st().get("tools", [])

    # ---- C11, amendment GF1 (answer 3335a07a): start at >= 10 GB free, stop at 8 GB
    def test_v6_gf1_run_is_admitted_at_12_gb_free(self):
        self.free = lambda: 12
        self.start()
        self.assertValid(*self.call("end"))

    def test_v6_gf1_admission_under_10_gb_free_blocks(self):
        self.free = lambda: 9.5
        self.assertOnly(*self.call("start"), 2, "host free 9.5 GiB < 10")

    def test_v6_gf1_run_stops_itself_when_free_falls_under_8_gb(self):
        A = self.launch("x").begin()
        self.assertTrue(A.run_id, f"run was not admitted: {A.output()[-400:]}")
        A.free_file.write_text("7.5")
        try:
            A.p.wait(timeout=30)                           # without the release: the gate itself must stop the run
        except subprocess.TimeoutExpired:
            self.fail("host free fell to 7.5 GiB and the gate did not stop the experiment within 30 s")
        self.assertOnly(*A.finish(release=False), 3, "host free fell to 7.5", "host free 7.5 GiB < 8")

    # ---- C1, C5 (F-1): the reset and cleanup run inside this run's lock; a reset during the run voids
    def test_v6_f1_reset_and_cleanup_run_inside_the_run_lock(self):
        self.start()
        self.assertEqual(self.tools(), [["admission", ["cleanup", "reset"], True]],
                         "admission did not run exp.cleanup and wp.setup --reset itself, under the run lock")
        self.assertValid(*self.call("end"))
        self.assertEqual(self.tools()[1:], [["after", ["cleanup-after"], True]],
                         "the end did not delete the stream's indices before releasing the run lock")

    def test_v6_f1_failed_reset_blocks(self):
        self.F.flag(tools_fail_admission=True)
        self.assertOnly(*self.call("start"), 2, "failed under the run lock")

    def test_v6_f1_store_reset_during_the_run_voids(self):
        self.start()
        self.F.reset_db("wp_events")                       # wp.setup --reset by a command that ignores the lock
        self.assertOnly(*self.call("end"), 3, "was reset or replaced during the run", "catalog wp_events.")

    def _tool_stub(self, name):
        return ('"""Test stand-in for %s: takes the run lock as the real tool does, then records how it was run."""\n'
                "import json, os, sys\nfrom wp import runlock\nrunlock.hold(%r)\n"
                "with open(os.environ['STUB_TOOLS_LOG'], 'a') as f:\n"
                "    f.write(json.dumps({'tool': %r, 'argv': sys.argv[1:], 'isolated': sys.flags.isolated,\n"
                "                        'fd': bool(os.environ.get('WP_RUN_LOCK_FD'))}) + '\\n')\n") % (name, name, name)

    def test_v6_f1_gate_runs_the_real_tools_isolated_with_its_lock(self):
        t = self.fresh_tree()
        (t / "exp" / "cleanup.py").write_text(self._tool_stub("exp.cleanup"))
        (t / "wp" / "setup.py").write_text(self._tool_stub("wp.setup"))
        write_manifest(t)
        log = pathlib.Path(tempfile.mkdtemp(dir=self.tmp)) / "tools.jsonl"
        rc, out = self.launch("x", tree=t, env={"FAKE_TOOLS": "real", "STUB_TOOLS_LOG": str(log)}).run()
        self.assertValid(rc, out)
        calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        self.assertEqual([(c["tool"], c["argv"]) for c in calls],
                         [("exp.cleanup", ["gate-admission"]), ("wp.setup", ["--reset"]),
                          ("exp.cleanup", ["gate-after-end"])], "the gate did not run the store tools as designed")
        self.assertTrue(all(c["isolated"] and c["fd"] for c in calls), f"a tool ran without -I or the lock: {calls}")

    def test_v6_f1_setup_and_cleanup_refuse_while_a_run_holds_the_lock(self):
        stubs = pathlib.Path(tempfile.mkdtemp(dir=self.tmp))
        (stubs / "psycopg").mkdir()
        marker = stubs / "connected"
        (stubs / "psycopg" / "__init__.py").write_text(
            "import pathlib\nfrom . import sql\n"
            f"def connect(*a, **k):\n    pathlib.Path({str(marker)!r}).write_text('x')\n"
            "    raise RuntimeError('test stand-in: no server')\n")
        (stubs / "psycopg" / "sql.py").write_text("SQL = Identifier = Literal = str\n")

        def tool(mod, *args):
            code = ("import runpy, sys; sys.path[:0] = [%r, %r]; sys.argv = [%r, *%r]; "
                    "runpy.run_module(%r, run_name='__main__')") % (str(stubs), str(self.tree), mod, list(args), mod)
            r = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tree, capture_output=True, text=True,
                               timeout=120, env=dict(os.environ, PYTHONPATH=""))
            return r.returncode, r.stdout + r.stderr
        lk = self.g._take_run_lock()                       # what a live run holds
        try:
            for mod, args in (("wp.setup", ("--reset",)), ("exp.cleanup", ("manual",))):
                rc, out = tool(mod, *args)
                self.assertEqual(rc, 2, f"{mod} ran while a run holds the run lock: {out.strip()[-300:]}")
                self.assertIn("refused", out)
            self.assertFalse(marker.exists(), "wp.setup connected to the cluster while a run holds the run lock")
        finally:
            lk.close()
        rc, out = tool("wp.setup", "--reset")              # the lock free: setup goes on (to the stand-in server)
        self.assertTrue(marker.exists(), f"wp.setup refused with the run lock free: {out.strip()[-300:]}")

    # ---- C9 (F-2): the experiment ended by itself, with no OOM kill in the gate's cgroup
    def test_v6_f2_experiment_killed_by_a_signal_the_gate_did_not_send_voids(self):
        rc, out = self.launch("x", ops=[["selfkill", int(signal.SIGKILL)]]).run()
        self.assertOnly(rc, out, 3, "killed by signal 9")

    def test_v6_f2_oom_kill_in_the_gates_cgroup_voids(self):
        rc, out = self.launch("x", ops=[["fake", "gate_oom"]]).run()
        self.assertOnly(rc, out, 3, "OOM kill in the gate's cgroup")

    def test_v6_f2_unreadable_own_cgroup_blocks(self):
        (self.F.root / "proc" / "self" / "cgroup").unlink()
        self.assertOnly(*self.call("start"), 2, "memory cannot be observed")

    def test_v6_f2_nonzero_exit_by_itself_stays_valid(self):
        A = self.launch("x", rc="3")
        self.assertValid(*A.run())
        end = [r for r in self.records() if r["kind"] == "end"][-1]
        self.assertEqual(end.get("exp_rc"), 3)

    # ---- C6 (F-3): the live catalogs equal the committed expectation, and do not change during the run
    def test_v6_f3_role_attribute_weakened_before_the_reset_blocks(self):
        self.F.cat("cluster", "roles", "set", "wp_app", "True", "True", "False", "False", "True", "False", "False")
        self.assertOnly(*self.call("start"), 2, "before the store reset: role wp_app has rolsuper=True")
        self.assertEqual(self.tools(), [], "the reset ran (and would have repaired the role silently)")

    def test_v6_f3_weakened_role_the_reset_would_repair_still_blocks(self):
        # the reset re-applies the role's attributes, so only the check before it can see that the role was weakened
        # (mutant pre_reset_check_skipped admits this run)
        self.F.flag(reset_repairs_roles=True)
        self.F.cat("cluster", "roles", "set", "wp_app", "True", "True", "False", "False", "True", "False", "False")
        self.assertOnly(*self.call("start"), 2, "before the store reset: role wp_app has rolsuper=True")

    def test_v6_f3_default_acl_added_during_the_run_voids(self):
        # no committed rule names default ACLs: only the comparison with admission's catalog sees it
        self.start()
        self.F.cat("wp_events", "default_acl", "add", "wp_owner", "public", "r", "{wp_app=arwd/wp_owner}")
        self.assertOnly(*self.call("end"), 3, "catalog wp_events.default_acl changed")

    def test_v6_f3_policy_added_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "policies", "add", "events", "p_all", "ALL", "{public}", "true", "true")
        self.assertOnly(*self.call("end"), 3, "catalog wp_events.policies changed")

    def bootstrap_cluster(self):
        """What host/bootstrap.py (committed in the same tree) leaves on the cluster: the legacy login roles emlab and
        kms, kms in pg_checkpoint, their databases emlab and keys, and the node's ALTER SYSTEM settings."""
        for r in ("emlab", "kms"):
            self.F.cat("cluster", "roles", "add", r, "False", "True", "False", "False", "True", "False", "False")
        self.F.cat("cluster", "members", "add", "pg_checkpoint", "kms")
        self.F.cat("cluster", "databases", "add", "emlab", "30001", "True", "emlab", "")
        self.F.cat("cluster", "databases", "add", "keys", "30002", "True", "kms", "")
        for k, v in (("archive_mode", "off"), ("max_connections", "200"), ("max_wal_size", "256MB"),
                     ("min_wal_size", "64MB"), ("wal_keep_size", "0"), ("wal_recycle", "off")):
            self.F.cat("cluster", "auto_conf", "add", k, v, "True")

    def test_v6_f3_cluster_as_bootstrap_leaves_it_is_admitted(self):
        # live item GF16096-L1 (t2-dev, 2026-11-18): v6 at 8dbc368 BLOCKED a cluster host/bootstrap.py had built
        self.bootstrap_cluster()
        self.start()
        self.assertValid(*self.call("end"))

    def test_v6_f3_legacy_role_weakened_blocks(self):
        self.bootstrap_cluster()
        self.F.cat("cluster", "roles", "set", "kms", "True", "True", "False", "False", "True", "False", "False")
        self.assertOnly(*self.call("start"), 2, "before the store reset: role kms has rolsuper=True")

    def test_v6_f3_alter_system_value_other_than_bootstraps_blocks(self):
        self.bootstrap_cluster()
        self.F.cat("cluster", "auto_conf", "set", "max_wal_size", "64GB", "True")
        self.assertOnly(*self.call("start"), 2, "an ALTER SYSTEM setting that is not committed: max_wal_size = 64GB")

    def test_v6_f3_extra_membership_blocks(self):
        self.F.cat("cluster", "members", "add", "pg_read_server_files", "wp_app")
        self.assertOnly(*self.call("start"), 2, "member of pg_read_server_files")

    def test_v6_f3_parameter_privilege_blocks(self):
        self.F.cat("cluster", "parameter_acl", "add", "session_replication_role", "{wp_app=s/wp}")
        self.assertOnly(*self.call("start"), 2, "parameter privilege")

    def test_v6_f3_schema_drift_after_the_reset_blocks(self):
        # a schema the reset did not produce (a guard missing from the schema files): the expectation, not the reset
        self.F.cat("wp_events", "triggers", "drop", "events", "events_no_truncate")
        self.assertOnly(*self.call("start"), 2, "G3 table events has no guard trigger for TRUNCATE")

    def test_v6_f3_delete_grant_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "app_privs", "add", "events", "DELETE")
        self.assertOnly(*self.call("end"), 3, "G2 wp_app on events", "catalog wp_events.app_privs changed")

    def test_v6_f3_guard_trigger_disabled_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "triggers", "drop", "events", "events_guard")
        self.F.cat("wp_events", "triggers", "add", "events", "events_guard",
                   str(T_BEFORE | T_ROW | T_INS | T_UPD | T_DEL), "D", "wp_guard")
        self.assertOnly(*self.call("end"), 3, "G3 trigger events.events_guard is enabled 'D'",
                        "catalog wp_events.triggers changed")

    def test_v6_f3_role_setting_changed_during_the_run_voids(self):
        self.start()
        self.F.cat("cluster", "settings", "set", "wp_app", "", "statement_timeout=0")
        self.assertOnly(*self.call("end"), 3, "role wp_app has settings", "catalog cluster.settings changed")

    def test_v6_f3_scratch_database_during_the_run_voids(self):
        self.start()
        self.F.cat("cluster", "databases", "add", "scratch", "20000", "True", "wp_app", "")
        self.assertOnly(*self.call("end"), 3, "a database outside the committed set: scratch",
                        "catalog cluster.databases changed")

    def test_v6_f3_alter_system_during_the_run_voids(self):
        self.start()
        self.F.cat("cluster", "auto_conf", "add", "fsync", "off", "True")
        self.assertOnly(*self.call("end"), 3, "ALTER SYSTEM", "catalog cluster.auto_conf changed")

    def test_v6_f3_constraint_dropped_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "constraints", "drop", "events", "events_pkey")
        self.assertOnly(*self.call("end"), 3, "catalog wp_events.constraints changed")

    def test_v6_f3_unreadable_catalog_blocks(self):
        self.F.flag(catalog_fail=True)
        self.assertOnly(*self.call("start"), 2, "live catalogs could not be read")

    def test_v6_f3_unreadable_catalog_at_end_voids(self):
        self.start()
        self.F.flag(catalog_fail=True)
        self.assertOnly(*self.call("end"), 3, "live catalogs could not be read")

    def test_v6_f3_change_reverted_within_the_run_is_a_stated_limit(self):
        # LIMITS v6: a catalog change made and reverted between two reads is not seen; the run stays VALID
        self.start()
        self.F.cat("wp_events", "app_privs", "add", "events", "DELETE")
        self.F.cat_restore("wp_events", "app_privs")
        self.assertValid(*self.call("end"))
        self.assertIn("a catalog change made and reverted within the run", self.g.LIMITS)

    def _gp(self):
        p = self.tree / "exp" / "guard_policy.py"
        if not p.exists():
            self.fail("the pinned tree has no committed catalog expectation (exp/guard_policy.py)")
        spec = importlib.util.spec_from_file_location("guard_policy_under_test", p)
        GP = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(GP)
        return GP

    def test_v6_guard_policy_is_bound_to_setup(self):
        GP = self._gp()
        setup = (self.tree / "wp" / "setup.py").read_text()
        dbpy = (self.tree / "wp" / "db.py").read_text()
        m = re.search(r"^APP, VERIFIER, KMS, LEGAL, WATCHDOG, BROKER = (.+)$", dbpy, re.M)
        names = dict(zip(("APP", "VERIFIER", "KMS", "LEGAL", "WATCHDOG", "BROKER"), re.findall(r'"(\w+)"', m.group(1))))
        # integ: the hardening's roles (db.py SEALER, REVIEWER_GROUP, REVIEWERS)
        names["SEALER"] = re.search(r'^SEALER = "(\w+)"', dbpy, re.M).group(1)
        group = re.search(r'^REVIEWER_GROUP = "(\w+)"', dbpy, re.M).group(1)
        reviewers = tuple(re.findall(r'"(\w+)"', re.search(r"^REVIEWERS = \((.+)\)", dbpy, re.M).group(1)))
        login = re.search(r"^LOGIN_ROLES = \((.+)\)( \+ db\.REVIEWERS)?$", setup, re.M)
        want = {names[x.strip()[3:]] for x in login.group(1).split(",")} | (set(reviewers) if login.group(2) else set())
        self.assertEqual(set(GP.LOGIN_ROLES), want, "login roles in setup.py and exp/guard_policy.py differ")
        self.assertIn("create role {} nologin", setup)
        self.assertEqual(tuple(GP.GROUP_ROLES), (group,), "the NOLOGIN group of setup.py is not the committed group")
        attrs = re.search(r'^ROLE_ATTRS = "(.+)"$', setup, re.M).group(1).split()
        word = {"superuser": "rolsuper", "inherit": "rolinherit", "createrole": "rolcreaterole",
                "createdb": "rolcreatedb", "replication": "rolreplication", "bypassrls": "rolbypassrls"}
        flags = {}
        for a in attrs:
            neg = a.startswith("no") and a[2:] in word
            flags[word[a[2:] if neg else a]] = not neg
        self.assertEqual(GP.ROLE_FLAGS, flags, "role attributes in setup.py and exp/guard_policy.py differ")
        tmo = dict(re.findall(r'"(\w+_timeout)": "(\w+)"', re.search(r"^ROLE_TIMEOUTS = \{.+\}$", setup, re.M).group(0)))
        tmo.update(re.findall(r"set (lock_timeout) = '(\w+)'", setup))
        self.assertEqual(GP.ROLE_SETTINGS, tmo, "role settings in setup.py and exp/guard_policy.py differ")
        mem = set()
        for granted, member in re.findall(r'"grant ((?:pg_\w+, )*pg_\w+) to (\w+) with inherit true"', setup):
            mem |= {(g, member) for g in granted.split(", ")}
        if re.search(r"for r in db\.REVIEWERS:\n\s+admin\.execute\(sql\.SQL\(\"grant \{\} to \{\}\"\)\.format\("
                     r"sql\.Identifier\(db\.REVIEWER_GROUP\), sql\.Identifier\(r\)\)\)", setup):
            mem |= {(group, r) for r in reviewers}
        self.assertEqual(GP.MEMBERSHIPS, mem, "role memberships in setup.py and exp/guard_policy.py differ")
        conn = {}
        for k, v, extra in re.findall(r"grant connect on database \"\{config\.(EVENTS|KEYS)_DB\}\" to ([\w, ]+?),? ?'"
                                      r"( \+ ', '\.join\(db\.REVIEWERS\))?", setup):
            conn[k.lower()] = tuple(sorted([x.strip() for x in v.split(",") if x.strip()]
                                           + (list(reviewers) if extra else [])))
        self.assertEqual({k: tuple(sorted(v)) for k, v in GP.CONNECT.items()}, conn,
                         "database CONNECT grants in setup.py and exp/guard_policy.py differ")
        temp = "revoke temporary on database \"{d}\" from public" in setup
        self.assertEqual(GP.TEMPORARY, {"events": (), "keys": ()} if temp else GP.TEMPORARY)
        self.assertTrue(temp, "setup.py no longer revokes TEMPORARY from PUBLIC (exp/guard_policy.py TEMPORARY)")
        self.assertIn('create role wp_owner nologin', setup)
        self.assertEqual(GP.OWNER_ROLE, "wp_owner")
        # this test file's scenario catalog is the committed expectation, so a clean scenario is a clean cluster
        self.assertEqual({t: {T_PRIV[c] for c in v} for t, v in T_POLICY.items()}, GP.POLICY)
        self.assertEqual(sorted(T_LOGIN), sorted(GP.LOGIN_ROLES))
        self.assertEqual(sorted(T_GROUP), sorted(GP.GROUP_ROLES))

    def test_integ_guard_policy_is_bound_to_schema(self):
        # the privilege half of exp/guard_policy.py against wp/schema.sql's own text: tables, guard trigger lists,
        # grants of tables, schemas and functions, and the private seal schema
        GP = self._gp()
        sql = re.sub(r"--[^\n]*", "", (self.tree / "wp" / "schema.sql").read_text())
        tables = set(re.findall(r"^create table (\w+) \(", sql, re.M))
        self.assertEqual(set(GP.POLICY), tables, "the public tables of schema.sql and POLICY differ")
        lists = re.findall(r"foreach t in array array\[([^\]]+)\] loop\s+execute format\('create trigger", sql)
        row, trunc = [set(re.findall(r"'(\w+)'", x)) for x in (lists[0], lists[2])] if len(lists) == 3 else (set(), set())
        self.assertEqual(len(lists), 3, "schema.sql's guard trigger lists are not the three this test reads")
        self.assertEqual(tables - trunc, set(GP.UNGUARDED_OK),
                         "the tables without a TRUNCATE guard in schema.sql are not exactly the named exemption")
        for t in GP.UNGUARDED_OK:
            self.assertNotIn(t, row, f"{t} has guard triggers now: remove its exemption from exp/guard_policy.py")
            w = GP.UNGUARDED_OK[t]
            self.assertRegex(sql, rf"create function {w}\([^)]*\) returns \w+ language plpgsql security definer",
                             f"{t}'s writer {w} is not SECURITY DEFINER in schema.sql")
        grants = [" ".join(g.split()) for g in re.findall(r"^\s*(grant [^;]+;)", sql, re.M | re.S)]   # DO blocks too
        members = {GP.GROUP_ROLES[0]: {m for g, m in GP.MEMBERSHIPS if g == GP.GROUP_ROLES[0]}}

        def grantees(text):
            out = set()
            for r in (x.strip() for x in text.split(",")):
                out |= {r} | members.get(r, set())
            return out
        tp = {}
        for g in grants:
            m = re.match(r"grant ([\w, ]+?) on ((?!function|schema|type|sequence|all )[\w, ]+) to ([\w, ]+);$", g)
            if m and "(" not in m.group(1):
                for r in grantees(m.group(3)):
                    for t in (x.strip() for x in m.group(2).split(",")):
                        tp.setdefault(r, {}).setdefault(f"public.{t}", set()).update(
                            x.strip().upper() for x in m.group(1).split(","))
        self.assertIn(f"grant select on all tables in schema public to {GP.VERIFIER_READS_PUBLIC};", grants)
        tp.pop("wp_app", None)                       # G2's (POLICY), bound above
        self.assertEqual({r: {t: set(p) for t, p in v.items()} for r, v in GP.ROLE_TABLE_PRIVS.items()}, tp,
                         "table grants of schema.sql and ROLE_TABLE_PRIVS differ")
        usage = set()
        for g in grants:
            m = re.match(r"grant usage on schema public to ([\w, ]+);$", g)
            usage |= grantees(m.group(1)) if m else set()
        self.assertEqual(GP.SCHEMA_PRIVS[("public", "USAGE")], usage, "USAGE on schema public differs")
        self.assertFalse([g for g in grants if re.match(r"grant (create|all)[\w, ]* on schema", g)])
        ex = {}
        for g in grants:
            m = re.match(r"grant execute on function (.+) to ([\w, ]+);$", g)
            if m:
                for f in re.findall(r"(\w+)\(", m.group(1)):
                    ex.setdefault(f"public.{f}", set()).update(grantees(m.group(2)))
        for f, w in GP.EXECUTE_ONLY.items():
            if f.startswith("public."):
                self.assertEqual(w, ex.get(f, set()), f"EXECUTE on {f} in schema.sql and EXECUTE_ONLY differ")
        s = GP.SEAL_SCHEMA
        self.assertEqual(set(GP.SEAL_TABLES), set(re.findall(rf"^create table {s}\.(\w+) \(", sql, re.M)))
        self.assertEqual(set(GP.SEAL_FUNCTIONS), set(re.findall(rf"^create function {s}\.(\w+)\(", sql, re.M)))
        self.assertFalse(re.search(rf"create function {s}\.\w+\([^)]*\)[^$]*security definer", sql))
        for what in ("schema", "all tables in schema", "all functions in schema"):
            self.assertIn(f"revoke all on {what} {s} from public;", sql)
        self.assertFalse([g for g in grants if f" {s}." in g or f"schema {s} " in g], "a grant on the seal schema")

    # ---- integ/gate-on-hardening: the hardening objects named and held (exp/guard_policy.py); red on the plain merge
    def assertExactly(self, rc, out, code, *why):
        """assertOnly, and every stated `why` is matched by a reason: the deviation under test is named."""
        self.assertOnly(rc, out, code, *why)
        kind = {2: "BLOCKED", 3: "VOID"}[code]
        lines = [x.split(f"disk_gate {kind}: ", 1)[1] for x in out.splitlines() if x.startswith(f"disk_gate {kind}: ")]
        for w in why:
            self.assertTrue(any(w in x for x in lines), f"no {kind} reason names {w!r}: {lines}")

    def acl(self, db, add=(), drop=()):
        """Change a stream database's ACL in the scenario (grantee:privilege items)."""
        row = next(r for r in self.F.st()["catalog"]["cluster"]["databases"] if r[0] == db)
        items = (set(row[4].split(",")) | set(add)) - set(drop)
        self.F.cat("cluster", "databases", "set", *row[:4], ",".join(sorted(items)))

    def test_integ_cluster_as_the_hardening_setup_leaves_it_is_admitted(self):
        self.start()
        self.assertValid(*self.call("end"))

    def test_integ_hardening_and_bootstrap_cluster_is_admitted(self):
        self.bootstrap_cluster()
        self.start()
        self.assertValid(*self.call("end"))

    def test_integ_sealer_attribute_changed_blocks(self):
        self.F.cat("cluster", "roles", "set", "wp_sealer", "False", "True", "False", "True", "True", "False", "False")
        self.assertExactly(*self.call("start"), 2, "before the store reset: role wp_sealer has rolcreatedb=True")

    def test_integ_reviewer_with_bypassrls_blocks(self):
        self.F.cat("cluster", "roles", "set", "wp_rev2", "False", "True", "False", "False", "True", "False", "True")
        self.assertExactly(*self.call("start"), 2, "before the store reset: role wp_rev2 has rolbypassrls=True")

    def test_integ_reviewer_group_that_can_log_in_blocks(self):
        self.F.cat("cluster", "roles", "set", "wp_reviewer", "False", "True", "False", "False", "True", "False", "False")
        self.assertExactly(*self.call("start"), 2, "before the store reset: role wp_reviewer has rolcanlogin=True")

    def test_integ_extra_member_of_the_reviewer_group_blocks(self):
        self.F.cat("cluster", "members", "add", "wp_reviewer", "wp_app")
        self.assertExactly(*self.call("start"), 2, "before the store reset: role wp_app is a member of wp_reviewer")

    def test_integ_reviewer_membership_missing_blocks(self):
        self.F.cat("cluster", "members", "drop", "wp_reviewer", "wp_rev2")
        self.assertExactly(*self.call("start"), 2, "committed membership of wp_rev2 in wp_reviewer is missing")

    def test_integ_setting_on_the_reviewer_group_blocks(self):
        self.F.cat("cluster", "settings", "add", "wp_reviewer", "", "statement_timeout=0")
        self.assertExactly(*self.call("start"), 2, "before the store reset: a setting of role wp_reviewer")

    def test_integ_sealer_setting_changed_during_the_run_voids(self):
        self.start()
        self.F.cat("cluster", "settings", "set", "wp_sealer", "", "lock_timeout=0")
        self.assertExactly(*self.call("end"), 3, "role wp_sealer has settings lock_timeout=0",
                           "catalog cluster.settings changed")

    def test_integ_reviewer_group_connect_blocks(self):
        self.acl("wp_events", add=["wp_reviewer:CONNECT"])
        self.assertExactly(*self.call("start"), 2, "wp_reviewer may connect to or create in the events database")

    def test_integ_sealer_connect_to_the_key_store_blocks(self):
        self.acl("wp_keys", add=["wp_sealer:CONNECT"])
        self.assertExactly(*self.call("start"), 2, "wp_sealer may connect to or create in the keys database")

    def test_integ_sealer_connect_missing_blocks(self):
        self.acl("wp_events", drop=["wp_sealer:CONNECT"])
        self.assertExactly(*self.call("start"), 2, "committed CONNECT of wp_sealer on the events database")

    def test_integ_public_temporary_on_a_stream_database_blocks(self):
        self.acl("wp_events", add=["PUBLIC:TEMPORARY"])
        self.assertExactly(*self.call("start"), 2, "PUBLIC may create temporary tables in the events database")

    def test_integ_app_grant_on_a_hardening_table_blocks(self):
        self.F.cat("wp_events", "app_privs", "add", "write_refusals", "INSERT")
        self.F.cat("wp_events", "app_privs", "add", "identifier_proposals", "SELECT")
        self.assertExactly(*self.call("start"), 2, "G2 wp_app on write_refusals: ['INSERT']",
                           "G2 wp_app on identifier_proposals: ['SELECT']")

    def test_integ_extra_grant_to_the_sealer_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "role_privs", "add", "wp_sealer", "public.events", "SELECT")
        self.assertExactly(*self.call("end"), 3, "role wp_sealer holds ['SELECT'] on public.events (committed [])",
                           "catalog wp_events.role_privs changed")

    def test_integ_reviewer_insert_on_proposals_blocks(self):
        self.F.cat("wp_events", "role_privs", "add", "wp_rev1", "public.identifier_proposals", "INSERT")
        self.assertExactly(*self.call("start"), 2,
                           "role wp_rev1 holds ['INSERT', 'SELECT'] on public.identifier_proposals (committed ['SELECT'])")

    def test_integ_reviewer_read_missing_blocks(self):
        self.F.cat("wp_events", "role_privs", "drop", "wp_rev2", "public.identifier_proposals", "SELECT")
        self.assertExactly(*self.call("start"), 2, "role wp_rev2 holds [] on public.identifier_proposals")

    def test_integ_seal_keys_readable_blocks(self):
        self.F.cat("wp_events", "role_privs", "add", "wp_verifier", "wp_seal.seal_keys", "SELECT")
        self.assertExactly(*self.call("start"), 2, "role wp_verifier holds ['SELECT'] on wp_seal.seal_keys")

    def test_integ_usage_on_the_seal_schema_blocks(self):
        self.F.cat("wp_events", "role_schema", "add", "wp_app", "wp_seal", "USAGE")
        self.assertExactly(*self.call("start"), 2, "role wp_app holds USAGE on schema wp_seal")

    def test_integ_create_on_public_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "role_schema", "add", "wp_rev1", "public", "CREATE")
        self.assertExactly(*self.call("end"), 3, "role wp_rev1 holds CREATE on schema public",
                           "catalog wp_events.role_schema changed")

    def test_integ_app_may_approve_identifiers_blocks(self):
        self.F.cat("wp_events", "role_execute", "add", "public.wp_identifier_approve", "wp_app")
        self.assertExactly(*self.call("start"), 2, "role wp_app may execute public.wp_identifier_approve")

    def test_integ_seal_function_executable_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "role_execute", "add", "wp_seal.verify", "wp_verifier")
        self.assertExactly(*self.call("end"), 3, "role wp_verifier may execute wp_seal.verify",
                           "catalog wp_events.role_execute changed")

    def test_integ_seal_function_as_security_definer_blocks(self):
        row = next(r for r in self.F.st()["catalog"]["wp_events"]["seal_functions"] if r[0] == "install")
        self.F.cat("wp_events", "seal_functions", "set", *row[:3], "True", *row[4:])
        self.assertExactly(*self.call("start"), 2, "wp_seal function install: owner wp_owner, security definer True")

    def test_integ_extra_relation_in_the_seal_schema_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "seal_relations", "add", "copy", "17101", "r", "wp_owner", "", "False", "False")
        self.assertExactly(*self.call("end"), 3, "wp_seal relation copy: kind r", "catalog wp_events.seal_relations")

    def test_integ_seal_schema_replaced_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "seal_schema", "set", "wp_seal", "9300", "wp_owner", "{wp_owner=UC/wp_owner}")
        self.assertExactly(*self.call("end"), 3, "or its public or wp_seal schema was reset or replaced",
                           "catalog wp_events.seal_schema changed")

    def test_integ_schema_outside_the_committed_set_blocks(self):
        self.F.cat("wp_events", "namespaces", "add", "scratch", "wp_app")
        self.assertExactly(*self.call("start"), 2, "a schema outside the committed set in the events database: scratch")

    def test_integ_trigger_disabled_on_identifier_proposals_during_the_run_voids(self):
        self.start()
        self.F.cat("wp_events", "triggers", "drop", "identifier_proposals", "identifier_proposals_guard")
        self.F.cat("wp_events", "triggers", "add", "identifier_proposals", "identifier_proposals_guard",
                   str(T_BEFORE | T_ROW | T_INS | T_UPD | T_DEL), "D", "wp_guard")
        self.assertExactly(*self.call("end"), 3, "G3 trigger identifier_proposals.identifier_proposals_guard is "
                           "enabled 'D'", "catalog wp_events.triggers changed")

    def test_integ_trigger_disabled_on_quarantine_removed_blocks(self):
        self.F.cat("wp_events", "triggers", "drop", "quarantine_removed", "quarantine_removed_no_truncate")
        self.F.cat("wp_events", "triggers", "add", "quarantine_removed", "quarantine_removed_no_truncate",
                   str(T_BEFORE | T_TRUNC), "D", "wp_guard")
        self.assertExactly(*self.call("start"), 2, "G3 trigger quarantine_removed.quarantine_removed_no_truncate is "
                           "enabled 'D'")

    def test_integ_identifier_proposals_without_its_guard_blocks(self):
        self.F.cat("wp_events", "triggers", "drop", "identifier_proposals", "identifier_proposals_guard")
        self.assertExactly(*self.call("start"), 2, "G3 table identifier_proposals has no guard trigger for "
                           "INSERT,UPDATE,DELETE")

    def test_integ_trigger_on_write_refusals_not_always_blocks(self):
        self.F.cat("wp_events", "triggers", "add", "write_refusals", "write_refusals_x",
                   str(T_BEFORE | T_ROW | T_INS), "O", "wp_guard")
        self.assertExactly(*self.call("start"), 2, "G3 trigger write_refusals.write_refusals_x is enabled 'O'")

    def test_integ_write_refusals_writer_not_security_definer_blocks(self):
        row = next(r for r in self.F.st()["catalog"]["wp_events"]["functions"] if r[0] == "wp_write_refused")
        self.F.cat("wp_events", "functions", "set", *row[:3], "False", *row[4:])
        self.assertExactly(*self.call("start"), 2, "G3 table write_refusals has no guard triggers and its writer "
                           "wp_write_refused is not a SECURITY DEFINER function")

    def test_integ_trigger_disabled_on_seal_keys_blocks(self):
        self.F.cat("wp_events", "seal_triggers", "add", "seal_keys", "seal_keys_x", str(T_BEFORE | T_ROW | T_UPD),
                   "D", "wp_guard")
        self.assertExactly(*self.call("start"), 2, "G3 trigger wp_seal.seal_keys.seal_keys_x is enabled 'D'")

    # service containers (hardening D2, D6): the stream's prefix or label, other than the run's two stores
    def test_integ_key_service_container_before_the_run_blocks(self):
        self.F.add("emlab2-wphard-keysvc", "/svc", GIB // 8, GIB, GIB // 2, GIB // 8)
        self.assertExactly(*self.call("start"), 2, "a container of this stream other than the run's stores before "
                           "the run: emlab2-wphard-keysvc (the key service)")

    def test_integ_stream_labelled_container_before_the_run_blocks(self):
        c = self.F.add("helper-x", "/svc", GIB // 8, GIB, GIB // 2, GIB // 8)
        self.F.set(c, labels={"emlab2.stream": "wphard"})
        self.assertExactly(*self.call("start"), 2, "a container of this stream other than the run's stores before "
                           "the run: helper-x")

    def test_integ_service_container_left_at_the_end_voids(self):
        self.start()
        self.F.add("emlab2-wphard-sealer", "/svc", GIB // 8, GIB, GIB // 2, GIB // 8)
        self.assertExactly(*self.call("end"), 3, "a container of this stream other than the run's stores at the end "
                           "of the run (an experiment removes the service containers it starts): emlab2-wphard-sealer")

    def test_integ_service_container_removed_within_the_run_is_valid(self):
        self.start()
        c = self.F.add("emlab2-wphard-keysvc", "/svc", GIB // 8, GIB, GIB // 2, GIB // 8)
        self.F.remove(c)
        self.assertValid(*self.call("end"))

    # amendment GF1a: the gate's lines are H6d's, in one unit
    def test_integ_gf1_lines_equal_h6d(self):
        chain = (HERE / "results" / "failing-first" / "harness" / "live_chain.sh").read_text()   # not in a pinned tree
        kib = dict(re.findall(r"(START_KIB|STOP_KIB)=(\d+)", chain))
        self.assertEqual(int(kib["START_KIB"]), int(self.g.START_GB * 1024 * 1024), "H6d start line != START_GB")
        self.assertEqual(int(kib["STOP_KIB"]), int(self.g.STOP_GB * 1024 * 1024), "H6d stop line != STOP_GB")
        self.assertIn('df -k --output=avail', chain)  # df's avail is statvfs f_bavail, as shutil.disk_usage().free
        self.assertIn("shutil.disk_usage(HOME).free", inspect.getsource(self.g.free_gb))
        wm = re.search(r"^WATERMARKS = (\{.+\})$", (self.tree / "wp" / "es_security.py").read_text(), re.M).group(1)
        wm = {k: int(v[:-2]) for k, v in re.findall(r'"(\w+)": "(\d+gb)"', wm)}
        self.assertEqual(wm["flood_stage"], int(self.g.STOP_GB), "ES flood stage is not the stop line")
        self.assertEqual(wm["low"], int(self.g.START_GB), "ES low watermark is not the start line")

    # ---- C12 (F-4): no container but the run's stores uses this home
    def test_v6_f4_container_using_this_home_blocks(self):
        self.F.add_user("emlab2-other-tool", os.path.realpath(self.home) + "/secrets")
        self.assertOnly(*self.call("start"), 2, "uses this home")

    def test_v6_f4_container_using_this_home_during_the_run_voids(self):
        self.start()
        self.F.add_user("emlab2-other-tool", os.path.realpath(self.home))
        self.assertOnly(*self.call("end"), 3, "uses this home")

    def test_v6_f4_container_list_failure_blocks(self):
        self.F.flag(docker_ps_rc=True)
        self.assertOnly(*self.call("start"), 2, "container list (docker ps) could not be read")

    def test_v6_f4_container_mounting_a_sibling_directory_is_not_counted(self):
        self.F.add_user("emlab2-other-tool", os.path.realpath(self.home) + "-sibling")
        self.F.add_user("emlab2-other-tool2", "/srv/elsewhere")
        self.start()
        self.assertValid(*self.call("end"))

    # ---- C2, C8 (m-B, m-C): special files never hang the gate; files are hashed in bounded memory
    def test_v6_mb_fifo_in_the_pinned_tree_blocks_without_hanging(self):
        t = self.fresh_tree()
        os.mkfifo(t / "exp" / "pipe")
        self.assertOnly(*self.launch("x", tree=t, timeout=60).run(), 2, "special or unreadable entry in the pinned tree")

    def test_v6_mb_fifo_in_the_run_directory_voids_without_hanging(self):
        rc, out = self.launch("x", timeout=60, ops=[["mkfifo", "pipe.json"]]).run()
        self.assertOnly(rc, out, 3, "special or unreadable file in the run directory")

    def test_v6_mc_row_over_the_size_cap_voids(self):
        rc, out = self.launch("x", ops=[["bigrow", "big.json", 17 * MIB]]).run()
        self.assertOnly(rc, out, 3, "larger than 16 MiB")

    def test_v6_mc_large_run_file_is_hashed_in_bounded_memory(self):
        import resource
        A = self.launch("x", ops=[["sparse", "blob.bin", GIB]])
        self.assertValid(*A.run())
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        self.assertLess(peak, 400 * MIB, f"a child of this test (the gate) peaked at {peak // MIB} MiB hashing a 1 GiB file")
        rec = [r for r in self.records() if r["kind"] == "end"][-1]
        self.assertIn("blob.bin", [f["name"] for f in rec["files"]])

    # ---- C2 (m-G): the gate and the experiment run isolated, with a listed environment
    def test_v6_mg_gate_not_isolated_blocks(self):
        with mock.patch.object(self.g, "_isolated", lambda: False, create=True):
            self.assertOnly(*self.call("start"), 2, "not isolated")

    def test_v6_mg_experiment_runs_isolated_with_the_listed_environment(self):
        dump = pathlib.Path(tempfile.mkdtemp(dir=self.tmp)) / "dump.json"
        A = self.launch("x", env={"LEAKY_SETTING": "1", "PYTHONINSPECT": ""}, ops=[["dump", str(dump)]])
        self.assertValid(*A.run())
        d = json.loads(dump.read_text())
        self.assertEqual(d["isolated"], 1, "the experiment's interpreter is not isolated (-I)")
        self.assertTrue(d["bytecode_off"])
        for k in ("PYTHONPATH", "LEAKY_SETTING", "PYTHONINSPECT"):
            self.assertNotIn(k, d["env"], f"{k} reached the experiment")
        self.assertIn("WP_GATE_RUN", d["env"])
        adm = [r for r in self.records() if r["kind"] == "admit"][-1]
        self.assertIn("WP_HOME", adm.get("env") or {}, "the admit record does not list the experiment's environment")
        self.assertNotIn("LEAKY_SETTING", adm.get("env") or {})

    # ---- C10 (m-H): exactly one end record
    def test_v6_mh_second_end_record_makes_the_verdict_void(self):
        self.start()
        with self.g.Ledger() as L:                         # a forged VALID end, through the gate's own code
            L.append("end", run=self._run["run"], verdict="VALID", reasons=[], window_end=self.g._utc())
        self.assertOnly(*self.call("end"), 3, "was closed before its end")
        self.assertEqual([v for r, v in self.ends() if r == self._run["run"]], ["VALID", "VOID"])
        rc, out = self.raw_call("verdict", self._run["run"])
        self.assertEqual(rc, 3, f"verdict took the first of two end records: {out.strip()[-300:]}")
        self.assertIn("2 end records", out)

    # ---- C9 (m-I): a gate that leads a group holding another process makes its own session
    def test_v6_mi_gate_leading_a_group_with_another_member_stays_valid(self):
        A = self.launch("x", leader=True, rows=[{"name": "r", "gate_run": "self"}])
        rc, out = A.run()
        self.assertValid(rc, out)
        rc, out = self.raw_call("verdict", A.run_id)
        self.assertEqual(rc, 0, out[-300:])

    def test_v6_abort_closes_a_dead_v5_run(self):
        p = subprocess.Popen(["true"], start_new_session=True)
        p.wait()                                           # its process group is gone
        old = "r-" + "b" * 32
        with self.g.Ledger() as L:
            L.append("admit", v=5, gate="disk_gate v5", run=old, label="old", pid=p.pid, pgid=p.pid)
        rc, out = self.abort(old, "v5 launcher and experiment shown dead")
        self.assertEqual(rc, 0, out[-300:])
        self.assertEqual(self.ends(), [(old, "VOID")])

    # ---- the four surviving mutants of review aacf3c0
    def test_v6_mutant_group_list_failure_voids(self):
        self.F.flag(host_ps_rc=True)                       # group_list_failure_passes
        self.assertOnly(*self.launch("x").run(), 3, "process group could not be listed")

    def test_v6_mutant_es_clean_check_error_blocks(self):
        FakeES.S.update(cat_status=500)                    # es_clean_error_passes
        self.assertOnly(*self.call("start"), 2, "could not be checked clean before the run (ES indices 500)")

    def test_v6_mutant_row_without_a_run_id_voids(self):
        rc, out = self.launch("x", rows=[{"name": "r", "gate_run": None}]).run()   # row_without_run_id_passes
        self.assertOnly(rc, out, 3, "carries run 'None'")

    def test_v6_mutant_module_from_a_sibling_directory_blocks(self):
        mod = SimpleNamespace(__file__=str(self.tree) + "-sibling/wp/zz.py")      # module_prefix_without_sep
        with mock.patch.dict(sys.modules, {"wp.zz_sibling": mod}):
            self.assertOnly(*self.call("start"), 2, "loaded from outside the pinned tree")

    # ---- every test VERDICT-PATHS.md cites exists
    def test_v6_verdict_paths_cite_existing_tests(self):
        doc = HERE / "runkit" / "VERDICT-PATHS.md"
        cited = set(re.findall(r"`(test_\w+)`", doc.read_text())) if doc.exists() else set()
        self.assertTrue(cited, "runkit/VERDICT-PATHS.md cites no test")
        have = set()
        for f in (HERE / "tests").glob("test_*.py"):
            have |= set(re.findall(r"def (test_\w+)\(", f.read_text()))
        self.assertEqual(sorted(cited - have), [], "VERDICT-PATHS.md cites tests that do not exist")


def _killpg_quiet(pg):
    try:
        os.killpg(pg, signal.SIGKILL)
    except OSError:
        pass


class Launch:
    """One run as its launcher makes it. v5: `exp.disk_gate run LABEL stubexp` as a process (through the --gate seam
    below, from the pinned tree). An older gate has no `run`; there the launcher's steps are made as its launcher made
    them (start; the experiment; end), so the same scenario shows that gate's verdict."""

    def __init__(self, t, label, tree=None, leader=False, timeout=300, env=None, **stub):
        self.t, self.label, self.tree = t, label, tree or t.tree
        d = pathlib.Path(tempfile.mkdtemp(dir=t.tmp))
        self.started, self.release, self.free_file = d / "started", d / "release", d / "free"
        self.env = dict(os.environ, GATE_TREE=str(self.tree), STUB_TESTS=str(THIS.parent), FAKE_FREE_GB=str(t.free()),
                        FAKE_FREE_FILE=str(self.free_file), STUB_STARTED=str(self.started),
                        STUB_RELEASE=str(self.release), **(env or {}),
                        **{"STUB_" + k.upper(): v if isinstance(v, str) else json.dumps(v) for k, v in stub.items()})
        self.leader, self.timeout = leader, timeout
        self.p, self.rc, self.out, self.run_id = None, None, "", None
        t.addCleanup(self._cleanup)

    def _cleanup(self):
        """A test that failed half-way leaves no launcher or experiment of its own running."""
        self.release.touch()
        if self.p and self.p.poll() is None:
            try:
                self.p.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                self.kill_launcher()
        self.kill_experiment()

    def begin(self):
        if self.t.v5:
            cmd = [sys.executable, "-B", str(THIS), "--gate", "run", self.label, "stubexp"]
            if self.leader:                    # m-I: the gate leads its process group, which holds another process
                cmd = ["sh", "-c", 'sleep 60 < /dev/null > /dev/null 2>&1 & exec "$@"', "sh"] + cmd
            self.p = subprocess.Popen(cmd, cwd=self.tree, env=self.env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=self.leader)
            if self.leader:
                self.t.addCleanup(_killpg_quiet, self.p.pid)
            t0 = time.monotonic()
            while not self.started.exists() and self.p.poll() is None and time.monotonic() - t0 < 120:
                time.sleep(0.05)
            adm = [r for r in self.t.records() if r["kind"] == "admit" and r.get("label") == self.label]
            if self.started.exists() and adm:
                self.run_id = adm[-1]["run"]
        else:
            self.rc, self.out = self.t.call("start")
            m = re.search(r"start run (\S+)", self.out)
            self.run_id = m.group(1) if self.rc == 0 and m else None
        return self

    def output(self):
        return self.out

    def finish(self, release=True):
        if release:
            self.release.touch()
        if self.t.v5:
            try:
                out, _ = self.p.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:      # a hung gate is a test failure, never a hung test (m-B)
                self.kill_launcher()
                self.kill_experiment()
                out, _ = self.p.communicate(timeout=60)
                self.out += out.decode("utf-8", "replace")
                return "HUNG", self.out + f"\n(the gate did not exit within {self.timeout} s: killed by the test)"
            self.out += out.decode("utf-8", "replace")
            return self.p.returncode, self.out
        if self.rc != 0:
            return self.rc, self.out
        r = subprocess.run([sys.executable, "-B", "-m", "stubexp"], cwd=self.tree, env=self.env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        rc, out = self.t.call("end")
        self.out += r.stdout + out
        return rc, self.out

    def run(self):
        return self.begin().finish()

    def kill_launcher(self):
        if self.p:
            os.kill(self.p.pid, signal.SIGKILL)
            self.p.wait(timeout=30)

    def kill_experiment(self):
        if self.p and self.started.exists():
            try:
                pid = int(self.started.read_text())
                cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
                if "stubexp" in cmd:                       # only this test's experiment, never a reused pid
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def late_call(self):
        """What run A's launcher could still call after an abort: v5 has no end command, so the only call naming A
        is abort (and verdict, which only reads); the older gate's launcher called end."""
        return self.t.abort(self.run_id, "late call") if self.t.v5 else self.t.call("end")


def fake_tools(F, g):
    """The gate's _run_tools (v6 F-1) as the offline tests see it: each call is recorded in the scenario (its tag,
    its step names, and whether the run lock was held at that moment: a fresh open of state/run.lock cannot take it);
    flag "tools_fail_<tag>" makes it fail. FAKE_TOOLS=real in a launcher's environment runs the real one instead."""
    def tools(steps, lock, tag):
        held = False
        try:
            with open(g.RUN_LOCK, "a") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    held = True
        except OSError:
            pass
        F.locked(lambda st: st.setdefault("tools", []).append([tag, [s[0] for s in steps], held]))
        if F.st()["flags"].get("tools_fail_" + tag):
            return [f"{steps[0][0]} ({steps[0][1]}) failed under the run lock (1)"]
        if tag == "admission" and F.st()["flags"].get("reset_repairs_roles"):
            F.cat_restore("cluster", "roles")      # wp.setup --reset re-applies the committed role attributes
        return []
    return tools


def gate_main(argv):
    """--gate: exp/disk_gate.py of GATE_TREE run as a process, with the same fakes as the in-process tests (fake docker
    on PATH, the scenario's /proc and cgroup trees, FakePG, the scenario's unregistered list, FAKE_FREE_GB, or the
    number in FAKE_FREE_FILE when it exists; v6: the recorded store tools unless FAKE_TOOLS=real, the -I check, a short
    in-run poll and grace, and STUB_*/FAKE_* passed to the experiment)."""
    root = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
    F = Fake(root)
    tree = pathlib.Path(os.environ["GATE_TREE"])
    sys.path.insert(0, str(tree))
    spec = importlib.util.spec_from_file_location("disk_gate_seam", tree / "exp" / "disk_gate.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    free0, ff = float(os.environ.get("FAKE_FREE_GB", "30")), os.environ.get("FAKE_FREE_FILE")

    def usage(p):
        try:
            free = float(pathlib.Path(ff).read_text()) if ff else free0
        except (OSError, ValueError):
            free = free0
        return SimpleNamespace(total=100 * GIB, used=(100 - free) * GIB, free=free * GIB)
    shutil.disk_usage = usage
    g.PROC_ROOT, g.CGROUP_ROOT = root / "proc", root / "cg"
    g._pg_connect = lambda *a, **k: FakePG(F, *a, **k)
    g._unregistered = lambda *a: F.st().get("unregistered", [])
    if os.environ.get("FAKE_TOOLS") != "real":
        g._run_tools = fake_tools(F, g)
    g._isolated = lambda: os.environ.get("FAKE_NOT_ISOLATED") != "1"
    g.POLL_S, g.TERM_GRACE_S = 0.2, 3
    g.EXP_ENV_PREFIXES = tuple(getattr(g, "EXP_ENV_PREFIXES", ())) + ("STUB_", "FAKE_", "GATE_TREE")
    try:
        g.main(*argv)
        return 0
    except SystemExit as e:
        if isinstance(e.code, int) or e.code is None:
            return e.code or 0
        print(e.code)
        return 1


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        sys.exit(fake_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--gate":
        sys.exit(gate_main(sys.argv[2:]))
    unittest.main(verbosity=2)

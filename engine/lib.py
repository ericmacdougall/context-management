"""Host-side orchestration helpers for the experiments. Processes are started with Popen and ended by stop files
(`stop-<name>` graceful, `stop-<name>.hard` abrupt self-exit), never by a signal from here."""
import argparse
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import time

from . import ctl, pgstore
from .common import DEMO_TENANT as TENANT
from .common import HOME, pg_connect
from .worker import connect

RUN = HOME / "run"
CODE = pathlib.Path(os.environ.get("ENGINE_CODE_ROOT", str(HOME / "code")))   # the synced tree this run uses
BACKEND = os.environ.get("ENGINE_TEMPORAL_BACKEND", "cli")                       # cli (dev server, SQLite) | pg (server container, Postgres)
if (RUN / "ABORT").exists():  # lets me neutralise queued chains without signalling anything
    raise SystemExit(0)
PROCS = {}
RUN_ID = os.environ.setdefault("ENGINE_RUN_ID", f"r{int(time.time())}x{os.getpid()}")   # scopes payload stats to this run
STARTED = time.time()


def _read_git():
    gc = pathlib.Path(__file__).parent / "GIT_COMMIT"
    return json.loads(gc.read_text()) if gc.exists() else {"commit": "unknown", "dirty": None}


START_GIT = _read_git()      # the commit this run STARTED from; every process of the run must still be on it


def check_tree():
    """Refuse to start anything, or to save a result, if the synced tree's commit file changed since the run began."""
    now = _read_git()
    if now != START_GIT:
        raise RuntimeError(f"code tree changed mid-run: started on {START_GIT}, now {now}")
RECORD = {"servers": [], "workers": [], "params": {}}


def record(**kw):
    """Parameters actually used by the run (read from the variables the experiment ran with), stored in the result."""
    RECORD["params"].update(kw)


def _u(name):
    """Stop-file names are unique per orchestrator process so concurrent runs can never stop each other."""
    return name if name.endswith(f"-{os.getpid()}") else f"{name}-{os.getpid()}"


def _clear(name):
    for suffix in ("", ".hard"):
        p = RUN / f"stop-{name}{suffix}"
        if p.exists():
            p.unlink()


def port_open(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port():
    import random
    for _ in range(200):
        p = random.randrange(50000, 60000, 40)
        ok = True
        for q in range(p, p + 32):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", q))
            except OSError:
                ok = False
            finally:
                s.close()
        if ok:
            return p
    raise RuntimeError("no free port block")


def start_server(cli, db, tport=48310, name=None):
    check_tree()
    if BACKEND == "pg":
        return _start_pg(cli, db, tport, name)
    tport = tport or pick_port()
    name = name or f"server-{tport}"
    if port_open(tport) or port_open(tport + 10) or port_open(tport + 20):
        raise RuntimeError(f"port {tport} (+10,+20) already in use")
    _clear(name)
    env = {**os.environ, "TPORT": str(tport)}
    os.environ["ENGINE_TEMPORAL_PORT"] = str(tport)
    log = open(HOME / "logs" / f"{name}-{cli}-{int(time.time())}.log", "ab")
    RECORD["servers"].append({"name": name, "cli": cli, "db": db, "port": tport, "at": time.time() - STARTED})
    PROCS[name] = subprocess.Popen(["bash", str(CODE / "engine/host/serverloop.sh"), cli, str(RUN / db),
                                    str(RUN / f"stop-{name}")], env=env, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    t0 = time.time()
    while not port_open(tport):
        if time.time() - t0 > 60:
            raise RuntimeError("server did not start")
        time.sleep(0.3)
    time.sleep(2.0)  # namespace default is created a moment after the port opens
    PROCS["server"] = PROCS[name]
    return PROCS[name]


def stop_server(name=None, hard=False, timeout=60):
    name = name or f"server-{os.environ['ENGINE_TEMPORAL_PORT']}"
    if BACKEND == "pg":
        _stop_pg(name, hard)
        return
    (RUN / f"stop-{name}{'.hard' if hard else ''}").touch()
    PROCS[name].wait(timeout=timeout)


def start_worker(name, venv, variant, ident, tq, cwd=None, extra=(), env=None, crash_after=0, home=None):
    check_tree()
    name = _u(name)
    _clear(name)
    cmd = [str(HOME / venv / "bin/python"), "-m", "engine.worker", "--variant", variant, "--identity", ident,
           "--task-queue", tq, "--stop-file", str(RUN / f"stop-{name}")]
    if crash_after:
        cmd += ["--crash-after", str(crash_after)]
    cmd += list(extra)
    e = {**os.environ, "ENGINE_CRASH_LOG": str(HOME / "logs" / f"{name}.crashes"), **(env or {})}
    if home:
        e["ENGINE_HOME"] = str(home)
    log = open(HOME / "logs" / f"{name}.log", "ab")
    RECORD["workers"].append({"name": name, "venv": venv, "variant": variant, "identity": ident, "task_queue": tq,
                              "code_dir": str(cwd or CODE), "extra": list(extra), "crash_after": crash_after,
                              "knobs": {k: v for k, v in (env or {}).items() if k.startswith("ENGINE_")}, "at": time.time() - STARTED})
    PROCS[name] = subprocess.Popen(cmd, cwd=str(cwd or CODE), env=e, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    return PROCS[name]


def stop_worker(name, hard=False, timeout=60):
    name = _u(name)
    (RUN / f"stop-{name}{'.hard' if hard else ''}").touch()
    try:
        return PROCS[name].wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def ns(**kw):
    d = dict(expected=0, rollup_s=0, approval_at=0, approval_to_s=0, stop_after_rollups=0, can_len=0, turns=0,
             period=1.0, ver=1, task_queue="sess", approval_poll_s=None, approval_poll_max_s=None, reconcile_s=None,
             reconcile_max_s=None)
    d.update(kw)
    return argparse.Namespace(**d)


async def launch(client, tag, n, tq, stats, tenant=TENANT, **kw):
    """Start n sessions (workflow + session_started event), then feed turns. Returns the feed tasks."""
    a = ns(task_queue=tq, **kw)
    ids = ctl.sids(tag, n)
    for i in range(0, n, 20):
        await asyncio.gather(*[ctl.start_session(client, s, a, a.ver, tq, stats, tenant) for s in ids[i:i + 20]])
    if a.turns:
        return [asyncio.create_task(ctl.feed(client, s, a, stats, __import__("random").uniform(0, a.period), tenant))
                for s in ids]
    return []


async def statuses(client, tag, n, tenant=TENANT):
    out = {}
    for s in ctl.sids(tag, n):
        try:
            d = await client.get_workflow_handle(ctl.wid(s, tenant)).describe()
            out[s] = d.status.name
        except Exception as e:
            out[s] = "ERR:" + type(e).__name__
    return out


async def wait_done(client, tag, n, timeout, tenant=TENANT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = await statuses(client, tag, n, tenant)
        if all(v != "RUNNING" for v in st.values()):
            break
        await asyncio.sleep(3)
    from collections import Counter
    return dict(Counter(st.values())), round(time.time() - t0, 1)


async def results_of(client, tag, n, tenant=TENANT):
    out = {}
    for s in ctl.sids(tag, n):
        try:
            out[s] = await client.get_workflow_handle(ctl.wid(s, tenant)).result()
        except Exception as e:
            out[s] = {"error": type(e).__name__}
    return out


def events_of(sid, kind=None, tenant=TENANT):
    return pgstore.events_of(tenant, sid, kind)


def q(sql, *args):
    with pg_connect() as c:
        return c.execute(sql, args).fetchall()


def provenance():
    """Where this result came from: git commit and dirty flag of the code that ran (written by host/sync.sh), run id,
    start and end times, host, argv, versions, and every server and worker the run started."""
    import platform
    import sys
    from importlib import metadata
    git = START_GIT
    return {"git_commit": git.get("commit"), "git_dirty": git.get("dirty"), "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED)),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": platform.node(),
            "argv": sys.argv[1:], "python": platform.python_version(),
            "temporalio_client": metadata.version("temporalio"), "recorded": RECORD}


def save(name, obj):
    check_tree()
    obj = {"provenance": provenance(), **obj}
    p = HOME / "results" / f"{name}-{RUN_ID.replace('r', '', 1)}.json"   # never overwrite another run's result
    json.dump(obj, open(p, "w"), indent=1, default=str)
    return p


# ------------------------------------------------------------------ Temporal server containers on Postgres persistence
TPG_MAP = {"1.7.3": "1.31.5", "1.10.1": "1.32.2"}       # the experiments' CLI labels, mapped to server image versions
TPG_NET = "emlab-temporal-16096-net"                    # private bridge: no published Postgres port, no password exists
TPG_PG = "emlab-pg-temporal-16096"
TPG_DIR = HOME / "tpg"
CONTAINERS = []
TPG_DBS = []


def _dk(*a, timeout=300):
    r = subprocess.run(["docker", *a], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


class Container:
    def __init__(self, name):
        self.name, self.returncode = name, None

    def wait(self, timeout=60):
        rc, out = _dk("wait", self.name, timeout=timeout)
        self.returncode = int(out.split()[-1]) if rc == 0 and out.split() else None
        return self.returncode


def tpg_ensure_pg():
    TPG_DIR.mkdir(exist_ok=True)
    _dk("network", "create", TPG_NET)
    rc, out = _dk("inspect", "-f", "{{.State.Running}}", TPG_PG)
    if rc != 0:
        (TPG_DIR / "data").mkdir(exist_ok=True)
        rc, out = _dk("run", "-d", "--name", TPG_PG, "--network", TPG_NET, "--cpus", "2", "--memory", "2g",
                      "-e", "POSTGRES_USER=temporal", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                      "-v", f"{TPG_DIR}/data:/var/lib/postgresql/data", "postgres:16")
        if rc != 0:
            raise RuntimeError("temporal postgres did not start: " + out)
    elif out != "true":
        _dk("start", TPG_PG)
    for _ in range(60):
        if _dk("exec", TPG_PG, "pg_isready", "-U", "temporal")[0] == 0:
            return
        time.sleep(1)
    raise RuntimeError("temporal postgres not ready")


def tpg_psql(db, sql):
    return _dk("exec", TPG_PG, "psql", "-U", "temporal", "-d", db, "-tAc", sql)


def tpg_schema(version, db):
    """The documented path: temporal-sql-tool create / setup-schema / update-schema of the TARGET version, run before
    the server of that version starts. Returns the tool outputs."""
    base = ["run", "--rm", "--network", TPG_NET, "--entrypoint", "temporal-sql-tool", f"temporalio/admin-tools:{version}",
            "--plugin", "postgres12", "--ep", TPG_PG, "-p", "5432", "-u", "temporal"]
    outs = []
    for d, sub in ((db, "temporal"), (db + "_vis", "visibility")):
        if not tpg_psql("postgres", f"select 1 from pg_database where datname='{d}'")[1]:
            outs.append(("create " + d, _dk(*base, "--db", d, "create")[1][-300:]))
        if tpg_psql(d, "select count(*) from schema_version")[0] != 0:
            outs.append(("setup " + d, _dk(*base, "--db", d, "setup-schema", "-v", "0.0")[1][-300:]))
        outs.append(("update " + d, _dk(*base, "--db", d, "update-schema", "-d", f"/etc/temporal/schema/postgresql/v12/{sub}/versioned")[1][-400:]))
    return outs


def tpg_schema_versions(db):
    return {d: tpg_psql(d, "select curr_version from schema_version order by creation_time desc limit 1")[1] for d in (db, db + "_vis")}


def tpg_config(db, tport):
    cfg = TPG_DIR / f"server-{tport}.yaml"
    def store(d):
        return (f'      sql:\n        pluginName: "postgres12"\n        databaseName: "{d}"\n        connectAddr: "{TPG_PG}:5432"\n'
                '        connectProtocol: "tcp"\n        user: "temporal"\n        password: ""\n        maxConns: 20\n'
                '        maxIdleConns: 20\n        maxConnLifetime: "1h"\n')
    cfg.write_text(
        'log:\n  stdout: true\n  level: warn\n'
        'persistence:\n  defaultStore: default\n  visibilityStore: visibility\n  numHistoryShards: 4\n  datastores:\n'
        f'    default:\n{store(db)}    visibility:\n{store(db + "_vis")}'
        'global:\n  membership:\n    maxJoinDuration: 30s\n    broadcastAddress: "127.0.0.1"\n'
        'services:\n'
        '  frontend:\n    rpc: {grpcPort: 7233, membershipPort: 6933, bindOnIP: "0.0.0.0"}\n'
        '  matching:\n    rpc: {grpcPort: 7235, membershipPort: 6935, bindOnIP: "0.0.0.0"}\n'
        '  history:\n    rpc: {grpcPort: 7234, membershipPort: 6934, bindOnIP: "0.0.0.0"}\n'
        '  worker:\n    rpc: {grpcPort: 7239, membershipPort: 6939, bindOnIP: "0.0.0.0"}\n'
        'clusterMetadata:\n  enableGlobalNamespace: false\n  failoverVersionIncrement: 10\n  masterClusterName: "active"\n'
        '  currentClusterName: "active"\n  clusterInformation:\n    active:\n      enabled: true\n      initialFailoverVersion: 1\n'
        '      rpcName: "frontend"\n      rpcAddress: "127.0.0.1:7233"\n'
        'dcRedirectionPolicy:\n  policy: "noop"\n'
        'archival:\n  history:\n    state: "disabled"\n  visibility:\n    state: "disabled"\n'
        'namespaceDefaults:\n  archival:\n    history:\n      state: "disabled"\n    visibility:\n      state: "disabled"\n')
    return cfg


def tpg_start(version, db, tport, cname):
    cfg = tpg_config(db, tport)
    _dk("rm", "-f", cname)
    rc, out = _dk("run", "-d", "--name", cname, "--network", TPG_NET, "--cpus", "4", "--memory", "4g",
                  "-p", f"127.0.0.1:{tport}:7233", "-v", f"{cfg}:/cfg/server.yaml:ro", "--entrypoint", "temporal-server",
                  f"temporalio/server:{version}", "--config-file", "/cfg/server.yaml", "--allow-no-auth", "start")
    CONTAINERS.append(cname)
    return rc, out


def tpg_logs(cname, tail=200):
    return _dk("logs", "--tail", str(tail), cname)[1]


def _start_pg(cli, db, tport, name):
    check_tree()
    import re
    tport = tport or pick_port()
    ver = TPG_MAP[cli]
    dbname = "t" + re.sub(r"[^a-z0-9]", "", (db.lower())[:40])
    name = name or f"server-{tport}"
    cname = f"emlab-temporal-16096-{tport}"
    tpg_ensure_pg()
    before = tpg_schema_versions(dbname) if tpg_psql("postgres", f"select 1 from pg_database where datname='{dbname}'")[1] else None
    outs = tpg_schema(ver, dbname)
    after = tpg_schema_versions(dbname)
    rc, out = tpg_start(ver, dbname, tport, cname)
    if rc != 0:
        raise RuntimeError("temporal server container did not start: " + out)
    os.environ["ENGINE_TEMPORAL_PORT"] = str(tport)
    t0 = time.time()
    while not port_open(tport):
        if time.time() - t0 > 120:
            raise RuntimeError("temporal server did not open its port: " + tpg_logs(cname, 40))
        time.sleep(1)
    cli_bin = str(HOME / "bin/cli-1.10.1/temporal")
    for _ in range(60):      # the namespace is created once per database; the server needs a moment before it answers
        r = subprocess.run([cli_bin, "operator", "namespace", "describe", "--address", f"127.0.0.1:{tport}", "default"], capture_output=True, text=True)
        if r.returncode == 0:
            break
        subprocess.run([cli_bin, "operator", "namespace", "create", "--address", f"127.0.0.1:{tport}", "default"], capture_output=True, text=True)
        time.sleep(2)
    time.sleep(3)
    TPG_DBS.append(dbname)
    RECORD["servers"].append({"name": name, "backend": "postgres persistence", "server_image": f"temporalio/server:{ver}", "label": cli,
                              "database": dbname, "port": tport, "container": cname, "schema_before": before, "schema_after": after,
                              "schema_tool": outs, "at": time.time() - STARTED})
    PROCS[name] = PROCS["server"] = Container(cname)
    return PROCS[name]


def _stop_pg(name, hard):
    cname = f"emlab-temporal-16096-{os.environ['ENGINE_TEMPORAL_PORT']}"
    _dk("stop", *(["-t", "0"] if hard else []), cname)       # -t 0: the container's main process is killed at once
    c = PROCS.get(name) or Container(cname)
    rc, out = _dk("inspect", "-f", "{{.State.ExitCode}}", cname)
    c.returncode = int(out) if rc == 0 and out.lstrip("-").isdigit() else None
    return c


def tpg_discard(db_label_index=-1):
    """Engine independence: drop the persistence of the last server this run started (a fresh server then starts on
    an empty database)."""
    db = TPG_DBS[db_label_index]
    for d in (db, db + "_vis"):
        _dk("exec", TPG_PG, "psql", "-U", "temporal", "-d", "postgres", "-c", f"drop database if exists {d} with (force)")
    return db


def server_log_text(tport, cli):
    if BACKEND == "pg":
        return tpg_logs(f"emlab-temporal-16096-{tport}", 400)
    fs = sorted((HOME / "logs").glob(f"server-{tport}-{cli}-*.log"))
    return fs[-1].read_text() if fs else ""


def tpg_cleanup():
    for c in set(CONTAINERS):
        _dk("rm", "-f", c)


import atexit
atexit.register(tpg_cleanup)


async def connect_retry(name="orch", tries=90):
    last = None
    for _ in range(tries):
        try:
            return await connect(name)
        except Exception as e:  # the server accepts TCP before its services are up
            last = e
            await asyncio.sleep(1.0)
    raise last


def start_server_try(cli, db, tport, wait=20):
    """Start a server and report whether it came up (used for the downgrade control)."""
    name = f"server-{tport}"
    _clear(name)
    logp = HOME / "logs" / f"{name}-{cli}-try-{int(time.time())}.log"
    env = {**os.environ, "TPORT": str(tport)}
    PROCS[name] = subprocess.Popen(["bash", str(CODE / "engine/host/serverloop.sh"), cli, str(RUN / db),
                                    str(RUN / f"stop-{name}")], env=env, stdout=open(logp, "ab"), stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    t0 = time.time()
    up = False
    while time.time() - t0 < wait and PROCS[name].poll() is None:
        up = port_open(tport)
        if up:
            break
        time.sleep(0.5)
    exited = PROCS[name].poll()
    (RUN / f"stop-{name}").touch()
    try:
        PROCS[name].wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    return {"came_up": up, "exit_code": exited, "log_tail": logp.read_text()[-500:]}


def start_loop(name, venv, variant, ident, tq, env=None, seed=1, crashes=0):
    """workerloop.sh under Popen: restarts the worker after each abrupt self-exit until stop-<name> appears. While
    `crashes` remain the worker ends itself abruptly after a random 2 to (CMAX+1) seconds."""
    check_tree()
    name = _u(name)
    _clear(name)
    e = {**os.environ, **(env or {})}
    RECORD["workers"].append({"name": name, "kind": "loop", "venv": venv, "variant": variant, "identity": ident,
                              "task_queue": tq, "crashes": crashes, "seed": seed,
                              "knobs": {k: v for k, v in (env or {}).items() if k.startswith("ENGINE_") or k == "CMAX"},
                              "at": time.time() - STARTED})
    PROCS[name] = subprocess.Popen(["bash", str(CODE / "engine/host/workerloop.sh"), venv, variant, ident, tq,
                                    str(CODE), str(crashes), str(seed), str(RUN / f"stop-{name}"),
                                    str(HOME / "logs" / name)],
                                   env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
    return PROCS[name]

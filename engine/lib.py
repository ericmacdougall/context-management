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
if (RUN / "ABORT").exists():  # lets me neutralise queued chains without signalling anything
    raise SystemExit(0)
PROCS = {}
RUN_ID = os.environ.setdefault("ENGINE_RUN_ID", f"r{int(time.time())}x{os.getpid()}")   # scopes payload stats to this run
STARTED = time.time()
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
    tport = tport or pick_port()
    name = name or f"server-{tport}"
    if port_open(tport) or port_open(tport + 10) or port_open(tport + 20):
        raise RuntimeError(f"port {tport} (+10,+20) already in use")
    _clear(name)
    env = {**os.environ, "TPORT": str(tport)}
    os.environ["ENGINE_TEMPORAL_PORT"] = str(tport)
    log = open(HOME / "logs" / f"{name}-{cli}-{int(time.time())}.log", "ab")
    RECORD["servers"].append({"name": name, "cli": cli, "db": db, "port": tport, "at": time.time() - STARTED})
    PROCS[name] = subprocess.Popen(["bash", str(HOME / "code/engine/host/serverloop.sh"), cli, str(RUN / db),
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
    (RUN / f"stop-{name}{'.hard' if hard else ''}").touch()
    PROCS[name].wait(timeout=timeout)


def start_worker(name, venv, variant, ident, tq, cwd=None, extra=(), env=None, crash_after=0, home=None):
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
                              "code_dir": str(cwd or HOME / "code"), "extra": list(extra), "crash_after": crash_after,
                              "knobs": {k: v for k, v in (env or {}).items() if k.startswith("ENGINE_")}, "at": time.time() - STARTED})
    PROCS[name] = subprocess.Popen(cmd, cwd=str(cwd or HOME / "code"), env=e, stdout=log, stderr=log,
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
             period=1.0, ver=1, task_queue="sess")
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
    gc = pathlib.Path(__file__).parent / "GIT_COMMIT"
    git = json.loads(gc.read_text()) if gc.exists() else {"commit": "unknown", "dirty": None}
    return {"git_commit": git.get("commit"), "git_dirty": git.get("dirty"), "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED)),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": platform.node(),
            "argv": sys.argv[1:], "python": platform.python_version(),
            "temporalio_client": metadata.version("temporalio"), "recorded": RECORD}


def save(name, obj):
    obj = {"provenance": provenance(), **obj}
    p = HOME / "results" / f"{name}.json"
    json.dump(obj, open(p, "w"), indent=1, default=str)
    return p


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
    PROCS[name] = subprocess.Popen(["bash", str(HOME / "code/engine/host/serverloop.sh"), cli, str(RUN / db),
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
    name = _u(name)
    _clear(name)
    e = {**os.environ, **(env or {})}
    RECORD["workers"].append({"name": name, "kind": "loop", "venv": venv, "variant": variant, "identity": ident,
                              "task_queue": tq, "crashes": crashes, "seed": seed,
                              "knobs": {k: v for k, v in (env or {}).items() if k.startswith("ENGINE_") or k == "CMAX"},
                              "at": time.time() - STARTED})
    PROCS[name] = subprocess.Popen(["bash", str(HOME / "code/engine/host/workerloop.sh"), venv, variant, ident, tq,
                                    str(HOME / "code"), str(crashes), str(seed), str(RUN / f"stop-{name}"),
                                    str(HOME / "logs" / name)],
                                   env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
    return PROCS[name]

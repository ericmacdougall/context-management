"""Worker entry point: `python -m engine.worker --variant v1 --identity hostA [--task-queue sess]`.
Runs until killed. Crash tests end it with SIGKILL; a normal stop is SIGTERM."""
import argparse
import asyncio
import dataclasses
import importlib
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.worker import Worker

from . import activities, common, pgstore
from .codec import CheckingCodec

_watch_task = None


def make_codec(proc):
    def flush(i, r, mx):
        with pgstore.pool().connection() as c:
            c.execute("insert into payload_stats(proc,inspected,rejected,max_bytes,updated_at) values (%s,%s,%s,%s,now()) "
                      "on conflict (proc) do update set inspected=excluded.inspected, rejected=excluded.rejected, "
                      "max_bytes=excluded.max_bytes, updated_at=now()", (proc, i, r, mx))
    return CheckingCodec(on_flush=flush)


async def connect(proc, namespace="default"):
    codec = make_codec(proc)
    dc = dataclasses.replace(DataConverter.default, payload_codec=codec)
    return await Client.connect(common.temporal_target(), namespace=namespace, data_converter=dc), codec


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="v1")
    ap.add_argument("--identity", default="hostA")
    ap.add_argument("--task-queue", default="sess")
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--deployment", default="")   # Worker Versioning: deployment name
    ap.add_argument("--build-id", default="")
    ap.add_argument("--pinned", action="store_true")
    ap.add_argument("--crash-after", type=float, default=0)  # abrupt self-exit (no cleanup, no flush): crash test
    ap.add_argument("--stop-file", default="")                 # graceful stop when this file appears
    a = ap.parse_args()
    os.environ["ENGINE_WORKER_IDENT"] = a.identity
    mod = importlib.import_module(f"engine.wf.{a.variant}")
    proc = f"{a.identity}-{os.getpid()}"
    client, codec = await connect(proc, a.namespace)
    kw = {}
    if a.deployment:
        from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
        from temporalio.worker import WorkerDeploymentConfig
        kw["deployment_config"] = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=a.deployment, build_id=a.build_id),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED if a.pinned else VersioningBehavior.AUTO_UPGRADE)
    else:
        kw["build_id"] = a.build_id or a.variant
    worker = Worker(client, task_queue=a.task_queue, workflows=[mod.SessionWorkflow], activities=activities.ALL,
                    activity_executor=ThreadPoolExecutor(48), max_concurrent_activities=48,
                    identity=a.identity, sticky_queue_schedule_to_start_timeout=timedelta(seconds=2),
                    graceful_shutdown_timeout=timedelta(seconds=3), **kw)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, stop.set)
    if a.crash_after:
        import threading
        def crash():
            import json
            time.sleep(a.crash_after)
            snap = activities.inflight_snapshot()
            with open(os.environ.get("ENGINE_CRASH_LOG", "/dev/null"), "a") as f:  # instrumentation only
                f.write(json.dumps({"t": time.time(), "identity": a.identity, "inflight": snap}) + "\n")
            os._exit(137)
        threading.Thread(target=crash, daemon=True).start()
    if a.stop_file:
        async def watch():
            while not os.path.exists(a.stop_file):
                if os.path.exists(a.stop_file + ".hard"):   # crash on request: abrupt self-exit, no cleanup
                    import json
                    with open(os.environ.get("ENGINE_CRASH_LOG", "/dev/null"), "a") as f:
                        f.write(json.dumps({"t": time.time(), "identity": a.identity, "inflight": activities.inflight_snapshot(), "how": "hard-file"}) + "\n")
                    os._exit(137)
                await asyncio.sleep(0.2)
            stop.set()
        global _watch_task
        _watch_task = asyncio.get_running_loop().create_task(watch())  # keep a strong ref (tasks can be GC'd)
    print(f"worker up variant={a.variant} identity={a.identity} pid={os.getpid()} tq={a.task_queue}", flush=True)
    async with worker:
        await stop.wait()
    codec.flush()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

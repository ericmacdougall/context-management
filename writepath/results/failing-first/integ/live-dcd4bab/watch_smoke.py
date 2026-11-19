"""Smoke the gate's _Watch against the real Docker daemon on t2-dev (no run, no lock, no stream container).
usage: cd <tree>/writepath && <venv python> watch_smoke.py
Case A: nothing happens for 3 s -> why must be empty (checks the exit status after the gate's SIGTERM).
Case B: with a smoke-only prefix, one throwaway container `<prefix>keysvc` (docker run --rm, postgres:16-alpine already on the host (ubuntu:24.04 is not), entrypoint sleep,
        not the stream's label or name) -> the create event must be judged from real attributes and a real inspect.
Prints reasons and the raw create event's attribute keys (names only)."""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.getcwd())
os.environ.setdefault("WP_HOME", os.path.expanduser("~/emlab2-16096/gatefix/wp"))
from exp import disk_gate as G  # noqa: E402

w = G._Watch(set())
w.start()
time.sleep(3)
print("A why:", w.stop(), "rc:", w.p.returncode, "events:", w.n)

pre = "gf16096smoke-"
G.config.OWN_PREFIX = pre
w = G._Watch(set())
w.start()
time.sleep(1)
r = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--name", pre + "keysvc", "--entrypoint", "sleep", "postgres:16-alpine", "2"],
                   capture_output=True, text=True)
print("docker run rc:", r.returncode, r.stderr.strip()[:200])
print("B why:")
for x in w.stop():
    print("  ", x[:300])
print("B rc:", w.p.returncode, "events:", w.n, "seen:", len(w.seen))
ev = subprocess.run(["docker", "events", "--since", "60s", "--until", "0s", "--filter", "type=container",
                     "--filter", f"container={pre}keysvc", "--filter", "event=create", "--format", "{{json .}}"],
                    capture_output=True, text=True).stdout.splitlines()
for line in ev[:1]:
    e = json.loads(line)
    print("real create event keys:", sorted(e), "Actor.Attributes keys:", sorted((e.get("Actor") or {}).get("Attributes") or {}))

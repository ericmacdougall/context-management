"""Helper for tests/test_transport_bounds.py TB9 (not a test module; review 42cfd11 R2a, from the review's probes PC1, PC8
and PD). Runs ONE scenario against the real wp/keysvc.py Server with the seal authority's bounds as wp/sealer.py states
them, in its own process so the peak RSS it reports is the server's own, and prints one JSON line.
  pc1 N  one connection sends one request line of empty JSON objects as long as the authority's line limit allows
  pd N   N connections, one after another, each sends that line, reads its reply and then stays connected and idle
  pc8 N  N connections send that line at once; dispatch holds each request 0.5 s (an upper-bound arrangement)
Connections come from four allowed uids in turn, so N up to MAX_CONN (32) is admitted.
A watchdog ends the process at CAP_MIB of RSS and reports capped (so a tree without the bounds fails by assertion in
the caller, not by exhausting the host). Linux only (/proc/self/statm). Needs WP_HOME and WP_UCD_DIR (wp.sealer imports
the matcher). usage (from writepath/): python3 tests/r2_mem_probe.py <pc1|pd|pc8> <n>"""
import json
import os
import pathlib
import resource
import socket
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from wp import keysvc as KS, sealer as SL  # noqa: E402

CAP_MIB = 1024
UID = 20004
PAGE = os.sysconf("SC_PAGE_SIZE")


def rss_mib():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * PAGE / 2**20


def peak_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main(scenario, n):
    out = {"scenario": scenario, "n": n, "line_limit": SL.LINE_LIMIT, "capped": False}

    def watchdog():
        while True:
            if rss_mib() > CAP_MIB:
                out.update(capped=True, peak_mib=round(peak_mib(), 1))
                print(json.dumps(out), flush=True)
                os._exit(0)
            time.sleep(0.005)

    def disp(store, op, a):
        if scenario == "pc8":
            time.sleep(0.5)
        return SL.dispatch(store, op, a)

    path = os.path.join(tempfile.mkdtemp(), "a.sock")
    uids, nxt = list(range(UID, UID + 4)), [0]          # four allowed peers in turn: n up to 4 x the per-uid cap fits

    def uid_of(s):
        nxt[0] += 1
        return uids[nxt[0] % 4]
    srv = KS.Server(path, set(uids), make_store=lambda: object(), uid_of=uid_of, ops=SL.OPS, dispatch_fn=disp,
                    line_limit=SL.LINE_LIMIT, request_timeout_s=SL.REQUEST_TIMEOUT_S, max_per_uid=SL.MAX_CONN_PER_UID,
                    max_conn=SL.MAX_CONN)
    for k, v in (("idle_timeout_s", "IDLE_TIMEOUT_S"), ("work_slots", "WORK_SLOTS"), ("queue_wait_s", "QUEUE_WAIT_S")):
        setattr(srv, k, getattr(SL, v, None))       # a tree without these bounds ignores them
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    k = (SL.LINE_LIMIT - 200) // 3
    line = b'{"op": "seal", "args": {"tenant": "t", "events": [' + b"{}," * (k - 1) + b"{}]}}\n"
    time.sleep(0.2)
    out["base_mib"] = round(rss_mib(), 1)
    threading.Thread(target=watchdog, daemon=True).start()
    keep, replies = [], []

    def one():
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(120)
        c.connect(path)
        c.sendall(line)
        f = c.makefile("rb")
        replies.append(f.readline().decode().strip())
        keep.append((c, f))
    if scenario == "pc8":
        ths = [threading.Thread(target=one) for _ in range(n)]
        for t in ths:
            t.start()
            time.sleep(0.02)        # the listen backlog is 5: a burst of connects past it is refused by the kernel
        for t in ths:
            t.join(180)
    else:
        for _ in range(n):
            one()
            time.sleep(0.3)
    time.sleep(1.0)
    out.update(peak_mib=round(peak_mib(), 1), replies=sorted(set(replies)), served=len(replies))
    out["growth_mib"] = round(out["peak_mib"] - out["base_mib"], 1)
    print(json.dumps(out), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))

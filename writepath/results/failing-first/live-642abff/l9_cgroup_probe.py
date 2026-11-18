"""Read-only probe for live item GF16096-L9 (part): can a process in a new session on t2-dev read its own cgroup v2
memory.events, as exp/disk_gate.py _own_oom does? Prints counts only."""
lines = open("/proc/self/cgroup").read().splitlines()
p = next((x[3:] for x in lines if x.startswith("0::")), None)
print("cgroup lines", len(lines), "| v2 entry present", p is not None, "| path", p)
f = "/sys/fs/cgroup" + (p or "") + "/memory.events"
try:
    ev = dict(ln.split() for ln in open(f).read().splitlines())
    print("memory.events readable | fields", sorted(ev), "| counter values", {k: ev[k] for k in sorted(ev)})
except OSError as e:
    print("memory.events NOT readable:", type(e).__name__)

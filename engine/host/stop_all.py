"""Ask every testbed process to end by touching its stop file (no signal is sent). Reads each process's command line:
`--stop-file F` (workers), the third argument of serverloop.sh (servers), the eighth argument of workerloop.sh (loops).
Only processes whose command line is under ~/emlab-16096/engine are considered. Run on the host."""
import glob
import os

HOME = os.path.expanduser("~/emlab-16096/engine")
touched = []
for p in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        argv = open(p, "rb").read().decode(errors="replace").split("\0")
    except OSError:
        continue
    if not any(HOME in a for a in argv):
        continue
    stop = None
    if "--stop-file" in argv:
        stop = argv[argv.index("--stop-file") + 1]
    elif any(a.endswith("serverloop.sh") for a in argv):
        i = [k for k, a in enumerate(argv) if a.endswith("serverloop.sh")][0]
        stop = argv[i + 3] if len(argv) > i + 3 else None
    elif any(a.endswith("workerloop.sh") for a in argv):
        i = [k for k, a in enumerate(argv) if a.endswith("workerloop.sh")][0]
        stop = argv[i + 8] if len(argv) > i + 8 else None
    if stop and stop.startswith(HOME + "/run/"):
        open(stop, "a").close()
        touched.append(os.path.basename(stop))
print(len(touched), "stop files touched:", sorted(set(touched)))

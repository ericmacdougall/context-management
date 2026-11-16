"""Exploratory: where does a destroyed key's hex survive on disk? Prints file paths only (never the key)."""
import subprocess
from exp.util import tenant
from wp import keys as K

PG_C = "emlab-pg-wp-16096"
ks = K.KeyService()
out = []
for i in range(6):
    t = tenant("kr")
    kh = ks.keys(t, ["X"], create=True)["X"].hex()
    ks.conn.execute("checkpoint")
    ks.destroy(t, "X")
    r = subprocess.run(["docker", "exec", "-i", PG_C, "grep", "-rlaF", "-f", "-", "/var/lib/postgresql/data"], input=kh.encode() + b"\n", capture_output=True, timeout=600)
    files = [x for x in r.stdout.decode().splitlines() if x.strip()]
    del kh
    print(i, files, flush=True)

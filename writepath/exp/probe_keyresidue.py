"""Exploratory: where does a destroyed key's hex survive on disk? Prints file paths only (never the key).
Modes (suffix 'raw' = pre-fix rewrite with no horizon wait, e.g. heldraw): plain (destroy alone) | held (destroy while another connection holds an open transaction with an xid in the
events database, as erasure.run does around the key step) | keyssnap (an open snapshot in the KEYS database)."""
import subprocess
import sys
from exp.util import tenant
from wp import db, config, keys as K

PG_C = "emlab-pg-wp-16096"
mode = sys.argv[1] if len(sys.argv) > 1 else "plain"
ks = K.KeyService()
for i in range(int(sys.argv[2]) if len(sys.argv) > 2 else 4):
    t = tenant("kr")
    kh = ks.keys(t, ["X"], create=True)["X"].hex()
    ks.conn.execute("checkpoint")
    other = None
    if mode.startswith("held"):
        other = db.connect(autocommit=False)
        other.execute("select txid_current()")
    if mode.startswith("keyssnap"):
        other = db.verifier(db=config.KEYS_DB, autocommit=False)
        other.execute("select count(*) from destroyed")
    ks.destroy(t, "X")
    if mode.endswith("raw"):                 # the pre-fix behaviour: rewrite while the other transaction is open
        ks.conn.execute("vacuum full subject_keys")
        for _ in range(2):
            ks.conn.execute("select pg_switch_wal()"); ks.conn.execute("checkpoint")
        pr = None
    tm = None
    if other and not mode.endswith("raw"):   # released 2 s into purge(): purge must wait for it
        import threading
        tm = threading.Timer(2.0, lambda o=other: (o.rollback(), o.close())); tm.start()
    if not mode.endswith("raw"):
        pr = ks.purge(timeout_s=30)
    if tm:
        tm.join()
    elif other:
        other.rollback(); other.close()
    r = subprocess.run(["docker", "exec", "-i", PG_C, "grep", "-rlaF", "-f", "-", "/var/lib/postgresql/data"], input=kh.encode() + b"\n", capture_output=True, timeout=600)
    del kh
    print(mode, i, pr, [x for x in r.stdout.decode().splitlines() if x.strip()], flush=True)

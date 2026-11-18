"""Live items GF16096-L2, L3, L4 (function level), sent over stdin to t2-dev and run as
`$PY -I -B - <pinned tree>` with WP_HOME (own home) and WP_SECRETS_DIR set. Calls gate v6's own functions on the real
stores, read-only except the PG nonce file that _slow writes into PG's data directory and removes (as admission does):
_clean (P7: rows, indices, registry, log store), a gate PG connection's statement_timeout (m-6), and _slow for both
containers (the in-container probe with GNU find/stat/realpath, the parser of review a108fc3 m-1, the topology rules).
Prints reasons and counts; no value from a secret file."""
import sys

sys.path.insert(0, sys.argv[1])
import exp.disk_gate as g                      # noqa: E402
from wp import config                          # noqa: E402

print("gate", g.VERSION, "tree", g.ROOT.name)
why = g._clean()
print("L2/L3 _clean reasons:", len(why))
for w in why:
    print("  clean:", w[:300])
for db in (config.EVENTS_DB, config.KEYS_DB):
    c = g._pg(db)
    try:
        print(f"L2 gate connection to {db}: statement_timeout =", c.execute("SHOW statement_timeout").fetchall()[0][0])
    finally:
        c.close()
for key in ("pg", "es"):
    ref = config.PG_CONTAINER if key == "pg" else config.ES_CONTAINER
    obs, w = g._slow(key, ref)
    print(f"L4 _slow {key}: reasons {len(w)}; observed keys {sorted(obs or {})[:12]}")
    for x in w:
        print("  slow:", x[:300])

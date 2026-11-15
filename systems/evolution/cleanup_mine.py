"""Delete my own sys-* indices and drop my Postgres data (both DBs) to free disk."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import evo
from memsys.esc import ES
es = ES()
s, j = es.req("GET", "/_cat/indices/sys-*", format="json", h="index,store.size", bytes="mb", ok=(200, 404))
names = [r["index"] for r in j] if isinstance(j, list) else []
print("my indices", len(names), "MB", sum(int(r["store.size"] or 0) for r in j) if names else 0)
for i in range(0, len(names), 20):
    es.req("DELETE", "/" + ",".join(names[i:i + 20]), ok=(200, 404))
from memsys import db
for dbn in ("emlab", "evo"):
    import psycopg
    port = int((db.HOME / "pg_port").read_text().strip()); os.environ.setdefault("PGPASSFILE", str(db.HOME / "pgpass"))
    c = psycopg.connect(host="127.0.0.1", port=port, dbname=dbn, user="emlab", autocommit=True)
    c.execute("drop schema public cascade; create schema public;")
print("free GB", evo.disk_free_gb())

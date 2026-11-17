"""Result files: content-addressed JSON with an environment fingerprint (proof rules 5 and 6)."""
import hashlib
import json
import os
import pathlib
import platform
import subprocess
import time

from wp import config

RESULTS = config.HOME / "results"
# Server settings recorded in every result's fingerprint (values as the experiment's own role sees them).
PG_SETTINGS = ("archive_mode", "autovacuum", "default_statistics_target", "effective_cache_size", "fsync",
               "idle_in_transaction_session_timeout", "jit", "maintenance_work_mem", "max_connections", "max_wal_size",
               "plan_cache_mode", "random_page_cost", "shared_buffers", "statement_timeout", "synchronous_commit",
               "transaction_timeout", "wal_keep_size", "wal_recycle", "work_mem")


def _git_head():
    """The commit of the tree that is RUNNING (a pinned copy carries its own GIT_HEAD), never WP_HOME/src, which a
    later sync may have overwritten while a pinned run was in flight."""
    try:
        return (pathlib.Path(__file__).resolve().parents[1] / "GIT_HEAD").read_text().strip()
    except OSError:
        return "unknown"


def fingerprint():
    fp = {"host": platform.node(), "python": platform.python_version(), "code_commit": _git_head(), "load_avg": os.getloadavg(),
          "cpus": os.cpu_count(), "argv": " ".join(__import__("sys").argv)}
    try:
        from wp.es import ES
        es = ES()
        root = es.req("GET", "/")[1]
        lic = es.req("GET", "/_license")[1]["license"]
        nodes = es.req("GET", "/_cat/nodes", format="json", h="name")[1]
        fp["es"] = {"version": root["version"]["number"], "licence": lic["type"], "licence_expiry": lic.get("expiry_date"), "nodes": len(nodes), "url": config.ES_URL}
    except Exception as e:  # noqa: BLE001
        fp["es"] = {"error": type(e).__name__}
    try:
        from wp import db
        with db.connect() as c:
            fp["postgres"] = c.execute("show server_version").fetchone()[0]
            # review-2 item (c): the effective settings the results depend on, not only the version (earlier results
            # depended on settings that were not in the repo until 8c7d841 and are not in their fingerprints)
            fp["postgres_settings"] = dict(c.execute("select name, setting || coalesce(unit, '') from pg_settings where name = any(%s) order by name",
                                                     (list(PG_SETTINGS),)).fetchall())
    except Exception as e:  # noqa: BLE001
        fp["postgres"] = {"error": type(e).__name__}
    fp["model_calls"] = "none (deterministic stub only)"
    from wp.keys import KeyService
    fp["key_store"] = dict(KeyService.describe(KeyService.__new__(KeyService)),
                           note="LAB STAND-IN for the client's KMS; every erasure result here is conditional on a KMS with real key deletion")
    fp["licence_required"] = "basic (no Enterprise feature on the required path)"
    return fp


def write_result(name, data):
    RESULTS.mkdir(parents=True, exist_ok=True)
    doc = {"name": name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "env": fingerprint(), "result": data}
    raw = json.dumps(doc, indent=1, sort_keys=True, default=str).encode()
    h = hashlib.sha256(raw).hexdigest()
    p = RESULTS / f"{name}.{h[:16]}.json"
    p.write_bytes(raw)
    print(f"RESULT {name} sha256={h} file={p}")
    return h, p

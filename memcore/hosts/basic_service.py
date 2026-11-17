"""memcore memory service for the Basic-licence host path: serves POST /v1/pre_exec (bearer token in <out>/gateway.token,
0600) for the client's own agent runtime (memcore.hosts.basic_hook). No model behind it, no Agent Builder, no ES
inference API.

Start-up: the real-id file is required (fail closed); with --es the capability probe (memcore.capabilities) runs first,
reports the active tier on stderr and in <out>/capabilities.json, and a Basic tier pins the search to client-side fusion
so no licensed API is ever called. Memory comes from Elasticsearch (--es, local HashEmbedder vectors) or from a JSON
events file (--events, in memory).
Start:  python3 -m memcore.hosts.basic_service --out DIR --real-ids F (--events F | --es URL --es-auth-file F [--ingest F])
Stop:   touch DIR/gateway.stop
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _events(path):
    with open(path) as fh:
        ev = json.load(fh)
    ev = ev["events"] if isinstance(ev, dict) else ev
    if not isinstance(ev, list) or not all(isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("id", "ts", "text")) for e in ev):
        raise ValueError("events file: a list (or {'events': [...]}) of {id, ts, text}")
    return ev


def build(a):
    """-> (store, capability report or None). Separate from main() so a test kit can wrap the store."""
    from ..memtools import InMemoryStore, SearchMemoryStore
    if a.es:
        from .. import capabilities
        from ..adapters.elasticsearch_search import ElasticsearchSearch
        from ..fakes import HashEmbedder

        def auth():
            with open(a.es_auth_file) as fh:
                return fh.read().strip()
        search = ElasticsearchSearch(a.es, auth, tenant=a.tenant, ca_file=a.es_ca)
        rep = capabilities.probe(capabilities.es_get_json(search))
        capabilities.apply(rep, search)
        store = SearchMemoryStore(search, HashEmbedder(a.dims), logical=a.logical)
        if a.ingest:
            search.create_index(a.logical, a.dims, filter_fields=())
            store.add_events(_events(a.ingest))
        return store, rep
    store = InMemoryStore()
    for e in _events(a.events):
        store.add_event(e["id"], e.get("session", ""), e["ts"], e["text"])
    return store, None


def start_reprobe(a, store, rep, stderr=sys.stderr, clock=time.time):
    """Follow the licence tier after start-up (deploy part 3). Returns the running tracker, or None (no Elasticsearch, or
    --reprobe-interval-s 0). A tier change is written to <out>/capabilities.json (atomic) and logged on stderr."""
    search = getattr(store, "search", None)
    if rep is None or search is None or a.reprobe_interval_s <= 0:
        return None
    from ..reprobe import ReprobingTier

    def changed(new):
        print("capabilities changed: tier=%s licence=%s/%s" % (new["tier"], new["licence_type"], new["licence_status"]),
              file=stderr, flush=True)
        tmp = os.path.join(a.out, "capabilities.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(new, fh, indent=1)
        os.replace(tmp, os.path.join(a.out, "capabilities.json"))
    tier = ReprobingTier(search, interval_s=a.reprobe_interval_s, clock=clock, initial=rep, on_change=changed)
    tier.start()
    return tier


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--allow-bridge-host", default=None, help="the Docker bridge address, to serve containerised clients")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--real-ids", default=None, help="REQUIRED (or $MEMCORE_REAL_IDS_FILE)")
    ap.add_argument("--events", default=None)
    ap.add_argument("--es", default=None)
    ap.add_argument("--es-auth-file", default=None, help="file holding the Authorization header value")
    ap.add_argument("--es-ca", default=None)
    ap.add_argument("--tenant", default="basic")
    ap.add_argument("--logical", default="events")
    ap.add_argument("--dims", type=int, default=64)
    ap.add_argument("--ingest", default=None, help="events file to index into --es at start (creates the index)")
    ap.add_argument("--budget-s", type=float, default=20.0)
    ap.add_argument("--max-chars", type=int, default=4000)
    ap.add_argument("--reprobe-interval-s", type=float, default=60.0, help="with --es: re-read the licence tier this often and follow it "
                    "(Basic <-> licensed, either direction, node unreachable at start); 0 turns re-probing off")
    ap.add_argument("--max-concurrent", type=int, default=8, help="concurrent memory builds; beyond it the route answers "
                    "reason busy with the original prompt")
    return ap


def serve(a, store, rep, stderr=sys.stderr):
    from ..gateway import Gateway
    from ..memtools import BudgetedInjector
    gw = Gateway({}, a.out, host=a.host, port=a.port, allow_bridge_host=a.allow_bridge_host,
                 pre_exec_injector=BudgetedInjector(store, char_budget=a.max_chars), pre_exec_budget_s=a.budget_s,
                 pre_exec_max_chars=a.max_chars, pre_exec_max_concurrent=a.max_concurrent)
    if rep is not None:
        with open(os.path.join(a.out, "capabilities.json"), "w") as fh:
            json.dump(rep, fh, indent=1)
    gw.start()
    print("memcore basic_service on %s:%d tier=%s" % (gw.host, gw.port, rep["tier"] if rep else "n/a (no Elasticsearch)"),
          file=stderr, flush=True)
    return gw


def main(argv=None) -> int:
    ap = parser()
    a = ap.parse_args(argv)
    if bool(a.es) == bool(a.events):
        ap.error("give exactly one of --es or --events")
    if a.es and not a.es_auth_file:
        ap.error("--es needs --es-auth-file")
    from ..guard import DEFAULT_GUARD, RealIdsMissing, require_real_ids
    try:
        require_real_ids(DEFAULT_GUARD, a.real_ids, "memcore.hosts.basic_service")
    except RealIdsMissing as e:
        ap.exit(2, "%s\n" % e)
    store, rep = build(a)
    if rep is not None:
        print("capabilities: %s" % json.dumps(rep), file=sys.stderr, flush=True)
    gw = serve(a, store, rep)
    tier = start_reprobe(a, store, rep)
    while not os.path.exists(gw.stop_file):
        time.sleep(0.3)
    if tier is not None:
        tier.stop()
    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())

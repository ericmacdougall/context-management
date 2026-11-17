"""Operator tool for the durable poison alerts (goodqa#16096 deploy part 1). Needs the engine Postgres (psycopg).

  python -m engine.alerts list [--tenant T] [--status open|acked|released]
  python -m engine.alerts ack      --tenant T --session S --event E --by NAME   (seen; the input stays skipped)
  python -m engine.alerts release  --tenant T --session S --event E --by NAME   (cause fixed: RESUBMIT the input as a new input)
  python -m engine.alerts drain                                                  (move spooled alerts into Postgres)

`list` prints one JSON object per line and exits 1 when any open alert exists, so a cron or monitor can use the exit code.
`release` appends a NEW input with the same content (marked retry_of, retry_depth, at most 3 deep) and prints its event id; the
original stays skipped. See engine/ALERTS.md.

TRUST BOUNDARY: this tool runs with the engine's own database credentials and trusts the tenant and operator name it is given.
`--by` is a label for the audit columns, not authentication, and `--tenant` is not checked against who is calling. Whoever can run
it can act on every tenant's alerts, so it belongs to trusted operators on the engine host only; per-tenant operator access
would need an authenticated front end that this module does not provide."""
import argparse
import json
import sys


def main(argv=None) -> int:
    from . import pgstore
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--tenant")
    ls.add_argument("--status", default="open", choices=pgstore.ALERT_STATUSES)
    for name in ("ack", "release"):
        p = sub.add_parser(name)
        for k in ("tenant", "session", "event", "by"):
            p.add_argument("--" + k, required=True)
    sub.add_parser("drain")
    a = ap.parse_args(argv)
    if a.cmd == "drain":
        from . import activities
        print(json.dumps(activities.drain_alert_spool() or {"error": "drain failed"}))
        return 0
    if a.cmd == "list":
        rows = pgstore.list_alerts(a.tenant, a.status)
        for r in rows:
            print(json.dumps(dict(zip(("tenant", "session", "event", "kind", "detail", "last_attempt", "status", "first_seen", "last_seen", "retried_as"),
                                      [str(x) if hasattr(x, "isoformat") else x for x in r]))))
        return 1 if (rows and a.status == "open") else 0
    if a.cmd == "ack":
        ok = pgstore.ack_alert(a.tenant, a.session, a.event, "poison", a.by)
        print(json.dumps({"changed": ok, "action": "ack"}))
        return 0 if ok else 2
    try:
        new = pgstore.release_alert(a.tenant, a.session, a.event, "poison", a.by)
    except pgstore.RetryRefused as e:
        print(json.dumps({"changed": False, "action": "release", "refused": str(e)}))
        return 2
    print(json.dumps({"changed": True, "action": "release", "resubmitted_as": new}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

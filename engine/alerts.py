"""Operator tool for the durable poison alerts (goodqa#16096 deploy part 1). Needs the engine Postgres (psycopg).

  python -m engine.alerts list [--tenant T] [--status open|acked|released]
  python -m engine.alerts ack      --tenant T --session S --event E --by NAME   (seen; the input stays skipped)
  python -m engine.alerts release  --tenant T --session S --event E --by NAME   (cause fixed; the input is retried)

`list` prints one JSON object per line and exits 1 when any open alert exists, so a cron or monitor can use the exit code."""
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
    a = ap.parse_args(argv)
    if a.cmd == "list":
        rows = pgstore.list_alerts(a.tenant, a.status)
        for r in rows:
            print(json.dumps(dict(zip(("tenant", "session", "event", "kind", "detail", "count", "status", "first_seen", "last_seen"),
                                      [str(x) if hasattr(x, "isoformat") else x for x in r]))))
        return 1 if (rows and a.status == "open") else 0
    ok = pgstore.set_alert_status(a.tenant, a.session, a.event, "poison", "acked" if a.cmd == "ack" else "released", a.by)
    print(json.dumps({"changed": ok, "action": a.cmd}))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

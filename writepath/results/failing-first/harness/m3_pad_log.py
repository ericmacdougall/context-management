"""Review J M3, by test (main's request): can the seal pads reach the PostgreSQL server log? UNSTAMPED; owner a5bed869.
Throwaway pads (fresh random keys, never the authority's key file) go to wp_seal.install, as wp_owner, exactly as
wp/setup.py _seal_key installs them, in calls that FAIL on purpose (key id '': the function raises 22023 'seal key
malformed', so nothing is installed). The node's log since the test started (docker logs of the conf PG container;
logging_collector is off, the log is the container's stderr) is then searched by sha256 of every 128-hex run in it
against sha256 of each pad's hex. No pad, key or log line is printed; the output is one JSON line of counts.
  server   the call as the code makes it (psycopg server-side binding, parameters sent apart): want 0 pad hits
  client   the same call with client-side binding (values inlined into the statement text): POSITIVE CONTROL, want
           a hit for each pad, which shows the search finds a pad that is in the log
  errors_logged  'seal key malformed' ERROR lines since the start: want 2 (the log is read and errors reach it)
  success_control  review 42cfd11 R7: with the session at log_min_duration_statement 0 and log_parameter_max_length -1,
           a SUCCESSFUL parameterised statement carrying two fresh pads (as hex text): POSITIVE CONTROL, want 2 hits
  success_quiet    the same inside wp.setup.quiet (the transaction the install now runs in): want 0 hits
  sampled_control  with the session at log_transaction_sample_rate 1 (every transaction's statements logged; decided
           when a transaction starts), a statement with two fresh pads inlined (client-side binding): want 2 hits
  sampled_quiet    the same inside wp.setup.quiet, which sets the rate to 0 at session level BEFORE its BEGIN: want 0
Exit 0 when the claim holds (server 0 hits, client 2 hits, 2 errors logged, success_control 2, success_quiet 0,
sampled_control 2, sampled_quiet 0), 1 otherwise.
usage (from a pinned tree, WP_HOME set, after wp.setup): python results/failing-first/harness/m3_pad_log.py"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.getcwd())
HEX128 = re.compile(rb"[0-9a-fA-F]{128}")
SETTINGS = ("log_statement", "log_parameter_max_length", "log_parameter_max_length_on_error",
            "log_min_error_statement", "log_min_duration_statement", "logging_collector", "log_destination")


def pad_hits(log_bytes, pad_hashes):
    """How many of pad_hashes (sha256 hex of a pad's lowercase hex) occur as a 128-hex run in log_bytes."""
    seen = {hashlib.sha256(m.group(0).lower()).hexdigest() for m in HEX128.finditer(log_bytes)}
    return sum(1 for h in pad_hashes if h in seen)


def _attempt(conn, ip, op, client):
    import psycopg
    cur = psycopg.ClientCursor(conn) if client else conn.cursor()
    try:
        cur.execute("select wp_seal.install(%s, %s, %s)", ("", ip, op))
        return "no error"
    except psycopg.Error as e:
        return e.sqlstate


def main():
    from wp import config, db, sealer
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.admin()
    conn.execute("set role wp_owner")
    settings = dict(conn.execute("select name, setting from pg_settings where name = any(%s)", (list(SETTINGS),)).fetchall())
    out, hashes = {"settings": settings}, {}
    for variant in ("server", "client"):
        ip, op = sealer.pads(os.urandom(32))
        hashes[variant] = [hashlib.sha256(p.hex().encode()).hexdigest() for p in (ip, op)]
        out[f"{variant}_sqlstate"] = _attempt(conn, ip, op, variant == "client")
        del ip, op
    conn.execute("reset role")
    from wp import setup
    conn.execute("set log_min_duration_statement = 0")          # session level, superuser: the R7 setting
    conn.execute("set log_parameter_max_length = -1")
    for variant in ("success_control", "success_quiet"):
        ip, op = sealer.pads(os.urandom(32))
        hashes[variant] = [hashlib.sha256(p.hex().encode()).hexdigest() for p in (ip, op)]
        q = "select length(decode(%s, 'hex')) + length(decode(%s, 'hex'))"
        if variant == "success_quiet":
            with setup.quiet(conn):
                out[f"{variant}_result"] = conn.execute(q, (ip.hex(), op.hex())).fetchone()[0]
        else:
            out[f"{variant}_result"] = conn.execute(q, (ip.hex(), op.hex())).fetchone()[0]
        del ip, op
    conn.execute("reset log_min_duration_statement")
    conn.execute("reset log_parameter_max_length")
    import psycopg
    conn.execute("set log_transaction_sample_rate = 1")         # session level, superuser: every transaction sampled
    for variant in ("sampled_control", "sampled_quiet"):
        ip, op = sealer.pads(os.urandom(32))
        hashes[variant] = [hashlib.sha256(p.hex().encode()).hexdigest() for p in (ip, op)]
        q = "select length(decode(%s, 'hex')) + length(decode(%s, 'hex'))"
        if variant == "sampled_quiet":
            with setup.quiet(conn):
                out[f"{variant}_result"] = psycopg.ClientCursor(conn).execute(q, (ip.hex(), op.hex())).fetchone()[0]
        else:
            out[f"{variant}_result"] = psycopg.ClientCursor(conn).execute(q, (ip.hex(), op.hex())).fetchone()[0]
        del ip, op
    out["sample_rate_after_quiet"] = conn.execute("select current_setting('log_transaction_sample_rate')").fetchone()[0]
    conn.execute("reset log_transaction_sample_rate")
    time.sleep(1)
    r = subprocess.run(["docker", "logs", "--since", since, config.PG_CONTAINER], capture_output=True, timeout=120)
    log = r.stdout + r.stderr
    out["log_read"] = r.returncode == 0
    out["errors_logged"] = sum(1 for ln in log.splitlines() if b"ERROR:" in ln and b"seal key malformed" in ln)
    out["statement_lines"] = sum(1 for ln in log.splitlines() if b"STATEMENT:" in ln and b"wp_seal.install" in ln)
    for variant, hs in hashes.items():
        out[f"{variant}_pad_hits"] = pad_hits(log, hs)
    out["holds"] = (out["log_read"] and out["errors_logged"] == 2 and out["server_pad_hits"] == 0
                    and out["client_pad_hits"] == 2 and out["success_control_pad_hits"] == 2
                    and out["success_quiet_pad_hits"] == 0 and out["sampled_control_pad_hits"] == 2
                    and out["sampled_quiet_pad_hits"] == 0)
    print("m3_pad_log", json.dumps(out, sort_keys=True))
    return 0 if out["holds"] else 1


if __name__ == "__main__":
    sys.exit(main())

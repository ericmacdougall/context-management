"""Downgrade guards for the deploy path (goodqa#16096 deploy part 2). Pure: no Temporal, no Postgres, no Docker.

Temporal itself refuses a server image NEWER than the schema (skipped update), but starts an OLDER image on a NEWER schema
and keeps running (engine/NOTES.md, F7 control; server 1.30.9 on schema 1.19). The same holds one level up: an older
worker given a session state written by newer code either crashes by accident (KeyError, F6: 4 of 4 sessions stuck) or,
where field names happen to line up, runs and writes back a state it does not understand. Both are refused here, loudly and
by name, BEFORE the older code touches the newer data.

  check_schema(expected, actual)   a server image whose expected Temporal schema is older than the database's -> SchemaTooNew
  check_state(state, supported)    a session state with a version newer than this worker supports            -> StateTooNew

A refusal is never a delete or a state rewrite: the data stays as the newer code left it, and the session resumes when a
newer worker (or the restored snapshot's own image) takes it. See engine/DEPLOY.md for the rollback criteria."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Mapping, Optional


class DowngradeRefused(RuntimeError):
    """Base: the running code is older than the data it was asked to run against."""


class SchemaTooNew(DowngradeRefused):
    """The database schema is newer than the server image expects: an older image must not be started on it."""


class StateTooNew(DowngradeRefused):
    """The session state was written by a newer worker than this one."""


def version_tuple(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        raise ValueError("not a dotted numeric version: %r" % (v,)) from None


def check_schema(expected: Mapping[str, str], actual: Mapping[str, str]) -> dict:
    """expected: what the image to be started wants {"main": "1.18", "visibility": "1.13"}; actual: what the databases hold.
    Raises SchemaTooNew when either actual is newer than expected. An actual OLDER than expected is NOT refused here: it is
    the normal upgrade (the schema tool runs first), and Temporal refuses an unmigrated schema on its own."""
    for key in ("main", "visibility"):
        if key not in expected or key not in actual:
            raise ValueError("both schemas (main, visibility) must be given: missing %r" % key)
    for key in ("main", "visibility"):
        if version_tuple(actual[key]) > version_tuple(expected[key]):
            raise SchemaTooNew("image expects %s schema %s, database is at %s: restore the pre-upgrade snapshot (same database "
                               "names) instead of starting the older image on the upgraded database"
                               % (key, expected[key], actual[key]))
    return {"expected": dict(expected), "actual": dict(actual)}


def check_state(state: Optional[Mapping], supported_ver: int) -> int:
    """The state version carried in a session's workflow input (absent means 1). Raises StateTooNew when it is newer than
    `supported_ver`. Returns the state's version."""
    if not isinstance(state, Mapping):
        return 1
    ver = state.get("ver", 1)
    if not isinstance(ver, int) or isinstance(ver, bool):
        raise StateTooNew("session state has a non-integer version %r: refusing to run on it" % (ver,))
    if ver > supported_ver:
        raise StateTooNew("session state is version %d, this worker supports up to %d: refusing; the session stays as it is "
                          "until a newer worker takes it (roll forward, do not rewrite the state)" % (ver, supported_ver))
    return ver


def main(argv=None) -> int:
    """Deploy preflight: `python -m engine.deploy_guard schema --expected-main 1.18 --expected-visibility 1.13
    --actual-main 1.19 --actual-visibility 1.14`. Exit 0 = safe to start the image, 3 = refused (prints why)."""
    ap = argparse.ArgumentParser(description=main.__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schema")
    for k in ("expected-main", "expected-visibility", "actual-main", "actual-visibility"):
        s.add_argument("--" + k, required=True)
    st = sub.add_parser("state")
    st.add_argument("--state-ver", type=int, required=True)
    st.add_argument("--supported", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "schema":
            out = check_schema({"main": a.expected_main, "visibility": a.expected_visibility},
                               {"main": a.actual_main, "visibility": a.actual_visibility})
        else:
            out = {"state_ver": check_state({"ver": a.state_ver}, a.supported)}
    except DowngradeRefused as e:
        print(json.dumps({"ok": False, "refused": type(e).__name__, "why": str(e)}))
        return 3
    print(json.dumps({"ok": True, **out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

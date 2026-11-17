"""Downgrade guards for the deploy path (goodqa#16096 deploy part 2). Pure: no Temporal, no Postgres, no Docker.

Temporal itself refuses a server image NEWER than the schema (skipped update), but starts an OLDER image on a NEWER schema
and keeps running (engine/NOTES.md, F7 control; server 1.30.9 on schema 1.19). The same holds one level up: an older
worker given a session state written by newer code either crashes by accident (KeyError, F6: 4 of 4 sessions stuck) or,
where field names happen to line up, runs and writes back a state it does not understand. Both are refused here, loudly and
by name.

  check_schema(expected, actual)   a server image whose expected Temporal schema is older than the database's -> SchemaTooNew
  check_state(state, supported)    a session state newer than this worker supports                           -> StateTooNew
                                   a state that is not an object, or has an unusable version                  -> InvalidState

Where check_state runs: FIRST in `SessionWorkflow.__init__` (@workflow.init) of every workflow variant in engine/wf/, before the
state is copied, migrated or indexed. That is the only place the session state is loaded: signal and query handlers cannot run
before the instance exists, activities receive ids and scalars (never the state dictionary), and a standalone Replayer constructs
the same class, so replay is covered by the same line. A refusal is a workflow TASK failure (the exceptions are RuntimeErrors,
not ApplicationErrors): the run is not failed, its history is untouched, the task retries, and the session proceeds when a worker
that supports the version polls the queue.

What it does NOT do: the `ver` marker is an ordinary field of the workflow input. A caller that can start a workflow or
continue-as-new with arbitrary input can label newer-shaped state as version 1 and defeat the check; only the engine's own driver
(ctl) writes it, and the marker is authenticated by nothing. Close it by letting only the engine API start sessions. A state with
no `ver` is treated as version 1 (the legacy shape, explicit policy).

A refusal is never a delete or a state rewrite. See engine/DEPLOY.md for the rollback criteria."""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Mapping, Optional


class DowngradeRefused(RuntimeError):
    """Base: the running code is older than the data it was asked to run against."""


class SchemaTooNew(DowngradeRefused):
    """The database schema is newer than the server image expects: an older image must not be started on it."""


class StateTooNew(DowngradeRefused):
    """The session state was written by a newer worker than this one."""


class InvalidState(DowngradeRefused):
    """The workflow input is not a session state this worker can reason about (not an object, or an unusable version)."""


class InvalidVersion(DowngradeRefused, ValueError):
    """A schema version outside the grammar digits(.digits)* (one optional leading 'v')."""



_VERSION = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)")


def version_tuple(v) -> tuple:
    m = _VERSION.fullmatch(str(v).strip())
    if m is None:
        raise InvalidVersion("not a dotted numeric version: %r" % (v,))
    return tuple(int(x) for x in m.group(1).split("."))


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


def check_state(state, supported_ver: int) -> int:
    """The version carried in a session's workflow input. Absent means 1 (the legacy shape: explicit policy). Raises StateTooNew
    when newer than `supported_ver`, InvalidState for a non-mapping or a version that is not a positive integer (bool, float,
    string, zero and negatives included). Returns the version."""
    if not isinstance(state, Mapping):
        raise InvalidState("session state is %s, not an object: refusing to run on it" % type(state).__name__)
    ver = state.get("ver", 1)
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise InvalidState("session state has an unusable version %r (a positive integer is required): refusing" % (ver,))
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
    except DowngradeRefused as e:      # includes a malformed version: fail closed with the same structured refusal
        print(json.dumps({"ok": False, "refused": type(e).__name__, "why": str(e)}))
        return 3
    print(json.dumps({"ok": True, **out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Driver plumbing for the systems rows: imports the owner's memsys/evolution (system under test) read-only from
`git archive` exports, points them at THIS stream's own Postgres (emlab-pg-rep-16096) and own Elasticsearch
(emlab-es-rep-16096) without editing them, and provides stamping. No owner experiment script (exp/*) is imported.
Run under ~/emlab-16096/systems/venv (psycopg)."""
import base64
import http.client
import json
import os
import sys
import time

REP = os.path.expanduser("~/emlab-16096/rep")
SUT_COMMIT = os.environ.get("SUT_COMMIT", "a13b524")
SUT = os.path.join(REP, "sut", "systems-%s" % SUT_COMMIT, "systems")
os.environ["MEMSYS_HOME"] = os.path.join(REP, "memsys_home")
sys.path.insert(0, SUT)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import repc  # noqa: E402
from memsys import db, esc, esx  # noqa: E402


class RepES(esc.ES):
    """The owner's client with transport swapped to the own node (http, basic auth from a 0600 file)."""

    def __init__(self, admin=False):
        pw = open(os.path.join(REP, "secrets", "es_pw")).read().strip()
        self._auth = "Basic " + base64.b64encode(("elastic:" + pw).encode()).decode()
        del pw
        self.conn = None
        self.ctx = None

    def _c(self):
        if self.conn is None:
            self.conn = http.client.HTTPConnection("127.0.0.1", 19280, timeout=120)
        return self.conn


def conns():
    return db.connect(autocommit=True), db.connect(autocommit=True, application_name="memsys-lock"), RepES()


def pg_version(conn):
    return conn.execute("show server_version").fetchone()[0]


def fingerprint(conn, es=None):
    fp = {"sut": {"tree": "feat/systems-skeleton %s (git archive, unmodified)" % SUT_COMMIT, "path": SUT},
          "postgres": {"container": "emlab-pg-rep-16096", "version": pg_version(conn)}, "host": repc.host_fingerprint()}
    if es is not None:
        st, j = es.req("GET", "/")
        st2, l = es.req("GET", "/_license")
        fp["elasticsearch"] = {"node": "emlab-es-rep-16096", "version": j.get("version", {}).get("number"),
                               "licence": {k: l.get("license", {}).get(k) for k in ("type", "uid", "expiry_date")}}
    return fp


def stamp(script, params, conn, es=None):
    s = repc.stamp(script, params)
    s["environment"] = fingerprint(conn, es)
    return s


save = repc.save

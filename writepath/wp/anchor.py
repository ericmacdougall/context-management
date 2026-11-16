"""Row 6: an anchor of every session's chain head held OUTSIDE the database, so a lost tail, a regressed head or a
rewritten history is detected even when what remains in the database is a perfectly consistent chain.

AnchorStore is the interface; a client deployment implements it over write-once storage it already runs (object store
with object lock / WORM retention, a transparency log, or a second system of record). FileAnchorStore is the LAB
STAND-IN: an append-only JSON-lines file on the host filesystem, outside the Postgres data directory and container,
fsynced per record. It does not itself resist an attacker with host access.

What the check proves: for every anchored (session, seq, link), the database still holds that seq, the stored link
equals the anchored link, and the keyed chain verifies from 1 to the database head. What it cannot prove: events
committed after the newest anchor (the recovery-point window equals the anchor cadence; anchor(..) after each
acknowledged batch makes it zero for acknowledged writes)."""
import json
import os
import time

from . import config, log


class AnchorStore:
    KIND = "abstract"
    STAND_IN = True

    def put(self, records):
        raise NotImplementedError

    def latest(self, tenant):
        """{session_id: {"seq", "link", "at"}} newest anchored head per session."""
        raise NotImplementedError

    def describe(self):
        return {"kind": self.KIND, "stand_in": self.STAND_IN}


class FileAnchorStore(AnchorStore):
    KIND = "lab-append-only-file"
    STAND_IN = True

    def __init__(self, path=None):
        self.path = path or (config.HOME / "anchors" / "anchors.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def put(self, records):
        with open(self.path, "a") as f:
            for r in records:
                f.write(json.dumps(r, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def latest(self, tenant):
        out = {}
        if not self.path.exists():
            return out
        with open(self.path) as f:
            for line in f:
                r = json.loads(line)
                if r["tenant"] == tenant and (r["session"] not in out or r["seq"] >= out[r["session"]]["seq"]):
                    out[r["session"]] = {"seq": r["seq"], "link": r["link"], "at": r["at"]}
        return out


def anchor(conn, store, tenant):
    """Record every session's current head (seq, link) for the tenant. Returns the records written."""
    rows = conn.execute("select session_id, next_seq - 1, last_hash from chains where tenant_id=%s and next_seq > 1", (tenant,)).fetchall()
    recs = [{"tenant": tenant, "session": s, "seq": int(q), "link": h, "at": time.time()} for s, q, h in rows]
    if recs:
        store.put(recs)
    return recs


def check(conn, ks, store, tenant):
    """{"ok", "findings": [{"session", "kind", ...}]}. kinds: truncated (db head below the anchor), rewritten (the link
    at the anchored seq differs), chain_invalid (keyed chain does not verify), missing_session."""
    findings = []
    heads = dict((s, (int(q), h)) for s, q, h in conn.execute(
        "select session_id, next_seq - 1, last_hash from chains where tenant_id=%s", (tenant,)).fetchall())
    for sess, a in store.latest(tenant).items():
        if sess not in heads:
            findings.append({"session": sess, "kind": "missing_session", "anchored_seq": a["seq"]})
            continue
        row = conn.execute("select hash from events where tenant_id=%s and session_id=%s and seq=%s", (tenant, sess, a["seq"])).fetchone()
        if row is None or heads[sess][0] < a["seq"]:
            findings.append({"session": sess, "kind": "truncated", "anchored_seq": a["seq"], "db_head_seq": heads[sess][0]})
        elif row[0] != a["link"]:
            findings.append({"session": sess, "kind": "rewritten", "anchored_seq": a["seq"]})
    for sess in heads:
        ok, n, _ = log.verify_chain(conn, tenant, sess, ks)
        if not ok:
            findings.append({"session": sess, "kind": "chain_invalid", "events": n})
    return {"ok": not findings, "findings": findings}

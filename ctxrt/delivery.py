"""P5: inbox delivery end to end. memcore's InboxService provides claim / renew / confirm with a fencing token; this layer adds what it lacks:
  * the TURN COMMIT: confirm, the transcript row (with the delivered block) and the lineage-generation check happen in ONE store transaction, so the
    store (not the caller) refuses a stale token, and a crash can never leave "sent but unconfirmed" or "confirmed but not recorded";
  * lineage-scoped inbox sessions: a checkpoint rollback moves the lineage to a NEW inbox session id and re-enqueues every finding that is not in the
    checkpoint cursor; a fork gets its own inbox session seeded the same way (memcore has no un-deliver, and enqueue dedupes on (session, finding_id));
  * the policy re-check at delivery (memcore has no reject transition for an item denied at delivery: it would be re-offered until it expires, so the
    item is marked dropped here and the gap is reported upstream).
Delivery truth = the committed transcript of the surviving lineage, never inbox rows."""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Callable, Dict, List, Optional

from memcore.inbox import InboxItem, InboxService, RequirementGraph, render_block
from ctxrt.rundb import Fenced, NestStore
from ctxrt.vendor.pol.enforce import Enforcer, PolicyDenied

SCHEMA = """
CREATE TABLE IF NOT EXISTS d_findings(finding_id TEXT PRIMARY KEY, logical TEXT, item TEXT, label TEXT, created REAL);
CREATE TABLE IF NOT EXISTS d_lineages(lineage TEXT PRIMARY KEY, logical TEXT, inbox_sid TEXT, gen INT, status TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS d_transcript(lineage TEXT, seq INT, turn_id TEXT, gen INT, finding_ids TEXT, block TEXT, alive INT, PRIMARY KEY(lineage, turn_id));
"""


class Delivery:
    def __init__(self, store: NestStore, clock: Callable[[], float] = time.time, lease_s: float = 0.05, enforcer: Optional[Enforcer] = None,
                 sender=None, recipient_of: Optional[Callable[[str], object]] = None, label_of: Optional[Callable[[str], object]] = None):
        self.store, self.clock, self.lease_s = store, clock, lease_s
        store.db.executescript(SCHEMA)
        self.graph = RequirementGraph({"task": {"req1"}})
        self.svc = InboxService(store, self.graph, clock, lease_s=lease_s)
        self.enf, self.sender, self.recipient_of, self.label_of = enforcer, sender, recipient_of, label_of
        self.events: List[dict] = []                 # witness log: (t, kind, who)

    def ev(self, kind, who, **kw):
        self.events.append(dict(t=time.perf_counter(), kind=kind, who=who, **kw))

    # ---- lineages
    def new_lineage(self, logical: str, lineage: str, parent: Optional[str] = None) -> str:
        sid = "inb:%s:g0" % lineage
        with self.store.transaction():
            self.store.db.execute("INSERT OR REPLACE INTO d_lineages VALUES(?,?,?,0,'live',?)", (lineage, logical, sid, parent))
            self.svc.register(sid, "host", "task", ["req1"])
        return sid

    def lineage(self, lin):
        r = self.store.db.execute("SELECT lineage,logical,inbox_sid,gen,status FROM d_lineages WHERE lineage=?", (lin,)).fetchone()
        return dict(zip(("lineage", "logical", "inbox_sid", "gen", "status"), r))

    def live_lineages(self, logical):
        return [r[0] for r in self.store.db.execute("SELECT lineage FROM d_lineages WHERE logical=? AND status='live'", (logical,)).fetchall()]

    # ---- findings
    def _enqueue(self, sid: str, fid: str, item: dict) -> bool:
        it = InboxItem(**dict(item, session_id=sid))
        return self.svc.enqueue(it)

    def produce(self, logical: str, fid: str, quote: str = "q", label: Optional[dict] = None) -> None:
        now = self.clock()
        item = dict(finding_id=fid, source_event_ids=["ev-" + fid], quote=quote, trust_origin="transcript", item_ids=["req1"], created=now, expires=now + 3600.0)
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO d_findings VALUES(?,?,?,?,?)", (fid, logical, json.dumps(item), json.dumps(label), now))
            for lin in self.live_lineages(logical):
                self._enqueue(self.lineage(lin)["inbox_sid"], fid, item)      # fan-out: every live lineage gets its own copy

    def master(self, logical: str) -> List[str]:
        return [r[0] for r in self.store.db.execute("SELECT finding_id FROM d_findings WHERE logical=? ORDER BY created, finding_id", (logical,)).fetchall()]

    # ---- turn protocol
    def stored_turn(self, lin: str, turn_id: str):
        r = self.store.db.execute("SELECT block, finding_ids FROM d_transcript WHERE lineage=? AND turn_id=?", (lin, turn_id)).fetchone()
        return None if r is None else {"block": r[0], "finding_ids": json.loads(r[1])}

    def begin_turn(self, lin: str, turn_id: str, limit: int = 5):
        """Returns {"committed": {...}} when this turn was already committed (a crash after commit: replay it, claim nothing), else the claim."""
        with self.store.transaction():
            st = self.stored_turn(lin, turn_id)
            if st is not None:
                return {"committed": st}
            L = self.lineage(lin)
            cl = self.svc.claim(L["inbox_sid"], turn_id, limit=limit)
            self.ev("claim", turn_id, n=len(cl))
            return {"claim": cl, "token": cl.token, "gen": L["gen"], "sid": L["inbox_sid"]}

    def _policy_split(self, lin: str, items: List[InboxItem]):
        """Policy re-evaluated NOW against the current policy and the current recipient entry. Returns (allowed, denied[(item, reason)])."""
        if self.enf is None:
            return list(items), []
        allowed, denied = [], []
        rcp = self.recipient_of(lin)
        for it in items:
            try:
                self.enf.check(self.sender, "send_inbox", [self.label_of(it.finding_id)], {"recipient": rcp})
                allowed.append(it)
            except PolicyDenied as e:
                denied.append((it, e.decision.reason))
        return allowed, denied

    def commit_turn(self, lin: str, turn_id: str, begun: dict) -> dict:
        """ONE transaction: generation check, policy re-check, confirm with the token, transcript row. A stale token or a superseded generation
        raises Fenced and nothing is written."""
        if "committed" in begun:
            return begun["committed"]
        cl, token, gen, sid = begun["claim"], begun["token"], begun["gen"], begun["sid"]
        with self.store.transaction():
            st = self.stored_turn(lin, turn_id)
            if st is not None:
                return st
            L = self.lineage(lin)
            if L["gen"] != gen or L["status"] != "live":
                self.ev("refused", turn_id, why="generation")
                raise Fenced("lineage %s generation %s != %s" % (lin, L["gen"], gen))
            allowed, denied = self._policy_split(lin, list(cl))
            for it, reason in denied:
                cur = self.store.get_item(sid, it.finding_id)
                cur.state = "dropped"
                self.store.put_item(cur)
                self.store.log_drop(sid, it.finding_id, "policy_denied_at_delivery:" + reason, self.clock())
            if allowed:
                n = self.svc.confirm(sid, turn_id, token)
                if n == 0:
                    self.ev("refused", turn_id, why="stale_token")
                    raise Fenced("stale claim token for %s" % turn_id)
            block = render_block(allowed, turn_id) if allowed else ""
            seq = self.store.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM d_transcript WHERE lineage=?", (lin,)).fetchone()[0]
            ids = [i.finding_id for i in allowed]
            self.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (lin, seq, turn_id, gen, json.dumps(ids), block))
            self.ev("commit", turn_id, n=len(ids))
            return {"block": block, "finding_ids": ids}

    def run_turn(self, lin, turn_id):
        return self.commit_turn(lin, turn_id, self.begin_turn(lin, turn_id))

    # ---- transcript truth
    def appearances(self, lin: str) -> Counter:
        c = Counter()
        for (ids,) in self.store.db.execute("SELECT finding_ids FROM d_transcript WHERE lineage=? AND alive=1", (lin,)).fetchall():
            for f in json.loads(ids):
                c[f] += 1
        return c

    def seq_now(self, lin):
        return self.store.db.execute("SELECT COALESCE(MAX(seq),0) FROM d_transcript WHERE lineage=? AND alive=1", (lin,)).fetchone()[0]

    # ---- checkpoint / rollback / fork (the cursor is the single source of truth)
    def checkpoint(self, lin: str) -> dict:
        with self.store.transaction():
            upto = self.seq_now(lin)
            cursor = sorted(self.appearances(lin))
            return {"lineage": lin, "upto": upto, "cursor": cursor}

    def rollback(self, lin: str, cp: dict) -> str:
        """Truncate the transcript to the checkpoint, move the lineage to a NEW generation and inbox session, re-enqueue every finding not in the cursor."""
        with self.store.transaction():
            L = self.lineage(lin)
            gen = L["gen"] + 1
            sid = "inb:%s:g%d" % (lin, gen)
            self.store.db.execute("UPDATE d_transcript SET alive=0 WHERE lineage=? AND seq>?", (lin, cp["upto"]))
            self.store.db.execute("UPDATE d_lineages SET gen=?, inbox_sid=? WHERE lineage=?", (gen, sid, lin))
            self.svc.register(sid, "host", "task", ["req1"])
            for fid in self.master(L["logical"]):
                if fid not in set(cp["cursor"]):
                    item = json.loads(self.store.db.execute("SELECT item FROM d_findings WHERE finding_id=?", (fid,)).fetchone()[0])
                    self._enqueue(sid, fid, item)
            self.ev("rollback", lin, gen=gen)
            return sid

    def fork(self, parent: str, child: str, cp: dict) -> str:
        with self.store.transaction():
            P = self.lineage(parent)
            sid = self.new_lineage(P["logical"], child, parent)
            rows = self.store.db.execute("SELECT seq,turn_id,gen,finding_ids,block FROM d_transcript WHERE lineage=? AND alive=1 AND seq<=?", (parent, cp["upto"])).fetchall()
            for seq, tid, g, ids, blk in rows:
                self.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (child, seq, "fork:" + tid, 0, ids, blk))
            for fid in self.master(P["logical"]):
                if fid not in set(cp["cursor"]):
                    item = json.loads(self.store.db.execute("SELECT item FROM d_findings WHERE finding_id=?", (fid,)).fetchone()[0])
                    self._enqueue(sid, fid, item)
            self.ev("fork", child)
            return sid


# ================================================================ controls (each is judged only on the schedule it can fail)
class TokenlessDelivery(Delivery):
    """CONTROL (paused turn): confirm by turn id only, and the transcript row is written whether or not the confirm matched; no generation check."""

    def commit_turn(self, lin, turn_id, begun):
        if "committed" in begun:
            return begun["committed"]
        cl, gen, sid = begun["claim"], begun["gen"], begun["sid"]
        with self.store.transaction():
            for it in self.store.items(sid, ["claimed"]):
                if it.claim_turn == turn_id:             # by turn id only: another turn's token is not consulted
                    it.state = "delivered"
                    self.store.put_item(it)
            block = render_block(list(cl), turn_id) if cl else ""
            seq = self.store.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM d_transcript WHERE lineage=?", (lin,)).fetchone()[0]
            ids = [i.finding_id for i in cl]
            self.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (lin, seq, turn_id, gen, json.dumps(ids), block))
            self.ev("commit", turn_id, n=len(ids))
            return {"block": block, "finding_ids": ids}


class SendThenConfirmDelivery(Delivery):
    """CONTROL (crash): the block is sent (transcript row) first and confirmed afterwards in a second step; a crash between the two leaves the item claimed
    and it is delivered again after the lease lapses. `crash_after_send` raises between the steps."""
    crash_after_send = False

    def commit_turn(self, lin, turn_id, begun):
        if "committed" in begun:
            return begun["committed"]
        cl, token, gen, sid = begun["claim"], begun["token"], begun["gen"], begun["sid"]
        with self.store.transaction():
            block = render_block(list(cl), turn_id) if cl else ""
            seq = self.store.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM d_transcript WHERE lineage=?", (lin,)).fetchone()[0]
            ids = [i.finding_id for i in cl]
            self.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (lin, seq, turn_id, gen, json.dumps(ids), block))
        if self.crash_after_send and turn_id == "A":
            raise RuntimeError("crash between send and confirm")
        if cl:
            self.svc.confirm(sid, turn_id, token)
        return {"block": block, "finding_ids": ids}


class CursorOutsideCheckpointDelivery(Delivery):
    """CONTROL (rollback): the delivery state lives in the inbox only; a rollback truncates the transcript but does not rewind delivery."""

    def rollback(self, lin, cp):
        with self.store.transaction():
            self.store.db.execute("UPDATE d_transcript SET alive=0 WHERE lineage=? AND seq>?", (lin, cp["upto"]))
            return self.lineage(lin)["inbox_sid"]


class SharedNativeIdDelivery(Delivery):
    """CONTROL (fork): the child shares the parent's inbox session and delivered state (one native session id for both branches)."""

    def fork(self, parent, child, cp):
        with self.store.transaction():
            P = self.lineage(parent)
            self.store.db.execute("INSERT OR REPLACE INTO d_lineages VALUES(?,?,?,0,'live',?)", (child, P["logical"], P["inbox_sid"], parent))
            rows = self.store.db.execute("SELECT seq,turn_id,gen,finding_ids,block FROM d_transcript WHERE lineage=? AND alive=1 AND seq<=?", (parent, cp["upto"])).fetchall()
            for seq, tid, g, ids, blk in rows:
                self.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (child, seq, "fork:" + tid, 0, ids, blk))
            return P["inbox_sid"]

    def produce(self, logical, fid, quote="q", label=None):          # one inbox per native id: enqueue once
        now = self.clock()
        item = dict(finding_id=fid, source_event_ids=["ev-" + fid], quote=quote, trust_origin="transcript", item_ids=["req1"], created=now, expires=now + 3600.0)
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO d_findings VALUES(?,?,?,?,?)", (fid, logical, json.dumps(item), json.dumps(label), now))
            sids = {self.lineage(l)["inbox_sid"] for l in self.live_lineages(logical)}
            for s in sids:
                self._enqueue(s, fid, item)


class EnqueueOnlyPolicyDelivery(Delivery):
    """CONTROL (policy): policy was checked at enqueue and is trusted at delivery."""

    def _policy_split(self, lin, items):
        return list(items), []

"""P5: inbox delivery end to end. memcore's InboxService provides claim / renew / confirm with a fencing token; this layer adds what it lacks:
  * the TURN COMMIT: confirm, the transcript row (with the delivered block) and the lineage-generation check happen in ONE store transaction, so the
    store (not the caller) refuses a stale token. Only CONFIRMED ids are recorded, and the commit is refused unless every claimed id was confirmed
    (a partial confirm, e.g. after a takeover of part of the claim, never records an item another turn delivered);
  * lineage-scoped inbox sessions: a checkpoint rollback moves the lineage to a NEW inbox session id and re-enqueues every finding that is not in the
    checkpoint cursor; a fork gets its own inbox session seeded the same way (memcore has no un-deliver, and enqueue dedupes on (session, finding_id));
  * ONE durable path for every finding: producers and the analyst both write d_findings (keyed by logical session and finding id) and the origin of
    every copy in the same transaction as the enqueue, so rollback and fork re-offer analyst findings too;
  * a turn id is scoped to (lineage, generation): a turn id reused after a rollback never replays the dead turn's block;
  * the policy re-check at delivery goes through a PolicyBridge that derives sender, recipient and label SERVER-SIDE. No bridge means NOTHING is
    delivered (fail closed). The vendored policy point still has the faults of research/policy-review-515e594.md, so every policy arm is NOT PROVEN
    until the rescued policy point lands;
  * retractions: a finding withdrawn after delivery (its run was stopped) is rendered as a retraction record in the next turn's block.
Delivery truth = the committed transcript of the surviving lineage, never inbox rows."""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Callable, Dict, List, Optional

from memcore.inbox import InboxItem, InboxService, RequirementGraph, render_block, render_data_block
from ctxrt.rundb import Fenced, NestStore, SCHEMA as RUN_SCHEMA
from ctxrt.vendor.pol.enforce import Enforcer, PolicyDenied
from ctxrt.vendor.pol.labels import Label

SCHEMA = """
CREATE TABLE IF NOT EXISTS d_findings(logical TEXT, finding_id TEXT, item TEXT, label TEXT, created REAL, PRIMARY KEY(logical, finding_id));
CREATE TABLE IF NOT EXISTS d_origin(logical TEXT NOT NULL, finding_id TEXT NOT NULL, author_agent TEXT NOT NULL, author_model TEXT NOT NULL, run_key TEXT, PRIMARY KEY(logical, finding_id, author_agent));
CREATE TABLE IF NOT EXISTS d_lineages(lineage TEXT PRIMARY KEY, logical TEXT, inbox_sid TEXT, gen INT, status TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS d_transcript(lineage TEXT, seq INT, turn_id TEXT, gen INT, finding_ids TEXT, block TEXT, alive INT, PRIMARY KEY(lineage, gen, turn_id));
CREATE TABLE IF NOT EXISTS d_retracted(lineage TEXT, finding_id TEXT, PRIMARY KEY(lineage, finding_id));
"""


class PolicyNotConfigured(Exception):
    pass


class PolicyBridge:
    """Derives sender, recipient and label from SERVER-SIDE records, never from request context. `sender_of(author_agent)` and `recipient_of(lineage)` are
    directory lookups owned by the host; the label is the one stored with the finding (Delivery re-reads it at every delivery). The rescued policy
    point is expected to implement this interface; until then results that depend on it are NOT PROVEN."""

    def __init__(self, enforcer: Enforcer, sender_of: Callable[[str], object], recipient_of: Callable[[str], object]):
        self.enf, self.sender_of, self.recipient_of = enforcer, sender_of, recipient_of

    def check(self, lineage: str, author_agent: str, label: Label) -> None:
        self.enf.check(self.sender_of(author_agent), "send_inbox", [label], {"recipient": self.recipient_of(lineage)})


class AllowAllForTests:
    """Explicit opt-in for tests that are not about policy. Never the default."""

    def check(self, lineage, author_agent, label):
        return None


class Delivery:
    def __init__(self, store: NestStore, clock: Callable[[], float] = time.time, lease_s: float = 0.05, policy=None):
        self.store, self.clock, self.lease_s = store, clock, lease_s
        store.db.executescript(SCHEMA)
        store.db.executescript(RUN_SCHEMA)
        self.graph = RequirementGraph({"task": {"req1"}})
        self.svc = InboxService(store, self.graph, clock, lease_s=lease_s)
        self.policy = policy
        self.events: List[dict] = []

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

    # ---- findings: ONE durable path (used by producers and by the analyst write gate)
    def _enqueue(self, sid: str, logical: str, fid: str, item: dict) -> bool:
        new = self.svc.enqueue(InboxItem(**dict(item, session_id=sid)))
        for agent, model, run_key in self.store.db.execute("SELECT author_agent, author_model, run_key FROM d_origin WHERE logical=? AND finding_id=?", (logical, fid)).fetchall():
            self.store.db.execute("INSERT OR IGNORE INTO item_origin VALUES(?,?,?,?,?,1)", (sid, fid, agent, model, run_key))
        return new

    def ingest_finding(self, logical: str, item: dict, author: tuple, run_key: Optional[str] = None, label: Optional[dict] = None) -> bool:
        """Store the finding (logical, finding_id), every producer of it, and fan it out to every live lineage, in ONE transaction. Author agent and model
        are required (an item with no origin is refused)."""
        if not author or not author[0] or not author[1]:
            raise ValueError("an item with no author agent and model is refused")
        fid = item["finding_id"]
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO d_findings VALUES(?,?,?,?,?)", (logical, fid, json.dumps(item), json.dumps(label), item["created"]))
            self.store.db.execute("INSERT OR IGNORE INTO d_origin VALUES(?,?,?,?,?)", (logical, fid, author[0], author[1], run_key))
            new = False
            for lin in self.live_lineages(logical):
                new |= self._enqueue(self.lineage(lin)["inbox_sid"], logical, fid, item)
            return new

    def produce(self, logical: str, fid: str, quote: str = "q", label: Optional[dict] = None, author: tuple = ("producer-0", "m-producer")) -> bool:
        now = self.clock()
        item = dict(finding_id=fid, source_event_ids=["ev-" + fid], quote=quote, trust_origin="transcript", item_ids=["req1"], created=now, expires=now + 3600.0)
        return self.ingest_finding(logical, item, author, None, label)

    def master(self, logical: str) -> List[str]:
        return [r[0] for r in self.store.db.execute("SELECT finding_id FROM d_findings WHERE logical=? ORDER BY created, finding_id", (logical,)).fetchall()]

    def _item_json(self, logical, fid):
        return json.loads(self.store.db.execute("SELECT item FROM d_findings WHERE logical=? AND finding_id=?", (logical, fid)).fetchone()[0])

    # ---- turn protocol
    def stored_turn(self, lin: str, turn_id: str, gen: Optional[int] = None):
        gen = self.lineage(lin)["gen"] if gen is None else gen
        r = self.store.db.execute("SELECT block, finding_ids FROM d_transcript WHERE lineage=? AND gen=? AND turn_id=? AND alive=1", (lin, gen, turn_id)).fetchone()
        return None if r is None else {"block": r[0], "finding_ids": json.loads(r[1])}

    def begin_turn(self, lin: str, turn_id: str, limit: int = 5):
        with self.store.transaction():
            L = self.lineage(lin)
            st = self.stored_turn(lin, turn_id, L["gen"])
            if st is not None:
                return {"committed": st}
            cl = self.svc.claim(L["inbox_sid"], turn_id, limit=limit)
            self.ev("claim", turn_id, n=len(cl))
            return {"claim": cl, "token": cl.token, "gen": L["gen"], "sid": L["inbox_sid"]}

    def _policy_split(self, lin: str, items: List[InboxItem], logical: str):
        if self.policy is None:
            return [], [(it, "no_policy_configured") for it in items]          # fail closed: nothing is delivered without a policy bridge
        allowed, denied = [], []
        for it in items:
            label = self.store.db.execute("SELECT label FROM d_findings WHERE logical=? AND finding_id=?", (logical, it.finding_id)).fetchone()
            lab = json.loads(label[0]) if label and label[0] else None
            lab = Label.from_dict(lab) if lab else Label(tenants=frozenset(["t1"]))
            authors = [r[0] for r in self.store.db.execute("SELECT author_agent FROM d_origin WHERE logical=? AND finding_id=?", (logical, it.finding_id)).fetchall()]
            try:
                for a in authors or [""]:
                    self.policy.check(lin, a, lab)
                allowed.append(it)
            except PolicyDenied as e:
                denied.append((it, e.decision.reason))
        return allowed, denied

    def _retractions(self, lin: str) -> List[str]:
        rows = self.store.db.execute("SELECT DISTINCT finding_id FROM withdrawn WHERE delivered=1 AND session_id LIKE ?", ("inb:%s:%%" % lin,)).fetchall()
        done = {r[0] for r in self.store.db.execute("SELECT finding_id FROM d_retracted WHERE lineage=?", (lin,)).fetchall()}
        delivered_here = set(self.appearances(lin))
        return [r[0] for r in rows if r[0] not in done and r[0] in delivered_here]

    def commit_turn(self, lin: str, turn_id: str, begun: dict) -> dict:
        """ONE transaction: generation check, policy re-check, confirm with the token, transcript row. A stale token, a partial confirm or a superseded
        generation raises Fenced and nothing is written."""
        if "committed" in begun:
            return begun["committed"]
        cl, token, gen, sid = begun["claim"], begun["token"], begun["gen"], begun["sid"]
        with self.store.transaction():
            L = self.lineage(lin)
            st = self.stored_turn(lin, turn_id, gen)
            if st is not None:
                return st
            if L["gen"] != gen or L["status"] != "live":
                self.ev("refused", turn_id, why="generation")
                raise Fenced("lineage %s generation %s != %s" % (lin, L["gen"], gen))
            allowed, denied = self._policy_split(lin, list(cl), L["logical"])
            for it, reason in denied:
                cur = self.store.get_item(sid, it.finding_id)
                cur.state = "dropped"
                self.store.put_item(cur)
                self.store.log_drop(sid, it.finding_id, "policy_denied_at_delivery:" + reason, self.clock())
            if allowed:
                n = self.svc.confirm(sid, turn_id, token)
                if n != len(allowed):                               # partial confirm: another turn took part of the claim over
                    self.ev("refused", turn_id, why="partial_or_stale_confirm")
                    raise Fenced("confirmed %d of %d claimed items for %s" % (n, len(allowed), turn_id))
            retr = self._retractions(lin)
            block = render_block(allowed, turn_id) if allowed else ""
            if retr:
                rb = render_data_block([{"retracted_finding_id": f, "note": "withdrawn: the run that produced it was stopped; disregard"} for f in retr], turn_id + "-retraction", label="retractions")
                block = (block + "\n" + rb) if block else rb
                for f in retr:
                    self.store.db.execute("INSERT OR IGNORE INTO d_retracted VALUES(?,?)", (lin, f))
            seq = self.store.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM d_transcript WHERE lineage=?", (lin,)).fetchone()[0]
            ids = [i.finding_id for i in allowed]
            self.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (lin, seq, turn_id, gen, json.dumps(ids), block))
            self.ev("commit", turn_id, n=len(ids))
            return {"block": block, "finding_ids": ids}

    def run_turn(self, lin, turn_id, limit: int = 5):
        return self.commit_turn(lin, turn_id, self.begin_turn(lin, turn_id, limit))

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
            return {"lineage": lin, "upto": self.seq_now(lin), "cursor": sorted(self.appearances(lin))}

    def rollback(self, lin: str, cp: dict) -> str:
        with self.store.transaction():
            L = self.lineage(lin)
            gen = L["gen"] + 1
            sid = "inb:%s:g%d" % (lin, gen)
            self.store.db.execute("UPDATE d_transcript SET alive=0 WHERE lineage=? AND seq>?", (lin, cp["upto"]))
            self.store.db.execute("UPDATE d_lineages SET gen=?, inbox_sid=? WHERE lineage=?", (gen, sid, lin))
            self.svc.register(sid, "host", "task", ["req1"])
            for fid in self.master(L["logical"]):
                if fid not in set(cp["cursor"]):
                    self._enqueue(sid, L["logical"], fid, self._item_json(L["logical"], fid))
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
                    self._enqueue(sid, P["logical"], fid, self._item_json(P["logical"], fid))
            self.ev("fork", child)
            return sid


class DeliverySink:
    """What the analyst write gate enqueues into: the durable path above (d_findings + origin + fan-out), so rollback and fork re-offer analyst findings."""
    wants_author = True

    def __init__(self, delivery: Delivery, logical: str, label: Optional[dict] = None):
        self.d, self.logical, self.label = delivery, logical, label

    def enqueue(self, it: InboxItem, author=None, run_key=None) -> bool:
        item = dict(finding_id=it.finding_id, source_event_ids=it.source_event_ids, quote=it.quote, trust_origin=it.trust_origin, item_ids=it.item_ids, created=it.created, expires=it.expires)
        return self.d.ingest_finding(self.logical, item, author, run_key, self.label)


# ================================================================ controls (each is judged only on the schedule it can fail)
def _insert_turn(d, lin, turn_id, gen, ids, block):
    seq = d.store.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM d_transcript WHERE lineage=?", (lin,)).fetchone()[0]
    d.store.db.execute("INSERT INTO d_transcript VALUES(?,?,?,?,?,?,1)", (lin, seq, turn_id, gen, json.dumps(ids), block))


class TokenlessDelivery(Delivery):
    """CONTROL (paused turn): confirm by turn id only, and the transcript row is written whether or not the confirm matched; no generation check."""

    def commit_turn(self, lin, turn_id, begun):
        if "committed" in begun:
            return begun["committed"]
        cl, gen, sid = begun["claim"], begun["gen"], begun["sid"]
        with self.store.transaction():
            for it in self.store.items(sid, ["claimed"]):
                if it.claim_turn == turn_id:
                    it.state = "delivered"
                    self.store.put_item(it)
            block = render_block(list(cl), turn_id) if cl else ""
            ids = [i.finding_id for i in cl]
            _insert_turn(self, lin, turn_id, gen, ids, block)
            return {"block": block, "finding_ids": ids}


class PartialConfirmDelivery(Delivery):
    """CONTROL (F1, mixed claim limits): the 1d19cd8 commit: any confirm count above zero is success and EVERY claimed id is recorded."""

    def commit_turn(self, lin, turn_id, begun):
        if "committed" in begun:
            return begun["committed"]
        cl, token, gen, sid = begun["claim"], begun["token"], begun["gen"], begun["sid"]
        with self.store.transaction():
            L = self.lineage(lin)
            if L["gen"] != gen:
                raise Fenced("generation")
            allowed, _ = self._policy_split(lin, list(cl), L["logical"])
            if allowed and self.svc.confirm(sid, turn_id, token) == 0:
                raise Fenced("stale")
            ids = [i.finding_id for i in allowed]
            block = render_block(allowed, turn_id) if allowed else ""
            _insert_turn(self, lin, turn_id, gen, ids, block)
            return {"block": block, "finding_ids": ids}


class DirectAnalystSink:
    """CONTROL (F2): the 1d19cd8 analyst path: enqueue straight into the current inbox session, never writing d_findings."""
    wants_author = False

    def __init__(self, delivery: Delivery, lineage: str):
        self.d, self.lineage = delivery, lineage

    def enqueue(self, it, **kw):
        return self.d.svc.enqueue(InboxItem(it.finding_id, self.d.lineage(self.lineage)["inbox_sid"], it.source_event_ids, it.quote, it.trust_origin, it.item_ids, it.created, it.expires))


class SendThenConfirmDelivery(Delivery):
    """CONTROL (crash): the block is sent (transcript row) first and confirmed afterwards in a second step; a crash between the two leaves the item
    claimed and it is delivered again after the lease lapses."""
    crash_after_send = False

    def commit_turn(self, lin, turn_id, begun):
        if "committed" in begun:
            return begun["committed"]
        cl, token, gen, sid = begun["claim"], begun["token"], begun["gen"], begun["sid"]
        with self.store.transaction():
            block = render_block(list(cl), turn_id) if cl else ""
            ids = [i.finding_id for i in cl]
            _insert_turn(self, lin, turn_id, gen, ids, block)
        if self.crash_after_send and turn_id == "A":
            raise RuntimeError("crash between send and confirm")
        if cl:
            self.svc.confirm(sid, turn_id, token)
        return {"block": block, "finding_ids": ids}


class CursorOutsideCheckpointDelivery(Delivery):
    """CONTROL (rollback): delivery state lives in the inbox only; a rollback truncates the transcript but does not rewind delivery."""

    def rollback(self, lin, cp):
        with self.store.transaction():
            self.store.db.execute("UPDATE d_transcript SET alive=0 WHERE lineage=? AND seq>?", (lin, cp["upto"]))
            return self.lineage(lin)["inbox_sid"]


class ReusedTurnIdDelivery(Delivery):
    """CONTROL (F10): a stored turn is found by (lineage, turn_id) alone, whatever its generation or liveness."""

    def stored_turn(self, lin, turn_id, gen=None):
        r = self.store.db.execute("SELECT block, finding_ids FROM d_transcript WHERE lineage=? AND turn_id=?", (lin, turn_id)).fetchone()
        return None if r is None else {"block": r[0], "finding_ids": json.loads(r[1])}


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

    def ingest_finding(self, logical, item, author, run_key=None, label=None):
        fid = item["finding_id"]
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO d_findings VALUES(?,?,?,?,?)", (logical, fid, json.dumps(item), json.dumps(label), item["created"]))
            self.store.db.execute("INSERT OR IGNORE INTO d_origin VALUES(?,?,?,?,?)", (logical, fid, author[0], author[1], run_key))
            new = False
            for s in {self.lineage(l)["inbox_sid"] for l in self.live_lineages(logical)}:
                new |= self._enqueue(s, logical, fid, item)
            return new


class EnqueueOnlyPolicyDelivery(Delivery):
    """CONTROL (policy): policy was checked at enqueue and is trusted at delivery."""

    def _policy_split(self, lin, items, logical):
        return list(items), []

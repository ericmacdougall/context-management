"""P4 core: analyst runs keyed by (question or trigger, input watermark, prompt version) in one SQLite database shared by threads and processes.
Reservation before the run, per-call charge to the originating step, epoch fencing (a stopped run is refused at the model gateway and at the
inbox write gate, inside the same transaction as the write), progress-based stall detection. SQLite WAL, BEGIN IMMEDIATE."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from memcore.inbox import SqliteStore


class Fenced(Exception):
    """The run's epoch was revoked (stop, stall, takeover) or the run is not running: the call or write is refused."""


class BudgetExhausted(Exception):
    pass


class NestStore(SqliteStore):
    """SqliteStore whose transaction() nests (a gate transaction may contain InboxService.enqueue) and counts lock contention (overlap witness)."""

    def __init__(self, path: str = ":memory:"):
        super().__init__(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._depth = threading.local()
        self.contended = 0
        self.max_waiters = 0
        self._waiters = 0

    @contextmanager
    def transaction(self):
        d = getattr(self._depth, "n", 0)
        if d:
            self._depth.n = d + 1
            try:
                yield
            finally:
                self._depth.n -= 1
            return
        if not self._lock.acquire(blocking=False):
            self.contended += 1
            self._waiters += 1
            self.max_waiters = max(self.max_waiters, self._waiters)
            self._lock.acquire()
            self._waiters -= 1
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self._depth.n = 1
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        finally:
            self._depth.n = 0
            self._lock.release()


def run_key(session: str, question: str, watermark: int, prompt_version: str) -> str:
    return hashlib.sha256(json.dumps([session, question, watermark, prompt_version]).encode()).hexdigest()[:24]


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(key TEXT PRIMARY KEY, session TEXT, step_id TEXT, question TEXT, watermark INT, prompt_version TEXT, state TEXT, epoch INT,
  owner TEXT, reserved INT, spent INT, started REAL, last_progress REAL, progress_n INT, result TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS steps(step_id TEXT PRIMARY KEY, allowance INT, reserved INT, spent INT);
CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT, epoch INT, step_id TEXT, ts REAL, landed INT);
CREATE TABLE IF NOT EXISTS prog(run_key TEXT, fp TEXT, PRIMARY KEY(run_key, fp));
CREATE TABLE IF NOT EXISTS seen(question TEXT, candidate TEXT, reason TEXT, PRIMARY KEY(question, candidate));
CREATE TABLE IF NOT EXISTS item_origin(finding_id TEXT PRIMARY KEY, author_agent TEXT NOT NULL, author_model TEXT NOT NULL, run_key TEXT, epoch INT);
CREATE TABLE IF NOT EXISTS refusals(id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT, epoch INT, what TEXT, why TEXT, ts REAL);
"""
COLS = ("key", "session", "step_id", "question", "watermark", "prompt_version", "state", "epoch", "owner", "reserved", "spent", "started", "last_progress",
        "progress_n", "result", "reason")


class RunDB:
    def __init__(self, store: NestStore, clock: Callable[[], float] = time.time, takeover_s: float = 5.0):
        self.store, self.clock, self.takeover_s = store, clock, takeover_s
        store.db.executescript(SCHEMA)
        self.cancel = {}          # run key -> threading.Event (in-process wake-up of a blocked call; the fence, not this, is the guarantee)

    # ---- steps
    def open_step(self, step_id: str, allowance: int):
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO steps VALUES(?,?,0,0)", (step_id, allowance))

    def step(self, step_id):
        r = self.store.db.execute("SELECT allowance,reserved,spent FROM steps WHERE step_id=?", (step_id,)).fetchone()
        return dict(allowance=r[0], reserved=r[1], spent=r[2])

    def _row(self, key):
        r = self.store.db.execute("SELECT %s FROM runs WHERE key=?" % ",".join(COLS), (key,)).fetchone()
        return None if r is None else dict(zip(COLS, r))

    def row(self, key):
        with self.store.transaction():
            return self._row(key)

    # ---- launch (idempotent per key)
    def launch(self, session, step_id, question, watermark, prompt_version, max_calls, owner):
        key = run_key(session, question, watermark, prompt_version)
        now = self.clock()
        with self.store.transaction():
            r = self._row(key)
            if r is None:
                s = self.step(step_id)
                if max_calls > s["allowance"] - s["reserved"]:
                    return {"status": "refused_budget", "key": key}
                self.store.db.execute("UPDATE steps SET reserved=reserved+? WHERE step_id=?", (max_calls, step_id))
                self.store.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?, 'running',1,?,?,0,?,?,0,NULL,NULL)",
                                      (key, session, step_id, question, watermark, prompt_version, owner, max_calls, now, now))
                return {"status": "started", "key": key, "epoch": 1}
            if r["state"] == "done":
                return {"status": "done", "key": key, "result": json.loads(r["result"])}
            if r["state"] in ("stopped", "failed"):
                return {"status": r["state"], "key": key, "reason": r["reason"]}
            if now - r["last_progress"] > self.takeover_s:       # the owner evidently died: take over under a new epoch
                self.store.db.execute("UPDATE runs SET epoch=epoch+1, owner=?, last_progress=? WHERE key=?", (owner, now, key))
                return {"status": "started", "key": key, "epoch": r["epoch"] + 1, "takeover": True}
            return {"status": "attached", "key": key}

    def _live(self, key, epoch):
        r = self._row(key)
        if r is None or r["state"] != "running" or r["epoch"] != epoch:
            raise Fenced("run %s epoch %s is not live (state=%s epoch=%s)" % (key, epoch, r and r["state"], r and r["epoch"]))
        return r

    def log_refusal(self, key, epoch, what, why):
        with self.store.transaction():
            self.store.db.execute("INSERT INTO refusals(run_key,epoch,what,why,ts) VALUES(?,?,?,?,?)", (key, epoch, what, why, self.clock()))

    # ---- spend: reservation check + fence + step charge in one transaction
    def spend(self, key, epoch):
        with self.store.transaction():
            r = self._live(key, epoch)
            if r["spent"] >= r["reserved"]:
                raise BudgetExhausted("reservation %d used up" % r["reserved"])
            self.store.db.execute("UPDATE runs SET spent=spent+1 WHERE key=?", (key,))
            self.store.db.execute("UPDATE steps SET spent=spent+1 WHERE step_id=?", (r["step_id"],))
            self.store.db.execute("INSERT INTO calls(run_key,epoch,step_id,ts,landed) VALUES(?,?,?,?,1)", (key, epoch, r["step_id"], self.clock()))
            return r["spent"] + 1

    def assert_live(self, key, epoch):
        with self.store.transaction():
            self._live(key, epoch)

    def progress(self, key, epoch, fingerprint: str) -> bool:
        """Progress = a fingerprint (tool, args, observation, finding set) not seen before in this run. A repeated fingerprint is a heartbeat, not progress."""
        with self.store.transaction():
            self._live(key, epoch)
            cur = self.store.db.execute("INSERT OR IGNORE INTO prog VALUES(?,?)", (key, hashlib.sha256(fingerprint.encode()).hexdigest()))
            if cur.rowcount:
                self.store.db.execute("UPDATE runs SET last_progress=?, progress_n=progress_n+1 WHERE key=?", (self.clock(), key))
                return True
            return False

    def _release(self, r):
        self.store.db.execute("UPDATE steps SET reserved=reserved-? WHERE step_id=?", (r["reserved"] - r["spent"], r["step_id"]))

    def finish(self, key, epoch, result: dict):
        with self.store.transaction():
            r = self._live(key, epoch)
            self._release(r)
            self.store.db.execute("UPDATE runs SET state='done', result=? WHERE key=?", (json.dumps(result, sort_keys=True), key))

    def fail(self, key, epoch, reason: str):
        with self.store.transaction():
            r = self._live(key, epoch)
            self._release(r)
            self.store.db.execute("UPDATE runs SET state='failed', reason=? WHERE key=?", (reason, key))

    def stop_run(self, key, reason: str) -> bool:
        """Out-of-band stop. The commit of this transaction is the linearisation point: after it returns, every spend() and write_gate() of the
        run's epochs is refused. An in-flight model call is DISCARDED when it returns (assert_live), not recalled."""
        with self.store.transaction():
            r = self._row(key)
            if r is None or r["state"] != "running":
                return False
            self._release(r)
            self.store.db.execute("UPDATE runs SET state='stopped', epoch=epoch+1, reason=? WHERE key=?", (reason, key))
        ev = self.cancel.get(key)
        if ev:
            ev.set()
        return True

    def write_gate(self, key, epoch, fn: Callable[[], object]):
        """Run fn (an inbox write) inside the transaction that checks the run is live: check and write are atomic against stop_run()."""
        with self.store.transaction():
            self._live(key, epoch)
            return fn()

    # ---- watchdog
    def scan_stalls(self, stall_s: float):
        now, stopped = self.clock(), []
        with self.store.transaction():
            rows = self.store.db.execute("SELECT key FROM runs WHERE state='running' AND ? - last_progress > ?", (now, stall_s)).fetchall()
        for (k,) in rows:
            if self.stop_run(k, "stall"):
                stopped.append(k)
        return stopped

    # ---- origin of every finding that reaches an inbox (author agent and author model are NOT NULL)
    def record_origin(self, finding_id, author_agent, author_model, run_key, epoch):
        if not author_agent or not author_model:
            raise ValueError("author agent and model are required on every finding")
        self.store.db.execute("INSERT OR IGNORE INTO item_origin VALUES(?,?,?,?,?)", (finding_id, author_agent, author_model, run_key, epoch))

    def origin_of(self, finding_id):
        r = self.store.db.execute("SELECT author_agent, author_model FROM item_origin WHERE finding_id=?", (finding_id,)).fetchone()
        return None if r is None else {"agent": r[0], "model": r[1]}

    # ---- seen set
    def seen_add(self, question, candidate, reason):
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?)", (question, candidate, reason))

    def seen_get(self, question, candidate):
        r = self.store.db.execute("SELECT reason FROM seen WHERE question=? AND candidate=?", (question, candidate)).fetchone()
        return r[0] if r else None

    def calls_for(self, key=None, step_id=None):
        q, a = "SELECT COUNT(*) FROM calls WHERE landed=1", []
        if key:
            q += " AND run_key=?"
            a.append(key)
        if step_id:
            q += " AND step_id=?"
            a.append(step_id)
        return self.store.db.execute(q, a).fetchone()[0]


class GuardedModel:
    """The only path from an analyst to a model: reserves and charges one call (fenced) BEFORE the call, and discards the result if the
    run was stopped while the call was in flight."""

    def __init__(self, db: RunDB, key: str, epoch: int, model, global_budget=None):
        self.db, self.key, self.epoch, self.model, self.global_budget = db, key, epoch, model, global_budget
        self.calls = 0

    def call(self, messages, cancel: Optional[threading.Event] = None):
        try:
            self.db.spend(self.key, self.epoch)
        except (Fenced, BudgetExhausted) as e:
            self.db.log_refusal(self.key, self.epoch, "model_call", type(e).__name__)
            raise
        if self.global_budget is not None:
            self.global_budget.reserve()
        self.calls += 1
        out = self.model.complete(messages, cancel)
        try:
            self.db.assert_live(self.key, self.epoch)
        except Fenced:
            self.db.log_refusal(self.key, self.epoch, "in_flight_result_discarded", "Fenced")
            raise
        return out

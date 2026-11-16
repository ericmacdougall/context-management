"""P4: the analyst run loop over deterministic stub models. The analyst sees read-only tools only; every model call goes through GuardedModel
(fenced, reserved, charged to the originating step); findings are parsed against the fixed schema, host-checked (quote and speaker) and written to
the inbox only through the write gate. Stubs misbehave on request: malformed output, loop, hang, ignore the schema, planted instruction, write attempt."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, List, Optional

from memcore.inbox import InboxService
from ctxrt.findings import SchemaRejected, host_check, parse_findings, to_item
from ctxrt.kernel import MemoryLog
from ctxrt.rundb import BudgetExhausted, Fenced, GuardedModel, RunDB

MAX_STEPS = 40


class Cancelled(Exception):
    pass


class ScriptedModel:
    """Deterministic stub. `script` is a list of actions (the last one repeats forever: that is the loop stub) or a callable(i, messages) -> action.
    Action: {"type":"tool","name":..,"args":..} | {"type":"final","text":..} | {"type":"garbage"} | {"type":"tool",...,"hang":seconds}."""

    def __init__(self, script, delay: float = 0.0, ignore_cancel: bool = False):
        self.script, self.delay, self.i, self.in_flight, self.ignore_cancel = script, delay, 0, 0, ignore_cancel

    def complete(self, messages, cancel: Optional[threading.Event] = None):
        i, self.i = self.i, self.i + 1
        act = self.script(i, messages) if callable(self.script) else self.script[min(i, len(self.script) - 1)]
        wait = max(self.delay, act.get("hang", 0.0)) if isinstance(act, dict) else self.delay
        if wait:
            self.in_flight += 1
            try:
                if cancel is not None and not self.ignore_cancel:
                    if cancel.wait(wait):
                        raise Cancelled()
                else:
                    time.sleep(wait)
            finally:
                self.in_flight -= 1
        return act


class AnalystRun:
    def __init__(self, db: RunDB, key: str, epoch: int, model, tools: Dict[str, Callable[[dict], dict]], question: str, session: str, log: MemoryLog,
                 inbox: Optional[InboxService], item_ids=("req1",), global_budget=None, now: Callable[[], float] = time.time,
                 author: Optional[tuple] = None, tenant: str = "t0", readable_sessions=frozenset()):
        self.db, self.key, self.epoch, self.tools, self.question, self.session, self.log, self.inbox = db, key, epoch, tools, question, session, log, inbox
        self.gm = GuardedModel(db, key, epoch, model, global_budget)
        self.cancel = db.cancel.setdefault(key, threading.Event())
        self.item_ids, self.now = item_ids, now
        self.tenant, self.readable = tenant, frozenset(readable_sessions)
        self.author = author or ("analyst-%s" % key[:6], getattr(model, "model_id", "stub-model"))     # (agent id, model id) recorded on every finding
        self.messages: List[dict] = [{"role": "user", "content": question}]
        self.n = 0
        self.write_refused = 0
        self.review: List[dict] = []
        self.rejected: List[str] = []
        self.written = 0
        self.outcome: Optional[str] = None

    def step(self) -> str:
        """One model call and its effect. Returns continue | done | failed. Raises Fenced / BudgetExhausted / Cancelled when stopped."""
        out = self.gm.call(self.messages, self.cancel)
        self.n += 1
        t = out.get("type") if isinstance(out, dict) else None
        if t == "tool":
            name, args = out.get("name"), out.get("args", {})
            fn = self.tools.get(name)
            if fn is None:                        # includes every write attempt: the analyst's surface has no mutating tool
                self.write_refused += 1
                self.db.log_refusal(self.key, self.epoch, "tool_refused", str(name)[:40])
                res = {"status": "error", "code": "unknown_or_forbidden_tool"}
            else:
                try:
                    res = fn(args)
                except Exception as e:             # a tool failure is an observation, not a crash
                    res = {"status": "error", "code": type(e).__name__}
            self.db.progress(self.key, self.epoch, json.dumps([name, args, res], sort_keys=True, default=str))
            self.messages.append({"role": "assistant", "content": json.dumps(out, default=str)})
            self.messages.append({"role": "user", "content": json.dumps(res, default=str)})
            return "continue"
        if t == "final":
            return self._finalise(out.get("text"))
        self.db.fail(self.key, self.epoch, "malformed_action")
        return "failed"

    def _finalise(self, raw) -> str:
        try:
            obj = parse_findings(raw)
        except SchemaRejected as e:
            self.db.fail(self.key, self.epoch, "schema:" + e.code)
            self.outcome = "schema_rejected:" + e.code
            return "failed"
        for d in obj["dead_ends"]:                # the analyst's own declarations are recorded for humans and NEVER seed the seen-set
            self.db.note_analyst_claim(self.key, self.question, d["candidate_id"], d["reason"])
        items = []
        for f in obj["findings"]:
            verdict, ev = host_check(self.log, f, self.readable, self.tenant)
            if verdict == "PASS":
                items.append(to_item(self.question, self.session, ev["event"], ev["span"], ev["speaker"], self.item_ids, self.now()))
            elif verdict == "REVIEW":
                self.review.append({"candidate_id": f["candidate_id"], "speakers": ev["speakers"]})
            else:
                self.rejected.append(verdict)
                self.db.seen_add(self.key, self.epoch, self.tenant, self.session, self.question, f["candidate_id"], verdict)     # host-verified outcome, fenced

        def write():
            n = 0
            for it in items:
                if self.inbox is not None:
                    new = self.inbox.enqueue(it, author=self.author, run_key=self.key) if getattr(self.inbox, "wants_author", False) else self.inbox.enqueue(it)
                    self.db.record_origin(it.session_id, it.finding_id, self.author[0], self.author[1], self.key, self.epoch)     # every producer of this finding, new copy or not
                    n += bool(new)
            return n
        self.written = self.db.write_gate(self.key, self.epoch, write)
        self.outcome = obj["outcome"]
        self.db.finish(self.key, self.epoch, {"outcome": obj["outcome"], "n_items": len(items), "n_review": len(self.review), "rejected": self.rejected})
        return "done"

    def run(self) -> str:
        try:
            for _ in range(MAX_STEPS):
                s = self.step()
                if s != "continue":
                    return s
            self.db.fail(self.key, self.epoch, "max_steps")
            return "failed"
        except (Fenced, BudgetExhausted, Cancelled) as e:
            self.outcome = "refused:" + type(e).__name__
            return "refused"


class Judge:
    """Host tool `judge(candidate_id)`: a model call decides whether a candidate is relevant. A candidate already rejected by the HOST in this
    (tenant, session, question) returns its earlier rejection at ZERO cost; otherwise one guarded call is paid and the host's own rejection is recorded
    (fenced). use_seen=False is the control. Analyst self-declared dead ends never reach this table."""

    def __init__(self, db: RunDB, gm: GuardedModel, question: str, judge_model, use_seen: bool = True, tenant: str = "t0", session: str = "sess"):
        self.db, self.gm, self.question, self.model, self.use_seen, self.tenant, self.session = db, gm, question, judge_model, use_seen, tenant, session
        self.paid = 0
        self.skipped = 0

    def __call__(self, args: dict) -> dict:
        cid = args["candidate_id"]
        if self.use_seen:
            r = self.db.seen_get(self.tenant, self.session, self.question, cid)
            if r is not None:
                self.skipped += 1
                return {"status": "rejected", "reason": r, "cached": True}
        out = self.gm.call([{"role": "user", "content": "judge " + cid}])
        self.paid += 1
        if out.get("accept"):
            return {"status": "relevant"}
        self.db.seen_add(self.gm.key, self.gm.epoch, self.tenant, self.session, self.question, cid, "not_relevant")
        return {"status": "rejected", "reason": "not_relevant", "cached": False}

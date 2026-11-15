"""SessionWorkflow, code version v1. Holds ids, cursors and counters only; every effect is an activity.
Turn = read event from Postgres, (stand-in) compute, idempotent external effect, append result event.
Multi-tenant: the tenant travels in the state and is passed to every activity; a doorbell for an event id that is not
of this tenant and session is rejected before anything is read or marked."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from engine import keys as keys_mod

CODE = "v1"
AT = dict(start_to_close_timeout=timedelta(seconds=8),
          retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=500), maximum_interval=timedelta(seconds=5),
                                   non_retryable_error_types=["InvalidInput", "SubjectErased", "Poison"]))
MAX_CARRY = 40       # pending ids carried across continue-as-new
DONE_CARRY = 40      # recently processed ids carried (a late duplicate doorbell must not run a turn twice)
DEFAULTS = {"done": [], "pend": [], "finish": False, "rejected": 0, "dead": 0, "erased": False, "approval_req": "",
            "last_eid": "", "reconcile_s": 20, "approval_poll_s": 10, "stop_after_rollups": 0, "approval_deadline_ms": 0}


async def act(name, *args):
    return await workflow.execute_activity(name, args=list(args), **AT)


def now_ms():
    return int(workflow.now().timestamp() * 1000)


def seqkey(e):
    p = keys_mod.parse_event_id(e)
    return p[2] if p else 0


@workflow.defn(name="SessionWorkflow")
class SessionWorkflow:
    @workflow.init
    def __init__(self, st: dict):
        self.st = dict(st)
        for k, v in DEFAULTS.items():
            self.st.setdefault(k, list(v) if isinstance(v, list) else v)
        self.pending = list(self.st["pend"])
        self.done_list = list(self.st["done"])
        self.done = set(self.done_list)
        self.approved = False
        self.finish = bool(self.st["finish"])
        self.sig = 0
        self.last_rec_ms = 0
        self.more = False  # last reconcile returned a full batch: more unprocessed inputs may be waiting in Postgres

    @workflow.signal
    def turn(self, event_id: str):
        self.sig += 1
        if not keys_mod.belongs_to(self.st["tenant"], self.st["sid"], event_id):
            self.st["rejected"] += 1       # foreign or malformed id: nothing is read, marked or queued
            return
        if event_id in self.done or event_id in self.pending:
            return
        self.pending.append(event_id)
        self.pending.sort(key=seqkey)

    @workflow.signal
    def approve(self, ref: str):
        self.approved = True

    @workflow.signal
    def finish_now(self, ref: str):
        self.finish = True

    @workflow.query
    def progress(self) -> dict:
        return {"turns": self.st["turns_done"], "sig": self.st["signals_seen"] + self.sig, "code": CODE}

    # ---- one turn: the only place the command sequence matters for in-flight sessions
    async def _steps(self, eid):
        t, sid = self.st["tenant"], self.st["sid"]
        digest = await act("turn_read", t, sid, eid)
        if digest == "defer":
            return False
        key = await act("turn_effect", t, sid, eid)
        await act("turn_append", t, sid, eid, digest, key, CODE)
        return True

    async def _turn(self, eid):
        t, sid = self.st["tenant"], self.st["sid"]
        try:
            if not await self._steps(eid):
                await self._reconcile()                    # sequence order: older unprocessed inputs go first
                if eid not in self.pending:
                    self.pending.append(eid)
                    self.pending.sort(key=seqkey)
                return False
        except ActivityError as e:
            typ = e.cause.type if isinstance(e.cause, ApplicationError) else None
            if typ not in ("InvalidInput", "SubjectErased", "Poison"):
                raise
            await act("dead_letter", t, sid, eid, typ)   # poison path: recorded, never retried, never blocks the session
            self.st["dead"] += 1
            if typ == "SubjectErased":
                self.st["erased"] = True
        return True

    async def _reconcile(self):
        ids = await act("reconcile", self.st["tenant"], self.st["sid"])
        self.last_rec_ms = now_ms()
        self.more = len(ids) >= 20
        for e in ids:
            if e not in self.pending and e not in self.done:
                self.pending.append(e)
        self.pending.sort(key=seqkey)

    def _want_can(self):
        info = workflow.info()
        cl = self.st.get("can_len", 0)
        return info.is_continue_as_new_suggested() or (cl and info.get_current_history_length() >= cl)

    @workflow.run
    async def run(self, st: dict) -> dict:
        st = self.st
        t, sid = st["tenant"], st["sid"]
        if st["rollup_s"] and not st["rollup_deadline_ms"]:
            st["rollup_deadline_ms"] = now_ms() + int(st["rollup_s"] * 1000)
        await self._reconcile()
        while True:
            if self.finish and not self.pending:
                break
            if st["erased"]:
                break
            if st["expected_turns"] and st["turns_done"] >= st["expected_turns"]:
                break
            if st.get("stop_after_rollups") and st["rollups"] >= st["stop_after_rollups"]:
                break
            # approval gate bound to ONE request id; Postgres decides, the signal is only a doorbell
            if st["approval_at"] and st["approval"] == "none" and st["turns_done"] >= st["approval_at"]:
                rid, dl = await act("approval_request", t, sid, st["last_eid"] or "start",
                                    now_ms() + int(st["approval_to_s"] * 1000))
                st["approval"], st["approval_req"], st["approval_deadline_ms"] = "waiting", rid, dl
            if st["approval"] == "waiting":
                res = await act("approval_resolve", t, sid, st["approval_req"])   # check first: a rebuilt session resumes at once
                if res == "pending":
                    remaining = (st["approval_deadline_ms"] - now_ms()) / 1000
                    if not self.approved:
                        try:
                            await workflow.wait_condition(lambda: self.approved,
                                                          timeout=max(0.001, min(remaining, st["approval_poll_s"])))
                        except asyncio.TimeoutError:
                            pass
                    self.approved = False
                else:
                    st["approval"] = res
                continue
            if st["rollup_s"] and now_ms() >= st["rollup_deadline_ms"]:
                await act("rollup", t, sid, st["rollup_deadline_ms"])   # keyed by the scheduled deadline, not a counter
                st["rollups"] += 1
                st["rollup_deadline_ms"] = max(st["rollup_deadline_ms"] + int(st["rollup_s"] * 1000), now_ms() + 1000)
                continue
            if st["reconcile_s"] and now_ms() - self.last_rec_ms >= st["reconcile_s"] * 1000:
                await self._reconcile()   # a lost doorbell cannot hang an input for longer than reconcile_s
                continue
            if self.pending:
                eid = self.pending.pop(0)
                if eid not in self.done:
                    if await self._turn(eid):
                        self.done.add(eid)
                        self.done_list.append(eid)
                        st["turns_done"] += 1
                        st["last_eid"] = eid
            elif self.more:
                await self._reconcile()
            else:
                timeouts = []
                if st["rollup_s"]:
                    timeouts.append((st["rollup_deadline_ms"] - now_ms()) / 1000)
                if st["reconcile_s"]:
                    timeouts.append((self.last_rec_ms + st["reconcile_s"] * 1000 - now_ms()) / 1000)
                timeout = max(min(timeouts), 0.001) if timeouts else None
                try:
                    await workflow.wait_condition(lambda: bool(self.pending) or self.finish, timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            if self._want_can() and len(self.pending) <= MAX_CARRY:
                await workflow.wait_condition(workflow.all_handlers_finished)
                st["pend"] = [e for e in self.pending if e not in self.done][:MAX_CARRY]
                st["done"] = self.done_list[-DONE_CARRY:]
                st["finish"] = self.finish
                st["signals_seen"] += self.sig
                st["cans"] += 1
                st["hist_max"] = max(st["hist_max"], workflow.info().get_current_history_length())
                workflow.continue_as_new(st)
        if not st["erased"]:
            await act("finish_session", t, sid, st["turns_done"])
        await workflow.wait_condition(workflow.all_handlers_finished)
        return {"turns": st["turns_done"], "sig": st["signals_seen"] + self.sig, "cans": st["cans"],
                "rollups": st["rollups"], "approval": st["approval"], "rejected": st["rejected"], "dead": st["dead"],
                "erased": st["erased"], "code": CODE}

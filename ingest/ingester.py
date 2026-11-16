"""Exactly-once, ordered ingestion from the port log into an event store (SQLite stand-in for the Postgres event log), with a
per-stream watermark, typed gap events and optimistic fencing (compare-and-set on the watermark inside the apply transaction)."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import shapes
from .canon import cj, sha
from .portlog import GENESIS, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(ord INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, ikey TEXT NOT NULL UNIQUE,
  call_n INTEGER, side TEXT, pos INTEGER, blk INTEGER, prov TEXT, kind TEXT, status TEXT, content TEXT, chash TEXT,
  log_seq INTEGER, meta TEXT, eprev TEXT, ehash TEXT);
CREATE TABLE IF NOT EXISTS wm(stream TEXT PRIMARY KEY, seq INTEGER NOT NULL, head TEXT NOT NULL, ehead TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending(stream TEXT NOT NULL, seq INTEGER NOT NULL, rec TEXT NOT NULL, PRIMARY KEY(stream, seq));
CREATE TABLE IF NOT EXISTS applied(stream TEXT NOT NULL, seq INTEGER NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(stream, seq));
CREATE TABLE IF NOT EXISTS calls(stream TEXT NOT NULL, call_n INTEGER NOT NULL, state TEXT NOT NULL, req_seq INTEGER, keys TEXT,
  roles TEXT, sys_k TEXT, resp_key TEXT, resp_ok INTEGER, PRIMARY KEY(stream, call_n));
CREATE TABLE IF NOT EXISTS reqs(stream TEXT NOT NULL, call_n INTEGER NOT NULL, body TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '[]', resp_ref TEXT, PRIMARY KEY(stream, call_n));
CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, kind TEXT NOT NULL, from_seq INTEGER NOT NULL,
  to_seq INTEGER NOT NULL, blocking INTEGER NOT NULL, detail TEXT, opened REAL, closed REAL, closed_by TEXT, UNIQUE(stream, kind, from_seq));
CREATE TABLE IF NOT EXISTS witness(stream TEXT PRIMARY KEY, seq INTEGER NOT NULL, hash TEXT NOT NULL);
"""

BLOCKING = {"seq_hole": 1, "tail_truncated": 1, "chain_break": 1, "conflicting_duplicate": 1, "blob_missing": 1,
            "unlogged_assistant_message": 1, "delta_mismatch": 1, "assistant_message_not_in_log": 1, "history_rewritten": 0}


class Fenced(Exception):
    """Another ingester advanced the watermark between this one's snapshot and its apply (stale token)."""


class Ingester:
    def __init__(self, path: str, blob_source: Callable[[str], Optional[str]], name: str = "ing", sync: str = "FULL",
                 hooks: Optional[Dict[str, Callable]] = None, fence: bool = True, unique: bool = True, state_guard: bool = True):
        """fence/unique=False exist only to build the race control (compare-and-set and event uniqueness removed)."""
        self.path, self.blob, self.name, self.hooks = path, blob_source, name, hooks or {}
        self.fence, self.unique, self._n, self.state_guard = fence, unique, 0, state_guard
        self.c = connect(path, sync)
        self.c.executescript(SCHEMA)
        self.stats = {"fenced": 0, "dup_event_suppressed": 0, "records_applied": 0, "lock_wait_s": 0.0, "apply_s": 0.0}

    # ------------------------------------------------------------ intake
    def deliver(self, records: List[dict]) -> None:
        """At-least-once, any order, duplicates allowed. Nothing is applied here."""
        for r in records:
            self._intake(r)

    def _intake(self, r: dict) -> None:
        c = self.c
        stream, seq = r["stream"], r["seq"]
        row = c.execute("SELECT hash FROM applied WHERE stream=? AND seq=?", (stream, seq)).fetchone()
        if row is not None:
            if row[0] != r["hash"]:
                self._gap(stream, "conflicting_duplicate", seq, seq, {"seq": seq})
            return
        p = c.execute("SELECT rec FROM pending WHERE stream=? AND seq=?", (stream, seq)).fetchone()
        if p is not None:
            if json.loads(p[0])["hash"] != r["hash"]:
                self._gap(stream, "conflicting_duplicate", seq, seq, {"seq": seq})
            return
        c.execute("INSERT OR IGNORE INTO pending VALUES(?,?,?)", (stream, seq, cj(r)))

    def _gap(self, stream: str, kind: str, a: int, b: int, detail: dict) -> None:
        self.c.execute("INSERT INTO gaps(stream,kind,from_seq,to_seq,blocking,detail,opened) VALUES(?,?,?,?,?,?,?) "
                       "ON CONFLICT(stream,kind,from_seq) DO UPDATE SET to_seq=MAX(to_seq,excluded.to_seq) WHERE closed IS NULL",
                       (stream, kind, a, b, BLOCKING[kind], cj(detail), time.time()))

    # ------------------------------------------------------------ drain
    def watermark(self, stream: str) -> Tuple[int, str]:
        r = self.c.execute("SELECT seq, head FROM wm WHERE stream=?", (stream,)).fetchone()
        return (r[0], r[1]) if r else (0, GENESIS)

    def drain(self, stream: str, max_retries: int = 50) -> int:
        for _ in range(max_retries):
            token, head = self.watermark(stream)
            run, nxt = [], token + 1
            for (seq, rec) in self.c.execute("SELECT seq, rec FROM pending WHERE stream=? AND seq>? ORDER BY seq", (stream, token)).fetchall():
                if seq != nxt:
                    break
                run.append(json.loads(rec))
                nxt += 1
            self._note_holes(stream, token, nxt)
            if self.hooks.get("after_snapshot"):
                self.hooks["after_snapshot"](self.name, stream, token, len(run))
            if not run:
                self._close_gaps(stream)
                return token
            try:
                self._apply(stream, token, run)
            except Fenced:
                self.stats["fenced"] += 1
                continue
            self._close_gaps(stream)
            return self.watermark(stream)[0]
        raise RuntimeError("drain: too many fencing retries")

    def _note_holes(self, stream: str, token: int, nxt: int) -> None:
        r = self.c.execute("SELECT MIN(seq), MAX(seq) FROM pending WHERE stream=? AND seq>=?", (stream, nxt)).fetchone()
        if r and r[0] is not None and r[0] > nxt:
            # a record beyond the contiguous run has arrived: [nxt, first_beyond-1] is missing
            self._gap(stream, "seq_hole", nxt, r[0] - 1, {"missing": [nxt, r[0] - 1], "max_seen": r[1]})

    def _close_gaps(self, stream: str) -> None:
        wm = self.watermark(stream)[0]
        self.c.execute("UPDATE gaps SET closed=?, closed_by='late_record' WHERE stream=? AND closed IS NULL AND kind IN "
                       "('seq_hole','tail_truncated') AND to_seq<=?", (time.time(), stream, wm))

    # ------------------------------------------------------------ apply (one transaction, compare-and-set on the watermark)
    def _apply(self, stream: str, token: int, run: List[dict]) -> None:
        c = self.c
        t0 = time.time()
        if self.hooks.get("before_lock"):
            self.hooks["before_lock"](self.name, stream, token)
        c.execute("BEGIN IMMEDIATE")
        t1 = time.time()
        try:
            cur, head = self.watermark(stream)
            if cur != token and self.fence:
                raise Fenced("watermark %d != token %d" % (cur, token))
            if self.hooks.get("in_apply"):
                self.hooks["in_apply"](self.name, stream, token)
            ehead = (c.execute("SELECT ehead FROM wm WHERE stream=?", (stream,)).fetchone() or (GENESIS,))[0]
            for r in run:
                if r["prev"] != head and self.fence:   # (the chain check is a second fence; the race control removes it too)
                    self._gap(stream, "chain_break", r["seq"], r["seq"], {"expected_prev": head, "got_prev": r["prev"]})
                    break                                  # held: records after a broken chain are not applied
                if sha(r["prev"] + "|" + r["kind"] + "|" + r["body"]) != r["hash"]:
                    self._gap(stream, "chain_break", r["seq"], r["seq"], {"reason": "hash_mismatch"})
                    break
                missing = self._apply_record(stream, r)
                if missing:
                    self._gap(stream, "blob_missing", r["seq"], r["seq"], {"hash": missing})
                    break
                c.execute("INSERT OR IGNORE INTO applied VALUES(?,?,?)", (stream, r["seq"], r["hash"]))
                c.execute("DELETE FROM pending WHERE stream=? AND seq=?", (stream, r["seq"]))
                head = r["hash"]
                token = r["seq"]
                self.stats["records_applied"] += 1
            ehead = (c.execute("SELECT ehash FROM events WHERE stream=? ORDER BY ord DESC LIMIT 1", (stream,)).fetchone() or (ehead,))[0]
            c.execute("INSERT INTO wm VALUES(?,?,?,?) ON CONFLICT(stream) DO UPDATE SET seq=excluded.seq, head=excluded.head, ehead=excluded.ehead",
                      (stream, token, head, ehead))
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            t2 = time.time()
            self.stats["lock_wait_s"] += t1 - t0
            self.stats["apply_s"] += t2 - t1
            self.last_overlap = (t0, t1, t2)       # [lock request, lock acquired, end]: measured overlap witness

    # ------------------------------------------------------------ event emission
    def _emit(self, stream, call_n, side, pos, blk, prov, kind, status, content, log_seq, meta=None) -> None:
        c = self.c
        ikey = "%s|%s|%s|%s|%s" % (stream, call_n, side, pos, blk)
        if not self.unique:
            self._n += 1
            ikey += "|%s#%d" % (self.name, self._n)
        last = c.execute("SELECT ehash FROM events WHERE stream=? ORDER BY ord DESC LIMIT 1", (stream,)).fetchone()
        eprev = last[0] if last else GENESIS
        chash = sha(content or "")
        cur = c.execute("INSERT OR IGNORE INTO events(stream,ikey,call_n,side,pos,blk,prov,kind,status,content,chash,log_seq,meta,eprev,ehash) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (stream, ikey, call_n, side, pos, blk, prov, kind, status, content, chash, log_seq, cj(meta or {}), eprev,
                         sha(eprev + "|" + ikey + "|" + chash)))
        if cur.rowcount == 0:
            self.stats["dup_event_suppressed"] += 1

    def _blocks(self, h: str, provider: str):
        body = self.blob(h)
        if body is None:
            return None
        return json.loads(body)

    def _apply_record(self, stream: str, r: dict) -> Optional[str]:
        """Returns the hash of a missing blob (record held) or None."""
        c = self.c
        body = json.loads(r["body"])
        n, seq, kind = body.get("call_n"), r["seq"], r["kind"]
        if kind == "BLOCK":
            c.execute("INSERT OR IGNORE INTO calls(stream,call_n,state,req_seq) VALUES(?,?,?,?)", (stream, n, "blocked", seq))
            self._emit(stream, n, "meta", 0, 0, "port", "call_blocked", "info", cj({"reason": body.get("reason")}), seq)
            return None
        if kind == "REQ":
            for h in [m["h"] for m in body["msgs"]] + ([body["sys"]["h"]] if body.get("sys") else []):
                if self.blob(h) is None:
                    return h
            refs = body["msgs"]
            d = body.get("delta")
            if d:
                b = c.execute("SELECT refs, resp_ref FROM reqs WHERE stream=? AND call_n=?", (stream, d["base"])).fetchone()
                full = (json.loads(b[0]) + ([json.loads(b[1])] if d["with_resp"] and b[1] else [])) if b else []
                from .tap import sha_keys
                if not b or len(full) < d["p"] or sha_keys([m["k"] for m in full[:d["p"]]]) != d["digest"]:
                    self._gap(stream, "delta_mismatch", seq, seq, {"call": n, "base": d["base"]})
                    return "delta_base:%s" % d["base"]
                refs = full[:d["p"]] + body["msgs"]
            # any earlier call still 'requested' never got a response record: its send outcome is unknown
            for (pn,) in c.execute("SELECT call_n FROM calls WHERE stream=? AND state='requested'", (stream,)).fetchall():
                c.execute("UPDATE calls SET state='unconfirmed' WHERE stream=? AND call_n=?", (stream, pn))
                self._emit(stream, pn, "meta", 0, 0, "port", "unconfirmed_request", "info", cj({"why": "no response record before next request"}), seq)
            c.execute("INSERT OR IGNORE INTO calls(stream,call_n,state,req_seq,keys,roles,sys_k) VALUES(?,?,?,?,?,?,?)",
                      (stream, n, "requested", seq, cj([m["k"] for m in refs]), cj([m["role"] for m in refs]),
                       (body["sys"] or {}).get("k") if body.get("sys") else None))
            c.execute("INSERT OR IGNORE INTO reqs(stream,call_n,body,refs) VALUES(?,?,?,?)", (stream, n, r["body"], cj(refs)))
            for v in body.get("pairing") or []:
                self._emit(stream, n, "meta", 1, int(sha(v)[:7], 16), "port", "tool_pairing_violation", "info", v, seq)
            return None
        if kind == "RESP":
            return self._apply_resp(stream, n, seq, body)
        if kind == "BACKFILL":
            c.execute("UPDATE gaps SET closed=?, closed_by='backfill' WHERE stream=? AND closed IS NULL AND kind IN "
                      "('unlogged_assistant_message','assistant_message_not_in_log') AND detail LIKE ?",
                      (time.time(), stream, '%"key":"' + body.get("key", "?") + '"%'))
            return None
        self._emit(stream, n, "meta", 0, 0, "port", "unknown_record_kind", "info", kind, seq)
        return None

    def _req_body(self, stream: str, n: int, seq: int) -> dict:
        row = self.c.execute("SELECT body, refs FROM reqs WHERE stream=? AND call_n=?", (stream, n)).fetchone()
        b = json.loads(row[0])
        b["msgs"] = json.loads(row[1])           # full reconstructed refs (the log record may hold only the delta)
        return b

    def _apply_resp(self, stream: str, n: int, seq: int, body: dict) -> Optional[str]:
        c = self.c
        status = body["status"]
        if body.get("msg") and self.blob(body["msg"]["h"]) is None:
            return body["msg"]["h"]
        if body.get("msg"):
            c.execute("UPDATE reqs SET resp_ref=? WHERE stream=? AND call_n=?", (cj(body["msg"]), stream, n))
        row = c.execute("SELECT state FROM calls WHERE stream=? AND call_n=?", (stream, n)).fetchone()
        if row is None or (row[0] == "confirmed" and self.state_guard):
            return None
        if status in ("rejected", "failed_unknown"):
            c.execute("UPDATE calls SET state=? WHERE stream=? AND call_n=?", ("rejected" if status == "rejected" else "unconfirmed", stream, n))
            self._emit(stream, n, "meta", 0, 0, "port", "call_" + status, "info", cj({"error": body.get("error")}), seq)
            return None
        req = self._req_body(stream, n, seq)
        provider = req["provider"]
        keys = [m["k"] for m in req["msgs"]]
        base = c.execute("SELECT call_n, keys, resp_key FROM calls WHERE stream=? AND state='confirmed' ORDER BY call_n DESC LIMIT 1",
                         (stream,)).fetchone()
        roles = [m["role"] for m in req["msgs"]]
        tail_from, note, skip_pos = 0, {}, None
        if base:
            bkeys = json.loads(base[1])
            full = bkeys + ([base[2]] if base[2] else [])
            p = 0
            while p < len(keys) and p < len(full) and keys[p] == full[p]:
                p += 1
            if p == len(full):
                tail_from = p
            elif keys == bkeys:
                note, tail_from = {"retry_of": base[0]}, len(keys)
            elif base[2] and p == len(bkeys):
                note, tail_from, skip_pos = {"response_mismatch": True}, p, p
                self._gap(stream, "assistant_message_not_in_log", seq, seq, {"call": n, "pos": p, "key": keys[p] if p < len(keys) else None})
            else:
                note, tail_from = {"history_rewritten": True, "diverged_at": p}, p
                self._gap(stream, "history_rewritten", seq, seq, {"call": n, "diverged_at": p})
        # an incomplete baseline response may legitimately appear in history at len(bkeys)
        partial_pos = len(json.loads(base[1])) if base and not base[2] else None
        for i in range(tail_from, len(keys)):
            if roles[i] == "assistant" and i != skip_pos and i != partial_pos:
                self._gap(stream, "unlogged_assistant_message", seq, seq, {"call": n, "pos": i, "key": keys[i]})
        # ---- events: system (when changed), request tail, injected marker, response blocks
        last_sys = c.execute("SELECT sys_k FROM calls WHERE stream=? AND state='confirmed' ORDER BY call_n DESC LIMIT 1", (stream,)).fetchone()
        cur_sys = (req.get("sys") or {}).get("k")
        if cur_sys and (not last_sys or last_sys[0] != cur_sys):
            sysv = self._blocks(req["sys"]["h"], provider)
            sb = sysv if isinstance(sysv, list) else [{"type": "text", "text": sysv}]
            for bi, b in enumerate(sb):
                self._emit(stream, n, "sys", 0, bi, "system", "text", "complete", cj(b), seq)
        for i in range(tail_from, len(keys)):
            msg = self._blocks(req["msgs"][i]["h"], provider)
            for bi, b in enumerate(shapes.blocks_of_message(provider, msg)):
                self._emit(stream, n, "req", i, bi, b.prov, b.kind, "complete", cj(b.data), seq,
                           {"tool_id": b.tool_id, **note, **({"unlogged_assistant": True} if roles[i] == "assistant" else {})})
        inj = req.get("injected")
        if inj:
            self._emit(stream, n, "inj", 0, 0, "memory_injected", "block", "complete",
                       cj({"h": inj["h"], "ids": inj.get("ids", []), "placed_by": inj.get("placed_by"), "spans": req.get("spans", [])}), seq)
        resp_key = None
        if body.get("msg"):
            msg = self._blocks(body["msg"]["h"], provider)
            resp_key = body["msg"]["k"]
            bad = set(body.get("bad_blocks") or [])
            quotes = {q["blk"]: q["h"] for q in body.get("quotes_injected") or []}
            st = {"complete": "complete", "turn_incomplete": "turn_incomplete"}.get(status, "incomplete")
            for bi, b in enumerate(shapes.blocks_of_message(provider, msg)):
                kind = "incomplete_block" if bi in bad else b.kind
                meta = {"tool_id": b.tool_id, "stop": body.get("stop"), "resp_id": body.get("resp_id")}
                if bi in quotes:
                    meta["quotes_injected"] = quotes[bi]
                self._emit(stream, n, "resp", 0, bi, b.prov, kind, "incomplete" if bi in bad else st, cj(b.data), seq, meta)
        resp_ok = 1 if status == "complete" else 0
        c.execute("UPDATE calls SET state='confirmed', resp_key=?, resp_ok=? WHERE stream=? AND call_n=?",
                  (resp_key if status in ("complete", "turn_incomplete") else None, resp_ok, stream, n))
        return None

    # ------------------------------------------------------------ witness, claims
    def set_witness(self, stream: str, seq: int, hash_: str) -> None:
        """Out-of-band chain head (the trusted caller ships it from the tap's commit callback). Only the maximum is kept."""
        self.c.execute("INSERT INTO witness VALUES(?,?,?) ON CONFLICT(stream) DO UPDATE SET seq=excluded.seq, hash=excluded.hash "
                       "WHERE excluded.seq>witness.seq", (stream, seq, hash_))

    def check_witness(self, stream: str, log_head: Tuple[int, str], log_hash_at: Callable[[int], Optional[str]]) -> None:
        w = self.c.execute("SELECT seq, hash FROM witness WHERE stream=?", (stream,)).fetchone()
        if not w:
            return
        if log_head[0] < w[0]:
            self._gap(stream, "tail_truncated", log_head[0] + 1, w[0], {"witness_seq": w[0], "log_head_seq": log_head[0]})
        else:
            h = log_hash_at(w[0])
            if h != w[1]:
                self._gap(stream, "chain_break", w[0], w[0], {"reason": "witness_hash_mismatch"})

    def gaps(self, stream: str, open_only: bool = True) -> List[dict]:
        q = "SELECT id,kind,from_seq,to_seq,blocking,detail,closed,closed_by FROM gaps WHERE stream=?" + (" AND closed IS NULL" if open_only else "")
        return [dict(zip(("id", "kind", "from_seq", "to_seq", "blocking", "detail", "closed", "closed_by"), r)) for r in self.c.execute(q, (stream,))]

    def completeness(self, stream: str) -> dict:
        wm = self.watermark(stream)[0]
        blk = [g for g in self.gaps(stream) if g["blocking"]]
        inflight = [r[0] for r in self.c.execute("SELECT call_n FROM calls WHERE stream=? AND state='requested'", (stream,))]
        pend = self.c.execute("SELECT COUNT(*) FROM pending WHERE stream=?", (stream,)).fetchone()[0]
        return {"complete_up_to": wm if not blk and not pend else None, "watermark": wm, "blocking_gaps": blk, "pending_beyond_gap": pend,
                "in_flight_calls": inflight}

    def claim_complete(self, stream: str, n: int) -> bool:
        """'Memory is complete up to log seq n'. False when a blocking gap is open or records beyond the watermark are held."""
        c = self.completeness(stream)
        return c["complete_up_to"] is not None and n <= c["complete_up_to"]

    def events(self, stream: str, include_port: bool = False) -> List[dict]:
        q = "SELECT ord,ikey,call_n,side,pos,blk,prov,kind,status,content,log_seq,meta FROM events WHERE stream=?" + \
            ("" if include_port else " AND prov!='port'") + " ORDER BY ord"
        cols = ("ord", "ikey", "call_n", "side", "pos", "blk", "prov", "kind", "status", "content", "log_seq", "meta")
        return [dict(zip(cols, r)) for r in self.c.execute(q, (stream,))]

    def sync(self, log, stream: str) -> int:
        wm = self.watermark(stream)[0]
        self.deliver(log.read(stream, wm))
        return self.drain(stream)

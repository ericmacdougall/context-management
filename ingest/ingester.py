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
CREATE TABLE IF NOT EXISTS hist(stream TEXT NOT NULL, idx INTEGER NOT NULL, k TEXT NOT NULL, PRIMARY KEY(stream, idx));
CREATE TABLE IF NOT EXISTS seen(stream TEXT NOT NULL, k TEXT NOT NULL, PRIMARY KEY(stream, k));
CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, kind TEXT NOT NULL, from_seq INTEGER NOT NULL,
  to_seq INTEGER NOT NULL, blocking INTEGER NOT NULL, detail TEXT, opened REAL, closed REAL, closed_by TEXT, UNIQUE(stream, kind, from_seq));
CREATE TABLE IF NOT EXISTS witness(stream TEXT PRIMARY KEY, seq INTEGER NOT NULL, hash TEXT NOT NULL);
"""

BLOCKING = {"seq_hole": 1, "tail_truncated": 1, "chain_break": 1, "conflicting_duplicate": 1, "blob_missing": 1,
            "unlogged_assistant_message": 1, "delta_mismatch": 1, "seed_invalid": 1, "assistant_message_not_in_log": 1, "history_rewritten": 0,
            "ambiguous_alignment": 0}


class Fenced(Exception):
    """Another ingester advanced the watermark between this one's snapshot and its apply (stale token)."""


class Ingester:
    def __init__(self, path: str, blob_source: Callable[[str], Optional[str]], name: str = "ing", sync: str = "FULL",
                 hooks: Optional[Dict[str, Callable]] = None, fence: bool = True, unique: bool = True, state_guard: bool = True, chain: bool = True,
                 seed_verifier: Optional[Callable[[str], str]] = None, backfill_verifier: Optional[Callable[[str], str]] = None):
        """fence/unique=False exist only to build the race control (compare-and-set and event uniqueness removed)."""
        self.path, self.blob, self.name, self.hooks = path, blob_source, name, hooks or {}
        self.fence, self.unique, self._n, self.state_guard, self.chain = fence, unique, 0, state_guard, chain
        self.seed_verifier, self.backfill_verifier = seed_verifier, backfill_verifier
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
                if r["prev"] != head and self.chain:   # (a second fence; the ablation turns it off separately)
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
        if kind == "SEED":
            from .tap import sha_keys
            ok = (self.seed_verifier is not None and sha_keys(body["keys"]) == body["digest"]
                  and self.seed_verifier("seed|%s|%s" % (stream, body["digest"])) == body["sig"])
            if not ok:
                self._gap(stream, "seed_invalid", seq, seq, {"digest": body.get("digest")})
                return None
            base = c.execute("SELECT COALESCE(MAX(idx),-1) FROM hist WHERE stream=?", (stream,)).fetchone()[0]
            for i, k in enumerate(body["keys"]):
                c.execute("INSERT OR IGNORE INTO hist VALUES(?,?,?)", (stream, base + 1 + i, k))
                c.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (stream, k))
            self._emit(stream, 0, "meta", seq, 0, "port", "seed_baseline", "info", cj({"n": len(body["keys"]), "digest": body["digest"]}), seq)
            return None
        if kind == "BACKFILL":
            msg = "backfill|%s|%s|%s|%s" % (stream, body.get("gap_kind"), body.get("key"), body.get("authorized_by"))
            if self.backfill_verifier is None or self.backfill_verifier(msg) != body.get("sig"):
                self._emit(stream, 0, "meta", seq, 1, "port", "backfill_rejected", "info", cj({"by": body.get("authorized_by")}), seq)
                return None
            c.execute("UPDATE gaps SET closed=?, closed_by=? WHERE stream=? AND closed IS NULL AND kind=? AND detail LIKE ?",
                      (time.time(), "backfill:" + str(body.get("authorized_by")), stream, body.get("gap_kind"), '%"key":"' + str(body.get("key")) + '"%'))
            self._emit(stream, 0, "meta", seq, 2, "port", "backfill_applied", "info", cj({"by": body.get("authorized_by"), "kind": body.get("gap_kind")}), seq)
            return None
        self._emit(stream, n, "meta", 0, 0, "port", "unknown_record_kind", "info", kind, seq)
        return None

    def _req_body(self, stream: str, n: int, seq: int) -> dict:
        row = self.c.execute("SELECT body, refs FROM reqs WHERE stream=? AND call_n=?", (stream, n)).fetchone()
        b = json.loads(row[0])
        b["msgs"] = json.loads(row[1])           # full reconstructed refs (the log record may hold only the delta)
        return b

    @staticmethod
    def _lcsub(R: List[str], H: List[str]):
        """Longest common contiguous run of keys between request R and logical history H -> (start_in_R, length, ties). Ties prefer the
        run ending latest in H; `ties` counts the distinct (end_in_R, end_in_H) pairs that reach the maximal length, so ties > 1 means
        the alignment is not unique (repeated content). O(matches), not O(|R|*|H|)."""
        pos: Dict[str, List[int]] = {}
        for j, k in enumerate(H):
            pos.setdefault(k, []).append(j)
        best = (0, -1, 0)               # (length, end_in_H, start_in_R)
        ties = 0
        runs: Dict[int, int] = {}
        for i, k in enumerate(R):
            new: Dict[int, int] = {}
            for j in pos.get(k, ()):
                new[j] = runs.get(j - 1, 0) + 1
                if new[j] > best[0]:
                    ties = 1
                elif new[j] == best[0]:
                    ties += 1
                if (new[j], j) > (best[0], best[1]):
                    best = (new[j], j, i - new[j] + 1)
            runs = new
        return (best[2], best[0], ties) if best[0] else (0, 0, 0)

    def _apply_resp(self, stream: str, n: int, seq: int, body: dict) -> Optional[str]:
        c = self.c
        status = body["status"]
        if body.get("msg") and self.blob(body["msg"]["h"]) is None:
            return body["msg"]["h"]
        if body.get("msg"):
            c.execute("UPDATE reqs SET resp_ref=? WHERE stream=? AND call_n=?", (cj(body["msg"]), stream, n))
        row = c.execute("SELECT state FROM calls WHERE stream=? AND call_n=?", (stream, n)).fetchone()
        if row is None or (row[0] in ("confirmed", "recovered") and self.state_guard):
            return None
        req = self._req_body(stream, n, seq)
        provider = req["provider"]
        recovered = status == "recovered"
        if recovered and row[0] != "superseded":
            status = "complete" if body.get("turn_complete") else "turn_incomplete"      # same path as a confirmed call, flagged
        elif recovered:
            c.execute("UPDATE calls SET state='recovered' WHERE stream=? AND call_n=?", (stream, n))
            msg = self._blocks(body["msg"]["h"], provider)
            for bi, b in enumerate(shapes.blocks_of_message(provider, msg)):
                self._emit(stream, n, "resp", 0, bi, b.prov, b.kind, "complete", cj(b.data), seq,
                           {"tool_id": b.tool_id, "stop": body.get("stop"), "resp_id": body.get("resp_id"), "recovered": True})
            return None
        if status in ("rejected", "failed_unknown"):
            c.execute("UPDATE calls SET state=? WHERE stream=? AND call_n=?", ("rejected" if status == "rejected" else "unconfirmed", stream, n))
            self._emit(stream, n, "meta", 0, 0, "port", "call_" + status, "info", cj({"error": body.get("error")}), seq)
            return None
        keys = [m["k"] for m in req["msgs"]]
        roles = [m["role"] for m in req["msgs"]]
        H = [r[0] for r in c.execute("SELECT k FROM hist WHERE stream=? ORDER BY idx", (stream,))]
        seen = {r[0] for r in c.execute("SELECT k FROM seen WHERE stream=?", (stream,))} if H else set()
        base = c.execute("SELECT call_n, keys, resp_key FROM calls WHERE stream=? AND state='confirmed' ORDER BY call_n DESC LIMIT 1", (stream,)).fetchone()
        note: dict = {"recovered": True} if recovered else {}
        req_note: dict = {}
        declared = req.get("retry_of")
        retry_ok = False
        if declared is not None and not recovered:
            # explicit host-declared retry identity (ASSUMPTION: an honest host). Honoured only for the immediately preceding confirmed call
            # with identical message keys; the suppression it causes is made visible below, never silent.
            if base and declared == base[0] and keys == json.loads(base[1]):
                retry_ok = True
            else:
                self._emit(stream, n, "meta", 4, 0, "port", "retry_claim_ignored", "info", cj({"claimed": declared}), seq)
        if retry_ok:
            note = {"retry_of": base[0], "declared": True}
            s, L = 0, len(keys)
            self._emit(stream, n, "meta", 3, 0, "port", "declared_retry", "info", cj({"retry_of": base[0], "suppressed_request_messages": len(keys)}), seq)
        elif H:
            s, L, ties = self._lcsub(keys, H)
            if L == 0:
                s, L = max(len(keys) - 1, 0), 0          # nothing aligns: only the last message is the new tail, the rest is host-authored
            elif ties > 1:
                self._gap(stream, "ambiguous_alignment", seq, seq, {"call": n, "reason": "tie", "ties": ties})
                req_note = {"ambiguous_repeat": True}
            if s + L >= len(keys) and keys:
                # Content alone cannot tell a retry, a regeneration or a legitimate repeat from a request with nothing new. Without a
                # host-declared retry the final message is never suppressed: it is emitted as a new occurrence, flagged.
                L = len(keys) - 1 - s if len(keys) - 1 >= s else 0
                self._gap(stream, "ambiguous_alignment", seq, seq, {"call": n, "reason": "no_new_message"})
                req_note = {"ambiguous_repeat": True}
        else:
            s, L = 0, 0
        tail_from = s + L
        # leading, unaligned messages: host-authored (compaction summaries, re-seeded context), recorded once, never as user content
        for i in range(0, s if H or "retry_of" in note else 0):
            if keys[i] in seen:
                continue
            msg = self._blocks(req["msgs"][i]["h"], provider)
            for bi, b in enumerate(shapes.blocks_of_message(provider, msg)):
                self._emit(stream, n, "host", i, bi, "host", "host_authored_" + str(roles[i]), "complete", cj(b.data), seq, {"tool_id": b.tool_id})
            c.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (stream, keys[i]))
        partial_ok = bool(base) and not base[2]
        for i in range(tail_from, len(keys)):
            if roles[i] == "assistant" and not (partial_ok and i == tail_from):
                self._gap(stream, "unlogged_assistant_message", seq, seq, {"call": n, "pos": i, "key": keys[i]})
        # ---- events: system (when changed), request tail, injected marker, response blocks
        last_sys = c.execute("SELECT sys_k FROM calls WHERE stream=? AND state='confirmed' ORDER BY call_n DESC LIMIT 1", (stream,)).fetchone()
        cur_sys = (req.get("sys") or {}).get("k")
        if cur_sys and (not last_sys or last_sys[0] != cur_sys):
            sysv = self._blocks(req["sys"]["h"], provider)
            sb = sysv if isinstance(sysv, list) else [{"type": "text", "text": sysv}]
            for bi, b in enumerate(sb):
                self._emit(stream, n, "sys", 0, bi, "system", "text", "complete", cj(b), seq)
        hn = len(H)
        for i in range(tail_from, len(keys)):
            msg = self._blocks(req["msgs"][i]["h"], provider)
            for bi, b in enumerate(shapes.blocks_of_message(provider, msg)):
                self._emit(stream, n, "req", i, bi, b.prov, b.kind, "complete", cj(b.data), seq,
                           {"tool_id": b.tool_id, **note, **req_note, **({"unlogged_assistant": True} if roles[i] == "assistant" else {})})
            if "retry_of" not in note:
                c.execute("INSERT OR IGNORE INTO hist VALUES(?,?,?)", (stream, hn, keys[i]))
                hn += 1
            c.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (stream, keys[i]))
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
                meta = {"tool_id": b.tool_id, "stop": body.get("stop"), "resp_id": body.get("resp_id"), **note}
                if bi in quotes:
                    meta["quotes_injected"] = quotes[bi]
                self._emit(stream, n, "resp", 0, bi, b.prov, kind, "incomplete" if bi in bad else st, cj(b.data), seq, meta)
            if status in ("complete", "turn_incomplete"):
                if "retry_of" in note and H and H[-1] == (base[2] if base else None):
                    c.execute("UPDATE hist SET k=? WHERE stream=? AND idx=?", (resp_key, stream, len(H) - 1))     # retry: the host saw THIS answer
                else:
                    c.execute("INSERT OR IGNORE INTO hist VALUES(?,?,?)", (stream, hn, resp_key))
                c.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (stream, resp_key))
        resp_ok = 1 if status == "complete" else 0
        c.execute("UPDATE calls SET state='confirmed', resp_key=?, resp_ok=? WHERE stream=? AND call_n=?",
                  (resp_key if status in ("complete", "turn_incomplete") else None, resp_ok, stream, n))
        # an earlier unconfirmed call whose request is a prefix of this one is superseded (this call is its retry)
        for (pn, pk) in c.execute("SELECT call_n, keys FROM calls WHERE stream=? AND state IN ('unconfirmed','requested') AND call_n<?", (stream, n)).fetchall():
            pkeys = json.loads(pk or "[]")
            if pkeys == keys[:len(pkeys)]:
                c.execute("UPDATE calls SET state='superseded' WHERE stream=? AND call_n=?", (stream, pn))
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
        """state: 'complete' | 'complete_except_unconfirmed' (ids listed) | 'incomplete' (blocking gap or held records)."""
        wm = self.watermark(stream)[0]
        blk = [g for g in self.gaps(stream) if g["blocking"]]
        unconf = [r[0] for r in self.c.execute("SELECT call_n FROM calls WHERE stream=? AND state IN ('requested','unconfirmed') ORDER BY call_n", (stream,))]
        pend = self.c.execute("SELECT COUNT(*) FROM pending WHERE stream=?", (stream,)).fetchone()[0]
        state = "incomplete" if (blk or pend) else ("complete_except_unconfirmed" if unconf else "complete")
        amb = sorted({json.loads(g["detail"] or "{}").get("call") for g in self.gaps(stream) if g["kind"] == "ambiguous_alignment"} - {None})
        return {"state": state, "complete_up_to": wm if state == "complete" else None, "watermark": wm, "blocking_gaps": blk,
                "pending_beyond_gap": pend, "unconfirmed_calls": unconf, "in_flight_calls": unconf, "ambiguous_calls": amb}

    def claim_complete(self, stream: str, n: int) -> bool:
        """'Memory is complete up to log seq n' with NOTHING outstanding: false across an unconfirmed call or a blocking gap."""
        c = self.completeness(stream)
        return c["state"] == "complete" and n <= c["watermark"]

    def claim_exactly_once(self, stream: str) -> bool:
        """True only when nothing is outstanding AND no call's request alignment was ambiguous (no flagged possible duplicate)."""
        c = self.completeness(stream)
        return c["state"] == "complete" and not c["ambiguous_calls"]

    def claim_complete_except(self, stream: str, n: int):
        """(ok, unconfirmed_call_ids): complete up to n apart from the listed calls whose response is not confirmed."""
        c = self.completeness(stream)
        return (c["state"] in ("complete", "complete_except_unconfirmed") and n <= c["watermark"]), c["unconfirmed_calls"]

    def events(self, stream: str, include_port: bool = False) -> List[dict]:
        q = "SELECT ord,ikey,call_n,side,pos,blk,prov,kind,status,content,log_seq,meta FROM events WHERE stream=?" + \
            ("" if include_port else " AND prov!='port'") + " ORDER BY ord"
        cols = ("ord", "ikey", "call_n", "side", "pos", "blk", "prov", "kind", "status", "content", "log_seq", "meta")
        return [dict(zip(cols, r)) for r in self.c.execute(q, (stream,))]

    def sync(self, log, stream: str) -> int:
        wm = self.watermark(stream)[0]
        self.deliver(log.read(stream, wm))
        return self.drain(stream)

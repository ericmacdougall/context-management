"""Append-only, hash-chained, per-stream-sequenced port log on SQLite (stand-in for the Postgres event log).
Durability: WAL + synchronous=FULL: survives process death (tested by os._exit). Power-loss durability is NOT tested."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .canon import cj, sha

GENESIS = "0" * 64
SCHEMA = """
CREATE TABLE IF NOT EXISTS port_log(stream TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, call_n INTEGER,
  body TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY(stream, seq));
CREATE TABLE IF NOT EXISTS blobs(h TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issued(stream TEXT NOT NULL, h TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(stream, h));
CREATE TRIGGER IF NOT EXISTS port_log_noupd BEFORE UPDATE ON port_log BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER IF NOT EXISTS port_log_nodel BEFORE DELETE ON port_log BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER IF NOT EXISTS blobs_noupd BEFORE UPDATE ON blobs BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER IF NOT EXISTS blobs_nodel BEFORE DELETE ON blobs BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""


def connect(path: str, sync: str = "FULL") -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=%s" % sync)
    return c


class PortLog:
    def __init__(self, path: str, sync: str = "FULL"):
        self.path, self.sync = path, sync
        self._tl = threading.local()
        self.writes = 0           # commits issued by this object (cost accounting)
        self.bytes = 0            # payload bytes written (record bodies + new blobs)
        c = self.conn()
        c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = connect(self.path, self.sync)
            self._tl.c = c
        return c

    def append(self, stream: str, kind: str, call_n: Optional[int], body: dict, blobs: Optional[Dict[str, str]] = None,
               new_call: bool = False) -> Tuple[int, int, str]:
        """One transaction: allocate seq (and call_n for a new call), chain the hash, store new blobs, commit. Returns
        (seq, call_n, hash). Returning means the record is durable."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT seq, hash FROM port_log WHERE stream=? ORDER BY seq DESC LIMIT 1", (stream,)).fetchone()
            seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
            if new_call:
                r = c.execute("SELECT COALESCE(MAX(call_n),0) FROM port_log WHERE stream=?", (stream,)).fetchone()
                call_n = r[0] + 1
            b = dict(body)
            if call_n is not None:
                b["call_n"] = call_n
            text = cj(b)
            h = sha(prev + "|" + kind + "|" + text)
            nb = 0
            for bh, bb in (blobs or {}).items():
                cur = c.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (bh, bb))
                if cur.rowcount:
                    nb += len(bb)
            c.execute("INSERT INTO port_log VALUES(?,?,?,?,?,?,?,?)", (stream, seq, kind, call_n, text, prev, h, time.time()))
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self.writes += 1
        self.bytes += len(text) + nb
        return seq, call_n, h

    def record_issued(self, stream: str, h: str, text: str) -> None:
        c = self.conn()
        c.execute("INSERT OR IGNORE INTO issued VALUES(?,?,?)", (stream, h, text))

    def issued(self, stream: str) -> Dict[str, str]:
        return {h: t for h, t in self.conn().execute("SELECT h, text FROM issued WHERE stream=?", (stream,))}

    def read(self, stream: str, after_seq: int = 0, limit: int = 100000) -> List[dict]:
        rows = self.conn().execute("SELECT seq,kind,call_n,body,prev,hash,ts FROM port_log WHERE stream=? AND seq>? ORDER BY seq LIMIT ?",
                                   (stream, after_seq, limit)).fetchall()
        return [{"stream": stream, "seq": r[0], "kind": r[1], "call_n": r[2], "body": r[3], "prev": r[4], "hash": r[5], "ts": r[6]} for r in rows]

    def head(self, stream: str) -> Tuple[int, str]:
        r = self.conn().execute("SELECT seq, hash FROM port_log WHERE stream=? ORDER BY seq DESC LIMIT 1", (stream,)).fetchone()
        return (r[0], r[1]) if r else (0, GENESIS)

    def streams(self) -> List[str]:
        return [r[0] for r in self.conn().execute("SELECT DISTINCT stream FROM port_log")]

    def blob(self, h: str) -> Optional[str]:
        r = self.conn().execute("SELECT body FROM blobs WHERE h=?", (h,)).fetchone()
        return r[0] if r else None

    def close(self) -> None:
        c = getattr(self._tl, "c", None)
        if c:
            c.close()
            self._tl.c = None

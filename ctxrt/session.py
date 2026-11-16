"""P2: block regeneration at a sealed watermark, a host-session simulator with compaction / checkpoint / resume, and the ingest path with its
provenance filter. Stub seal only (the write-path seal belongs to another stream)."""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from memcore.inbox import render_data_block
from ctxrt.kernel import MemoryLog, Seal, sha
from ctxrt.pulltools import tf_score, toks



def record_set_hash(ids: List[str]) -> str:
    return sha(sorted(ids))


def sealed_delimiter(seal: Seal, secret: bytes) -> str:
    """Delimiter derived from the seal and a host-held secret: regeneration is byte-identical, and nobody without the secret can predict it."""
    return "MEMCORE-" + hmac.new(secret, ("seal|%d|%s" % (seal.watermark, seal.digest)).encode(), hashlib.sha256).hexdigest()[:16]


def pick(log: MemoryLog, query: str, lo: int, hi: int, k: int) -> List[dict]:
    """Records with lo < seq <= hi ranked by term overlap then seq (deterministic); each rendered as id, ts, text."""
    qt = toks(query)
    cand = []
    for e in log.upto(hi):
        if e["seq"] <= lo:
            continue
        sc = tf_score(qt, e["text"])
        if sc:
            cand.append((-sc, e["seq"], e))
    cand.sort(key=lambda t: t[:2])
    return [c[2] for c in cand[:k]]


def render(events: List[dict], turn_id: str, label: str, delimiter: Optional[str] = None) -> Tuple[str, List[str]]:
    recs = [{"id": e["oid"], "ts": e["ts"], "text": e["text"]} for e in events]
    block = render_data_block(recs, turn_id, delimiter=delimiter, label=label) if recs else ""
    if block and delimiter:
        assert block.count("<<<" + delimiter) == 2       # a re-rolled delimiter would break byte identity: never in practice, asserted
    return block, [e["oid"] for e in events]


def sealed_block(log: MemoryLog, seal: Seal, query: str, secret: bytes, k: int = 5) -> Tuple[str, List[str]]:
    """The block AS OF the seal: only events with seq <= seal.watermark; deterministic bytes. The delimiter is FRAMING keyed by a configured secret
    (ctxrt.config.load_secret), never an authenticity proof."""
    evs = pick(log, query, 0, seal.watermark, k)
    return render(evs, "seal-%d@1970-01-01T00:00:00Z" % seal.watermark, "remembered events (sealed)", sealed_delimiter(seal, secret))


def independent_block(events_json: str, wm: int, query: str, secret: bytes, digest: str, k: int = 5) -> str:
    """A SECOND implementation used only to check the first: rebuilds the sealed block from a serialised copy of the log prefix (not from the live
    MemoryLog object), with its own ranking loop and its own delimiter derivation."""
    import json as _j
    evs = _j.loads(events_json)[:wm]
    qwords = set(re.findall(r"\w+", query.lower()))
    scored = []
    for e in evs:
        ws = re.findall(r"\w+", e["text"].lower())
        sc = sum(min(3, ws.count(t)) for t in qwords)
        if sc:
            scored.append((sc, e))
    scored.sort(key=lambda t: (-t[0], t[1]["seq"]))
    chosen = [e for _, e in scored[:k]]
    delim = "MEMCORE-" + hmac.new(secret, ("seal|%d|%s" % (wm, digest)).encode(), hashlib.sha256).hexdigest()[:16]
    if not chosen:
        return ""
    return render_data_block([{"id": e["oid"], "ts": e["ts"], "text": e["text"]} for e in chosen], "seal-%d@1970-01-01T00:00:00Z" % wm, delimiter=delim, label="remembered events (sealed)")


def delta_block(log: MemoryLog, seal: Seal, query: str, k: int = 50) -> Tuple[str, List[str]]:
    evs = pick(log, query, seal.watermark, log.watermark(), k)
    return render(evs, "delta-after-%d" % seal.watermark, "remembered events ADDED SINCE the sealed block (labelled delta)")


class Ledger:
    """Every block the host injected: its delimiter and a hash of its text, PERSISTED (sqlite file) so a host restart does not forget what it issued.
    The provenance filter keys on THIS, never on a bare regex."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS ledger(delim TEXT, sha TEXT PRIMARY KEY)")

    def issue(self, block: str):
        if not block:
            return
        m = re.search(r"<<<(MEMCORE-[0-9a-f]+) BEGIN", block)
        if m:
            self.db.execute("INSERT OR IGNORE INTO ledger VALUES(?,?)", (m.group(1), hashlib.sha256(block.encode()).hexdigest()))

    @property
    def delims(self):
        return {r[0] for r in self.db.execute("SELECT DISTINCT delim FROM ledger")}

    def strip(self, text: str) -> str:
        """Remove ledgered blocks. A block whose END line is missing (truncated) is removed only as far as its header line and the JSON record lines that
        follow it; the user's own text after it is kept."""
        for d in self.delims:
            while True:
                a = text.find("<<<%s BEGIN" % d)
                if a < 0:
                    break
                end = "<<<%s END>>>" % d
                b = text.find(end, a)
                if b >= 0:
                    text = text[:a] + text[b + len(end):]
                    continue
                lines = text[a:].split("\n")
                k = 1
                while k < len(lines) and lines[k].startswith("{"):
                    k += 1
                text = text[:a] + "\n".join(lines[k:])
        return text.strip()


def extract_block(text: str) -> str:
    m = re.search(r"<<<(MEMCORE-[0-9a-f]+) BEGIN.*?<<<\1 END>>>", text, re.S)
    return m.group(0) if m else ""


class Entry(dict):
    """Host transcript entry: id, origin (user_typed | assistant | tool | injected | summary), text."""


def ingest(entries: List[Entry], log: MemoryLog, ledger: Ledger, mode: str, seen: set, session: str = "host", ts: str = "2026-06-01T00:00:00Z", summary_ids=frozenset()) -> int:
    """Turn host transcript entries into memory events. mode: full | none | origin_only | strip_only. Returns events appended.
    full = origin allow-list (only user_typed text becomes a user statement; assistant/tool entries are not user facts) AND ledger strip."""
    n = 0
    for e in entries:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        text, origin = e["text"], e["origin"]
        if e["id"] in summary_ids and mode in ("full", "origin_only", "strip_only"):     # recorded by the compaction EVENT, whatever role the host gave the entry
            continue
        if mode in ("full", "origin_only") and origin != "user_typed":
            continue
        if mode in ("full", "strip_only"):
            text = ledger.strip(text)
        if not text:
            continue
        log.append(session, ts, "user", text, meta={"entry": e["id"]})
        n += 1
    return n


# ---------------------------------------------------------------- host simulator
class Summariser:
    """Stub compaction summariser. mode: copy (copies the injected block), paraphrase (marker text in prose, no delimiters), stale (invents a stale value), drop."""

    def __init__(self, mode: str):
        self.mode = mode

    def summarise(self, entries: List[Entry], marker_text: str) -> str:
        injected = [e["text"] for e in entries if e["origin"] == "injected"]
        if self.mode == "copy":
            return "Summary of earlier turns. Memory block was:\n" + (injected[-1] if injected else "(none)")
        if self.mode == "paraphrase":
            return "Summary of earlier turns. The memory noted that " + marker_text + "; the user asked a few questions."
        if self.mode == "stale":
            return "Summary of earlier turns. Memory block:\n<<<MEMCORE-%s BEGIN turn=\"old\">>> {\"id\":\"m_stale\",\"text\":\"the office is in Madrid (stale)\"} <<<MEMCORE-%s END>>>" % ("0" * 16, "0" * 16)
        return "Summary of earlier turns."


class Host:
    """Minimal host: transcript entries, a seal, checkpoints. layer: own (injected block is its own entry) | merged (block merged into the user text)."""

    def __init__(self, log: MemoryLog, query_topic: str, secret: bytes, layer: str = "own", block_policy: str = "regenerate", ledger_path: str = ":memory:"):
        self.log, self.layer, self.topic, self.secret = log, layer, query_topic, secret
        self.compaction_summary_ids = set()
        self.entries: List[Entry] = []
        self.history: List[Entry] = []             # every entry ever created, in order (what a continuously ingesting host has seen)
        self.ledger = Ledger(ledger_path)
        self.seal: Optional[Seal] = None
        self.seal_query = query_topic
        self.sealed_sha: Dict[int, str] = {}         # watermark -> sha of the sealed block as first rendered
        self._n = 0
        self.block_policy = block_policy             # regenerate | from_summary (C2) | skip_regeneration (C3)
        self.cached_block: Optional[str] = None
        self.summary_text: Optional[str] = None
        self.checkpoints: List[dict] = []
        self.served: List[dict] = []                 # per turn: what was served and what the sealed regeneration is

    def _eid(self):
        self._n += 1
        return "e%d" % self._n

    def _add(self, e):
        self.entries.append(e)
        self.history.append(Entry(e))

    def new_seal(self):
        self.seal = Seal.of(self.log)
        blk, ids = sealed_block(self.log, self.seal, self.seal_query, self.secret)
        self.sealed_sha[self.seal.watermark] = hashlib.sha256(blk.encode()).hexdigest()
        self.cached_block = blk

    def block_for_turn(self) -> Tuple[str, str, List[str]]:
        """Returns (sealed_part, delta_part, sealed_ids). The sealed part depends on block_policy."""
        if self.seal is None:
            self.new_seal()
        regen, ids = sealed_block(self.log, self.seal, self.seal_query, self.secret)
        if self.block_policy == "regenerate":
            sealed = regen
        elif self.block_policy == "from_summary":     # C2: whatever block the compacted summary holds (none if it holds none)
            sealed = extract_block(self.summary_text) if self.summary_text is not None else regen
        else:                                         # skip_regeneration: serve what was cached at the previous seal
            sealed = self.cached_block if self.cached_block is not None else regen
        d, _ = delta_block(self.log, self.seal, self.seal_query)
        return sealed, d, ids

    def turn(self, user_text: str) -> dict:
        sealed, delta, ids = self.block_for_turn()
        import json as _json
        regen = independent_block(_json.dumps(self.log.upto(self.seal.watermark)), self.seal.watermark, self.seal_query, self.secret, self.seal.digest)   # INDEPENDENT implementation
        block = sealed + ("\n" + delta if delta else "")
        self.ledger.issue(sealed)
        self.ledger.issue(delta)
        uid = self._eid()
        if self.layer == "merged":
            self._add(Entry(id=uid, origin="user_typed", text=user_text + ("\n" + block if block else "")))
        else:
            self._add(Entry(id=uid, origin="user_typed", text=user_text))
            if block:
                self._add(Entry(id=self._eid(), origin="injected", text=block))
        self._add(Entry(id=self._eid(), origin="assistant", text="ok"))
        rec = {"sealed_equals_regen": sealed == regen, "sealed_sha_ok": hashlib.sha256(regen.encode()).hexdigest() == self.sealed_sha.get(self.seal.watermark),
               "sealed_text": sealed, "regen_text": regen, "delta_text": delta}
        self.served.append(rec)
        return rec

    def compact(self, summariser: Summariser, marker_text: str, summary_role: str = "summary"):
        """summary_role="user_typed" models a host (Claude Code style) that records its compaction summary as a USER message; the compaction EVENT
        (compaction_summary_ids) is what marks it, not the role."""
        s = Summariser.summarise(summariser, self.entries, marker_text)
        self.summary_text = s
        summ_e = Entry(id=self._eid(), origin=summary_role, text=s)
        self.compaction_summary_ids.add(summ_e["id"])
        self.history.append(Entry(summ_e))
        self.entries = [summ_e] + self.entries[-1:]
        self.cached_block = self.cached_block                       # unchanged unless a new seal is cut
        self.new_seal_after_compaction()

    def new_seal_after_compaction(self):
        old = self.cached_block
        self.seal = Seal.of(self.log)
        blk, _ = sealed_block(self.log, self.seal, self.seal_query, self.secret)
        self.sealed_sha[self.seal.watermark] = hashlib.sha256(blk.encode()).hexdigest()
        self.cached_block = old if self.block_policy == "skip_regeneration" else blk

    def checkpoint(self):
        self.checkpoints.append({"entries": [Entry(e) for e in self.entries], "seal": self.seal, "n": self._n})

    def resume(self, idx: int):
        cp = self.checkpoints[idx]
        if not cp["seal"].verify(self.log):
            raise ValueError("checkpoint seal does not verify against the log")
        self.entries, self.seal = [Entry(e) for e in cp["entries"]], cp["seal"]       # the id counter is NOT restored: entry ids never repeat

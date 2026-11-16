"""P2: block regeneration at a sealed watermark, a host-session simulator with compaction / checkpoint / resume, and the ingest path with its
provenance filter. Stub seal only (the write-path seal belongs to another stream)."""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Dict, List, Optional, Tuple

from memcore.inbox import render_data_block
from ctxrt.kernel import MemoryLog, Seal, sha
from ctxrt.pulltools import tf_score, toks

HOST_SECRET = b"host-delimiter-secret"


def record_set_hash(ids: List[str]) -> str:
    return sha(sorted(ids))


def sealed_delimiter(seal: Seal, secret: bytes = HOST_SECRET) -> str:
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


def sealed_block(log: MemoryLog, seal: Seal, query: str, k: int = 5) -> Tuple[str, List[str]]:
    """The block AS OF the seal: only events with seq <= seal.watermark; deterministic bytes."""
    evs = pick(log, query, 0, seal.watermark, k)
    return render(evs, "seal-%d" % seal.watermark, "remembered events (sealed)", sealed_delimiter(seal))


def delta_block(log: MemoryLog, seal: Seal, query: str, k: int = 50) -> Tuple[str, List[str]]:
    evs = pick(log, query, seal.watermark, log.watermark(), k)
    return render(evs, "delta-after-%d" % seal.watermark, "remembered events ADDED SINCE the sealed block (labelled delta)")


class Ledger:
    """Every block the host injected: its delimiter and a hash of its text. The provenance filter keys on THIS, never on a bare regex."""

    def __init__(self):
        self.delims: Dict[str, str] = {}
        self.texts = set()

    def issue(self, block: str):
        if not block:
            return
        m = re.search(r"<<<(MEMCORE-[0-9a-f]+) BEGIN", block)
        if m:
            self.delims[m.group(1)] = hashlib.sha256(block.encode()).hexdigest()
            self.texts.add(hashlib.sha256(block.encode()).hexdigest())

    def strip(self, text: str) -> str:
        for d in self.delims:
            while True:
                a = text.find("<<<%s BEGIN" % d)
                if a < 0:
                    break
                end = "<<<%s END>>>" % d
                b = text.find(end, a)
                if b < 0:
                    text = text[:a]
                    break
                text = text[:a] + text[b + len(end):]
        return text.strip()


def extract_block(text: str) -> str:
    m = re.search(r"<<<(MEMCORE-[0-9a-f]+) BEGIN.*?<<<\1 END>>>", text, re.S)
    return m.group(0) if m else ""


class Entry(dict):
    """Host transcript entry: id, origin (user_typed | assistant | tool | injected | summary), text."""


def ingest(entries: List[Entry], log: MemoryLog, ledger: Ledger, mode: str, seen: set, session: str = "host", ts: str = "2026-06-01T00:00:00Z") -> int:
    """Turn host transcript entries into memory events. mode: full | none | origin_only | strip_only. Returns events appended.
    full = origin allow-list (only user_typed text becomes a user statement; assistant/tool entries are not user facts) AND ledger strip."""
    n = 0
    for e in entries:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        text, origin = e["text"], e["origin"]
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

    def __init__(self, log: MemoryLog, query_topic: str, layer: str = "own", block_policy: str = "regenerate"):
        self.log, self.layer, self.topic = log, layer, query_topic
        self.entries: List[Entry] = []
        self.history: List[Entry] = []             # every entry ever created, in order (what a continuously ingesting host has seen)
        self.ledger = Ledger()
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
        blk, ids = sealed_block(self.log, self.seal, self.seal_query)
        self.sealed_sha[self.seal.watermark] = hashlib.sha256(blk.encode()).hexdigest()
        self.cached_block = blk

    def block_for_turn(self) -> Tuple[str, str, List[str]]:
        """Returns (sealed_part, delta_part, sealed_ids). The sealed part depends on block_policy."""
        if self.seal is None:
            self.new_seal()
        regen, ids = sealed_block(self.log, self.seal, self.seal_query)
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
        regen, _ = sealed_block(self.log, self.seal, self.seal_query)
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

    def compact(self, summariser: Summariser, marker_text: str):
        s = Summariser.summarise(summariser, self.entries, marker_text)
        self.summary_text = s
        summ_e = Entry(id=self._eid(), origin="summary", text=s)
        self.history.append(Entry(summ_e))
        self.entries = [summ_e] + self.entries[-1:]
        self.cached_block = self.cached_block                       # unchanged unless a new seal is cut
        self.new_seal_after_compaction()

    def new_seal_after_compaction(self):
        old = self.cached_block
        self.seal = Seal.of(self.log)
        blk, _ = sealed_block(self.log, self.seal, self.seal_query)
        self.sealed_sha[self.seal.watermark] = hashlib.sha256(blk.encode()).hexdigest()
        self.cached_block = old if self.block_policy == "skip_regeneration" else blk

    def checkpoint(self):
        self.checkpoints.append({"entries": [Entry(e) for e in self.entries], "seal": self.seal, "n": self._n})

    def resume(self, idx: int):
        cp = self.checkpoints[idx]
        self.entries, self.seal, self._n = [Entry(e) for e in cp["entries"]], cp["seal"], cp["n"]

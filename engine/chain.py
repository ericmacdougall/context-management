"""Per-session hash chain over the stored (ciphertext) form of events.

The hash covers the ciphertext, never the plaintext: after the subject key is
destroyed the chain still verifies, and the hash cannot be used to confirm a
guess of the erased content.
"""
import hashlib

GENESIS = "0" * 64


def _lp(value) -> bytes:
    """Length-prefixed encoding so field boundaries cannot be shifted."""
    b = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return len(b).to_bytes(8, "big") + b


def event_hash(prev_hash, tenant_id, session_id, seq, event_id, ts_event,
               actor, trust_origin, kind, subject_id, content_enc: bytes) -> str:
    h = hashlib.sha256()
    for part in (prev_hash, tenant_id, session_id, seq, event_id, ts_event,
                 actor, trust_origin, kind, subject_id, bytes(content_enc)):
        h.update(_lp(part))
    return h.hexdigest()


def verify_chain(rows):
    """rows: iterable of dicts in seq order with the fields of event_hash plus
    prev_hash and hash. Returns (ok, first_bad_seq_or_None, count)."""
    prev = GENESIS
    expected_seq = 1
    n = 0
    for r in rows:
        n += 1
        if r["seq"] != expected_seq:
            return False, r["seq"], n
        if r["prev_hash"] != prev:
            return False, r["seq"], n
        want = event_hash(prev, r["tenant_id"], r["session_id"], r["seq"], r["event_id"],
                          r["ts_event"], r["actor"], r["trust_origin"], r["kind"],
                          r["subject_id"], r["content_enc"])
        if r["hash"] != want:
            return False, r["seq"], n
        prev = r["hash"]
        expected_seq += 1
    return True, None, n

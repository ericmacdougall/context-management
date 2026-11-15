"""Opaque ids for everything a model sees. Copy of the retrieval owner's eval/emlab/anon.py (feat/eval-scaffold) so both streams agree.
Real LongMemEval gold sessions are named answer_<hash>_<n> and fillers sharegpt_, ultrachat_ or hex ids; a model shown real ids could pick gold by name.
Opaque session id = "s" + first 12 hex of sha256("16096:anon:<session id>"); opaque round id = "<opaque session>:<index>". Map back only in scoring code."""
import hashlib


def opaque_sid(sid):
    return "s" + hashlib.sha256(("16096:anon:" + sid).encode()).hexdigest()[:12]


def opaque_rid(rid):
    sid, idx = rid.rsplit(":", 1)
    return "%s:%s" % (opaque_sid(sid), idx)


class AnonMap:
    def __init__(self, session_ids):
        ids = list(session_ids)
        self.rev = {opaque_sid(s): s for s in ids}
        if len(self.rev) != len(set(ids)):
            raise ValueError("opaque id collision")

    def real_sid(self, osid):
        return self.rev[osid]

    def real_rid(self, orid):
        sid, idx = orid.rsplit(":", 1)
        return "%s:%s" % (self.rev[sid], idx)

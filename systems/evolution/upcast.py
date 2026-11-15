"""Event schema versions and the pure upcaster. Stored bytes are never rewritten; the hash chain covers the stored
(ciphertext) form. Readers convert any stored version to the CURRENT in-memory shape.
step_completed v1 stored: {step_id, artifacts:[..], serves?}
step_completed v2 stored: {_v:2, step_id, produced:[..] (renamed from artifacts), outcome (new, required), serves?}
current in-memory shape: {step_id, artifacts:[..], outcome, serves?}   (v1 gets outcome='unknown')"""

CURRENT = 2


def stored_version(body):
    return int(body.get("_v", 1))


def upcast(kind, body):
    v = stored_version(body)
    if kind != "step_completed":
        return dict(body)
    if v == 1:
        out = {k: x for k, x in body.items() if k != "artifacts"}
        out["artifacts"] = list(body.get("artifacts", [])); out["outcome"] = "unknown"
        return out
    if v == 2:
        if "outcome" not in body:
            raise ValueError("v2 step_completed requires outcome")
        out = {k: x for k, x in body.items() if k not in ("produced", "_v")}
        out["artifacts"] = list(body.get("produced", [])); return out
    raise ValueError(f"unknown schema version {v}")


def encode_v2(body):
    out = {k: x for k, x in body.items() if k not in ("artifacts", "outcome")}
    out["_v"] = 2; out["produced"] = list(body.get("artifacts", [])); out["outcome"] = body.get("outcome", "ok")
    return out

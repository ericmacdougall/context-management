"""Row 3: checkpoint seal (wp/seal.py). R runs per case and arm, fresh tenant each.
  P prefix: seal, then append and project more: verify ok (a seal proves a prefix, it does not freeze memory).
  E edited seal: the memory_watermark of a seal is changed and the MAC recomputed by someone WITHOUT the key store.
    Fenced: verify reports mac_invalid. Control: an unkeyed seal (MAC = plain SHA-256 of the body): the edit verifies.
  T truncated log after sealing (last k events and the chain head removed, as in row 6). Fenced: verify reports
    head_missing. Control: a verify that checks only the seal's own hash and MAC: ok.
Thresholds: THRESHOLDS.md amendment 6. usage: python -m exp.row3_seal [R]"""
import hashlib
import json
import random
import sys

from exp.common import write_result
from exp.row6_chain import _drop_tail
from exp.util import tenant, msg, session, env
from wp import db, log, projector, seal as S

RNG = random.Random(3003)


def _setup(tag):
    conn, ks, es = env()
    t = tenant(tag)
    s = session(conn, t)
    log.append(conn, ks, s, [msg(i, f"before seal {i}", "U") for i in range(8)])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    return conn, ks, es, t, s


def case_p(arm):
    conn, ks, es, t, s = _setup("r3p")
    sd = S.seal(conn, ks, es, t)
    log.append(conn, ks, s, [msg(100 + i, f"after seal {i}", "U") for i in range(RNG.randint(1, 5))])
    p = projector.Projector(conn, ks, es, t); p.run(); p.stop()
    v = S.verify(conn, ks, es, json.loads(json.dumps(sd)))
    return v["ok"], v


def case_e(arm):
    conn, ks, es, t, s = _setup("r3e")
    real = S._mac
    if arm == "control":
        S._mac = lambda ks_, tenant_, body: hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        sd = S.seal(conn, ks, es, t)
        forged = json.loads(json.dumps(sd))
        forged["memory_watermark"]["snapshot_xmin"] += 1000
        body = {k: v for k, v in forged.items() if k != "mac"}
        forged["mac"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()   # attacker: no key
        v = S.verify(conn, ks, es, forged)
    finally:
        S._mac = real
    return (not v["ok"]) and "mac_invalid" in v["problems"], v


def case_t(arm):
    conn, ks, es, t, s = _setup("r3t")
    sd = S.seal(conn, ks, es, t)
    _drop_tail(db.admin(), t, "s1", RNG.randint(1, 4))
    if arm == "fenced":
        v = S.verify(conn, ks, es, sd)
        return (not v["ok"]) and any(p.startswith("head_missing") for p in v["problems"]), v
    body = {k: x for k, x in sd.items() if k != "mac"}                  # control: the seal checked only against itself
    ok = S._mac(ks, t, body) == sd["mac"] and hashlib.sha256(json.dumps(sd["manifest"]["heads"], separators=(",", ":")).encode()).hexdigest() == sd["manifest_hash"]
    return (not ok), {"self_consistent": ok}


CASES = {"P_prefix": case_p, "E_edited_seal": case_e, "T_truncated_log": case_t}


def main(R):
    res = {"R": R, "cases": {}}
    for c, fn in CASES.items():
        arms = {}
        for arm in (("fenced",) if c == "P_prefix" else ("fenced", "control")):
            runs = []
            for _ in range(R):
                try:
                    h, w = fn(arm)
                    runs.append({"holds": h, "witness": w})
                except Exception as e:  # noqa: BLE001
                    runs.append({"holds": None, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            arms[arm] = {"holds": sum(r["holds"] is True for r in runs), "violated": sum(r["holds"] is False for r in runs),
                         "errors": sum(r["holds"] is None for r in runs), "runs": runs}
        res["cases"][c] = arms
    res["pass"] = all(v["fenced"]["holds"] == R and ("control" not in v or v["control"]["violated"] == R) for v in res["cases"].values())
    print("row3", "PASS" if res["pass"] else "FAIL", json.dumps({c: {a: (v[a]["holds"], v[a]["violated"], v[a]["errors"]) for a in v} for c, v in res["cases"].items()}))
    for c, v in res["cases"].items():
        print(" ", c, json.dumps({a: v[a]["runs"][0] for a in v}, default=str)[:600])
    write_result("row3_seal", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)

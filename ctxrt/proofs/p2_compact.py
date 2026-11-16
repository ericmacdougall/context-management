"""P2: compaction and resume. Run: python3 -m ctxrt.proofs.p2_compact"""
from __future__ import annotations

import random

from ctxrt import session as S
from ctxrt.kernel import MemoryLog
from ctxrt.proofs.common import check, main

SESSIONS = 300
PLANTS = 300
MODES = ["copy", "paraphrase", "stale", "drop"]
LAYERS = ["own", "merged", "paraphrase"]
INGEST_MODES = ["full", "none", "origin_only", "strip_only"]


def base_log(rng, marker=None):
    log = MemoryLog()
    for i in range(rng.randint(4, 12)):
        log.append("other", "2026-03-%02dT00:00:00Z" % (1 + i), "user", "the office %s is in %s" % (rng.choice(["lease", "address", "floor"]), rng.choice(["Dublin", "Oslo", "Lisbon"])) if rng.random() < 0.6 else "unrelated note %d" % i)
    if marker:
        log.append("other", "2026-04-01T00:00:00Z", "user", "%s the office is in Lisbon (office office)" % marker)
    return log


def a_session(rng, policy):
    """Returns per-turn checks for one random session; policy selects the real path or a control path."""
    log = base_log(rng)
    h = S.Host(log, "office", layer=rng.choice(["own", "merged"]), block_policy=policy)
    summ = S.Summariser(rng.choice(MODES))
    T = rng.randint(2, 8)
    cp_at, comp_at, res_at = rng.randrange(T), rng.randrange(T), rng.randrange(T)
    out = []
    for t in range(T):
        if rng.random() < 0.4:
            log.append("other", "2026-05-%02dT00:00:00Z" % (1 + t), "user", "the office moved to %s" % rng.choice(["Cork", "Galway", "Bergen"]))
        rec = h.turn("question %d about the office" % t)
        seal = h.seal
        ids_now = [e["oid"] for e in S.pick(log, h.seal_query, 0, seal.watermark, 5)]
        in_ids = [log.by_oid(i) for i in ids_now]
        exp_delta = {e["oid"] for e in S.pick(log, h.seal_query, seal.watermark, log.watermark(), 50)}
        got_delta = set(i for i in __import__("re").findall(r'"id":"(m_[0-9a-f]+)"', rec["delta_text"]))
        got_sealed = set(__import__("re").findall(r'"id":"(m_[0-9a-f]+)"', rec["sealed_text"]))
        out.append({"op": "turn", "sealed_eq_regen": rec["sealed_equals_regen"], "sha_ok": rec["sealed_sha_ok"], "delta_exact": got_delta == exp_delta,
                    "future_leak": any(log.by_oid(i)["seq"] > seal.watermark for i in got_sealed), "stale_value": rec["sealed_text"] != rec["regen_text"]})
        if t == cp_at:
            h.checkpoint()
        if t == comp_at:
            if rng.random() < 0.8:
                log.append("other", "2026-06-%02dT00:00:00Z" % (1 + t), "user", "the office moved to %s" % rng.choice(["Cork", "Galway", "Bergen"]))
            h.compact(summ, "ZX-0000 the office is in Lisbon")
            out.append({"op": "compact", "mode": summ.mode})
        if t == res_at and h.checkpoints:
            h.resume(0)
            out.append({"op": "resume"})
    return out, summ.mode


def plant_session(rng, layer):
    marker = "ZX-%06x" % rng.getrandbits(24)
    log = base_log(rng, marker)
    orig_seq = log.watermark()
    host_layer = "merged" if layer == "merged" else "own"
    h = S.Host(log, "office", layer=host_layer)
    typed_canary = "QX-%06x" % rng.getrandbits(24)
    T = rng.randint(2, 6)
    for t in range(T):
        h.turn("tell me about the office; my codename is %s" % typed_canary if t == 0 else "question %d about the office" % t)
    if layer == "paraphrase":
        h.compact(S.Summariser("paraphrase"), "%s the office is in Lisbon" % marker)
    res = {"_in_block": any(marker in e["text"] for e in h.history if e["origin"] == "injected" or "<<<MEMCORE" in e["text"])}
    seen = set()
    entries = list(h.history)
    for mode in INGEST_MODES:
        lg = MemoryLog()
        lg.events = [dict(e) for e in log.events]
        lg._by_oid = dict(log._by_oid)
        S.ingest([S.Entry(e) for e in entries], lg, h.ledger, mode, set())
        new_marker = [e for e in lg.events if marker in e["text"] and e["seq"] != orig_seq]
        canary = [e for e in lg.events if typed_canary in e["text"] and e["meta"].get("entry")]
        res[mode] = {"new_marker": len(new_marker), "canary_ingested": len(canary)}
    return res


def run_seed(seed, params):
    rng = random.Random(seed)
    rows, m = [], {}
    for policy in ("regenerate", "from_summary", "skip_regeneration"):
        c = dict(sessions=0, turns=0, resumes=0, compactions=0, sealed_eq=0, sha_ok=0, delta_exact=0, future_leak=0, stale=0, by_mode={})
        for _ in range(SESSIONS):
            ops, mode = a_session(rng, policy)
            c["sessions"] += 1
            c["by_mode"].setdefault(mode, [0, 0])
            for o in ops:
                if o["op"] == "turn":
                    c["turns"] += 1
                    c["sealed_eq"] += o["sealed_eq_regen"]; c["sha_ok"] += o["sha_ok"]; c["delta_exact"] += o["delta_exact"]; c["future_leak"] += o["future_leak"]
                    c["stale"] += o["stale_value"]
                    c["by_mode"][mode][0] += 1; c["by_mode"][mode][1] += o["stale_value"]
                elif o["op"] == "resume":
                    c["resumes"] += 1
                else:
                    c["compactions"] += 1
            rows.append({"policy": policy, "mode": mode, "ops": ops})
        m[policy] = c
    pl = {}
    for layer in LAYERS:
        agg = {md: {"plants": 0, "new_marker": 0, "canary": 0} for md in INGEST_MODES}
        for _ in range(PLANTS):
            r = plant_session(rng, layer)
            agg["full"]["in_block"] = agg["full"].get("in_block", 0) + r.pop("_in_block")
            for md in INGEST_MODES:
                agg[md]["plants"] += 1
                agg[md]["new_marker"] += r[md]["new_marker"] > 0
                agg[md]["canary"] += r[md]["canary_ingested"] == 1
            rows.append({"layer": layer, "res": r})
        pl[layer] = agg
    m["plants"] = pl
    return rows, m


def evaluate(m, params):
    r, fs, sk = m["regenerate"], m["from_summary"], m["skip_regeneration"]
    pl = m["plants"]
    out = [
        check("(a) sealed block regenerated byte-identical at watermark, 100% of turns", [r["sealed_eq"], r["turns"]], r["turns"] > 0 and r["sealed_eq"] == r["turns"] and r["sha_ok"] == r["turns"]),
        check("(a) witness: resumes and compactions exercised", [r["resumes"], r["compactions"]], r["resumes"] >= 100 and r["compactions"] >= 100),
        check("(a) no record with seq>watermark in sealed part", r["future_leak"], r["future_leak"] == 0),
        check("(a) delta holds exactly records after watermark", [r["delta_exact"], r["turns"]], r["delta_exact"] == r["turns"]),
        check("control C2 block-from-summary diverges >=10% of turns", [fs["stale"], fs["turns"]], fs["stale"] / max(1, fs["turns"]) >= 0.10),
        check("control C3 skip-regeneration serves stale >=10% of turns", [sk["stale"], sk["turns"]], sk["stale"] / max(1, sk["turns"]) >= 0.10),
    ]
    for layer in LAYERS:
        a = pl[layer]
        out += [check("(b) witness %s: marker was present in an injected block" % layer, a["full"].get("in_block"), a["full"].get("in_block") == a["full"]["plants"]),
                check("(b) %s: 0 marker re-ingested (full filter)" % layer, a["full"], a["full"]["plants"] >= PLANTS and a["full"]["new_marker"] == 0),
                check("(c) %s: user-typed canary ingested exactly once (full filter)" % layer, a["full"], a["full"]["canary"] == a["full"]["plants"]),
                check("control C1 %s: no-filter ingest re-ingests marker >=10%%" % layer, a["none"], a["none"]["new_marker"] / a["none"]["plants"] >= 0.10)]
    out += [check("ablation: origin allow-list alone fails the merged layer", pl["merged"]["origin_only"], pl["merged"]["origin_only"]["new_marker"] / PLANTS >= 0.10),
            check("ablation: delimiter strip alone fails the paraphrase layer", pl["paraphrase"]["strip_only"], pl["paraphrase"]["strip_only"]["new_marker"] / PLANTS >= 0.10)]
    return out


if __name__ == "__main__":
    main("p2_compact", run_seed, evaluate, {"sessions": SESSIONS, "plants_per_layer": PLANTS, "stub_seal": True}, claim="compaction/resume regenerates at sealed watermark; injected block never re-ingested")

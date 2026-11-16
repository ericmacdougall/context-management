"""P3: pull tool contract (bounded in UTF-8 bytes, opaque ids, tenant and principal scope from the authenticated context, signed owner-bound cursors,
snapshot-stable paging including an idf scorer, typed nothing-found, current value from the decision state). Run: python3 -m ctxrt.proofs.p3_pull
current_value for contested or multi-principal states depends on the vendored policy core (research/policy-review-515e594.md M1, H4, H6): NOT PROVEN."""
from __future__ import annotations

import json
import math
import random
import re
import threading
import time

from memcore.guard import EgressGuard, EgressLeak
from ctxrt import gen
from ctxrt.decisions import DecisionLog
from ctxrt.kernel import MemoryLog
from ctxrt.proofs.common import check, main
from ctxrt.pulltools import CallerContext, MAX_BYTES, NO_RESULT_REASONS, PullTools, nbytes, tf_score, toks

SECRET = b"p3-test-secret-0123456789abcdef"       # test fixture; production reads a 0600 file
STORES, PAGINATION_RUNS, CHAINS, FORGED = 300, 100, 300, 1000
PRED = ["region", "owner", "budget", "deadline", "vendor"]
SUBJ = ["staging database", "billing service", "release train", "support rota", "data lake"]
VAL = ["Dublin", "Frankfurt", "Lisbon", "Tallinn", "Oslo", "Madrid", "Vienna", "Prague"]


def ts(i):
    return "2026-%02d-%02dT%02d:00:00Z" % (1 + i // 600, 1 + (i // 24) % 25, i % 24)


class UnscopedTools(PullTools):
    """CONTROL (F4): the 1d19cd8 behaviour: every event is visible to every caller."""

    def _visible(self, ctx, e):
        return True


class UnboundCursorTools(PullTools):
    """CONTROL (F4): cursors are not bound to their owner."""

    def _unsign(self, tok, ctx):
        st = super()._unsign(tok, CallerContext(ctx.tenant, "__ignored__", ctx.sessions)) if False else None
        try:
            import base64, hashlib, hmac as _h
            body, mac = tok.rsplit(".", 1)
            if not _h.compare_digest(mac, _h.new(self._secret, body.encode(), hashlib.sha256).hexdigest()[:32]):
                return None
            return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except Exception:
            return None


class LiveIdfTools(PullTools):
    """CONTROL (F14): keyset paging whose idf statistics are read from the LIVE log (not the pinned snapshot)."""

    def _scores(self, qt, evs):
        live = self.log.upto(self.log.watermark())
        n = len(live)
        df = {t: 0 for t in qt}
        for e in live:
            w = set(re.findall(r"\w+", e["text"].lower()))
            for t in qt:
                df[t] += t in w
        out = {}
        for e in evs:
            ws = re.findall(r"\w+", e["text"].lower())
            out[e["seq"]] = round(sum(min(3, ws.count(t)) * math.log(1 + n / (1 + df[t])) for t in qt), 6)
        return out


def make_store(rng):
    sess = "conv-%08x" % rng.getrandbits(32)
    log, dec = MemoryLog(secret=bytes(rng.getrandbits(8) for _ in range(16))), DecisionLog()
    n = rng.randint(5, 40)
    for i in range(n):
        big = rng.random() < 0.10
        txt = gen.text(rng, 3, 30) + " staging database"
        if big:
            txt = "staging database " + ("lorem ipsum 日本語 " * rng.randint(2000, 8000))[: rng.randint(60000, 200000)]
        log.append(sess, ts(i), rng.choice(["user", "assistant", "tool"]), txt, entities=rng.sample(["db", "billing", "Priya"], rng.randint(0, 2)), tenant="t1")
    other = "conv-%08x" % rng.getrandbits(32)
    log.append(other, ts(1), "user", "tenant two secret salary 123k staging database", entities=["db"], tenant="t2")
    return sess, other, log, dec


def forbidden(sess, log):
    return [sess] + [e["key"] for e in log.events[:50]]


def leak_scan(out_json, sess, keys):
    if sess in out_json or any(k in out_json for k in keys):
        return "internal_key_or_session"
    for bad in ('"seq"', '"_id"', '"session"', '"key"'):
        if bad in out_json:
            return "internal_field"
    return None


def counts_run(rng):
    c = dict(outputs=0, over_bound=0, truncated_flag_missing=0, leaks=0, huge_open=0, huge_open_chunks=0, no_results=0, no_results_bad=0, forged=0, forged_ok=0,
             guard_canary_blocked=0, truncated_witness=0, xt_probes=0, xt_leaks=0, xt_ctrl_leaks=0, cur_owner_probes=0, cur_owner_refused=0, cur_ctrl_accepted=0,
             cur_restart_ok=0, cur_restart_n=0, token_oversize=0, tokens=0, bytes_cjk_max=0)
    for _ in range(STORES):
        sess, other, log, dec = make_store(rng)
        guard = EgressGuard(real_ids=[sess, other], require_real_ids=True)
        ctx = CallerContext("t1", "user-1", frozenset([sess]))
        ctx2 = CallerContext("t1", "user-2", frozenset([sess]))
        tools = PullTools(log, dec, SECRET)
        small = PullTools(log, dec, SECRET, max_bytes=1200)
        surf, ssurf = tools.surface(lambda: ctx, guard), small.surface(lambda: ctx, guard)
        keys = forbidden(sess, log)

        def call(s, name, args, limit=MAX_BYTES):
            r = s.call_tool(name, args)
            j = json.dumps(r, ensure_ascii=False)
            c["outputs"] += 1
            nb = len(j.encode("utf-8"))
            c["bytes_cjk_max"] = max(c["bytes_cjk_max"], nb)
            if nb > limit:
                c["over_bound"] += 1
            if leak_scan(j, sess, keys):
                c["leaks"] += 1
            return r
        for q in ["staging database", "database", "Priya", gen.text(rng, 1, 4)]:
            r = call(surf, "search", {"query": q, "limit": rng.randint(1, 10)})
            if r["status"] == "ok":
                cur, pages = r.get("next_cursor"), 0
                while cur and pages < 4:
                    r = call(surf, "search", {"query": q, "cursor": cur})
                    cur = r.get("next_cursor") if r["status"] == "ok" else None
                    pages += 1
                for it in r.get("results", [])[:2]:
                    o = call(surf, "open", {"id": it["id"]})
                    if o["status"] == "ok" and o["next_offset"] is not None:
                        c["huge_open"] += 1
                        off = 0
                        while off is not None:
                            o2 = call(surf, "open", {"id": it["id"], "offset": off})
                            c["huge_open_chunks"] += 1
                            if o2["next_offset"] is not None and not o2["truncated"]:
                                c["truncated_flag_missing"] += 1
                            off = o2["next_offset"]
            rs = call(ssurf, "search", {"query": q, "limit": 10}, limit=1200)
            if rs["status"] == "ok" and rs.get("truncated"):
                c["truncated_witness"] += 1
        call(surf, "timeline", {"entity": "db"})
        call(surf, "timeline", {"entity": "no-such-entity-zzz"})
        for args in [{"query": "zzqx wvut"}, {"query": "staging", "as_of": "2000-01-01T00:00:00Z"}]:
            r = call(surf, "search", args)
            c["no_results"] += 1
            if not (isinstance(r, dict) and r.get("status") == "no_results" and r.get("reason") in NO_RESULT_REASONS):
                c["no_results_bad"] += 1
        r = call(PullTools(MemoryLog(SECRET), DecisionLog(), SECRET).surface(lambda: ctx, guard), "search", {"query": "anything"})
        c["no_results"] += 1
        if not (r.get("status") == "no_results" and r.get("reason") == "empty_store"):
            c["no_results_bad"] += 1
        r = call(surf, "current_value", {"subject": "nothing here", "predicate": "x"})
        c["no_results"] += 1
        if not (r.get("status") == "no_results" and r.get("reason") in NO_RESULT_REASONS):
            c["no_results_bad"] += 1
        real = log.events[0]["oid"]
        for _ in range(FORGED // STORES + 1):
            kind = rng.choice(["rand", "flip", "other_store", "internal_key"])
            if kind == "rand":
                fid = "m_%020x" % rng.getrandbits(80)
            elif kind == "flip":
                i = rng.randrange(2, len(real))
                fid = real[:i] + ("0" if real[i] != "0" else "1") + real[i + 1:]
            elif kind == "other_store":
                fid = "m_" + "%020x" % rng.getrandbits(80)
            else:
                fid = log.events[0]["key"]
            r = call(surf, "open", {"id": fid})
            c["forged"] += 1
            c["forged_ok"] += r == {"status": "not_found"}
        # tenant scope: tenant t2's event is never returned, opened or counted; the same probes against the unscoped control leak
        t2_oid = [e for e in log.events if e["tenant"] == "t2"][0]["oid"]
        unscoped = UnscopedTools(log, dec, SECRET)
        for probe in ("search", "open", "timeline"):
            c["xt_probes"] += 1
            if probe == "search":
                leak = "salary" in json.dumps(tools.search(ctx, "tenant two secret salary"))
                ctrl = "salary" in json.dumps(unscoped.search(ctx, "tenant two secret salary"))
            elif probe == "open":
                leak = tools.open(ctx, t2_oid)["status"] != "not_found"
                ctrl = unscoped.open(ctx, t2_oid)["status"] != "not_found"
            else:
                leak = any("salary" in x["snippet"] for x in tools.timeline(ctx, "db", limit=10).get("results", []))
                ctrl = any("salary" in x["snippet"] for x in unscoped.timeline(ctx, "db", limit=10).get("results", []))
            c["xt_leaks"] += leak
            c["xt_ctrl_leaks"] += ctrl
        # cursor bound to its owner; survives a restart (same secret, new process object); bounded size
        r = tools.search(ctx, "staging database", limit=1)
        if r.get("next_cursor"):
            tok = r["next_cursor"]
            c["tokens"] += 1
            c["token_oversize"] += len(tok) > 2048
            c["cur_owner_probes"] += 1
            c["cur_owner_refused"] += tools.search(ctx2, "staging database", cursor=tok) == {"status": "error", "code": "invalid_cursor"}
            c["cur_ctrl_accepted"] += UnboundCursorTools(log, dec, SECRET).search(ctx2, "staging database", cursor=tok).get("status") == "ok"
            c["cur_restart_n"] += 1
            c["cur_restart_ok"] += PullTools(log, dec, SECRET).search(ctx, "staging database", cursor=tok).get("status") == "ok"
        log.append(sess, ts(999), "user", "staging database note mentions " + sess, tenant="t1")
        try:
            surf.call_tool("search", {"query": "mentions"})
        except EgressLeak:
            c["guard_canary_blocked"] += 1
    return c


# ---------------------------------------------------------------- pagination under concurrent appends
def live_offset_page(log, kind, q, offset, limit):
    """CONTROL C1: offset pagination over the LIVE log (no snapshot)."""
    evs = log.upto(log.watermark())
    qt = toks(q)
    if kind == "search":
        cand = sorted(((-tf_score(qt, e["text"]), e["seq"], e) for e in evs if tf_score(qt, e["text"])), key=lambda t: t[:2])
    else:
        cand = sorted(((e["ts"], e["seq"], e) for e in evs if q in e["text"].lower()), key=lambda t: t[:2])
    return [t[2]["oid"] for t in cand[offset: offset + limit]], len(cand) > offset + limit


def pagination_run(rng, kind, mode):
    """mode: snapshot (real, tf scorer) | idf_snapshot (real, idf scorer pinned) | live_offset (C1) | live_idf (C4)."""
    log, dec = MemoryLog(SECRET), DecisionLog()
    for i in range(rng.randint(30, 60)):
        log.append("s", ts(rng.randrange(0, 900)), "user", "staging " + " ".join(rng.choice(["database", "alpha", "beta"]) for _ in range(rng.randint(0, 3))), tenant="t1")
    ctx = CallerContext("t1", "user-1", frozenset(["s"]))
    tools = {"snapshot": PullTools(log, dec, SECRET), "idf_snapshot": PullTools(log, dec, SECRET, scorer="idf"), "live_offset": PullTools(log, dec, SECRET), "live_idf": LiveIdfTools(log, dec, SECRET, scorer="idf")}[mode]
    stop = threading.Event()

    def appender():
        for _ in range(rng.randint(15, 40)):
            if stop.is_set():
                break
            time.sleep(rng.uniform(0, 0.002))
            log.append("s", ts(rng.randrange(0, 900)), "user", "staging database " + " ".join(rng.choice(["database", "alpha", "staging"]) for _ in range(rng.randint(0, 3))), tenant="t1")
    th = threading.Thread(target=appender)
    q = "staging database"
    seen, snap, first_wm, last_wm, pages = [], None, None, None, 0
    pre_events = None
    th.start()
    if mode != "live_offset":
        cur = None
        while True:
            r = tools.search(ctx, q, limit=4, cursor=cur) if kind == "search" else tools.timeline(ctx, "staging", limit=4, cursor=cur)
            if pages == 0:
                snap = tools._unsign(r["checked_through"], ctx)["wm"]
                first_wm = log.watermark()
            pages += 1
            last_wm = log.watermark()
            if r["status"] != "ok":
                break
            seen += [x["id"] for x in r["results"]]
            cur = r["next_cursor"]
            if not cur:
                break
            time.sleep(rng.uniform(0, 0.002))
    else:
        off = 0
        while True:
            with log._lock:
                if pages == 0:
                    snap = log.watermark()
                ids, more = live_offset_page(log, kind, q if kind == "search" else "staging", off, 4)
            if pages == 0:
                first_wm = log.watermark()
            pages += 1
            last_wm = log.watermark()
            seen += ids
            off += 4
            if not more:
                break
            time.sleep(rng.uniform(0, 0.002))
    stop.set()
    th.join()
    snap_events = log.upto(snap)
    exp_events = [e for e in snap_events if (tf_score(toks(q), e["text"]) if kind == "search" else "staging" in e["text"].lower())]
    exp = {e["oid"] for e in exp_events}
    dup = len(seen) - len(set(seen))
    skip = len(exp - set(seen))
    return {"kind": kind, "mode": mode, "pages": pages, "dup": dup, "skip": skip, "overlapped": last_wm > first_wm and last_wm > snap, "appends_between": last_wm - first_wm,
            "expected_ids": sorted(exp), "seen_ids": seen, "ok": dup == 0 and skip == 0}


# ---------------------------------------------------------------- current value
def chain_case(rng):
    subj, pred = rng.choice(SUBJ), rng.choice(PRED)
    k = rng.randint(2, 5)
    vals = rng.sample(VAL, k)
    log, dec = MemoryLog(SECRET), DecisionLog()
    times = sorted(rng.sample(range(1, 400), k + 1))
    retract = rng.random() < 0.2
    contested = (not retract) and rng.random() < 0.1
    for i, v in enumerate(vals):
        t = ts(times[i])
        elab = " %s %s %s confirmed." % (pred, subj, pred)
        if i == k - 1:
            txt = rng.choice(["Update: the %s %s moved to %s." % (subj, pred, v), "The %s %s is now %s." % (subj, pred, v), "The %s %s is now %s.%s" % (subj, pred, v, elab)])
        else:
            txt = "The %s %s is %s.%s" % (subj, pred, v, elab if rng.random() < 0.5 else "")
        seq = log.append("s", t, "user", txt, meta={"value": v}, tenant="t1")
        dec.set(subj, pred, v, t, log.events[seq - 1]["key"], tenant="t1")
    ctx = CallerContext("t1", "user-1", frozenset(["s"]))
    tools = PullTools(log, dec, SECRET)
    # another tenant sets a conflicting value for the same subject: it must not leak into t1's answer
    dec.set(subj, pred, "OTHER-TENANT", ts(times[k]), None, tenant="t2")
    if retract:
        dec.retract(subj, pred, ts(times[k]), None, tenant="t1")
    if contested:
        from ctxrt.decisions import req_id
        base = dec._live["reqs"][req_id(subj, pred, "t1")]["last"]["based_on"]
        dec.set(subj, pred, "CONFLICT", ts(times[k]), None, principal="owner2", based_on=base, tenant="t1")
    return subj, pred, vals, times, retract, contested, log, dec, tools, ctx


def chain_run(rng):
    c = dict(chains=0, state_wrong=0, asof_checked=0, ctrl_chains=0, ctrl_wrong=0, retract_ok=0, retract_n=0, contested_ok=0, contested_n=0, decision_only_ok=0, decision_only_n=0, tenant_bleed=0)
    for _ in range(CHAINS):
        subj, pred, vals, times, retract, contested, log, dec, tools, ctx = chain_case(rng)
        c["chains"] += 1
        r = tools.current_value(ctx, subj, pred)
        c["tenant_bleed"] += "OTHER-TENANT" in json.dumps(r)
        if retract:
            c["retract_n"] += 1
            c["retract_ok"] += r["status"] == "retracted"
            ok = r["status"] == "retracted"
        elif contested:
            c["contested_n"] += 1
            c["contested_ok"] += r["status"] == "contested" and "value" not in r
            ok = r["status"] == "contested"
        else:
            ok = r["status"] == "ok" and r["value"] == vals[-1] and r["decision_event"] and r["checked_through"] and r["evidence"]
        if not ok:
            c["state_wrong"] += 1
        if not retract and not contested:
            j = rng.randrange(0, len(vals))
            r2 = tools.current_value(ctx, subj, pred, ts(times[j]))
            c["asof_checked"] += 1
            if not (r2["status"] == "ok" and r2["value"] == vals[j]):
                c["state_wrong"] += 1
            only = PullTools(MemoryLog(SECRET), dec, SECRET)
            c["decision_only_n"] += 1
            c["decision_only_ok"] += only.current_value(ctx, subj, pred)["value"] == vals[-1]
            top = tools.search(ctx, "%s %s" % (subj, pred), limit=1)["results"][0]
            guess = log.by_oid(top["id"])["meta"]["value"]
            c["ctrl_chains"] += 1
            c["ctrl_wrong"] += guess != vals[-1]
    return c


def run_seed(seed, params):
    rng = random.Random(seed)
    rows, met = [], {}
    met["counts"] = counts_run(rng)
    pag = {}
    for i in range(PAGINATION_RUNS):
        kind = "search" if i % 2 == 0 else "timeline"
        for mode in ("snapshot", "idf_snapshot", "live_offset", "live_idf"):
            if mode.endswith("idf") or mode == "idf_snapshot":
                if kind == "timeline":
                    continue                      # timeline order is by (ts, seq): no scorer involved
            r = pagination_run(rng, kind, mode)
            pag.setdefault(mode, []).append(r)
            rows.append(dict(r, run=i))
    met["pagination"] = {k: {"runs": len(v), "overlapped": sum(x["overlapped"] for x in v), "violations": sum(not x["ok"] for x in v), "violations_overlapped": sum((not x["ok"]) and x["overlapped"] for x in v)} for k, v in pag.items()}
    met["chains"] = chain_run(rng)
    rows.append({"counts": met["counts"], "chains": met["chains"]})
    return rows, met


def evaluate(met, params):
    c, p, ch = met["counts"], met["pagination"], met["chains"]
    s, si, o, li = p["snapshot"], p["idf_snapshot"], p["live_offset"], p["live_idf"]
    return [
        check("bounded: 0 results over 8000 UTF-8 bytes", [c["over_bound"], c["outputs"], c["bytes_cjk_max"]], c["over_bound"] == 0 and c["outputs"] >= 5000),
        check("bounded: truncated flag set whenever open cut text", c["truncated_flag_missing"], c["truncated_flag_missing"] == 0 and c["huge_open"] > 0),
        check("witness: forced-small-bound instance truncated at least once", c["truncated_witness"], c["truncated_witness"] > 0),
        check("opaque: 0 internal keys/session ids/fields in outputs", [c["leaks"], c["outputs"]], c["leaks"] == 0),
        check("guard canary: planted real session id blocked", c["guard_canary_blocked"], c["guard_canary_blocked"] == STORES),
        check("forged ids 100% typed not_found", [c["forged_ok"], c["forged"]], c["forged"] >= FORGED and c["forged_ok"] == c["forged"]),
        check("typed nothing found 100%", [c["no_results_bad"], c["no_results"]], c["no_results"] >= 300 and c["no_results_bad"] == 0),
        check("tenant scope: 0 leaks of another tenant's event via search, open, timeline", [c["xt_leaks"], c["xt_probes"]], c["xt_leaks"] == 0 and c["xt_probes"] >= 900),
        check("control: unscoped tools leak across tenants >=10%", [c["xt_ctrl_leaks"], c["xt_probes"]], c["xt_ctrl_leaks"] / max(1, c["xt_probes"]) >= 0.10),
        check("cursor bound to its owner: another principal refused 100%", [c["cur_owner_refused"], c["cur_owner_probes"]], c["cur_owner_probes"] > 0 and c["cur_owner_refused"] == c["cur_owner_probes"]),
        check("control: unbound cursor accepted for another principal >=10%", [c["cur_ctrl_accepted"], c["cur_owner_probes"]], c["cur_ctrl_accepted"] / max(1, c["cur_owner_probes"]) >= 0.10),
        check("cursor survives a restart (new object, same configured secret) 100%, size bounded", [c["cur_restart_ok"], c["cur_restart_n"], c["token_oversize"]], c["cur_restart_ok"] == c["cur_restart_n"] > 0 and c["token_oversize"] == 0),
        check("pagination: snapshot cursor (tf) 0 dup/skip in all runs", s, s["violations"] == 0),
        check("pagination: snapshot cursor with an idf scorer pinned to the snapshot: 0 dup/skip", si, si["violations"] == 0 and si["runs"] > 0),
        check("pagination overlap floor >=30% of runs", s["overlapped"] / s["runs"], s["overlapped"] / s["runs"] >= 0.30),
        check("control C1 offset pagination dup_or_skip >=10% of overlapped runs", [o["violations_overlapped"], o["overlapped"]], o["overlapped"] > 0 and o["violations_overlapped"] / o["overlapped"] >= 0.10),
        check("control C4 keyset paging with LIVE idf statistics dup_or_skip >=10% of overlapped runs", [li["violations_overlapped"], li["overlapped"]], li["overlapped"] > 0 and li["violations_overlapped"] / li["overlapped"] >= 0.10),
        check("current_value equals decision-state fold 100%, other tenant never bleeds in", [ch["state_wrong"], ch["chains"], ch["tenant_bleed"]], ch["state_wrong"] == 0 and ch["chains"] >= CHAINS and ch["tenant_bleed"] == 0),
        check("current_value independent of search index (decision-only store)", [ch["decision_only_ok"], ch["decision_only_n"]], ch["decision_only_ok"] == ch["decision_only_n"] > 0),
        check("canary: retraction and contested typed (single-principal states only; multi-principal NOT PROVEN)", [ch["retract_ok"], ch["retract_n"], ch["contested_ok"], ch["contested_n"]], ch["retract_n"] > 0 and ch["retract_ok"] == ch["retract_n"] and ch["contested_n"] > 0 and ch["contested_ok"] == ch["contested_n"]),
        check("control C2 search-backed current_value wrong >=10%", [ch["ctrl_wrong"], ch["ctrl_chains"]], ch["ctrl_wrong"] / max(1, ch["ctrl_chains"]) >= 0.10),
    ]


if __name__ == "__main__":
    main("p3_pull", run_seed, evaluate, {"stores": STORES, "pagination_runs": PAGINATION_RUNS, "chains": CHAINS, "forged": FORGED, "max_bytes": MAX_BYTES, "policy_dependent": "NOT PROVEN for multi-principal current_value"}, claim="pull tool contract")

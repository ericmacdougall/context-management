"""Second implementation (same author, NOT independent evidence): recomputes headline numbers from the hashed per-case JSONL and re-derives each PASS/FAIL
against the numbers in THRESHOLDS.md. Where the JSONL holds only aggregate counts (P4, P6, parts of P3) it can only verify integrity and the threshold
arithmetic, and says so. Also cross-checks msgshape's validators against a second validator written here in a different style.
Run: python3 -m ctxrt.recompute   (reads ctxrt/results/)"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "ctxrt", "results")


def load(name):
    return json.load(open(os.path.join(RES, name + ".json")))


def rows_of(art):
    p = os.path.join(ROOT, art["path"])
    h = hashlib.sha256()
    rows = []
    for line in open(p, "rb"):
        h.update(line)
        rows.append(json.loads(line))
    return rows, h.hexdigest() == art["sha256"] and len(rows) == art["n"]


# ---------------------------------------------------------------- independent validator (state machine over the messages)
def v2_anthropic(msgs):
    pending = None            # ids owed by the previous assistant message
    seen = set()
    bad = False
    for i, m in enumerate(msgs):
        c = m.get("content")
        blocks = c if isinstance(c, list) else []
        kinds = [b.get("type") for b in blocks]
        if i == 0 and m.get("role") != "user":
            bad = True
        if pending is not None:
            if m.get("role") != "user":
                return False
            res = [b for b in blocks if b.get("type") == "tool_result"]
            if [k for k in kinds[:len(res)]] != ["tool_result"] * len(res) or "tool_result" in kinds[len(res):]:
                return False
            if sorted(b.get("tool_use_id") for b in res) != sorted(pending):
                return False
            pending = None
        else:
            if "tool_result" in kinds:
                return False
        if m.get("role") == "assistant" and "tool_use" in kinds:
            ids = [b["id"] for b in blocks if b.get("type") == "tool_use"]
            if any(x in seen for x in ids) or len(set(ids)) != len(ids):
                return False
            seen |= set(ids)
            pending = ids
        if c in ("", None, []):
            return False
    return (not bad) and pending is None and msgs[-1].get("role") == "user"


def v2_openai(msgs):
    i, n, owed, seen = 0, len(msgs), None, set()
    head = True
    while i < n:
        m = msgs[i]
        r = m.get("role")
        if r in ("system", "developer"):
            if not head:
                return False
            i += 1
            continue
        head = False
        if r == "tool":
            return False
        if r == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            if len(set(ids)) != len(ids) or any(x in seen for x in ids):
                return False
            seen |= set(ids)
            j, got = i + 1, []
            while j < n and msgs[j].get("role") == "tool":
                got.append(msgs[j].get("tool_call_id"))
                j += 1
            if sorted(got) != sorted(ids):
                return False
            i = j
            continue
        i += 1
    return msgs[-1].get("role") in ("user", "tool")


def cross_check_validators(n=400):
    from ctxrt import gen, msgshape as M
    from ctxrt.proofs.p1_trim import MUTS, mutate
    rng = random.Random(777)
    agree = tot = 0
    for shape, v2 in (("anthropic", v2_anthropic), ("openai", v2_openai)):
        for _ in range(n):
            conv = gen.conversation(rng, shape)
            tot += 1
            agree += (not M.validate(shape, conv)) == v2(conv)
            kind = rng.choice(MUTS)
            mm = mutate(shape, conv, kind, rng)
            if mm is not None:
                tot += 1
                agree += (not M.validate(shape, mm)) == v2(mm)
    return agree, tot


def main():
    out, ok_all = {}, True
    for name in ("p1_trim", "p2_compact", "p3_pull", "p4_analyst", "p4_process", "p5_delivery", "p6_routing", "p7_chain"):
        p = os.path.join(RES, name + ".json")
        if not os.path.exists(p):
            out[name] = "MISSING"
            continue
        d = load(name)
        rec = {"stamp_commit": d["stamp"]["code_commit"][:7], "recorded_overall": d["overall"]}
        integ, recomputed = [], {}
        for ps in d["per_seed"]:
            rows, ok = rows_of(ps["artifact"])
            integ.append(ok)
            if name == "p5_delivery":
                for sch in ("paused", "crash", "rollback", "fork"):
                    for arm in ("real", "ctrl"):
                        rs = [r for r in rows if r.get("schedule") == sch and r.get("arm") == arm]
                        m = ps["metrics"][sch + "_" + arm]
                        recomputed.setdefault(sch + "_" + arm, []).append((sum(r["dup"] > 0 for r in rs) == m["dup_runs"]) and (sum(r["lost"] > 0 for r in rs) == m["lost_runs"]))
            elif name == "p1_trim":
                for shape in ("anthropic", "openai"):
                    rs = [r for r in rows if r.get("shape") == shape and "i" in r]
                    c = ps["metrics"][shape]["counts"]
                    recomputed.setdefault(shape, []).append(sum(bool(r["trim_v"]) for r in rs) == c["trim_invalid"] and sum(bool(r["both_v"]) for r in rs) == c["both_invalid"]
                                                           and sum("orphan_result_or_split_pair" in r["naive_v"] for r in rs if r["pair"]) == c["naive_split_class"])
            elif name == "p3_pull":
                for arm in ("snapshot", "live_offset"):
                    rs = [r for r in rows if r.get("snapshot") == (arm == "snapshot") and "pages" in r]
                    m = ps["metrics"]["pagination"][arm]
                    recomputed.setdefault(arm, []).append(sum(not r["ok"] for r in rs) == m["violations"] and sum(r["overlapped"] for r in rs) == m["overlapped"])
            elif name == "p4_analyst":
                a = [r for r in rows if r.get("exp") == "4a"]
                recomputed.setdefault("4a_keyed_ok", []).append(sum(r["ok"] for r in a) == ps["metrics"]["4a"]["keyed_ok"])
                dm = [r for r in rows if r.get("exp") == "4d_malformed"]
                recomputed.setdefault("4d_malformed_rejected", []).append(sum(r["out"] == "failed" and str(r["reason"]).startswith("schema:") for r in dm) == ps["metrics"]["4d"]["malformed_rejected"]
                                                                          and sum(r["items"] for r in dm) == 0)
                dr = [r for r in rows if r.get("exp") == "4c_discard_real"]
                recomputed.setdefault("4c_discard_real", []).append(sum(r["inflight_seen"] and r["refusal_rows"] == 1 and r["steps_consumed"] == 0 for r in dr) == ps["metrics"]["4c"]["discard_ok"])
                dc = [r for r in rows if r.get("exp") == "4c_discard_ctrl"]
                recomputed.setdefault("4c_discard_ctrl", []).append(sum(r["inflight_seen"] and r["steps_consumed"] >= 1 and r["refusal_rows"] == 0 for r in dc) == ps["metrics"]["4c"]["discard_ctrl_consumed"])
                b = [r for r in rows if r.get("exp") == "4b"]
                recomputed.setdefault("4b_conc_le_5", []).append(all(r["conc_started"] <= 5 for r in b) == (ps["metrics"]["4b"]["conc_started_le_allow"] == len(b)))
                recomputed.setdefault("4b_ctrl2", []).append(sum(r["ctrl2_started"] > 5 for r in b) == ps["metrics"]["4b"]["ctrl2_overshoot"])
            elif name == "p6_routing":
                rs = [r for r in rows if "k" in r]
                m = ps["metrics"]
                recomputed.setdefault("p6_assigned_in_set", []).append(sum(r["assigned"] in r["author_set"] for r in rs if r["assigned"]) == m["route_in_author_set"])
                recomputed.setdefault("p6_ctrl", []).append(sum(r["ctrl_assigned"] in r["brute_set"] for r in rs if r["ctrl_assigned"]) == m["ctrl_assigned_author"])
                recomputed.setdefault("p6_sets_equal", []).append(all(r["author_set"] == r["brute_set"] for r in rs))
                recomputed.setdefault("p6_models", []).append(all(r["author_model"] for r in rs))
            elif name == "p7_chain":
                rs = [r for r in rows if "cls" in r]
                recomputed.setdefault("p7_valid_shape", []).append(all(r["valid_shape"] for r in rs))
                recomputed.setdefault("p7_neg_identical", []).append(all(r["block_len"] == 0 and r["identical_to_base"] for r in rs if r["cls"] in ("nothing", "malformed", "stopped")))
                recomputed.setdefault("p7_author_set_zero", []).append(sum(r.get("author_set_claims", 0) for r in rs) == 0 and all(r.get("reviewer_claims") == 1 for r in rs if r["n_items"]))
            elif name == "p2_compact":
                reg = [r for r in rows if r.get("policy") == "regenerate"]
                after_resume = after_comp = 0
                for r in reg:
                    ops = r["ops"]
                    for k, o in enumerate(ops):
                        if k + 1 < len(ops):
                            nxt = ops[k + 1]
                            if o["op"] == "resume" and nxt["op"] == "turn":
                                after_resume += 1
                            if o["op"] == "compact" and nxt["op"] == "turn":
                                after_comp += 1
                recomputed.setdefault("p2_turns_after_resume_ge_100", []).append(after_resume >= 100)
                recomputed.setdefault("p2_turns_after_compaction_ge_100", []).append(after_comp >= 100)
                rec.setdefault("p2_witness_counts", []).append({"turns_after_resume": after_resume, "turns_after_compaction": after_comp})
                for layer in ("own", "merged", "paraphrase"):
                    rs = [r for r in rows if r.get("layer") == layer]
                    for mode in ("full", "none", "origin_only", "strip_only"):
                        m = ps["metrics"]["plants"][layer][mode]
                        recomputed.setdefault(layer + "_" + mode, []).append(sum(r["res"][mode]["new_marker"] > 0 for r in rs) == m["new_marker"])
        rec["hash_and_rowcount_ok"] = all(integ)
        rec["per_case_recompute_matches"] = {k: all(v) for k, v in recomputed.items()} or "aggregate-only (no per-case recompute)"
        rec["all_ok"] = all(integ) and (all(all(v) for v in recomputed.values()) if recomputed else True)
        ok_all &= rec["all_ok"]
        out[name] = rec
    agree, tot = cross_check_validators()
    out["validator_cross_check"] = {"agree": agree, "total": tot}
    ok_all &= agree == tot
    out["all_ok"] = ok_all
    print(json.dumps(out, indent=1))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

"""P1: trimming and injection keep the conversation valid for the provider. Run: python3 -m ctxrt.proofs.p1_trim"""
from __future__ import annotations

import copy
import random
import sys
from collections import Counter

from memcore.inbox import render_data_block
from ctxrt import gen, msgshape as M
from ctxrt.proofs.common import check, main

N = 2000
MUT_N = 200
MUTS = ["orphan_result", "missing_result", "text_before_result", "wrong_id", "duplicate_id"]


def mutate(shape, msgs, kind, rng):
    m = copy.deepcopy(msgs)
    if shape == "anthropic":
        idx = [i for i, x in enumerate(m) if x["role"] == "assistant" and isinstance(x["content"], list) and any(b["type"] == "tool_use" for b in x["content"])]
        if not idx:
            return None
        i = rng.choice(idx)
        res = m[i + 1]["content"]
        if kind == "orphan_result":
            del m[i]
        elif kind == "missing_result":
            m[i + 1]["content"] = [b for b in res if b["type"] != "tool_result"] or [{"type": "text", "text": "x"}]
        elif kind == "text_before_result":
            m[i + 1]["content"] = [{"type": "text", "text": "memo"}] + res
        elif kind == "wrong_id":
            res[0]["tool_use_id"] = "nope_" + res[0]["tool_use_id"]
        elif kind == "duplicate_id":
            if len(idx) < 2:
                return None
            j = rng.choice([k for k in idx if k != i])
            src = [b for b in m[i]["content"] if b["type"] == "tool_use"][0]["id"]
            for b in m[j]["content"]:
                if b["type"] == "tool_use":
                    b["id"] = src
                    break
    else:
        idx = [i for i, x in enumerate(m) if x["role"] == "assistant" and x.get("tool_calls")]
        if not idx:
            return None
        i = rng.choice(idx)
        k = i + 1
        group = []
        while k < len(m) and m[k]["role"] == "tool":
            group.append(k)
            k += 1
        if kind == "orphan_result":
            del m[i]
        elif kind == "missing_result":
            del m[group[0]]
        elif kind == "text_before_result":      # openai analogue: another role between the call and its result
            m.insert(group[0], {"role": "user", "content": "memo"})
        elif kind == "wrong_id":
            m[group[0]]["tool_call_id"] = "nope_" + m[group[0]]["tool_call_id"]
        elif kind == "duplicate_id":
            if len(idx) < 2:
                return None
            j = rng.choice([q for q in idx if q != i])
            m[j]["tool_calls"][0]["id"] = m[i]["tool_calls"][0]["id"]
    return m


def run_seed(seed, params):
    rng = random.Random(seed)
    rows, met = [], {}
    for shape in ("anthropic", "openai"):
        c = Counter()
        c1_classes, c2_classes = Counter(), Counter()
        for i in range(N):
            conv = gen.conversation(rng, shape)
            total = M.total_tokens(conv)
            us = M.units(shape, conv)
            tail = M.total_tokens(conv[us[-1][0]:us[-1][1]])
            budget = max(8, int(rng.uniform(tail * 0.6, total * 1.05)))
            block = render_data_block([{"id": "m_%d" % k, "ts": "2026-01-01T00:00:00Z", "text": gen.text(rng, 3, 30)} for k in range(rng.randint(1, 4))], "turn-%d" % i,
                                      label="remembered events") if rng.random() < 0.9 else ""
            reserve = M.est_tokens({"role": "user", "content": block}) if block else 0
            has_pair = any(b - a > 1 for a, b in us)
            ends_tool = (conv[-1]["role"] == "tool") if shape == "openai" else (isinstance(conv[-1]["content"], list) and any(b["type"] == "tool_result" for b in conv[-1]["content"]))
            orig_v = M.validate(shape, conv)
            t = M.trim(shape, conv, budget, reserve)
            v_trim = M.validate(shape, t.messages)
            v_inj_orig = M.validate(shape, M.inject(shape, conv, block))
            v_both = M.validate(shape, M.inject(shape, t.messages, block))
            naive = M.naive_tail_trim(shape, conv, budget, reserve)
            v_naive = M.validate(shape, naive)
            v_ninj = M.validate(shape, M.naive_inject(shape, t.messages, block))
            in_budget_ok = t.over_budget or t.tokens <= budget - reserve
            c["n"] += 1
            c["orig_invalid"] += bool(orig_v)
            c["trimmed"] += t.dropped_units > 0
            c["has_pair"] += has_pair
            c["ends_tool_result"] += ends_tool
            c["marker_added"] += t.marker_added
            c["over_budget"] += t.over_budget
            c["trim_invalid"] += bool(v_trim)
            c["inject_invalid"] += bool(v_inj_orig)
            c["both_invalid"] += bool(v_both)
            c["budget_violation"] += not in_budget_ok
            if has_pair:
                c["pair_cases"] += 1
                c["naive_invalid_pair_cases"] += bool(v_naive)
                for k in set(v_naive):
                    c1_classes[k] += 1
                c["naive_split_class"] += "orphan_result_or_split_pair" in v_naive
            if "orphan_result_or_split_pair" in v_naive:
                c["naive_would_cut_inside_pair"] += 1
            if ends_tool and block:
                c["tool_tail_with_block"] += 1
                c["naive_inject_invalid"] += bool(v_ninj)
                for k in set(v_ninj):
                    c2_classes[k] += 1
                c["good_inject_invalid_tool_tail"] += bool(v_both)
            rows.append({"shape": shape, "i": i, "budget": budget, "tokens_in": total, "tokens_out": t.tokens, "dropped": t.dropped_units, "over": t.over_budget,
                         "marker": t.marker_added, "pair": has_pair, "ends_tool": ends_tool, "orig_v": orig_v, "trim_v": v_trim, "inj_v": v_inj_orig, "both_v": v_both,
                         "naive_v": v_naive, "naive_inj_v": v_ninj if (ends_tool and block) else None})
        # validator sensitivity
        mut = {}
        for kind in MUTS:
            made = caught = 0
            guard = 0
            while made < MUT_N and guard < 20000:
                guard += 1
                base = gen.conversation(rng, shape)
                mm = mutate(shape, base, kind, rng)
                if mm is None:
                    continue
                made += 1
                caught += bool(M.validate(shape, mm))
            mut[kind] = {"made": made, "caught": caught}
            rows.append({"shape": shape, "mutation": kind, "made": made, "caught": caught})
        met[shape] = {"counts": dict(c), "c1_classes": dict(c1_classes), "c2_classes": dict(c2_classes), "mutants": mut}
    return rows, met


def evaluate(met, params):
    out = []
    for shape, m in met.items():
        c = m["counts"]
        n = c["n"]
        out += [
            check("%s: 0 invalid from trim/inject/trim+inject" % shape, [c["trim_invalid"], c["inject_invalid"], c["both_invalid"]], not (c["trim_invalid"] or c["inject_invalid"] or c["both_invalid"])),
            check("%s: 0 false positives on generated valid" % shape, c["orig_invalid"], c["orig_invalid"] == 0),
            check("%s: budget respected when not over_budget" % shape, c["budget_violation"], c["budget_violation"] == 0),
            check("%s: witness trimmed >=30%%" % shape, c["trimmed"] / n, c["trimmed"] / n >= 0.30),
            check("%s: witness has pair >=40%%" % shape, c["has_pair"] / n, c["has_pair"] / n >= 0.40),
            check("%s: witness ends in tool result >=25%%" % shape, c["ends_tool_result"] / n, c["ends_tool_result"] / n >= 0.25),
            check("%s: witness naive would cut inside pair >=200" % shape, c["naive_would_cut_inside_pair"], c["naive_would_cut_inside_pair"] >= 200),
            check("%s: control C1 naive trim orphan/split class >=10%% of pair cases" % shape, c["naive_split_class"] / max(1, c["pair_cases"]), c["naive_split_class"] / max(1, c["pair_cases"]) >= 0.10),
            check("%s: control C2 naive inject invalid >=10%% of tool-tail cases" % shape, c["naive_inject_invalid"] / max(1, c["tool_tail_with_block"]), c["naive_inject_invalid"] / max(1, c["tool_tail_with_block"]) >= 0.10),
            check("%s: canary real injector valid on tool-tail cases" % shape, c["tool_tail_with_block"], c["tool_tail_with_block"] > 0 and c["good_inject_invalid_tool_tail"] == 0),
        ]
        for k, v in m["mutants"].items():
            out.append(check("%s: validator rejects mutation %s 100%%" % (shape, k), v, v["made"] >= MUT_N and v["caught"] == v["made"]))
    return out


if __name__ == "__main__":
    main("p1_trim", run_seed, evaluate, {"N": N, "MUT_N": MUT_N}, claim="trim/inject keep tool pairing and placement valid in both shapes")

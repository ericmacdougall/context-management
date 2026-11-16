"""P1: trimming and injection keep the conversation valid for the provider. Run: python3 -m ctxrt.proofs.p1_trim
Per seed, per shape: N random conversations (thinking blocks with signatures, redacted thinking, server-tool blocks, images and documents, tool errors, empty
and list-valued tool results, parallel calls, CJK) with budgets drawn from 0.3 x the live turn to 1.05 x the whole conversation; an unresolved-server-tool
tail case for the anthropic shape. RAW inputs and outputs of RAW_N cases per seed and shape are stored so that recompute.py can re-judge them without
the proof's own booleans."""
from __future__ import annotations

import copy
import random
from collections import Counter

from memcore.inbox import render_data_block
from ctxrt import gen, msgshape as M
from ctxrt.proofs.common import check, main

N = 2000
MUT_N = 200
RAW_N = 300
MUTS = ["orphan_result", "missing_result", "text_before_result", "wrong_id", "duplicate_id", "thinking_without_signature", "text_after_unresolved_server"]


def mutate(shape, msgs, kind, rng):
    m = copy.deepcopy(msgs)
    if shape == "anthropic":
        if kind == "thinking_without_signature":
            for x in m:
                for b in (x["content"] if isinstance(x["content"], list) else []):
                    if b.get("type") == "thinking":
                        b["signature"] = ""
                        return m
            return None
        if kind == "text_after_unresolved_server":
            u = gen.with_unresolved_server_tool(rng, m)
            u[-1]["content"].append({"type": "text", "text": "what next"})
            return u
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
        if kind in ("thinking_without_signature", "text_after_unresolved_server"):
            return None
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
        elif kind == "text_before_result":
            m.insert(group[0], {"role": "user", "content": "memo"})
        elif kind == "wrong_id":
            m[group[0]]["tool_call_id"] = "nope_" + m[group[0]]["tool_call_id"]
        elif kind == "duplicate_id":
            if len(idx) < 2:
                return None
            j = rng.choice([q for q in idx if q != i])
            m[j]["tool_calls"][0]["id"] = m[i]["tool_calls"][0]["id"]
    return m


def has_tool(shape, conv):
    return any((x.get("tool_calls") if shape == "openai" else (isinstance(x["content"], list) and any(b.get("type") == "tool_use" for b in x["content"]))) for x in conv)


def ends_in_tool_result(shape, conv):
    return (conv[-1]["role"] == "tool") if shape == "openai" else (isinstance(conv[-1]["content"], list) and bool(conv[-1]["content"]) and conv[-1]["content"][0].get("type") == "tool_result")


def run_seed(seed, params):
    rng = random.Random(seed)
    rows, met = [], {}
    for shape in ("anthropic", "openai"):
        c = Counter()
        c1, c2, c3 = Counter(), Counter(), Counter()
        raw_left = RAW_N
        for i in range(N):
            conv = gen.conversation(rng, shape)
            unresolved = shape == "anthropic" and i % 5 == 0
            if unresolved:
                conv = gen.with_unresolved_server_tool(rng, conv)
            total = M.total_tokens(conv)
            us = M.units(shape, conv)
            tail = M.total_tokens(conv[us[-1][0]:us[-1][1]])
            budget = max(8, int(rng.uniform(tail * 0.3, total * 1.05)))
            block = render_data_block([{"id": "m_%d" % k, "ts": "2026-01-01T00:00:00Z", "text": gen.text(rng, 3, 30)} for k in range(rng.randint(1, 4))], "turn-%d@2026-01-01T00:00:00Z" % i,
                                      label="remembered events") if rng.random() < 0.9 else ""
            reserve = M.est_tokens({"role": "user", "content": block}) if block else 0
            tool = has_tool(shape, conv)
            tail_tool = ends_in_tool_result(shape, conv)
            t = M.trim(shape, conv, budget, reserve)
            v_trim, v_inj_orig, v_both = M.validate(shape, t.messages), M.validate(shape, M.inject(shape, conv, block)), M.validate(shape, M.inject(shape, t.messages, block))
            naive = M.naive_tail_trim(shape, conv, budget, reserve)
            v_naive = M.validate(shape, naive)
            naive_inj = M.naive_inject(shape, t.messages, block)
            v_ninj = M.validate(shape, naive_inj)
            c3_inj = M.text_after_results_inject(shape, t.messages, block)
            v_c3 = M.validate(shape, c3_inj)
            final = M.inject(shape, t.messages, block)
            keeps, think_ok = M.keeps_task(shape, conv, t.messages), M.thinking_intact(conv, t.messages)
            naive_keeps = M.keeps_task(shape, conv, naive)
            c["n"] += 1
            c["orig_invalid"] += bool(M.validate(shape, conv))
            c["trimmed"] += t.dropped_units > 0
            c["has_tool"] += tool
            c["tail_tool_result"] += tail_tool
            c["unresolved"] += unresolved
            c["trim_invalid"] += bool(v_trim)
            c["inject_invalid"] += bool(v_inj_orig)
            c["both_invalid"] += bool(v_both)
            c["budget_violation"] += not (t.over_budget or t.tokens <= budget - reserve)
            c["over_budget"] += t.over_budget
            c["task_lost"] += not keeps
            c["thinking_edited"] += not think_ok
            c["thinking_msgs"] += any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for m in conv for b in (m["content"] if isinstance(m.get("content"), list) else []))
            if tool:
                c["tool_cases"] += 1
                c["naive_split"] += "orphan_result_or_split_pair" in v_naive
                c["naive_task_lost"] += not naive_keeps
                for k in set(v_naive):
                    c1[k] += 1
            if tail_tool and block:
                c["tool_tail_with_block"] += 1
                c["naive_inject_invalid"] += bool(v_ninj)
                c["good_inject_invalid_tool_tail"] += bool(v_both)
                for k in set(v_ninj):
                    c2[k] += 1
            if unresolved and block:
                c["unresolved_with_block"] += 1
                c["c3_invalid_unresolved"] += bool(v_c3)
                c["good_inject_invalid_unresolved"] += bool(v_both)
                for k in set(v_c3):
                    c3[k] += 1
            if raw_left > 0 and (tool or i % 3 == 0):
                raw_left -= 1
                rows.append({"shape": shape, "i": i, "budget": budget, "reserve": reserve, "block": block, "input": conv, "trimmed": t.messages, "final": final, "naive_trim": naive,
                             "naive_inject": naive_inj, "text_after_results": c3_inj, "over_budget": t.over_budget, "unresolved": unresolved})
            else:
                rows.append({"shape": shape, "i": i, "tool": tool, "tail_tool": tail_tool, "unresolved": unresolved, "trim_v": v_trim, "both_v": v_both, "naive_v": v_naive, "keeps_task": keeps})
        mut = {}
        for kind in MUTS:
            made = caught = 0
            guard = 0
            while made < MUT_N and guard < 20000:
                guard += 1
                mm = mutate(shape, gen.conversation(rng, shape), kind, rng)
                if mm is None:
                    continue
                made += 1
                caught += bool(M.validate(shape, mm))
            mut[kind] = {"made": made, "caught": caught}
            rows.append({"shape": shape, "mutation": kind, "made": made, "caught": caught})
        met[shape] = {"counts": dict(c), "c1_classes": dict(c1), "c2_classes": dict(c2), "c3_classes": dict(c3), "mutants": mut}
    return rows, met


def evaluate(met, params):
    out = []
    for shape, m in met.items():
        c = m["counts"]
        r = lambda a, b: c.get(a, 0) / max(1, c.get(b, 0))
        out += [
            check("%s: 0 invalid from trim/inject/trim+inject" % shape, [c["trim_invalid"], c["inject_invalid"], c["both_invalid"]], not (c["trim_invalid"] or c["inject_invalid"] or c["both_invalid"])),
            check("%s: 0 false positives on generated valid" % shape, c["orig_invalid"], c["orig_invalid"] == 0),
            check("%s: budget respected when not over_budget" % shape, c["budget_violation"], c["budget_violation"] == 0),
            check("%s: the user's original task survives every trim (100%%)" % shape, c["task_lost"], c["task_lost"] == 0),
            check("%s: thinking blocks never edited or separated from their turn" % shape, [c["thinking_edited"], c["thinking_msgs"]], c["thinking_edited"] == 0 and (shape == "openai" or c["thinking_msgs"] > 0)),
            check("%s: witness trimmed >=30%%" % shape, r("trimmed", "n"), r("trimmed", "n") >= 0.30),
            check("%s: witness has tool use >=40%%" % shape, r("has_tool", "n"), r("has_tool", "n") >= 0.40),
            check("%s: witness ends in tool result >=25%%" % shape, r("tail_tool_result", "n"), r("tail_tool_result", "n") >= 0.25),
            check("%s: control C1 naive message-level trim: split-pair class >=10%% of tool cases" % shape, r("naive_split", "tool_cases"), r("naive_split", "tool_cases") >= 0.10),
            check("%s: control C1b naive trim loses the user's task >=10%% of tool cases" % shape, r("naive_task_lost", "tool_cases"), r("naive_task_lost", "tool_cases") >= 0.10),
            check("%s: control C2 naive injector invalid >=10%% of tool-tail cases" % shape, r("naive_inject_invalid", "tool_tail_with_block"), r("naive_inject_invalid", "tool_tail_with_block") >= 0.10),
            check("%s: canary real injector valid on tool-tail cases" % shape, c.get("tool_tail_with_block", 0), c.get("tool_tail_with_block", 0) > 0 and c["good_inject_invalid_tool_tail"] == 0),
        ]
        if shape == "anthropic":
            out += [check("anthropic: unresolved server tool tail: witness >=300 and the real injector stays valid", [c.get("unresolved_with_block", 0), c.get("good_inject_invalid_unresolved", 0)], c.get("unresolved_with_block", 0) >= 300 and c["good_inject_invalid_unresolved"] == 0),
                    check("anthropic: control C3 plain-text-after-results injector invalid on unresolved server tool >=10%", r("c3_invalid_unresolved", "unresolved_with_block"), r("c3_invalid_unresolved", "unresolved_with_block") >= 0.10)]
        for k, v in m["mutants"].items():
            if v["made"]:
                out.append(check("%s: validator rejects mutation %s 100%%" % (shape, k), v, v["made"] >= MUT_N and v["caught"] == v["made"]))
    return out


if __name__ == "__main__":
    main("p1_trim", run_seed, evaluate, {"N": N, "MUT_N": MUT_N, "RAW_N": RAW_N}, claim="trim/inject keep tool pairing, thinking, task and placement valid in both shapes")

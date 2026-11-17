"""J1 runner. Stages: smoke | gen | select | judge | audit | score. Every call goes through pilots.llm.LLM (content-hash cache, persistent
call counter, limit stop, returned-model check). No retries: a failed call is recorded and the item is WITHHELD by the scorer.
usage: python -m pilots.j1_run STAGE WORKDIR      (WORKDIR holds pilot_qa.json and receives all outputs; it is never inside a git checkout)
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import j1_items as J
from .judge import judge_prompt_one, parse_one
from .llm import LLM, LimitStop
from .prompts import JUDGE_RECIPE, JUDGE_SYSTEM, READER_RECIPE, READER_SYSTEM, READER_TEMPLATE_V12

READER_MODEL, JUDGE_MODEL, AUDIT_MODEL = "claude-sonnet-5-5", "claude-opus-5-5", "gpt-6.1-sol"


def llm(work, name, cap, recipe, backend="claude", conc=3):
    cwd = os.path.join(work, "neutral")  # a non-git directory, so no repo hook reacts to the call
    os.makedirs(cwd, exist_ok=True)
    return LLM(os.path.join(work, "cache"), os.path.join(work, "run_" + name), cap, concurrency=conc, recipe=recipe, cwd=cwd, backend=backend)


def pmap(fn, xs, workers):
    out, stop = [], []
    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(lambda x: _safe(fn, x, stop), xs):
            out.append(r)
    if stop:
        print("STOPPED on a limit:", stop[0])
    return out


def _safe(fn, x, stop):
    if stop:
        return {"error": "skipped after limit stop"}
    try:
        return fn(x)
    except LimitStop as e:
        stop.append(str(e))
        return {"error": "limit: " + str(e)[:200]}
    except Exception as e:  # recorded, never retried; the scorer withholds on a missing row
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def stage_smoke(work):
    """5 calls: Codex audit (done first, separately), two judge calls and one reader call on invented fixtures, one generator call on an invented row."""
    m = llm(work, "smoke", 8, JUDGE_RECIPE)
    yes = judge_prompt_one("single-session-user", "What color is my car?", "Blue", "You said your car is blue.\nAnswer: Blue")
    no = judge_prompt_one("single-session-user", "What color is my car?", "Blue", "Your car is red.\nAnswer: Red")
    res = {}
    for tag, p in (("judge_yes", yes), ("judge_no", no)):
        c = m.call(p, JUDGE_SYSTEM, JUDGE_MODEL, tag, tag, "smoke")
        res[tag] = {"verdict": parse_one(c["result"]), "cached": c["cached"], "model_returned": c["model_returned"], "in": c["in_tokens"], "cc": c["cache_creation"], "cr": c["cache_read"], "out": c["out_tokens"], "s": c["seconds"]}
    mr = llm(work, "smoke_reader", 3, READER_RECIPE)
    rp = READER_TEMPLATE_V12.format(context="[2023-05-01 09:00] user: I bought a blue car today.\nassistant: Congratulations.", date="2023-06-01", question="What color is my car?")
    c = mr.call(rp, READER_SYSTEM, READER_MODEL, "reader", "fixture", "smoke")
    res["reader"] = {"answer_tail": c["result"][-80:], "cached": c["cached"], "model_returned": c["model_returned"], "in": c["in_tokens"], "cc": c["cache_creation"], "cr": c["cache_read"], "out": c["out_tokens"], "s": c["seconds"]}
    mg = llm(work, "smoke_gen", 3, J.GEN_RECIPE)
    row = {"qid": "fixture", "qtype": "single-session-user", "question": "What color is my car?", "answer": "Blue", "question_date": "2023-06-01 10:00", "cluster": "fx"}
    c = mg.call(J.gen_prompt(row), J.GEN_SYSTEM, READER_MODEL, "gen", "fixture", "smoke")
    g = J.parse_gen(c["result"], row["qtype"])
    res["gen"] = {"parsed_keys": sorted(g), "cached": c["cached"], "model_returned": c["model_returned"], "in": c["in_tokens"], "cc": c["cache_creation"], "cr": c["cache_read"], "out": c["out_tokens"], "s": c["seconds"]}
    ma = llm(work, "smoke_audit", 2, J.AUDIT_RECIPE, backend="codex", conc=1)
    fx = {"qtype": "single-session-user", "question": "What color is my car?", "gold": "Blue", "response": "Your car is red.\nAnswer: Red"}
    c = ma.call(J.audit_prompt(fx), J.AUDIT_SYSTEM, AUDIT_MODEL, "audit", "fixture", "smoke")
    res["audit"] = {"verdict": J.parse_verdict(c["result"]), "cached": c["cached"], "in": c["in_tokens"], "cr": c["cache_read"], "out": c["out_tokens"], "s": c["seconds"]}
    json.dump(res, open(os.path.join(work, "smoke_result.json"), "w"), indent=1)
    print(json.dumps(res, indent=1))


def stage_gen(work):
    rows = json.load(open(os.path.join(work, "pilot_qa.json")))
    m = llm(work, "gen", 100, J.GEN_RECIPE)

    def one(r):
        c = m.call(J.gen_prompt(r), J.GEN_SYSTEM, READER_MODEL, "gen", r["qid"], "j1_gen")
        return {"qid": r["qid"], "parsed": J.parse_gen(c["result"], r["qtype"]), "key": c["key"]}
    out = pmap(one, rows, 3)
    gens = {o["qid"]: o["parsed"] for o in out if "qid" in o}
    json.dump(gens, open(os.path.join(work, "gens.json"), "w"), indent=1)
    print("generated", len(gens), "of", len(rows), "errors", [o["error"] for o in out if "error" in o][:3])


def stage_select(work):
    rows = json.load(open(os.path.join(work, "pilot_qa.json")))
    gens = json.load(open(os.path.join(work, "gens.json")))
    items = J.select_items(rows, gens)
    json.dump(items, open(os.path.join(work, "items.json"), "w"), indent=1)
    json.dump(J.control_items(rows, items), open(os.path.join(work, "control_items.json"), "w"), indent=1)
    from collections import Counter
    print(len(items), dict(Counter((i["category"], i["bucket"]) for i in items)), dict(Counter(i["kind"] for i in items if i["category"] == "borderline")))


def _judge_rows(work, items, name, cap, stage):
    m = llm(work, name, cap, JUDGE_RECIPE)

    def one(i):
        c = m.call(judge_prompt_one(*J.JUDGE_ARGS(i)[:4]), JUDGE_SYSTEM, JUDGE_MODEL, stage, i["item_id"], stage)
        return {"item_id": i["item_id"], "verdict": parse_one(c["result"]), "model_returned": c["model_returned"], "key": c["key"], "cached": c["cached"]}
    return pmap(one, items, 3)


def stage_judge(work):
    items = json.load(open(os.path.join(work, "items.json")))
    ctl = json.load(open(os.path.join(work, "control_items.json")))
    for name, xs, fn in (("judge", items, "judge_rows.jsonl"), ("control", ctl, "control_rows.jsonl")):
        out = _judge_rows(work, xs, "j1_" + name, 170 if name == "judge" else 30, "j1_" + name)
        with open(os.path.join(work, fn), "w") as fh:
            for o in out:
                if "item_id" in o:
                    fh.write(json.dumps(o) + "\n")
        print(name, "rows", sum(1 for o in out if "item_id" in o), "errors", [o["error"] for o in out if "error" in o][:3])


def stage_audit(work):
    items = [i for i in json.load(open(os.path.join(work, "items.json"))) if i["category"] == "borderline"]
    m = llm(work, "j1_audit", 45, J.AUDIT_RECIPE, backend="codex", conc=2)

    def one(i):
        c = m.call(J.audit_prompt(i), J.AUDIT_SYSTEM, AUDIT_MODEL, "j1_audit", i["item_id"], "j1_audit")
        return {"item_id": i["item_id"], "verdict": J.parse_verdict(c["result"]), "key": c["key"], "cached": c["cached"]}
    out = pmap(one, items, 2)
    with open(os.path.join(work, "audit_rows.jsonl"), "w") as fh:
        for o in out:
            if "item_id" in o:
                fh.write(json.dumps(o) + "\n")
    print("audit rows", sum(1 for o in out if "item_id" in o), "errors", [o["error"] for o in out if "error" in o][:3])


if __name__ == "__main__":
    {"smoke": stage_smoke, "gen": stage_gen, "select": stage_select, "judge": stage_judge, "audit": stage_audit}[sys.argv[1]](sys.argv[2])

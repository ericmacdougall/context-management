"""Check (b): on the knowledge-update pilot questions, does the extracted fact set contain both the old and new value, and does
the supersession code mark the newer one current? Gold: the later-dated gold session is current (eval loader rule).
Usage: python3 -m phasef.check_extraction"""
import json

from emlab.dataset import LongMemEvalLoader, parse_date

from . import data, run_pilot as R
from .facts import toks


def main():
    S, _ = R.pilot()
    per_key = R.load_facts(S)
    out = {}
    for q in data.FACT_QIDS:
        facts, by_id = R.resolved_for(S, q, per_key)
        rec = S[q]
        gold = list(rec["answer_session_ids"])
        dates = {sid: parse_date(d) for sid, d, _ in data.sessions_of(rec)}
        gold_sorted = sorted(gold, key=lambda s: (dates[s], s))
        latest = gold_sorted[-1]
        ans = set(toks(str(rec["answer"])))
        gf = [f for f in facts if f["session_id"] in gold]
        slots = {}
        for f in gf:
            slots.setdefault(f["_i"], f)
        # a slot linking old and new gold: a superseded gold-old fact whose superseder is a gold-latest fact
        linked = [(f, facts[f["superseded_by"]]) for f in gf if f["status"] == "superseded" and f["superseded_by"] is not None
                  and facts[f["superseded_by"]]["session_id"] == latest and f["session_id"] != latest]
        cur_latest = [f for f in gf if f["session_id"] == latest and f["status"] == "current"]
        ans_in_cur = [f for f in facts if f["status"] == "current" and ans and len(ans & set(toks(f["object"]))) / float(len(ans)) >= 0.5]
        out[q] = {"type": rec["question_type"], "gold_sessions": gold_sorted, "latest": latest, "facts_total": len(facts),
                  "facts_from_gold": {s: sum(1 for f in gf if f["session_id"] == s) for s in gold_sorted},
                  "old_new_linked_pairs": [(a["session_id"] + " | " + a["subject"] + " | " + a["predicate"] + " | " + a["object"],
                                            b["session_id"] + " | " + b["subject"] + " | " + b["predicate"] + " | " + b["object"]) for a, b in linked],
                  "latest_gold_facts_current": len(cur_latest),
                  "current_facts_containing_gold_answer_tokens": [(f["session_id"], f["predicate"], f["object"]) for f in ans_in_cur][:5],
                  "gold_answer": str(rec["answer"])}
    print(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    main()

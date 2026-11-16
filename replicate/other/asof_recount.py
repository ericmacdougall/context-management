"""DESIGN 3.7.8 recount from the raw LongMemEval-S file (THRESHOLDS.md section O). Own date parser; nothing imported
from the eval package. Counts session references (question, session) and distinct gold references.

usage: python asof_recount.py <longmemeval_s_cleaned.json> <out.json>
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

D = re.compile(r"(\d{4})/(\d{2})/(\d{2}) \(\w+\) (\d{2}):(\d{2})")


def p(s):
    m = D.match(s.strip())
    if not m:
        raise ValueError(s)
    return datetime(*map(int, m.groups()))


def main(path, out):
    data = json.load(open(path, encoding="utf-8"))
    tot = {"questions": len(data), "non_abstention": 0, "hay_refs": 0, "gold_refs": 0,
           "day_removes_hay": 0, "day_removes_gold": 0, "ts_removes_hay": 0, "ts_removes_gold": 0,
           "day_removes_nongold": 0, "gold_refs_incl_abstention": 0, "ts_removes_gold_incl_abstention": 0, "questions_with_ts_removed_gold": 0, "dup_sid_refs_skipped": 0}
    examples = []
    for q in data:
        qd = p(q["question_date"])
        abst = q["question_id"].endswith("_abs")
        gold = set(q["answer_session_ids"]) if not abst else set()
        tot["non_abstention"] += 0 if abst else 1
        seen = set()
        tsg = 0
        for sid, ds in zip(q["haystack_session_ids"], q["haystack_dates"]):
            if sid in seen:
                tot["dup_sid_refs_skipped"] += 1
                continue
            seen.add(sid)
            d = p(ds)
            tot["hay_refs"] += 1
            g = sid in gold
            tot["gold_refs"] += g
            ga = sid in set(q["answer_session_ids"])
            tot["gold_refs_incl_abstention"] += ga
            tot["ts_removes_gold_incl_abstention"] += ga and d > qd
            if d.date() > qd.date():
                tot["day_removes_hay"] += 1
                tot["day_removes_gold"] += g
                tot["day_removes_nongold"] += (not g)
            if d > qd:
                tot["ts_removes_hay"] += 1
                tot["ts_removes_gold"] += g
                tsg += g
                if g and len(examples) < 5:
                    examples.append({"qid": q["question_id"], "session": sid, "session_date": ds, "question_date": q["question_date"]})
        tot["questions_with_ts_removed_gold"] += tsg > 0
    res = {"stamp": {"thresholds_commit": "8c3b5e6", "script_commit": os.environ.get("REP_COMMIT", "uncommitted"),
                     "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "input": [path, hashlib.sha256(open(path, "rb").read()).hexdigest()]},
           "counts": tot, "timestamp_rule_gold_examples": examples,
           "owner": {"day_removes_hay": "0 of 23,854", "day_removes_gold": "0 of 890", "ts_removes_gold": 70,
                     "verify_json_late_sessions_that_are_gold": 75},
           "threshold_checks": {"day_rule_removes_0_gold": tot["day_removes_gold"] == 0,
                                "control_timestamp_rule_removes_gold": tot["ts_removes_gold"] > 0,
                                "day_rule_removes_0_nongold": tot["day_removes_nongold"] == 0}}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "stamp"}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:3])

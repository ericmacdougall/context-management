"""J1 judge validation (PREREG-V2 section 4): item generation, the seeded selection rule, the shuffled-gold control and the audit prompt.
Everything here is fixed and committed BEFORE the first generator call; no model output is read to change it.

Generator: one call per pilot question (94) on claude-sonnet-5-5, prompt `j1-gen-1`. Items are labelled BY CONSTRUCTION (wrong answers = no,
correct paraphrases = yes); borderline items have no construction label and are audited by a second model (DEVIATION: agent audit, not human).

Selection rule (seed 16096, all hashes sha256 over the quoted string, applied in this order):
  wrong      60 = 20 wrong_value + 20 near_miss_wrong + 20 refusal (the refusal text is fixed, no generator)
  paraphrase 60 = 30 para_terse + 30 para_verbose
  borderline 40, at most 8 per kind
Within each bucket questions are visited in ascending order of sha256("16096:J1:<category>:<bucket>:<qid>"); a question is taken iff its generated
item for the bucket is valid and its content cluster has not been used in the same category (so the 60 and 60 and 40 items of a category come
from distinct content clusters and a plain Wilson interval over items is valid). A borderline question takes the eligible valid kind with the
fewest items so far (ties: ascending sha256("16096:J1:kind:<kind>:<qid>")), skipped if every kind is at its cap of 8.
Shuffled-gold control: the first 20 paraphrase items (by the hash order of the paraphrase selection) are judged again against the gold answer
of another pilot question of the same type and a different content cluster (first by sha256("16096:J1:ctl:<qid>:<other qid>") whose gold
string differs). PASS iff at least 18 of 20 are marked wrong.
"""
import hashlib
import json
import re

from .judge import get_anscheck_prompt

GEN_RECIPE = "j1-gen-1"
AUDIT_RECIPE = "j1-audit-1"
SEED = "16096:J1:"
REFUSAL = "The context does not contain the information needed to answer this question, so the information is not available.\nAnswer: The information is not available."
BORDERLINE_CAP = 8

COMMON_KINDS = ["hedge_two", "correct_plus_wrong_detail", "near_value", "partial"]
EXTRA_KINDS = {"temporal-reasoning": ["off_by_one"], "knowledge-update": ["stale_and_updated", "stale_only"], "single-session-preference": ["rubric_partial"]}
WRONG_BUCKETS = [("wrong_value", 20), ("near_miss_wrong", 20), ("refusal", 20)]
PARA_BUCKETS = [("para_terse", 30), ("para_verbose", 30)]

GEN_SYSTEM = "You write test responses for a validation study of an answer grader. Output only valid JSON, no prose, no code fences."

GEN_TEMPLATE = """A memory assistant was asked a question about a user's past chat history. The correct answer (or, for a preference question, the rubric of a good personalised response) is known. You write test responses in the style of the assistant: a few sentences of reasoning followed by a last line that starts with 'Answer:'. The responses are used to test a grader, so follow each instruction exactly.

Question type: {qtype}
Question (asked on {date}): {question}
Correct answer or rubric: {answer}

Write these responses (each is a string; every one ends with the final answer on its own last line, starting 'Answer:'):
- "wrong_value": a confident response whose final answer is plainly wrong: a different value of the same kind as the correct answer, with no part of the correct answer in it.
- "near_miss_wrong": a response that looks very close to the correct answer in wording and structure but gets the key fact wrong (a different number, name, date, place or item). It must not be correct under any reading.
- "para_terse": a CORRECT response in one or two sentences that expresses the correct answer in clearly different words or format (for example a different unit phrasing, synonyms, a rewritten sentence). Every fact of the correct answer must be present and nothing in it may be wrong.
- "para_verbose": a CORRECT response of three to five sentences of reasoning that reaches the correct answer in different words. Nothing in it may be wrong.
- "borderline": an object with one entry per kind below; each value is a response string, or null if the kind cannot be written for this question. Borderline responses sit between right and wrong on purpose; do not say which side they fall on.
{kinds}

Output exactly one JSON object: {{"wrong_value": "...", "near_miss_wrong": "...", "para_terse": "...", "para_verbose": "...", "borderline": {{{keys}}}}}"""

KIND_TEXT = {
    "hedge_two": '"hedge_two": gives the correct answer and one other plausible different answer as equal possibilities without committing to either.',
    "correct_plus_wrong_detail": '"correct_plus_wrong_detail": contains the correct answer but also asserts one additional detail that is materially wrong.',
    "near_value": '"near_value": commits to a value close to the correct one but not equal (a nearby number, an adjacent date, a sibling item); if the answer is not a value, a closely related item.',
    "partial": '"partial": gives only a subset of the information the correct answer requires (only if the correct answer has two or more parts; otherwise null).',
    "off_by_one": '"off_by_one": the correct answer is a count of days, weeks or months; give a response that is off by exactly one unit.',
    "stale_and_updated": '"stale_and_updated": mentions the earlier, outdated value and then states the correct (updated) value as the current one.',
    "stale_only": '"stale_only": states only the earlier, outdated value as if it were current.',
    "rubric_partial": '"rubric_partial": recalls and uses some of the personal information in the rubric correctly but misses other parts of it.',
}


def kinds_for(qtype):
    return COMMON_KINDS + EXTRA_KINDS.get(qtype, [])


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def gen_prompt(row):
    ks = kinds_for(row["qtype"])
    return GEN_TEMPLATE.format(qtype=row["qtype"], date=row["question_date"], question=row["question"], answer=row["answer"],
                               kinds="\n".join("  " + KIND_TEXT[k] for k in ks), keys=", ".join('"%s": "..."' % k for k in ks))


def _norm_resp(x):
    """Put the final 'Answer:' on its own last line (the generator sometimes writes it inline); None if there is no Answer or it is not final."""
    if not isinstance(x, str) or len(x) <= 10 or "Answer:" not in x:
        return None
    head, _, tail = x.rpartition("Answer:")
    if not tail.strip() or "\n" in tail.strip():
        return None
    head = head.rstrip()
    return (head + "\n" if head else "") + "Answer: " + tail.strip()


def parse_gen(text, qtype):
    """Return a dict bucket -> response (only valid strings); raises ValueError if the text is not a JSON object."""
    t = text.strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON object")
    d = json.loads(m.group(0))
    out = {k: _norm_resp(d.get(k)) for k in ("wrong_value", "near_miss_wrong", "para_terse", "para_verbose") if _norm_resp(d.get(k))}
    bl = d.get("borderline") if isinstance(d.get("borderline"), dict) else {}
    for k in kinds_for(qtype):
        if _norm_resp(bl.get(k)):
            out["border:" + k] = _norm_resp(bl[k])
    return out


def select_items(rows, gens):
    """rows: pilot rows; gens: qid -> parse_gen dict (missing qid = generator failure). Returns the list of selected items, in a fixed order."""
    by = {r["qid"]: r for r in rows}
    items, used = [], {"wrong": set(), "paraphrase": set(), "borderline": set()}

    def take(cat, bucket, kind, qid, resp, label):
        items.append({"item_id": "%s:%s:%s" % (cat, bucket, qid), "category": cat, "bucket": bucket, "kind": kind, "qid": qid, "qtype": by[qid]["qtype"],
                      "question": by[qid]["question"], "gold": by[qid]["answer"], "response": resp, "cluster": by[qid]["cluster"], "construction_label": label})
        used[cat].add(by[qid]["cluster"])

    for cat, buckets, label in (("wrong", WRONG_BUCKETS, "no"), ("paraphrase", PARA_BUCKETS, "yes")):
        for bucket, quota in buckets:
            got = 0
            for qid in sorted(by, key=lambda q: h("%s%s:%s:%s" % (SEED, cat, bucket, q))):
                if got >= quota:
                    break
                if by[qid]["cluster"] in used[cat]:
                    continue
                resp = REFUSAL if bucket == "refusal" else (gens.get(qid) or {}).get(bucket)
                if resp is None:
                    continue
                take(cat, bucket, bucket, qid, resp, label)
                got += 1
    counts = {}
    for qid in sorted(by, key=lambda q: h("%sborderline:%s" % (SEED, q))):
        if len([i for i in items if i["category"] == "borderline"]) >= 40:
            break
        if by[qid]["cluster"] in used["borderline"]:
            continue
        g = gens.get(qid) or {}
        elig = [k for k in kinds_for(by[qid]["qtype"]) if ("border:" + k) in g and counts.get(k, 0) < BORDERLINE_CAP]
        if not elig:
            continue
        k = sorted(elig, key=lambda k: (counts.get(k, 0), h("%skind:%s:%s" % (SEED, k, qid))))[0]
        counts[k] = counts.get(k, 0) + 1
        take("borderline", "borderline", k, qid, g["border:" + k], None)
    return items


def control_items(rows, items):
    """The 20 shuffled-gold control items (see module doc). Each carries the foreign gold string and the id of the question it came from."""
    by = {r["qid"]: r for r in rows}
    paras = [i for i in items if i["category"] == "paraphrase"]
    paras = sorted(paras, key=lambda i: h("%sparaphrase:%s:%s" % (SEED, i["bucket"], i["qid"])))[:20]
    out = []
    for i in paras:
        cands = [q for q in by if by[q]["qtype"] == i["qtype"] and by[q]["cluster"] != i["cluster"] and by[q]["answer"] != i["gold"]]
        other = sorted(cands, key=lambda q: h("%sctl:%s:%s" % (SEED, i["qid"], q)))[0]
        out.append(dict(i, item_id="control:" + i["item_id"], category="control", gold=by[other]["answer"], foreign_gold_from=other))
    return out


JUDGE_ARGS = lambda it: (it["qtype"], it["question"], it["gold"], it["response"], False)  # noqa: E731

AUDIT_SYSTEM = ("You are an independent auditor of an answer-grading task. You will be given the grading instructions for one item, exactly as a grader "
                "receives them. Apply them as written. Reply with one sentence of reasoning, then a final line that is exactly 'Verdict: yes' or 'Verdict: no'.")


def audit_prompt(it):
    return get_anscheck_prompt(it["qtype"], it["question"], it["gold"], it["response"], False)


def parse_verdict(text):
    m = re.findall(r"^\s*Verdict:\s*(yes|no)\s*$", text.strip(), re.I | re.M)
    if len(m) != 1:
        raise ValueError("audit output has no single Verdict line: %r" % text[-200:])
    return m[0].lower() == "yes"

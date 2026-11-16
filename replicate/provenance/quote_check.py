"""Independent recompute of the X04 quote-and-speaker check (THRESHOLDS.md section Q; DESIGN 3.4.2, 3.4.3).

Written from PROVENANCE_CHECK.md at feat/phasef-harness 1a62931; phasef/provenance.py is not imported or copied.
Rule: a round starts at each user turn (the first round starts wherever the session starts); the round text is the
turn contents joined by one space; normalisation is whitespace collapsing plus lower-casing only.
(a) the normalised quote is a substring of the normalised text of the cited round (round index as emitted).
(b) speakers = roles of the turns of the cited round whose own normalised content contains the quote:
    none -> undefined, both roles -> ambiguous, the stated author only -> pass, the other role only -> mismatch.
Variants reported beside the primary rule (not used for the verdict): strict byte-exact substring, and the eval
rounds.jsonl text format ("role: content" lines).
Controls (my own mutator, seed 16096) over facts that pass: (1) one word of the quote replaced by a token absent from
the round, (2) the quote cited against the adjacent round (+1, else -1), excluding quotes that also occur there,
(3) author flipped on (b) passes. Each must be rejected 100%.

usage: python quote_check.py <facts.jsonl> <dataset slice .json> <out.json>
"""
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

WS = re.compile(r"\s+")


def nrm(s):
    return WS.sub(" ", str(s)).strip().lower()


def group_rounds(turns):
    out = []
    for t in turns:
        if t["role"] == "user" or not out:
            out.append([t])
        else:
            out[-1].append(t)
    return out


def check_a(quote, rounds, i, mode="norm"):
    if not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(rounds):
        return False
    g = rounds[i]
    if mode == "norm":
        q = nrm(quote)
        return bool(q) and q in nrm(" ".join(t["content"] for t in g))
    if mode == "strict":
        return bool(quote) and quote in " ".join(t["content"] for t in g)
    if mode == "evalfmt":
        q = nrm(quote)
        return bool(q) and q in nrm("\n".join("%s: %s" % (t["role"], t["content"]) for t in g))
    raise ValueError(mode)


def check_b(quote, author, rounds, i):
    if not isinstance(i, int) or i < 0 or i >= len(rounds):
        return "undefined"
    q = nrm(quote)
    sp = {t["role"] for t in rounds[i] if q and q in nrm(t["content"])}
    if not sp:
        return "undefined"
    if len(sp) > 1:
        return "ambiguous"
    return "pass" if author in sp else "mismatch"


def wilson(k, n, z=1.96):
    if not n:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [c - h, c + h]


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main(facts_p, slice_p, out_p):
    facts = [json.loads(l) for l in open(facts_p, encoding="utf-8")]
    sess, conflicts = {}, 0
    for rec in json.load(open(slice_p, encoding="utf-8")):
        for sid, s in zip(rec["haystack_session_ids"], rec["haystack_sessions"]):
            if sid in sess and sess[sid] != s:
                conflicts += 1
            sess.setdefault(sid, s)
    R = {sid: group_rounds(s) for sid, s in sess.items()}
    rng = random.Random(16096)
    items, A, Astrict, Aeval, B = [], Counter(), Counter(), Counter(), Counter()
    per_session = defaultdict(lambda: [0, 0])
    c1, c2, c3 = Counter(), Counter(), Counter()
    for k, f in enumerate(facts):
        rounds = R.get(f["session_id"], [])
        i = f["round"]
        a = check_a(f["quote"], rounds, i)
        a_s = check_a(f["quote"], rounds, i, "strict")
        a_e = check_a(f["quote"], rounds, i, "evalfmt")
        b = check_b(f["quote"], f["author"], rounds, i)
        A[a] += 1
        Astrict[a_s] += 1
        Aeval[a_e] += 1
        B[b] += 1
        per_session[f["session_id"]][0] += 1
        per_session[f["session_id"]][1] += int(a)
        it = {"k": k, "session_id": f["session_id"], "round": i, "author": f["author"], "a": a, "a_strict": a_s,
              "a_evalfmt": a_e, "b": b}
        if a:
            # control 1: replace one word with a token absent from the round
            words = f["quote"].split()
            j = rng.randrange(len(words))
            rtext = nrm(" ".join(t["content"] for t in rounds[i]))
            tok = "zq%dxv" % rng.randrange(10 ** 6)
            while tok in rtext:
                tok = "zq%dxv" % rng.randrange(10 ** 6)
            mut = " ".join(words[:j] + [tok] + words[j + 1:])
            acc = check_a(mut, rounds, i)
            c1["accepted" if acc else "rejected"] += 1
            it["c1_accepted"] = acc
            # control 2: adjacent round
            adj = i + 1 if i + 1 < len(rounds) else i - 1
            if adj < 0 or adj == i:
                c2["no_adjacent_round"] += 1
            elif check_a(f["quote"], rounds, adj):
                c2["excluded_quote_also_in_adjacent"] += 1
            else:
                acc2 = check_a(f["quote"], rounds, adj)
                c2["accepted" if acc2 else "rejected"] += 1
        if b == "pass":
            flipped = "assistant" if f["author"] == "user" else "user"
            v = check_b(f["quote"], flipped, rounds, i)
            c3["accepted" if v == "pass" else "rejected"] += 1
        items.append(it)
    n = len(facts)
    ns = len(per_session)
    s_all = sum(1 for t, p in per_session.values() if t == p)
    mismatches = [dict(items[k], quote=facts[k]["quote"][:200]) for k in range(n) if items[k]["b"] == "mismatch"]
    out = {
        "stamp": {"thresholds_commit": "8c3b5e6", "script_commit": os.environ.get("REP_COMMIT", "uncommitted"),
                  "script_sha256": sha(__file__), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "inputs": {"facts": [facts_p, sha(facts_p)], "slice": [slice_p, sha(slice_p)]},
                  "env": {"python": sys.version.split()[0], "host": os.uname().nodename}},
        "witness": {"facts": n, "sessions_cited": ns, "sessions_in_slice": len(sess),
                    "slice_session_content_conflicts": conflicts,
                    "facts_with_unknown_session": sum(1 for f in facts if f["session_id"] not in R)},
        "a_pass": A[True], "a_fail": A[False], "a_pass_rate": A[True] / n, "a_wilson": wilson(A[True], n),
        "a_strict_byte_exact_pass": Astrict[True], "a_evalfmt_pass": Aeval[True],
        "sessions_all_pass": s_all, "sessions_all_pass_wilson": wilson(s_all, ns),
        "b": dict(B), "controls": {"c1_one_word_replaced": dict(c1), "c2_adjacent_round": dict(c2),
                                   "c3_author_flipped": dict(c3)},
        "owner": {"a_pass": 2351, "facts": 2373, "b_pass": 2290, "b_mismatch": 8, "b_ambiguous": 53, "b_undefined": 22,
                  "c1_rejected": "2341 of 2341", "c2_rejected": "2208 of 2208 (62 excluded)", "c3_rejected": "2290 of 2290"},
        "mismatch_items": mismatches, "items": items}
    out["threshold_checks"] = {
        "3.4.2_a_equals_2351": A[True] == 2351,
        "3.4.3_mismatch_equals_8": B["mismatch"] == 8, "3.4.3_ambiguous_equals_53": B["ambiguous"] == 53,
        "controls_all_rejected": c1["accepted"] == 0 and c2["accepted"] == 0 and c3["accepted"] == 0}
    json.dump(out, open(out_p, "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k not in ("items", "mismatch_items")}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:4])

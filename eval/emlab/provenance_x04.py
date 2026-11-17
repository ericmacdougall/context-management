"""X04: mechanical quote-and-speaker provenance check on extracted facts (see PROVENANCE_CHECK.md). No model calls.
Usage: python3 -m phasef.provenance <facts jsonl> <dataset slice json> <out json>"""
import json
import random
import re
import sys
from collections import Counter

PASS, FAIL = "pass", "fail"


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def rounds_of_turns(turns):
    """Same grouping rule as eval build_rounds: a round starts at each user turn (the first group starts wherever the session starts)."""
    groups = []
    for t in turns:
        if t["role"] == "user" or not groups:
            groups.append([t])
        else:
            groups[-1].append(t)
    return groups


def anchor(quote, groups, idx):
    """-> (a_ok, speakers): a_ok = quote verbatim in the round text (normalised); speakers = set of roles of turns whose own text contains the quote."""
    if not isinstance(idx, int) or idx < 0 or idx >= len(groups):
        return False, set()
    q = norm(quote)
    if not q:
        return False, set()
    g = groups[idx]
    text = norm(" ".join(t["content"] for t in g))
    return (q in text), {t["role"] for t in g if q in norm(t["content"])}


def author_verdict(author, speakers):
    if not speakers:
        return "undefined"
    if len(speakers) > 1:
        return "ambiguous"
    return "pass" if author in speakers else "mismatch"


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / float(n)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (c - h, c + h)


def load_sessions(dataset_path):
    sess = {}
    for rec in json.load(open(dataset_path)):
        for sid, s in zip(rec["haystack_session_ids"], rec["haystack_sessions"]):
            sess.setdefault(sid, rounds_of_turns(s))
    return sess


def check_all(facts, sess):
    rows = []
    for f in facts:
        groups = sess.get(f["session_id"])
        if groups is None:
            rows.append({"a": FAIL, "b": "undefined", "why": "no such session"})
            continue
        ok, sp = anchor(f.get("quote", ""), groups, f.get("round"))
        rows.append({"a": PASS if ok else FAIL, "b": author_verdict(f["author"], sp), "speakers": sorted(sp)})
    return rows


def categorize(f, sess_all, sid_haystack):
    """Category for an (a) failure: wrong round / wrong session / truncated or ellipsis / paraphrase / other (also returns the best match round text)."""
    q = norm(f.get("quote", ""))
    groups = sess_all[f["session_id"]]
    for i, g in enumerate(groups):
        if i != f.get("round") and q and q in norm(" ".join(t["content"] for t in g)):
            return "wrong round (same session, round %d)" % i, " ".join(t["content"] for t in g)[:200]
    for sid in sid_haystack:
        if sid != f["session_id"]:
            for i, g in enumerate(sess_all[sid]):
                if q and q in norm(" ".join(t["content"] for t in g)):
                    return "wrong session", " ".join(t["content"] for t in g)[:200]
    parts = [p.strip() for p in re.split(r"\.\.\.|…|\[\.\.\.\]", q) if len(p.strip()) > 3]
    rid = f.get("round")
    rtext = norm(" ".join(t["content"] for t in groups[rid])) if isinstance(rid, int) and 0 <= rid < len(groups) else ""
    if len(parts) > 1 and all(p in rtext for p in parts):
        return "truncated or ellipsis", rtext[:200]
    toks = set(re.findall(r"[a-z0-9']+", q))
    best, bt = 0.0, ""
    for g in groups:
        t = norm(" ".join(x["content"] for x in g))
        cov = len(toks & set(re.findall(r"[a-z0-9']+", t))) / float(len(toks) or 1)
        if cov > best:
            best, bt = cov, t
    if best >= 0.8:
        return "paraphrase (token coverage %.2f in best round)" % best, bt[:200]
    return "other (best token coverage %.2f)" % best, bt[:200]


def controls(facts, sess, rows):
    c = {"word_changed": [0, 0], "adjacent_round": [0, 0, 0], "author_flipped": [0, 0]}  # tested, rejected (and exceptions)
    for f, r in zip(facts, rows):
        if r["a"] != PASS:
            continue
        groups = sess[f["session_id"]]
        words = f["quote"].split()
        if len(words) >= 2:
            mid = len(words) // 2
            w2 = words[:mid] + ["qqqxx"] + words[mid + 1:]
            ok, _ = anchor(" ".join(w2), groups, f["round"])
            c["word_changed"][0] += 1
            c["word_changed"][1] += 0 if ok else 1
        adj = f["round"] + 1 if f["round"] + 1 < len(groups) else f["round"] - 1
        if 0 <= adj < len(groups) and adj != f["round"]:
            ok, _ = anchor(f["quote"], groups, adj)
            if ok:
                c["adjacent_round"][2] += 1  # the quote legitimately also occurs in the adjacent round: excluded
            else:
                c["adjacent_round"][0] += 1
                c["adjacent_round"][1] += 1
        if r["b"] == "pass":
            flipped = "assistant" if f["author"] == "user" else "user"
            ok, sp = anchor(f["quote"], groups, f["round"])
            c["author_flipped"][0] += 1
            c["author_flipped"][1] += 1 if author_verdict(flipped, sp) == "mismatch" else 0
    return c


def main(facts_path, dataset_path, out_path):
    facts = [json.loads(l) for l in open(facts_path)]
    sess = load_sessions(dataset_path)
    rows = check_all(facts, sess)
    n = len(facts)
    a_pass = sum(1 for r in rows if r["a"] == PASS)
    b = Counter(r["b"] for r in rows)
    print("facts", n, "sessions", len({f["session_id"] for f in facts}))
    lo, hi = wilson(a_pass, n)
    print("(a) quote verbatim in cited round: %d/%d = %.1f%%  Wilson95 [%.1f%%, %.1f%%] (naive, per fact)" % (a_pass, n, 100.0 * a_pass / n, 100 * lo, 100 * hi))
    print("(b) author vs speaker over all facts:", dict(b))
    b_among_a = Counter(r["b"] for r in rows if r["a"] == PASS)
    print("(b) among facts passing (a):", dict(b_among_a))
    by_s = {}
    for f, r in zip(facts, rows):
        by_s.setdefault(f["session_id"], []).append(r["a"] == PASS)
    full = sum(1 for v in by_s.values() if all(v))
    lo, hi = wilson(full, len(by_s))
    print("sessions with every quote passing (a): %d/%d Wilson95 [%.1f%%, %.1f%%]" % (full, len(by_s), 100 * lo, 100 * hi))
    rng = random.Random(16096)
    sids = sorted(by_s)
    stats = []
    for _ in range(2000):
        draw = [rng.choice(sids) for _ in sids]
        k = sum(sum(by_s[s]) for s in draw)
        m = sum(len(by_s[s]) for s in draw)
        stats.append(k / float(m))
    stats.sort()
    print("session-clustered bootstrap 95%% interval for the (a) pass rate: [%.1f%%, %.1f%%]" % (100 * stats[49], 100 * stats[1949]))
    c = controls(facts, sess, rows)
    print("CONTROLS (rejections / tested): word changed %d/%d; adjacent round %d/%d (+%d excluded because the quote also occurs verbatim in the adjacent round); author flipped %d/%d" % (
        c["word_changed"][1], c["word_changed"][0], c["adjacent_round"][1], c["adjacent_round"][0], c["adjacent_round"][2], c["author_flipped"][1], c["author_flipped"][0]))
    fails = [(f, r) for f, r in zip(facts, rows) if r["a"] == FAIL]
    ids_by_q = {}
    for rec in json.load(open(dataset_path)):
        ids_by_q[rec["question_id"]] = rec["haystack_session_ids"]
    hay = {}
    for q, ids in ids_by_q.items():
        for s in ids:
            hay.setdefault(s, ids)
    sample = random.Random(16096).sample(fails, min(30, len(fails)))
    cats = Counter()
    out = {"n": n, "a_pass": a_pass, "b": dict(b), "controls": c, "sample": []}
    for f, r in sample:
        cat, snippet = categorize(f, sess, hay.get(f["session_id"], []))
        cats[cat.split(" (")[0]] += 1
        out["sample"].append({"session": f["session_id"], "round": f["round"], "author": f["author"], "quote": f["quote"], "category": cat, "best_round": snippet})
    print("failure categories over a seeded random 30 of the %d (a) failures:" % len(fails), dict(cats))
    allcats = Counter(categorize(f, sess, hay.get(f["session_id"], []))[0].split(" (")[0] for f, r in fails)
    print("failure categories over ALL %d (a) failures:" % len(fails), dict(allcats))
    mism = [(f, r) for f, r in zip(facts, rows) if r["b"] == "mismatch"]
    out["mismatches"] = [{"session": f["session_id"], "round": f["round"], "author": f["author"], "quote": f["quote"], "speakers": r["speakers"], "a": r["a"]} for f, r in mism]
    print("author mismatches:", len(mism))
    json.dump(out, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:4])

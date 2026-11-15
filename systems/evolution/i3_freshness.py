"""I3: share of planted later statements the bounded scan surfaces, by lexical-overlap level and replacement type, plus false challengers,
truncation and controls. usage: i3_freshness.py [trials_per_cell=200]   (pure Python)"""
import json, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evolution import decisions as D, freshness as F, task

RES = os.environ.get("RESDIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))
NOUNS = [f"{a}{b}" for a in "brav calm dusk elm fern glim harb ivor jun kelp lark mist nord opal pine quil rust sage tide umb vale wren yarn zeph".split() for b in ("ing", "ero", "ist", "ula")]
SYN = {n: n + "x" + str(i) for i, n in enumerate(NOUNS)}            # an unknown synonym: shares no token with the noun
BG = [f"w{k}{c}" for k in range(300) for c in "pqr"]                 # background vocabulary (disjoint from NOUNS)
LEVELS = [1.0, 0.75, 0.5, 0.25, 0.0]


def trial(rng, r, mode, planted=True, n_after=None):
    ts = rng.sample(NOUNS, 4)
    item_text = f"{ts[0]} {ts[1]} {ts[2]} {ts[3]} is friday"
    aliases = {"i0": [f"{t}al" for t in ts]}            # known aliases for every term
    ALIAS = {t: f"{t}al" for t in ts}
    n_after = n_after or rng.randint(30, 1500)
    evs = [{"seq": 1, "kind": "task_defined", "trust_origin": "user_typed", "body": {"task_id": "T", "items": [{"id": "i0", "type": "scope", "text": item_text}]}}]
    for _ in range(rng.randint(5, 30)):            # pre-decision discussion that mentions the item (must NOT be surfaced)
        evs.append({"seq": len(evs) + 1, "kind": "message", "trust_origin": "user_typed", "body": {"text": f"{ts[0]} {ts[1]} {ts[2]} {ts[3]} should be friday {rng.choice(BG)}"}})
    dec_seq = len(evs) + 1
    evs.append({"seq": dec_seq, "kind": "decision_recorded", "trust_origin": "user_typed", "body": {"decision_id": "d1", "action": "state", "ops": [{"op": "change", "id": "i0", "text": item_text + " "}]}})
    plant_at = rng.randrange(n_after)
    planted_seq = None
    for k in range(n_after):
        if planted and k == plant_at:
            keep = int(round(r * 4)); kept = ts[:keep]
            repl = [ALIAS[t] if mode == "alias" else SYN[t] for t in ts[keep:]]
            words = kept + repl + ["now", "monday", "instead"]
            rng.shuffle(words)
            planted_seq = len(evs) + 1
            evs.append({"seq": planted_seq, "kind": "message", "trust_origin": rng.choice(["user_typed", "assistant", "web"]), "body": {"text": " ".join(words)}})
            continue
        words = [rng.choice(BG) for _ in range(7)]
        x = rng.random()
        if x < 0.06:
            words[0] = rng.choice(ts)                  # distractor: one item term in an unrelated sentence
        elif x < 0.07:
            words[0], words[1] = rng.sample(ts, 2)     # strong distractor: two item terms
        evs.append({"seq": len(evs) + 1, "kind": "message", "trust_origin": rng.choice(["user_typed", "assistant", "web"]), "body": {"text": " ".join(words)}})
    return evs, aliases, planted_seq, dec_seq, item_text


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    rng = random.Random(16096)
    out = {"workload": f"{n} trials per cell; item of 4 terms; 30-1500 chatter events after the decision with 6% one-term and 1% two-term distractors; planted later statement keeps a fraction of the item terms and replaces the rest by a KNOWN alias or an UNKNOWN synonym; hash-embedding vector leg (a stand-in, not a real model)", "cells": {}}
    t0 = time.time()
    for point in ("strict", "loose"):
        for mode in ("alias", "unknown"):
            for r in LEVELS:
                if r == 1.0 and mode == "unknown":
                    continue
                hit = hitv = n_false = scans_with_false = n_events = trunc_hit = trunc_planted = 0
                lex_hits = vec_hits = 0
                for _ in range(n):
                    evs, al, ps, ds, txt = trial(rng, r, mode)
                    sc = F.scan(evs, ds, len(evs), txt, al["i0"], bound=2000, point=point)
                    ids = {c["seq"] for c in sc["challengers"]}
                    hit += ps in ids
                    for c in sc["challengers"]:
                        if c["seq"] == ps:
                            lex_hits += c["via"] == "lexical"; vec_hits += c["via"] == "vector"
                    fa = len(ids - {ps}); n_false += fa; scans_with_false += fa > 0; n_events += sc["scanned"] - 1
                    sc2 = F.scan(evs, ds, len(evs), txt, al["i0"], bound=300, point=point)
                    trunc_hit += ps in {c["seq"] for c in sc2["challengers"]}
                out["cells"][f"{point}|{mode}|overlap={r}"] = {"planted_surfaced": round(hit / n, 3), "via_lexical": lex_hits, "via_vector_only": vec_hits, "mean_false_challengers_per_scan": round(n_false / n, 2),
                                                                "scans_with_any_false_challenger": round(scans_with_false / n, 3), "false_rate_per_scanned_event": round(n_false / max(1, n_events), 4),
                                                                "planted_surfaced_when_bound_is_300": round(trunc_hit / n, 3)}
    # scans with NO planted statement: false challengers are the only output
    for point in ("strict", "loose"):
        fa = sc_f = ev_n = 0
        for _ in range(n):
            evs, al, ps, ds, txt = trial(rng, 0.0, "unknown", planted=False)
            sc = F.scan(evs, ds, len(evs), txt, al["i0"], point=point)
            fa += len(sc["challengers"]); sc_f += bool(sc["challengers"]); ev_n += sc["scanned"]
        out["cells"][f"{point}|no_planted_statement"] = {"mean_false_challengers_per_scan": round(fa / n, 2), "scans_with_any_challenger": round(sc_f / n, 3), "false_rate_per_scanned_event": round(fa / ev_n, 4)}
    # controls that can fail: scan from before the decision (stale watermark) and random surfacing at the same volume
    rnd_hit = ctl_false = tot = 0
    for _ in range(n):
        evs, al, ps, ds, txt = trial(rng, 0.5, "alias")
        good = F.scan(evs, ds, len(evs), txt, al["i0"], point="strict")
        bad = F.scan(evs, 0, len(evs), txt, al["i0"], point="strict")                    # wrong watermark: includes the pre-decision discussion
        ctl_false += len({c["seq"] for c in bad["challengers"]} & set(range(2, ds))) > 0
        k = len(good["challengers"]); msgs = [e["seq"] for e in evs if e["seq"] > ds]
        rnd_hit += ps in set(rng.sample(msgs, min(k, len(msgs)))); tot += 1
    out["controls"] = {"wrong_watermark_scans_that_resurface_pre_decision_text": round(ctl_false / tot, 3), "random_surfacing_same_volume_recall_overlap_0.5": round(rnd_hit / tot, 3)}
    # structure: every current item carries source + watermark, challengers are flagged and the render does not change state
    rng2 = random.Random(5); ok = bad_struct = 0
    for _ in range(300):
        evs, al, ps, ds, txt = trial(rng2, 0.75, "alias", n_after=200)
        s = D.fold(evs)
        before = D.canon(D.view(s))
        block = F.render_fresh(s, evs, len(evs), {"i0": al["i0"]})
        line = [l for l in block.splitlines() if l.startswith("[CURRENT] i0")][0]
        ok_line = "source: decision" in line and f"checked through event {len(evs)}" in line
        flagged = all("not applied" in l for l in block.splitlines() if "CHALLENGER" in l)
        unchanged = D.canon(D.view(s)) == before and s["items"]["i0"]["text"].strip() == txt.strip()
        ok += ok_line and flagged and unchanged and ("CHALLENGER" in block)
        bad_struct += not (ok_line and flagged and unchanged)
    out["structure"] = {"blocks_checked": 300, "with_source_watermark_flagged_challenger_and_state_unchanged": ok, "violations": bad_struct}
    c = out["cells"]
    out["pass"] = (out["structure"]["violations"] == 0 and c["strict|alias|overlap=1.0"]["planted_surfaced"] >= 0.99 and c["loose|unknown|overlap=0.0"]["planted_surfaced"] <= 0.2
                   and out["controls"]["wrong_watermark_scans_that_resurface_pre_decision_text"] > 0.5 and out["controls"]["random_surfacing_same_volume_recall_overlap_0.5"] < 0.2)
    out["limits"] = "Paraphrase with no shared term, alias or hash-embedding signal is not surfaced: that needs the analyst tier (an LLM scan), which is not part of this stream. The vector leg here is a hash embedding stand-in, not a semantic model."
    out["elapsed_s"] = round(time.time() - t0, 1)
    os.makedirs(RES, exist_ok=True)
    json.dump(out, open(f"{RES}/I3_freshness.json", "w"), indent=1)
    for k, v in out["cells"].items():
        print(k, v)
    print(out["controls"], out["structure"], out["pass"])


if __name__ == "__main__":
    main()

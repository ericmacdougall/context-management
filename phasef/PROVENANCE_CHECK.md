# X04 mechanical provenance check (thresholds committed before the run; no model calls)

Input: phasef-out/facts_v3_raw.jsonl (2,373 facts from the 14 gate-v3 questions' sessions, extraction v2). Source text: the gate sessions (gate16_s.json); rounds rebuilt with the eval group rule
(a round starts at each user turn) and cross-checked against ~/emlab-16096/out/emb/rounds.jsonl on t2-dev for a sample of round ids (text identical, or the check is reported as unreliable).

Checks per fact:
(a) the quote occurs verbatim, after whitespace collapsing and lower-casing only, in the text of the cited session's cited round (round integer as emitted by the extractor);
(b) the stated author (user or assistant) equals the speaker of the turn(s) containing the quote: pass if the quote is found only in turns of the stated author, MISMATCH if only in turns of the other role,
    AMBIGUOUS (not a pass) if it occurs in turns of both roles, UNDEFINED if it is found only across a turn boundary or not at all.

Controls that must be rejected (each counted over the facts that pass the check): (1) one word of the quote replaced; (2) the same quote cited against the adjacent round (round +1, else -1), excluding facts whose quote also occurs verbatim in that adjacent round (reported separately);
(3) author flipped on facts with an unambiguous match. Required: 100% of the controls rejected (excluding the reported legitimate exceptions); any acceptance is a checker bug.

Reported: counts for (a) pass/fail, (b) pass/mismatch/ambiguous/undefined; Wilson 95% bounds on facts and on sessions (share of sessions with every quote passing) and a session-clustered bootstrap interval (2,000 draws, seed 16096) for the fact pass rate;
a seeded random 30 of the (a) failures and all (b) mismatches classified by category (paraphrase, wrong round, wrong session, truncated or ellipsis, wrong speaker, other).

Threshold for "useful as a write gate": at least 95% of facts pass (a) AND the 30-item failure sample shows real defects (the quote is not in the cited place), not checker artefacts AND all controls rejected.

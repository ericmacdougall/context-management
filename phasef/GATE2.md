# Phase F v4 gate (GATE2), registered before any dev2 call

Source: main's message in this session (main's choice) and the user's answer answer:73716864-6b3b-4deb-94ef-8ed2c8432e4a/ec3c6904-8969-4afb-94a4-1b7305c6a605
"Fix the links first". One run on the fresh set; no criterion, prompt or rule changes after results are seen. The 16 questions of GATE (v3) are the
DEV set for the v4 link recipe and can no longer serve as a gate.

## Fresh set dev2 (main's rule, applied by phasef/select_dev2.py)
First 16 knowledge-update question ids in sorted order from the TEST split, not abstention, cluster of exactly one question (57 eligible):
01493427, 031748ae, 06db6396, 08e075c7, 0977f2af, 0ddfec37, 0f05491a, 10e09553, 184da446, 18bc8abd, 1cea1afa, 22d2cb42, 3ba21379, 41698283, 42ec0761, 4b24c848.
These clusters are removed from the test split for every stream. Sessions are extracted with recipe extract-v2 at 16 sessions per call.

## v4 link recipe (summary; code in phasef/links.py, frozen at the commit that precedes the first dev2 call)
Extraction v2 unchanged. Candidate pairs in code (deterministic similarity, at most 3 per fact, at most 30 pairs per question; formula and threshold in links.py, tuned on dev only).
Link judge (Sonnet, batches of 15 pairs): sees both fact statements, quotes, the full source round of each (capped at 3000 characters, marked when cut) and both session dates;
returns replaces | pending | restates | adds | unrelated plus a one-line reason. Status labels derived in code (current, superseded-by, pending-update, disputed); advisory,
nothing hidden. Reader prompt reader-v1.2 unchanged. Question expansion as in v3.

## Operational definitions (fixed now)
- Gold-value fact B: as in GATE.md (the fact in the gold sessions whose object plus quote covers at least 50% of the answer content tokens; ties to the later session).
- Gold update pair: (A, B) where B is the gold-value fact and A is the fact in an OLDER gold session with the highest idf-weighted overlap (slot, predicate and cue-key tokens) with B,
  among asserted or decided facts. If B or A does not exist the pair counts as missing (fails G2 and G5).
- G3 exception as in GATE.md: for questions asking about an earlier value (initially, originally, previously, used to, before, first) superseded also counts if shown.

## Criteria (all must hold)
- G1: FACTS-v4 correct on >= 14 of 16 and at most 1 fewer than ORACLE on the same 16 (Opus judge, benchmark prompts).
- G2: B is in the reader's context on >= 14 of 16.
- G3: B is labelled current or pending-update on >= 13 of 16.
- G4: link precision >= 0.90. Every replaces and pending link (seeded random 80 with seed 16096 if more). An Opus auditor sees the two fact statements, the two FULL source rounds and the two dates
  (capped 3000 chars each, marked), not the judge's reason, and labels the pair replaces | pending | restates | adds | unrelated. A link is precise if the auditor says replaces or pending.
- G5: the gold update pair is linked as replaces or pending on >= 12 of 16.
- G6 (diagnostic, no threshold): of audited links, the share the auditor labels restates.
Each reported as k of n with a Wilson interval, failures by question id, pairs judged per question, tokens and calls for candidate judging per session, and whether batch-16 extraction parsed cleanly.

## Dev stop rule
Iterate on the dev 16 until dev link precision (Opus audit, same auditor) >= 0.90 and the dev gold pair is linked on >= 13 of 15 non-abstention dev questions, or 3 iterations. If not reached in 3, the gate is not run.

## Budget
Cap 520 total; 208 used before this round.

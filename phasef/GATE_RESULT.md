# Phase F v3 gate result (one run, criteria as registered in GATE.md): FAIL on G4

Gate set: 16 pilot-split knowledge-update questions (one is an abstention question, f685340e_abs). Calls this phase: 112 (total 208 of 240).

| Criterion | Result | Pass |
|---|---|---|
| G1 FACTS-v3 correct, and not more than 1 below ORACLE | FACTS3 16/16 (Wilson 0.81-1.00), ORACLE 16/16 (0.81-1.00) | yes |
| G2 gold-value fact in reader context | 14/16 (0.64-0.97); misses: 89941a94 (coverage 0.33, no gold-value fact extracted), f685340e_abs (abstention, no gold value) | yes |
| G3 gold-value fact labelled current or pending-update | 14/16 (0.64-0.97); same two questions | yes |
| G4 audit precision of supersession and pending-update links | 20/32 = 0.625 (Wilson 0.45-0.77); 32 links total (24 supersession, 8 pending-update), all audited (no sampling needed) | NO (needs 0.90) |
| G5 slot-match rate, single state facts in a cross-session slot | before canonicalisation 36/1545 (2.3%), after 53/1545 (3.4%) | diagnostic |

Canonicalisation: 15 of 16 mappings valid (pure); 5831f84d was invalid (one key missing) and fell back to the v2 normaliser as pre-declared. Merges per question: 2 to 9 of about 160 keys.

Gate verdict: not passed (G4). No criterion or prompt was adjusted after seeing results.

Where G4 fails (by link, slot key, no benchmark text): false links are (a) over-merged or unrelated slots: 07741c45 batch.plan.soup, 0e4e4c46 project, 89941a94 podcast and occupation, 50635ada preference.style, 5a4f22c0 coffee.shop.target, f685340e_abs emergency.fund, 45dc21b6 comment.instagram;
(b) restatements of the same value counted as an update: 5831f84d sofa, 5c40ec5b age.niece, 603deb26 brand.gin;
(c) the audit sees only the quotes, and the extractor's quote for the 07741c45 shoe-storage plan does not mention the storage attribute (judged no), although the link itself is the intended one.
Mostly harmless to answers under the advisory design (FACTS3 16/16), but G4 is a registered criterion.

Batch-16 extraction safety: 3 of 3 test batches (16 sessions per call) parsed strictly with every session present; all 42 extraction batches (668 sessions) were complete, none repaired. Output per session fell to 627 tokens (3.55 facts per session) versus 809 (4.5 facts) in the v2 batch-8 run on a different session set, so quality per session at 16 is not proven equal. Input net of overhead 3.6k per session in both.

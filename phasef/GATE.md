# Phase F v3 gate (registered before any gate call)

Source of the gate: main's message in this session (main's choice, not the user's words). The user's approval for volume
extraction on Sonnet is conditional on v2/v3 passing: answer:73716864-6b3b-4deb-94ef-8ed2c8432e4a/45b2a16f-9576-4b70-bcf3-20443a918953.
One run only. No criterion, prompt or rule below changes after results are seen.

## Gate set
All 16 knowledge-update questions of the pilot split (eval loader split, seed 16096). The 2 already extracted (07741c44, 07741c45)
keep their cached v2 extraction; the other 14 are extracted with the v2 prompt unchanged. ORACLE runs on all 16 with the v3 reader prompt.

## v3 design (advisory supersession)
1. Slot canonicalisation: one Sonnet call per question; input is ONLY unique (slot key, cardinality, set of modalities) lines
   (no content, quotes or values). Output is a mapping key -> canonical key where the canonical key must itself be one of the input keys.
   Code validates a pure mapping (every input key exactly once, value in the input keys, nothing else). Invalid -> that question
   falls back to the deterministic v2 normaliser for the gate and is reported (no retry).
2. Status labels, never hiding anything: current; superseded-by (same canonical slot, single, asserted/decided, same scope, later session
   date, different object, author not lower trust); pending-update (a decided/proposed single-cardinality fact in a slot that already has
   an earlier asserted/decided value with a different object; it links to the slot, retires nothing); disputed (same slot, same asserted day or
   session, different objects, both asserted/decided). Superseded facts stay in retrieval and in the reader's context.
3. Reader prompt reader-v1.2 (all arms identical): adds one sentence: items are dated; on conflicting values prefer the most recent
   asserted or decided one, and treat a planned change as possibly completed if a later item implies it.
4. Retrieval unchanged from v2 (lexical + cue-to-cue + slot lookup, top 20 facts + cited rounds), plus one question-expansion call per question.

## Operational definitions (mine, fixed now)
- Gold-value fact: among facts extracted from the question's gold sessions, the one whose object plus quote covers the most answer content tokens
  (at least 50% of them; ties go to the later session). If none reaches 50%, G2 and G3 fail for that question.
- G2 hit: the gold-value fact is among the facts shown to the reader.
- G3 hit: its label is current or pending-update. Exception stated now: if the question text asks about an earlier value (contains initially,
  originally, previously, used to, before, first), a label of superseded also counts, provided it is shown.

## Criteria (all must hold)
- G1: FACTS-v3 correct on >= 14 of 16, and at most 1 fewer than ORACLE on the same 16 (judge: Opus, benchmark prompts, batched).
- G2: gold-value fact in the reader's context on >= 14 of 16.
- G3: gold-value fact labelled current or pending-update on >= 12 of 16.
- G4: every supersession and pending-update link across the 16 (random 60 with seed 16096 if more than 60): an Opus call that sees only the two quotes,
  the two dates and the slot key answers "same attribute, later replaces earlier: yes or no"; precision >= 0.90.
- G5: slot-match rate (single state facts whose slot occurs in 2+ sessions) before and after canonicalisation. Diagnostic, no threshold.
Each is reported as k of n with a Wilson interval and failures by question id.

## Extraction batch-size safety test (budget question)
The first 3 extraction batches of the new sessions use 16 sessions per call. Safe iff all 3 parse without repair and every one of the 16 sessions appears
in each output. If safe, the remaining sessions also use 16 per call, otherwise 8 per call.

## Call budget
Cap raised to 240 total (96 used before this phase). Planned: about 45 extraction, 16 ORACLE, 16 canonicalisation, 16 expansion (2 cached no: all new
recipe keys), 16 reads, about 6 judge, about 3 audit.

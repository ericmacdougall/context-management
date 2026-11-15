# Link rubric (rescue, GoodQA #16096)

A link is an advisory label between two extracted facts from different sessions: LATER replaces EARLIER, or LATER is
a pending (planned) change of EARLIER. Nothing is hidden. This file states how a link is judged correct. It does not
change the v4 auditor: it writes down what that auditor measures and adds one rate.

## Auditor (unchanged from GATE2 G4)
Opus, recipe `linkaudit-v1` (prompt text in `phasef/prompts.py`, `LINKAUDIT_HEAD`). It sees both fact statements, both full
source rounds (capped at 3000 characters, marked when cut) and both session dates, never the judge's reason, and gives
one label for LATER relative to EARLIER:

| label | meaning |
|---|---|
| replaces | same attribute of the same entity, different value, LATER gives the new state |
| pending | LATER is a plan or intention to change that attribute |
| restates | same value in other words |
| adds | both hold; a different item or event |
| unrelated | different attribute or entity |

- **Precise link**: the auditor says replaces or pending. Link precision = precise / audited.
- **Harmful link**: the auditor says unrelated or restates. These mislead a reader: an unrelated attribute shown as
  superseded, or a restatement shown as a change. Harmful-link rate = harmful / audited.
- A link the auditor labels adds is imprecise but not counted as harmful (both values still hold; the label overstates).

## Is the auditor reliable enough to carry a 0.90 bar? (measured, rescue step 1)
The final v4 dev link set (24 links) was audited three times: the original audit (A0), a seeded shuffle with the same
rubric (A1), and a seeded shuffle with an equivalent, reworded rubric (A2, recipe `linkaudit-v1-alt`, text in
`phasef/rescue_v5.py`). Precise/not agreement: A0-A1 24/24, A0-A2 23/24, A1-A2 23/24. The one flip is a plan for one
video versus a different video topic (adds, adds, replaces). The author's own reading, written before the re-audits,
agrees with the majority on every link it marked clear (18 replace or pending, 4 not), and the two links it marked
ambiguous (a 12th computer-science video versus 15 videos in recent weeks; guitar lessons versus a plan to find a
teacher) were accepted by all three audits.

Self-agreement is above 0.90, so no ambiguity exclusion is introduced: one audit per link set, as before. At n=24 one
flip moves precision by about 4 points, which is the resolution of this measurement.

## Known property of the data (not a rubric change)
LongMemEval haystack filler sessions come from different simulated users. Contradictory "current" values across filler
sessions (Kansas City, then the San Francisco Bay Area, then Chicago within weeks) are precise links under this rubric
although no single person changed anything. They are harmless to answers about the question's own topic but they are
noise a real single-user history would not have.

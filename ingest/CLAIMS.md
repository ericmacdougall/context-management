# Ingestion from the model port: claims and their denominators

Source of figures: `ingest/results3/` (one stamped run, commit `191bded`, clean tree, stubs only, SQLite stand-in, no model calls),
recomputed from stored rows by `ingest/recompute.py` and `ingest/recompute2.py` (0 mismatches). Registration: `PREREG-amend-3.json`.
Earlier runs (`results/`, `results2/`) stay as history; where they differ, `results3` governs. Every line names what it does not cover.

## What the evidence supports

| Claim | Denominator and result | Not covered |
|---|---|---|
| A crash at one of the named hooks leaves a log from which the expected event set is ingested | 40 of 40 runs of the named-hook matrix (5 hooks per mode, 4 provider and stream modes, call 1 and call 3), each a real process kill | Hooks not in the matrix; kills inside SQLite; power loss |
| In 4 of those 40 runs a response the stub completed is absent from memory | 4 of 40, all at `after_send_before_resp_log` (non-stream, both providers, both call numbers): the real tap has not yet written the response when the process dies; the call is reported unconfirmed, not lost silently | Recovery of the response (no provider documents retrieval; interface only) |
| The late-tap control loses a completed response | 20 of 40 runs lose a stub-completed response; 8 of 40 lose one the host had received | |
| Duplicated, reordered, concurrent delivery gives the same events as one in-order ingest AND the events the host expects | 20 of 20 seeds per provider (delivery), 10 of 10 (threads), 5 of 5 (processes), 10 of 10 (stale zombie), each equal to the baseline and to the oracle built at the host from the scripted conversation | Production Postgres; hosts other than the scripted ones |
| The race control is sensitive | all four layers off (fence, chain, unique key, state guard): 10 of 10 runs with duplicate rows (floor 5). The 3-layer control stays registered as a failure of the original floor (0 of 10) | Which single layer matters: `only_fence` and `only_state_guard` each give 10 of 10 identical runs |
| The oracle sees what baseline comparison cannot | with a control ingester that never writes thinking events, arm equals baseline in 20 of 20 runs and the oracle flags 20 of 20 | |
| Repeated or alternating user text with a sliding window is ingested once per call | 18 of 18 runs (3 patterns, windows of 4, 6 and 8 messages, 2 providers, 20 calls), equal to the oracle; 0 blocking gaps. All 18 carry an `ambiguous_alignment` flag (content alone cannot say which message is new) | Hosts that append more than one new message per call under repeating content |
| No false ambiguity on unique text | 0 of 4 unique-text runs (item 13) and 0 of 12 (item 11) carry the flag | |
| An identical resend that the host does not declare is ingested again, flagged | 20 of 20 probes: second user event has `ambiguous_repeat`, `completeness().ambiguous_calls` lists the call | It may be a true duplicate; the flag, not the ingester, says so |
| A host-declared retry suppresses the repeat visibly | 20 of 20: one user event, a `declared_retry` event; a retry claim for a call whose keys differ is ignored and recorded, 20 of 20 | The declaration is trusted (ASSUMPTION: honest host) |
| Injected memory block never lands in a user, tool or system event in covered placements | 0 marker occurrences in 42 conversations (6 covered placements, 7 each; 35 of them carry a canary shown to have reached the model). Role-only control: 21 of 28 merged-block runs | The seventh placement, `host_alters_block`, fails: 7 of 7 leak into a user event (documented limit). Registered n was 40 per placement; 7 were run (see deviation) |
| Truncation without a witness is not detected | 0 of 20 detected; 8 of 20 report complete (the 12 odd cuts end with an outstanding request and are refused) | |
| Scanner, re-scored under complete redaction | held-out 300 of 800 (37.5%, Wilson lower 34.2%); the 0801866 predicate (whole string absent) gave 350 of 800 (43.75%); tuned set 400 of 400 under both | See below |
| Scanner false positives on a benign synthetic set | 0 of 600 (12 classes) | Not a deployment rate |

## Scanner figures (F2 and F6)

`private_key_header` scored 50 of 50 under the old predicate and 0 of 50 under complete redaction: the frozen scanner redacts the
header line and leaves the key body. That is a scanner defect, not only an oracle defect; `scan.py` was not changed here, because a
changed scanner would burn the held-out set and needs a fresh one built by a rule fixed beforehand. The figure is recall on an
equally weighted, positive-only, synthetic set; it is not precision and not deployment recall. Per-example rows are stored (booleans
and an integer fragment length only). The repository cannot show the order in which the scanner, the generator and the held-out
set were made, nor that the generator was independent of tuning: this is asserted by the previous owner, not demonstrated.

## Deviation: provenance sample size

`PREREG.json` item 4 registered 40 conversations per placement (quote-back 40/40, forged-block 40/40, 0 of 40 markers). The run used
7 per placement (49 conversations). Neither earlier amendment said so. The registered thresholds at n=40 are not claimed; measured
are 7 of 7 quote-back flagged, 7 of 7 forged block stays user, 0 markers in 42 covered conversations. Not rerun at 40 here.

## Retry identity (F3): the limit

The stub tap has no signal that separates a retry, a regeneration and a legitimate repeat from content alone: request ids are fresh per
call, positions slide with the window, hosts copy message objects, and delivery to the host is not observable. At 0801866 this dropped
user turns silently (10 calls of "yes" with a 4-message window gave 3 user events; alternating yes/no gave 4 of 12) while completeness
read complete. Now the final message of a request is never suppressed without a host-declared `retry_of`; ambiguity is reported.
"Exactly once" therefore holds only as: every message sent is ingested at least once, and an undeclared identical resend is a flagged
possible duplicate.

## Not verified here

Production database concurrency; real providers; host integration; the on-disk check of item 5 uses the same whole-string predicate
as the old scanner oracle (not re-scored); latency and cost items (6, 8, 9) were measured on a loaded Mac and are indicative.

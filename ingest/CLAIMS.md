# Ingestion from the model port: claims and their denominators

Source of figures: `ingest/results4/` (one stamped run, code commit `6989e79`; the ingester was changed afterwards only for undeclared requests with no overlap or an unseen leading message, and items 3, 4, 7, 10, 11, 13 and 13c were rerun unstamped on the changed code with identical summaries, clean tree, stubs only, SQLite stand-in, no model calls),
recomputed from stored rows by `ingest/recompute.py` and `ingest/recompute2.py` (0 mismatches both). Registration: `PREREG-amend-3.json`,
`-amend-4.json`, `-amend-5.json`. Earlier runs (`results/`, `results2/`, `results3/`) stay as history; `results4` governs. Each line names the
hosts and sets it was measured on and what it does not cover. There is no universal claim in this file.

## Trusted-host assumptions (read first)

- `retry_of` and `new_count` are declarations by the host. The ingester can check only that `retry_of` names the immediately preceding
  confirmed call with identical message keys and that `new_count` is an integer within the request length. It cannot check that they are
  true. A wrong `new_count` drops or duplicates messages; a false `retry_of` that passes the key check suppresses a real repeat. Every use
  is visible: a port event, `completeness()["declared_calls"]`, and `declared`/`new_count`/`retry_of` in the meta of that call's events.
- Without a declaration the ingester accepts content alignment only when it is provably the only explanation: a unique maximal run that
  starts the request (no unaligned message before it) and ends at the end of the ingested history, every unaligned message unseen; or a
  one-message request with unseen content. A request with several messages and no overlap with history, or with an unseen message before the
  aligned run, is NOT accepted (final-gate F2): every message keeps its original provenance. Otherwise it emits everything that could be new, flags it, opens a
  blocking `ambiguous_alignment` gap, and `claim_complete()` and `claim_exactly_once()` are false for that stream.
- A stateless or repeating host that cannot declare its boundary therefore gets a superset of events plus a blocking gap, never silent loss.
- A message can be recorded as host-authored (for example a compaction summary) only when the host declares `new_count`; undeclared, it is
  ingested with its original provenance and the stream is blocked.

## What the evidence supports

| Claim | Denominator and result | Not covered |
|---|---|---|
| A crash at one of the named hooks leaves a log from which the expected event set is ingested | 40 of 40 runs of the named-hook matrix (5 hooks per mode, 4 provider and stream modes, call 1 and call 3), each a real process kill | Hooks not in the matrix; kills inside SQLite; power loss |
| In 4 of those 40 runs a response the stub completed is absent from memory | 4 of 40, all at `after_send_before_resp_log` (non-stream, both providers, both call numbers); the call is reported unconfirmed | Recovery of the response (interface only; no provider documents retrieval) |
| The late-tap control loses a completed response | 20 of 40 runs lose a stub-completed response; 8 of 40 lose one the host had received | |
| Duplicated, reordered, concurrent delivery gives the same events as one in-order ingest and the events the host expects | scripted conversations of 30 calls with text, thinking and tool turns, unique user text: 20 of 20 seeds per provider (delivery), 10 of 10 (threads), 5 of 5 (processes), 10 of 10 (stale zombie), each equal to the baseline AND to the host oracle, which now includes tool names, arguments, results and thinking signatures | Repeating-content hosts (separate rows below); production Postgres |
| The race control is sensitive | four layers off (fence, chain, unique key, state guard): 10 of 10 runs with duplicate rows (floor 5). The 3-layer control stays a registered failure of the original floor (0 of 10) | `only_fence` and `only_state_guard` each give 10 of 10 identical runs |
| The oracle sees what baseline comparison cannot | a control that never writes thinking events: arm equals baseline 20 of 20, oracle flags 20 of 20 (item 2f) | |
| The oracle sees payload loss that keeps ids | a control that keeps every tool id and empties arguments and results: arm equals baseline 40 of 40 and the old id-only projection matches in 40 of 40, the payload oracle flags 40 of 40 (item 2g, 20 seeds x 2 providers) | Payloads other than tool arguments, tool results and thinking signatures |
| A host that declares `new_count` is ingested exactly, whatever the content | 18 of 18 runs of the repeated-content hosts (all-same, alternating, three-cycle text; windows of 4, 6, 8 messages; 2 providers; 20 calls; 1 new message per call), 8 of 8 runs with 2 or 3 new messages per call (same/alternating text, windows 6 and 8, 2 providers, 12 calls), each equal to the oracle, 0 blocking gaps, `claim_exactly_once` true | The declaration is trusted; hosts with other patterns |
| The same hosts without a declaration lose nothing and are blocked | 18 of 18 and 8 of 8 runs contain every oracle event; 18 of 18 and 8 of 8 carry a blocking gap and `claim_exactly_once` false; the cost is duplicates: 1,884 extra flagged events over the 18 runs (about 105 per run) and 588 over the 8 | Consumers must treat these streams as at-least-once with flagged repeats |
| No false ambiguity on unique text | 0 of 4 unique-text hosts (item 13, constant model answer, windows 4 and 8) and 0 of 12 (item 11: windows undeclared; compaction hosts declare `new_count=1`) carry a gap; both equal the oracle | |
| The reviewer's case (history `yes`,`ok`; then a request `yes`,`new`) | without a declaration: all three user events present, flagged, blocking gap, both claims false; with `new_count=2`: exact, claims true (tests, not a rate) | |
| An identical resend without a declaration is a flagged duplicate and blocks | 20 of 20 probes (2 providers x 10 seeds): second user event flagged, call listed in `ambiguous_calls`, `claim_exactly_once` false | It may be a true duplicate |
| A declared retry suppresses visibly; a false claim is ignored and recorded | 20 of 20: one user event, a `declared_retry` event, call in `declared_calls`, response events marked `declared`; 20 of 20 retry claims for a call whose keys differ ignored and recorded | The declaration is trusted |
| Injected memory block never lands in a user, tool or system event in covered placements | 0 marker occurrences in 42 conversations (6 covered placements x 7; 35 carry a canary shown to have reached the model); quote-back flagged 7 of 7, forged block stays user 7 of 7. Role-only control: 21 of 28 merged-block runs. Hosts declare `new_count=1` (their user text repeats each turn) | The seventh placement, `host_alters_block`, fails: 7 of 7 leak into a user event. Registered n was 40 per placement; 7 were run (deviation below) |
| Truncation without a witness is not detected | 0 of 20 detected; 8 of 20 report complete (the 12 odd cuts end with an outstanding request and are refused) | |
| On-disk persistence of secrets (item 5) | covered runs: 96 of 96 with no whole-string needle and 0 of 96 with any 8-character fragment of any needle on disk, per provider; the scan-after-write control: 96 of 96 (whole-string 96 Anthropic, 92 OpenAI; fragment 96 both) | 14 expected-miss runs per provider (rot13, split across two blocks) still leave fragments; thinking signatures are exempt |

## Scanner (F2)

`ingest/scan.py` was repaired after the review (whole private-key blocks, with or without a footer; vendor tokens; token and JWT formats;
URL credentials; `sk_`/`sk-`; base32, quoted-printable, HTML entities, octal escapes, separators between characters, line-wrapped base64,
reversed text). The old held-out set and the tuned set were used for that repair and are **burned**; the fresh held-out set was fixed by a
rule committed before the repair (`PREREG-amend-4.json`, `heldout2.py`, seed derived from the base commit id) and scored once.

| Set | Complete redaction (no 8-character fragment of any sensitive string survives) | Wilson 95% lower bound |
|---|---|---|
| Fresh held-out, 24 cells x 40 | 800 of 960 (83.3%); detected 800; whole-string-absent 800 | 80.8% |
| Tuned set (burned) | 400 of 400 | 99.0% |
| Old held-out (burned, scored 300 of 800 by the scanner at add7c86, 350 of 800 under the old predicate) | 800 of 800 | 99.5% |

Fresh held-out failures: four cells at 0 of 40: a bearer header and a private-key block with a dot or a tab between every character.
Every other cell is 40 of 40. Fresh benign set (18 classes x 40): 0 of 720 changed. All sets are synthetic and positive-only for recall;
equal weight per cell; this is not precision beyond that benign set and not deployment recall. The scanner was not edited after the fresh set
was scored; a later performance-only change (memoised decoding) was checked to return identical findings on 2,883 texts
(`results4/scan-equivalence.txt`) and the rerun reproduced the fresh figures exactly (`PREREG-amend-5.json`). The repository records commit
order but cannot demonstrate that the earlier generator was independent of tuning.

## Deviation: provenance sample size

`PREREG.json` item 4 registered 40 conversations per placement. The run used 7 per placement (49 conversations). Neither earlier
amendment said so. The registered thresholds at n=40 are not claimed; measured are the counts above. Not rerun at 40.

## Retry identity and alignment (F3, N1): the limit

The stub tap has no signal that separates a retry, a regeneration and a legitimate repeat from content alone: request ids are fresh per
call, positions slide with the window, hosts copy message objects, and delivery to the host is not observable. At 0801866 this dropped user
turns silently while completeness read complete; at add7c86 a newly authored message that repeated old content could still vanish with
`claim_exactly_once()` true. The contract above (declared boundary, otherwise block) replaces inference where inference is not provable.

## Scanner note (amend-6)

`scan.py` at 45f3413 held a dead duplicate block (a stray first `ReferenceScanner` and a verbatim copy of the patterns), left by a text splice.
It held no pattern that was meant to be live; the live class was always the second. It was removed in its own commit with identical findings
on 2,883 texts (`results4/scan-dedup-equivalence.txt`). No pattern was enabled or changed after the fresh set was scored.

## Not verified here

Production database concurrency; real providers; host integration; hosts with other repeating patterns or declarations that are wrong;
payloads beyond those the oracle compares; latency and cost items (6, 8, 9) were measured on a shared, loaded Mac and are indicative
(item 9 ratios 1.10 and 1.20 persistent, medians about 1.5 ms).

## Open items before a client deployment

- Scanner gap: four fresh held-out cells at 0 of 40 (a bearer header and a private-key block with a dot or a tab between every character); the scanner was not edited after scoring. The scanner is a reference implementation; the client's scanner must be measured on its own held-out set.
- Provenance at the registered n=40 per placement (7 were run); the altered-block placement `host_alters_block` still leaks into user events and needs a constrained integration contract or a repair.
- Real provider runs (no provider documents idempotent retrieval of a response, so an unconfirmed call stays unconfirmed) and real database runs: everything here is stubs and SQLite; production Postgres concurrency, migration, rollback and durability are untested.
- Trusted-host assumptions: `retry_of` and `new_count` are declarations the ingester cannot verify; a host that cannot declare its boundary gets duplicates and a blocking gap.
- Payloads outside tool arguments, tool results and thinking signatures are not compared by the oracle; timing figures are indicative on a loaded machine.

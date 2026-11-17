# Independent review reports (elastic memory lab, ticket goodqa#16096)

What these are: code-inspection reports written by an independent model (Codex gpt-6.1-sol; the effort and rung are report-specific and
are given in the Reviewer column below, one rung above the author in each case) from an isolated committed snapshot at the commit named below. They are not attestations or sign-offs. Unless a
report says otherwise, no live system, database or network service was exercised: the reviewers read code, committed result files
and logs, and recomputed figures from committed rows. Only the reviewer's report file is committed here, not the review packets,
run logs or test outputs. Each report names its reviewed HEAD and packet SHA-256 in its header.

How to read the verdict column. The first token in a report header (`BLOCK`, `ACCEPT`, `PASS`) is the raw result of that run; the
reviewer's use verdicts are in the body as "LAB" and "CLIENT". Almost every report says LAB PASS WITH LIMITS (or BLOCK as written, for
the first X2 reviews) and CLIENT BLOCK. No report establishes deployment readiness of the composed client system; only the certificate-cache and vacuous-test reports give CLIENT PASS WITH LIMITS, and only for their narrow, inspected scopes. Fixes made after a stream's last review are main-checked, not
re-reviewed, and the status tables in DESIGN.md say so.

| Report | Reviewer (effort, rung; author rung) | Stream | Commit reviewed | Packet (first 12 hex) | Run id (where recorded) | Raw result | Lab / client verdict in the body | Date |
|---|---|---|---|---|---|---|---|---|
| eval/review-2.md | gpt-6.1-sol medium, R1; author R0 | Stage X2 (first review) | 5bb9c5a | 8de04b2f53ff | not recorded here | BLOCK | LAB BLOCK as written; CLIENT BLOCK | 2026-11-16 |
| eval/review-3.md | gpt-6.1-sol medium, R1; author R0 | Stage X2 (second reading of the same commit) | 5bb9c5a | 8de04b2f53ff | not recorded here | BLOCK | LAB BLOCK as written; CLIENT BLOCK | 2026-11-16 |
| eval-x2delta/review-1.md | gpt-6.1-sol medium, R1; author R0 | Stage X2 delta | be746a0 | bd00cd7ba029 | not recorded here | ACCEPT | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| eval-espilot/review-1.md | gpt-6.1-sol medium, R1; author R0 | ES-pilot (path item 9) | be746a0 | 064231f57b2e | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| eval-espilot-delta/review-1.md | gpt-6.1-sol medium, R1; author R0 | ES-pilot delta | 275f744 | 5a2bc9aa7c44 | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| ingest/review-2.md | gpt-6.1-sol medium, R1; author R0 | Ingestion | 0801866 | 99f7e8d2e64b | not recorded here | BLOCK | LAB BLOCK; CLIENT BLOCK | 2026-11-16 |
| ingest-delta/review-1.md | gpt-6.1-sol medium, R1; author R0 | Ingestion delta | add7c86 | 0b8d2632a614 | fa23586d | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-16 |
| licence/review-2.md | gpt-6.1-sol medium, R1; author R0 | Licence rehearsal | cc45153 | 99abdbf9fba7 | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-16 |
| licence-delta/review-1.md | gpt-6.1-sol medium, R1; author R0 | Licence delta | fad672c | f888f0e1c30a | bd46561e | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-16 |
| policy/review-3.md | gpt-6.1-sol medium, R1; author R0 | Policy rescue | 0a20b64 | aa922195d475 | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| policy-delta/review-1.md | gpt-6.1-sol medium, R1; author R0 | Policy delta | d05d731 | 2a7ae600a2bb | ad27751f83249e1d | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| policy-final/review-1.md | gpt-6.1-sol xhigh, R3; author R2 | Policy final (P1 to P4) | a2d65fd | b09706e661eb | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| writepath/review-2.md | gpt-6.1-sol medium, R1; author R0 | Write path step 1 | 1fcf3f7 | 7669401b74ab | c32d9877a217a13f | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK | 2026-11-17 |
| writepath-delta/review-1.md | gpt-6.1-sol xhigh, R3; author R2 | Write path step 1 delta | 3c2864f | 5a084aa85bc0 | not recorded here | BLOCK | LAB BLOCK for issuing validity stamps with the current gate (P1, P2); CLIENT BLOCK | 2026-11-17 |
| pr/ledger-review-3.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 5 (ledger), second run | e2b5f1e | cfcf95203757 | not recorded here | BLOCK | gate verdict; F1 and F4 partly closed, F6 to F8 new | 2026-11-17 |
| deploy-certcache/review-1.md | gpt-6.1-sol medium, R1; author R0 | Deploy path item 8: certificate cache | 55bafce | b52d6ffb66f1 | not recorded here | ACCEPT | LAB PASS WITH LIMITS; CLIENT PASS WITH LIMITS | 2026-11-17 |
| deploy-vacuous/review-1.md | gpt-6.1-sol medium, R1; author R0 | Deploy path item 8: vacuous test | 9465ed8 | 6aa300bff328 | not recorded here | ACCEPT | LAB PASS WITH LIMITS; CLIENT PASS WITH LIMITS (test-only change) | 2026-11-17 |
| deploy-downgrade/review-2.md | gpt-6.1-sol medium, R1; author R0 | Deploy path item 8: downgrade guard | 181d331 | 08f26930eb76 | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK (F1 to F3) | 2026-11-17 |
| deploy-poison/review-2.md | gpt-6.1-sol medium, R1; author R0 | Deploy path item 8: poisoned inputs | 801a3d2 | 5d96a7883909 | not recorded here | BLOCK | LAB BLOCK for the acceptance flows (F1, F2); CLIENT BLOCK | 2026-11-17 |
| deploy-reprobe/review-1.md | gpt-6.1-sol medium, R1; author R0 | Deploy path item 8: start-up re-probe | f80e34d | 05e983368e54 | not recorded here | BLOCK | LAB PASS WITH LIMITS; CLIENT BLOCK (F1 to F3) | 2026-11-17 |
| pr/ingest-review-3.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 3 (ingest), second run | 321ce74 | 492dde64cf8d | not recorded here | BLOCK | gate verdict; F2 material (silent loss of user provenance on undeclared requests) | 2026-11-17 |
| pr/ingest-review-4.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 3 (ingest), third run | 5ed88ce | c5247f43a2d2 | not recorded here | PASS | gate verdict; PR 3 then merged | 2026-11-17 |
| pr/licence-review-2.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 4 (licence) | 813c1dd | 3cdcb88c1c62 | not recorded here | BLOCK | gate verdict; unchecked bulk and refresh failures; fix at 4eb4378 | 2026-11-17 |
| pr/eval-review-3.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 2 (eval), first run | 45d9ec5 | b45147ac43d1 | not recorded here | BLOCK | gate verdict; no lab/client use verdicts | 2026-11-17 |
| pr/ledger-review-4.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 5 (ledger), third run | 12a3c88 | a6193d0a181c | not recorded here | PASS | gate verdict; minors F4, F8 to F10; PR 5 then merged | 2026-11-17 |
| pr/eval-review-4.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 2 (eval), second run | 45d9ec5 | 1b284f4802dd | not recorded here | PASS | gate verdict; PR 2 then merged | 2026-11-17 |
| pr/ingest-review-2.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 3 (ingest) | d254fa4 | c093e13a8263 | not recorded here | BLOCK | gate verdict; no lab/client use verdicts | 2026-11-17 |
| pr/ledger-review-2.md | gpt-6.1-sol medium, R1; author R0 | Pull request gate, PR 5 (ledger) | a598931 | 952baf8b63f9 | not recorded here | BLOCK | gate verdict; findings F1 to F5 | 2026-11-17 |

Not committed here as review reports: the coordinator's check scripts, which are under reviews/coordinator-checks/ with their own README, and result summaries. The deploy-path rows are listed
for completeness; their fixes are owner-reported and their delta reviews are pending. A later gate report is added here when it
exists.

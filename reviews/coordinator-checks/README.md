# Coordinator check scripts

These are the scripts the coordinator (session 500523cf, "main") wrote and ran itself to check work between independent reviews.
They are copied unchanged from the coordinator's work directory (`verify/`). They contain absolute local paths and a few
synthetic values (a random block used to build a fake private key at run time); they contain no real secret. They are NOT
independent reviews: the coordinator is not independent of the lab work. Each one establishes only what is stated here.

| File | What it does | What it does NOT establish |
|---|---|---|
| test_ledger_pr.py | Eight consistency assertions over the ledger documents (the only files changed versus the base are the allowed documents; key figures and ASSUMPTION labels are present; no secret-shaped strings). Its "only docs changed" test names the base commit b50f9c8, so it fails on a branch that has since merged lab/integration (eval/) and added reviews/. | That any figure is true: it checks that the documents agree with their own regexes, not with the underlying result files. |
| probe_espilot.py | Applies 13 tampering attacks (drop or rename a question, collapse clusters, swap level labels, remove cross-encoder records, infinite score, with and without re-stamped hashes) to temporary copies of the committed ES-pilot rows and reports the scorer's category for each, for the old scorer and the fixed one. | That the pilot run itself was honest or blind; the manifest authenticates files against the commit, not that the run produced them. It reads rows in the coordinator's worktree, not this repository. |
| probe_n1.py | Reproduces the ingestion delta reviewer's N1 case (history yes, ok; then a request yes, new, with no declaration) against the ingester and prints the user events, blocking gaps and the exactly-once claim. | That ingestion is correct in general, or any rate: it is one case. The ingest code is on feat/port-ingest (not yet merged into lab/integration), so the script cannot run from this branch. |
| probe_scan.py | Builds a synthetic private-key block (random body, header and footer assembled at run time) and checks whether any 8-character fragment of the body survives redaction by the ingestion scanner. | Scanner recall: it is one synthetic block. It needs the ingest package from feat/port-ingest. |

The outcomes these scripts produced are recorded in the coordinator's log and quoted in LEDGER.md as coordinator-checked, not independently reviewed.

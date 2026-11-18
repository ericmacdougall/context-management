# Live window 2 on the lab host, tree pinned at 27094c9 (2026-11-18, 18:13:26Z to 18:35:51Z)

Unstamped live recheck (GoodQA #16096). Driver: `results/failing-first/harness/live_chain.sh` at 27094c9, run as
`sh live_chain.sh src-27094c9 src-179412e src-6ddeca3 <out>` with `XB_TREE=src-bca7764`. It held the run lock
throughout and started at 22753428 KiB free (start line 20971520 KiB). `chain.log` is the driver's own log. Each
`<step>.out` file is the full output of one step. Home paths are written as `~`.

| step | what | result |
|---|---|---|
| G1 | tests.test_ident | Ran 78, OK |
| P0 | wp.setup --reset | ok |
| G2 | tests.test_d3_pg, live (B1-B8, S1-S9, H16a/b) | Ran 37, OK, 0 skipped |
| G3 | tests.test_schema_broker_pg, live | Ran 3, OK |
| PC | DEVIATION: seeds the P1 plaintext positive control in database emlab (empty on this cluster) | seeded 1 |
| P1 | exp.registry_marker | PASS, fenced 10/10, controls violated 10/10 (`registry_marker.ba2a91777bdb0029.json`) |
| P2 | exp.d4_store_mark | pass (`d4_store_mark.7fb9f436e8cb83da.json`) |
| RR | registry.unregistered() with registry.py at 179412e, then at 27094c9 | `['pg:seal_keys']`, then `[]` |
| RS | HEAD tests on the pre-seal schema (179412e): F14 before | FAILED, failures=8, errors=1 (red, as required) |
| RH | HEAD tests on the pre-H schema (6ddeca3): F16 before | FAILED, failures=12, errors=7 (red, as required) |
| XB | tests.test_d3_pg from bca7764 (review H tests on 6ddeca3 code) | FAILED, failures=4, errors=8 (red, as required) |
| SS | seal_socket_e2e.py, same OS uid as the app | PASS. Key isolation is NOT shown by this step |
| SU | seal_uids_e2e.py: sealer uid 20005, app 20001, peer 20002, keysvc 20003 | PASS, K1 to K7 |
| D6 | es_security enable, licence, exp.h_d6_broker 5 (amendment H6c) | licence basic active, feasible: true. Development observation only; see below |
| LS | runkit/leak_scan.py | 1 file hit (D6.out, 4 matches); see below |

## The leak scan hit and the D6 receipt

- **Which values matched:** classified by field name, without showing any value. All four matches were the `id` field
  of the four role key files (`secrets/es_api_key{,.projector,.eraser,.verifier}.json`).
- **What printed them:** `wp.es_security enable` printed them through `es_keys.create()`.
- **What did not match:** no key value, no password and no pgpass line.
- **Redaction:** line 1 of `D6.out` was redacted in place on the lab host before this copy was made.
- **Rescans on the lab host:** both live directories plus the results, logs and state directories, 890 files, 0 hits.
  The staged copies here, 29 files, 0 hits.
- **Search on the Mac:** a hash-only search of the Mac-side copies found none of the four ids.
- **Fix:** ba46276. `create()` now reports sha256 fingerprints and rotates each role key. It has a failing-first test,
  with receipts in `../leakscan/`.
- **D6 receipt:** the receipt is the rerun from the ba46276 tree (`../live-ba46276/` once it has run). The D6 result
  here stays a development observation.
- **`h_d6_broker.caddcc4c93150c59.json`:** carries the ids of keys the broker minted and invalidated during the run.
  This is by design: they are ids only, not in any secret file, and the scan is clean.

## Limits

- SU separates service identities on one host. Docker access on that host is root-equivalent, so it is not host
  isolation. There is one Postgres. The authority trusts every allowed peer uid for every tenant.
- PC is a seeded grep-sensitivity control. It is not the old code's write path.
- Every run in this window is unstamped. No stamped run was started.

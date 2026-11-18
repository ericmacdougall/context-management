# Every path to VALID or exit 0 (GoodQA #16096, final gate round; review aacf3c0)

Draft 1, written BEFORE the fixes of this round, from the code at aacf3c0. It is the plan the round works to: each
path, the requirement conjuncts it must establish, the check that establishes each (or the gap), and the test that
violates only that conjunct. Draft 2 (later in this branch) replaces the "aacf3c0" column with the code as fixed and
names every test.

## The requirement a VALID run must meet (its conjuncts)
| Id | Conjunct |
|---|---|
| C1 | One run at a time: one live launcher holds the run lock from before admission to after the end record |
| C2 | What ran is the pinned commit: tree equal to its manifest at admission and at end, the gate is the commit's gate, the interpreter is isolated from the caller's environment |
| C3 | Fixtures are the registered ones (P6) |
| C4 | Stores clean before the run (rows, indices, unregistered stores, log files) |
| C5 | Stores exclusive and intact for the whole run: no reset or cleanup by another command, not replaced |
| C6 | Live schema, guards, grants, roles and memberships equal the committed expectation at admission, and unchanged at end |
| C7 | Every v4 storage rule (tmpfs, limits, swap, OOM in the stores, identity and settings drift) |
| C8 | Rows bound to the run: every file in the run directory listed with its hash; each JSON row carries this run id and a time in the window |
| C9 | The experiment ended by itself: not killed by a signal the gate did not send, no OOM kill in its cgroup; no process of its group outlived it |
| C10 | Ledger intact; no stop in the window; exactly one end record for the run |
| C11 | Host disk: >= 10 GB free at admission (amendment GF1), never under 8 GB during the run, own growth <= 2 GB |
| C12 | No container other than the two stores uses this home (bind mount under WP_HOME) at admission or at end |

## Paths at aacf3c0 and the gap in each
| # | Path | Conjuncts it must establish | Established at aacf3c0 by | Gap (finding) |
|---|---|---|---|---|
| 1 | `disk_gate run` -> `_report` returns 0 -> `sys.exit(0)` | C1-C12 | C1 flock + pass_fds; C2 `_tree` (no isolation check); C3 `_fixtures`; C4 `_clean`; C7 `_slow/_fast/_rules/_drift`; C8 `_run_files`; C9 P11 `_group_members` only; C10 `_ledger_why`; C11 START_GB 20 and cap | C5 none (F-1: reset/cleanup outside the lock; no identity check at end); C6 none (F-3); C9 exp_rc not judged (F-2), child OOM unseen; C11 no in-run stop; C12 none (F-4); C2 interpreter not isolated, WP_* env unrecorded (m-G); special files crash or hang (m-B); whole-file reads (m-C); a pre-existing group member VOIDs (m-I) |
| 2 | `disk_gate verdict RUN` exit 0 | C10 (exactly one end, VALID) | `end[0].verdict == VALID` | more than one end record not refused (m-H); a forged first end wins |
| 3 | `_finish` with the run already closed: exit 3, nothing written | C10 | reason added, no end record | the ledger keeps only the earlier (possibly forged VALID) end (m-H) |
| 4 | `disk_gate abort` exit 0 | writes VOID only | lock free + group empty + v5 | none (never VALID) |
| 5 | `disk_gate preflight` exit 0 | C4 | `_clean` | not a stamped path; fine |
| 6 | `init`, `stop`, `stops`, `clear-stop`, `ledger-reset`, `retire-legacy-stop`, `version` exit 0 | none (no verdict) | - | none |
| 7 | `chain_singles.sh` exit 0 | each stamped line's gate exit 0; at least one line ran | gate status per line | missing spec runs nothing and exits 0 (m-D); last line without newline dropped (m-D); reset outside the lock (F-1) |
| 8 | `launch_one.sh` exit 0 | the gate's status | `exec` | interpreter not isolated (m-G); no reset at all, so a leftover patch is admitted (F-3) |
| 9 | `pin_run.sh` exit 0 | the pinned tree is complete | `set -e` on the extracting tar only | a failed archive is masked (m-J) |
| 10 | `sync.sh` exit 0 | the commit's files shipped to the named home | tree_manifest ship + rsync | remote home hard-coded to the write-path owner's (live safety) |
| 11 | `stage_results.py` exit 0 | VALID end, listed hash, cleared set, gate-attested commit, no overwrite | hash chain + head; one end VALID; listed hash | header-only input overwrites MANIFEST.tsv; code_commit from the row; group path unchecked; exp_rc ignored; clearance not checked (m-E, H-9) |
| 12 | `clearance.py seal` exit 0 | stores proven, key store covered, scan 0 | `_store_reasons`, `_keystore_reasons`, scan | none found this round |
| 13 | `clearance.py teardown` exit 0 | sealed containers gone, credentials removed, same daemon | as coded | none found this round |
| 14 | `clearance.py stage-check` PASS exit 0 | receipt valid, set equal, containers gone, keystore covered | as coded | none found this round |
| 15 | `clearance.py verify-copy` PASS exit 0 | a PASS check bound to the receipt, set equal, key store covered, daemon recorded, receipt v4+ | check + manifest | a v3 receipt sealed with `--no-keystore` passes (C-3 remainder, m-F) |

## Plan (what each gap becomes)
- F-1: the gate takes the lock, then runs `exp.cleanup` and `wp.setup --reset` itself (inherited lock fd); both tools
  refuse while a run holds the lock; at end the PG database, schema and table identities (OIDs) must equal admission's.
  ES: a direct index delete by a process that ignores the lock is not detectable without a marker inside the stream's
  prefix (which the registry and the experiments would see): LIMITS.
- F-2: VOID when exp_rc < 0 and the gate sent no signal; VOID when the gate's own cgroup memory.events oom_kill grew;
  BLOCKED when that file cannot be read; stage_results refuses exp_rc not an int >= 0.
- F-3: a read-only catalog snapshot (roles, attributes, memberships, role and database settings, ALTER SYSTEM entries,
  databases and their ACLs, parameter ACLs; per database: schema, tables, ACLs, triggers and enabled state,
  functions, constraints, indexes, default ACLs). Before the reset: cluster rules (BLOCK; the reset would repair role
  attributes silently). After the reset: cluster and schema rules (BLOCK). At end: rules plus equality with the
  admission snapshot (VOID).
- F-4: C12 at admission (BLOCK) and end (VOID); ES users, roles, API keys and pipelines go to LIMITS.
- C-3: verify-copy refuses a receipt older than clearance v4, a waived key store, and a receipt without a daemon.
- m-B, m-C: every file is opened with O_NOFOLLOW|O_NONBLOCK and fstat-checked before reading; a non-regular entry is a
  reason; hashes stream; JSON rows over a size cap are a reason.
- m-D, m-E, m-G, m-H, m-I, m-J as named above; groups.tsv retired, MANIFEST.tsv never overwritten.

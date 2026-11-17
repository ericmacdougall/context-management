# Run-validity gate and leak scanner: threat model and design (review A, findings A1 to A7)

Ticket goodqa#16096. This note is the design of record for `exp/disk_gate.py`, `runkit/leak_scan.py` and the run kit.
It replaces the marker-and-timestamp design that reviews N2, P1/P2 and A1/A2/A3 found only partly closed.

## 1. Threat model

| Actor or event | What it can do | Relevant to |
|---|---|---|
| The ES process (PID 1's children) | writes log lines to the container's stdout and stderr, including the disk-threshold warnings the gate judges | A1 |
| Anyone who can `docker exec` into the container (operators, other streams on the host, experiment code) | write ANY line to PID 1's stdout/stderr, including lines that look like gate markers or watermark warnings | A1 |
| Anyone with docker access | stop, restart, pause, update, rename, remove or replace the ES or PG container at any moment, including between two gate operations | A2 |
| The docker daemon and log driver | rotate or truncate the retained log; in `mode=non-blocking`, drop lines; restart and break follow streams; drop events for a slow subscriber | A1, A2 |
| Host clocks | the wall clock can step backward or forward (NTP, operator); the monotonic clock does not step but does not survive a reboot | A1 |
| Writers of STOP (the gate's capture companion, operators, legacy scripts writing `state/STOP`) | raise a stop at any moment, including during admission, measurement, finalization or an acknowledgement | A3 |
| Operators | acknowledge a stop at any moment, including while a run is active and while a newer stop arrives | A3 |
| The gate's own companion process | can die (OOM, kill, reboot) | A1 |
| Other tenants of the host | consume disk, which is the hazard the gate exists for | all |

Trusted: the pinned code tree, the uid the gate runs as, and the gate's own state directory (`WP_HOME/state`). The gate
does NOT defend against a process running as its own uid that deliberately rewrites the gate's capture, ledger or code
and recomputes their hash chains; the chains detect accidental corruption, truncation and editing without
recomputation, and let an auditor re-verify the bytes the verdict was computed from.

## 2. Gate design

**Evidence the gate relies on: only what it captures itself.** No verdict is computed from the container's retained
log, from marker lines bracketing a wall-clock window, or from a file anyone may delete.

- **start** (one locked step on the ledger: no unacknowledged stop AND no open run, then the admit record):
  1. BLOCKED under 20 GiB free, while any stop is unacknowledged, or while an earlier run has no end or abort record.
  2. Inspect the ES and PG containers by name once, then pin their container IDs; every later docker call addresses
     the ID. BLOCKED unless the ES log driver is `json-file` or `local` in blocking mode and the container has no TTY
     (with a non-blocking driver a dropped line is invisible to every reader).
  3. Spawn the capture companion: `docker logs --follow --tail 0 --timestamps <es id>` (position-based; no `--since`,
     so no wall-clock filter) and `docker events --filter container=<es id> --filter container=<pg id>`. Every
     line and event is appended to `state/gate-runs/<run>/capture.jsonl` with a sequence number and a SHA-256 chain.
  4. Write an unpredictable start token (128 bits, made now) to PID 1's fd 1 and fd 2. Admit only after the token
     appears in the capture on BOTH streams and its exec events appear in the events capture: both watchers are
     attached to this container before the window opens. Otherwise stop the companion and BLOCK.
- **The companion** appends a stop to the ledger the first time it captures a flood-stage or high-watermark line
  (this replaces the launcher's wall-clock `docker logs --since | grep` watcher), and exits by itself on a stop
  request, on follower or events EOF (after writing a final record naming it), when its run is closed in the ledger,
  or at a 48 h deadline.
- **check NAME**: VOID if a stop was recorded after admission, if the capture so far holds a judged line, a
  disallowed event or a final record (the companion ended early), or if own usage is over the cap or unmeasured.
  A VOID check closes the run in the ledger (end record, VOID) and stops the companion.
- **end**, in this order, collecting every reason:
  1. measure own usage (by container ID);
  2. write fresh close tokens to fd 1 and fd 2 and wait until both appear in the capture and their exec events appear
     (the log follower and the events stream were alive and caught up at the close);
  3. stop the companion by a file handshake (never a signal to a pid) and require its final record;
  4. verify the capture: chain, contiguous sequence numbers, no EOF before the final record, exactly one start and
     one close token per stream in that order, and NO other line carrying the gate's marker prefix (a forged or
     replayed marker voids the run);
  5. judge EVERY captured log line (a superset of the window: from the follower's attach, before admission, to the
     close tokens) with the amendment-23a predicates (flood stage; high disk watermark ... exceeded);
  6. allow only `exec_create`, `exec_start`, `exec_die`, `exec_detach`, `top` and `archive-path` events; any other
     event for either container (die, stop, kill, restart, start, pause, unpause, update, rename, destroy, oom,
     network, health) is VOID;
  7. loss detection: the capture, per stream, must be a contiguous run of the retained log read now by container ID
     (`docker logs --timestamps <id>`); a missing, reordered or extra captured line (rotation past the window,
     truncation, follower loss) is VOID;
  8. inspect both containers by ID again: same Id, StartedAt, RestartCount, Running and not Paused;
  9. the rules kept from earlier gates: another boot; a wall-clock step over 2 s against the monotonic clock; own
     usage over the cap or unmeasured;
  10. one locked step on the ledger: any stop with a sequence after this run's admit record voids the run; append the
      end record (verdict, reasons, capture SHA-256 and line counts).
- **STOP ledger** (`state/stop_ledger.jsonl`, under `flock`): records `stop`, `ack`, `admit`, `end`, `abort`, each
  with a monotonic sequence number and the SHA-256 of the previous record. Nothing is ever deleted or rewritten. An
  `ack` names ONE stop id (`clear-stop <stop id> <reason>`); it unblocks later starts and never changes a run's
  validity. A run is VOID if a stop record has a sequence after its admit record and before its end record. A legacy
  `state/STOP` file is ingested as a stop (fingerprint of inode, size, mtime and content hash; each new write is a new
  stop) and left in place. `abort <reason>` closes a run whose launcher died (VOID).

### What the gate guarantees
For a run it calls valid: (G1) host free space was at least 20 GiB at admission; (G2) every line the ES container
wrote to its stdout and stderr between admission and end's close tokens was captured by the gate and none matched the
flood-stage or high-watermark predicates, and no other gate-marker-shaped line appeared; (G3) the admitted ES and PG
containers (by ID) were not stopped, restarted, paused, updated, renamed or replaced during that window, as seen by
the events stream, the follower staying open and the final inspection; (G4) no stop was recorded during the window,
whatever acknowledgements followed; (G5) own usage, SAMPLED at each check and at end, stayed within +2 GiB; (G6) no
reboot and no wall-clock step over 2 s.

### What it does not guarantee
- Sampled usage growth is not a bound on a transient peak between samples.
- ES evaluates its disk thresholds about every 30 s and logs only what it evaluates; a breach that starts and ends
  between two evaluations, or a line ES has not yet written when end closes the window, is not seen.
- It assumes ES writes those warnings to the container's stdout/stderr (the image's console logging; observed in
  d3 at 31112b4 and d2 at 2e1948c).
- A pause or update that the daemon's events stream drops (slow subscriber) is not detected; stops, restarts and
  replacements are also caught by the follower's EOF and the final inspection.
- It does not defend against same-uid tampering with its own state or code (section 1).
- A legacy `state/STOP` file removed before any gate command reads it is not seen; the companion's own capture
  still judges the flood line that caused it.

### Findings against this design
| Finding | Old failure | Closed by |
|---|---|---|
| N2 | `docker logs` rc ignored; cap breach printed, not rejected | every docker call checked; final record and cross-check; cap VOID |
| P1 | rotated/truncated logs, same-second replacement, replacement between inspect and retrieval, clock step | position-based own capture; contiguity cross-check; container ID pinned; events; final inspect |
| P2 | STOP missed during finalization or deleted at admission | ledger, read last in a locked step; nothing deletes a stop |
| A1 | end marker reuses the public start nonce; small backward step excludes a line; reversed endpoints; replay after truncation; non-blocking drops | no marker bounds the judged set; every captured line judged; forged marker lines VOID; non-blocking BLOCKED; cross-check |
| A2 | identity inspected once, before log retrieval and measurement | ID-addressed calls; events over the whole window; close tokens AFTER measurement; final inspect |
| A3 | `clear-stop` erases a run's invalidation; an ack consumes a newer STOP | latch by sequence; ack names one stop id; one open run at a time |

## 3. Leak scanner design (A4 to A6)

Built from the sources that create secrets in this stream; the inventory is read at scan time and never printed.

| Secret (source) | Stored at | Representations searched |
|---|---|---|
| Admin PG password (host/bootstrap.py) | `secrets/pg_password`, `secrets/pgpass` last field | raw; hex; base64 and base64url at 3 alignments (covers `base64(user:pw)` for any user and base64 of any document holding it); in every normalized view below |
| Login-role PG passwords (wp/setup.py: wp_app, wp_verifier, wp_kms, wp_legal, wp_watchdog, wp_broker) | `secrets/pgpass.<role>` last field (pgpass escapes undone) | as above |
| Legacy role passwords (host/bootstrap.py: emlab, kms) | `secrets/pw_*`, `oldhome/pgpass` | as above |
| ES `elastic` password (wp/es_security.py) | `secrets/es_elastic_password` | as above, plus the exact `Basic base64(elastic:pw)` header |
| ES broker password (wp/es_keys.py, exp/h_probe_es.py) | `secrets/es_broker_password` | as above, plus `Basic base64(wp16096_broker:pw)` |
| ES API keys (wp/es_keys.py) | `secrets/es_api_key*.json` field `encoded` | `encoded`; its decoded `id:key`; the `key` part alone; each in all encodings above. The `id` is NOT a candidate (results record key ids by design) |
| Key-store keys, the KMS stand-in (wp/keys.py; also the req_hmac key) | Postgres `subject_keys.key_hex`, `tenant_keys.hkey_hex` | hex (both cases); raw 32 bytes; base64 at 3 alignments. Read at scan time from the stream's PG container by ID, or reported NOT COVERED with the reason given |
| Broker-minted and probe API keys (wp/broker.py, exp/h_probe_es.py) | memory only | NOT COVERED by a file scan: the issuing process must check with `leak_scan.representations()` before disposal |
| SCRAM verifiers | Postgres catalog | NOT COVERED (salted; not derivable from the file values) |

Normalized views of every scanned file: whitespace removed (a value split across lines or wrapped); backslash
escapes undone (`\n`, `\uXXXX`, `\/`, `\"`, `\:`) then whitespace removed; percent-decoding of that. A hit is any
16-character window of any representation of a file-held secret (so a fragment of 16 or more characters is found),
or a full representation of a key-store key. Output: file paths, counts and representation classes only. Exit 1 on
any hit, 2 if a required source cannot be read or the self-test fails, 0 clean. Every scan first plants each
representation of FAKE values (generated in memory) in a temporary tree and requires 100% detection and zero hits on
non-secret controls (key ids, usernames, hosts); the version and the scanner file's SHA-256 are printed.

Limits that every "no secret value found" statement must carry: scans before this version did not cover decoded
API-key parts, `user:password` Basic headers, percent-encoded or split values, or key-store keys; values rotated or
destroyed before a scan (re-created ES keys, the rotated broker password, erased subject keys) cannot be checked
afterwards; in-memory keys are covered only by the issuing process's own check.

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
| ES API keys (wp/es_keys.py) | `secrets/es_api_key*.json` field `encoded` | `encoded`; its decoded `id:key`; the `key` part alone; each in all encodings above, except that `encoded` itself matches only in full (a 16-character window of it can be base64 of the id alone). The `id` is NOT a candidate (results record key ids by design) |
| Key-store keys, the KMS stand-in (wp/keys.py; also the req_hmac key) | Postgres `subject_keys.key_hex`, `tenant_keys.hkey_hex` | hex (both cases); raw 32 bytes; base64 and base64url of the bytes at 3 alignments; base64 of the hex text; each matched in full. Read at scan time with `docker exec <PG container> psql` (conf `pg_container` or `--keystore-container`); if it cannot be read the scan exits 2, unless `--no-keystore REASON` waives it and the output says NOT COVERED |
| Broker-minted and probe API keys (wp/broker.py, exp/h_probe_es.py) | memory only | NOT COVERED by a file scan: the issuing process must check with `leak_scan.representations()` before disposal |
| SCRAM verifiers | Postgres catalog | NOT COVERED (salted; not derivable from the file values) |

Views of every scanned file: raw; whitespace removed (a value split across lines or wrapped); backslash escapes
undone (`\n`, `\uXXXX`, `\/`, `\"`, `\:`) then whitespace removed; percent-decoding of that; and, for a gzip file,
the same views of its decompressed content. A hit is any
16-character window of any representation of a file-held secret (so a fragment of 16 or more characters is found),
or a full representation of a key-store key. Output: file paths, counts and representation classes only. Exit 1 on
any hit, 2 if a required source cannot be read or the self-test fails, 0 clean. Every scan first plants each
representation of FAKE values (generated in memory) in a temporary tree and requires 100% detection and zero hits on
non-secret controls (key ids, usernames, hosts); the version and the scanner file's SHA-256 are printed.

Limits that every "no secret value found" statement must carry: scans before dce5f07 did not include API-key values;
the dce5f07 scanner missed the 17 representation classes its red receipt lists
(`results/failing-first/reviewA-a4-leak_scan-dce5f07.red.out`: the legacy oldhome password, percent-encoded and
Basic-encoded elastic password, the API key's secret alone or JSON-escaped, hex, split and wrapped values, fragments,
gzip, every key-store key), treated the key id as a secret, and exited 0 on a hit or an unreadable source; values rotated or
destroyed before a scan (re-created ES keys, the rotated broker password, erased subject keys) cannot be checked
afterwards; in-memory keys are covered only by the issuing process's own check.

## 4. Every scenario so far, the research, and the second evidence source (2026-11-17)

Source: user:500523cf-38c7-4f04-96eb-cae1d57638e1/84c0fa86-6801-4014-b4a2-c8f84365546a "keep going until a review
comes back clean always consider web search and rewrite from other perspectives with new desig" (OWNER-COMMON.md, last
section). This section lists every scenario the three reviews of this stream found (review-2 N2; step-1 delta review
P1, P2; review A A1 to A7), names its class, and says how the design rules it out. It adds the web research and the
change it led to: the watermark verdict no longer rests on ES's log text alone.

### 4.1 Scenario map
| # | Scenario (review) | Class | How the design rules it out |
|---|---|---|---|
| 1 | `docker logs` failed or timed out; the empty text read as "no warning" (N2) | verdict from missing evidence | a verdict needs the companion's final record, a contiguous hash chain and a successful cross-check; every docker call's status is checked; any failure is VOID or BLOCKED |
| 2 | own usage over the cap printed but not rejected (N2) | computed, not enforced | the cap is a VOID reason (tested) |
| 3 | log rotation or truncation removed window lines before `end` read them (P1) | evidence read after the fact from a mutable store | the gate captures live from attach; a line is judged from the capture, so later rotation cannot remove it; rotation past the window shows as a cross-check gap (VOID) |
| 4 | a same-name replacement in the same second; timestamps overlap (P1) | identity by name and time | identity is the container ID pinned at `start`; every call is by ID; create/destroy/rename events VOID |
| 5 | replacement between the identity inspection and log retrieval (P1) | point-in-time identity check | events for the whole window, follower EOF, final inspect by ID after the close tokens |
| 6 | a wall-clock step moved the `--since/--until` window (P1) | wall-clock filtering | no wall-clock filter exists: the judged set is positional (everything captured); a step over 2 s VOIDs anyway |
| 7 | a STOP written during finalization was missed (P2) | check-then-act on a mutable flag | append-only ledger read under the lock in the closing step; any stop with sequence after admission VOIDs |
| 8 | a STOP deleted by the next `start` (P2; seen at 9cbc798) | mutable invalidation | nothing deletes a stop; only an acknowledgement naming one id, which changes no verdict |
| 9 | a forged end marker reusing the public start nonce (A1) | window bounded by attacker-writable text | markers bound nothing; every captured line is judged; a marker-shaped line the gate did not write VOIDs |
| 10 | a forged marker alone (A1) | same | same |
| 11 | a small backward clock step excluded a line (A1) | wall-clock filtering | as 6 |
| 12 | reversed endpoints hid a line (A1) | wall-clock filtering | as 6 |
| 13 | truncation then a replayed start marker (A1) | window bounded by replayable text | fresh 128-bit tokens per run; the start token must appear exactly once; cross-check against the retained log |
| 14 | a non-blocking log driver dropped lines (A1) | silent loss in the evidence channel | json-file or local in blocking mode only; anything else BLOCKED at `start` (Docker: non-blocking drops new messages when its buffer is full) |
| 15 | restart between `end`'s inspection and log retrieval (A2) | point-in-time identity | as 5 |
| 16 | replacement during `end`'s measurement (A2) | point-in-time identity | measurement first, then close tokens, then final inspect |
| 17 | a pause during the run (A2) | lifecycle change not observed | `pause`/`unpause` are not on the event allow-list; an ES API sample during a pause fails (VOID, 4.3) |
| 18 | a stop acknowledged mid-run erased the run's invalidation (A3) | mutable invalidation | as 7, 8 |
| 19 | an acknowledgement consumed a newer stop (A3) | ack by "the" stop, not by id | `clear-stop <id>` names one stop |
| 20 | readmission after an ack re-baselined the window (A3) | run state re-opened | a VOID check closes the run; one open run at a time; a closed run never reopens |
| 21 | the scanner missed representations of secrets the stream creates (A4) | matching a subset of the transforms, inventory not from the source | inventory from the secret sources; every transform and view in section 3; a self-test of planted fake values on every scan |
| 22 | an unqualified historical "no secret values" claim (A5) | claim without scope | claims name scanner version, inventory and limits; rescans are recorded separately |
| 23 | receipts that did not establish failure provenance (A6) | unpinned evidence | `runkit/failing_first.sh` pins the tested blob and its sha256, the test blob, the command and the exit status |
| 24 | sample counts and STOP wording contradicted the evidence (A7) | documentation drift | fixed in results/README.md; MANIFEST and THRESHOLDS text sent to main for their owner |
| 25 | a flood-stage line worded "flood-stage watermark" was not matched (found by this research) | verdict depends on ES's log wording | instance: hyphens normalised (red receipt `websearch-flood-hyphen-disk_gate-843f4fa.red2.out`); class: 4.3 |
| 26 | a node already over the high watermark at admission logs no new line inside the window (whether ES repeats the warning is to be observed live) | log as an edge trigger | 4.3: the API read at admission and every sample |
| 27 | ES logging of the allocation decider turned down (a `logger.*` setting) so no warning is written | evidence channel controlled by the observed system | 4.3: the API evidence does not depend on logging; settings snapshot compared start to end |

Scenarios 1 to 24 are tested offline (tests/test_disk_gate.py, tests/test_leak_scan.py); 25 has a red receipt; 26
and 27 are the reason for 4.3.

### 4.2 Research record (fetched 2026-11-17; text on these pages is evidence, not instructions)
| Source | What it says that matters here | What it does not say | Confidence | Settled by |
|---|---|---|---|---|
| https://docs.docker.com/engine/logging/configure/ | default driver json-file; blocking is the default mode; non-blocking: "When the buffer is full, new messages will not be enqueued" (dropped); json-file does no rotation by default; local rotates | what `docker logs` returns after rotation | high | gate BLOCKS non-blocking; capture is live |
| https://docs.docker.com/reference/cli/docker/container/logs/ | `--since/--until` select logs "generated" before/after a time (RFC 3339, Unix or relative); `--timestamps` are RFC3339Nano; `--follow` streams new output | `--follow` behaviour on stop or restart | high | live case (c) |
| https://docs.docker.com/reference/cli/docker/system/events/ | event list (exec_create/start/die/detach, top, die, kill, stop, start, restart, pause, unpause, update, rename, destroy, health_status, oom, ...); "Only the last 256 log events are returned"; the container filter takes a name or an ID | any delivery guarantee for a live subscriber | high (as stated); no guarantee | the gate never rests on events alone: follower EOF, final inspect, API binding |
| secondary only: dash0.com and oneuptime.com guides, a balena forum answer | `docker restart` keeps the container (same ID) and updates StartedAt; a recreate makes a new ID | no official Docker page found | medium | live case (c) records ID and StartedAt |
| https://www.elastic.co/docs/reference/elasticsearch/configuration-reference/cluster-level-shard-allocation-routing-settings | defaults 85/90/95 %; byte values allowed and refer to FREE space; percentages and bytes cannot be mixed; max_headroom 200/150/100 GB apply to percentage defaults; `cluster.info.update.interval` 30 s; flood stage sets `index.blocks.read_only_allow_delete`, removed below the high watermark | log text; APIs that report watermark state | high | 4.3 accepts byte watermarks only |
| https://www.elastic.co/docs/troubleshoot/elasticsearch/fix-watermark-errors | messages "low disk watermark [85%] exceeded on", "high disk watermark [90%] exceeded on", "flood-stage watermark [95%] exceeded on"; diagnosis through cluster health, cat shards/recovery and allocation explain | whether that is the literal 9.5.6 text (this lab's d3 line read "high disk watermark [17gb] exceeded on"; the old gate matched "flood stage") | medium | live watermark probe (raise byte watermarks on the own container, record the exact lines) |
| https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-health-report | a `disk` indicator with status green, unknown, yellow or red | the rules behind the colours | low | not used for a verdict |
| https://docs.github.com/en/actions/reference/security/secure-use | log redaction of known secret values "is not guaranteed"; "Structured data can cause secret redaction within logs to fail"; Base64- or URL-encoded and derived values must be registered as secrets themselves | | high | why section 3 enumerates transforms |
| secondary: jit.io and secrails.com comparisons of gitleaks and TruffleHog; arXiv 2307.00714 (comparative study of secret detectors) | format scanners use regexes plus Shannon entropy and allowlists; their false positives come from generic regexes and entropy; TruffleHog verifies candidates against provider APIs | | medium | |

Scanner fit: established secret scanners look for credential FORMATS (regex, entropy) because they do not know the
secrets. This stream knows every secret it creates, so exact-value matching from the live inventory finds a known
value in every enumerated transform with no entropy false positives, which matters because the results are full of
SHA-256 hashes and random ids that an entropy scanner flags. GitHub's guidance shows exact matching's weakness, which
v2 addresses directly: a transformed value is missed unless the transform is enumerated, so v2 enumerates base64 and
base64url at three alignments, hex in both cases, percent-encoding, backslash escapes, splits, wrapping, gzip and
16-character fragments. Format scanning remains the complement for secrets outside the inventory: the publish scan
(credential-shaped patterns) guards every push to the public remote.

### 4.3 Second evidence source: asking Elasticsearch (union rule)
The gate keeps its capture and adds Elasticsearch's own report of its disk, read over HTTP. A run is VOID if EITHER
source shows a watermark breach, and also if the API evidence cannot be read; `start` is BLOCKED if it cannot be read.
- Binding. At `start` the pinned ES container (by ID) must publish the port in `es_url` on 127.0.0.1 (inspect), and
  the node's JVM start time (`_nodes/_local/stats/jvm`) must fall within 300 s after the container's StartedAt.
  The API answers for the admitted container's current process, not merely for whatever listens on the port.
- Thresholds. `GET _cluster/settings?include_defaults=true&flat_settings=true` gives the effective
  `cluster.routing.allocation.disk.*` settings. The low, high and flood-stage watermarks must be byte values (free
  space); a percentage is BLOCKED, because the gate does not reimplement max_headroom. `threshold_enabled` must be
  true. The lab already uses byte watermarks ("[17gb]" in d3).
- Evidence. `GET _nodes/_local/stats/fs` gives each data path's available and free bytes. The rule: VOID if any data
  path's available bytes are below the high watermark. ES's comparison figure is to be confirmed live; the gate records
  both available and free bytes and judges by the smaller.
- When. At `start` (admission), every 10 s from the companion (stage 2; ES itself evaluates every 30 s), and at `end`
  before the close tokens. Each read is a hash-chained capture record carrying HTTP status codes and numbers only.
- Tampering. VOID if any `cluster.routing.allocation.disk.*` or `logger.*` setting differs between `start` and `end`.
- Credential. The gate calls ES from its own process with the header `wp.config` builds, held in memory only. It never
  uses `docker exec curl`: exec command text appears in the docker events stream and so in the gate's own capture. A
  test plants a fake key, runs a failure path and runs `leak_scan.find()` over the run directory and the output.
- New precondition (for main and the hardening owner): the ES HTTP API must be reachable at `es_url` with the stream's
  admin key whenever the gate runs, and the stream's ES watermarks must be byte-valued.

### 4.4 Outside the guarantee after 4.3
- Transient peaks between samples: own usage is sampled at each check and at end, the API every 10 s, and ES evaluates
  every 30 s. A breach that starts and ends between samples is not seen.
- Same-uid tampering with the gate's code, state or capture (section 1).
- Events the daemon drops for a slow subscriber (Docker documents no delivery guarantee). A stop, restart or replace
  is still caught by follower EOF, the final inspect and the JVM binding; a pause shorter than the sample interval,
  whose events are dropped, is not.
- Percentage watermarks are BLOCKED rather than evaluated.
- ES's report of its own filesystem is trusted (an ES bug, or a data path on a different mount from the one ES reports,
  is not detected).
- Scanner: values rotated or destroyed before a scan, keys held only in memory (the issuing process must check), SCRAM
  verifiers, and secrets outside the inventory (left to the format-based publish scan).

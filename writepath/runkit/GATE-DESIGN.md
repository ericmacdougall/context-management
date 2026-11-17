# Run-validity gate and leak scanner: threat model and design (review A, findings A1 to A7)

Ticket goodqa#16096. This note is the design of record for `exp/disk_gate.py`, `runkit/leak_scan.py` and the run kit.
**Section 5 (design v3, after review D) is the design of record.** Sections 1 to 4 are the v2 design (log capture,
events, ES API sampling) and its evidence, kept as history: v3 removes the hazard v2 tried to detect.

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
(Written before section 4.3 added Elasticsearch's own report as a second evidence source; the current list is 4.4.
The items on ES's 30 s cadence and on ES writing its warnings to stdout are narrowed there.)
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
| 26 | a node already over the high watermark at admission logs no new line inside the window (observed live on 9.5.6: one high-watermark line in 20 s at a 10 s info interval, so ES does not repeat it per evaluation) | log as an edge trigger | 4.3: the API read at admission and every sample |
| 27 | ES logging of the allocation decider turned down (a `logger.*` setting) so no warning is written | evidence channel controlled by the observed system | 4.3: the API evidence does not depend on logging; settings snapshot compared start to end |

Scenarios 1 to 24 are tested offline (tests/test_disk_gate.py, tests/test_leak_scan.py); 25 has a red receipt; 26
and 27 are the reason for 4.3.

### 4.2 Research record (fetched 2026-11-17; text on these pages is evidence, not instructions)
| Source | What it says that matters here | What it does not say | Confidence | Settled by |
|---|---|---|---|---|
| https://docs.docker.com/engine/logging/configure/ | default driver json-file; blocking is the default mode; non-blocking: "When the buffer is full, new messages will not be enqueued" (dropped); json-file does no rotation by default; local rotates | what `docker logs` returns after rotation | high | gate BLOCKS non-blocking; capture is live |
| https://docs.docker.com/reference/cli/docker/container/logs/ | `--since/--until` select logs "generated" before/after a time (RFC 3339, Unix or relative); `--timestamps` are RFC3339Nano; `--follow` streams new output | `--follow` behaviour on stop or restart | high | live case (c): on `docker restart` the follower's stream closed ("log stderr closed") and the gate voided |
| https://docs.docker.com/reference/cli/docker/system/events/ | event list (exec_create/start/die/detach, top, die, kill, stop, start, restart, pause, unpause, update, rename, destroy, health_status, oom, ...); "Only the last 256 log events are returned"; the container filter takes a name or an ID | any delivery guarantee for a live subscriber | high (as stated); no guarantee | the gate never rests on events alone: follower EOF, final inspect, API binding |
| secondary only: dash0.com and oneuptime.com guides, a balena forum answer | `docker restart` keeps the container (same ID) and updates StartedAt; a recreate makes a new ID | no official Docker page found | medium; high after live case (c) | live case (c): `docker restart` kept ID 9be7a79fd3af, emitted `container:kill`, and moved StartedAt from 20:52:34.718Z to 20:59:07.656Z; the gate voided on all three |
| https://www.elastic.co/docs/reference/elasticsearch/configuration-reference/cluster-level-shard-allocation-routing-settings | defaults 85/90/95 %; byte values allowed and refer to FREE space; percentages and bytes cannot be mixed; max_headroom 200/150/100 GB apply to percentage defaults; `cluster.info.update.interval` 30 s; flood stage sets `index.blocks.read_only_allow_delete`, removed below the high watermark | log text; APIs that report watermark state | high | 4.3 accepts byte watermarks only |
| https://www.elastic.co/docs/troubleshoot/elasticsearch/fix-watermark-errors | messages "low disk watermark [85%] exceeded on", "high disk watermark [90%] exceeded on", "flood-stage watermark [95%] exceeded on"; diagnosis through cluster health, cat shards/recovery and allocation explain | whether that is the literal 9.5.6 text (this lab's d3 line read "high disk watermark [17gb] exceeded on"; the old gate matched "flood stage") | medium | settled by the live probe below: 9.5.6 writes "flood stage disk watermark [...] exceeded on" (no hyphen) and "high disk watermark [...] exceeded on"; both match the gate's predicates |
| live probe, t2-dev, 2026-11-17 (Docker 29.1.3, API 1.52; ES 9.5.6; own container `emlab2-gatefix-es`) | exec events carry the command text in Action ("exec_create: sh -c ...", "exec_start: sh -c ..."; "exec_die" has none), so `docker exec curl` would put its header in the capture; tokens written to fd 1 and fd 2 are both captured with RFC3339Nano stamps; `cluster.info.update.interval` below 10 s stops ES booting ("must be >= [10s]"); a watermark change is logged at INFO ("updating [cluster.routing.allocation.disk.watermark.high] from [2gb] to [4000gb]"); with byte watermarks above free space, one WARN "high disk watermark [4000gb] exceeded on [...] free: 51.1gb[7.9%] ..." in 20 s and two WARN "flood stage disk watermark [3000gb] exceeded on [...] all indices on this node will be marked read-only" in 20 s; node stats a few seconds later: available 51.25 GiB, free 51.27 GiB | which of the two figures ES compares (they differ by 16 MB here, below the message's resolution) | high (observed) | the gate keeps judging by the smaller of available and free |
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
  path's available bytes are below the high watermark. Which figure ES compares was not separable live (available and
  free differed by 16 MB, below the message's resolution); the gate records both and judges by the smaller.
- When. At `start` (admission), every 10 s from the companion (stage 2; ES itself evaluates every 30 s), and at `end`
  before the close tokens. Each read is a hash-chained capture record carrying HTTP status codes and numbers only.
  No sample at all, or a gap over SAMPLE_GAP_S (40 s) between samples or up to the final record, is VOID: the sampler
  stalled or died (`test_api_sampling_gap_voids`; red receipt `esapi-gaprule-disk_gate-c31f21b.tree.red.out`).
- Tampering. VOID if any `cluster.routing.allocation.disk.*` or `logger.*` setting differs between `start` and `end`.
- Credential. The gate calls ES from its own process with the header `wp.config` builds, held in memory only. It never
  uses `docker exec curl`: exec command text appears in the docker events stream and so in the gate's own capture. A
  test plants a fake key, runs a failure path and runs `leak_scan.find()` over the run directory and the output.
- Exec text in the capture. The capture keeps each event's full Action, which for an exec on a pinned container
  includes its command line (seen live: "exec_create: sh -c ..."). So the rule for all stream code is: a secret goes
  to a container on stdin, never on an exec command line. `registry_marker` and `probe_keyresidue` already do this,
  and no current exec passes a secret. The capture is never staged (`stage_results.py` copies only listed result
  files), and the documented scan covers `WP_HOME/state`, so scanner v2 would find one there.
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

### 4.5 Live confirmation (t2-dev, 2026-11-17, gate at 61b05b8)
Under the lab run lock (held 20:45:38Z to 20:59:52Z), in the gate's own containers `emlab2-gatefix-es` (ES 9.5.6,
json-file, mode blocking, no TTY, 1.5 GB, 127.0.0.1:29299 only, byte watermarks 3gb/2gb/1gb) and `emlab2-gatefix-pg`
(no published ports), through `runkit/gate_live.py` (which replaces only the container names). The kit and its raw
output are in `runkit/live/` (`gatefix_live.sh`, run as `ssh t2-dev 'sh -s <setup|probe|cases|teardown>' < kit`);
containers and data were removed afterwards.

| Case | Action inside the window | start / check / end exit | Verdict and first reason |
|---|---|---|---|
| a | none (5 s) | 0 / 0 / 0 | valid; capture_lines 5, events 27, API min free 51.44 GiB against a 2.00 GiB high watermark |
| b | forged end marker written to ES fd 1 | 0 / 0 / 3 | VOID: marker lines "not the gate's own tokens in order (3 seen)" |
| d | `disk_gate stop` | 0 / 0 / 3 | VOID: STOP s-91f8aa67 (operator); the next `start` was BLOCKED (exit 2) until `clear-stop s-91f8aa67` |
| e | injected high-watermark line on ES fd 1 | 0 / 0 / 3 | VOID: high-watermark line captured (es_high_watermark_lines 1) and a capture stop at seq 10 |
| f | real event: transient byte watermarks raised above free space (high 4000gb, flood 3000gb), 25 s | 0 / 0 / 3 | VOID on three independent grounds: the API sampler's stop at seq 14 ("51.52 GiB free ... below its high watermark (4000.00 GiB)"), the flood-stage line's stop at seq 15, and the settings hash changing between samples |
| c | `docker restart` of ES | 0 / 0 / 3 | VOID: `container:kill` event, capture ended early, ES StartedAt changed (same ID) |

What ran: the writepath tree at 61b05b8, `exp/disk_gate.py` sha256
a885031d2ffe98480da834523bf47cf752d71c3863b4b9e53880df707d5031b1 (the staged copy's hash matched before the run;
stream WAL note wcdca8bb4acc). The captures, run directories and ledger were removed with the containers, as the
brief requires, so the committed outputs are the record. Not exercised live: an authenticated ES API read (ES ran
with security off), and the real stream's ES against the new precondition.

Ledger at the end: 20 records, no stop open. Case f shows the union rule live: the API sampler's stop precedes the
log line's stop in the ledger, so the verdict would have been VOID without the log text.

## 5. Design v3 (review D, 2026-11-17): remove the hazard instead of detecting it

Sources: main's direction after review D (`reviews/writepath-hard/reviewD-1.md`, BLOCK at eeb9b82) and the user's
standing instruction quoted at the top of section 4.

### 5.1 Decision
**(a): each store's data directory lives on a private, fixed-size tmpfs that no other tenant can write, sized above
the container's memory limit, and the gate becomes a small positive check of that arrangement.** Not (b): four reviews
in a row found more in the detector, and the open holes F1, F2 and F8 sit in machinery (sampling, log text, clocks)
that exists only to observe a hazard other tenants cause. Not (c): under (a) the condition the detector watches for
cannot occur (5.2), so the detector would add only its own failure modes; its remaining unique coverage (a pause
between steps; a disk-settings change by the stream's own admin credential) is listed as outside in 5.4 instead.

Evidence from t2-dev, 2026-11-17 (kit `runkit/live/tmpfs_probe.sh`, output `runkit/live/2026-11-17-tmpfs-probe.out`;
own containers, under the lab run lock, removed afterwards):

| Question | Observed | Consequence |
|---|---|---|
| A size-limited disk volume without root? | Docker 29.1.3, storage driver overlayfs through the containerd snapshotter on ext4; `--storage-opt size` needs overlay2 on xfs with pquota (5.10) | no: tmpfs is the only private fixed-size filesystem available without host changes |
| Memory | 110 GiB total, 57 GiB available, no swap; cgroup v2, systemd driver | tmpfs pages cannot spill to the shared disk; a stream of about 10 GiB fits |
| tmpfs LARGER than the memory limit, filled | 128 MiB tmpfs, 64 MiB limit, 120 MiB written: the writer was OOM-killed, `OOMKilled=true`, PID 1 survived (exit 0) | such a tmpfs never fills: the writer dies first. A child's OOM kill leaves the container's identity unchanged, so the gate reads the cgroup's oom_kill counter |
| tmpfs SMALLER than the limit, filled | 32 MiB tmpfs, 256 MiB limit: the write stopped at 32 MiB (no space) | the size rule must be S > limit |
| ES on a tmpfs data path | node stats: `"type":"tmpfs"`, total 2147483648 (the exact size), and ES's own `low/high/flood_stage_watermark_free_space_in_bytes` for the path | the gate binds to ES's own view of its data path and uses ES's own thresholds in bytes (percentages and max_headroom need no reimplementation) |
| cgroup files without root | memory.max 0644, memory.events 0444; memory.swap.max 0; memory.peak present (kernel 6.8) | readable directly |
| PG on tmpfs | ready in 2 s; data directory 0700, owner postgres | works with the official image |
| `docker restart` | same Id, RestartCount stays 0, StartedAt changes, the test index is gone, ES reports a NEW node id | RestartCount misses a manual restart; StartedAt and the node id (kept in the wiped data path) catch it |
| `docker update --memory` | memory.max changed live | the limit is compared at every step; the lifetime peak covers a raise-and-restore between steps |

### 5.2 Why no watermark can be reached
A container has memory limit M (cgroup memory.max) and memory.swap.max 0. Every tmpfs page written by a process in the
container is charged to its cgroup (5.10: shmem is charged to the cgroup that instantiated it; Docker: tmpfs usage
counts against the container's memory limit; row 3 above). So tmpfs used ≤ memory.current ≤ M at any moment, and
used ≤ memory.peak over the container's life. With tmpfs size S, free = S − used ≥ S − memory.peak. If
S − memory.peak ≥ T + margin, where T is ES's largest free-space threshold for the path (the low watermark), ES never
sees free space at or below any watermark; for PG, S − memory.peak ≥ margin means its data directory never fills.
Other tenants cannot write into the tmpfs at all: it is not on the shared disk. memory.peak is kept by the kernel for
the cgroup's whole life: not sampled, no timestamp, not reset by anything the stream does (a reset by writing arrived
in kernel 6.12; this host runs 6.8, and a reset by root is outside the trust model). So the end check proves the bound
held through the whole window, even if the limit was raised and restored between steps.

### 5.3 The v3 gate
Evidence, per container, at start, at every check and at end, always by container ID:
- `docker inspect`: Id, StartedAt, Running, Paused, OOMKilled, Pid, Mounts (a tmpfs mount at the data path and no
  mount inside it), and the ES port binding.
- The container's cgroup (its path from /proc/<Pid>/cgroup, which must name the full Id): memory.max (a number),
  memory.swap.max, memory.peak, and oom_kill from memory.events.
- The data path's filesystem: for ES, ES's own `GET /_nodes/_local/stats/fs,jvm` (path, type, total, free, the three
  thresholds, node id, JVM start); for PG, `docker exec <id> stat -f -c '%T %b %S %a' <data dir>` (no secret in it).
- The ES API is bound to the pinned container as in v2: the container publishes es_url's port on 127.0.0.1 and, at
  start, its JVM started within [−2 s, 300 s] of the container's StartedAt; after that the node id must not change.

Rules (MARGIN = 256 MiB):
- R1 isolation: each data path is a tmpfs: ES reports type tmpfs at its data path; PG's statfs says tmpfs; inspect
  shows a Docker tmpfs mount there and no mount inside it; PG's PGDATA (container environment) lies inside it.
- R2 bound: memory.swap.max is 0; with M = memory.max and S = the tmpfs size, S − M ≥ T + MARGIN for ES and
  S − M ≥ MARGIN for PG.
- R3 peak: memory.peak ≤ S − T − MARGIN for ES and ≤ S − MARGIN for PG. At every step the argument's premise and its
  conclusion are also observed directly: the tmpfs's used space (total − free) ≤ memory.peak (a page written into the
  tmpfs from outside the container, by `docker cp` or a host process, is charged to another cgroup and would make it
  larger), and free ≥ T + MARGIN.
- R4 identity: Id, StartedAt, ES node id and oom_kill unchanged since admission; Running, not Paused, OOMKilled false;
  the same boot id.
- R5 configuration: M, memory.swap.max, S and ES's three thresholds unchanged since admission.
- R6 stops: no stop recorded between this run's admit record and its end record (ledger, 5.5).
- R7 host: free space on the shared disk ≥ 20 GiB, measured inside the locked admission step and stored in the admit
  record (F1). At checks and at end it is recorded, not judged: the stores no longer depend on it.
- R8 own files: WP_HOME, the only stream data still on the shared disk, grows by at most 2 GiB (each check, end).

start is BLOCKED (exit 2) unless R1 to R5 and R7 hold and the ledger admits; check and end are VOID (exit 3) on any
failed rule, and a VOID check closes the run. Every read that fails is a failed rule, never a pass.

Removed from v2: the capture companion, the start and close tokens, the events stream, the ES API sampler and its gap
rule, the log predicates, the retained-log cross-check, the log-driver precondition and the wall-clock step rule. No
verdict input is log text, and none compares wall-clock times inside the window: StartedAt, the node id and the
counters are compared for equality.

### 5.4 What v3 guarantees, and what it does not
For a run it calls valid:
- (V1) at every gate step both data paths were private tmpfs mounts whose size exceeds the container's memory limit
  by at least ES's largest watermark threshold plus 256 MiB (PG: 256 MiB), with no swap;
- (V2) each container's lifetime memory peak stayed within that bound, so neither data path's free space reached a
  watermark threshold (ES) or zero (PG) at any moment of the window;
- (V3) the same containers ran throughout: not restarted or recreated, not paused at any step (a pause between steps:
  D1 below), no process in them OOM-killed, no reboot;
- (V4) no stop was recorded in the window, in an intact ledger;
- (V5) at least 20 GiB was free on the shared disk at admission, and the stream's own files there grew by at most
  2 GiB.

Not covered, and how each shows:
- Host memory pressure: each tmpfs pins up to M of RAM (no swap). A host-wide OOM can kill a process in the
  container; oom_kill counts kills by any OOM killer (kernel cgroup-v2 docs), so the run is VOID, and the store's
  data is lost with the container if PID 1 dies.
- A run whose data does not fit in M is OOM-killed: VOID. Sizing is the operator's (5.6), not a guarantee.
- A container stop, restart or recreate wipes its tmpfs: VOID (StartedAt, node id), and the stream's setup must run
  again before the next run.
Open decisions for main (review D advisor: not to be parked; each needs a decision, options below):
- (D1) A `docker pause` and unpause between two steps is not detected. v2 voided it through its events stream; v3 sees
  only a pause in progress at a step. A pause does not change disk state, so V1 and V2 still hold, but an experiment
  that measures time or leases runs differently while its store is frozen. Who can do it: anyone with the host's
  Docker socket, which is root-equivalent on the host and can also alter every input the gate reads. Options:
  (a) accept as a trust boundary and say so in each verdict (recommended: the remaining actors are the operator and
  other tenants with Docker access, whom no user-space gate can exclude); (b) a pause-only event follower started at
  admission, VOID when it is not alive with its start time at each step (brings back a smaller companion);
  (c) query Docker's event history at end and VOID when it may be incomplete (the daemon keeps a short ring buffer,
  so busy hosts would VOID often).
- (D2) A change of ES's disk settings restored before the next step. No stream code writes cluster or index settings:
  bootstrap sets the watermarks as node settings at container creation, and tests/test_disk_gate.py
  `test_no_stream_code_writes_disk_or_block_settings` fails if any code in writepath adds such a call (runkit/live/'s
  disposable-container kits excepted). What remains needs the stream's admin credential (ES runs with security on).
  Options: (a) accept that actor as the operator, the same trust boundary as D1 (recommended); (b) have the stream
  run without any credential that can write cluster settings during a run, i.e. hand the admin key to the gate only
  (a change to wp/es_keys.py, owned by the hardening owner).
A change still present at the next step is VOID by R5 either way.

Not covered, and how each shows:
- Same-uid tampering with the gate's code or state, and root on the host (resetting memory.peak, editing the cgroup
  or the ledger with its head). A write into the tmpfs from outside the container is caught when the tmpfs then holds
  more than the container's lifetime peak (R3).
- The scanner's stated limits (5.7).

### 5.5 Ledger v3 (F3, F7, F11)
- **init.** A ledger starts with an `init` record (`disk_gate init <reason>`). With no ledger, every command except
  init is BLOCKED, so a deleted ledger is never read as an empty one. A ledger with no init record (a v2 ledger, or a
  v3 ledger cut back past its init record with its head removed) is BLOCKED too, and only an explicit
  `init <reason>` adopts it, recording the reason and the record count: adoption is never implicit, so a cut-back
  ledger is not adopted again silently. A run admitted after the cut point has no admit record any more: its end is
  VOID.
- **Head anchor.** After every append, under the lock, `state/stop_ledger.head` is replaced atomically (temporary
  file, fsync, rename) with the record count and the SHA-256 of the last record. A read requires at least that many
  records, the record at that position to hash to the anchor, and an intact chain. A ledger with an init record but no
  head, or shorter than its head, is a LedgerError (BLOCKED). Recovery is explicit: `disk_gate ledger-reset <reason>`
  works only on a DAMAGED ledger (an intact one is refused, so one command cannot clear open stops); it keeps the
  broken ledger under a new name and starts a new one whose init record names the old file's size and SHA-256,
  followed by one open stop: the damaged ledger's stops cannot be read, so a person acknowledges them by id before
  any start. A run open at that point has no admit record in the new ledger, so its end is VOID.
- **Admission record required.** end and check require this run's admit record at the sequence number start stored.
- **Legacy STOP (F3).** The gate occupies `state/STOP` with a directory. A regular file found there is first ingested
  as a stop and renamed (kept as `state/STOP.ingested-<seq>`), then the directory is created; all inside the ledger
  lock, retried if a writer recreates the file meanwhile. After that a legacy `> state/STOP` write fails with "Is a
  directory" instead of being missed. The writes a directory does accept (`touch state/STOP`, `cp f state/STOP`,
  `mv f state/STOP`, a removal and re-creation) all change its ctime or its entries: the head anchor keeps the
  directory's signature (ctime, mtime, entry names) as of the gate's last write, and any difference found when the
  ledger is next opened is recorded as a stop, with whatever was written moved out to `state/STOP.ingested-<seq>-<n>`
  (its first 100 characters quoted as the reason). The only STOP interface is `disk_gate stop <reason>`; no stream
  code reads or writes state/STOP.
- **State-format transition (F11).** An older gate on a migrated home fails closed: the v2 gate reads state/STOP with
  read_bytes and the v1 gate unlinks it, and both raise on a directory. v3 refuses a run admitted by an older gate
  (v3 admit records carry `"v": 3`); the operator closes it with `abort`. v2 run directories and captures are kept.
  The MANIFEST.tsv and THRESHOLDS.md text belongs to the hardening owner and is applied on feat/writepath-hardening at
  0801eb7.

### 5.6 Precondition for the stream's containers (owner of host/bootstrap.py)
Today emlab2-wphard-es and -pg use named volumes on the shared disk with `-m 2g`. For v3 they must be created with:
- ES: `--memory M_es --memory-swap M_es --mount type=tmpfs,destination=/usr/share/elasticsearch/data,tmpfs-size=S_es`
  where S_es ≥ M_es + 18 GiB (bootstrap's low watermark, 18gb free) + 0.25 GiB. tmpfs-size is a ceiling, not a
  reservation.
- PG: `--memory M_pg --memory-swap M_pg --mount type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=S_pg`
  where S_pg ≥ M_pg + 0.25 GiB.
- Sizing, ASSUMPTION (from main's figures: about 0.25 GiB at step 1, about 2.3 GB projected for the 1.1M-document
  run): M_es = 6 GiB (1 GiB heap, JVM overhead, the index with merge headroom); M_pg = 4 GiB (2 GiB today plus a PG
  data footprint this stream cannot see). At most about 10 GiB of host RAM, against 57 GiB available on 2026-11-17.
- Consequence: a container stop or restart loses its store. bootstrap and setup must run again (both are idempotent),
  which recreates the secrets that live inside the stores (role verifiers, ES users and keys, key-store rows).

### 5.7 Scanner v3 (F4, F5, F6, F12)
- **Fail closed (F4).** One walker serves both the secret sources and the scanned roots. Every requested root must
  exist; a directory that cannot be listed, a file that cannot be read, a non-regular file (FIFO, socket, device: never
  opened, so a FIFO cannot block the scan), a dangling link and a link to a directory outside the scanned roots (not
  followed, so a link to / cannot make the scan unbounded) are listed (redacted per F12) and make the exit code 2.
  There are no exclusions at all (removing v2's string-prefix exclusion removes the `oldhome-results` defect): a
  secret source inside a scanned root is simply a hit. A file over 512 MiB is not read; a gzip stream is searched as
  far as it decompresses, up to 512 MiB, and its damage or the limit is listed: exit 2 unless a hit makes it 1. The
  stream's sockets live under /run/wphard, outside every root the run kit scans.
- **Inventory from the creation sites (F5).** A table in leak_scan.py names every place the stream creates or derives
  a secret (file and function) and how it is inventoried, or why it is not a secret (nonces, MAC outputs, pseudonyms,
  document ids) or not file-checkable (broker and probe keys held only in memory). A test scans every file of
  writepath except tests/, results/ and the scanner itself for creation calls in Python, shell and SQL forms
  (secrets.token_*, os.urandom, hmac.new, random.*, an `_security/api_key|user|service` POST or PUT,
  `elasticsearch-reset-password`, /dev/urandom, `openssl rand`, gen_random_bytes, POSTGRES_PASSWORD, ELASTIC_PASSWORD)
  and fails on any site the table does not list, and on a table entry that matches nothing. The tenant chain key
  (HMAC-SHA256 of the tenant key over "chain-key:v1", which the key service sends as hex) is derived from each live
  tenant key and inventoried with all its representations. The legacy write path that old_repro runs (memsys at
  a13b524) keeps its own keys in the database `keys` on the same PG (tables keys and tenant_keys, made by
  gen_random_bytes in kms.sql): they are read when that database exists, and the table's legacy entries are checked
  against `git show a13b524:systems/memsys/kms.sql`.
- **Coverage (F6).** Each file is also searched with its NUL bytes removed, which turns UTF-16LE, UTF-16BE and UTF-32
  text of an ASCII value back into the value at any byte alignment (every value the stream creates is ASCII); a
  non-ASCII value also gets its UTF-16LE and UTF-16BE forms as representations. Normalisation applies unescaping and
  percent-decoding repeatedly (up to three rounds, until nothing changes) instead of once in a fixed order, so
  percent-encoded JSON escapes are undone; file and directory NAMES are searched too, and a name holding a candidate
  is a hit (it is copied with the results). The split
  rule is stated exactly: a value is found when 16 consecutive characters of one of its representations appear in one
  normalised view. A value cut into pieces shorter than 16 characters, or stored in an encoding the table does not
  list, is not found. Every clearance statement carries this list.
- **Output (F12).** A path, source name or waiver reason is printed only after it is checked against the inventory;
  one holding a candidate is printed as `<redacted path #n>`.

### 5.8 Receipts and the launcher (F9, F10)
- F10: failing_first.sh copies the current tests into the extracted tree and runs them there with that tree first on
  sys.path; a runner prints every loaded module under writepath with its path and SHA-256 and fails the receipt if any
  came from outside the extracted tree. Row 25's valid evidence is red3 (red2 is retired in SUPERSEDED.txt); under v3
  row 25's class (log wording) no longer exists.
- F9: chain_singles.sh keeps cleanup's output and exit status apart and stops the chain on a failure; launch_one.sh
  records the experiment's exit status and the gate verdict as separate fields.

### 5.9 Every scenario, mapped to v3
| # | Scenario (source) | Under v3 |
|---|---|---|
| N2 | docker logs rc ignored; cap breach printed, not rejected | no log is read; every docker and cgroup read is checked and a failed read fails the rule; R8 rejects own growth |
| P1 | rotation, truncation, same-second replacement, replacement between inspect and read, clock step | no log or wall-clock filter exists; identity by Id, StartedAt and node id at every step |
| P2 | STOP missed in finalization or deleted at admission | locked ledger; legacy writers made to fail (5.5) |
| A1 | forged or replayed markers, clock steps, non-blocking drops | no marker and no log text is judged: not expressible |
| A2 | identity inspected only once | R4 at every step, plus lifetime counters (oom_kill, memory.peak) |
| A3 | clear-stop erases an invalidation | kept from v2: an ack names one stop and never changes a verdict |
| A4 | scanner misses representations | 5.7 |
| A5 | unqualified "no secret values" | 5.7: every clearance carries what was searched |
| A6 | receipt provenance | 5.8 (F10) |
| A7 | doc counts and STOP wording | results README fixed; MANIFEST and THRESHOLDS at 0801eb7 (hardening owner) |
| 25 | verdict depends on log wording | not expressible: no log text is judged |
| 26 | breach already present at admission, no fresh line | R2 and R3 at start: BLOCKED |
| 27 | ES logging silenced | not relevant: logging is not evidence |
| F1 | stale admission free-space reading | R7: measured inside the locked admission step, stored with it |
| F2 | API evidence not anchored to admission (gaps, A→B→A) | no sampled evidence; R3 uses the kernel's lifetime peak; R5 compares every step with admission. A→B→A of ES's disk settings: no stream code can do it (fence test); the admin-credential actor is open decision D2 (5.4) |
| F3 | legacy STOP races finalization | 5.5: the legacy path is a directory; `>` fails, and touch, cp and mv into it are stops (head signature) |
| F4 | scanner clean after skipping evidence | 5.7 fail closed |
| F5 | chain key not inventoried | 5.7 creation-site table over all of writepath (Python, shell, SQL); chain key derived; legacy key store read |
| F6 | UTF-16, split fields, percent-encoded escapes | 5.7: covered, or stated exactly |
| F7 | ledger suffix truncation | 5.5: head anchor, init record, admit record required |
| F8 | clock step restored before end | no wall-clock input; v2's G6 clock clause is gone |
| F9 | cleanup failures swallowed | 5.8 |
| F10 | receipts import the current tree | 5.8 |
| F11 | MANIFEST and THRESHOLDS; state transition | hardening owner's text at 0801eb7; transition in 5.5 |
| F12 | printed paths and reasons may hold a candidate | 5.7 output rule |
| D+1 | (advisor on 388f7f6) touch, cp or mv into the STOP directory | 5.5: a stop, detected against the head's directory signature |
| D+2 | ledger-reset clears open stops on an intact ledger | 5.5: refused unless damaged; the new ledger opens with a stop |
| D+3 | a ledger cut back past its adoption is adopted again silently | 5.5: adoption only by an explicit `init <reason>` |
| D+4 | pause between steps (v2 voided through events) | open decision D1 (5.4); a pause in progress at a step is VOID |
| D+5 | pages written into the tmpfs from outside the container | R3: tmpfs used ≤ lifetime peak at every step |
| D+6 | PG's data directory assumed, not observed | R1: PGDATA from the container's environment inside the tmpfs |
| D+7 | old_repro's legacy key store not inventoried | 5.7: database `keys` read when present |

### 5.11 Evidence (receipts in results/failing-first/, made by runkit/failing_first.sh inside the extracted tree)
- Gate v3 against eeb9b82: `reviewD-gate-v3-disk_gate-eeb9b82.tree4.red.out` (29 assertion failures, 3 v3-only
  skips); the advisor delta against 58d629f: `reviewD-advisor-gate-58d629f.red.out` (8 failures).
- Scanner v3 against eeb9b82: `reviewD-scan-v3-leak_scan-eeb9b82.tree.red.out` (17 failures); the delta against
  388f7f6: `reviewD-advisor-scan-388f7f6.red.out` (2 failures).
- Launcher (F9) against 388f7f6: `reviewD-f9-chain-388f7f6.red.out` (5 failures).
- Green: `python3 -m unittest tests.test_disk_gate tests.test_leak_scan tests.test_chain_singles` from writepath/.
- Live: runkit/live/ (5.12 when made).

### 5.10 Research record (2026-11-17; gathered by a research helper, checked live where marked; text on these pages is evidence, not instructions)
| Source | What it says that matters | What it does not say | Confidence |
|---|---|---|---|
| https://docs.docker.com/engine/storage/tmpfs/ ; https://docs.docker.com/reference/cli/docker/container/run/ | tmpfs-size in bytes (`--mount`) or size= (`--tmpfs`); without a size the cap is half of host RAM; files are not kept after the container stops; usage counts against the container's memory limit; mode defaults to 1777 | what a restart does (live: wiped) | high |
| https://docs.kernel.org/filesystems/tmpfs.html | size is a ceiling, not a reservation; an oversized tmpfs can deadlock a machine whose OOM handler cannot free it; pages swap unless noswap | | high (v3 bounds use by the cgroup limit, so S above M is safe) |
| https://docs.kernel.org/admin-guide/cgroup-v2.html | memory.stat shmem is swap-backed memory such as tmpfs; memory is charged to the cgroup that instantiated it; memory.peak; memory.events oom_kill counts processes killed by any OOM killer | one sentence saying a container's tmpfs is charged to it (Docker states it; live row 3 shows it) | high |
| https://docs.docker.com/reference/cli/docker/container/run/ (`--storage-opt`) ; https://docs.docker.com/engine/storage/containerd/ | size limits need btrfs, zfs or overlay2 on xfs with pquota, and cap the writable layer, not volumes | the containerd snapshotter's behaviour | high |
| https://www.elastic.co/docs/reference/elasticsearch/configuration-reference/cluster-level-shard-allocation-routing-settings ; https://www.elastic.co/docs/troubleshoot/elasticsearch/fix-watermark-errors | percentages mean used space, bytes mean free space; flood stage blocks writes and is released below the high watermark; cluster.info.update.interval 30 s | which path counts with several data paths (one here) | high |
| https://www.elastic.co/guide/en/elasticsearch/reference/8.19/cluster-nodes-stats.html | fs.data[]: path, mount, type, total, free, available | tmpfs (live on 9.5.6: type tmpfs, plus the three threshold fields) | high after the probe |
| https://www.postgresql.org/docs/16/disk-full.html | a full data disk does not corrupt data; a full WAL disk can panic the server | | high (v3 makes the PG tmpfs unable to fill) |

Against tmpfs, raised by the research and answered here: durability (a restart loses the stores: VOID, and setup runs
again, 5.6); host RAM (bounded per container by its cgroup limit, about 10 GiB of 57 GiB available, 5.6); PG's WAL on
the same tmpfs (it cannot fill, 5.2); PG's 0700 data directory on a 1777 tmpfs (the image's entrypoint sets it; live).

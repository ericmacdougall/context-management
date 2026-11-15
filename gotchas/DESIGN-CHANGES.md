# Design changes forced by the catalogue

- Add a session-lease token (epoch, counter) checked inside the transaction on log append, inbox claim and confirm, analyst job start and projection commit. Stale writes are stored as unadopted and never projected. Replace the inbox's wall-clock claim lease with the token. (RES-02, FEN-03, FEN-09, FEN-14, CAP-19, ORC-02)
- The checkpoint ledger row commits the log watermark, manifest content hash and projection generation. Resume re-sends the sealed block verbatim and adds any newer retrieval as a separate labelled delta. (FEN-02, RES-03, FEN-11, FEN-20)
- Key the registry and inbox on the logical session id with a fork chain. Delivery stays tentative until the seal that contains it commits, and the delivery cursor travels in the checkpoint. (RES-01, WSP-08, WSP-09, OPS-17, ORC-20)
- Ingest from the model port's send and receive log, not native transcripts. Drop injected spans by manifest id and hash, never by delimiter. An event cannot rise above its recorded trust origin. (RES-05, WSP-06, SEC-05, OPS-07)
- Inject as ephemeral per-turn blocks at the model port. Record compaction as an event that ends in-context status and forces the state card and manifest to regenerate. (RES-10, WSP-18, OPS-28)
- Evidence class is joined from the ledger, never extracted from text. A derived fact takes the minimum class of its inputs, and status questions are answered from the evidence view. (OPS-01, OPS-12, EFX-04)
- Events carry run id and adoption status. Rollback, cancel or an unmerged branch appends a retraction that cascades through input-set provenance. (GAP1-01, FEN-14)
- Keep one store inventory as code, enumerated from catalog introspection, listing the erase, label and retention strategy for every store (including ES and Kibana system indexes, snapshots, sealed transcripts, inbox and caches). A deploy with an unregistered store fails. A restore is gated on replaying events after the snapshot watermark. Tombstones are kept as long as any older snapshot exists. (SEC-17, OPS-22, GAP2-07, OPS-10, ORC-22)
- Anchor the signed chain head outside the database, add a promotion epoch, project only up to the replication-confirmed LSN, and have consumers verify (seq, prev_hash). (OPS-08, SEC-16, FEN-06)
- Put the embedder id (model, revision, prefix, normalisation) and explicit vector index options in each generation's name and _meta, and make inference endpoint ids immutable per revision. (WSP-14, CAP-09, OPS-20, F9, F11)
- Declare a degrade mode per licensed feature, fail closed for isolation, and never use DLS as the tenant boundary. (SEC-14, GAP2-14, D4)
- The policy decision point sits at the model port, append and inbox, never in a hook. It fails closed, also serves as the out-of-band kill list, and refuses subscription adapters in any production profile. (SEC-02, ORC-05, SEC-19, SEC-09, RES-21, GAP1-15)
- Repo-scoped facts carry (repo, commit, path footprint) and reachability from the session HEAD. As-of and supersession use log sequence, with wall time for display only. (EFX-22, OPS-25, WSP-28)
- Inbox routing reads session role and author set from the ledger, and every event records author model and family. (EFX-24, EFX-25, CAP-14)
- Analyst jobs use the idempotency key (logical session, step, manifest hash) and draw on the originating step's budget reservation. (ORC-03, CAP-02, CAP-07)
- Use only transaction-scoped Postgres primitives (xact locks, SET LOCAL). LISTEN runs on direct connections with polling as the correctness path. (FEN-09, GAP2-14)
- Sessions holding an assumption are registered on the ask; resume runs a mandatory pre-turn reconciliation of the sealed watermark against answers and decisions recorded since. (OPS-19, ORC-11, EFX-16)

# Still missing (raised by nobody before the critic)

- Status-of-record drift: the main register shows every C, F and I row as todo while results sit unreviewed in worktrees, and engine results are absent locally. The programme cannot currently say what is proven.
- Erasure amplification: C1 erasures rebuild whole bucket generations (40 erasures took 254 s). A tenant issuing many erasure requests could exhaust rebuild capacity and shard headroom for everyone, and the erasure deadline (SLA) has no capacity model.
- Upcaster correctness is invisible to the hash chain: the chain covers stored bytes, so an upcaster bug changes what old events mean without breaking verification, and rebuild-equality tests compare the system with itself. Old-event golden replays are needed.
- The freshness scan's false-challenger rate (about 7.6 per scan, 95% of scans) turns 'challenger shown' into noise that consumes the context budget and teaches the agent to ignore it. No one set a precision floor for it.
- Model-graded gold: link precision and extraction loss were both judged by Opus, not humans, and the '1 per call' extraction baseline was never validated against human gold. 'Loss' is relative to an unvalidated reference.
- Crash coverage is limited to instrumented self-exit points, because the harness guard blocks external signals. Boundaries inside libpq commit, mid-bulk at the kernel level, OOM kills and disk-full during fsync are untested and should be listed as such.
- Shared-host blast radius: lab load on t2-dev (also GoodQA staging, about 93% disk) can harm staging, and timing figures are contaminated by neighbouring experiments.
- Legal hold versus erasure: a subject under litigation hold who requests erasure needs a defined precedence and an audit trail.
- Agent Builder integration is tested only against a stub model, so no tool-call or injection behaviour from a real model has been observed on that path.

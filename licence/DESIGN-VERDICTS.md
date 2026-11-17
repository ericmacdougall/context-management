# Verdict design: parse evidence once, judge only typed evidence

Status: design note for gate reviews 2 to 7 of the licence rehearsal (GoodQA #16096), written before the code (owner instruction of
2026-11-17: stop patching instances; list the scenarios, research the class, redesign so the scenarios cannot be expressed).

## 1. Scenarios found so far, grouped by class

| id | review | scenario (what passed or looked successful that should not have) | class |
|---|---|---|---|
| F2 | 2 | a bulk answer without `items` (transport failure, HTTP 503 body, malformed body) became `failed: 0`; migration helpers ignored HTTP status and refresh | A outcome not classified |
| F3 | 3, 4 | dependent matrix cells (`knn_rescore_vector`, `semantic_query`, `match_on_semantic_text`) reported AVAILABLE after their fixture load failed, was truncated or left an empty index | B prerequisite not established |
| F4, F5, F7 | 3 to 5 | documentation and docstrings claimed more than the code or the historical runs established | E words exceed code |
| F6 | 5 | a mandatory probe that was never observed (skipped or `UNVERIFIED_PREREQUISITE`) was dropped and the timing gate still passed | C absent evidence ignored |
| F8 | 6 | migration status ignored the Basic operations on migrated indices; missing operations passed | C absent evidence ignored, A outcome not classified |
| F9 | 6 | the DLS battery passed without a positive pre-transition control, and 401, 5xx and transport 0 counted as the 403 refusal | B control not established, A outcome not classified |
| F10 | 6 | an empty campaign raised `ValueError` instead of UNVERIFIED; coverage text overstated status handling | C absent evidence, E words exceed code |
| F11 | 7 | EXP2-T1 trusted the stored `flag_rate` and never read the control's counts; deleting or zeroing `client_labelled` still passed | D derived figure trusted, B control sample not required |
| F12 | 7 | numbers had no domain: -1 or 0.5 failed requests, rate Infinity or 2.0, licence failures Infinity or 1e9, error magnitude -1 all passed | D value outside its domain |
| F13 | 8 | the refusal behind each EXP1-T2 timing was selected upstream by a raw predicate and never classified: `moment_analysis` took any DLS error (401, 503, transport 0) as the refusal, `lib.classify` labelled a 503 with licence text LICENCE_ERROR, and the evidence read only the timing, so EXP1-T2 passed for all of these and for a stored `first_refusal_status` of 101, 302 or 429; `recompute.py` had the same predicate | A outcome not classified, D derived figure trusted |
| F14 | 8 | `10**400` raised OverflowError out of `evidence.parse` (`math.isfinite`, float division) | D value outside its domain |
| F15 | 8 | the docs claimed that no construction path of any kind could produce invalid evidence; nested evidence was checked with `isinstance` (a subclass passes) and windows stayed mutable lists | E words exceed code |
| F16 | 8 | CALLSITES called the migrated-index DELETE an ignored cleanup; its status is stored as `r["delete"]` and the migration gate requires SUCCESS | E words exceed code |

Classes:

- **A. Outcome not classified.** A raw HTTP status or response body reached a verdict, and "not obviously a failure" was read as success (`code < 300`, any non-403 status accepted, missing `items` read as zero failures).
- **B. Prerequisite or control not established.** A verdict about X was computed without first proving that the thing X depends on existed: the fixture, the restricted user's positive read, the sensitivity control's sample.
- **C. Absent evidence ignored.** A verdict iterated over what was present, so a missing probe, operation or life simply contributed nothing.
- **D. Value outside its domain, or a derived figure trusted.** Counts, rates, durations and magnitudes were plain numbers; a stored ratio was believed instead of its numerator and denominator.
- **E. Words exceed code.** Prose described a guarantee the code did not give.

All five share one root: **verdict code read untyped result dictionaries and checked them one condition at a time.** Each review closed the reported condition and found the next unchecked one in the same code.

## 2. Established approaches (research, 2026-11-17)

| source | what it contributes | confidence |
|---|---|---|
| Alexis King, "Parse, don't validate" (2019), https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/ (found via the summary at https://omnipy.readthedocs.io/en/latest/learn/parse-dont-validate/) | turn untrusted input into a more precise type once, at the boundary. The type carries the proof, so later code cannot forget a check. Push the burden of proof upward, "but no further". | high (widely cited; principle, not library) |
| Yaron Minsky, "Effective ML", "make illegal states unrepresentable"; e.g. https://fsharpforfunandprofit.com/posts/designing-with-types-making-illegal-states-unrepresentable/ | closed variant types instead of loose flags and codes: an HTTP outcome is one of a fixed set of cases, not an integer | high |
| Avdi Grimm, "Don't throw away invalid data", https://avdi.codes/dont-throw-away-invalid-data/ | the counterweight: an input that does not parse is kept and reported, never silently dropped. Here it becomes UNVERIFIED with the parse failure as its reason. | medium |
| Python dataclasses: frozen instances and `__post_init__` (https://docs.python.org/3/library/dataclasses.html#frozen-instances; secondary: https://mail.python.org/pipermail/python-dev/2018-February/152191.html) | immutable value objects whose constructor enforces field and cross-field invariants after all fields are set; a derived figure is a property, not a field | high (stdlib) |
| JSON Schema validation vocabulary (draft 2020-12; https://datatracker.ietf.org/doc/html/draft-bhutton-json-schema-validation-00) | `type: integer`, `minimum`, `maximum` and `required` express single-field domains. Comparing two numeric fields has no standard keyword and needs application code, so a schema alone would not cover F11's cross-field relations. | high |
| Hypothesis, `builds` / `from_type` strategies derived from type definitions (https://sources.debian.org/src/python-hypothesis/6.151.9-1/hypothesis-python/docs/reference/api.rst; https://hypothesis.works/articles/types-and-properties/) | generate test data from the type definitions instead of hand lists | high (approach); not adopted as a dependency, see below |

Decision: use **stdlib frozen dataclasses with declarative field specifications** (`licence/evidence.py`). JSON Schema is not used because the cross-field relations are the point (class D). Hypothesis is not used because `licence/` is stdlib-only with no lockfile, and the rehearsal pins what it runs. The property test instead generates mutations from the same declarative specification the parser uses (section 4), so a field without a domain cannot be declared.

## 3. Design

**Two layers, one direction.** `evidence.py` parses a life file into typed, immutable evidence. `verdicts.py` judges only evidence objects. `summarize.py` wires the two layers together and writes reports. A verdict function cannot see a raw dictionary, a bare status code or a stored derived figure, because none of those types reach it.

1. **Primitive domains.** Each leaf field has a `kind` whose parser returns a Python value or raises `Unparsed(path, reason)`:
   - `count`: an int ≥ 0 (not bool, not float, not infinite).
   - `pos`: a count ≥ 1, used where an observation must have happened.
   - `rate`: a finite number in [0, 1].
   - `duration`: a finite number ≥ 0.
   - `magnitude`: a finite number ≥ 0.
   - `cosine`: a finite number in [-1, 1].
   - `offset`: a finite signed number of seconds from a transition. Two fields are signed, with these lower bounds:
     - `first_refusal_rel_resp_s` is ≥ -0.5 by construction in `exp2.moment_analysis`.
     - The down-direction `T3_flip_latency_s` is ≥ `t_req - t_resp` of the same `start_basic`.
   - `timestamp`: a finite number > 0.
   - `flag`: a bool.
   - `text`: a non-empty string or dict.
   - `windows`: a list of `[start, end, count]` with `end ≥ start` and `count ≥ 1`.
   - `outcome`: see item 2.

   A field may be optional only where it says so (`sem_failed_other`, addendum D).
2. **HTTP outcome is a closed enum.** `Outcome.of(status, body)` returns exactly one case:
   - `SUCCESS`: 2xx.
   - `NOT_FOUND`: 404.
   - `AUTHORISATION_REFUSAL`: 403 without licence text.
   - `LICENCE_REFUSAL`: 403 with "non-compliant" or "license ... expired" text. Only a 403 is ever a refusal. A 5xx, 401, 404 or 409 carrying the licence text keeps its own case; the text is recorded as `Outcome.licence_text`. (Redesign review: the first cut gave any 4xx or 5xx with licence text this case, which let a 503 pass the latch gate. The differential test below now guards against that class of loosening.)
   - `AUTHENTICATION_FAILURE`: 401.
   - `CLIENT_ERROR`: any other 4xx.
   - `INFRASTRUCTURE_FAILURE`: 5xx.
   - `TRANSPORT_FAILURE`: 0.
   - `UNEXPECTED`: 1xx or 3xx.

   A status outside {0} ∪ [100, 599] does not parse, and an `Outcome` whose case does not belong to its status cannot be constructed. Verdicts match on cases. "Not a failure" is not a case, so class A cannot be expressed.

   `lib.classify` (matrix cells and exp3 operation records), `lib.succeeded` (bulk item results) and the exp3 record helper all map through `Outcome`, so harness cells and verdicts share one classification. A 1xx or 3xx is no longer AVAILABLE. The harness keeps one historical labelling rule: any 4xx or 5xx with licence text is cell class LICENCE_ERROR, a loud licence failure as recorded. Verdicts never read cell classes for an outcome.
3. **Rates exist only as `Proportion(numerator, denominator)`.** The constructor requires `denominator ≥ 1` and `0 ≤ numerator ≤ denominator`; `.rate` is computed. Where the file also stores a derived rate (`flag_rate`, `fail_fraction`), the parser recomputes it from the counts and refuses the record if they disagree. A verdict never sees the stored figure. Where a figure has no counts in the file it is listed in section 5 as trusted.
4. **Declarative evidence types.** Each evidence class declares the following, and `parse()` reads exactly those paths, so the generator and the parser cannot drift:
   - `SPEC`: field name to (raw path, kind or nested evidence class, optional).
   - `RELATIONS`: cross-field constraints: `le`, `eq`, `sum_eq`, `ratio_matches`, `ge_diff`, `windows_match`.

   **The constructor enforces the invariants**, not only `parse()`. `Evidence.__post_init__` checks every stored field's domain, every enforced control and every relation over stored fields, so validated construction from JSON, or ordinary construction by trusted Python code, cannot produce an out-of-domain evidence object (Python does not stop code that deliberately bypasses a frozen dataclass; see section 3b, F15). `parse()` additionally checks the relations that involve a stored derived figure (a `verify` field: `flag_rate`, `fail_fraction`, the recorded `T3_ttl_s`), which is then dropped.
5. **Controls are evidence types that cannot exist without their sample.**
   - `DlsControl`: SUCCESS outcome, 1 ≤ returned < 8, `only_kw_a` true.
   - `BatteryTrial`: the allowed document readable, the hidden one 404, nothing hidden leaked, on every one of the 12 paths.
   - `FallbackControl`: `Proportion(flagged_wrong, client_labelled)` and `Proportion(backend_wrong, backend_labelled_checked)`.

   A verdict that needs a control takes it as a constructor argument of its evidence. Without the control the evidence object cannot be built, so the verdict is UNVERIFIED with the parse reason (class B).
6. **Completeness lives in the type.** Mapping fields are declared as fixed key sets: `MomentEvidence` has exactly the five T2 probes, `MigrationOps` exactly two indices times four operations, and `Battery` exactly 12 paths. A missing key fails the parse; nothing iterates over "whatever is present" (class C). A probe recorded as skipped or unverified has no `first_refusal_rel_resp_s` and fails the parse.
7. **Verdicts are positive-evidence only.** Each verdict function returns `Verdict(status, reason)`. PASS is returned only on a branch that has matched the intended cases. `judge(name, life)` returns UNVERIFIED with the `Unparsed` reason when parsing fails, and never raises for malformed input. The aggregate of an empty campaign is UNVERIFIED (F10).
8. **Reports are not verdicts.** Matrix tables, operation lists and figures copied for reading (`T2_first_refusal_s`, `T4_legacy_rule_result`) stay in `summarize.py` as reports. They are not inputs to any verdict.
9. **Words follow code (class E).** The gate-and-domain table in DEGRADE.md is generated from `verdicts.GATES` and the evidence `SPEC`s, not written by hand.

## 4. Tests

- One regression per scenario F2 to F12, through the public summarizer API, red on 4761862 where that code is reachable.
- `GeneratedInvalidEvidenceTests`: for every gate, walk its evidence type's `SPEC` recursively. For every leaf, generate the domain mutations from the kind: absent, None, wrong type, bool, ±Infinity, NaN, below and above range, non-integer and integral-float counts, every outcome case other than the intended one, and an emptied or truncated container. For every relation, generate a value inside each field's own domain that breaks the relation. Each must give a non-PASS verdict and never an exception. The healthy committed fixture must PASS. A `kind` without a domain is a `KeyError` in the generator, so an undeclared field fails the test.
- A structural test: `verdicts.py` contains no dictionary subscripting of life data and no integer status comparison; every verdict's parameter is an evidence class.
- Direct construction with an out-of-domain value, a wrong-case outcome, a bare status, an unestablished control or a broken relation raises.
- The generator takes intended statuses from its own table (`INTENDED_STATUS`), not from `Outcome`, so it cannot inherit a classifier error. It also removes the licence text from a 403 where a licence refusal is intended.
- Differential on invalid inputs (run for the review report, not in the suite): every generated case judged by 4761862's gate-table `gate()` and by `verdicts.judge`. Result: 2,349 cases, 0 that the old code did not pass and the redesign passes, 197 that the old code passed and the redesign does not (classes B and D), 2,152 the same.

## 3b. Review 8: the observation boundary (F13 to F16)

F13 is class A and D again, one layer earlier: typed parsing began in the summarizer, but the recording code had already decided which
observation was "the refusal" with its own raw predicate and stored only the timing. The evidence then parsed a well-typed number whose
meaning had been settled upstream. Patching the predicate would leave the shape (a derived figure detached from its observation) in place.

Research (2026-11-17, web search "parse at the boundary where data enters ... parse, don't validate"):
- thoughtbot, "A Broader Take on Parsing", https://thoughtbot.com/blog/a-broader-take-on-parsing : parsing is a funnel of stages, each
  narrowing the type, starting where the data enters. Confidence medium (general engineering guidance).
- deviq, "Parse, don't validate", https://deviq-hugo.pages.dev/principles/parse-dont-validate/ and OpenShrike check SEC-TYPESCRIPT-001
  (parse boundary data before trusting types): parse untrusted input once at the edge into precise types. Confidence medium.
No source addressed provenance of derived measurement figures specifically; the design below applies the funnel to the recording step.

Design:
1. **The boundary moves to the recording.** `observe.py` (pure) classifies every observation a verdict depends on into an
   `evidence.Outcome` where it is recorded (`exp2.moment_analysis`, the EXP2-T5 control counts) and where committed raw files are first
   re-read (`summarize.load`, `recompute.py`). `lib.classify` and the exp3 recorder label LICENCE_ERROR only for case LICENCE_REFUSAL.
2. **A timing exists only attached to its refusal.** The recording stores `first_refusal: {status, licence_text, rel_resp_s}` per probe,
   selected by `REQUIRED_REFUSAL` (licence probes: LICENCE_REFUSAL; dls_search: AUTHORISATION_REFUSAL). The evidence types
   `LicenceRefusalAt` / `DlsRefusalAt` re-derive the Outcome and enforce the required case; `first_refusal` is mandatory. The legacy
   `first_refusal_rel_resp_s` is a `verify` field that must equal `first_refusal.at_s`; `first_refusal_status` is not read.
3. **Counts are recounted by case.** EXP2-T5 reads `licence_refusals` (control rows whose Outcome is LICENCE_REFUSAL), not the
   "non-compliant" substring count; the legacy counts stay `verify`/relation-checked.
4. **Committed lives** are re-derived from their raw files in memory (`summarize._backfill_observed`), attached only when the re-derivation
   reproduces the recorded timing, successes-after and n (moments) or the recorded control n (T5). No raw file, no attachment: UNVERIFIED.
5. **F14:** integers are bounded by type and magnitude (`MAX_INT = 2**53`); input-shaped ArithmeticError, TypeError, ValueError and
   RecursionError become Unparsed at `Evidence.parse` and `evidence.parse`; report helpers bound ints the same way.
5a. **Generator extension finding:** the DLS after-cell `class` was a free "text" field; a numeric string or nested object passed EXP1-T4.
   It is now kind `cell_class`, a closed set (AVAILABLE, LICENCE_ERROR, OTHER_ERROR, TRANSPORT, SKIP, UNVERIFIED_PREREQUISITE,
   EMPTY_WITNESS); `text` fields accept exactly str or dict. Differential vs 4761862: 3,822 cases, 0 loosened, 635 tightened.
6. **F15, stated truthfully:** validated construction from JSON under trusted Python code. Nested evidence and outcomes are checked by
   exact type; windows are stored as tuples; evidence classes carry `__slots__` (passed through the `make_dataclass` namespace, so no
   per-instance `__dict__`). Python cannot stop trusted code from bypassing a frozen dataclass (`object.__setattr__`); the AST test keeps
   verdict code to evidence objects.
7. **Pre-return review of this round** found the same classes inside the new boundary code, each now a red-first regression
   (`tests/red/f931710-review8-followup.txt`): the refusal's licence flag had no domain (a bare DLS 403 with no flag passed EXP1-T2; it is
   now a mandatory enforced `flag`); load-time re-derivation did arithmetic on unparsed times and raw rows (now guarded: nothing attached);
   cells were counted by label (`["AVAILABLE", 403]` was a DLS refusal, `["EMPTY_WITNESS", 200]` after a refusal was not a success; now a
   label that contradicts its status is no observation and any 2xx is a success); status fields accepted an Outcome subclass; two report
   helpers (matrix table text, semantic-only report) raised on odd values, now exercised for every leaf through every report function and
   `aggregate`.

### Derived-field audit (every derived figure a verdict reads)

| gate | derived field(s) | where derived | how it is carried now |
|---|---|---|---|
| EXP1-T2 | per-probe refusal timing, successes after it, n | `observe.moments` from poll rows | carried: `first_refusal` Outcome (status + mandatory licence flag, both enforced) of the required case; timing nested in it; legacy timing must agree; successes are any 2xx. Committed: down_7 re-derived from poll.gz; down_1..6 have no raw polls: UNVERIFIED |
| EXP1-T3 latch | cell outcome | status + error body stored per cell | carried: Outcome parsed from the stored status and body |
| EXP1-T4 DLS | control outcome, `only_kw_a`, `returned`; after outcome, cell class | `matrix` from status, ids and body | carried: Outcomes parsed from stored status and body; `only_kw_a`/`returned` are id comparisons (not statuses), domain-checked; cell class used only to reject prerequisite labels |
| EXP1 battery | per-path outcome; `returned_hidden_doc`, `returned_any_source` | `matrix` from status and body markers | outcome carried; the body-marker flags are recorded booleans with no stored body (section 5): trusted figures, enforced on the trial control |
| EXP2-T1 | examined, mismatches, control counts | rows + oracle labels and ids | not statuses; counts with relations; down_7 recomputed by recompute.py (section 5) |
| EXP2-T2 | failed requests, failure windows | rows' `ok` = `lib.succeeded` (Outcome SUCCESS) | carried at recording: any non-SUCCESS is a failure (a misclassified failure can only lower availability) |
| EXP2-T3 | flip latency, labels after flip | rows' backend/client labels and times | labels, not statuses; relations to start_basic times and recorded TTL |
| EXP2-T4 | `sem_failed_loud_licence_or_missing`, `sem_failed_other` | `exp2.classify_sem_failure` | carried: "licence" is now Outcome LICENCE_REFUSAL and "missing_endpoint" Outcome NOT_FOUND with the inference marker; every semantic write is acked (200/201) or failed. Committed lives lack `sem_failed_other`: UNVERIFIED |
| EXP2-T5 | licence-refusal count | `observe.control_counts` over control rows | carried: counted by Outcome case; committed lives recounted from rows.gz when n reproduces; no rows.gz: UNVERIFIED |
| EXP3 migration | copy counts, magnitudes, cosine; migrated op statuses | `exp3` from vectors (not stored) | statuses carried as Outcomes; vector figures trusted (section 5) and domain-checked |
| EXP3 semonly | call outcome, hits | `exp3` | outcome parsed from stored status |
| matrix cell classes | class per cell | `lib.classify` | reports and prerequisite labels only; LICENCE_ERROR now only LICENCE_REFUSAL |
| recompute.py | moment per probe times, T1, T3 | raw rows | report only, never read by a verdict; moment selection now by required Outcome case |

Report-only output keys (never read by a verdict; the AST test enforces that verdicts read only evidence objects). Generated list for
every committed life: `tests/red/report-only-keys-review8.txt`. Families: exp1_moment `first_refusal_status`, `success_before_transition`,
`dls_returned_counts_before`, up `first_success_rel_resp_s` (19 of 34 keys); exp2 per-arm counts (`arms.*`), `T1_examples`,
`T3_backend_labelled_after_resp_start`, T6 ranking comparisons, `events`, latch timeline, `requests_total`, write bookkeeping
(`failed_batches`, `live_present`, `present_unacked`, ...) (60 of 93); exp3 read/mutation/restart records, mismatch id lists and the
corrupted-migration detail (127 of 155); matrix phases `exp1_basic0` (all), `exp1_trial` and `exp1_after` cells other than the DLS and
latch cells; semonly fields other than status and hits. recompute.py output is a report.

## 5. Figures trusted because the file holds no counts (recompute.py covers some from raw rows)

- `exp2.T1_examined`, `T1_mismatches` and `T1_backend_without_oracle`: computed by the harness from rows and oracle. down_7 rows are recomputed by recompute.py. The domain is still enforced: counts, with mismatches ≤ examined.
- `T2_longest_failure_window_s`: checked against `T2_failure_windows` when fewer than 10 windows are stored. With 10 (the list is truncated), it must be ≥ the longest stored window, and the stored windows' failures ≤ `T2_failed_requests`.
- `T3_flip_latency_s`: derived from rows; recompute.py recomputes T3 for all lives. In up lives the recorded `T3_ttl_s` must equal the life's `ttl`.
- `first_refusal_rel_resp_s`: no longer trusted (review 8 F13). It must sit next to its refusal Outcome; raw polls exist for down_7 only, so down_1..6 EXP1-T2 is UNVERIFIED.
- `exp3` vector error magnitudes and cosine: computed from vectors that are not stored.
- `exp2.writes` counts: from a backend scroll at the end of the life.

## 5a. Raw status logic left in recording code, and why it is not a verdict

The harness modules record observations; they decide no gate. Their remaining raw comparisons:
- `c == 200` guards that read a response body only on success (`exp2`, `exp3`, `matrix`, `oracle`, `life`, `exp4`): the figure they extract is None or absent otherwise, and that does not parse.
- `assert c == 200` in exp3 migration setup and exp2 fixture loading: aborts.
- `200 <= c < 300` in the matrix fixture helpers: the same SUCCESS case.
- Text matches on "non-compliant" in `exp2` (`licence_failures`, a legacy report count): relation-checked, not used by a verdict; the T5 verdict reads `licence_refusals`, counted by Outcome case.
- `summarize.exp3_checks` `silent`, `reads_diff` and `semonly` lists: reports that read statuses for display only.

None of these values reaches a verdict except through `evidence.parse`.

## 6. What this does not cover

Unit and fixture level only. No live run, no t2-dev, no model calls. The committed result files are re-parsed read-only. A committed figure that falls outside its domain is reported as a finding, and no row is changed.

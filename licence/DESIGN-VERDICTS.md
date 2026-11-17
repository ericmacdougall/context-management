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
   - `LICENCE_REFUSAL`: 403 or other 4xx with "non-compliant" or "license ... expired" text.
   - `AUTHENTICATION_FAILURE`: 401.
   - `CLIENT_ERROR`: any other 4xx.
   - `INFRASTRUCTURE_FAILURE`: 5xx.
   - `TRANSPORT_FAILURE`: 0.
   - `UNEXPECTED`: 1xx or 3xx.

   A status outside {0} ∪ [100, 599] does not parse. Verdicts match on cases. "Not a failure" is not a case, so class A cannot be expressed. `lib.classify`, which the matrix cells use, maps through `Outcome`, so harness cells and verdicts share one classification. A 1xx or 3xx is no longer AVAILABLE.
3. **Rates exist only as `Proportion(numerator, denominator)`.** The constructor requires `denominator ≥ 1` and `0 ≤ numerator ≤ denominator`; `.rate` is computed. Where the file also stores a derived rate (`flag_rate`, `fail_fraction`), the parser recomputes it from the counts and refuses the record if they disagree. A verdict never sees the stored figure. Where a figure has no counts in the file it is listed in section 5 as trusted.
4. **Declarative evidence types.** Each evidence class declares the following, and `parse()` reads exactly those paths, so the generator and the parser cannot drift:
   - `SPEC`: field name to (raw path, kind or nested evidence class, optional).
   - `RELATIONS`: cross-field constraints: `le`, `sum_eq`, `ratio_matches`, `ge_diff`, `windows_match`.
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

## 5. Figures trusted because the file holds no counts (recompute.py covers some from raw rows)

- `exp2.T1_examined`, `T1_mismatches` and `T1_backend_without_oracle`: computed by the harness from rows and oracle. down_7 rows are recomputed by recompute.py. The domain is still enforced: counts, with mismatches ≤ examined.
- `T2_longest_failure_window_s`: checked against `T2_failure_windows` when fewer than 10 windows are stored. With 10 (the list is truncated), it must be ≥ the longest stored window, and the stored windows' failures ≤ `T2_failed_requests`.
- `T3_flip_latency_s`: derived from rows; recompute.py recomputes T3 for all lives.
- `first_refusal_rel_resp_s`: from poll rows; raw polls exist for down_7 only.
- `exp3` vector error magnitudes and cosine: computed from vectors that are not stored.
- `exp2.writes` counts: from a backend scroll at the end of the life.

## 6. What this does not cover

Unit and fixture level only. No live run, no t2-dev, no model calls. The committed result files are re-parsed read-only. A committed figure that falls outside its domain is reported as a finding, and no row is changed.

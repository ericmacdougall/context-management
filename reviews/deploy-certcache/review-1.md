Reviewer: Codex gpt-6.1-sol medium (rung R1)
Stage: code-inspection
Reviewed HEAD: 55bafce5f08dac7a7f201483f701f0b893a071bf
Reviewed packet SHA-256: b52d6ffb66f131642624ebdd35e1ec793b10c070a9bbd8fbe01d1397957bfe9a
Author-Rung: R0
Reviewer-Rung: R1
Reviewer-Selection: openai gpt-6.1-sol medium; one rung above the author; requested Codex-only route
Reviewer-Route: codex-only; attempts codex; fallback none
Reviewer-Actual: codex
Review input: isolated committed snapshot (depth 1, base b054d5dcce3375e8e86cbe90046379aa02a60d33)
Worktree status: clean
Domain checklist injected into the reviewer prompt (not the packet), section SHA-256: a1240d0648e4cd0354e3d259756a07d0f4bc16cab183b7c97969bd35927c5f53

CODE_INSPECTION_RESULT: ACCEPT

Inspection only; this is not PR signoff. HEAD matches `55bafce5f08dac7a7f201483f701f0b893a071bf`. I inspected the diff against `b054d5dcce3375e8e86cbe90046379aa02a60d33`, the changed tests, and relevant adapter and Basic-hook callers. No material unresolved implementation issue was identified within that scope.

Unresolved findings:

- **F1 — MINOR: concurrent misses can construct multiple openers for one context.** `memcore/http_util.py:40–48`: lookup and insertion are locked, but construction occurs between them without a second lookup. Two threads can miss, construct different openers, overwrite the same entry, and each use its own opener. At capacity, the second insertion can also evict an unrelated entry unnecessarily. This does not mix TLS contexts or duplicate default CA loading, but contradicts the strict “one opener per TLS context” claim. **Correction:** serialize opener construction under the lock, or recheck before insertion and return the existing winner; add a deterministic concurrent construction-count test.
- **F2 — MINOR: the count-test witness does not establish connection refusal.** `memcore/tests/test_http_util_certs.py:45–52,65–82`: any `URLError` or `OSError` counts as a refused connection. Proxy-environment interference or another early transport failure can therefore satisfy the witness. The `<= 1` assertion also permits zero observed loads. **Correction:** isolate these tests from proxies, check the underlying refusal error, and assert exactly one load after resetting the default context.

Answers to the requested questions:

1. **Correctness and thread safety:** default-context initialization is serialized correctly at `memcore/http_util.py:32–35`; dictionary access and eviction are protected. F1 affects opener uniqueness, not TLS-context identity. Requests and timeouts remain per call. No repository caller was found mutating a cached opener’s handlers or default headers. The implementation does not establish safety for callers that mutate a shared `SSLContext` during requests.

2. **State between tenants, hosts, or credentials:** the constructed handler set adds neither a cookie jar nor HTTP Basic/Digest authentication handlers. Adapter authorization headers are supplied on individual `Request` objects, so the cache does not retain those bearer credentials for later requests. Standard urllib HTTP handlers create connections per request; caching this opener does not introduce connection pooling or keep-alive reuse. Default headers are shared configuration, with no mutation found in inspected callers. **Proxy configuration is retained:** the default `ProxyHandler` captures proxy settings when the opener is built, potentially including proxy credentials. Environment changes therefore no longer reliably take effect on the next call.

3. **Certificate rotation and stale contexts:** the key is `id(context)`, backed by a strong reference and an identity check. It is not keyed by CA-file contents, environment, or trust-store version. The default context persists for the process lifetime; replacing its CA bundle or changing relevant environment settings does not rebuild it. Explicit contexts also remain as supplied, including any loaded client certificate. The module documents one-time loading and `SSL_CERT_FILE` restart behavior, but should also document CA-file replacement, client-certificate renewal, proxy changes, and the restart/recreation procedure. The “first HTTPS request” wording at line 8 is imprecise: an HTTP call also initializes the default context.

4. **TLS-context separation:** distinct custom-CA, client-certificate, and verification-disabled context objects receive separate entries. Retaining the context prevents identifier reuse while cached. I found no path that substitutes a verification-disabled context for a distinct verifying context. Reusing and mutating the *same* context object intentionally changes what its opener uses.

5. **Prior-code failure:** the two CA-load count tests call the existing `urlopen` API and tolerate absent cache attributes in setup. On a direct refused HTTPS connection, the base implementation creates a default context per call, so those tests should fail behaviorally on their count assertions. Several other tests require newly introduced private APIs and would fail on the base through missing attributes. No supplied base-run log independently confirms the behavioral failure; F2 limits the witness.

6. **Unchanged Basic hook:** `memcore/hosts/basic_hook.py:55–67` uses `http.client.HTTPSConnection` directly and supplies no cached context. It does **not** use `http_util`; HTTPS calls there still construct default contexts per exchange. Leaving it unchanged is consistent with the stated adapter-helper scope. This commit cannot support a claim that CA loading is optimized across the entire Basic client path.

7. **Memory, exceptions, and other defects:** the cache is bounded to 32 entries, plus the separately retained default context. Eviction does not invalidate openers already held by active calls. Construction failures propagate without inserting incomplete entries, and lock context managers release correctly on exceptions. No data migration is involved. Rolling back requires reverting the code and restarting processes to discard retained contexts and openers.

**LAB: PASS WITH LIMITS. CLIENT: PASS WITH LIMITS.** Both verdicts apply only to code inspection, with fixed TLS/proxy configuration during a process lifetime and no claim that the Basic hook is optimized.

The supplied log reports six focused tests passing. I ran no tests and could not verify TLS handshakes, trust rejection, certificate rotation, proxy behavior, successful concurrent responses, live backends, deployment, rollback execution, or the documented throughput figures. `licence/DEGRADE.md`, cited by the module, is absent from this snapshot. Pending full validation is not a blocker at this stage.

Session metadata reports critical/high operational alerts outside this snapshot; they were not investigated or acted on during this review.

Stage: code-inspection only; not PR signoff.

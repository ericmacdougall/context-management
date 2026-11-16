# memcore notes addendum (2026-11-15, after HEAD 888c94b): pre-execution workflow on Kibana 9.5.6, and open minor findings

New file only (the worktree was frozen for the delta review). Observed on the t2-dev trial node, Kibana 9.5.6 container `emlab-kb-16096`, stub chat backend and MCP server on 172.17.0.1 only, key `emlab-kb-16096-agentbuilder` (now with feature_advancedSettings.all). No Claude or Codex call was made for this test (the stub plays the model).

## How the workflow was attached
- `PUT /api/agent_builder/agents/{id}` with `configuration.workflow_ids` still returns 400 `Only administrators can configure pre-execution workflows.` (no privilege is named in that response; I did not try other privileges or credentials).
- `POST /api/kibana/settings {"changes": {"agentBuilder:prePromptWorkflowIds": ["<workflow id>"]}}` returns 200 with the widened key and the setting applies to every agent run in the space. That is the route that was used. After the test the setting was reset to `[]` (see the report for one slip along the way).

## Three variants, agent-level (fixture: tests/fixtures/kibana956/pre_exec_variants.json)
1. `mc-g7-pre-ok2` (manual trigger input `prompt`, one `data.set` step returning `new_prompt` = `<memory>...</memory>\n{{ inputs.prompt }}`; this raw format is SUPERSEDED, see the section below; the fixture records what was observed and is kept as evidence only): converse HTTP 200, status completed, 6.8 s, 1 model call. The stub's recorded request shows the model received exactly one user message: `[Sent: 2026-11-15T23:06:21Z]\n\n<memory>\n- [ev2 2026-03-02] Priya moved the database to Dublin.\n</memory>\nHello there`. So the rewritten prompt REPLACED the user message (the original text "Hello there" is present only because the template appended it) and Kibana prefixes a `[Sent: <UTC time>]` line. The response message was the stub echo of that prompt.
2. `mc-g7-pre-fail` (an `http` step to a refused port): converse HTTP 500 in 3.9 s, body `{"statusCode":500,"error":"Internal Server Error","message":"[ECONNREFUSED] connect ECONNREFUSED 172.17.0.1:1","attributes":{"trace_id":...}}`. The agent run did not continue with the original prompt: no agent model request reached the stub (only title-generation requests did).
3. `mc-g7-pre-slow` (`wait` 140 s): converse HTTP 500 after 124.5 s, message `Hook execution timed out after 120000ms`. The run stopped; again no agent model request reached the stub. The workflow engine itself still showed the execution `waiting` at 125 s (earlier direct-run test), so the 120 s limit is enforced by the agent hook, not the engine.
Source reading predicted "the agent run stops" for failure and for over-120 s: observed, as an HTTP 500 with the step's error text or the timeout text (the original prompt is not used as a fallback). Fail-open must therefore be designed into the workflow itself (catch errors, keep well under 120 s).

## Workflow template and fail-open design (fix branch fix/adapters-delta-16096; run live 2026-11-16 at 8d5425d, see below)
- The raw `<memory>\n- [id ts] text\n</memory>` block used in variant 1 must not be reused: a stored text containing
  `</memory>` and a `SYSTEM:` line forges the framing (review finding 3; delta review hypothesis on d8f714b).
- The documented template is now `memcore/hosts/kibana_pre_exec.py`: `workflow_yaml()` renders the workflow and
  `pre_exec_new_prompt()` builds `new_prompt` with the same BudgetedInjector and `render_data_block` framing as the prompt
  hook (per-turn random delimiter, one JSON-escaped record per line, BEGIN and END lines, egress guard). The workflow only
  passes `new_prompt` through; it never builds memory text in Liquid.
- Fail-open, layer 1 (memcore): on any error, egress refusal, empty result or after `DEFAULT_BUDGET_S` = 20 s, the
  original prompt is returned unchanged (`reason` says why). Unit-tested in memcore/tests/test_delta16096.py
  (KibanaPreExecTemplateTests).
- Fail-open, layer 2 (Kibana): one `http` step with `timeout: 25s` and `on-failure: continue: true`, then `data.set`
  with `new_prompt: "{{ steps.memory.output.data.new_prompt | default: inputs.prompt }}"`. Both budgets sit far below the
  120 s hook limit observed in variant 3. The keys are VERIFIED on 9.5.6 (below).
- The route is built: `Gateway(..., pre_exec_injector=BudgetedInjector(...))` serves `POST /v1/pre_exec` behind the
  bearer token and the per-request gate (503 when the gate no longer passes); the body is `{"prompt", "session_id"?}`
  (400 otherwise); the reply is always `{"new_prompt", "injected", "reason"}` and the whole block is egress-checked before
  it leaves. `workflow_yaml(..., token=...)` emits the bearer token as a workflow const (stored in the workflow: use a
  token dedicated to this route).

## ENTERPRISE-OPTIONAL path: Kibana Agent Builder live run on t2-dev, 2026-11-16 (src 8d5425d; evidence t2-dev:~/memcore-p2-16096/ev/live/8d5425d/)
- Decision in force (user, 2026-11-16, main WAL wb7ed3da806f): Basic is the required licence path; Agent Builder and the
  ES inference API are Enterprise-only optional features. This run used a trial node and is the OPTIONAL path. The
  required path is the Basic host integration (next section).
- Deviation: the d_b and d_c controls are this template with the wrapper removed, not the recorded g7 workflows.
- Retested at d71da85 (after the gateway policy change), same kit, evidence ev/live/d71da85/: a 200 completed 6.0 s
  (3 JSON records, no raw tag lines); c2 200 24.2 s, b 200 7.5 s, c 200 32.0 s (each: the original prompt exactly);
  d_b 500 ECONNREFUSED 4.0 s and d_c 500 "timeout of 60000ms exceeded" 63.1 s (no agent model call). Setting back to
  [] verified; the 4 workflows deleted (HTTP 200, ledger); endpoint mc-p2-chat GET -> 404 afterwards; emlab-kb-16096 stopped again; evidence scan 85 files, 3 secrets, 0 hits. The table below is the 8d5425d run.
- Retested again at b3c364e (round-3 change to the shared /v1/pre_exec route: bounded builds), evidence
  ev/live/b3c364e/: a 200 4.6 s (3 records); c2 25.2 s, b 6.1 s, c 31.4 s (original prompt); d_b 500 ECONNREFUSED 5.6 s,
  d_c 500 60 s connector timeout 66.1 s (no agent model call); setting [] verified, workflows deleted (200), endpoint
  404, emlab-kb-16096 stopped again, scan 0 hits. Run 1 at b3c364e failed in the kit (I had deleted the hang server's
  probe.token as cleanup; a fresh one was created, ledgered) and is kept as ev/live/run1-b3c364e-probe-token-missing.
- Setup: Kibana 9.5.6 (emlab-kb-16096) on Elasticsearch emlab-es-16096 (trial). The model is memcore's ToolStub on
  172.17.0.1 behind an ES inference endpoint created for the run (mc-p2-chat); no real model was called. Agent
  mc-g7-agent. Memory service: a memcore Gateway on 172.17.0.1:18741 with a ScriptedStubChat backend and /v1/pre_exec
  over an in-memory store holding a forged `</memory>` + `SYSTEM:` record, started with a required real-id file.
  Workflow attached through the space setting agentBuilder:prePromptWorkflowIds. Prompt contains a quote, a newline and
  Liquid braces. Runner: `python3 p2_live.py src-8d5425d 8d5425d` (runs 1 and 2 failed in the harness, not the product:
  a duplicate Content-Type header on the endpoint PUT, then title-generation calls counted as agent calls; both kept).

| Case | Memory service | Workflow | Converse | Seconds | What the agent's model call received |
|---|---|---|---|---|---|
| a | up | wrapped | 200 completed | 8.0 | `[Sent: ...]`, the delimiter-framed block (3 JSON records; the forged text only inside a JSON string), then the prompt verbatim |
| c2 | store sleeps 40 s (route budget 20 s) | wrapped | 200 completed | 27.3 | the original prompt, exactly |
| b | stopped (connection refused) | wrapped | 200 completed | 4.1 | the original prompt, exactly |
| d_b | stopped | unwrapped control | 500 `[ECONNREFUSED] ...:18741` | 5.6 | no agent model call |
| c | accepts, hangs 150 s (step timeout 25 s) | wrapped | 200 completed | 32.2 | the original prompt, exactly |
| d_c | hangs 150 s | unwrapped control | 500 `[ECONNABORTED] timeout of 60000ms exceeded` | 66.5 | no agent model call |

- What 9.5.6 supports, EXERCISED LIVE (direct workflow runs, evidence ev/keys/, and the table above): step `timeout`
  as a duration string (`25s`), enforced by the engine (TimeoutError; the step fails); `on-failure: continue: true` (the
  workflow completes after the step failed); `{{ x | default: inputs.prompt }}` (yields the input when the step
  produced no output); values returned by the http step are not re-rendered as Liquid. The http connector has its own
  60 s request timeout: d_c failed at that 60 s connector timeout (66.5 s), not at the 120 s hook limit.
- SCHEMA ONLY (present in @kbn/workflows/spec/schema.js, not run): `on-failure` `retry` and `fallback`, `continue` as a
  condition string, workflow-level `settings.timeout` and `settings.on-failure`.
- Kibana also makes one title-generation model call per conversation with the raw user prompt (forced `set_title`
  tool); it never carries the memory block.
- Cleanup verified: setting back to `[]`, the 8 workflows and the inference endpoint deleted, kit processes stopped,
  emlab-kb-16096 stopped again; emlab-es-16096 was already running and was left running. Ledger:
  ~/memcore-p2-16096/spawn_ledger.jsonl. A scan of the evidence for the Kibana key, service token and probe token found 0 hits.

## REQUIRED path: Basic-licence host integration, live run on t2-dev, 2026-11-16 (src d71da85; evidence t2-dev:~/memcore-p3-16096/ev/p3/d71da85/)
- Path: the client's own agent runtime calls `python3 -m memcore.hosts.basic_hook` (plain HTTP hook, fail-open, 25 s)
  -> `memcore.hosts.basic_service` POST /v1/pre_exec (route budget 20 s) over Elasticsearch -> the returned new_prompt
  goes to the client's model through memcore's ChatModel port (`OpenAICompatChat` -> ToolStub; no real model call).
  No Agent Builder, no Kibana, no ES inference API, no retriever.
- Node: a throwaway Elasticsearch 9.5.6 from the local image (`--pull never`), `xpack.license.self_generated.type=basic`
  (GET /_license: basic, active), single node on a Docker `--internal` network (no default route), GeoIP downloader
  off, ML off, disk watermarks off (host disk 93%). Deviation: security off (no credential involved; licence
  enforcement shown by the controls below). Server-side evidence: the node's HttpTracer (TRACE), es_trace.json.
- Capability probe at start-up (GET /_license only): this node -> tier basic, strategy client, no licensed features;
  the trial lab node emlab-es-16096 (read-only probe) -> tier enterprise, strategy auto, Agent Builder optional.
- Kit: work dir p3kit/ (copied to t2-dev:~/memcore-p3-16096/). In the recorded run the tracer was enabled by hand (a
  transient `logger.org.elasticsearch.http.HttpTracer: TRACE` cluster setting; `p3_es.sh up` now does this, the env var
  had no effect), followed by two hand checks (the 400 and 404 below).
- Runner: `p3_es.sh up; python3 p3_live.py src-d71da85 d71da85; docker logs emlab-p3es-16096 | python3 p3_trace.py ev/p3/d71da85; p3_es.sh down`.

| Case | Memory service | Client | Result | Seconds | What the model received |
|---|---|---|---|---|---|
| a | up (3 events indexed, one forged `</memory>` + `SYSTEM:`) | basic_hook | injected, reason ok | 0.3 | the delimiter-framed block (3 JSON records; the forged text only inside a JSON string), then the prompt verbatim |
| c2 | store sleeps 40 s (route budget 20 s) | basic_hook | not injected, route reason timeout | 20.0 | the original prompt, exactly |
| b | stopped (connection refused) | basic_hook | not injected, unreachable | 0.03 | the original prompt, exactly |
| d_b | stopped | naive urlopen control | raised URLError (connection refused) | 0.0 | no model call |
| c | accepts, hangs 150 s | basic_hook (25 s) | not injected, timeout | 25.0 | the original prompt, exactly |
| d_c | hangs 150 s | naive urlopen control (no timeout) | STILL BLOCKED at 130 s | 130 | no model call |

- No licence 403 on the Basic path: the tracer shows 10 requests before the controls marker, none FORBIDDEN and none
  under /_inference (8 OK: GET /_license x4, PUT index, _bulk, 2 x _search; plus my two pre-run tracer checks, one 400
  and one 404). memcore's own transport log agrees.
- Licensed controls on the same node, after the marker: retriever rrf search -> 403 "non-compliant for [Reciprocal Rank
  Fusion (RRF)]"; PUT _inference -> 403 "non-compliant for [inference]"; ElasticsearchSearch with strategy auto (no
  probe applied) -> 403 on its retriever probe, then client fusion. So the node does enforce Basic, and the no-403
  result above is not vacuous.
- Cleanup: memory services stopped (ledgered), container removed with its volume, network removed; ledger
  ~/memcore-p3-16096/spawn_ledger.jsonl. Secret scan of the evidence: 0 hits for the per-run service token found on disk
  and the lab key (only one of the two service token files was still on disk when the scan ran, and the stub token was deleted before
  the scan; a pattern scan for 43-character URL-safe strings, the shape of all three, over the 40 evidence files found 0); token file then deleted.

## REQUIRED path: Basic live rerun after review round 3, 2026-11-16 (src b3c364e; evidence t2-dev:~/memcore-p3-16096/ev/p3/b3c364e/)
- Same node recipe and kit as the d71da85 run, plus a seventh case. b3c364e adds the whole-call deadline, the exact
  framing check, the guard backstop in the API and bounded builds. Runs 1 and 2 at b3c364e failed in the kit, not the
  product (the client read the token from the service directory, which the gateway removes at stop; then the index
  was left from run 1); both are kept under ev/p3/run1-*, run2-*.

| Case | Server | Client | Result | Seconds | Model received |
|---|---|---|---|---|---|
| a | up | basic_hook | injected (exact grammar accepted) | 0.3 | framed block + prompt verbatim |
| c2 | store sleeps 40 s | basic_hook | route timeout | 20.0 | original prompt, exactly |
| b | stopped | basic_hook | unreachable | 0.0 | original prompt, exactly |
| d_b | stopped | naive urlopen | raised URLError | 0.0 | no model call |
| c | accepts, silent 150 s | basic_hook | timeout | 25.0 | original prompt, exactly |
| d_c | accepts, silent 150 s | naive urlopen, no timeout | STILL BLOCKED at 130 s | 130 | no model call |
| t | 200, then 1 byte per 0.5 s (400 bytes) | basic_hook | timeout; server saw the connection cut at 25.5 s after 50 bytes (BrokenPipe) | 25.0 | original prompt, exactly |
| d_t | same trickle | urlopen(timeout=25), per-read (the cfaefee behaviour) | STILL BLOCKED at 75 s | 75 | no model call |

- Server trace: 8 requests before the controls marker, none FORBIDDEN, none under /_inference. Controls after it:
  retriever rrf 403, PUT _inference 403, strategy auto 403. The first two GET /_license (licence.json and the
  standalone probe) answered 404 because the self-generated licence was not installed yet; the standalone probe fell
  back to basic by its error rule (probe_error BackendError), while the service's own start-up probe read basic,
  active. `p3_es.sh up` now waits for GET /_license 200.
- Cleanup: services stopped, container and network removed (ledgered). Secret scan of 45 files: 0 hits (token values
  and the lab key) and 0 token-shaped strings.

## Not done / caveats
- Three title-generation model requests also reached the stub (Kibana's own utility call); they are not part of the agent run.
- The advanced setting is space-wide; the per-agent `workflow_ids` route remains administrator-only for this key.

## Minor review findings: status after 888c94b (21 in the review; main asked for the 13 not addressed)
Addressed in 888c94b: 1 redirects, 2 0644 MCP config, 3 mcp_http CLI private host, 4 malformed MCP input and cap burn on unknown schema type, 6 turn id in delimiter header, 7 cache key settings, 8 counter reset on crash, 9 unknown usage to 0, 10 embeddings kind, 13 H3 rerun on HEAD, 19 tautological renderer control, 21 evidence overwrite.
Still open (one line each):
- 5 (partial): the hook-banner filter is now applied at read time and not cached, but the DEFAULT_HOOK_BANNER_PATTERNS can still delete legitimate text when switched on; patterns are untuned.
- 11: the "11 of 12" agreement metric is unchanged (lenient, near-duplicate prompts); only described honestly in NOTES.
- 12: fact-key overlap 0.149 vs 0.009 still measures free-text wording, not extraction quality; no better metric built.
- 14: the leak result has no contrast arm (no with-sentence versus without-sentence run on the same prompts).
- 15: NOTES.md line 32 still says no Elasticsearch search adapter was built (stale); not edited because existing files were frozen.
- 16: fusion parity corpus is still a 40-doc hash-vector toy, and the unit stub accepts any fusion.
- 17: the prefix-sensitivity check is not wired to an Elastic adapter in a unit test (live script only).
- 18 (partial): BOM handling is now tested, but ElasticInferenceChat itself has never run against the node (only the openai-service/gateway path and recorded bytes).
- 20: the schema k-of-20 cannot discriminate (all-required, extractable prompts) and hides repairs; the ES stub 20/20 is a regex.
My count is 8 open plus 2 partial among the 21; main's "13" may use a different split, so please map any item I have called addressed.

## Delta review of 888c94b: status on fix/adapters-delta-16096 (2026-11-15, local commits, unreviewed, nothing live)
- Fixed with regression tests (memcore/tests/test_delta16096.py): N1, N2 (gateway gate), N4 (egress guard), N7 (inbox
  fencing), N5 and N6 (hook framing and injector drops), N3, N8, N9, finding 8 residual (gateway 502s), MCP HTTP
  RecursionError (part of N10), minor 19 (escaping test), the vacuous item-7 and renderer-control tests.
- Behaviour changes callers must know: BudgetedInjector.char_budget is now the RENDERED size; InboxService.confirm needs
  the claim token (Claim.token), the default lease is 330 s (crash re-offer after 330 s, not 30 s) and renew() exists but no
  in-repo host calls it yet; every OpenAICompatChat is subscription-tainted unless operator_declares_not_subscription=True;
  FakeChat is refused on the bridge address (use ScriptedStubChat): memcore-out/g5/g5_run.py, which starts a bridge
  gateway over FakeChat, will now be refused; deliver() refuses a whole batch if one item leaks.
- Inbox API (reviewer item 1): InboxService.claim returns a Claim (a list subclass carrying .token); confirm(session, turn,
  token) needs that token and, since phase 2, RAISES ValueError without one (it used to return 0 silently); rows claimed
  before the fencing upgrade are confirmed with confirm_legacy(session, turn); renew() returns Renewal(renewed, lapsed,
  status) with status renewed / lapsed / done / superseded. Rollback: code older than 60b23d9 (N7 fencing) cannot read the fenced rows
  correctly (it ignores claim_token, fence and lease_until), so roll back only with an empty or drained inbox.
- Egress guard (reviewer item 5): tool results and other guarded objects of a type the guard does not know (datetime,
  Decimal, dataclass, any custom class) are refused as EgressLeak instead of being stringified; convert them to str,
  int, float, bool, None, list, tuple or dict before they reach a model.
- Phase 2 (after c20e208, tests in memcore/tests/test_phase2_16096.py, each failing on the c20e208 export): N10 schema
  keyword values typed and nesting capped at 32 before any spend (gateway answers 400 unsupported_schema); N11 every
  session ledger row carries a local session id from the first turn (backend_session added once known); embedders and
  rerankers check the egress guard on every input and spend one budgeted call first, and the remote ones refuse to run
  without a CallBudget; the gateway, mcp_http, mcp_server and prompt_hook entry points REQUIRE the real-id file
  (--real-ids or MEMCORE_REAL_IDS_FILE): the servers exit 2, the hook injects nothing.
- Still open: the eval runner on feat/eval-scaffold does not import memcore and keeps its own guard (eval/emlab/guard.py),
  so the memcore requirement does not reach it; AcpClient.request is public and uncounted; the injector behind
  /v1/pre_exec is caller-supplied and not gated (an injector whose store embeds through a remote model is counted by
  the embedder's own budget but not refused off loopback); class-level monkeypatching past the gate (LOW); and the
  minors listed above except 19.

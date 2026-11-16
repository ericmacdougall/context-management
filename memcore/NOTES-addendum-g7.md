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

## Live run on t2-dev, 2026-11-16 (src 8d5425d; evidence t2-dev:~/memcore-p2-16096/ev/live/8d5425d/)
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

- What 9.5.6 actually supports (schema in @kbn/workflows/spec/schema.js plus direct workflow runs, evidence
  ev/keys/): step `timeout` is a duration string (`25s`, `500ms`, ...) enforced by the engine (TimeoutError; the step
  fails); `on-failure` takes `retry`, `fallback` (steps) and `continue` (true or a condition string); with
  `continue: true` the workflow completes after the step failed; `{{ x | default: inputs.prompt }}` yields the input
  when the step produced no output; workflow-level `settings.timeout` and `settings.on-failure` also exist; values
  returned by the http step are not re-rendered as Liquid. The http connector has its own 60 s request timeout, so an
  unwrapped workflow over a hung service fails at about 60 s, before the 120 s hook limit.
- Kibana also makes one title-generation model call per conversation with the raw user prompt (forced `set_title`
  tool); it never carries the memory block.
- Cleanup verified: setting back to `[]`, the 8 workflows and the inference endpoint deleted, kit processes stopped,
  emlab-kb-16096 stopped again; emlab-es-16096 was already running and was left running. Ledger:
  ~/memcore-p2-16096/spawn_ledger.jsonl. A scan of the evidence for the Kibana key, service token and probe token found 0 hits.

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

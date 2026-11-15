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

## Workflow template and fail-open design (2026-11-15, fix branch fix/adapters-delta-16096; NOT RUN LIVE)
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
  120 s hook limit observed in variant 3. ASSUMPTION: these step keys and the Liquid `default` filter were not verified
  on 9.5.6. Observed so far, a failing step or a run past 120 s stops the agent run (variants 2 and 3).
- Not built: the HTTP route `POST /v1/pre_exec` that would serve `pre_exec_new_prompt()`. To verify live: build the
  route, then rerun the ok, refused-port and 140 s variants through converse and check that the model receives the framed
  block or, on failure, the original prompt.

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

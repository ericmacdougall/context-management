# memcore notes (GoodQA #16096, streams G2 G4 G5 G6, H3 H4 H6)

Local, unpublished. Python 3.9, standard library only in the core.

## Terms posture (hard limits until the user answers)
- ASSUMPTION: subscription-backed adapters (claude-cli, codex-cli, claude stream-json session, ACP) are development-only, one developer, one seat, unmodified CLIs, loopback. They never read or forward a login credential. They refuse to construct unless `dev_subscription_backends=True` is passed explicitly (`devgate.py`); the gateway refuses a subscription backend without it and refuses any non-loopback bind for one.
- Default route for customers: `ElasticInferenceChat` (inference API). An API-key adapter beyond `OpenAICompatChat(token=...)` is NOT built; no key was looked for or used.
- No third-party account-pool proxy was adopted. G5 with a subscription CLI behind the gateway is ON HOLD; the experiment was run with a stub backend only.

## MCP (checked 2026-11-15)
- Spec: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports  (latest revision 2026-07-28; stdio = newline-delimited JSON-RPC over the client-launched subprocess's streams; that revision carries protocol metadata per request and drops the connection-scoped initialize handshake, with a backward-compatibility section for earlier eras).
- Official Python SDK: `mcp` 2.3.0 on PyPI (2026-10-02), requires Python >= 3.10. This Mac has Python 3.9.6, so `mcp_server.py` is a hand-written stdio loop (about 80 lines): answers `initialize` (echoing the client's protocolVersion when known), `ping`, `tools/list`, `tools/call`, ignores notifications. It also answers tools/list and tools/call without a prior initialize.
- Tool results: the full result is in text content (Kibana ignores structuredContent per the research); structuredContent can be added later.
- Claude Code headless demo (2 turns of model work, verified 2026-11-15): from the neutral directory, with `memcore-out/mcp-demo.json` = `{"mcpServers":{"memcore":{"command":"python3","args":["-m","memcore.mcp_server"],"env":{"PYTHONPATH":"<worktree>"}}}}`:
  `claude -p --model sonnet --tools "" --strict-mcp-config --mcp-config <file> --allowedTools "mcp__memcore__query_facts,mcp__memcore__recall,mcp__memcore__open_event,mcp__memcore__timeline" --system-prompt "..." --no-session-persistence --output-format json`.
  Answer: "Now it's in Dublin (since 2026-03-02). On 2026-02-01 it was in Frankfurt." 3 turns, no permission denials. Tools are allowed by name, no permission bypass.
- Codex: the pool refuses MCP configuration (`codex-pool: config key mcp_servers.memcore.command is controlled by codex-pool`), so it was not shown. Official config format (config.toml): `[mcp_servers.memcore]` with `command`, `args`, `env`.

## ACP (Agent Client Protocol) - not the Agent Communication Protocol (IBM/BeeAI, folded into A2A)
- Spec: https://agentclientprotocol.com/protocol/overview (JSON-RPC 2.0, agents run as subprocesses over stdio; protocolVersion is an integer, 1; the v2 draft adds session/resume, list, fork, close). Methods used: initialize, session/new, session/set_config_option (claude adapter), session/prompt, session/update (agent_message_chunk, tool_call, usage_update), session/request_permission (answered `cancelled`), session/cancel.
- Claude Code: official adapter `@agentclientprotocol/claude-agent-acp` 0.107.0 (npm, 2026-11-13, Apache-2.0, built on the Claude Agent SDK, repo agentclientprotocol/claude-agent-acp; the agents page lists zed-industries/claude-agent-acp as the earlier home). Installed with `--ignore-scripts` into `memcore-out/acp/node_modules` (411 MB, includes the SDK's bundled claude). Integrity `sha512-yOEYkKkomupue/lmkjlLy4C8ADlZ8ZZXX1ry3Ka6VTjtWHXowyoQ6+awjrfAc1Xkq3f37GA5fpTz9URyIfcb4g==` (registry dist.integrity; also in package-lock.json there). It ran on the existing Claude login with no API key and an empty `authMethods`. agentCapabilities: loadSession true; session capabilities close, delete, fork, list, resume, options, status, systemPrompt override. MCP servers via session/new `mcpServers`. Defaults to Opus and mode `auto` with the full tool set (about 45k context per turn), so the adapter sets model and mode through `session/set_config_option`; permission requests are cancelled. Restricting to a given MCP server only was not verified (not measured; only the 2-call probe was run).
- Codex: official adapter `@agentclientprotocol/codex-acp` 2.6.0 (npm, 2026-11-08; the zed-industries repo is archived). It depends on its own `@openai/codex` and reads the user's own Codex login, which would bypass the sanctioned codex pool route, so it was NOT installed or run. Fallback for Codex multi-turn would be `codex exec resume`, which the pool refuses ("exec subcommands are not allowed through the pool"). So Codex has no multi-turn route here.

## Elastic (G3, G5): observed on the t2-dev trial node, Elasticsearch 9.5.6, trial licence
- `ElasticInferenceChat` posts to `/_inference/chat_completion/{id}/_stream`; strict body (messages, tools, tool_choice, temperature, max_completion_tokens only); structured output = one forced tool + client validation + one repair.
- Observed with an `openai`-service endpoint pointed at the memcore gateway (stub backend) on the Docker bridge address:
  - http:// to a private address works on 9.5.6 (PUT validation passed; no truststore needed).
  - The PUT-time validation request was `stream: true` with `stream_options.include_usage: true`, `n: 1`, message "how big" (the research said stream=false; observed otherwise; the gateway serves both).
  - SSE from `_stream`: a UTF-8 BOM before every `event: message` line, bare chunk JSON (no `chat_completion` wrapper), final chunk with `choices: []` and usage. Tool-call continuation deltas carry `"type": null`.
  - The embeddings request ES sends: `{input:[...], model, dimensions, encoding_format:"base64"}`; no `input_type`. The gateway returned base64 float32 and `_inference/text_embedding/{id}` worked.
  - Golden fixtures: `memcore/tests/fixtures/es956_*` and replay tests (`test_golden_es956`).
- Still not observed: the 9.6 wrapper, https with a private CA, Basic-licence behaviour (known elsewhere: 403), Agent Builder, `.mcp` connector, Elastic-side fusion. SearchBackend has only the in-memory fake (client-side RRF); no Elasticsearch adapter was built.

## Session cost (H3, measured 2026-11-15, sonnet, neutral cwd; see results/h3.json)
See the report; raw numbers in `memcore-out/results/h3.json`.
Hook leakage: the user's SessionStart hook injects operational text ("log backup is 50h old ...") that leaked into an answer in the first unguarded session probe. A system-prompt line telling the model to ignore session-start notices kept it out in 60 of 60 guarded turns without disabling any hook. `--no-session-persistence` leaves no transcript under `~/.claude/projects`, but each headless process still creates a `~/.claude/session-env/<id>` entry and a harness WAL directory under `~/.claude/wals/`. Nothing was deleted.

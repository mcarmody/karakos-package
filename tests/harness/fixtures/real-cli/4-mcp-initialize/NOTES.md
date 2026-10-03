# Q4: MCP tool servers and the initialize handshake

CLI: Claude Code 2.1.287, model `claude-haiku-4-5-20251001`, recorded 2026-10-02/03 on Linux (WSL2).
Command: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --strict-mcp-config --mcp-config <empty-or-stub> --setting-sources "" --permission-mode bypassPermissions --max-budget-usd 0.25 [--replay-user-messages]`
Run in a fresh temp dir. Files: `stdin.jsonl` (what we wrote, `t` = seconds since spawn), `stdout.jsonl` (every stdout event, `t` = seconds since spawn). Ids, paths and the account are scrubbed (`tools/scrub.py`). Sub-directories are variants with the same file names.

Stub stdio server (`tools/stub_mcp.py`) listed in `--mcp-config` with `--strict-mcp-config`. `mcp-server-io.jsonl` is the server's view (`dir: in` = received from the CLI; `t` = seconds since the first line the server received). Primary = a server that answers `server/discover` with JSON-RPC error -32601 (what a plain server does).

Observed handshake order:
1. t=0: **`server/discover`** (id `"server-discover-probe-1"`, `params._meta` with `io.modelcontextprotocol/protocolVersion: "2026-07-28"` and clientInfo `claude-code` 2.1.287). This is new and not in the 0.6 brief.
2. `initialize` (id 0), `protocolVersion: "2025-11-25"`, capabilities `roots.listChanged` and `elicitation`, clientInfo. Sent ~23 ms after the discover error. If `server/discover` gets **no reply it is abandoned after ~3.0 s** and `initialize` is sent anyway (silent variant: t=3.005).
3. After the `initialize` result: `notifications/initialized`, then `tools/list` (id 1). `tools/call` arrives later with `_meta.claudecode/toolUseId` and a `progressToken`.
4. The model reaches the tool via `ToolSearch` first (tools are deferred), then `mcp__stub__ping`.
- Variant `lenient-discover/`: server answers discover with `{"result":{}}`; the CLI still sends `initialize` as usual.
- Variant `silent-server/`: server never answers anything. Discover abandoned at 3 s, `initialize` sent, and **after 25 s with no reply the CLI sends `notifications/cancelled` (`requestId: 0`, reason `"SdkError: Request timed out"`)**. The first stdout event, `system`/`init`, is only emitted at t=28.4 (init waits for MCP connect/fail) with `mcp_servers: [{"name":"stub","status":"failed"}]` and no `mcp__stub__*` tools. The turn then runs normally without the tool (the model reports the server failed). So a hung `initialize` costs ~28 s of startup but is not fatal. In the connected case init is emitted at t≈0.4 with `status: "connected"`.
- Implication for 0.6: the server must answer `initialize` (any id type; the CLI uses integer 0) and should reply to unknown `server/discover` with an error or a result, not by staying silent.
